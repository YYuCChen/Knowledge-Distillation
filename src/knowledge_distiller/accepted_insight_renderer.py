from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass
from pathlib import PurePosixPath

from .knowledge_derivation import (
    KnowledgeEvidence,
    knowledge_candidate_from_payload,
)
from .obsidian_renderer import _source_blocks, _target_block
from .organization_models import (
    InputKind,
    InsightPayload,
    Participant,
    RelationPayload,
    canonical_json,
    insight_payload_to_dict,
    parse_insight_payload,
    parse_relation_payload,
    relation_payload_to_dict,
    semantic_signature,
)


RENDERER_CONTRACT = "accepted-insight-markdown-v1"
PLACEMENT_RECEIPT_CODEC = "accepted-placement-receipt-v1"


@dataclass(frozen=True)
class AcceptedRenderFormationEvent:
    event_id: int
    started_at: str
    completed_at: str


@dataclass(frozen=True)
class AcceptedRenderJudgment:
    judgment_id: int
    decision: str
    annotation_text: str | None
    decided_at: str


@dataclass(frozen=True)
class AcceptedRenderExitFact:
    fact_kind: str
    event_id: int
    reason_text: str
    created_at: str
    replacement_insight_id: int | None = None


@dataclass(frozen=True)
class AcceptedRenderUsedRelation:
    relation_version_id: int
    relation_id: int
    version_no: int
    produced_event_id: int
    position: int
    role_text: str
    payload: RelationPayload
    semantic_signature: str
    dependency_signature: str


@dataclass(frozen=True)
class AcceptedRenderEvidence:
    evidence_id: str
    source_fact_id: int
    start_offset: int
    end_offset: int
    evidence_text: str


@dataclass(frozen=True)
class AcceptedRenderSourceLeaf:
    material_id: int
    platform: str
    platform_item_id: str
    original_url: str
    canonical_url: str | None
    material_created_at: str
    knowledge_result_id: int
    knowledge_result_created_at: str
    knowledge_payload_json: str
    invalidated_at: str | None
    invalidation_reason: str | None
    published_at: str
    published_path: str
    source_fact_id: int
    source_metadata_json: str
    content_snapshot: str
    uncertainty_json: str
    replaces_source_fact_id: int | None
    source_change_reason: str | None
    source_fact_created_at: str
    point_id: str
    point_role: str
    point_statement: str
    point_argument: str
    evidences: tuple[AcceptedRenderEvidence, ...]
    knowledge_title: str
    knowledge_summary: str
    source_label: str


@dataclass(frozen=True)
class AcceptedPublicationLink:
    publication_id: int
    insight_version_id: int
    insight_id: int
    version_no: int
    claim: str
    relative_path: str
    machine_identity: str


@dataclass(frozen=True)
class AcceptedEvolutionReference:
    relation_kind: str
    relation_event_id: int | None
    relation_reason_text: str | None
    insight_version_id: int
    insight_id: int
    version_no: int
    judgment_id: int
    accepted_at: str
    initial_role: str
    current_role: str
    claim: str
    publication: AcceptedPublicationLink | None


@dataclass(frozen=True)
class AcceptedRenderLineageNode:
    insight_version_id: int
    insight_id: int
    version_no: int
    previous_version_id: int | None
    semantic_signature: str
    dependency_signature: str
    created_at: str
    payload: InsightPayload
    judgment: AcceptedRenderJudgment
    initial_role: str
    current_role: str
    historical_reason: str | None
    historical_at: str | None
    caused_by_event_id: int | None
    caused_by_judgment_id: int | None
    replacement_insight_id: int | None
    primary_cause_accepted: AcceptedEvolutionReference | None
    primary_exit_fact: AcceptedRenderExitFact | None
    additional_exit_facts: tuple[AcceptedRenderExitFact, ...]
    formation_event: AcceptedRenderFormationEvent
    participants: tuple[Participant, ...]
    used_relations: tuple[AcceptedRenderUsedRelation, ...]
    source_leaf_identities: tuple[tuple[int, str], ...]
    existing_publication: AcceptedPublicationLink | None = None


@dataclass(frozen=True)
class AcceptedInsightRenderContext:
    insight_version_id: int
    insight_id: int
    version_no: int
    root: AcceptedRenderLineageNode
    lineage_nodes: tuple[AcceptedRenderLineageNode, ...]
    source_leaves: tuple[AcceptedRenderSourceLeaf, ...]
    evolution_references: tuple[AcceptedEvolutionReference, ...]
    snapshot_fact_ids: tuple[str, ...]
    renderer_contract: str = RENDERER_CONTRACT


@dataclass(frozen=True)
class AcceptedPlacementReceipt:
    codec: str
    machine_identity: str
    relative_path: str
    insight_version_id: int
    judgment_id: int
    placed_at: str
    renderer_contract: str
    render_context_signature: str
    render_context: dict[str, object]
    core_markdown_sha256: str


@dataclass(frozen=True)
class RenderedAcceptedInsight:
    content: bytes
    core_markdown: str
    relative_path: str
    machine_identity: str
    render_context_signature: str
    content_sha256: str
    placement_receipt_json: str
    placement_receipt_comment: str


def accepted_insight_relative_path(insight_id: int, version_no: int) -> str:
    if insight_id <= 0 or version_no <= 0:
        raise ValueError("Accepted insight identity and version must be positive")
    return (
        "知识蒸馏器/新知/"
        f"AI新知--insight-{insight_id}-v{version_no}.md"
    )


def accepted_insight_machine_identity(
    insight_id: int,
    insight_version_id: int,
) -> str:
    if insight_id <= 0 or insight_version_id <= 0:
        raise ValueError("Accepted insight identity and version must be positive")
    return f"accepted-insight:{insight_id}:version:{insight_version_id}"


def render_accepted_insight(
    context: AcceptedInsightRenderContext,
    *,
    placed_at: str,
) -> RenderedAcceptedInsight:
    _validate_context(context)
    if not isinstance(placed_at, str) or not placed_at.strip():
        raise ValueError("Accepted placement time is required")

    context_payload = accepted_render_context_to_dict(context)
    context_signature = accepted_render_context_signature(context)
    relative_path = accepted_insight_relative_path(
        context.insight_id,
        context.version_no,
    )
    machine_identity = accepted_insight_machine_identity(
        context.insight_id,
        context.insight_version_id,
    )
    core_markdown = _render_core_markdown(
        context,
        context_signature=context_signature,
        relative_path=relative_path,
        machine_identity=machine_identity,
    )
    core_hash = _sha256(core_markdown.encode("utf-8"))
    receipt_payload = {
        "codec": PLACEMENT_RECEIPT_CODEC,
        "machine_identity": machine_identity,
        "relative_path": relative_path,
        "insight_version_id": context.insight_version_id,
        "judgment_id": context.root.judgment.judgment_id,
        "placed_at": placed_at,
        "renderer_contract": context.renderer_contract,
        "render_context_signature": context_signature,
        "render_context": context_payload,
        "core_markdown_sha256": core_hash,
    }
    receipt_json = canonical_json(receipt_payload)
    encoded = base64.urlsafe_b64encode(receipt_json.encode("utf-8")).decode(
        "ascii"
    ).rstrip("=")
    receipt_comment = f"<!-- kd_accepted_placement_receipt:{encoded} -->"
    content = f"{core_markdown}{receipt_comment}\n".encode("utf-8")
    return RenderedAcceptedInsight(
        content=content,
        core_markdown=core_markdown,
        relative_path=relative_path,
        machine_identity=machine_identity,
        render_context_signature=context_signature,
        content_sha256=_sha256(content),
        placement_receipt_json=receipt_json,
        placement_receipt_comment=receipt_comment,
    )


def accepted_render_context_signature(
    context: AcceptedInsightRenderContext,
) -> str:
    """Return the canonical signature used by the renderer and publisher."""
    _validate_context(context)
    return _sha256(
        canonical_json(accepted_render_context_to_dict(context)).encode("utf-8")
    )


def decode_accepted_placement_receipt(value: str) -> AcceptedPlacementReceipt:
    try:
        raw = json.loads(value)
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError("Accepted placement receipt is invalid") from error
    if not isinstance(raw, dict) or set(raw) != {
        "codec",
        "machine_identity",
        "relative_path",
        "insight_version_id",
        "judgment_id",
        "placed_at",
        "renderer_contract",
        "render_context_signature",
        "render_context",
        "core_markdown_sha256",
    }:
        raise ValueError("Accepted placement receipt is invalid")
    if canonical_json(raw) != value:
        raise ValueError("Accepted placement receipt is not canonical")
    if raw["codec"] != PLACEMENT_RECEIPT_CODEC:
        raise ValueError("Accepted placement receipt codec is unsupported")
    if raw["renderer_contract"] != RENDERER_CONTRACT:
        raise ValueError("Accepted placement renderer contract is unsupported")
    for key in (
        "machine_identity",
        "relative_path",
        "placed_at",
    ):
        if not isinstance(raw[key], str) or not raw[key].strip():
            raise ValueError("Accepted placement receipt is invalid")
    for key in ("render_context_signature", "core_markdown_sha256"):
        if not _is_sha256(raw[key]):
            raise ValueError("Accepted placement receipt is invalid")
    for key in ("insight_version_id", "judgment_id"):
        if not _positive_plain_int(raw[key]):
            raise ValueError("Accepted placement receipt is invalid")
    if not isinstance(raw["render_context"], dict):
        raise ValueError("Accepted placement receipt is invalid")
    context = accepted_render_context_from_dict(raw["render_context"])
    expected_signature = _sha256(
        canonical_json(raw["render_context"]).encode("utf-8")
    )
    if expected_signature != raw["render_context_signature"]:
        raise ValueError("Accepted placement receipt context signature is invalid")
    if (
        raw["insight_version_id"] != context.insight_version_id
        or raw["judgment_id"] != context.root.judgment.judgment_id
        or raw["renderer_contract"] != context.renderer_contract
        or raw["relative_path"]
        != accepted_insight_relative_path(context.insight_id, context.version_no)
        or raw["machine_identity"]
        != accepted_insight_machine_identity(
            context.insight_id,
            context.insight_version_id,
        )
    ):
        raise ValueError("Accepted placement receipt identity is invalid")
    return AcceptedPlacementReceipt(
        codec=str(raw["codec"]),
        machine_identity=str(raw["machine_identity"]),
        relative_path=str(raw["relative_path"]),
        insight_version_id=int(raw["insight_version_id"]),
        judgment_id=int(raw["judgment_id"]),
        placed_at=str(raw["placed_at"]),
        renderer_contract=str(raw["renderer_contract"]),
        render_context_signature=str(raw["render_context_signature"]),
        render_context=dict(raw["render_context"]),
        core_markdown_sha256=str(raw["core_markdown_sha256"]),
    )


