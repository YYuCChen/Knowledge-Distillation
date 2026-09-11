from __future__ import annotations

import json

import pytest

from knowledge_distiller.growth_modeling import (
    GrowthModelFailure,
    GrowthRuntimeResult,
    HistoricalRecallAdapter,
    RecallSelection,
    RelationInsightAdapter,
    build_historical_recall_planner,
    build_relation_insight_planner,
)
from knowledge_distiller.growth_qualification import (
    QualificationContext,
    qualify_growth_plan,
)
from knowledge_distiller.organization_models import (
    EvolutionBasisCard,
    GrowthIdentityCatalog,
    InsightCatalogState,
    InsightIdentityCatalogEntry,
    RelationIdentityCatalogEntry,
    parse_insight_payload,
    parse_relation_payload,
)
from tests.fixtures.growth import (
    empty_growth_plan_payload,
    empty_identity_catalog,
    healthy_boundary,
    insight_payload,
    relation_payload,
)


class FakeRuntime:
    def __init__(self, payload: object, *, stop_reason: str = "end_turn", available=True):
        self.payload = payload
        self.stop_reason = stop_reason
        self.available = available
        self.calls: list[dict[str, object]] = []

    def is_available(self) -> bool:
        return self.available

    def complete(self, *, system_prompt, input_payload, max_tokens):
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "input_payload": input_payload,
                "max_tokens": max_tokens,
            }
        )
        return GrowthRuntimeResult(
            self.payload if isinstance(self.payload, str) else json.dumps(self.payload),
            self.stop_reason,
        )


def test_recall_adapter_only_accepts_ids_from_frozen_identity_lists():
    boundary = healthy_boundary()
    runtime = FakeRuntime(
        {
            "codec": "historical-recall-v1",
            "source_knowledge_ids": [3],
            "accepted_insight_version_ids": [50],
            "current_relation_version_ids": [70],
            "reconsideration_hint_version_ids": [80],
        }
    )
    result = HistoricalRecallAdapter(runtime).recall(boundary)

    assert result.failure is None
    assert result.selection is not None
    assert result.selection.source_knowledge_ids == (3,)
    assert runtime.calls[0]["input_payload"]["reconsideration_hints"][0][
        "boundary_role"
    ] == "reconsideration_hint"
    frozen_point = runtime.calls[0]["input_payload"]["frozen_new"][0]["points"][0]
    assert frozen_point == {
        "point_id": "p1",
        "role": "core",
        "statement": "Statement 1/p1",
        "argument": "Argument 1/p1",
    }


def test_recall_adapter_rejects_unknown_identity_and_bool_as_int():
    boundary = healthy_boundary()
    for invalid_id in (999, True):
        runtime = FakeRuntime(
            {
                "codec": "historical-recall-v1",
                "source_knowledge_ids": [invalid_id],
                "accepted_insight_version_ids": [],
                "current_relation_version_ids": [],
                "reconsideration_hint_version_ids": [],
            }
        )
        result = HistoricalRecallAdapter(runtime).recall(boundary)
        assert result.failure is GrowthModelFailure.INVALID_OUTPUT


def test_relation_adapter_requires_normal_end_turn_before_strict_codec():
    boundary = healthy_boundary(include_hint=False)
    runtime = FakeRuntime(empty_growth_plan_payload(), stop_reason="max_tokens")
    result = RelationInsightAdapter(runtime).plan(
        boundary,
        HistoricalRecallAdapter(FakeRuntime({}), expand_all=True).recall(boundary).selection,
        identity_catalog=empty_identity_catalog(),
        expanded_inputs={},
        topic_before={},
        topic_plan={},
    )

    assert result.failure is GrowthModelFailure.INCOMPLETE
    assert result.plan is None


def test_production_relation_adapter_normal_zero_reaches_codec_and_qualification():
    """E2E-10: normal adapter end_turn, strict codec and qualification prove zero output."""
    boundary = healthy_boundary(include_hint=False)
    runtime = FakeRuntime(empty_growth_plan_payload())
    recall = HistoricalRecallAdapter(FakeRuntime({}), expand_all=True).recall(boundary)

    planning = RelationInsightAdapter(runtime).plan(
        boundary,
        recall.selection,
        identity_catalog=empty_identity_catalog(),
        expanded_inputs={"sources": []},
        topic_before={"state": "no_index"},
        topic_plan={"topics": [], "unassigned_points": []},
    )
    assert planning.failure is None
    qualified = qualify_growth_plan(
        planning.plan,
        QualificationContext(boundary=boundary),
    )

    assert qualified.plan.new_relations == ()
    assert qualified.plan.candidate_versions == ()
    assert len(runtime.calls) == 1
    assert runtime.calls[0]["input_payload"]["codec"] == "growth-planner-input-v1"


