from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from ..database import (
    get_task_current_knowledge_result,
    get_task_material_identity,
    record_knowledge_result_published,
    record_task_failure,
)
from ..obsidian_renderer import render_task_knowledge_markdown


class PublicationKind(StrEnum):
    PUBLISHED = "published"
    RECOVERED = "recovered"
    ALREADY_PUBLISHED = "already_published"
    CONFLICT = "conflict"
    FAILED = "failed"


@dataclass(frozen=True)
class PublicationResult:
    task_id: int
    kind: PublicationKind
    relative_path: str | None = None


def publication_relative_path(knowledge_result_id: int, title: str) -> Path:
    readable = " ".join(title.split())
    readable = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", readable)
    readable = re.sub(r"-+", "-", readable).strip(" .-")
    readable = readable[:80].rstrip(" .-") or "知识"
    return Path("知识蒸馏器") / f"{readable}--kr-{knowledge_result_id}.md"


def publish_task_knowledge(
    database_path: Path,
    task_id: int,
    vault_root: Path,
) -> PublicationResult:
    knowledge = get_task_current_knowledge_result(database_path, task_id)
    identity = get_task_material_identity(database_path, task_id)
    if knowledge is None or identity is None:
        raise ValueError("Task has no publishable KnowledgeResult")

    knowledge_result_id = int(knowledge["knowledge_result_id"])
    if knowledge["published_at"] is not None and knowledge["published_path"] is not None:
        return PublicationResult(
            task_id,
            PublicationKind.ALREADY_PUBLISHED,
            str(knowledge["published_path"]),
        )

    try:
        payload = json.loads(knowledge["payload_json"])
        title = payload["title"]
    except (KeyError, TypeError, ValueError):
        return _failed(database_path, task_id, "obsidian_render_failed")
    if not isinstance(title, str) or not title.strip():
        return _failed(database_path, task_id, "obsidian_render_failed")

    relative_path = publication_relative_path(knowledge_result_id, title)
    target = vault_root / relative_path
    try:
        if not vault_root.is_dir():
            return _failed(database_path, task_id, "obsidian_vault_unavailable")
        rendered = render_task_knowledge_markdown(database_path, task_id).markdown
        target.parent.mkdir(parents=True, exist_ok=True)
        placed = False
        if not target.exists():
            placed = _place_no_clobber(target, rendered)
        if not placed:
            if not _existing_file_matches(
                target,
                rendered,
                identity.platform,
                identity.platform_item_id,
                knowledge_result_id,
            ):
                record_task_failure(
                    database_path,
                    task_id,
                    "obsidian_publishing",
                    "obsidian_target_conflict",
                )
                return PublicationResult(
                    task_id,
                    PublicationKind.CONFLICT,
                    relative_path.as_posix(),
                )
    except (OSError, UnicodeError, ValueError):
        return _failed(database_path, task_id, "obsidian_publication_failed")

    try:
        recording = record_knowledge_result_published(
            database_path,
            task_id,
            knowledge_result_id,
            relative_path.as_posix(),
        )
    except (sqlite3.Error, ValueError):
        return _failed(database_path, task_id, "obsidian_publication_failed")

    return PublicationResult(
        task_id,
        PublicationKind.PUBLISHED if placed else PublicationKind.RECOVERED,
        recording.published_path,
    )


def _place_no_clobber(target: Path, markdown: str) -> bool:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".knowledge-distiller-",
        suffix=".tmp",
        dir=target.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as output:
            output.write(markdown)
        try:
            os.link(temporary, target)
        except FileExistsError:
            return False
        return True
    finally:
        temporary.unlink(missing_ok=True)


def _existing_file_matches(
    target: Path,
    expected_markdown: str,
    platform: str,
    platform_item_id: str,
    knowledge_result_id: int,
) -> bool:
    if not target.is_file():
        return False
    existing = target.read_text(encoding="utf-8")
    machine_identity = _machine_identity(existing)
    expected_identity = (platform, platform_item_id, knowledge_result_id)
    return machine_identity == expected_identity and existing == expected_markdown


def _machine_identity(markdown: str) -> tuple[str, str, int] | None:
    lines = markdown.splitlines()
    if not lines or lines[0] != "---":
        return None
    try:
        closing = lines.index("---", 1)
    except ValueError:
        return None
    values: dict[str, str] = {}
    for line in lines[1:closing]:
        key, separator, value = line.partition(":")
        if separator:
            values[key.strip()] = value.strip()
    try:
        platform = json.loads(values["kd_material_platform"])
        platform_item_id = json.loads(values["kd_material_item_id"])
        knowledge_result_id = int(values["kd_knowledge_result_id"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(platform, str) or not isinstance(platform_item_id, str):
        return None
    return platform, platform_item_id, knowledge_result_id


def _failed(database_path: Path, task_id: int, reason: str) -> PublicationResult:
    record_task_failure(
        database_path,
        task_id,
        "obsidian_publishing",
        reason,
    )
    return PublicationResult(task_id, PublicationKind.FAILED)
