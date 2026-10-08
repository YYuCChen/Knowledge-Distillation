from __future__ import annotations

import hashlib
import json
from pathlib import Path
import runpy
import shutil
import subprocess
import sys

import pytest

from knowledge_distiller.v1.wiki_kit import verify_source_kit
from knowledge_distiller.v1.wiki_kit_runtime import (
    WikiKitRuntime,
    WikiKitRuntimeError,
    bundled_kit_root,
    helper_main,
)


KIT = Path(__file__).resolve().parents[2] / "vault-kit"


def _vault(root: Path) -> Path:
    root.mkdir()
    manifest = verify_source_kit(KIT)
    for item in manifest.files:
        target = root / item.install_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(KIT / item.source_path, target)
    (root / ".kd").mkdir(exist_ok=True)
    (root / ".kd/wiki-kit.json").write_text(json.dumps({
        "kit_version": manifest.kit_version,
        "protocol_version": manifest.protocol_version,
        "manifest_sha256": manifest.manifest_sha256,
        "files": [item.__dict__ for item in manifest.files],
    }, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    initialized = subprocess.run(
        [sys.executable, str(root / "tools/wiki_session.py"), "--root", str(root),
         "--", sys.executable, str(root / "tools/kb.py"), "init", "--root", str(root)],
        cwd=root, capture_output=True, text=True, check=False)
    assert initialized.returncode == 0, initialized.stderr
    return root


def test_source_runtime_uses_verified_absolute_python_and_ignores_hostile_pythonpath(
    tmp_path, monkeypatch
):
    vault = _vault(tmp_path / "vault")
    hostile = tmp_path / "hostile"
    hostile.mkdir()
    canary = tmp_path / "canary"
    (hostile / "sitecustomize.py").write_text(
        f"from pathlib import Path;Path({str(canary)!r}).write_text('ran')",
        encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(hostile))
    runtime = WikiKitRuntime(KIT, python_executable=sys.executable)

    command = runtime.command("kb", vault, ("protocol-scan",))
    assert Path(command[0]).is_absolute()
    assert command[1:3] == ("-E", "-B")
    result = runtime.run("kb", vault, ("protocol-scan",))
    assert result.returncode == 0
    assert json.loads(result.stdout)["protocol_version"] == 2
    assert not canary.exists()


def test_source_runtime_dispatches_display_with_root_after_explicit_arguments(
    tmp_path,
):
    vault = _vault(tmp_path / "vault")
    journal = tmp_path / "display-journal"
    runtime = WikiKitRuntime(KIT, python_executable=sys.executable)

    command = runtime.command(
        "display",
        vault,
        ("plan", "--journal-root", str(journal)),
    )
    assert command == (
        sys.executable,
        "-E",
        "-B",
        str(KIT / "tools/wiki_display.py"),
        "plan",
        "--journal-root",
        str(journal),
        "--root",
        str(vault),
    )
    result = runtime.run(
        "display",
        vault,
        ("plan", "--journal-root", str(journal)),
    )
    assert result.returncode == 0
    assert json.loads(result.stdout)["state"] == "planned"


def test_source_runtime_probes_the_selected_interpreter_not_parent_version(tmp_path):
    fake = tmp_path / "python"
    fake.write_text("#!/bin/sh\nprintf '%s\\n' '[\"CPython\",\"9.9.9\"]'\n", encoding="utf-8")
    fake.chmod(0o700)
    with pytest.raises(WikiKitRuntimeError, match="runner_unavailable"):
        WikiKitRuntime(KIT, python_executable=fake).verify()


def test_frozen_command_uses_only_internal_helper_and_bundled_manifest(tmp_path):
    vault = _vault(tmp_path / "vault")
    app = tmp_path / "KnowledgeDistiller"
    app.write_text("synthetic", encoding="utf-8")
    runtime = WikiKitRuntime(KIT, frozen_executable=app)
    command = runtime.command("session", vault, ("status",))
    assert command == (
        str(app), "--wiki-kit", "session", "--vault-root", str(vault), "--", "status")
    assert "python" not in " ".join(command).lower()

    display = runtime.command(
        "display",
        vault,
        ("plan", "--journal-root", str(tmp_path / "journal")),
    )
    assert display == (
        str(app), "--wiki-kit", "display", "--vault-root", str(vault), "--",
        "plan", "--journal-root", str(tmp_path / "journal"),
    )

    with pytest.raises(WikiKitRuntimeError, match="kit_incompatible"):
        runtime.command("../wiki_display", vault)
    managed = runtime.command('managed', vault, ('describe-generated',))
    assert managed == (str(app), '--wiki-kit', 'managed', '--vault-root', str(vault), '--', 'describe-generated')
    assert '-c' not in managed
    assert runtime.command('managed', vault, ('describe-state',)) == (
        str(app), '--wiki-kit', 'managed', '--vault-root', str(vault), '--', 'describe-state')


def test_helper_dispatch_runs_bundled_protocol_without_opening_application(
    tmp_path, monkeypatch, capsys
):
    vault = _vault(tmp_path / "vault")
    monkeypatch.setattr(
        "knowledge_distiller.v1.wiki_kit_runtime.bundled_kit_root", lambda: KIT)
    assert helper_main(["kb", "--vault-root", str(vault), "--", "protocol-scan"]) == 0
    assert json.loads(capsys.readouterr().out)["protocol_version"] == 2

    journal = tmp_path / "display-journal"
    assert helper_main([
        "display", "--vault-root", str(vault), "--", "plan",
        "--journal-root", str(journal),
    ]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "planned"

    assert helper_main([
        "wiki_display.py", "--vault-root", str(vault), "--", "plan",
        "--journal-root", str(tmp_path / "invalid-journal"),
    ]) == 2


def test_frozen_bundle_resolves_internal_resource_link_and_dispatches(
    tmp_path, monkeypatch, capsys,
):
    vault = _vault(tmp_path / "vault")
    contents = tmp_path / "KnowledgeDistiller.app/Contents"
    frameworks = contents / "Frameworks"
    resources = contents / "Resources"
    executable = contents / "MacOS/KnowledgeDistiller"
    frameworks.mkdir(parents=True)
    resources.mkdir()
    executable.parent.mkdir()
    executable.write_bytes(b"synthetic executable")
    shutil.copytree(KIT, resources / "vault-kit")
    (frameworks / "vault-kit").symlink_to("../Resources/vault-kit", target_is_directory=True)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(frameworks), raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))

    assert bundled_kit_root() == (resources / "vault-kit").resolve()
    assert helper_main([
        "kb", "--vault-root", str(vault), "--", "protocol-scan",
    ]) == 0
    assert json.loads(capsys.readouterr().out)["protocol_version"] == 2
    # Actual frozen application dispatch; no AppPaths/DB/server/model startup.
    from knowledge_distiller.v1.mac_app import main
    before = {p.relative_to(vault): p.read_bytes() for p in vault.rglob('*') if p.is_file()}
    (vault / 'tools/kb.py').write_text("raise AssertionError('staging kb imported')\n")
    staged = (vault / 'tools/kb.py').read_bytes()
    with pytest.raises(SystemExit) as stopped:
        main(['--wiki-kit', 'managed', '--vault-root', str(vault), '--', 'describe-generated'])
    assert stopped.value.code == 0
    rows = json.loads(capsys.readouterr().out)
    assert {r[0] for r in rows} >= {'wiki/index.md', 'wiki/待确认.md', 'wiki/主题/AI.md'}
    with pytest.raises(SystemExit) as state_stopped:
        main(['--wiki-kit', 'managed', '--vault-root', str(vault), '--', 'describe-state'])
    assert state_stopped.value.code == 0
    facts = json.loads(capsys.readouterr().out)
    assert facts['pages'] and all(set(p) == {'path', 'sha256', 'type', 'confirmed', 'declared_topics'}
                                  for p in facts['pages'])
    assert (vault / 'tools/kb.py').read_bytes() == staged
    assert all(p.read_bytes() == data for rel, data in before.items()
               if rel.as_posix() != 'tools/kb.py' for p in [vault / rel])
    assert helper_main(['managed', '--vault-root', str(vault), '--', 'unapproved-operation']) == 2


def test_source_managed_dispatch_renders_without_using_staging_code(tmp_path):
    vault = _vault(tmp_path / 'vault')
    (vault / 'tools/kb.py').write_text("raise AssertionError('untrusted staging code')\n")
    result = WikiKitRuntime(KIT, python_executable=sys.executable).run('managed', vault, ('describe-generated',))
    assert result.returncode == 0
    assert any(row[0] == 'wiki/index.md' for row in json.loads(result.stdout))


def test_frozen_bundle_rejects_resource_link_outside_application(
    tmp_path, monkeypatch,
):
    contents = tmp_path / "KnowledgeDistiller.app/Contents"
    frameworks = contents / "Frameworks"
    executable = contents / "MacOS/KnowledgeDistiller"
    outside = tmp_path / "outside/vault-kit"
    frameworks.mkdir(parents=True)
    executable.parent.mkdir()
    executable.write_bytes(b"synthetic executable")
    shutil.copytree(KIT, outside)
    (frameworks / "vault-kit").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(frameworks), raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))

    with pytest.raises(WikiKitRuntimeError, match="kit_symlink"):
        bundled_kit_root()


def test_mac_release_resources_are_exactly_manifest_owned_files():
    resources = runpy.run_path(str(KIT.parent / "packaging/resources.py"))
    rows = resources["vault_kit_datas"](KIT.parent)
    destinations = {
        (Path(destination) / Path(source).name).as_posix()
        for source, destination in rows
    }
    manifest = json.loads((KIT / "kit-manifest.json").read_text(encoding="utf-8"))
    assert destinations == {"vault-kit/kit-manifest.json"} | {
        "vault-kit/" + item["source_path"] for item in manifest["files"]}
    for source, _destination in rows:
        relative = Path(source).relative_to(KIT).as_posix()
        if relative == "kit-manifest.json":
            continue
        expected = next(item["sha256"] for item in manifest["files"]
                        if item["source_path"] == relative)
        assert hashlib.sha256(Path(source).read_bytes()).hexdigest() == expected
