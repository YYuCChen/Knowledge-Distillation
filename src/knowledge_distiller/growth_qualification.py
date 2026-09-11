from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from .organization_models import (
    AcceptedInsightInput,
    CandidateVersionPlan,
    EvolutionBasisCard,
    GrowthBoundary,
    GrowthPlan,
    InputKind,
    NewRelationPlan,
    Participant,
    QualifiedGrowthPlan,
    RelationAction,
    RelationBoundaryInput,
    RelationRefKind,
    RelationReview,
    SourcePointIdentity,
    build_evolution_basis_card,
    canonical_json,
    insight_payload_to_dict,
    relation_payload_to_dict,
    semantic_signature,
    sha256_json,
)


class GrowthQualificationError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class QualificationContext:
    boundary: GrowthBoundary
    selected_source_knowledge_ids: frozenset[int] = frozenset()
    selected_accepted_insight_version_ids: frozenset[int] = frozenset()
    selected_current_relation_version_ids: frozenset[int] = frozenset()
    selected_reconsideration_hint_version_ids: frozenset[int] = frozenset()
    known_relation_semantic_signatures: Mapping[int, str] = field(default_factory=dict)
    known_insight_semantic_signatures: Mapping[int, str] = field(default_factory=dict)
    rethink_insight_semantic_signatures: Mapping[int, str] = field(default_factory=dict)
    latest_relation_versions: Mapping[int, int] = field(default_factory=dict)
    latest_insight_versions: Mapping[int, int] = field(default_factory=dict)
    latest_relation_evolution_bases: Mapping[int, EvolutionBasisCard] = field(
        default_factory=dict
    )
    latest_insight_evolution_bases: Mapping[int, EvolutionBasisCard] = field(
        default_factory=dict
    )
    relation_evolution_history_by_semantic_signature: Mapping[
        str, tuple[tuple[int, str], ...]
    ] = field(default_factory=dict)
    insight_evolution_history_by_semantic_signature: Mapping[
        str, tuple[tuple[int, str], ...]
    ] = field(default_factory=dict)
    relation_identity_by_semantic_signature: Mapping[str, int] = field(
        default_factory=dict
    )
    insight_identity_by_semantic_signature: Mapping[str, int] = field(
        default_factory=dict
    )
    replaced_insight_identities: Mapping[int, int] = field(default_factory=dict)
    catalog_basis_invalid_relation_versions: Mapping[int, int] = field(
        default_factory=dict
    )
    identity_catalog_signature: str = ""


