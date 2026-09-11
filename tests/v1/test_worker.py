import threading
from pathlib import Path

import pytest

from knowledge_distiller.v1.store import Store
from knowledge_distiller.v1.worker import SingleWorker


@pytest.mark.parametrize("fail_during_construction", [False, True])
def test_unexpected_item_failure_does_not_stop_fifo(
    tmp_path: Path, caplog, fail_during_construction: bool
) -> None:
    store = Store(tmp_path / "knowledge.sqlite3")
    store.initialize()
    first = store.create_item("https://v.douyin.com/first/")
    second = store.create_item("https://v.douyin.com/second/")
    processed = threading.Event()
    calls = []

    class Distiller:
        def run(self, item_id):
            calls.append(item_id)
            if item_id == first:
                raise OSError("private-provider-response-secret")
            # A normal typed failure is also a completed unit of worker work.
            store.mark_failed(item_id, "collecting", "douyin_source_unavailable")
            processed.set()

    def build():
        if fail_during_construction and store.item_bundle(first)["state"] == "working":
            raise ValueError("private-configuration-secret")
        return Distiller()

    worker = SingleWorker(store, build, idle_seconds=10)
    worker.start()
    try:
        assert processed.wait(2), "an unexpected failure stranded the following item"
        assert store.item_bundle(first)["state"] == "failed"
        assert store.item_bundle(first)["error_code"] == "processing_unexpected_failure"
        assert store.item_bundle(second)["error_code"] == "douyin_source_unavailable"
        assert calls == ([second] if fail_during_construction else [first, second])
        assert "private-" not in caplog.text
    finally:
        worker.stop()
    store.retry_item(first)
    assert store.item_bundle(first)["state"] == "queued"
