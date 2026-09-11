from __future__ import annotations

import pytest

import knowledge_distiller.legacy.web as web_module
from knowledge_distiller.accepted_insight_library import AcceptedInsightLibraryError
from knowledge_distiller.database import connect
from knowledge_distiller.growth_modeling import (
    HistoricalRecallAdapter,
    RelationInsightAdapter,
)
from knowledge_distiller.insight_judgment_service import (
    InsightJudgmentService,
    JudgmentResult,
    JudgmentResultKind,
)
from knowledge_distiller.topic_indexing import TopicDraft, TopicIndexing, TopicPlan
from knowledge_distiller.topic_library import TopicLibrary, TopicRefreshKind
from knowledge_distiller.legacy.web import create_app
from tests.fixtures.growth import add_formal_knowledge, empty_growth_plan_payload
from tests.test_accepted_insight_library import _accepted_first
from tests.test_growth_web import Runtime, productive_plan, recall_payload
from tests.test_insight_judgment_service import (
    _produce_first_version,
    _produce_successor,
    _service_for,
)
from tests.test_organization_service import _service


class AllPointsTopicIndexer:
    def __init__(self):
        self.calls = 0

    def is_available(self):
        return True

    def organize(self, points, existing_topics):
        self.calls += 1
        if not points:
            return TopicIndexing.succeeded(TopicPlan((), ()))
        return TopicIndexing.succeeded(
            TopicPlan(
                (
                    TopicDraft(
                        None,
                        "web-accepted-topic",
                        "来源型主题",
                        "主链保持来源型观点，AI 新知只作辅助定位。",
                        tuple(point.reference for point in points),
                    ),
                ),
                (),
            )
        )


class StubJudgmentService:
    def __init__(self, result=None, *, error=None):
        self.result = result
        self.error = error
        self.calls = []

    def record_judgment(self, version_id, decision, annotation):
        self.calls.append((version_id, decision, annotation))
        if self.error is not None:
            raise self.error
        return self.result


def _web_app(path, *, vault_root=None, judgment_service=None, topic_indexer=None):
    recall_runtime = Runtime(recall_payload())
    growth_runtime = Runtime(empty_growth_plan_payload())
    app = create_app(
        path,
        obsidian_vault_root=vault_root,
        topic_indexer=topic_indexer or AllPointsTopicIndexer(),
        historical_recall_planner=HistoricalRecallAdapter(recall_runtime),
        relation_insight_planner=RelationInsightAdapter(growth_runtime),
        insight_judgment_service=judgment_service,
    )
    app.config.update(TESTING=True)
    return app, recall_runtime, growth_runtime


def test_create_app_composes_one_real_judgment_service(tmp_path):
    app, _, _ = _web_app(tmp_path / "composition.sqlite3")

    assert isinstance(
        app.config["INSIGHT_JUDGMENT_SERVICE"], InsightJudgmentService
    )


def test_candidate_page_is_exploration_with_real_payload_and_exact_form(tmp_path):
    path = tmp_path / "candidate-exploration.sqlite3"
    add_formal_knowledge(path, "a")
    add_formal_knowledge(path, "b")
    plan = productive_plan(claim='<script>alert("claim")</script>')
    payload = plan["candidate_versions"][0]["payload"]
    payload["short_discussion"] = "javascript:alert(1) is text, not a link."
    payload["connection_reasons"] = [
        '<img src=x onerror="reason">',
        "第二条连接理由",
    ]
    payload["limitations"] = [
        {"kind": "boundary", "text": '<svg onload="limit">'}
    ]
    recall_runtime = Runtime(recall_payload())
    growth_runtime = Runtime(plan)
    app = create_app(
        path,
        topic_indexer=AllPointsTopicIndexer(),
        historical_recall_planner=HistoricalRecallAdapter(recall_runtime),
        relation_insight_planner=RelationInsightAdapter(growth_runtime),
    )
    app.config.update(TESTING=True)
    client = app.test_client()
    assert client.post(
        "/knowledge/organization-events", data={"intent": "start"}
    ).status_code == 303
    with connect(path) as connection:
        version_id = int(
            connection.execute(
                "SELECT insight_version_id FROM insight_versions"
            ).fetchone()[0]
        )

    response = client.get("/knowledge/insight-candidates")

    assert response.status_code == 200
    assert "认知暗涌" in response.text
    assert "值得看看" in response.text
    assert "第二条连接理由" in response.text
    assert "javascript:alert(1) is text" in response.text
    assert "可选个人批注" in response.text
    assert f'/knowledge/insight-candidates/{version_id}/judgments' in response.text
    assert response.text.index("再想想") < response.text.index("有点意思")
    assert 'name="decision" value="rethink"' in response.text
    assert 'name="decision" value="interesting"' in response.text
    assert "<script>" not in response.text
    assert "<img src=x" not in response.text
    assert "<svg onload" not in response.text
    assert 'href="javascript:' not in response.text


