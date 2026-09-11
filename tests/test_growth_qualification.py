from __future__ import annotations

import copy
from dataclasses import replace

import pytest

from knowledge_distiller.growth_qualification import (
    GrowthQualificationError,
    QualificationContext,
    qualify_growth_plan,
)
from knowledge_distiller.organization_models import (
    AcceptedDisqualification,
    InsightDisqualificationKind,
    InputKind,
    Participant,
    RequiredPremise,
    build_evolution_basis_card,
    parse_insight_payload,
    parse_relation_payload,
    parse_growth_plan,
    semantic_signature,
)
from tests.fixtures.growth import (
    accepted_participant,
    empty_growth_plan_payload,
    healthy_boundary,
    insight_payload,
    relation_payload,
    source_participant,
)


def _current_review(*, action="unchanged", **extra):
    result = {
        "relation_id": 7,
        "relation_version_id": 70,
        "action": action,
        "reason_text": "Completed current relation review",
        "directly_affected": True,
    }
    result.update(extra)
    return result


def _hint_review(*, action="attention", **extra):
    result = {
        "relation_id": 8,
        "relation_version_id": 80,
        "action": action,
        "reason_text": "Reviewed retired relation from scratch",
        "directly_affected": True,
    }
    result.update(extra)
    return result


def _candidate(*, used_relations=()):
    return {
        "new_insight_key": "i-new",
        "target_kind": "create_identity",
        "payload": insight_payload(),
        "participants": [
            source_participant("a", 1, position=0),
            source_participant("b", 2, position=1),
        ],
        "used_relations": list(used_relations),
    }


def _new_relation(*, key="r-new", target_kind="create_identity", **target):
    result = {
        "new_relation_key": key,
        "target_kind": target_kind,
        "payload": relation_payload(),
        "participants": [
            source_participant("a", 1, position=0),
            source_participant("b", 2, position=1),
        ],
        "used_relations": [],
    }
    result.update(target)
    return result


def test_semantic_signature_ignores_local_premise_and_participant_handles():
    original_relation = relation_payload()
    renamed_relation = copy.deepcopy(original_relation)
    renamed_relation["required_premises"] = [
        {
            **premise,
            "premise_id": f"renamed-{index}",
            "supported_by": [f"source_{key}" for key in premise["supported_by"]],
        }
        for index, premise in enumerate(original_relation["required_premises"])
    ]
    original_insight = insight_payload()
    renamed_insight = copy.deepcopy(original_insight)
    renamed_insight["required_premises"] = [
        {
            **premise,
            "premise_id": f"renamed-{index}",
            "supported_by": [f"source_{key}" for key in premise["supported_by"]],
        }
        for index, premise in enumerate(original_insight["required_premises"])
    ]

    assert semantic_signature(parse_relation_payload(original_relation)) == (
        semantic_signature(parse_relation_payload(renamed_relation))
    )
    assert semantic_signature(parse_insight_payload(original_insight)) == (
        semantic_signature(parse_insight_payload(renamed_insight))
    )


def test_semantic_signature_canonicalizes_unordered_fields_without_deduplicating():
    relation = relation_payload()
    relation["conditions"].append("Another condition")
    relation["limitations"].append(
        {"kind": "boundary", "text": "Another limitation"}
    )
    reordered_relation = copy.deepcopy(relation)
    for field in ("conditions", "limitations", "required_premises"):
        reordered_relation[field].reverse()

    insight = insight_payload()
    insight["connection_reasons"].append("A second connection reason")
    insight["limitations"].append(
        {"kind": "condition", "text": "Another insight limitation"}
    )
    reordered_insight = copy.deepcopy(insight)
    for field in ("connection_reasons", "limitations", "required_premises"):
        reordered_insight[field].reverse()

    relation_signature = semantic_signature(parse_relation_payload(relation))
    assert relation_signature == semantic_signature(
        parse_relation_payload(reordered_relation)
    )
    assert semantic_signature(parse_insight_payload(insight)) == semantic_signature(
        parse_insight_payload(reordered_insight)
    )

    duplicated_condition = copy.deepcopy(relation)
    duplicated_condition["conditions"].append(relation["conditions"][0])
    assert relation_signature != semantic_signature(
        parse_relation_payload(duplicated_condition)
    )


def test_evolution_basis_canonicalizes_handles_but_preserves_premise_support_pairing():
    participants = (
        Participant("a", InputKind.SOURCE_KNOWLEDGE, 0, "first", 1, "p1"),
        Participant("b", InputKind.SOURCE_KNOWLEDGE, 1, "second", 2, "p1"),
    )
    premises = (
        RequiredPremise("x", "Premise X", ("a",)),
        RequiredPremise("y", "Premise Y", ("b",)),
    )
    renamed_and_reordered = (
        Participant("source_b", InputKind.SOURCE_KNOWLEDGE, 0, "rewritten", 2, "p1"),
        Participant("source_a", InputKind.SOURCE_KNOWLEDGE, 1, "different", 1, "p1"),
    )
    renamed_premises = (
        RequiredPremise("new-y", "Premise Y", ("source_b",)),
        RequiredPremise("new-x", "Premise X", ("source_a",)),
    )
    swapped_support = (
        RequiredPremise("new-y", "Premise Y", ("source_a",)),
        RequiredPremise("new-x", "Premise X", ("source_b",)),
    )

    first = build_evolution_basis_card(participants, premises, ())
    renamed = build_evolution_basis_card(
        renamed_and_reordered, renamed_premises, ()
    )
    swapped = build_evolution_basis_card(
        renamed_and_reordered, swapped_support, ()
    )

    assert renamed.fingerprint == first.fingerprint
    assert swapped.fingerprint != first.fingerprint


def test_legal_candidate_has_two_independent_participants_and_complete_signatures():
    """E2E-17/26: candidate qualifies on direct formal premises without stable relation."""
    boundary = healthy_boundary(include_hint=False)
    payload = empty_growth_plan_payload()
    payload["candidate_versions"] = [_candidate()]

    qualified = qualify_growth_plan(
        parse_growth_plan(payload),
        QualificationContext(boundary),
    )

    assert qualified.final_required_source_set == tuple(
        sorted(
            point.identity
            for point in boundary.frozen_new[0].points
            + boundary.frozen_new[1].points
        )
    )
    assert qualified.final_required_planned_relation_set == ()
    assert len(qualified.dependency_signature) == 64


