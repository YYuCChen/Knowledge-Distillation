from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil

import pytest

from knowledge_distiller.v1 import wiki_kit as kit_module
from knowledge_distiller.v1.wiki_kit import (
    KitFile,
    WikiKitError,
    plan_upgrade,
    read_receipt,
    verify_installed_kit,
    verify_source_kit,
)


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


def _mock_v2_release(monkeypatch):
    bodies = {path: f"v2:{path}\n".encode() for path in V2_TARGETS}
    monkeypatch.setattr(kit_module, "_V2_UNRECEIPTED_FILES", tuple(
        KitFile(path, path, hashlib.sha256(body).hexdigest())
        for path, body in bodies.items()
    ))
    return bodies


def _v2_upgrade_fixture(tmp_path, monkeypatch, *, missing=None, changed=None,
                        occupied_new=None):
    old = _mock_v2_release(monkeypatch)
    desired_bodies = {
        path: (old[path] if path == "CLAUDE.md" else f"v3:{path}\n".encode())
        for path in V2_TARGETS
    }
    desired_bodies.update({path: f"v3:{path}\n".encode() for path in V3_NEW_TARGETS})
    source = tmp_path / "source"
    desired = _kit(source, [
        (path, path, body) for path, body in desired_bodies.items()
    ], version="v3")
    vault = tmp_path / "vault"
    vault.mkdir()
    for path, body in old.items():
        if path == missing:
            continue
        target = vault / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"user changed\n" if path == changed else body)
    if occupied_new:
        target = vault / occupied_new
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"user file\n")
    return vault, source, desired, old


def _kit(root: Path, entries, *, version="test-1"):
    root.mkdir()
    files = []
    for source, target, content in entries:
        path = root / source
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        files.append({"source_path": source, "install_path": target,
                      "sha256": hashlib.sha256(content).hexdigest()})
    payload = {"kit_version": version, "protocol_version": 1, "files": files}
    (root / "kit-manifest.json").write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return verify_source_kit(root)


def _install(vault: Path, source: Path):
    manifest = verify_source_kit(source)
    for item in manifest.files:
        target = vault / item.install_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / item.source_path, target)
    receipt = {
        "kit_version": manifest.kit_version,
        "protocol_version": manifest.protocol_version,
        "manifest_sha256": manifest.manifest_sha256,
        "files": [item.__dict__ for item in manifest.files],
    }
    target = vault / ".kd/wiki-kit.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(receipt, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return manifest


def test_receipt_verifies_owned_files_and_detects_drift(tmp_path):
    source = tmp_path / "source"
    desired = _kit(source, [("tools/a.py", "tools/a.py", b"one")])
    vault = tmp_path / "vault"
    vault.mkdir()
    _install(vault, source)
    receipt = read_receipt(vault)
    assert receipt == desired
    verify_installed_kit(vault, receipt)
    assert plan_upgrade(vault, source) == (desired, ())
    (vault / "tools/a.py").write_bytes(b"user edit")
    with pytest.raises(WikiKitError, match="kit_drift"):
        plan_upgrade(vault, source)


def test_new_manifest_target_never_overwrites_unmanaged_file(tmp_path):
    old_source = tmp_path / "old"
    _kit(old_source, [("a", "tools/a", b"a")], version="old")
    vault = tmp_path / "vault"
    vault.mkdir()
    _install(vault, old_source)
    (vault / "tools/new").write_bytes(b"user")
    new_source = tmp_path / "new"
    _kit(new_source, [("a", "tools/a", b"a"), ("new", "tools/new", b"kit")], version="new")
    with pytest.raises(WikiKitError, match="kit_unmanaged_target"):
        plan_upgrade(vault, new_source)


