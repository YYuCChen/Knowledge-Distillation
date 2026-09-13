"""Build one deterministic, platform-independent document-model asset."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from knowledge_distiller.v1.docling_component import DoclingComponent


def package(source, output):
    component = DoclingComponent(output)
    component.verify(source)
    output.mkdir(parents=True, exist_ok=True)
    archive = output / ('docling-' + component.identity + '.zip')
    # Exclusive creation prevents silently replacing an already signed asset.
    with zipfile.ZipFile(archive, 'x', compression=zipfile.ZIP_DEFLATED) as target:
        for name in sorted(component.files):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            with (source / name).open('rb') as src, target.open(info, 'w', force_zip64=True) as dst:
                digest = hashlib.sha256()
                count = 0
                while block := src.read(1024 * 1024):
                    digest.update(block)
                    count += len(block)
                    dst.write(block)
                entry = component.files[name]
                if digest.hexdigest() != entry['sha256'] or count != entry['size']:
                    raise ValueError('Model changed during packaging: ' + name)
    with archive.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    descriptor = {'component': 'docling', 'identity': component.identity,
                  'platforms': ['macos-arm64', 'windows-x86_64'],
                  'archive': archive.name, 'size': archive.stat().st_size,
                  'sha256': digest, 'unpacked_size': sum(e['size'] for e in component.files.values())}
    archive.with_suffix('.json').write_text(json.dumps(descriptor, indent=2) + '\n', encoding='utf-8')
    return descriptor


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(package(args.source, args.output), indent=2))