def test_candidate_cannot_count_two_points_from_one_source_as_independent_lineages():
    """E2E-26: two top-level participants still need distinguishable source bases."""
    boundary = healthy_boundary(include_hint=False)
    payload = empty_growth_plan_payload()
    candidate = _candidate()
    candidate["participants"] = [
        source_participant("a", 1, position=0, point_id="p1"),
        source_participant("b", 1, position=1, point_id="p2"),
    ]
    candidate["payload"]["required_premises"] = [
        {"premise_id": "p1", "text": "one", "supported_by": ["a"]},
        {"premise_id": "p2", "text": "two", "supported_by": ["b"]},
    ]
    payload["candidate_versions"] = [candidate]
    boundary = boundary.__class__(
        event_id=boundary.event_id,
        frozen_new=(
            boundary.frozen_new[0].__class__(
                **{
                    **boundary.frozen_new[0].__dict__,
                    "points": (
                        boundary.frozen_new[0].points[0],
                        boundary.frozen_new[0].points[0].__class__(
                            1, "p2", "other", "Statement 1/p2", "Argument 1/p2"
                        ),
                    ),
                }
            ),
            boundary.frozen_new[1],
        ),
        eligible_history=boundary.eligible_history,
        accepted_current=boundary.accepted_current,
        current_relations=boundary.current_relations,
        reconsideration_hints=boundary.reconsideration_hints,
    )

    with pytest.raises(GrowthQualificationError, match="formal lineage units"):
        qualify_growth_plan(parse_growth_plan(payload), QualificationContext(boundary))


def test_two_points_from_a_and_one_from_b_form_two_independent_lineage_units():
    boundary = healthy_boundary(include_hint=False)
    source_a = replace(
        boundary.frozen_new[0],
        points=(
            boundary.frozen_new[0].points[0],
            replace(
                boundary.frozen_new[0].points[0],
                point_id="p2",
                role="other",
                statement="A second necessary point from source A",
                argument="The second A point supports a distinct premise",
            ),
        ),
    )
    boundary = replace(
        boundary,
        frozen_new=(source_a, boundary.frozen_new[1]),
    )
    payload = empty_growth_plan_payload()
    candidate = _candidate()
    candidate["participants"] = [
        source_participant("a1", 1, position=0, point_id="p1"),
        source_participant("a2", 1, position=1, point_id="p2"),
        source_participant("b", 2, position=2, point_id="p1"),
    ]
    candidate["payload"]["required_premises"] = [
        {"premise_id": "pa1", "text": "A point one", "supported_by": ["a1"]},
        {"premise_id": "pa2", "text": "A point two", "supported_by": ["a2"]},
        {"premise_id": "pb", "text": "B point one", "supported_by": ["b"]},
    ]
    payload["candidate_versions"] = [candidate]

    qualified = qualify_growth_plan(
        parse_growth_plan(payload), QualificationContext(boundary)
    )

    assert len(qualified.plan.candidate_versions[0].participants) == 3


def test_two_accepted_lineage_units_with_identical_recursive_leaves_are_rejected():
    boundary = healthy_boundary(include_hint=False)
    first = boundary.accepted_current[0]
    second_node = replace(
        first.lineage_nodes[0],
        insight_version_id=60,
        insight_id=6,
        produced_event_id=41,
    )
    second = replace(
        first,
        insight_version_id=60,
        insight_id=6,
        produced_event_id=41,
        lineage_nodes=(second_node,),
        qualification_signature="6" * 64,
    )
    boundary = replace(boundary, accepted_current=(first, second))
    payload = empty_growth_plan_payload()
    candidate = _candidate()
    candidate["participants"] = [
        {
            "participant_key": "a",
            "input_kind": "accepted_insight",
            "accepted_insight_version_id": 50,
            "position": 0,
            "contribution_text": "First accepted lineage",
        },
        {
            "participant_key": "b",
            "input_kind": "accepted_insight",
            "accepted_insight_version_id": 60,
            "position": 1,
            "contribution_text": "Second accepted lineage",
        },
    ]
    payload["candidate_versions"] = [candidate]

    with pytest.raises(GrowthQualificationError, match="no independent source"):
        qualify_growth_plan(
            parse_growth_plan(payload),
            QualificationContext(
                boundary,
                selected_accepted_insight_version_ids=frozenset({50, 60}),
            ),
        )


def test_each_participant_must_support_a_declared_required_premise():
    """E2E-37: recalled/background items cannot be padded into participation."""
    boundary = healthy_boundary(include_hint=False)
    payload = empty_growth_plan_payload()
    candidate = _candidate()
    candidate["payload"]["required_premises"] = [
        {"premise_id": "only-a", "text": "Only A", "supported_by": ["a"]}
    ]
    payload["candidate_versions"] = [candidate]

    with pytest.raises(GrowthQualificationError, match="each participant"):
        qualify_growth_plan(parse_growth_plan(payload), QualificationContext(boundary))


def test_used_current_relation_requires_completed_review_and_post_h_current():
    """E2E-13: a used/affected current relation cannot skip its final review."""
    boundary = healthy_boundary(include_hint=False)
    payload = empty_growth_plan_payload()
    payload["candidate_versions"] = [
        _candidate(
            used_relations=(
                {
                    "ref_kind": "boundary_current",
                    "relation_version_id": 70,
                    "role_text": "Used connection",
                },
            )
        )
    ]

    with pytest.raises(GrowthQualificationError, match="must be reviewed"):
        qualify_growth_plan(parse_growth_plan(payload), QualificationContext(boundary))

    payload["relation_reviews"] = [_current_review(action="basis_invalid")]
    with pytest.raises(GrowthQualificationError, match="exits post-H"):
        qualify_growth_plan(
            parse_growth_plan(payload),
            QualificationContext(
                boundary, selected_current_relation_version_ids=frozenset({70})
            ),
        )


