from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from knowledge_distiller.v1 import wiki_kit as kit_module
from knowledge_distiller.v1 import wiki_kit_install as install_module
from knowledge_distiller.v1.wiki_kit import KitFile, read_receipt
from knowledge_distiller.v1.wiki_kit_install import WikiKitInstallError, WikiKitInstaller
from knowledge_distiller.v1.wiki_lock import VaultWriteLock
from knowledge_distiller.v1.worker_lifecycle import WorkAdmissionGate, WorkerCoordinator


class Worker:
    def start(self): return True
    def stop(self, _timeout=0): return True
    def reserve_for_update(self): return True
    def release_update(self): pass
    def update_ready(self): return True


V2_TARGETS = (
    "AGENTS.md",
    "CLAUDE.md",
    "tools/kb.py",
    ".agents/skills/kb-confirm/SKILL.md",
    ".agents/skills/kb-ingest/SKILL.md",
    ".agents/skills/kb-lint/SKILL.md",
    ".agents/skills/kb-read/SKILL.md",
)
V3_NEW_TARGETS = (
    "tools/wiki_session.py",
    "tools/wiki_display.py",
    ".kd/assets/kd-wiki.css",
)


def _service(tmp_path: Path, entries, version="v1"):
    kit = tmp_path / f"kit-{version}"
    kit.mkdir()
    files = []
    for source, target, body in entries:
        path = kit / source
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        files.append({"source_path": source, "install_path": target,
                      "sha256": hashlib.sha256(body).hexdigest()})
    (kit / "kit-manifest.json").write_text(json.dumps({
        "kit_version": version, "protocol_version": 2, "files": files,
    }, sort_keys=True), encoding="utf-8")
    gate = WorkAdmissionGate()
    coordinator = WorkerCoordinator(Worker(), Worker(), gate)
    return WikiKitInstaller(kit, tmp_path / "runtime", gate, coordinator)


def _legacy_upgrade(tmp_path: Path, monkeypatch):
    old = {path: f"v2:{path}\n".encode() for path in V2_TARGETS}
    monkeypatch.setattr(kit_module, "_V2_UNRECEIPTED_FILES", tuple(
        KitFile(path, path, hashlib.sha256(body).hexdigest())
        for path, body in old.items()
    ))
    desired = {
        path: (old[path] if path == "CLAUDE.md" else f"v3:{path}\n".encode())
        for path in V2_TARGETS
    }
    desired.update({path: f"v3:{path}\n".encode() for path in V3_NEW_TARGETS})
    service = _service(
        tmp_path,
        [(path, path, body) for path, body in desired.items()],
        "v3",
    )
    vault = tmp_path / "vault"
    vault.mkdir()
    for path, body in old.items():
        target = vault / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
    protected = {
        "wiki/index.md": b"wiki\n",
        "raw/外部/R-keep.md": b"raw\n",
        ".graph/state.json": b'{"state":"keep"}\n',
    }
    for path, body in protected.items():
        target = vault / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
    return service, vault, old, desired, protected


def _assert_protected(vault: Path, protected: dict[str, bytes]) -> None:
    assert {path: (vault / path).read_bytes() for path in protected} == protected


def test_install_is_idempotent_and_never_touches_user_directories(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "raw").mkdir()
    (vault / "raw/keep.md").write_bytes(b"raw")
    (vault / "wiki").mkdir()
    (vault / "wiki/keep.md").write_bytes(b"wiki")
    service = _service(tmp_path, [("tools/a.py", "tools/a.py", b"a")])

    assert service.status(vault).state == "missing"
    first = service.install(vault)
    assert first.state == "installed"
    assert first.changed_paths == ("tools/a.py", ".kd/wiki-kit.json")
    assert service.install(vault).state == "unchanged"
    assert service.status(vault).state == "ready"
    assert (vault / "raw/keep.md").read_bytes() == b"raw"
    assert (vault / "wiki/keep.md").read_bytes() == b"wiki"


def test_exact_v2_install_uses_existing_repair_action_and_one_transaction(
    tmp_path, monkeypatch,
) -> None:
    service, vault, old, desired, protected = _legacy_upgrade(tmp_path, monkeypatch)
    status = service.status(vault)
    assert (status.state, status.action) == ("update_available", "repair")

    result = service.install(vault)

    assert result.state == "updated"
    assert set(result.changed_paths) == (
        set(V2_TARGETS) - {"CLAUDE.md"} | set(V3_NEW_TARGETS)
        | {".kd/wiki-kit.json"}
    )
    for path, body in desired.items():
        assert (vault / path).read_bytes() == body
    assert (vault / "CLAUDE.md").read_bytes() == old["CLAUDE.md"]
    assert read_receipt(vault) is not None
    assert service.status(vault).state == "ready"
    _assert_protected(vault, protected)


