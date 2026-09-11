"""Process boundary for the installed OpenCLI raw readers; no CLI display data."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .chrome import ChromeSessionError


def read_opencli(script, platform, url, context=None, *, endpoint=None):
    node = shutil.which('node')
    bundle_root = Path(getattr(sys, '_MEIPASS', '/nonexistent'))
    bundled = bundle_root / 'opencli'
    if getattr(sys, 'frozen', False) and bundled.is_dir():
        root = bundled
        bundled_node = bundle_root / 'bin' / ('node.exe' if sys.platform == 'win32' else 'node')
        node = str(bundled_node) if bundled_node.is_file() else None
    else:
        configured = os.environ.get('KNOWLEDGE_DISTILLER_OPENCLI')
        executable = shutil.which('opencli')
        candidates = [Path(configured)] if configured else []
        if executable:
            base = Path(executable).resolve()
            candidates += [base, base.parent / 'node_modules/@jackwener/opencli']
            while base.parent != base:
                base = base.parent
                candidates.append(base)
        if sys.platform == 'win32':
            candidates.append(Path(__file__).resolve().parents[3] /
                              '.windows-build/tools/opencli/node_modules/@jackwener/opencli')
        root = next((path for path in candidates if (path / 'dist/src/browser/page.js').is_file()), None)
        if root is None:
            raise ChromeSessionError(platform + '_runtime_unavailable')
    if not node or not (root / 'dist/src/browser/page.js').is_file():
        raise ChromeSessionError(platform + '_runtime_unavailable')
    try:
        result = subprocess.run(
            [node, str(Path(__file__).parent / 'adapters' / (script + '.mjs')), str(root), url, context or '', *([endpoint] if endpoint else [])],
            capture_output=True, text=True, encoding='utf-8', timeout=90,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
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