def test_attention_hint_has_no_used_power_until_selected_reviewed_and_activated():
    """E2E-45: hint is recall-only until exact same-event requalification/activation."""
    boundary = healthy_boundary()
    payload = empty_growth_plan_payload()
    payload["candidate_versions"] = [
        _candidate(
            used_relations=(
                {
                    "ref_kind": "requalified_current",
                    "relation_version_id": 80,
                    "role_text": "Restored exact relation",
                },
            )
        )
    ]
    payload["relation_reviews"] = [_hint_review(action="unchanged")]

    with pytest.raises(GrowthQualificationError, match="not been requalified"):
        qualify_growth_plan(
            parse_growth_plan(payload),
            QualificationContext(
                boundary,
                selected_reconsideration_hint_version_ids=frozenset({80}),
            ),
        )

    payload["relation_reviews"] = [
        _hint_review(action="attention", attention_state="activated")
    ]
    qualified = qualify_growth_plan(
        parse_growth_plan(payload),
        QualificationContext(
            boundary,
            selected_reconsideration_hint_version_ids=frozenset({80}),
        ),
    )
    assert qualified.final_reconsideration_hint_set == (80,)
    assert qualified.final_requalified_current_set == (80,)


def test_candidate_can_use_independently_qualified_same_event_stable_relation():
    """E2E-43: planned stable is a used connection, never participant or lineage."""
    boundary = healthy_boundary(include_hint=False)
    payload = empty_growth_plan_payload()
    payload["new_relations"] = [_new_relation()]
    payload["candidate_versions"] = [
        _candidate(
            used_relations=(
                {
                    "ref_kind": "planned_stable",
                    "new_relation_key": "r-new",
                    "role_text": "Used only as connection",
                },
            )
        )
    ]

    qualified = qualify_growth_plan(
        parse_growth_plan(payload),
        QualificationContext(boundary),
    )
    assert qualified.final_required_planned_relation_set == ("r-new",)
    assert all(
        item.participant_key in {"a", "b"}
        for item in qualified.plan.candidate_versions[0].participants
    )


def test_unknown_or_rejected_planned_relation_key_cannot_leave_dangling_candidate():
    """E2E-15/E2E-44: illegal/rejected relation cannot support a candidate local ref."""
    boundary = healthy_boundary(include_hint=False)
    payload = empty_growth_plan_payload()
    payload["candidate_versions"] = [
        _candidate(
            used_relations=(
                {
                    "ref_kind": "planned_stable",
                    "new_relation_key": "missing",
                    "role_text": "Dangling",
                },
            )
        )
    ]

    with pytest.raises(GrowthQualificationError, match="not stable output"):
        qualify_growth_plan(parse_growth_plan(payload), QualificationContext(boundary))


def test_single_knowledge_cannot_create_a_stable_self_relation():
    """E2E-18: two points from one KR remain one formal knowledge unit."""
    boundary = healthy_boundary(include_hint=False)
    first = boundary.frozen_new[0]
    extra_point = first.points[0].__class__(
        1, "p2", "other", "Statement 1/p2", "Argument 1/p2"
    )
    boundary = boundary.__class__(
        event_id=boundary.event_id,
        frozen_new=(
            first.__class__(**{**first.__dict__, "points": (*first.points, extra_point)}),
            boundary.frozen_new[1],
        ),
        eligible_history=boundary.eligible_history,
        accepted_current=boundary.accepted_current,
        current_relations=boundary.current_relations,
        reconsideration_hints=boundary.reconsideration_hints,
    )
    payload = empty_growth_plan_payload()
    relation = _new_relation()
    relation["participants"] = [
        source_participant("a", 1, position=0, point_id="p1"),
        source_participant("b", 1, position=1, point_id="p2"),
    ]
    payload["new_relations"] = [relation]

    with pytest.raises(GrowthQualificationError, match="different formal knowledge"):
        qualify_growth_plan(parse_growth_plan(payload), QualificationContext(boundary))


def test_one_specific_mechanism_can_retain_three_distinct_participants():
    """E2E-19: A/B/C sharing one mechanism form one qualified multi-relation."""
    boundary = healthy_boundary(include_hint=False)
    payload = empty_growth_plan_payload()
    relation = _new_relation()
    relation["participants"].append(source_participant("c", 3, position=2))
    relation["payload"]["required_premises"].append(
        {"premise_id": "rp3", "text": "C supplies the third condition", "supported_by": ["c"]}
    )
    payload["new_relations"] = [relation]

    qualified = qualify_growth_plan(
        parse_growth_plan(payload),
        QualificationContext(
            boundary, selected_source_knowledge_ids=frozenset({3})
        ),
    )

    assert len(qualified.plan.new_relations[0].participants) == 3


@pytest.mark.parametrize(
    ("input_kind", "match"),
    [
        ("historical_source", "source participant is outside boundary"),
        ("accepted_current", "accepted participant is not current boundary"),
    ],
)
def test_unselected_historical_or_accepted_input_has_no_participant_power(
    input_kind, match
):
    boundary = healthy_boundary(include_hint=False)
    payload = empty_growth_plan_payload()
    relation = _new_relation()
    if input_kind == "historical_source":
        relation["participants"][0] = source_participant("a", 3, position=0)
        selected_context = QualificationContext(
            boundary, selected_source_knowledge_ids=frozenset({3})
        )
    else:
        relation["participants"][0] = {
            "participant_key": "a",
            "input_kind": "accepted_insight",
            "accepted_insight_version_id": 50,
            "position": 0,
            "contribution_text": "Accepted current supplies one premise",
        }
        selected_context = QualificationContext(
            boundary,
            selected_accepted_insight_version_ids=frozenset({50}),
        )
    payload["new_relations"] = [relation]
    plan = parse_growth_plan(payload)

    with pytest.raises(GrowthQualificationError, match=match):
        qualify_growth_plan(plan, QualificationContext(boundary))

    assert qualify_growth_plan(plan, selected_context).plan.new_relations


