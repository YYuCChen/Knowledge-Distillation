from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .identity import ConfirmedMaterialIdentity
from .schema_migrations import migrate_database


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def connect(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize_database(database_path: Path) -> None:
    migrate_database(database_path)


def create_task(database_path: Path, submitted_url: str) -> int:
    now = utc_now()
    with connect(database_path) as connection:
        cursor = connection.execute(
            """
            INSERT INTO tasks (submitted_url, created_at, updated_at)
            VALUES (?, ?, ?)
            """,
            (submitted_url, now, now),
        )
        return int(cursor.lastrowid)


def get_task(database_path: Path, task_id: int) -> sqlite3.Row | None:
    with connect(database_path) as connection:
        return connection.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()


def get_task_material_identity(
    database_path: Path,
    task_id: int,
) -> ConfirmedMaterialIdentity | None:
    with connect(database_path) as connection:
        row = connection.execute(
            """
            SELECT m.platform, m.platform_item_id, m.original_url, m.canonical_url
            FROM tasks AS t
            JOIN materials AS m ON m.material_id = t.material_id
            WHERE t.task_id = ?
            """,
            (task_id,),
        ).fetchone()
    if row is None:
        return None
    return ConfirmedMaterialIdentity(
        platform=str(row["platform"]),
        platform_item_id=str(row["platform_item_id"]),
        original_url=str(row["original_url"]),
        canonical_url=str(row["canonical_url"] or row["original_url"]),
    )


def get_task_current_source_fact(
    database_path: Path,
    task_id: int,
) -> sqlite3.Row | None:
    with connect(database_path) as connection:
        return connection.execute(
            """
            SELECT sf.*
            FROM tasks AS t
            JOIN materials AS m ON m.material_id = t.material_id
            JOIN source_facts AS sf
              ON sf.source_fact_id = m.current_source_fact_id
            WHERE t.task_id = ?
            """,
            (task_id,),
        ).fetchone()


def get_task_current_knowledge_result(
    database_path: Path,
    task_id: int,
) -> sqlite3.Row | None:
    with connect(database_path) as connection:
        return connection.execute(
            """
            SELECT kr.*
            FROM tasks AS t
            JOIN materials AS m ON m.material_id = t.material_id
            JOIN knowledge_results AS kr
              ON kr.knowledge_result_id = m.current_knowledge_result_id
            WHERE t.task_id = ?
            """,
            (task_id,),
        ).fetchone()


_FORMAL_KNOWLEDGE_QUERY = """
            SELECT
                m.material_id,
                m.platform,
                m.platform_item_id,
                kr.knowledge_result_id,
                kr.source_fact_id,
                kr.payload_json,
                kr.created_at,
                kr.published_at,
                kr.published_path,
                sf.metadata_json,
                sf.content_snapshot
            FROM materials AS m
            JOIN knowledge_results AS kr
              ON kr.knowledge_result_id = m.current_knowledge_result_id
             AND kr.source_fact_id = m.current_source_fact_id
            JOIN source_facts AS sf
              ON sf.source_fact_id = kr.source_fact_id
             AND sf.material_id = m.material_id
            WHERE kr.invalidated_at IS NULL
              AND kr.published_at IS NOT NULL
              AND kr.published_path IS NOT NULL
              AND TRIM(kr.published_path) != ''
"""


def query_searchable_formal_knowledge(
    connection: sqlite3.Connection,
) -> list[sqlite3.Row]:
    return connection.execute(
        _FORMAL_KNOWLEDGE_QUERY
        + " ORDER BY kr.created_at DESC, kr.knowledge_result_id DESC"
    ).fetchall()


def query_recent_formal_knowledge(
    connection: sqlite3.Connection,
) -> list[sqlite3.Row]:
    return connection.execute(
        _FORMAL_KNOWLEDGE_QUERY
        + " ORDER BY kr.published_at DESC, kr.knowledge_result_id DESC"
    ).fetchall()


def list_searchable_formal_knowledge(database_path: Path) -> list[sqlite3.Row]:
    """Return every current, valid, published formal result without mutation."""
    with connect(database_path) as connection:
        return query_searchable_formal_knowledge(connection)


def list_recent_formal_knowledge(database_path: Path) -> list[sqlite3.Row]:
    """Return eligible formal results in newest-publication order."""
    with connect(database_path) as connection:
        return query_recent_formal_knowledge(connection)


@dataclass(frozen=True)
class MaterialAttachment:
    material_id: int
    task_id: int
    material_created: bool
    task_reused: bool


class TaskMaterialConflict(Exception):
    pass


@dataclass(frozen=True)
class SourceFactCreation:
    source_fact_id: int
    material_id: int
    created: bool


@dataclass(frozen=True)
class KnowledgeResultCreation:
    knowledge_result_id: int
    material_id: int
    source_fact_id: int
    created: bool


@dataclass(frozen=True)
class PublicationRecording:
    knowledge_result_id: int
    published_at: str
    published_path: str
    recorded: bool


def attach_task_to_material(
    database_path: Path,
    task_id: int,
    identity: ConfirmedMaterialIdentity,
) -> MaterialAttachment:
    now = utc_now()
    with connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        task = connection.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        if task is None:
            raise LookupError(f"Task {task_id} does not exist")

        if task["material_id"] is not None:
            material = connection.execute(
                "SELECT * FROM materials WHERE material_id = ?",
                (task["material_id"],),
            ).fetchone()
            if (
                material["platform"] != identity.platform
                or material["platform_item_id"] != identity.platform_item_id
            ):
                raise TaskMaterialConflict("Task is already linked to another material")
            _clear_task_diagnostics(connection, task_id, now)
            return MaterialAttachment(
                material_id=int(material["material_id"]),
                task_id=task_id,
                material_created=False,
                task_reused=True,
            )

        material = connection.execute(
            """
            SELECT * FROM materials
            WHERE platform = ? AND platform_item_id = ?
            """,
            (identity.platform, identity.platform_item_id),
        ).fetchone()
        material_created = material is None
        if material is None:
            material_id = int(
                connection.execute(
                    """
                    INSERT INTO materials (
                        platform, platform_item_id, original_url,
                        canonical_url, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        identity.platform,
                        identity.platform_item_id,
                        identity.original_url,
                        identity.canonical_url,
                        now,
                    ),
                ).lastrowid
            )
        else:
            material_id = int(material["material_id"])

        existing_task = connection.execute(
            "SELECT task_id FROM tasks WHERE material_id = ?", (material_id,)
        ).fetchone()
        if existing_task is not None:
            existing_task_id = int(existing_task["task_id"])
            connection.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))
            _clear_task_diagnostics(connection, existing_task_id, now)
            return MaterialAttachment(
                material_id=material_id,
                task_id=existing_task_id,
                material_created=False,
                task_reused=True,
            )

        connection.execute(
            """
            UPDATE tasks
            SET material_id = ?, updated_at = ?,
                last_failure_boundary = NULL, last_failure_reason = NULL,
                waiting_boundary = NULL, waiting_reason = NULL
            WHERE task_id = ?
            """,
            (material_id, now, task_id),
        )
        return MaterialAttachment(
            material_id=material_id,
            task_id=task_id,
            material_created=material_created,
            task_reused=False,
        )


def _clear_task_diagnostics(
    connection: sqlite3.Connection, task_id: int, updated_at: str
) -> None:
    connection.execute(
        """
        UPDATE tasks
        SET updated_at = ?,
            last_failure_boundary = NULL, last_failure_reason = NULL,
            waiting_boundary = NULL, waiting_reason = NULL
        WHERE task_id = ?
        """,
        (updated_at, task_id),
    )


def record_task_waiting_for_login(database_path: Path, task_id: int) -> None:
    record_task_waiting_for_source_condition(
        database_path,
        task_id,
        "douyin_login_required",
    )


def record_task_waiting_for_source_condition(
    database_path: Path,
    task_id: int,
    reason: str,
) -> None:
    record_task_waiting(
        database_path,
        task_id,
        "source_fact_production",
        reason,
    )


def record_task_waiting(
    database_path: Path,
    task_id: int,
    boundary: str,
    reason: str,
) -> None:
    with connect(database_path) as connection:
        connection.execute(
            """
            UPDATE tasks
            SET updated_at = ?,
                waiting_boundary = ?,
                waiting_reason = ?,
                last_failure_boundary = NULL,
                last_failure_reason = NULL
            WHERE task_id = ?
            """,
            (utc_now(), boundary, reason, task_id),
        )


def record_task_identity_failure(
    database_path: Path, task_id: int, reason: str
) -> None:
    record_task_source_failure(database_path, task_id, reason)


def record_task_source_failure(
    database_path: Path, task_id: int, reason: str
) -> None:
    record_task_failure(
        database_path,
        task_id,
        "source_fact_production",
        reason,
    )


def record_task_failure(
    database_path: Path,
    task_id: int,
    boundary: str,
    reason: str,
) -> None:
    with connect(database_path) as connection:
        connection.execute(
            """
            UPDATE tasks
            SET updated_at = ?,
                last_failure_boundary = ?,
                last_failure_reason = ?,
                waiting_boundary = NULL,
                waiting_reason = NULL
            WHERE task_id = ?
            """,
            (utc_now(), boundary, reason, task_id),
        )


def clear_task_diagnostics(database_path: Path, task_id: int) -> None:
    with connect(database_path) as connection:
        _clear_task_diagnostics(connection, task_id, utc_now())


def establish_source_fact(
    database_path: Path,
    task_id: int,
    metadata: dict[str, object],
    content_snapshot: str,
    uncertainties: list[dict[str, object]],
) -> SourceFactCreation:
    now = utc_now()
    metadata_json = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
    uncertainty_json = json.dumps(
        uncertainties,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    with connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """
            SELECT t.material_id, m.current_source_fact_id
            FROM tasks AS t
            LEFT JOIN materials AS m ON m.material_id = t.material_id
            WHERE t.task_id = ?
            """,
            (task_id,),
        ).fetchone()
        if row is None:
            raise LookupError(f"Task {task_id} does not exist")
        if row["material_id"] is None:
            raise ValueError("Task has no confirmed material identity")
        material_id = int(row["material_id"])
        if row["current_source_fact_id"] is not None:
            return SourceFactCreation(
                int(row["current_source_fact_id"]),
                material_id,
                False,
            )

        source_fact_id = int(
            connection.execute(
                """
                INSERT INTO source_facts (
                    material_id, metadata_json, content_snapshot,
                    uncertainty_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    material_id,
                    metadata_json,
                    content_snapshot,
                    uncertainty_json,
                    now,
                ),
            ).lastrowid
        )
        updated = connection.execute(
            """
            UPDATE materials
            SET current_source_fact_id = ?
            WHERE material_id = ? AND current_source_fact_id IS NULL
            """,
            (source_fact_id, material_id),
        )
        if updated.rowcount != 1:
            raise sqlite3.IntegrityError("Material source fact changed concurrently")
        _clear_task_diagnostics(connection, task_id, now)
        return SourceFactCreation(source_fact_id, material_id, True)


def establish_knowledge_result(
    database_path: Path,
    task_id: int,
    source_fact_id: int,
    payload: dict[str, object],
) -> KnowledgeResultCreation:
    now = utc_now()
    payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    with connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """
            SELECT
                t.material_id,
                m.current_source_fact_id,
                m.current_knowledge_result_id
            FROM tasks AS t
            LEFT JOIN materials AS m ON m.material_id = t.material_id
            WHERE t.task_id = ?
            """,
            (task_id,),
        ).fetchone()
        if row is None:
            raise LookupError(f"Task {task_id} does not exist")
        if row["material_id"] is None:
            raise ValueError("Task has no confirmed material identity")
        material_id = int(row["material_id"])
        if row["current_source_fact_id"] != source_fact_id:
            raise ValueError("Knowledge result must use current SourceFact")

        if row["current_knowledge_result_id"] is not None:
            knowledge_result_id = int(row["current_knowledge_result_id"])
            existing = connection.execute(
                """
                SELECT source_fact_id
                FROM knowledge_results
                WHERE knowledge_result_id = ?
                """,
                (knowledge_result_id,),
            ).fetchone()
            if existing is None or int(existing["source_fact_id"]) != source_fact_id:
                raise sqlite3.IntegrityError(
                    "Current knowledge result does not match current SourceFact"
                )
            return KnowledgeResultCreation(
                knowledge_result_id,
                material_id,
                source_fact_id,
                False,
            )

        knowledge_result_id = int(
            connection.execute(
                """
                INSERT INTO knowledge_results (
                    source_fact_id, payload_json, created_at
                ) VALUES (?, ?, ?)
                """,
                (source_fact_id, payload_json, now),
            ).lastrowid
        )
        updated = connection.execute(
            """
            UPDATE materials
            SET current_knowledge_result_id = ?
            WHERE material_id = ?
              AND current_source_fact_id = ?
              AND current_knowledge_result_id IS NULL
            """,
            (knowledge_result_id, material_id, source_fact_id),
        )
        if updated.rowcount != 1:
            raise sqlite3.IntegrityError(
                "Material knowledge result changed concurrently"
            )
        _clear_task_diagnostics(connection, task_id, now)
        return KnowledgeResultCreation(
            knowledge_result_id,
            material_id,
            source_fact_id,
            True,
        )


