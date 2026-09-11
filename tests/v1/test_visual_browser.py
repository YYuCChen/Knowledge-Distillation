"""Browser regressions for Figma geometry and the existing confirmation flows."""
import threading
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server

from knowledge_distiller.faithful_review import ReviewConcern
from knowledge_distiller.v1.web import create_app
from knowledge_distiller.v1.worker import SingleWorker
from .test_insights import prepared
from .test_pipeline import distiller


@pytest.fixture
def browser_page():
    with sync_playwright() as p:
        if not Path(p.chromium.executable_path).exists():
            pytest.skip('Browser regression requires playwright install chromium')
        browser = p.chromium.launch()
        page = browser.new_page(viewport={'width': 1440, 'height': 1024})
        yield page
        browser.close()


@pytest.fixture
def visual_app(tmp_path):
    insights, _ = prepared(tmp_path)
    server = make_server('127.0.0.1', 0, create_app(insights.store, object()), threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f'http://127.0.0.1:{server.server_port}', insights
    server.shutdown()


def test_settings_column_does_not_move_when_forms_make_page_scroll(visual_app, browser_page):
    url, _ = visual_app
    page = browser_page
    page.goto(url + '/settings')
    page.evaluate('document.fonts.ready')
    assert page.locator('.page-width').first.bounding_box()['x'] == 400
    assert page.locator('.group-heading').first.bounding_box()['height'] == 56
    page.locator('.setting-group > summary').nth(0).click()
    page.locator('.setting-group > summary').nth(1).click()
    page.locator('.model-setting > summary').click()
    page.locator('#llm-provider').select_option('openai')
    page.locator('.asr-setting > summary').click()
    page.locator('#asr-provider').select_option('doubao')
    assert page.evaluate('document.documentElement.scrollHeight > innerHeight')
    assert page.locator('.page-width').first.bounding_box()['x'] == 400
    base = page.locator('.model-base').bounding_box()
    model = page.locator('#llm-custom-row').bounding_box()
    assert base['y'] == model['y'] and base['width'] == model['width']
    expect(page.locator('#llm-key-form .model-secret')).to_be_visible()
    assert page.locator('#llm-key-form .model-secret').bounding_box()['width'] > base['width']
    expect(page.locator('.doubao-onboarding [data-setup-step="1"]')).to_be_visible()
    expect(page.locator('#asr-save')).to_be_hidden()
    page.locator('.doubao-onboarding [data-setup-next="2"]').click()
    expect(page.locator('.doubao-onboarding [data-setup-step="2"]')).to_be_visible()
    expect(page.locator('.doubao-onboarding [data-setup-step="1"]')).to_be_hidden()
    assert page.locator('.page-width').first.bounding_box()['x'] == 400
    assert page.evaluate('document.documentElement.scrollWidth <= document.documentElement.clientWidth')


def test_buttons_press_without_moving_and_invalid_file_dialog_is_keyboard_safe(visual_app, browser_page, tmp_path):
    url, _ = visual_app
    page = browser_page
    page.goto(url + '/insights')
    page.locator('.insight-card summary').click()
    for selector, pressed in [('.insight-primary', 'rgb(107, 106, 101)'), ('button[value="rethink"]', 'rgb(240, 238, 231)')]:
        button = page.locator(selector)
        box = button.bounding_box()
        button.hover()
        assert button.bounding_box() == box
        page.mouse.down()
        page.wait_for_function('(s) => getComputedStyle(document.querySelector(s[0])).backgroundColor === s[1]', arg=[selector, pressed])
        assert button.bounding_box() == box
        page.mouse.move(1200, 900)
        page.mouse.up()
    page.goto(url + '/')
    file_input = page.locator('[data-source-file]')
    file_input.set_input_files({'name': 'unsupported.txt', 'mimeType': 'text/plain', 'buffer': b'visual'})
    dialog = page.get_by_role('dialog')
    expect(dialog).to_be_visible()
    expect(dialog).to_contain_text('PDF、EPUB 或 Markdown')
    expect(dialog.get_by_role('button', name='知道了')).to_be_focused()
    page.keyboard.press('Escape')
    expect(dialog).to_have_count(0)
    expect(file_input).to_have_value('')


def test_rerecognize_cancel_preserves_waiting_source(tmp_path, browser_page):
    service, store, _, _, _ = distiller(tmp_path, concerns=(ReviewConcern(0, 2, '持续', '首词需要确认', True, ('继续',)),))
    app = create_app(store, service)
    app.test_client().post('/submissions', data={'content': 'https://www.douyin.com/video/123'})
    SingleWorker(store, service).run_one()
    before = store.item_bundle(1)
    assert before['state'] == 'waiting_user'
    server = make_server('127.0.0.1', 0, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        page = browser_page
        page.goto(f'http://127.0.0.1:{server.server_port}/?item=1')
        arrow = page.locator('[data-card-toggle]')
        arrow.hover()
        box = arrow.bounding_box()
        index = page.locator('.confirmation-card .todo-index')
        icon = arrow.locator('img').bounding_box()
        # Compare the designed text line box centre, independent of font glyph ink.
        number_center = index.bounding_box()['y'] + 16 + 20 / 2
        assert abs(number_center - (icon['y'] + icon['height'] / 2)) < 0.5
        assert box['width'] == 64
        assert box['height'] == page.locator('.confirmation-card').bounding_box()['height'] - 2
        assert arrow.evaluate("e => getComputedStyle(e).backgroundColor") == 'rgba(0, 0, 0, 0)'
        assert arrow.evaluate("e => getComputedStyle(e).boxShadow") == 'none'
        page.mouse.down()
        assert arrow.evaluate("e => getComputedStyle(e).backgroundColor") == 'rgba(0, 0, 0, 0)'
        page.mouse.move(0, 0)
        page.mouse.up()
        arrow.focus()
        assert arrow.evaluate("e => getComputedStyle(e).outlineStyle") == 'none'

        arrow.click(position={'x': box['width'] / 2, 'y': box['height'] - 3})
        expect(arrow).to_have_attribute('aria-expanded', 'true')
        expanded = arrow.bounding_box()
        assert expanded['height'] == page.locator('.confirmation-card').bounding_box()['height'] - 2
        arrow.click(position={'x': expanded['width'] / 2, 'y': expanded['height'] - 3})
        expect(arrow).to_have_attribute('aria-expanded', 'false')
        arrow.focus()
        page.keyboard.press('Enter')
        page.locator('[data-rerecognize] button').click()
        dialog = page.get_by_role('dialog')
        expect(dialog.get_by_role('button', name='取消')).to_be_focused()
        page.keyboard.press('Escape')
        expect(dialog).to_have_count(0)
        expect(page.locator('[data-rerecognize] button')).to_be_focused()
        # Cleanup must not depend on delivery of the queued native close event.
        page.evaluate("document.addEventListener('close', e => e.stopImmediatePropagation(), true)")
        page.locator('[data-rerecognize] button').click()
        dialog.get_by_role('button', name='取消').click()
        expect(dialog).to_have_count(0)
        expect(page.locator('[data-rerecognize] button')).to_be_focused()
        expect(page.locator('#confirmation-action-feedback')).to_have_count(0)
        assert store.item_bundle(1) == before
    finally:
        server.shutdown()


def test_reviewed_focus_layout_and_navigation_details(visual_app, browser_page):
    url, insights = visual_app
    page = browser_page
    page.goto(url + '/')
    topic_link = page.locator('.main-nav a').first
    topic_link.hover()
    expect(topic_link).to_have_css('text-decoration-line', 'underline')
    page.locator('.intake textarea').focus()
    expect(page.locator('.intake textarea')).to_have_css('outline-style', 'none')
    button = page.locator('.intake button[type=submit]')
    button.hover()
    expect(button).to_have_css('box-shadow', 'rgb(150, 169, 192) 0px 0px 0px 1px inset')
    button.click()
    error = page.locator('.intake .form-error')
    expect(error).to_be_visible()
    assert error.bounding_box()['y'] < button.bounding_box()['y'] + button.bounding_box()['height']
    assert error.bounding_box()['x'] + error.bounding_box()['width'] < button.bounding_box()['x']
    page.goto(url + '/topics')
    page.locator('.topic-search input').focus()
    expect(page.locator('.topic-search input')).to_have_css('outline-style', 'none')
    page.locator('.topic-card').first.click()
    title = page.locator('.topic-point summary strong').first
    before = title.bounding_box()
    page.locator('.topic-point summary').first.click()
    assert title.bounding_box() == before
    assert page.locator('.quote-label img').first.get_attribute('src').endswith('.svg')
    page.goto(url + '/settings')
    groups = page.locator('.setting-group')
    first, second = groups.nth(0).bounding_box(), groups.nth(1).bounding_box()
    assert first['y'] + first['height'] == second['y']
    groups.nth(1).locator(':scope > summary').click()
    page.locator('.model-setting > summary').click()
    connect, save = page.locator('.codex-connect'), page.locator('#llm-save')
    assert connect.evaluate('(e) => e.form.id') == 'codex-connect'
    assert save.evaluate('(e) => e.form.id') == 'llm-form'
    assert connect.bounding_box()['y'] == save.bounding_box()['y']
    assert connect.bounding_box()['x'] < save.bounding_box()['x']
    page.locator('.asr-setting > summary').click()
    expect(page.locator('#asr-save')).to_be_hidden()
    expect(page.locator('#qwen-install')).to_be_visible()
    page.locator('#asr-provider').select_option('doubao')
    page.locator('.doubao-onboarding [data-setup-next="2"]').click()
    actions=page.locator('.doubao-onboarding [data-setup-step="2"] .feishu-form-actions')
    link,button=actions.locator('a').bounding_box(),actions.locator('button').bounding_box()
    assert abs((link['y']+link['height']/2)-(button['y']+button['height']/2))<1
    assert link['x']<button['x']
    page.goto(url + '/insights')
    filters = page.locator('.insight-filters')
    assert filters.bounding_box()['height'] == 24
    expect(filters).to_have_css('padding', '2px')
    expect(filters).to_have_css('border-radius', '4px')
    page.locator('.insight-card summary').click()
    summary = page.locator('.insight-card summary')
    tag_bottom = page.locator('.insight-tags').bounding_box()['y'] + page.locator('.insight-tags').bounding_box()['height']
    discussion_top = page.locator('.insight-discussion').bounding_box()['y']
    assert discussion_top - tag_bottom == 16
    assert summary.evaluate('(e) => getComputedStyle(e,"::after").content') == 'none'
    note = page.locator('.insight-note')
    assert note.evaluate('(e) => getComputedStyle(e,"::before").width') == '134px'
    page.locator('.insight-note textarea').focus()
    expect(page.locator('.insight-note textarea')).to_have_css('outline-style', 'none')
    page.goto(url + '/insights?state=rethink')
    expect(page.locator('[data-empty]')).to_have_text('暂时没有需要再想一想的新知。')

    insights.judge(insights.list('pending')[0]['id'], 'interesting', '先前批注')
    page.goto(url + '/insights?state=interesting')
    page.locator('.insight-card summary').click()
    expect(page.locator('.personal-history h2')).to_have_css('width', '134px')
    expect(page.locator('.personal-history h2')).to_have_css('border-bottom-width', '1px')


def test_concern_context_fits_rendered_width_and_keeps_full_highlight(browser_page):
    page = browser_page
    page.set_content('''<style>.source-fragment {width:440px;font:500 10px serif;letter-spacing:.5px} mark {font-weight:600}</style>
        <p class="source-fragment" data-context-before="" data-context-after=""><span data-context-leading></span><mark>when</mark><span data-context-trailing></span></p>''')
    page.locator('.source-fragment').evaluate('''e => {
        e.dataset.contextBefore = 'Some people think about these things every day. '.repeat(8);
        e.dataset.contextAfter = ', in fact, the situation is very different from what they imagine. '.repeat(8);
    }''')
    script = Path(__file__).parents[2] / 'src/knowledge_distiller/v1/static/home.js'
    page.add_script_tag(path=str(script))
    wide = page.locator('.source-fragment').inner_text()
    assert len(wide) > 50  # Previous fixed 12+12-character context fails this.
    page.locator('.source-fragment').evaluate("e => e.style.width = '240px'")
    page.wait_for_function("document.querySelector('.source-fragment').textContent.length < " + str(len(wide)))
    assert page.locator('mark').inner_text() == 'when'
    narrow = page.locator('.source-fragment').inner_text()
    page.locator('.source-fragment').evaluate("e => e.style.width = '440px'")
    page.wait_for_function("document.querySelector('.source-fragment').textContent.length > " + str(len(narrow)))
    assert page.locator('.source-fragment').inner_text() == wide


def test_english_choices_wrap_and_manual_entry_is_optional(visual_app, browser_page, tmp_path):
    url, insights = visual_app
    item = insights.store.create_item('https://www.douyin.com/video/123')
    original = 'It has a lot of it.'
    alternative = 'It has a lot of potential to improve the situation for everyone involved.'
    unbroken = 'https://example.com/' + 'verylongunbrokenword' * 24
    insights.store.mark_waiting(item, {'snapshot': original, 'concerns': [{
        'start': 0, 'end': len(original), 'text': original, 'reason': '末尾词不清楚',
        'candidates': [original, alternative, unbroken], 'candidate_explanations': {
            original: '保留原表达：它有很多这类东西。', alternative: '它很有潜力改善所有相关人的处境。', unbroken: '连续长词或网址也不能撑破卡片。'},
    }]})
    page = browser_page
    page.route('**/confirmation-audio?**', lambda route: route.fulfill(status=404))
    page.goto(url + f'/?item={item}')
    choices = page.locator('.candidate-actions button')
    expect(choices).to_have_count(3)
    expect(choices.nth(1)).not_to_be_visible()
    page.locator("[data-card-toggle]").click()
    expect(choices.nth(1)).to_be_visible()
    expect(page.locator('.candidate-row').nth(1)).to_contain_text(alternative)
    expect(page.locator('.candidate-row').nth(1)).to_contain_text('它很有潜力')
    expect(page.locator('.concern-reason')).to_have_count(0)
    expect(page.locator('.manual-entry input[name=value]')).to_be_visible()
    expect(page.locator('[data-transcript-text], .transcript-review')).to_have_count(0)
    for width in (1440, 600, 360):
        page.set_viewport_size({'width': width, 'height': 1000})
        for choice in choices.all():
            assert choice.evaluate('(e) => e.scrollWidth <= e.clientWidth && e.scrollHeight <= e.clientHeight')
        assert page.locator('.confirmation-card').evaluate('(e) => e.scrollWidth <= e.clientWidth')
        assert page.locator('.candidate-actions').evaluate('(e) => e.scrollWidth <= e.clientWidth')
        expect(choices.nth(1)).to_have_css('white-space', 'normal')
        expect(choices.nth(1)).to_contain_text(alternative)
        explanation = page.locator('.candidate-explanation').nth(1)
        assert explanation.bounding_box()['y'] >= choices.nth(1).bounding_box()['y'] + choices.nth(1).bounding_box()['height']
        count = page.locator('.candidate-count').bounding_box()
        arrow = page.locator('.card-chevron img').bounding_box()
        assert abs(count['y'] + count['height'] / 2 - arrow['y'] - arrow['height'] / 2) < 0.5
        expect(choices.nth(1)).to_have_text(alternative)
    expect(page.locator('.manual-entry input[name=value]')).to_be_visible()
    page.screenshot(path=str(tmp_path / 'english-choices.png'), full_page=True)


def test_chinese_choices_stay_compact_and_wrap_without_english_help(visual_app, browser_page):
    url, insights = visual_app
    item = insights.store.create_item('https://www.douyin.com/video/123')
    original = '内壁脏、接缝线泥等纹'
    insights.store.mark_waiting(item, {'snapshot': '前面谈到' + original + '，这是中文材料。', 'concerns': [{
        'start': 4, 'end': 4 + len(original), 'text': original, 'reason': '请确认原音中的这个词',
        'candidates': [original, '内壁章、接缝线泥等纹'],
        'candidate_explanations': {original: '按原字面；额外解释', '内壁章、接缝线泥等纹': '另一释义'},
    }]})
    page = browser_page
    page.goto(url + f'/?item={item}')
    expect(page.locator('.compact-candidates button')).to_have_count(2)
    expect(page.locator('.candidate-copy, .candidate-basis, .suggest-candidates')).to_have_count(0)
    page.locator('[data-card-toggle]').click()
    for width in (1440, 600, 360):
        page.set_viewport_size({'width': width, 'height': 1024})
        assert page.locator('.confirmation-card').evaluate('(e) => e.scrollWidth <= e.clientWidth')
        for button in page.locator('.compact-candidates button').all():
            assert button.evaluate('(e) => e.scrollWidth <= e.clientWidth && e.scrollHeight <= e.clientHeight')
    expect(page.locator('.manual-confirmation input[name=value]')).to_be_visible()


@pytest.mark.parametrize('width', [1440, 946, 390])
def test_vault_row_keeps_only_original_change_location_action(visual_app, browser_page, tmp_path, width):
    url, insights = visual_app
    vault = tmp_path / ('很长的知识仓库名称' * 8)
    vault.mkdir()
    insights.store.set_setting('vault_path', str(vault))
    page = browser_page
    page.set_viewport_size({'width': width, 'height': 1024})
    page.goto(url + '/settings')
    page.locator('.setting-group > summary').nth(2).click()
    page.evaluate('document.fonts.ready')
    row = page.locator('.vault-row').filter(has_text='Obsidian库位置')
    change = row.get_by_role('button', name='更换文件夹')
    expect(row.get_by_role('link')).to_have_count(0)
    expect(row.get_by_role('button')).to_have_count(1)
    b, r = change.bounding_box(), row.bounding_box()
    assert b['x'] >= r['x'] and b['x'] + b['width'] <= r['x'] + r['width']
    if width > 720:
        assert r['height'] == 41
        assert b['x'] > row.locator('.vault-path').bounding_box()['x']
    change.scroll_into_view_if_needed()
    before = change.bounding_box()
    change.hover()
    assert change.bounding_box() == before
    assert page.evaluate('document.documentElement.scrollWidth <= document.documentElement.clientWidth')
    page.screenshot(path=str(tmp_path / f'paths-{width}.png'))


def test_reading_style_action_survives_busy_button_disabling(visual_app,browser_page,tmp_path,monkeypatch):
    from knowledge_distiller.v1 import reading_style
    url,insights=visual_app
    vault=tmp_path/'style-vault';vault.mkdir()
    insights.store.set_setting('vault_path',str(vault))
    monkeypatch.setattr(reading_style,'content',lambda:b'.kd-reading{}')
    reading_style.install(vault)
    page=browser_page
    page.goto(url+'/settings?open=paths')
    panel=page.locator('.reading-style-configuration')
    panel.get_by_role('button',name='关闭此排版',exact=True).click()
    expect(panel.get_by_role('button',name='启用此排版',exact=True)).to_be_visible()
    assert reading_style.state(vault)=='disabled'
    panel.get_by_role('button',name='启用此排版',exact=True).click()
    expect(panel.get_by_role('button',name='关闭此排版',exact=True)).to_be_visible()
    assert reading_style.state(vault)=='enabled'
    expect(panel.locator('p')).to_have_count(0)
    assert abs(panel.locator('[role=status]').bounding_box()['y']-panel.locator('strong').bounding_box()['y']) < 2


def test_path_actions_have_five_characters_and_matching_edges(visual_app,browser_page):
    url,_=visual_app
    page=browser_page
    page.goto(url+'/settings?open=paths')
    buttons=page.locator('#path-settings button:visible, #path-settings .row-actions > .secondary-button:visible')
    boxes=[]
    for button in buttons.all():
        assert len(button.inner_text().strip())==5
        box=button.bounding_box();boxes.append(box)
        assert 60 <= box['width'] <= 72
        assert box['height']==24
    assert max(box['width'] for box in boxes)-min(box['width'] for box in boxes)<1
    row_actions=page.locator('#path-settings .setting-row > .row-actions:visible')
    edges=[round(node.bounding_box()['x']+node.bounding_box()['width']) for node in row_actions.all()]
    assert len(set(edges))==1
    expect(page.locator('.vault-configuration')).to_have_count(0)


def test_update_copy_shows_version_release_date_without_platform(tmp_path, browser_page):
    from knowledge_distiller.v1.store import Store
    app = create_app(Store(tmp_path/'db.sqlite3'), object())
    updates = app.extensions['updates']
    updates.info.update(version='2026.09.11.5', display_version='1.11')
    updates.phase = 'latest'
    server = make_server('127.0.0.1', 0, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        page = browser_page
        page.goto(f'http://127.0.0.1:{server.server_port}/settings?open=updates')
        panel = page.locator('#version-updates')
        expect(panel.locator('[data-update-status]')).to_have_text('已是最新版本')
        expect(panel.locator('.update-version-info')).to_have_text('当前版本：V1.11')
        expect(panel.locator('.update-release-date')).to_have_text('发行时间：2026-09-11')
        assert 'macOS' not in panel.inner_text() and 'arm64' not in panel.inner_text()
    finally:
        server.shutdown()