@pytest.mark.parametrize("fact_kind", ["basis_invalid", "refuted"])
def test_exact_recalled_accepted_current_can_be_disqualified(fact_kind):
    boundary = healthy_boundary(include_hint=False)
    payload = empty_growth_plan_payload()
    payload["accepted_disqualifications"] = [
        {
            "insight_version_id": 50,
            "fact_kind": fact_kind,
            "reason_text": "Formal review established this exact exit cause",
        }
    ]

    qualified = qualify_growth_plan(
        parse_growth_plan(payload),
        QualificationContext(
            boundary,
            selected_accepted_insight_version_ids=frozenset({50}),
        ),
    )

    assert qualified.plan.accepted_disqualifications[0].fact_kind.value == fact_kind


@pytest.mark.parametrize(
    ("target_id", "selected_ids"),
    [
        (50, frozenset()),
        (999, frozenset({999})),
    ],
)
def test_accepted_disqualification_requires_exact_recalled_current_boundary(
    target_id, selected_ids
):
    payload = empty_growth_plan_payload()
    payload["accepted_disqualifications"] = [
        {
            "insight_version_id": target_id,
            "fact_kind": "basis_invalid",
            "reason_text": "Not an exact expanded current target",
        }
    ]

    with pytest.raises(
        GrowthQualificationError,
        match="not exact recalled current input",
    ):
        qualify_growth_plan(
            parse_growth_plan(payload),
            QualificationContext(
                healthy_boundary(include_hint=False),
                selected_accepted_insight_version_ids=selected_ids,
            ),
        )


@pytest.mark.parametrize("catalog_state", ["pending", "rethink", "historical"])
def test_noncurrent_catalog_state_cannot_be_disqualification_target(catalog_state):
    boundary = replace(
        healthy_boundary(include_hint=False), accepted_current=()
    )
    payload = empty_growth_plan_payload()
    payload["accepted_disqualifications"] = [
        {
            "insight_version_id": 50,
            "fact_kind": "refuted",
            "reason_text": f"Catalog-only {catalog_state} has no action power",
        }
    ]

    with pytest.raises(GrowthQualificationError):
        qualify_growth_plan(
            parse_growth_plan(payload),
            QualificationContext(
                boundary,
                selected_accepted_insight_version_ids=frozenset({50}),
            ),
        )


@pytest.mark.parametrize("output_kind", ["relation", "candidate"])
def test_same_plan_cannot_use_and_disqualify_exact_accepted_version(output_kind):
    payload = empty_growth_plan_payload()
    output = _new_relation() if output_kind == "relation" else _candidate()
    output["participants"][0] = accepted_participant("a", 50, position=0)
    payload[
        "new_relations" if output_kind == "relation" else "candidate_versions"
    ] = [output]
    payload["accepted_disqualifications"] = [
        {
            "insight_version_id": 50,
            "fact_kind": "basis_invalid",
            "reason_text": "The exact input cannot remain usable",
        }
    ]

    with pytest.raises(GrowthQualificationError, match="cannot use"):
        qualify_growth_plan(
            parse_growth_plan(payload),
            QualificationContext(
                healthy_boundary(include_hint=False),
                selected_accepted_insight_version_ids=frozenset({50}),
            ),
        )


def test_same_plan_cannot_replace_and_disqualify_same_accepted_identity():
    payload = empty_growth_plan_payload()
    candidate = _candidate()
    candidate["replaces_insight_id"] = 5
    payload["candidate_versions"] = [candidate]
    payload["accepted_disqualifications"] = [
        {
            "insight_version_id": 50,
            "fact_kind": "refuted",
            "reason_text": "A single final exit cause is required",
        }
    ]

    with pytest.raises(GrowthQualificationError, match="replaced and disqualified"):
        qualify_growth_plan(
            parse_growth_plan(payload),
            QualificationContext(
                healthy_boundary(include_hint=False),
                selected_accepted_insight_version_ids=frozenset({50}),
            ),
        )


def test_disqualified_insight_identity_can_later_be_replaced():
    payload = empty_growth_plan_payload()
    candidate = _candidate()
    candidate["replaces_insight_id"] = 5
    payload["candidate_versions"] = [candidate]

    qualified = qualify_growth_plan(
        parse_growth_plan(payload),
        QualificationContext(
            healthy_boundary(include_hint=False),
            selected_accepted_insight_version_ids=frozenset({50}),
            latest_insight_versions={5: 50},
        ),
    )

    assert qualified.plan.candidate_versions[0].replaces_insight_id == 5


@pytest.mark.parametrize(
    ("output_kind", "ref_kind"),
    [
        ("relation", "boundary_current"),
        ("candidate", "boundary_current"),
        ("relation", "requalified_current"),
        ("candidate", "requalified_current"),
    ],
)
def test_used_relation_cannot_directly_depend_on_disqualified_accepted(
    output_kind, ref_kind
):
    boundary = healthy_boundary(include_hint=True)
    if ref_kind == "boundary_current":
        original = boundary.current_relations[0]
        relation_version_id = original.relation_version_id
        boundary = replace(
            boundary,
            current_relations=(
                replace(
                    original,
                    participants=(
                        Participant(
                            "accepted",
                            InputKind.ACCEPTED_INSIGHT,
                            0,
                            "Accepted input is a direct relation participant",
                            accepted_insight_version_id=50,
                        ),
                        original.participants[1],
                    ),
                ),
            ),
        )
        review = _current_review()
        selected_current = frozenset({relation_version_id})
        selected_hints = frozenset()
    else:
        original = boundary.reconsideration_hints[0]
        relation_version_id = original.relation_version_id
        boundary = replace(
            boundary,
            reconsideration_hints=(
                replace(
                    original,
                    participants=(
                        Participant(
                            "accepted",
                            InputKind.ACCEPTED_INSIGHT,
                            0,
                            "Accepted input is a direct relation participant",
                            accepted_insight_version_id=50,
                        ),
                        original.participants[1],
                    ),
                ),
            ),
        )
        review = _hint_review(attention_state="activated")
        selected_current = frozenset()
        selected_hints = frozenset({relation_version_id})

    payload = empty_growth_plan_payload()
    payload["relation_reviews"] = [review]
    output = _new_relation() if output_kind == "relation" else _candidate()
    output["used_relations"] = [
        {
            "ref_kind": ref_kind,
            "relation_version_id": relation_version_id,
            "role_text": "This direct dependency cannot survive the action",
        }
    ]
    payload[
        "new_relations" if output_kind == "relation" else "candidate_versions"
    ] = [output]
    payload["accepted_disqualifications"] = [
        {
            "insight_version_id": 50,
            "fact_kind": "basis_invalid",
            "reason_text": "The accepted direct basis no longer qualifies",
        }
    ]

    with pytest.raises(GrowthQualificationError, match="directly depends"):
        qualify_growth_plan(
            parse_growth_plan(payload),
            QualificationContext(
                boundary,
                selected_accepted_insight_version_ids=frozenset({50}),
                selected_current_relation_version_ids=selected_current,
                selected_reconsideration_hint_version_ids=selected_hints,
            ),
        )


