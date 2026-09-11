"""Optional Windows worker: fixed CPU inference with real EOS qualification."""
import contextlib
import json
import os
from pathlib import Path
import sys
import tempfile
import wave


def main():
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
            complete = bool(generated.shape[1]) and int(generated[0, -1]) in eos
            parsed = processor.decode(generated, return_format='parsed')[0]
            chunks.append(dict(text=parsed['transcription'], language=parsed.get('language'),
                               start=start / 16000, end=end / 16000,
                               finish_reason='eos' if complete else 'length', truncated=not complete))
    complete = bool(chunks) and all(not chunk['truncated'] for chunk in chunks)
    return dict(text=' '.join(c['text'] for c in chunks), language=chunks[0]['language'] if chunks else None,
                finish_reason='eos' if complete else 'length', truncated=not complete, chunks=chunks)


if __name__ == '__main__':
    try:
        with contextlib.redirect_stdout(sys.stderr):
            result = main()
        Path(sys.argv[-1]).write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    except Exception as error:
        print(type(error).__name__, file=sys.stderr)
        raise SystemExit(1)
