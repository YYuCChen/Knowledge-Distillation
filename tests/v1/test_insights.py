import sqlite3

import pytest

from knowledge_distiller.organization_models import EventStatus
from knowledge_distiller.v1.database import connect
from knowledge_distiller.v1.insights import InsightLibrary, InsightError
from tests.test_organization_service import _productive_plan
from .test_organization import organization
from .test_topics import library


def prepared(tmp_path):
    _, store, _ = library(tmp_path)
    plan = _productive_plan()
    plan['candidate_versions'][0]['payload']['scan_tags'] = ['来源核对', '证据边界', '认知增量']
    service, _ = organization(store, growth=plan)
    result = service.drive(service.start_or_reuse().event_id)
    assert result.event.status is EventStatus.SUCCEEDED
    insights = InsightLibrary(store)
    pending = insights.list('pending')
    assert len(pending) == 1
    assert len(pending[0]['sources']) == 2
    return insights, pending[0]['id']


def test_judgment_and_append_only_ideas(tmp_path):
    insights, version = prepared(tmp_path)
    insights.judge(version, 'interesting', '我的判断')
    assert not insights.list('pending')
    assert insights.list('interesting')[0]['annotation'] == '我的判断'
    entry = insights.add_idea(version, 'idea-1', '  原样保留的想法\n')
    assert insights.add_idea(version, 'idea-1', '  原样保留的想法\n') == entry
    with pytest.raises(InsightError):
        insights.add_idea(version, 'idea-1', '另一个想法')
    with connect(insights.store.path) as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE personal_cognition_entries SET text='覆盖' WHERE entry_id=?", (entry,))
    assert len(insights.read(version)['notes']) == 1


def test_rethink_reconsideration_preserves_initial_judgment(tmp_path):
    insights, version = prepared(tmp_path)
    insights.judge(version, 'rethink', '暂时保留意见')
    assert len(insights.list('rethink')) == 1
    with pytest.raises(InsightError):
        insights.add_idea(version, 'not-accepted', '不能提前写入')
    receipt = insights.reconsider(version, 'reconsider-1', '新的认识')
    assert insights.reconsider(version, 'reconsider-1', '新的认识') == receipt
    assert not insights.list('rethink')
    accepted = insights.list('interesting')[0]
    assert accepted['annotation'] == '暂时保留意见'
    assert accepted['notes'][0]['entry_kind'] == 'new_view'
    replay = insights.judge(version, 'rethink', '暂时保留意见')
    assert replay.judgment.accepted_initial_role is None
    insights.add_idea(version, 'idea-after', '继续想法')
    with connect(insights.store.path) as db:
        assert db.execute('SELECT decision FROM user_insight_judgments').fetchone()[0] == 'rethink'
        assert db.execute('SELECT COUNT(*) FROM accepted_insight_versions').fetchone()[0] == 1
    with pytest.raises(InsightError):
        insights.reconsider(version, 'reconsider-1', '改写认识')


def test_page_mutations_and_current_only_search(tmp_path):
    from knowledge_distiller.v1.web import create_app
    insights, version = prepared(tmp_path)
    client = create_app(insights.store, object()).test_client()
    pending = client.get('/insights')
    assert pending.status_code == 200
    assert '来源核对' in pending.text
    assert client.post(f'/insights/{version}/judge', data={'decision':'interesting','text':'保留批注'}, headers={'X-Requested-With':'insight'}).status_code == 204
    assert client.post(f'/insights/{version}/idea', data={'operation_id':'web-idea','text':'新想法'}, headers={'X-Requested-With':'insight'}).status_code == 200
    assert '新想法' in client.get('/insights?state=interesting').text
    assert '保留批注' not in client.get('/insights').text


