from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Mapping

from .domain import knowledge_from_dict
from .markdown import render_markdown
from .store import Store


class PublicationState(StrEnum):
    PUBLISHED = "published"
    RECOVERED = "recovered"
    ALREADY_PUBLISHED = "already_published"
    CONFLICT = "conflict"


@dataclass(frozen=True)
class Publication:
    state: PublicationState
    relative_path: str


def publication_path(knowledge_result_id: int, title: str) -> Path:
    readable = " ".join(title.split())
    readable = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", readable)
    readable = re.sub(r"-+", "-", readable).strip(" .-")
    readable = readable[:80].rstrip(" .-") or "知识"
    return Path("知识蒸馏器") / f"{readable}--kr-{knowledge_result_id}.md"


def publish(store: Store, item_id: int, vault: Path) -> Publication:
    row = store.item_bundle(item_id)
    if row is None or row["knowledge_result_id"] is None:
        raise ValueError("item has no publishable KnowledgeResult")
    if not vault.is_dir():
        raise ValueError("Obsidian Vault is unavailable")

    result_id = int(row["knowledge_result_id"])
    if row["published_path"] is not None:
        return Publication(PublicationState.ALREADY_PUBLISHED, row["published_path"])

    metadata = json.loads(row["metadata_json"])
    payload = json.loads(row["payload_json"])
    if not isinstance(metadata, Mapping):
        raise ValueError("source metadata is invalid")
    knowledge = knowledge_from_dict(row["snapshot"], payload)
    relative = publication_path(result_id, knowledge.title)
    # New notes retain text and source locators, without working media copies.
    metadata = dict(metadata)
    metadata['source_lineage'] = json.loads(row['lineage_json'])
    metadata['published_media'] = []
    rendered = render_markdown(
        source_kind=row["source_kind"],
        source_key=row["source_key"],
        submitted_url=row["submitted_url"],
        canonical_url=row["canonical_url"],
        metadata=metadata,
        source_fact_id=int(row["source_fact_id"]),
        snapshot=row["snapshot"],
        knowledge_result_id=result_id,
        knowledge=knowledge,
    ).text
    target = vault / relative
    if target.parent.is_symlink():
        return Publication(PublicationState.CONFLICT, relative.as_posix())
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except FileExistsError:
        return Publication(PublicationState.CONFLICT, relative.as_posix())

    placed = False
    if not target.exists() and not target.is_symlink():
        placed = _place_no_clobber(target, rendered)
    if not placed:
        if (
            target.is_symlink()
            or not target.is_file()
            or target.read_bytes() != rendered.encode("utf-8")
        ):
            return Publication(PublicationState.CONFLICT, relative.as_posix())

    store.mark_published(result_id, relative.as_posix(), vault=vault)
    return Publication(
        PublicationState.PUBLISHED if placed else PublicationState.RECOVERED,
        relative.as_posix(),
    )


def _place_no_clobber(target: Path, content: str | bytes) -> bool:
    descriptor, name = tempfile.mkstemp(
        prefix=".knowledge-distiller-", suffix=".tmp", dir=target.parent
    )
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content.encode('utf-8') if isinstance(content, str) else content)
        try:
            os.link(temporary, target)
        except FileExistsError:
            return False
        return True
    finally:
        temporary.unlink(missing_ok=True)
