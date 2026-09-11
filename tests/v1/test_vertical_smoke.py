import json
import subprocess
import time
from pathlib import Path

from knowledge_distiller.faithful_review import (
    FaithfulReview,
    FaithfulReviewCandidate,
)
from knowledge_distiller.primary import (
    FFmpegAudioNormalizer,
    PrimaryChunk,
    PrimaryRecognition,
    PrimaryRecovery,
)
from knowledge_distiller.v1.domain import (
    CapturedMaterial,
    Evidence,
    Knowledge,
    Point,
)
from knowledge_distiller.v1.pipeline import Distiller
from knowledge_distiller.v1.store import Store
from knowledge_distiller.v1.web import create_app
from knowledge_distiller.v1.worker import SingleWorker


class Source:
    def __init__(self, media: Path):
        self.media = media

    def capture(self, submitted_url: str, work_dir: Path) -> CapturedMaterial:
        return CapturedMaterial(
            "douyin",
            "real-media-1",
            submitted_url,
            "https://www.douyin.com/video/real-media-1",
            {"author": {"display_name": "纵向测试"}},
            self.media,
            1.0,
        )


class Recognizer:
    def recognize(self, audio):
        text = "持续切换会带来额外损耗。"
        return PrimaryRecognition.succeeded(
            PrimaryRecovery(
                text,
                "zh",
                (PrimaryChunk(text, 0.0, audio.duration_seconds, "zh"),),
            )
        )


class Reviewer:
    def review(self, recovery):
        return FaithfulReview.succeeded(
            FaithfulReviewCandidate(recovery.text, ())
        )


class Model:
    def derive(self, snapshot: str, uncertainties=()):
        return Knowledge(
            "注意力需要边界",
            "说明边界如何保护有限注意力。",
            "主动设定边界可以减少注意力损耗。",
            (
                Point(
                    "p1",
                    "边界保护注意力。",
                    "持续切换会带来额外损耗。",
                    ("e1",),
                ),
            ),
            (),
            (Evidence("e1", 0, 7, "持续切换会带来"),),
        )


class UnusedClipper:
    def clip(self, *args, **kwargs):
        raise AssertionError("normal source must not create confirmation audio")


def test_http_to_real_media_to_obsidian_vertical_smoke(tmp_path: Path) -> None:
    media = tmp_path / "source.mp4"
    _make_media(media)
    store = Store(tmp_path / "knowledge.sqlite3")
    vault = tmp_path / "vault"
    vault.mkdir()
    service = Distiller(
        store=store,
        source=Source(media),
        normalizer=FFmpegAudioNormalizer(),
        recognizer=Recognizer(),
        reviewer=Reviewer(),
        confirmation_clipper=UnusedClipper(),
        knowledge_model=Model(),
        runtime_root=tmp_path / "runtime",
        vault=vault,
    )
    worker = SingleWorker(store, service, idle_seconds=10)
    app = create_app(store, service, wake_worker=worker.wake)
    app.config.update(TESTING=True)
    worker.start()
    try:
        response = app.test_client().post(
            "/submissions",
            data={"content": "https://www.douyin.com/video/123/"},
        )
        deadline = time.monotonic() + 5
        while store.item_bundle(1)["state"] != "succeeded":
            assert time.monotonic() < deadline
            time.sleep(0.01)
        page = app.test_client().get(response.headers["Location"])
    finally:
        worker.stop()

    row = store.item_bundle(1)
    published = vault / row["published_path"]
    assert response.status_code == 302
    assert page.status_code == 200
    assert "注意力需要边界" in page.text
    assert row["state"] == "succeeded"
    assert row["source_fact_id"] is not None
    assert row["knowledge_result_id"] is not None
    assert published.is_file()
    assert "持续切换会带来" in published.read_text(encoding="utf-8")


def _make_media(path: Path) -> None:
    result = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=320x240:d=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1",
            "-shortest",
            "-c:v",
            "mpeg4",
            "-c:a",
            "aac",
            str(path),
        ],
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
