"""Process boundary for the installed OpenCLI raw readers; no CLI display data."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

from .chrome import ChromeSessionError


def read_opencli(script, platform, url, context=None, *, endpoint=None):
    node = shutil.which('node')
    bundled = Path(getattr(sys, '_MEIPASS', '/nonexistent')) / 'opencli'
    if getattr(sys, 'frozen', False) and bundled.is_dir():
        root = bundled
    else:
        executable = shutil.which('opencli')
        if not executable:
            raise ChromeSessionError(platform + '_runtime_unavailable')
        root = Path(executable).resolve()
        while root.parent != root and not (root / 'dist/src/browser/page.js').is_file():
            root = root.parent
    if not node or not (root / 'dist/src/browser/page.js').is_file():
        raise ChromeSessionError(platform + '_runtime_unavailable')
    try:
        result = subprocess.run(
            [node, str(Path(__file__).parent / 'adapters' / (script + '.mjs')), str(root), url, context or '', *([endpoint] if endpoint else [])],
            capture_output=True, text=True, encoding='utf-8', timeout=90)
        value = json.loads(result.stdout)
        if not isinstance(value, dict):
            raise ValueError
        code = value.get('error')
        if isinstance(code, str) and code.startswith(platform + '_'):
            raise ChromeSessionError(code)
        if result.returncode or code:
            raise ValueError
        return value
    except (OSError, subprocess.TimeoutExpired, ValueError) as error:
        raise ChromeSessionError(platform + '_upstream_failed') from error
