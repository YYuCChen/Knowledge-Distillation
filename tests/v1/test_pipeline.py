import json
import sqlite3
import time
from pathlib import Path

import pytest

from knowledge_distiller.faithful_review import (
    FaithfulReview,
    FaithfulReviewCandidate,
    ReviewConcern,
    ReviewFailure,
)
from knowledge_distiller.primary import (
    AudioNormalization,
    PrimaryRecognition,
    PrimaryRecovery,
    StandardAudio,
)
from knowledge_distiller.v1.domain import (
    CapturedMaterial,
    Evidence,
    Knowledge,
    Point,
    SourceFact,
)
from knowledge_distiller.v1.pipeline import Distiller
from knowledge_distiller.v1.database import connect
from knowledge_distiller.v1.store import Store
from knowledge_distiller.v1.worker import SingleWorker
from knowledge_distiller.v1.web import create_app


class Source:
    def __init__(self, root: Path):
        self.root = root
        self.calls = 0
        self.reuse_calls = 0

    def capture(self, submitted_url: str, work_dir: Path) -> CapturedMaterial:
        self.calls += 1
        media = work_dir / "media" / "source.mp4"
        media.parent.mkdir(parents=True, exist_ok=True)
        media.write_bytes(b"media")
        return CapturedMaterial(
            "douyin",
            "123",
            submitted_url,
            "https://www.douyin.com/video/123",
            {"author": {"display_name": "测试作者"}},
            media,
            10.0,
        )

    def reuse_retained(
        self,
        *,
        source_key: str,
        submitted_url: str,
        canonical_url: str,
        metadata,
        work_dir: Path,
    ) -> CapturedMaterial | None:
        self.reuse_calls += 1
        media = work_dir / "media" / "source.mp4"
        if not media.is_file():
            return None
        return CapturedMaterial(
            "douyin",
            source_key,
            submitted_url,
            canonical_url,
            metadata,
            media,
            10.0,
        )


class Normalizer:
    def __init__(self, root: Path):
        self.audio = root / "standard.wav"
        self.audio.write_bytes(b"audio")

    def normalize(self, media, work_dir):
        return AudioNormalization.succeeded(StandardAudio(self.audio, 10.0))


class Recognizer:
    def recognize(self, audio):
        return PrimaryRecognition.succeeded(
            PrimaryRecovery("持续切换会带来额外损耗。", "zh", ())
        )


class Reviewer:
    def __init__(self, concerns=()):
        self.concerns = concerns

    def review(self, recovery):
        return FaithfulReview.succeeded(
            FaithfulReviewCandidate(recovery.text, self.concerns)
        )


class FailsOnceReviewer(Reviewer):
    def __init__(self):
        super().__init__()
        self.calls = 0

    def review(self, recovery):
        self.calls += 1
        if self.calls == 1:
            return FaithfulReview.failed(ReviewFailure.RUNTIME_FAILED)
        return super().review(recovery)


class Clipper:
    def __init__(self):
        self.calls = []

    def clip(self, audio, recovery, candidate_text, concern, output_path):
        self.calls.append((audio, recovery, candidate_text, concern, output_path))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"local confirmation audio")
        return output_path


class Model:
    def __init__(self):
        self.calls = 0

    def derive(self, snapshot, uncertainties=()):
        self.calls += 1
        evidence_text = snapshot[:7]
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
            (Evidence("e1", 0, len(evidence_text), evidence_text),),
        )


def distiller(tmp_path: Path, *, concerns=(), reviewer=None):
    store = Store(tmp_path / "knowledge.sqlite3")
    store.initialize()
    vault = tmp_path / "vault"
    vault.mkdir()
    source = Source(tmp_path)
    model = Model()
    service = Distiller(
        store=store,
        source=source,
        normalizer=Normalizer(tmp_path),
        recognizer=Recognizer(),
        reviewer=reviewer or Reviewer(concerns),
        confirmation_clipper=Clipper(),
        knowledge_model=model,
        runtime_root=tmp_path / "runtime",
        vault=vault,
    )
    return service, store, source, model, vault


def finish_transcript(service, store, item_id):
    """Explicit user approval after all individual concerns are resolved."""
    assert store.item_bundle(item_id)["state"] == "waiting_user"
    assert store.item_bundle(item_id)["source_fact_id"] is None
    result = service.finish_transcript(item_id, token=confirmation_token(store, item_id))
    assert result.state == "queued"
    return result


def test_pipeline_completes_one_visible_knowledge_result(tmp_path: Path) -> None:
    service, store, _, _, vault = distiller(tmp_path)
    item_id = store.create_item("https://v.douyin.com/a/")

    result = service.run(item_id)

    row = store.item_bundle(item_id)
    assert result.state == "succeeded"
    assert row["source_fact_id"] is not None
    assert row["knowledge_result_id"] is not None
    assert (vault / row["published_path"]).is_file()


