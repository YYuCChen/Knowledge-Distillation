from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest

import knowledge_distiller.v1.wiki_publish as publish_module
from knowledge_distiller.v1.wiki_lock import VaultWriteLock
from knowledge_distiller.v1.wiki_publish import (
    PublishExpectation,
    PublishState,
    WikiPublishError,
    publish_wiki,
    recover_wiki,
)


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _put(root: Path, relative: str, content: bytes, *, mode: int = 0o600) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(mode)


def _roots(tmp_path: Path) -> tuple[Path, Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "target-vault"
    staging = tmp_path / "staging-vault"
    journal = tmp_path / "private-journal"
    target.mkdir()
    staging.mkdir()
    return target, staging, journal


def test_publish_new_and_replacement_files_preserves_unlisted_pages_and_commits(
    tmp_path,
):
    target, staging, journal = _roots(tmp_path)
    old = b"old source page\n"
    new = b"new source page\n"
    _put(target, "wiki/source.md", old, mode=0o640)
    _put(target, "wiki/keep.md", b"user page remains\n")
    staged = {
        "wiki/source.md": new,
        "wiki/nested/new.md": b"new page\n",
        ".graph/graph.json": b'{"nodes": []}\n',
        ".graph/state.json": b'{"version": 1}\n',
        ".graph/检查结果.md": b"# synthetic check\n",
    }
    for relative, content in staged.items():
        _put(staging, relative, content)
    expected = {
        relative: PublishExpectation(
            _sha(old) if relative == "wiki/source.md" else None,
            _sha(content),
        )
        for relative, content in staged.items()
    }

    with VaultWriteLock.acquire(target) as lock:
        result = publish_wiki(target, staging, journal, expected, lock=lock)
        first_recovery = recover_wiki(target, journal, lock=lock)
        second_recovery = recover_wiki(target, journal, lock=lock)

    assert result.state is PublishState.COMMITTED
    assert first_recovery.state is PublishState.COMMITTED
    assert second_recovery.state is PublishState.COMMITTED
    for relative, content in staged.items():
        assert (target / relative).read_bytes() == content
    assert (target / "wiki/keep.md").read_bytes() == b"user page remains\n"
    assert stat.S_IMODE((target / "wiki/source.md").stat().st_mode) == 0o640
    assert stat.S_IMODE(journal.stat().st_mode) == 0o700
    assert stat.S_IMODE((journal / "backups").stat().st_mode) == 0o700
    assert stat.S_IMODE((journal / "backups/0004.before").stat().st_mode) == 0o600
    assert stat.S_IMODE((journal / "journal.json").stat().st_mode) == 0o600
    serialized = (journal / "journal.json").read_text(encoding="utf-8")
    assert "old source page" not in serialized
    assert "new source page" not in serialized
    assert json.loads(serialized)["state"] == "committed"


@pytest.mark.parametrize(
    "relative",
    (
        "../escape.md",
        "/absolute.md",
        "wiki/../../escape.md",
        "raw/本人/R-20261001-0001.md",
        ".graph/other.json",
        "state.json",
        "检查结果.md",
        "tools/kb.py",
        "个人/笔记.md",
    ),
)
def test_publish_rejects_traversal_raw_unknown_graph_kit_and_personal_paths(
    tmp_path, relative
):
    target, staging, journal = _roots(tmp_path)
    expected = {relative: PublishExpectation(None, _sha(b"blocked"))}
    with VaultWriteLock.acquire(target) as lock:
        with pytest.raises(
            WikiPublishError,
            match="publish_path_invalid|publish_path_not_allowed",
        ):
            publish_wiki(target, staging, journal, expected, lock=lock)
    assert list(target.iterdir()) == []


def test_publish_rejects_staging_and_target_symlinks(tmp_path):
    target, staging, journal = _roots(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    _put(outside, "page.md", b"outside")
    (staging / "wiki").mkdir()
    (staging / "wiki/page.md").symlink_to(outside / "page.md")
    expected = {
        "wiki/page.md": PublishExpectation(None, _sha(b"outside")),
    }
    with VaultWriteLock.acquire(target) as lock:
        with pytest.raises(WikiPublishError, match="publish_path_unsafe"):
            publish_wiki(target, staging, journal, expected, lock=lock)

    target_two, staging_two, journal_two = _roots(tmp_path / "second")
    _put(staging_two, "wiki/page.md", b"staged")
    (target_two / "wiki").symlink_to(outside, target_is_directory=True)
    expected_two = {
        "wiki/page.md": PublishExpectation(_sha(b"outside"), _sha(b"staged")),
    }
    with VaultWriteLock.acquire(target_two) as lock:
        with pytest.raises(WikiPublishError, match="publish_path_unsafe"):
            publish_wiki(
                target_two,
                staging_two,
                journal_two,
                expected_two,
                lock=lock,
            )
    assert (outside / "page.md").read_bytes() == b"outside"


@pytest.mark.parametrize(
    ("event", "crash_path"),
    (
        ("before_replace", "wiki/a.md"),
        ("after_replace", "wiki/a.md"),
        ("before_replace", "wiki/b.md"),
        ("after_replace", "wiki/b.md"),
    ),
)
def test_each_replace_boundary_is_recoverable_and_recovery_is_idempotent(
    tmp_path, monkeypatch, event, crash_path
):
    target, staging, journal = _roots(tmp_path)
    original = b"original a\n"
    _put(target, "wiki/a.md", original)
    _put(staging, "wiki/a.md", b"published a\n")
    _put(staging, "wiki/b.md", b"published b\n")
    expected = {
        "wiki/a.md": PublishExpectation(_sha(original), _sha(b"published a\n")),
        "wiki/b.md": PublishExpectation(None, _sha(b"published b\n")),
    }

    def interrupt(name: str, relative: str) -> None:
        if (name, relative) == (event, crash_path):
            raise RuntimeError("synthetic crash")

    monkeypatch.setattr(publish_module, "_event", interrupt)
    with VaultWriteLock.acquire(target) as lock:
        with pytest.raises(RuntimeError, match="synthetic crash"):
            publish_wiki(target, staging, journal, expected, lock=lock)
        recovered = recover_wiki(target, journal, lock=lock)
        repeated = recover_wiki(target, journal, lock=lock)

    assert recovered.state is PublishState.RECOVERED
    assert repeated.state is PublishState.RECOVERED
    assert (target / "wiki/a.md").read_bytes() == original
    assert not (target / "wiki/b.md").exists()


def test_recovery_preserves_later_user_edit_and_reports_failure(tmp_path, monkeypatch):
    target, staging, journal = _roots(tmp_path)
    original = b"original\n"
    published = b"published\n"
    user_edit = b"user edited after interruption\n"
    _put(target, "wiki/page.md", original)
    _put(staging, "wiki/page.md", published)
    expected = {
        "wiki/page.md": PublishExpectation(_sha(original), _sha(published)),
    }

    def interrupt(name: str, relative: str) -> None:
        if name == "after_replace" and relative == "wiki/page.md":
            raise RuntimeError("synthetic crash")

    monkeypatch.setattr(publish_module, "_event", interrupt)
    with VaultWriteLock.acquire(target) as lock:
        with pytest.raises(RuntimeError, match="synthetic crash"):
            publish_wiki(target, staging, journal, expected, lock=lock)
        (target / "wiki/page.md").write_bytes(user_edit)
        result = recover_wiki(target, journal, lock=lock)

    assert result.state is PublishState.RECOVERY_FAILED
    assert result.conflicts == ("wiki/page.md",)
    assert (target / "wiki/page.md").read_bytes() == user_edit


def test_readback_interruption_leaves_all_files_recoverable(tmp_path, monkeypatch):
    target, staging, journal = _roots(tmp_path)
    original = b"old\n"
    _put(target, "wiki/a.md", original)
    _put(staging, "wiki/a.md", b"new a\n")
    _put(staging, "wiki/b.md", b"new b\n")
    expected = {
        "wiki/a.md": PublishExpectation(_sha(original), _sha(b"new a\n")),
        "wiki/b.md": PublishExpectation(None, _sha(b"new b\n")),
    }

    def fail_readback(name: str, relative: str) -> None:
        if name == "before_readback" and relative == "wiki/a.md":
            raise OSError("synthetic readback failure")

    monkeypatch.setattr(publish_module, "_event", fail_readback)
    with VaultWriteLock.acquire(target) as lock:
        with pytest.raises(WikiPublishError, match="publish_io_failed"):
            publish_wiki(target, staging, journal, expected, lock=lock)
        recovered = recover_wiki(target, journal, lock=lock)

    assert recovered.state is PublishState.RECOVERED
    assert (target / "wiki/a.md").read_bytes() == original
    assert not (target / "wiki/b.md").exists()


@pytest.mark.parametrize("crash_event", ("after_temp_fsync", "after_replace"))
def test_real_process_exit_releases_lock_and_recovery_removes_owned_temporary(
    tmp_path, crash_event
):
    target, staging, journal = _roots(tmp_path)
    _put(target, "wiki/keep.md", b"keep\n")
    _put(staging, "wiki/new.md", b"new\n")
    script = r"""
import hashlib
import os
from pathlib import Path
import sys

import knowledge_distiller.v1.wiki_publish as module
from knowledge_distiller.v1.wiki_lock import VaultWriteLock
from knowledge_distiller.v1.wiki_publish import PublishExpectation, publish_wiki

target, staging, journal = map(Path, sys.argv[1:4])
crash_event = sys.argv[4]
content = (staging / "wiki/new.md").read_bytes()
expected = {
    "wiki/new.md": PublishExpectation(None, hashlib.sha256(content).hexdigest()),
}

def exit_process(name, relative):
    if name == crash_event and relative == "wiki/new.md":
        os._exit(73)

module._event = exit_process
with VaultWriteLock.acquire(target) as lock:
    publish_wiki(target, staging, journal, expected, lock=lock)
"""
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(target),
            str(staging),
            str(journal),
            crash_event,
        ],
        check=False,
        capture_output=True,
        text=True,
        env=os.environ.copy(),
        timeout=10,
    )
    assert completed.returncode == 73
    assert (journal / "journal.json").is_file()
    assert list((target / "wiki").glob(".kd-publish-*.tmp"))

    with VaultWriteLock.acquire(target) as lock:
        recovered = recover_wiki(target, journal, lock=lock)
        repeated = recover_wiki(target, journal, lock=lock)

    assert recovered.state is PublishState.RECOVERED
    assert repeated.state is PublishState.RECOVERED
    assert not (target / "wiki/new.md").exists()
    assert (target / "wiki/keep.md").read_bytes() == b"keep\n"
    assert list((target / "wiki").glob(".kd-publish-*.tmp")) == []


