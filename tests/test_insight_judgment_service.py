from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from knowledge_distiller.database import connect
from knowledge_distiller.insight_judgment_service import (
    InsightJudgmentService,
    JudgmentResultKind,
)
from tests.fixtures.growth import (
    add_formal_knowledge,
    empty_growth_plan_payload,
    insight_payload,
    source_participant,
)
from tests.test_organization_service import (
    _core_replacement_plan,
    _productive_plan,
    _service,
)


NOW = "2026-08-21T10:00:00+00:00"


def _produce_first_version(path):
    _, kr_a = add_formal_knowledge(path, "a")
    _, kr_b = add_formal_knowledge(path, "b")
    service, _, _ = _service(path, plan=_productive_plan(kr_a, kr_b))
    event_id = service.start_or_reuse().event_id
    result = service.drive(event_id)
    assert result.event.status == "succeeded"
    with connect(path) as connection:
        row = connection.execute(
            """
            SELECT insight_id, insight_version_id
            FROM insight_versions WHERE produced_event_id = ?
            """,
            (event_id,),
        ).fetchone()
    return int(row["insight_id"]), int(row["insight_version_id"])


def _produce_successor(path, insight_id, previous_version_id, suffix):
    _, kr_a = add_formal_knowledge(path, f"{suffix}-a")
    _, kr_b = add_formal_knowledge(path, f"{suffix}-b")
    plan = empty_growth_plan_payload()
    plan["new_input_reviews"] = [
        {
            "knowledge_result_id": value,
            "outcome": "participated",
            "reason_text": "Contributed to the evolved candidate",
        }
        for value in (kr_a, kr_b)
    ]
    plan["candidate_versions"] = [
        {
            "new_insight_key": f"i-{suffix}",
            "target_kind": "evolve_identity",
            "insight_id": insight_id,
            "previous_insight_version_id": previous_version_id,
            "payload": insight_payload(claim=f"Evolved claim {suffix}"),
            "participants": [
                source_participant("a", kr_a, position=0),
                source_participant("b", kr_b, position=1),
            ],
            "used_relations": [],
        }
    ]
    plan["rejected_outputs"] = []
    service, _, _ = _service(path, plan=plan)
    event_id = service.start_or_reuse().event_id
    result = service.drive(event_id)
    assert result.event.status == "succeeded"
    with connect(path) as connection:
        return int(
            connection.execute(
                "SELECT insight_version_id FROM insight_versions WHERE produced_event_id = ?",
                (event_id,),
            ).fetchone()[0]
        )


def _service_for(path, *, injector=None):
    return InsightJudgmentService(
        path,
        now=lambda: NOW,
        failure_injector=injector,
    )


def test_interesting_and_rethink_have_exact_append_only_consequences(tmp_path):
    """E2E-27/28: exact judgments create only their authorized hard consequences."""
    path = tmp_path / "judgments.sqlite3"
    _, v1 = _produce_first_version(path)

    interesting = _service_for(path).record_judgment(
        v1,
        "interesting",
        "  Keep this exact annotation.  ",
    )

    assert interesting.kind is JudgmentResultKind.RECORDED
    assert interesting.judgment.annotation_text == "  Keep this exact annotation.  "
    assert interesting.judgment.accepted_initial_role == "current"
    assert interesting.judgment.accepted_current_role == "current"

    path2 = tmp_path / "rethink.sqlite3"
    _, rethink_version = _produce_first_version(path2)
    rethink = _service_for(path2).record_judgment(
        rethink_version,
        "rethink",
        " \t\n ",
    )
    assert rethink.kind is JudgmentResultKind.RECORDED
    assert rethink.judgment.annotation_text is None
    assert rethink.judgment.accepted_current_role is None
    with connect(path2) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM accepted_insight_versions"
        ).fetchone()[0] == 0


