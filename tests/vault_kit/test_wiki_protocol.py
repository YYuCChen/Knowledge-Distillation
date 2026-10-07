from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


KIT = Path(__file__).resolve().parents[2] / "vault-kit"


def _install(root: Path) -> None:
    manifest = json.loads((KIT / "kit-manifest.json").read_text(encoding="utf-8"))
    for item in manifest["files"]:
        target = root / item["install_path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(KIT / item["source_path"], target)


def _wrapped(root: Path, *command: str):
    return subprocess.run(
        [sys.executable, str(root / "tools/wiki_session.py"), "--root", str(root),
         "--", *command], capture_output=True, text=True, cwd=root, check=False)


@pytest.fixture
def vault(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    _install(root)
    result = _wrapped(root, sys.executable, str(root / "tools/kb.py"), "init", "--root", str(root))
    assert result.returncode == 0, result.stderr
    return root


def _raw(root: Path, raw_id: str, *, identity: str, extra: str = "") -> Path:
    folder = "外部" if identity == "第三方" else "自述"
    day = raw_id[2:10]
    target = root / "raw" / folder / day[:4] / day[4:6] / f"{raw_id}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        f"---\n编号: {raw_id}\n格式版本: 1\n身份: {identity}\n"
        f"收录于: 2026-10-01T10:00:00+08:00\n{extra}---\n\n合成原文。\n\n^source-1\n",
        encoding="utf-8")
    return target


def test_protocol_scan_is_structured_read_only_and_parses_relations(vault):
    external = _raw(vault, "R-20261001-0001", identity="第三方")
    own = _raw(
        vault, "R-20261001-0002", identity="本人附言",
        extra=("附言对象: R-20261001-0001\n邻接:\n"
               "  - {编号: R-20261001-0001, 间隔秒: 60}\n"),
    )
    before = {path: path.read_bytes() for path in (external, own)}
    result = subprocess.run(
        [sys.executable, str(vault / "tools/kb.py"), "protocol-scan", "--root", str(vault)],
        capture_output=True, text=True, cwd=vault, check=False)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["protocol_version"] == 2
    assert payload["issue_counts"]["错误"] == 0
    assert payload["candidate_count"] == 0
    assert payload["health"] == {
        "eligible": False,
        "due": False,
        "due_reason": "not_eligible",
        "last_lint_date": "",
        "lint_count": 0,
    }
    by_id = {row["raw_id"]: row for row in payload["pending"]}
    assert by_id["R-20261001-0002"]["identity"] == "本人附言"
    assert by_id["R-20261001-0002"]["addendum_target"] == "R-20261001-0001"
    assert by_id["R-20261001-0002"]["adjacent_raw_ids"] == ["R-20261001-0001"]
    assert len(by_id["R-20261001-0001"]["content_sha256"]) == 64
    assert {path: path.read_bytes() for path in before} == before


def test_protocol_scan_reports_unreadable_or_symlink_raw_in_counts(vault, tmp_path):
    bad = vault / "raw/外部/2026/10/R-20261001-0001.md"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_bytes(b"\xff\xfe")
    link = vault / "raw/自述/2026/10/R-20261001-0002.md"
    link.parent.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    link.symlink_to(outside)
    result = subprocess.run(
        [sys.executable, str(vault / "tools/kb.py"), "protocol-scan", "--root", str(vault)],
        capture_output=True, text=True, cwd=vault, check=False)
    payload = json.loads(result.stdout)
    assert payload["issue_counts"]["错误"] >= 2
    assert payload["pending"] == []


def test_mutating_kb_refuses_without_live_session(vault):
    result = subprocess.run(
        [sys.executable, str(vault / "tools/kb.py"), "--root", str(vault)],
        capture_output=True, text=True, cwd=vault, check=False)
    assert result.returncode != 0
    assert "vault_lock_required" in result.stderr


def test_health_status_uses_candidate_source_and_ordered_valid_log_events(vault):
    today = dt.date.today()
    ingest_day = (today - dt.timedelta(days=1)).isoformat()
    current_lint_day = today.isoformat()
    due_lint_day = (today - dt.timedelta(days=7)).isoformat()
    future_lint_day = (today + dt.timedelta(days=1)).isoformat()
    raw = _raw(vault, "R-20261001-0001", identity="第三方")
    log = vault / "wiki/log.md"
    log.write_text(
        f"# 变更日志\n\n## [{ingest_day}] ingest | 合成\n"
        f"- 已处理 {raw.relative_to(vault).as_posix()}\n",
        encoding="utf-8")

    def scan():
        result = subprocess.run(
            [sys.executable, str(vault / "tools/kb.py"), "protocol-scan", "--root", str(vault)],
            capture_output=True, text=True, cwd=vault, check=False)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    first = scan()
    assert first["pending"] == []
    assert first["health"]["due_reason"] == "first"
    assert first["health"]["due"] is True

    with open(log, "a", encoding="utf-8") as stream:
        stream.write(f"\n## [{current_lint_day}] lint | 合成体检\n- 完成。\n")
    current = scan()
    assert current["health"] == {
        "eligible": True,
        "due": False,
        "due_reason": "current",
        "last_lint_date": current_lint_day,
        "lint_count": 1,
    }

    with open(log, "a", encoding="utf-8") as stream:
        stream.write(f"\n## [{today.isoformat()}] confirm | 合成确认\n- 完成。\n")
    waiting = scan()
    assert waiting["health"]["due_reason"] == "changed_waiting"
    assert waiting["health"]["due"] is False

    log.write_text(log.read_text(encoding="utf-8").replace(
        f"[{current_lint_day}] lint", f"[{due_lint_day}] lint"), encoding="utf-8")
    due = scan()
    assert due["health"]["due_reason"] == "changed_due"
    assert due["health"]["due"] is True

    with open(log, "a", encoding="utf-8") as stream:
        stream.write(f"\n## [{future_lint_day}] lint | 未来无效\n- 不可信。\n")
    unknown = scan()
    assert unknown["issue_counts"]["错误"] >= 1
    assert unknown["health"]["due_reason"] == "unknown"
