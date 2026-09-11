from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .database import connect
from .knowledge_derivation import (
    KnowledgeCandidate,
    KnowledgeEvidence,
    KnowledgePoint,
    knowledge_candidate_from_payload,
)


@dataclass(frozen=True)
class RenderedMarkdown:
    markdown: str
    source_block_count: int
    evidence_navigation_count: int


@dataclass(frozen=True)
class _SourceBlock:
    start_offset: int
    end_offset: int
    text: str
    block_id: str


def render_task_knowledge_markdown(
    database_path: Path,
    task_id: int,
) -> RenderedMarkdown:
    with connect(database_path) as connection:
        task = connection.execute(
            "SELECT task_id FROM tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if task is None:
            raise LookupError(f"Task {task_id} does not exist")
        row = connection.execute(
            """
            SELECT
                m.platform,
                m.platform_item_id,
                m.original_url,
                m.canonical_url,
                kr.knowledge_result_id,
                kr.source_fact_id,
                kr.payload_json,
                kr.invalidated_at,
                sf.metadata_json,
                sf.content_snapshot
            FROM tasks AS t
            JOIN materials AS m ON m.material_id = t.material_id
            JOIN knowledge_results AS kr
              ON kr.knowledge_result_id = m.current_knowledge_result_id
            JOIN source_facts AS sf
              ON sf.source_fact_id = kr.source_fact_id
             AND sf.source_fact_id = m.current_source_fact_id
            WHERE t.task_id = ?
            """,
            (task_id,),
        ).fetchone()
    if row is None:
        raise ValueError("Task has no current publishable KnowledgeResult")
    if row["invalidated_at"] is not None:
        raise ValueError("Current KnowledgeResult is invalidated")

    try:
        payload = json.loads(row["payload_json"])
        metadata = json.loads(row["metadata_json"])
    except (TypeError, ValueError) as error:
        raise ValueError("Formal publication payload is invalid") from error
    if not isinstance(payload, Mapping) or not isinstance(metadata, Mapping):
        raise ValueError("Formal publication payload is invalid")

    source_fact_id = int(row["source_fact_id"])
    snapshot = str(row["content_snapshot"])
    try:
        candidate = knowledge_candidate_from_payload(
            source_fact_id,
            snapshot,
            payload,
        )
    except ValueError as error:
        raise ValueError("KnowledgeResult cannot be faithfully rendered") from error

    return _render_markdown(
        platform=str(row["platform"]),
        platform_item_id=str(row["platform_item_id"]),
        original_url=str(row["original_url"]),
        canonical_url=(
            str(row["canonical_url"])
            if row["canonical_url"] is not None
            else None
        ),
        knowledge_result_id=int(row["knowledge_result_id"]),
        source_fact_id=source_fact_id,
        metadata=metadata,
        snapshot=snapshot,
        candidate=candidate,
    )


def _render_markdown(
    *,
    platform: str,
    platform_item_id: str,
    original_url: str,
    canonical_url: str | None,
    knowledge_result_id: int,
    source_fact_id: int,
    metadata: Mapping[str, object],
    snapshot: str,
    candidate: KnowledgeCandidate,
) -> RenderedMarkdown:
    if "\n" in candidate.title or any(
        "\n" in point.statement
        for point in candidate.core_points + candidate.other_points
    ):
        raise ValueError("Markdown heading content must be single-line")

    source_blocks = _source_blocks(snapshot)
    evidence_by_id = {
        evidence.evidence_id: evidence
        for evidence in candidate.evidence_registry
    }
    evidence_targets = {
        evidence.evidence_id: _target_block(evidence, source_blocks).block_id
        for evidence in candidate.evidence_registry
    }

    lines = [
        "---",
        f"kd_material_platform: {_yaml_string(platform)}",
        f"kd_material_item_id: {_yaml_string(platform_item_id)}",
        f"kd_knowledge_result_id: {knowledge_result_id}",
        "---",
        "",
        f"# {candidate.title}",
        "",
        *_quote_lines(candidate.summary),
        "",
        "## 核心观点",
        "",
    ]

    evidence_navigation_count = 0
    for point in candidate.core_points:
        evidence_navigation_count += _append_point(
            lines,
            point,
            evidence_by_id,
            evidence_targets,
        )

    if candidate.other_points:
        lines.extend(["## 更多正式观点", ""])
        for point in candidate.other_points:
            evidence_navigation_count += _append_point(
                lines,
                point,
                evidence_by_id,
                evidence_targets,
            )

    lines.extend(["## 完整原文", ""])
    for block in source_blocks:
        lines.extend([f"{block.text} ^{block.block_id}", ""])

    lines.extend(["## 来源身份信息", ""])
    lines.extend(
        _source_identity_lines(
            platform,
            original_url,
            canonical_url,
            metadata,
        )
    )
    lines.append("")
    return RenderedMarkdown(
        markdown="\n".join(lines),
        source_block_count=len(source_blocks),
        evidence_navigation_count=evidence_navigation_count,
    )


def _append_point(
    lines: list[str],
    point: KnowledgePoint,
    evidence_by_id: Mapping[str, KnowledgeEvidence],
    evidence_targets: Mapping[str, str],
) -> int:
    lines.append(f"> [!note]- {point.statement}")
    lines.extend(_quote_lines(point.argument))
    lines.extend([">", "> **来源依据**"])
    for evidence_id in point.evidence_ids:
        evidence = evidence_by_id[evidence_id]
        label = _evidence_label(evidence.evidence_text)
        target = evidence_targets[evidence_id]
        lines.append(f"> - [[#^{target}|{label}]]")
    lines.append("")
    return len(point.evidence_ids)


def _source_blocks(snapshot: str) -> tuple[_SourceBlock, ...]:
    if not snapshot.strip():
        raise ValueError("SourceFact snapshot is empty")
    blocks: list[_SourceBlock] = []
    start = 0
    for separator in re.finditer(r"\n(?:[ \t]*\n)+", snapshot):
        if separator.start() <= start:
            raise ValueError("SourceFact contains an empty natural paragraph")
        blocks.append(
            _SourceBlock(
                start,
                separator.start(),
                snapshot[start : separator.start()],
                f"source-{len(blocks) + 1}",
            )
        )
        start = separator.end()
    if start >= len(snapshot):
        raise ValueError("SourceFact contains an empty natural paragraph")
    blocks.append(
        _SourceBlock(
            start,
            len(snapshot),
            snapshot[start:],
            f"source-{len(blocks) + 1}",
        )
    )
    return tuple(blocks)


def _target_block(
    evidence: KnowledgeEvidence,
    blocks: tuple[_SourceBlock, ...],
) -> _SourceBlock:
    for block in blocks:
        if block.start_offset <= evidence.start_offset < block.end_offset:
            return block
    raise ValueError("Evidence start has no natural paragraph")


def _source_identity_lines(
    platform: str,
    original_url: str,
    canonical_url: str | None,
    metadata: Mapping[str, object],
) -> list[str]:
    lines = [f"- 平台：{platform}"]
    author = metadata.get("author")
    if isinstance(author, Mapping):
        _append_metadata_line(lines, "作者", author.get("display_name"))
        _append_metadata_line(lines, "平台账号", author.get("platform_account_id"))
    _append_metadata_line(lines, "原平台标题", metadata.get("source_title"))
    _append_metadata_line(lines, "原平台描述", metadata.get("original_description"))
    _append_metadata_line(lines, "发布时间", metadata.get("published_at"))
    source_url = canonical_url or original_url
    lines.append(f"- 原链接：<{source_url}>")
    return lines


def _append_metadata_line(
    lines: list[str],
    label: str,
    value: object,
) -> None:
    if isinstance(value, str) and value:
        lines.append(f"- {label}：{value}")


def _quote_lines(value: str) -> list[str]:
    return [f"> {line}" if line else ">" for line in value.split("\n")]


def _evidence_label(evidence_text: str) -> str:
    compact = " ".join(evidence_text.split())
    preview = compact[:32] + ("…" if len(compact) > 32 else "")
    return ("来源：" + preview).replace("\\", "\\\\").replace("|", "\\|")


def _yaml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)
