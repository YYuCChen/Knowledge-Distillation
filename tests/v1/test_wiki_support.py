"""Synthetic-only contracts; fake judgments do not prove model semantics."""
from dataclasses import replace
import json
import os
from pathlib import Path

import pytest

from knowledge_distiller.v1 import wiki_support as ws

RAW_PATH = "raw/外部/2026/10/R-20261008-0001.md"
REF = RAW_PATH + "#^source-1"
PAGE = "wiki/来源/来源：测试.md"


class FakeClient:
    def __init__(self, outcomes=None, category="negation", field="text", response=None):
        self.outcomes = outcomes or {}
        self.category, self.field, self.response = category, field, response
        self.calls = []

    def complete(self, *, system, user, max_tokens):
        self.calls.append((system, json.loads(user), max_tokens))
        if self.response is not None:
            return self.response
        checks = []
        for index, claim in enumerate(json.loads(user)["claims"]):
            status = self.outcomes.get(index, "supported")
            checks.append({"claim_id": claim["claim_id"], "status": status, "reason": "合成判断",
                           "issues": [] if status == "supported" else [
                               {"field": self.field, "category": self.category, "reason": "合成定位"}]})
        return json.dumps({"checks": checks}, ensure_ascii=False)


class Harness:
    def __init__(self, tmp_path):
        self.stage = tmp_path / "staging"
        self.stage.mkdir()
        self.checkpoint = tmp_path / "checkpoint"
        self.raw = self.add_raw(RAW_PATH, "第三方", "作者甲",
            "作者甲认为：只有在低温条件下，数值不是 12，而是 10；这不是普遍结论。\n\n^source-1\n\n"
            "另一操作需要等待 20 秒。\n\n^source-2\n")

    def write(self, path, content):
        target = self.stage / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    def add_raw(self, path, identity, author, body):
        rid = Path(path).stem
        content = (f"---\n编号: {rid}\n身份: {identity}\n标题: 测试\n作者: {author}\n"
                   "产生于: 2026-10-08T12:00:00+08:00\n---\n\n" + body).encode()
        self.write(path, content)
        return ws.FrozenRaw(path, rid, content, ws.sha256(content))

    def make(self, text, *, before=None, raws=None, pages=(), parent=None, generated=(), path=PAGE):
        content = text.encode()
        before_bytes = before.encode() if before is not None else None
        change = ws.DocumentChange(path, before_bytes, ws.sha256(before_bytes) if before_bytes is not None else None,
                                   content, ws.sha256(content))
        self.write(path, content)
        ordinary = ws.build_registry(self.stage, (change,), raws or (self.raw,), pages=pages, generated=generated)
        if parent is None:
            return ordinary
        mapping = tuple(ws.ClaimMapping(old.block.claim_id, new.block.path, new.block.position)
                        for old, new in zip(parent.claims, ordinary.claims))
        return ws.build_registry(self.stage, (change,), raws or (self.raw,), pages=pages,
                                 generated=generated, parent_registry=parent, claim_mapping=mapping)

    def gate(self, registry, **kwargs):
        return ws.WikiSupportGate(self.checkpoint, "task_batch_1", registry, "1" * 64, **kwargs)


@pytest.fixture
def h(tmp_path):
    return Harness(tmp_path)


def cited(text="作者甲认为数值不是 12，而是 10。", anchor=REF):
    return text + "（" + anchor + "）"


def test_exact_managed_system_no_claims_is_durable_without_fake_support(h):
    path='wiki/index.md'; text='# 知识库索引\n\n<!-- 自动生成，勿手改 -->\n'
    certificate=ws.GeneratedSection(path,ws.sha256(text.encode()),'@system',text.encode())
    registry=h.make(text,path=path,generated=(certificate,))
    client=FakeClient(); result=h.gate(registry).review(client)
    assert result.status=='no_changed_claims' and not client.calls and result.receipt_path.is_file()
    assert h.gate(registry).review(client).status=='no_changed_claims' and not client.calls
    changed=h.make(text+'\n用户说所有条件都适用。\n',path=path,generated=(certificate,))
    assert changed.claims and any(c.diagnostics for c in changed.claims)


def test_exact_managed_section_preserves_extra_business_prose(h):
    path='wiki/主题/测试.md'
    region='## 核心认知（自动）\n<!-- 自动生成，勿手改 -->\n（暂无）\n\n'
    text=region+'## 用户观点\n所有温度都为12。\n'
    certificate=ws.GeneratedSection(path,ws.sha256(text.encode()),'核心认知（自动）',region.encode())
    registry=h.make(text,path=path,generated=(certificate,))
    assert len(registry.claims)==2 and all(c.block.section=='用户观点' for c in registry.claims)


def test_full_block_registry_does_not_drop_uncited_assertions(h):
    registry = h.make("## 摘要\n无出处摘要。\n\n## 核心论点\n- " + cited() +
                      "\n- 未引列表主张。\n\n> 未引引文。\n\n| 条件 | 值 |\n|---|---|\n| 低温 | 99 |\n\n"
                      "```python\nassert result == 99\n```\n\n<!-- 未引隐藏主张 -->\n")
    assert len(registry.claims) == 7
    assert {c.block.kind for c in registry.claims} >= {"table_open", "fence", "html_block"}
    assert sum(bool(c.diagnostics) for c in registry.claims) == 6
    client = FakeClient()
    result = h.gate(registry).review(client)
    assert result.status == "source_boundary_failed" and not client.calls
    assert any(d.category == "missing_citation" and "摘要" in d.position for d in result.diagnostics)


