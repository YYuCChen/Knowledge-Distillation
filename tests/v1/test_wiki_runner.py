from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import threading
import time

import pytest

from knowledge_distiller.v1.llm import LLMRequestError
from knowledge_distiller.v1.wiki_runner import CodexWikiRunner, WikiRunnerError, batch_prompt


KIT = Path(__file__).resolve().parents[2] / "vault-kit"


def _executable(tmp_path: Path, source: str) -> Path:
    script = tmp_path / "fake_codex.py"
    script.write_text(source, encoding="utf-8")
    wrapper = tmp_path / "codex"
    wrapper.write_text(
        "#!/bin/sh\nexec " + shlex.quote(sys.executable) + " " + shlex.quote(str(script)) + ' "$@"\n',
        encoding="utf-8")
    wrapper.chmod(0o700)
    return wrapper


def _staging(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "staging"
    runtime = tmp_path / "runtime"
    root.mkdir()
    runtime.mkdir()
    (root / "tools").mkdir()
    shutil.copyfile(KIT / "tools/wiki_session.py", root / "tools/wiki_session.py")
    return root, runtime


def test_runner_uses_staging_session_sanitized_cli_and_fixed_usage(tmp_path, monkeypatch):
    root, runtime = _staging(tmp_path)
    fake = _executable(tmp_path, """import json,os,subprocess,sys
from pathlib import Path
root=Path.cwd()
status=subprocess.run([sys.executable,str(root/'tools/wiki_session.py'),'--root',str(root),'status'])
(root/'observed.json').write_text(json.dumps({'argv':sys.argv[1:],'prompt':sys.stdin.read(),
 'status':status.returncode,'has_api_key':'OPENAI_API_KEY' in os.environ,
 'socket':os.environ.get('KD_WIKI_LOCK_SOCKET','')}))
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':3,'output_tokens':2,
 'private':999}}))
raise SystemExit(status.returncode)
""")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-child")
    runner = CodexWikiRunner(
        timeout_seconds=5,
        executable_resolver=lambda: str(fake),
        model_probe=lambda: [{"model": "gpt-test", "efforts": ["high"]}],
    )
    result = runner.run(
        root, runtime, model="gpt-test", effort="high", batch_no=2,
        raw_paths=["raw/外部/2026/10/R-20261001-0001.md"])
    assert result.succeeded
    assert dict(result.usage) == {"input_tokens": 3, "output_tokens": 2}
    observed = json.loads((root / "observed.json").read_text())
    assert observed["status"] == 0
    assert observed["has_api_key"] is False
    assert observed["socket"].startswith("/private/tmp/.kdws-")
    assert "--ignore-user-config" in observed["argv"]
    assert "--ignore-rules" in observed["argv"]
    assert "--strict-config" in observed["argv"]
    assert "workspace-write" not in observed["argv"]
    assert 'default_permissions="wiki_staging"' in observed["argv"]
    profiles = [value for value in observed["argv"]
                if value.startswith("permissions.wiki_staging=")]
    assert len(profiles) == 1
    assert 'extends=":read-only"' in profiles[0]
    assert 'filesystem={":workspace_roots"={"."="write"}}' in profiles[0]
    assert 'domains={}' in profiles[0]
    assert 'allow_local_binding=false' in profiles[0]
    assert json.dumps(observed["socket"]) + '="allow"' in profiles[0]
    assert not any(value.startswith("sandbox_workspace_write.") for value in observed["argv"])
    assert not any(value.startswith("network.") for value in observed["argv"])
    assert "features.hooks=false" not in observed["argv"]  # disabled with the CLI feature switch
    assert observed["argv"].count("--disable") >= 10
    assert "只处理以下冻结 raw" in observed["prompt"]
    assert "合成 staging" not in observed["prompt"]


def test_timeout_kills_stubborn_descendant_even_after_leader_exits(tmp_path):
    root, runtime = _staging(tmp_path)
    fake = _executable(tmp_path, """import subprocess,sys,time
from pathlib import Path
code='''import signal,time\nfrom pathlib import Path\nsignal.signal(signal.SIGTERM,signal.SIG_IGN)\nPath("child.ready").write_text("ready")\nwhile True: time.sleep(1)\n'''
child=subprocess.Popen([sys.executable,'-c',code])
Path('child.pid').write_text(str(child.pid))
while not Path('child.ready').exists(): time.sleep(.01)
raise SystemExit(0)
""")
    runner = CodexWikiRunner(timeout_seconds=1, executable_resolver=lambda: str(fake),
                             model_probe=lambda: [])
    result = runner.run(
        root, runtime, model="gpt-test", effort="high", batch_no=1,
        raw_paths=["raw/外部/2026/10/R-20261001-0001.md"], skip_preflight=True)
    assert result.error_code == "runner_timeout"
    child_pid = int((root / "child.pid").read_text())
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("stubborn runner descendant survived process-group timeout")


def test_invalid_utf8_and_preflight_failures_are_fixed_codes(tmp_path):
    root, runtime = _staging(tmp_path)
    invalid = _executable(tmp_path, "import os\nos.write(1,b'\\xff')\n")
    runner = CodexWikiRunner(timeout_seconds=2, executable_resolver=lambda: str(invalid),
                             model_probe=lambda: [])
    result = runner.run(
        root, runtime, model="gpt-test", effort="high", batch_no=1,
        raw_paths=["raw/外部/2026/10/R-20261001-0001.md"], skip_preflight=True)
    assert result.error_code == "agent_failed"

    missing = CodexWikiRunner(
        executable_resolver=lambda: str(invalid),
        model_probe=lambda: (_ for _ in ()).throw(LLMRequestError("llm_config_unavailable")),
    )
    with pytest.raises(WikiRunnerError, match="config_required"):
        missing.preflight("gpt-test", "high")
    unavailable = CodexWikiRunner(executable_resolver=lambda: str(invalid), model_probe=lambda: [])
    with pytest.raises(WikiRunnerError, match="model_unavailable"):
        unavailable.preflight("gpt-test", "high")


def test_prompt_rejects_path_escape():
    with pytest.raises(WikiRunnerError, match="batch_boundary_invalid"):
        batch_prompt(1, ["raw/外部/../../private.md"])


def test_cancel_during_preflight_is_not_cleared_before_process_start(tmp_path, monkeypatch):
    root, runtime = _staging(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    started = []

    def resolver():
        entered.set()
        assert release.wait(5)
        return "/synthetic/codex"

    monkeypatch.setattr(
        "knowledge_distiller.v1.wiki_runner.subprocess.Popen",
        lambda *_args, **_kwargs: started.append(True))
    runner = CodexWikiRunner(
        executable_resolver=resolver,
        model_probe=lambda: [{"model": "gpt-test", "efforts": ["high"]}],
    )
    results = []
    thread = threading.Thread(target=lambda: results.append(runner.run(
        root, runtime, model="gpt-test", effort="high", batch_no=1,
        raw_paths=["raw/外部/2026/10/R-20261001-0001.md"])))
    thread.start()
    assert entered.wait(2)
    runner.cancel()
    release.set()
    thread.join(5)

    assert not thread.is_alive()
    assert results[0].error_code == "interrupted"
    assert started == []
