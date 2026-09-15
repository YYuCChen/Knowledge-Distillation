from knowledge_distiller.v1.database import SCHEMA_VERSION
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from knowledge_distiller.v1.chrome import ChromeSessionError
from knowledge_distiller.v1.database import connect
from knowledge_distiller.v1.settings import SettingsService
from knowledge_distiller.v1.store import Store
from knowledge_distiller.v1.web import create_app
from knowledge_distiller.v1.youtube import (
    YouTubeSource, YouTubeSourceError, YouTubeConnection, connection_authority,
    youtube_identity, _qualify,
)

KEY = 'aaaaaaaaaaa'
URL = f'https://www.youtube.com/watch?v={KEY}'


@pytest.fixture
def store(tmp_path):
    store = Store(tmp_path / 'test.sqlite3')
    store.initialize()
    store.save_connection('youtube', None)
    return store


@pytest.mark.parametrize('url', [URL, f'https://youtu.be/{KEY}?si=share', f'https://m.youtube.com/shorts/{KEY}', f'https://www.youtube.com/live/{KEY}?si=share'])
def test_url_variants_have_one_exact_identity(url):
    assert youtube_identity(url) == (KEY, URL)


@pytest.mark.parametrize('url', [URL+'&v=abcdefghijk', URL+'&list=PL123',
    'https://www.youtube.com/@channel', 'https://www.youtube.com/playlist?list=PL123',
    'https://youtube.com.evil.test/watch?v='+KEY, 'https://user:secret@youtube.com/watch?v='+KEY,
    'https://www.youtube.com/watch?v=123', 'https://youtu.be/'+KEY+'/extra'])
def test_rejects_ambiguous_or_non_item_input(url):
    with pytest.raises(ValueError): youtube_identity(url)


@pytest.mark.parametrize('change', [{'id':'different'}, {'live_status':'is_live'},
    {'live_status':'post_live'}, {'live_status':None}, {'duration':float('nan')}, {'duration':True}])
def test_rejects_wrong_identity_and_unfinished_or_unknown_audio(change):
    with pytest.raises(YouTubeSourceError):
        _qualify({'id':KEY,'live_status':'not_live','duration':2, **change}, KEY)


class Session:
    def __init__(self): self.calls = 0
    def cookies(self): self.calls += 1; return []
    def verify(self): return YouTubeConnection()


@pytest.fixture
def media(tmp_path):
    target = tmp_path / 'fixture.mp4'
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=black:s=32x32:r=2',
                    '-f','lavfi','-i','sine=frequency=400','-t','2','-c:v','libx264','-c:a','aac',str(target)], check=True)
    return target


def downloader(media, change=None):
    def run(key, url, cookies, root):
        assert (key, url) == (KEY, URL)
        root.mkdir(parents=True, exist_ok=True)
        target = root / 'source.mp4'; shutil.copyfile(media, target)
        if change: change()
        return {'id':KEY,'live_status':'not_live','duration':2,'title':'样本'}, target
    return run


def test_audio_only_capture_complete_decode_and_exact_retained_reuse(store, media, tmp_path):
    source = YouTubeSource(store, Session(), downloader(media))
    authority = connection_authority(store.connection('youtube'))
    captured = source.capture(URL, tmp_path/'work', expected_authority=authority)
    probe = json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-of','json',str(captured.media_path)]))
    assert {s['codec_type'] for s in probe['streams']} == {'audio'}
    assert captured.metadata['session_authority'] == authority
    assert not (tmp_path/'work'/'download').exists()
    reused = source.reuse_retained(source_key=KEY, submitted_url=URL, canonical_url=URL,
        metadata=captured.metadata, work_dir=tmp_path/'work', expected_authority=authority)
    assert reused.media_path == captured.media_path
    captured.media_path.write_bytes(b'partial')
    with pytest.raises(YouTubeSourceError, match='youtube_media_invalid'):
        source.reuse_retained(source_key=KEY, submitted_url=URL, canonical_url=URL,
            metadata=captured.metadata, work_dir=tmp_path/'work', expected_authority=authority)


def test_mid_capture_reconnect_cannot_qualify_material(store, media, tmp_path):
    source = YouTubeSource(store, Session(), downloader(media, lambda:store.save_connection('youtube', None)))
    with pytest.raises(ChromeSessionError, match='youtube_connection_changed'):
        source.capture(URL, tmp_path/'work')
    assert not (tmp_path/'work'/'media'/'source.mka').exists()


