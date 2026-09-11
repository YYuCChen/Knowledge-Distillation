import json
import threading
from pathlib import Path

import pytest

from knowledge_distiller.v1.domain import (
    CapturedMaterial,
    Evidence,
    Knowledge,
    Point,
    SourceFact,
)
from knowledge_distiller.v1.pipeline import DistillResult
from knowledge_distiller.v1.store import Store
from knowledge_distiller.v1.web import create_app, douyin_url
from knowledge_distiller.v1.worker import SingleWorker


class RecordingDistiller:
    def __init__(self, store: Store):
        self.store = store
        self.runs: list[int] = []
        self.resolutions: list[tuple[int, str, str]] = []
        self.rerecognitions: list[int] = []
        self.confirmation_tokens: list[str] = []
        self.wakes = 0

    def wake(self) -> None:
        self.wakes += 1

    def run(self, item_id: int) -> DistillResult:
        self.runs.append(item_id)
        return DistillResult(item_id, self.store.item_bundle(item_id)["state"])

    def resolve(self, item_id: int, action: str, value: str, *, token: str = "", concern_id: str = "") -> DistillResult:
        self.confirmation_tokens.append(token)
        self.resolutions.append((item_id, action, value))
        if action in {"candidate", "manual"}:
            self.store.resume_item(item_id)
            return DistillResult(item_id, "queued")
        return DistillResult(item_id, self.store.item_bundle(item_id)["state"])

    def rerecognize(self, item_id: int, *, token: str = "") -> DistillResult:
        self.confirmation_tokens.append(token)
        self.rerecognitions.append(item_id)
        self.store.resume_item(item_id)
        return DistillResult(item_id, "queued")


@pytest.fixture
def web(tmp_path: Path):
    store = Store(tmp_path / "knowledge.sqlite3")
    distiller = RecordingDistiller(store)
    app = create_app(store, distiller, wake_worker=distiller.wake)
    app.config.update(TESTING=True)
    return app.test_client(), store, distiller


def test_home_starts_as_a_plain_submission_surface(web) -> None:
    client, _, _ = web

    response = client.get("/")

    assert response.status_code == 200
    assert "知识蒸馏器" in response.text
    assert "粘贴链接" in response.text
    assert "最近整理" not in response.text
    assert "Settings 正在接入" not in response.text


def test_legacy_unconfirmed_item_requires_explicit_rerecognition(web) -> None:
    client, store, distiller = web
    item = store.create_item('https://v.douyin.com/a/')
    store.mark_failed(item, 'reviewing', 'source_unconfirmed')
    assert '重新识别素材' in client.get(f'/?item={item}').text
    assert client.post(f'/items/{item}/retry').status_code == 409
    assert client.post(f'/items/{item}/retry', data={'action': 'rerecognize'}).status_code == 302
    assert distiller.wakes == 1


def test_submission_accepts_one_douyin_item_and_enqueues_it(web) -> None:
    client, store, distiller = web

    response = client.post(
        "/submissions",
        data={"content": "看看这个 https://www.douyin.com/video/123/。"},
    )

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/?item=1")
    assert distiller.runs == []
    assert distiller.wakes == 1
    assert store.item_bundle(1)["submitted_url"] == "https://www.douyin.com/video/123/"
    assert store.item_bundle(1)["state"] == "queued"


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("", "请输入"),
        ("https://example.com/video/1", "暂不支持这个来源链接"),
    ],
)
def test_invalid_submission_stays_visible_without_creating_work(
    web, value: str, message: str
) -> None:
    client, store, distiller = web

    response = client.post("/submissions", data={"content": value})

    assert response.status_code == 400
    assert message in response.text
    assert distiller.runs == []
    assert store.recent_items() == ()


