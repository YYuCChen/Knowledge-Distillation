"""Feishu quick notes (handoff-feishu-capture.md §6 except 4.4; raw-interface §5.2).

Synthetic messages, audio and model answers only. Real Feishu send/receive is
reported separately and is not claimed by these tests.
"""
import json
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from knowledge_distiller.faithful_review import FaithfulReview, FaithfulReviewCandidate, ReviewConcern
from knowledge_distiller.primary import AudioNormalization, PrimaryRecognition, PrimaryRecovery, StandardAudio
from knowledge_distiller.v1.captures import Captures, CLOUD_SETTING
from knowledge_distiller.v1.database import connect
from knowledge_distiller.v1.feishu_inbox import FeishuInbox, history_message
from knowledge_distiller.v1.feishu_intake import FeishuIntake
from knowledge_distiller.v1.raw import RawLedger, parse_envelope
from knowledge_distiller.v1.store import Store

START = 1_790_000_000_000  # Fixed synthetic Feishu create_time (ms).


@pytest.fixture
def world(tmp_path):
    store = Store(tmp_path / 'isolated.sqlite3')
    store.initialize()
    vault = tmp_path / 'vault'
    vault.mkdir()
    store.set_setting('vault_path', str(vault))
    inbox = FeishuInbox(store, 'app-test')
    inbox.bind(bot_open_id='ou_bot', user_open_id='ou_owner', chat_id='oc_private', start_ms=0)
    audio = tmp_path / 'voice.wav'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=2',
                    '-ar', '16000', '-ac', '1', str(audio)], check=True)
    api = SimpleNamespace(downloads=[], download_message_file=lambda message_id, key: (
        api.downloads.append(message_id) or audio.read_bytes()))
    client = SimpleNamespace(model='judge-model', calls=[], answer={'identity': 'my_thought', 'confidence': 0.95})
    client.complete = lambda **kwargs: (client.calls.append(kwargs) or json.dumps(client.answer))
    intake = FeishuIntake(inbox, SimpleNamespace(submit=link_submit(store), collections=None),
                          api=api, client_factory=lambda: client)
    return SimpleNamespace(store=store, vault=vault, inbox=inbox, intake=intake, api=api, client=client,
                           captures=Captures(store, client_factory=lambda: client, api=api), tmp=tmp_path)


def link_submit(store):
    def submit(url, *, receipt_key, durable):
        return store.create_item(url, receipt_key=receipt_key)
    return submit


def send(world, mid, *, text=None, voice=False, at=0):
    content = json.dumps({'file_key': 'file_v3_' + mid, 'duration': 2000}) if voice else json.dumps({'text': text})
    raw = {'message_id': mid, 'chat_id': 'oc_private', 'create_time': str(START + at * 1000),
           'sender': {'id': 'ou_owner', 'id_type': 'open_id', 'sender_type': 'user'},
           'msg_type': 'audio' if voice else 'text', 'body': {'content': content}, 'mentions': []}
    world.inbox.receive(history_message(raw), history=True)
    return world.intake.process(mid)


def raw_files(world, folder):
    root = world.vault / 'raw' / folder
    return sorted(root.rglob('*.md')) if root.exists() else []


def envelope(path):
    front = path.read_text(encoding='utf-8')[4:].split('\n---\n', 1)[0]
    return yaml.safe_load(front)


def capture_of(world, mid):
    return world.captures.for_message('app-test', mid)


# ───────────────────────── Scenarios ─────────────────────────

