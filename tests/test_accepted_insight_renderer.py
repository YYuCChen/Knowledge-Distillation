from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from dataclasses import replace

import pytest

import knowledge_distiller.accepted_insight_library as accepted_library
from knowledge_distiller.accepted_insight_library import (
    AcceptedRenderContextKind,
    load_accepted_render_context,
    read_accepted_render_context,
)
from knowledge_distiller.accepted_insight_renderer import (
    accepted_insight_machine_identity,
    accepted_insight_relative_path,
    accepted_render_context_from_dict,
    accepted_render_context_to_dict,
    decode_accepted_placement_receipt,
    decode_accepted_placement_receipt_from_content,
    render_accepted_insight,
)
from knowledge_distiller.database import connect
from tests.fixtures.growth import add_formal_knowledge, empty_growth_plan_payload
from tests.test_accepted_insight_library import _accepted_first, _accepted_nested
from tests.test_insight_judgment_service import (
    _produce_first_version,
    _produce_successor,
    _service_for,
)
from tests.test_organization_service import (
    _core_replacement_plan,
    _dual_accepted_disqualification_plan,
    _service,
)


PLACED_AT = "2026-08-22T08:00:00+00:00"


def _context(path, insight_version_id):
    result = read_accepted_render_context(path, insight_version_id)
    assert result.kind is AcceptedRenderContextKind.FOUND
    assert result.context is not None
    return result.context


