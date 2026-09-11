"""Regressions for the September 9 activity and reading-style feedback."""
import hashlib
import json
import sqlite3
import threading
from pathlib import Path

import pytest

from knowledge_distiller.v1 import reading_style
from knowledge_distiller.v1.collections import Collections
from knowledge_distiller.v1.web import create_app
from .test_topics import library
from .test_organization import organization


@pytest.mark.parametrize('organizing,working,queued,label', [
    (False,False,False,'正常'), (True,False,False,'正在整理'),
    (False,True,False,'正在蒸馏'), (True,True,False,'正在整理 · 正在蒸馏'),
    (False,False,True,'队列等待'),
])
def test_activity_uses_persisted_work_and_keeps_zero_queue_hidden(tmp_path,organizing,working,queued,label):
    _,store,_=library(tmp_path)
    if organizing:
        service,_=organization(store)
        service.start_or_reuse()
    if working or queued:
        store.create_item('https://www.douyin.com/video/999')
        if working:store.claim_next_item()
    app=create_app(store,object(),collection_service=Collections(store))
    status=app.test_client().get('/').text.split('<span class="topbar-status"',1)[1].split('</header>',1)[0]
    assert label in status
    assert ('is-active' in status)==bool(organizing or working)
    assert ('class="queue-state"' in status)==bool(working or queued)
    assert '0 批 · 0 等待' not in status
    if queued:assert '0 批 · 1 等待' in status
    if working:
        store.mark_failed(3,'collecting','douyin_source_unavailable')
        status=app.test_client().get('/').text.split('<span class="topbar-status"',1)[1].split('</header>',1)[0]
        assert '正在蒸馏' not in status
        assert 'queue-state' not in status


def test_only_known_unmodified_reading_style_can_upgrade(tmp_path,monkeypatch):
    old=b'old application-owned CSS'
    monkeypatch.setattr(reading_style,'content',lambda:old)
    path=reading_style.install(tmp_path)
    config=tmp_path/'.obsidian/appearance.json'
    preferences=json.loads(config.read_bytes())
    preferences['user-preference']='keep'
    config.write_text(json.dumps(preferences))
    before=config.read_bytes()
    monkeypatch.setattr(reading_style,'PREVIOUS_SHA256',hashlib.sha256(old).hexdigest())
    monkeypatch.setattr(reading_style,'content',lambda:b'updated CSS')
    assert reading_style.state(tmp_path)=='update_available'
    reading_style.install(tmp_path)
    assert path.read_bytes()==b'updated CSS'
    assert config.read_bytes()==before
    assert reading_style.state(tmp_path)=='enabled'
    path.write_bytes(old+b' user customization')
    with pytest.raises(ValueError,match='reading_style_conflict'):
        reading_style.install(tmp_path)
    assert path.read_bytes()==old+b' user customization'
    assert config.read_bytes()==before


@pytest.mark.parametrize('failed', [False, True])
def test_activity_after_organization_finishes_and_app_reopens(tmp_path, failed):
    _, store, _ = library(tmp_path)
    def fail(point, connection):
        if failed and point == 'after_topic_replace':
            raise sqlite3.IntegrityError('isolated organization failure')
    service, _ = organization(store, fail=fail)
    event = service.start_or_reuse()
    def status():
        # Rebuild the application so the result cannot depend on frontend state.
        page = create_app(store, object()).test_client().get('/').text
        return page.split('<span class="topbar-status"', 1)[1].split('</header>', 1)[0]
    assert '正在整理' in status()
    service.drive(event.event_id)
    assert '正常' in status() and 'is-active' not in status()
    item = store.create_item('https://www.douyin.com/video/999')
    assert '队列等待' in status()
    store.claim_next_item()
    assert '正在蒸馏' in status()
    store.mark_failed(item, 'collecting', 'douyin_source_unavailable')
    assert '正常' in status() and 'queue-state' not in status()


from .test_visual_browser import browser_page, visual_app