def qualify_growth_plan(
    plan: GrowthPlan,
    context: QualificationContext,
) -> QualifiedGrowthPlan:
    boundary = context.boundary
    _qualify_new_input_reviews(plan, boundary)

    current_by_version = {
        item.relation_version_id: item for item in boundary.current_relations
    }
    hint_by_version = {
        item.relation_version_id: item for item in boundary.reconsideration_hints
    }
    relation_by_id = {
        item.relation_id: item
        for item in (*boundary.current_relations, *boundary.reconsideration_hints)
    }
    reviews_by_id = _qualify_relation_reviews(
        plan,
        relation_by_id,
        context.catalog_basis_invalid_relation_versions,
        context.selected_current_relation_version_ids,
        context.selected_reconsideration_hint_version_ids,
        current_by_version,
        hint_by_version,
    )

    allowed_source_points = {
        point.identity
        for source in (
            *boundary.frozen_new,
            *(
                item
                for item in boundary.eligible_history
                if item.knowledge_result_id
                in context.selected_source_knowledge_ids
            ),
        )
        for point in source.points
    }
    accepted_by_version = {
        item.insight_version_id: item
        for item in boundary.accepted_current
        if item.insight_version_id
        in context.selected_accepted_insight_version_ids
    }
    planned_disqualified_identities = {
        accepted_by_version[action.insight_version_id].insight_id
        for action in plan.accepted_disqualifications
        if action.insight_version_id in accepted_by_version
    }
    relation_by_key = {item.new_relation_key: item for item in plan.new_relations}
    relation_semantic: dict[str, str] = {}
    relation_evolution_basis: dict[str, EvolutionBasisCard] = {}
    relation_target_by_semantic: dict[str, str] = {}
    qualification_signatures: dict[str, str] = {}

    _qualify_relation_targets(plan, reviews_by_id, relation_by_id, context)
    _qualify_replaced_accepted_impact(
        plan,
        boundary,
        reviews_by_id,
        context,
    )
    for relation in plan.new_relations:
        _qualify_participants(
            relation.participants,
            relation.payload.required_premises,
            allowed_source_points,
            accepted_by_version,
            minimum=2,
        )
        formal_units = {
            (
                participant.input_kind.value,
                participant.knowledge_result_id
                if participant.input_kind is InputKind.SOURCE_KNOWLEDGE
                else participant.accepted_insight_version_id,
            )
            for participant in relation.participants
        }
        if len(formal_units) < 2:
            _fail(
                "self_relation",
                "a stable relation needs two different formal knowledge units",
            )
        _qualify_relation_refs(
            relation.used_relations,
            reviews_by_id,
            current_by_version,
            hint_by_version,
            relation_by_key,
            allow_planned=False,
        )
        signature = semantic_signature(relation.payload)
        if signature in relation_target_by_semantic:
            _fail(
                "duplicate_relation",
                "one final plan cannot repeat an exact relation payload",
            )
        basis = build_evolution_basis_card(
            relation.participants,
            relation.payload.required_premises,
            _used_relation_dependencies(relation.used_relations, {}, {}),
        )
        history = _evolution_history(
            signature,
            context.relation_evolution_history_by_semantic_signature,
            context.relation_identity_by_semantic_signature,
            context.latest_relation_evolution_bases,
        )
        if relation.target_kind == "create_identity":
            if history:
                _fail(
                    "duplicate_relation",
                    "an exact relation payload cannot create another identity",
                )
        else:
            assert relation.relation_id is not None
            if any(identity != relation.relation_id for identity, _ in history):
                _fail(
                    "duplicate_relation",
                    "an exact relation payload belongs to another identity",
                )
            if any(
                fingerprint is None or fingerprint == basis.fingerprint
                for identity, fingerprint in history
                if identity == relation.relation_id
            ) or (
                not history
                and context.known_relation_semantic_signatures.get(
                    relation.relation_id
                )
                == signature
            ):
                _fail(
                    "duplicate_relation",
                    "same-semantic relation evolution needs a changed formal basis",
                )
        relation_semantic[relation.new_relation_key] = signature
        relation_evolution_basis[relation.new_relation_key] = basis
        relation_target_by_semantic[signature] = relation.new_relation_key
        qualification_signatures[f"relation:{relation.new_relation_key}"] = (
            _qualification_signature(relation.participants, relation.payload.required_premises)
        )

    candidate_semantic: dict[str, str] = {}
    candidate_evolution_basis: dict[str, EvolutionBasisCard] = {}
    candidate_target_by_semantic: dict[str, str] = {}
    candidate_evolution_target: dict[int, str] = {}
    candidate_replacement_target: dict[int, str] = {}
    for candidate in plan.candidate_versions:
        if candidate.replaces_insight_id in planned_disqualified_identities:
            _fail(
                "conflicting_accepted_current_exit",
                "one accepted identity cannot be replaced and disqualified in the same plan",
            )
        if candidate.insight_id is not None:
            if candidate.insight_id in candidate_evolution_target:
                _fail(
                    "multiple_insight_versions",
                    "one event can create one version per insight identity",
                )
            candidate_evolution_target[candidate.insight_id] = (
                candidate.new_insight_key
            )
        if candidate.replaces_insight_id is not None:
            if candidate.replaces_insight_id in candidate_replacement_target:
                _fail(
                    "multiple_insight_replacements",
                    "one insight identity can have one replacement",
                )
            candidate_replacement_target[candidate.replaces_insight_id] = (
                candidate.new_insight_key
            )
        _qualify_candidate(
            candidate,
            allowed_source_points,
            accepted_by_version,
            reviews_by_id,
            current_by_version,
            hint_by_version,
            relation_by_key,
            context.latest_insight_versions,
            context.replaced_insight_identities,
        )
        signature = semantic_signature(candidate.payload)
        if signature in candidate_target_by_semantic:
            _fail(
                "duplicate_candidate",
                "one final plan cannot repeat an exact insight payload",
            )
        basis = build_evolution_basis_card(
            candidate.participants,
            candidate.payload.required_premises,
            _used_relation_dependencies(
                candidate.used_relations,
                relation_semantic,
                relation_evolution_basis,
            ),
        )
        history = _evolution_history(
            signature,
            context.insight_evolution_history_by_semantic_signature,
            context.insight_identity_by_semantic_signature,
            context.latest_insight_evolution_bases,
        )
        if candidate.target_kind == "create_identity":
            if history:
                _fail(
                    "duplicate_candidate",
                    "an exact insight payload cannot create another identity",
                )
        else:
            assert candidate.insight_id is not None
            if any(identity != candidate.insight_id for identity, _ in history):
                _fail(
                    "duplicate_candidate",
                    "an exact insight payload belongs to another identity",
                )
            unchanged_basis = any(
                fingerprint is None or fingerprint == basis.fingerprint
                for identity, fingerprint in history
                if identity == candidate.insight_id
            ) or (
                not history
                and context.known_insight_semantic_signatures.get(
                    candidate.insight_id
                )
                == signature
            )
            if unchanged_basis:
                if (
                    context.rethink_insight_semantic_signatures.get(
                        candidate.insight_id
                    )
                    == signature
                ):
                    _fail(
                        "rethink_unchanged",
                        "rethink candidate has no substantive basis change",
                    )
                _fail(
                    "duplicate_candidate",
                    "same-semantic insight evolution needs a changed formal basis",
                )
        candidate_semantic[candidate.new_insight_key] = signature
        candidate_evolution_basis[candidate.new_insight_key] = basis
        candidate_target_by_semantic[signature] = candidate.new_insight_key
        qualification_signatures[f"candidate:{candidate.new_insight_key}"] = (
            _qualification_signature(candidate.participants, candidate.payload.required_premises)
        )

    if set(candidate_evolution_target) & set(candidate_replacement_target):
        _fail(
            "conflicting_insight_identity_actions",
            "one event cannot both evolve and replace the same insight identity",
        )

    disqualification_targets = _qualify_accepted_disqualifications(
        plan,
        accepted_by_version,
        current_by_version,
        hint_by_version,
    )
    _qualify_actual_impact(plan, reviews_by_id)
    source_set, accepted_set = _final_participant_sets(plan)
    boundary_relation_set, hint_set, requalified_set, planned_set = (
        _final_relation_sets(plan, hint_by_version)
    )
    dependency_current_versions = tuple(
        sorted(
            set(boundary_relation_set)
            | {
                review.relation_version_id
                for review in plan.relation_reviews
                if review.relation_version_id in current_by_version
            }
        )
    )
    semantic_signatures = tuple(
        sorted(
            [(f"relation:{key}", value) for key, value in relation_semantic.items()]
            + [(f"candidate:{key}", value) for key, value in candidate_semantic.items()]
        )
    )
    qualification_pairs = tuple(sorted(qualification_signatures.items()))
    dependency_payload = {
        "codec": "growth-dependency-v1",
        "event_id": boundary.event_id,
        "identity_catalog_signature": context.identity_catalog_signature,
        "sources": [
            {
                "knowledge_result_id": item.knowledge_result_id,
                "point_id": item.point_id,
                "qualification_signature": _source_qualification_signature(
                    boundary, item.knowledge_result_id
                ),
            }
            for item in source_set
        ],
        "accepted": [
            {
                "insight_version_id": value,
                "qualification_signature": accepted_by_version[value].qualification_signature,
                "source_leaves": [
                    {
                        "knowledge_result_id": leaf.knowledge_result_id,
                        "point_id": leaf.point_id,
                    }
                    for leaf in accepted_by_version[value].source_leaves
                ],
            }
            for value in accepted_set
        ],
        "accepted_disqualifications": [
            {
                "insight_version_id": action.insight_version_id,
                "insight_id": disqualification_targets[
                    action.insight_version_id
                ].insight_id,
                "fact_kind": action.fact_kind.value,
                "qualification_signature": disqualification_targets[
                    action.insight_version_id
                ].qualification_signature,
            }
            for action in plan.accepted_disqualifications
        ],
        "boundary_current_relations": [
            {
                "relation_version_id": value,
                "qualification_signature": (
                    current_by_version[value].qualification_signature
                ),
            }
            for value in dependency_current_versions
        ],
        "reconsideration_hints": [
            {
                "relation_version_id": value,
                "qualification_signature": hint_by_version[value].qualification_signature,
                "requalified": value in requalified_set,
            }
            for value in hint_set
        ],
        "planned_relations": [
            {
                "key": key,
                "semantic_signature": relation_semantic[key],
                "qualification_signature": qualification_signatures[f"relation:{key}"],
            }
            for key in planned_set
        ],
        "semantic_signatures": [list(item) for item in semantic_signatures],
        "qualification_signatures": [list(item) for item in qualification_pairs],
        "evolution_basis_fingerprints": [
            [f"relation:{key}", value.fingerprint]
            for key, value in sorted(relation_evolution_basis.items())
        ] + [
            [f"candidate:{key}", value.fingerprint]
            for key, value in sorted(candidate_evolution_basis.items())
        ],
    }
    return QualifiedGrowthPlan(
        plan=plan,
        semantic_signatures=semantic_signatures,
        qualification_signatures=qualification_pairs,
        dependency_signature=sha256_json(dependency_payload),
        final_required_source_set=source_set,
        final_required_accepted_set=accepted_set,
        final_required_boundary_relation_set=boundary_relation_set,
        final_reconsideration_hint_set=hint_set,
        final_requalified_current_set=requalified_set,
        final_required_planned_relation_set=planned_set,
    )


