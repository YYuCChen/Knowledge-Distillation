from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
KIT = ROOT / "vault-kit"
DISPLAY_PATH = KIT / "tools/wiki_display.py"
SPEC = importlib.util.spec_from_file_location("wiki_display_tested", DISPLAY_PATH)
DISPLAY = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = DISPLAY
SPEC.loader.exec_module(DISPLAY)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _install(root: Path) -> None:
    manifest = json.loads((KIT / "kit-manifest.json").read_text(encoding="utf-8"))
    for item in manifest["files"]:
        target = root / item["install_path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(KIT / item["source_path"], target)


def _locked(root: Path, *command: str):
    return subprocess.run(
        [sys.executable, str(root / "tools/wiki_session.py"), "--root", str(root), "--", *command],
        cwd=root, capture_output=True, text=True, check=False,
    )


def _display_cli(root: Path, journal: Path, command: str, plan_id: str | None = None):
    args = [sys.executable, str(root / "tools/wiki_display.py"), command,
            "--journal-root", str(journal)]
    if plan_id:
        args += ["--plan-id", plan_id]
    args += ["--root", str(root)]  # The packaged runtime may append root last.
    if command == "plan":
        return subprocess.run(args, cwd=root, capture_output=True, text=True, check=False)
    return _locked(root, *args)


def _old_vault(tmp_path: Path, *, body_changed_after_hash: bool = False):
    root = tmp_path / "vault"
    root.mkdir()
    _install(root)
    for directory in ["wiki/来源", "raw/外部/2026/10", ".graph"]:
        (root / directory).mkdir(parents=True, exist_ok=True)
    page = root / "wiki/来源/来源：合成材料.md"
    original = """---
编号: SRC-0001
类型: 来源
创建: 2026-09-01
更新: 2026-09-01
cssclasses: [user-reading, kd-wiki]
主题: [AI]
作者: 合成作者
平台: 合成平台
发布日期: 2026-09-01
素材类型: 其他
原始文件: raw/外部/2026/10/R-20261001-0001.md
---

# 来源：合成材料

## 摘要
合成摘要。

## 核心论点
- 合成作者认为需要保留证据。（[[raw/外部/2026/10/R-20261001-0001.md#^source-1|原始段落]]）

## 引发的想法
"""
    page.write_text(original, encoding="utf-8")
    raw = root / "raw/外部/2026/10/R-20261001-0001.md"
    raw.write_text("---\n编号: R-20261001-0001\n身份: 第三方\n---\n\n合成原文。\n\n^source-1\n",
                   encoding="utf-8")
    for name, value in {
        "index.md": "# 索引\n",
        "待确认.md": "# 待确认清单\n",
        "log.md": "# 变更日志\n",
        "体检报告.md": "# 体检报告\n\n合成检查。\n",
    }.items():
        (root / "wiki" / name).write_text(value, encoding="utf-8")
    state = {"hashes": {"wiki/来源/来源：合成材料.md": DISPLAY.legacy_content_hash(original)}}
    state_path = root / ".graph/state.json"
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    if body_changed_after_hash:
        page.write_text(original.replace("合成摘要。", "合成摘要已经由用户修改。"), encoding="utf-8")
    return root, page, raw, state_path


@pytest.mark.parametrize(
    ("relative", "frontmatter", "css_class", "label"),
    [
        ("wiki/来源/来源：A.md", "主题: [AI]\n发布日期: 2026-10-01", "kd-wiki-source", "来源"),
        ("wiki/概念/A.md", "主题: [AI]\n子类: 术语", "kd-wiki-concept", "概念"),
        ("wiki/认知/A.md", "主题: [AI]\n确认: 候选\n证据: 有据", "kd-wiki-cognition", "认知候选"),
        ("wiki/方法/A.md", "主题: [AI]\n确认: 候选\n证据: 推测", "kd-wiki-method", "方法候选"),
        ("wiki/技能/A.md", "主题: [AI]\n确认: 候选", "kd-wiki-skill", "技能候选"),
        ("wiki/实践/A.md", "主题: [AI]\n确认: 候选\n结果: 未知", "kd-wiki-practice", "实践候选"),
        ("wiki/综合/综合：A.md", "主题: [AI]", "kd-wiki-synthesis", "综合"),
        ("wiki/主题/AI.md", "类型: 主题", "kd-wiki-topic", "主题"),
    ],
)
def test_eight_page_templates_add_only_verified_display(relative, frontmatter, css_class, label):
    original = f"---\ncssclasses: [user-class, kd-wiki]\n{frontmatter}\n---\n\n# {Path(relative).stem}\n\n## 正文\n[[证据页]] raw/外部/2026/10/R-20261001-0001.md#^source-1\n"
    before = (
        DISPLAY.business_sha256(original, relative),
        DISPLAY.identity_sha256(original, relative),
        DISPLAY.references_sha256(original, relative),
    )
    rendered, changed = DISPLAY.add_or_refresh_display(original, relative)
    assert changed is True
    assert f"{css_class}]" in rendered
    assert "cssclasses: [user-class, kd-wiki, kd-reading," in rendered
    marker = next(line for line in rendered.splitlines() if line.startswith("<!-- kd-wiki-display:"))
    assert "added=kd-reading," in marker and "added=kd-reading,kd-wiki," not in marker
    assert f"**{label}**" in rendered
    assert rendered.count("# " + Path(relative).stem) == 1
    assert before == (
        DISPLAY.business_sha256(rendered, relative),
        DISPLAY.identity_sha256(rendered, relative),
        DISPLAY.references_sha256(rendered, relative),
    )
    rerendered, rerun_changed = DISPLAY.add_or_refresh_display(rendered, relative)
    assert rerun_changed is False and rerendered == rendered
    with_user_class = rendered.replace("user-class,", "user-class, later-user-class,")
    refreshed, _ = DISPLAY.add_or_refresh_display(with_user_class, relative)
    assert "later-user-class" in refreshed
    assert DISPLAY.business_sha256(with_user_class, relative) != before[0]


@pytest.mark.parametrize(
    ("relative", "css_class"),
    [("wiki/index.md", "kd-wiki-index"), ("wiki/待确认.md", "kd-wiki-pending"),
     ("wiki/log.md", "kd-wiki-log"), ("wiki/体检报告.md", "kd-wiki-health")],
)
def test_four_system_templates_have_system_and_specific_classes(relative, css_class):
    rendered = DISPLAY.render_system_document(relative, Path(relative).stem, "合成正文。\n", detail="合成状态")
    assert "kd-wiki-system" in rendered and css_class in rendered
    assert "[!kd-page]" in rendered and "合成状态" in rendered
    assert DISPLAY.add_or_refresh_display(rendered, relative, detail="合成状态") == (rendered, False)


def test_reserved_marker_cannot_hide_user_class_or_arbitrary_body():
    relative = "wiki/概念/合成概念.md"
    original = "---\ncssclasses: [user-class]\n主题: [AI]\n子类: 术语\n---\n\n# 合成概念\n\n## 定义\n正文。\n"
    rendered, _ = DISPLAY.add_or_refresh_display(original, relative)
    forged_class = re.sub(r"added=[a-z0-9,_-]+ -->", "added=user-class -->", rendered, count=1)
    with pytest.raises(DISPLAY.DisplayError, match="display_class_proof_invalid"):
        DISPLAY.business_sha256(forged_class, relative)

    lines = rendered.splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith("<!-- kd-wiki-display:"))
    end = lines.index("<!-- /kd-wiki-display -->")
    lines.insert(end, "这里是伪装成展示块的用户正文。")
    end += 1
    inner = "\n".join(lines[start + 1:end])
    added = re.search(r"added=([a-z0-9,_-]+|-) -->", lines[start]).group(1)
    lines[start] = f"<!-- kd-wiki-display:v1 inner={_sha(inner.encode())} added={added} -->"
    with pytest.raises(DISPLAY.DisplayError, match="display_block_invalid"):
        DISPLAY.business_sha256("\n".join(lines), relative)