def test_qualification_rejects_duplicate_disqualification_when_codec_is_bypassed():
    plan = parse_growth_plan(empty_growth_plan_payload())
    duplicate = replace(
        plan,
        accepted_disqualifications=(
            AcceptedDisqualification(
                50,
                InsightDisqualificationKind.BASIS_INVALID,
                "First final action",
            ),
            AcceptedDisqualification(
                50,
                InsightDisqualificationKind.BASIS_INVALID,
                "Second final action",
            ),
        ),
    )

    with pytest.raises(GrowthQualificationError, match="repeats a final"):
        qualify_growth_plan(
            duplicate,
            QualificationContext(
                healthy_boundary(include_hint=False),
                selected_accepted_insight_version_ids=frozenset({50}),
            ),
        )


def test_qualification_accepts_two_distinct_disqualification_kinds_for_one_target():
    payload = empty_growth_plan_payload()
    payload["accepted_disqualifications"] = [
        {
            "insight_version_id": 50,
            "fact_kind": fact_kind,
            "reason_text": f"Formal review established {fact_kind}",
        }
        for fact_kind in ("basis_invalid", "refuted")
    ]

    qualified = qualify_growth_plan(
        parse_growth_plan(payload),
        QualificationContext(
            healthy_boundary(include_hint=False),
            selected_accepted_insight_version_ids=frozenset({50}),
        ),
    )

    assert len(qualified.plan.accepted_disqualifications) == 2


def test_mixed_or_unstable_mechanism_can_finish_without_a_formal_relation():
    """E2E-14/E2E-20: exploration is rejection metadata, never a relation."""
    boundary = healthy_boundary(include_hint=False)
    payload = empty_growth_plan_payload()
    payload["rejected_outputs"] = [
        {
            "output_kind": "exploration_only",
            "related_ids": [1, 2, 3],
            "reason_code": "mixed_or_not_stable",
        }
    ]

    qualified = qualify_growth_plan(
        parse_growth_plan(payload), QualificationContext(boundary)
    )

    assert qualified.plan.new_relations == ()
    assert qualified.plan.rejected_outputs[0].output_kind == "exploration_only"


def test_evolved_relation_is_single_same_identity_direct_successor():
    """E2E-25: evolved review and new version form one exact direct successor."""
    boundary = healthy_boundary(include_hint=False)
    payload = empty_growth_plan_payload()
    payload["relation_reviews"] = [
        _current_review(action="evolved", successor_key="r-next")
    ]
    payload["new_relations"] = [
        _new_relation(
            key="r-next",
            target_kind="evolve_identity",
            relation_id=7,
            previous_relation_version_id=70,
        )
    ]

    qualified = qualify_growth_plan(
        parse_growth_plan(payload),
        QualificationContext(
            boundary,
            selected_current_relation_version_ids=frozenset({70}),
            latest_relation_versions={7: 70},
        ),
    )
    assert qualified.plan.new_relations[0].relation_id == 7

    payload["new_relations"][0]["previous_relation_version_id"] = 69
    with pytest.raises(GrowthQualificationError, match="exact reviewed successor"):
        qualify_growth_plan(
            parse_growth_plan(payload),
            QualificationContext(
                boundary,
                selected_current_relation_version_ids=frozenset({70}),
                latest_relation_versions={7: 70},
            ),
        )


def test_wrong_and_replaced_requires_distinct_qualified_new_identity():
    """E2E-24: wrong and replaced stay two axes with one replacement target."""
    boundary = healthy_boundary(include_hint=False)
    payload = empty_growth_plan_payload()
    payload["relation_reviews"] = [
        _current_review(
            action="wrong_and_replaced", replacement_new_relation_key="r-replacement"
        )
    ]
    payload["new_relations"] = [_new_relation(key="r-replacement")]

    qualified = qualify_growth_plan(
        parse_growth_plan(payload),
        QualificationContext(
            boundary,
            selected_current_relation_version_ids=frozenset({70}),
        ),
    )
    assert qualified.plan.relation_reviews[0].action.value == "wrong_and_replaced"

    payload["new_relations"][0].update(
        target_kind="evolve_identity", relation_id=7, previous_relation_version_id=70
    )
    with pytest.raises(
        GrowthQualificationError,
        match="exact reviewed successor|new qualified identity",
    ):
        qualify_growth_plan(
            parse_growth_plan(payload),
            QualificationContext(
                boundary,
                selected_current_relation_version_ids=frozenset({70}),
            ),
        )


def test_exact_semantic_duplicate_cannot_create_relation_or_candidate_version():
    boundary = healthy_boundary(include_hint=False)
    payload = empty_growth_plan_payload()
    payload["new_relations"] = [
        _new_relation(
            key="r-next",
            target_kind="evolve_identity",
            relation_id=7,
            previous_relation_version_id=70,
        )
    ]
    payload["relation_reviews"] = [
        _current_review(action="evolved", successor_key="r-next")
    ]
    plan = parse_growth_plan(payload)
    signature = semantic_signature(plan.new_relations[0].payload)

    with pytest.raises(GrowthQualificationError, match="changed formal basis"):
        qualify_growth_plan(
            plan,
            QualificationContext(
                boundary,
                selected_current_relation_version_ids=frozenset({70}),
                known_relation_semantic_signatures={7: signature},
                latest_relation_versions={7: 70},
            ),
        )


