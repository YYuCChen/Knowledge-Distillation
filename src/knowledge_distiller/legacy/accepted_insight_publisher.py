from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Callable

from ..accepted_insight_library import (
    load_accepted_render_context,
    validate_accepted_receipt_snapshot_anchors,
)
from ..accepted_insight_renderer import (
    AcceptedInsightRenderContext,
    RenderedAcceptedInsight,
    accepted_insight_machine_identity,
    accepted_insight_relative_path,
    accepted_render_context_from_dict,
    accepted_render_context_signature,
    decode_accepted_placement_receipt_from_content,
    render_accepted_insight,
)
from ..database import connect, utc_now
from ..knowledge_derivation import KnowledgeEvidence
from ..obsidian_renderer import _evidence_label, _source_blocks, _target_block


class AcceptedPublicationKind(StrEnum):
    PUBLISHED = "published"
    RECOVERED = "recovered"
    ALREADY_PUBLISHED = "already_published"
    CONFLICT = "conflict"
    NOT_ELIGIBLE = "not_eligible"
    FAILED = "failed"


@dataclass(frozen=True)
class AcceptedPublicationResult:
    insight_version_id: int
    kind: AcceptedPublicationKind
    publication_id: int | None = None
    relative_path: str | None = None
    error_code: str | None = None


@dataclass(frozen=True)
class _AcceptedPublishIdentity:
    insight_version_id: int
    insight_id: int
    version_no: int
    judgment_id: int


@dataclass(frozen=True)
class _PublicationRow:
    publication_id: int
    insight_version_id: int
    relative_path: str


FailureInjector = Callable[[str], None]
Clock = Callable[[], str]


class _SourcePreflightError(Exception):
    pass


def publish_accepted_insight(
    database_path: Path,
    insight_version_id: int,
    vault_root: Path,
    *,
    clock: Clock = utc_now,
    failure_injector: FailureInjector | None = None,
) -> AcceptedPublicationResult:
    """Publish one exact accepted version with file-first crash recovery."""
    if insight_version_id <= 0:
        return _result(
            insight_version_id,
            AcceptedPublicationKind.NOT_ELIGIBLE,
            error_code="accepted_version_not_eligible",
        )

    try:
        with connect(database_path) as connection:
            existing = _load_publication_row(connection, insight_version_id)
            if existing is not None:
                return _already(existing)
            identity = _load_publish_identity(connection, insight_version_id)
    except (sqlite3.Error, TypeError, ValueError):
        return _result(
            insight_version_id,
            AcceptedPublicationKind.FAILED,
            error_code="accepted_publication_database_unreadable",
        )
    if identity is None:
        return _not_eligible(database_path, insight_version_id)

    relative_path = accepted_insight_relative_path(
        identity.insight_id,
        identity.version_no,
    )
    try:
        resolved_vault, target = _resolve_target(vault_root, relative_path)
    except (OSError, ValueError):
        return _result(
            insight_version_id,
            AcceptedPublicationKind.FAILED,
            relative_path=relative_path,
            error_code="accepted_vault_unavailable",
        )

    if _path_is_occupied(target):
        return _recover_window_b(
            database_path,
            identity,
            resolved_vault,
            target,
            relative_path,
            clock=clock,
            failure_injector=failure_injector,
        )

    try:
        context = _load_latest_context(database_path, insight_version_id)
    except sqlite3.Error:
        return _result(
            insight_version_id,
            AcceptedPublicationKind.FAILED,
            relative_path=relative_path,
            error_code="accepted_publication_database_unreadable",
        )
    except (TypeError, ValueError):
        return _result(
            insight_version_id,
            AcceptedPublicationKind.FAILED,
            relative_path=relative_path,
            error_code="accepted_render_context_unreadable",
        )
    if context is None:
        return _result(
            insight_version_id,
            AcceptedPublicationKind.NOT_ELIGIBLE,
            relative_path=relative_path,
            error_code="accepted_version_not_eligible",
        )

    while True:
        try:
            rendered = render_accepted_insight(context, placed_at=clock())
            target.parent.mkdir(parents=True, exist_ok=True)
            if _resolved_inside_vault(target, resolved_vault) != target:
                raise ValueError("Accepted target escaped its resolved Vault")
            result, newer_context = _publish_window_a_once(
                database_path,
                identity,
                resolved_vault,
                target,
                rendered,
                clock=clock,
                failure_injector=failure_injector,
            )
        except (OSError, UnicodeError, ValueError):
            return _result(
                insight_version_id,
                AcceptedPublicationKind.FAILED,
                relative_path=relative_path,
                error_code="accepted_publication_io_failed",
            )
        if result is not None:
            return result
        if newer_context is None:
            return _result(
                insight_version_id,
                AcceptedPublicationKind.NOT_ELIGIBLE,
                relative_path=relative_path,
                error_code="accepted_version_not_eligible",
            )
        context = newer_context