def test_interrupted_v2_adoption_rolls_back_all_owned_changes(
    tmp_path, monkeypatch,
) -> None:
    service, vault, old, _desired, protected = _legacy_upgrade(tmp_path, monkeypatch)

    class Crash(BaseException): pass
    monkeypatch.setattr(
        install_module, "_event",
        lambda name, _path: (_ for _ in ()).throw(Crash())
        if name == "after_replace" else None,
    )
    with pytest.raises(Crash):
        service.install(vault)
    assert service.status(vault).state == "recovery_required"

    monkeypatch.setattr(install_module, "_event", lambda *_args: None)
    assert service.recover(vault).state == "recovered"
    assert {path: (vault / path).read_bytes() for path in V2_TARGETS} == old
    assert all(not (vault / path).exists() for path in V3_NEW_TARGETS)
    assert not (vault / ".kd/wiki-kit.json").exists()
    _assert_protected(vault, protected)


def test_v2_adoption_recovery_preserves_user_edit_after_crash(
    tmp_path, monkeypatch,
) -> None:
    service, vault, _old, _desired, protected = _legacy_upgrade(tmp_path, monkeypatch)

    class Crash(BaseException): pass
    monkeypatch.setattr(
        install_module, "_event",
        lambda name, _path: (_ for _ in ()).throw(Crash())
        if name == "after_replace" else None,
    )
    with pytest.raises(Crash):
        service.install(vault)
    (vault / "AGENTS.md").write_bytes(b"user after crash\n")
    monkeypatch.setattr(install_module, "_event", lambda *_args: None)

    with pytest.raises(WikiKitInstallError, match="recovery_conflict"):
        service.recover(vault)
    assert (vault / "AGENTS.md").read_bytes() == b"user after crash\n"
    assert service.status(vault).state == "recovery_required"
    _assert_protected(vault, protected)


def test_v2_adoption_plan_to_write_change_is_preserved_and_writes_nothing_else(
    tmp_path, monkeypatch,
) -> None:
    service, vault, old, _desired, protected = _legacy_upgrade(tmp_path, monkeypatch)
    original = install_module.plan_upgrade

    def change_after_plan(*args):
        planned = original(*args)
        (vault / "AGENTS.md").write_bytes(b"user during plan\n")
        return planned

    monkeypatch.setattr(install_module, "plan_upgrade", change_after_plan)
    with pytest.raises(WikiKitInstallError, match="install_conflict"):
        service.install(vault)
    assert (vault / "AGENTS.md").read_bytes() == b"user during plan\n"
    assert {path: (vault / path).read_bytes()
            for path in V2_TARGETS if path != "AGENTS.md"} == {
                path: body for path, body in old.items() if path != "AGENTS.md"
            }
    assert all(not (vault / path).exists() for path in V3_NEW_TARGETS)
    assert not (vault / ".kd/wiki-kit.json").exists()
    _assert_protected(vault, protected)


def test_v2_adoption_exit_after_commit_recovers_as_committed(
    tmp_path, monkeypatch,
) -> None:
    service, vault, _old, desired, protected = _legacy_upgrade(tmp_path, monkeypatch)

    class Crash(BaseException): pass
    monkeypatch.setattr(
        install_module, "_event",
        lambda name, _path: (_ for _ in ()).throw(Crash())
        if name == "after_commit" else None,
    )
    with pytest.raises(Crash):
        service.install(vault)
    assert service.status(vault).state == "recovery_required"

    monkeypatch.setattr(install_module, "_event", lambda *_args: None)
    assert service.recover(vault).state == "committed"
    assert {path: (vault / path).read_bytes() for path in desired} == desired
    assert service.status(vault).state == "ready"
    _assert_protected(vault, protected)


def test_no_receipt_target_and_symlink_are_never_adopted(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "tools").mkdir()
    (vault / "tools/a.py").write_bytes(b"user")
    service = _service(tmp_path, [("tools/a.py", "tools/a.py", b"kit")])
    with pytest.raises(Exception, match="kit_unmanaged_target"):
        service.install(vault)
    assert (vault / "tools/a.py").read_bytes() == b"user"

    (vault / "tools/a.py").unlink()
    (vault / "tools/a.py").symlink_to(tmp_path / "outside")
    with pytest.raises(Exception, match="kit_symlink"):
        service.install(vault)


