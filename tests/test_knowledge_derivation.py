import json
from dataclasses import replace

import httpx
import pytest

from knowledge_distiller.knowledge_derivation import (
    AnthropicCompatibleDerivationRuntime,
    DerivationFailure,
    KnowledgeCandidate,
    KnowledgeDerivationAdapter,
    KnowledgeEvidence,
    KnowledgePoint,
    _DerivationRuntimeFailure,
    _DerivationRuntimeResult,
    _DerivationRuntimeUnavailable,
    knowledge_candidate_from_payload,
    knowledge_candidate_payload,
    validate_knowledge_candidate,
)


SNAPSHOT = (
    "药品集采先由公立医院上报约定采购量。"
    "企业参与报价，中选后按约定供应。"
    "完成采购量后，节约资金的一部分可以用于医院医护人员绩效奖励。"
)


def valid_payload():
    def evidence(evidence_id, evidence_text):
        return {
            "id": evidence_id,
            "occurrence": 0,
            "evidence_text": evidence_text,
        }

    return {
        "title": "药品集采如何形成采购与激励闭环",
        "summary": "医院报量、企业竞价供应，完成采购后再按节约结果形成激励。",
        "core_points": [
            {
                "id": "p1",
                "statement": "药品集采从公立医院上报约定采购量开始。",
                "argument": "先汇总明确需求，再进入企业报价和后续供应环节。",
                "evidence_ids": ["e1", "e2"],
            },
            {
                "id": "p2",
                "statement": "中选企业承担按约定供应的责任。",
                "argument": "企业报价中选后进入供应环节，采购完成后才讨论节约激励。",
                "evidence_ids": ["e2", "e3"],
            },
        ],
        "other_points": [
            {
                "id": "p3",
                "statement": "采购节约可以转化为医院医护人员的绩效激励。",
                "argument": "完成采购量以后，部分节约资金可以用于绩效奖励。",
                "evidence_ids": ["e3"],
            }
        ],
        "evidence_registry": [
            evidence("e1", "药品集采先由公立医院上报约定采购量。"),
            evidence("e2", "企业参与报价，中选后按约定供应。"),
            evidence(
                "e3",
                "完成采购量后，节约资金的一部分可以用于医院医护人员绩效奖励。",
            ),
        ],
    }


class DerivationBinding:
    def __init__(self, payload=None, *, stop_reason="end_turn", error=None):
        self.payload = payload
        self.stop_reason = stop_reason
        self.error = error
        self.calls = []

    def complete(self, source_fact_id, snapshot, uncertainties):
        self.calls.append((source_fact_id, snapshot, uncertainties))
        if self.error is not None:
            raise self.error
        return _DerivationRuntimeResult(
            self.payload
            if isinstance(self.payload, str)
            else json.dumps(self.payload, ensure_ascii=False),
            self.stop_reason,
        )


def test_complete_candidate_supports_multiple_and_shared_evidence():
    binding = DerivationBinding(valid_payload())

    result = KnowledgeDerivationAdapter(binding).derive(7, SNAPSHOT, [])

    assert result.failure is None
    candidate = result.candidate
    assert candidate.title == "药品集采如何形成采购与激励闭环"
    assert candidate.core_points[0].evidence_ids == ("e1", "e2")
    assert candidate.core_points[1].evidence_ids == ("e2", "e3")
    assert candidate.other_points[0].evidence_ids == ("e3",)
    evidence = {item.evidence_id: item for item in candidate.evidence_registry}
    assert evidence["e2"].source_fact_id == 7
    assert (
        SNAPSHOT[evidence["e2"].start_offset : evidence["e2"].end_offset]
        == evidence["e2"].evidence_text
    )
    assert binding.calls == [(7, SNAPSHOT, [])]
    assert "_DerivationRuntimeResult" not in repr(candidate)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update(title=""),
        lambda payload: payload["evidence_registry"][0].update(
            evidence_text="快照中不存在的证据"
        ),
        lambda payload: payload["core_points"][0].update(
            evidence_ids=["missing"]
        ),
        lambda payload: payload["core_points"][1].update(id="p1"),
        lambda payload: payload["evidence_registry"][1].update(id="e1"),
    ],
)
def test_malformed_or_inconsistent_payload_is_rejected(mutate):
    payload = valid_payload()
    mutate(payload)

    result = KnowledgeDerivationAdapter(DerivationBinding(payload)).derive(
        7,
        SNAPSHOT,
        [],
    )

    assert result.failure is DerivationFailure.INVALID_OUTPUT


