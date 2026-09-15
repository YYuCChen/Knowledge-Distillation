"""Native self-contained bootstrap build; does not use any system Python at install time."""
import argparse
import json
import os
from pathlib import Path
import platform
import plistlib
import runpy
import subprocess
import sys


def main():
    project = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(project / 'src'))
    from knowledge_distiller.v1.adapters.python_policy import check_current
    runtime = check_current()
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--tools', type=Path, required=True)
    parser.add_argument('--version', required=True, help='Same build number as the candidate request')
    parser.add_argument('--product-version', required=True, help='Same product version as the candidate request')
    parser.add_argument('--signing-config', type=Path)
    args = parser.parse_args()
    version_api = runpy.run_path(str(project / 'packaging/version_metadata.py'))
    native_version = version_api['native_versions'](args.product_version, args.version)
    if args.output.exists() and any(args.output.iterdir()):
        parser.error('输出目录必须为空。')
    args.output.mkdir(parents=True, exist_ok=True)
    windows = sys.platform == 'win32'
    if not windows and (sys.platform != 'darwin' or platform.machine() != 'arm64'):
        parser.error('请使用目标平台的原生构建环境。')
    tool = args.tools / ('hpatchz.exe' if windows else 'BinaryDelta')
    if not tool.is_file():
        parser.error('缺少固定版本差量解码器。')
    resources = project / 'src/knowledge_distiller/v1/adapters'
    resource_api = runpy.run_path(str(project / 'packaging/resources.py'))
    icon = project / 'packaging/assets' / ('installer-icon.ico' if windows else 'installer-icon.icns')
    if not icon.is_file():
        parser.error('缺少安装器图标；先在 Mac 上运行 scripts/build_installer_icon.py。')
    command = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--noupx', '--onefile',
        '--name', 'KnowledgeDistillerInstaller', '--icon', str(icon), '--paths', str(project / 'src'),
        '--distpath', str(args.output / 'dist'), '--workpath', str(args.output / 'work'),
        '--specpath', str(args.output), '--add-binary', str(tool) + os.pathsep + 'tools']
    for source, destination in [
        (project / 'packaging/update_config.json', 'knowledge_distiller/v1/adapters'),
        (resources / 'update-codec-notices.txt', 'knowledge_distiller/v1/adapters'),
        (resources / 'docling-model-notices.txt', 'knowledge_distiller/v1/adapters'),
        (resources / 'python-runtime.json', 'knowledge_distiller/v1/adapters'),
        (resources / 'docling-models-manifest.json', 'knowledge_distiller/v1/adapters')]:
        command += ['--add-data', str(source) + os.pathsep + destination]
    for source, destination in resource_api['installer_datas'](project):
        command += ['--add-data', source + os.pathsep + destination]
    for module in resource_api['installer_hiddenimports'](sys.platform):
        command += ['--hidden-import', module]
    if windows:
        version_file = args.output / 'installer-version.txt'
        version_file.write_text(version_api['windows_version_resource'](args.product_version, args.version), encoding='utf-8')
        command += ['--version-file', str(version_file)]
    if not windows:
        command += ['--windowed', '--osx-bundle-identifier', 'local.knowledge-distiller.installer']
    command += [str(project / 'packaging/component_installer_entry.py')]
    with (args.output / 'build.log').open('w') as log:
        subprocess.run(command, cwd=project, stdout=log, stderr=subprocess.STDOUT, check=True)
    artifact = args.output / 'dist' / ('KnowledgeDistillerInstaller.exe' if windows else 'KnowledgeDistillerInstaller.app')
    if not windows:
        info_path = artifact / 'Contents/Info.plist'
        info = plistlib.loads(info_path.read_bytes())
        info.update(native_version)
        info_path.write_bytes(plistlib.dumps(info))
        signing = runpy.run_path(str(project / 'packaging/mac_signing.py'))
        config = json.loads(args.signing_config.read_text()) if args.signing_config else {}
        signing['sign_bundle'](artifact, config)
        subprocess.run(['codesign', '--verify', '--deep', '--strict', str(artifact)], check=True)
    (args.output / 'build-manifest.json').write_text(json.dumps({
        'python': runtime, 'platform': sys.platform, 'artifact': str(artifact),
        'version': args.version, 'product_version': args.product_version,
        'source_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=project, text=True).strip(),
        'source_dirty': bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=project, text=True).strip())}, indent=2))
    print(artifact)


if __name__ == '__main__':
    main()
