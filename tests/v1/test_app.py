import time
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
        app.config["KNOWLEDGE_DISTILLER_WORKER"].stop()


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
        app.config["KNOWLEDGE_DISTILLER_WORKER"].stop()


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
