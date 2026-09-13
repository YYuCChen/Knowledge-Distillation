"""Single Python contract shared by builders, frozen probes and isolated workers."""
import platform
import sys

import json
from pathlib import Path

CONTRACT = json.loads(Path(__file__).with_name('python-runtime.json').read_text(encoding='utf-8'))
PYTHON_VERSION = CONTRACT['version']
PYTHON_SERIES = tuple(map(int, PYTHON_VERSION.split('.')[:2]))
RUNTIMES = CONTRACT['runtimes']


def check_current():
    record = dict(version=platform.python_version(), executable=sys.executable,
                  prefix=sys.prefix, architecture=platform.machine(), implementation=platform.python_implementation())
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