def test_nonblocking_uncertainty_is_preserved_without_user_gate(tmp_path):
    concern = ReviewConcern(0, 2, '持续', '轻微读音不确定，不改变含义', False)
    service, store, _, model, _ = distiller(tmp_path, concerns=(concern,))
    item = store.create_item('https://v.douyin.com/a/')
    assert service.run(item).state == 'succeeded'
    row = store.item_bundle(item)
    assert row['confirmation_json'] is None and model.calls == 1
    uncertainty, = json.loads(row['uncertainties_json'])
    assert uncertainty['text'] == '持续'
    assert row['snapshot'][uncertainty['start']:uncertainty['end']] == '持续'


def test_meaning_changing_concern_waits_for_exact_human_choice(
    tmp_path: Path,
) -> None:
    concern = ReviewConcern(0, 2, "持续", "首词可能识别错误", True, ("继续",))
    service, store, _, _, vault = distiller(tmp_path, concerns=(concern,))
    item_id = store.create_item("https://v.douyin.com/a/")

    waiting = service.run(item_id)
    audio = service.confirmation_audio(item_id)
    resolved = service.resolve(item_id, "candidate", "继续", token=confirmation_token(store, item_id))
    assert resolved.state == "queued"
    queued = resolved
    worker = SingleWorker(store, service)
    assert worker.run_one() == item_id

    row = store.item_bundle(item_id)
    assert waiting.state == "waiting_user"
    assert audio is not None
    assert not audio.exists()
    assert queued.state == "queued"
    assert row["state"] == "succeeded"
    assert row["snapshot"].startswith("继续切换")
    assert (vault / row["published_path"]).is_file()


def test_unable_confirmation_records_unknown_without_guessing(tmp_path: Path) -> None:
    concern = ReviewConcern(0, 2, "持续", "无法确认", True)
    service, store, _, _, _ = distiller(tmp_path, concerns=(concern,))
    item_id = store.create_item("https://v.douyin.com/a/")
    service.run(item_id)
    audio = service.confirmation_audio(item_id)

    resolved = service.resolve(item_id, "unable", token=confirmation_token(store, item_id))
    assert resolved.state == "queued"
    result = resolved

    row = store.item_bundle(item_id)
    assert result.state == "queued"
    assert audio is not None and audio.exists()
    assert row["source_fact_id"] is None
    pending = json.loads(row["confirmation_json"])
    assert pending["concerns"] == []
    assert pending["snapshot"] == "[听辨不清]切换会带来额外损耗。"
    unknown = pending["uncertainties"][0]
    assert unknown["status"] == "unresolved"
    assert unknown["original_text"] == "持续"
    assert pending["snapshot"][unknown["start"]:unknown["end"]] == "[听辨不清]"
    assert row["knowledge_result_id"] is None


def test_rerecognize_requeues_same_item_without_establishing_source_fact(
    tmp_path: Path,
) -> None:
    concern = ReviewConcern(0, 2, "持续", "首词可能识别错误", True, ("继续",))
    service, store, _, _, _ = distiller(tmp_path, concerns=(concern,))
    item_id = store.create_item("https://v.douyin.com/a/")
    service.run(item_id)
    audio = service.confirmation_audio(item_id)

    result = service.rerecognize(item_id, token=confirmation_token(store, item_id))

    row = store.item_bundle(item_id)
    assert result.item_id == item_id
    assert result.state == "queued"
    assert row["state"] == "queued"
    assert row["confirmation_json"] is None
    assert row["source_fact_id"] is None
    assert audio is not None
    assert not audio.exists()


def test_confirmation_audio_is_item_scoped_and_never_cached(tmp_path: Path) -> None:
    concern = ReviewConcern(0, 2, "持续", "首词可能识别错误", True, ("继续",))
    service, store, _, _, _ = distiller(tmp_path, concerns=(concern,))
    item_id = store.create_item("https://v.douyin.com/a/")
    service.run(item_id)
    client = create_app(store, service).test_client()

    response = client.get(f"/items/{item_id}/confirmation-audio")

    assert response.status_code == 200
    assert response.mimetype == "audio/wav"
    assert response.headers["Cache-Control"] == "no-store"
    assert client.get("/items/999/confirmation-audio").status_code == 404
    service.resolve(item_id, "candidate", "继续", token=confirmation_token(store, item_id))
    assert client.get(f"/items/{item_id}/confirmation-audio").status_code == 404


