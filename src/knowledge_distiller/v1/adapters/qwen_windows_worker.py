"""Optional Windows worker: fixed CPU inference with real EOS qualification."""
import contextlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import wave


def has_repetition_run(text):
    """Flag five consecutive copies of a word or phrase for shorter decoding.

    This is an uncertainty signal, never an instruction to deduplicate.
    Tokenize Han characters individually so unspaced Chinese is covered too.
    The same conservative threshold applies to any word or phrase, not to a
    fixture vocabulary or an expected count. Shorter children still need EOS.
    """
    tokens = re.findall(r'[\u3400-\u9fff]|[^\W_]+', text.casefold())
    for start in range(len(tokens) - 4):
        for width in range(1, (len(tokens) - start) // 5 + 1):
            pattern = tokens[start:start + width]
            if all(tokens[start + n * width:start + (n + 1) * width] == pattern
                   for n in range(1, 5)):
                return True
    return False


def decode_source(processor, generated):
    """Keep model words; lossy post-processing is uncertainty, not evidence.

    Transformers' parsed decoder collapses repeated characters and phrases.
    Real speech can repeat, and an EOS alone does not qualify that output:
    the observed 30-word fixture generated 24 words before being reduced to 2.
    Return the raw transcription and ask the existing recovery path to
    subdivide whenever the processor would have changed its content.
    """
    text = processor.decode(generated, return_format='raw', skip_special_tokens=True)[0].strip()
    if text.startswith('assistant\n'):
        text = text[len('assistant\n'):]
    language = None
    prefix, marker, body = text.partition('<asr_text>')
    if marker and prefix.strip().lower().startswith('language '):
        language = prefix.strip()[len('language '):].strip()
        language = None if language.lower() == 'none' else language or None
        text = body.strip()
    parsed = processor.decode(generated, return_format='parsed')[0]
    return dict(text=text, language=language,
                postprocessing_changed_text=parsed['transcription'] != text)


def main():
    import runpy
    runpy.run_path(str(Path(__file__).with_name("python_policy.py")))["check_current"]()
    action, model, model_id, revision, runtime_version = sys.argv[1:6]
    if action == 'install':
        from huggingface_hub import snapshot_download
        snapshot_download(model_id, revision=revision, local_dir=model)
        return {'installed': True}
    os.environ['HF_HUB_OFFLINE'] = '1'
    import torch
    from transformers import AutoProcessor, Qwen3ASRForConditionalGeneration
    torch.set_num_threads(min(6, os.cpu_count() or 1))
    processor = AutoProcessor.from_pretrained(model, local_files_only=True)
    engine = Qwen3ASRForConditionalGeneration.from_pretrained(
        model, local_files_only=True, dtype=torch.float32, attn_implementation='eager').eval()
    chunks = []
    with wave.open(sys.argv[6], 'rb') as audio, tempfile.TemporaryDirectory() as temporary:
        if audio.getnchannels() != 1 or audio.getsampwidth() != 2 or audio.getframerate() != 16000:
            raise ValueError('expected_normalized_pcm16_mono_16k')
        total = audio.getnframes()
        for start in range(0, total, 20 * 16000):
            end = min(total, start + 20 * 16000)
            piece = Path(temporary) / 'chunk.wav'
            with wave.open(str(piece), 'wb') as out:
                out.setparams(audio.getparams())
                out.writeframes(audio.readframes(end - start))
            inputs = processor.apply_transcription_request(audio=str(piece)).to('cpu', torch.float32)
            with torch.inference_mode():
                ids = engine.generate(**inputs, max_new_tokens=1024, do_sample=False)
            generated = ids[:, inputs['input_ids'].shape[1]:]
            eos = engine.generation_config.eos_token_id
            eos = eos if isinstance(eos, list) else [eos]
            ended = bool(generated.shape[1]) and int(generated[0, -1]) in eos
            decoded = decode_source(processor, generated)
            uncertain = decoded['postprocessing_changed_text'] or has_repetition_run(decoded['text'])
            complete = ended and not uncertain
            reason = 'eos' if complete else ('repetition_requires_recovery' if ended else 'length')
            chunks.append(dict(text=decoded['text'], language=decoded['language'],
                               start=start / 16000, end=end / 16000,
                               finish_reason=reason, truncated=not complete))
    complete = bool(chunks) and all(not chunk['truncated'] for chunk in chunks)
    return dict(text=' '.join(c['text'] for c in chunks), language=chunks[0]['language'] if chunks else None,
                finish_reason='eos' if complete else next(
                    (c['finish_reason'] for c in chunks if c['truncated']), 'length'),
                truncated=not complete, chunks=chunks)


if __name__ == '__main__':
    try:
        with contextlib.redirect_stdout(sys.stderr):
            result = main()
        Path(sys.argv[-1]).write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    except Exception as error:
        print(type(error).__name__, file=sys.stderr)
        raise SystemExit(1)