def test_waiting_item_offers_only_its_current_confirmation(web) -> None:
    client, store, distiller = web
    item_id = store.create_item("https://v.douyin.com/a/")
    store.mark_waiting(
        item_id,
        {
            "snapshot": "持续切换会带来额外损耗。",
            "concerns": [
                {
                    "start": 0,
                    "end": 2,
                    "text": "持续",
                    "reason": "首词可能识别错误",
                    "candidates": ["继续"],
                }
            ],
        },
    )

    token = json.loads(store.item_bundle(item_id)["confirmation_json"])["token"]
    page = client.get("/")
    resolution = client.post(
        f"/items/{item_id}/confirm",
        data={"action": "candidate", "value": "继续", "token": token},
    )

    assert "<mark>持续</mark>" in page.text
    assert "重新识别" in page.text
    assert "查看来源" in page.text
    assert "external-link.svg" in page.text
    assert "选择更符合原意的一项" not in page.text
    assert "请回听填写" in page.text
    assert "自定义输入…" in page.text
    assert "无法确认" in page.text
    assert resolution.status_code == 302
    assert distiller.resolutions == [(item_id, "candidate", "继续")]
    assert distiller.confirmation_tokens == [token]
    assert page.text.count(f'name="token" value="{token}"') == 3
    assert 'name="action" value="finish"' not in page.text
    assert 'data-context-before=' not in page.text
    assert 'aria-label="待确认转写全文"' not in page.text
    assert 'data-transcript-text' not in page.text
    assert 'transcript-correction' not in page.text
    assert '完整原音' not in page.text
    assert distiller.wakes == 1
    assert store.item_bundle(item_id)["state"] == "queued"


def test_waiting_item_can_request_rerecognition(web) -> None:
    client, store, distiller = web
    item_id = store.create_item("https://v.douyin.com/a/")
    store.mark_waiting(item_id, {"snapshot": "疑点", "concerns": []})

    response = client.post(f"/items/{item_id}/rerecognize")

    assert response.status_code == 302
    assert distiller.rerecognitions == [item_id]
    assert distiller.wakes == 1
    assert store.item_bundle(item_id)["state"] == "queued"
    assert client.post(f"/items/{item_id}/rerecognize").status_code == 409


def test_completed_item_is_projected_as_visible_knowledge(web, tmp_path: Path) -> None:
    client, store, _ = web
    item_id = _complete_item(store, tmp_path)

    response = client.get(f"/?item={item_id}")

    assert response.status_code == 200
    assert "最近整理" in response.text
    assert "注意力需要边界" in response.text
    assert "说明边界如何保护有限注意力" in response.text
    assert "主动设定边界可以减少注意力损耗" in response.text
    assert "边界保护注意力" in response.text
    assert "恢复能力决定注意力韧性" in response.text
    assert "测试作者" in response.text
    assert "收录于" in response.text
    assert "跳转来源" in response.text
    assert "已保存至仓库" not in response.text
    assert 'class="original-link is-disabled"' not in response.text
    assert "publication-message" not in response.text
    assert "在 Finder 中显示笔记" not in response.text
    assert "尚未连接仓库" not in response.text
    assert "main-point.svg" in response.text
    assert "subpoint.svg" in response.text
    assert "attachment.svg" in response.text
    assert "external-link.svg" in response.text
    assert "obsidian://open" not in response.text
    assert "icons/upload.svg" not in response.text
    assert "0 批 · 0 等待" not in response.text
    assert "obsidian://open?vault=" not in response.text
    assert "知识蒸馏器/注意力--kr-1.md" not in response.text
    assert "knowledge_result_id" not in response.text
    assert "payload_json" not in response.text


def test_url_parser_keeps_only_the_douyin_work_url() -> None:
    assert (
        douyin_url("分享： https://www.douyin.com/video/123456)")
        == "https://www.douyin.com/video/123456"
    )


def test_only_failed_item_can_be_retried(web) -> None:
    client, store, distiller = web
    failed_id = store.create_item("https://v.douyin.com/a/")
    store.mark_failed(failed_id, "collecting", "douyin_not_configured")
    active_id = store.create_item("https://v.douyin.com/b/")

    page = client.get("/")

    retried = client.post(f"/items/{failed_id}/retry")
    conflict = client.post(f"/items/{active_id}/retry")
    absent = client.post("/items/999/retry")

    assert "1 项需处理" in page.text
    assert retried.status_code == 302
    assert distiller.runs == []
    assert distiller.wakes == 1
    assert store.item_bundle(failed_id)["state"] == "queued"
    assert conflict.status_code == 409
    assert absent.status_code == 404


