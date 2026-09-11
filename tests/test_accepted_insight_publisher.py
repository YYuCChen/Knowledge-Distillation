from __future__ import annotations

import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

import knowledge_distiller.legacy.accepted_insight_publisher as publisher_module
from knowledge_distiller.legacy.accepted_insight_publisher import (
    AcceptedPublicationKind,
    publish_accepted_insight,
)
from knowledge_distiller.accepted_insight_renderer import (
    accepted_insight_relative_path,
    decode_accepted_placement_receipt_from_content,
    render_accepted_insight,
)
from knowledge_distiller.database import connect, utc_now
from knowledge_distiller.insight_judgment_service import InsightJudgmentService
from knowledge_distiller.obsidian_renderer import render_task_knowledge_markdown
from tests.fixtures.growth import add_formal_knowledge, empty_growth_plan_payload
from tests.test_accepted_insight_library import _accepted_first
from tests.test_accepted_insight_renderer import _context
from tests.test_insight_judgment_service import (
    _produce_first_version,
    _produce_successor,
    _service_for,
)
from tests.test_organization_service import _service


PLACED_AT = "2026-08-21T11:00:00+00:00"
LATER_AT = "2026-08-21T12:00:00+00:00"


def _clock(value=PLACED_AT):
    return lambda: value


def _write_source_assets(database_path, vault_root):
    with connect(database_path) as connection:
        rows = connection.execute(
            """
            SELECT t.task_id, kr.published_path
            FROM tasks AS t
            JOIN materials AS m ON m.material_id = t.material_id
            JOIN knowledge_results AS kr
              ON kr.knowledge_result_id = m.current_knowledge_result_id
            WHERE kr.published_path IS NOT NULL
            ORDER BY t.task_id
            """
        ).fetchall()
    for row in rows:
        target = vault_root / str(row["published_path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            render_task_knowledge_markdown(
                database_path,
                int(row["task_id"]),
            ).markdown,
            encoding="utf-8",
        )


def _setup_current(tmp_path, name="accepted"):
    database_path = tmp_path / f"{name}.sqlite3"
    vault_root = tmp_path / f"{name}-vault"
    vault_root.mkdir()
    insight_id, version_id = _accepted_first(database_path)
    _write_source_assets(database_path, vault_root)
    return database_path, vault_root, insight_id, version_id


def _publication_rows(database_path):
    with connect(database_path) as connection:
        return connection.execute(
            """
            SELECT * FROM accepted_insight_publications
            ORDER BY publication_id
            """
        ).fetchall()


