from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass

import knowledge_distiller.organization_service as organization_service_module
from knowledge_distiller.growth_qualification import GrowthQualificationError
from knowledge_distiller.organization_models import (
    AcceptedInsightInput,
    AcceptedInsightLineageNode,
    GrowthBoundary,
    GrowthIdentityCatalog,
    GrowthPlan,
    InputKind,
    Participant,
    RelationBoundaryInput,
    SourceKnowledgeInput,
    SourcePointIdentity,
    SourcePointInput,
    parse_insight_payload,
)
from knowledge_distiller.database import (
    attach_task_to_material,
    create_task,
    establish_knowledge_result,
    establish_source_fact,
    initialize_database,
    record_knowledge_result_published,
)
from knowledge_distiller.identity import ConfirmedMaterialIdentity


SOURCE_SNAPSHOT = "正式来源支持当前观点。"


@dataclass
class ServiceQualificationCapture:
    growth_plan: GrowthPlan | None = None
    error_code: str | None = None
    call_count: int = 0
    topic_change_stage: str | None = None
    topic_change_error_code: str | None = None
    topic_change_call_count: int = 0


@contextmanager
def capture_service_growth_qualification():
    """Observe the real drive qualification call without changing its control flow."""
    original_growth = organization_service_module.qualify_growth_plan
    original_topic_changes = organization_service_module._qualify_topic_changes
    capture = ServiceQualificationCapture()

    def observed(plan, context):
        capture.call_count += 1
        if capture.growth_plan is None:
            capture.growth_plan = plan
        try:
            return original_growth(plan, context)
        except GrowthQualificationError as error:
            capture.error_code = error.code
            raise

    def observed_topic_changes(before, after, assessments):
        capture.topic_change_call_count += 1
        capture.topic_change_stage = "topic_changes"
        try:
            return original_topic_changes(before, after, assessments)
        except GrowthQualificationError as error:
            capture.topic_change_error_code = error.code
            raise

    organization_service_module.qualify_growth_plan = observed
    organization_service_module._qualify_topic_changes = observed_topic_changes
    try:
        yield capture
    finally:
        organization_service_module.qualify_growth_plan = original_growth
        organization_service_module._qualify_topic_changes = original_topic_changes


def empty_identity_catalog() -> GrowthIdentityCatalog:
    return GrowthIdentityCatalog((), (), "0" * 64)


def add_formal_knowledge(
    database_path,
    item_id: str,
    *,
    statements: tuple[str, ...] = ("一个可核查的正式观点。",),
) -> tuple[int, int]:
    """Build a healthy current/published SourceFact and KnowledgeResult graph."""
    initialize_database(database_path)
    task_id = create_task(database_path, f"https://example.test/{item_id}")
    attach_task_to_material(
        database_path,
        task_id,
        ConfirmedMaterialIdentity(
            "douyin",
            item_id,
            f"https://example.test/{item_id}",
            f"https://example.test/{item_id}",
        ),
    )
    source = establish_source_fact(
        database_path,
        task_id,
        {"author": {"display_name": f"作者 {item_id}"}},
        SOURCE_SNAPSHOT,
        [],
    )
    knowledge = establish_knowledge_result(
        database_path,
        task_id,
        source.source_fact_id,
        {
            "title": f"知识 {item_id}",
            "summary": f"{item_id} 的一句话总括。",
            "core_points": [
                {
                    "id": f"p{position}",
                    "statement": statement,
                    "argument": f"{statement}的完整论证。",
                    "evidence_ids": ["e1"],
                }
                for position, statement in enumerate(statements, start=1)
            ],
            "other_points": [],
            "evidence_registry": [
                {
                    "id": "e1",
                    "source_fact_id": source.source_fact_id,
                    "start": 0,
                    "end": len(SOURCE_SNAPSHOT),
                    "evidence_text": SOURCE_SNAPSHOT,
                }
            ],
        },
    )
    record_knowledge_result_published(
        database_path,
        task_id,
        knowledge.knowledge_result_id,
        f"知识蒸馏器/{item_id}.md",
    )
    return source.source_fact_id, knowledge.knowledge_result_id


def source_input(
    knowledge_result_id: int,
    *,
    role: str,
    point_ids: tuple[str, ...] = ("p1",),
) -> SourceKnowledgeInput:
    return SourceKnowledgeInput(
        knowledge_result_id=knowledge_result_id,
        source_fact_id=knowledge_result_id + 100,
        title=f"Knowledge {knowledge_result_id}",
        summary=f"Summary {knowledge_result_id}",
        points=tuple(
            SourcePointInput(
                knowledge_result_id,
                point_id,
                "core" if position == 0 else "other",
                f"Statement {knowledge_result_id}/{point_id}",
                f"Argument {knowledge_result_id}/{point_id}",
            )
            for position, point_id in enumerate(point_ids)
        ),
        boundary_role=role,
        qualification_signature=f"{knowledge_result_id:064x}"[-64:],
    )