def test_same_semantic_relation_can_evolve_only_with_a_new_formal_basis():
    boundary = healthy_boundary(include_hint=False)
    original_payload = empty_growth_plan_payload()
    original_payload["new_relations"] = [
        _new_relation(
            key="r-next",
            target_kind="evolve_identity",
            relation_id=7,
            previous_relation_version_id=70,
        )
    ]
    original_payload["relation_reviews"] = [
        _current_review(action="evolved", successor_key="r-next")
    ]
    original_plan = parse_growth_plan(original_payload)
    original_relation = original_plan.new_relations[0]
    signature = semantic_signature(original_relation.payload)
    original_basis = build_evolution_basis_card(
        original_relation.participants,
        original_relation.payload.required_premises,
        (),
    )
    context = QualificationContext(
        boundary,
        selected_source_knowledge_ids=frozenset({3}),
        selected_current_relation_version_ids=frozenset({70}),
        known_relation_semantic_signatures={7: signature},
        latest_relation_versions={7: 70},
        latest_relation_evolution_bases={7: original_basis},
        relation_evolution_history_by_semantic_signature={
            signature: ((7, original_basis.fingerprint),)
        },
        relation_identity_by_semantic_signature={signature: 7},
    )

    with pytest.raises(GrowthQualificationError, match="changed formal basis"):
        qualify_growth_plan(original_plan, context)

    changed_payload = copy.deepcopy(original_payload)
    changed_payload["new_relations"][0]["participants"][1] = source_participant(
        "b", 3, position=1
    )
    qualified = qualify_growth_plan(parse_growth_plan(changed_payload), context)

    assert qualified.plan.new_relations[0].relation_id == 7


def test_same_semantic_candidate_rethink_requires_new_formal_basis_not_wording():
    boundary = healthy_boundary(include_hint=False)
    payload = empty_growth_plan_payload()
    candidate = _candidate()
    candidate.update(
        target_kind="evolve_identity",
        insight_id=9,
        previous_insight_version_id=90,
    )
    payload["candidate_versions"] = [candidate]
    plan = parse_growth_plan(payload)
    planned = plan.candidate_versions[0]
    signature = semantic_signature(planned.payload)
    original_basis = build_evolution_basis_card(
        planned.participants,
        planned.payload.required_premises,
        (),
    )
    context = QualificationContext(
        boundary,
        selected_source_knowledge_ids=frozenset({3}),
        known_insight_semantic_signatures={9: signature},
        rethink_insight_semantic_signatures={9: signature},
        latest_insight_versions={9: 90},
        latest_insight_evolution_bases={9: original_basis},
        insight_evolution_history_by_semantic_signature={
            signature: ((9, original_basis.fingerprint),)
        },
        insight_identity_by_semantic_signature={signature: 9},
    )

    wording_only = copy.deepcopy(payload)
    wording_only["candidate_versions"][0]["participants"][0][
        "contribution_text"
    ] = "A newly worded explanation of the exact same contribution"
    with pytest.raises(GrowthQualificationError) as captured:
        qualify_growth_plan(parse_growth_plan(wording_only), context)
    assert captured.value.code == "rethink_unchanged"

    changed_basis = copy.deepcopy(payload)
    changed_basis["candidate_versions"][0]["participants"][1] = (
        source_participant("b", 3, position=1)
    )
    qualified = qualify_growth_plan(parse_growth_plan(changed_basis), context)

    assert qualified.plan.candidate_versions[0].insight_id == 9


def test_same_semantic_candidate_can_evolve_for_exact_accepted_version_change():
    boundary = healthy_boundary(include_hint=False)
    payload = empty_growth_plan_payload()
    candidate = _candidate()
    candidate.update(
        target_kind="evolve_identity",
        insight_id=9,
        previous_insight_version_id=90,
        participants=[
            accepted_participant("a", 50, position=0),
            source_participant("b", 1, position=1),
        ],
    )
    payload["candidate_versions"] = [candidate]
    plan = parse_growth_plan(payload)
    planned = plan.candidate_versions[0]
    signature = semantic_signature(planned.payload)
    previous_basis = build_evolution_basis_card(
        (
            Participant(
                "a",
                InputKind.ACCEPTED_INSIGHT,
                0,
                "Historical accepted input",
                accepted_insight_version_id=49,
            ),
            Participant(
                "b",
                InputKind.SOURCE_KNOWLEDGE,
                1,
                "Same source point",
                knowledge_result_id=1,
                point_id="p1",
            ),
        ),
        planned.payload.required_premises,
        (),
    )

    qualified = qualify_growth_plan(
        plan,
        QualificationContext(
            boundary,
            selected_accepted_insight_version_ids=frozenset({50}),
            known_insight_semantic_signatures={9: signature},
            latest_insight_versions={9: 90},
            latest_insight_evolution_bases={9: previous_basis},
            insight_evolution_history_by_semantic_signature={
                signature: ((9, previous_basis.fingerprint),)
            },
            insight_identity_by_semantic_signature={signature: 9},
        ),
    )

    assert qualified.plan.candidate_versions[0].insight_id == 9


def test_same_semantic_candidate_can_evolve_for_exact_dependency_change():
    boundary = healthy_boundary(include_hint=True)
    payload = empty_growth_plan_payload()
    payload["relation_reviews"] = [
        _hint_review(action="attention", attention_state="activated")
    ]
    candidate = _candidate(
        used_relations=(
            {
                "ref_kind": "requalified_current",
                "relation_version_id": 80,
                "role_text": "New exact dependency",
            },
        )
    )
    candidate.update(
        target_kind="evolve_identity",
        insight_id=9,
        previous_insight_version_id=90,
    )
    payload["candidate_versions"] = [candidate]
    plan = parse_growth_plan(payload)
    planned = plan.candidate_versions[0]
    signature = semantic_signature(planned.payload)
    previous_basis = build_evolution_basis_card(
        planned.participants,
        planned.payload.required_premises,
        ("relation_version:70",),
    )

    qualified = qualify_growth_plan(
        plan,
        QualificationContext(
            boundary,
            selected_reconsideration_hint_version_ids=frozenset({80}),
            known_insight_semantic_signatures={9: signature},
            latest_insight_versions={9: 90},
            latest_insight_evolution_bases={9: previous_basis},
            insight_evolution_history_by_semantic_signature={
                signature: ((9, previous_basis.fingerprint),)
            },
            insight_identity_by_semantic_signature={signature: 9},
        ),
    )

    assert qualified.final_requalified_current_set == (80,)


