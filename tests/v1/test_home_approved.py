"""Approved home projections over private sources, real raw proof and worker."""
import json
import threading
from pathlib import Path
from types import SimpleNamespace

from bs4 import BeautifulSoup
import pytest
from knowledge_distiller.v1.collections import Collections
from knowledge_distiller.v1.database import connect
from knowledge_distiller.v1.douyin_collections import Scope, Member, connection_authority
from knowledge_distiller.v1.store import Store
from knowledge_distiller.v1.web import create_app, _home_context
from knowledge_distiller.v1.worker import SingleWorker
from .test_collections import Discovery, Boundary, accept
from .test_pipeline import distiller, ReviewConcern
from .test_captures import world as capture_world, send, capture_of, finish_link, run_material, raw_files


def build_home(root, *, all_saved=False, stopped=False):
    root.mkdir(exist_ok=True)
    store=Store(root/'synthetic.sqlite3');store.initialize();store.save_connection('douyin',None)
    scope=Scope('creator_collection','900','合成同题任务','creator',
        tuple(Member(str(101+i),f'合成素材 {i+1:02d}',True,0,f'v{i}') for i in range(14)),
        '2026-10-08T12:00:00+08:00',connection_authority(store))
    collections=Collections(store,Discovery(scope))
    operation,_=accept(collections)
    boundary=Boundary(store,root)
    members=collections.detail(operation)['members']
    for member in members[:14 if all_saved else 5]:
        store.mark_working(member['item_id'],'collecting')
        boundary.run(member['item_id'])
    with connect(store.path) as db:
        db.execute('UPDATE collection_operations SET state=? WHERE operation_id=?',
                   ('succeeded' if all_saved else 'cancelled' if stopped else 'working',operation))
    if not all_saved and not stopped:
        store.mark_working(members[5]['item_id'],'reviewing')
    wiki=SimpleNamespace(request_refresh=lambda **_:None,
        snapshot=lambda:{'state':'ready','raw_count':14 if all_saved else 5,'actions':['submit']})
    app=create_app(store,boundary,collection_service=collections,wiki_workflow=wiki)
    return SimpleNamespace(app=app,store=store,operation=operation,collections=collections,
        boundary=boundary,members=members)


def test_raw_cards_and_real_collection_counts(tmp_path):
    w=build_home(tmp_path)
    page=BeautifulSoup(w.app.test_client().get('/').text,'html.parser')
    assert [b.text for b in page.select('.process-counts b')]==['14','5','9']
    assert page.select_one('.processing-row span').text=='6/14'
    assert page.select_one('.queue-state').text=='1 条处理中 · 8 条等待'
    assert [v.text.strip() for v in page.select('.phase')]==['采集','识别','确认','保存']
    assert len(page.select('.knowledge-card'))==5
    assert not page.select('.knowledge-points,.collection-list')
    assert page.select_one('.organization-copy strong').text=='5 份素材待整理'
    assert all(v.text=='原始资料已保存，等待整理' for v in page.select('.knowledge-subtitle'))
    # A missing Vault does not invalidate durable writer history or make GET 500.
    w.store.set_setting('vault_path',str(tmp_path/'missing-vault'))
    assert w.app.test_client().get('/').status_code==200
    assert len(BeautifulSoup(w.app.test_client().get('/').text,'html.parser').select('.knowledge-card'))==5


def test_all_raw_saved_remain_visible_without_knowledge(tmp_path):
    w=build_home(tmp_path,all_saved=True)
    page=BeautifulSoup(w.app.test_client().get('/').text,'html.parser')
    assert len(page.select('.knowledge-card'))==14
    assert not page.select('.processing-panel,.queue-state,.knowledge-points')
    assert page.select_one('.organization-copy strong').text=='14 份素材待整理'


