"""Run transport/reader regressions with the production Node runtime."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_owned_navigation_and_zhihu_reader():
    if not shutil.which('node'):
        pytest.skip('Node runtime unavailable')
    result = subprocess.run(
        ['node', '--test', str(Path(__file__).with_name('owned_zhihu.test.mjs'))],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