def _database_without_publications(database_path):
    with sqlite3.connect(database_path) as connection:
        tables = tuple(
            row[0]
            for row in connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                  AND name != 'accepted_insight_publications'
                ORDER BY name
                """
            )
        )
        return tuple(
            (
                table,
                tuple(
                    tuple(row)
                    for row in connection.execute(
                        f'SELECT * FROM "{table}" ORDER BY rowid'
                    )
                ),
            )
            for table in tables
        )


@pytest.mark.parametrize("historical", [False, True])
def test_current_and_historical_publish_exact_file_first_fact(tmp_path, historical):
    path, vault, insight_id, version_id = _setup_current(tmp_path, str(historical))
    if historical:
        successor = _produce_successor(path, insight_id, version_id, "historical")
        assert _service_for(path).record_judgment(
            successor,
            "interesting",
        ).kind == "recorded"
    _write_source_assets(path, vault)
    context = _context(path, version_id)
    expected = render_accepted_insight(context, placed_at=PLACED_AT)
    before = _database_without_publications(path)

    result = publish_accepted_insight(
        path,
        version_id,
        vault,
        clock=_clock(),
    )

    assert result.kind is AcceptedPublicationKind.PUBLISHED
    assert result.insight_version_id == version_id
    assert result.publication_id is not None
    assert result.relative_path == expected.relative_path
    target = vault / result.relative_path
    assert target.read_bytes() == expected.content
    receipt = decode_accepted_placement_receipt_from_content(target.read_bytes())
    assert receipt.insight_version_id == version_id
    assert receipt.judgment_id == context.root.judgment.judgment_id
    assert receipt.render_context_signature == expected.render_context_signature
    assert receipt.render_context["root"]["current_role"] == (
        "historical" if historical else "current"
    )
    rows = _publication_rows(path)
    assert len(rows) == 1
    row = rows[0]
    assert int(row["publication_id"]) == result.publication_id
    assert int(row["insight_version_id"]) == version_id
    assert int(row["judgment_id"]) == context.root.judgment.judgment_id
    assert str(row["content_sha256"]) == expected.content_sha256
    assert str(row["render_context_signature"]) == expected.render_context_signature
    assert str(row["placement_receipt_json"]) == expected.placement_receipt_json
    assert str(row["placed_at"]) == PLACED_AT
    assert _database_without_publications(path) == before
    with connect(path) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("decision", [None, "rethink"])
def test_pending_and_rethink_are_not_eligible_and_create_nothing(tmp_path, decision):
    path = tmp_path / f"{decision or 'pending'}.sqlite3"
    vault = tmp_path / f"{decision or 'pending'}-vault"
    vault.mkdir()
    insight_id, version_id = _produce_first_version(path)
    if decision is not None:
        assert _service_for(path).record_judgment(
            version_id,
            decision,
        ).kind == "recorded"

    result = publish_accepted_insight(path, version_id, vault, clock=_clock())

    assert result.kind is AcceptedPublicationKind.NOT_ELIGIBLE
    assert result.publication_id is None
    assert result.relative_path == accepted_insight_relative_path(insight_id, 1)
    assert not (vault / result.relative_path).exists()
    assert _publication_rows(path) == []


def test_formally_damaged_accepted_is_failed_not_not_eligible(tmp_path):
    path, vault, _, version_id = _setup_current(tmp_path, "damaged")
    with connect(path) as connection:
        connection.execute("DROP TRIGGER insight_versions_cannot_be_updated")
        connection.execute(
            """
            UPDATE insight_versions SET semantic_signature = ?
            WHERE insight_version_id = ?
            """,
            ("0" * 64, version_id),
        )

    result = publish_accepted_insight(path, version_id, vault, clock=_clock())

    assert result.kind is AcceptedPublicationKind.FAILED
    assert result.error_code in {
        "accepted_render_context_unreadable",
        "accepted_source_preflight_failed",
    }
    assert result.publication_id is None
    assert _publication_rows(path) == []


def test_already_published_never_accesses_or_repairs_the_file(tmp_path, monkeypatch):
    path, vault, _, version_id = _setup_current(tmp_path, "already")
    first = publish_accepted_insight(path, version_id, vault, clock=_clock())
    target = vault / first.relative_path
    target.unlink()
    monkeypatch.setattr(
        publisher_module,
        "_resolve_target",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("ALREADY accessed the Vault")
        ),
    )

    repeated = publish_accepted_insight(
        path,
        version_id,
        tmp_path / "missing-vault",
        clock=_clock(),
    )

    assert repeated.kind is AcceptedPublicationKind.ALREADY_PUBLISHED
    assert repeated.publication_id == first.publication_id
    assert repeated.relative_path == first.relative_path
    assert not target.exists()
    assert len(_publication_rows(path)) == 1


@pytest.mark.parametrize(
    "damage",
    [
        "missing",
        "unsafe",
        "symlink",
        "outside",
        "non_utf8",
        "frontmatter",
        "point",
        "evidence_label",
        "source_link",
        "block",
    ],
)
def test_source_physical_preflight_is_all_or_nothing(tmp_path, damage):
    path, vault, _, version_id = _setup_current(tmp_path, damage)
    context = _context(path, version_id)
    leaf = context.source_leaves[0]
    source = vault / leaf.published_path

    if damage == "missing":
        source.unlink()
    elif damage == "unsafe":
        with connect(path) as connection:
            connection.execute(
                """
                UPDATE knowledge_results SET published_path = '../escape.md'
                WHERE knowledge_result_id = ?
                """,
                (leaf.knowledge_result_id,),
            )
    elif damage == "symlink":
        outside = tmp_path / "source-outside.md"
        outside.write_bytes(source.read_bytes())
        source.unlink()
        source.symlink_to(outside)
    elif damage == "outside":
        outside = tmp_path / "outside-source"
        outside.mkdir()
        (outside / "source.md").write_bytes(source.read_bytes())
        (vault / "escape").symlink_to(outside, target_is_directory=True)
        with connect(path) as connection:
            connection.execute(
                """
                UPDATE knowledge_results SET published_path = 'escape/source.md'
                WHERE knowledge_result_id = ?
                """,
                (leaf.knowledge_result_id,),
            )
    elif damage == "non_utf8":
        source.write_bytes(b"\xff\xfe")
    else:
        markdown = source.read_text(encoding="utf-8")
        if damage == "frontmatter":
            markdown = markdown.replace(
                f'kd_material_item_id: "{leaf.platform_item_id}"',
                'kd_material_item_id: "wrong-item"',
                1,
            )
        elif damage == "point":
            markdown = markdown.replace(
                f"> [!note]- {leaf.point_statement}",
                "> [!note]- changed point",
                1,
            )
        elif damage == "evidence_label":
            markdown = markdown.replace("|来源：", "|损坏：", 1)
        elif damage == "source_link":
            markdown = markdown.replace("[[#^source-1|", "[[#^source-9|", 1)
        else:
            markdown = markdown.replace(
                f"{leaf.content_snapshot} ^source-1",
                leaf.content_snapshot,
                1,
            )
        source.write_text(markdown, encoding="utf-8")

    result = publish_accepted_insight(path, version_id, vault, clock=_clock())

    assert result.kind is AcceptedPublicationKind.FAILED
    target = vault / accepted_insight_relative_path(
        context.insight_id,
        context.version_no,
    )
    assert not target.exists()
    assert _publication_rows(path) == []


@pytest.mark.parametrize("occupancy", ["file", "directory", "symlink", "broken"])
def test_target_nonmatching_occupancy_is_conflict_without_clobber(tmp_path, occupancy):
    path, vault, _, version_id = _setup_current(tmp_path, occupancy)
    context = _context(path, version_id)
    target = vault / accepted_insight_relative_path(
        context.insight_id,
        context.version_no,
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / f"{occupancy}-outside.md"
    if occupancy == "file":
        target.write_text("# User file\n", encoding="utf-8")
    elif occupancy == "directory":
        target.mkdir()
    elif occupancy == "symlink":
        outside.write_text("# Outside\n", encoding="utf-8")
        target.symlink_to(outside)
    else:
        target.symlink_to(tmp_path / "does-not-exist.md")
    before = os.lstat(target)

    result = publish_accepted_insight(path, version_id, vault, clock=_clock())

    assert result.kind is AcceptedPublicationKind.CONFLICT
    after = os.lstat(target)
    assert (after.st_ino, after.st_mode, after.st_size) == (
        before.st_ino,
        before.st_mode,
        before.st_size,
    )
    assert _publication_rows(path) == []


def test_same_machine_identity_with_different_bytes_is_conflict(tmp_path):
    path, vault, _, version_id = _setup_current(tmp_path, "same-identity")
    context = _context(path, version_id)
    rendered = render_accepted_insight(context, placed_at=PLACED_AT)
    target = vault / rendered.relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    changed = rendered.content.replace(b"## New", b"## Changed", 1)
    if changed == rendered.content:
        changed = rendered.content.replace("## 新知主句".encode(), b"## tampered", 1)
    target.write_bytes(changed)

    result = publish_accepted_insight(path, version_id, vault, clock=_clock())

    assert result.kind is AcceptedPublicationKind.CONFLICT
    assert target.read_bytes() == changed
    assert _publication_rows(path) == []


def test_link_file_exists_race_enters_window_b_and_reports_conflict(
    tmp_path,
    monkeypatch,
):
    path, vault, _, version_id = _setup_current(tmp_path, "link-race")
    context = _context(path, version_id)
    target = vault / accepted_insight_relative_path(
        context.insight_id,
        context.version_no,
    )
    user_content = b"# Concurrent user file\n"

    def occupy_then_fail(_temporary, target_name):
        publisher_module.Path(target_name).write_bytes(user_content)
        raise FileExistsError(target_name)

    monkeypatch.setattr(publisher_module.os, "link", occupy_then_fail)

    result = publish_accepted_insight(path, version_id, vault, clock=_clock())

    assert result.kind is AcceptedPublicationKind.CONFLICT
    assert target.read_bytes() == user_content
    assert _publication_rows(path) == []


@pytest.mark.parametrize(
    "failure_point",
    ["temp_write", "temp_flush", "temp_fsync", "temp_close", "target_link"],
)
def test_prelink_io_failures_leave_no_target_or_success(tmp_path, failure_point):
    path, vault, _, version_id = _setup_current(tmp_path, failure_point)
    context = _context(path, version_id)

    def fail(point):
        if point == failure_point:
            raise OSError(f"simulated {point} failure")

    result = publish_accepted_insight(
        path,
        version_id,
        vault,
        clock=_clock(),
        failure_injector=fail,
    )

    assert result.kind is AcceptedPublicationKind.FAILED
    target = vault / accepted_insight_relative_path(
        context.insight_id,
        context.version_no,
    )
    assert not target.exists()
    assert _publication_rows(path) == []
    assert not list(target.parent.glob(".knowledge-distiller-accepted-*.tmp"))


@pytest.mark.parametrize(
    "failure_point",
    [
        "after_target_link",
        "parent_fsync",
        "after_publication_insert",
        "before_commit",
    ],
)
def test_postlink_failures_recover_without_touching_target(tmp_path, failure_point):
    path, vault, _, version_id = _setup_current(tmp_path, failure_point)

    def fail(point):
        if point == failure_point:
            raise OSError(f"simulated {point} failure")

    failed = publish_accepted_insight(
        path,
        version_id,
        vault,
        clock=utc_now,
        failure_injector=fail,
    )
    target = vault / failed.relative_path

    assert failed.kind is AcceptedPublicationKind.FAILED
    assert target.is_file()
    assert _publication_rows(path) == []
    before = target.stat()
    before_bytes = target.read_bytes()
    receipt = decode_accepted_placement_receipt_from_content(before_bytes)

    recovered = publish_accepted_insight(
        path,
        version_id,
        vault,
        clock=utc_now,
    )

    after = target.stat()
    assert recovered.kind is AcceptedPublicationKind.RECOVERED
    assert recovered.publication_id is not None
    assert target.read_bytes() == before_bytes
    assert (after.st_ino, after.st_mtime_ns, after.st_mode) == (
        before.st_ino,
        before.st_mtime_ns,
        before.st_mode,
    )
    row = _publication_rows(path)[0]
    assert str(row["placed_at"]) == receipt.placed_at
    assert str(row["recorded_at"])


def test_window_b_rejects_forged_immutable_participant_anchor(tmp_path):
    path, vault, _, version_id = _setup_current(tmp_path, "forged-anchor")
    context = _context(path, version_id)
    forged_participant = replace(
        context.root.participants[0],
        contribution_text="Forged contribution",
    )
    forged_root = replace(
        context.root,
        participants=(forged_participant, *context.root.participants[1:]),
    )
    forged_context = replace(
        context,
        root=forged_root,
        lineage_nodes=(forged_root, *context.lineage_nodes[1:]),
    )
    forged = render_accepted_insight(forged_context, placed_at=PLACED_AT)
    target = vault / forged.relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(forged.content)

    result = publish_accepted_insight(path, version_id, vault, clock=_clock())

    assert result.kind is AcceptedPublicationKind.CONFLICT
    assert target.read_bytes() == forged.content
    assert _publication_rows(path) == []


def test_window_b_recovers_old_snapshot_after_role_exit_and_additional_fact(tmp_path):
    path, vault, insight_id, version_id = _setup_current(tmp_path, "role-drift")

    def fail_after_insert(point):
        if point == "after_publication_insert":
            raise OSError("simulated database failure")

    failed = publish_accepted_insight(
        path,
        version_id,
        vault,
        clock=_clock(),
        failure_injector=fail_after_insert,
    )
    target = vault / failed.relative_path
    before = target.stat()
    before_bytes = target.read_bytes()

    successor = _produce_successor(path, insight_id, version_id, "later-current")
    assert InsightJudgmentService(
        path,
        now=_clock(LATER_AT),
    ).record_judgment(
        successor,
        "interesting",
    ).kind == "recorded"
    _, source_id = add_formal_knowledge(path, "later-exit")
    plan = empty_growth_plan_payload()
    plan["new_input_reviews"] = [
        {
            "knowledge_result_id": source_id,
            "outcome": "considered_no_formal_result",
            "reason_text": "Establish a later event",
        }
    ]
    service, _, _ = _service(path, plan=plan)
    event_id = service.start_or_reuse().event_id
    assert service.drive(event_id).event.status == "succeeded"
    with connect(path) as connection:
        connection.execute(
            """
            INSERT INTO insight_version_disqualifications (
                insight_version_id, fact_kind, event_id,
                reason_text, created_at
            ) VALUES (?, 'basis_invalid', ?, 'Later basis loss', ?)
            """,
            (version_id, event_id, "2026-08-21T13:00:00+00:00"),
        )

    recovered = publish_accepted_insight(
        path,
        version_id,
        vault,
        clock=_clock("2026-08-21T14:00:00+00:00"),
    )

    after = target.stat()
    assert recovered.kind is AcceptedPublicationKind.RECOVERED
    assert target.read_bytes() == before_bytes
    assert (after.st_ino, after.st_mtime_ns, after.st_mode) == (
        before.st_ino,
        before.st_mtime_ns,
        before.st_mode,
    )


def test_window_b_allows_later_predecessor_publication_navigation(tmp_path):
    path, vault, insight_id, first = _setup_current(tmp_path, "later-navigation")
    second = _produce_successor(path, insight_id, first, "second")
    assert _service_for(path).record_judgment(second, "interesting").kind == "recorded"
    _write_source_assets(path, vault)

    def fail_after_insert(point):
        if point == "after_publication_insert":
            raise OSError("simulated database failure")

    failed = publish_accepted_insight(
        path,
        second,
        vault,
        clock=utc_now,
        failure_injector=fail_after_insert,
    )
    assert failed.kind is AcceptedPublicationKind.FAILED
    predecessor = publish_accepted_insight(
        path,
        first,
        vault,
        clock=utc_now,
    )
    assert predecessor.kind is AcceptedPublicationKind.PUBLISHED

    recovered = publish_accepted_insight(
        path,
        second,
        vault,
        clock=utc_now,
    )

    assert recovered.kind is AcceptedPublicationKind.RECOVERED
    assert len(_publication_rows(path)) == 2


def test_window_a_context_drift_rerenders_before_any_target_is_linked(
    tmp_path,
    monkeypatch,
):
    path, vault, insight_id, version_id = _setup_current(tmp_path, "window-a-drift")
    real_publish_once = publisher_module._publish_window_a_once
    changed = False

    def drift_then_publish(*args, **kwargs):
        nonlocal changed
        if not changed:
            changed = True
            successor = _produce_successor(
                path,
                insight_id,
                version_id,
                "lock-drift",
            )
            assert _service_for(path).record_judgment(
                successor,
                "interesting",
            ).kind == "recorded"
        return real_publish_once(*args, **kwargs)

    monkeypatch.setattr(
        publisher_module,
        "_publish_window_a_once",
        drift_then_publish,
    )

    result = publish_accepted_insight(path, version_id, vault, clock=_clock())

    assert changed
    assert result.kind is AcceptedPublicationKind.PUBLISHED
    receipt = decode_accepted_placement_receipt_from_content(
        (vault / result.relative_path).read_bytes()
    )
    assert receipt.render_context["root"]["current_role"] == "historical"
    assert len(_publication_rows(path)) == 1


def test_two_concurrent_publishers_create_one_target_and_one_fact(tmp_path):
    path, vault, _, version_id = _setup_current(tmp_path, "concurrent")

    def publish():
        return publish_accepted_insight(path, version_id, vault, clock=_clock())

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(lambda _value: publish(), range(2)))

    assert {result.kind for result in results} == {
        AcceptedPublicationKind.PUBLISHED,
        AcceptedPublicationKind.ALREADY_PUBLISHED,
    }
    assert len({result.publication_id for result in results}) == 1
    assert len(_publication_rows(path)) == 1
    targets = list((vault / "知识蒸馏器" / "新知").glob("*.md"))
    assert len(targets) == 1
    assert not list(targets[0].parent.glob("*.tmp"))