def decode_accepted_placement_receipt_from_content(
    content: bytes,
) -> AcceptedPlacementReceipt:
    if not isinstance(content, bytes):
        raise ValueError("Accepted published content must be bytes")
    prefix = b"<!-- kd_accepted_placement_receipt:"
    suffix = b" -->\n"
    try:
        content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("Accepted published content is not UTF-8") from error
    marker_start = content.rfind(prefix)
    if (
        marker_start <= 0
        or not content.endswith(suffix)
        or content[marker_start - 1 : marker_start] != b"\n"
    ):
        raise ValueError("Accepted placement receipt comment is missing")
    try:
        encoded = content[marker_start + len(prefix) : -len(suffix)].decode(
            "ascii"
        )
    except UnicodeDecodeError as error:
        raise ValueError("Accepted placement receipt comment is invalid") from error
    if (
        not encoded
        or "=" in encoded
        or any(
            character
            not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
            for character in encoded
        )
    ):
        raise ValueError("Accepted placement receipt comment is invalid")
    try:
        receipt_bytes = base64.b64decode(
            encoded + "=" * (-len(encoded) % 4),
            altchars=b"-_",
            validate=True,
        )
        receipt_json = receipt_bytes.decode("utf-8")
    except (ValueError, UnicodeDecodeError, binascii.Error) as error:
        raise ValueError("Accepted placement receipt comment is invalid") from error
    canonical_encoded = base64.urlsafe_b64encode(receipt_bytes).decode("ascii").rstrip(
        "="
    )
    if canonical_encoded != encoded:
        raise ValueError("Accepted placement receipt comment is not canonical")
    receipt = decode_accepted_placement_receipt(receipt_json)
    core_bytes = content[:marker_start]
    if _sha256(core_bytes) != receipt.core_markdown_sha256:
        raise ValueError("Accepted placement receipt core hash is invalid")
    return receipt


def accepted_render_context_to_dict(
    context: AcceptedInsightRenderContext,
) -> dict[str, object]:
    return {
        "codec": "accepted-insight-render-context-v1",
        "renderer_contract": context.renderer_contract,
        "insight_version_id": context.insight_version_id,
        "insight_id": context.insight_id,
        "version_no": context.version_no,
        "root": _lineage_node_to_dict(context.root),
        "lineage_nodes": [
            _lineage_node_to_dict(node) for node in context.lineage_nodes
        ],
        "source_leaves": [
            _source_leaf_to_dict(leaf) for leaf in context.source_leaves
        ],
        "evolution_references": [
            _evolution_reference_to_dict(item)
            for item in context.evolution_references
        ],
        "snapshot_fact_ids": list(context.snapshot_fact_ids),
    }


def accepted_render_context_from_dict(
    value: object,
) -> AcceptedInsightRenderContext:
    """Strictly reconstruct the frozen snapshot embedded in a placement receipt."""
    item = _decode_mapping(
        value,
        {
            "codec",
            "renderer_contract",
            "insight_version_id",
            "insight_id",
            "version_no",
            "root",
            "lineage_nodes",
            "source_leaves",
            "evolution_references",
            "snapshot_fact_ids",
        },
        "Accepted render context",
    )
    if item["codec"] != "accepted-insight-render-context-v1":
        raise ValueError("Accepted render context codec is unsupported")
    context = AcceptedInsightRenderContext(
        renderer_contract=_decode_text(
            item["renderer_contract"],
            "Accepted renderer contract",
        ),
        insight_version_id=_decode_positive_int(
            item["insight_version_id"],
            "Accepted insight version ID",
        ),
        insight_id=_decode_positive_int(
            item["insight_id"],
            "Accepted insight ID",
        ),
        version_no=_decode_positive_int(
            item["version_no"],
            "Accepted insight version number",
        ),
        root=_decode_lineage_node(item["root"]),
        lineage_nodes=tuple(
            _decode_lineage_node(node)
            for node in _decode_list(item["lineage_nodes"], "Accepted lineage")
        ),
        source_leaves=tuple(
            _decode_source_leaf(leaf)
            for leaf in _decode_list(
                item["source_leaves"],
                "Accepted source leaves",
            )
        ),
        evolution_references=tuple(
            _decode_evolution_reference(reference)
            for reference in _decode_list(
                item["evolution_references"],
                "Accepted evolution references",
            )
        ),
        snapshot_fact_ids=tuple(
            _decode_text(fact_id, "Accepted snapshot fact ID")
            for fact_id in _decode_list(
                item["snapshot_fact_ids"],
                "Accepted snapshot fact IDs",
            )
        ),
    )
    _validate_context(context)
    if accepted_render_context_to_dict(context) != value:
        raise ValueError("Accepted render context is not canonical")
    return context


def validate_accepted_insight_render_context(
    context: AcceptedInsightRenderContext,
) -> None:
    _validate_context(context)


def _decode_lineage_node(value: object) -> AcceptedRenderLineageNode:
    item = _decode_mapping(
        value,
        {
            "insight_version_id",
            "insight_id",
            "version_no",
            "previous_version_id",
            "semantic_signature",
            "dependency_signature",
            "created_at",
            "payload",
            "judgment",
            "initial_role",
            "current_role",
            "historical_reason",
            "historical_at",
            "caused_by_event_id",
            "caused_by_judgment_id",
            "replacement_insight_id",
            "primary_cause_accepted",
            "primary_exit_fact",
            "additional_exit_facts",
            "formation_event",
            "participants",
            "used_relations",
            "source_leaf_identities",
            "existing_publication",
        },
        "Accepted lineage node",
    )
    judgment = _decode_mapping(
        item["judgment"],
        {"judgment_id", "decision", "annotation_text", "decided_at"},
        "Accepted judgment",
    )
    formation = _decode_mapping(
        item["formation_event"],
        {"event_id", "started_at", "completed_at"},
        "Accepted formation event",
    )
    primary_cause = item["primary_cause_accepted"]
    primary_exit = item["primary_exit_fact"]
    publication = item["existing_publication"]
    return AcceptedRenderLineageNode(
        insight_version_id=_decode_positive_int(
            item["insight_version_id"],
            "Accepted lineage version ID",
        ),
        insight_id=_decode_positive_int(item["insight_id"], "Accepted lineage ID"),
        version_no=_decode_positive_int(
            item["version_no"],
            "Accepted lineage version number",
        ),
        previous_version_id=_decode_optional_positive_int(
            item["previous_version_id"],
            "Accepted predecessor version ID",
        ),
        semantic_signature=_decode_sha256(
            item["semantic_signature"],
            "Accepted semantic signature",
        ),
        dependency_signature=_decode_sha256(
            item["dependency_signature"],
            "Accepted dependency signature",
        ),
        created_at=_decode_text(item["created_at"], "Accepted creation time"),
        payload=parse_insight_payload(item["payload"]),
        judgment=AcceptedRenderJudgment(
            judgment_id=_decode_positive_int(
                judgment["judgment_id"],
                "Accepted judgment ID",
            ),
            decision=_decode_text(judgment["decision"], "Accepted judgment decision"),
            annotation_text=_decode_optional_text(
                judgment["annotation_text"],
                "Accepted annotation",
            ),
            decided_at=_decode_text(
                judgment["decided_at"],
                "Accepted judgment time",
            ),
        ),
        initial_role=_decode_role(item["initial_role"]),
        current_role=_decode_role(item["current_role"]),
        historical_reason=_decode_optional_text(
            item["historical_reason"],
            "Accepted historical reason",
        ),
        historical_at=_decode_optional_text(
            item["historical_at"],
            "Accepted historical time",
        ),
        caused_by_event_id=_decode_optional_positive_int(
            item["caused_by_event_id"],
            "Accepted cause event ID",
        ),
        caused_by_judgment_id=_decode_optional_positive_int(
            item["caused_by_judgment_id"],
            "Accepted cause judgment ID",
        ),
        replacement_insight_id=_decode_optional_positive_int(
            item["replacement_insight_id"],
            "Accepted replacement insight ID",
        ),
        primary_cause_accepted=(
            _decode_evolution_reference(primary_cause)
            if primary_cause is not None
            else None
        ),
        primary_exit_fact=(
            _decode_exit_fact(primary_exit)
            if primary_exit is not None
            else None
        ),
        additional_exit_facts=tuple(
            _decode_exit_fact(fact)
            for fact in _decode_list(
                item["additional_exit_facts"],
                "Accepted additional exit facts",
            )
        ),
        formation_event=AcceptedRenderFormationEvent(
            event_id=_decode_positive_int(
                formation["event_id"],
                "Accepted formation event ID",
            ),
            started_at=_decode_text(
                formation["started_at"],
                "Accepted formation start time",
            ),
            completed_at=_decode_text(
                formation["completed_at"],
                "Accepted formation completion time",
            ),
        ),
        participants=tuple(
            _decode_participant(participant)
            for participant in _decode_list(
                item["participants"],
                "Accepted participants",
            )
        ),
        used_relations=tuple(
            _decode_used_relation(relation)
            for relation in _decode_list(
                item["used_relations"],
                "Accepted used relations",
            )
        ),
        source_leaf_identities=tuple(
            _decode_source_identity(identity)
            for identity in _decode_list(
                item["source_leaf_identities"],
                "Accepted source identities",
            )
        ),
        existing_publication=(
            _decode_publication(publication)
            if publication is not None
            else None
        ),
    )


