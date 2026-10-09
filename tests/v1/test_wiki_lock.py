from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

import pytest

from knowledge_distiller.v1.wiki_lock import VaultWriteLock, WikiLockError, canonical_vault


KIT = Path(__file__).resolve().parents[2] / "vault-kit"


def _install_tools(vault: Path) -> None:
    (vault / "tools").mkdir(parents=True)
    shutil.copyfile(KIT / "tools/kb.py", vault / "tools/kb.py")
    shutil.copyfile(KIT / "tools/wiki_display.py", vault / "tools/wiki_display.py")
    shutil.copyfile(KIT / "tools/wiki_session.py", vault / "tools/wiki_session.py")


def _wrapper(vault: Path, command: list[str]) -> list[str]:
    return [sys.executable, str(vault / "tools/wiki_session.py"),
            "--root", str(vault), "--", *command]


def test_directory_inode_lock_excludes_another_process(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    script = """from knowledge_distiller.v1.wiki_lock import VaultWriteLock
import pathlib,sys
try:
    lock=VaultWriteLock.acquire(pathlib.Path(sys.argv[1]))
except BlockingIOError:
    raise SystemExit(17)
else:
    lock.close()
"""
    with VaultWriteLock.acquire(vault):
        result = subprocess.run([sys.executable, "-c", script, str(vault)], check=False)
    assert result.returncode == 17
    assert subprocess.run([sys.executable, "-c", script, str(vault)], check=False).returncode == 0


def test_vault_root_and_intermediate_symlinks_are_rejected(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(WikiLockError, match="vault_symlink"):
        canonical_vault(link)


def test_manual_wrapper_allows_agent_grandchild_with_close_fds(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    _install_tools(vault)
    init = subprocess.run(_wrapper(vault, [sys.executable, str(vault / "tools/kb.py"),
                                            "init", "--root", str(vault)]),
                          capture_output=True, text=True, check=False)
    assert init.returncode == 0, init.stderr
    agent = vault / "agent.py"
    agent.write_text(
        "import subprocess,sys\n"
        "r=subprocess.run([sys.executable,sys.argv[1],'--root',sys.argv[2]],close_fds=True)\n"
        "raise SystemExit(r.returncode)\n", encoding="utf-8")
    result = subprocess.run(_wrapper(vault, [sys.executable, str(agent),
                                              str(vault / "tools/kb.py"), str(vault)]),
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


def test_manual_wrapper_uses_private_short_socket_with_long_tmpdir(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    _install_tools(vault)
    long_tmp = tmp_path / ("deep-" + "x" * 150)
    long_tmp.mkdir()
    probe = """import json,os,pathlib,stat,subprocess,sys
endpoint=os.environ['KD_WIKI_LOCK_SOCKET']
directory=pathlib.Path(endpoint).parent
info=directory.lstat()
result=subprocess.run([sys.executable,sys.argv[1],'--root',sys.argv[2],'status'],close_fds=True)
print(json.dumps({'endpoint':endpoint,'mode':stat.S_IMODE(info.st_mode),
                  'owner':info.st_uid,'status':result.returncode}))
raise SystemExit(result.returncode)
"""
    environment = os.environ.copy()
    environment["TMPDIR"] = str(long_tmp)
    result = subprocess.run(
        _wrapper(vault, [sys.executable, "-c", probe,
                         str(vault / "tools/wiki_session.py"), str(vault)]),
        env=environment, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    observed = json.loads(result.stdout)
    endpoint = Path(observed["endpoint"])
    assert observed["status"] == 0
    assert observed["mode"] == 0o700
    assert observed["owner"] == os.getuid()
    assert len(os.fsencode(endpoint)) < 104
    assert not endpoint.is_relative_to(long_tmp)
    assert not endpoint.exists()
    assert not endpoint.parent.exists()


def test_fake_or_mismatched_session_environment_is_rejected(tmp_path):
    vault = tmp_path / "vault"
    other = tmp_path / "other"
    vault.mkdir()
    other.mkdir()
    _install_tools(vault)
    env = os.environ.copy()
    env.update({"KD_WIKI_LOCK_FD": "99999", "KD_WIKI_LOCK_VAULT_KEY": "0" * 64,
                "KD_WIKI_LOCK_SOCKET": str(tmp_path / "missing.sock"),
                "KD_WIKI_LOCK_TOKEN": "fake"})
    result = subprocess.run([sys.executable, str(vault / "tools/wiki_session.py"),
                             "--root", str(vault), "status"], env=env, check=False)
    assert result.returncode == 1
    descriptor = os.open(other, os.O_RDONLY)
    try:
        env["KD_WIKI_LOCK_FD"] = str(descriptor)
        result = subprocess.run([sys.executable, str(vault / "tools/wiki_session.py"),
                                 "--root", str(vault), "status"], env=env,
                                pass_fds=(descriptor,), check=False)
        assert result.returncode == 1
    finally:
        os.close(descriptor)


def test_launcher_sigkill_does_not_unlock_while_agent_survives(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    _install_tools(vault)
    ready = vault / "ready"
    sleeper = vault / "sleep.py"
    sleeper.write_text(
        "from pathlib import Path\nimport sys,time\nPath(sys.argv[1]).write_text('ready')\ntime.sleep(2)\n",
        encoding="utf-8")
    launcher = subprocess.Popen(_wrapper(vault, [sys.executable, str(sleeper), str(ready)]))
    deadline = time.monotonic() + 5
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert ready.exists()
    os.kill(launcher.pid, signal.SIGKILL)
    launcher.wait(timeout=2)
    with pytest.raises(BlockingIOError, match="vault_busy"):
        VaultWriteLock.acquire(vault)
    time.sleep(2.2)
    with VaultWriteLock.acquire(vault):
        pass
