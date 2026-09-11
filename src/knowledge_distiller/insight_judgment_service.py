from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Callable

from .accepted_insight_library import (
    _load_accepted_exit_facts,
    _load_judgment_target_lineage,
    _select_primary_exit_fact,
)
from .database import connect, utc_now
from .organization_models import decode_insight_payload, semantic_signature


class JudgmentResultKind(StrEnum):
    RECORDED = "recorded"
    ALREADY_RECORDED = "already_recorded"
    CONFLICT = "conflict"
    NOT_FOUND = "not_found"
    NOT_ESTABLISHED = "not_established"
    IDENTITY_MISMATCH = "identity_mismatch"
    UNREADABLE = "unreadable"


@dataclass(frozen=True)
class InsightJudgmentRecord:
    judgment_id: int
    insight_id: int
    insight_version_id: int
    decision: str
    annotation_text: str | None
    decided_at: str
    accepted_initial_role: str | None
    accepted_current_role: str | None
    accepted_historical_reason: str | None


@dataclass(frozen=True)
class JudgmentResult:
    kind: JudgmentResultKind
    judgment: InsightJudgmentRecord | None = None


FailureInjector = Callable[[str, sqlite3.Connection], None]


class InsightJudgmentService:
    def __init__(
        self,
        database_path: Path,
        *,
        now: Callable[[], str] = utc_now,
        failure_injector: FailureInjector | None = None,
        lineage_reader=None,
    ):
        self.lineage_reader = lineage_reader or _load_judgment_target_lineage
        self.database_path = database_path
        self.now = now
        self.failure_injector = failure_injector

    def record_judgment(
        self,
        insight_version_id: int,
        decision: str,
        annotation: str | None = None,
    ) -> JudgmentResult:
        if decision not in {"interesting", "rethink"}:
            raise ValueError("decision must be interesting or rethink")
        annotation_text = _normalize_annotation(annotation)
        connection = connect(self.database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            target = connection.execute(
                """
                SELECT iv.*, identity.created_event_id,
                       event.status AS produced_event_status
                FROM insight_versions AS iv
                LEFT JOIN insight_identities AS identity
                  ON identity.insight_id = iv.insight_id
                LEFT JOIN organization_events AS event
                  ON event.event_id = iv.produced_event_id
                WHERE iv.insight_version_id = ?
                """,
                (insight_version_id,),
            ).fetchone()
            if target is None:
                connection.rollback()
                return JudgmentResult(JudgmentResultKind.NOT_FOUND)
            identity_state = _validate_version_identity(connection, target)
            if identity_state is not None:
                connection.rollback()
                return JudgmentResult(identity_state)
            if target["produced_event_status"] != "succeeded":
                connection.rollback()
                return JudgmentResult(JudgmentResultKind.NOT_ESTABLISHED)
            try:
                payload = decode_insight_payload(str(target["payload_json"]))
                if semantic_signature(payload) != str(target["semantic_signature"]):
                    raise ValueError("Insight semantic signature is invalid")
                self.lineage_reader(connection, insight_version_id)
            except (json.JSONDecodeError, TypeError, ValueError):
                connection.rollback()
                return JudgmentResult(JudgmentResultKind.UNREADABLE)

            try:
                exit_cause = _target_exit_cause(
                    connection,
                    int(target["insight_id"]),
                    insight_version_id,
                )
            except (TypeError, ValueError):
                connection.rollback()
                return JudgmentResult(JudgmentResultKind.UNREADABLE)

            existing = connection.execute(
                """
                SELECT * FROM user_insight_judgments
                WHERE insight_version_id = ?
                """,
                (insight_version_id,),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["decision"]) != decision
                    or existing["annotation_text"] != annotation_text
                ):
                    connection.rollback()
                    return JudgmentResult(JudgmentResultKind.CONFLICT)
                try:
                    record = _read_record(
                        connection,
                        int(existing["judgment_id"]),
                        exit_cause=exit_cause,
                    )
                except (TypeError, ValueError):
                    connection.rollback()
                    return JudgmentResult(JudgmentResultKind.UNREADABLE)
                connection.rollback()
                return JudgmentResult(
                    JudgmentResultKind.ALREADY_RECORDED,
                    record,
                )

            try:
                history = _read_identity_accepted_history(
                    connection,
                    int(target["insight_id"]),
                )
            except (TypeError, ValueError):
                connection.rollback()
                return JudgmentResult(JudgmentResultKind.UNREADABLE)

            now = self.now()
            cursor = connection.execute(
                """
                INSERT INTO user_insight_judgments (
                    insight_id, insight_version_id, decision,
                    annotation_text, decided_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    int(target["insight_id"]),
                    insight_version_id,
                    decision,
                    annotation_text,
                    now,
                ),
            )
            judgment_id = int(cursor.lastrowid)
            self._inject("after_judgment_insert", connection)
            if decision == "interesting":
                role = _choose_accepted_role(
                    int(target["version_no"]),
                    exit_cause,
                    history,
                )
                if role.retire_current_judgment_id is not None:
                    update = connection.execute(
                        """
                        UPDATE accepted_insight_versions
                        SET current_role = 'historical', historical_at = ?,
                            historical_reason = 'newer_accepted_current',
                            caused_by_judgment_id = ?
                        WHERE insight_id = ? AND current_role = 'current'
                          AND judgment_id = ?
                        """,
                        (
                            now,
                            judgment_id,
                            int(target["insight_id"]),
                            role.retire_current_judgment_id,
                        ),
                    )
                    if update.rowcount != 1:
                        raise sqlite3.IntegrityError(
                            "Accepted current changed during judgment"
                        )
                    self._inject("after_current_retirement", connection)
                self._inject("before_accepted_insert", connection)
                _insert_accepted(
                    connection,
                    target,
                    judgment_id,
                    now,
                    role,
                )
                self._inject("after_accepted_insert", connection)
            record = _read_record(connection, judgment_id)
            connection.commit()
            return JudgmentResult(JudgmentResultKind.RECORDED, record)
        except sqlite3.Error:
            connection.rollback()
            return JudgmentResult(JudgmentResultKind.UNREADABLE)
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _inject(self, point: str, connection: sqlite3.Connection) -> None:
        if self.failure_injector is not None:
            self.failure_injector(point, connection)


@dataclass(frozen=True)
class _ExitCause:
    reason: str
    event_id: int
    replacement_insight_id: int | None = None


@dataclass(frozen=True)
class _AcceptedHistory:
    judgment_id: int
    insight_version_id: int
    version_no: int
    initial_role: str
    current_role: str


@dataclass(frozen=True)
class AcceptedRoleChoice:
    initial_role: str
    current_role: str
    historical_reason: str | None = None
    caused_by_event_id: int | None = None
    caused_by_judgment_id: int | None = None
    replacement_insight_id: int | None = None
    disqualification_reason: str | None = None
    retire_current_judgment_id: int | None = None


def _normalize_annotation(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("annotation must be text or None")
    return None if not value.strip() else value


def _validate_version_identity(connection, target) -> JudgmentResultKind | None:
    if target["created_event_id"] is None:
        return JudgmentResultKind.IDENTITY_MISMATCH
    version_no = int(target["version_no"])
    if version_no == 1:
        if target["previous_version_id"] is not None:
            return JudgmentResultKind.IDENTITY_MISMATCH
        if int(target["created_event_id"]) != int(target["produced_event_id"]):
            return JudgmentResultKind.IDENTITY_MISMATCH
        return None
    predecessor = connection.execute(
        """
        SELECT insight_id, version_no
        FROM insight_versions
        WHERE insight_version_id = ?
        """,
        (target["previous_version_id"],),
    ).fetchone()
    if (
        predecessor is None
        or int(predecessor["insight_id"]) != int(target["insight_id"])
        or int(predecessor["version_no"]) != version_no - 1
    ):
        return JudgmentResultKind.IDENTITY_MISMATCH
    return None


def accepted_role_for_version(connection: sqlite3.Connection, target: sqlite3.Row) -> AcceptedRoleChoice:
    """Resolve an acceptance role inside the caller's write transaction.

    Initial judgment and reconsideration share the same exit and version rules.
    This only reads facts; the caller owns grant insertion and current retirement.
    """
    return _choose_accepted_role(
        int(target["version_no"]),
        _target_exit_cause(connection, int(target["insight_id"]), int(target["insight_version_id"])),
        _read_identity_accepted_history(connection, int(target["insight_id"])),
    )


def _target_exit_cause(
    connection: sqlite3.Connection,
    insight_id: int,
    insight_version_id: int,
) -> _ExitCause | None:
    facts = _load_accepted_exit_facts(
        connection,
        insight_id,
        insight_version_id,
    )
    if not facts:
        return None
    primary = _select_primary_exit_fact(facts)
    return _ExitCause(
        primary.fact_kind,
        primary.event_id,
        primary.replacement_insight_id,
    )


def _read_identity_accepted_history(
    connection: sqlite3.Connection,
    insight_id: int,
) -> tuple[_AcceptedHistory, ...]:
    rows = connection.execute(
        """
        SELECT a.judgment_id, a.insight_version_id,
               a.initial_role, a.current_role, iv.version_no
        FROM accepted_insight_versions AS a
        JOIN insight_versions AS iv
          ON iv.insight_version_id = a.insight_version_id
         AND iv.insight_id = a.insight_id
        WHERE a.insight_id = ?
        ORDER BY iv.version_no
        """,
        (insight_id,),
    ).fetchall()
    current_count = sum(row["current_role"] == "current" for row in rows)
    if current_count > 1:
        raise ValueError("Insight identity has multiple accepted current versions")
    for row in rows:
        if row["current_role"] == "current" and _target_exit_cause(
            connection,
            insight_id,
            int(row["insight_version_id"]),
        ) is not None:
            raise ValueError(
                "Accepted current has an established exit fact but was not retired"
            )
    return tuple(
        _AcceptedHistory(
            int(row["judgment_id"]),
            int(row["insight_version_id"]),
            int(row["version_no"]),
            str(row["initial_role"]),
            str(row["current_role"]),
        )
        for row in rows
    )


def _choose_accepted_role(
    version_no: int,
    exit_cause: _ExitCause | None,
    history: tuple[_AcceptedHistory, ...],
) -> AcceptedRoleChoice:
    if exit_cause is not None:
        return AcceptedRoleChoice(
            "historical",
            "historical",
            exit_cause.reason,
            caused_by_event_id=exit_cause.event_id,
            replacement_insight_id=exit_cause.replacement_insight_id,
            disqualification_reason=(
                exit_cause.reason
                if exit_cause.reason in {"basis_invalid", "refuted"}
                else None
            ),
        )
    newer_current = next(
        (
            item
            for item in history
            if item.version_no > version_no and item.current_role == "current"
        ),
        None,
    )
    if newer_current is not None:
        return AcceptedRoleChoice(
            "historical",
            "historical",
            "born_older_than_current",
            caused_by_judgment_id=newer_current.judgment_id,
        )
    newer_ever_current = next(
        (
            item
            for item in history
            if item.version_no > version_no and item.initial_role == "current"
        ),
        None,
    )
    if newer_ever_current is not None:
        return AcceptedRoleChoice(
            "historical",
            "historical",
            "born_after_newer_ever_current",
            caused_by_judgment_id=newer_ever_current.judgment_id,
        )
    older_current = next(
        (
            item
            for item in history
            if item.version_no < version_no and item.current_role == "current"
        ),
        None,
    )
    return AcceptedRoleChoice(
        "current",
        "current",
        retire_current_judgment_id=(
            older_current.judgment_id if older_current is not None else None
        ),
    )


def _insert_accepted(connection, target, judgment_id: int, now: str, role) -> None:
    connection.execute(
        """
        INSERT INTO accepted_insight_versions (
            insight_version_id, insight_id, judgment_id,
            judgment_decision, initial_role, current_role, accepted_at,
            historical_at, historical_reason, caused_by_event_id,
            caused_by_judgment_id, replacement_insight_id,
            disqualification_reason
        ) VALUES (?, ?, ?, 'interesting', ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(target["insight_version_id"]),
            int(target["insight_id"]),
            judgment_id,
            role.initial_role,
            role.current_role,
            now,
            now if role.current_role == "historical" else None,
            role.historical_reason,
            role.caused_by_event_id,
            role.caused_by_judgment_id,
            role.replacement_insight_id,
            role.disqualification_reason,
        ),
    )