def test_plain_text_thought_with_cloud_judgment_goes_to_self(world):
    world.store.set_setting(CLOUD_SETTING, 'on')
    assert send(world, 'om_t1', text='我觉得很多成功学其实也是事后解释，不对，应该说是先有结果再找理由。') == 'accepted'
    self_files = raw_files(world, '自述')
    assert len(self_files) == 1 and raw_files(world, '外部') == []
    env = envelope(self_files[0])
    assert (env['身份'], env['作者'], env['渠道']) == ('本人', '本人', '飞书文字')
    assert env['身份判定'] == {'结果': '本人', '依据': '模型·judge-model', '置信度': 0.95, '用户改判': '无'}
    # 原话不做整理稿: the exact words, including the self-correction.
    assert '我觉得很多成功学其实也是事后解释，不对，应该说是先有结果再找理由。\n\n^source-1' in self_files[0].read_text(encoding='utf-8')
    assert env['编号'] == capture_of(world, 'om_t1')['raw_id']
    # Zero friction: the chat gets one confirmation and no question.
    from knowledge_distiller.v1.feishu_cards import FeishuCards
    card = FeishuCards(world.inbox, None, None, None).card('om_t1')
    assert card['header']['title']['content'] == '已记录' and len(card['body']['elements']) == 1


def test_undecided_short_text_waits_for_the_desk_and_is_never_assumed_mine(world):
    world.store.set_setting(CLOUD_SETTING, 'on')
    world.client.answer = {'identity': 'my_thought', 'confidence': 0.7}  # Not sure enough.
    send(world, 'om_s1', text='明天再说')
    capture = capture_of(world, 'om_s1')
    assert world.captures.identity(capture['capture_id'])['result'] == 'pending'
    assert raw_files(world, '自述') == [] and raw_files(world, '外部') == []
    from knowledge_distiller.v1.web import create_app
    client = create_app(world.store, object()).test_client()
    home = client.get('/').get_data(as_text=True)
    assert '1 条随手记待定身份' in home and '明天再说' in home and '身份待定' in home
    assert client.post(f"/captures/{capture['capture_id']}/identity", data={'identity': 'my_thought'}).status_code == 302
    env = envelope(raw_files(world, '自述')[0])
    assert env['身份判定']['依据'] == '用户' and env['身份判定']['用户改判'] == '有'
    events = world.captures.events(capture['capture_id'])
    assert [(e['result'], e['basis']) for e in events] == [('pending', '规则未决'), ('my_thought', '用户')]
    assert '身份待定' not in client.get('/').get_data(as_text=True)


def test_cloud_switch_off_uses_rules_only(world):
    assert world.store.setting(CLOUD_SETTING) is None  # Off unless the user enables it.
    send(world, 'om_o1', text='这个想法值得记一下')
    capture = capture_of(world, 'om_o1')
    event = world.captures.identity(capture['capture_id'])
    assert event['result'] == 'pending' and event['basis'] == '规则未决·云端判断关闭'
    assert world.client.calls == []  # Nothing private was sent to a model.


def test_large_pasted_third_party_text_never_enters_the_personal_layer(world):
    pasted = ('转自某公众号\n\n' + '第三方长文段落。' * 20 + '\n\n') * 3
    assert send(world, 'om_p1', text=pasted) == 'accepted'
    capture = capture_of(world, 'om_p1')
    event = world.captures.identity(capture['capture_id'])
    assert event['result'] == 'third_party' and event['basis'].startswith('规则')
    item = world.store.item_bundle(capture['item_id'])
    assert item['input_kind'] == 'direct_text'
    # The existing direct-text material path: its raw goes to raw/外部 with the capture's id and time.
    from .test_pipeline import Model
    run_material(world, capture['item_id'])
    external = raw_files(world, '外部')
    assert raw_files(world, '自述') == [] and len(external) == 1
    env = envelope(external[0])
    assert env['编号'] == capture['raw_id'] and env['身份'] == '第三方' and env['渠道'] == '直接文本'


def run_material(world, item_id):
    from knowledge_distiller.v1.pipeline import Distiller
    from .test_pipeline import Model
    service = Distiller(store=world.store, source=None, normalizer=None, recognizer=None, reviewer=None,
                        confirmation_clipper=None, knowledge_model=Model(), runtime_root=world.tmp / 'runtime',
                        vault=world.vault)
    return service.run(item_id)