def _load_latest_context(
    database_path: Path,
    insight_version_id: int,
) -> AcceptedInsightRenderContext | None:
    connection = connect(database_path)
    try:
        connection.execute("BEGIN")
        return load_accepted_render_context(connection, insight_version_id)
    finally:
        connection.close()


def _publish_window_a_once(
    database_path: Path,
    identity: _AcceptedPublishIdentity,
    vault_root: Path,
    target: Path,
    rendered: RenderedAcceptedInsight,
    *,
    clock: Clock,
    failure_injector: FailureInjector | None,
) -> tuple[AcceptedPublicationResult | None, AcceptedInsightRenderContext | None]:
    connection = connect(database_path)
    target_was_linked = False
    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = _load_publication_row(connection, identity.insight_version_id)
        if existing is not None:
            connection.rollback()
            return _already(existing), None
        if _path_is_occupied(target):
            result = _recover_window_b_locked(
                connection,
                identity,
                vault_root,
                target,
                rendered.relative_path,
                clock=clock,
                failure_injector=failure_injector,
            )
            if result.kind is AcceptedPublicationKind.RECOVERED:
                _inject(failure_injector, "before_commit")
                connection.commit()
            else:
                connection.rollback()
            return result, None

        locked_context = load_accepted_render_context(
            connection,
            identity.insight_version_id,
        )
        if locked_context is None:
            connection.rollback()
            return None, None
        if (
            accepted_render_context_signature(locked_context)
            != rendered.render_context_signature
        ):
            connection.rollback()
            return None, locked_context

        _preflight_source_assets(vault_root, locked_context)
        linked = _place_complete_no_clobber(
            target,
            rendered.content,
            failure_injector=failure_injector,
        )
        if not linked:
            result = _recover_window_b_locked(
                connection,
                identity,
                vault_root,
                target,
                rendered.relative_path,
                clock=clock,
                failure_injector=failure_injector,
            )
            if result.kind is AcceptedPublicationKind.RECOVERED:
                _inject(failure_injector, "before_commit")
                connection.commit()
            else:
                connection.rollback()
            return result, None
        target_was_linked = True

        publication_id = _insert_publication(
            connection,
            identity,
            rendered,
            placed_at=decode_accepted_placement_receipt_from_content(
                rendered.content
            ).placed_at,
            recorded_at=clock(),
        )
        _inject(failure_injector, "after_publication_insert")
        _inject(failure_injector, "before_commit")
        connection.commit()
        return (
            _result(
                identity.insight_version_id,
                AcceptedPublicationKind.PUBLISHED,
                publication_id=publication_id,
                relative_path=rendered.relative_path,
            ),
            None,
        )
    except sqlite3.Error:
        connection.rollback()
        return (
            _result(
                identity.insight_version_id,
                AcceptedPublicationKind.FAILED,
                relative_path=rendered.relative_path,
                error_code="accepted_publication_database_failed",
            ),
            None,
        )
    except _SourcePreflightError:
        connection.rollback()
        return (
            _result(
                identity.insight_version_id,
                AcceptedPublicationKind.FAILED,
                relative_path=rendered.relative_path,
                error_code="accepted_source_preflight_failed",
            ),
            None,
        )
    except (OSError, UnicodeError, ValueError):
        connection.rollback()
        return (
            _result(
                identity.insight_version_id,
                AcceptedPublicationKind.FAILED,
                relative_path=rendered.relative_path,
                error_code=(
                    "accepted_publication_recoverable_failure"
                    if target_was_linked
                    else "accepted_publication_io_failed"
                ),
            ),
            None,
        )
    finally:
        connection.close()