class BlockingDistiller:
    def __init__(self, store: Store):
        self.store = store
        self.entered = {1: threading.Event(), 2: threading.Event()}
        self.release = {1: threading.Event(), 2: threading.Event()}

    def run(self, item_id: int) -> DistillResult:
        self.entered[item_id].set()
        if not self.release[item_id].wait(2):
            raise RuntimeError("test did not release blocked distill")
        self.store.mark_succeeded(item_id)
        return DistillResult(item_id, "succeeded")


def test_real_worker_exposes_working_and_fifo_waiting_through_normal_submit(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "knowledge.sqlite3")
    store.initialize()
    distiller = BlockingDistiller(store)
    worker = SingleWorker(store, distiller, idle_seconds=10)
    app = create_app(store, distiller, wake_worker=worker.wake)
    app.config.update(TESTING=True)
    worker.start()
    try:
        first = app.test_client().post(
            "/submissions", data={"content": "https://www.douyin.com/video/111/"}
        )
        assert first.status_code == 302
        assert distiller.entered[1].wait(1)
        assert store.item_bundle(1)["state"] == "working"

        submitted: dict[str, object] = {}
        returned = threading.Event()

        def submit_second() -> None:
            submitted["response"] = app.test_client().post(
                "/submissions", data={"content": "https://www.douyin.com/video/222/"}
            )
            returned.set()

        request_thread = threading.Thread(target=submit_second)
        request_thread.start()
        assert returned.wait(0.5), "submit waited for distillation instead of returning"
        request_thread.join()
        assert submitted["response"].status_code == 302
        assert store.item_bundle(2)["state"] == "queued"

        page = app.test_client().get("/")
        assert "处理中" in page.text
        assert "等待中" in page.text
        assert page.text.count("data-live-status") == 2

        distiller.release[1].set()
        assert distiller.entered[2].wait(1)
        assert store.item_bundle(1)["state"] == "succeeded"
        assert store.item_bundle(2)["state"] == "working"
        assert "处理中" in app.test_client().get("/").text
    finally:
        distiller.release[1].set()
        distiller.release[2].set()
        worker.stop()


def _complete_item(store: Store, tmp_path: Path, *, metadata_extra=None) -> int:
    item_id = store.create_item("https://v.douyin.com/a/")
    media = tmp_path / "source.mp4"
    media.write_bytes(b"media")
    material_id = store.attach_material(
        item_id,
        CapturedMaterial(
            "douyin",
            "123",
            "https://v.douyin.com/a/",
            "https://www.douyin.com/video/123",
            {
                "author": {"display_name": "测试作者"},
                "original_description": "原始标题",
                **(metadata_extra or {}),
            },
            media,
            12.5,
        ),
    )
    snapshot = "持续切换会带来额外损耗，也会削弱恢复能力。"
    source_fact_id = store.establish_source_fact(material_id, SourceFact(snapshot))
    result_id = store.establish_knowledge(
        source_fact_id,
        Knowledge(
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
            (
                Point(
                    "p2",
                    "恢复能力决定注意力韧性。",
                    "持续切换也会削弱恢复能力。",
                    ("e2",),
                ),
            ),
            (
                Evidence("e1", 0, 7, "持续切换会带来"),
                Evidence("e2", 14, 20, "削弱恢复能力"),
            ),
        ),
    )
    relative_path = Path("知识蒸馏器/注意力--kr-1.md")
    vault = tmp_path / "测试 Vault"
    published = vault / relative_path
    published.parent.mkdir(parents=True)
    published.write_text("# 注意力需要边界\n", encoding="utf-8")
    store.set_setting("vault_path", str(vault))
    store.mark_published(result_id, relative_path.as_posix(), vault=vault)
    store.mark_succeeded(item_id)
    return item_id


