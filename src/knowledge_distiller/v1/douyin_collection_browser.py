"""Read collection APIs inside the selected, authorized browser profile.

The installed downloader's HTTP client works for individual videos but its
signed collection requests return 403. These are the same native endpoints,
executed in Chrome's own session instead of copying its cookies into HTTP.
"""
import json
import logging
from urllib.parse import urlencode

from .chrome import ChromePage, ChromeSessionError
from .douyin_collections import CollectionError, _pages


class _BrowserRequestDenied(CollectionError):
    """The raw request was refused; a normal same-item page may still load."""


class BrowserCollectionClient:
    def __init__(self, session):
        chrome = getattr(session, 'chrome', session)
        self.chrome = chrome
        self.port_file = getattr(chrome, '_active_port_file', None)
        self.page = None
        self._mix_first_pages = {}
        self._mix_kinds = {}
        self._mix_metadata = {}
        self._native_mix_pages = {}
        self._observer_installed = False
        self._detail_reads = 0

    async def __aenter__(self):
        self.page = (self.chrome.browser_page('https://www.douyin.com/')
                     if hasattr(self.chrome, 'browser_page')
                     else ChromePage('https://www.douyin.com/', self.port_file))
        try:
            self._ready()
            account = await self._fetch('/aweme/v1/web/user/profile/self/', {})
            if not isinstance(account.get('user'), dict) or not account['user'].get('sec_uid'):
                raise ChromeSessionError('douyin_login_required')
            return self
        except Exception:
            self.page.close()
            raise

    async def __aexit__(self, *_):
        self.page.close()

    def _ready(self):
        if not self.page.wait_for("location.hostname==='www.douyin.com' && document.readyState==='complete'"):
            raise CollectionError('collection_browser_unavailable')

    async def _fetch(self, path, params):
        url = 'https://www.douyin.com' + path + '?' + urlencode({'device_platform': 'webapp', 'aid': 6383, **params})
        response = self.page.call('Runtime.evaluate', {
            # The site's own collection requests send UIFID in a header as
            # well as the cookie. Keep it inside the authorized page session.
            'expression': '(async()=>{const uifid=document.cookie.split("; ").find(x=>x.startsWith("UIFID="))?.slice(6);'
                'const headers=uifid&&' + json.dumps('/series/' in path) + '?{uifid}:{};const r=await fetch(' + json.dumps(url) +
                ',{credentials:"include",headers});return {status:r.status,body:await r.text()}})()',
            'returnByValue': True, 'awaitPromise': True,
        }, page=True)
        value = response.get('result', {}).get('value')
        data = None
        try:
            if response.get('exceptionDetails') or not isinstance(value, dict):
                raise CollectionError('collection_upstream_failed')
            if value.get('status') == 401:
                raise ChromeSessionError('douyin_login_required')
            if value.get('status') == 403:
                raise _BrowserRequestDenied('collection_upstream_failed')
            if value.get('status') != 200:
                raise CollectionError('collection_upstream_failed')
            try:
                data = json.loads(value['body'])
            except (KeyError, TypeError, ValueError) as error:
                raise CollectionError('collection_upstream_failed') from error
            if not isinstance(data, dict) or data.get('status_code') != 0:
                raise CollectionError('collection_upstream_failed')
            return data
        finally:
            if path in {'/aweme/v1/web/user/profile/self/', '/aweme/v1/web/aweme/detail/'}:
                _log_read('raw', 'detail' if 'aweme/detail' in path else 'self', self._detail_reads,
                          value, data, exception=bool(response.get('exceptionDetails')))

    async def resolve_short_url(self, url):
        self.page.call('Page.navigate', {'url': 'about:blank'}, page=True)
        if not self.page.wait_for("location.href==='about:blank'"):
            raise CollectionError('collection_browser_unavailable')
        self.page.call('Page.navigate', {'url': url}, page=True)
        self._ready()
        # Collection pages replace browser history with the current episode.
        # The document navigation still identifies the originally shared scope.
        return self.page.evaluate('''(() => {
            const entry = performance.getEntriesByType('navigation')[0]?.name;
            if (entry) {
                const u = new URL(entry);
                if (u.hostname === 'www.douyin.com' && /^\/collection\/\d+\/?$/.test(u.pathname))
                    return entry;
            }
            return location.href;
        })()''')

    async def get_video_detail(self, key):
        self._detail_reads += 1
        try:
            detail = (await self._fetch('/aweme/v1/web/aweme/detail/', {'aweme_id': key})).get('aweme_detail')
        except _BrowserRequestDenied:
            # Some works reject the minimal home-page fetch while the normal
            # video page's own signed request succeeds. Observe that request;
            # do not guess signatures, copy credentials or accept another work.
            if not isinstance(key, str) or not key.isascii() or not key.isdigit():
                raise CollectionError('collection_identity_mismatch')
            self._native_start('https://www.douyin.com/video/' + key)
            event = self.page.wait_for('window.__kdCollections?.find(x=>x.path==="/aweme/v1/web/aweme/detail/"'
                '&&x.key===' + json.dumps(key) + ')')
            _log_read('native', 'detail', self._detail_reads, event,
                      event.get('data') if isinstance(event, dict) else None, key=key)
            if not isinstance(event, dict) or event.get('status') != 200:
                raise CollectionError('collection_upstream_failed')
            data = event.get('data')
            if not isinstance(data, dict) or data.get('status_code') != 0:
                raise CollectionError('collection_upstream_failed')
            detail = data.get('aweme_detail')
            if not isinstance(detail, dict) or str(detail.get('aweme_id')) != key:
                raise CollectionError('collection_identity_mismatch')
        if (isinstance(detail, dict) and detail.get('images')
                and '版本过低，升级后可展示全部信息' in str(detail.get('desc', ''))):
            if not isinstance(key, str) or not key.isascii() or not key.isdigit():
                raise CollectionError('douyin_gallery_incomplete')
            self.page.call('Page.navigate', {'url': 'about:blank'}, page=True)
            if not self.page.wait_for("location.href==='about:blank'"):
                raise CollectionError('collection_browser_unavailable')
            self.page.call('Page.navigate', {'url': 'https://www.douyin.com/note/' + key}, page=True)
            self._ready()
            expression = _GALLERY_SSR.replace('ITEM_ID', json.dumps(key))
            native = self.page.wait_for(expression)
            return _complete_gallery(detail, native)
        return detail

    async def get_user_info(self, key):
        return (await self._fetch('/aweme/v1/web/user/profile/other/', {'sec_user_id': key})).get('user')

    async def get_user_mix(self, key, cursor):
        if cursor != 0:
            raise CollectionError('collection_membership_incomplete')
        self._native_start('https://www.douyin.com/user/' + key)
        tab = "[...document.querySelectorAll('span')].find(x=>x.textContent==='合集')"
        if not self.page.wait_for('Boolean(' + tab + ')'):
            raise CollectionError('collection_browser_unavailable')
        self.page.evaluate('(' + tab + ').click()')
        mixes = []
        seen = {}
        # The web profile combines two independently paginated lists. A single
        # completed mix/list response omits collections migrated to series.
        for kind, field in [('series', 'series_infos'), ('mix', 'mix_infos')]:
            path = '/aweme/v1/web/' + kind + '/list/'
            async def fetch(next_cursor):
                return {'raw': self._native_page(path, key, next_cursor, profile=True)}
            rows = await _pages(fetch, keys=[field])
            for row in rows:
                identity = str(row.get(kind + '_id') or '')
                if not identity.isdigit():
                    raise CollectionError('collection_membership_incomplete')
                count = (row.get('stats' if kind == 'series' else 'statis') or {}).get('updated_to_episode')
                if count is not None and (type(count) is not int or count < 0):
                    raise CollectionError('collection_membership_incomplete')
                snapshot = (kind, row.get(kind + '_name'), count)
                if identity in seen:
                    if seen[identity] != snapshot:
                        raise CollectionError('collection_scope_changed')
                    continue
                seen[identity] = snapshot
                self._mix_kinds[identity] = kind
                if kind == 'mix':
                    self._mix_metadata[identity] = row
                mixes.append({'mix_id': identity, 'mix_name': row.get(kind + '_name'), 'member_count': count})
        return {'raw': {'status_code': 0, 'has_more': 0, 'cursor': 0, 'mix_infos': mixes}}

    async def get_user_post(self, key, cursor):
        return {'raw': await self._fetch('/aweme/v1/web/aweme/post/', {'sec_user_id': key, 'max_cursor': cursor, 'count': 18})}

    async def get_mix_detail(self, key):
        # The current web collection panel uses series/aweme, whose members
        # still carry the original mix_info. Legacy mix/detail and mix/aweme
        # are rejected even in the logged-in browser session.
        if self._mix_kinds.get(key) == 'series':
            data = await self._series_page(key, 0)
        else:
            self._native_start('https://www.douyin.com/collection/' + key)
            event = self.page.wait_for('window.__kdCollections?.find(x=>x.key===' + json.dumps(key) +
                '&&["/aweme/v1/web/mix/aweme/","/aweme/v1/web/series/aweme/"].includes(x.path))')
            if not isinstance(event, dict):
                raise CollectionError('collection_membership_incomplete')
            kind = 'series' if '/series/' in event['path'] else 'mix'
            self._mix_kinds[key] = kind
            # A series panel may start at the last watched episode; enumerate
            # from zero, never mistake that visible suffix for the collection.
            data = (await self._series_page(key, 0) if kind == 'series'
                    else self._native_page('/aweme/v1/web/mix/aweme/', key, 0))
        rows = data.get('aweme_list')
        if rows == [] and data.get('has_more') == 0:
            raise CollectionError('collection_empty')
        if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
            raise CollectionError('collection_membership_incomplete')
        detail = rows[0].get('mix_info')
        if not isinstance(detail, dict) or str(detail.get('mix_id')) != key:
            raise CollectionError('collection_identity_mismatch')
        if self._mix_kinds[key] == 'mix':
            # Legacy member payloads omit the collection author. Resolve the
            # exact entry on the member author's native collection list rather
            # than assuming member author == collection owner.
            self._native_mix_pages[(key, 0)] = data
            if key not in self._mix_metadata:
                async def fetch(cursor):
                    page = self._native_page('/aweme/v1/web/mix/aweme/', key, cursor)
                    self._native_mix_pages[(key, cursor)] = page
                    return {'raw': page}
                await _pages(fetch, keys=['aweme_list'])
                author = (rows[0].get('author') or {}).get('sec_uid')
                if not isinstance(author, str) or not author:
                    raise CollectionError('collection_identity_mismatch')
                await self.get_user_mix(author, 0)
            metadata = self._mix_metadata.get(key)
            if not metadata:
                raise CollectionError('collection_identity_mismatch')
            detail = metadata
        self._mix_first_pages[key] = data
        return detail

    async def get_mix_aweme(self, key, cursor):
        if cursor == 0 and key in self._mix_first_pages:
            return {'raw': self._mix_first_pages.pop(key)}
        if self._mix_kinds.get(key) == 'mix':
            if (key, cursor) in self._native_mix_pages:
                return {'raw': self._native_mix_pages[(key, cursor)]}
            return {'raw': self._native_page('/aweme/v1/web/mix/aweme/', key, cursor)}
        return {'raw': await self._series_page(key, cursor)}

    async def _series_page(self, key, cursor):
        return await self._fetch('/aweme/v1/web/series/aweme/', {
            'series_id': key, 'pull_type': 2, 'cursor': cursor,
            'source': 'playlet_homepage_hot', 'count': 6,
        })

    def _native_start(self, url):
        if not self._observer_installed:
            self.page.call('Page.enable', {}, page=True)
            self.page.call('Page.addScriptToEvaluateOnNewDocument', {'source': _COLLECTION_OBSERVER}, page=True)
            self._observer_installed = True
        self.page.call('Page.navigate', {'url': 'about:blank'}, page=True)
        if not self.page.wait_for("location.href==='about:blank'"):
            raise CollectionError('collection_browser_unavailable')
        self.page.call('Page.navigate', {'url': url}, page=True)
        self._ready()

    def _native_page(self, path, key, cursor, *, profile=False):
        expression = 'window.__kdCollections?.find(x=>x.path===' + json.dumps(path) + \
            '&&x.key===' + json.dumps(key) + '&&x.cursor===' + json.dumps(cursor) + ')'
        if cursor:
            # Trigger the real site's pagination so its own client signs the
            # legacy cross-origin request. Do not manufacture/replay signatures.
            if profile:
                self.page.evaluate('''(() => {
                    for (const x of [document.scrollingElement, ...document.querySelectorAll('div')]) {
                        if (x && x.scrollHeight > x.clientHeight && x.clientHeight > 0)
                            x.scrollTop = x.scrollHeight;
                    }
                })()''')
            else:
                self.page.evaluate('''(() => {
                    let x = document.querySelector('[data-e2e="cover-age-title-container"]');
                    while (x && ![...x.querySelectorAll('button')].some(b=>b.textContent==='点击加载更多')) x=x.parentElement;
                    const button=x && [...x.querySelectorAll('button')].find(b=>b.textContent==='点击加载更多');
                    if (button) button.click();
                })()''')
        event = self.page.wait_for(expression)
        if not isinstance(event, dict) or event.get('status') != 200:
            raise CollectionError('collection_membership_incomplete')
        data = event.get('data')
        if not isinstance(data, dict) or data.get('status_code') != 0:
            raise CollectionError('collection_upstream_failed')
        return data


