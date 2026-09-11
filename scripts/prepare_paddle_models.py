"""Prepare the two deployed small OCR models and record every shipped byte."""
import argparse
import hashlib
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    from paddlex import create_model
    from knowledge_distiller.v1.ocr import DETECTION_MODEL, RECOGNITION_MODEL
    import shutil
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    for name in (DETECTION_MODEL, RECOGNITION_MODEL):
        predictor = create_model(name, device='cpu', engine_config={'run_mode':'paddle', 'cpu_threads':2})
        source = Path(predictor.model_dir)
        shutil.copytree(source, output / name, dirs_exist_ok=True)
    files = {}
    for path in sorted(output.rglob('*')):
        if path.is_file() and path.name != 'manifest.json':
            with path.open('rb') as source:
                digest = hashlib.file_digest(source, 'sha256').hexdigest()
            files[path.relative_to(output).as_posix()] = dict(size=path.stat().st_size, sha256=digest)
    (output / 'manifest.json').write_text(json.dumps(dict(models=[DETECTION_MODEL, RECOGNITION_MODEL], files=files), indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
