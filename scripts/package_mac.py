"""Archive a verified Mac bundle and its user guide without an installer."""
import argparse
import hashlib
import json
from pathlib import Path
import plistlib
import runpy
import shutil
import subprocess


def main():
    parser = argparse.ArgumentParser(description='制作本地验收用 Mac 文件包；运行时需已就绪的独立模型组件')
    parser.add_argument('--app', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--version', required=True)
    args = parser.parse_args()
    app = args.app.resolve()
    output = args.output.resolve()
    if not (app / 'Contents/MacOS/KnowledgeDistiller').is_file():
        parser.error('缺少构建完成的应用')
    project = Path(__file__).resolve().parents[1]
    info = plistlib.loads((app/'Contents/Info.plist').read_bytes())
    if args.version != info['CFBundleShortVersionString']:
        parser.error('--version 必须匹配包内产品版本')
    if output.exists() and any(output.iterdir()):
        parser.error('输出目录必须为空')
    output.mkdir(parents=True, exist_ok=True)
    subprocess.run(['codesign', '--verify', '--deep', '--strict', str(app)], check=True)
    name = '知识蒸馏器-' + args.version + '-macOS-arm64'
    folder = output / name
    folder.mkdir()
    subprocess.run(['ditto', str(app), str(folder / app.name)], check=True)
    guide = Path(__file__).resolve().parents[1] / 'packaging/开始使用.txt'
    (folder / guide.name).write_bytes(guide.read_bytes())
    with (app / 'Contents/Info.plist').open('rb') as source:
        minimum = plistlib.load(source)['LSMinimumSystemVersion']
    (folder / '版本信息.json').write_text(json.dumps({
        'version': args.version, 'build_version': info['CFBundleVersion'], 'platform': 'macOS', 'architecture': 'arm64',
        'minimum_macos': minimum, 'signing': info.get('KDCodeSigningMode', 'ad-hoc'), 'notarized': False,
        'qwen': 'optional', 'docling': 'included', 'docling_models': 'external-component-required',
    }, ensure_ascii=False, indent=2), encoding='utf-8')
    archive = output / (name + '.zip')
    subprocess.run(['ditto', '-c', '-k', '--sequesterRsrc', '--keepParent',
                    str(folder), str(archive)], check=True)
    digest = hashlib.file_digest(archive.open('rb'), 'sha256').hexdigest()
    (output / 'SHA256SUMS.txt').write_text(digest + '  ' + archive.name + '\n')
    # The archive is the deliverable; do not retain another unpacked app copy.
    shutil.rmtree(folder)
    print(archive)


if __name__ == '__main__':
    main()