def test_interrupted_install_recovers_and_post_crash_user_edit_is_preserved(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    service = _service(tmp_path, [("tools/a.py", "tools/a.py", b"new")])

    class Crash(BaseException): pass
    monkeypatch.setattr(install_module, "_event",
                        lambda name, _path: (_ for _ in ()).throw(Crash())
                        if name == "after_replace" else None)
    with pytest.raises(Crash):
        service.install(vault)
    assert (vault / "tools/a.py").read_bytes() == b"new"
    assert service.status(vault).state == "recovery_required"

    monkeypatch.setattr(install_module, "_event", lambda *_args: None)
    assert service.recover(vault).state == "recovered"
    assert not (vault / "tools/a.py").exists()
    assert service.install(vault).state == "installed"

    # A second interrupted upgrade must not overwrite a later user edit.
    upgraded = _service(tmp_path, [("tools/a.py", "tools/a.py", b"v2")], "v2")
    monkeypatch.setattr(install_module, "_event",
                        lambda name, _path: (_ for _ in ()).throw(Crash())
                        if name == "after_replace" else None)
    with pytest.raises(Crash):
        upgraded.install(vault)
    (vault / "tools/a.py").write_bytes(b"user after crash")
    monkeypatch.setattr(install_module, "_event", lambda *_args: None)
    with pytest.raises(WikiKitInstallError, match="recovery_conflict"):
        upgraded.recover(vault)
    assert (vault / "tools/a.py").read_bytes() == b"user after crash"
    assert upgraded.status(vault).state == "recovery_required"


def test_process_exit_mid_install_leaves_recoverable_private_journal(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    service = _service(tmp_path, [("tools/a.py", "tools/a.py", b"new")])
    child = os.fork()
    if child == 0:
        install_module._event = lambda name, _path: os._exit(73) if name == "after_replace" else None
        service.install(vault)
        os._exit(0)
    _pid, status = os.waitpid(child, 0)
    assert os.waitstatus_to_exitcode(status) == 73
    assert service.status(vault).state == "recovery_required"
    assert service.recover(vault).state == "recovered"
    assert not (vault / "tools/a.py").exists()


def test_process_exit_after_commit_recovers_as_committed_without_rollback(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    service = _service(tmp_path, [("tools/a.py", "tools/a.py", b"new")])
    child = os.fork()
    if child == 0:
        install_module._event = lambda name, _path: os._exit(74) if name == "after_commit" else None
        service.install(vault)
        os._exit(0)
    _pid, status = os.waitpid(child, 0)
    assert os.waitstatus_to_exitcode(status) == 74
    assert service.status(vault).state == "recovery_required"
    assert service.recover(vault).state == "committed"
    assert (vault / "tools/a.py").read_bytes() == b"new"
    assert service.status(vault).state == "ready"


def test_change_after_upgrade_plan_is_not_adopted_as_transaction_baseline(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    first = _service(tmp_path, [("tools/a.py", "tools/a.py", b"v1")], "v1")
    first.install(vault)
    second = _service(tmp_path, [("tools/a.py", "tools/a.py", b"v2")], "v2")
    original = install_module.plan_upgrade

    def change_after_plan(*args):
        result = original(*args)
        (vault / "tools/a.py").write_bytes(b"user in plan window")
        return result

    monkeypatch.setattr(install_module, "plan_upgrade", change_after_plan)
    with pytest.raises(WikiKitInstallError, match="install_conflict"):
        second.install(vault)
    assert (vault / "tools/a.py").read_bytes() == b"user in plan window"


def test_symlinked_runtime_is_rejected_before_changing_target_permissions(tmp_path):
    target = tmp_path / "private-target"
    target.mkdir(mode=0o755)
    link = tmp_path / "runtime-link"
    link.symlink_to(target, target_is_directory=True)
    source_service = _service(tmp_path, [("a", "tools/a", b"a")])
    gate = WorkAdmissionGate()
    coordinator = WorkerCoordinator(Worker(), Worker(), gate)
    before = target.stat().st_mode & 0o777
    with pytest.raises(WikiKitInstallError, match="journal_invalid"):
        WikiKitInstaller(source_service.kit_root, link, gate, coordinator)
    assert target.stat().st_mode & 0o777 == before


def test_manual_vault_lock_rejects_install_without_writing(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    service = _service(tmp_path, [("a", "tools/a", b"a")])
    with VaultWriteLock.acquire(vault):
        with pytest.raises(WikiKitInstallError, match="vault_busy"):
            service.install(vault)
    assert not (vault / "tools/a").exists()


def test_final_manifest_verification_rolls_back_owned_writes_and_preserves_other_drift(
        tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    first = _service(tmp_path, [("a", "tools/a", b"v1"),
                                ("b", "tools/b", b"stable")], "v1")
    first.install(vault)
    second = _service(tmp_path, [("a", "tools/a", b"v2"),
                                 ("b", "tools/b", b"stable")], "v2")
    changed = False

    def drift_unchanged_file(name, _path):
        nonlocal changed
        if name == "after_replace" and not changed:
            changed = True
            (vault / "tools/b").write_bytes(b"user during install")

    monkeypatch.setattr(install_module, "_event", drift_unchanged_file)
    with pytest.raises(WikiKitInstallError, match="install_failed"):
        second.install(vault)
    assert (vault / "tools/a").read_bytes() == b"v1"
    assert (vault / "tools/b").read_bytes() == b"user during install"
    assert second.status(vault).state == "conflict"