def test_local_handle_renaming_cannot_evade_create_identity_semantic_duplicate():
    boundary = healthy_boundary(include_hint=False)
    first = _new_relation()
    first_plan = parse_growth_plan(
        {**empty_growth_plan_payload(), "new_relations": [first]}
    )
    signature = semantic_signature(first_plan.new_relations[0].payload)
    renamed = copy.deepcopy(first)
    renamed["participants"] = [
        source_participant("source_a", 1, position=0),
        source_participant("source_b", 2, position=1),
    ]
    for index, premise in enumerate(renamed["payload"]["required_premises"]):
        premise["premise_id"] = f"renamed-{index}"
        premise["supported_by"] = [
            "source_a" if key == "a" else "source_b"
            for key in premise["supported_by"]
        ]

    with pytest.raises(GrowthQualificationError) as captured:
        qualify_growth_plan(
            parse_growth_plan(
                {**empty_growth_plan_payload(), "new_relations": [renamed]}
            ),
            QualificationContext(
                boundary,
                relation_identity_by_semantic_signature={signature: 99},
            ),
        )

    assert captured.value.code == "duplicate_relation"


def test_create_identity_rejects_exact_semantic_from_any_historical_identity():
    boundary = healthy_boundary(include_hint=False)
    relation_plan = empty_growth_plan_payload()
    relation_plan["new_relations"] = [_new_relation()]
    parsed_relation = parse_growth_plan(relation_plan)
    relation_signature = semantic_signature(parsed_relation.new_relations[0].payload)
    with pytest.raises(GrowthQualificationError, match="exact relation payload"):
        qualify_growth_plan(
            parsed_relation,
            QualificationContext(
                boundary,
                relation_identity_by_semantic_signature={relation_signature: 999},
            ),
        )


@pytest.mark.parametrize("output_kind", ["relation", "candidate"])
def test_one_plan_rejects_two_local_targets_with_the_same_semantic(output_kind):
    boundary = healthy_boundary(include_hint=False)
    payload = empty_growth_plan_payload()
    if output_kind == "relation":
        first = _new_relation()
        second = copy.deepcopy(first)
        second["new_relation_key"] = "r-duplicate"
        payload["new_relations"] = [first, second]
    else:
        first = _candidate()
        second = copy.deepcopy(first)
        second["new_insight_key"] = "i-duplicate"
        payload["candidate_versions"] = [first, second]

    with pytest.raises(GrowthQualificationError, match="cannot repeat an exact"):
        qualify_growth_plan(
            parse_growth_plan(payload), QualificationContext(boundary)
        )

    candidate_plan = empty_growth_plan_payload()
    candidate_plan["candidate_versions"] = [_candidate()]
    parsed_candidate = parse_growth_plan(candidate_plan)
    candidate_signature = semantic_signature(
        parsed_candidate.candidate_versions[0].payload
    )
    with pytest.raises(GrowthQualificationError, match="exact insight payload"):
        qualify_growth_plan(
            parsed_candidate,
            QualificationContext(
                boundary,
                insight_identity_by_semantic_signature={candidate_signature: 1000},
            ),
        )


def test_evolved_candidate_must_name_exact_latest_predecessor():
    boundary = healthy_boundary(include_hint=False)
    payload = empty_growth_plan_payload()
    candidate = _candidate()
    candidate.update(
        target_kind="evolve_identity",
        insight_id=5,
        previous_insight_version_id=49,
    )
    payload["candidate_versions"] = [candidate]

    with pytest.raises(GrowthQualificationError, match="exact latest version"):
        qualify_growth_plan(
            parse_growth_plan(payload),
            QualificationContext(
                boundary,
                latest_insight_versions={5: 50},
            ),
        )


@pytest.mark.parametrize(
    ("collision_kind", "match"),
    [
        ("evolve", "one version per insight identity"),
        ("replace", "one replacement"),
    ],
)
def test_one_plan_rejects_multiple_versions_or_replacements_for_one_insight_identity(
    collision_kind, match
):
    boundary = healthy_boundary(include_hint=False)
    payload = empty_growth_plan_payload()
    first = _candidate()
    second = copy.deepcopy(first)
    second["new_insight_key"] = "i-second"
    second["payload"]["claim"] = "A distinct second semantic candidate"
    if collision_kind == "evolve":
        for candidate in (first, second):
            candidate.update(
                target_kind="evolve_identity",
                insight_id=5,
                previous_insight_version_id=50,
            )
    else:
        for candidate in (first, second):
            candidate["replaces_insight_id"] = 5
    payload["candidate_versions"] = [first, second]

    with pytest.raises(GrowthQualificationError, match=match):
        qualify_growth_plan(
            parse_growth_plan(payload),
            QualificationContext(
                boundary,
                selected_accepted_insight_version_ids=frozenset({50}),
                latest_insight_versions={5: 50},
            ),
        )


@pytest.mark.parametrize(
    ("action", "match"),
    [
        ("evolve", "permanently replaced"),
        ("replace_again", "cannot be replaced twice"),
    ],
)
def test_permanently_replaced_insight_cannot_evolve_or_be_replaced_again(
    action, match
):
    boundary = healthy_boundary(include_hint=False)
    payload = empty_growth_plan_payload()
    candidate = _candidate()
    if action == "evolve":
        candidate.update(
            target_kind="evolve_identity",
            insight_id=5,
            previous_insight_version_id=50,
        )
    else:
        candidate["replaces_insight_id"] = 5
    payload["candidate_versions"] = [candidate]

    with pytest.raises(GrowthQualificationError, match=match):
        qualify_growth_plan(
            parse_growth_plan(payload),
            QualificationContext(
                boundary,
                selected_accepted_insight_version_ids=frozenset({50}),
                latest_insight_versions={5: 50},
                replaced_insight_identities={5: 6},
            ),
        )