def _read_record(
    connection: sqlite3.Connection,
    judgment_id: int,
    *,
    exit_cause: _ExitCause | None = None,
) -> InsightJudgmentRecord:
    row = connection.execute(
        """
        SELECT j.*, a.initial_role, a.current_role, a.historical_reason
        FROM user_insight_judgments AS j
        LEFT JOIN accepted_insight_versions AS a
          ON a.judgment_id = j.judgment_id
         AND a.insight_version_id = j.insight_version_id
        WHERE j.judgment_id = ?
        """,
        (judgment_id,),
    ).fetchone()
    if row is None:
        raise ValueError("Judgment disappeared")
    if row["decision"] == "interesting" and row["initial_role"] is None:
        raise ValueError("Interesting judgment is missing accepted consequence")
    if row["decision"] == "rethink" and row["initial_role"] is not None:
        from .accepted_insight_library import read_accepted_row
        if read_accepted_row(connection, int(row["insight_version_id"])) is None:
            raise ValueError("Rethink judgment has no valid reconsideration grant")
    current_role = (
        str(row["current_role"]) if row["current_role"] is not None else None
    )
    historical_reason = (
        str(row["historical_reason"])
        if row["historical_reason"] is not None
        else None
    )
    if (
        row["decision"] == "interesting"
        and current_role == "current"
        and exit_cause is not None
    ):
        current_role = "historical"
        historical_reason = exit_cause.reason
    return InsightJudgmentRecord(
        int(row["judgment_id"]),
        int(row["insight_id"]),
        int(row["insight_version_id"]),
        str(row["decision"]),
        str(row["annotation_text"]) if row["annotation_text"] is not None else None,
        str(row["decided_at"]),
        str(row["initial_role"]) if row["decision"] == "interesting" and row["initial_role"] is not None else None,
        current_role if row["decision"] == "interesting" else None,
        historical_reason if row["decision"] == "interesting" else None,
    )