@pytest.mark.parametrize(
    ("kind", "decision", "status", "location_suffix", "feedback"),
    [
        (
            JudgmentResultKind.RECORDED,
            "interesting",
            303,
            "/knowledge/insights/7",
            None,
        ),
        (
            JudgmentResultKind.ALREADY_RECORDED,
            "interesting",
            303,
            "/knowledge/insights/7",
            None,
        ),
        (
            JudgmentResultKind.RECORDED,
            "rethink",
            303,
            "/knowledge/insight-candidates?judgment_notice=rethink",
            None,
        ),
        (JudgmentResultKind.CONFLICT, "interesting", 409, None, "不同的判断或批注"),
        (JudgmentResultKind.NOT_FOUND, "interesting", 404, None, "不存在"),
        (JudgmentResultKind.NOT_ESTABLISHED, "interesting", 404, None, "尚未正式成立"),
        (JudgmentResultKind.IDENTITY_MISMATCH, "interesting", 500, None, "无法安全判断"),
        (JudgmentResultKind.UNREADABLE, "interesting", 500, None, "无法安全判断"),
    ],
)
def test_judgment_post_maps_domain_results_without_route_sql(
    tmp_path,
    kind,
    decision,
    status,
    location_suffix,
    feedback,
):
    service = StubJudgmentService(JudgmentResult(kind))
    app, _, _ = _web_app(
        tmp_path / f"mapping-{kind.value}-{decision}.sqlite3",
        judgment_service=service,
    )

    response = app.test_client().post(
        "/knowledge/insight-candidates/7/judgments",
        data={"decision": decision, "annotation": "  原样批注  "},
    )

    assert response.status_code == status
    assert service.calls == [(7, decision, "  原样批注  ")]
    if location_suffix is not None:
        assert response.headers["Location"].endswith(location_suffix)
    if feedback is not None:
        assert feedback in response.text


def test_judgment_post_rejects_bad_decision_and_hides_unexpected_error(tmp_path):
    service = StubJudgmentService(
        error=RuntimeError("private provider SQL detail")
    )
    app, _, _ = _web_app(
        tmp_path / "judgment-http-errors.sqlite3",
        judgment_service=service,
    )
    client = app.test_client()

    assert client.post(
        "/knowledge/insight-candidates/1/judgments", data={}
    ).status_code == 400
    assert client.post(
        "/knowledge/insight-candidates/1/judgments",
        data={"decision": "approve"},
    ).status_code == 400
    assert service.calls == []

    failed = client.post(
        "/knowledge/insight-candidates/1/judgments",
        data={"decision": "interesting"},
    )
    assert failed.status_code == 500
    assert "无法安全判断" in failed.text
    assert "private provider SQL detail" not in failed.text