def _qualify_accepted_disqualifications(
    plan: GrowthPlan,
    accepted_by_version: Mapping[int, AcceptedInsightInput],
    current_by_version: Mapping[int, RelationBoundaryInput],
    hint_by_version: Mapping[int, RelationBoundaryInput],
) -> dict[int, AcceptedInsightInput]:
    targets: dict[int, AcceptedInsightInput] = {}
    actions: set[tuple[int, str]] = set()
    for action in plan.accepted_disqualifications:
        target = accepted_by_version.get(action.insight_version_id)
        if target is None:
            _fail(
                "accepted_disqualification_outside_boundary",
                "accepted disqualification target is not exact recalled current input",
            )
        action_key = (action.insight_version_id, action.fact_kind.value)
        if action_key in actions:
            _fail(
                "duplicate_accepted_disqualification",
                "one accepted version repeats a final disqualification kind",
            )
        actions.add(action_key)
        targets[action.insight_version_id] = target

    used_accepted_versions = {
        participant.accepted_insight_version_id
        for output in (*plan.new_relations, *plan.candidate_versions)
        for participant in output.participants
        if participant.input_kind is InputKind.ACCEPTED_INSIGHT
    }
    if set(targets) & used_accepted_versions:
        _fail(
            "used_accepted_disqualification",
            "same-event formal output cannot use an accepted version being disqualified",
        )

    used_existing_relation_versions = {
        reference.relation_version_id
        for output in (*plan.new_relations, *plan.candidate_versions)
        for reference in output.used_relations
        if reference.ref_kind in {
            RelationRefKind.BOUNDARY_CURRENT,
            RelationRefKind.REQUALIFIED_CURRENT,
        }
    }
    for version_id in used_existing_relation_versions:
        relation = current_by_version.get(version_id) or hint_by_version.get(
            version_id
        )
        if relation is None:
            continue
        direct_accepted = {
            participant.accepted_insight_version_id
            for participant in relation.participants
            if participant.input_kind is InputKind.ACCEPTED_INSIGHT
        }
        if set(targets) & direct_accepted:
            _fail(
                "used_relation_depends_on_disqualified_accepted",
                "same-event used relation directly depends on an accepted version being disqualified",
            )

    replaced_identities = {
        candidate.replaces_insight_id
        for candidate in plan.candidate_versions
        if candidate.replaces_insight_id is not None
    }
    if any(
        target.insight_id in replaced_identities for target in targets.values()
    ):
        _fail(
            "conflicting_accepted_current_exit",
            "one accepted identity cannot be replaced and disqualified in the same plan",
        )
    return targets


