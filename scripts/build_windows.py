"""Build only from the verified Windows environment and model inventories."""
import argparse
import hashlib
from importlib.metadata import distributions
import json
import os
from pathlib import Path
import platform
import runpy
import re
import subprocess
import sys


def source_hashes(project):
    files = [project / 'pyproject.toml', Path(__file__).resolve()]
    for folder in ('src', 'packaging', 'scripts'):
        files.extend(p for p in (project / folder).rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc')
    return {p.relative_to(project).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(files)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--product-version', required=True)
    parser.add_argument('--cache-root', type=Path, required=True)

    args = parser.parse_args()
    if sys.platform != 'win32' or platform.machine().lower() not in {'amd64', 'x86_64'}:
        parser.error('Build on Windows x64')
    if not re.fullmatch(r'\d{4}\.\d{2}\.\d{2}\.\d+', args.version):
        parser.error('Expected build number YYYY.MM.DD.N')
    if not re.fullmatch(r'\d+(?:\.\d+){0,3}', args.product_version):
        parser.error('Expected numeric product version')
    project = Path(__file__).resolve().parents[1]
    cache = args.cache_root.resolve()

    output = args.output.resolve()
    if output.exists() and any(output.iterdir()):
        parser.error('Use an empty output directory')
    output.mkdir(parents=True, exist_ok=True)
    models = cache / 'docling-models'
    paddle = cache / 'paddle-models'
    metadata = output / 'build-input' / 'windows-version.json'
    metadata.parent.mkdir()
    head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=project, text=True).strip()
    dirty = subprocess.check_output(['git', 'status', '--porcelain'], cwd=project, text=True).strip()
    if dirty:
        raise RuntimeError('Build requires a clean committed source snapshot')
    update_config=json.loads((project/'packaging/update_config.json').read_text())
    metadata.write_text(json.dumps({'feed_url': update_config['feed_url'].replace('appcast.xml','appcast-windows.xml'),
                                   'public_key':update_config['public_key'], 'version': args.version, 'product_version': args.product_version,
                                   'source_commit': head}), encoding='utf-8')
    env = dict(os.environ, KD_BUILD_WINDOWS_CACHE=str(cache), KD_BUILD_WINDOWS_VERSION=str(metadata), KD_BUILD_DOCLING_MODELS=str(models), KD_BUILD_PADDLE_MODELS=str(paddle),
               PYINSTALLER_CONFIG_DIR=str(output / 'pyinstaller-config'),
               PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
    manifest = dict(platform=platform.platform(), architecture=platform.machine(), python=platform.python_version(),
                    git_head=head, version=args.version, product_version=args.product_version,
                    packages={d.metadata['Name']: d.version for d in distributions()},
                    sources=source_hashes(project),
                    status='building')
    try:
        models = cache / 'docling-models'
        runpy.run_path(str(project / 'packaging/docling_models.py'))['model_datas'](models)
        paddle = cache / 'paddle-models'
        inventory = json.loads((paddle / 'manifest.json').read_text(encoding='utf-8'))
        for name, entry in inventory['files'].items():
            path = paddle / name
            if not path.resolve().is_relative_to(paddle.resolve()):
                raise ValueError('Paddle model path escapes root')
            with path.open('rb') as source:
                digest = hashlib.file_digest(source, 'sha256').hexdigest()
            if digest != entry['sha256'] or path.stat().st_size != entry['size']:
                raise ValueError('Paddle model checksum mismatch: ' + name)
        with (output / 'build.log').open('w', encoding='utf-8') as log:
            subprocess.run([sys.executable, '-m', 'PyInstaller', '--clean', '--noconfirm', '--distpath', str(output),
                            '--workpath', str(output / 'pyinstaller'),
                            str(project / 'packaging/KnowledgeDistillerWindows.spec')],
                           cwd=project, env=env, stdout=log, stderr=subprocess.STDOUT, check=True)
        if source_hashes(project) != manifest['sources']:
            manifest['status'] = 'source-changed-during-build'
            raise RuntimeError('Build inputs changed; rebuild before release')
        manifest['status'] = 'built-not-yet-accepted'
    except Exception as error:
        if manifest['status'] == 'building':
            manifest['status'] = 'failed'
        manifest['error'] = type(error).__name__ + ': ' + str(error)
        raise
    finally:
        (output / 'build-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