class Normalizer:
    def __init__(self, root):
        self.root = root

    def normalize(self, media, work_dir):
        audio = work_dir / 'standard.wav'
        work_dir.mkdir(parents=True, exist_ok=True)
        audio.write_bytes(Path(media.path).read_bytes())
        return AudioNormalization.succeeded(StandardAudio(audio, 2.0))


class Recognizer:
    def recognize(self, audio):
        return PrimaryRecognition.succeeded(PrimaryRecovery('呃我觉得十身这个说法不太对', 'zh', ()))


class Reviewer:
    def __init__(self, concerns):
        self.concerns, self.calls = concerns, 0

    def review(self, recovery):
        self.calls += 1
        return FaithfulReview.succeeded(FaithfulReviewCandidate(recovery.text, self.concerns))


class Clipper:
    def clip(self, audio, recovery, text, concern, output):
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b'clip')
        return output


def voice_service(world, concerns=()):
    from knowledge_distiller.v1.pipeline import Distiller
    reviewer = Reviewer(concerns)
    service = Distiller(store=world.store, source=None, normalizer=Normalizer(world.tmp), recognizer=Recognizer(),
                        reviewer=reviewer, confirmation_clipper=Clipper(), knowledge_model=None,
                        runtime_root=world.tmp / 'runtime', vault=world.vault)
    return service, reviewer


def test_voice_thought_with_a_concern_keeps_the_asr_original_and_releases_audio_after(world):
    world.store.set_setting(CLOUD_SETTING, 'on')
    concern = ReviewConcern(4, 6, '十身', '可能是"十神"', True, ('十神',))
    assert send(world, 'om_v1', voice=True, at=10) == 'accepted'
    capture = capture_of(world, 'om_v1')
    assert world.captures.identity(capture['capture_id'])['basis'] == '规则·语音默认本人'
    assert Path(capture['audio_path']).is_file() and world.api.downloads == ['om_v1']
    service, reviewer = voice_service(world, (concern,))
    item = capture['item_id']
    assert service.run(item).state == 'waiting_user'
    transcript = world.captures.transcript(capture['capture_id'])
    assert transcript['text'] == '呃我觉得十身这个说法不太对' and transcript['engine']
    assert raw_files(world, '自述') == []  # Concerns first; raw only for finished captures.
    assert Path(capture_of(world, 'om_v1')['audio_path']).is_file()  # Kept for listening back.
    from .test_pipeline import confirmation_token
    assert service.resolve(item, 'candidate', '十神', token=confirmation_token(world.store, item)).state == 'queued'
    assert service.run(item).state == 'succeeded'
    text = raw_files(world, '自述')[0].read_text(encoding='utf-8')
    env = envelope(raw_files(world, '自述')[0])
    assert env['渠道'] == '飞书语音' and env['身份'] == '本人'
    assert env['订正'] == [{'片段': 'source-1', '原识别': '十身', '订正为': '十神', '方式': '用户核对'}]
    assert '呃我觉得十神这个说法不太对\n\n^source-1' in text  # Fillers kept, correction applied.
    assert world.captures.transcript(capture['capture_id'])['text'] == '呃我觉得十身这个说法不太对'
    released = capture_of(world, 'om_v1')
    assert released['audio_released_at'] and not Path(released['audio_path']).exists()
    assert world.store.item_bundle(item)['knowledge_result_id'] is None  # Own words, not a V1 note.


def test_voice_with_cloud_off_skips_model_review(world):
    send(world, 'om_v2', voice=True)
    capture = capture_of(world, 'om_v2')
    service, reviewer = voice_service(world, (ReviewConcern(0, 1, '呃', '语气词', True),))
    assert service.run(capture['item_id']).state == 'succeeded'
    assert reviewer.calls == 0
    env = envelope(raw_files(world, '自述')[0])
    assert '订正' not in env and env['取得方式']['识别']


