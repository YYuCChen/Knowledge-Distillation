"""Runs only in the optional component's Python, never in the base application."""
import contextlib
import json
import os
from pathlib import Path
import sys


def main():
    import runpy
    runpy.run_path(str(Path(__file__).with_name("python_policy.py")))["check_current"]()
    action, model, model_id, revision, runtime_version = sys.argv[1:6]
    from importlib.metadata import version
    if version('mlx-qwen3-asr') != runtime_version:
        raise RuntimeError('runtime_version_mismatch')
    if action == 'install':
        from huggingface_hub import snapshot_download, HfApi
        # Metadata only; failure does not block the actual resumable download.
        progress=Path(model).parent.parent/'model-download.json'
        try:
            info=HfApi().model_info(model_id,revision=revision,files_metadata=True,timeout=8)
            sizes=[entry.size for entry in info.siblings]
            total=sum(sizes) if sizes and all(isinstance(size,int) for size in sizes) else None
            temporary=progress.with_suffix('.tmp')
            temporary.write_text(json.dumps({'total_bytes':total}))
            temporary.replace(progress)
        except Exception:
            with contextlib.suppress(OSError):progress.unlink()
        snapshot_download(repo_id=model_id, revision=revision, local_dir=model)
        return {'installed': True}
    if action != 'transcribe':
        raise ValueError('unknown_action')
    # The caller also sets offline env. No implicit first-use download or fallback.
    os.environ['HF_HUB_OFFLINE'] = '1'
    from mlx_qwen3_asr import transcribe
    result = transcribe(Path(sys.argv[6]), model=model, language=None,
                        return_chunks=True, return_timestamps=False,
                        forced_aligner=None, verbose=False)
    return {key: getattr(result, key, None) for key in
            ('text', 'language', 'finish_reason', 'truncated', 'chunks')}


if __name__ == '__main__':
    try:
        with contextlib.redirect_stdout(sys.stderr):
            result = main()
        # A file keeps transcript text out of ordinary process logs.
        Path(sys.argv[-1]).write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    except Exception as error:
        print(type(error).__name__, file=sys.stderr)
        raise SystemExit(1)