def _qualify_new_input_reviews(plan: GrowthPlan, boundary: GrowthBoundary) -> None:
    expected = {item.knowledge_result_id for item in boundary.frozen_new}
    actual = [item.knowledge_result_id for item in plan.new_input_reviews]
    if len(actual) != len(set(actual)) or set(actual) != expected:
        _fail("frozen_review_coverage", "new input reviews must exactly cover frozen new")


def _qualify_relation_reviews(
    plan: GrowthPlan,
    relation_by_id: Mapping[int, object],
    catalog_basis_invalid_relation_versions: Mapping[int, int],
    selected_current_relation_version_ids: frozenset[int],
    selected_reconsideration_hint_version_ids: frozenset[int],
    current_by_version: Mapping[int, object],
    hint_by_version: Mapping[int, object],
) -> dict[int, RelationReview]:
    reviews: dict[int, RelationReview] = {}
    for review in plan.relation_reviews:
        boundary = relation_by_id.get(review.relation_id)
        if boundary is None:
            if (
                catalog_basis_invalid_relation_versions.get(review.relation_id)
                != review.relation_version_id
                or review.action is not RelationAction.EVOLVED
            ):
                _fail(
                    "unknown_relation_review",
                    "relation review is outside exact boundary or basis-invalid target",
                )
        elif boundary.relation_version_id != review.relation_version_id:
            _fail("unknown_relation_review", "relation review is outside exact boundary")
        if review.relation_id in reviews:
            _fail("duplicate_relation_review", "one relation identity has multiple final reviews")
        if review.relation_version_id in current_by_version:
            if (
                review.relation_version_id
                not in selected_current_relation_version_ids
            ):
                _fail(
                    "unexpanded_current_review",
                    "only recall-expanded current relations can be formally reviewed",
                )
        elif review.relation_version_id in hint_by_version:
            if (
                review.relation_version_id
                not in selected_reconsideration_hint_version_ids
            ):
                _fail("unselected_hint_review", "only recalled hints can be reviewed")
            if review.action is RelationAction.ATTENTION:
                if review.attention_state != "activated":
                    _fail("hint_not_activated", "a hint attention review must activate it")
            elif review.action is not RelationAction.EVOLVED:
                # A selected hint can stay retired without power. It remains a reviewed
                # hint, but cannot be referenced as requalified_current.
                pass
        reviews[review.relation_id] = review
    reviewed_hint_versions = {
        review.relation_version_id
        for review in reviews.values()
        if review.relation_version_id in hint_by_version
    }
    if reviewed_hint_versions != set(
        selected_reconsideration_hint_version_ids
    ):
        _fail("hint_review_coverage", "every recalled hint must be reviewed from scratch")
    return reviews