def test_code_containing_fake_heading_or_anchor_is_one_complete_claim(h):
    registry = h.make("```text\n## 不是真小节\n" + REF + "\n^source-8\n```\n")
    assert len(registry.claims) == 1
    assert registry.claims[0].block.kind == "fence"
    assert "## 不是真小节" in registry.claims[0].block.text


def test_table_is_full_including_header_and_non_cited_rows(h):
    registry = h.make("| 条件 | 声称 |\n|---|---|\n| 低温 | " + cited("99") + " |\n| 高温 | 无限 |\n")
    assert len(registry.claims) == 1
    assert "高温 | 无限" in registry.claims[0].block.text
    client = FakeClient({0: "unsupported"}, "condition")
    assert h.gate(registry).review(client).diagnostics[0].category == "condition"


def test_changed_blocks_only_and_stable_id_across_newlines(h):
    before = "## 摘要\n" + cited("旧摘要。") + "\n\n## 核心论点\n" + cited() + "\n"
    after = before.replace("旧摘要。", "新摘要。")
    registry = h.make(after, before=before)
    assert len(registry.claims) == 1
    second = h.make(after.replace("新摘要。", "新摘要。\n仍同一段。"), before=before)
    assert registry.claims[0].block.claim_id == second.claims[0].block.claim_id
    assert registry.candidate_hash != second.candidate_hash


def test_duplicate_claim_occurrences_have_distinct_deterministic_ids(h):
    text = "- " + cited() + "\n- " + cited() + "\n"
    first = h.make(text)
    assert len({c.block.claim_id for c in first.claims}) == 2
    assert h.make(text) == first


def test_verified_display_only_is_ignored_and_original_lines_retained(h):
    body = "## 摘要\n" + cited() + "\n"
    display, _ = ws._display().add_or_refresh_display(body, PAGE)
    registry = h.make(display)
    assert len(registry.claims) == 1
    assert registry.claims[0].block.start_line > 2
    with pytest.raises(ws.WikiSupportError, match="display_invalid"):
        h.make(display.replace("> [!kd-page]", "> 非产品内容"))


def test_auto_comment_alone_cannot_hide_content(h):
    registry = h.make("## 我的相关认知（自动）\n<!-- 自动生成，勿手改 -->\n隐藏主张。\n",
                      path="wiki/概念/测试.md")
    assert registry.claims
    assert all(c.diagnostics for c in registry.claims)


def test_trusted_exact_generated_section_can_be_excluded(h):
    path = "wiki/概念/测试.md"
    section = "## 我的相关认知（自动）\n<!-- 自动生成，勿手改 -->\n- 程序生成条目\n"
    text = "## 定义\n" + cited() + "\n\n" + section
    proof = ws.GeneratedSection(path, ws.sha256(text.encode()), "我的相关认知（自动）", section.encode())
    registry = h.make(text, path=path, generated=(proof,))
    assert len(registry.claims) == 1
    invalid = replace(proof, content=b"wrong")
    assert len(h.make(text, path=path, generated=(invalid,)).claims) > 1


@pytest.mark.parametrize("category", sorted(ws.CATEGORIES))
def test_semantic_diagnostic_payload_and_private_failure(h, category):
    registry = h.make(cited() + "\n")
    client = FakeClient({0: "unsupported"}, category)
    result = h.gate(registry).review(client)
    assert result.status == "source_support_failed"
    diagnostic = result.diagnostics[0]
    assert diagnostic.claim_id == registry.claims[0].block.claim_id
    assert diagnostic.category == category and diagnostic.path == PAGE
    payload = client.calls[0][1]["claims"][0]
    assert payload["text"] == registry.claims[0].block.text
    evidence = payload["evidence"][0]
    assert "20 秒" in evidence["full_raw"] and "20 秒" not in evidence["excerpt"]
    assert evidence["identity"] == "第三方" and evidence["stable_id"] == h.raw.stable_id
    assert "否定" in client.calls[0][0] and "cross_point" in client.calls[0][0]


@pytest.mark.parametrize("identity,folder", [("本人", "自述"), ("本人附言", "自述"), ("第三方", "外部")])
def test_preserves_envelope_identity_and_ai_speculation_marker(h, identity, folder):
    path = f"raw/{folder}/2026/10/R-20261008-0002.md"
    raw = h.add_raw(path, identity, "本人" if identity != "第三方" else "作者乙", "原话。\n\n^source-1\n")
    registry = h.make("[推测] " + cited("用户是否认可尚未知。", path + "#^source-1"), raws=(raw,))
    evidence = registry.claims[0].evidence[0]
    assert evidence["identity"] == identity
    assert registry.claims[0].block.text.startswith("[推测]")


@pytest.mark.parametrize("ref,category", [(RAW_PATH, "raw_anchor_required"),
    (RAW_PATH + "#^source-88", "raw_anchor_missing"),
    ("raw/外部/未知.md#^source-1", "raw_not_frozen"),
    ("raw/../private.md#^source-1", "citation_path_unsafe"),
    (RAW_PATH + "#^image-1", "unsupported_kind")])
def test_reference_boundaries(h, ref, category):
    registry = h.make(cited(anchor=ref))
    assert category in {d.category for d in registry.claims[0].diagnostics}


def test_recursion_carries_dependency_claim_and_raw(h):
    path = "wiki/来源/来源：依据.md"
    content = ("## 核心论点\n" + cited()).encode()
    h.write(path, content)
    page = ws.FrozenPage(path, content, ws.sha256(content))
    registry = h.make("作者甲认为数值是 10。[[来源：依据#核心论点]]", pages=(page,))
    assert not registry.claims[0].diagnostics
    item = registry.claims[0].evidence[0]
    assert item["dependencies"][0]["path"] == path
    assert item["raw_path"] == RAW_PATH