def test_real_judgment_post_crash_is_not_success_and_next_request_is_clean(
    tmp_path,
):
    """§13.4B: the real POST preserves atomicity across crash and retry."""
    path = tmp_path / "judgment-post-crash.sqlite3"
    insight_id, v1 = _produce_first_version(path)
    v2 = _produce_successor(path, insight_id, v1, "v2")
    assert _service_for(path).record_judgment(v1, "interesting").kind is (
        JudgmentResultKind.RECORDED
    )
    crashed = False

    def inject(point, _connection):
        nonlocal crashed
        if point == "after_accepted_insert" and not crashed:
            crashed = True
            raise SystemExit("POST judgment crash")

    app, _, _ = _web_app(
        path,
        judgment_service=_service_for(path, injector=inject),
    )
    client = app.test_client()
    target = f"/knowledge/insight-candidates/{v2}/judgments"
    action = {"decision": "interesting", "annotation": "route note"}

    with pytest.raises(SystemExit, match="POST judgment crash"):
        client.post(target, data=action)

    with connect(path) as reopened:
        assert reopened.execute(
            """
            SELECT COUNT(*) FROM user_insight_judgments
            WHERE insight_version_id = ?
            """,
            (v2,),
        ).fetchone()[0] == 0
        assert [
            tuple(row)
            for row in reopened.execute(
                """
                SELECT insight_version_id, current_role
                FROM accepted_insight_versions ORDER BY insight_version_id
                """
            ).fetchall()
        ] == [(v1, "current")]
        assert reopened.execute("PRAGMA foreign_key_check").fetchall() == []

    recorded = client.post(target, data=action)
    already = client.post(target, data=action)
    conflict = client.post(
        target,
        data={"decision": "rethink", "annotation": "route note"},
    )

    assert recorded.status_code == already.status_code == 303
    assert recorded.headers["Location"].endswith(
        f"/knowledge/insights/{v2}"
    )
    assert conflict.status_code == 409
    with connect(path) as reopened:
        assert reopened.execute(
            "SELECT COUNT(*) FROM user_insight_judgments"
        ).fetchone()[0] == 2
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


def test_interesting_flows_to_complete_escaped_detail_and_list(tmp_path):
    path = tmp_path / "interesting-web.sqlite3"
    add_formal_knowledge(path, '<img src=x onerror="source">')
    add_formal_knowledge(path, "b")
    plan = productive_plan(claim='<script>alert("accepted")</script>')
    payload = plan["candidate_versions"][0]["payload"]
    payload["short_discussion"] = "javascript:alert(2) remains plain text."
    payload["connection_reasons"] = ['<b onmouseover="reason">连接</b>']
    payload["limitations"] = [
        {"kind": "boundary", "text": '<svg onload="limitation">'}
    ]
    plan["new_relations"][0]["payload"]["relation_statement"] = (
        '<iframe src="javascript:alert(3)">'
    )
    plan["candidate_versions"][0]["used_relations"][0]["role_text"] = (
        '<span onclick="role">历史连接角色</span>'
    )
    recall_runtime = Runtime(recall_payload())
    growth_runtime = Runtime(plan)
    app = create_app(
        path,
        topic_indexer=AllPointsTopicIndexer(),
        historical_recall_planner=HistoricalRecallAdapter(recall_runtime),
        relation_insight_planner=RelationInsightAdapter(growth_runtime),
    )
    app.config.update(TESTING=True)
    client = app.test_client()
    assert client.post(
        "/knowledge/organization-events", data={"intent": "start"}
    ).status_code == 303
    with connect(path) as connection:
        version_id = int(
            connection.execute(
                "SELECT insight_version_id FROM insight_versions"
            ).fetchone()[0]
        )

    judged = client.post(
        f"/knowledge/insight-candidates/{version_id}/judgments",
        data={
            "decision": "interesting",
            "annotation": '<img src=x onerror="annotation">',
        },
    )
    detail = client.get(judged.headers["Location"])
    candidates = client.get("/knowledge/insight-candidates")
    listed = client.get("/knowledge/insights")

    assert judged.status_code == 303
    assert judged.headers["Location"].endswith(f"/knowledge/insights/{version_id}")
    assert detail.status_code == 200
    assert "AI 衍生新知" in detail.text
    assert "当前版本" in detail.text
    assert "这不等于真理认证或证据等级提升" in detail.text
    assert "形成于整理事件" in detail.text
    assert "实际参与知识" in detail.text
    assert "历史连接角色" in detail.text
    assert "递归形成谱系" in detail.text
    assert "来源型知识与依据" in detail.text
    assert "知识 &lt;img src=x onerror=&#34;source&#34;&gt;" in detail.text
    assert "javascript:alert(2) remains plain text" in detail.text
    assert "<script>" not in detail.text
    assert "<iframe" not in detail.text
    assert "<span onclick" not in detail.text
    assert "<svg onload" not in detail.text
    assert "<img src=x onerror" not in detail.text
    assert 'href="javascript:' not in detail.text
    assert "/publish" not in detail.text
    assert "暂时没有待判断的新知候选" in candidates.text
    assert "alert(&#34;accepted&#34;)" in listed.text
    assert "当前新知" in listed.text
    assert 'action="/knowledge/insights"' in listed.text
    assert 'action="/knowledge"' not in listed.text
    assert 'action="/knowledge"' not in detail.text