def _qualify_relation_targets(
    plan: GrowthPlan,
    reviews_by_id: Mapping[int, RelationReview],
    relation_by_id: Mapping[int, object],
    context: QualificationContext,
) -> None:
    targets: set[int] = set()
    relation_keys = {item.new_relation_key for item in plan.new_relations}
    by_key = {item.new_relation_key: item for item in plan.new_relations}
    for relation in plan.new_relations:
        if relation.target_kind == "create_identity":
            continue
        assert relation.relation_id is not None
        assert relation.previous_relation_version_id is not None
        if relation.relation_id in targets:
            _fail("multiple_relation_versions", "one event can create one version per identity")
        targets.add(relation.relation_id)
        boundary = relation_by_id.get(relation.relation_id)
        exact_catalog_version = context.catalog_basis_invalid_relation_versions.get(
            relation.relation_id
        )
        review = reviews_by_id.get(relation.relation_id)
        exact_boundary_version = (
            boundary is not None
            and boundary.relation_version_id
            == relation.previous_relation_version_id
        )
        if (
            not exact_boundary_version
            and exact_catalog_version != relation.previous_relation_version_id
            or review is None
            or review.action is not RelationAction.EVOLVED
            or review.successor_key != relation.new_relation_key
        ):
            _fail("invalid_evolution", "evolved relation must be exact reviewed successor")
        latest = context.latest_relation_versions.get(relation.relation_id)
        if latest is not None and latest != relation.previous_relation_version_id:
            _fail("non_direct_evolution", "evolved predecessor is not the exact latest version")
    for review in reviews_by_id.values():
        if review.action is RelationAction.EVOLVED:
            if review.successor_key not in relation_keys:
                _fail("missing_evolution_successor", "evolved review has no successor")
            successor = by_key[review.successor_key]
            if successor.relation_id != review.relation_id:
                _fail("cross_identity_evolution", "evolved successor changed identity")
        if review.action in {RelationAction.REPLACED, RelationAction.WRONG_AND_REPLACED}:
            key = review.replacement_new_relation_key
            if key not in relation_keys or by_key[key].target_kind != "create_identity":
                _fail("invalid_replacement", "replacement must be a new qualified identity")