def test_unchanged_multihop_claim_is_reviewed_and_can_fail_alone(h):
    a, b, c = "wiki/来源/A.md", "wiki/来源/B.md", "wiki/来源/C.md"
    old_a, new_a = cited("只有低温才成立。"), cited("仅低温可成立。")
    b_text = "只有低温才成立。[[A]]"
    c_text = "任何温度都成立。[[B]]"
    h.write(b, b_text.encode())
    page = ws.FrozenPage(b, b_text.encode(), ws.sha256(b_text.encode()))
    changes = []
    for path, before, after in ((a, old_a, new_a), (c, c_text, c_text)):
        h.write(path, after.encode())
        changes.append(ws.DocumentChange(path, before.encode(), ws.sha256(before.encode()),
                                        after.encode(), ws.sha256(after.encode())))
    registry = ws.build_registry(h.stage, tuple(changes), (h.raw,), pages=(page,))
    assert [claim.block.path for claim in registry.claims] == [a, c]
    assert [d["path"] for d in registry.claims[1].evidence[0]["dependencies"]] == [b, a]
    client = FakeClient({1: "unsupported"}, category="condition")
    result = h.gate(registry).review(client)
    assert result.status == "source_support_failed"
    assert result.checks[0]["status"] == "supported"
    assert {d.claim_id for d in result.diagnostics} == {registry.claims[1].block.claim_id}
    assert client.calls[0][1]["claims"][1]["text"] == c_text


@pytest.mark.parametrize("mode,category", [("cycle", "dependency_cycle"),
                                           ("unresolved", "wiki_reference_ambiguous")])
def test_unchanged_dependency_traversal_failure_cannot_skip_review(h, mode, category):
    path = "wiki/来源/B.md"
    text = "依赖[[来源：测试]]" if mode == "cycle" else "依赖[[未冻结]]"
    h.write(path, text.encode())
    page = ws.FrozenPage(path, text.encode(), ws.sha256(text.encode()))
    claim = "字面未改[[B]]"
    registry = h.make(claim, before=claim, pages=(page,))
    assert len(registry.claims) == 1
    assert category in {d.category for d in registry.claims[0].diagnostics}
    client = FakeClient()
    assert h.gate(registry).review(client).status == "source_boundary_failed"
    assert not client.calls


@pytest.mark.parametrize("mode,category", [("cycle", "dependency_cycle"), ("synthesis", "synthesis_as_evidence"),
    ("ambiguous", "wiki_reference_ambiguous"), ("no_anchor", "wiki_claim_ambiguous"),
    ("uncited", "dependency_missing_citation")])
def test_recursive_rejections(h, mode, category):
    path = "wiki/综合/依据.md" if mode == "synthesis" else "wiki/来源/依据.md"
    text = "反复引用[[来源：测试]]" if mode == "cycle" else cited()
    if mode == "no_anchor":
        text += "\n\n" + cited("第二条。")
    if mode == "uncited":
        text = "无出处依据。"
    content = text.encode()
    h.write(path, content)
    pages = [ws.FrozenPage(path, content, ws.sha256(content))]
    if mode == "ambiguous":
        other = "wiki/概念/依据.md"
        h.write(other, content)
        pages.append(ws.FrozenPage(other, content, ws.sha256(content)))
    registry = h.make("论断[[依据]]", pages=tuple(pages))
    assert category in {d.category for d in registry.claims[0].diagnostics}


def test_frontmatter_provenance_program_checks_and_judgment_registered(h):
    fm = f"---\n原始文件: {RAW_PATH}\n标题: 测试\n作者: 作者甲\n发布日期: 2026-10-08\n"
    registry = h.make(fm + "---\n" + cited())
    assert not any(c.diagnostics for c in registry.claims)
    assert sum(c.block.kind == "provenance_metadata" for c in registry.claims) == 4
    wrong = h.make(fm.replace("作者: 作者甲", "作者: 用户") + "当前判断: 用户已认可\n---\n" + cited())
    assert {d.category for c in wrong.claims for d in c.diagnostics} >= {"metadata_mismatch", "missing_citation"}


def test_only_raw_envelope_verified_metadata_can_use_program_basis(h):
    text = f'---\n原始文件: {RAW_PATH}\n标题: 测试\n作者: 作者甲\n---\n'+cited()+'\n'
    registry = h.make(text)
    checks = [dict(claim_id=c.block.claim_id, status='supported', basis=(
        'program' if c.block.kind == 'provenance_metadata' else 'raw'),
        reason='Synthetic response for deterministic metadata basis.', issues=[]) for c in registry.claims]
    parsed, diagnostics = ws.parse_checks(registry, json.dumps({'checks': checks}))
    assert not diagnostics and len(parsed) == len(registry.claims)
    assert {c.block.position for c in registry.claims if c.block.kind == 'provenance_metadata'} == {
        'fm:作者', 'fm:标题', 'fm:原始文件'}
    wrong = h.make(text.replace('作者: 作者甲', '作者: 未署名'))
    _, diagnostics = ws.parse_checks(wrong, json.dumps({'checks': checks}))
    assert any(d.reason == 'program_basis_outside_management' for d in diagnostics)
    client = FakeClient()
    assert h.gate(wrong).review(client).status == 'source_boundary_failed' and not client.calls
    for item in checks:
        item['basis'] = 'program'
    _, diagnostics = ws.parse_checks(registry, json.dumps({'checks': checks}))
    assert any(d.reason == 'program_basis_outside_management' and d.claim_id ==
               next(c.block.claim_id for c in registry.claims if c.block.kind != 'provenance_metadata')
               for d in diagnostics)