def test_duplicate_material_reuses_formal_result(tmp_path: Path) -> None:
    service, store, source, model, _ = distiller(tmp_path)
    first = store.create_item("https://v.douyin.com/a/")
    second = store.create_item("https://www.douyin.com/video/123")

    assert service.run(first).state == "succeeded"
    assert service.run(second).state == "succeeded"

    assert source.calls == 2
    assert model.calls == 1
    assert store.item_bundle(second)["knowledge_result_id"] == store.item_bundle(first)[
        "knowledge_result_id"
    ]

    # The task history remains complete; Home consumes each knowledge only once.
    from knowledge_distiller.v1.web import _home_context
    for selected in (None, first, second):
        context = _home_context(store, selected)
        assert len(context['recent']) == 1
        assert context['recent'][0]['id'] == first
        if selected is not None:
            assert context['selected']['id'] == first
    client = create_app(store, service).test_client()
    assert client.get(f'/?item={second}').text.count('class="knowledge-card"') == 1


def test_retry_after_review_failure_reuses_retained_media(tmp_path: Path) -> None:
    reviewer = FailsOnceReviewer()
    service, store, source, _, _ = distiller(tmp_path, reviewer=reviewer)
    item_id = store.create_item("https://v.douyin.com/a/")

    first = service.run(item_id)
    failed = store.item_bundle(item_id)
    store.retry_item(item_id)
    worker = SingleWorker(store, service)
    assert worker.run_one() == item_id
    assert store.item_bundle(item_id)["state"] == "succeeded"
    second = store.item_bundle(item_id)

    assert first.state == "failed"
    assert failed["material_id"] is not None
    assert failed["source_fact_id"] is None
    assert second["state"] == "succeeded"
    assert source.calls == 1
    assert source.reuse_calls == 1


def test_worker_restart_resumes_existing_formal_objects_without_duplicates(
    tmp_path: Path,
) -> None:
    service, store, source, model, vault = distiller(tmp_path)
    item_id = store.create_item("https://v.douyin.com/a/")
    assert store.claim_next_item() == item_id
    captured = source.capture("https://v.douyin.com/a/", tmp_path / "captured")
    material_id = store.attach_material(item_id, captured)
    snapshot = "持续切换会带来额外损耗。"
    fact_id = store.establish_source_fact(material_id, SourceFact(snapshot))
    result_id = store.establish_knowledge(fact_id, model.derive(snapshot))

    restarted = SingleWorker(store, service, idle_seconds=10)
    restarted.start()
    try:
        deadline = time.monotonic() + 2
        while store.item_bundle(item_id)["state"] != "succeeded":
            assert time.monotonic() < deadline
            time.sleep(0.01)
    finally:
        restarted.stop()

    row = store.item_bundle(item_id)
    assert row["source_fact_id"] == fact_id
    assert row["knowledge_result_id"] == result_id
    assert source.calls == 1
    assert model.calls == 1
    assert (vault / row["published_path"]).is_file()


def confirmation_token(store: Store, item_id: int) -> str:
    return json.loads(store.item_bundle(item_id)["confirmation_json"]).get("token", "")


@pytest.mark.parametrize("action", ["manual", "candidate", "unable", "rerecognize"])
def test_old_confirmation_page_cannot_act_on_next_concern(tmp_path: Path, action: str):
    concerns = (
        ReviewConcern(0, 2, "持续", "首词不明", True, ("继续",)),
        ReviewConcern(2, 4, "切换", "次词不明", True, ("继续",)),
    )
    service, store, _, _, _ = distiller(tmp_path, concerns=concerns)
    item_id = store.create_item("https://v.douyin.com/a/")
    service.run(item_id)
    client = create_app(store, service).test_client()
    token = confirmation_token(store, item_id)
    first = client.post(f"/items/{item_id}/confirm", data={
        "action": "manual", "value": "继续", "token": token,
    })
    assert first.status_code == 302
    pending = store.item_bundle(item_id)["confirmation_json"]
    route = "rerecognize" if action == "rerecognize" else "confirm"

    repeated = client.post(f"/items/{item_id}/{route}", data={
        "action": action, "value": "继续", "token": token,
    })

    assert repeated.status_code in {400, 409}
    row = store.item_bundle(item_id)
    assert row["confirmation_json"] == pending
    assert row["state"] == "waiting_user"
    assert row["source_fact_id"] is None


