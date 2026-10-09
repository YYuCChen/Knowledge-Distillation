from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from knowledge_distiller.v1 import wiki_style as style_module
from knowledge_distiller.v1.wiki_kit_install import WikiKitInstaller
from knowledge_distiller.v1.wiki_style import WikiStyleError, WikiStyleService
from knowledge_distiller.v1.worker_lifecycle import WorkAdmissionGate, WorkerCoordinator


class Worker:
    def start(self): return True
    def stop(self, _timeout=0): return True
    def reserve_for_update(self): return True
    def release_update(self): pass
    def update_ready(self): return True


def _kit(root: Path, css: bytes, version: str) -> Path:
    kit = root / f"kit-{version}"
    kit.mkdir()
    asset = kit / "styles/kd-wiki.css"
    asset.parent.mkdir()
    asset.write_bytes(css)
    manifest = {"kit_version": version, "protocol_version": 2, "files": [{
        "source_path": "styles/kd-wiki.css",
        "install_path": ".kd/assets/kd-wiki.css",
        "sha256": hashlib.sha256(css).hexdigest(),
    }]}
    (kit / "kit-manifest.json").write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    return kit


def _services(tmp_path: Path, css=b"v1", version="v1"):
    gate = WorkAdmissionGate()
    coordinator = WorkerCoordinator(Worker(), Worker(), gate)
    runtime = tmp_path / "runtime"
    return (WikiKitInstaller(_kit(tmp_path, css, version), runtime, gate, coordinator),
            WikiStyleService(runtime, gate, coordinator))


def test_style_install_and_enable_are_separate_and_preserve_appearance(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / ".obsidian").mkdir()
    original = {"theme": "moon", "enabledCssSnippets": ["mine"], "unknown": {"x": 1}}
    (vault / ".obsidian/appearance.json").write_text(json.dumps(original), encoding="utf-8")
    kit, style = _services(tmp_path)
    kit.install(vault)

    assert style.status(vault).state == "missing"
    result = style.install(vault)
    assert result.state == "installed"
    assert style.status(vault).state == "installed"
    assert json.loads((vault / ".obsidian/appearance.json").read_text()) == original

    assert style.enable(vault).state == "enabled"
    enabled = json.loads((vault / ".obsidian/appearance.json").read_text())
    assert enabled == {"theme": "moon", "enabledCssSnippets": ["mine", "kd-wiki"],
                       "unknown": {"x": 1}}
    assert style.enable(vault).changed_paths == ()
    assert style.disable(vault).state == "installed"
    disabled = json.loads((vault / ".obsidian/appearance.json").read_text())
    assert disabled["enabledCssSnippets"] == ["mine"]
    assert disabled["theme"] == "moon" and disabled["unknown"] == {"x": 1}


def test_style_update_preserves_enabled_state_and_unmanaged_target_conflicts(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    kit, style = _services(tmp_path)
    kit.install(vault)
    style.install(vault)
    style.enable(vault)

    new_kit_root = _kit(tmp_path, b"v2", "v2")
    new_kit = WikiKitInstaller(new_kit_root, tmp_path / "runtime", style.gate,
                               style.coordinator)
    new_kit.install(vault)
    assert style.status(vault).state == "update_available"
    assert style.install(vault).state == "updated"
    assert style.status(vault).state == "enabled"
    assert (vault / ".obsidian/snippets/kd-wiki.css").read_bytes() == b"v2"

    other = tmp_path / "other"
    other.mkdir()
    kit2, style2 = _services(other)
    vault2 = other / "vault"
    vault2.mkdir()
    kit2.install(vault2)
    (vault2 / ".obsidian/snippets").mkdir(parents=True)
    (vault2 / ".obsidian/snippets/kd-wiki.css").write_bytes(b"user")
    assert style2.status(vault2).state == "conflict"
    with pytest.raises(WikiStyleError, match="style_unmanaged_target"):
        style2.install(vault2)
    assert (vault2 / ".obsidian/snippets/kd-wiki.css").read_bytes() == b"user"


def test_style_drift_and_appearance_symlink_are_preserved(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    kit, style = _services(tmp_path)
    kit.install(vault)
    style.install(vault)
    snippet = vault / ".obsidian/snippets/kd-wiki.css"
    snippet.write_bytes(b"user edit")
    assert style.status(vault).state == "conflict"
    with pytest.raises(WikiStyleError, match="style_drift"):
        style.install(vault)
    assert snippet.read_bytes() == b"user edit"

    snippet.write_bytes(b"v1")
    appearance = vault / ".obsidian/appearance.json"
    appearance.symlink_to(tmp_path / "outside")
    assert style.status(vault).state == "conflict"
    with pytest.raises(Exception, match="install_symlink"):
        style.enable(vault)


def test_appearance_change_after_parse_is_not_overwritten(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    kit, style = _services(tmp_path)
    kit.install(vault)
    style.install(vault)
    (vault / ".obsidian/appearance.json").write_text(
        json.dumps({"theme": "before", "enabledCssSnippets": []}), encoding="utf-8")
    original = style_module._appearance

    calls = 0

    def change_after_parse(selected):
        nonlocal calls
        calls += 1
        result = original(selected)
        if calls == 2:
            (vault / ".obsidian/appearance.json").write_text(
                json.dumps({"theme": "user", "enabledCssSnippets": ["mine"]}), encoding="utf-8")
        return result

    monkeypatch.setattr(style_module, "_appearance", change_after_parse)
    with pytest.raises(Exception, match="install_conflict"):
        style.enable(vault)
    assert json.loads((vault / ".obsidian/appearance.json").read_text()) == {
        "theme": "user", "enabledCssSnippets": ["mine"]}
