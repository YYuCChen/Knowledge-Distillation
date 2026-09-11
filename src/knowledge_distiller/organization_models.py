from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Mapping, Sequence


class OrganizationCodecError(ValueError):
    """A persisted or planner payload is not the exact supported version."""


class InputKind(StrEnum):
    SOURCE_KNOWLEDGE = "source_knowledge"
    ACCEPTED_INSIGHT = "accepted_insight"


class RelationRefKind(StrEnum):
    BOUNDARY_CURRENT = "boundary_current"
    REQUALIFIED_CURRENT = "requalified_current"
    PLANNED_STABLE = "planned_stable"


class RelationAction(StrEnum):
    UNCHANGED = "unchanged"
    ATTENTION = "attention"
    EVOLVED = "evolved"
    BASIS_INVALID = "basis_invalid"
    WRONG = "wrong"
    REPLACED = "replaced"
    WRONG_AND_REPLACED = "wrong_and_replaced"


class RelationClassification(StrEnum):
    STABLE = "stable"
    EXPLORATION = "exploration"
    ILLEGAL = "illegal"


class ClaimKind(StrEnum):
    JUDGMENT = "judgment"
    HYPOTHESIS = "hypothesis"
    QUESTION = "question"


class InsightValueKind(StrEnum):
    COMMON_MECHANISM = "common_mechanism"
    CONFLICT_EXPLANATION = "conflict_explanation"
    CAUSAL_COMPLETION = "causal_completion"
    BOUNDARY_REVISION = "boundary_revision"
    HYPOTHESIS_OR_QUESTION = "hypothesis_or_question"


class InsightCatalogState(StrEnum):
    PENDING = "pending"
    RETHINK = "rethink"
    ACCEPTED_CURRENT = "accepted_current"
    ACCEPTED_HISTORICAL = "accepted_historical"


class InsightDisqualificationKind(StrEnum):
    BASIS_INVALID = "basis_invalid"
    REFUTED = "refuted"


class LimitationKind(StrEnum):
    UNCERTAINTY = "uncertainty"
    CONFLICT = "conflict"
    CONDITION = "condition"
    BOUNDARY = "boundary"
    COUNTEREXAMPLE = "counterexample"
    TO_VERIFY = "to_verify"


class EventStatus(StrEnum):
    RUNNING = "running"
    FAILED = "failed"
    SUCCEEDED = "succeeded"


class OrganizationFailureCode(StrEnum):
    INPUT_READ_FAILED = "input_read_failed"
    TOPIC_PLANNING_FAILED = "topic_planning_failed"
    RECALL_FAILED = "recall_failed"
    GROWTH_PLANNING_FAILED = "growth_planning_failed"
    INVALID_MODEL_OUTPUT = "invalid_model_output"
    QUALIFICATION_FAILED = "qualification_failed"
    FROZEN_INPUT_INELIGIBLE = "frozen_input_ineligible"
    DEPENDENCY_CHANGED = "dependency_changed"
    TOPIC_BASELINE_CHANGED = "topic_baseline_changed"
    PERSISTENCE_FAILED = "persistence_failed"


@dataclass(frozen=True, order=True)
class SourcePointIdentity:
    knowledge_result_id: int
    point_id: str


@dataclass(frozen=True)
class SourcePointInput:
    knowledge_result_id: int
    point_id: str
    role: str
    statement: str
    argument: str

    @property
    def identity(self) -> SourcePointIdentity:
        return SourcePointIdentity(self.knowledge_result_id, self.point_id)


@dataclass(frozen=True)
class SourceKnowledgeInput:
    knowledge_result_id: int
    source_fact_id: int
    title: str
    summary: str
    points: tuple[SourcePointInput, ...]
    boundary_role: str
    qualification_signature: str


@dataclass(frozen=True)
class AcceptedInsightInput:
    insight_version_id: int
    insight_id: int
    version_no: int
    produced_event_id: int
    payload: InsightPayload
    lineage_nodes: tuple[AcceptedInsightLineageNode, ...]
    source_leaves: tuple[SourcePointIdentity, ...]
    qualification_signature: str
    identity_kind: str = "ai_derived_insight"
    current_role: str = "current"
    disqualification_facts: tuple[str, ...] = ()
    replacement_insight_id: int | None = None

    @property
    def claim(self) -> str:
        return self.payload.claim


@dataclass(frozen=True)
class UsedRelationInput:
    relation_version_id: int
    relation_id: int
    version_no: int
    produced_event_id: int
    position: int
    role_text: str
    payload_json: str


@dataclass(frozen=True)
class AcceptedInsightLineageNode:
    insight_version_id: int
    insight_id: int
    version_no: int
    produced_event_id: int
    payload: InsightPayload
    participants: tuple[Participant, ...]
    used_relations: tuple[UsedRelationInput, ...]
    source_leaves: tuple[SourcePointIdentity, ...]


@dataclass(frozen=True)
class RelationBoundaryInput:
    relation_id: int
    relation_version_id: int
    version_no: int
    boundary_role: str
    payload_json: str
    semantic_signature: str
    dependency_signature: str
    qualification_signature: str
    participants: tuple[Participant, ...]
    used_relations: tuple[UsedRelationInput, ...]


@dataclass(frozen=True)
class GrowthBoundary:
    event_id: int
    frozen_new: tuple[SourceKnowledgeInput, ...]
    eligible_history: tuple[SourceKnowledgeInput, ...]
    accepted_current: tuple[AcceptedInsightInput, ...]
    current_relations: tuple[RelationBoundaryInput, ...]
    reconsideration_hints: tuple[RelationBoundaryInput, ...]


@dataclass(frozen=True)
class EvolutionBasisCard:
    participant_identities: tuple[tuple[str, int, str | None], ...]
    premise_support_map: tuple[tuple[str, tuple[int, ...]], ...]
    used_relation_dependencies: tuple[str, ...]
    fingerprint: str