def test_real_recovery_exit_after_temp_fsync_can_be_retried(tmp_path):
    target, staging, journal = _roots(tmp_path)
    original = b"original\n"
    published = b"published\n"
    _put(target, "wiki/page.md", original)
    _put(staging, "wiki/page.md", published)
    publish_script = r"""
import hashlib
import os
from pathlib import Path
import sys

import knowledge_distiller.v1.wiki_publish as module
from knowledge_distiller.v1.wiki_lock import VaultWriteLock
from knowledge_distiller.v1.wiki_publish import PublishExpectation, publish_wiki

target, staging, journal = map(Path, sys.argv[1:4])
before = (target / "wiki/page.md").read_bytes()
after = (staging / "wiki/page.md").read_bytes()
expected = {
    "wiki/page.md": PublishExpectation(
        hashlib.sha256(before).hexdigest(),
        hashlib.sha256(after).hexdigest(),
    ),
}

def exit_after_replace(name, relative):
    if name == "after_replace" and relative == "wiki/page.md":
        os._exit(73)

module._event = exit_after_replace
with VaultWriteLock.acquire(target) as lock:
    publish_wiki(target, staging, journal, expected, lock=lock)
"""
    interrupted_publish = subprocess.run(
        [
            sys.executable,
            "-c",
            publish_script,
            str(target),
            str(staging),
            str(journal),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=os.environ.copy(),
        timeout=10,
    )
    assert interrupted_publish.returncode == 73
    assert (target / "wiki/page.md").read_bytes() == published

    recovery_script = r"""
import os
from pathlib import Path
import sys

import knowledge_distiller.v1.wiki_publish as module
from knowledge_distiller.v1.wiki_lock import VaultWriteLock
from knowledge_distiller.v1.wiki_publish import recover_wiki

target, journal = map(Path, sys.argv[1:3])

def exit_during_recovery(name, relative):
    if name == "recovery_after_temp_fsync" and relative == "wiki/page.md":
        os._exit(74)

module._event = exit_during_recovery
with VaultWriteLock.acquire(target) as lock:
    recover_wiki(target, journal, lock=lock)
"""
    interrupted_recovery = subprocess.run(
        [sys.executable, "-c", recovery_script, str(target), str(journal)],
        check=False,
        capture_output=True,
        text=True,
        env=os.environ.copy(),
        timeout=10,
    )
    assert interrupted_recovery.returncode == 74
    temporary = list((target / "wiki").glob(".kd-publish-*.tmp"))
    assert len(temporary) == 1
    assert temporary[0].read_bytes() == original
    assert (target / "wiki/page.md").read_bytes() == published

    with VaultWriteLock.acquire(target) as lock:
        recovered = recover_wiki(target, journal, lock=lock)
        repeated = recover_wiki(target, journal, lock=lock)

    assert recovered.state is PublishState.RECOVERED
    assert repeated.state is PublishState.RECOVERED
    assert (target / "wiki/page.md").read_bytes() == original
    assert list((target / "wiki").glob(".kd-publish-*.tmp")) == []


def test_manifest_digest_changes_are_rejected_before_target_writes(tmp_path):
    target, staging, journal = _roots(tmp_path)
    _put(target, "wiki/page.md", b"current")
    _put(staging, "wiki/page.md", b"staged")
    with VaultWriteLock.acquire(target) as lock:
        with pytest.raises(WikiPublishError, match="staging_digest_changed"):
            publish_wiki(
                target,
                staging,
                journal,
                {"wiki/page.md": PublishExpectation(_sha(b"current"), "0" * 64)},
                lock=lock,
            )
    assert (target / "wiki/page.md").read_bytes() == b"current"

    target_two, staging_two, journal_two = _roots(tmp_path / "second")
    _put(target_two, "wiki/page.md", b"current")
    _put(staging_two, "wiki/page.md", b"staged")
    with VaultWriteLock.acquire(target_two) as lock:
        with pytest.raises(WikiPublishError, match="target_digest_changed"):
            publish_wiki(
                target_two,
                staging_two,
                journal_two,
                {"wiki/page.md": PublishExpectation("1" * 64, _sha(b"staged"))},
                lock=lock,
            )
    assert (target_two / "wiki/page.md").read_bytes() == b"current"


def test_missing_staging_file_reports_unsupported_deletion_and_keeps_target(tmp_path):
    target, staging, journal = _roots(tmp_path)
    current = b"must remain\n"
    _put(target, "wiki/page.md", current)
    expected = {
        "wiki/page.md": PublishExpectation(_sha(current), _sha(b"deleted")),
    }
    with VaultWriteLock.acquire(target) as lock:
        with pytest.raises(WikiPublishError, match="deletion_not_supported"):
            publish_wiki(target, staging, journal, expected, lock=lock)
    assert (target / "wiki/page.md").read_bytes() == current


def test_edit_after_preflight_is_not_overwritten_and_recovery_reports_conflict(
    tmp_path, monkeypatch
):
    target, staging, journal = _roots(tmp_path)
    original = b"original\n"
    user_edit = b"concurrent user edit\n"
    staged = b"staged\n"
    _put(target, "wiki/page.md", original)
    _put(staging, "wiki/page.md", staged)
    expected = {
        "wiki/page.md": PublishExpectation(_sha(original), _sha(staged)),
    }

    def edit_before_replace(name: str, relative: str) -> None:
        if name == "before_replace" and relative == "wiki/page.md":
            (target / relative).write_bytes(user_edit)

    monkeypatch.setattr(publish_module, "_event", edit_before_replace)
    with VaultWriteLock.acquire(target) as lock:
        with pytest.raises(WikiPublishError, match="target_digest_changed"):
            publish_wiki(target, staging, journal, expected, lock=lock)
        recovered = recover_wiki(target, journal, lock=lock)

    assert recovered.state is PublishState.RECOVERY_FAILED
    assert recovered.conflicts == ("wiki/page.md",)
    assert (target / "wiki/page.md").read_bytes() == user_edit


def test_invalid_lock_and_nested_journal_are_rejected_without_creating_paths(tmp_path):
    target, staging, journal = _roots(tmp_path)
    _put(staging, "wiki/page.md", b"staged")
    expected = {
        "wiki/page.md": PublishExpectation(None, _sha(b"staged")),
    }
    fake = VaultWriteLock(target, -1)
    with pytest.raises(WikiPublishError, match="vault_lock_invalid"):
        publish_wiki(target, staging, journal, expected, lock=fake)
    assert not journal.exists()

    nested = target / "raw/private-journal"
    with VaultWriteLock.acquire(target) as lock:
        with pytest.raises(WikiPublishError, match="publish_roots_overlap"):
            publish_wiki(target, staging, nested, expected, lock=lock)
    assert not (target / "raw").exists()
    assert list(target.iterdir()) == []


def test_recover_requires_existing_private_journal_and_does_not_create_it(tmp_path):
    target = tmp_path / "target-vault"
    missing = tmp_path / "missing-journal"
    target.mkdir()
    with VaultWriteLock.acquire(target) as lock:
        with pytest.raises(WikiPublishError, match="journal_path_unsafe"):
            recover_wiki(target, missing, lock=lock)
    assert not missing.exists()
