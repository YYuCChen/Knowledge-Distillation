from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from ..database import connect


class NextBoundary(StrEnum):
    SOURCE_FACT_PRODUCTION = "source_fact_production"
    KNOWLEDGE_DERIVATION = "knowledge_derivation"
    OBSIDIAN_PUBLISHING = "obsidian_publishing"
    COMPLETE = "complete"


def decide_next_boundary(database_path: Path, task_id: int) -> NextBoundary:
    with connect(database_path) as connection:
        row = connection.execute(
            """
            SELECT
                t.task_id,
                t.material_id,
                m.current_source_fact_id,
                m.current_knowledge_result_id,
                kr.published_at,
                kr.published_path
            FROM tasks AS t
            LEFT JOIN materials AS m ON m.material_id = t.material_id
            LEFT JOIN knowledge_results AS kr
                ON kr.knowledge_result_id = m.current_knowledge_result_id
            WHERE t.task_id = ?
            """,
            (task_id,),
        ).fetchone()

    if row is None:
        raise LookupError(f"Task {task_id} does not exist")
    if row["material_id"] is None or row["current_source_fact_id"] is None:
        return NextBoundary.SOURCE_FACT_PRODUCTION
    if row["current_knowledge_result_id"] is None:
        return NextBoundary.KNOWLEDGE_DERIVATION
    if row["published_at"] is None or row["published_path"] is None:
        return NextBoundary.OBSIDIAN_PUBLISHING
    return NextBoundary.COMPLETE
