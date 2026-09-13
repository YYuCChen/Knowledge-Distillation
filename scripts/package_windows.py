"""Create a ZIP from an accepted frozen directory, never from the workspace."""
import argparse
import hashlib
from importlib.metadata import distributions
import json
from pathlib import Path
import shutil
import sys
import zipfile


def digest(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--app', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--cache-root', type=Path, required=True)
    parser.add_argument('--test-report', type=Path, required=True)
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    cache = args.cache_root.resolve()
    app = args.app.resolve()
    version = json.loads((app / '_internal/windows-version.json').read_text(encoding='utf-8'))
    manifest = json.loads((app.parent / 'build-manifest.json').read_text(encoding='utf-8'))
    if version['version'] != args.version or manifest.get('version') != args.version or manifest.get('git_head') != version['source_commit']:
        parser.error('Requested version, frozen metadata and source manifest differ')
    if manifest.get('status') != 'built-not-yet-accepted':
        parser.error('Frozen build did not complete successfully')

    if not (app / 'KnowledgeDistiller.exe').is_file():
        parser.error('Frozen Windows executable missing')
    for family in ('paddle-models',):
        if not (app / '_internal' / family / 'manifest.json').is_file():
            parser.error('Model inventory missing: ' + family)
    output = args.output.resolve()
    if output.exists() and any(output.iterdir()):
        parser.error('Use an empty delivery directory')
    output.mkdir(parents=True, exist_ok=True)
    release = output / '知识蒸馏器'
    shutil.copytree(app, release)
    shutil.copyfile(project / 'packaging/Windows使用说明.md', release / 'Windows使用说明.md')
    shutil.copyfile(project / 'packaging/Windows使用说明.md', release / '使用说明.txt')
    shutil.copyfile(project / 'packaging/Windows第三方组件.md', release / 'Windows第三方组件.md')
    shutil.copyfile(args.test_report, release / '测试与支持范围.md')
    shutil.copyfile(project / 'packaging/create_shortcut.vbs', release / '创建桌面快捷方式.vbs')
    shutil.copyfile(project / 'packaging/assets/app-icon.ico', release / 'app-icon.ico')
    notices = release / 'licenses'
    notices.mkdir()
    shutil.copytree(cache / 'model-licenses', notices / 'models')
    ffmpeg_sources = notices / 'FFmpeg-sources'
    ffmpeg_sources.mkdir()
    source_inventory = json.loads((cache / 'ffmpeg-sources/sources.json').read_text(encoding='utf-8'))
    selected_sources = [entry for entry in source_inventory['downloads'] if entry['file'].startswith('FFmpeg-')]
    for entry in selected_sources:
        source = cache / 'ffmpeg-sources' / entry['file']
        if digest(source) != entry['sha256']:
            raise ValueError('FFmpeg source checksum mismatch')
        shutil.copyfile(source, ffmpeg_sources / source.name)
    (ffmpeg_sources / 'sources.json').write_text(json.dumps(selected_sources, indent=2), encoding='utf-8')
    shutil.copyfile(project / 'packaging/windows-requirements-lock.txt', release / 'windows-requirements-lock.txt')
    build_manifest = app.parent / 'build-manifest.json'
    public_manifest = {key: manifest[key] for key in ('platform', 'architecture', 'python', 'git_head', 'version', 'product_version', 'packages', 'sources', 'status', 'python_inventory')}
    (release / 'build-manifest.json').write_text(json.dumps(public_manifest, indent=2), encoding='utf-8')
    inventory = {}
    for dist in distributions():
        name = dist.metadata.get('Name', 'unknown')
        inventory[name] = dist.version
        for resource in dist.files or []:
            if any(word in resource.name.lower() for word in ('license', 'copying', 'notice')) and '.dist-info' in str(resource):
                source = Path(dist.locate_file(resource))
                if source.is_file():
                    target = notices / name / str(resource).replace('\\', '/').split('.dist-info/', 1)[-1]
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, target)
    python_license = Path(sys.base_prefix) / 'LICENSE.txt'
    shutil.copyfile(python_license, notices / 'Python-LICENSE.txt')
    for source in (cache / 'tools/ffmpeg-github').rglob('*'):
        if source.is_file() and source.name.lower() in {'license', 'license.txt', 'readme.txt'}:
            shutil.copyfile(source, notices / ('FFmpeg-' + source.name))
    node_license = Path('C:/Program Files/nodejs/LICENSE')
    if node_license.is_file():
        shutil.copyfile(node_license, notices / 'Node-LICENSE.txt')
    (release / 'build-dependencies.json').write_text(json.dumps(inventory, indent=2), encoding='utf-8')
    hashes = {p.relative_to(release).as_posix(): digest(p) for p in sorted(release.rglob('*')) if p.is_file()}
    (release / 'FILES-SHA256.json').write_text(json.dumps(hashes, indent=2), encoding='utf-8')
    archive = output / ('KnowledgeDistiller-' + args.version + '-Windows-x64.zip')
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as zipout:
        for path in sorted(release.rglob('*')):
            if path.is_file():
                zipout.write(path, (Path(release.name) / path.relative_to(release)).as_posix())
    import runpy
    size_report = runpy.run_path(str(project/'scripts/check_release_sizes.py'))['check_files']([archive])
    (output/'size-check.json').write_text(json.dumps(size_report,indent=2),encoding='utf-8')
    (output / 'SHA256SUMS.txt').write_text(digest(archive) + '  ' + archive.name + '\n', encoding='ascii')
    print(archive)


if __name__ == '__main__':
    main()
