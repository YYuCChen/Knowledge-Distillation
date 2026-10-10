"""Production home markup/JS with isolated synthetic data and deferred replies."""
import io
import json
import wave

import pytest
from playwright.sync_api import sync_playwright, expect

from knowledge_distiller.v1.web import create_app
from .test_visual_browser import visual_app


@pytest.fixture
def interaction_page():
    # WebKit uses an isolated test context; never open the user's Chrome profile.
    with sync_playwright() as runtime:
        browser = runtime.webkit.launch()
        page = browser.new_page(viewport={'width': 1440, 'height': 1024})
        yield page
        browser.close()


def review(store, count=3):
    item = store.create_item('https://example.test/synthetic-confirmations')
    snapshot = '这是独立的合成素材。' * count
    pending = {'snapshot': snapshot, 'token': 'synthetic-review', 'review_required': False,
        'concerns': [{'start': n * 10, 'end': n * 10 + 1,
            'text': snapshot[n * 10:n * 10 + 1], 'audio_name': f'clip-{n}.wav',
            'candidates': []} for n in range(count)]}
    store.mark_waiting(item, pending)
    return item


def stub_audio(page):
    requests = []
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as audio:
        audio.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        audio.writeframes(b'\x01\x00' * 16000 * 10)
    def serve(route):
        requests.append(route.request.url)
        route.fulfill(content_type='audio/wav', body=buffer.getvalue())
    page.route('**/confirmation-audio?**', serve)
    return requests


def test_collapsed_cards_do_not_load_audio_and_refresh_preserves_playback(visual_app, interaction_page):
    url, insights = visual_app
    item = review(insights.store, 84)
    page = interaction_page
    requests = stub_audio(page)
    page.goto(url + f'/?item={item}')
    page.evaluate('clearTimeout(pollTimer)')
    assert page.locator('audio').count() == 84
    assert page.locator('audio[src]').count() == 0
    assert requests == []
    page.locator('[data-card-toggle]').first.click()
    page.wait_for_function("document.querySelector('audio').duration === 10")
    assert page.locator('audio[src]').count() == 1
    assert all('clip-0.wav' in request for request in requests)
    page.evaluate("window.originalAudio = document.querySelector('audio'); originalAudio.muted = true; originalAudio.play()")
    html = create_app(insights.store, object()).test_client().get('/').text
    page.evaluate('(html) => applyPage(html)', html)
    assert page.evaluate("document.querySelector('audio') === originalAudio && !originalAudio.paused")
    assert page.locator('audio[src]').count() == 1
    page.locator('[data-card-toggle]').first.click()
    page.locator('[data-card-toggle]').first.click()
    assert page.evaluate("document.querySelector('audio') === originalAudio")
    assert page.locator('audio[src]').count() == 1


@pytest.mark.parametrize('failed_first', [False, True])
def test_quick_submissions_queue_without_changing_copy_or_losing_drafts(visual_app, interaction_page, failed_first):
    url, insights = visual_app
    item = review(insights.store)
    page = interaction_page
    stub_audio(page)
    held = []
    page.route('**/items/*/confirm', lambda route: held.append(route))
    page.goto(url + f'/?item={item}')
    page.evaluate(r'''clearTimeout(pollTimer);
        window.errors = []; window.kdDialog = async message => errors.push(message);
        window.posts = []; const actualFetch = window.fetch;
        window.fetch = (url, options) => {
            if (options?.method === 'POST' && /\/confirm$/.test(url)) posts.push(Array.from(options.body.entries()));
            return actualFetch(url, options);
        };''')
    cards = page.locator('.todo-card-shell[data-sync-key^="member-"]')
    forms = cards.locator('.manual-confirmation')
    ids = forms.evaluate_all('(forms) => forms.map(form => form.id)')
    for n in range(3):
        cards.nth(n).locator('[data-card-toggle]').click()
        cards.nth(n).locator('input[name=value]').fill(['甲', '乙', '第三张草稿'][n])
    buttons = [page.locator(f'button[form="{name}"]') for name in ids]
    buttons[0].click()
    buttons[1].click()
    page.wait_for_function('posts.length === 1')
    assert len(held) == 1
    for button in buttons[:2]:
        expect(button).to_have_text('提交')
        expect(button).to_be_disabled()
    expect(page.locator(f'#{ids[2]} input[name=value]')).to_have_value('第三张草稿')
    if failed_first:
        held[0].fulfill(status=500, content_type='text/plain', body='合成保存失败')
    else:
        pending = json.loads(insights.store.item_bundle(item)['confirmation_json'])
        pending['concerns'] = pending['concerns'][1:]
        insights.store.mark_waiting(item, pending)
        html = create_app(insights.store, object()).test_client().get('/').text
        held[0].fulfill(content_type='text/html', body=html)
    page.wait_for_function('posts.length === 2')
    assert len(held) == 2
    assert page.evaluate("posts.map(fields => fields.find(([key]) => key === 'value')[1])") == ['甲', '乙']
    if failed_first:
        expect(buttons[0]).to_be_enabled()
        expect(page.locator(f'#{ids[0]} input[name=value]')).to_have_value('甲')
        assert page.evaluate('errors') == ['合成保存失败']
    pending = json.loads(insights.store.item_bundle(item)['confirmation_json'])
    pending['concerns'] = [c for c in pending['concerns'] if c['audio_name'] != 'clip-1.wav']
    insights.store.mark_waiting(item, pending)
    html = create_app(insights.store, object()).test_client().get('/').text
    held[1].fulfill(content_type='text/html', body=html)
    page.wait_for_function('!updating')
    expect(page.locator(f'#{ids[1]}')).to_have_count(0)
    expect(page.locator(f'#{ids[2]} input[name=value]')).to_have_value('第三张草稿')


def test_success_keeps_server_disabled_state_and_original_button_copy(visual_app, interaction_page):
    url, _ = visual_app
    page = interaction_page
    held = []
    page.route('**/synthetic-action', lambda route: held.append(route))
    page.goto(url)
    page.evaluate('''clearTimeout(pollTimer);
        document.querySelector('#home-results').innerHTML =
          '<form id="synthetic" action="/synthetic-action"><button type="submit">原有按钮</button></form>';
        window.reply = document.documentElement.outerHTML.replace(
          '<button type="submit">原有按钮', '<button type="submit" disabled>原有按钮');''')
    button = page.locator('#synthetic button')
    button.click()
    expect(button).to_have_text('原有按钮')
    expect(button).to_be_disabled()
    assert len(held) == 1
    held[0].fulfill(content_type='text/html', body=page.evaluate('reply'))
    page.wait_for_function('!updating')
    expect(button).to_have_text('原有按钮')
    expect(button).to_be_disabled()