def test_confirmation_rechecks_pending_inside_transaction(tmp_path: Path, monkeypatch):
    concern = ReviewConcern(0, 2, "持续", "首词不明", True)
    service, store, _, _, _ = distiller(tmp_path, concerns=(concern,))
    item_id = store.create_item("https://v.douyin.com/a/")
    service.run(item_id)
    token = confirmation_token(store, item_id)
    commit = store.resolve_confirmation

    def concurrent_change(item_id, expected_json, **kwargs):
        # A competing request progresses after Distiller has read and validated.
        store.mark_waiting(item_id, json.loads(expected_json))
        return commit(item_id, expected_json, **kwargs)

    monkeypatch.setattr(store, "resolve_confirmation", concurrent_change)
    with pytest.raises(ValueError, match="来源确认已更新"):
        service.resolve(item_id, "manual", "继续", token=token)

    assert confirmation_token(store, item_id) != token
    assert store.item_bundle(item_id)["source_fact_id"] is None
    assert store.item_bundle(item_id)["state"] == "waiting_user"


def test_legacy_pending_requires_rerecognition_before_confirmation(tmp_path: Path):
    concern = ReviewConcern(0, 2, "持续", "首词不明", True)
    service, store, _, _, _ = distiller(tmp_path, concerns=(concern,))
    item_id = store.create_item("https://v.douyin.com/a/")
    service.run(item_id)
    pending = json.loads(store.item_bundle(item_id)["confirmation_json"])
    pending.pop("token")
    with connect(store.path) as connection:
        connection.execute("UPDATE distill_items SET confirmation_json = ? WHERE item_id = ?",
                           (json.dumps(pending), item_id))

    with pytest.raises(ValueError, match="旧版待确认请重新识别"):
        service.resolve(item_id, "manual", "继续")
    assert service.rerecognize(item_id).state == "queued"
    assert store.item_bundle(item_id)["source_fact_id"] is None


def test_pending_token_survives_reopen_and_missing_token_is_rejected(tmp_path: Path):
    concern = ReviewConcern(0, 2, "持续", "首词不明", True)
    service, store, _, _, _ = distiller(tmp_path, concerns=(concern,))
    item_id = store.create_item("https://v.douyin.com/a/")
    service.run(item_id)
    token = confirmation_token(store, item_id)
    assert token
    assert confirmation_token(Store(store.path), item_id) == token
    with pytest.raises(ValueError, match="来源确认已更新"):
        service.resolve(item_id, "manual", "继续")
    with pytest.raises(ValueError, match="来源确认已更新"):
        service.rerecognize(item_id)
    row = store.item_bundle(item_id)
    assert row["state"] == "waiting_user"
    assert row["source_fact_id"] is None


def test_rerecognition_cleanup_does_not_remove_the_next_attempt_audio(tmp_path: Path, monkeypatch):
    concern = ReviewConcern(0, 2, "持续", "首词不明", True)
    service, store, _, _, _ = distiller(tmp_path, concerns=(concern,))
    item_id = store.create_item("https://v.douyin.com/a/")
    service.run(item_id)
    old_audio = service.confirmation_audio(item_id)
    commit = store.resolve_confirmation

    def immediate_worker(item_id, expected_json, **kwargs):
        state = commit(item_id, expected_json, **kwargs)
        service.run(item_id)
        return state

    monkeypatch.setattr(store, "resolve_confirmation", immediate_worker)
    service.rerecognize(item_id, token=confirmation_token(store, item_id))

    current_audio = service.confirmation_audio(item_id)
    assert current_audio is not None
    assert current_audio.exists()
    assert current_audio != old_audio
    assert not old_audio.exists()


def test_confirmation_preserves_nonblocking_uncertainty_after_length_change(tmp_path: Path):
    concerns = (
        ReviewConcern(0, 2, "持续", "首词不明", True),
        ReviewConcern(2, 4, "切换", "局部读音略有不确定", False),
        ReviewConcern(7, 9, "额外", "范围词不明", True),
        ReviewConcern(9, 11, "损耗", "末词读音略有不确定", False),
    )
    service, store, _, _, _ = distiller(tmp_path, concerns=concerns)
    item_id = store.create_item("https://v.douyin.com/a/")
    service.run(item_id)
    client = create_app(store, service).test_client()

    response = client.post(f"/items/{item_id}/confirm", data={
        "action": "manual", "value": "不断地", "token": confirmation_token(store, item_id),
    })
    assert response.status_code == 302
    assert store.item_bundle(item_id)["state"] == "waiting_user"
    response = client.post(f"/items/{item_id}/confirm", data={
        "action": "manual", "value": "新", "token": confirmation_token(store, item_id),
    })

    assert response.status_code == 302
    assert store.item_bundle(item_id)["state"] == "queued"
    row = store.item_bundle(item_id)
    uncertainties = json.loads(row["uncertainties_json"])
    preserved = next((entry for entry in uncertainties if entry["text"] == "切换"), None)
    assert preserved is not None
    assert preserved["reason"] == "局部读音略有不确定"
    assert row["snapshot"][preserved["start"]:preserved["end"]] == "切换"
    following = next(entry for entry in uncertainties if entry["text"] == "损耗")
    assert row["snapshot"][following["start"]:following["end"]] == "损耗"
    assert row["snapshot"] == "不断地切换会带来新损耗。"
    assert any(entry.get("by") == "human" for entry in uncertainties)


