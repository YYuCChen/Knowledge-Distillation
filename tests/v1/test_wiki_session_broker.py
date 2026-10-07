from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from knowledge_distiller.v1.wiki_lock import VaultWriteLock
from knowledge_distiller.v1.wiki_session_broker import WikiSessionBroker


KIT = Path(__file__).resolve().parents[2] / "vault-kit"


def test_broker_proves_live_staging_lock_to_agent_grandchild(tmp_path):
    staging = tmp_path / "staging"
    runtime = tmp_path / "runtime"
    staging.mkdir()
    runtime.mkdir()
    (staging / "tools").mkdir()
    shutil.copyfile(KIT / "tools/wiki_session.py", staging / "tools/wiki_session.py")
    grandchild = (
        "import subprocess,sys;"
        "r=subprocess.run([sys.executable,sys.argv[1],'--root',sys.argv[2],'status']);"
        "raise SystemExit(r.returncode)"
    )
    with WikiSessionBroker(staging, runtime) as broker:
        environment = os.environ.copy()
        environment.update(broker.environment())
        agent = subprocess.run(
            [sys.executable, "-c", grandchild, str(staging / "tools/wiki_session.py"),
             str(staging)],
            env=environment, close_fds=True, check=False)
        assert agent.returncode == 0
        with pytest.raises(BlockingIOError):
            VaultWriteLock.acquire(staging)

    rejected = subprocess.run(
        [sys.executable, str(staging / "tools/wiki_session.py"), "--root", str(staging),
         "status"], env=environment, close_fds=True, check=False)
    assert rejected.returncode == 1


def test_broker_rejects_forged_token(tmp_path):
    staging = tmp_path / "staging"
    runtime = tmp_path / "runtime"
    staging.mkdir()
    runtime.mkdir()
    (staging / "tools").mkdir()
    shutil.copyfile(KIT / "tools/wiki_session.py", staging / "tools/wiki_session.py")
    with WikiSessionBroker(staging, runtime) as broker:
        environment = os.environ.copy()
        environment.update(broker.environment())
        environment["KD_WIKI_LOCK_TOKEN"] = "0" * 64
        result = subprocess.run(
            [sys.executable, str(staging / "tools/wiki_session.py"), "--root", str(staging),
             "status"], env=environment, close_fds=True, check=False)
    assert result.returncode == 1


def test_broker_socket_stays_short_for_long_unicode_staging_path(tmp_path):
    staging = tmp_path / ("知识蒸馏器-" + "很长" * 20) / ("任务-" + "路径" * 20)
    runtime = tmp_path / "runtime"
    staging.mkdir(parents=True)
    runtime.mkdir()
    with WikiSessionBroker(staging, runtime) as broker:
        endpoint = broker.environment()["KD_WIKI_LOCK_SOCKET"]
        assert len(os.fsencode(endpoint)) < 104
        assert not endpoint.startswith(str(runtime))
