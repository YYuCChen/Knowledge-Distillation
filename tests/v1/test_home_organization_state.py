"""V3 Local Web wiki status, actions, and durable retry contracts."""
from __future__ import annotations

import pytest

from knowledge_distiller.v1.store import Store
from knowledge_distiller.v1.web import _wiki_status_view, create_app


TASK_ID = "a" * 32


class Workflow:
    def __init__(self, snapshot):
        self.current = dict(snapshot)
        self.submissions = 0
        self.retries = []
        self.refreshes = []

    def snapshot(self):
        return dict(self.current)

    def submit_all(self):
        self.submissions += 1
        self.current = {
            **self.current, "state": "queued", "task_id": TASK_ID,
            "actions": [], "error_code": None,
        }
        return self.snapshot()

    def retry(self, task_id):
        self.retries.append(task_id)
        self.current = {**self.current, "state": "queued", "actions": [], "error_code": None}
        return self.snapshot()

    def request_refresh(self, force=False):
        self.refreshes.append(force)
        return self.snapshot()


def snapshot(state, **values):
    return {
        "state": state,
        "task_id": values.pop("task_id", TASK_ID),
        "raw_count": values.pop("raw_count", 12),
        "batch_count": values.pop("batch_count", 3),
        "completed_batch_count": values.pop("completed_batch_count", 1),
        "candidate_count": values.pop("candidate_count", 5),
        "error_code": values.pop("error_code", None),
        "recovery_state": values.pop("recovery_state", "not_needed"),
        "actions": values.pop("actions", []),
        "result_relpaths": values.pop("result_relpaths", []),
        **values,
    }


def client_for(tmp_path, current, **kwargs):
    store = Store(tmp_path / "knowledge.sqlite3")
    workflow = Workflow(current)
    app = create_app(store, object(), wiki_workflow=workflow, **kwargs)
    app.config["TESTING"] = True
    return app.test_client(), store, workflow


@pytest.mark.parametrize(("current", "texts", "absent"), [
    (snapshot("ready", actions=["submit"]),
     ("12 份素材待整理", "会整理本次开始前已经收到的全部素材", "开始整理"), ()),
    (snapshot("queued"),
     ("知识整理已排队", "本次 12 份素材 · 等待开始", "已在等待"), ()),
    (snapshot("running"),
     ("正在整理 · 已完成 1 / 3 批", "本次 12 份素材", "正在整理"), ("%", "预计")),
    (snapshot("succeeded", completed_batch_count=3, actions=["open_index", "open_pending"],
              result_relpaths=["wiki/index.md", "wiki/待确认.md"]),
     ("知识整理完成 · 3 批", "已保存到知识库；还有 5 条待确认",
      "查看待确认", "打开知识库"), ()),
    (snapshot("failed", actions=["retry"]),
     ("本次整理停在第 2 批", "已完成 1 批并保留", "继续整理"), ()),
    (snapshot("failed", error_code="config_required", actions=["settings"]),
     ("还不能开始整理", "请先在设置中连接 Codex", "打开设置"), ("继续整理",)),
    (snapshot("ready", error_code="vault_busy"),
     ("知识库正在由另一个整理任务维护", "本次没有创建重复任务", "暂时不可开始"),
     ("开始整理",)),
])
def test_approved_wiki_states_render_from_persisted_snapshot(tmp_path, current, texts, absent):
    client, _store, workflow = client_for(tmp_path, current)
    page = client.get("/")
    assert page.status_code == 200
    for text in texts:
        assert text in page.text
    for text in absent:
        assert text not in page.text
    assert workflow.refreshes == [False]


def test_unknown_counts_never_render_as_zero_or_none(tmp_path):
    current = snapshot(
        "running", raw_count=None, batch_count=None,
        completed_batch_count=None, candidate_count=None,
    )
    page = client_for(tmp_path, current)[0].get("/")
    section = page.text.split('data-sync-key="organization"', 1)[1].split("</section>", 1)[0]
    assert "正在整理" in section
    assert "None" not in section
    assert "0 / 0" not in section
    assert "%" not in section and "预计" not in section


def test_submit_is_idempotent_at_workflow_boundary(tmp_path):
    current = snapshot("ready", actions=["submit"])
    client, _store, workflow = client_for(tmp_path, current)
    assert client.post("/organization").status_code == 200
    assert client.post("/organization").status_code == 200
    assert workflow.submissions == 2
    assert workflow.snapshot()["task_id"] == TASK_ID
    page = client.get("/")
    assert "知识整理已排队" in page.text


def test_old_organization_engine_cannot_be_injected(tmp_path):
    store = Store(tmp_path / "knowledge.sqlite3")
    with pytest.raises(TypeError):
        create_app(store, object(), organization=object())


def test_wiki_forms_have_stable_ids_for_same_state_server_reconciliation(tmp_path):
    ready = client_for(tmp_path, snapshot("ready", actions=["submit"]))[0].get("/")
    assert 'id="wiki-submit"' in ready.text
    failed = client_for(tmp_path, snapshot("failed", actions=["retry"]))[0].get("/")
    assert 'id="wiki-retry"' in failed.text