def test_v2_unreceipted_identity_is_pinned_to_the_published_tag() -> None:
    assert {item.install_path: item.sha256 for item in kit_module._V2_UNRECEIPTED_FILES} == {
        "AGENTS.md": "11cde287539983c48d3506a38336b4323685550463d9e37733150e8c548b2cc6",
        "CLAUDE.md": "373b06b72e1ccc4851755bc6ebd2b7e45298397289363d4041b3610c15a2e423",
        "tools/kb.py": "95843adb3fe7ec4aa2421a5c77b2deafa4606c64950002975260147d1367805e",
        ".agents/skills/kb-confirm/SKILL.md": "64645bb356c4b91066b649cb7da138c0ebb84b030053f0c56f1d62c222e8418c",
        ".agents/skills/kb-ingest/SKILL.md": "fdab61a1add73b03c03174dc737f75f9930d4c8fdffdf17e3c06ea0e426e7e78",
        ".agents/skills/kb-lint/SKILL.md": "140298f393959092983c49ea3926761beec0a8e5134eab92910e97eb8c2294b5",
        ".agents/skills/kb-read/SKILL.md": "b1792923b8656c59ba7c32ba05430ed78fb7c04733893e826b044029896ac2f8",
    }


def test_exact_complete_v2_install_gets_cas_upgrade_plan_without_writes(
    tmp_path, monkeypatch,
) -> None:
    vault, source, desired, old = _v2_upgrade_fixture(tmp_path, monkeypatch)
    before = {path: (vault / path).read_bytes() for path in V2_TARGETS}

    planned, actions = plan_upgrade(vault, source)

    assert planned == desired
    by_path = {action.install_path: action for action in actions}
    assert set(by_path) == set(V2_TARGETS) - {"CLAUDE.md"} | set(V3_NEW_TARGETS)
    for path in set(V2_TARGETS) - {"CLAUDE.md"}:
        assert by_path[path].expected_before == hashlib.sha256(old[path]).hexdigest()
    for path in V3_NEW_TARGETS:
        assert by_path[path].expected_before is None
    assert {path: (vault / path).read_bytes() for path in V2_TARGETS} == before
    assert not (vault / ".kd/wiki-kit.json").exists()


@pytest.mark.parametrize("kind", ["missing", "changed"])
@pytest.mark.parametrize("target", V2_TARGETS)
def test_partial_or_changed_v2_install_is_never_adopted(
    tmp_path, monkeypatch, kind, target,
) -> None:
    options = {kind: target}
    vault, source, _desired, _old = _v2_upgrade_fixture(
        tmp_path, monkeypatch, **options)
    before = {
        path.relative_to(vault).as_posix(): path.read_bytes()
        for path in vault.rglob("*") if path.is_file()
    }
    with pytest.raises(WikiKitError, match="kit_unmanaged_target"):
        plan_upgrade(vault, source)
    assert {
        path.relative_to(vault).as_posix(): path.read_bytes()
        for path in vault.rglob("*") if path.is_file()
    } == before


@pytest.mark.parametrize("target", V3_NEW_TARGETS)
def test_exact_v2_install_does_not_take_over_an_existing_v3_target(
    tmp_path, monkeypatch, target,
) -> None:
    vault, source, _desired, _old = _v2_upgrade_fixture(
        tmp_path, monkeypatch, occupied_new=target)
    with pytest.raises(WikiKitError, match="kit_unmanaged_target"):
        plan_upgrade(vault, source)
    assert (vault / target).read_bytes() == b"user file\n"


def test_manifest_and_installed_paths_reject_traversal_and_symlinks(tmp_path):
    bad = tmp_path / "bad"
    bad.mkdir()
    payload = {"kit_version": "x", "protocol_version": 1, "files": [{
        "source_path": "../secret", "install_path": "tools/a", "sha256": "0" * 64,
    }]}
    (bad / "kit-manifest.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(WikiKitError, match="kit_manifest_invalid"):
        verify_source_kit(bad)

    source = tmp_path / "source"
    _kit(source, [("a", "tools/a", b"a")])
    vault = tmp_path / "vault"
    vault.mkdir()
    manifest = _install(vault, source)
    (vault / "tools/a").unlink()
    (vault / "tools/a").symlink_to(tmp_path / "outside")
    with pytest.raises(WikiKitError, match="kit_symlink"):
        verify_installed_kit(vault, manifest)

    source_link = tmp_path / "source-link"
    source_link.symlink_to(source, target_is_directory=True)
    with pytest.raises(WikiKitError, match="kit_symlink"):
        verify_source_kit(source_link)