@pytest.mark.parametrize("payload", ["not json", {}, [], None])
def test_empty_or_malformed_model_result_is_rejected(payload):
    result = KnowledgeDerivationAdapter(DerivationBinding(payload)).derive(
        7,
        SNAPSHOT,
        [],
    )

    assert result.failure is DerivationFailure.INVALID_OUTPUT


def test_non_normal_model_ending_is_rejected():
    result = KnowledgeDerivationAdapter(
        DerivationBinding(valid_payload(), stop_reason="max_tokens")
    ).derive(7, SNAPSHOT, [])

    assert result.failure is DerivationFailure.INCOMPLETE


@pytest.mark.parametrize(
    "error,expected",
    [
        (_DerivationRuntimeUnavailable(), DerivationFailure.RUNTIME_UNAVAILABLE),
        (_DerivationRuntimeFailure(), DerivationFailure.RUNTIME_FAILED),
    ],
)
def test_runtime_failures_are_translated(error, expected):
    result = KnowledgeDerivationAdapter(
        DerivationBinding(error=error)
    ).derive(7, SNAPSHOT, [])

    assert result.failure is expected


def test_project_validator_rejects_invalid_locator_or_source_fact():
    valid = KnowledgeDerivationAdapter(DerivationBinding(valid_payload())).derive(
        7,
        SNAPSHOT,
        [],
    ).candidate
    evidence = valid.evidence_registry[0]
    wrong_span = replace(
        valid,
        evidence_registry=(
            replace(evidence, end_offset=evidence.end_offset - 1),
            *valid.evidence_registry[1:],
        ),
    )
    wrong_source = replace(
        valid,
        evidence_registry=(
            replace(evidence, source_fact_id=8),
            *valid.evidence_registry[1:],
        ),
    )

    assert validate_knowledge_candidate(7, SNAPSHOT, wrong_span) is False
    assert validate_knowledge_candidate(7, SNAPSHOT, wrong_source) is False


def test_repeated_evidence_is_accepted_with_an_explicit_valid_span():
    repeated_text = "按约定供应。"
    repeated_snapshot = repeated_text + "其他内容。" + repeated_text
    second_start = repeated_snapshot.rindex(repeated_text)
    payload = {
        "title": "重复原文仍可明确定位",
        "summary": "证据由明确跨度定位。",
        "core_points": [
            {
                "id": "p1",
                "statement": "引用第二次出现的原文。",
                "argument": "明确跨度能够区分相同文本的不同出现位置。",
                "evidence_ids": ["e1"],
            }
        ],
        "other_points": [],
        "evidence_registry": [
            {
                "id": "e1",
                "occurrence": 1,
                "evidence_text": repeated_text,
            }
        ],
    }

    result = KnowledgeDerivationAdapter(DerivationBinding(payload)).derive(
        7,
        repeated_snapshot,
        [],
    )

    assert result.failure is None
    assert result.candidate.evidence_registry[0].start_offset == second_start


def test_provider_evidence_occurrence_outside_exact_matches_is_rejected():
    payload = valid_payload()
    payload["evidence_registry"][0]["occurrence"] = 1

    result = KnowledgeDerivationAdapter(DerivationBinding(payload)).derive(
        7,
        SNAPSHOT,
        [],
    )

    assert result.failure is DerivationFailure.INVALID_OUTPUT