def test_missing_heading_is_owned_by_display_block_and_block_yaml_keeps_user_order():
    relative = "wiki/方法/合成方法.md"
    original = """---
cssclasses:
    - first-user
    - kd-wiki
主题: [AI]
确认: 候选
证据: 有据
---

## 做法
业务正文。
"""
    rendered, _ = DISPLAY.add_or_refresh_display(original, relative)
    assert "    - first-user\n    - kd-wiki\n    - kd-reading\n    - kd-wiki-method" in rendered
    assert rendered.count("# 合成方法") == 1
    assert DISPLAY.business_sha256(rendered, relative) == DISPLAY.business_sha256(original, relative)


def test_approved_css_is_manifest_owned_asset_and_not_an_obsidian_install():
    approved = ROOT / "docs/releases/v3.0/design/obsidian/kd-wiki.css"
    asset = KIT / "styles/kd-wiki.css"
    approved_production = approved.read_text(encoding="utf-8").replace(
        "/* V3.0 design specimen only.",
        "/* V3.0 approved knowledge-wiki reading styles.",
    ).replace(
        ".kd-wiki .markdown-preview-sizer,\n"
        ".markdown-source-view.kd-wiki .cm-contentContainer {\n"
        "  max-width: 640px;\n"
        "  margin-inline: auto;\n"
        "}",
        ".kd-wiki .markdown-preview-sizer {\n"
        "  max-width: 640px;\n"
        "  margin-inline: auto;\n"
        "}\n\n"
        ".markdown-source-view.kd-wiki .cm-contentContainer {\n"
        "  width: 100%;\n"
        "  max-width: 640px;\n"
        "  box-sizing: border-box;\n"
        "  margin-inline: auto;\n"
        "}",
    )
    assert asset.read_text(encoding="utf-8") == approved_production
    manifest = json.loads((KIT / "kit-manifest.json").read_text(encoding="utf-8"))
    row = next(item for item in manifest["files"] if item["source_path"] == "styles/kd-wiki.css")
    assert row["install_path"] == ".kd/assets/kd-wiki.css"
    assert row["sha256"] == _sha(asset.read_bytes())
    assert all(not item["install_path"].startswith(".obsidian/") for item in manifest["files"])


