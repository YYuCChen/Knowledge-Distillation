"""Source-only release checks: never build/install an app or start a worker."""
import ast
import importlib.util
from pathlib import Path
import runpy
import subprocess
import sys

PROJECT = Path(__file__).resolve().parents[2]
RESOURCES = runpy.run_path(str(PROJECT / 'packaging/resources.py'))


def test_production_imports_never_reach_legacy():
    package = PROJECT / 'src' / 'knowledge_distiller'
    for path in package.rglob('*.py'):
        if 'legacy' in path.relative_to(package).parts:
            continue
        relative = path.relative_to(package).with_suffix('')
        module = 'knowledge_distiller.' + '.'.join(relative.parts)
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                imports = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if relative.parts[0] == 'v1' and (node.module or '').startswith('knowledge_distiller.'):
                    assert all(not alias.name.startswith('_') for alias in node.names), (path, node.module)
                parent = module.rsplit('.', 1)[0]
                name = importlib.util.resolve_name('.' * node.level + (node.module or ''), parent) if node.level else node.module
                imports = [name or '', *[(name or '') + '.' + alias.name for alias in node.names]]
            else:
                continue
            assert not any(name.startswith('knowledge_distiller.legacy') for name in imports), path
    result = subprocess.run([sys.executable, '-c',
        'import knowledge_distiller, sys; '
        'assert not any(n.startswith("knowledge_distiller.legacy") for n in sys.modules); '
        'assert "flask" not in sys.modules'], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_release_resources_include_every_v1_asset_and_no_legacy():
    datas = RESOURCES['application_datas'](PROJECT)
    files = {Path(source) for source, _ in datas}
    package = PROJECT / 'src/knowledge_distiller'
    for folder in ('templates', 'static', 'adapters'):
        expected = {p for p in (package / 'v1' / folder).rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc'}
        assert expected <= files
    assert all('legacy' not in Path(source).parts for source, _ in datas)
    assert all(destination.startswith('knowledge_distiller/v1/') for _, destination in datas)


def test_opencli_manifest_keeps_runtime_and_excludes_development_assets(tmp_path):
    names = ['package.json', 'LICENSE', 'cli-manifest.json',
        'dist/src/browser/page.js', 'dist/src/browser/daemon-transport.js',
        'dist/src/registry.js', 'clis/zhihu/auth.js', 'clis/weibo/auth.js',
        'clis/twitter/thread.js', 'node_modules/dependency/runtime.json',
        'node_modules/dependency/LICENSE', 'dist/src/page.test.js',
        'dist/src/page.js.map', 'dist/src/page.d.ts', 'skills/SKILL.md',
        'scripts/postinstall.js', 'README.md', '.DS_Store']
    for name in names:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('fixture')
    selected = {Path(source).relative_to(tmp_path).as_posix() for source, _ in RESOURCES['opencli_datas'](tmp_path)}
    assert selected == set(names[:11])
    (tmp_path / 'clis/zhihu/auth.js').unlink()
    import pytest
    with pytest.raises(RuntimeError, match='clis/zhihu/auth.js'):
        RESOURCES['opencli_datas'](tmp_path)


def test_production_composition_imports_without_legacy_sources(tmp_path):
    import shutil
    source = PROJECT / 'src/knowledge_distiller'
    shutil.copytree(source, tmp_path / 'knowledge_distiller',
                    ignore=shutil.ignore_patterns('legacy', '__pycache__'))
    result = subprocess.run([sys.executable, '-I', '-c',
        'import sys; sys.path.insert(0, sys.argv[1]); '
        'import knowledge_distiller.v1.app, knowledge_distiller.v1.mac_app; '
        'assert not any(n.startswith("knowledge_distiller.legacy") for n in sys.modules)',
        str(tmp_path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_installed_opencli_manifest_imports_in_isolation(tmp_path):
    import json
    import shutil
    import pytest
    executable = shutil.which('opencli')
    node = shutil.which('node')
    if not executable or not node:
        pytest.skip('Requires installed OpenCLI and Node; no browser connection is used')
    root = Path(executable).resolve()
    while not (root / 'dist/src/browser/page.js').is_file():
        if root.parent == root:
            pytest.fail('Installed OpenCLI runtime root was not found')
        root = root.parent
    for source, destination in RESOURCES['opencli_datas'](root):
        target = tmp_path / destination / Path(source).name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    modules = ['dist/src/browser/page.js', 'dist/src/browser/daemon-transport.js',
               'dist/src/registry.js', 'clis/zhihu/auth.js',
               'clis/weibo/auth.js', 'clis/twitter/thread.js']
    script = ';'.join('await import(' + json.dumps((tmp_path / 'opencli' / name).as_uri()) + ')'
                      for name in modules)
    result = subprocess.run([node, '--input-type=module', '-e', script], cwd=tmp_path,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