def relation_boundary(
    relation_id: int,
    relation_version_id: int,
    *,
    role: str,
) -> RelationBoundaryInput:
    return RelationBoundaryInput(
        relation_id=relation_id,
        relation_version_id=relation_version_id,
        version_no=1,
        boundary_role=role,
        payload_json=(
            '{"codec":"relation-v1","conditions":[],"limitations":[],'
            f'"relation_statement":"Relation {relation_id}",'
            '"required_premises":[{"premise_id":"rp1",'
            '"supported_by":["a","b"],"text":"Supported"}],'
            '"stable_value":"Reusable"}'
        ),
        semantic_signature=f"{relation_id + 10:064x}"[-64:],
        dependency_signature=f"{relation_id + 20:064x}"[-64:],
        qualification_signature=f"{relation_id + 30:064x}"[-64:],
        participants=(
            Participant(
                "a", InputKind.SOURCE_KNOWLEDGE, 0,
                "Source 1 supplies the first necessary basis",
                knowledge_result_id=1, point_id="p1",
            ),
            Participant(
                "b", InputKind.SOURCE_KNOWLEDGE, 1,
                "Source 2 supplies the second necessary basis",
                knowledge_result_id=2, point_id="p1",
            ),
        ),
        used_relations=(),
    )


def healthy_boundary(*, include_hint: bool = True) -> GrowthBoundary:
    frozen_a = source_input(1, role="frozen_new")
    frozen_b = source_input(2, role="frozen_new")
    history = tuple(
        source_input(value, role="eligible_history") for value in (3, 4, 5)
    )
    accepted_payload = parse_insight_payload(insight_payload(claim="Accepted input"))
    accepted_participants = (
        Participant(
            "a", InputKind.SOURCE_KNOWLEDGE, 0,
            "Source 4 supplies one accepted premise",
            knowledge_result_id=4, point_id="p1",
        ),
        Participant(
            "b", InputKind.SOURCE_KNOWLEDGE, 1,
            "Source 5 supplies another accepted premise",
            knowledge_result_id=5, point_id="p1",
        ),
    )
    leaves = (
        SourcePointIdentity(4, "p1"),
        SourcePointIdentity(5, "p1"),
    )
    accepted = AcceptedInsightInput(
        insight_version_id=50,
        insight_id=5,
        version_no=1,
        produced_event_id=40,
        payload=accepted_payload,
        lineage_nodes=(
            AcceptedInsightLineageNode(
                insight_version_id=50,
                insight_id=5,
                version_no=1,
                produced_event_id=40,
                payload=accepted_payload,
                participants=accepted_participants,
                used_relations=(),
                source_leaves=leaves,
            ),
        ),
        source_leaves=leaves,
        qualification_signature=f"{50:064x}",
    )
    current = relation_boundary(7, 70, role="current_input")
    hint = relation_boundary(8, 80, role="reconsideration_hint")
    return GrowthBoundary(
        event_id=10,
        frozen_new=(frozen_a, frozen_b),
        eligible_history=history,
        accepted_current=(accepted,),
        current_relations=(current,),
        reconsideration_hints=(hint,) if include_hint else (),
    )


def empty_growth_plan_payload() -> dict[str, object]:
    return {
        "codec": "growth-plan-v1",
        "new_input_reviews": [
            {
                "knowledge_result_id": 1,
                "outcome": "considered_no_formal_result",
                "reason_text": "No formal result",
            },
            {
                "knowledge_result_id": 2,
                "outcome": "considered_no_formal_result",
                "reason_text": "No formal result",
            },
        ],
        "relation_reviews": [],
        "accepted_disqualifications": [],
        "new_relations": [],
        "candidate_versions": [],
        "topic_change_assessments": [],
        "rejected_outputs": [
            {
                "output_kind": "exploration_only",
                "related_ids": [1, 2],
                "reason_code": "no_stable_increment",
            }
        ],
    }


def source_participant(
    key: str,
    knowledge_result_id: int,
    *,
    position: int,
    point_id: str = "p1",
) -> dict[str, object]:
    return {
        "participant_key": key,
        "input_kind": "source_knowledge",
        "knowledge_result_id": knowledge_result_id,
        "point_id": point_id,
        "position": position,
        "contribution_text": f"Contribution {key}",
    }


def accepted_participant(
    key: str,
    insight_version_id: int,
    *,
    position: int,
) -> dict[str, object]:
    return {
        "participant_key": key,
        "input_kind": "accepted_insight",
        "accepted_insight_version_id": insight_version_id,
        "position": position,
        "contribution_text": f"Contribution {key}",
    }


def premise(premise_id: str, *supported_by: str) -> dict[str, object]:
    return {
        "premise_id": premise_id,
        "text": f"Premise {premise_id}",
        "supported_by": list(supported_by),
    }


def relation_payload(*, statement: str = "A changes how B applies") -> dict[str, object]:
    return {
        "codec": "relation-v1",
        "relation_statement": statement,
        "conditions": ["Within the stated scope"],
        "limitations": [{"kind": "condition", "text": "Scope is explicit"}],
        "stable_value": "Reusable boundary explanation",
        "required_premises": [premise("rp1", "a"), premise("rp2", "b")],
    }


def insight_payload(*, claim: str = "A and B reveal a narrower boundary") -> dict[str, object]:
    return {
        "codec": "insight-v1",
        "claim_kind": "judgment",
        "claim": claim,
        "short_discussion": "A supplies one condition while B supplies another. Together they establish a new boundary.",
        "value_kind": "boundary_revision",
        "connection_reasons": ["A and B contribute distinct necessary conditions"],
        "limitations": [{"kind": "boundary", "text": "Only the stated cases"}],
        "required_premises": [premise("ip1", "a"), premise("ip2", "b")],
    }