def _qualify_replaced_accepted_impact(
    plan: GrowthPlan,
    boundary: GrowthBoundary,
    reviews_by_id: Mapping[int, RelationReview],
    context: QualificationContext,
) -> None:
    replaced_identities = {
        candidate.replaces_insight_id
        for candidate in plan.candidate_versions
        if candidate.replaces_insight_id is not None
    }
    if not replaced_identities:
        return
    accepted_current_version_by_identity = {
        item.insight_id: item.insight_version_id
        for item in boundary.accepted_current
    }
    for identity in replaced_identities:
        accepted_version = accepted_current_version_by_identity.get(identity)
        if (
            accepted_version is not None
            and accepted_version
            not in context.selected_accepted_insight_version_ids
        ):
            _fail(
                "unexpanded_accepted_replacement",
                "replacing an accepted current identity requires its exact recalled version",
            )
    accepted_identity_by_version = {
        item.insight_version_id: item.insight_id
        for item in boundary.accepted_current
    }
    for output in (*plan.new_relations, *plan.candidate_versions):
        for participant in output.participants:
            if (
                participant.input_kind is InputKind.ACCEPTED_INSIGHT
                and accepted_identity_by_version.get(
                    participant.accepted_insight_version_id or 0
                )
                in replaced_identities
            ):
                _fail(
                    "replaced_accepted_participant",
                    "same-event outputs cannot depend on an accepted identity being replaced",
                )
    permanent_exit_actions = {
        RelationAction.EVOLVED,
        RelationAction.BASIS_INVALID,
        RelationAction.WRONG,
        RelationAction.REPLACED,
        RelationAction.WRONG_AND_REPLACED,
    }
    for relation in (
        *boundary.current_relations,
        *boundary.reconsideration_hints,
    ):
        depends_on_replaced = any(
            participant.input_kind is InputKind.ACCEPTED_INSIGHT
            and accepted_identity_by_version.get(
                participant.accepted_insight_version_id or 0
            )
            in replaced_identities
            for participant in relation.participants
        )
        if not depends_on_replaced:
            continue
        review = reviews_by_id.get(relation.relation_id)
        if review is None or review.action not in permanent_exit_actions:
            _fail(
                "replaced_accepted_relation_impact",
                "a relation based on a replaced accepted identity must permanently exit or evolve",
            )


def _qualify_participants(
    participants: tuple[Participant, ...],
    premises,
    allowed_source_points: set[SourcePointIdentity],
    accepted_by_version: Mapping[int, AcceptedInsightInput],
    *,
    minimum: int,
) -> None:
    if len(participants) < minimum:
        _fail("participant_count", f"at least {minimum} participants are required")
    keys = {item.participant_key for item in participants}
    used_supports = {key for premise in premises for key in premise.supported_by}
    if not used_supports <= keys:
        _fail("unknown_premise_support", "premise support must reference a participant")
    if keys - used_supports:
        _fail("non_contributing_participant", "each participant must support a necessary premise")
    for participant in participants:
        if participant.input_kind is InputKind.SOURCE_KNOWLEDGE:
            identity = SourcePointIdentity(
                participant.knowledge_result_id or 0, participant.point_id or ""
            )
            if identity not in allowed_source_points:
                _fail("source_outside_boundary", "source participant is outside boundary")
        else:
            if participant.accepted_insight_version_id not in accepted_by_version:
                _fail("accepted_outside_boundary", "accepted participant is not current boundary")