def test_same_retry_is_idempotent_but_decision_or_annotation_change_conflicts(tmp_path):
    path = tmp_path / "idempotency.sqlite3"
    _, version_id = _produce_first_version(path)
    service = _service_for(path)

    first = service.record_judgment(version_id, "interesting", "note")
    same = service.record_judgment(version_id, "interesting", "note")
    changed_annotation = service.record_judgment(
        version_id, "interesting", "other note"
    )
    changed_decision = service.record_judgment(version_id, "rethink", "note")

    assert first.kind is JudgmentResultKind.RECORDED
    assert same.kind is JudgmentResultKind.ALREADY_RECORDED
    assert same.judgment == first.judgment
    assert changed_annotation.kind is JudgmentResultKind.CONFLICT
    assert changed_decision.kind is JudgmentResultKind.CONFLICT
    with connect(path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM user_insight_judgments"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM accepted_insight_versions"
        ).fetchone()[0] == 1


@pytest.mark.parametrize("newer_decision", [None, "rethink"])
def test_late_older_interesting_can_be_current_when_newer_never_current(
    tmp_path,
    newer_decision,
):
    """E2E-30: a newer pending/rethink version does not steal current."""
    path = tmp_path / f"late-{newer_decision}.sqlite3"
    insight_id, v1 = _produce_first_version(path)
    v2 = _produce_successor(path, insight_id, v1, "v2")
    if newer_decision is not None:
        assert _service_for(path).record_judgment(v2, newer_decision).kind is (
            JudgmentResultKind.RECORDED
        )

    result = _service_for(path).record_judgment(v1, "interesting")

    assert result.kind is JudgmentResultKind.RECORDED
    assert result.judgment.accepted_current_role == "current"
    with connect(path) as connection:
        assert connection.execute(
            "SELECT current_role FROM accepted_insight_versions WHERE insight_version_id = ?",
            (v1,),
        ).fetchone()[0] == "current"


def test_newer_interesting_retires_older_current_and_old_never_resurrects(tmp_path):
    path = tmp_path / "retirement.sqlite3"
    insight_id, v1 = _produce_first_version(path)
    v2 = _produce_successor(path, insight_id, v1, "v2")
    v3 = _produce_successor(path, insight_id, v2, "v3")
    service = _service_for(path)

    assert service.record_judgment(v1, "interesting").judgment.accepted_current_role == "current"
    assert service.record_judgment(v2, "interesting").judgment.accepted_current_role == "current"
    assert service.record_judgment(v3, "interesting").judgment.accepted_current_role == "current"

    with connect(path) as connection:
        roles = connection.execute(
            """
            SELECT iv.version_no, a.initial_role, a.current_role, a.historical_reason
            FROM accepted_insight_versions AS a
            JOIN insight_versions AS iv ON iv.insight_version_id = a.insight_version_id
            ORDER BY iv.version_no
            """
        ).fetchall()
    assert [tuple(row) for row in roles] == [
        (1, "current", "historical", "newer_accepted_current"),
        (2, "current", "historical", "newer_accepted_current"),
        (3, "current", "current", None),
    ]


def test_late_older_interesting_is_historical_while_newer_is_current(tmp_path):
    path = tmp_path / "ever-current.sqlite3"
    insight_id, v1 = _produce_first_version(path)
    v2 = _produce_successor(path, insight_id, v1, "v2")
    v3 = _produce_successor(path, insight_id, v2, "v3")
    service = _service_for(path)
    service.record_judgment(v2, "interesting")
    service.record_judgment(v3, "interesting")

    late = service.record_judgment(v1, "interesting")

    assert late.kind is JudgmentResultKind.RECORDED
    assert late.judgment.accepted_initial_role == "historical"
    assert late.judgment.accepted_historical_reason == "born_older_than_current"


def test_late_interesting_after_formal_identity_replacement_is_historical(tmp_path):
    """E2E-31: replacement retires the old identity without accepting the new one."""
    path = tmp_path / "replacement.sqlite3"
    insight_id, old_version = _produce_first_version(path)
    _, kr_c = add_formal_knowledge(path, "replacement-c")
    _, kr_d = add_formal_knowledge(path, "replacement-d")
    replacement, _, _ = _service(
        path,
        plan=_core_replacement_plan(insight_id, kr_c, kr_d),
    )
    event_id = replacement.start_or_reuse().event_id
    assert replacement.drive(event_id).event.status == "succeeded"

    late = _service_for(path).record_judgment(old_version, "interesting")

    assert late.kind is JudgmentResultKind.RECORDED
    assert late.judgment.accepted_initial_role == "historical"
    assert late.judgment.accepted_historical_reason == "identity_replaced"
    with connect(path) as connection:
        replacement_version = int(
            connection.execute(
                "SELECT insight_version_id FROM insight_versions WHERE produced_event_id = ?",
                (event_id,),
            ).fetchone()[0]
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM user_insight_judgments WHERE insight_version_id = ?",
            (replacement_version,),
        ).fetchone()[0] == 0