@dataclass(frozen=True)
class RelationIdentityCatalogEntry:
    relation_id: int
    relation_version_id: int
    version_no: int
    payload: RelationPayload
    semantic_signature: str
    evolution_basis: EvolutionBasisCard
    is_latest: bool
    is_current: bool
    permanent_facts: tuple[str, ...]


@dataclass(frozen=True)
class InsightIdentityCatalogEntry:
    insight_id: int
    insight_version_id: int
    version_no: int
    payload: InsightPayload
    semantic_signature: str
    evolution_basis: EvolutionBasisCard
    is_latest: bool
    state: InsightCatalogState
    permanent_facts: tuple[str, ...]
    replaced_by_insight_id: int | None


@dataclass(frozen=True)
class GrowthIdentityCatalog:
    relation_versions: tuple[RelationIdentityCatalogEntry, ...]
    insight_versions: tuple[InsightIdentityCatalogEntry, ...]
    signature: str


@dataclass(frozen=True)
class Participant:
    participant_key: str
    input_kind: InputKind
    position: int
    contribution_text: str
    knowledge_result_id: int | None = None
    point_id: str | None = None
    accepted_insight_version_id: int | None = None

    @property
    def identity(self) -> tuple[str, int, str | None]:
        if self.input_kind is InputKind.SOURCE_KNOWLEDGE:
            assert self.knowledge_result_id is not None
            return (self.input_kind.value, self.knowledge_result_id, self.point_id)
        assert self.accepted_insight_version_id is not None
        return (self.input_kind.value, self.accepted_insight_version_id, None)


@dataclass(frozen=True)
class RequiredPremise:
    premise_id: str
    text: str
    supported_by: tuple[str, ...]


@dataclass(frozen=True)
class Limitation:
    kind: LimitationKind
    text: str


@dataclass(frozen=True)
class RelationPayload:
    relation_statement: str
    conditions: tuple[str, ...]
    limitations: tuple[Limitation, ...]
    stable_value: str
    required_premises: tuple[RequiredPremise, ...]
    codec: str = "relation-v1"


@dataclass(frozen=True)
class InsightPayload:
    claim_kind: ClaimKind
    claim: str
    short_discussion: str
    value_kind: InsightValueKind
    connection_reasons: tuple[str, ...]
    limitations: tuple[Limitation, ...]
    required_premises: tuple[RequiredPremise, ...]
    codec: str = "insight-v1"
    scan_tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class RelationReference:
    ref_kind: RelationRefKind
    role_text: str
    relation_version_id: int | None = None
    new_relation_key: str | None = None


@dataclass(frozen=True)
class NewInputReview:
    knowledge_result_id: int
    outcome: str
    reason_text: str


@dataclass(frozen=True)
class RelationReview:
    relation_id: int
    relation_version_id: int
    action: RelationAction
    reason_text: str
    directly_affected: bool
    attention_state: str | None = None
    successor_key: str | None = None
    replacement_new_relation_key: str | None = None


@dataclass(frozen=True)
class AcceptedDisqualification:
    insight_version_id: int
    fact_kind: InsightDisqualificationKind
    reason_text: str


@dataclass(frozen=True)
class NewRelationPlan:
    new_relation_key: str
    target_kind: str
    payload: RelationPayload
    participants: tuple[Participant, ...]
    used_relations: tuple[RelationReference, ...]
    relation_id: int | None = None
    previous_relation_version_id: int | None = None


@dataclass(frozen=True)
class CandidateVersionPlan:
    new_insight_key: str
    target_kind: str
    payload: InsightPayload
    participants: tuple[Participant, ...]
    used_relations: tuple[RelationReference, ...]
    insight_id: int | None = None
    previous_insight_version_id: int | None = None
    replaces_insight_id: int | None = None


@dataclass(frozen=True)
class TopicChangeAssessment:
    topic_ref: str
    changed: bool
    reason_text: str


@dataclass(frozen=True)
class RejectedOutput:
    output_kind: str
    related_ids: tuple[int, ...]
    reason_code: str


@dataclass(frozen=True)
class GrowthPlan:
    new_input_reviews: tuple[NewInputReview, ...]
    relation_reviews: tuple[RelationReview, ...]
    accepted_disqualifications: tuple[AcceptedDisqualification, ...]
    new_relations: tuple[NewRelationPlan, ...]
    candidate_versions: tuple[CandidateVersionPlan, ...]
    topic_change_assessments: tuple[TopicChangeAssessment, ...]
    rejected_outputs: tuple[RejectedOutput, ...]
    codec: str = "growth-plan-v1"


@dataclass(frozen=True)
class QualifiedGrowthPlan:
    plan: GrowthPlan
    semantic_signatures: tuple[tuple[str, str], ...]
    qualification_signatures: tuple[tuple[str, str], ...]
    dependency_signature: str
    final_required_source_set: tuple[SourcePointIdentity, ...]
    final_required_accepted_set: tuple[int, ...]
    final_required_boundary_relation_set: tuple[int, ...]
    final_reconsideration_hint_set: tuple[int, ...]
    final_requalified_current_set: tuple[int, ...]
    final_required_planned_relation_set: tuple[str, ...]


@dataclass(frozen=True)
class OrganizationSuccess:
    n: int
    m: int
    k: int
    topic_after: Mapping[str, object]
    topic_changes: tuple[str, ...]
    new_input_reviews: tuple[NewInputReview, ...]
    relation_reviews: tuple[RelationReview, ...]
    accepted_disqualifications: tuple[AcceptedDisqualification, ...]
    rejected_counts: tuple[tuple[str, int], ...]
    dependency_signature: str
    relation_local_map: tuple[tuple[str, int], ...]
    insight_local_map: tuple[tuple[str, int], ...]
    final_required_sources: tuple[SourcePointIdentity, ...]
    final_required_accepted: tuple[int, ...]
    final_required_boundary_relations: tuple[int, ...]
    final_reconsideration_hints: tuple[int, ...]
    final_requalified_current: tuple[int, ...]
    final_required_planned_relations: tuple[str, ...]
    recursive_dependency_closure: tuple[
        tuple[int, tuple[SourcePointIdentity, ...]], ...
    ]
    codec: str = "organization-success-v2"