def test_time_value_compared_as_same_instant_not_guessed(h):
    text = f"---\n原始文件: {RAW_PATH}\n发布时间: 2026-10-08T04:00:00+00:00\n---\n" + cited()
    assert not any(c.diagnostics for c in h.make(text).claims)
    assert any(c.diagnostics for c in h.make(text.replace("04:00", "05:00")).claims)


@pytest.mark.parametrize("text", ["![[附件/raw/R-20261008-0001/image-1.jpg]]", "![图](x.png)", "<video src='x'></video>"])
def test_native_media_is_explicitly_unsupported(h, text):
    result = h.gate(h.make(text)).review(FakeClient())
    assert result.status == "source_boundary_failed"
    assert any(d.category == "unsupported_kind" for d in result.diagnostics)


def test_empty_registry_never_supported_or_published(h):
    text = cited()
    result = h.gate(h.make(text, before=text)).review(FakeClient())
    assert result.status == "no_changed_claims"


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "unknown", "extra", "bad_status", "bad_field", "empty_reason"])
def test_strict_exact_claim_coverage(h, mutation):
    registry = h.make(cited())
    base = json.loads(FakeClient().complete(system="", user=json.dumps(registry.payload()), max_tokens=1))
    check = base["checks"][0]
    if mutation == "missing": base["checks"] = []
    elif mutation == "duplicate": base["checks"].append(dict(check))
    elif mutation == "unknown": check["claim_id"] = "unknown"
    elif mutation == "extra": check["publish"] = True
    elif mutation == "bad_status": check["status"] = "probably"
    elif mutation == "bad_field":
        check.update(status="unsupported", issues=[{"field": "execute", "category": "unsupported", "reason": "x"}])
    elif mutation == "empty_reason": check["reason"] = " "
    result = h.gate(registry).review(FakeClient(response=json.dumps(base)))
    assert result.status == "technical_failure"


@pytest.mark.parametrize("response", ["not json", '{"checks":[],"checks":[]}', '{"checks":NaN}', "[]"])
def test_invalid_json_is_technical_failure(h, response):
    assert h.gate(h.make(cited())).review(FakeClient(response=response)).status == "technical_failure"


def test_success_reuses_receipt_on_restart_and_checks_private_permissions(h):
    registry = h.make(cited())
    client = FakeClient()
    first = h.gate(registry).review(client)
    again = h.gate(registry).review(client)
    assert first == again and len(client.calls) == 1
    assert first.status == "supported_candidate_not_published"
    assert first.receipt_path.stat().st_mode & 0o077 == 0
    assert first.receipt_path.parent.stat().st_mode & 0o077 == 0


def test_reserve_before_external_repair_and_explicit_mapping(h):
    initial = h.make("## 摘要\n作者甲认为数值是 10。\n")
    gate = h.gate(initial)
    assert gate.review(FakeClient()).status == "source_boundary_failed"
    reservation = gate.reserve_repair()
    state = gate._load(gate.directory / "state.json")
    assert state["used"] == 1 and not state["repair"]["consumed"]
    assert reservation.feedback["diagnostics"][0]["category"] == "missing_citation"
    fixed = h.make("## 摘要\n" + cited("作者甲认为数值是 10。") + "\n", parent=initial)
    result = h.gate(fixed).review(FakeClient(), reservation=reservation)
    assert result.status == "supported_candidate_not_published"
    assert result.parent_hash == initial.candidate_hash and result.used_repairs == 1
    assert fixed.claims[0].block.claim_id == initial.claims[0].block.claim_id


def test_repair_preserves_successful_text_and_refuses_delete_add_reorder(h):
    text = "- " + cited("第一条错误。") + "\n- " + cited("第二条正确。") + "\n"
    initial = h.make(text)
    gate = h.gate(initial)
    gate.review(FakeClient({0: "unsupported"}))
    reservation = gate.reserve_repair()
    for changed in (text.replace("第二条正确。", "悄改成功主张。"), text.splitlines()[0] + "\n",
                    text + "- " + cited("新增第三条。") + "\n", "\n".join(reversed(text.splitlines())) + "\n"):
        with pytest.raises(ws.WikiSupportError, match="repair_invalid"):
            fixed = h.make(changed, parent=initial)
            h.gate(fixed).review(FakeClient(), reservation=reservation)


def test_repair_without_original_mapping_rejected(h):
    initial = h.make(cited("错误。"))
    gate = h.gate(initial)
    gate.review(FakeClient({0: "unsupported"}))
    reservation = gate.reserve_repair()
    fixed = h.make(cited("修复。"))
    with pytest.raises(ws.WikiSupportError, match="repair_invalid"):
        h.gate(fixed).review(FakeClient(), reservation=reservation)