def test_visibility_routes_preserve_raw_and_never_wake(tmp_path):
    w=build_home(tmp_path,stopped=True)
    client=w.app.test_client()
    before=w.collections.detail(w.operation)
    files={str(p):p.read_bytes() for p in w.boundary.vault.rglob('*') if p.is_file()}
    page=client.get('/').text
    assert '隐藏此任务' in page and '9 条未继续处理' in page
    assert client.post(f'/collections/{w.operation}/visibility/hide').status_code==303
    hidden=BeautifulSoup(client.get('/').text,'html.parser')
    assert not hidden.select('.knowledge-card')
    assert hidden.select_one('[data-sync-key="hidden-tasks"]')
    viewed=BeautifulSoup(client.get(f'/?view_collection={w.operation}').text,'html.parser')
    assert len(viewed.select('.knowledge-card'))==5
    assert client.post(f'/collections/{w.operation}/visibility/restore').status_code==303
    assert len(BeautifulSoup(client.get('/').text,'html.parser').select('.knowledge-card'))==5
    assert w.collections.detail(w.operation)==before
    assert {str(p):p.read_bytes() for p in w.boundary.vault.rglob('*') if p.is_file()}==files


def test_explicit_annotation_correction_only_withdraws_old_projection(capture_world):
    w=capture_world
    send(w,'om_target',text='https://www.douyin.com/video/101',at=0)
    target=w.store.recent_items()[0]['item_id'];finish_link(w,target)
    w.store.mark_working(target,'publishing')
    w.store.complete_raw_item(target,w.vault)
    send(w,'om_note',text='合成补充观点',at=30)
    capture=capture_of(w,'om_note')
    w.captures.decide(capture['capture_id'],'third_party')
    item=capture_of(w,'om_note')['item_id'];run_material(w,item)
    app=create_app(w.store,SimpleNamespace())
    with app.test_request_context('/'):
        assert item in {v['id'] for v in _home_context(w.store,None)['recent']}
    old={str(p):p.read_bytes() for p in w.vault.rglob('*') if p.is_file()}
    w.captures.decide(capture['capture_id'],'annotation',target='om_target')
    with app.test_request_context('/'):
        context=_home_context(w.store,None)
        assert item not in {v['id'] for v in context['recent']}
        assert target in {v['id'] for v in context['recent']}
    assert all(Path(p).read_bytes()==data for p,data in old.items())
    assert w.captures.identity(capture['capture_id'])['target_message_id']=='om_target'
    assert [e['result'] for e in w.captures.events(capture['capture_id'])][-2:]==['third_party','annotation']


def test_exact_discovery_does_not_prepare_next_item(tmp_path):
    service,store,_,_,_=distiller(tmp_path,concerns=(ReviewConcern(0,2,'持续','首词',True,('继续',)),))
    first=store.create_item('https://v.douyin.com/a/')
    second=store.create_item('https://v.douyin.com/b/');service.run(second)
    # Make only second require a new proof through a real current mutation.
    pending=json.loads(store.item_bundle(second)['confirmation_json'])
    with connect(store.path) as db:
        pending['snapshot']+='合成补充'
        db.execute('UPDATE distill_items SET confirmation_json=? WHERE item_id=?',(json.dumps(pending),second))
    before=store.item_bundle(second)['confirmation_json']
    assert store.discover_pending_presentations(item_id=first,limit=1)['enqueued']==()
    assert store.item_bundle(second)['state']=='waiting_user'
    assert store.item_bundle(second)['confirmation_json']==before


def test_polling_never_reads_raw_files_and_nonraw_history_is_not_saved(tmp_path,monkeypatch):
    w=build_home(tmp_path)
    from knowledge_distiller.v1.raw import RawLedger
    monkeypatch.setattr(RawLedger,'read_item',lambda *a,**k: (_ for _ in ()).throw(AssertionError('home must use durable receipt')))
    item=w.store.create_item('https://v.douyin.com/legacy/')
    # Legacy terminal flag without a writer receipt must never appear as raw.
    w.store.mark_succeeded(item)
    response=w.app.test_client().get('/')
    assert response.status_code==200
    assert len(BeautifulSoup(response.text,'html.parser').select('.knowledge-card'))==5
    from .test_web import _complete_item
    historical=_complete_item(w.store,tmp_path)
    w.store.set_setting('vault_path',str(tmp_path/'missing-vault'))
    response=w.app.test_client().get('/')
    assert response.status_code==200
    page=BeautifulSoup(response.text,'html.parser')
    assert len(page.select('.knowledge-card'))==6
    assert '边界保护注意力' in response.text


