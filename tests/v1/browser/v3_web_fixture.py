"""Serve the V3 Web review surface from an explicit disposable data directory.

This fixture uses the production Flask templates and JavaScript with synthetic
store records.  Its workflow state is selectable, but it does not run Codex or
prove the background wiki pipeline.
"""
from __future__ import annotations

import argparse
import html
import json
from pathlib import Path

from flask import redirect, url_for
from werkzeug.serving import make_server

from knowledge_distiller.v1.insights import InsightLibrary
from knowledge_distiller.v1.web import create_app
from tests.test_organization_service import _productive_plan
from tests.v1.test_organization import organization
from tests.v1.test_topics import library


TASK_ID = "f" * 32
STATES = {
    "ready": {
        "state": "ready", "raw_count": 6, "actions": ["submit", "refresh"],
    },
    "queued": {
        "state": "queued", "raw_count": 6, "batch_count": 2, "actions": [],
    },
    "running": {
        "state": "running", "raw_count": 6, "batch_count": 2,
        "completed_batch_count": 1, "actions": [],
    },
    "succeeded": {
        "state": "succeeded", "raw_count": 6, "batch_count": 2,
        "completed_batch_count": 2, "candidate_count": 5,
        "actions": ["open_index", "open_pending"],
        "result_relpaths": ["wiki/index.md", "wiki/待确认.md"],
    },
    "failed": {
        "state": "failed", "raw_count": 6, "batch_count": 2,
        "completed_batch_count": 1, "error_code": "runner_failed",
        "recovery_state": "succeeded", "actions": ["retry"],
    },
    "settings": {
        "state": "config_required", "error_code": "kit_missing",
        "actions": ["settings"],
    },
    "conflict": {
        "state": "ready", "raw_count": 6, "error_code": "vault_busy",
        "actions": ["refresh"],
    },
}


def _snapshot(name: str) -> dict:
    return {
        "state": "unknown",
        "task_id": TASK_ID,
        "raw_count": None,
        "batch_count": None,
        "completed_batch_count": None,
        "candidate_count": None,
        "error_code": None,
        "recovery_state": "not_needed",
        "actions": [],
        "result_relpaths": [],
        **STATES[name],
    }


class SyntheticWorkflow:
    """In-memory state selector for UI review; never presented as real work."""

    def __init__(self) -> None:
        self.name = "ready"

    def snapshot(self) -> dict:
        return _snapshot(self.name)

    def submit_all(self) -> dict:
        self.name = "queued"
        return self.snapshot()

    def retry(self, task_id: str) -> dict:
        if task_id != TASK_ID:
            raise ValueError("task_not_found")
        self.name = "queued"
        return self.snapshot()

    def request_refresh(self, *, force: bool = False) -> dict:
        return self.snapshot()


class SyntheticDistiller:
    """Expose the production missing-clip contract without inventing audio."""

    runtime_root = None

    def confirmation_audio(self, item_id: int, concern_id: str):
        return None


def build_fixture(data_dir: Path):
    if data_dir.exists() and any(data_dir.iterdir()):
        raise ValueError("--data-dir must be a new or empty disposable directory")
    data_dir.mkdir(parents=True, exist_ok=True)
    data_dir.chmod(0o700)

    _topic_library, store, _worker = library(data_dir)
    plan = _productive_plan()
    plan["candidate_versions"][0]["payload"]["scan_tags"] = [
        "来源核对", "证据边界", "认知增量",
    ]
    service, _runtime = organization(store, growth=plan)
    result = service.drive(service.start_or_reuse().event_id)
    if result.event.status.value != "succeeded":
        raise RuntimeError("synthetic history setup failed")
    insight = InsightLibrary(store).list("pending")[0]
    InsightLibrary(store).judge(
        insight["id"], "interesting", "合成判断：保留来源边界再使用。",
    )

    waiting_id = store.create_item(
        "https://example.invalid/synthetic-review",
        title="合成疑点素材",
    )
    store.mark_waiting(waiting_id, {
        "snapshot": "合成疑点素材：持续切换会带来额外损耗。",
        "concerns": [{
            "start": 7,
            "end": 9,
            "text": "持续",
            "reason": "合成疑点：首词可能识别错误",
            "candidates": ["继续"],
        }],
    })
    vault = data_dir / "synthetic-vault"
    vault.mkdir()
    store.set_setting("vault_path", str(vault))

    workflow = SyntheticWorkflow()
    app = create_app(store, SyntheticDistiller(), wiki_workflow=workflow)
    app.config.update(TESTING=False)

    @app.get("/__fixture")
    def fixture_index():
        links = "".join(
            f'<li><a href="/__fixture/state/{html.escape(name)}">{html.escape(name)}</a></li>'
            for name in STATES
        )
        return (
            "<!doctype html><meta charset=utf-8><title>V3 合成审阅入口</title>"
            "<h1>V3 合成审阅入口</h1>"
            "<p>仅切换界面状态；不会运行 Codex 或后台知识整理。</p>"
            f"<ul>{links}</ul>"
            '<p><a href="/">首页</a> · <a href="/topics">历史主题</a> · '
            '<a href="/insights?state=interesting">历史新知与合成判断</a></p>'
        )

    @app.get("/__fixture/state/<name>")
    def fixture_state(name: str):
        if name not in STATES:
            return "unknown synthetic state", 404
        workflow.name = name
        return redirect(url_for("home"))

    return app, store, workflow


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    app, store, _workflow = build_fixture(args.data_dir.resolve())
    server = make_server("127.0.0.1", args.port, app, threaded=True)
    print(json.dumps({
        "url": f"http://127.0.0.1:{server.server_port}/__fixture",
        "data_dir": str(args.data_dir.resolve()),
        "database": str(store.path),
        "synthetic": True,
        "real_workflow": False,
    }, ensure_ascii=False), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