def test_editor_container_has_definite_width_before_the_reading_cap():
    css = (KIT / "styles/kd-wiki.css").read_text(encoding="utf-8")
    rule = re.search(
        r"\.markdown-source-view\.kd-wiki \.cm-contentContainer \{(?P<body>[^}]*)\}",
        css,
    )
    assert rule is not None
    declarations = rule.group("body")
    assert declarations.index("width: 100%") < declarations.index("max-width: 640px")
    assert "box-sizing: border-box" in declarations


def test_plan_apply_repeat_and_revert_restore_exact_pages_state_and_raw(tmp_path):
    root, page, raw, state_path = _old_vault(tmp_path)
    journal = tmp_path / "private-journal"
    before = {path: path.read_bytes() for path in [page, raw, state_path, root / "wiki/index.md",
                                                   root / "wiki/待确认.md", root / "wiki/log.md",
                                                   root / "wiki/体检报告.md"]}
    planned = _display_cli(root, journal, "plan")
    assert planned.returncode == 0, planned.stderr
    plan = json.loads(planned.stdout)
    applied = _display_cli(root, journal, "apply", plan["plan_id"])
    assert applied.returncode == 0, applied.stderr
    applied_payload = json.loads(applied.stdout)
    assert applied_payload["state"] == "committed"
    assert raw.read_bytes() == before[raw]
    text = page.read_text(encoding="utf-8")
    assert "user-reading, kd-wiki, kd-reading, kd-wiki-source" in text
    assert text.count("[!kd-page]") == 1
    state = json.loads(state_path.read_text(encoding="utf-8"))
    relative = page.relative_to(root).as_posix()
    assert state["hash_versions"][relative] == 2
    assert state["hashes"][relative] == DISPLAY.business_content_hash(text, relative)
    repeated = _display_cli(root, journal, "apply", plan["plan_id"])
    assert repeated.returncode == 0 and json.loads(repeated.stdout)["changed_paths"] == 0

    reverted = _display_cli(root, journal, "revert", plan["plan_id"])
    assert reverted.returncode == 0, reverted.stderr
    assert {path: path.read_bytes() for path in before} == before
    repeated_revert = _display_cli(root, journal, "revert", plan["plan_id"])
    assert repeated_revert.returncode == 0
    assert json.loads(repeated_revert.stdout)["changed_paths"] == 0
    assert all(path.stat().st_mode & 0o077 == 0 for path in journal.rglob("*"))