@pytest.mark.parametrize('preparation_fails',[False,True])
def test_browser_pending_preparation_preserves_cards_and_draft(tmp_path,preparation_fails):
    from playwright.sync_api import sync_playwright, expect
    from werkzeug.serving import make_server
    service,store,source,_,_=distiller(tmp_path,concerns=(
        ReviewConcern(0,2,'持续','首词',True,('不断继续',)),
        ReviewConcern(2,4,'切换','次词',True,('转换',)),
        ReviewConcern(4,5,'会','第三词',True)))
    item=store.create_item('https://v.douyin.com/a/');service.run(item)
    entered=threading.Event();release=threading.Event()
    prepare=service.prepare_pending_presentation
    def held(item_id):
        entered.set()
        assert release.wait(10)
        if preparation_fails:
            from knowledge_distiller.v1.confirmation_preparation import PreparationError
            raise PreparationError('review_incomplete')
        return prepare(item_id)
    service.prepare_pending_presentation=held
    worker=SingleWorker(store,service)
    server=make_server('127.0.0.1',0,create_app(store,service,wake_worker=worker.wake),threaded=True)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();worker.start()
    try:
        with sync_playwright() as pw:
            browser=pw.chromium.launch();page=browser.new_page(viewport={'width':1280,'height':1000})
            page.goto(f'http://127.0.0.1:{server.server_port}/?item={item}')
            cards=page.locator('[data-confirmation-card]');expect(cards).to_have_count(3)
            cards.nth(2).locator('[data-card-toggle]').click()
            cards.nth(2).locator('input[name=value]').fill('保留的合成草稿')
            page.evaluate('window.originalDraft=document.querySelectorAll("[data-confirmation-card]")[2].querySelector("input[name=value]");window.originalIntake=document.querySelector(".intake")')
            cards.nth(1).locator('[data-card-toggle]').click()
            cards.nth(1).locator('button[value="转换"]').click()
            assert entered.wait(3)
            expect(page.locator('[data-preparing-confirmation]')).to_have_count(2)
            expect(cards).to_have_count(2)
            expect(cards.nth(1).locator('input[name=value]')).to_have_value('保留的合成草稿')
            expect(cards.nth(1).get_by_role('button',name='提交',exact=True)).to_be_disabled()
            assert page.locator('[data-stale-confirmation]').count()==0
            assert '已在另一端处理' not in page.locator('#home-results').inner_text()
            assert page.evaluate('originalDraft===document.querySelectorAll("[data-confirmation-card]")[1].querySelector("input[name=value]")')
            page.screenshot(path=str(tmp_path/'confirmation-preparing.png'),full_page=True)
            release.set()
            if preparation_fails:
                expect(page.locator('.failure-card')).to_have_count(1,timeout=6000)
                expect(cards).to_have_count(2)
                expect(cards.nth(1).get_by_role('button',name='提交',exact=True)).to_be_disabled()
                assert page.locator('[data-stale-confirmation]').count()==0
                assert '已在另一端处理' not in page.locator('#home-results').inner_text()
                browser.close()
                return
            expect(page.locator('[data-preparing-confirmation]')).to_have_count(0,timeout=6000)
            expect(cards.nth(1).get_by_role('button',name='提交',exact=True)).to_be_enabled()
            expect(cards.nth(1).locator('[data-card-toggle]')).to_have_attribute('aria-expanded','true')
            assert page.evaluate('originalIntake===document.querySelector(".intake")')
            assert source.calls==1
            page.screenshot(path=str(tmp_path/'confirmation-ready.png'),full_page=True)
            browser.close()
    finally:
        release.set();worker.stop(3);server.shutdown();thread.join(3)
