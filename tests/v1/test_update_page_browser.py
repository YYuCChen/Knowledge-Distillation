"""Component snapshots must render the existing update controls completely."""
import json
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader
from playwright.sync_api import sync_playwright, expect

from knowledge_distiller.v1 import web


@pytest.mark.parametrize('refresh_available', [False, True])
def test_component_release_reaches_download_and_install_without_full_asset(refresh_available):
    state = dict(token='synthetic-token', phase='idle', configured=True, attention=False,
                 display_version='2.0', release_date='2026-09-30', release=None,
                 full_update_required=False, manual_update_only=False, can_install=True)
    release = dict(version='2026.10.10.1', display_version='2.0.1', notes='',
                   selected=dict(size=5869650), full_reason='复用已校验组件')
    if refresh_available:
        state.update(phase='available', release=release)
    env = Environment(loader=FileSystemLoader(Path(web.__file__).parent / 'templates'))
    html = env.get_template('update_settings.html').render(update=state, open_group='updates',
        url_for=lambda *a, **k: '/static/' + k['filename'])
    script = (Path(web.__file__).parent / 'static/updates.js').read_text()
    requests, errors = [], []
    with sync_playwright() as runtime:
        browser = runtime.webkit.launch()
        page = browser.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        def respond(route):
            name = route.request.url.rsplit('/', 1)[-1]
            if route.request.method == 'POST':
                requests.append(name)
                assert name in ('check', 'download', 'install')
                state.update(phase={'check':'available', 'download':'downloaded', 'install':'installing'}[name], release=release)
            route.fulfill(content_type='application/json', body=json.dumps(state))
        page.route('**/settings/updates/**', respond)
        page.route('http://synthetic.test/', lambda route: route.fulfill(content_type='text/html; charset=utf-8', body='<meta charset="utf-8">' + html + '<script id="update-state" type="application/json">' + json.dumps(state) + '</script><script>' + script + '</script>'))
        page.goto('http://synthetic.test/')
        button = page.locator('[data-update-primary]')
        if not refresh_available:
            button.click()
        expect(button).to_have_text('下载更新')
        expect(button).to_be_enabled()
        expect(page.locator('[data-update-package-size]')).to_have_text('更新包大小 5.6MB')
        expect(page.locator('[data-update-full-fallback]')).to_be_hidden()
        button.click()
        expect(button).to_have_text('安装并重启')
        expect(button).to_be_enabled()
        button.click()
        expect(button).to_have_text('安装中…')
        expect(button).to_be_disabled()
        assert not errors
        assert requests == (['download','install'] if refresh_available else ['check','download','install'])
        browser.close()