def _decode_exit_fact(value: object) -> AcceptedRenderExitFact:
    item = _decode_mapping(
        value,
        {
            "fact_kind",
            "event_id",
            "reason_text",
            "created_at",
            "replacement_insight_id",
        },
        "Accepted exit fact",
    )
    return AcceptedRenderExitFact(
        fact_kind=_decode_text(item["fact_kind"], "Accepted exit fact kind"),
        event_id=_decode_positive_int(item["event_id"], "Accepted exit event ID"),
        reason_text=_decode_text(item["reason_text"], "Accepted exit reason"),
        created_at=_decode_text(item["created_at"], "Accepted exit fact time"),
        replacement_insight_id=_decode_optional_positive_int(
            item["replacement_insight_id"],
            "Accepted exit replacement insight ID",
        ),
    )


def _decode_participant(value: object) -> Participant:
    base_keys = {
        "participant_key",
        "input_kind",
        "position",
        "contribution_text",
    }
    if type(value) is not dict:
        raise ValueError("Accepted participant is invalid")
    raw_kind = value.get("input_kind")
    try:
        input_kind = InputKind(raw_kind)
    except (TypeError, ValueError) as error:
        raise ValueError("Accepted participant kind is invalid") from error
    expected = (
        base_keys | {"knowledge_result_id", "point_id"}
        if input_kind is InputKind.SOURCE_KNOWLEDGE
        else base_keys | {"accepted_insight_version_id"}
    )
    item = _decode_mapping(value, expected, "Accepted participant")
    return Participant(
        participant_key=_decode_text(
            item["participant_key"],
            "Accepted participant key",
        ),
        input_kind=input_kind,
        position=_decode_nonnegative_int(
            item["position"],
            "Accepted participant position",
        ),
        contribution_text=_decode_text(
            item["contribution_text"],
            "Accepted participant contribution",
        ),
        knowledge_result_id=(
            _decode_positive_int(
                item["knowledge_result_id"],
                "Accepted participant KnowledgeResult ID",
            )
            if input_kind is InputKind.SOURCE_KNOWLEDGE
            else None
        ),
        point_id=(
            _decode_text(item["point_id"], "Accepted participant point ID")
            if input_kind is InputKind.SOURCE_KNOWLEDGE
            else None
        ),
        accepted_insight_version_id=(
            _decode_positive_int(
                item["accepted_insight_version_id"],
                "Accepted participant version ID",
            )
            if input_kind is InputKind.ACCEPTED_INSIGHT
            else None
        ),
    )


def _decode_used_relation(value: object) -> AcceptedRenderUsedRelation:
    item = _decode_mapping(
        value,
        {
            "relation_version_id",
            "relation_id",
            "version_no",
            "produced_event_id",
            "position",
            "role_text",
            "payload",
            "semantic_signature",
            "dependency_signature",
        },
        "Accepted used relation",
    )
    return AcceptedRenderUsedRelation(
        relation_version_id=_decode_positive_int(
            item["relation_version_id"],
            "Accepted relation version ID",
        ),
        relation_id=_decode_positive_int(
            item["relation_id"],
            "Accepted relation ID",
        ),
        version_no=_decode_positive_int(
            item["version_no"],
            "Accepted relation version number",
        ),
        produced_event_id=_decode_positive_int(
            item["produced_event_id"],
            "Accepted relation formation event ID",
        ),
        position=_decode_nonnegative_int(
            item["position"],
            "Accepted relation position",
        ),
        role_text=_decode_text(item["role_text"], "Accepted relation role"),
        payload=parse_relation_payload(item["payload"]),
        semantic_signature=_decode_sha256(
            item["semantic_signature"],
            "Accepted relation semantic signature",
        ),
        dependency_signature=_decode_sha256(
            item["dependency_signature"],
            "Accepted relation dependency signature",
        ),
    )


def _decode_source_identity(value: object) -> tuple[int, str]:
    item = _decode_list(value, "Accepted source identity")
    if len(item) != 2:
        raise ValueError("Accepted source identity is invalid")
    return (
        _decode_positive_int(item[0], "Accepted source KnowledgeResult ID"),
        _decode_text(item[1], "Accepted source point ID"),
    )


def _decode_source_leaf(value: object) -> AcceptedRenderSourceLeaf:
    item = _decode_mapping(
        value,
        {"material", "knowledge_result", "source_fact", "point", "source_label"},
        "Accepted source leaf",
    )
    material = _decode_mapping(
        item["material"],
        {
            "material_id",
            "platform",
            "platform_item_id",
            "original_url",
            "canonical_url",
            "created_at",
        },
        "Accepted Material",
    )
    result = _decode_mapping(
        item["knowledge_result"],
        {
            "knowledge_result_id",
            "source_fact_id",
            "created_at",
            "payload_json",
            "invalidated_at",
            "invalidation_reason",
            "published_at",
            "published_path",
            "title",
            "summary",
        },
        "Accepted KnowledgeResult",
    )
    source_fact = _decode_mapping(
        item["source_fact"],
        {
            "source_fact_id",
            "material_id",
            "metadata_json",
            "content_snapshot",
            "uncertainty_json",
            "replaces_source_fact_id",
            "change_reason",
            "created_at",
        },
        "Accepted SourceFact",
    )
    point = _decode_mapping(
        item["point"],
        {"point_id", "role", "statement", "argument", "evidences"},
        "Accepted source point",
    )
    evidences = tuple(
        _decode_evidence(evidence)
        for evidence in _decode_list(
            point["evidences"],
            "Accepted source evidences",
        )
    )
    return AcceptedRenderSourceLeaf(
        material_id=_decode_positive_int(
            material["material_id"],
            "Accepted Material ID",
        ),
        platform=_decode_text(material["platform"], "Accepted Material platform"),
        platform_item_id=_decode_text(
            material["platform_item_id"],
            "Accepted Material platform item ID",
        ),
        original_url=_decode_text(
            material["original_url"],
            "Accepted Material URL",
        ),
        canonical_url=_decode_optional_text(
            material["canonical_url"],
            "Accepted Material canonical URL",
        ),
        material_created_at=_decode_text(
            material["created_at"],
            "Accepted Material creation time",
        ),
        knowledge_result_id=_decode_positive_int(
            result["knowledge_result_id"],
            "Accepted KnowledgeResult ID",
        ),
        knowledge_result_created_at=_decode_text(
            result["created_at"],
            "Accepted KnowledgeResult creation time",
        ),
        knowledge_payload_json=_decode_text(
            result["payload_json"],
            "Accepted KnowledgeResult payload",
        ),
        invalidated_at=_decode_optional_text(
            result["invalidated_at"],
            "Accepted KnowledgeResult invalidation time",
        ),
        invalidation_reason=_decode_optional_text(
            result["invalidation_reason"],
            "Accepted KnowledgeResult invalidation reason",
        ),
        published_at=_decode_text(
            result["published_at"],
            "Accepted source publication time",
        ),
        published_path=_decode_text(
            result["published_path"],
            "Accepted source publication path",
        ),
        source_fact_id=_decode_positive_int(
            result["source_fact_id"],
            "Accepted KnowledgeResult SourceFact ID",
        ),
        source_metadata_json=_decode_text(
            source_fact["metadata_json"],
            "Accepted SourceFact metadata",
        ),
        content_snapshot=_decode_text(
            source_fact["content_snapshot"],
            "Accepted SourceFact snapshot",
        ),
        uncertainty_json=_decode_text(
            source_fact["uncertainty_json"],
            "Accepted SourceFact uncertainty",
        ),
        replaces_source_fact_id=_decode_optional_positive_int(
            source_fact["replaces_source_fact_id"],
            "Accepted replaced SourceFact ID",
        ),
        source_change_reason=_decode_optional_text(
            source_fact["change_reason"],
            "Accepted SourceFact change reason",
        ),
        source_fact_created_at=_decode_text(
            source_fact["created_at"],
            "Accepted SourceFact creation time",
        ),
        point_id=_decode_text(point["point_id"], "Accepted source point ID"),
        point_role=_decode_text(point["role"], "Accepted source point role"),
        point_statement=_decode_text(
            point["statement"],
            "Accepted source point statement",
        ),
        point_argument=_decode_text(
            point["argument"],
            "Accepted source point argument",
        ),
        evidences=evidences,
        knowledge_title=_decode_text(result["title"], "Accepted knowledge title"),
        knowledge_summary=_decode_text(
            result["summary"],
            "Accepted knowledge summary",
        ),
        source_label=_decode_text(item["source_label"], "Accepted source label"),
    )


