"""Release data manifest. No imports of PyInstaller or application runtime."""
from pathlib import Path
import tomllib


def application_datas(project: Path) -> list[tuple[str, str]]:
    """Use the same explicit resource list as the Python package."""
    config = tomllib.loads((project / 'pyproject.toml').read_text())
    patterns = config['tool']['setuptools']['package-data']['knowledge_distiller']
    package = project / 'src' / 'knowledge_distiller'
    files = {path for pattern in patterns for path in package.glob(pattern) if path.is_file()}
    return [(str(path), str(Path('knowledge_distiller') / path.relative_to(package).parent))
            for path in sorted(files)]


def opencli_datas(root: Path) -> list[tuple[str, str]]:
    """Preserve JS runtime/dependency trees, omit upstream development assets.

    Keep clis and node_modules intact: adapters use package self-exports and
    dynamic imports. Their licenses and runtime data must travel with them.
    """
    required = ('package.json', 'LICENSE', 'cli-manifest.json',
                'dist/src/browser/page.js', 'dist/src/browser/daemon-transport.js',
                'dist/src/registry.js', 'clis/zhihu/auth.js',
                'clis/weibo/auth.js', 'clis/twitter/thread.js')
    for name in required:
        if not (root / name).is_file():
            raise RuntimeError(f'OpenCLI release resource missing: {name}')
    files = {root / name for name in ('package.json', 'LICENSE', 'cli-manifest.json')}
    for folder in ('dist', 'clis', 'node_modules'):
        files.update(path for path in (root / folder).rglob('*') if path.is_file()
                     and not (folder == 'dist' and path.name.endswith(('.test.js', '.map', '.d.ts'))))
    return [(str(path), str(Path('opencli') / path.relative_to(root).parent))
            for path in sorted(files)]