def test_relation_adapter_receives_identity_catalog_without_input_authority():
    boundary = healthy_boundary(include_hint=False)
    runtime = FakeRuntime(empty_growth_plan_payload())
    catalog = GrowthIdentityCatalog(
        relation_versions=(
            RelationIdentityCatalogEntry(
                9,
                90,
                1,
                parse_relation_payload(relation_payload()),
                "a" * 64,
                EvolutionBasisCard(
                    (("source_knowledge", 1, "p1"),),
                    (("Premise", (0,)),),
                    (),
                    "d" * 64,
                ),
                True,
                False,
                ("wrong",),
            ),
        ),
        insight_versions=(
            InsightIdentityCatalogEntry(
                10,
                100,
                1,
                parse_insight_payload(insight_payload()),
                "b" * 64,
                EvolutionBasisCard(
                    (("source_knowledge", 2, "p1"),),
                    (("Premise", (0,)),),
                    (),
                    "e" * 64,
                ),
                True,
                InsightCatalogState.PENDING,
                (),
                None,
            ),
        ),
        signature="c" * 64,
    )

    planning = RelationInsightAdapter(runtime).plan(
        boundary,
        HistoricalRecallAdapter(FakeRuntime({}), expand_all=True).recall(boundary).selection,
        identity_catalog=catalog,
        expanded_inputs={"frozen_new": []},
        topic_before={},
        topic_plan={},
    )

    assert planning.failure is None
    transported = runtime.calls[0]["input_payload"]["identity_catalog"]
    assert transported["codec"] == "growth-identity-catalog-v2"
    assert transported["authority"] == (
        "identity_reuse_and_evolution_comparison_only"
    )
    assert transported["relation_versions"][0]["permanent_facts"] == ["wrong"]
    assert transported["insight_versions"][0]["state"] == "pending"
    assert transported["relation_versions"][0]["evolution_basis"]["fingerprint"] == (
        "d" * 64
    )
    assert transported["relation_versions"][0]["evolution_basis"][
        "premise_support_map"
    ] == [
        {"premise_text": "Premise", "support_identity_indexes": [0]}
    ]
    assert "participant" in transported["prohibited_permissions"]
    assert "identity_catalog" not in runtime.calls[0]["input_payload"]["expanded_inputs"]


@pytest.mark.parametrize(
    ("selected_hints", "expected_versions"),
    [((), []), ((80,), [80])],
)
def test_relation_adapter_only_receives_exact_recall_selected_hint_cards(
    selected_hints, expected_versions
):
    boundary = healthy_boundary(include_hint=True)
    runtime = FakeRuntime(empty_growth_plan_payload())
    recall = RecallSelection((), (), (), selected_hints)

    planning = RelationInsightAdapter(runtime).plan(
        boundary,
        recall,
        identity_catalog=empty_identity_catalog(),
        expanded_inputs={},
        topic_before={},
        topic_plan={},
    )

    assert planning.failure is None
    assert [
        item["relation_version_id"]
        for item in runtime.calls[0]["input_payload"]["reconsideration_hints"]
    ] == expected_versions


