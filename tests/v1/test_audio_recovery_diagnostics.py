"""BUG-20260917-01: each concern-audio recovery failure has its own redacted subcode.

Synthetic audio and isolated data only. The card wording, text, candidates and
any saved judgement stay exactly as they were after every failure.
"""
import json
import threading
import wave
from pathlib import Path

import pytest

from knowledge_distiller.faithful_review import ReviewConcern
from knowledge_distiller.primary import PrimaryChunk, PrimaryRecovery, StandardAudio
from knowledge_distiller.v1 import audio_diagnostics
from knowledge_distiller.v1.confirmation import ConfirmationAudioError, FFmpegConfirmationClipper
from .test_pipeline import distiller

CONCERN = ReviewConcern(0, 4, '持续切换', '首词可能识别错误', True, ('继续切换',))
PRIVATE = ('持续切换', '继续切换', '额外损耗')


def synthetic_wav(path, seconds=10):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        stream.writeframes(b'\x01\x00' * 16000 * seconds)


class FailingClipper:
    def __init__(self, stage):
        self.stage = stage
    def clip(self, *args):
        raise ConfirmationAudioError('confirmation_audio_unavailable', stage=self.stage)


@pytest.fixture
def waiting(tmp_path):
    """A task waiting on one concern whose first clip failed (the card's recovery entry)."""
    service, store, *_ = distiller(tmp_path, concerns=(CONCERN,))
    service.confirmation_clipper = FailingClipper('ffmpeg')
    item = store.create_item('https://v.douyin.com/test/')
    assert service.run(item).state == 'waiting_user'
    pending = json.loads(store.item_bundle(item)['confirmation_json'])
    assert pending['concerns'][0]['audio_recovery_required']
    synthetic_wav(service.runtime_root / 'items' / str(item) / 'audio' / 'standard.wav')
    return service, store, item


def stages(service, item):
    return [entry['stage'] for entry in audio_diagnostics.entries(service.runtime_root, item)]


def recover(service, store, item):
    pending = json.loads(store.item_bundle(item)['confirmation_json'])
    return service.recover_confirmation_audio(item, token=pending['token'],
                                              concern_id=pending['concerns'][0]['audio_name'])


def unchanged_after_failure(service, store, item, stage):
    before = store.item_bundle(item)['confirmation_json']
    with pytest.raises(ValueError):
        recover(service, store, item)
    assert store.item_bundle(item)['confirmation_json'] == before  # Text, candidates, judgement kept.
    entry = audio_diagnostics.entries(service.runtime_root, item)[-1]
    assert entry['stage'] == stage
    assert entry['concern_id'] == json.loads(before)['concerns'][0]['audio_name']
    record = (service.runtime_root / 'items' / str(item) / audio_diagnostics.NAME).read_text()
    assert not any(value in record for value in PRIVATE)
    return entry


def test_first_clip_failure_is_recorded_as_the_reason_for_the_recovery_entry(waiting):
    service, store, item = waiting
    entry, = audio_diagnostics.entries(service.runtime_root, item)
    assert entry['stage'] == 'initial_clip_failed' and entry['clip_stage'] == 'ffmpeg'


def test_timeline_missing(waiting):
    service, store, item = waiting
    row = store.item_bundle(item)
    pending = json.loads(row['confirmation_json'])
    pending.pop('audio_timeline')
    store.update_confirmation_suggestions(item, row['confirmation_json'], pending)
    unchanged_after_failure(service, store, item, 'timeline_missing')


def test_source_audio_missing(waiting):
    service, store, item = waiting
    (service.runtime_root / 'items' / str(item) / 'audio' / 'standard.wav').unlink()
    assert unchanged_after_failure(service, store, item, 'source_audio_missing')['exception_type'] == 'ValueError'


def test_relocation_failure_from_replay_asr(waiting, monkeypatch):
    service, store, item = waiting
    monkeypatch.setattr('knowledge_distiller.v1.pipeline.locate_concern_audio', lambda *args, **kwargs: None)
    def replay_fails(*args):
        raise ValueError('replay recognition failed')
    monkeypatch.setattr('knowledge_distiller.v1.audio_location_recovery.recover_locations', replay_fails)
    unchanged_after_failure(service, store, item, 'relocation_failed')


def test_relocation_failure_when_recovered_timeline_still_cannot_locate(waiting):
    service, store, item = waiting
    service.confirmation_clipper = FailingClipper('locate')
    assert unchanged_after_failure(service, store, item, 'relocation_failed')['clip_stage'] == 'locate'


@pytest.mark.parametrize('clip_stage,stage', [('ffmpeg', 'clip_failed'), ('invalid_output', 'clip_failed'),
                                              ('write', 'write_failed')])