def test_obsidian_handoff_requires_registered_exact_vault(tmp_path, monkeypatch):
    from knowledge_distiller.v1.web import _obsidian_url
    vault = tmp_path/'a'/'vault'; vault.mkdir(parents=True)
    (vault/'note.md').write_text('owned')
    other = tmp_path/'b'/'vault'; other.mkdir(parents=True)
    registry = tmp_path/'Library/Application Support/obsidian/obsidian.json'
    registry.parent.mkdir(parents=True)
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path))
    registry.write_text(json.dumps({'vaults': {'other': {'path': str(other)}}}))
    assert _obsidian_url(str(vault), 'note.md') is None
    registry.write_text(json.dumps({'vaults': {'other': {'path': str(other)}, 'exact-id': {'path': str(vault)}}}))
    assert _obsidian_url(str(vault), 'note.md') == 'obsidian://open?vault=exact-id&file=note.md'
    assert _obsidian_url(str(vault), '../outside.md') is None


def test_startup_with_saved_credentials_does_not_access_or_relabel_keychain(tmp_path):
    from knowledge_distiller.v1.settings import SettingsService
    store = Store(tmp_path / 'knowledge.sqlite3')
    store.initialize()
    store.set_settings({'llm_provider': 'codex', 'llm_model': 'gpt-5.6-luna',
        'llm_effort': 'max', 'llm_service_tier': 'fast',
        'llm_draft_secret_account': 'saved-unused-api',
        'asr_seed_api_account': 'saved-unused-asr', 'asr_model': 'Qwen/Qwen3-ASR-1.7B'})
    before = store.settings()
    def forbidden_keychain_access(account):
        pytest.fail('Opening the app must not access or relabel saved credentials')
    settings = SettingsService(store, keychain_factory=forbidden_keychain_access)
    client = create_app(store, RecordingDistiller(store), settings).test_client()
    for route in ('/', '/settings', '/topics', '/insights'):
        assert client.get(route).status_code == 200
    assert store.settings() == before


def test_english_confirmation_shows_complete_choices_with_chinese_explanations(web):
    client, store, _ = web
    item = store.create_item("https://v.douyin.com/a/")
    original = "It has a lot of it."
    alternative = "It has a lot of potential."
    store.mark_waiting(item, {"snapshot": original, "concerns": [{
        "start": 0, "end": len(original), "text": original,
        "reason": "末尾词不清楚", "candidates": [original, alternative],
        "candidate_explanations": {original: "保留说话者原有表达", alternative: "它有很大的潜力；结合前文提出的可能表达"},
    }]})
    page = client.get("/").text
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(page, "html.parser")
    buttons = soup.select(".candidate-actions button")
    assert [b["value"] for b in buttons] == [original, alternative]
    assert buttons[0].text == original
    assert buttons[1].text == alternative
    assert "它有很大的潜力" in soup.select(".candidate-explanation")[1].text
    assert "hidden" in soup.select_one(".confirmation-actions").attrs
    assert not soup.select_one(".manual-entry").has_attr("open")
    assert not soup.select("[data-suggest-candidates]")
    assert not soup.select("textarea[readonly], [data-transcript-text], .transcript-review")
    assert "candidate-guidance" not in page


def test_legacy_zero_concern_item_only_offers_continue_without_full_text_editor(web):
    client, store, _ = web
    item = store.create_item("https://v.douyin.com/a/")
    store.mark_waiting(item, {"snapshot": "无需校对的旧任务完整文字", "concerns": [], "review_required": True})
    page = client.get("/").text
    assert "继续生成知识" in page
    assert f'action="/items/{item}/continue"' in page
    assert "无需校对的旧任务完整文字" not in page
    assert "data-transcript" not in page
    assert "选中文字" not in page