def _qualify_candidate(
    candidate: CandidateVersionPlan,
    allowed_source_points: set[SourcePointIdentity],
    accepted_by_version: Mapping[int, AcceptedInsightInput],
    reviews_by_id: Mapping[int, RelationReview],
    current_by_version: Mapping[int, object],
    hint_by_version: Mapping[int, object],
    relation_by_key: Mapping[str, NewRelationPlan],
    latest_insight_versions: Mapping[int, int],
    replaced_insight_identities: Mapping[int, int],
) -> None:
    _qualify_participants(
        candidate.participants,
        candidate.payload.required_premises,
        allowed_source_points,
        accepted_by_version,
        minimum=2,
    )
    lineage_units: dict[tuple[str, int], set[int]] = {}
    for participant in candidate.participants:
        if participant.input_kind is InputKind.SOURCE_KNOWLEDGE:
            knowledge_result_id = participant.knowledge_result_id or 0
            lineage_units.setdefault(
                (InputKind.SOURCE_KNOWLEDGE.value, knowledge_result_id),
                set(),
            ).add(knowledge_result_id)
        else:
            accepted_version_id = participant.accepted_insight_version_id or 0
            accepted = accepted_by_version[accepted_version_id]
            lineage_units[
                (InputKind.ACCEPTED_INSIGHT.value, accepted_version_id)
            ] = {
                leaf.knowledge_result_id for leaf in accepted.source_leaves
            }
    if len(lineage_units) < 2:
        _fail("independent_lineage", "candidate needs two formal lineage units")
    source_leaves = set().union(*lineage_units.values())
    if len(source_leaves) < 2:
        _fail("independent_lineage", "candidate needs two distinguishable source bases")
    for identity, leaves in lineage_units.items():
        other_leaves = set().union(
            *(
                value
                for other_identity, value in lineage_units.items()
                if other_identity != identity
            )
        )
        if leaves <= other_leaves:
            _fail(
                "necessary_contribution",
                "a candidate lineage unit has no independent source contribution",
            )
    _qualify_relation_refs(
        candidate.used_relations,
        reviews_by_id,
        current_by_version,
        hint_by_version,
        relation_by_key,
        allow_planned=True,
    )
    if candidate.target_kind == "evolve_identity":
        if candidate.insight_id is None or candidate.previous_insight_version_id is None:
            _fail("invalid_insight_evolution", "insight evolution is incomplete")
        if candidate.insight_id in replaced_insight_identities:
            _fail(
                "replaced_insight_evolution",
                "a permanently replaced insight identity cannot evolve",
            )
        latest = latest_insight_versions.get(candidate.insight_id)
        if latest is None or latest != candidate.previous_insight_version_id:
            _fail(
                "non_direct_insight_evolution",
                "insight predecessor is not the exact latest version",
            )
    if candidate.replaces_insight_id is not None:
        if candidate.replaces_insight_id in replaced_insight_identities:
            _fail(
                "repeated_insight_replacement",
                "an insight identity cannot be replaced twice",
            )
        if candidate.replaces_insight_id not in latest_insight_versions:
            _fail(
                "unknown_insight_replacement",
                "replaced insight identity does not exist",
            )
        if candidate.insight_id == candidate.replaces_insight_id:
            _fail("self_insight_replacement", "insight replacement must change identity")


def _qualify_relation_refs(
    refs,
    reviews_by_id: Mapping[int, RelationReview],
    current_by_version: Mapping[int, object],
    hint_by_version: Mapping[int, object],
    relation_by_key: Mapping[str, NewRelationPlan],
    *,
    allow_planned: bool,
) -> None:
    reviews_by_version = {review.relation_version_id: review for review in reviews_by_id.values()}
    for ref in refs:
        if ref.ref_kind is RelationRefKind.BOUNDARY_CURRENT:
            if ref.relation_version_id not in current_by_version:
                _fail("invalid_boundary_current", "boundary current ref is not exact current input")
            review = reviews_by_version.get(ref.relation_version_id)
            if review is None:
                _fail("unreviewed_used_relation", "every used boundary relation must be reviewed")
            if review.action not in {RelationAction.UNCHANGED} and not (
                review.action is RelationAction.ATTENTION
                and review.attention_state == "activated"
            ):
                _fail("exited_used_relation", "used boundary relation exits post-H current")
        elif ref.ref_kind is RelationRefKind.REQUALIFIED_CURRENT:
            if ref.relation_version_id not in hint_by_version:
                _fail("invalid_requalified_current", "requalified ref is not a frozen hint")
            review = reviews_by_version.get(ref.relation_version_id)
            if (
                review is None
                or review.action is not RelationAction.ATTENTION
                or review.attention_state != "activated"
            ):
                _fail("hint_not_requalified", "hint has not been requalified and activated")
        else:
            if not allow_planned:
                _fail("planned_relation_cycle", "relations cannot depend on same-event relation")
            if ref.new_relation_key not in relation_by_key:
                _fail("unknown_planned_relation", "planned relation key is not stable output")


def _qualify_actual_impact(
    plan: GrowthPlan, reviews_by_id: Mapping[int, RelationReview]
) -> None:
    used_existing_versions = {
        ref.relation_version_id
        for relation in plan.new_relations
        for ref in relation.used_relations
        if ref.relation_version_id is not None
    } | {
        ref.relation_version_id
        for candidate in plan.candidate_versions
        for ref in candidate.used_relations
        if ref.relation_version_id is not None
    }
    reviewed_versions = {
        review.relation_version_id for review in reviews_by_id.values()
    }
    if not used_existing_versions <= reviewed_versions:
        _fail("actual_impact_open", "used relation is outside completed impact review")
    # A planner marks direct impact explicitly. Requiring a final review row for each
    # such declaration makes zero-change distinguishable from an unfinished pass.
    for review in plan.relation_reviews:
        if review.directly_affected and not review.reason_text.strip():
            _fail("actual_impact_reason", "directly affected relation lacks review")


