import time
import stat
from pathlib import Path

from knowledge_distiller.v1.app import AppPaths, create_application


class Chrome:
    def __init__(self):
        self.calls = 0

    def verify(self):
        self.calls += 1
        raise AssertionError("GET must not connect")

    def cookies(self):
        self.calls += 1
        raise AssertionError("unconfigured source must not read browser cookies")


def test_application_prepares_private_wiki_runtime_before_worker_start(tmp_path):
    paths = AppPaths(tmp_path / "app-data")
    assert not paths.runtime.exists()
    app = create_application(paths, chrome=Chrome(), start_workers=False)
    try:
        assert paths.runtime.is_dir()
        assert stat.S_IMODE(paths.runtime.stat().st_mode) == 0o700
        assert app.config["KNOWLEDGE_DISTILLER_WIKI_WORKER"].runtime_root == paths.runtime
    finally:
        assert app.config["KNOWLEDGE_DISTILLER_WORKERS"].stop()
        app.config["KNOWLEDGE_DISTILLER_CLOSE_BROWSERS"]()


def test_fresh_application_runs_synthetic_wiki_task_without_precreated_runtime(tmp_path):
    from .test_wiki_worker import FakeRunner, _install, _raw

    paths = AppPaths(tmp_path / "fresh-data")
    vault = tmp_path / "synthetic-vault"
    vault.mkdir()
    _install(vault)
    raw = _raw(vault, 1)
    before = raw.read_bytes()
    assert not paths.runtime.exists()

    app = create_application(paths, chrome=Chrome(), start_workers=False)
    wiki_worker = app.config["KNOWLEDGE_DISTILLER_WIKI_WORKER"]
    production_runner = wiki_worker.runner
    try:
        app.config["KNOWLEDGE_DISTILLER_STORE"].set_settings({
            "vault_path": str(vault),
            "llm_provider": "codex",
            "llm_model": "gpt-test",
            "llm_effort": "high",
            "llm_state": "configured",
        })
        wiki_worker.runner = FakeRunner()
        submitted = app.extensions["wiki_workflow"].submit_all()
        result = wiki_worker.run_one()
        finished = wiki_worker.store.get(submitted["task_id"])
        wiki_worker.request_observation(vault, force=True)
        assert wiki_worker._refresh_observation() is True
        completed = app.extensions["wiki_workflow"].snapshot()

        assert submitted["state"] == "queued"
        assert result is not None and result.error_code is None
        assert finished.state == "succeeded"
        assert completed["state"] == "succeeded"
        assert completed["completed_batch_count"] == completed["batch_count"] == 1
        assert paths.runtime.is_dir()
        assert raw.read_bytes() == before
    finally:
        wiki_worker.runner = production_runner
        assert app.config["KNOWLEDGE_DISTILLER_WORKERS"].stop()
        app.config["KNOWLEDGE_DISTILLER_CLOSE_BROWSERS"]()


def test_application_boots_clean_v1_home_and_settings(tmp_path: Path) -> None:
    chrome = Chrome()
    app = create_application(AppPaths(tmp_path / "app-data"), chrome=chrome)
    try:
        app.config.update(TESTING=True)
        client = app.test_client()

        home = client.get("/")
        settings = client.get("/settings")

        assert home.status_code == 200
        assert settings.status_code == 200
        assert "知识蒸馏器" in home.text
        assert "社媒连接" in settings.text
        assert (tmp_path / "app-data" / "knowledge.sqlite3").is_file()
        assert chrome.calls == 0
    finally:
        assert app.config["KNOWLEDGE_DISTILLER_WORKERS"].stop()


def test_unconfigured_submission_fails_at_exact_first_missing_boundary(
    tmp_path: Path,
) -> None:
    chrome = Chrome()
    app = create_application(AppPaths(tmp_path / "app-data"), chrome=chrome)
    app.config.update(TESTING=True)

    try:
        response = app.test_client().post(
            "/submissions",
            data={"content": "https://www.douyin.com/video/123/"},
        )
        store = app.config["KNOWLEDGE_DISTILLER_STORE"]
        deadline = time.monotonic() + 2
        while store.item_bundle(1)["state"] != "failed":
            assert time.monotonic() < deadline
            time.sleep(0.01)
        page = app.test_client().get(response.headers["Location"])

        row = store.item_bundle(1)
        assert response.status_code == 302
        assert "请先在设置中连接抖音" in page.text
        assert "前往设置" in page.text
        assert "重试" in page.text
        assert row["error_code"] == "douyin_not_configured"
        assert row["material_id"] is None
        assert chrome.calls == 0
    finally:
        assert app.config["KNOWLEDGE_DISTILLER_WORKERS"].stop()


def test_pending_update_blocks_writes_until_acceptance(tmp_path):
    import sqlite3
    paths=AppPaths(tmp_path/'data')
    app=create_application(paths,chrome=Chrome(),start_workers=False)
    client=app.test_client()
    try:
        def rows():
            with sqlite3.connect(paths.database) as db:
                return tuple(db.iterdump())
        before=rows()
        response=client.post('/submissions',data={'content':'更新验收期间不能写入的新素材'})
        assert response.status_code==503
        assert rows()==before
        assert client.get('/').status_code==200
        assert client.get('/settings/updates/status').json['phase']=='installing'
        app.extensions['updates'].phase='idle'
        response=client.post('/submissions',data={'content':'确认升级后允许写入的新素材'})
        assert response.status_code==302
        assert rows()!=before
    finally:
        app.config['KNOWLEDGE_DISTILLER_CLOSE_BROWSERS']()