def test_revert_preserves_user_edit_and_does_not_partially_restore_state(tmp_path):
    root, page, raw, state_path = _old_vault(tmp_path)
    journal = tmp_path / "private-journal"
    plan = json.loads(_display_cli(root, journal, "plan").stdout)
    assert _display_cli(root, journal, "apply", plan["plan_id"]).returncode == 0
    page.write_text(page.read_text(encoding="utf-8") + "\n用户在迁移后新增的内容。\n", encoding="utf-8")
    page_after_user = page.read_bytes()
    state_after_apply = state_path.read_bytes()
    raw_before = raw.read_bytes()
    result = _display_cli(root, journal, "revert", plan["plan_id"])
    assert result.returncode != 0 and json.loads(result.stderr)["error_code"] == "recovery_conflict"
    assert page.read_bytes() == page_after_user
    assert state_path.read_bytes() == state_after_apply
    assert raw.read_bytes() == raw_before


def test_apply_requires_live_lock_and_raw_digest_stays_a_hard_gate(tmp_path):
    root, page, raw, state_path = _old_vault(tmp_path)
    journal = tmp_path / "private-journal"
    plan = json.loads(_display_cli(root, journal, "plan").stdout)
    direct = subprocess.run(
        [sys.executable, str(root / "tools/wiki_display.py"), "apply",
         "--plan-id", plan["plan_id"], "--journal-root", str(journal), "--root", str(root)],
        cwd=root, capture_output=True, text=True, check=False,
    )
    assert direct.returncode != 0 and json.loads(direct.stderr)["error_code"] == "vault_lock_required"
    before_page, before_state = page.read_bytes(), state_path.read_bytes()
    raw.write_text(raw.read_text(encoding="utf-8") + "\n用户后改。\n", encoding="utf-8")
    refused = _display_cli(root, journal, "apply", plan["plan_id"])
    assert refused.returncode != 0 and json.loads(refused.stderr)["error_code"] == "raw_changed"
    assert page.read_bytes() == before_page and state_path.read_bytes() == before_state


def test_unmarked_kd_page_callout_is_not_adopted_or_duplicated():
    relative = "wiki/概念/合成概念.md"
    original = """---
cssclasses: [kd-reading, kd-wiki, kd-wiki-concept]
主题: [AI]
子类: 术语
---

# 合成概念

> [!kd-page]
> **概念** · 他人观点与术语  
> 不代表用户已经认可

## 定义
这是既有无 marker 的展示样稿。
"""
    with pytest.raises(DISPLAY.DisplayError, match="display_callout_unowned"):
        DISPLAY.add_or_refresh_display(original, relative)
    assert "kd-wiki-display" not in original and original.count("[!kd-page]") == 1


def test_real_edit_before_migration_is_not_blessed_by_hash_transition(tmp_path):
    root, page, _, state_path = _old_vault(tmp_path, body_changed_after_hash=True)
    journal = tmp_path / "private-journal"
    old_state = json.loads(state_path.read_text(encoding="utf-8"))
    relative = page.relative_to(root).as_posix()
    plan = json.loads(_display_cli(root, journal, "plan").stdout)
    assert _display_cli(root, journal, "apply", plan["plan_id"]).returncode == 0
    migrated_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert migrated_state["hashes"][relative] == old_state["hashes"][relative]
    assert relative not in migrated_state.get("hash_versions", {})

    result = _locked(root, sys.executable, str(root / "tools/kb.py"), "--root", str(root))
    # The synthetic page can produce unrelated lint findings, but writes still complete.
    assert result.returncode in {0, 1}, result.stderr
    assert f"更新: {dt.date.today().isoformat()}" in page.read_text(encoding="utf-8")
    final_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert final_state["hash_versions"][relative] == 2


