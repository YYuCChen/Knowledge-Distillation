"""Synthetic, pure parser contracts; no AI, files, DB, Vault, or network."""
import copy
import hashlib
import json

import pytest

from knowledge_distiller.v1 import knowledge_candidate as module
from knowledge_distiller.v1.domain import validate_knowledge
from knowledge_distiller.v1.knowledge_candidate import CandidateError, parse_knowledge_candidate
from knowledge_distiller.v1.knowledge_model import source_segments


def point(start="s1", end=None, statement="先检查再执行", argument="检查可以发现前置缺口"):
    return {"statement": statement, "argument": argument,
            "source_ranges": [{"start_segment": start, "end_segment": end or start}]}


def payload():
    return {"qualified": True, "rejection_reason": None, "title": "执行前检查",
            "subtitle": "前置条件的核对", "summary": "先确认条件，再执行操作。",
            "core_points": [point()], "other_points": []}


def parse(value=None, snapshot="先检查。再执行。"):
    return parse_knowledge_candidate(snapshot, json.dumps(payload() if value is None else value, ensure_ascii=False))


def test_shared_evidence_has_no_orphan_table_and_core_then_other_ids():
    value = payload()
    value["core_points"].append(point("s2"))
    value["other_points"] = [point("s1")]
    result = parse(value).knowledge
    assert [p.point_id for p in result.core_points + result.other_points] == ["p1", "p2", "p3"]
    assert [p.evidence_ids for p in result.core_points + result.other_points] == [("e1",), ("e2",), ("e1",)]
    assert [e.evidence_id for e in result.evidence] == ["e1", "e2"]
    assert {e.evidence_id for e in result.evidence} == {ref for p in result.core_points + result.other_points for ref in p.evidence_ids}


def test_repeated_sentences_keep_distinct_occurrences_and_original_whitespace():
    snapshot = "  同一句。\n\t同一句。  "
    before = snapshot
    value = payload()
    value["core_points"] = [point("s2"), point("s1")]
    result = parse(value, snapshot).knowledge
    assert [(e.start, e.end) for e in result.evidence] == [source_segments(snapshot)["s2"], source_segments(snapshot)["s1"]]
    assert result.evidence[0].text == "\t同一句。  "
    assert result.evidence[1].text == "  同一句。\n"
    assert result.core_points[0].evidence_ids != result.core_points[1].evidence_ids
    assert snapshot == before
    for evidence in result.evidence:
        assert snapshot[evidence.start:evidence.end] == evidence.text


def test_multisegment_range_preserves_every_character_and_point_text():
    snapshot = " \t甲。\n\n  乙！\n"
    value = payload()
    value["core_points"] = [point("s1", "s2", "  保留观点  ", "\t论证\n细节 ")]
    result = parse(value, snapshot).knowledge
    assert result.evidence[0].text == snapshot
    assert result.core_points[0].statement == "  保留观点  "
    assert result.core_points[0].argument == "\t论证\n细节 "
    validate_knowledge(snapshot, result)


@pytest.mark.parametrize("selection,category,path", [
    ({"start_segment": "s99", "end_segment": "s1"}, "segment_unknown", ".start_segment"),
    ({"start_segment": "s1", "end_segment": "s99"}, "segment_unknown", ".end_segment"),
    ({"start_segment": 1, "end_segment": "s1"}, "segment_unknown", ".start_segment"),
    ({"start_segment": "s2", "end_segment": "s1"}, "range_reversed", ""),
    ({"start_segment": "s1"}, "field_missing", ".end_segment"),
    ({"start_segment": "s1", "end_segment": "s1", "id": "e9"}, "field_unexpected", ""),
])
def test_invalid_ranges_have_machine_location(selection, category, path):
    value = payload()
    value["core_points"][0]["source_ranges"] = [selection]
    with pytest.raises(CandidateError) as raised:
        parse(value)
    assert raised.value.category == category
    assert raised.value.field_path == "$.core_points[0].source_ranges[0]" + path


def test_duplicate_range_in_one_point_is_rejected_without_dropping_it():
    value = payload()
    value["core_points"][0]["source_ranges"] *= 2
    before = copy.deepcopy(value)
    with pytest.raises(CandidateError, match="range_duplicate") as raised:
        parse(value)
    assert raised.value.field_path == "$.core_points[0].source_ranges[1]"
    assert value == before