def test_link_then_annotation_records_adjacency_and_target(world):
    assert send(world, 'om_l1', text='https://www.douyin.com/video/101', at=0) == 'accepted'
    link_item = world.store.recent_items()[0]['item_id']
    assert send(world, 'om_a1', text='这篇重点看后半段', at=95) == 'accepted'
    capture = capture_of(world, 'om_a1')
    assert world.captures.identity(capture['capture_id'])['result'] == 'annotation'
    assert raw_files(world, '自述') == []  # Waits for the link's raw id.
    material_raw = finish_link(world, link_item)
    world.captures.write_ready()
    env = envelope(raw_files(world, '自述')[0])
    assert env['身份'] == '本人附言' and env['附言对象'] == material_raw
    assert env['邻接'] == [{'编号': material_raw, '间隔秒': 95}]


def finish_link(world, item_id):
    """Stand-in for the link pipeline: a finished material and its raw file."""
    from knowledge_distiller.v1.domain import CapturedMaterial, SourceFact
    media = world.tmp / 'link.mp4'
    media.write_bytes(b'media')
    material = world.store.attach_material(item_id, CapturedMaterial(
        'douyin', '101', 'https://www.douyin.com/video/101', 'https://www.douyin.com/video/101', {}, media, 5.0))
    world.store.establish_source_fact(material, SourceFact('链接里的原文。'))
    ledger = RawLedger(world.store)
    record = ledger.ensure_material(material, **world.captures.material_hints(item_id))
    ledger.write(record)
    world.store.mark_succeeded(item_id)
    return record['raw_id']


def test_three_fragments_in_two_minutes_stay_separate_and_unchanged(world):
    world.store.set_setting(CLOUD_SETTING, 'on')
    for index, (mid, text) in enumerate([('om_f1', '先写一个开头'), ('om_f2', '然后是中间的想法'),
                                         ('om_f3', '最后补一句结论')]):
        send(world, mid, text=text, at=index * 50)
    files = raw_files(world, '自述')
    assert len(files) == 3
    by_text = {envelope(f)['标题']: envelope(f) for f in files}
    first, second, third = (capture_of(world, m)['raw_id'] for m in ('om_f1', 'om_f2', 'om_f3'))
    assert by_text['先写一个开头'].get('邻接') is None
    assert by_text['然后是中间的想法']['邻接'] == [{'编号': first, '间隔秒': 50}]
    assert by_text['最后补一句结论']['邻接'] == [{'编号': first, '间隔秒': 100}, {'编号': second, '间隔秒': 50}]
    # Aggregation into one thought belongs to the wiki layer (decision 0004 D8); originals stay intact.
    assert [capture_of(world, m)['text'] for m in ('om_f1', 'om_f2', 'om_f3')] == ['先写一个开头', '然后是中间的想法', '最后补一句结论']


def test_redelivery_and_restart_never_duplicate(world):
    world.store.set_setting(CLOUD_SETTING, 'on')
    send(world, 'om_r1', text='只记一次的想法')
    send(world, 'om_r1', text='只记一次的想法')  # History replay of the same message id.
    restarted = Captures(Store(world.store.path), client_factory=lambda: world.client)
    assert restarted.write_ready() == {}
    assert world.intake.process('om_r1') == 'accepted'
    with connect(world.store.path) as db:
        assert db.execute('SELECT count(*) FROM captures').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM raw_records').fetchone()[0] == 1
    assert len(raw_files(world, '自述')) == 1 and len(world.client.calls) == 1


# ───────────────────────── Irreversible guarantees ─────────────────────────

def test_capture_records_are_immutable(world):
    world.store.set_setting(CLOUD_SETTING, 'on')
    send(world, 'om_i0', text='https://www.douyin.com/video/101')
    send(world, 'om_i1', text='不可改的原话', at=10)  # Rows exist, so each guard is exercised.
    with connect(world.store.path) as db:
        for sql in ("UPDATE captures SET text='改'", 'DELETE FROM captures',
                    "UPDATE capture_identity_events SET result='third_party'", 'DELETE FROM capture_identity_events',
                    "UPDATE delivery_adjacency SET gap_seconds=0", 'DELETE FROM delivery_adjacency'):
            with pytest.raises(sqlite3.IntegrityError, match='immutable'):
                db.execute(sql)