def test_provider_evidence_text_mismatching_its_span_is_rejected():
    payload = valid_payload()
    payload["evidence_registry"][0]["evidence_text"] = "并非该跨度中的逐字原文"

    result = KnowledgeDerivationAdapter(DerivationBinding(payload)).derive(
        7,
        SNAPSHOT,
        [],
    )

    assert result.failure is DerivationFailure.INVALID_OUTPUT


def test_project_validator_rejects_unreferenced_evidence():
    evidence_text = "上报约定采购量"
    start = SNAPSHOT.index(evidence_text)
    candidate = KnowledgeCandidate(
        "标题",
        "总括",
        (
            KnowledgePoint("p1", "观点", "论证", ("e1",)),
        ),
        (),
        (
            KnowledgeEvidence("e1", 7, 0, 19, SNAPSHOT[:19]),
            KnowledgeEvidence(
                "unused",
                7,
                start,
                start + len(evidence_text),
                evidence_text,
            ),
        ),
    )

    assert validate_knowledge_candidate(7, SNAPSHOT, candidate) is False


def test_project_validator_rejects_invalid_candidate_container_types():
    valid = KnowledgeDerivationAdapter(DerivationBinding(valid_payload())).derive(
        7,
        SNAPSHOT,
        [],
    ).candidate

    assert (
        validate_knowledge_candidate(
            7,
            SNAPSHOT,
            replace(valid, core_points=list(valid.core_points)),
        )
        is False
    )
    assert (
        validate_knowledge_candidate(
            7,
            SNAPSHOT,
            replace(
                valid,
                core_points=(replace(valid.core_points[0], evidence_ids=["e1"]),),
            ),
        )
        is False
    )


def test_runtime_explicitly_disables_extra_provider_thinking(monkeypatch):
    captured = {}

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(valid_payload(), ensure_ascii=False),
                    }
                ],
                "stop_reason": "end_turn",
            }

    def fake_post(url, *, headers, json, timeout):
        captured.update(json)
        return Response()

    monkeypatch.setattr(httpx, "post", fake_post)

    result = AnthropicCompatibleDerivationRuntime(
        "https://provider.example",
        "derivation-model",
        "private-test-key",
    ).complete(7, SNAPSHOT, [])

    assert captured["max_tokens"] == 8192
    assert captured["thinking"] == {"type": "disabled"}
    assert result.stop_reason == "end_turn"


def test_formal_payload_codec_round_trip_preserves_complete_candidate():
    candidate = KnowledgeDerivationAdapter(DerivationBinding(valid_payload())).derive(
        7,
        SNAPSHOT,
        [],
    ).candidate

    restored = knowledge_candidate_from_payload(
        7,
        SNAPSHOT,
        knowledge_candidate_payload(candidate),
    )

    assert restored == candidate
    assert restored.core_points[0].argument == candidate.core_points[0].argument
    assert restored.other_points[0].point_id == "p3"
    assert restored.evidence_registry == candidate.evidence_registry


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.pop("summary"),
        lambda payload: payload.update(core_points="not-a-list"),
        lambda payload: payload["core_points"][0].update(id=12),
        lambda payload: payload["evidence_registry"][0].update(start=True),
        lambda payload: payload["core_points"][1].update(id="p1"),
        lambda payload: payload["evidence_registry"][1].update(id="e1"),
        lambda payload: payload["core_points"][0].update(evidence_ids=["missing"]),
        lambda payload: payload["evidence_registry"][0].update(
            evidence_text="不在对应跨度"
        ),
    ],
)
def test_formal_payload_codec_strictly_rejects_invalid_payload(mutate):
    candidate = KnowledgeDerivationAdapter(DerivationBinding(valid_payload())).derive(
        7,
        SNAPSHOT,
        [],
    ).candidate
    payload = knowledge_candidate_payload(candidate)
    mutate(payload)

    with pytest.raises(ValueError, match="KnowledgeResult payload is invalid"):
        knowledge_candidate_from_payload(7, SNAPSHOT, payload)