def test_detail_shows_escaped_additional_facts_without_rewriting_primary(tmp_path):
    path = tmp_path / "additional-facts-web.sqlite3"
    _, version_id = _accepted_first(path)
    _, frozen_id = add_formal_knowledge(path, "additional-fact-review")
    plan = empty_growth_plan_payload()
    plan["new_input_reviews"] = [
        {
            "knowledge_result_id": frozen_id,
            "outcome": "considered_no_formal_result",
            "reason_text": "Completed review",
        }
    ]
    plan["accepted_disqualifications"] = [
        {
            "insight_version_id": version_id,
            "fact_kind": "refuted",
            "reason_text": "Primary refutation",
        },
        {
            "insight_version_id": version_id,
            "fact_kind": "basis_invalid",
            "reason_text": '<img src=x onerror="additional">',
        },
    ]
    service, _, _ = _service(
        path,
        plan=plan,
        accepted_ids=(version_id,),
    )
    event_id = service.start_or_reuse().event_id
    assert service.drive(event_id).event.status == "succeeded"
    app, _, _ = _web_app(path)
    witness = connect(path)
    try:
        before = int(witness.execute("PRAGMA data_version").fetchone()[0])
        detail = app.test_client().get(f"/knowledge/insights/{version_id}")
        after = int(witness.execute("PRAGMA data_version").fetchone()[0])
    finally:
        witness.close()
    listed = app.test_client().get("/knowledge/insights")

    assert detail.status_code == listed.status_code == 200
    assert "后续正式依据已经反驳这版新知" in detail.text
    assert "后来成立的事实" in detail.text
    assert "不改写上方的历史主因" in detail.text
    assert "维持这版新知所需的必要基础已经失效" in detail.text
    assert '&lt;img src=x onerror=&#34;additional&#34;&gt;' in detail.text
    assert '<img src=x onerror="additional">' not in detail.text
    assert "后来成立的事实" not in listed.text
    assert "Primary refutation" not in listed.text
    assert "additional" not in listed.text
    assert after == before


def test_rethink_fades_out_and_never_enters_accepted_routes(tmp_path):
    path = tmp_path / "rethink-web.sqlite3"
    _, version_id = _produce_first_version(path)
    app, _, _ = _web_app(path)
    client = app.test_client()

    response = client.post(
        f"/knowledge/insight-candidates/{version_id}/judgments",
        data={"decision": "rethink", "annotation": " \t\n "},
    )
    candidates = client.get(response.headers["Location"])

    assert response.status_code == 303
    assert "已从认知暗涌中安静收起" in candidates.text
    assert "暂时没有待判断的新知候选" in candidates.text
    assert client.get(f"/knowledge/insights/{version_id}").status_code == 404
    listed = client.get("/knowledge/insights")
    searched = client.get("/knowledge/insights?q=narrower")
    assert "A and B reveal a narrower boundary" not in listed.text
    assert "A and B reveal a narrower boundary" not in searched.text