def test_changing_a_written_decision_supersedes_and_keeps_the_old_file(world):
    world.store.set_setting(CLOUD_SETTING, 'on')
    send(world, 'om_l2', text='https://www.douyin.com/video/101', at=0)
    finish_link(world, world.store.recent_items()[0]['item_id'])
    send(world, 'om_c1', text='这条先存着以后看', at=30)
    capture = capture_of(world, 'om_c1')
    old = raw_files(world, '自述')
    old_text = old[0].read_text(encoding='utf-8')
    world.captures.decide(capture['capture_id'], 'third_party')
    run_material(world, capture_of(world, 'om_c1')['item_id'])
    external = raw_files(world, '外部')
    new = [f for f in external if envelope(f).get('取代') == envelope(old[0])['编号']]
    assert len(new) == 1 and old[0].read_text(encoding='utf-8') == old_text
    events = [e['result'] for e in world.captures.events(capture['capture_id'])]
    assert events[0] in {'annotation', 'my_thought'} and events[-1] == 'third_party'


def test_adjacency_is_fixed_when_captured(world):
    send(world, 'om_x1', text='https://www.douyin.com/video/101', at=0)
    send(world, 'om_x2', text='记一下', at=40)
    send(world, 'om_x3', text='https://www.douyin.com/video/102', at=60)  # Later deliveries are not "before" om_x2.
    with connect(world.store.path) as db:
        rows = db.execute("SELECT earlier_message_id, gap_seconds FROM delivery_adjacency WHERE message_id='om_x2'").fetchall()
    assert [tuple(r) for r in rows] == [('om_x1', 40)]


def test_window_is_configurable(world):
    world.store.set_setting('capture_adjacency_minutes', '1')
    send(world, 'om_w1', text='https://www.douyin.com/video/101', at=0)
    send(world, 'om_w2', text='两分钟后的想法', at=120)
    with connect(world.store.path) as db:
        assert db.execute("SELECT count(*) FROM delivery_adjacency WHERE message_id='om_w2'").fetchone()[0] == 0


def test_settings_toggle_controls_the_cloud_judge(world):
    from knowledge_distiller.v1.settings import SettingsService
    from knowledge_distiller.v1.web import create_app
    client = create_app(world.store, object(), SettingsService(world.store, qwen_probe=lambda: True)).test_client()
    page = client.get('/settings?open=paths').get_data(as_text=True)
    assert '随手记云端判断' in page and '已关闭' in page and '启用此判断' in page
    assert client.post('/settings/capture-judgment', data={'action': 'enable'}).status_code == 302
    assert world.store.setting(CLOUD_SETTING) == 'on'
    assert '关闭此判断' in client.get('/settings?open=paths').get_data(as_text=True)
    client.post('/settings/capture-judgment', data={'action': 'disable'})
    assert world.store.setting(CLOUD_SETTING) == 'off'


def test_browser_desk_decision_writes_the_note_and_clears_the_card(world):
    import threading
    from playwright.sync_api import sync_playwright, expect
    from werkzeug.serving import make_server
    from knowledge_distiller.v1.web import create_app
    send(world, 'om_b1', text='要不要换个方向')
    server = make_server('127.0.0.1', 0, create_app(world.store, object()), threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as playwright:
            if not Path(playwright.chromium.executable_path).exists():
                pytest.skip('Browser regression requires playwright install chromium')
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.goto(f'http://127.0.0.1:{server.server_port}/')
            page.locator('details[data-persist-details="todo"] > summary').click()
            card = page.locator('[data-sync-key^="capture-"]')
            expect(card).to_contain_text('要不要换个方向')
            page.locator('textarea[name="content"]').fill('未提交的投递草稿')
            card.get_by_role('button', name='我的想法').click()
            expect(page.locator('[data-sync-key^="capture-"]')).to_have_count(0)
            assert page.locator('textarea[name="content"]').input_value() == '未提交的投递草稿'
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=2)
    assert len(raw_files(world, '自述')) == 1