def _record_publication(path, context, publication_id=1):
    rendered = render_accepted_insight(context, placed_at=PLACED_AT)
    with connect(path) as connection:
        connection.execute(
            """
            INSERT INTO accepted_insight_publications (
                publication_id, insight_version_id, judgment_id,
                relative_path, machine_identity, content_sha256,
                render_context_signature, placement_receipt_json,
                placed_at, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                publication_id,
                context.insight_version_id,
                context.root.judgment.judgment_id,
                rendered.relative_path,
                rendered.machine_identity,
                rendered.content_sha256,
                rendered.render_context_signature,
                rendered.placement_receipt_json,
                PLACED_AT,
                "2026-08-22T08:01:00+00:00",
            ),
        )
    return rendered


def _assert_receipt_snapshot_rerenders_exactly(rendered):
    receipt = decode_accepted_placement_receipt(rendered.placement_receipt_json)
    reconstructed = accepted_render_context_from_dict(receipt.render_context)
    assert render_accepted_insight(
        reconstructed,
        placed_at=receipt.placed_at,
    ).content == rendered.content
    return receipt


def _database_rows(path):
    with sqlite3.connect(path) as connection:
        tables = tuple(
            str(row[0])
            for row in connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
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


def test_current_context_and_renderer_are_exact_deterministic_and_read_only(tmp_path):
    path = tmp_path / "current.sqlite3"
    vault = tmp_path / "vault-must-not-exist"
    _, version_id = _accepted_first(path, annotation="My exact note")
    monitor = sqlite3.connect(path)
    before_version = int(monitor.execute("PRAGMA data_version").fetchone()[0])
    before_rows = _database_rows(path)

    context = _context(path, version_id)
    rendered = render_accepted_insight(context, placed_at=PLACED_AT)
    repeated = render_accepted_insight(context, placed_at=PLACED_AT)

    assert context.root.judgment.decision == "interesting"
    assert context.root.judgment.annotation_text == "My exact note"
    assert context.root.current_role == "current"
    assert len(context.root.participants) == 2
    assert len(context.root.used_relations) == 1
    assert len(context.lineage_nodes) == 1
    assert len(context.source_leaves) == 2
    assert all(leaf.material_id > 0 for leaf in context.source_leaves)
    assert all(leaf.evidences for leaf in context.source_leaves)
    assert all(
        evidence.source_fact_id == leaf.source_fact_id
        for leaf in context.source_leaves
        for evidence in leaf.evidences
    )
    assert rendered == repeated
    assert rendered.relative_path == accepted_insight_relative_path(
        context.insight_id,
        context.version_no,
    )
    assert rendered.machine_identity == accepted_insight_machine_identity(
        context.insight_id,
        context.insight_version_id,
    )
    assert rendered.content.endswith(
        (rendered.placement_receipt_comment + "\n").encode("utf-8")
    )
    receipt = _assert_receipt_snapshot_rerenders_exactly(rendered)
    assert receipt.insight_version_id == version_id
    assert receipt.render_context_signature == rendered.render_context_signature
    assert receipt.placed_at == PLACED_AT
    assert decode_accepted_placement_receipt_from_content(
        rendered.content
    ) == receipt
    with pytest.raises(ValueError, match="core hash"):
        decode_accepted_placement_receipt_from_content(
            rendered.content.replace(b"## ", b"### ", 1)
        )

    markdown = rendered.content.decode("utf-8")
    assert "# AI 衍生新知｜A and B reveal a narrower boundary" in markdown
    assert (
        "不是作者原话、来源事实、客观真理认证或用户本人观点" in markdown
    )
    assert context.root.payload.short_discussion in markdown
    assert context.root.payload.connection_reasons[0] in markdown
    assert context.root.payload.limitations[0].text in markdown
    assert "## 我的批注\n\n> [!note] 用户个人认知\n> My exact note" in markdown
    assert "## 实际参与知识" in markdown
    assert "### 使用过的连接判断" in markdown
    assert "不是参与知识、独立谱系或 evidence" in markdown
    assert "## 完整产生谱系" in markdown
    assert "#^source-1" in markdown
    assert markdown.index("## 新知主句") < markdown.index("## 精炼短论述")
    assert markdown.index("## 精炼短论述") < markdown.index("## 连接理由")
    assert markdown.index("## 连接理由") < markdown.index("## 我的批注")
    assert markdown.index("## 我的批注") < markdown.index("## 实际参与知识")

    assert _database_rows(path) == before_rows
    assert int(monitor.execute("PRAGMA data_version").fetchone()[0]) == before_version
    assert not vault.exists()
    monitor.close()


def test_annotation_absence_omits_the_entire_section(tmp_path):
    path = tmp_path / "no-annotation.sqlite3"
    _, version_id = _accepted_first(path)

    rendered = render_accepted_insight(_context(path, version_id), placed_at=PLACED_AT)

    assert "## 我的批注" not in rendered.core_markdown


def test_recursive_context_expands_every_ai_event_participant_limit_and_source_leaf(
    tmp_path,
):
    path = tmp_path / "recursive.sqlite3"
    parent_identity, parent = _accepted_first(path)
    parent_context = _context(path, parent)
    parent_rendered = _record_publication(path, parent_context)
    child = _accepted_nested(path, parent)
    parent_successor = _produce_successor(
        path,
        parent_identity,
        parent,
        "historical-parent",
    )
    assert _service_for(path).record_judgment(
        parent_successor,
        "interesting",
    ).kind == "recorded"

    context = _context(path, child)
    rendered = render_accepted_insight(context, placed_at=PLACED_AT)

    assert len(context.lineage_nodes) == 2
    assert context.lineage_nodes[1].insight_version_id == parent
    assert context.lineage_nodes[1].current_role == "historical"
    assert context.lineage_nodes[1].existing_publication is not None
    assert len(context.root.participants) == 2
    assert {
        participant.input_kind.value for participant in context.root.participants
    } == {"source_knowledge", "accepted_insight"}
    assert len(context.source_leaves) == 3
    markdown = rendered.core_markdown
    assert markdown.count("AI 层｜insight-") == 2
    assert context.lineage_nodes[1].primary_cause_accepted is not None
    assert (
        context.lineage_nodes[1].primary_cause_accepted.insight_version_id
        == parent_successor
    )
    assert "Evolved claim historical-parent" in markdown
    assert context.lineage_nodes[1].payload.claim in markdown
    assert context.lineage_nodes[1].payload.limitations[0].text in markdown
    assert "永久历史主因：newer_accepted_current" in markdown
    assert "判断时初始角色：current" in markdown
    assert "本次渲染角色：historical" in markdown
    assert (
        f"judgment-{context.lineage_nodes[1].caused_by_judgment_id}"
        in markdown
    )
    assert (
        f"[[{parent_rendered.relative_path}|打开独立资产："
        in markdown
    )
    for node in context.lineage_nodes:
        assert f"organization-event-{node.formation_event.event_id}" in markdown
        for participant in node.participants:
            assert participant.contribution_text in markdown
    for leaf in context.source_leaves:
        assert leaf.point_statement in markdown
        assert f"[[{leaf.published_path}#^source-1|" in markdown


def test_born_historical_context_keeps_primary_and_later_exit_facts(tmp_path):
    path = tmp_path / "born-historical.sqlite3"
    insight_id, v1 = _produce_first_version(path)
    v2 = _produce_successor(path, insight_id, v1, "v2")
    assert _service_for(path).record_judgment(v2, "interesting").kind == "recorded"
    late = _service_for(path).record_judgment(v1, "interesting")
    assert late.kind == "recorded"
    assert late.judgment.accepted_initial_role == "historical"

    _, source_id = add_formal_knowledge(path, "later-exit")
    plan = empty_growth_plan_payload()
    plan["new_input_reviews"] = [
        {
            "knowledge_result_id": source_id,
            "outcome": "considered_no_formal_result",
            "reason_text": "Establish a later formal event",
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
            (v1, event_id, "2026-08-22T09:00:00+00:00"),
        )

    context = _context(path, v1)
    rendered = render_accepted_insight(context, placed_at=PLACED_AT)

    assert context.root.initial_role == "historical"
    assert context.root.current_role == "historical"
    assert context.root.historical_reason == "born_older_than_current"
    assert context.root.primary_cause_accepted is not None
    assert context.root.primary_cause_accepted.insight_version_id == v2
    assert [item.fact_kind for item in context.root.additional_exit_facts] == [
        "basis_invalid"
    ]
    assert "永久历史主因：born_older_than_current" in rendered.core_markdown
    assert "判断时初始角色：historical" in rendered.core_markdown
    assert "本次渲染角色：historical" in rendered.core_markdown
    assert context.root.primary_cause_accepted.claim in rendered.core_markdown
    assert (
        f"judgment-{context.root.caused_by_judgment_id}"
        in rendered.core_markdown
    )
    assert "追加退出事实：basis_invalid" in rendered.core_markdown


def test_born_after_newer_ever_current_uses_exact_historical_cause(tmp_path):
    path = tmp_path / "born-after-newer-ever-current.sqlite3"
    insight_id, v1 = _produce_first_version(path)
    v2 = _produce_successor(path, insight_id, v1, "newer-ever-current")
    assert _service_for(path).record_judgment(v2, "interesting").kind == "recorded"

    _, source_id = add_formal_knowledge(path, "retire-newer-current")
    plan = empty_growth_plan_payload()
    plan["new_input_reviews"] = [
        {
            "knowledge_result_id": source_id,
            "outcome": "considered_no_formal_result",
            "reason_text": "Establish the formal disqualification event",
        }
    ]
    plan["accepted_disqualifications"] = [
        {
            "insight_version_id": v2,
            "fact_kind": "basis_invalid",
            "reason_text": "The newer accepted basis became invalid",
        }
    ]
    service, _, _ = _service(path, plan=plan, accepted_ids=(v2,))
    event_id = service.start_or_reuse().event_id
    assert service.drive(event_id).event.status == "succeeded"

    late = _service_for(path).record_judgment(v1, "interesting")
    assert late.kind == "recorded"
    assert late.judgment.accepted_historical_reason == (
        "born_after_newer_ever_current"
    )

    context = _context(path, v1)
    rendered = render_accepted_insight(context, placed_at=PLACED_AT)

    assert context.root.initial_role == "historical"
    assert context.root.current_role == "historical"
    assert context.root.historical_reason == "born_after_newer_ever_current"
    cause = context.root.primary_cause_accepted
    assert cause is not None
    assert cause.insight_id == context.insight_id
    assert cause.insight_version_id == v2
    assert cause.version_no > context.version_no
    assert cause.initial_role == "current"
    assert cause.current_role == "historical"
    assert "永久历史主因：born_after_newer_ever_current" in (
        rendered.core_markdown
    )
    assert cause.claim in rendered.core_markdown
    _assert_receipt_snapshot_rerenders_exactly(rendered)


def test_existing_direct_predecessor_publication_is_navigation_not_current_authority(
    tmp_path,
):
    path = tmp_path / "previous-publication.sqlite3"
    insight_id, v1 = _accepted_first(path)
    published_v1 = _record_publication(path, _context(path, v1))
    v2 = _produce_successor(path, insight_id, v1, "v2")
    assert _service_for(path).record_judgment(v2, "interesting").kind == "recorded"

    context = _context(path, v2)
    rendered = render_accepted_insight(context, placed_at=PLACED_AT)

    assert [item.insight_version_id for item in context.evolution_references] == [v1]
    assert context.evolution_references[0].relation_kind == "direct_predecessor"
    assert context.evolution_references[0].publication is not None
    assert f"[[{published_v1.relative_path}|" in rendered.core_markdown
    assert context.root.current_role == "current"
    assert context.lineage_nodes[0].existing_publication is None
    receipt_context = accepted_render_context_to_dict(context)
    receipt_context["evolution_references"] = []
    with pytest.raises(ValueError, match="direct predecessor"):
        accepted_render_context_from_dict(receipt_context)


def test_unpublished_accepted_predecessor_is_rendered_but_pending_is_excluded(
    tmp_path,
):
    path = tmp_path / "unpublished-accepted-predecessor.sqlite3"
    insight_id, v1 = _accepted_first(path)
    v1_claim = _context(path, v1).root.payload.claim
    v2 = _produce_successor(path, insight_id, v1, "accepted-v2")
    assert _service_for(path).record_judgment(v2, "interesting").kind == "recorded"

    accepted_context = _context(path, v2)
    accepted_rendered = render_accepted_insight(
        accepted_context,
        placed_at=PLACED_AT,
    )

    assert len(accepted_context.evolution_references) == 1
    reference = accepted_context.evolution_references[0]
    assert reference.relation_kind == "direct_predecessor"
    assert reference.insight_version_id == v1
    assert reference.claim == v1_claim
    assert reference.publication is None
    assert v1_claim in accepted_rendered.core_markdown
    assert "尚无独立已发布资产；不伪造链接或自动补发" in (
        accepted_rendered.core_markdown
    )

    pending = _produce_successor(path, insight_id, v2, "pending-v3")
    pending_claim = "Evolved claim pending-v3"
    v4 = _produce_successor(path, insight_id, pending, "accepted-v4")
    assert _service_for(path).record_judgment(v4, "interesting").kind == "recorded"
    pending_context = _context(path, v4)
    pending_rendered = render_accepted_insight(
        pending_context,
        placed_at=PLACED_AT,
    )

    assert all(
        item.insight_version_id != pending
        for item in pending_context.evolution_references
    )
    assert pending_context.root.previous_version_id is None
    assert "直接已认可前序版本：无" in pending_rendered.core_markdown
    assert pending_claim not in pending_rendered.core_markdown
    _assert_receipt_snapshot_rerenders_exactly(pending_rendered)


def test_current_to_historical_binds_exact_accepted_judgment_cause(tmp_path):
    path = tmp_path / "current-to-historical.sqlite3"
    insight_id, v1 = _accepted_first(path)
    v2 = _produce_successor(path, insight_id, v1, "new-current")
    recorded = _service_for(path).record_judgment(v2, "interesting")
    assert recorded.kind == "recorded"

    context = _context(path, v1)
    rendered = render_accepted_insight(context, placed_at=PLACED_AT)

    assert context.root.initial_role == "current"
    assert context.root.current_role == "historical"
    assert context.root.primary_cause_accepted is not None
    assert context.root.primary_cause_accepted.insight_version_id == v2
    assert (
        context.root.primary_cause_accepted.judgment_id
        == context.root.caused_by_judgment_id
    )
    assert "判断时初始角色：current" in rendered.core_markdown
    assert "本次渲染角色：historical" in rendered.core_markdown
    assert context.root.primary_cause_accepted.claim in rendered.core_markdown
    assert (
        f"judgment-{context.root.primary_cause_accepted.judgment_id}"
        in rendered.core_markdown
    )
    _assert_receipt_snapshot_rerenders_exactly(rendered)


def test_replacement_reference_and_primary_exit_show_exact_identity_without_link(
    tmp_path,
):
    path = tmp_path / "replacement-primary.sqlite3"
    old_insight_id, old_version = _accepted_first(path)
    old_context_before = _context(path, old_version)
    old_claim = old_context_before.root.payload.claim
    _, kr_c = add_formal_knowledge(path, "replacement-c")
    _, kr_d = add_formal_knowledge(path, "replacement-d")
    plan = _core_replacement_plan(old_insight_id, kr_c, kr_d)
    plan["relation_reviews"] = [
        {
            "relation_id": relation.relation_id,
            "relation_version_id": relation.relation_version_id,
            "action": "basis_invalid",
            "reason_text": "The accepted core is replaced in this event",
            "directly_affected": True,
        }
        for relation in old_context_before.root.used_relations
    ]
    service, _, _ = _service(
        path,
        plan=plan,
        accepted_ids=(old_version,),
        current_relation_ids=tuple(
            relation.relation_version_id
            for relation in old_context_before.root.used_relations
        ),
    )
    event_id = service.start_or_reuse().event_id
    assert service.drive(event_id).event.status == "succeeded"
    with connect(path) as connection:
        replacement = connection.execute(
            """
            SELECT insight_version_id, insight_id
            FROM insight_versions
            WHERE produced_event_id = ?
            """,
            (event_id,),
        ).fetchone()
    replacement_version = int(replacement["insight_version_id"])
    replacement_insight_id = int(replacement["insight_id"])
    assert _service_for(path).record_judgment(
        replacement_version,
        "interesting",
    ).kind == "recorded"

    old_context = _context(path, old_version)
    old_rendered = render_accepted_insight(old_context, placed_at=PLACED_AT)
    assert old_context.root.primary_exit_fact is not None
    assert old_context.root.primary_exit_fact.fact_kind == "identity_replaced"
    assert (
        old_context.root.primary_exit_fact.replacement_insight_id
        == replacement_insight_id
    )
    assert f"replacement identity：insight-{replacement_insight_id}" in (
        old_rendered.core_markdown
    )

    replacement_context = _context(path, replacement_version)
    replacement_rendered = render_accepted_insight(
        replacement_context,
        placed_at=PLACED_AT,
    )
    references = tuple(
        item
        for item in replacement_context.evolution_references
        if item.relation_kind == "replaced_identity"
    )
    assert len(references) == 1
    assert references[0].insight_version_id == old_version
    assert references[0].relation_event_id == event_id
    assert references[0].relation_reason_text == (
        "Core claim replaced by qualified candidate"
    )
    assert references[0].claim == old_claim
    assert references[0].publication is None
    assert old_claim in replacement_rendered.core_markdown
    assert f"organization-event-{event_id}" in replacement_rendered.core_markdown
    assert "Core claim replaced by qualified candidate" in (
        replacement_rendered.core_markdown
    )
    _assert_receipt_snapshot_rerenders_exactly(replacement_rendered)
    receipt_context = accepted_render_context_to_dict(replacement_context)
    replaced = next(
        item
        for item in receipt_context["evolution_references"]
        if item["relation_kind"] == "replaced_identity"
    )
    replaced["insight_id"] = replacement_context.insight_id
    with pytest.raises(ValueError, match="replace itself"):
        accepted_render_context_from_dict(receipt_context)


def test_later_replacement_remains_additional_to_earlier_primary_exit(tmp_path):
    path = tmp_path / "replacement-additional.sqlite3"
    old_insight_id, old_version = _accepted_first(path)
    _, review_source = add_formal_knowledge(path, "earlier-disqualification")
    review_plan = empty_growth_plan_payload()
    review_plan["new_input_reviews"] = [
        {
            "knowledge_result_id": review_source,
            "outcome": "considered_no_formal_result",
            "reason_text": "Establish the earlier exit event",
        }
    ]
    review_plan["accepted_disqualifications"] = [
        {
            "insight_version_id": old_version,
            "fact_kind": "basis_invalid",
            "reason_text": "Earlier basis loss",
        }
    ]
    review, _, _ = _service(
        path,
        plan=review_plan,
        accepted_ids=(old_version,),
    )
    review_event = review.start_or_reuse().event_id
    assert review.drive(review_event).event.status == "succeeded"

    _, kr_c = add_formal_knowledge(path, "later-replacement-c")
    _, kr_d = add_formal_knowledge(path, "later-replacement-d")
    replacement, _, _ = _service(
        path,
        plan=_core_replacement_plan(old_insight_id, kr_c, kr_d),
    )
    replacement_event = replacement.start_or_reuse().event_id
    assert replacement.drive(replacement_event).event.status == "succeeded"

    context = _context(path, old_version)
    rendered = render_accepted_insight(context, placed_at=PLACED_AT)

    assert context.root.primary_exit_fact is not None
    assert context.root.primary_exit_fact.fact_kind == "basis_invalid"
    assert context.root.primary_exit_fact.event_id == review_event
    assert [item.fact_kind for item in context.root.additional_exit_facts] == [
        "identity_replaced"
    ]
    additional = context.root.additional_exit_facts[0]
    assert additional.event_id == replacement_event
    assert additional.replacement_insight_id is not None
    assert "永久历史主因：basis_invalid" in rendered.core_markdown
    assert (
        f"追加退出事实：identity_replaced / event-{replacement_event}"
        in rendered.core_markdown
    )
    assert (
        f"replacement identity：insight-{additional.replacement_insight_id}"
        in rendered.core_markdown
    )


def test_event_primary_reverse_closure_matches_formal_exit_selection(tmp_path):
    path = tmp_path / "event-primary-reverse-closure.sqlite3"
    _, version_id = _accepted_first(path)
    _, source_id = add_formal_knowledge(path, "dual-exit-review")
    service, _, _ = _service(
        path,
        plan=_dual_accepted_disqualification_plan(source_id, version_id),
        accepted_ids=(version_id,),
    )
    event_id = service.start_or_reuse().event_id
    assert service.drive(event_id).event.status == "succeeded"

    context = _context(path, version_id)
    rendered = render_accepted_insight(context, placed_at=PLACED_AT)
    root = context.root
    primary = root.primary_exit_fact
    assert primary is not None
    assert primary.fact_kind == "refuted"
    assert primary.event_id == event_id
    assert [fact.fact_kind for fact in root.additional_exit_facts] == [
        "basis_invalid"
    ]
    basis = root.additional_exit_facts[0]
    assert basis.event_id == event_id
    _assert_receipt_snapshot_rerenders_exactly(rendered)

    def assert_invalid(bad_root, message):
        bad_context = replace(
            context,
            root=bad_root,
            lineage_nodes=(bad_root, *context.lineage_nodes[1:]),
        )
        with pytest.raises(ValueError, match=message):
            accepted_render_context_from_dict(
                accepted_render_context_to_dict(bad_context)
            )

    assert_invalid(
        replace(
            root,
            additional_exit_facts=(primary, *root.additional_exit_facts),
        ),
        "primary is duplicated",
    )
    assert_invalid(
        replace(
            root,
            additional_exit_facts=(
                replace(basis, event_id=primary.event_id - 1),
            ),
        ),
        "predates",
    )
    assert_invalid(
        replace(
            root,
            historical_reason="basis_invalid",
            primary_exit_fact=basis,
            additional_exit_facts=(primary,),
        ),
        "refutation must be primary",
    )
    assert_invalid(
        replace(
            root,
            additional_exit_facts=(
                replace(
                    basis,
                    fact_kind="identity_replaced",
                    replacement_insight_id=root.insight_id + 1,
                ),
            ),
        ),
        "same-event exit causes are ambiguous",
    )


def test_connection_level_loader_uses_callers_existing_transaction(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "connection-loader.sqlite3"
    _, version_id = _accepted_first(path)
    with connect(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        monkeypatch.setattr(
            accepted_library,
            "connect",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("connection-level loader opened another connection")
            ),
        )
        context = load_accepted_render_context(connection, version_id)
        assert context is not None
        rendered = render_accepted_insight(context, placed_at=PLACED_AT)
        assert rendered.render_context_signature
        connection.rollback()


def test_pending_rethink_and_non_interesting_contexts_cannot_render(tmp_path):
    pending_path = tmp_path / "pending.sqlite3"
    _, pending = _produce_first_version(pending_path)
    assert read_accepted_render_context(
        pending_path, pending
    ).kind is AcceptedRenderContextKind.NOT_FOUND

    rethink_path = tmp_path / "rethink.sqlite3"
    _, rethink = _produce_first_version(rethink_path)
    assert _service_for(rethink_path).record_judgment(
        rethink, "rethink"
    ).kind == "recorded"
    assert read_accepted_render_context(
        rethink_path, rethink
    ).kind is AcceptedRenderContextKind.NOT_FOUND

    accepted_path = tmp_path / "invalid-render-object.sqlite3"
    _, accepted = _accepted_first(accepted_path)
    context = _context(accepted_path, accepted)
    bad_root = replace(
        context.root,
        judgment=replace(context.root.judgment, decision="rethink"),
    )
    with pytest.raises(ValueError, match="Only interesting"):
        render_accepted_insight(
            replace(
                context,
                root=bad_root,
                lineage_nodes=(bad_root, *context.lineage_nodes[1:]),
            ),
            placed_at=PLACED_AT,
        )


@pytest.mark.parametrize(
    "damage",
    [
        "semantic",
        "broken_lineage",
        "cycle",
        "unsafe_handoff",
        "missing_source_asset_identity",
        "missing_evidence",
        "source_ownership",
    ],
)
def test_damaged_exact_context_fails_without_partial_render(tmp_path, damage):
    path = tmp_path / f"damaged-{damage}.sqlite3"
    _, parent = _accepted_first(path)
    target = parent
    if damage == "cycle":
        target = _accepted_nested(path, parent)

    with connect(path) as connection:
        if damage == "semantic":
            connection.execute("DROP TRIGGER insight_versions_cannot_be_updated")
            connection.execute(
                "UPDATE insight_versions SET semantic_signature = ? WHERE insight_version_id = ?",
                ("0" * 64, target),
            )
        elif damage == "broken_lineage":
            connection.execute(
                "DROP TRIGGER insight_version_participants_cannot_be_deleted"
            )
            connection.execute(
                """
                DELETE FROM insight_version_participants
                WHERE insight_version_id = ? AND position = 0
                """,
                (target,),
            )
        elif damage == "cycle":
            connection.execute(
                "DROP TRIGGER insight_version_participants_cannot_be_updated"
            )
            connection.execute(
                """
                UPDATE insight_version_participants
                SET input_kind = 'accepted_insight', knowledge_result_id = NULL,
                    point_id = NULL, accepted_insight_version_id = ?
                WHERE insight_version_id = ? AND position = 0
                """,
                (target, parent),
            )
        elif damage == "unsafe_handoff":
            connection.execute(
                "UPDATE knowledge_results SET published_path = '../escape.md'"
            )
        elif damage == "missing_source_asset_identity":
            connection.execute(
                """
                UPDATE materials SET platform_item_id = ''
                WHERE material_id = (SELECT MIN(material_id) FROM materials)
                """
            )
        elif damage == "missing_evidence":
            connection.execute(
                "DROP TRIGGER knowledge_result_content_cannot_be_updated"
            )
            row = connection.execute(
                "SELECT knowledge_result_id, payload_json FROM knowledge_results LIMIT 1"
            ).fetchone()
            payload = json.loads(str(row["payload_json"]))
            payload["evidence_registry"] = []
            connection.execute(
                "UPDATE knowledge_results SET payload_json = ? WHERE knowledge_result_id = ?",
                (json.dumps(payload), int(row["knowledge_result_id"])),
            )
        else:
            connection.execute(
                "DROP TRIGGER knowledge_result_content_cannot_be_updated"
            )
            source_ids = tuple(
                int(row[0])
                for row in connection.execute(
                    "SELECT source_fact_id FROM source_facts ORDER BY source_fact_id"
                )
            )
            knowledge = connection.execute(
                "SELECT knowledge_result_id, source_fact_id FROM knowledge_results LIMIT 1"
            ).fetchone()
            other_source = next(
                value for value in source_ids if value != int(knowledge["source_fact_id"])
            )
            connection.commit()
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute(
                "UPDATE knowledge_results SET source_fact_id = ? WHERE knowledge_result_id = ?",
                (other_source, int(knowledge["knowledge_result_id"])),
            )
            connection.commit()
            connection.execute("PRAGMA foreign_keys = ON")

    result = read_accepted_render_context(path, target)
    assert result.kind is AcceptedRenderContextKind.UNREADABLE
    assert result.context is None


def test_renderer_rejects_partial_source_context_before_producing_bytes(tmp_path):
    path = tmp_path / "partial-context.sqlite3"
    _, version_id = _accepted_first(path)
    context = _context(path, version_id)
    first_leaf = context.source_leaves[0]
    broken = replace(
        context,
        source_leaves=(replace(first_leaf, evidences=()), *context.source_leaves[1:]),
    )

    with pytest.raises(ValueError, match="no evidence"):
        render_accepted_insight(broken, placed_at=PLACED_AT)


def test_reverse_codec_rejects_shape_enum_identity_semantic_and_closure_damage(
    tmp_path,
):
    path = tmp_path / "strict-reverse-codec.sqlite3"
    _, version_id = _accepted_first(path)
    payload = accepted_render_context_to_dict(_context(path, version_id))

    damaged = []
    unknown = deepcopy(payload)
    unknown["unknown"] = True
    damaged.append(unknown)
    missing = deepcopy(payload)
    del missing["root"]["judgment"]
    damaged.append(missing)
    wrong_type = deepcopy(payload)
    wrong_type["version_no"] = True
    damaged.append(wrong_type)
    wrong_enum = deepcopy(payload)
    wrong_enum["root"]["initial_role"] = "pending"
    damaged.append(wrong_enum)
    wrong_identity = deepcopy(payload)
    wrong_identity["insight_id"] += 1
    damaged.append(wrong_identity)
    wrong_semantic = deepcopy(payload)
    wrong_semantic["root"]["semantic_signature"] = "0" * 64
    damaged.append(wrong_semantic)
    broken_closure = deepcopy(payload)
    broken_closure["source_leaves"].pop()
    damaged.append(broken_closure)
    broken_facts = deepcopy(payload)
    broken_facts["snapshot_fact_ids"].pop()
    damaged.append(broken_facts)

    for item in damaged:
        with pytest.raises(ValueError):
            accepted_render_context_from_dict(item)


def test_multiline_special_evidence_uses_a_single_line_safe_wiki_alias(tmp_path):
    path = tmp_path / "single-line-evidence-alias.sqlite3"
    _, version_id = _accepted_first(path)
    context = _context(path, version_id)
    leaf = context.source_leaves[0]
    evidence = leaf.evidences[0]
    prefix = "Line one |\nLine two ] "
    changed_text = prefix + evidence.evidence_text
    changed_snapshot = (
        leaf.content_snapshot[: evidence.start_offset]
        + changed_text
        + leaf.content_snapshot[evidence.end_offset :]
    )
    delta = len(prefix)
    payload = json.loads(leaf.knowledge_payload_json)
    for item in payload["evidence_registry"]:
        if item["id"] == evidence.evidence_id:
            item["end"] += delta
            item["evidence_text"] = changed_text
        elif item["start"] >= evidence.end_offset:
            item["start"] += delta
            item["end"] += delta
    changed_evidence = replace(
        evidence,
        end_offset=evidence.end_offset + delta,
        evidence_text=changed_text,
    )
    changed_leaf = replace(
        leaf,
        content_snapshot=changed_snapshot,
        knowledge_payload_json=json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        evidences=(changed_evidence, *leaf.evidences[1:]),
    )
    changed_context = replace(
        context,
        source_leaves=(changed_leaf, *context.source_leaves[1:]),
    )

    rendered = render_accepted_insight(changed_context, placed_at=PLACED_AT)
    evidence_link = next(
        line
        for line in rendered.core_markdown.splitlines()
        if "#^source-" in line and "Line one" in line
    )

    assert "Line one \\| Line two \\]" in evidence_link
    assert evidence_link.count("[[") == 1
    assert evidence_link.count("]]") == 1


def test_stable_target_and_machine_identity_do_not_depend_on_claim():
    assert accepted_insight_relative_path(12, 3) == (
        "知识蒸馏器/新知/AI新知--insight-12-v3.md"
    )
    assert accepted_insight_machine_identity(12, 99) == (
        "accepted-insight:12:version:99"
    )
