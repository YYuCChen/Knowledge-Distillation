import base64
import json
from pathlib import Path
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from Crypto.PublicKey import ECC
from Crypto.Signature import eddsa
import pytest

from knowledge_distiller.v1.updates import Updates, UpdateError, parse_feed
from knowledge_distiller.v1.store import Store
from knowledge_distiller.v1.worker import SingleWorker


def signed_feed(key, version='2', archive=b'archive', name='update.zip'):
    sign = lambda data: base64.b64encode(eddsa.new(key, 'rfc8032').sign(data)).decode()
    content = f'''<rss xmlns:sparkle="http://www.andymatuschak.org/xml-namespaces/sparkle"><channel><item><sparkle:version>{version}</sparkle:version><description>测试说明</description><enclosure url="{name}" length="{len(archive)}" sparkle:edSignature="{sign(archive)}" /></item></channel></rss>\n'''.encode()
    return content+f'<!-- sparkle-signatures:\nedSignature: {sign(content)}\nlength: {len(content)}\n-->\n'.encode()


@pytest.fixture
def key():
    return ECC.generate(curve='Ed25519')


def public(key):
    return base64.b64encode(key.public_key().export_key(format='raw')).decode()


def test_full_fallback_requires_explicit_action_and_persists_choice(tmp_path,key,monkeypatch):
    info={'version':'1','display_version':'1','bundle':None,'public_key':public(key),'feed_url':'https://example.test/appcast.xml'}
    updates=Updates(tmp_path,info=info)
    updates.root.mkdir()
    release=parse_feed(signed_feed(key),public(key),'1')
    release['selected']={'name':'small.delta','size':2,'signature':'test'}
    updates.release=release
    (updates.root/'full-update-required.json').write_text(json.dumps({'version':'2'}))
    assert updates.snapshot()['full_update_required']
    updates.phase='downloaded'
    installed=[]
    updates.install=lambda:installed.append(True)
    with pytest.raises(UpdateError,match='请先确认改用完整包'):
        updates.request_install()
    assert installed==[]
    downloaded=[]
    monkeypatch.setattr(updates,'download',lambda asset:downloaded.append(asset['name']))
    assert downloaded==[]
    updates.start('download-full');updates.thread.join(3)
    assert downloaded==['update.zip']
    assert updates.record['full_version']=='2'
    assert not updates.snapshot()['full_update_required']
    assert updates._parse_release(signed_feed(key))['selected']['name']=='update.zip'


def test_feed_authenticates_notes_and_paths_before_use(key):
    feed = signed_feed(key)
    assert parse_feed(feed, public(key), '1')['notes'] == '测试说明'
    assert parse_feed(feed, public(key), '2') is None
    with pytest.raises(UpdateError):
        parse_feed(feed.replace('测试'.encode(), '伪造'.encode()), public(key), '1')
    with pytest.raises(UpdateError):
        parse_feed(signed_feed(key, name='../private'), public(key), '1')
    with pytest.raises(UpdateError):
        parse_feed(feed, public(ECC.generate(curve='Ed25519')), '1')


def test_daily_check_seen_version_download_and_restart(tmp_path, key):
    remote = tmp_path/'remote'; remote.mkdir()
    (remote/'appcast.xml').write_bytes(signed_feed(key))
    (remote/'update.zip').write_bytes(b'archive')
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs): super().__init__(*args, directory=remote, **kwargs)
        def log_message(self, *args): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    info = {'version': '1', 'display_version': '1', 'bundle':None, 'public_key':public(key),
            'feed_url':f'http://127.0.0.1:{server.server_port}/appcast.xml', 'testing':True}
    try:
        updates = Updates(tmp_path/'data', info=info, clock=lambda: 100000)
        assert updates.start('check', automatic=True); updates.thread.join(5)
        assert updates.phase == 'available'
        assert not updates.start('check', automatic=True)
        assert updates.snapshot()['attention']
        updates.mark_seen('2'); assert not updates.snapshot()['attention']
        updates.start('download'); updates.thread.join(5)
        assert updates.phase == 'downloaded'
        restarted = Updates(tmp_path/'data', info=info, clock=lambda: 100001)
        assert restarted.phase == 'downloaded'
        assert not restarted.snapshot()['attention']
        assert not restarted.start('check', automatic=True)
        # Downloading has never invoked installation; a separate explicit action does.
        installed = []
        restarted.install = lambda: installed.append(True)
        assert installed == []
        restarted.request_install(); assert installed == [True]
        (remote/'appcast.xml').write_bytes(signed_feed(key, version='3'))
        updates.start('check'); updates.thread.join(5)
        assert updates.snapshot()['attention']
    finally:
        server.shutdown(); server.server_close()


def test_corrupt_download_and_install_recheck(tmp_path, key):
    info = {'version':'1', 'display_version':'1', 'bundle':None, 'public_key':public(key), 'feed_url':'https://example.test/appcast.xml'}
    updates = Updates(tmp_path, info=info)
    updates.release = parse_feed(signed_feed(key), public(key), '1')
    updates.root.mkdir()
    (updates.root/'update.zip').write_bytes(b'corrupt')
    updates.phase = 'downloaded'
    installed = []
    updates.install = lambda: installed.append(True)
    with pytest.raises(UpdateError): updates.request_install()
    assert not installed


def test_restart_after_interrupted_download_keeps_available_version(tmp_path, key):
    root = tmp_path/'updates'; root.mkdir()
    (root/'appcast.xml').write_bytes(signed_feed(key))
    (root/'update.zip.part').write_bytes(b'incomplete')
    info = {'version':'1', 'display_version':'1', 'bundle':None,
            'public_key':public(key), 'feed_url':'https://example.test/appcast.xml'}
    updates = Updates(tmp_path, info=info)
    assert updates.snapshot()['phase'] == 'available'
    assert updates.snapshot()['release']['version'] == '2'
    assert updates.thread is None  # Restoring state never restarts downloads.


