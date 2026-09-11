"""Download exactly the handed-off HF revisions; never resolve mutable main."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import urllib.request


def valid(path, entry):
    if not path.is_file() or path.stat().st_size != entry['size']:
        return False
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest() == entry['sha256']


def prepare(manifest_path, output):
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    revisions = {s['repo'].replace('/', '--'): s for s in manifest['sources'] if 'repo' in s}
    output.mkdir(parents=True, exist_ok=True)
    for name, entry in manifest['files'].items():
        path = output / name
        if not path.resolve().is_relative_to(output.resolve()):
            raise ValueError('Model path escapes output')
        if valid(path, entry):
            continue
        family, relative = name.split('/', 1)
        if family == 'RapidOcr':
            continue
        source = revisions[family]
        url = f"https://huggingface.co/{source['repo']}/resolve/{source['revision']}/{relative}"
        path.parent.mkdir(parents=True, exist_ok=True)
        print('Download', name, flush=True)
        temporary = path.with_suffix(path.suffix + '.part')
        with urllib.request.urlopen(url, timeout=120) as response, temporary.open('wb') as target:
            shutil.copyfileobj(response, target, 1024 * 1024)
        if not valid(temporary, entry):
            raise RuntimeError('Manifest checksum mismatch: ' + name)
        temporary.replace(path)
    if any(not valid(output / name, entry) for name, entry in manifest['files'].items() if name.startswith('RapidOcr/')):
        from importlib.metadata import version
        if version('rapidocr') != '3.9.2':
            raise RuntimeError('RapidOCR version mismatch')
        from docling.models.stages.ocr.rapid_ocr_model import RapidOcrModel
        RapidOcrModel.download_models(backend='onnxruntime', lang='ch', local_dir=output / 'RapidOcr', force=True)
    for name, entry in manifest['files'].items():
        if not valid(output / name, entry):
            raise RuntimeError('Manifest checksum mismatch: ' + name)
    (output / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    print('Verified', len(manifest['files']), 'model files', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    prepare(args.manifest, args.output)