def test_failed_task_retry_uses_persisted_identity(tmp_path):
    client, _store, workflow = client_for(
        tmp_path, snapshot("failed", actions=["retry"], recovery_state="required"))
    response = client.post(f"/organization/{TASK_ID}/retry")
    assert response.status_code == 200
    assert workflow.retries == [TASK_ID]
    assert "知识整理已排队" in client.get("/").text
    assert client.post("/organization/not-a-task/retry").status_code == 404


def test_post_renders_nonpersistent_conflict_and_same_failed_retry_result(tmp_path):
    class ConflictWorkflow(Workflow):
        def submit_all(self):
            self.submissions += 1
            return snapshot("ready", error_code="vault_busy", actions=["refresh"])
    store = Store(tmp_path / "conflict.sqlite3")
    conflict = ConflictWorkflow(snapshot("ready", actions=["submit"]))
    client = create_app(store, object(), wiki_workflow=conflict).test_client()
    response = client.post("/organization")
    assert response.status_code == 200
    assert "知识库正在由另一个整理任务维护" in response.text

    class StillFailedWorkflow(Workflow):
        def retry(self, task_id):
            self.retries.append(task_id)
            return self.snapshot()
    failed = StillFailedWorkflow(snapshot("failed", actions=["retry"]))
    failed_client = create_app(
        Store(tmp_path / "failed.sqlite3"), object(), wiki_workflow=failed).test_client()
    response = failed_client.post(f"/organization/{TASK_ID}/retry")
    assert response.status_code == 200
    assert 'id="wiki-retry"' in response.text and "继续整理" in response.text


def test_submit_exception_does_not_claim_that_no_task_was_created(tmp_path):
    class UncertainWorkflow(Workflow):
        def submit_all(self):
            raise RuntimeError("wake failed after durable submit")

    workflow = UncertainWorkflow(snapshot("ready", actions=["submit"]))
    client = create_app(
        Store(tmp_path / "uncertain.sqlite3"), object(), wiki_workflow=workflow,
    ).test_client()

    response = client.post("/organization")

    assert response.status_code == 503
    assert "暂时无法确认整理状态" in response.text
    assert "已有记录保持不变" in response.text
    assert "没有创建任务" not in response.text


def test_missing_workflow_is_explicitly_unavailable(tmp_path):
    store = Store(tmp_path / "knowledge.sqlite3")
    client = create_app(store, object()).test_client()
    response = client.post("/organization")
    assert response.status_code == 503
    assert "整理服务尚未启动" in response.text


def test_result_links_accept_only_fixed_relpaths_and_verified_urls(monkeypatch):
    calls = []
    def url(_vault, relative):
        calls.append(relative)
        return f"obsidian://verified/{relative}"
    monkeypatch.setattr("knowledge_distiller.v1.web._obsidian_url", url)
    view = _wiki_status_view(snapshot(
        "succeeded", actions=["open_index", "open_pending"],
        result_relpaths=["wiki/index.md", "wiki/待确认.md", "../private.md"],
    ), "/synthetic/vault")
    assert view["index_url"] == "obsidian://verified/wiki/index.md"
    assert view["pending_url"] == "obsidian://verified/wiki/待确认.md"
    assert calls == ["wiki/index.md", "wiki/待确认.md"]


def test_unknown_snapshot_is_low_noise_and_does_not_offer_action(tmp_path):
    client, _store, _workflow = client_for(tmp_path, {"state": "unknown"})
    page = client.get("/")
    assert "整理状态暂不可用" in page.text
    section = page.text.split('data-sync-key="organization"', 1)[1].split("</section>", 1)[0]
    assert "<form" not in section and "<button" not in section


def test_history_notice_is_kept_off_the_home_page(tmp_path):
    page = client_for(tmp_path, snapshot("ready", actions=["submit"]))[0].get("/")
    assert "历史内容只读保留" not in page.text


def test_failed_without_retry_never_promises_that_it_can_continue(tmp_path):
    page = client_for(tmp_path, snapshot(
        "failed", actions=["refresh"], error_code="recovery_failed",
        recovery_state="failed"))[0].get("/")
    assert "本次整理需要恢复" in page.text
    assert "暂时不能继续" in page.text
    assert "可以从未完成批次继续" not in page.text


@pytest.mark.parametrize(("error_code", "detail"), [
    ("config_required", "连接 Codex"),
    ("kit_missing", "知识库工具尚未安装"),
    ("kit_incompatible", "知识库工具需要更新"),
])
def test_settings_state_distinguishes_codex_and_kit_repairs(tmp_path, error_code, detail):
    page = client_for(tmp_path, snapshot(
        "failed", actions=["settings"], error_code=error_code))[0].get("/")
    assert "还不能开始整理" in page.text and detail in page.text


def test_update_reservation_rejects_post_and_successful_lease_is_released(tmp_path):
    from knowledge_distiller.v1.worker_lifecycle import WorkAdmissionGate
    gate = WorkAdmissionGate()
    client, _store, workflow = client_for(
        tmp_path, snapshot("ready", actions=["submit"]), admission_gate=gate)
    assert gate.reserve()
    blocked = client.post("/organization")
    assert blocked.status_code == 503
    assert "应用正在准备更新" in blocked.text
    assert workflow.submissions == 0
    # The retired endpoint is a read-only refusal, not admitted work.
    assert client.post("/insights/1/judge").status_code == 410
    gate.release_reservation()
    assert client.post("/organization").status_code == 200
    assert gate.idle