@pytest.mark.parametrize('old,new,allowed', [
    ('这是素材对[[实验结果外推]]的限制。', '这是素材对实验结果外推的限制。', True),
    ('这是素材对[[实验结果外推|外推边界]]的限制。', '这是素材对外推边界的限制。', True),
    ('这是素材对[[实验结果外推|外推边界]]的限制。', '这是素材对[[另一概念|外推边界]]的限制。', True),
    ('这是素材对[[实验结果外推]]的限制。', '这是素材对的限制。', False),
    ('这是素材对[[实验结果外推|外推边界]]的限制。', '这是素材对[[实验结果外推|普遍规律]]的限制。', False),
    ('实验结果外推与[[实验结果外推]]均有限制。', '与[[实验结果外推]]均有限制。', False),
    ('这是素材对[[实验结果外推]]的限制。', '这是素材对实验结果外推的普遍肯定。', False),
])
def test_citation_only_repair_preserves_visible_wikilink_prose(h, old, new, allowed):
    pages = []
    for title in ('实验结果外推', '另一概念'):
        path = f'wiki/概念/{title}.md'
        content = (cited('作者甲只记录低温数值10。') + '\n').encode()
        h.write(path, content)
        pages.append(ws.FrozenPage(path, content, ws.sha256(content)))
    initial = h.make(cited(old), pages=tuple(pages))
    gate = h.gate(initial)
    assert gate.review(FakeClient({0: 'unsupported'}, field='citations')).status == 'source_support_failed'
    reservation = gate.reserve_repair()
    fixed = h.make(cited(new), pages=tuple(pages), parent=initial)
    if allowed:
        assert h.gate(fixed).review(FakeClient(), reservation=reservation).status == 'supported_candidate_not_published'
    else:
        with pytest.raises(ws.WikiSupportError, match='repair_invalid'):
            h.gate(fixed).review(FakeClient(), reservation=reservation)


@pytest.mark.parametrize('spelling', ['bare', 'wikilink', 'markdown'])
def test_citation_only_raw_address_replacement_preserves_labels(h, spelling):
    second = RAW_PATH + '#^source-2'
    def source(ref):
        if spelling == 'wikilink':
            return '[[' + ref + ']]'
        if spelling == 'markdown':
            return '[原文证据](' + ref + ')'
        return '（' + ref + '）'
    initial = h.make('作者甲记录操作条件。' + source(REF))
    gate = h.gate(initial)
    assert gate.review(FakeClient({0: 'unsupported'}, field='citations')).status == 'source_support_failed'
    reservation = gate.reserve_repair()
    fixed = h.make('作者甲记录操作条件。' + source(second), parent=initial)
    assert h.gate(fixed).review(FakeClient(), reservation=reservation).status == 'supported_candidate_not_published'


def test_citation_normalization_never_rescans_alias_or_changes_markdown_label():
    alias = '证据地址 ' + REF
    assert ws._citation_free('[[' + REF + '|' + alias + ']]', (REF,)) == alias
    assert ws._citation_free('[原文证据](' + REF + ')', (REF,)) == '原文证据'
    assert ws._citation_free('[改了正文](' + REF + ')', (REF,)) != '原文证据'
    assert ws._citation_free('[[实验结果外推|含|糊]]', ('实验结果外推',)) == '[[实验结果外推|含|糊]]'


def test_default_two_repairs_bound_to_baseline_not_current_claim(h):
    text = "- " + cited("甲。") + "\n- " + cited("乙。") + "\n- " + cited("丙。") + "\n"
    initial = h.make(text)
    gate = h.gate(initial)
    gate.review(FakeClient({0: "unsupported", 1: "unsupported", 2: "unsupported"}))
    r1 = gate.reserve_repair()
    first = h.make(text.replace("甲。", "甲修复。"), parent=initial)
    gate1 = h.gate(first)
    assert gate1.review(FakeClient({1: "unsupported", 2: "unsupported"}), reservation=r1).status == "source_support_failed"
    r2 = gate1.reserve_repair()
    second = h.make(text.replace("甲。", "甲修复。").replace("乙。", "乙修复。"), parent=first)
    gate2 = h.gate(second)
    assert gate2.review(FakeClient({2: "unsupported"}), reservation=r2).used_repairs == 2
    exhausted = h.gate(second).reserve_repair()
    assert exhausted.feedback["status"] == "budget_exhausted"


def test_same_candidate_same_failure_stops_without_model_call(h):
    registry = h.make(cited())
    client = FakeClient({0: "unsupported"})
    gate = h.gate(registry)
    gate.review(client)
    reservation = gate.reserve_repair()
    assert h.gate(registry).review(client, reservation=reservation).status == "no_improvement"
    assert len(client.calls) == 1


def test_changed_candidate_same_failure_positions_is_no_improvement(h):
    initial = h.make(cited("错误。"))
    gate = h.gate(initial)
    gate.review(FakeClient({0: "unsupported"}))
    reserved = gate.reserve_repair()
    changed = h.make(cited("仍错误。"), parent=initial)
    assert h.gate(changed).review(FakeClient({0: "unsupported"}), reservation=reserved).status == "no_improvement"


@pytest.mark.parametrize('new_failure', ['author', 'body'])
def test_cleared_hard_boundary_allows_first_semantic_failures_and_remaining_budget(h, new_failure):
    text = f'---\n原始文件: {RAW_PATH}\n标题: 测试\n作者: 未署名\n发布日期: 2026-10-08\n---\n'+cited()+'\n'
    initial = h.make(text)
    client = FakeClient()
    gate = h.gate(initial)
    original = gate.review(client)
    assert original.status == 'source_boundary_failed' and not original.checks and not client.calls
    first_reservation = gate.reserve_repair()
    fixed = h.make(text.replace('作者: 未署名', '作者: 作者甲'), parent=initial)
    author = next(c for c in fixed.claims if c.block.position == 'fm:作者')
    assert author.evidence[0]['kind'] == 'program_verified_metadata'
    failed_id = (author.block.claim_id if new_failure == 'author' else
                 next(c.block.claim_id for c in fixed.claims if c.block.kind != 'provenance_metadata'))
    index = next(i for i, c in enumerate(fixed.claims) if c.block.claim_id == failed_id)
    semantic_client = FakeClient({index: 'unsupported'})
    repaired_gate = h.gate(fixed)
    result = repaired_gate.review(semantic_client, reservation=first_reservation)
    assert result.status == 'source_support_failed' and result.checks and result.used_repairs == 1
    assert len(semantic_client.calls) == 1
    # Re-parsing the persisted receipt after restart preserves the stage boundary.
    assert h.gate(fixed).review(semantic_client).status == 'source_support_failed'
    assert len(semantic_client.calls) == 1
    second = h.gate(fixed).reserve_repair()
    assert second.number == 2
    assert second.feedback['allowed_fields'] == [{'claim_id': failed_id, 'field': 'text'}]
    changed = text.replace('作者: 未署名', '作者: 作者甲')
    if new_failure == 'body':
        changed = changed.replace('作者甲认为数值不是 12，而是 10。', '作者甲认为只有低温条件下数值是 10。')
        last = h.make(changed, parent=fixed)
        final = h.gate(last).review(FakeClient({index: 'unsupported'}), reservation=second)
        assert final.status == 'no_improvement' and final.used_repairs == 2