def _log_read(stage, kind, number, response, data, *, exception=False, key=None):
    """Status-only evidence; never log URLs, source text, identifiers or cookies."""
    response = response if isinstance(response, dict) else {}
    data = data if isinstance(data, dict) else {}
    status, code = response.get('status'), data.get('status_code')
    diagnostic = {'stage': stage, 'kind': kind, 'read_number': number,
                  'http_status': status if type(status) is int else None,
                  'status_code': code if type(code) is int else None,
                  'exception': exception}
    if kind == 'detail':
        detail = data.get('aweme_detail')
        diagnostic['detail_present'] = isinstance(detail, dict)
        if key is not None:
            diagnostic['identity_matches'] = isinstance(detail, dict) and str(detail.get('aweme_id')) == key
    logging.getLogger(__name__).info('douyin_browser_read %s', json.dumps(diagnostic))


# Observe only collection and exact-work responses in this client's temporary tab. The site's
# requests/credentials are unchanged; stored keys omit cookies, headers and URL
# signatures. Wrapping before page scripts preserves the site's request stack.
_COLLECTION_OBSERVER = r'''(() => {
    window.__kdCollections = [];
    const paths = new Set(['/aweme/v1/web/mix/list/', '/aweme/v1/web/series/list/',
        '/aweme/v1/web/mix/aweme/', '/aweme/v1/web/series/aweme/', '/aweme/v1/web/aweme/detail/']);
    function relevant(url) {
        try {
            const u = new URL(url, location.href);
            return ['www.douyin.com','www-hj.douyin.com'].includes(u.hostname) && paths.has(u.pathname);
        } catch (_) { return false; }
    }
    function record(url, status, text) {
        try {
            const u = new URL(url, location.href);
            if (!relevant(url)) return;
            const q = u.searchParams;
            window.__kdCollections.push({path:u.pathname,
                key:q.get('mix_id')||q.get('series_id')||q.get('sec_user_id')||q.get('aweme_id'),
                cursor:Number(q.get('cursor')||0), status, data:JSON.parse(text)});
        } catch (_) { /* Non-JSON cannot establish a complete collection. */ }
    }
    const open = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function(method, url, ...args) {
        if (!this.__kdCollectionObserved) {
            this.addEventListener('load', () => {
                if (relevant(this.responseURL)) record(this.responseURL, this.status,
                    this.responseType === 'json' ? JSON.stringify(this.response) : this.responseText);
            });
            this.__kdCollectionObserved = true;
        }
        return open.call(this, method, url, ...args);
    };
    const nativeFetch = window.fetch;
    window.fetch = function(...args) {
        return nativeFetch.apply(this,args).then(response => {
            if (relevant(response.url))
                response.clone().text().then(text => record(response.url,response.status,text)).catch(()=>{});
            return response;
        });
    };
})()'''