def test_repeated_capture_has_stable_snapshot_and_reuses_material(store, media, tmp_path):
    from knowledge_distiller.v1.source_versions import snapshot_key
    source = YouTubeSource(store, Session(), downloader(media))
    first = source.capture(URL, tmp_path/'first')
    second = source.capture(URL, tmp_path/'second')
    assert first.media_path.read_bytes() == second.media_path.read_bytes()
    assert snapshot_key(first) == snapshot_key(second)
    first_item, second_item = store.create_item(URL), store.create_item(URL)
    assert store.attach_material(first_item, first) == store.attach_material(second_item, second)
    # A real source change must still produce a distinct immutable snapshot.
    second.media_path.write_bytes(second.media_path.read_bytes() + b'changed')
    assert snapshot_key(first) != snapshot_key(second)


def test_queue_freezes_generation_and_does_not_use_replaced_session(store, tmp_path):
    item = store.create_item(URL)
    bound = json.loads(store.item_bundle(item)['platform_authority_json'])
    store.save_connection('youtube', None)
    session = Session()
    with pytest.raises(ChromeSessionError, match='youtube_connection_changed'):
        YouTubeSource(store, session).capture(URL, tmp_path/'work', expected_authority=bound)
    assert session.calls == 0


def test_attach_rechecks_authority_in_transaction(store, media, tmp_path):
    captured = YouTubeSource(store, Session(), downloader(media)).capture(URL, tmp_path/'work')
    item = store.create_item(URL)
    store.clear_connection('youtube')
    with pytest.raises(ChromeSessionError): store.attach_material(item, captured)
    with connect(store.path) as db:
        assert db.execute('SELECT count(*) FROM materials').fetchone()[0] == 0


def test_settings_and_home_intake_use_youtube_connection(store):
    session = Session()
    service = SettingsService(store, youtube=session)
    app = create_app(store, lambda:None, service)
    client = app.test_client()
    page = client.get('/settings').get_data(as_text=True)
    assert '/settings/youtube/connect' in page and '/settings/youtube/clear' in page
    client.post('/settings/youtube/clear')
    assert client.post('/submissions', data={'content':URL}).status_code == 400
    assert store.recent_items() == ()
    assert client.post('/settings/youtube/connect').status_code == 302
    assert client.post('/submissions', data={'content':URL}).status_code == 302
    assert len(store.recent_items()) == 1
    assert 'YouTube' in client.get('/').get_data(as_text=True)


def test_explicit_retry_after_failed_acquisition_binds_reconnected_session(store):
    item = store.create_item(URL)
    old = json.loads(store.item_bundle(item)['platform_authority_json'])
    store.mark_failed(item, 'collecting', 'youtube_upstream_failed')
    store.save_connection('youtube', None)
    store.retry_item(item)
    row = store.item_bundle(item)
    current = json.loads(row['platform_authority_json'])
    assert row['state'] == 'queued' and current['generation'] == old['generation'] + 1
    assert current == connection_authority(store.connection('youtube'))


def test_v7_upgrade_preserves_queue_and_connections(tmp_path):
    path=tmp_path/'v7.sqlite3'
    store=Store(path);store.initialize()
    item=store.create_item('https://www.douyin.com/video/123')
    store.save_connection('douyin','显示名')
    before=dict(store.connection('douyin'))
    with connect(path) as db:
        for table in ('media_lifecycle','feishu_parts','feishu_receipts','feishu_binding','collection_previews','collection_confirmations','collection_events','collection_results','collection_members','collection_operations'):
            db.execute('DROP TABLE '+table)
        db.execute('DROP TABLE source_media')
        db.execute('ALTER TABLE source_connections DROP COLUMN browser_context')
        # A historical v7 database has none of the v18 review objects.
        db.execute('DROP TRIGGER distill_review_revision')
        db.execute('DROP TABLE source_review_results')
        db.execute('ALTER TABLE distill_items DROP COLUMN review_revision')
        db.execute('ALTER TABLE distill_items DROP COLUMN platform_authority_json')
        db.execute("DROP TABLE IF EXISTS group_decisions")
        db.execute("DROP TABLE IF EXISTS manual_cards")
        db.execute('PRAGMA user_version=7')
    store.initialize()
    assert dict(store.connection('douyin'))==before
    row=store.item_bundle(item)
    assert row['state']=='queued' and row['platform_authority_json']=='{}'
    with connect(path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0]== SCHEMA_VERSION
        assert not db.execute('PRAGMA foreign_key_check').fetchall()


