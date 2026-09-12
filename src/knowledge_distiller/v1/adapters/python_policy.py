"""Single Python contract shared by builders, frozen probes and isolated workers."""
import platform
import sys

PYTHON_VERSION = '3.11.16'
PYTHON_SERIES = (3, 11)
RUNTIMES = {
    'mac': {
        'url': 'https://github.com/astral-sh/python-build-standalone/releases/download/20260901/cpython-3.11.16%2B20260901-aarch64-apple-darwin-install_only_stripped.tar.gz',
        'sha256': '768f05cf200273bbdda9a5955a5a6892a4b22f2a0b1e4b0a9160f5c7fce86816',
    },
    'windows': {
        'url': 'https://github.com/astral-sh/python-build-standalone/releases/download/20260901/cpython-3.11.16%2B20260901-x86_64-pc-windows-msvc-install_only_stripped.tar.gz',
        'sha256': '06cbe479e039f5b9cb5640c286d790074d63f549f92a32d599a3748293bd4510',
    },
}


def check_current():
    record = dict(version=platform.python_version(), executable=sys.executable,
                  prefix=sys.prefix, implementation=platform.python_implementation())
    if record['version'] != PYTHON_VERSION or record['implementation'] != 'CPython':
        raise RuntimeError('Python runtime mismatch: expected CPython ' + PYTHON_VERSION + ', got ' + str(record))
    return record


def bundle_inventory(root):
    """Reject foreign CPython binaries, including abandoned ABI extensions."""
    import re
    names = []
    for path in root.rglob('*'):
        if not path.is_file():
            continue
        name = path.name.lower()
        matches = re.findall(r'(?:python3\.?|cpython-3|\.cp3)(\d{2})(?=[\W_]|$)', name)
        if any(value != '11' for value in matches):
            raise RuntimeError('Foreign Python runtime in bundle: ' + str(path.relative_to(root)))
        if name in {'python311.dll', 'libpython3.11.dylib', 'python', 'python3', 'python3.11'} or matches:
            names.append(path.relative_to(root).as_posix())
    if not names:
        raise RuntimeError('No Python runtime found in bundle')
    return sorted(names)