def test_insight_list_search_partitions_and_local_degradation(tmp_path):
    path = tmp_path / "accepted-list-web.sqlite3"
    insight_id, v1 = _accepted_first(path)
    v2 = _produce_successor(path, insight_id, v1, "v2")
    _service_for(path).record_judgment(v2, "interesting")
    app, _, _ = _web_app(path)
    client = app.test_client()

    listed = client.get("/knowledge/insights")
    searched = client.get("/knowledge/insights?q=Evolved")
    historical_detail = client.get(f"/knowledge/insights/{v1}")

    assert listed.status_code == searched.status_code == 200
    assert 'class="accepted-section accepted-current"' in listed.text
    assert 'class="accepted-section accepted-historical"' in listed.text
    assert listed.text.index("Evolved claim v2") < listed.text.index(
        "A and B reveal a narrower boundary"
    )
    assert "Evolved claim v2" in searched.text
    assert "A and B reveal a narrower boundary" not in searched.text
    assert "更新认可版本曾成为当前表达，旧版因此永久进入历史" in listed.text
    assert "更新认可版本曾成为当前表达，旧版因此永久进入历史" in (
        historical_detail.text
    )
    assert "仍合资格" not in historical_detail.text

    with connect(path) as connection:
        connection.execute("DROP TRIGGER insight_versions_cannot_be_updated")
        connection.execute(
            "UPDATE insight_versions SET semantic_signature = ? WHERE insight_version_id = ?",
            ("f" * 64, v1),
        )
    degraded = client.get("/knowledge/insights")
    assert degraded.status_code == 200
    assert "有 1 条已认可新知当前无法读取" in degraded.text
    assert "Evolved claim v2" in degraded.text


def test_insight_list_and_search_whole_failures_are_not_empty(tmp_path, monkeypatch):
    app, _, _ = _web_app(tmp_path / "accepted-list-failure.sqlite3")
    client = app.test_client()

    def fail(*_args, **_kwargs):
        raise AcceptedInsightLibraryError("private accepted SQL")

    monkeypatch.setattr(web_module, "list_accepted_insights", fail)
    listed = client.get("/knowledge/insights")
    monkeypatch.setattr(web_module, "search_accepted_insights", fail)
    searched = client.get("/knowledge/insights?q=anything")

    assert listed.status_code == searched.status_code == 500
    assert "新知暂时无法读取" in listed.text
    assert "本次新知搜索没有完成" in searched.text
    assert "private accepted SQL" not in listed.text + searched.text


def test_accepted_detail_distinguishes_missing_and_unreadable(tmp_path):
    pending_path = tmp_path / "pending-detail-web.sqlite3"
    _, pending = _produce_first_version(pending_path)
    pending_client = _web_app(pending_path)[0].test_client()
    assert pending_client.get(f"/knowledge/insights/{pending}").status_code == 404

    damaged_path = tmp_path / "damaged-detail-web.sqlite3"
    _, damaged = _accepted_first(damaged_path)
    with connect(damaged_path) as connection:
        connection.execute("DROP TRIGGER insight_versions_cannot_be_updated")
        connection.execute(
            "UPDATE insight_versions SET semantic_signature = ? WHERE insight_version_id = ?",
            ("f" * 64, damaged),
        )
    damaged_response = _web_app(damaged_path)[0].test_client().get(
        f"/knowledge/insights/{damaged}"
    )
    assert damaged_response.status_code == 500
    assert "这条新知暂时无法安全读取" in damaged_response.text