def _final_participant_sets(
    plan: GrowthPlan,
) -> tuple[tuple[SourcePointIdentity, ...], tuple[int, ...]]:
    participants = [
        participant
        for item in (*plan.new_relations, *plan.candidate_versions)
        for participant in item.participants
    ]
    sources = tuple(
        sorted(
            {
                SourcePointIdentity(
                    participant.knowledge_result_id or 0,
                    participant.point_id or "",
                )
                for participant in participants
                if participant.input_kind is InputKind.SOURCE_KNOWLEDGE
            }
        )
    )
    accepted = tuple(
        sorted(
            {
                participant.accepted_insight_version_id or 0
                for participant in participants
                if participant.input_kind is InputKind.ACCEPTED_INSIGHT
            }
        )
    )
    return sources, accepted


def _final_relation_sets(plan: GrowthPlan, hint_by_version: Mapping[int, object]):
    boundary: set[int] = set()
    requalified: set[int] = set()
    planned: set[str] = set()
    for item in (*plan.new_relations, *plan.candidate_versions):
        for ref in item.used_relations:
            if ref.ref_kind is RelationRefKind.BOUNDARY_CURRENT:
                boundary.add(ref.relation_version_id or 0)
            elif ref.ref_kind is RelationRefKind.REQUALIFIED_CURRENT:
                requalified.add(ref.relation_version_id or 0)
            else:
                planned.add(ref.new_relation_key or "")
    reviewed_hints = {
        review.relation_version_id
        for review in plan.relation_reviews
        if review.relation_version_id in hint_by_version
    }
    return (
        tuple(sorted(boundary)),
        tuple(sorted(reviewed_hints)),
        tuple(sorted(requalified)),
        tuple(sorted(planned)),
    )


def _evolution_history(
    signature: str,
    history_by_signature: Mapping[str, tuple[tuple[int, str], ...]],
    identity_by_signature: Mapping[str, int],
    latest_basis_by_identity: Mapping[int, EvolutionBasisCard],
) -> tuple[tuple[int, str | None], ...]:
    history = history_by_signature.get(signature)
    if history is not None:
        return history
    identity = identity_by_signature.get(signature)
    if identity is None:
        return ()
    basis = latest_basis_by_identity.get(identity)
    return ((identity, basis.fingerprint if basis is not None else None),)


def _used_relation_dependencies(
    refs,
    planned_relation_semantics: Mapping[str, str],
    planned_relation_bases: Mapping[str, EvolutionBasisCard],
) -> tuple[str, ...]:
    dependencies = []
    for ref in refs:
        if ref.ref_kind is RelationRefKind.PLANNED_STABLE:
            key = ref.new_relation_key or ""
            try:
                signature = planned_relation_semantics[key]
                basis = planned_relation_bases[key]
            except KeyError:
                _fail(
                    "unknown_planned_relation",
                    "planned evolution dependency has no qualified relation",
                )
            dependencies.append(
                f"planned_relation:{signature}:{basis.fingerprint}"
            )
        else:
            dependencies.append(f"relation_version:{ref.relation_version_id or 0}")
    return tuple(dependencies)


def _qualification_signature(participants, premises) -> str:
    return sha256_json(
        {
            "codec": "growth-qualification-v1",
            "participants": [
                {
                    "key": item.participant_key,
                    "identity": list(item.identity),
                    "contribution": item.contribution_text,
                }
                for item in participants
            ],
            "premises": [
                {
                    "id": item.premise_id,
                    "text": item.text,
                    "supported_by": list(item.supported_by),
                }
                for item in premises
            ],
        }
    )


def _source_qualification_signature(boundary: GrowthBoundary, knowledge_result_id: int) -> str:
    for item in (*boundary.frozen_new, *boundary.eligible_history):
        if item.knowledge_result_id == knowledge_result_id:
            return item.qualification_signature
    _fail("source_signature_missing", "source qualification signature is unavailable")
    raise AssertionError


def _fail(code: str, message: str) -> None:
    raise GrowthQualificationError(code, message)