def test_expired_audio_is_reacquired_without_overwriting_formal_fact(store, media, tmp_path):
    from datetime import UTC, datetime, timedelta
    source=YouTubeSource(store, Session(), downloader(media))
    captured=source.capture(URL,tmp_path/'work')
    old=dict(captured.metadata)
    old['captured_at']=(datetime.now(UTC)-timedelta(hours=73)).isoformat()
    assert source.reuse_retained(source_key=KEY,submitted_url=URL,canonical_url=URL,
        metadata=old,work_dir=tmp_path/'work') is None


def test_broken_optional_caption_keeps_audio_fallback_available(tmp_path):
    from knowledge_distiller.v1.youtube import _captions
    from knowledge_distiller.v1.subtitle_baseline import select_subtitle
    from types import SimpleNamespace
    path = tmp_path / 'broken.vtt'
    path.write_bytes(b'\xff\xfeinvalid utf8')
    tracks = _captions({'id': KEY, 'requested_subtitles': {'en': {'filepath': str(path)}}}, tmp_path)
    assert tracks[0]['error'] == 'optional_caption_unavailable'
    selected, diagnostics = select_subtitle(SimpleNamespace(source_key=KEY,
        duration_seconds=20, metadata={'original_language':'en', 'captions':tracks}))
    assert selected is None
    assert diagnostics['caption_selection']


def test_new_generation_retry_reacquires_unfinished_material(store, media, tmp_path):
    source=YouTubeSource(store,Session(),downloader(media))
    item=store.create_item(URL)
    captured=source.capture(URL,tmp_path/'work')
    store.attach_material(item,captured)
    store.mark_failed(item,'reviewing','review_runtime_unavailable')
    store.save_connection('youtube',None)
    store.retry_item(item)
    bound=json.loads(store.item_bundle(item)['platform_authority_json'])
    assert source.reuse_retained(source_key=KEY,submitted_url=URL,canonical_url=URL,
        metadata=captured.metadata,work_dir=tmp_path/'work',expected_authority=bound) is None
    newer=source.capture(URL,tmp_path/'work',expected_authority=bound)
    store.attach_material(item,newer)
    assert json.loads(store.item_bundle(item)['metadata_json'])['session_authority']==bound


def test_queued_youtube_item_is_not_labeled_douyin(store):
    from knowledge_distiller.v1.web import _item_view
    item=store.create_item(URL)
    view=_item_view(store.item_bundle(item),None,store.path.parent)
    assert view['title']=='YouTube 内容'
    assert view['source_type']=='YouTube'


def test_caption_identity_requires_exact_inventory_and_exposes_missing_language(tmp_path):
    from types import SimpleNamespace
    from knowledge_distiller.v1.youtube import _captions
    from knowledge_distiller.v1.subtitle_baseline import select_subtitle
    path = tmp_path / 'caption.vtt'
    path.write_text('WEBVTT\n\n00:00.000 --> 00:01.000\nsource text\n')
    url = 'https://captions.example/video?lang=en'
    info = {'id': KEY, 'requested_subtitles': {'en': {'filepath': str(path), 'url': url}},
            'subtitles': {'en': [{'url': url}]}}
    tracks = _captions(info, tmp_path)
    assert tracks[0]['kind'] == 'manual' and tracks[0]['translated'] is False
    selected, detail = select_subtitle(SimpleNamespace(source_key=KEY, duration_seconds=1,
        metadata={'captions': tracks, 'original_language': None}))
    assert selected is None and detail['caption_selection'][0]['code'] == 'original_language_unverified'
    info['subtitles']['en'][0]['url'] = 'https://captions.example/other'
    assert _captions(info, tmp_path)[0]['kind'] == 'unverified'
    translated = 'https://captions.example/video?lang=es&%74lang=en'
    info['requested_subtitles']['en']['url'] = translated
    info['automatic_captions'] = {'en': [{'url': translated}]}
    track = _captions(info, tmp_path)[0]
    assert track['kind'] == 'automatic' and track['translated'] is True


def test_invalid_optional_caption_url_does_not_abort_audio_fallback(tmp_path):
    from knowledge_distiller.v1.youtube import _captions
    path = tmp_path / 'caption.vtt'
    path.write_text('WEBVTT\n\n00:00.000 --> 00:01.000\nsource text\n')
    url = 'https://[invalid/video'
    info = {'id': KEY, 'requested_subtitles': {'en': {'filepath': str(path), 'url': url}},
            'subtitles': {'en': [{'url': url}]}}
    track = _captions(info, tmp_path)[0]
    assert track['translated'] is None
