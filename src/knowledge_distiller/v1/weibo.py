from __future__ import annotations

import json
import re
import shutil
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit
from uuid import NAMESPACE_URL, uuid5

from .chrome import ChromeSessionError
from .opencli_session import read_opencli
from .xiaohongshu import CapturedNote, verify_media, XiaohongshuSourceError


class WeiboSourceError(RuntimeError):
    pass


def weibo_identity(value):
    try:
        url = urlsplit(value.strip())
        if url.scheme not in {'http', 'https'} or url.username or url.password or url.port:
            raise ValueError
        if url.hostname not in {'weibo.com', 'www.weibo.com', 'm.weibo.cn'}:
            raise ValueError
        if url.hostname != 'm.weibo.cn' and url.path in {'/ttarticle/p/show', '/ttarticle/x/m/show'}:
            query = parse_qs(url.query if url.path == '/ttarticle/p/show' else url.fragment.lstrip('/'))
            ids = query.get('id', [])
            if len(ids) != 1 or not re.fullmatch(r'[0-9]+', ids[0]):
                raise ValueError
            return 'article:' + ids[0], None
        match = re.fullmatch(r'/(detail|status|[0-9]+)/([A-Za-z0-9]+)/?', url.path)
        if not match or (match[1] == 'detail' and not match[2].isdigit()):
            raise ValueError
        if url.hostname == 'm.weibo.cn' and match[1] not in {'detail', 'status'}:
            raise ValueError
        return match[2], match[1] if match[1].isdigit() else None
    except ValueError as error:
        raise ValueError('请提交一条微博或头条文章的完整链接。') from error


def connection_authority(row):
    if row is None or row['state']=='unconfigured' or not row['browser_context']:
        raise ChromeSessionError('weibo_not_configured')
    if row['state']!='connected':raise ChromeSessionError('weibo_login_required')
    context=row['browser_context']
    instance=uuid5(NAMESPACE_URL,f"weibo:{row['connected_at']}:{row['generation']}:{context}").hex
    return {'class':'EXISTING_BROWSER_OWNED','connection_instance_id':instance,
            'session_authority_ref':f'browser:{instance}','generation':row['generation'],
            'connected_at':row['connected_at'],'browser_context':context}


class WeiboSession:
    def read(self,url,context=None):return read_opencli('weibo','weibo',url,context)
    def verify(self):return self.read('https://weibo.com/')['contextId']


def qualify_weibo(state, key, expected_uid=None):
    if state.get('loggedIn') is not True:
        raise ChromeSessionError('weibo_login_required')
    if state.get('requestedId') != key:
        raise WeiboSourceError('weibo_identity_mismatch')
    if key.startswith('article:'):
        article = _qualify_article(state.get('article'), key[8:])
        return {'_article': article}, key, article['title'] + '\n\n' + article['text'], article['pageUrl']
    try:
        data = json.loads(state['body'])
    except (ValueError, TypeError, KeyError) as error:
        raise WeiboSourceError('weibo_snapshot_unknown') from error
    if not isinstance(data, dict):
        raise WeiboSourceError('weibo_snapshot_unknown')
    status_id = str(data.get('idstr') or data.get('id') or '')
    if data.get('id') is not None and str(data['id']) != status_id:
        raise WeiboSourceError('weibo_identity_mismatch')
    bid = data.get('mblogid')
    if not isinstance(bid, str) or not status_id.isdigit() or key not in {status_id, bid}:
        raise WeiboSourceError('weibo_identity_mismatch')
    user = data.get('user') or {}
    if not isinstance(user, dict):
        raise WeiboSourceError('weibo_snapshot_unknown')
    uid = str(user.get('idstr') or user.get('id') or '')
    if expected_uid is not None and expected_uid != uid:
        raise WeiboSourceError('weibo_identity_mismatch')
    is_long = data.get('isLongText') or data.get('is_long_text') or data.get('truncated')
    if data.get('pic_num') or data.get('pic_ids') or data.get('pics') or data.get('mix_media_info'):
        raise WeiboSourceError('weibo_media_unsupported')
    # Page attachments can carry native video or an article even with pic_num=0.
    page = data.get('page_info')
    if (page and (not isinstance(page, dict) or page.get('object_type') != 'article')) or data.get('media_info'):
        raise WeiboSourceError('weibo_media_unsupported')
    if (not is_long and data.get('isLongText') is not False) or data.get('pic_num') != 0:
        raise WeiboSourceError('weibo_snapshot_unknown')
    text = data.get('text_raw')
    if is_long:
        expanded = state.get('longText') or {}
        if not isinstance(expanded, dict) or expanded.get('requestedId') != status_id:
            raise WeiboSourceError('weibo_identity_mismatch')
        try:
            payload = json.loads(expanded['body'])
            detail = payload['data']
            if expanded.get('status') != 200 or payload.get('ok') != 1 or not isinstance(detail, dict):
                raise ValueError
            text = detail.get('longTextContent_raw')
            if text is None:
                text = _plain_long_text(detail.get('longTextContent'))
        except (KeyError, TypeError, ValueError) as error:
            raise WeiboSourceError('weibo_text_incomplete') from error
    if not isinstance(text, str) or not text.strip():
        raise WeiboSourceError('weibo_text_incomplete')
    if data.get('retweeted_status') and text.strip() in {'转发微博', '轉發微博', '转发', '轉發', '//', 'Repost'}:
        raise WeiboSourceError('weibo_nested_only')
    if page:
        article_id = page.get('page_id')
        if not isinstance(article_id, str) or page.get('object_id') != '1022:' + article_id:
            raise WeiboSourceError('weibo_identity_mismatch')
        article = _qualify_article(state.get('article'), article_id)
        data['_article'] = article
        text += '\n\n' + article['title'] + '\n\n' + article['text']
    data['_is_long'] = bool(is_long)
    return data, status_id, text, f'https://weibo.com/detail/{status_id}'


