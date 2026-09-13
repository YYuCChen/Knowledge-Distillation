"""Create a fixed platform base; publishing is a separate verified operation."""
import argparse
import hashlib
import json
from pathlib import Path
import plistlib
import stat
import sys
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from knowledge_distiller.v1.program_tree import inventory, identity


def package(root, output, platform):
    files = inventory(root, platform)
    tree_id = identity(root, platform)
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f'base-{platform}-{tree_id}.zip'
    if platform == 'windows-x86_64':
        from knowledge_distiller.v1.windows_delta_v2 import build_payload
        build_payload(root, archive)
        version = json.loads((root / '_internal/windows-version.json').read_text())['version']
    else:
        version = plistlib.loads((root / 'Contents/Info.plist').read_bytes())['CFBundleVersion']
        with zipfile.ZipFile(archive, 'x', compression=zipfile.ZIP_DEFLATED) as target:
            for name, entry in sorted(files.items()):
                kind = entry['kind']
                info = zipfile.ZipInfo(name + ('/' if kind == 'directory' else ''),
                                       date_time=(2026, 1, 1, 0, 0, 0))
                info.create_system = 3
                mode = {'file': stat.S_IFREG, 'directory': stat.S_IFDIR, 'link': stat.S_IFLNK}[kind]
                info.external_attr = (mode | entry.get('mode', 0o777)) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                if kind != 'file':
                    target.writestr(info, entry['target'].encode() if kind == 'link' else b'')
                else:
                    with (root / name).open('rb') as source, target.open(info, 'w', force_zip64=True) as sink:
                        while block := source.read(1024**2):
                            sink.write(block)
        if identity(root, platform) != tree_id:
            raise ValueError('Program changed during base packaging')
    with archive.open('rb') as source:
        sha = hashlib.file_digest(source, 'sha256').hexdigest()
    descriptor = {'identity': tree_id, 'version': version, 'platform': platform,
        'archive': archive.name, 'sha256': sha, 'size': archive.stat().st_size,
        'unpacked_size': sum(entry.get('size', len(entry.get('target', '').encode())) for entry in files.values())}
    archive.with_suffix('.json').write_text(json.dumps(descriptor, indent=2))
    return descriptor


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--program', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--platform', choices=['macos-arm64', 'windows-x86_64'], required=True)
    args = parser.parse_args()
    print(json.dumps(package(args.program, args.output, args.platform), indent=2))