def test_duplicate_waiting_item_reuses_fact_instead_of_reconfirming_it(tmp_path: Path):
    concern = ReviewConcern(0, 2, "持续", "首词不明", True)
    service, store, _, _, _ = distiller(tmp_path, concerns=(concern,))
    items = [store.create_item("https://v.douyin.com/a/") for _ in range(2)]
    for item_id in items:
        service.run(item_id)
    client = create_app(store, service).test_client()
    first, second = items
    client.post(f"/items/{first}/confirm", data={
        "action": "manual", "value": "继续", "token": confirmation_token(store, first),
    })

    assert store.item_bundle(first)["state"] == "queued"

    response = client.post(f"/items/{second}/confirm", data={
        "action": "manual", "value": "反复", "token": confirmation_token(store, second),
    })
    assert response.status_code == 400
    assert "当前纠正尚未保存" in response.text
    assert store.item_bundle(second)["state"] == "waiting_user"
    response = client.post(f"/items/{second}/confirm", data={
        "action": "manual", "value": "继续", "token": confirmation_token(store, second),
    })
    assert response.status_code == 302
    row = store.item_bundle(second)
    assert row["state"] == "queued"
    assert row["confirmation_json"] is None
    assert row["snapshot"].startswith("继续")
    assert row["source_fact_id"] == store.item_bundle(first)["source_fact_id"]


def test_confirmation_failure_rolls_back_fact_and_pending_together(tmp_path: Path):
    concern = ReviewConcern(0, 2, "持续", "首词不明", True)
    service, store, _, _, _ = distiller(tmp_path, concerns=(concern,))
    item_id = store.create_item("https://v.douyin.com/a/")
    service.run(item_id)
    pending = store.item_bundle(item_id)["confirmation_json"]
    with connect(store.path) as connection:
        connection.execute("""CREATE TRIGGER reject_confirmation_queue
            BEFORE UPDATE ON distill_items
            WHEN OLD.state = 'waiting_user' AND NEW.state = 'queued'
            BEGIN SELECT RAISE(ABORT, 'simulated queue failure'); END""")
    app = create_app(store, service)
    app.config["TESTING"] = True

    with pytest.raises(sqlite3.IntegrityError, match="simulated queue failure"):
        app.test_client().post(f"/items/{item_id}/confirm", data={
            "action": "manual", "value": "继续", "token": confirmation_token(store, item_id),
        })

    row = store.item_bundle(item_id)
    assert row["source_fact_id"] is None
    assert row["state"] == "waiting_user"
    assert row["confirmation_json"] == pending


def test_all_concerns_can_be_resolved_out_of_order(tmp_path: Path):
    concerns = (ReviewConcern(0, 2, '持续', '首词', True, ('继续',)),
                ReviewConcern(2, 4, '切换', '次词', True, ('转换',)))
    service, store, _, _, _ = distiller(tmp_path, concerns=concerns)
    item = store.create_item('https://v.douyin.com/a/')
    service.run(item)
    pending = json.loads(store.item_bundle(item)['confirmation_json'])
    first_id, second_id = [c['audio_name'] for c in pending['concerns']]
    client = create_app(store, service).test_client()
    page = client.get('/?item=1').text
    assert '2 处待确认' in page
    assert first_id in page and second_id in page
    service.resolve(item, 'manual', '重新转换', token=pending['token'], concern_id=second_id)
    pending = json.loads(store.item_bundle(item)['confirmation_json'])
    assert pending['concerns'][0]['audio_name'] == first_id
    assert pending['concerns'][0]['start'] == 0
    service.resolve(item, 'candidate', '继续', token=pending['token'], concern_id=first_id)
    assert store.item_bundle(item)["state"] == "queued"
    assert store.item_bundle(item)['snapshot'] == '继续重新转换会带来额外损耗。'