def test_late_interesting_projects_one_preestablished_disqualification_as_historical(
    tmp_path,
):
    """A-scope projection only: this does not create a disqualification producer."""
    path = tmp_path / "preestablished-disqualification.sqlite3"
    _, version_id = _produce_first_version(path)
    with connect(path) as connection:
        event_id = int(
            connection.execute(
                "SELECT produced_event_id FROM insight_versions WHERE insight_version_id = ?",
                (version_id,),
            ).fetchone()[0]
        )
        connection.execute(
            """
            INSERT INTO insight_version_disqualifications (
                insight_version_id, fact_kind, event_id,
                reason_text, created_at
            ) VALUES (?, 'basis_invalid', ?, 'Pre-established fixture fact', ?)
            """,
            (version_id, event_id, NOW),
        )

    result = _service_for(path).record_judgment(version_id, "interesting")

    assert result.kind is JudgmentResultKind.RECORDED
    assert result.judgment.accepted_initial_role == "historical"
    assert result.judgment.accepted_historical_reason == "basis_invalid"


def test_same_event_dual_disqualification_is_born_historical_with_refuted_primary(
    tmp_path,
):
    path = tmp_path / "dual-disqualification.sqlite3"
    _, version_id = _produce_first_version(path)
    with connect(path) as connection:
        event_id = int(
            connection.execute(
                "SELECT produced_event_id FROM insight_versions WHERE insight_version_id = ?",
                (version_id,),
            ).fetchone()[0]
        )
        connection.executemany(
            """
            INSERT INTO insight_version_disqualifications (
                insight_version_id, fact_kind, event_id,
                reason_text, created_at
            ) VALUES (?, ?, ?, 'Pre-established fixture fact', ?)
            """,
            [
                (version_id, "basis_invalid", event_id, NOW),
                (version_id, "refuted", event_id, NOW),
            ],
        )

    result = _service_for(path).record_judgment(version_id, "interesting")

    assert result.kind is JudgmentResultKind.RECORDED
    assert result.judgment.accepted_initial_role == "historical"
    assert result.judgment.accepted_historical_reason == "refuted"
    with connect(path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM user_insight_judgments"
        ).fetchone()[0] == 1
        assert tuple(
            connection.execute(
                """
                SELECT current_role, historical_reason, caused_by_event_id
                FROM accepted_insight_versions
                WHERE insight_version_id = ?
                """,
                (version_id,),
            ).fetchone()
        ) == ("historical", "refuted", event_id)