def test_remaining_hard_boundary_does_not_count_as_first_semantic_progress(h):
    text = f'---\n原始文件: {RAW_PATH}\n作者: 未署名\n---\n'+cited()+'\n'
    initial = h.make(text)
    client = FakeClient()
    gate = h.gate(initial)
    assert gate.review(client).status == 'source_boundary_failed'
    reservation = gate.reserve_repair()
    still_wrong = h.make(text.replace('作者: 未署名', '作者: 另一个错误'), parent=initial)
    result = h.gate(still_wrong).review(client, reservation=reservation)
    assert result.status == 'no_improvement' and not result.checks and not client.calls


def test_config_budget_and_raw_change_do_not_reset_existing_gate(h):
    registry = h.make(cited())
    h.gate(registry).review(FakeClient({0: "unsupported"}))
    for kwargs in ({"max_repairs": 3}, {"max_tokens": 8000}):
        with pytest.raises(ws.WikiSupportError, match="binding_mismatch"):
            h.gate(registry, **kwargs).review(FakeClient())
    with pytest.raises(ws.WikiSupportError, match="binding_mismatch"):
        ws.WikiSupportGate(h.checkpoint, "task_batch_1", registry, "2" * 64).review(FakeClient())
    other = h.add_raw(RAW_PATH, "第三方", "作者甲", "全新原文。\n\n^source-1\n")
    changed = h.make(cited(), raws=(other,))
    with pytest.raises(ws.WikiSupportError, match="binding_mismatch"):
        h.gate(changed).review(FakeClient())


def test_interrupted_call_reserved_before_call_never_automatically_resends(h):
    class Crash(BaseException): pass
    class CrashClient:
        def complete(self, **kwargs): raise Crash()
    registry = h.make(cited())
    with pytest.raises(Crash): h.gate(registry).review(CrashClient())
    fresh = FakeClient()
    assert h.gate(registry).review(fresh).status == "interrupted"
    assert not fresh.calls


def test_received_response_survives_crash_before_decision(h, monkeypatch):
    registry = h.make(cited())
    gate = h.gate(registry)
    original = gate._write
    class Crash(BaseException): pass
    def write(path, value):
        if path.name.startswith("decision-"): raise Crash()
        original(path, value)
    monkeypatch.setattr(gate, "_write", write)
    client = FakeClient()
    with pytest.raises(Crash): gate.review(client)
    assert h.gate(registry).review(client).status == "supported_candidate_not_published"
    assert len(client.calls) == 1


def test_client_exception_and_protocol_error_do_not_leak_source_or_secret(h):
    class Broken:
        def complete(self, **kwargs): raise RuntimeError("SECRET credential and private body")
    registry = h.make(cited())
    result = h.gate(registry).review(Broken())
    assert result.status == "technical_failure"
    assert "SECRET" not in result.receipt_path.read_text()
    with pytest.raises(ws.WikiSupportError) as error:
        ws.parse_checks(registry, "SECRET credential and private body")
    assert "SECRET" not in str(error.value)


def test_symlink_regular_file_utf8_hash_and_duplicate_raw_id(h, tmp_path):
    registry = h.make(cited())
    source = h.stage / RAW_PATH
    saved = source.read_bytes()
    source.unlink()
    outside = tmp_path / "outside.md"
    outside.write_bytes(saved)
    source.symlink_to(outside)
    with pytest.raises(ws.WikiSupportError, match="path_unsafe"): registry.verify()
    source.unlink()
    source.write_bytes(saved)
    with pytest.raises(ws.WikiSupportError, match="hash_mismatch"):
        ws.build_registry(h.stage, registry.changes, (replace(h.raw, sha256="0" * 64),))
    with pytest.raises(ws.WikiSupportError, match="utf8_invalid"):
        ws.build_registry(h.stage, (replace(registry.changes[0], after=b"\xff", after_sha256=ws.sha256(b"\xff")),), (h.raw,))
    with pytest.raises(ws.WikiSupportError, match="raw_invalid"):
        ws.build_registry(h.stage, registry.changes, (h.raw, h.raw))


def test_checkpoint_symlink_corruption_and_concurrent_lock_refuse(h, tmp_path):
    registry = h.make(cited())
    gate = h.gate(registry)
    with gate._lock():
        with pytest.raises(ws.WikiSupportError, match="checkpoint_busy"): gate.review(FakeClient())
    gate.review(FakeClient())
    state = gate.directory / "state.json"
    state.write_text("bad JSON")
    with pytest.raises(ws.WikiSupportError, match="checkpoint_corrupt"): gate.review(FakeClient())
    state.unlink()
    outside = tmp_path / "state.json"
    outside.write_text("do not overwrite")
    os.chmod(outside, 0o600)
    state.symlink_to(outside)
    with pytest.raises(ws.WikiSupportError): gate.review(FakeClient())
    assert outside.read_text() == "do not overwrite"


