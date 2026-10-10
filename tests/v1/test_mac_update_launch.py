import plistlib
import subprocess
from types import SimpleNamespace

import pytest

from knowledge_distiller.v1 import mac_update_launch as module


def test_launch_uses_independent_gui_job_and_quotes_paths(tmp_path, monkeypatch):
    root = tmp_path / '中文 space'; root.mkdir()
    plan = root / 'plan.json'; plan.write_text('{}')
    calls = []
    monkeypatch.setattr(module.subprocess, 'run', lambda args, **kw: calls.append(args))
    handle = module.launch(root / 'helper', plan, root / 'log')
    assert calls[0][:2] == ['/bin/launchctl', 'bootstrap']
    config = plistlib.loads(next(root.glob('*.plist')).read_bytes())
    assert config['RunAtLoad'] and config['AbandonProcessGroup']
    assert config['ProgramArguments'][2:4] == [str(root/'helper'), str(plan)]
    assert config['ProgramArguments'][-1] == handle.label
    script = next(root.glob('*.sh')).read_text()
    assert '"$1" --component "$2"' in script
    assert script.index('/bin/mv') < script.index('/bin/launchctl bootout')
    handle.result.write_text('0\n')
    assert handle.wait() == 0


def test_failed_job_start_keeps_application_usable(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0])
    monkeypatch.setattr(module.subprocess, 'run', fail)
    with pytest.raises(module.UpdateError, match='当前应用仍可使用'):
        module.launch(tmp_path/'helper', tmp_path/'plan', tmp_path/'log')
    assert not list(tmp_path.glob('*.plist'))
    assert not list(tmp_path.glob('*.sh'))


@pytest.mark.parametrize('code,output', [(1, ''), (0, 'state = not running\nlast exit code = 126')])
def test_missing_completion_does_not_wait_forever(tmp_path, monkeypatch, code, output):
    monkeypatch.setattr(module.subprocess, 'run', lambda *a, **k:
                        SimpleNamespace(returncode=code, stdout=output))
    assert module.DetachedUpdate('gui/501/synthetic', tmp_path/'result').wait() == 1