def test_install_reservation_blocks_claim_race(tmp_path):
    store = Store(tmp_path/'db.sqlite3'); store.initialize()
    worker = SingleWorker(store, lambda: None)
    worker._activity.acquire()
    try: assert not worker.reserve_for_update()
    finally: worker._activity.release()
    assert worker.reserve_for_update()
    assert worker._update_reserved
    worker.release_update()
    assert not worker._update_reserved


def test_update_actions_require_same_origin_token(tmp_path):
    from knowledge_distiller.v1.web import create_app
    store = Store(tmp_path/'db.sqlite3')
    app = create_app(store, lambda: None)
    client = app.test_client()
    assert client.post('/settings/updates/check').status_code == 403
    token = app.extensions['updates'].token
    assert client.post('/settings/updates/check', headers={'X-Update-Token':token, 'Origin':'https://evil.test'}).status_code == 403
    assert client.post('/settings/updates/check', headers={'X-Update-Token':token}).status_code == 409
    assert client.get('/settings').status_code == 200


def test_installing_blocks_concurrent_check_after_http_gate(tmp_path, key):
    updates = Updates(tmp_path, info={'version':'1','display_version':'1','bundle':None,'public_key':public(key),'feed_url':'https://example.test/appcast.xml'})
    updates.phase = 'installing'
    with pytest.raises(UpdateError): updates.start('check')
    with pytest.raises(UpdateError): updates.start('download')
    assert updates.phase == 'installing'


def test_queued_work_refuses_install_reservation(tmp_path):
    store = Store(tmp_path/'db.sqlite3'); store.initialize()
    store.create_item('https://www.douyin.com/video/999')
    worker = SingleWorker(store, lambda: None)
    assert not worker.reserve_for_update()
    assert not worker._update_reserved


def test_candidate_startup_holds_all_workers_and_web_writes(tmp_path):
    from knowledge_distiller.v1.app import AppPaths, create_application
    app = create_application(AppPaths(tmp_path), start_workers=False)
    worker = app.config['KNOWLEDGE_DISTILLER_WORKER']
    assert worker._thread is None
    assert app.test_client().post('/organization').status_code == 503
    assert app.test_client().get('/').status_code == 200
    assert app.extensions['updates'].phase == 'installing'


@pytest.mark.parametrize('width', [1440, 640, 390])
def test_compact_update_journey_and_notes_ack(tmp_path, key, width):
    from knowledge_distiller.v1.web import create_app
    from werkzeug.serving import make_server
    from playwright.sync_api import sync_playwright
    app = create_app(Store(tmp_path/'db.sqlite3'), lambda: None)
    updates = app.extensions['updates']
    updates.info.update(version='1',display_version='2026.09.09.11',public_key=public(key),feed_url='https://example.test/appcast.xml')
    updates.release = parse_feed(signed_feed(key, version='2026.09.09.12'), public(key), '1')
    updates.phase = 'available'
    updates.record['checked_at'] = 100000
    server = make_server('127.0.0.1', 0, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={'width':width,'height':1024})
            page.goto(f'http://127.0.0.1:{server.server_port}/settings')
            group = page.locator('#version-updates')
            assert group.locator('summary').first.inner_text().strip().endswith('版本更新')
            assert page.locator('[data-update-chevron]').get_attribute('alt') == '有新版更新说明'
            assert page.locator('[data-update-description]').is_hidden()
            assert updates.snapshot()['attention']
            group.locator('summary').first.click()
            assert page.locator('[data-update-primary]').inner_text() == '下载更新'
            page.wait_for_function('document.querySelector("[data-update-chevron]").alt === ""')
            assert page.locator('[data-update-notes-toggle]').count() == 0
            assert page.locator('[data-update-package-size]').inner_text().startswith('更新包大小 ')
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            if width == 1440:
                assert page.locator('.update-content').bounding_box()['height'] < 160
                assert abs(page.locator('[data-update-status]').bounding_box()['y'] - page.locator('[data-update-package-size]').bounding_box()['y']) < 6
            assert page.locator('[data-update-description]').is_visible()
            assert not updates.snapshot()['attention']
            assert updates.release['version'] == '2026.09.09.12'
            page.goto(f'http://127.0.0.1:{server.server_port}/settings?open=updates')
            assert page.locator('[data-update-description]').is_visible()
            assert page.locator('[data-update-chevron]').get_attribute('alt') == ''
            assert page.locator('[data-update-primary]').inner_text() == '下载更新'
            updates.root.mkdir(exist_ok=True)
            updates.release['selected']={'name':'failed.delta','size':1375950,'signature':'test'}
            (updates.root/'full-update-required.json').write_text(json.dumps({'version':updates.release['version']}))
            updates.phase='downloaded'
            page.reload()
            assert page.locator('[data-update-primary]').is_hidden()
            assert page.locator('[data-update-full-fallback]').is_visible()
            assert '尚未下载完整包' in page.locator('[data-update-status]').inner_text()
            assert page.locator('[data-update-package-size]').inner_text() == '更新包大小 1.3MB'
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            browser.close()
    finally:
        server.shutdown(); server.server_close()


def test_manual_candidate_cannot_install_even_with_callback(tmp_path):
    updates=Updates(tmp_path,info={'version':'1','display_version':'1.1','bundle':None,
                    'public_key':'','feed_url':'','manual_update_only':True})
    called=[]
    updates.install=lambda:called.append(True)
    updates.phase='downloaded'
    assert updates.snapshot()['can_install'] is False
    with pytest.raises(UpdateError,match='手动'):
        updates.request_install()
    assert not called