@dataclass(frozen=True)
class OrganizationEventRead:
    event_id: int
    status: EventStatus
    started_at: str
    completed_at: str | None
    failure_code: OrganizationFailureCode | None
    frozen_new_ids: tuple[int, ...]
    success: OrganizationSuccess | None


@dataclass(frozen=True)
class PendingCandidateRead:
    insight_version_id: int
    insight_id: int
    version_no: int
    event_id: int
    payload: InsightPayload


@dataclass(frozen=True)
class PendingCandidateList:
    candidates: tuple[PendingCandidateRead, ...]
    unreadable_count: int


def canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256_json(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def relation_payload_to_dict(payload: RelationPayload) -> dict[str, object]:
    return {
        "codec": payload.codec,
        "relation_statement": payload.relation_statement,
        "conditions": list(payload.conditions),
        "limitations": [
            {"kind": item.kind.value, "text": item.text}
            for item in payload.limitations
        ],
        "stable_value": payload.stable_value,
        "required_premises": [_premise_to_dict(item) for item in payload.required_premises],
    }


def insight_payload_to_dict(payload: InsightPayload) -> dict[str, object]:
    return {
        **({"scan_tags": list(payload.scan_tags)} if payload.scan_tags else {}),
        "codec": payload.codec,
        "claim_kind": payload.claim_kind.value,
        "claim": payload.claim,
        "short_discussion": payload.short_discussion,
        "value_kind": payload.value_kind.value,
        "connection_reasons": list(payload.connection_reasons),
        "limitations": [
            {"kind": item.kind.value, "text": item.text}
            for item in payload.limitations
        ],
        "required_premises": [_premise_to_dict(item) for item in payload.required_premises],
    }


def encode_relation_payload(payload: RelationPayload) -> str:
    decoded = parse_relation_payload(relation_payload_to_dict(payload))
    if decoded != payload:
        raise OrganizationCodecError("relation payload does not round-trip")
    return canonical_json(relation_payload_to_dict(decoded))


def decode_relation_payload(raw: str) -> RelationPayload:
    return parse_relation_payload(_json_object(raw, "relation payload"))


def encode_insight_payload(payload: InsightPayload) -> str:
    decoded = parse_insight_payload(insight_payload_to_dict(payload))
    if decoded != payload:
        raise OrganizationCodecError("insight payload does not round-trip")
    return canonical_json(insight_payload_to_dict(decoded))


def decode_insight_payload(raw: str) -> InsightPayload:
    return parse_insight_payload(_json_object(raw, "insight payload"))


def parse_relation_payload(value: object) -> RelationPayload:
    item = _mapping(value, "relation payload")
    _exact_keys(
        item,
        {
            "codec",
            "relation_statement",
            "conditions",
            "limitations",
            "stable_value",
            "required_premises",
        },
        "relation payload",
    )
    if item["codec"] != "relation-v1":
        raise OrganizationCodecError("unsupported relation payload codec")
    return RelationPayload(
        relation_statement=_text(item["relation_statement"], "relation statement"),
        conditions=_text_tuple(item["conditions"], "relation conditions"),
        limitations=_limitations(item["limitations"]),
        stable_value=_text(item["stable_value"], "stable value"),
        required_premises=_premises(item["required_premises"]),
    )


def parse_insight_payload(value: object) -> InsightPayload:
    item = _mapping(value, "insight payload")
    _exact_keys(
        item,
        {
            *({"scan_tags"} if "scan_tags" in item else set()),
            "codec",
            "claim_kind",
            "claim",
            "short_discussion",
            "value_kind",
            "connection_reasons",
            "limitations",
            "required_premises",
        },
        "insight payload",
    )
    if item["codec"] != "insight-v1":
        raise OrganizationCodecError("unsupported insight payload codec")
    reasons = _text_tuple(item["connection_reasons"], "connection reasons")
    if not 1 <= len(reasons) <= 3:
        raise OrganizationCodecError("connection reasons must contain 1 to 3 items")
    tags = tuple(_list(item["scan_tags"], "scan tags")) if "scan_tags" in item else ()
    if "scan_tags" in item:
        import re
        if (not isinstance(item["scan_tags"], list) or len(tags) != 3
                or any(not isinstance(tag, str) or not re.fullmatch(r"[\u3400-\u9fff]{4}", tag) for tag in tags) or len(set(tags)) != 3):
            raise OrganizationCodecError("scan tags require three distinct four-character Chinese labels")
    return InsightPayload(
        scan_tags=tags,
        claim_kind=_enum(ClaimKind, item["claim_kind"], "claim kind"),
        claim=_text(item["claim"], "claim"),
        short_discussion=_text(item["short_discussion"], "short discussion"),
        value_kind=_enum(InsightValueKind, item["value_kind"], "value kind"),
        connection_reasons=reasons,
        limitations=_limitations(item["limitations"]),
        required_premises=_premises(item["required_premises"]),
    )


def parse_growth_plan(raw: str | Mapping[str, object]) -> GrowthPlan:
    value = _json_object(raw, "growth plan") if isinstance(raw, str) else raw
    item = _mapping(value, "growth plan")
    _exact_keys(
        item,
        {
            "codec",
            "new_input_reviews",
            "relation_reviews",
            "accepted_disqualifications",
            "new_relations",
            "candidate_versions",
            "topic_change_assessments",
            "rejected_outputs",
        },
        "growth plan",
    )
    if item["codec"] != "growth-plan-v1":
        raise OrganizationCodecError("unsupported growth plan codec")
    plan = GrowthPlan(
        new_input_reviews=_parse_list(item["new_input_reviews"], _new_input_review),
        relation_reviews=_parse_list(item["relation_reviews"], _relation_review),
        accepted_disqualifications=_parse_list(
            item["accepted_disqualifications"], _accepted_disqualification
        ),
        new_relations=_parse_list(item["new_relations"], _new_relation),
        candidate_versions=_parse_list(item["candidate_versions"], _candidate),
        topic_change_assessments=_parse_list(
            item["topic_change_assessments"], _topic_change_assessment
        ),
        rejected_outputs=_parse_list(item["rejected_outputs"], _rejected_output),
    )
    _unique((value.new_relation_key for value in plan.new_relations), "new relation key")
    _unique((value.new_insight_key for value in plan.candidate_versions), "new insight key")
    _unique((value.relation_id for value in plan.relation_reviews), "relation review")
    _unique(
        (
            (value.insight_version_id, value.fact_kind.value)
            for value in plan.accepted_disqualifications
        ),
        "accepted disqualification target and kind",
    )
    return plan


def growth_plan_to_dict(plan: GrowthPlan) -> dict[str, object]:
    return {
        "codec": plan.codec,
        "new_input_reviews": [
            {
                "knowledge_result_id": value.knowledge_result_id,
                "outcome": value.outcome,
                "reason_text": value.reason_text,
            }
            for value in plan.new_input_reviews
        ],
        "relation_reviews": [_relation_review_to_dict(value) for value in plan.relation_reviews],
        "accepted_disqualifications": [
            _accepted_disqualification_to_dict(value)
            for value in plan.accepted_disqualifications
        ],
        "new_relations": [_new_relation_to_dict(value) for value in plan.new_relations],
        "candidate_versions": [_candidate_to_dict(value) for value in plan.candidate_versions],
        "topic_change_assessments": [
            {
                "topic_ref": value.topic_ref,
                "changed": value.changed,
                "reason_text": value.reason_text,
            }
            for value in plan.topic_change_assessments
        ],
        "rejected_outputs": [
            {
                "output_kind": value.output_kind,
                "related_ids": list(value.related_ids),
                "reason_code": value.reason_code,
            }
            for value in plan.rejected_outputs
        ],
    }


def encode_growth_plan(plan: GrowthPlan) -> str:
    parsed = parse_growth_plan(growth_plan_to_dict(plan))
    if parsed != plan:
        raise OrganizationCodecError("growth plan does not round-trip")
    return canonical_json(growth_plan_to_dict(parsed))


def encode_success_payload(success: OrganizationSuccess) -> str:
    value = {
        "codec": success.codec,
        "N": success.n,
        "M": success.m,
        "K": success.k,
        "topic_after": dict(success.topic_after),
        "topic_changes": list(success.topic_changes),
        "new_input_reviews": [
            {
                "knowledge_result_id": review.knowledge_result_id,
                "outcome": review.outcome,
                "reason_text": review.reason_text,
            }
            for review in success.new_input_reviews
        ],
        "relation_reviews": [_relation_review_to_dict(review) for review in success.relation_reviews],
        "rejected_counts": [
            {"kind": kind, "count": count} for kind, count in success.rejected_counts
        ],
        "dependency_signature": success.dependency_signature,
        "relation_local_map": [
            {"key": key, "relation_version_id": value}
            for key, value in success.relation_local_map
        ],
        "insight_local_map": [
            {"key": key, "insight_version_id": value}
            for key, value in success.insight_local_map
        ],
        "final_required_sources": [
            {
                "knowledge_result_id": value.knowledge_result_id,
                "point_id": value.point_id,
            }
            for value in success.final_required_sources
        ],
        "final_required_accepted": list(success.final_required_accepted),
        "final_required_boundary_relations": list(
            success.final_required_boundary_relations
        ),
        "final_reconsideration_hints": list(success.final_reconsideration_hints),
        "final_requalified_current": list(success.final_requalified_current),
        "final_required_planned_relations": list(
            success.final_required_planned_relations
        ),
        "recursive_dependency_closure": [
            {
                "insight_version_id": insight_version_id,
                "source_leaves": [
                    {
                        "knowledge_result_id": leaf.knowledge_result_id,
                        "point_id": leaf.point_id,
                    }
                    for leaf in leaves
                ],
            }
            for insight_version_id, leaves in success.recursive_dependency_closure
        ],
    }
    if success.codec == "organization-success-v2":
        value["accepted_disqualifications"] = [
            _accepted_disqualification_to_dict(item)
            for item in success.accepted_disqualifications
        ]
    parsed = parse_success_payload(value)
    if parsed != success:
        raise OrganizationCodecError("success payload does not round-trip")
    return canonical_json(value)


def decode_success_payload(raw: str) -> OrganizationSuccess:
    return parse_success_payload(_json_object(raw, "success payload"))


def parse_success_payload(value: object) -> OrganizationSuccess:
    item = _mapping(value, "success payload")
    common_keys = {
        "codec",
        "N",
        "M",
        "K",
        "topic_after",
        "topic_changes",
        "new_input_reviews",
        "relation_reviews",
        "rejected_counts",
        "dependency_signature",
        "relation_local_map",
        "insight_local_map",
        "final_required_sources",
        "final_required_accepted",
        "final_required_boundary_relations",
        "final_reconsideration_hints",
        "final_requalified_current",
        "final_required_planned_relations",
        "recursive_dependency_closure",
    }
    if item.get("codec") == "organization-success-v1":
        _exact_keys(item, common_keys, "success payload")
        accepted_disqualifications = ()
    elif item.get("codec") == "organization-success-v2":
        _exact_keys(
            item,
            common_keys | {"accepted_disqualifications"},
            "success payload",
        )
        accepted_disqualifications = _parse_list(
            item["accepted_disqualifications"], _accepted_disqualification
        )
        _unique(
            (
                (action.insight_version_id, action.fact_kind.value)
                for action in accepted_disqualifications
            ),
            "accepted disqualification target and kind",
        )
    else:
        raise OrganizationCodecError("unsupported success payload codec")
    rejected_counts = _key_count_pairs(item["rejected_counts"], "kind", "count")
    relation_map = _key_count_pairs(
        item["relation_local_map"], "key", "relation_version_id", positive=True
    )
    insight_map = _key_count_pairs(
        item["insight_local_map"], "key", "insight_version_id", positive=True
    )
    topic_after = _mapping(item["topic_after"], "topic after")
    final_sources = tuple(
        _source_point_identity(value)
        for value in _list(item["final_required_sources"], "final required sources")
    )
    _unique(final_sources, "final required source")
    closure: list[tuple[int, tuple[SourcePointIdentity, ...]]] = []
    for raw in _list(item["recursive_dependency_closure"], "recursive dependency closure"):
        value = _mapping(raw, "recursive dependency item")
        _exact_keys(
            value,
            {"insight_version_id", "source_leaves"},
            "recursive dependency item",
        )
        leaves = tuple(
            _source_point_identity(leaf)
            for leaf in _list(value["source_leaves"], "recursive source leaves")
        )
        _unique(leaves, "recursive source leaf")
        closure.append(
            (
                _integer(
                    value["insight_version_id"],
                    "recursive insight version id",
                    minimum=1,
                ),
                leaves,
            )
        )
    _unique((value[0] for value in closure), "recursive insight version")
    return OrganizationSuccess(
        n=_integer(item["N"], "N", minimum=1),
        m=_integer(item["M"], "M", minimum=0),
        k=_integer(item["K"], "K", minimum=0),
        topic_after=dict(topic_after),
        topic_changes=_text_tuple(item["topic_changes"], "topic changes", allow_empty=True),
        new_input_reviews=_parse_list(item["new_input_reviews"], _new_input_review),
        relation_reviews=_parse_list(item["relation_reviews"], _relation_review),
        accepted_disqualifications=accepted_disqualifications,
        rejected_counts=tuple((key, int(count)) for key, count in rejected_counts),
        dependency_signature=_signature(item["dependency_signature"], "dependency signature"),
        relation_local_map=tuple((key, int(count)) for key, count in relation_map),
        insight_local_map=tuple((key, int(count)) for key, count in insight_map),
        final_required_sources=final_sources,
        final_required_accepted=_integer_tuple(
            item["final_required_accepted"],
            "final required accepted",
            allow_empty=True,
        ),
        final_required_boundary_relations=_integer_tuple(
            item["final_required_boundary_relations"],
            "final required boundary relations",
            allow_empty=True,
        ),
        final_reconsideration_hints=_integer_tuple(
            item["final_reconsideration_hints"],
            "final reconsideration hints",
            allow_empty=True,
        ),
        final_requalified_current=_integer_tuple(
            item["final_requalified_current"],
            "final requalified current",
            allow_empty=True,
        ),
        final_required_planned_relations=_text_tuple(
            item["final_required_planned_relations"],
            "final required planned relations",
            allow_empty=True,
        ),
        recursive_dependency_closure=tuple(closure),
        codec=str(item["codec"]),
    )


def semantic_signature(payload: RelationPayload | InsightPayload) -> str:
    if isinstance(payload, RelationPayload):
        semantic_payload = {
            "codec": "relation-semantic-v1",
            "relation_statement": payload.relation_statement,
            "conditions": sorted(payload.conditions),
            "limitations": [
                {"kind": item.kind.value, "text": item.text}
                for item in sorted(
                    payload.limitations,
                    key=lambda value: (value.kind.value, value.text),
                )
            ],
            "stable_value": payload.stable_value,
            "required_premise_texts": [
                item.text for item in sorted(
                    payload.required_premises, key=lambda value: value.text
                )
            ],
        }
    else:
        semantic_payload = {
            "codec": "insight-semantic-v1",
            "claim_kind": payload.claim_kind.value,
            "claim": payload.claim,
            "short_discussion": payload.short_discussion,
            "value_kind": payload.value_kind.value,
            "connection_reasons": sorted(payload.connection_reasons),
            "limitations": [
                {"kind": item.kind.value, "text": item.text}
                for item in sorted(
                    payload.limitations,
                    key=lambda value: (value.kind.value, value.text),
                )
            ],
            "required_premise_texts": [
                item.text for item in sorted(
                    payload.required_premises, key=lambda value: value.text
                )
            ],
        }
    return sha256_json(semantic_payload)


def build_evolution_basis_card(
    participants: Sequence[Participant],
    premises: Sequence[RequiredPremise],
    used_relation_dependencies: Sequence[str],
) -> EvolutionBasisCard:
    """Build comparison-only formation facts, excluding free-form wording."""
    identity_by_key: dict[str, tuple[str, int, str | None]] = {}
    for participant in participants:
        if participant.participant_key in identity_by_key:
            raise ValueError("Evolution basis has duplicate participant keys")
        identity_by_key[participant.participant_key] = participant.identity
    identities = tuple(sorted(identity_by_key.values()))
    if len(identities) != len(set(identities)):
        raise ValueError("Evolution basis has duplicate participant identities")
    index_by_identity = {identity: index for index, identity in enumerate(identities)}
    support_map = []
    for premise in premises:
        try:
            supported = tuple(
                sorted(
                    index_by_identity[identity_by_key[key]]
                    for key in premise.supported_by
                )
            )
        except KeyError as exc:
            raise ValueError("Evolution basis premise references an unknown participant") from exc
        support_map.append((premise.text, supported))
    premise_support_map = tuple(sorted(support_map))
    dependencies = tuple(sorted(used_relation_dependencies))
    if len(dependencies) != len(set(dependencies)):
        raise ValueError("Evolution basis has duplicate relation dependencies")
    payload = {
        "codec": "growth-evolution-basis-v1",
        "participant_identities": [list(identity) for identity in identities],
        "premise_support_map": [
            {
                "premise_text": text,
                "support_identity_indexes": list(supported),
            }
            for text, supported in premise_support_map
        ],
        "used_relation_dependencies": list(dependencies),
    }
    return EvolutionBasisCard(
        identities,
        premise_support_map,
        dependencies,
        sha256_json(payload),
    )


def evolution_basis_card_to_dict(value: EvolutionBasisCard) -> dict[str, object]:
    return {
        "codec": "growth-evolution-basis-v1",
        "participant_identities": [list(item) for item in value.participant_identities],
        "premise_support_map": [
            {
                "premise_text": text,
                "support_identity_indexes": list(supported),
            }
            for text, supported in value.premise_support_map
        ],
        "used_relation_dependencies": list(value.used_relation_dependencies),
        "fingerprint": value.fingerprint,
    }


def _new_input_review(value: object) -> NewInputReview:
    item = _mapping(value, "new input review")
    _exact_keys(item, {"knowledge_result_id", "outcome", "reason_text"}, "new input review")
    outcome = _text(item["outcome"], "new input outcome")
    if outcome not in {"participated", "considered_no_formal_result"}:
        raise OrganizationCodecError("invalid new input outcome")
    return NewInputReview(
        _integer(item["knowledge_result_id"], "knowledge result id", minimum=1),
        outcome,
        _text(item["reason_text"], "new input review reason"),
    )


def _relation_review(value: object) -> RelationReview:
    item = _mapping(value, "relation review")
    base = {
        "relation_id",
        "relation_version_id",
        "action",
        "reason_text",
        "directly_affected",
    }
    action = _enum(RelationAction, item.get("action"), "relation action")
    expected = set(base)
    if action is RelationAction.ATTENTION:
        expected.add("attention_state")
    if action is RelationAction.EVOLVED:
        expected.add("successor_key")
    if action in {RelationAction.REPLACED, RelationAction.WRONG_AND_REPLACED}:
        expected.add("replacement_new_relation_key")
    _exact_keys(item, expected, "relation review")
    attention_state = item.get("attention_state")
    if attention_state is not None and attention_state not in {"activated", "retired"}:
        raise OrganizationCodecError("invalid attention state")
    return RelationReview(
        relation_id=_integer(item["relation_id"], "relation id", minimum=1),
        relation_version_id=_integer(
            item["relation_version_id"], "relation version id", minimum=1
        ),
        action=action,
        reason_text=_text(item["reason_text"], "relation review reason"),
        directly_affected=_boolean(item["directly_affected"], "directly affected"),
        attention_state=attention_state,
        successor_key=(
            _text(item["successor_key"], "successor key")
            if "successor_key" in item
            else None
        ),
        replacement_new_relation_key=(
            _text(item["replacement_new_relation_key"], "replacement relation key")
            if "replacement_new_relation_key" in item
            else None
        ),
    )


def _accepted_disqualification(value: object) -> AcceptedDisqualification:
    item = _mapping(value, "accepted disqualification")
    _exact_keys(
        item,
        {"insight_version_id", "fact_kind", "reason_text"},
        "accepted disqualification",
    )
    return AcceptedDisqualification(
        insight_version_id=_integer(
            item["insight_version_id"], "insight version id", minimum=1
        ),
        fact_kind=_enum(
            InsightDisqualificationKind,
            item["fact_kind"],
            "accepted disqualification kind",
        ),
        reason_text=_text(
            item["reason_text"], "accepted disqualification reason"
        ),
    )


def _new_relation(value: object) -> NewRelationPlan:
    item = _mapping(value, "new relation")
    target_kind = item.get("target_kind")
    common = {
        "new_relation_key",
        "target_kind",
        "payload",
        "participants",
        "used_relations",
    }
    if target_kind == "create_identity":
        _exact_keys(item, common, "new relation")
        relation_id = previous = None
    elif target_kind == "evolve_identity":
        _exact_keys(
            item,
            common | {"relation_id", "previous_relation_version_id"},
            "new relation",
        )
        relation_id = _integer(item["relation_id"], "relation id", minimum=1)
        previous = _integer(
            item["previous_relation_version_id"],
            "previous relation version id",
            minimum=1,
        )
    else:
        raise OrganizationCodecError("invalid relation target kind")
    participants = _participants(item["participants"])
    return NewRelationPlan(
        new_relation_key=_text(item["new_relation_key"], "new relation key"),
        target_kind=str(target_kind),
        payload=parse_relation_payload(item["payload"]),
        participants=participants,
        used_relations=_relation_references(item["used_relations"], allow_planned=False),
        relation_id=relation_id,
        previous_relation_version_id=previous,
    )


def _candidate(value: object) -> CandidateVersionPlan:
    item = _mapping(value, "candidate version")
    target_kind = item.get("target_kind")
    common = {
        "new_insight_key",
        "target_kind",
        "payload",
        "participants",
        "used_relations",
    }
    optional = {"replaces_insight_id"} if "replaces_insight_id" in item else set()
    if target_kind == "create_identity":
        _exact_keys(item, common | optional, "candidate version")
        insight_id = previous = None
    elif target_kind == "evolve_identity":
        _exact_keys(
            item,
            common | {"insight_id", "previous_insight_version_id"} | optional,
            "candidate version",
        )
        insight_id = _integer(item["insight_id"], "insight id", minimum=1)
        previous = _integer(
            item["previous_insight_version_id"],
            "previous insight version id",
            minimum=1,
        )
    else:
        raise OrganizationCodecError("invalid insight target kind")
    return CandidateVersionPlan(
        new_insight_key=_text(item["new_insight_key"], "new insight key"),
        target_kind=str(target_kind),
        payload=parse_insight_payload(item["payload"]),
        participants=_participants(item["participants"]),
        used_relations=_relation_references(item["used_relations"], allow_planned=True),
        insight_id=insight_id,
        previous_insight_version_id=previous,
        replaces_insight_id=(
            _integer(item["replaces_insight_id"], "replaced insight id", minimum=1)
            if "replaces_insight_id" in item
            else None
        ),
    )


def _topic_change_assessment(value: object) -> TopicChangeAssessment:
    item = _mapping(value, "topic change assessment")
    _exact_keys(item, {"topic_ref", "changed", "reason_text"}, "topic change assessment")
    return TopicChangeAssessment(
        _text(item["topic_ref"], "topic reference"),
        _boolean(item["changed"], "topic changed"),
        _text(item["reason_text"], "topic change reason"),
    )


def _rejected_output(value: object) -> RejectedOutput:
    item = _mapping(value, "rejected output")
    _exact_keys(item, {"output_kind", "related_ids", "reason_code"}, "rejected output")
    output_kind = _text(item["output_kind"], "rejected output kind")
    if output_kind not in {
        "exploration_only",
        "illegal",
        "duplicate_relation",
        "duplicate_candidate",
        "qualification_rejected",
    }:
        raise OrganizationCodecError("invalid rejected output kind")
    related = _integer_tuple(item["related_ids"], "related ids", allow_empty=True)
    return RejectedOutput(output_kind, related, _text(item["reason_code"], "reason code"))


def _participants(value: object) -> tuple[Participant, ...]:
    values = _list(value, "participants")
    result: list[Participant] = []
    for raw in values:
        item = _mapping(raw, "participant")
        kind = _enum(InputKind, item.get("input_kind"), "participant input kind")
        common = {"participant_key", "input_kind", "position", "contribution_text"}
        if kind is InputKind.SOURCE_KNOWLEDGE:
            _exact_keys(
                item,
                common | {"knowledge_result_id", "point_id"},
                "source participant",
            )
            participant = Participant(
                _text(item["participant_key"], "participant key"),
                kind,
                _integer(item["position"], "participant position", minimum=0),
                _text(item["contribution_text"], "participant contribution"),
                knowledge_result_id=_integer(
                    item["knowledge_result_id"], "knowledge result id", minimum=1
                ),
                point_id=_text(item["point_id"], "point id"),
            )
        else:
            _exact_keys(
                item,
                common | {"accepted_insight_version_id"},
                "accepted participant",
            )
            participant = Participant(
                _text(item["participant_key"], "participant key"),
                kind,
                _integer(item["position"], "participant position", minimum=0),
                _text(item["contribution_text"], "participant contribution"),
                accepted_insight_version_id=_integer(
                    item["accepted_insight_version_id"],
                    "accepted insight version id",
                    minimum=1,
                ),
            )
        result.append(participant)
    _dense_positions((item.position for item in result), "participant")
    _unique((item.participant_key for item in result), "participant key")
    _unique((item.identity for item in result), "participant identity")
    return tuple(result)


def _relation_references(value: object, *, allow_planned: bool) -> tuple[RelationReference, ...]:
    values = _list(value, "used relations")
    result: list[RelationReference] = []
    identities: list[tuple[str, object]] = []
    for raw in values:
        item = _mapping(raw, "relation reference")
        kind = _enum(RelationRefKind, item.get("ref_kind"), "relation reference kind")
        if kind is RelationRefKind.PLANNED_STABLE:
            if not allow_planned:
                raise OrganizationCodecError("relation plans cannot use planned relations")
            _exact_keys(item, {"ref_kind", "new_relation_key", "role_text"}, "planned relation reference")
            reference = RelationReference(
                kind,
                _text(item["role_text"], "relation role"),
                new_relation_key=_text(item["new_relation_key"], "new relation key"),
            )
            identities.append((kind.value, reference.new_relation_key))
        else:
            _exact_keys(item, {"ref_kind", "relation_version_id", "role_text"}, "boundary relation reference")
            reference = RelationReference(
                kind,
                _text(item["role_text"], "relation role"),
                relation_version_id=_integer(
                    item["relation_version_id"], "relation version id", minimum=1
                ),
            )
            identities.append((kind.value, reference.relation_version_id))
        result.append(reference)
    _unique(identities, "used relation reference")
    return tuple(result)


def _premises(value: object) -> tuple[RequiredPremise, ...]:
    values = _list(value, "required premises")
    if not values:
        raise OrganizationCodecError("required premises cannot be empty")
    result: list[RequiredPremise] = []
    for raw in values:
        item = _mapping(raw, "required premise")
        _exact_keys(item, {"premise_id", "text", "supported_by"}, "required premise")
        supported_by = _text_tuple(item["supported_by"], "premise support")
        if not supported_by:
            raise OrganizationCodecError("premise support cannot be empty")
        _unique(supported_by, "premise support")
        result.append(
            RequiredPremise(
                _text(item["premise_id"], "premise id"),
                _text(item["text"], "premise text"),
                supported_by,
            )
        )
    _unique((item.premise_id for item in result), "premise id")
    return tuple(result)


def _limitations(value: object) -> tuple[Limitation, ...]:
    values = _list(value, "limitations")
    result: list[Limitation] = []
    for raw in values:
        item = _mapping(raw, "limitation")
        _exact_keys(item, {"kind", "text"}, "limitation")
        result.append(
            Limitation(
                _enum(LimitationKind, item["kind"], "limitation kind"),
                _text(item["text"], "limitation text"),
            )
        )
    return tuple(result)


def _participant_to_dict(value: Participant) -> dict[str, object]:
    result: dict[str, object] = {
        "participant_key": value.participant_key,
        "input_kind": value.input_kind.value,
        "position": value.position,
        "contribution_text": value.contribution_text,
    }
    if value.input_kind is InputKind.SOURCE_KNOWLEDGE:
        result["knowledge_result_id"] = value.knowledge_result_id
        result["point_id"] = value.point_id
    else:
        result["accepted_insight_version_id"] = value.accepted_insight_version_id
    return result


def _reference_to_dict(value: RelationReference) -> dict[str, object]:
    result: dict[str, object] = {
        "ref_kind": value.ref_kind.value,
        "role_text": value.role_text,
    }
    if value.ref_kind is RelationRefKind.PLANNED_STABLE:
        result["new_relation_key"] = value.new_relation_key
    else:
        result["relation_version_id"] = value.relation_version_id
    return result


def _premise_to_dict(value: RequiredPremise) -> dict[str, object]:
    return {
        "premise_id": value.premise_id,
        "text": value.text,
        "supported_by": list(value.supported_by),
    }


def _relation_review_to_dict(value: RelationReview) -> dict[str, object]:
    result: dict[str, object] = {
        "relation_id": value.relation_id,
        "relation_version_id": value.relation_version_id,
        "action": value.action.value,
        "reason_text": value.reason_text,
        "directly_affected": value.directly_affected,
    }
    if value.action is RelationAction.ATTENTION:
        result["attention_state"] = value.attention_state
    if value.action is RelationAction.EVOLVED:
        result["successor_key"] = value.successor_key
    if value.action in {RelationAction.REPLACED, RelationAction.WRONG_AND_REPLACED}:
        result["replacement_new_relation_key"] = value.replacement_new_relation_key
    return result


def _accepted_disqualification_to_dict(
    value: AcceptedDisqualification,
) -> dict[str, object]:
    return {
        "insight_version_id": value.insight_version_id,
        "fact_kind": value.fact_kind.value,
        "reason_text": value.reason_text,
    }


def _new_relation_to_dict(value: NewRelationPlan) -> dict[str, object]:
    result: dict[str, object] = {
        "new_relation_key": value.new_relation_key,
        "target_kind": value.target_kind,
        "payload": relation_payload_to_dict(value.payload),
        "participants": [_participant_to_dict(item) for item in value.participants],
        "used_relations": [_reference_to_dict(item) for item in value.used_relations],
    }
    if value.target_kind == "evolve_identity":
        result["relation_id"] = value.relation_id
        result["previous_relation_version_id"] = value.previous_relation_version_id
    return result


def _candidate_to_dict(value: CandidateVersionPlan) -> dict[str, object]:
    result: dict[str, object] = {
        "new_insight_key": value.new_insight_key,
        "target_kind": value.target_kind,
        "payload": insight_payload_to_dict(value.payload),
        "participants": [_participant_to_dict(item) for item in value.participants],
        "used_relations": [_reference_to_dict(item) for item in value.used_relations],
    }
    if value.target_kind == "evolve_identity":
        result["insight_id"] = value.insight_id
        result["previous_insight_version_id"] = value.previous_insight_version_id
    if value.replaces_insight_id is not None:
        result["replaces_insight_id"] = value.replaces_insight_id
    return result


def _json_object(raw: str, label: str) -> Mapping[str, object]:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as error:
        raise OrganizationCodecError(f"{label} is not valid JSON") from error
    return _mapping(value, label)


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise OrganizationCodecError(f"{label} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise OrganizationCodecError(f"{label} keys must be strings")
    return value


def _list(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise OrganizationCodecError(f"{label} must be an array")
    return value


def _parse_list(value: object, parser):
    return tuple(parser(item) for item in _list(value, "list"))


def _exact_keys(value: Mapping[str, object], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise OrganizationCodecError(f"{label} has unknown or missing fields")


def _text(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise OrganizationCodecError(f"{label} must be text")
    text = value.strip()
    if not text:
        raise OrganizationCodecError(f"{label} cannot be empty")
    return text


def _text_tuple(value: object, label: str, *, allow_empty: bool = True) -> tuple[str, ...]:
    result = tuple(_text(item, label) for item in _list(value, label))
    if not allow_empty and not result:
        raise OrganizationCodecError(f"{label} cannot be empty")
    return result


def _integer(value: object, label: str, *, minimum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise OrganizationCodecError(f"{label} must be an integer >= {minimum}")
    return value


def _integer_tuple(value: object, label: str, *, allow_empty: bool) -> tuple[int, ...]:
    result = tuple(_integer(item, label, minimum=1) for item in _list(value, label))
    if not allow_empty and not result:
        raise OrganizationCodecError(f"{label} cannot be empty")
    _unique(result, label)
    return result


def _boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise OrganizationCodecError(f"{label} must be boolean")
    return value


def _enum(enum_type, value: object, label: str):
    if not isinstance(value, str):
        raise OrganizationCodecError(f"{label} must be text")
    try:
        return enum_type(value)
    except ValueError as error:
        raise OrganizationCodecError(f"invalid {label}") from error


def _dense_positions(values: Sequence[int], label: str) -> None:
    materialized = list(values)
    if sorted(materialized) != list(range(len(materialized))):
        raise OrganizationCodecError(f"{label} positions must be dense and unique")


def _unique(values, label: str) -> None:
    materialized = list(values)
    if len(materialized) != len(set(materialized)):
        raise OrganizationCodecError(f"duplicate {label}")


def _signature(value: object, label: str) -> str:
    text = _text(value, label)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise OrganizationCodecError(f"{label} must be lowercase sha256")
    return text


def _key_count_pairs(
    value: object,
    key_name: str,
    count_name: str,
    *,
    positive: bool = False,
) -> tuple[tuple[str, int], ...]:
    pairs: list[tuple[str, int]] = []
    for raw in _list(value, "key/count pairs"):
        item = _mapping(raw, "key/count pair")
        _exact_keys(item, {key_name, count_name}, "key/count pair")
        pairs.append(
            (
                _text(item[key_name], key_name),
                _integer(item[count_name], count_name, minimum=1 if positive else 0),
            )
        )
    _unique((key for key, _ in pairs), key_name)
    return tuple(pairs)


def _source_point_identity(value: object) -> SourcePointIdentity:
    item = _mapping(value, "source point identity")
    _exact_keys(
        item,
        {"knowledge_result_id", "point_id"},
        "source point identity",
    )
    return SourcePointIdentity(
        _integer(item["knowledge_result_id"], "knowledge result id", minimum=1),
        _text(item["point_id"], "point id"),
    )