def successor(insights, version, tmp_path):
    from .test_submitted_sources import Model, ForbiddenAudio
    from knowledge_distiller.v1.file_sources import prepare_direct_text
    from knowledge_distiller.v1.pipeline import Distiller
    from knowledge_distiller.v1.worker import SingleWorker
    from tests.fixtures.growth import empty_growth_plan_payload, insight_payload, source_participant
    audio = ForbiddenAudio()
    engine = Distiller(store=insights.store, source=audio, normalizer=audio, recognizer=audio,
        reviewer=audio, confirmation_clipper=audio, knowledge_model=Model(), runtime_root=tmp_path/'runtime', vault=tmp_path/'vault')
    worker = SingleWorker(insights.store, engine)
    for text in ('新正文丙。', '新正文丁。'):
        insights.store.submit_source(prepare_direct_text(text))
        worker.run_one()
    old = insights.read(version)
    plan = empty_growth_plan_payload()
    plan['new_input_reviews'] = [dict(knowledge_result_id=i,outcome='participated',reason_text='贡献新依据') for i in (3,4)]
    payload = insight_payload(claim='More precise successor claim')
    payload['scan_tags'] = ['来源核对','证据边界','认知增量']
    plan['candidate_versions'] = [dict(new_insight_key='evolved', target_kind='evolve_identity',
        insight_id=old['insight_id'], previous_insight_version_id=version, payload=payload,
        participants=[source_participant('a',3,position=0),source_participant('b',4,position=1)], used_relations=[])]
    plan['rejected_outputs'] = []
    service,_ = organization(insights.store,growth=plan)
    result=service.drive(service.start_or_reuse().event_id)
    assert result.event.status is EventStatus.SUCCEEDED
    return insights.list('pending')[0]['id']


def test_newer_reconsideration_retires_old_without_resurrection(tmp_path):
    insights, old = prepared(tmp_path)
    insights.judge(old, 'interesting')
    new = successor(insights, old, tmp_path)
    insights.judge(new, 'rethink')
    insights.reconsider(new, 'new-accepted')
    assert insights.read(old)['state'] == 'historical'
    assert [x['id'] for x in insights.list('interesting')] == [new]
    with pytest.raises(InsightError):
        insights.add_idea(old, 'old-note', '不再是当前版本')


def test_late_reconsideration_can_be_born_historical(tmp_path):
    insights, old = prepared(tmp_path)
    insights.judge(old, 'rethink')
    new = successor(insights, old, tmp_path)
    insights.judge(new, 'interesting')
    insights.reconsider(old, 'late', '认识仍保存为历史')
    assert insights.read(old)['state'] == 'historical'
    assert [x['id'] for x in insights.list('interesting')] == [new]