# The public note page's own React server record carries full native desc while
# the older web detail endpoint may return an explicit upgrade/truncation stub.
# Read only the exact main-aweme prop, never recommended items or DOM prose.
_GALLERY_SSR = r'''(() => {
    const found = [], chunks = [];
    for (const script of document.scripts) {
        const text = script.textContent;
        const prefix = 'self.__pace_f.push(';
        if (!text.startsWith(prefix)) continue;
        let chunk;
        try { chunk = JSON.parse(text.slice(prefix.length, -1))[1]; }
        catch (_) { continue; }
        if (typeof chunk === 'string') chunks.push(chunk);
    }
    const stream = chunks.join('');
    function resolveText(value) {
        if (typeof value !== 'string') return null;
        if (value.startsWith('$$')) return value.slice(1);
        if (!/^\$[0-9a-f]+$/.test(value)) return value;
        const id = value.slice(1);
        const header = new RegExp('(?:^|\\n)' + id + ':T([0-9a-f]+),', 'g');
        const matches = [...stream.matchAll(header)];
        if (matches.length !== 1) return null;
        const match = matches[0], length = parseInt(match[1], 16);
        const bytes = new TextEncoder().encode(stream.slice(match.index + match[0].length));
        if (bytes.length < length) return null;
        try { return new TextDecoder('utf-8', {fatal: true}).decode(bytes.slice(0, length)); }
        catch (_) { return null; }
    }
    for (const chunk of chunks) {
        const match = /^[0-9a-f]+:(\[.*)\n?$/s.exec(chunk);
        if (!match) continue;
        let record;
        try { record = JSON.parse(match[1]); }
        catch (_) { continue; }
        const props = Array.isArray(record) && record[3];
        if (!props || props.awemeId !== ITEM_ID || props.aweme?.statusCode !== 0) continue;
        const d = props.aweme.detail;
        if (!d || d.awemeId !== ITEM_ID) continue;
        const desc = resolveText(d.desc);
        if (desc === null) continue;
        found.push({aweme_id: d.awemeId, aweme_type: d.awemeType, desc,
            images: Array.isArray(d.images) ? d.images.map(i => ({
                uri: i.uri, url_list: i.urlList, video: i.video,
                width: i.width, height: i.height
            })) : null});
    }
    return found.length === 1 ? found[0] : null;
})()'''


def _complete_gallery(detail, native):
    from .douyin_text import DouyinTextError, parse_douyin_text

    if not isinstance(native, dict) or native.get('aweme_id') != detail.get('aweme_id'):
        raise CollectionError('douyin_gallery_incomplete')
    try:
        parsed = parse_douyin_text(native)
        original = detail.get('images')
        if not parsed or parsed.kind != 'gallery' or not isinstance(original, list):
            raise CollectionError('douyin_gallery_incomplete')
        original_ids = [image.get('uri') for image in original if isinstance(image, dict)]
        if len(original_ids) != len(original) or original_ids != [image.identity for image in parsed.images]:
            raise CollectionError('source_snapshot_changed')
    except DouyinTextError as error:
        raise CollectionError(error.code) from error
    # Retain author/time and existing native provenance, replacing exactly the
    # complete text + same ordered image set from the verified main-aweme record.
    return {**detail, 'desc': native['desc'], 'images': native['images'],
            'native_text_origin': 'douyin_note_main_aweme_ssr'}