def test_legacy_continue_uses_numbered_card_between_other_pending_items(web):
    from bs4 import BeautifulSoup
    client, store, _ = web
    for _ in range(2):
        item = store.create_item('https://v.douyin.com/a/')
        store.mark_waiting(item, {'snapshot': '旧任务', 'concerns': [], 'review_required': True})
    soup = BeautifulSoup(client.get('/').text, 'html.parser')
    cards = soup.select('.todo-card')
    assert len(cards) == 2
    assert [card.select_one('.todo-index').text for card in cards] == ['1', '2']
    for card in cards:
        assert card.select_one('.failure-source')
        assert card.select_one('.failure-reason')
        assert card.select_one('button[value=finish]').text == '继续生成'


@pytest.mark.parametrize('snapshot', ['内壁脏、接缝线泥等纹', '中文材料提到 AI 和 YouTube，仍然保持简洁'])
def test_chinese_confirmation_keeps_compact_choices_without_english_aids(web, snapshot):
    from bs4 import BeautifulSoup
    client, store, _ = web
    item = store.create_item('https://v.douyin.com/a/')
    store.mark_waiting(item, {'snapshot': snapshot, 'concerns': [{
        'start': 0, 'end': 3, 'text': snapshot[:3], 'reason': '请确认这个词',
        'candidates': [snapshot[:3], '内壁章'],
        'candidate_explanations': {snapshot[:3]: '中文解释；判断依据', '内壁章': '另一种解释'},
    }]})
    soup = BeautifulSoup(client.get('/').text, 'html.parser')
    assert len(soup.select('.confirmation-head .compact-candidates button')) == 2
    assert [b['value'] for b in soup.select('.compact-candidates button')] == [snapshot[:3], '内壁章']
    assert not soup.select('.candidate-copy, .candidate-basis, .candidate-explanation, .suggest-candidates')
    assert soup.select_one('.confirmation-actions audio')
    assert soup.select_one('.manual-confirmation')


def test_chinese_sentence_choices_show_only_different_words_and_submit_full_value(web):
    from bs4 import BeautifulSoup
    client, store, distiller = web
    original = '亚马逊和Tim是在削弱你的品牌力'
    alternative = '亚马逊和Temu是在削弱你的品牌力'
    item = store.create_item('https://v.douyin.com/a/')
    store.mark_waiting(item, {'snapshot': '前面的中文上下文。' + original + '。后面的中文上下文。', 'concerns': [{
        'start': 9, 'end': 9 + len(original), 'text': original, 'reason': '不应再显示的长解释', 'audio_name': 'concern-test.wav',
        'candidates': [original, alternative],
    }]})
    pending = json.loads(store.item_bundle(item)['confirmation_json'])
    concern = pending['concerns'][0]
    soup = BeautifulSoup(client.get('/').text, 'html.parser')
    assert soup.select_one('mark').text == 'Tim'
    assert [b.text for b in soup.select('.compact-candidates button')] == ['Tim', 'Temu']
    assert [b['value'] for b in soup.select('.compact-candidates button')] == [original, alternative]
    assert '不应再显示的长解释' not in soup.text
    assert '亚马逊和' in soup.select_one('.source-fragment').text
    client.post(f'/items/{item}/confirm', data={'action':'manual','value':'Temu','local_edit':'1',
        'token':pending['token'], 'concern_id':concern['audio_name']})
    assert distiller.resolutions[-1] == (item, 'manual', alternative)


def test_home_displays_static_photo_processing_scope(web, tmp_path):
    client, store, _ = web
    metadata = {'source_scope': '仅处理静态原图和配文；动态部分及其声音未处理。'}
    _complete_item(store, tmp_path, metadata_extra=metadata)
    assert '来源范围：' + metadata['source_scope'] in client.get('/').text


def test_image_evidence_failure_explains_unconfirmed_text_instead_of_retry_later(web):
    client, store, _ = web
    item = store.create_item('https://www.douyin.com/note/123')
    store.mark_failed(item, 'distilling', 'knowledge_evidence_invalid')
    html = client.get('/').text
    assert '生成的证据引用了尚未确认的图片文字或无效图片依据' in html
    assert '本次处理未完成，可以稍后重试。' not in html
