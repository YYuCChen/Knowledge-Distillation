import json
from dataclasses import replace

import httpx
import pytest

from knowledge_distiller.knowledge_derivation import (
    KnowledgeCandidate,
    KnowledgeEvidence,
    KnowledgePoint,
)
from knowledge_distiller.legacy.knowledge_qualification import (
    AnthropicCompatibleQualificationRuntime,
    KnowledgeQualificationAdapter,
    QualificationFailure,
    _QualificationRuntimeFailure,
    _QualificationRuntimeResult,
    _QualificationRuntimeUnavailable,
)


SNAPSHOT = "医院先报量，企业中选后按约定供应。"


def candidate():
    evidence_text = SNAPSHOT
    return KnowledgeCandidate(
        "集采从报量进入履约",
        "医院先报量，企业中选后履约。",
        (
            KnowledgePoint(
                "p1",
                "医院报量是后续采购安排的起点。",
                "医院先报量，随后企业中选并按约定供应。",
                ("e1",),
            ),
        ),
        (),
        (
            KnowledgeEvidence(
                "e1",
                7,
                0,
                len(evidence_text),
                evidence_text,
            ),
        ),
    )


def verdict(
    *,
    qualified=True,
    evidence_supports=True,
    uncertainty_preserved=True,
    core_order_valid=True,
    other_order_valid=True,
    issues=None,
):
    return {
        "qualified": qualified,
        "point_reviews": [
            {
                "point_id": "p1",
                "evidence_supports": evidence_supports,
                "uncertainty_preserved": uncertainty_preserved,
            }
        ],
        "core_order_valid": core_order_valid,
        "other_order_valid": other_order_valid,
        "issues": issues or [],
    }


class QualificationBinding:
    def __init__(self, payload=None, *, stop_reason="end_turn", error=None):
        self.payload = payload
        self.stop_reason = stop_reason
        self.error = error
        self.calls = []

    def complete(self, source_fact_id, snapshot, uncertainties, candidate_value):
        self.calls.append(
            (source_fact_id, snapshot, uncertainties, candidate_value)
        )
        if self.error is not None:
            raise self.error
        return _QualificationRuntimeResult(
            self.payload
            if isinstance(self.payload, str)
            else json.dumps(self.payload, ensure_ascii=False),
            self.stop_reason,
        )


def test_complete_semantic_review_qualifies_without_rewriting_candidate():
    binding = QualificationBinding(verdict())
    candidate_value = candidate()

    result = KnowledgeQualificationAdapter(binding).qualify(
        7,
        SNAPSHOT,
        [],
        candidate_value,
    )

    assert result.qualified is True
    assert result.issues == ()
    assert result.failure is None
    assert binding.calls == [(7, SNAPSHOT, [], candidate_value)]


@pytest.mark.parametrize(
    "updates,reason",
    [
        (
            {"evidence_supports": False},
            "证据只与主题相关，不能支持观点中的条件。",
        ),
        (
            {"uncertainty_preserved": False},
            "观点把来源中的局部不确定表达升级成了确定结论。",
        ),
    ],
)
def test_semantic_review_rejects_unsupported_or_overcertain_point(updates, reason):
    payload = verdict(
        qualified=False,
        issues=[{"point_id": "p1", "reason": reason}],
        **updates,
    )

    result = KnowledgeQualificationAdapter(
        QualificationBinding(payload)
    ).qualify(7, SNAPSHOT, [], candidate())

    assert result.qualified is False
    assert result.failure is None
    assert result.issues[0].point_id == "p1"
    assert result.issues[0].reason == reason


def test_semantic_review_rejects_invalid_reading_order():
    result = KnowledgeQualificationAdapter(
        QualificationBinding(
            verdict(
                qualified=False,
                core_order_valid=False,
                issues=[{"point_id": None, "reason": "核心观点顺序打乱作者逻辑。"}],
            )
        )
    ).qualify(7, SNAPSHOT, [], candidate())

    assert result.qualified is False
    assert result.issues[0].point_id is None


def test_point_reviews_must_follow_existing_candidate_order():
    candidate_value = candidate()
    second_point = KnowledgePoint(
        "p2",
        "企业中选后承担供应责任。",
        "企业中选以后按约定供应。",
        ("e1",),
    )
    candidate_value = replace(
        candidate_value,
        core_points=(candidate_value.core_points[0], second_point),
    )
    payload = verdict()
    payload["point_reviews"] = [
        {
            "point_id": "p2",
            "evidence_supports": True,
            "uncertainty_preserved": True,
        },
        {
            "point_id": "p1",
            "evidence_supports": True,
            "uncertainty_preserved": True,
        },
    ]

    result = KnowledgeQualificationAdapter(
        QualificationBinding(payload)
    ).qualify(7, SNAPSHOT, [], candidate_value)

    assert result.failure is QualificationFailure.INVALID_OUTPUT


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        {},
        {"qualified": True},
        verdict(qualified=True, evidence_supports=False),
        verdict(qualified=False, evidence_supports=False),
        {
            **verdict(),
            "point_reviews": [],
        },
    ],
)
def test_malformed_or_contradictory_qualification_is_rejected(payload):
    result = KnowledgeQualificationAdapter(
        QualificationBinding(payload)
    ).qualify(7, SNAPSHOT, [], candidate())

    assert result.failure is QualificationFailure.INVALID_OUTPUT


def test_non_normal_qualification_ending_is_rejected():
    result = KnowledgeQualificationAdapter(
        QualificationBinding(verdict(), stop_reason="max_tokens")
    ).qualify(7, SNAPSHOT, [], candidate())

    assert result.failure is QualificationFailure.INCOMPLETE


@pytest.mark.parametrize(
    "error,expected",
    [
        (
            _QualificationRuntimeUnavailable(),
            QualificationFailure.RUNTIME_UNAVAILABLE,
        ),
        (_QualificationRuntimeFailure(), QualificationFailure.RUNTIME_FAILED),
    ],
)
def test_qualification_runtime_failures_are_translated(error, expected):
    result = KnowledgeQualificationAdapter(
        QualificationBinding(error=error)
    ).qualify(7, SNAPSHOT, [], candidate())

    assert result.failure is expected


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
                        "text": json.dumps(verdict(), ensure_ascii=False),
                    }
                ],
                "stop_reason": "end_turn",
            }

    def fake_post(url, *, headers, json, timeout):
        captured.update(json)
        return Response()

    monkeypatch.setattr(httpx, "post", fake_post)

    result = AnthropicCompatibleQualificationRuntime(
        "https://provider.example",
        "qualification-model",
        "private-test-key",
    ).complete(7, SNAPSHOT, [], candidate())

    assert captured["max_tokens"] == 4096
    assert captured["thinking"] == {"type": "disabled"}
    assert result.stop_reason == "end_turn"