def test_compact_current_cannot_be_formally_reviewed_without_recall_expansion():
    boundary = healthy_boundary(include_hint=False)
    payload = empty_growth_plan_payload()
    payload["relation_reviews"] = [_current_review()]

    with pytest.raises(GrowthQualificationError, match="recall-expanded"):
        qualify_growth_plan(
            parse_growth_plan(payload), QualificationContext(boundary)
        )


def test_current_relation_graph_signature_is_part_of_dependency_signature():
    boundary = healthy_boundary(include_hint=False)
    payload = empty_growth_plan_payload()
    payload["relation_reviews"] = [_current_review()]
    payload["candidate_versions"] = [
        _candidate(
            used_relations=(
                {
                    "ref_kind": "boundary_current",
                    "relation_version_id": 70,
                    "role_text": "Uses the fully expanded current graph",
                },
            )
        )
    ]
    plan = parse_growth_plan(payload)
    context = QualificationContext(
        boundary, selected_current_relation_version_ids=frozenset({70})
    )
    original = qualify_growth_plan(plan, context)
    drifted_boundary = replace(
        boundary,
        current_relations=(
            replace(
                boundary.current_relations[0],
                qualification_signature="f" * 64,
            ),
        ),
    )
    drifted = qualify_growth_plan(
        plan,
        replace(context, boundary=drifted_boundary),
    )

    assert drifted.dependency_signature != original.dependency_signature


def test_one_plan_cannot_both_evolve_and_replace_the_same_insight_identity():
    boundary = healthy_boundary(include_hint=False)
    payload = empty_growth_plan_payload()
    evolved = _candidate()
    evolved.update(
        target_kind="evolve_identity",
        insight_id=5,
        previous_insight_version_id=50,
    )
    replacement = copy.deepcopy(_candidate())
    replacement["new_insight_key"] = "i-replacement"
    replacement["payload"]["claim"] = "A distinct claim replaces the old core"
    replacement["replaces_insight_id"] = 5
    payload["candidate_versions"] = [evolved, replacement]

    with pytest.raises(GrowthQualificationError, match="both evolve and replace"):
        qualify_growth_plan(
            parse_growth_plan(payload),
            QualificationContext(
                boundary,
                selected_accepted_insight_version_ids=frozenset({50}),
                latest_insight_versions={5: 50},
            ),
        )


def _accepted_dependent_boundary():
    boundary = healthy_boundary(include_hint=False)
    dependent = replace(
        boundary.current_relations[0],
        participants=(
            Participant(
                "a",
                InputKind.ACCEPTED_INSIGHT,
                0,
                "The accepted claim supplies the first premise",
                accepted_insight_version_id=50,
            ),
            Participant(
                "b",
                InputKind.SOURCE_KNOWLEDGE,
                1,
                "The source supplies the second premise",
                knowledge_result_id=1,
                point_id="p1",
            ),
        ),
    )
    return replace(boundary, current_relations=(dependent,))


def _replacement_candidate():
    candidate = _candidate()
    candidate["new_insight_key"] = "i-replacement"
    candidate["payload"]["claim"] = "A new core replaces accepted identity five"
    candidate["replaces_insight_id"] = 5
    return candidate


def test_replacing_accepted_identity_requires_all_dependent_relations_to_exit():
    boundary = _accepted_dependent_boundary()
    payload = empty_growth_plan_payload()
    payload["candidate_versions"] = [_replacement_candidate()]

    with pytest.raises(
        GrowthQualificationError, match="must permanently exit"
    ) as error:
        qualify_growth_plan(
            parse_growth_plan(payload),
            QualificationContext(
                boundary,
                selected_accepted_insight_version_ids=frozenset({50}),
                latest_insight_versions={5: 50},
            ),
        )
    assert error.value.code == "replaced_accepted_relation_impact"

    payload["relation_reviews"] = [_current_review(action="basis_invalid")]
    qualified = qualify_growth_plan(
        parse_growth_plan(payload),
        QualificationContext(
            boundary,
            selected_accepted_insight_version_ids=frozenset({50}),
            selected_current_relation_version_ids=frozenset({70}),
            latest_insight_versions={5: 50},
        ),
    )
    assert qualified.plan.relation_reviews[0].action.value == "basis_invalid"


def test_accepted_current_replacement_requires_exact_recall_selected_version():
    boundary = healthy_boundary(include_hint=False)
    payload = empty_growth_plan_payload()
    payload["candidate_versions"] = [_replacement_candidate()]

    with pytest.raises(
        GrowthQualificationError, match="exact recalled version"
    ) as error:
        qualify_growth_plan(
            parse_growth_plan(payload),
            QualificationContext(
                boundary, latest_insight_versions={5: 50}
            ),
        )
    assert error.value.code == "unexpanded_accepted_replacement"

    qualified = qualify_growth_plan(
        parse_growth_plan(payload),
        QualificationContext(
            boundary,
            selected_accepted_insight_version_ids=frozenset({50}),
            latest_insight_versions={5: 50},
        ),
    )
    assert qualified.plan.candidate_versions[0].replaces_insight_id == 5


@pytest.mark.parametrize("output_kind", ["relation", "candidate"])
def test_same_event_output_cannot_use_accepted_identity_being_replaced(output_kind):
    boundary = healthy_boundary(include_hint=False)
    payload = empty_growth_plan_payload()
    replacement = _replacement_candidate()
    output = _new_relation() if output_kind == "relation" else replacement
    output["participants"] = [
        {
            "participant_key": "a",
            "input_kind": "accepted_insight",
            "accepted_insight_version_id": 50,
            "position": 0,
            "contribution_text": "Uses the accepted identity being replaced",
        },
        source_participant("b", 1, position=1),
    ]
    if output_kind == "relation":
        payload["new_relations"] = [output]
        payload["candidate_versions"] = [replacement]
    else:
        payload["candidate_versions"] = [output]

    with pytest.raises(GrowthQualificationError, match="being replaced"):
        qualify_growth_plan(
            parse_growth_plan(payload),
            QualificationContext(
                boundary,
                selected_accepted_insight_version_ids=frozenset({50}),
                latest_insight_versions={5: 50},
            ),
        )