def test_kb_adds_display_without_changing_business_update_then_detects_body_edit(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    _install(root)
    init = _locked(root, sys.executable, str(root / "tools/kb.py"), "init", "--root", str(root))
    assert init.returncode == 0, init.stderr
    page = root / "wiki/概念/合成概念.md"
    original = """---
编号: CPT-0001
类型: 概念
创建: 2000-01-01
更新: 2000-01-01
主题: [AI]
子类: 术语
---

# 合成概念

## 定义
初始业务正文。
## 各家观点
## 我的相关认知（自动）
## 相关概念
"""
    page.write_text(original, encoding="utf-8")
    state_path = root / ".graph/state.json"
    state_path.write_text(json.dumps({"hashes": {page.relative_to(root).as_posix():
                                                  DISPLAY.legacy_content_hash(original)}},
                                     ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    first = _locked(root, sys.executable, str(root / "tools/kb.py"), "--root", str(root))
    assert first.returncode == 0, first.stdout + first.stderr
    displayed = page.read_text(encoding="utf-8")
    assert "更新: 2000-01-01" in displayed and "kd-wiki-concept" in displayed
    second = _locked(root, sys.executable, str(root / "tools/kb.py"), "--root", str(root))
    assert second.returncode == 0 and "更新: 2000-01-01" in page.read_text(encoding="utf-8")
    assert "kd-wiki-index" in (root / "wiki/index.md").read_text(encoding="utf-8")
    assert "kd-wiki-pending" in (root / "wiki/待确认.md").read_text(encoding="utf-8")
    page.write_text(page.read_text(encoding="utf-8").replace("初始业务正文。", "用户修改后的业务正文。"),
                    encoding="utf-8")
    third = _locked(root, sys.executable, str(root / "tools/kb.py"), "--root", str(root))
    assert third.returncode == 0
    assert f"更新: {dt.date.today().isoformat()}" in page.read_text(encoding="utf-8")


def test_plan_rejects_overlapping_or_symlink_journal_without_touching_vault(tmp_path):
    root, page, raw, state_path = _old_vault(tmp_path)
    before = {path: path.read_bytes() for path in [page, raw, state_path]}
    overlap = _display_cli(root, root / ".private", "plan")
    assert overlap.returncode != 0 and json.loads(overlap.stderr)["error_code"] == "journal_overlaps_vault"
    real = tmp_path / "real-journal"
    real.mkdir()
    link = tmp_path / "journal-link"
    link.symlink_to(real, target_is_directory=True)
    linked = _display_cli(root, link, "plan")
    assert linked.returncode != 0 and json.loads(linked.stderr)["error_code"] == "journal_path_invalid"
    assert {path: path.read_bytes() for path in before} == before


def test_apply_rejects_parent_symlink_created_after_plan(tmp_path):
    root, page, raw, state_path = _old_vault(tmp_path)
    journal = tmp_path / "private-journal"
    plan = json.loads(_display_cli(root, journal, "plan").stdout)
    outside = tmp_path / "outside-source"
    page.parent.rename(outside)
    page.parent.symlink_to(outside, target_is_directory=True)
    outside_page = outside / page.name
    before = outside_page.read_bytes()
    result = _display_cli(root, journal, "apply", plan["plan_id"])
    assert result.returncode != 0 and json.loads(result.stderr)["error_code"] == "path_symlink"
    assert outside_page.read_bytes() == before
    assert raw.exists() and state_path.exists()


def test_malformed_plan_type_returns_fixed_error(tmp_path):
    root, *_ = _old_vault(tmp_path)
    journal = tmp_path / "private-journal"
    plan = json.loads(_display_cli(root, journal, "plan").stdout)
    plan_path = journal / f"{plan['plan_id']}.json"
    plan_path.write_text("[]\n", encoding="utf-8")
    result = _display_cli(root, journal, "apply", plan["plan_id"])
    assert result.returncode != 0
    assert json.loads(result.stderr)["error_code"] == "plan_invalid"


def test_scalar_cssclasses_is_conservative_and_never_reformatted(tmp_path):
    root, page, raw, state_path = _old_vault(tmp_path)
    page.write_text(page.read_text(encoding="utf-8").replace(
        "cssclasses: [user-reading, kd-wiki]", "cssclasses: user-reading"), encoding="utf-8")
    before = {path: path.read_bytes() for path in [page, raw, state_path]}
    result = _display_cli(root, tmp_path / "private-journal", "plan")
    assert result.returncode != 0
    assert json.loads(result.stderr)["error_code"] == "cssclasses_scalar_unsupported"
    assert {path: path.read_bytes() for path in before} == before