def test_different_event_disqualifications_use_first_established_primary(tmp_path):
    path = tmp_path / "ordered-disqualification.sqlite3"
    _, version_id = _produce_first_version(path)
    with connect(path) as connection:
        first_event_id = int(
            connection.execute(
                "SELECT produced_event_id FROM insight_versions WHERE insight_version_id = ?",
                (version_id,),
            ).fetchone()[0]
        )
    _, later_source = add_formal_knowledge(path, "later-cause")
    later_plan = empty_growth_plan_payload()
    later_plan["new_input_reviews"] = [
        {
            "knowledge_result_id": later_source,
            "outcome": "considered_no_formal_result",
            "reason_text": "Establish a later event for ordered facts",
        }
    ]
    later_service, _, _ = _service(path, plan=later_plan)
    later_event_id = later_service.start_or_reuse().event_id
    assert later_service.drive(later_event_id).event.status == "succeeded"
    assert first_event_id < later_event_id
    with connect(path) as connection:
        connection.executemany(
            """
            INSERT INTO insight_version_disqualifications (
                insight_version_id, fact_kind, event_id,
                reason_text, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    version_id,
                    "basis_invalid",
                    first_event_id,
                    "First established cause",
                    NOW,
                ),
                (
                    version_id,
                    "refuted",
                    later_event_id,
                    "Later additional cause",
                    NOW,
                ),
            ],
        )

    result = _service_for(path).record_judgment(version_id, "interesting")

    assert result.kind is JudgmentResultKind.RECORDED
    assert result.judgment.accepted_historical_reason == "basis_invalid"


def test_same_event_replacement_and_disqualification_without_stored_primary_is_unreadable(
    tmp_path,
):
    path = tmp_path / "same-event-uncalibrated-tie.sqlite3"
    old_insight_id, version_id = _produce_first_version(path)
    _, kr_c = add_formal_knowledge(path, "tie-c")
    _, kr_d = add_formal_knowledge(path, "tie-d")
    replacement_plan = _productive_plan(kr_c, kr_d)
    replacement_plan["new_relations"][0]["payload"]["relation_statement"] = (
        "A separate relation creates the replacement fixture"
    )
    replacement_plan["candidate_versions"][0]["payload"]["claim"] = (
        "A separate replacement fixture claim"
    )
    replacement, _, _ = _service(path, plan=replacement_plan)
    event_id = replacement.start_or_reuse().event_id
    assert replacement.drive(event_id).event.status == "succeeded"
    with connect(path) as connection:
        replacement_insight_id = int(
            connection.execute(
                """
                SELECT insight_id FROM insight_versions
                WHERE produced_event_id = ?
                """,
                (event_id,),
            ).fetchone()[0]
        )
        connection.execute(
            """
            INSERT INTO insight_identity_replacements (
                replaced_insight_id, replacement_insight_id, event_id,
                reason_text, created_at
            ) VALUES (?, ?, ?, 'Fixture replacement', ?)
            """,
            (old_insight_id, replacement_insight_id, event_id, NOW),
        )
        connection.execute(
            """
            INSERT INTO insight_version_disqualifications (
                insight_version_id, fact_kind, event_id,
                reason_text, created_at
            ) VALUES (?, 'refuted', ?, 'Fixture refutation', ?)
            """,
            (version_id, event_id, NOW),
        )

    result = _service_for(path).record_judgment(version_id, "interesting")

    assert result.kind is JudgmentResultKind.UNREADABLE
    with connect(path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM user_insight_judgments WHERE insight_version_id = ?",
            (version_id,),
        ).fetchone()[0] == 0


@pytest.mark.parametrize(
    "failure_point",
    [
        "after_judgment_insert",
        "after_current_retirement",
        "before_accepted_insert",
        "after_accepted_insert",
    ],
)
def test_system_exit_crash_reopens_cleanly_and_explicit_retry_succeeds(
    tmp_path,
    failure_point,
):
    """§13.4B: crash leaves the old current intact and the target retryable."""
    path = tmp_path / f"crash-{failure_point}.sqlite3"
    insight_id, v1 = _produce_first_version(path)
    v2 = _produce_successor(path, insight_id, v1, "v2")
    assert _service_for(path).record_judgment(v1, "interesting").kind is (
        JudgmentResultKind.RECORDED
    )
    crashed = False

    def inject(point, _connection):
        nonlocal crashed
        if point == failure_point and not crashed:
            crashed = True
            raise SystemExit("simulated judgment crash")

    with pytest.raises(SystemExit, match="simulated judgment crash"):
        _service_for(path, injector=inject).record_judgment(v2, "interesting")

    with connect(path) as reopened:
        assert [
            tuple(row)
            for row in reopened.execute(
                """
                SELECT insight_version_id, decision
                FROM user_insight_judgments ORDER BY judgment_id
                """
            ).fetchall()
        ] == [(v1, "interesting")]
        assert [
            tuple(row)
            for row in reopened.execute(
                """
                SELECT insight_version_id, current_role, historical_reason
                FROM accepted_insight_versions ORDER BY insight_version_id
                """
            ).fetchall()
        ] == [(v1, "current", None)]
        assert reopened.execute(
            """
            SELECT COUNT(*) FROM accepted_insight_versions
            WHERE current_role = 'current'
            """
        ).fetchone()[0] == 1
        assert reopened.execute("PRAGMA foreign_key_check").fetchall() == []

    retried = _service_for(path).record_judgment(v2, "interesting")

    assert retried.kind is JudgmentResultKind.RECORDED
    assert retried.judgment.accepted_current_role == "current"
    with connect(path) as reopened:
        assert [
            tuple(row)
            for row in reopened.execute(
                """
                SELECT iv.version_no, a.current_role, a.historical_reason
                FROM accepted_insight_versions AS a
                JOIN insight_versions AS iv
                  ON iv.insight_version_id = a.insight_version_id
                ORDER BY iv.version_no
                """
            ).fetchall()
        ] == [
            (1, "historical", "newer_accepted_current"),
            (2, "current", None),
        ]
        assert reopened.execute(
            """
            SELECT COUNT(*) FROM accepted_insight_versions
            WHERE current_role = 'current'
            """
        ).fetchone()[0] == 1
        assert reopened.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize(
    ("left", "right", "expected_kinds"),
    [
        (("interesting", None), ("interesting", None), {"recorded", "already_recorded"}),
        (("rethink", None), ("rethink", None), {"recorded", "already_recorded"}),
        (("interesting", None), ("rethink", None), {"recorded", "conflict"}),
        (("interesting", "a"), ("interesting", "b"), {"recorded", "conflict"}),
    ],
)
def test_two_connections_serialize_same_and_conflicting_judgments(
    tmp_path,
    left,
    right,
    expected_kinds,
):
    path = tmp_path / "concurrent.sqlite3"
    _, version_id = _produce_first_version(path)

    def record(value):
        return _service_for(path).record_judgment(version_id, *value).kind.value

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(record, value) for value in (left, right)]
        kinds = {future.result() for future in futures}

    assert kinds == expected_kinds
    with connect(path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM user_insight_judgments"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM accepted_insight_versions"
        ).fetchone()[0] in {0, 1}


def test_missing_and_unsucceeded_targets_are_distinct(tmp_path):
    path = tmp_path / "targets.sqlite3"
    add_formal_knowledge(path, "a")
    service = _service_for(path)
    assert service.record_judgment(999, "interesting").kind is JudgmentResultKind.NOT_FOUND

    with connect(path) as connection:
        event_id = int(
            connection.execute(
                """
                INSERT INTO organization_events (
                    status, started_at, topic_before_json,
                    topic_before_signature, topic_guard_signature,
                    boundary_signature
                ) VALUES ('running', ?, '{}', ?, ?, ?)
                """,
                (NOW, "a" * 64, "b" * 64, "c" * 64),
            ).lastrowid
        )
        insight_id = int(
            connection.execute(
                "INSERT INTO insight_identities (created_event_id, created_at) VALUES (?, ?)",
                (event_id, NOW),
            ).lastrowid
        )
        version_id = int(
            connection.execute(
                """
                INSERT INTO insight_versions (
                    insight_id, version_no, previous_version_id,
                    produced_event_id, payload_json, semantic_signature,
                    dependency_signature, created_at
                ) VALUES (?, 1, NULL, ?, ?, ?, ?, ?)
                """,
                (
                    insight_id,
                    event_id,
                    '{"codec":"insight-v1"}',
                    "d" * 64,
                    "e" * 64,
                    NOW,
                ),
            ).lastrowid
        )
    assert service.record_judgment(version_id, "interesting").kind is (
        JudgmentResultKind.NOT_ESTABLISHED
    )


def test_damaged_and_identity_mismatched_targets_are_distinct(tmp_path):
    damaged_path = tmp_path / "damaged.sqlite3"
    _, damaged = _produce_first_version(damaged_path)
    with connect(damaged_path) as connection:
        connection.execute("DROP TRIGGER insight_versions_cannot_be_updated")
        connection.execute(
            "UPDATE insight_versions SET semantic_signature = ? WHERE insight_version_id = ?",
            ("f" * 64, damaged),
        )
    assert _service_for(damaged_path).record_judgment(
        damaged, "interesting"
    ).kind is JudgmentResultKind.UNREADABLE

    mismatch_path = tmp_path / "identity-mismatch.sqlite3"
    insight_id, first = _produce_first_version(mismatch_path)
    second = _produce_successor(mismatch_path, insight_id, first, "v2")
    with connect(mismatch_path) as connection:
        connection.execute("DROP TRIGGER insight_versions_cannot_be_updated")
        connection.execute(
            "UPDATE insight_versions SET version_no = 3 WHERE insight_version_id = ?",
            (second,),
        )
    assert _service_for(mismatch_path).record_judgment(
        second, "interesting"
    ).kind is JudgmentResultKind.IDENTITY_MISMATCH