def test_legacy_unable_returns_to_same_confirmation_without_rerunning(tmp_path: Path):
    service, store, source, _, _ = distiller(tmp_path, concerns=(ReviewConcern(0, 2, '持续', '疑点', True),))
    item = store.create_item('https://v.douyin.com/a/')
    service.run(item)
    client = create_app(store, service).test_client()
    pending = store.item_bundle(item)['confirmation_json']
    store.resolve_confirmation(item, pending, unable=True)
    page = client.get('/?item=1').text
    assert '返回确认' in page and '前往设置' not in page
    assert client.post('/items/1/retry').status_code == 409
    assert client.post('/items/1/resume-confirmation').status_code == 302
    assert store.item_bundle(item)['confirmation_json'] == pending
    assert service.confirmation_audio(item).exists()
    assert source.calls == 1


def test_manual_error_is_local_to_its_card(tmp_path: Path):
    service, store, _, _, _ = distiller(tmp_path, concerns=(ReviewConcern(0, 2, '持续', '疑点', True),))
    item = store.create_item('https://v.douyin.com/a/')
    service.run(item)
    pending = json.loads(store.item_bundle(item)['confirmation_json'])
    response = create_app(store, service).test_client().post('/items/1/confirm', data={
        'action': 'manual', 'value': ' ', 'token': pending['token'], 'concern_id': pending['concerns'][0]['audio_name']})
    assert response.status_code == 400
    assert 'placeholder="请输入正确文字"' in response.text
    assert 'class="form-error"' not in response.text
    assert 'aria-invalid="true"' in response.text


def test_long_choices_compact_the_actual_replacement_span():
    from knowledge_distiller.v1.pipeline import _compact_concern
    c = _compact_concern({'start': 10, 'end': 21, 'text': '想满五百块钱送货到门口', 'candidates': ['想满五百块钱送货到门口', '满五百块钱送货到门口']})
    assert c['text'] == '想满'
    assert c['candidates'] == ['想满', '满']
    assert (c['start'], c['end']) == (10, 12)


def test_browser_confirmation_keeps_other_drafts_and_does_not_reload(tmp_path: Path):
    import threading
    from playwright.sync_api import sync_playwright, expect
    from werkzeug.serving import make_server

    concerns = (
        ReviewConcern(0, 2, '持续', '首词', True, ('不断继续',)),
        ReviewConcern(2, 4, '切换', '次词', True, ('转换',)),
        ReviewConcern(4, 5, '会', '第三词', True),
    )
    service, store, source, _, _ = distiller(tmp_path, concerns=concerns)
    item = store.create_item('https://v.douyin.com/a/')
    service.run(item)
    server = make_server('127.0.0.1', 0, create_app(store, service), threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as playwright:
            if not Path(playwright.chromium.executable_path).exists():
                pytest.skip('Browser regression requires playwright install chromium')
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={'width': 1440, 'height': 1024})
            page.goto(f'http://127.0.0.1:{server.server_port}/?item={item}')
            cards = page.locator('[data-confirmation-card]')
            expect(cards).to_have_count(3)
            assert page.locator('.outline-choice').evaluate_all(
                '(buttons) => buttons.every(b => b.scrollWidth <= b.clientWidth)'
            )
            third = cards.nth(2)
            third.locator('[data-card-toggle]').click()
            third.get_by_role('button', name='提交', exact=True).click()
            draft = third.locator('input[name="value"]')
            expect(draft).to_have_attribute('placeholder', '请输入正确文字')
            expect(page.locator('.intake .form-error')).to_have_count(0)
            draft.fill('另一疑点草稿')
            cards.nth(1).locator('[data-card-toggle]').click()
            cards.nth(1).locator('button[value="转换"]').click()
            expect(cards).to_have_count(2)
            expect(cards.nth(1).locator('input[name="value"]')).to_have_value('另一疑点草稿')
            expect(cards.nth(1).locator('[data-card-toggle]')).to_have_attribute('aria-expanded', 'true')
            cards.first.locator('[data-card-toggle]').click()
            cards.first.get_by_role('button', name='无法确认').click()
            expect(cards).to_have_count(1)
            expect(page.get_by_role('button', name='重试', exact=True)).to_have_count(0)
            expect(cards.first.locator('input[name="value"]')).to_have_value('另一疑点草稿')
            expect(cards.first.locator('[data-card-toggle]')).to_have_attribute('aria-expanded', 'true')
            assert source.calls == 1
            assert store.item_bundle(item)['source_fact_id'] is None
            pending = json.loads(store.item_bundle(item)['confirmation_json'])
            assert pending['snapshot'].startswith('[听辨不清]转换')
            assert pending['concerns'][0]['start'] == len('[听辨不清]转换')
            cards.first.locator('input[name="value"]').fill('')
            cards.first.locator('[data-card-toggle]').click()
            page.evaluate('window.intakeNode = document.querySelector(".intake")')
            # A queued sibling makes polling active, without running any model.
            queued = store.create_item('https://v.douyin.com/b/')
            # Fetch once to reveal the queue; subsequent updates use the real timer.
            page.evaluate('async () => applyPage(await (await fetch(location.href)).text())')
            store.mark_working(queued, 'reviewing')
            expect(page.get_by_role('heading', name='处理中', exact=True)).to_be_visible(timeout=6000)
            assert page.evaluate('window.intakeNode === document.querySelector(".intake")')
            assert page.locator('.outline-choice').count() == 0
            expect(cards.first.locator('.manual-hint')).to_have_text('请回听填写')
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=3)


