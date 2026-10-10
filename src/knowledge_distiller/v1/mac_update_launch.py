"""Run the update helper in its own launchd job, outside the GUI app's job."""
import subprocess
import time
import os
import plistlib
import re
from pathlib import Path
from uuid import uuid4

from .updates import UpdateError


_WRAPPER = '''#!/bin/sh
export PYINSTALLER_RESET_ENVIRONMENT=1
"$1" --component "$2"
status=$?
printf '%s\\n' "$status" > "$3.tmp"
/bin/mv "$3.tmp" "$3"
/bin/launchctl bootout "$4"
'''


class DetachedUpdate:
    def __init__(self, label, result):
        self.label, self.result = label, result

    def wait(self):
        deadline = time.monotonic() + 3600
        while time.monotonic() < deadline:
            if self.result.exists():
                try:
                    return int(self.result.read_text(encoding='utf-8').strip())
                except (OSError, ValueError):
                    return 1
            job = subprocess.run(['/bin/launchctl', 'print', self.label],
                                 capture_output=True, text=True)
            if job.returncode:
                # The wrapper persists its result before removing its job.
                if self.result.exists():
                    continue
                return 1
            if re.search(r'^\s*state = not running\s*$', job.stdout, re.M) and re.search(
                    r'^\s*last exit code = -?\d+', job.stdout, re.M):
                # Launchd can reject a script before the wrapper can persist a
                # result (permissions/TCC). Do not leave the UI installing.
                return 1
            time.sleep(.5)
        return 1


def launch(helper, plan, log):
    root = Path(plan).parent
    nonce = uuid4().hex
    label = 'local.knowledge-distiller.update-' + nonce
    domain = 'gui/' + str(os.getuid())
    service = domain + '/' + label
    wrapper = root / ('update-job-' + nonce + '.sh')
    result = root / ('update-job-' + nonce + '.result')
    job = root / ('update-job-' + nonce + '.plist')
    wrapper.write_text(_WRAPPER, encoding='utf-8')
    job.write_bytes(plistlib.dumps(dict(Label=label, RunAtLoad=True, KeepAlive=False,
        AbandonProcessGroup=True, StandardOutPath=str(log), StandardErrorPath=str(log),
        ProgramArguments=['/bin/sh', str(wrapper), str(helper), str(plan), str(result), service])))
    try:
        subprocess.run(['/bin/launchctl', 'bootstrap', domain, str(job)],
                       check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError) as error:
        wrapper.unlink(missing_ok=True)
        job.unlink(missing_ok=True)
        raise UpdateError('无法启动独立更新任务，当前应用仍可使用。请稍后重试。') from error
    return DetachedUpdate(service, result)