def _decode_evidence(value: object) -> AcceptedRenderEvidence:
    item = _decode_mapping(
        value,
        {
            "evidence_id",
            "source_fact_id",
            "start_offset",
            "end_offset",
            "evidence_text",
        },
        "Accepted evidence",
    )
    return AcceptedRenderEvidence(
        evidence_id=_decode_text(item["evidence_id"], "Accepted evidence ID"),
        source_fact_id=_decode_positive_int(
            item["source_fact_id"],
            "Accepted evidence SourceFact ID",
        ),
        start_offset=_decode_nonnegative_int(
            item["start_offset"],
            "Accepted evidence start offset",
        ),
        end_offset=_decode_positive_int(
            item["end_offset"],
            "Accepted evidence end offset",
        ),
        evidence_text=_decode_text(
            item["evidence_text"],
            "Accepted evidence text",
        ),
    )


def _decode_publication(value: object) -> AcceptedPublicationLink:
    item = _decode_mapping(
        value,
        {
            "publication_id",
            "insight_version_id",
            "insight_id",
            "version_no",
            "claim",
            "relative_path",
            "machine_identity",
        },
        "Accepted publication navigation",
    )
    return AcceptedPublicationLink(
        publication_id=_decode_positive_int(
            item["publication_id"],
            "Accepted publication ID",
        ),
        insight_version_id=_decode_positive_int(
            item["insight_version_id"],
            "Accepted publication version ID",
        ),
        insight_id=_decode_positive_int(
            item["insight_id"],
            "Accepted publication insight ID",
        ),
        version_no=_decode_positive_int(
            item["version_no"],
            "Accepted publication version number",
        ),
        claim=_decode_text(item["claim"], "Accepted publication claim"),
        relative_path=_decode_text(
            item["relative_path"],
            "Accepted publication path",
        ),
        machine_identity=_decode_text(
            item["machine_identity"],
            "Accepted publication machine identity",
        ),
    )


def _decode_evolution_reference(value: object) -> AcceptedEvolutionReference:
    item = _decode_mapping(
        value,
        {
            "relation_kind",
            "relation_event_id",
            "relation_reason_text",
            "insight_version_id",
            "insight_id",
            "version_no",
            "judgment_id",
            "accepted_at",
            "initial_role",
            "current_role",
            "claim",
            "publication",
        },
        "Accepted evolution reference",
    )
    publication = item["publication"]
    return AcceptedEvolutionReference(
        relation_kind=_decode_text(
            item["relation_kind"],
            "Accepted evolution relation kind",
        ),
        relation_event_id=_decode_optional_positive_int(
            item["relation_event_id"],
            "Accepted evolution event ID",
        ),
        relation_reason_text=_decode_optional_text(
            item["relation_reason_text"],
            "Accepted evolution reason",
        ),
        insight_version_id=_decode_positive_int(
            item["insight_version_id"],
            "Accepted evolution version ID",
        ),
        insight_id=_decode_positive_int(
            item["insight_id"],
            "Accepted evolution insight ID",
        ),
        version_no=_decode_positive_int(
            item["version_no"],
            "Accepted evolution version number",
        ),
        judgment_id=_decode_positive_int(
            item["judgment_id"],
            "Accepted evolution judgment ID",
        ),
        accepted_at=_decode_text(
            item["accepted_at"],
            "Accepted evolution acceptance time",
        ),
        initial_role=_decode_role(item["initial_role"]),
        current_role=_decode_role(item["current_role"]),
        claim=_decode_text(item["claim"], "Accepted evolution claim"),
        publication=(
            _decode_publication(publication)
            if publication is not None
            else None
        ),
    )


def _decode_mapping(
    value: object,
    expected_keys: set[str],
    label: str,
) -> dict[str, object]:
    if type(value) is not dict or set(value) != expected_keys:
        raise ValueError(f"{label} is invalid")
    return value


def _decode_list(value: object, label: str) -> list[object]:
    if type(value) is not list:
        raise ValueError(f"{label} is invalid")
    return value