@pytest.mark.parametrize("sufficient", [True, False])
def test_unknown_only_publishes_when_remaining_content_is_usable(tmp_path: Path, sufficient: bool):
    from dataclasses import replace
    from knowledge_distiller.v1.knowledge_model import KnowledgeModelError

    class RemainingModel(Model):
        def derive(self, snapshot, uncertainties=()):
            assert uncertainties[0]['status'] == 'unresolved'
            if not sufficient:
                raise KnowledgeModelError('knowledge_not_qualified')
            result = super().derive(snapshot, uncertainties)
            start = snapshot.index('额外损耗')
            return replace(result, core_points=(), other_points=result.core_points,
                           evidence=(Evidence('e1', start, start + 4, '额外损耗'),))

    service, store, source, _, _ = distiller(tmp_path, concerns=(ReviewConcern(0, 2, '持续', '首词', True),))
    service.knowledge_model = RemainingModel()
    item = store.create_item('https://v.douyin.com/a/')
    service.run(item)
    service.resolve(item, 'unable', token=confirmation_token(store, item))
    assert store.item_bundle(item)["state"] == "queued"
    result = service.run(item)
    row = store.item_bundle(item)
    assert source.calls == 1
    if sufficient:
        assert result.state == 'succeeded'
        payload = json.loads(row['payload_json'])
        assert payload['core_points'] == []
        assert len(payload['other_points']) == 1
        published = (service.vault / row['published_path']).read_text()
        assert '## 核心观点' not in published
        assert '[听辨不清]' in published
        page = create_app(store, service).test_client().get(f'/?item={item}').text
        assert 'knowledge-points core-points' not in page
        assert 'knowledge-points other-points' in page
    else:
        assert result.state == 'waiting_user'
        assert row['source_fact_id'] is None
        assert row['knowledge_result_id'] is None and row['published_path'] is None
        assert service.confirmation_audio(item).exists()
        pending = json.loads(row['confirmation_json'])
        assert '剩余明确内容不足' in pending['concerns'][0]['reason']
        service.resolve(item, 'manual', '持续', token=pending['token'])
        finish_transcript(service, store, item)
        service.knowledge_model = Model()
        assert service.run(item).state == 'succeeded'
        assert store.item_bundle(item)['snapshot'] == '持续切换会带来额外损耗。'
        assert source.calls == 1


def test_partial_source_retry_preserves_review_without_recapturing(tmp_path: Path):
    from knowledge_distiller.v1.knowledge_model import KnowledgeModelError

    class OfflineModel:
        calls = 0

        def derive(self, snapshot, uncertainties=()):
            self.calls += 1
            raise KnowledgeModelError('llm_request_failed')

    service, store, source, _, _ = distiller(tmp_path, concerns=(ReviewConcern(0, 2, '持续', '首词', True),))
    service.knowledge_model = OfflineModel()
    item = store.create_item('https://v.douyin.com/a/')
    service.run(item)
    service.resolve(item, 'unable', token=confirmation_token(store, item))
    assert store.item_bundle(item)["state"] == "queued"
    pending = store.item_bundle(item)['confirmation_json']
    assert service.run(item).state == 'failed'
    assert create_app(store, service).test_client().post(f'/items/{item}/retry').status_code == 302
    assert store.item_bundle(item)['confirmation_json'] == pending
    assert service.run(item).state == 'failed'
    assert source.calls == 1 and service.knowledge_model.calls == 2
    assert store.item_bundle(item)['source_fact_id'] is None


def test_skipped_concern_tracks_earlier_manual_length_change(tmp_path: Path):
    concerns = (ReviewConcern(0, 2, '持续', '首词', True), ReviewConcern(2, 4, '切换', '次词', True))
    service, store, _, _, _ = distiller(tmp_path, concerns=concerns)
    item = store.create_item('https://v.douyin.com/a/')
    service.run(item)
    pending = json.loads(store.item_bundle(item)['confirmation_json'])
    service.resolve(item, 'unable', token=pending['token'], concern_id=pending['concerns'][1]['audio_name'])
    service.resolve(item, 'manual', '不断地继续', token=confirmation_token(store, item))
    assert store.item_bundle(item)["state"] == "queued"
    row = store.item_bundle(item)
    pending = json.loads(row['confirmation_json'])
    assert row['state'] == 'queued' and row['source_fact_id'] is None
    assert pending['snapshot'] == '不断地继续[听辨不清]会带来额外损耗。'
    for entry in pending['uncertainties'] + pending['deferred_concerns']:
        assert (entry['start'], entry['end']) == (5, 11)
        assert pending['snapshot'][entry['start']:entry['end']] == '[听辨不清]'