def test_topic_auxiliary_is_separate_current_only_and_read_failure_is_local(
    tmp_path, monkeypatch
):
    path = tmp_path / "topic-auxiliary-web.sqlite3"
    insight_id, v1 = _accepted_first(path)
    indexer = AllPointsTopicIndexer()
    library = TopicLibrary(path, indexer)
    assert library.refresh(force=True).kind is TopicRefreshKind.REFRESHED
    topic_id = library.snapshot().topics[0].topic_id
    app, _, _ = _web_app(path, topic_indexer=indexer)
    app.config["TOPIC_LIBRARY"] = library
    client = app.test_client()

    with connect(path) as connection:
        memberships_before = int(
            connection.execute("SELECT COUNT(*) FROM topic_memberships").fetchone()[0]
        )
    response = client.get(f"/knowledge/topics/{topic_id}")

    assert response.status_code == 200
    assert response.text.index("一个可核查的正式观点") < response.text.index(
        "相关 AI 新知"
    )
    assert "A and B reveal a narrower boundary" in response.text
    assert f'/knowledge/insights/{v1}' in response.text
    with connect(path) as connection:
        assert int(
            connection.execute("SELECT COUNT(*) FROM topic_memberships").fetchone()[0]
        ) == memberships_before

    real_auxiliary = web_module.list_topic_auxiliary_insights

    def fail(*_args, **_kwargs):
        raise AcceptedInsightLibraryError("private auxiliary SQL")

    monkeypatch.setattr(web_module, "list_topic_auxiliary_insights", fail)
    failed = client.get(f"/knowledge/topics/{topic_id}")
    assert failed.status_code == 200
    assert "一个可核查的正式观点" in failed.text
    assert "相关 AI 新知暂时无法读取" in failed.text
    assert "private auxiliary SQL" not in failed.text
    monkeypatch.setattr(
        web_module, "list_topic_auxiliary_insights", real_auxiliary
    )

    v2 = _produce_successor(path, insight_id, v1, "v2")
    _service_for(path).record_judgment(v2, "interesting")
    assert library.refresh(force=True).kind is TopicRefreshKind.REFRESHED
    refreshed_topic_id = library.snapshot().topics[0].topic_id
    after = client.get(f"/knowledge/topics/{refreshed_topic_id}")
    assert after.status_code == 200
    assert "A and B reveal a narrower boundary" not in after.text
    assert "Evolved claim v2" in after.text


def test_all_g2_gets_are_database_model_judgment_and_vault_read_only(tmp_path):
    path = tmp_path / "all-g2-gets.sqlite3"
    _, version_id = _accepted_first(path)
    indexer = AllPointsTopicIndexer()
    library = TopicLibrary(path, indexer)
    assert library.refresh(force=True).kind is TopicRefreshKind.REFRESHED
    topic_id = library.snapshot().topics[0].topic_id
    vault_root = tmp_path / "vault"
    target_dir = vault_root / "知识蒸馏器"
    target_dir.mkdir(parents=True)
    for name in ("a.md", "b.md"):
        (target_dir / name).write_text(f"source {name}\n", encoding="utf-8")
    vault_before = {
        item.name: (item.read_bytes(), item.stat().st_mtime_ns)
        for item in target_dir.iterdir()
    }
    never_judge = StubJudgmentService(
        error=AssertionError("GET must not record a judgment")
    )
    app, recall_runtime, growth_runtime = _web_app(
        path,
        vault_root=vault_root,
        judgment_service=never_judge,
        topic_indexer=indexer,
    )
    app.config["TOPIC_LIBRARY"] = library
    client = app.test_client()
    witness = connect(path)
    try:
        before = int(witness.execute("PRAGMA data_version").fetchone()[0])
        responses = (
            client.get("/knowledge/insight-candidates"),
            client.get("/knowledge/insights"),
            client.get("/knowledge/insights?q=narrower"),
            client.get(f"/knowledge/insights/{version_id}"),
            client.get(f"/knowledge/topics/{topic_id}"),
        )
        after = int(witness.execute("PRAGMA data_version").fetchone()[0])
    finally:
        witness.close()

    assert all(response.status_code == 200 for response in responses)
    detail = responses[3]
    assert detail.text.count("obsidian://open?path=") == 2
    assert 'href="javascript:' not in detail.text
    assert before == after
    assert never_judge.calls == []
    assert recall_runtime.calls == growth_runtime.calls == []
    assert indexer.calls == 1
    assert {
        item.name: (item.read_bytes(), item.stat().st_mtime_ns)
        for item in target_dir.iterdir()
    } == vault_before