def test_reconsideration_failure_rolls_back_note_and_grant(tmp_path):
    insights, version = prepared(tmp_path)
    insights.judge(version, 'rethink')
    with connect(insights.store.path) as db:
        db.execute("CREATE TRIGGER reject_grant BEFORE INSERT ON accepted_insight_versions BEGIN SELECT RAISE(ABORT,'fixture failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        insights.reconsider(version, 'atomic', '同次看法')
    with connect(insights.store.path) as db:
        assert db.execute('SELECT COUNT(*) FROM insight_reconsiderations').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM personal_cognition_entries').fetchone()[0] == 0
    assert insights.read(version)['state'] == 'rethink'


def test_search_excludes_pending_rethink_and_personal_notes(tmp_path):
    from knowledge_distiller.v1.web import create_app
    insights, version = prepared(tmp_path)
    client = create_app(insights.store, object()).test_client()
    term = insights.read(version)['payload'].claim.split()[0]
    assert 'search-insight' not in client.get('/topics',query_string={'q':term}).text
    insights.judge(version,'rethink')
    assert 'search-insight' not in client.get('/topics',query_string={'q':term}).text
    insights.reconsider(version,'search-grant')
    assert 'search-insight' in client.get('/topics',query_string={'q':term}).text
    insights.add_idea(version,'private-idea','只在个人笔记出现的独有词')
    assert 'search-insight' not in client.get('/topics',query_string={'q':'只在个人笔记出现的独有词'}).text


@pytest.mark.parametrize('tags', [None, ['两字','来源核对','证据边界'], ['来源核对']*3, [{},'来源核对','证据边界']])
def test_malformed_scan_tags_fail_closed(tags):
    from knowledge_distiller.organization_models import parse_insight_payload, OrganizationCodecError
    from tests.fixtures.growth import insight_payload
    payload = insight_payload()
    payload['scan_tags'] = tags
    with pytest.raises(OrganizationCodecError):
        parse_insight_payload(payload)


def test_browser_judgment_reconsideration_idea_and_draft_restore(tmp_path):
    import threading
    from pathlib import Path
    from playwright.sync_api import sync_playwright, expect
    from werkzeug.serving import make_server
    from knowledge_distiller.v1.web import create_app
    insights, version = prepared(tmp_path)
    server = make_server('127.0.0.1',0,create_app(insights.store,object()),threaded=True)
    thread = threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    try:
        with sync_playwright() as playwright:
            if not Path(playwright.chromium.executable_path).exists():
                pytest.skip('Browser regression requires playwright install chromium')
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={'width':1440,'height':1024})
            page.goto(f'http://127.0.0.1:{server.server_port}/insights')
            assert page.locator('.insight-scroll').bounding_box()['y'] == 196
            page.locator('summary').click()
            layout = page.evaluate('''() => {
                const note = document.querySelector('.insight-note');
                const label = note.querySelector('label').getBoundingClientRect();
                const input = note.querySelector('textarea').getBoundingClientRect();
                const discussion = document.querySelector('.insight-discussion');
                const right = discussion.getBoundingClientRect().right - parseFloat(getComputedStyle(discussion).paddingRight);
                return {sameLine: Math.abs(label.top-input.top)<1, afterColon: Math.abs(input.left-label.right)<1,
                        aligned: Math.abs(input.right-right)<1, bottom: innerHeight-document.querySelector('.insight-scroll').getBoundingClientRect().bottom};
            }''')
            assert layout == dict(sameLine=True, afterColon=True, aligned=True, bottom=48)
            page.get_by_role('textbox',name='批注',exact=True).fill('初次判断原文')
            page.get_by_role('button',name='再想想',exact=True).click()
            expect(page.locator('.insight-card')).to_have_count(0)
            expect(page.locator('[data-empty]')).to_contain_text('已判断的内容')
            page.get_by_role('link',name='再想想',exact=True).click()
            page.locator('summary').click()
            page.get_by_role('textbox',name='新看法',exact=True).fill('认识的变化')
            page.get_by_role('button',name='改观',exact=True).click()
            expect(page.locator('.insight-card')).to_have_count(0)
            page.get_by_role('link',name='有意思',exact=True).click()
            page.locator('summary').click()
            idea = page.get_by_role('textbox',name='新想法',exact=True)
            idea.fill('离开后应恢复的草稿')
            page.get_by_role('link',name='设置',exact=True).click()
            page.get_by_role('link',name='返回',exact=True).click()
            expect(idea).to_have_value('离开后应恢复的草稿')
            page.get_by_role('button',name='记录',exact=True).click()
            expect(idea).to_have_value('')
            expect(page.locator('.personal-history p')).to_have_count(3)
            expect(page.locator('.personal-history time')).to_have_count(3)
            assert page.get_by_role('button',name='记录',exact=True).bounding_box()['width'] == 60
            assert len(insights.read(version)['notes']) == 2
            idea.fill('刷新后丢弃')
            page.reload()
            expect(idea).to_have_value('')
            # Native scrollbar gutters reserve space but must not shift the reading column.
            assert page.evaluate('document.documentElement.scrollWidth <= document.documentElement.clientWidth')
            assert page.locator('.insight-page').bounding_box()['x'] == 400
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=2)