def _recover_window_b(
    database_path: Path,
    identity: _AcceptedPublishIdentity,
    vault_root: Path,
    target: Path,
    relative_path: str,
    *,
    clock: Clock,
    failure_injector: FailureInjector | None,
) -> AcceptedPublicationResult:
    connection: sqlite3.Connection | None = None
    try:
        connection = connect(database_path)
        connection.execute("BEGIN IMMEDIATE")
        existing = _load_publication_row(connection, identity.insight_version_id)
        if existing is not None:
            connection.rollback()
            return _already(existing)
        result = _recover_window_b_locked(
            connection,
            identity,
            vault_root,
            target,
            relative_path,
            clock=clock,
            failure_injector=failure_injector,
        )
        if result.kind is AcceptedPublicationKind.RECOVERED:
            _inject(failure_injector, "before_commit")
            connection.commit()
        else:
            connection.rollback()
        return result
    except sqlite3.Error:
        if connection is not None:
            connection.rollback()
        return _result(
            identity.insight_version_id,
            AcceptedPublicationKind.FAILED,
            relative_path=relative_path,
            error_code="accepted_publication_database_failed",
        )
    except (OSError, UnicodeError):
        if connection is not None:
            connection.rollback()
        return _result(
            identity.insight_version_id,
            AcceptedPublicationKind.FAILED,
            relative_path=relative_path,
            error_code="accepted_publication_io_failed",
        )
    finally:
        if connection is not None:
            connection.close()


def _recover_window_b_locked(
    connection: sqlite3.Connection,
    identity: _AcceptedPublishIdentity,
    vault_root: Path,
    target: Path,
    relative_path: str,
    *,
    clock: Clock,
    failure_injector: FailureInjector | None,
) -> AcceptedPublicationResult:
    try:
        resolved_target = _resolved_inside_vault(target, vault_root)
    except ValueError:
        resolved_target = None
    if resolved_target != target:
        return _result(
            identity.insight_version_id,
            AcceptedPublicationKind.CONFLICT,
            relative_path=relative_path,
            error_code="accepted_target_conflict",
        )
    target_kind = _target_kind(target)
    if target_kind != "regular":
        return _result(
            identity.insight_version_id,
            AcceptedPublicationKind.CONFLICT,
            relative_path=relative_path,
            error_code="accepted_target_conflict",
        )
    try:
        content = _read_regular_file_no_follow(target)
        receipt = decode_accepted_placement_receipt_from_content(content)
        if (
            receipt.insight_version_id != identity.insight_version_id
            or receipt.judgment_id != identity.judgment_id
            or receipt.relative_path != relative_path
            or receipt.machine_identity
            != accepted_insight_machine_identity(
                identity.insight_id,
                identity.insight_version_id,
            )
        ):
            raise ValueError("Accepted recovery identity does not match")
        context = accepted_render_context_from_dict(receipt.render_context)
        expected = render_accepted_insight(context, placed_at=receipt.placed_at)
        if expected.content != content:
            raise ValueError("Accepted recovery bytes do not match receipt")
        validate_accepted_receipt_snapshot_anchors(
            connection,
            context,
            placed_at=receipt.placed_at,
        )
    except ValueError:
        return _result(
            identity.insight_version_id,
            AcceptedPublicationKind.CONFLICT,
            relative_path=relative_path,
            error_code="accepted_target_conflict",
        )

    publication_id = _insert_publication(
        connection,
        identity,
        expected,
        placed_at=receipt.placed_at,
        recorded_at=clock(),
        actual_content=content,
    )
    _inject(failure_injector, "after_publication_insert")
    return _result(
        identity.insight_version_id,
        AcceptedPublicationKind.RECOVERED,
        publication_id=publication_id,
        relative_path=relative_path,
    )