@pytest.fixture
def feedback_app(tmp_path):
    from werkzeug.serving import make_server
    _, store, _ = library(tmp_path)
    vault = tmp_path / 'vault'
    vault.mkdir(exist_ok=True)
    store.set_setting('vault_path', str(vault))
    server = make_server('127.0.0.1', 0, create_app(store, object()), threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{server.server_port}'
    server.shutdown()
    thread.join()


def test_home_status_geometry_and_fragment_only_refresh(visual_app,browser_page):
    url,insights=visual_app
    page=browser_page
    page.goto(url+'/')
    center=page.locator('.app-state').bounding_box()
    assert abs(center['y']+center['height']/2-20)<1
    assert page.locator('.queue-state').count()==0
    # A status-only refresh must not be discarded when the card HTML is unchanged.
    page.evaluate('''() => {
      const html = document.documentElement.outerHTML.replace('正常','正在整理');
      applyPage(html);
    }''')
    assert '正在整理' in page.locator('.topbar-status').inner_text()


def test_setting_action_edges_and_guide_assets(visual_app,browser_page):
    url,_=visual_app
    page=browser_page
    page.goto(url+'/settings?open=models')
    page.evaluate('document.querySelectorAll(".setting-group,.model-setting,.asr-setting").forEach(e=>e.open=true)')
    page.select_option('#asr-provider','doubao')
    page.evaluate('document.fonts.ready')
    rights=page.locator('.row-actions').evaluate_all('es=>es.filter(e=>e.getBoundingClientRect().height).map(e=>e.lastElementChild.getBoundingClientRect().right)')
    assert max(rights)-min(rights)<1
    assert abs(page.locator('#llm-save').bounding_box()['x']+page.locator('#llm-save').bounding_box()['width']-rights[0])<1
    guide=page.locator('.doubao-guide-link a').get_attribute('href')
    page.goto(url+guide)
    assert page.get_by_role('heading',name='先分清：语音识别、TOS 和大模型').count()==1
    page.wait_for_function('Array.from(document.images).every(i=>i.complete&&i.naturalWidth>0)')
    assert page.locator('img').count()==2
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')


@pytest.mark.parametrize('width', [1440, 640, 390])
def test_settings_long_path_and_compact_inline_actions(feedback_app, browser_page, width):
    url = feedback_app
    page = browser_page
    page.set_viewport_size({'width': width, 'height': 1024})
    page.goto(url + '/settings?open=paths')
    page.evaluate('''() => {
        const path = document.querySelector('.vault-path');
        path.textContent = '/Users/示例/很长的知识目录/'.repeat(14);
        path.title = path.textContent;
    }''')
    buttons = page.locator('#path-settings .setting-row .secondary-button')
    boxes = [buttons.nth(i).bounding_box() for i in range(buttons.count())]
    assert all(box is not None and box['height'] == 24 for box in boxes)
    assert max(b['x'] + b['width'] for b in boxes) - min(b['x'] + b['width'] for b in boxes) < 1
    assert all(len(t.strip()) == 5 for t in buttons.all_text_contents())
    assert page.locator('.vault-location-row button').all_text_contents() == ['更换文件夹']
    status = page.locator('.reading-style-configuration [role="status"]').bounding_box()
    action = page.locator('.reading-style-configuration button').bounding_box()
    assert abs(status['y'] + status['height']/2 - action['y'] - action['height']/2) < 1
    assert page.locator('.reading-style-configuration > summary').count() == 0
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.locator('.setting-group > summary').last.click()
    page.locator('.setting-group > summary').last.click()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')


def test_organize_button_role_and_geometry(feedback_app, browser_page):
    url = feedback_app
    page = browser_page
    page.goto(url + '/')
    organize = page.locator('.organization-section button')
    submit = page.locator('.intake-actions button')
    assert organize.evaluate('e=>getComputedStyle(e).backgroundColor') == 'rgb(20, 20, 19)'
    a, b = organize.bounding_box(), submit.bounding_box()
    assert abs(a['x'] + a['width'] - b['x'] - b['width']) < 1
    organize.hover()
    assert organize.bounding_box() == a
    page.mouse.down()
    page.wait_for_function("getComputedStyle(document.querySelector('.organization-section button')).backgroundColor === 'rgb(107, 106, 101)'")
    assert organize.bounding_box() == a
    page.mouse.move(0, 0)
    page.mouse.up()
    # Disabled feedback is a separate state, not the black enabled role.
    organize.evaluate('e=>e.disabled=true')
    page.wait_for_timeout(150)
    assert organize.is_disabled()
    assert organize.evaluate('e=>getComputedStyle(e).backgroundColor') != 'rgb(20, 20, 19)'


def test_offline_guide_is_in_application_resource_manifest():
    import runpy
    project = Path(__file__).resolve().parents[2]
    manifest = runpy.run_path(str(project / 'packaging/resources.py'))
    paths = {Path(source).relative_to(project / 'src').as_posix()
             for source, _ in manifest['application_datas'](project)}
    prefix = 'knowledge_distiller/v1/static/guides/'
    assert {prefix + name for name in (
        'doubao-asr.html', 'guide.css', 'assets/doubao-asr/create-bucket.png',
        'assets/doubao-asr/advanced-settings.png')} <= paths
