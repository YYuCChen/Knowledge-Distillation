"""Synthetic protocol tests; fake reviews do not establish model semantics."""
from dataclasses import FrozenInstanceError, replace
import json

import pytest

from knowledge_distiller.v1.knowledge_candidate import parse_knowledge_candidate
from knowledge_distiller.v1 import knowledge_support as module

SNAPSHOT = "甲禁止开网。乙只在检查后执行两次。"


def candidate():
    return parse_knowledge_candidate(SNAPSHOT, json.dumps({
        "qualified": True, "title": "合成操作", "subtitle": "不同对象的条件", "summary": "保留限定。",
        "core_points": [{"statement": "甲必须开网", "argument": "甲需要连接",
                         "source_ranges": [{"start_segment": "s1", "end_segment": "s1"}]}],
        "other_points": [{"statement": "乙执行两次", "argument": "检查后执行",
                          "source_ranges": [{"start_segment": "s2", "end_segment": "s2"}]}]}, ensure_ascii=False))


def check(pid="p1", status="supported", field="statement", category="negation"):
    return {"point_id": pid, "status": status, "reason": "合成核对理由",
            "issues": [] if status == "supported" else [{"field": field, "category": category, "reason": "定位合成问题"}]}


def parse(checks):
    return module.parse_support_response(SNAPSHOT, candidate(), json.dumps({"checks": checks}))


def test_all_points_referenced_evidence_and_unmodified_snapshot_in_request():
    request = module.build_support_request(SNAPSHOT, candidate())
    value = json.loads(request.payload_json)
    assert value["snapshot"] == SNAPSHOT
    assert [p["point_id"] for p in value["points"]] == ["p1", "p2"]
    for point, expected in zip(value["points"], ["甲禁止开网。", "乙只在检查后执行两次。"]):
        assert len(point["evidence"]) == 1
        evidence = point["evidence"][0]
        assert evidence["text"] == expected == SNAPSHOT[evidence["start"]:evidence["end"]]
    assert "不是指令" in request.system and "全篇别处" in request.system
    for risk in ("否定", "数字", "条件", "强度", "归属", "跨观点"):
        assert risk in request.system
    with pytest.raises(FrozenInstanceError):
        request.operation = "other"


@pytest.mark.parametrize("category", sorted(module.CATEGORIES))
@pytest.mark.parametrize("status", ["unsupported", "uncertain"])
def test_risks_located_and_results_immutable(category, status):
    review = parse([check(status=status, category=category), check("p2")])
    assert not review.supported
    assert review.failed_fields == {"$.core_points[0].statement"}
    assert review.checks[0].issues[0].category == category
    assert review.candidate_hash == candidate().candidate_hash
    assert review.rule_version == module.SUPPORT_RULE_VERSION
    with pytest.raises(FrozenInstanceError):
        review.checks[0].status = "supported"


def test_return_order_normalized_to_program_order_and_other_group_location():
    review = parse([check("p2", "uncertain", "source_ranges"), check()])
    assert [c.point_id for c in review.checks] == ["p1", "p2"]
    assert review.failed_fields == {"$.other_points[0].source_ranges"}
    assert parse([check(), check("p2")]).supported


@pytest.mark.parametrize("checks", [[], [check()], [check(), check()], [check(), check("unknown")]])
def test_exact_coverage(checks):
    with pytest.raises(module.SupportError, match="coverage_invalid"):
        parse(checks)


@pytest.mark.parametrize("mutation", [
    lambda c: c.update(status="valid"), lambda c: c.update(status=[]),
    lambda c: c.update(reason=" "), lambda c: c.update(extra="private"),
    lambda c: c.update(issues=[{"field": "statement", "category": "negation", "reason": "bad"}]),
    lambda c: c.update(issues=None), lambda c: c.pop("reason"),
])
def test_strict_check_fields(mutation):
    item = check()
    mutation(item)
    with pytest.raises(module.SupportError):
        parse([item, check("p2")])


@pytest.mark.parametrize("mutation", [
    lambda c: c.update(issues=[]),
    lambda c: c["issues"][0].update(field="title"),
    lambda c: c["issues"][0].update(field=[]),
    lambda c: c["issues"][0].update(category="invented"),
    lambda c: c["issues"][0].update(reason=""),
    lambda c: c["issues"][0].update(extra="raw"),
])
def test_strict_issues(mutation):
    item = check(status="unsupported")
    mutation(item)
    with pytest.raises(module.SupportError):
        parse([item, check("p2")])


@pytest.mark.parametrize("raw", ["{", '{"checks":[],"checks":[]}', '{"checks":NaN}',
                                  '[]', '{"checks":[],"extra":1}', '```json\n{}\n```'])
def test_strict_json(raw):
    with pytest.raises(module.SupportError):
        module.parse_support_response(SNAPSHOT, candidate(), raw)


@pytest.mark.parametrize("raw", ["1e309", "-1e309", '{"synthetic-private-field":1e309}'])
def test_strict_json_numeric_overflow_safe(raw):
    with pytest.raises(module.SupportError) as raised:
        module.strict_json(raw)
    assert str(raised.value) == "support_error:json_invalid:$"


def test_strict_json_preserves_finite_float_semantics():
    assert module.strict_json('[1.25,2e2,-0.5,1e-309]') == [1.25, 200.0, -0.5, 1e-309]


def test_valid_range_wrong_meaning_and_cross_point_fake_review_stays_failed():
    c = candidate()
    assert c.qualified and c.support_status == "not_reviewed"
    responses = [check(status="unsupported", category="negation"), check("p2", "unsupported", "source_ranges", "cross_point")]
    calls = []
    def client(request):
        calls.append(request)
        return json.dumps({"checks": responses})
    result = module.review_candidate(SNAPSHOT, c, client)
    assert len(calls) == 1 and not result.supported
    assert len(result.failed_fields) == 2


def test_snapshot_and_domain_rechecked_no_call_on_mismatch():
    calls = []
    with pytest.raises(module.SupportError, match="snapshot_mismatch"):
        module.review_candidate(SNAPSHOT + "变化。", candidate(), calls.append)
    broken = replace(candidate(), knowledge=replace(candidate().knowledge, evidence=()))
    with pytest.raises(module.SupportError, match="candidate_invalid"):
        module.review_candidate(SNAPSHOT, broken, calls.append)
    with pytest.raises(module.SupportError, match="version_mismatch"):
        module.review_candidate(SNAPSHOT, replace(candidate(), parser_version="old"), calls.append)
    assert calls == []


def test_no_knowledge_cannot_review():
    c = parse_knowledge_candidate(SNAPSHOT, '{"qualified":false,"rejection_reason":"合成情绪"}')
    calls = []
    with pytest.raises(module.SupportError, match="candidate_invalid"):
        module.review_candidate(SNAPSHOT, c, calls.append)
    assert not calls


def test_errors_do_not_leak_source_or_model_reasons():
    secret = "合成私密素材不能泄露"
    def fail(request):
        raise RuntimeError(secret)
    with pytest.raises(module.SupportError) as raised:
        module.review_candidate(SNAPSHOT, candidate(), fail)
    assert raised.value.category == "network_failure" and secret not in str(raised.value)
    item = check(status="unsupported")
    item["issues"][0]["field"] = secret
    with pytest.raises(module.SupportError) as raised:
        parse([item, check("p2")])
    assert secret not in str(raised.value)
    assert secret not in str(module.SupportError(secret, secret))