@pytest.mark.parametrize("target", ["root", "namespace"])
def test_checkpoint_root_created_private_and_existing_public_root_refused(h, target):
    gate = h.gate(h.make(cited()))
    with gate._lock():
        assert h.checkpoint.stat().st_mode & 0o777 == 0o700
        assert gate.directory.stat().st_mode & 0o777 == 0o700
        assert h.checkpoint.stat().st_uid == os.getuid()
    public = h.checkpoint if target == "root" else gate.directory
    public.chmod(0o755)
    client = FakeClient()
    with pytest.raises(ws.WikiSupportError, match="path_unsafe"):
        gate.review(client)
    assert not client.calls
    assert public.stat().st_mode & 0o777 == 0o755


def test_checkpoint_symlink_ancestor_refused(h, tmp_path):
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    registry = h.make(cited())
    with pytest.raises(ws.WikiSupportError, match="path_unsafe"):
        ws.WikiSupportGate(alias / "checkpoint", "task", registry, "1" * 64)
    assert not (real / "checkpoint").exists()


@pytest.mark.parametrize("target", ["root", "namespace"])
def test_checkpoint_directories_require_current_owner(h, monkeypatch, target):
    from types import SimpleNamespace
    gate = h.gate(h.make(cited()))
    with gate._lock():
        pass
    path = gate.root if target == "root" else gate.directory
    original = Path.lstat
    def lstat(candidate, *args, **kwargs):
        info = original(candidate, *args, **kwargs)
        if candidate != path:
            return info
        return SimpleNamespace(st_mode=info.st_mode, st_uid=os.getuid() + 1,
                               st_nlink=info.st_nlink)
    monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(ws.WikiSupportError, match="path_unsafe"):
        with gate._lock():
            pass


def test_checkpoint_flock_errors_are_not_hidden_and_fd_is_closed(h, monkeypatch):
    import errno
    import fcntl
    gate = h.gate(h.make(cited()))
    held = []
    original = fcntl.flock
    def fail(fd, operation):
        held.append(fd)
        raise OSError(errno.EIO, "synthetic storage error")
    monkeypatch.setattr(fcntl, "flock", fail)
    with pytest.raises(ws.WikiSupportError, match="storage_failure"):
        with gate._lock():
            pass
    with pytest.raises(OSError):
        os.fstat(held[0])
    monkeypatch.setattr(fcntl, "flock", original)
    with gate._lock():
        pass


def test_checkpoint_lock_blocks_another_process_and_releases(h):
    import subprocess
    import sys
    gate = h.gate(h.make(cited()))
    script = """import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDWR | os.O_NOFOLLOW)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    sys.exit(23)
finally:
    os.close(fd)
"""
    def other_process():
        return subprocess.run([sys.executable, "-c", script, str(gate.directory / "lock")],
                              capture_output=True, timeout=10)
    with gate._lock():
        result = other_process()
        assert result.returncode == 23, result.stderr.decode()
    result = other_process()
    assert result.returncode == 0, result.stderr.decode()


@pytest.mark.parametrize("name", ["lock", "state.json"])
@pytest.mark.parametrize("mode", ["hardlink", "public", "symlink", "directory"])
def test_checkpoint_lock_and_records_reject_unsafe_files(h, tmp_path, name, mode):
    gate = h.gate(h.make(cited()))
    gate.review(FakeClient())
    target = gate.directory / name
    saved = target.read_bytes()
    if mode == "hardlink":
        os.link(target, tmp_path / "alias")
    elif mode == "public":
        target.chmod(0o644)
    else:
        target.unlink()
        if mode == "symlink":
            outside = tmp_path / "outside"
            outside.write_bytes(saved)
            outside.chmod(0o600)
            target.symlink_to(outside)
        else:
            target.mkdir(mode=0o700)
    client = FakeClient()
    with pytest.raises(ws.WikiSupportError):
        gate.review(client)
    assert not client.calls
    if mode in {"hardlink", "public"}:
        assert target.read_bytes() == saved


@pytest.mark.parametrize("field", ["st_uid", "st_nlink", "st_mode"])
def test_private_read_revalidates_open_fd_not_only_lstat(h, monkeypatch, field):
    gate = h.gate(h.make(cited()))
    gate.review(FakeClient())
    state = gate.directory / "state.json"
    original = ws.os.fstat
    from types import SimpleNamespace
    def fstat(fd):
        info = original(fd)
        values = {name: getattr(info, name) for name in (
            "st_mode", "st_uid", "st_nlink", "st_dev", "st_ino", "st_size", "st_mtime_ns")}
        values[field] = {"st_uid": os.getuid() + 1, "st_nlink": 2,
                         "st_mode": (info.st_mode & ~0o777) | 0o644}[field]
        return SimpleNamespace(**values)
    monkeypatch.setattr(ws.os, "fstat", fstat)
    with pytest.raises(ws.WikiSupportError, match="path_unsafe"):
        ws._read(state, private=True)