@pytest.mark.parametrize("ranges", [[], None, {}, "s1"])
def test_empty_or_nonlist_ranges_rejected(ranges):
    value = payload()
    value["core_points"][0]["source_ranges"] = ranges
    with pytest.raises(CandidateError, match="ranges_empty_or_invalid"):
        parse(value)


@pytest.mark.parametrize("snapshot,start,end", [
    ("甲。[听辨不清]。乙。", "s1", "s3"),
    ("甲。[听辨不清]。乙。", "s2", "s2"),
    ("[听辨不清]甲。", "s1", "s1"),
])
def test_missing_source_overlap_and_crossing_rejected(snapshot, start, end):
    value = payload()
    value["core_points"] = [point(start, end)]
    with pytest.raises(CandidateError, match="range_source_missing"):
        parse(value, snapshot)


def test_unselected_missing_source_does_not_reject_valid_local_range():
    assert parse(snapshot="甲。[听辨不清]。乙。").knowledge.evidence[0].text == "甲。"


@pytest.mark.parametrize("scope,key", [("root", "evidence"), ("root", "id"), ("root", "evidence_ids"),
                                       ("point", "id"), ("point", "evidence_ids")])
def test_old_table_and_model_ids_are_rejected(scope, key):
    value = payload()
    target = value if scope == "root" else value["core_points"][0]
    target[key] = ["e1"]
    with pytest.raises(CandidateError, match="field_unexpected"):
        parse(value)