def test_production_relation_prompt_contains_complete_strict_output_contract():
    boundary = healthy_boundary(include_hint=False)
    runtime = FakeRuntime(empty_growth_plan_payload())

    result = RelationInsightAdapter(runtime).plan(
        boundary,
        RecallSelection((), (), (), ()),
        identity_catalog=empty_identity_catalog(),
        expanded_inputs={},
        topic_before={},
        topic_plan={},
    )

    assert result.failure is None
    prompt = runtime.calls[0]["system_prompt"]
    for required in (
        '"codec":"growth-plan-v1"',
        '"accepted_disqualifications":[]',
        '"fact_kind":"basis_invalid"|"refuted"',
        '"directly_affected":boolean',
        '"attention_state":"activated"|"retired"',
        '"target_kind":"create_identity"',
        '"target_kind":"evolve_identity"',
        '"previous_relation_version_id":integer>=1',
        '"previous_insight_version_id":integer>=1',
        '"replaces_insight_id":integer>=1',
        '"input_kind":"source_knowledge"',
        '"input_kind":"accepted_insight"',
        '"ref_kind":"planned_stable"',
        '"codec":"relation-v1"',
        '"codec":"insight-v1"',
        '"claim_kind":"judgment"|"hypothesis"|"question"',
        '"output_kind":"exploration_only"|"illegal"',
        "basis_invalid表示必要基础失效，refuted表示正式依据足以反驳该exact version",
        "frozen_new 可直接成为 participant",
        "exact Recall selected 的项才有 participant 权",
        "relation_current 只是 compact impact locator",
        "selected_current_relations 的 full graph",
        "selected_reconsideration_hints 的 full graph 仍只有 requalification-only",
        "H 写本 event activation 前不得 used",
        "identity_catalog 不授予 accepted current 永久退出权",
        "expanded_inputs.accepted_current 看到 exact selected version",
        "每个 target 的 basis_invalid 与 refuted 各最多一项",
        "refuted 成为主历史原因",
        "不得因为 ancestor accepted、来源叶、父关系后来退出 current 而自动图遍历或机械级联",
        "new_input_reviews 必须 exact/unique 覆盖 frozen_new_ids",
        "稳定关系至少两个不同正式知识单元",
        "每个 participant_key 必须至少出现在一项必要 premise",
        "递归来源 leaves 最终至少覆盖两个不同 KR",
        "同一 KR 的多个 point 不会自动算成两条独立谱系",
        "required_premises.supported_by 显式证明",
        "精确复制 expanded_inputs 中已有的 (knowledge_result_id, point_id)",
        "不得把 source_fact_id 当作 knowledge_result_id",
        "只能 create_identity",
        "relation_reviews 和 existing relation refs 必须为空",
        "planned_stable 只能引用同 plan new_relations",
        "不得引用 rejected/exploration key",
        "同 plan exact payload duplicate 不得换 local key 重复",
        "只在 rejected_outputs 保留拒绝类别",
        "evolution_basis 是 comparison-only card",
        "canonical premise text→support identity 配对",
        "同 identity 同 semantic 只有 comparison basis 实质变化",
        "只改 participant_key、premise_id、position、contribution_text、role_text",
        "exact latest、非 current、permanent_facts 含 basis_invalid",
        "wrong/replaced relation 永久禁止恢复",
        "该旧 version 仍不是 participant、used relation、current、hint",
        "仅重排 conditions、limitations、connection_reasons、required_premises",
        "相同正式项的重复次数仍保留",
        "topic_change_assessments 只允许评估 label-only diff",
        "exact 同一 existing topic_id、exact member set 相同",
        'topic_ref 唯一格式为 "topic:<id>"',
        "new_topic_key/new topic、existing topic exit、member-set change",
        "必须零 assessment",
        "不存在 label-only diff 时必须 exact []",
        "unknown、duplicate、extra assessment 全部禁止",
    ):
        assert required in prompt
    assert "同一 insight_version_id 最多一项" not in prompt
    current = runtime.calls[0]["input_payload"]["relation_current"][0]
    assert current["expansion_state"] == (
        "compact_impact_and_recall_locator_only"
    )
    assert current["participant_identities"] == [
        {
            "participant_key": "a",
            "input_kind": "source_knowledge",
            "knowledge_result_id": 1,
            "point_id": "p1",
            "accepted_insight_version_id": None,
        },
        {
            "participant_key": "b",
            "input_kind": "source_knowledge",
            "knowledge_result_id": 2,
            "point_id": "p1",
            "accepted_insight_version_id": None,
        },
    ]


def test_builders_return_real_production_adapters(monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_DISTILLER_GROWTH_BASE_URL", "https://example.invalid")
    monkeypatch.setenv("KNOWLEDGE_DISTILLER_GROWTH_MODEL", "model")
    monkeypatch.setenv("KNOWLEDGE_DISTILLER_GROWTH_API_KEY", "secret")

    recall = build_historical_recall_planner()
    relation = build_relation_insight_planner()

    assert isinstance(recall, HistoricalRecallAdapter)
    assert isinstance(relation, RelationInsightAdapter)
    assert recall.is_available()
    assert relation.is_available()