def _insert_publication(
    connection: sqlite3.Connection,
    identity: _AcceptedPublishIdentity,
    rendered: RenderedAcceptedInsight,
    *,
    placed_at: str,
    recorded_at: str,
    actual_content: bytes | None = None,
) -> int:
    content = rendered.content if actual_content is None else actual_content
    cursor = connection.execute(
        """
        INSERT INTO accepted_insight_publications (
            insight_version_id, judgment_id, relative_path,
            machine_identity, content_sha256, render_context_signature,
            placement_receipt_json, placed_at, recorded_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            identity.insight_version_id,
            identity.judgment_id,
            rendered.relative_path,
            rendered.machine_identity,
            hashlib.sha256(content).hexdigest(),
            rendered.render_context_signature,
            rendered.placement_receipt_json,
            placed_at,
            recorded_at,
        ),
    )
    return int(cursor.lastrowid)


def _preflight_source_assets(
    vault_root: Path,
    context: AcceptedInsightRenderContext,
) -> None:
    try:
        for leaf in context.source_leaves:
            source = _resolve_vault_relative(vault_root, leaf.published_path)
            if _target_kind(source) != "regular":
                raise ValueError("Accepted source asset is not a regular file")
            content = _read_regular_file_no_follow(source)
            markdown = content.decode("utf-8")
            identity = _source_machine_identity(markdown)
            if identity != (
                leaf.platform,
                leaf.platform_item_id,
                leaf.knowledge_result_id,
            ):
                raise ValueError("Accepted source asset identity does not match")
            lines = markdown.splitlines()
            if f"> [!note]- {leaf.point_statement}" not in lines:
                raise ValueError("Accepted source point is missing")
            blocks = _source_blocks(leaf.content_snapshot)
            for evidence in leaf.evidences:
                formal_evidence = KnowledgeEvidence(
                    evidence.evidence_id,
                    evidence.source_fact_id,
                    evidence.start_offset,
                    evidence.end_offset,
                    evidence.evidence_text,
                )
                block = _target_block(formal_evidence, blocks)
                label = _evidence_label(evidence.evidence_text)
                if f"> - [[#^{block.block_id}|{label}]]" not in lines:
                    raise ValueError("Accepted source evidence link is missing")
                if f"{block.text} ^{block.block_id}" not in markdown:
                    raise ValueError("Accepted source block anchor is missing")
    except (OSError, UnicodeError, ValueError) as error:
        raise _SourcePreflightError(
            "Accepted source physical preflight failed"
        ) from error


def _place_complete_no_clobber(
    target: Path,
    content: bytes,
    *,
    failure_injector: FailureInjector | None,
) -> bool:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".knowledge-distiller-accepted-",
        suffix=".tmp",
        dir=target.parent,
    )
    temporary = Path(temporary_name)
    output = None
    try:
        output = os.fdopen(descriptor, "wb", closefd=True)
        _inject(failure_injector, "temp_write")
        written = output.write(content)
        if written != len(content):
            raise OSError("Accepted temporary file write was incomplete")
        _inject(failure_injector, "temp_flush")
        output.flush()
        _inject(failure_injector, "temp_fsync")
        os.fsync(output.fileno())
        _inject(failure_injector, "temp_close")
        output.close()
        output = None
        _inject(failure_injector, "target_link")
        try:
            os.link(temporary, target)
        except FileExistsError:
            return False
        _inject(failure_injector, "after_target_link")
        temporary.unlink()
        _fsync_directory(target.parent, failure_injector=failure_injector)
        return True
    finally:
        if output is not None:
            output.close()
        temporary.unlink(missing_ok=True)


def _fsync_directory(
    directory: Path,
    *,
    failure_injector: FailureInjector | None,
) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(directory, flags)
    try:
        _inject(failure_injector, "parent_fsync")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_regular_file_no_follow(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("Accepted asset is not a regular file")
        chunks = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    finally:
        os.close(descriptor)


def _source_machine_identity(markdown: str) -> tuple[str, str, int] | None:
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
        if not separator or key.strip() in values:
            return None
        values[key.strip()] = value.strip()
    try:
        platform = json.loads(values["kd_material_platform"])
        platform_item_id = json.loads(values["kd_material_item_id"])
        knowledge_result_id = int(values["kd_knowledge_result_id"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if (
        not isinstance(platform, str)
        or not isinstance(platform_item_id, str)
        or knowledge_result_id <= 0
    ):
        return None
    return platform, platform_item_id, knowledge_result_id


def _resolve_target(vault_root: Path, relative_path: str) -> tuple[Path, Path]:
    resolved_vault = Path(vault_root).resolve(strict=True)
    if not resolved_vault.is_dir():
        raise ValueError("Accepted Vault root is not a directory")
    target = _resolve_vault_relative(resolved_vault, relative_path, strict=False)
    return resolved_vault, target


def _resolve_vault_relative(
    vault_root: Path,
    relative_path: str,
    *,
    strict: bool = True,
) -> Path:
    if (
        not isinstance(relative_path, str)
        or relative_path != relative_path.strip()
        or "\\" in relative_path
        or "\x00" in relative_path
    ):
        raise ValueError("Accepted Vault path is unsafe")
    relative = PurePosixPath(relative_path)
    if (
        relative.is_absolute()
        or relative.suffix.lower() != ".md"
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ValueError("Accepted Vault path is unsafe")
    candidate = vault_root.joinpath(*relative.parts)
    resolved_parent = candidate.parent.resolve(strict=strict)
    if not resolved_parent.is_relative_to(vault_root):
        raise ValueError("Accepted Vault path escaped its root")
    resolved = resolved_parent / candidate.name
    if strict and not os.path.lexists(resolved):
        raise FileNotFoundError(resolved)
    return resolved


def _resolved_inside_vault(target: Path, vault_root: Path) -> Path:
    resolved = target.parent.resolve(strict=True) / target.name
    if not resolved.is_relative_to(vault_root):
        raise ValueError("Accepted target escaped its resolved Vault")
    return resolved


def _target_kind(path: Path) -> str:
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return "missing"
    return "regular" if stat.S_ISREG(mode) else "other"


def _path_is_occupied(path: Path) -> bool:
    return os.path.lexists(path)


def _load_publish_identity(
    connection: sqlite3.Connection,
    insight_version_id: int,
) -> _AcceptedPublishIdentity | None:
    row = connection.execute(
        """
        SELECT a.insight_version_id, a.insight_id, a.judgment_id,
               a.judgment_decision, iv.version_no, j.decision
        FROM accepted_insight_versions AS a
        JOIN insight_versions AS iv
          ON iv.insight_version_id = a.insight_version_id
         AND iv.insight_id = a.insight_id
        JOIN user_insight_judgments AS j
          ON j.insight_version_id = a.insight_version_id
         AND j.judgment_id = a.judgment_id
         AND j.decision = a.judgment_decision
        WHERE a.insight_version_id = ?
        """,
        (insight_version_id,),
    ).fetchone()
    if row is None:
        accepted_exists = connection.execute(
            """
            SELECT 1 FROM accepted_insight_versions
            WHERE insight_version_id = ?
            """,
            (insight_version_id,),
        ).fetchone()
        if accepted_exists is not None:
            raise ValueError("Accepted publish identity is broken")
        return None
    if row["judgment_decision"] != "interesting" or row["decision"] != "interesting":
        raise ValueError("Accepted publish judgment is not interesting")
    return _AcceptedPublishIdentity(
        int(row["insight_version_id"]),
        int(row["insight_id"]),
        int(row["version_no"]),
        int(row["judgment_id"]),
    )


def _load_publication_row(
    connection: sqlite3.Connection,
    insight_version_id: int,
) -> _PublicationRow | None:
    row = connection.execute(
        """
        SELECT publication_id, insight_version_id, relative_path
        FROM accepted_insight_publications
        WHERE insight_version_id = ?
        """,
        (insight_version_id,),
    ).fetchone()
    if row is None:
        return None
    return _PublicationRow(
        int(row["publication_id"]),
        int(row["insight_version_id"]),
        str(row["relative_path"]),
    )


def _not_eligible(
    database_path: Path,
    insight_version_id: int,
) -> AcceptedPublicationResult:
    relative_path = None
    try:
        with connect(database_path) as connection:
            row = connection.execute(
                """
                SELECT insight_id, version_no FROM insight_versions
                WHERE insight_version_id = ?
                """,
                (insight_version_id,),
            ).fetchone()
        if row is not None:
            relative_path = accepted_insight_relative_path(
                int(row["insight_id"]),
                int(row["version_no"]),
            )
    except (sqlite3.Error, TypeError, ValueError):
        pass
    return _result(
        insight_version_id,
        AcceptedPublicationKind.NOT_ELIGIBLE,
        relative_path=relative_path,
        error_code="accepted_version_not_eligible",
    )


def _already(row: _PublicationRow) -> AcceptedPublicationResult:
    return _result(
        row.insight_version_id,
        AcceptedPublicationKind.ALREADY_PUBLISHED,
        publication_id=row.publication_id,
        relative_path=row.relative_path,
    )


def _result(
    insight_version_id: int,
    kind: AcceptedPublicationKind,
    *,
    publication_id: int | None = None,
    relative_path: str | None = None,
    error_code: str | None = None,
) -> AcceptedPublicationResult:
    return AcceptedPublicationResult(
        insight_version_id=insight_version_id,
        kind=kind,
        publication_id=publication_id,
        relative_path=relative_path,
        error_code=error_code,
    )


def _inject(injector: FailureInjector | None, point: str) -> None:
    if injector is not None:
        injector(point)