def test_clip_and_write_failures(waiting, clip_stage, stage):
    service, store, item = waiting
    service.confirmation_clipper = FailingClipper(clip_stage)
    assert unchanged_after_failure(service, store, item, stage)['clip_stage'] == clip_stage


def test_state_save_failure_removes_the_unrecorded_clip(waiting, monkeypatch):
    service, store, item = waiting
    from .test_pipeline import Clipper
    service.confirmation_clipper = Clipper()
    def conflict(*args):
        raise ValueError('来源确认已更新，请刷新后再操作。')
    monkeypatch.setattr(store, 'update_confirmation_suggestions', conflict)
    unchanged_after_failure(service, store, item, 'state_save_failed')
    assert not list((service.runtime_root / 'items' / str(item) / 'confirmation').glob('concern-1-*.wav'))


def test_recovered_clip_is_recorded_and_served(waiting):
    service, store, item = waiting
    from .test_pipeline import Clipper
    service.confirmation_clipper = Clipper()
    recover(service, store, item)
    concern = json.loads(store.item_bundle(item)['confirmation_json'])['concerns'][0]
    assert not concern['audio_recovery_required'] and stages(service, item)[-1] == 'recovered'
    assert service.confirmation_audio(item, concern['audio_name']).read_bytes() == b'local confirmation audio'


@pytest.mark.parametrize('case,stage', [('locate', 'locate'), ('ffmpeg', 'ffmpeg'),
                                        ('invalid', 'invalid_output'), ('write', 'write')])
def test_real_clipper_reports_its_failing_step(tmp_path, monkeypatch, case, stage):
    from types import SimpleNamespace
    source = tmp_path / 'standard.wav'
    synthetic_wav(source)
    monkeypatch.setattr('knowledge_distiller.v1.confirmation.locate_concern_audio',
                        lambda *args, **kwargs: None if case == 'locate' else (1.0, 3.0))
    class Runner:
        def run(self, command):
            if case == 'ffmpeg':
                return SimpleNamespace(returncode=1)
            Path(command[-1]).write_bytes(b'not a wav')
            return SimpleNamespace(returncode=0)
    output = tmp_path / 'confirmation' / 'concern-1.wav'
    if case == 'write':
        (tmp_path / 'confirmation').write_text('a file blocks the clip directory')
    recovery = PrimaryRecovery('持续切换', 'zh', (PrimaryChunk('持续切换', 0, 10, 'zh'),))
    with pytest.raises(ConfirmationAudioError) as failure:
        FFmpegConfirmationClipper(Runner()).clip(StandardAudio(source, 10), recovery, '持续切换', CONCERN, output)
    assert failure.value.args == ('confirmation_audio_unavailable',) and failure.value.stage == stage


def test_http_serve_and_browser_playback_failures_are_distinct(waiting):
    from knowledge_distiller.v1.web import create_app
    service, store, item = waiting
    concern = json.loads(store.item_bundle(item)['confirmation_json'])['concerns'][0]['audio_name']
    client = create_app(store, service).test_client()
    assert client.get(f'/items/{item}/confirmation-audio', query_string={'concern_id': concern}).status_code == 404
    assert client.post(f'/items/{item}/confirmation-audio/diagnostic',
                       data={'concern_id': concern, 'media_error': '4'}).status_code == 204
    serve, playback = audio_diagnostics.entries(service.runtime_root, item)[-2:]
    assert (serve['stage'], serve['reason']) == ('serve_failed', 'missing_file')
    assert (playback['stage'], playback['media_error'], playback['concern_id']) == ('playback_failed', 4, concern)
    assert client.post('/items/999/confirmation-audio/diagnostic', data={}).status_code == 404


def test_browser_reports_a_concern_audio_that_cannot_play(waiting):
    from playwright.sync_api import sync_playwright
    from werkzeug.serving import make_server
    from knowledge_distiller.v1.web import create_app
    service, store, item = waiting
    server = make_server('127.0.0.1', 0, create_app(store, service), threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as playwright:
            if not Path(playwright.chromium.executable_path).exists():
                pytest.skip('Browser regression requires playwright install chromium')
            browser = playwright.chromium.launch()
            page = browser.new_page()
            with page.expect_request(lambda request: request.url.endswith('/confirmation-audio/diagnostic')):
                page.goto(f'http://127.0.0.1:{server.server_port}/?item={item}')
            page.wait_for_timeout(300)
            recovery_entries = page.locator('form[data-recover-audio]').count()
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=2)
    assert 'playback_failed' in stages(service, item) and 'serve_failed' in stages(service, item)
    assert recovery_entries == 1  # The finalized card keeps its recovery entry.