def test_checkpoint_flock_uses_validated_fd_without_path_reopen(h, monkeypatch):
    import fcntl
    gate = h.gate(h.make(cited()))
    opened, locked = [], []
    original_open, original_flock = ws.os.open, fcntl.flock
    def open_fd(path, flags, mode=0o777, **kwargs):
        fd = original_open(path, flags, mode, **kwargs)
        if Path(path) == gate.directory / "lock":
            opened.append(fd)
        return fd
    def flock(fd, operation):
        locked.append(fd)
        assert fd == opened[-1]
        assert ws.os.fstat(fd).st_nlink == 1
        return original_flock(fd, operation)
    monkeypatch.setattr(ws.os, "open", open_fd)
    monkeypatch.setattr(fcntl, "flock", flock)
    with gate._lock():
        assert len(opened) == len(locked) == 1
        with pytest.raises(ws.WikiSupportError, match="checkpoint_busy"):
            with gate._lock():
                pass
    with gate._lock():
        pass  # release at context exit permits another acquisition
    assert len(opened) == len(locked) == 3


def test_duplicate_raw_anchor_and_supersession_refuse(h):
    content = h.raw.content + b"\nother\n\n^source-1\n"
    h.write(RAW_PATH, content)
    raw = replace(h.raw, content=content, sha256=ws.sha256(content))
    with pytest.raises(ws.WikiSupportError, match="raw_invalid"): h.make(cited(), raws=(raw,))


def test_superseded_raw_cannot_support_claim(h):
    path = "raw/外部/2026/10/R-20261008-0002.md"
    replacement = h.add_raw(path, "第三方", "作者甲", "新原文。\n\n^source-1\n")
    content = replacement.content.replace(b"---\n\n", ("取代: " + h.raw.stable_id + "\n---\n\n").encode())
    replacement = replace(replacement, content=content, sha256=ws.sha256(content))
    h.write(path, content)
    registry = h.make(cited(), raws=(h.raw, replacement))
    assert any(d.category == "raw_superseded" for d in registry.claims[0].diagnostics)


def test_dependency_depth_and_block_anchor(h):
    path = "wiki/来源/依据.md"
    content = (cited() + "\n\n^p1\n").encode()
    h.write(path, content)
    page = ws.FrozenPage(path, content, ws.sha256(content))
    registry = h.make("论断[[依据#^p1]]", pages=(page,))
    assert not registry.claims[0].diagnostics
    shallow = ws.build_registry(h.stage, registry.changes, registry.raws, pages=(page,), max_depth=1)
    assert any(d.category == "dependency_depth" for d in shallow.claims[0].diagnostics)


def test_failed_claims_cannot_swap_even_when_both_text_fields_allowed(h):
    text = "- " + cited("甲错误。") + "\n- " + cited("乙错误。") + "\n"
    initial = h.make(text)
    gate = h.gate(initial)
    gate.review(FakeClient({0: "unsupported", 1: "unsupported"}))
    reservation = gate.reserve_repair()
    swapped = h.make("\n".join(reversed(text.splitlines())) + "\n", parent=initial)
    with pytest.raises(ws.WikiSupportError, match="repair_invalid"):
        h.gate(swapped).review(FakeClient(), reservation=reservation)


def test_fixing_one_claim_cannot_mutate_unreviewed_existing_business_block(h):
    before = "## 摘要\n" + cited("已有摘要。") + "\n\n## 核心论点\n" + cited("旧观点。") + "\n"
    after = before.replace("旧观点。", "错误观点。")
    initial = h.make(after, before=before)
    gate = h.gate(initial)
    gate.review(FakeClient({0: "unsupported"}))
    reservation = gate.reserve_repair()
    with pytest.raises(ws.WikiSupportError, match="repair_invalid"):
        updated = h.make(after.replace("错误观点。", "修正观点。").replace("已有摘要。", "偷偷修改。"),
                         before=before, parent=initial)
        h.gate(updated).review(FakeClient(), reservation=reservation)


def test_changed_staging_raw_and_nonregular_files_refuse(h):
    registry = h.make(cited())
    source = h.stage / RAW_PATH
    source.write_bytes(h.raw.content + b"unexpected")
    with pytest.raises(ws.WikiSupportError, match="hash_mismatch"): registry.verify()
    source.unlink()
    source.mkdir()
    with pytest.raises(ws.WikiSupportError, match="path_unsafe"): registry.verify()


def test_unconsumed_external_repair_reservation_is_not_reissued(h):
    initial = h.make(cited())
    gate = h.gate(initial)
    gate.review(FakeClient({0: "unsupported"}))
    reservation = gate.reserve_repair()
    assert reservation.number == 1
    with pytest.raises(ws.WikiSupportError, match="repair_invalid"):
        h.gate(initial).reserve_repair()


def test_reserved_candidate_and_receipt_hash_corruption_cannot_fake_success(h):
    registry = h.make(cited())
    gate = h.gate(registry)
    result = gate.review(FakeClient())
    response = json.loads(result.receipt_path.read_text())
    response["payload"]["raw"] = '{"checks":[]}'
    result.receipt_path.write_text(json.dumps(response))
    with pytest.raises(ws.WikiSupportError, match="checkpoint_corrupt"):
        h.gate(registry).review(FakeClient())


def test_uncertain_is_failure_not_publish_conclusion(h):
    result = h.gate(h.make(cited())).review(FakeClient({0: "uncertain"}, "missing_context"))
    assert result.status == "source_support_failed"
    assert result.checks[0]["status"] == "uncertain"


@pytest.mark.parametrize("field", ["x: !!binary aGVsbG8=", "x: .inf", "x: &x [*x]", "x: 1\nx: 2"])
def test_untrusted_yaml_objects_and_duplicate_keys_fixed_error(h, field):
    with pytest.raises(ws.WikiSupportError, match="input_invalid") as error:
        h.make("---\n" + field + "\n---\n" + cited())
    assert str(error.value) == "wiki_support:input_invalid"