def _decode_text(value: object, label: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{label} is invalid")
    return value


def _decode_optional_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _decode_text(value, label)


def _decode_positive_int(value: object, label: str) -> int:
    if not _positive_plain_int(value):
        raise ValueError(f"{label} is invalid")
    return value


def _decode_optional_positive_int(value: object, label: str) -> int | None:
    if value is None:
        return None
    return _decode_positive_int(value, label)


def _decode_nonnegative_int(value: object, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} is invalid")
    return value


def _decode_sha256(value: object, label: str) -> str:
    if not _is_sha256(value):
        raise ValueError(f"{label} is invalid")
    return value


def _decode_role(value: object) -> str:
    role = _decode_text(value, "Accepted role")
    if role not in {"current", "historical"}:
        raise ValueError("Accepted role is invalid")
    return role


def _lineage_node_to_dict(node: AcceptedRenderLineageNode) -> dict[str, object]:
    return {
        "insight_version_id": node.insight_version_id,
        "insight_id": node.insight_id,
        "version_no": node.version_no,
        "previous_version_id": node.previous_version_id,
        "semantic_signature": node.semantic_signature,
        "dependency_signature": node.dependency_signature,
        "created_at": node.created_at,
        "payload": insight_payload_to_dict(node.payload),
        "judgment": {
            "judgment_id": node.judgment.judgment_id,
            "decision": node.judgment.decision,
            "annotation_text": node.judgment.annotation_text,
            "decided_at": node.judgment.decided_at,
        },
        "initial_role": node.initial_role,
        "current_role": node.current_role,
        "historical_reason": node.historical_reason,
        "historical_at": node.historical_at,
        "caused_by_event_id": node.caused_by_event_id,
        "caused_by_judgment_id": node.caused_by_judgment_id,
        "replacement_insight_id": node.replacement_insight_id,
        "primary_cause_accepted": (
            _evolution_reference_to_dict(node.primary_cause_accepted)
            if node.primary_cause_accepted is not None
            else None
        ),
        "primary_exit_fact": (
            {
                "fact_kind": node.primary_exit_fact.fact_kind,
                "event_id": node.primary_exit_fact.event_id,
                "reason_text": node.primary_exit_fact.reason_text,
                "created_at": node.primary_exit_fact.created_at,
                "replacement_insight_id": (
                    node.primary_exit_fact.replacement_insight_id
                ),
            }
            if node.primary_exit_fact is not None
            else None
        ),
        "additional_exit_facts": [
            {
                "fact_kind": item.fact_kind,
                "event_id": item.event_id,
                "reason_text": item.reason_text,
                "created_at": item.created_at,
                "replacement_insight_id": item.replacement_insight_id,
            }
            for item in node.additional_exit_facts
        ],
        "formation_event": {
            "event_id": node.formation_event.event_id,
            "started_at": node.formation_event.started_at,
            "completed_at": node.formation_event.completed_at,
        },
        "participants": [_participant_to_dict(item) for item in node.participants],
        "used_relations": [
            {
                "relation_version_id": item.relation_version_id,
                "relation_id": item.relation_id,
                "version_no": item.version_no,
                "produced_event_id": item.produced_event_id,
                "position": item.position,
                "role_text": item.role_text,
                "payload": relation_payload_to_dict(item.payload),
                "semantic_signature": item.semantic_signature,
                "dependency_signature": item.dependency_signature,
            }
            for item in node.used_relations
        ],
        "source_leaf_identities": [
            [knowledge_result_id, point_id]
            for knowledge_result_id, point_id in node.source_leaf_identities
        ],
        "existing_publication": (
            _publication_to_dict(node.existing_publication)
            if node.existing_publication is not None
            else None
        ),
    }


def _participant_to_dict(item: Participant) -> dict[str, object]:
    value: dict[str, object] = {
        "participant_key": item.participant_key,
        "input_kind": item.input_kind.value,
        "position": item.position,
        "contribution_text": item.contribution_text,
    }
    if item.input_kind is InputKind.SOURCE_KNOWLEDGE:
        value.update(
            knowledge_result_id=item.knowledge_result_id,
            point_id=item.point_id,
        )
    else:
        value["accepted_insight_version_id"] = item.accepted_insight_version_id
    return value


def _source_leaf_to_dict(leaf: AcceptedRenderSourceLeaf) -> dict[str, object]:
    return {
        "material": {
            "material_id": leaf.material_id,
            "platform": leaf.platform,
            "platform_item_id": leaf.platform_item_id,
            "original_url": leaf.original_url,
            "canonical_url": leaf.canonical_url,
            "created_at": leaf.material_created_at,
        },
        "knowledge_result": {
            "knowledge_result_id": leaf.knowledge_result_id,
            "source_fact_id": leaf.source_fact_id,
            "created_at": leaf.knowledge_result_created_at,
            "payload_json": leaf.knowledge_payload_json,
            "invalidated_at": leaf.invalidated_at,
            "invalidation_reason": leaf.invalidation_reason,
            "published_at": leaf.published_at,
            "published_path": leaf.published_path,
            "title": leaf.knowledge_title,
            "summary": leaf.knowledge_summary,
        },
        "source_fact": {
            "source_fact_id": leaf.source_fact_id,
            "material_id": leaf.material_id,
            "metadata_json": leaf.source_metadata_json,
            "content_snapshot": leaf.content_snapshot,
            "uncertainty_json": leaf.uncertainty_json,
            "replaces_source_fact_id": leaf.replaces_source_fact_id,
            "change_reason": leaf.source_change_reason,
            "created_at": leaf.source_fact_created_at,
        },
        "point": {
            "point_id": leaf.point_id,
            "role": leaf.point_role,
            "statement": leaf.point_statement,
            "argument": leaf.point_argument,
            "evidences": [
                {
                    "evidence_id": item.evidence_id,
                    "source_fact_id": item.source_fact_id,
                    "start_offset": item.start_offset,
                    "end_offset": item.end_offset,
                    "evidence_text": item.evidence_text,
                }
                for item in leaf.evidences
            ],
        },
        "source_label": leaf.source_label,
    }


def _publication_to_dict(item: AcceptedPublicationLink) -> dict[str, object]:
    return {
        "publication_id": item.publication_id,
        "insight_version_id": item.insight_version_id,
        "insight_id": item.insight_id,
        "version_no": item.version_no,
        "claim": item.claim,
        "relative_path": item.relative_path,
        "machine_identity": item.machine_identity,
    }


def _evolution_reference_to_dict(
    item: AcceptedEvolutionReference,
) -> dict[str, object]:
    return {
        "relation_kind": item.relation_kind,
        "relation_event_id": item.relation_event_id,
        "relation_reason_text": item.relation_reason_text,
        "insight_version_id": item.insight_version_id,
        "insight_id": item.insight_id,
        "version_no": item.version_no,
        "judgment_id": item.judgment_id,
        "accepted_at": item.accepted_at,
        "initial_role": item.initial_role,
        "current_role": item.current_role,
        "claim": item.claim,
        "publication": (
            _publication_to_dict(item.publication)
            if item.publication is not None
            else None
        ),
    }


def _validate_context(context: AcceptedInsightRenderContext) -> None:
    if context.renderer_contract != RENDERER_CONTRACT:
        raise ValueError("Accepted renderer contract is unsupported")
    if (
        context.insight_version_id <= 0
        or context.insight_id <= 0
        or context.version_no <= 0
    ):
        raise ValueError("Accepted render root identity is invalid")
    if (
        context.root.insight_version_id != context.insight_version_id
        or context.root.insight_id != context.insight_id
        or context.root.version_no != context.version_no
    ):
        raise ValueError("Accepted render root identity does not match context")
    if not context.lineage_nodes or context.lineage_nodes[0] != context.root:
        raise ValueError("Accepted render lineage must start at the root")

    nodes = {node.insight_version_id: node for node in context.lineage_nodes}
    if len(nodes) != len(context.lineage_nodes):
        raise ValueError("Accepted render lineage contains duplicate nodes")
    sources = {
        (leaf.knowledge_result_id, leaf.point_id): leaf
        for leaf in context.source_leaves
    }
    if len(sources) != len(context.source_leaves):
        raise ValueError("Accepted render context contains duplicate source leaves")
    if tuple(sorted(sources)) != tuple(sources):
        raise ValueError("Accepted render source leaves are not deterministic")

    for node in context.lineage_nodes:
        _validate_lineage_node(node)
    for leaf in context.source_leaves:
        _validate_source_leaf(leaf)

    reachable: set[int] = set()
    traversal_order: list[int] = []
    visiting: set[int] = set()

    def walk(version_id: int) -> set[tuple[int, str]]:
        if version_id in visiting:
            raise ValueError("Accepted render lineage contains a cycle")
        if version_id in reachable:
            return set(nodes[version_id].source_leaf_identities)
        node = nodes.get(version_id)
        if node is None:
            raise ValueError("Accepted render lineage is broken")
        visiting.add(version_id)
        traversal_order.append(version_id)
        calculated: set[tuple[int, str]] = set()
        for participant in node.participants:
            if participant.input_kind is InputKind.SOURCE_KNOWLEDGE:
                identity = (
                    participant.knowledge_result_id or 0,
                    participant.point_id or "",
                )
                if identity not in sources:
                    raise ValueError("Accepted render source handoff is missing")
                calculated.add(identity)
            else:
                calculated.update(
                    walk(participant.accepted_insight_version_id or 0)
                )
        visiting.remove(version_id)
        if tuple(sorted(calculated)) != node.source_leaf_identities:
            raise ValueError("Accepted render lineage leaf closure is invalid")
        reachable.add(version_id)
        return calculated

    root_leaves = walk(context.insight_version_id)
    if reachable != set(nodes) or root_leaves != set(sources):
        raise ValueError("Accepted render context contains partial lineage")
    if tuple(traversal_order) != tuple(
        node.insight_version_id for node in context.lineage_nodes
    ):
        raise ValueError("Accepted render lineage order is not deterministic")

    if len(set(context.snapshot_fact_ids)) != len(context.snapshot_fact_ids):
        raise ValueError("Accepted render snapshot fact IDs are duplicated")
    if tuple(sorted(context.snapshot_fact_ids)) != context.snapshot_fact_ids:
        raise ValueError("Accepted render snapshot fact IDs are not deterministic")
    for reference in context.evolution_references:
        _validate_evolution_reference(reference)
        if reference.relation_kind == "historical_cause":
            raise ValueError("Accepted context evolution reference kind is invalid")
    reference_keys = tuple(
        (
            reference.relation_kind,
            reference.insight_id,
            reference.version_no,
            reference.insight_version_id,
        )
        for reference in context.evolution_references
    )
    if len(set(reference_keys)) != len(reference_keys):
        raise ValueError("Accepted evolution references are duplicated")
    if tuple(sorted(reference_keys)) != reference_keys:
        raise ValueError("Accepted evolution references are not deterministic")
    direct_predecessors = tuple(
        reference
        for reference in context.evolution_references
        if reference.relation_kind == "direct_predecessor"
    )
    if (
        (context.root.previous_version_id is not None)
        != (len(direct_predecessors) == 1)
    ) or len(direct_predecessors) > 1 or (
        direct_predecessors
        and (
            direct_predecessors[0].insight_version_id
            != context.root.previous_version_id
            or direct_predecessors[0].insight_id != context.insight_id
            or direct_predecessors[0].version_no != context.version_no - 1
        )
    ):
        raise ValueError("Accepted direct predecessor reference is invalid")
    for reference in context.evolution_references:
        if (
            reference.relation_kind == "replaced_identity"
            and reference.insight_id == context.insight_id
        ):
            raise ValueError("Accepted replacement cannot replace itself")
    if context.snapshot_fact_ids != _expected_snapshot_fact_ids(context):
        raise ValueError("Accepted render snapshot fact closure is invalid")


def _validate_lineage_node(node: AcceptedRenderLineageNode) -> None:
    if node.judgment.decision != "interesting":
        raise ValueError("Only interesting accepted versions can be rendered")
    if semantic_signature(node.payload) != node.semantic_signature:
        raise ValueError("Accepted render semantic signature is invalid")
    if not _is_sha256(node.dependency_signature) or not node.created_at:
        raise ValueError("Accepted render version anchor is invalid")
    if node.version_no == 1 and node.previous_version_id is not None:
        raise ValueError("Accepted render version lineage is invalid")
    if node.initial_role not in {"current", "historical"}:
        raise ValueError("Accepted initial role is invalid")
    if node.current_role not in {"current", "historical"}:
        raise ValueError("Accepted current role is invalid")
    if node.initial_role == "historical" and node.current_role != "historical":
        raise ValueError("Accepted historical version cannot be current")
    if node.current_role == "current":
        if any(
            value is not None
            for value in (
                node.historical_reason,
                node.historical_at,
                node.caused_by_event_id,
                node.caused_by_judgment_id,
                node.replacement_insight_id,
                node.primary_cause_accepted,
                node.primary_exit_fact,
            )
        ) or node.additional_exit_facts:
            raise ValueError("Accepted current version has historical fields")
    elif not node.historical_reason or not node.historical_at:
        raise ValueError("Accepted historical version lacks its primary cause")
    if node.caused_by_event_id is not None:
        if (
            node.primary_exit_fact is None
            or node.primary_exit_fact.event_id != node.caused_by_event_id
            or node.primary_exit_fact.fact_kind != node.historical_reason
            or node.primary_exit_fact.replacement_insight_id
            != node.replacement_insight_id
        ):
            raise ValueError("Accepted historical primary exit fact is invalid")
    elif node.primary_exit_fact is not None:
        raise ValueError("Accepted historical primary exit fact is unexpected")
    if node.caused_by_judgment_id is not None:
        if (
            node.primary_cause_accepted is None
            or node.primary_cause_accepted.judgment_id
            != node.caused_by_judgment_id
            or node.primary_cause_accepted.relation_kind != "historical_cause"
            or node.primary_cause_accepted.insight_id != node.insight_id
            or node.primary_cause_accepted.version_no <= node.version_no
        ):
            raise ValueError("Accepted historical judgment cause is invalid")
        _validate_evolution_reference(node.primary_cause_accepted)
    elif node.primary_cause_accepted is not None:
        raise ValueError("Accepted historical judgment cause is unexpected")
    judgment_causes = {
        "newer_accepted_current",
        "born_older_than_current",
        "born_after_newer_ever_current",
    }
    event_causes = {"basis_invalid", "refuted", "identity_replaced"}
    if node.current_role == "historical":
        if node.historical_reason in judgment_causes:
            if (
                node.caused_by_judgment_id is None
                or node.caused_by_event_id is not None
                or node.replacement_insight_id is not None
                or node.primary_cause_accepted is None
                or node.primary_cause_accepted.initial_role != "current"
                or (
                    node.historical_reason == "newer_accepted_current"
                    and node.initial_role != "current"
                )
                or (
                    node.historical_reason
                    in {
                        "born_older_than_current",
                        "born_after_newer_ever_current",
                    }
                    and node.initial_role != "historical"
                )
                or (
                    node.historical_reason == "born_after_newer_ever_current"
                    and node.primary_cause_accepted.current_role != "historical"
                )
            ):
                raise ValueError("Accepted historical judgment semantics are invalid")
        elif node.historical_reason in event_causes:
            if (
                node.caused_by_event_id is None
                or node.caused_by_judgment_id is not None
                or (
                    (node.historical_reason == "identity_replaced")
                    != (node.replacement_insight_id is not None)
                )
            ):
                raise ValueError("Accepted historical event semantics are invalid")
        else:
            raise ValueError("Accepted historical reason is invalid")
    exit_facts = (
        *((node.primary_exit_fact,) if node.primary_exit_fact is not None else ()),
        *node.additional_exit_facts,
    )
    for fact in exit_facts:
        _validate_exit_fact(fact)
    if node.primary_exit_fact is not None:
        _validate_event_primary_closure(
            node.primary_exit_fact,
            node.additional_exit_facts,
        )
    exit_keys = tuple(
        (
            fact.event_id,
            {"refuted": 0, "basis_invalid": 1, "identity_replaced": 2}[
                fact.fact_kind
            ],
        )
        for fact in node.additional_exit_facts
    )
    if len(set(exit_keys)) != len(exit_keys) or tuple(sorted(exit_keys)) != exit_keys:
        raise ValueError("Accepted additional exit facts are not deterministic")
    if (
        node.formation_event.event_id <= 0
        or not node.formation_event.started_at
        or not node.formation_event.completed_at
    ):
        raise ValueError("Accepted formation event is incomplete")
    if len(node.participants) < 2:
        raise ValueError("Accepted render lineage is incomplete")
    for position, participant in enumerate(node.participants):
        if participant.position != position:
            raise ValueError("Accepted participant positions are not dense")
        if participant.input_kind is InputKind.SOURCE_KNOWLEDGE:
            if not participant.knowledge_result_id or not participant.point_id:
                raise ValueError("Accepted source participant identity is invalid")
        elif not participant.accepted_insight_version_id:
            raise ValueError("Accepted participant identity is invalid")
    for position, relation in enumerate(node.used_relations):
        if relation.position != position:
            raise ValueError("Accepted used relation positions are not dense")
        if semantic_signature(relation.payload) != relation.semantic_signature:
            raise ValueError("Accepted used relation semantic signature is invalid")
        if not _is_sha256(relation.dependency_signature):
            raise ValueError("Accepted used relation dependency is invalid")
    if tuple(sorted(set(node.source_leaf_identities))) != node.source_leaf_identities:
        raise ValueError("Accepted lineage source leaves are not deterministic")
    if node.existing_publication is not None:
        _validate_publication(node.existing_publication)
        if node.existing_publication.insight_version_id != node.insight_version_id:
            raise ValueError("Accepted lineage publication points to another version")


def _validate_exit_fact(fact: AcceptedRenderExitFact) -> None:
    if (
        fact.fact_kind not in {"basis_invalid", "refuted", "identity_replaced"}
        or fact.event_id <= 0
        or not fact.reason_text
        or not fact.created_at
        or (
            (fact.fact_kind == "identity_replaced")
            != (fact.replacement_insight_id is not None)
        )
        or (
            fact.replacement_insight_id is not None
            and fact.replacement_insight_id <= 0
        )
    ):
        raise ValueError("Accepted exit fact is invalid")


def _validate_event_primary_closure(
    primary: AcceptedRenderExitFact,
    additional: tuple[AcceptedRenderExitFact, ...],
) -> None:
    if any(
        fact.event_id == primary.event_id
        and fact.fact_kind == primary.fact_kind
        for fact in additional
    ):
        raise ValueError("Accepted event primary is duplicated")
    if any(fact.event_id < primary.event_id for fact in additional):
        raise ValueError("Accepted additional exit fact predates its primary")
    same_event_kinds = {
        primary.fact_kind,
        *(
            fact.fact_kind
            for fact in additional
            if fact.event_id == primary.event_id
        ),
    }
    if "identity_replaced" in same_event_kinds and len(same_event_kinds) > 1:
        raise ValueError("Accepted same-event exit causes are ambiguous")
    if "refuted" in same_event_kinds and primary.fact_kind != "refuted":
        raise ValueError("Accepted same-event refutation must be primary")


def _validate_source_leaf(leaf: AcceptedRenderSourceLeaf) -> None:
    if min(
        leaf.material_id,
        leaf.knowledge_result_id,
        leaf.source_fact_id,
    ) <= 0:
        raise ValueError("Accepted source identity is invalid")
    for value in (
        leaf.platform,
        leaf.platform_item_id,
        leaf.original_url,
        leaf.material_created_at,
        leaf.knowledge_result_created_at,
        leaf.knowledge_payload_json,
        leaf.published_at,
        leaf.source_metadata_json,
        leaf.content_snapshot,
        leaf.uncertainty_json,
        leaf.source_fact_created_at,
        leaf.point_id,
        leaf.point_role,
        leaf.point_statement,
        leaf.point_argument,
        leaf.knowledge_title,
        leaf.knowledge_summary,
        leaf.source_label,
    ):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Accepted source asset identity is incomplete")
    _validate_relative_markdown_path(leaf.published_path)
    if not leaf.evidences:
        raise ValueError("Accepted source point has no evidence")
    try:
        knowledge_payload = json.loads(leaf.knowledge_payload_json)
        source_metadata = json.loads(leaf.source_metadata_json)
        uncertainties = json.loads(leaf.uncertainty_json)
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError("Accepted source formal facts are invalid") from error
    if (
        type(knowledge_payload) is not dict
        or type(source_metadata) is not dict
        or type(uncertainties) is not list
    ):
        raise ValueError("Accepted source formal facts are invalid")
    candidate = knowledge_candidate_from_payload(
        leaf.source_fact_id,
        leaf.content_snapshot,
        knowledge_payload,
    )
    points = candidate.core_points + candidate.other_points
    point = next(
        (item for item in points if item.point_id == leaf.point_id),
        None,
    )
    if (
        point is None
        or leaf.point_role
        != ("core" if point in candidate.core_points else "other")
        or point.statement != leaf.point_statement
        or point.argument != leaf.point_argument
        or candidate.title != leaf.knowledge_title
        or candidate.summary != leaf.knowledge_summary
        or tuple(item.evidence_id for item in leaf.evidences)
        != point.evidence_ids
    ):
        raise ValueError("Accepted KnowledgeResult source handoff is invalid")
    candidate_evidence = {
        evidence.evidence_id: evidence for evidence in candidate.evidence_registry
    }
    evidence_ids = set()
    blocks = _source_blocks(leaf.content_snapshot)
    for evidence in leaf.evidences:
        if evidence.evidence_id in evidence_ids:
            raise ValueError("Accepted source evidence is duplicated")
        evidence_ids.add(evidence.evidence_id)
        if evidence.source_fact_id != leaf.source_fact_id:
            raise ValueError("Accepted source evidence belongs to another SourceFact")
        if candidate_evidence.get(evidence.evidence_id) != KnowledgeEvidence(
            evidence.evidence_id,
            evidence.source_fact_id,
            evidence.start_offset,
            evidence.end_offset,
            evidence.evidence_text,
        ):
            raise ValueError("Accepted source evidence handoff is invalid")
        if (
            evidence.start_offset < 0
            or evidence.end_offset <= evidence.start_offset
            or evidence.end_offset > len(leaf.content_snapshot)
            or leaf.content_snapshot[
                evidence.start_offset : evidence.end_offset
            ]
            != evidence.evidence_text
        ):
            raise ValueError("Accepted source evidence locator is invalid")
        _target_block(
            KnowledgeEvidence(
                evidence.evidence_id,
                evidence.source_fact_id,
                evidence.start_offset,
                evidence.end_offset,
                evidence.evidence_text,
            ),
            blocks,
        )


def _validate_publication(publication: AcceptedPublicationLink) -> None:
    if min(
        publication.publication_id,
        publication.insight_version_id,
        publication.insight_id,
        publication.version_no,
    ) <= 0:
        raise ValueError("Accepted publication navigation identity is invalid")
    _validate_relative_markdown_path(publication.relative_path)
    if publication.machine_identity != accepted_insight_machine_identity(
        publication.insight_id,
        publication.insight_version_id,
    ):
        raise ValueError("Accepted publication navigation identity is invalid")
    if publication.relative_path != accepted_insight_relative_path(
        publication.insight_id,
        publication.version_no,
    ):
        raise ValueError("Accepted publication navigation path is invalid")
    if not publication.claim:
        raise ValueError("Accepted publication navigation is invalid")


def _validate_evolution_reference(
    reference: AcceptedEvolutionReference,
) -> None:
    if reference.relation_kind not in {
        "direct_predecessor",
        "replaced_identity",
        "historical_cause",
    }:
        raise ValueError("Accepted evolution reference kind is invalid")
    if min(
        reference.insight_version_id,
        reference.insight_id,
        reference.version_no,
        reference.judgment_id,
    ) <= 0:
        raise ValueError("Accepted evolution reference identity is invalid")
    if (
        not reference.accepted_at
        or not reference.claim
        or reference.initial_role not in {"current", "historical"}
        or reference.current_role not in {"current", "historical"}
        or (
            reference.initial_role == "historical"
            and reference.current_role != "historical"
        )
    ):
        raise ValueError("Accepted evolution reference is invalid")
    if (
        (reference.relation_kind == "replaced_identity")
        != (
            reference.relation_event_id is not None
            and reference.relation_reason_text is not None
        )
        or (
            reference.relation_kind != "replaced_identity"
            and reference.relation_reason_text is not None
        )
        or (
            reference.relation_reason_text is not None
            and not reference.relation_reason_text.strip()
        )
        or (
            reference.relation_event_id is not None
            and reference.relation_event_id <= 0
        )
    ):
        raise ValueError("Accepted evolution relation event is invalid")
    if reference.publication is not None:
        _validate_publication(reference.publication)
        if (
            reference.publication.insight_version_id
            != reference.insight_version_id
            or reference.publication.insight_id != reference.insight_id
            or reference.publication.version_no != reference.version_no
            or reference.publication.claim != reference.claim
        ):
            raise ValueError("Accepted evolution publication link is invalid")


def _expected_snapshot_fact_ids(
    context: AcceptedInsightRenderContext,
) -> tuple[str, ...]:
    values: set[str] = set()

    def add_reference(
        reference: AcceptedEvolutionReference,
        *,
        replacement_insight_id: int | None,
    ) -> None:
        values.update(
            {
                f"accepted:{reference.insight_version_id}",
                f"insight:{reference.insight_id}",
                f"insight-version:{reference.insight_version_id}",
                f"judgment:{reference.judgment_id}",
            }
        )
        if reference.relation_event_id is not None:
            values.add(f"organization-event:{reference.relation_event_id}")
            if replacement_insight_id is None:
                raise ValueError("Accepted replacement snapshot target is missing")
            values.add(
                f"identity-replacement:{reference.insight_id}:"
                f"{replacement_insight_id}:{reference.relation_event_id}"
            )
        if reference.publication is not None:
            values.add(f"publication:{reference.publication.publication_id}")

    for node in context.lineage_nodes:
        values.update(
            {
                f"accepted:{node.insight_version_id}",
                f"insight:{node.insight_id}",
                f"insight-version:{node.insight_version_id}",
                f"judgment:{node.judgment.judgment_id}",
                f"organization-event:{node.formation_event.event_id}",
            }
        )
        values.update(
            f"insight-participant:{node.insight_version_id}:{item.position}"
            for item in node.participants
        )
        for relation in node.used_relations:
            values.add(f"relation:{relation.relation_id}")
            values.add(f"relation-version:{relation.relation_version_id}")
        for fact in node.additional_exit_facts:
            values.add(
                f"accepted-exit:{node.insight_version_id}:{fact.event_id}:"
                f"{fact.fact_kind}"
            )
        if node.primary_exit_fact is not None:
            values.add(
                f"accepted-exit:{node.insight_version_id}:"
                f"{node.primary_exit_fact.event_id}:"
                f"{node.primary_exit_fact.fact_kind}"
            )
        if node.caused_by_judgment_id is not None:
            values.add(f"judgment:{node.caused_by_judgment_id}")
        if node.primary_cause_accepted is not None:
            add_reference(
                node.primary_cause_accepted,
                replacement_insight_id=None,
            )
        if node.replacement_insight_id is not None:
            values.add(f"insight:{node.replacement_insight_id}")
        if node.existing_publication is not None:
            values.add(f"publication:{node.existing_publication.publication_id}")
    for leaf in context.source_leaves:
        values.update(
            {
                f"material:{leaf.material_id}",
                f"source-fact:{leaf.source_fact_id}",
                f"knowledge-result:{leaf.knowledge_result_id}",
                f"source-point:{leaf.knowledge_result_id}:{leaf.point_id}",
            }
        )
        values.update(
            f"evidence:{leaf.knowledge_result_id}:{leaf.point_id}:"
            f"{evidence.evidence_id}"
            for evidence in leaf.evidences
        )
    for reference in context.evolution_references:
        add_reference(
            reference,
            replacement_insight_id=context.insight_id,
        )
    return tuple(sorted(values))


def _render_core_markdown(
    context: AcceptedInsightRenderContext,
    *,
    context_signature: str,
    relative_path: str,
    machine_identity: str,
) -> str:
    root = context.root
    if "\n" in root.payload.claim or "\r" in root.payload.claim:
        raise ValueError("Accepted claim cannot be represented as a Markdown heading")
    lines = [
        "---",
        'kd_asset_type: "accepted_insight"',
        f"kd_insight_id: {context.insight_id}",
        f"kd_insight_version_id: {context.insight_version_id}",
        f"kd_judgment_id: {root.judgment.judgment_id}",
        f"kd_renderer_contract: {_yaml_string(context.renderer_contract)}",
        f"kd_render_context_sha256: {_yaml_string(context_signature)}",
        f"kd_machine_identity: {_yaml_string(machine_identity)}",
        f"kd_relative_path: {_yaml_string(relative_path)}",
        "---",
        "",
        f"# AI 衍生新知｜{root.payload.claim}",
        "",
        (
            "> [!important] 身份说明\n"
            "> 这是由正式知识谱系产生的 AI 衍生认知。"
            "用户点击“有点意思”只表示值得长期留下；"
            "它不是作者原话、来源事实、客观真理认证或用户本人观点，"
            "仍保留不确定性并允许未来修正。"
        ),
        "",
        "## 新知主句",
        "",
        f"**表达类型：{root.payload.claim_kind.value}**",
        "",
        root.payload.claim,
        "",
        "## 精炼短论述",
        "",
        root.payload.short_discussion,
        "",
        "## 连接理由",
        "",
    ]
    _append_original_bullets(lines, root.payload.connection_reasons)
    lines.extend(["## 限制、冲突与待验证部分", ""])
    if root.payload.limitations:
        for limitation in root.payload.limitations:
            _append_original_bullet(
                lines,
                f"[{limitation.kind.value}] {limitation.text}",
            )
    else:
        lines.extend(
            [
                "当前正式组成未记录额外限制；"
                "这不表示现实中不存在限制。",
                "",
            ]
        )

    if root.judgment.annotation_text is not None:
        lines.extend(
            [
                "## 我的批注",
                "",
                "> [!note] 用户个人认知",
                *_quote_lines(root.judgment.annotation_text),
                "",
            ]
        )

    node_by_id = {
        node.insight_version_id: node for node in context.lineage_nodes
    }
    source_by_id = {
        (leaf.knowledge_result_id, leaf.point_id): leaf
        for leaf in context.source_leaves
    }
    lines.extend(["## 实际参与知识", "", "### 来源型知识", ""])
    source_participants = tuple(
        item
        for item in root.participants
        if item.input_kind is InputKind.SOURCE_KNOWLEDGE
    )
    if source_participants:
        for participant in source_participants:
            leaf = source_by_id[
                (participant.knowledge_result_id or 0, participant.point_id or "")
            ]
            _append_source_participant(lines, participant, leaf)
    else:
        lines.extend(["本层没有直接来源型参与者。", ""])

    lines.extend(["### 已认可 AI 新知", ""])
    accepted_participants = tuple(
        item
        for item in root.participants
        if item.input_kind is InputKind.ACCEPTED_INSIGHT
    )
    if accepted_participants:
        for participant in accepted_participants:
            child = node_by_id[participant.accepted_insight_version_id or 0]
            _append_accepted_participant(lines, participant, child)
    else:
        lines.extend(["本层没有直接已认可 AI 新知参与者。", ""])

    lines.extend(["### 使用过的连接判断", ""])
    _append_used_relations(lines, root.used_relations)

    lines.extend(["## 完整产生谱系", ""])
    _append_lineage_node(
        lines,
        root,
        node_by_id=node_by_id,
        source_by_id=source_by_id,
        depth=0,
    )

    lines.extend(["## 形成与演化位置", ""])
    lines.extend(
        [
            f"- 形成事件：organization-event-{root.formation_event.event_id}",
            f"- 形成完成时间：{root.formation_event.completed_at}",
            f"- 用户判断时间：{root.judgment.decided_at}",
            f"- 实质版本：insight-{root.insight_id} / v{root.version_no} "
            f"(version-id {root.insight_version_id})",
            f"- 判断时初始角色：{root.initial_role}",
            f"- 本次渲染角色：{root.current_role}",
        ]
    )
    if root.current_role == "historical":
        _append_historical_position(lines, root)
    if context.evolution_references:
        lines.extend(["", "### Accepted 演化关系", ""])
        for reference in context.evolution_references:
            _append_evolution_reference(lines, reference)
    lines.append("")
    return "\n".join(lines)


def _append_source_participant(
    lines: list[str],
    participant: Participant,
    leaf: AcceptedRenderSourceLeaf,
) -> None:
    alias = _link_alias(
        f"打开《{leaf.knowledge_title}》中的观点：{leaf.point_statement}"
    )
    lines.extend(
        [
            f"- [[{leaf.published_path}|{alias}]]",
            f"  - 形成作用：{participant.contribution_text}",
            f"  - exact identity：KR-{leaf.knowledge_result_id} / {leaf.point_id}",
            "",
        ]
    )


def _append_accepted_participant(
    lines: list[str],
    participant: Participant,
    child: AcceptedRenderLineageNode,
) -> None:
    lines.extend(
        [
            f"- AI 新知 insight-{child.insight_id} v{child.version_no}："
            f"{child.payload.claim}",
            f"  - 形成作用：{participant.contribution_text}",
            f"  - 判断时初始角色：{child.initial_role}",
            f"  - 本次渲染角色：{child.current_role}",
        ]
    )
    if child.existing_publication is not None:
        alias = _link_alias(f"打开独立资产：{child.payload.claim}")
        lines.append(
            f"  - [[{child.existing_publication.relative_path}|{alias}]]"
        )
    lines.append("")


def _append_historical_position(
    lines: list[str],
    node: AcceptedRenderLineageNode,
) -> None:
    lines.extend(
        [
            f"- 永久历史主因：{node.historical_reason}",
            f"- 进入历史时间：{node.historical_at}",
        ]
    )
    if node.primary_cause_accepted is not None:
        lines.append("- Judgment 主因：")
        _append_evolution_reference(
            lines,
            node.primary_cause_accepted,
            indent="  ",
        )
    if node.primary_exit_fact is not None:
        fact = node.primary_exit_fact
        lines.append(
            f"- Event 主因：{fact.fact_kind} / event-{fact.event_id} / "
            f"{fact.reason_text}"
        )
        if fact.replacement_insight_id is not None:
            lines.append(
                f"  - replacement identity：insight-{fact.replacement_insight_id}"
            )
    for fact in node.additional_exit_facts:
        lines.append(
            f"- 追加退出事实：{fact.fact_kind} / event-{fact.event_id} / "
            f"{fact.reason_text}"
        )
        if fact.replacement_insight_id is not None:
            lines.append(
                f"  - replacement identity：insight-{fact.replacement_insight_id}"
            )


def _append_evolution_reference(
    lines: list[str],
    reference: AcceptedEvolutionReference,
    *,
    indent: str = "",
) -> None:
    label = {
        "direct_predecessor": "直接前序 accepted 版本",
        "replaced_identity": "被替代 accepted identity",
        "historical_cause": "导致历史角色的 accepted 版本",
    }[reference.relation_kind]
    lines.extend(
        [
            f"{indent}- {label}：insight-{reference.insight_id} "
            f"v{reference.version_no}（version-id "
            f"{reference.insight_version_id}）｜{reference.claim}",
            f"{indent}  - interesting judgment：judgment-{reference.judgment_id}",
            f"{indent}  - 判断时初始角色：{reference.initial_role}",
            f"{indent}  - 本次渲染角色：{reference.current_role}",
        ]
    )
    if reference.relation_event_id is not None:
        lines.append(
            f"{indent}  - replacement event："
            f"organization-event-{reference.relation_event_id}"
        )
        lines.append(
            f"{indent}  - replacement reason："
            f"{reference.relation_reason_text}"
        )
    if reference.publication is None:
        lines.append(f"{indent}  - 尚无独立已发布资产；不伪造链接或自动补发。")
    else:
        alias = _link_alias(f"打开独立资产：{reference.claim}")
        lines.append(
            f"{indent}  - [[{reference.publication.relative_path}|{alias}]]"
        )
    lines.append("")


def _append_used_relations(
    lines: list[str],
    relations: tuple[AcceptedRenderUsedRelation, ...],
) -> None:
    if not relations:
        lines.extend(["本层没有使用正式连接判断。", ""])
        return
    for relation in relations:
        lines.extend(
            [
                f"- relation-{relation.relation_id} v{relation.version_no}："
                f"{relation.payload.relation_statement}",
                f"  - 形成时作用：{relation.role_text}",
                f"  - 长期解释价值：{relation.payload.stable_value}",
                (
                    "  - 权限：系统派生连接判断；"
                    "不是参与知识、独立谱系或 evidence。"
                ),
            ]
        )
        if relation.payload.conditions:
            lines.append("  - 成立条件：")
            lines.extend(
                f"    - {condition}" for condition in relation.payload.conditions
            )
        if relation.payload.limitations:
            lines.append("  - 限制：")
            lines.extend(
                f"    - [{limitation.kind.value}] {limitation.text}"
                for limitation in relation.payload.limitations
            )
        if relation.payload.required_premises:
            lines.append("  - 必要前提：")
            lines.extend(
                f"    - {premise.text}（formal support handles: "
                f"{', '.join(premise.supported_by)}）"
                for premise in relation.payload.required_premises
            )
        lines.append("")


def _append_lineage_node(
    lines: list[str],
    node: AcceptedRenderLineageNode,
    *,
    node_by_id: dict[int, AcceptedRenderLineageNode],
    source_by_id: dict[tuple[int, str], AcceptedRenderSourceLeaf],
    depth: int,
) -> None:
    heading = "#" * min(3 + depth, 6)
    lines.extend(
        [
            f"{heading} AI 层｜insight-{node.insight_id} v{node.version_no}",
            "",
            f"- AI 主句：{node.payload.claim}",
            f"- 表达类型：{node.payload.claim_kind.value}",
            f"- 精炼短论述：{node.payload.short_discussion}",
            f"- 形成事件：organization-event-{node.formation_event.event_id}",
            f"- 形成开始时间：{node.formation_event.started_at}",
            f"- 形成完成时间：{node.formation_event.completed_at}",
            f"- 判断时初始角色：{node.initial_role}",
            f"- 本次渲染角色：{node.current_role}",
            "- 直接已认可前序版本："
            f"{node.previous_version_id or '无（首版或前序未认可）'}",
            "- 连接理由：",
        ]
    )
    if node.current_role == "historical":
        _append_historical_position(lines, node)
    for reason in node.payload.connection_reasons:
        lines.append(f"  - {reason}")
    lines.append("- 沿途限制：")
    if node.payload.limitations:
        for limitation in node.payload.limitations:
            lines.append(f"  - [{limitation.kind.value}] {limitation.text}")
    else:
        lines.append("  - 当前正式组成未记录额外限制。")
    lines.extend(["- 形成时使用过的连接判断：", ""])
    _append_used_relations(lines, node.used_relations)
    lines.extend(["- 当时实际参与者：", ""])
    for participant in node.participants:
        if participant.input_kind is InputKind.SOURCE_KNOWLEDGE:
            leaf = source_by_id[
                (participant.knowledge_result_id or 0, participant.point_id or "")
            ]
            _append_source_lineage_leaf(lines, participant, leaf)
        else:
            child = node_by_id[participant.accepted_insight_version_id or 0]
            lines.extend(
                [
                    f"  - AI participant：insight-{child.insight_id} "
                    f"v{child.version_no}｜{child.payload.claim}",
                    f"    - 形成作用：{participant.contribution_text}",
                    "",
                ]
            )
            _append_lineage_node(
                lines,
                child,
                node_by_id=node_by_id,
                source_by_id=source_by_id,
                depth=depth + 1,
            )


def _append_source_lineage_leaf(
    lines: list[str],
    participant: Participant,
    leaf: AcceptedRenderSourceLeaf,
) -> None:
    alias = _link_alias(
        f"打开《{leaf.knowledge_title}》中的观点：{leaf.point_statement}"
    )
    lines.extend(
        [
            f"  - 来源叶：KR-{leaf.knowledge_result_id} / {leaf.point_id}",
            f"    - 观点：{leaf.point_statement}",
            f"    - 形成作用：{participant.contribution_text}",
            f"    - 来源身份：{leaf.source_label}（{leaf.platform} / "
            f"{leaf.platform_item_id}）",
            f"    - 来源型资产：[[{leaf.published_path}|{alias}]]",
            (
                "    - exact evidence 导航"
                "（自然段只负责落点，不扩大 evidence 语义）："
            ),
        ]
    )
    blocks = _source_blocks(leaf.content_snapshot)
    for evidence in leaf.evidences:
        block = _target_block(
            KnowledgeEvidence(
                evidence.evidence_id,
                evidence.source_fact_id,
                evidence.start_offset,
                evidence.end_offset,
                evidence.evidence_text,
            ),
            blocks,
        )
        label = _evidence_alias(evidence)
        lines.append(
            f"      - [[{leaf.published_path}#^{block.block_id}|{label}]]"
        )
    lines.append("")


def _append_original_bullets(lines: list[str], values: tuple[str, ...]) -> None:
    if not values:
        raise ValueError("Accepted connection reasons cannot be empty")
    for value in values:
        _append_original_bullet(lines, value)


def _append_original_bullet(lines: list[str], value: str) -> None:
    parts = value.split("\n")
    lines.append(f"- {parts[0]}")
    lines.extend(f"  {part}" for part in parts[1:])
    lines.append("")


def _quote_lines(value: str) -> list[str]:
    return [f"> {line}" if line else ">" for line in value.split("\n")]


def _link_alias(value: str) -> str:
    compact = " ".join(value.split())
    return compact.replace("\\", "\\\\").replace("|", "\\|").replace("]", "\\]")


def _evidence_alias(evidence: AcceptedRenderEvidence) -> str:
    compact = " ".join(evidence.evidence_text.split())
    preview = compact[:32] + ("…" if len(compact) > 32 else "")
    return _link_alias(f"来源：{preview}")


def _yaml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _validate_relative_markdown_path(value: str) -> None:
    if not isinstance(value, str) or value != value.strip() or "\\" in value or "\x00" in value:
        raise ValueError("Published handoff path is unsafe")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.suffix.lower() != ".md"
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("Published handoff path is unsafe")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _positive_plain_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0