def record_knowledge_result_published(
    database_path: Path,
    task_id: int,
    knowledge_result_id: int,
    published_path: str,
) -> PublicationRecording:
    relative_path = Path(published_path)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise ValueError("Published path must be relative to the Vault")

    now = utc_now()
    with connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """
            SELECT
                m.current_knowledge_result_id,
                kr.published_at,
                kr.published_path
            FROM tasks AS t
            JOIN materials AS m ON m.material_id = t.material_id
            JOIN knowledge_results AS kr
              ON kr.knowledge_result_id = m.current_knowledge_result_id
            WHERE t.task_id = ?
            """,
            (task_id,),
        ).fetchone()
        if row is None:
            raise ValueError("Task has no current KnowledgeResult")
        if int(row["current_knowledge_result_id"]) != knowledge_result_id:
            raise ValueError("Publication must use the current KnowledgeResult")

        if row["published_at"] is not None and row["published_path"] is not None:
            return PublicationRecording(
                knowledge_result_id,
                str(row["published_at"]),
                str(row["published_path"]),
                False,
            )
        if row["published_at"] is not None or row["published_path"] is not None:
            raise sqlite3.IntegrityError("Publication fact is incomplete")

        updated = connection.execute(
            """
            UPDATE knowledge_results
            SET published_at = ?, published_path = ?
            WHERE knowledge_result_id = ?
              AND published_at IS NULL
              AND published_path IS NULL
            """,
            (now, published_path, knowledge_result_id),
        )
        if updated.rowcount != 1:
            raise sqlite3.IntegrityError("Publication fact changed concurrently")
        _clear_task_diagnostics(connection, task_id, now)
        return PublicationRecording(
            knowledge_result_id,
            now,
            published_path,
            True,
        )