def test_youtube_uses_same_worker_knowledge_and_source_publication(tmp_path):
    from dataclasses import replace
    from knowledge_distiller.v1.youtube import connection_authority
    service, store, _, model, vault = distiller(tmp_path)
    store.save_connection('youtube', None)
    authority = connection_authority(store.connection('youtube'))
    class YouTube(Source):
        def capture(self, url, work_dir, *, expected_authority):
            assert expected_authority == authority
            base = super().capture(url, work_dir)
            return replace(base, source_kind='youtube', source_key='aaaaaaaaaaa',
                canonical_url='https://www.youtube.com/watch?v=aaaaaaaaaaa',
                metadata={**base.metadata, 'session_authority': authority})
    service.youtube_source = YouTube(tmp_path)
    item = store.create_item('https://www.youtube.com/watch?v=aaaaaaaaaaa')
    assert SingleWorker(store, lambda:service).run_one() == item
    row = store.item_bundle(item)
    assert row['state'] == 'succeeded' and row['source_kind'] == 'youtube'
    assert row['source_fact_id'] and model.calls == 1
    assert 'youtube.com/watch?v=aaaaaaaaaaa' in (vault/row['published_path']).read_text()


def test_quality_rejection_reason_survives_restart_and_retry_preserves_source(tmp_path):
    from knowledge_distiller.v1.knowledge_model import parse_knowledge

    reason = '只有“大象鼻子很长、很酷”的简短描述，没有足够内容形成知识。<script>alert(1)</script>'

    class Rejected:
        def derive(self, snapshot, uncertainties=()):
            return parse_knowledge(snapshot, json.dumps({'qualified': False, 'rejection_reason': reason}))

    service, store, source, model, _ = distiller(tmp_path)
    service.knowledge_model = Rejected()
    item = store.create_item('https://v.douyin.com/a/')
    assert service.run(item).state == 'failed'
    original = store.item_bundle(item)
    assert original['source_fact_id'] is not None
    assert original['knowledge_result_id'] is None
    reopened = Store(store.path)
    reopened.initialize()
    assert reopened.item_bundle(item)['rejection_reason'] == reason
    page = create_app(reopened, service).test_client().get(f'/?item={item}').text
    assert '没有足够内容形成知识' in page
    assert '<script>alert(1)</script>' not in page
    assert '&lt;script&gt;' in page
    assert '重新提炼' in page
    reopened.retry_item(item)
    assert reopened.item_bundle(item)['rejection_reason'] is None
    service.knowledge_model = model
    assert service.run(item).state == 'succeeded'
    assert source.calls == 1
    assert reopened.item_bundle(item)['source_fact_id'] == original['source_fact_id']


def test_dismiss_failed_item_hides_it_durably_without_deleting_source(tmp_path):
    from knowledge_distiller.v1.knowledge_model import KnowledgeModelError

    class Rejected:
        def derive(self, snapshot, uncertainties=()):
            raise KnowledgeModelError('knowledge_not_qualified', rejection_reason='仅描述外观，缺少可提炼观点。')

    service, store, _, _, _ = distiller(tmp_path)
    service.knowledge_model = Rejected()
    item = store.create_item('https://v.douyin.com/a/')
    assert service.run(item).state == 'failed'
    original = dict(store.item_bundle(item))
    client = create_app(store, service).test_client()
    assert '放弃' in client.get('/').text
    assert client.post(f'/items/{item}/dismiss').status_code == 302
    assert client.post(f'/items/{item}/dismiss').status_code == 302
    restarted = Store(store.path)
    restarted.initialize()
    current = dict(restarted.item_bundle(item))
    assert current.pop('dismissed_at') is not None
    original.pop('dismissed_at')
    assert current == original
    assert restarted.recent_items() == ()
    assert '仅描述外观' not in client.get(f'/?item={item}').text
    assert client.post(f'/items/{item}/retry').status_code == 409
    queued = store.create_item('https://v.douyin.com/b/')
    assert client.post(f'/items/{queued}/dismiss').status_code == 409
    store.mark_working(queued, 'collecting')
    assert client.post(f'/items/{queued}/dismiss').status_code == 409