def _plain_long_text(value):
    from html.parser import HTMLParser
    if not isinstance(value, str):
        raise ValueError('missing long text')
    class Text(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.parts = []
        def handle_data(self, data): self.parts.append(data)
        def handle_starttag(self, tag, attrs):
            if tag == 'br': self.parts.append('\n')
        def handle_endtag(self, tag):
            if tag in {'p', 'div'}: self.parts.append('\n')
    parser = Text()
    parser.feed(value)
    parser.close()
    return ''.join(parser.parts).strip()


def _qualify_article(article, article_id):
    if not isinstance(article, dict) or article.get('requestedId') != article_id:
        raise WeiboSourceError('weibo_identity_mismatch')
    try:
        identity, _ = weibo_identity(article['pageUrl'])
    except (KeyError, TypeError, ValueError) as error:
        raise WeiboSourceError('weibo_identity_mismatch') from error
    if identity != 'article:' + article_id:
        raise WeiboSourceError('weibo_identity_mismatch')
    if article.get('isMask') != '0' or article.get('isPay') != '0':
        raise WeiboSourceError('weibo_article_restricted')
    if article.get('unsupportedMedia') is not False:
        raise WeiboSourceError('weibo_media_unsupported')
    if any(not isinstance(article.get(k), str) or not article[k].strip() for k in ('title', 'text', 'html')):
        raise WeiboSourceError('weibo_article_incomplete')
    if not isinstance(article.get('images'), list):
        raise WeiboSourceError('weibo_article_incomplete')
    for number, image in enumerate(article['images'], 1):
        if (not isinstance(image, dict) or image.get('id') != f'image-{number}'
                or f'〔图片 {number}〕' not in article['text']):
            raise WeiboSourceError('weibo_article_incomplete')
        _image_url(image.get('url'))
    return article


def _image_url(value):
    if not isinstance(value, str):
        raise WeiboSourceError('weibo_media_incomplete')
    try:
        url = urlsplit(value)
        if (url.scheme not in {'http', 'https'} or url.username or url.password or url.port
                or not (url.hostname or '').endswith('.sinaimg.cn')):
            raise ValueError
    except ValueError as error:
        raise WeiboSourceError('weibo_media_incomplete') from error


def download_image(url, destination):
    import httpx
    try:
        for _ in range(5):
            _image_url(url)
            with httpx.stream('GET', url, headers={'Referer': 'https://weibo.com/'}, timeout=60) as response:
                if response.is_redirect:
                    from urllib.parse import urljoin
                    url = urljoin(url, response.headers['location'])
                    continue
                response.raise_for_status()
                with destination.open('xb') as output:
                    for chunk in response.iter_bytes():
                        output.write(chunk)
                return
        raise WeiboSourceError('weibo_media_incomplete')
    except (httpx.HTTPError, OSError, KeyError) as error:
        raise WeiboSourceError('weibo_media_incomplete') from error


class WeiboSource:
    def __init__(self, store, session=None, downloader=download_image, verifier=verify_media):
        self.store = store
        self.session = session or WeiboSession()
        self.downloader = downloader
        self.verifier = verifier

    def capture(self, submitted_url, work_dir, *, expected_authority):
        key, uid = weibo_identity(submitted_url)
        authority = connection_authority(self.store.connection('weibo'))
        if authority != expected_authority:
            raise ChromeSessionError('weibo_connection_changed')
        try:
            state = self.session.read(submitted_url, authority['browser_context'])
            if state.get('contextId') != authority['browser_context']:
                raise ChromeSessionError('weibo_connection_changed')
            data, status_id, text, canonical = qualify_weibo(state, key, uid)
        except ChromeSessionError as error:
            if str(error) == 'weibo_login_required':
                self.store.require_relogin('weibo')
            raise
        article = data.get('_article')
        members = []
        root = work_dir / 'weibo-media'
        if article:
            if root.exists():
                shutil.rmtree(root)
            root.mkdir(parents=True)
        try:
            for image in article['images'] if article else ():
                path = root / image['id']
                self.downloader(image['url'], path)
                members.append(self.verifier(path, image['id'], 'image'))
            if connection_authority(self.store.connection('weibo')) != authority:
                raise ChromeSessionError('weibo_connection_changed')
        except Exception as error:
            if article:
                shutil.rmtree(root)
            if isinstance(error, XiaohongshuSourceError):
                raise WeiboSourceError('weibo_media_invalid') from error
            raise
        metadata = {
            'note_kind': 'normal', 'item_kind': 'article' if article else 'long_text' if data.get('_is_long') else 'ordinary_text',
            'platform_item_id': status_id,
            'source_title': article['title'] if article else '', 'original_description': text,
            'media_members': [m.manifest() for m in members],
            'scope': 'PRIMARY_PAYLOAD_ONLY/NESTED_EXCLUDED',
            'captured_at': datetime.now(UTC).isoformat(), 'session_authority': authority,
            'author': {'display_name': (data.get('user') or {}).get('screen_name') or (article or {}).get('authorName')},
            'published_at': data.get('created_at') or (article or {}).get('publishedAt'),
        }
        if article:
            metadata.update(native_kind='article', article_id=article['requestedId'],
                article_url=article['pageUrl'], native_html=article['html'],
                original_images=article['images'])
        return CapturedNote(status_id, submitted_url, canonical, metadata, tuple(members), source_kind='weibo')

    def reuse_retained(self,*,source_key,submitted_url,canonical_url,metadata,work_dir,expected_authority):
        authority=connection_authority(self.store.connection('weibo'))
        if authority!=expected_authority:raise ChromeSessionError('weibo_connection_changed')
        if metadata.get('session_authority')!=authority:return None
        try:
            age=(datetime.now(UTC)-datetime.fromisoformat(metadata['captured_at'])).total_seconds()
            if not 0<=age<=72*3600:return None
        except (KeyError,TypeError,ValueError):return None
        members = []
        for entry in metadata.get('media_members', []):
            member_id = entry['member_id']
            if not re.fullmatch(r'image-[1-9][0-9]*', member_id):
                raise WeiboSourceError('weibo_media_invalid')
            path = work_dir / 'weibo-media' / member_id
            if path.is_symlink():
                raise WeiboSourceError('weibo_media_invalid')
            if not path.exists():
                return None
            try:
                member = self.verifier(path, member_id, 'image')
            except XiaohongshuSourceError as error:
                raise WeiboSourceError('weibo_media_invalid') from error
            if member.manifest() != entry:
                raise WeiboSourceError('weibo_media_invalid')
            members.append(member)
        return CapturedNote(source_key,submitted_url,canonical_url,metadata,tuple(members),source_kind='weibo')
