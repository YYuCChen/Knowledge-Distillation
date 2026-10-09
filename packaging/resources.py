"""Release data manifest. No imports of PyInstaller or application runtime."""
from pathlib import Path
import tomllib


def application_datas(project: Path) -> list[tuple[str, str]]:
    """Use the same explicit resource list as the Python package."""
    config = tomllib.loads((project / 'pyproject.toml').read_text())
    patterns = config['tool']['setuptools']['package-data']['knowledge_distiller']
    package = project / 'src' / 'knowledge_distiller'
    files = {path for pattern in patterns for path in package.glob(pattern) if path.is_file()}
    return [(str(path), (Path('knowledge_distiller') / path.relative_to(package).parent).as_posix())
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
    return [(str(path), (Path('opencli') / path.relative_to(root).parent).as_posix())
            for path in sorted(files)]


def installer_datas(project: Path) -> list[tuple[str, str]]:
    """The installer reads these files by package-relative paths at runtime."""
    root = project / 'src/knowledge_distiller/v1/installer_assets'
    files = [root / name for name in ('page.html', 'installer-logo.svg')]
    for path in files:
        if not path.is_file():
            raise RuntimeError(f'Installer release resource missing: {path.name}')
    return [(str(path), 'knowledge_distiller/v1/installer_assets') for path in files]


def installer_hiddenimports(platform: str) -> list[str]:
    """Shared installer/helper bridge inputs; no imports or platform side effects."""
    common = ['knowledge_distiller.v1.component_attempt',
              'knowledge_distiller.v1.installer_platform',
              'knowledge_distiller.v1.install_problem']
    if platform == 'win32':
        return common + ['win32job', 'pythoncom', 'pywintypes', 'win32gui',
                         'win32com.shell.shell', 'win32com.shell.shellcon', 'win32com.client']
    if platform == 'darwin':
        return common + ['AppKit', 'Foundation']
    raise ValueError('Unsupported installer target platform')