def test_ids_and_hash_stable_under_json_key_order_and_formatting():
    value = payload()
    a = parse(value)
    reordered = dict(reversed(list(value.items())))
    reordered["core_points"] = [dict(reversed(list(value["core_points"][0].items())))]
    b = parse_knowledge_candidate("先检查。再执行。", json.dumps(reordered, indent=4, ensure_ascii=True))
    assert a == b == parse(value)
    assert a.snapshot_sha256 == hashlib.sha256("先检查。再执行。".encode()).hexdigest()
    canonical = json.dumps({"payload": value, "snapshot_sha256": a.snapshot_sha256,
                            "contract_version": module.CONTRACT_VERSION},
                           ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert a.candidate_hash == hashlib.sha256(canonical.encode()).hexdigest()
    assert (a.contract_version, a.parser_version, a.validator_version) == (
        module.CONTRACT_VERSION, module.PARSER_VERSION, module.VALIDATOR_VERSION)


def test_payload_snapshot_and_contract_changes_change_diagnostic_hash(monkeypatch):
    original = parse()
    value = payload()
    value["core_points"][0]["argument"] += "。另一个条件"
    assert parse(value).candidate_hash != original.candidate_hash
    changed_source = parse(snapshot="先检查。再执行。新增背景。")
    assert changed_source.candidate_hash != original.candidate_hash
    assert changed_source.knowledge == original.knowledge
    monkeypatch.setattr(module, "CONTRACT_VERSION", "future-contract")
    assert parse().candidate_hash != original.candidate_hash


def test_no_knowledge_is_a_normal_candidate_separate_from_failures():
    value = {"qualified": False, "rejection_reason": "  只有情绪表达，缺少独立判断  "}
    result = parse(value)
    assert result.qualified is False and result.knowledge is None
    assert result.rejection_reason == value["rejection_reason"]
    assert result.support_status == "not_reviewed"
    with pytest.raises(CandidateError) as syntax:
        parse_knowledge_candidate("合成来源。", "{")
    assert syntax.value.category == "json_syntax_invalid"
    with pytest.raises(CandidateError) as structure:
        parse({"qualified": False, "rejection_reason": " "})
    assert structure.value.category == "text_empty_or_invalid"


@pytest.mark.parametrize("value", [[], None, {"qualified": "true"}, {"qualified": 1}, {},
                                  {"qualified": False}, {"qualified": False, "rejection_reason": None}])
def test_malformed_candidate_is_not_no_knowledge(value):
    with pytest.raises(CandidateError):
        parse_knowledge_candidate("合成来源。", json.dumps(value))


@pytest.mark.parametrize("text,category", [("```json\n{}\n```", "json_syntax_invalid"),
    ('{"qualified":false,"qualified":true}', "json_duplicate_key"),
    ('{"qualified": NaN}', "json_syntax_invalid"), ("{} trailing", "json_syntax_invalid")])
def test_strict_json_rejects_ambiguous_or_nonstandard_input(text, category):
    with pytest.raises(CandidateError) as raised:
        parse_knowledge_candidate("合成来源。", text)
    assert raised.value.category == category


@pytest.mark.parametrize("mutate,path", [
    (lambda v: v.pop("summary"), "$.summary"),
    (lambda v: v.update(subtitle=v["title"]), "$.subtitle"),
    (lambda v: v.update(summary="分行\n摘要"), "$.summary"),
    (lambda v: v.update(core_points=[], other_points=[]), "$.core_points"),
    (lambda v: v["core_points"][0].update(argument=""), "$.core_points[0].argument"),
    (lambda v: v.update(rejection_reason="有知识却拒绝"), "$.rejection_reason"),
])
def test_domain_structure_guarantees_have_field_locations(mutate, path):
    value = payload()
    mutate(value)
    with pytest.raises(CandidateError) as raised:
        parse(value)
    assert raised.value.field_path == path


def test_other_points_only_is_valid():
    value = payload()
    value["other_points"], value["core_points"] = value["core_points"], []
    assert parse(value).knowledge.other_points[0].point_id == "p1"


def test_existing_domain_validator_is_called_and_errors_are_sanitized(monkeypatch):
    calls = []
    def reject(snapshot, knowledge):
        calls.append((snapshot, knowledge))
        raise ValueError(snapshot)
    monkeypatch.setattr(module, "validate_knowledge", reject)
    with pytest.raises(CandidateError) as raised:
        parse(snapshot="私密合成正文。")
    assert len(calls) == 1
    assert raised.value.category == "domain_invalid"
    assert raised.value.field_path == "$.knowledge"
    assert "私密合成正文" not in str(raised.value)
    assert raised.value.__suppress_context__


def test_empty_evidence_guard_even_if_segment_provider_regresses(monkeypatch):
    monkeypatch.setattr(module, "source_segments", lambda snapshot: {"s1": (0, 2)})
    with pytest.raises(CandidateError, match="evidence_empty"):
        parse(snapshot="  甲。")


def test_unknown_field_names_cannot_leak_source_into_error_messages():
    value = payload()
    value["私密合成正文"] = "私密合成正文"
    with pytest.raises(CandidateError) as raised:
        parse(value, "私密合成正文。")
    assert str(raised.value) == "field_unexpected: $"


@pytest.mark.parametrize("snapshot", ["", " \n\t", None])
def test_empty_or_nontext_snapshot_is_a_fault(snapshot):
    with pytest.raises(CandidateError, match="snapshot_empty_or_invalid"):
        parse_knowledge_candidate(snapshot, json.dumps(payload()))


def test_missing_source_ranges_is_located():
    value = payload()
    del value["core_points"][0]["source_ranges"]
    with pytest.raises(CandidateError) as raised:
        parse(value)
    assert (raised.value.category, raised.value.field_path) == (
        "field_missing", "$.core_points[0].source_ranges")


@pytest.mark.parametrize("kind", ["image", "video", "collection"])
def test_nontext_kinds_explicitly_unsupported(kind):
    with pytest.raises(CandidateError) as raised:
        parse_knowledge_candidate("合成来源。", json.dumps(payload()), source_kind=kind)
    assert (raised.value.category, raised.value.field_path) == ("source_kind_unsupported", "$.source_kind")


def test_correct_numbering_but_wrong_meaning_is_only_structural_candidate():
    value = payload()
    value["core_points"] = [point(statement="必须开启网络", argument="网络连接是操作前提")]
    result = parse(value, "禁止开启网络。")
    assert result.qualified and result.knowledge is not None
    assert result.knowledge.evidence[0].text == "禁止开启网络。"
    assert result.knowledge.core_points[0].statement == "必须开启网络"
    assert result.support_status == "not_reviewed"
