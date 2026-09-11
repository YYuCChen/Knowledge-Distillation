"""Explicit same-platform leaf selection; existing source adapters own capture."""
from datetime import UTC, datetime
from importlib import import_module

from .bilibili import BilibiliMember
from .chrome import ChromeSessionError
from .douyin_collections import CollectionError, Scope, _digest
from .intake import platform_for_url

CAPTURE_ON_PROCESSING = 'capture_on_processing'
MODULES = {'youtube': 'youtube', 'xiaohongshu': 'xiaohongshu', 'x': 'xpost',
           'zhihu': 'zhihu', 'weibo': 'weibo'}


def authority_for(store, platform):
    if platform == 'bilibili':
        return {'platform': platform, 'class': 'PUBLIC'}
    if platform == 'douyin':
        from .douyin_collections import connection_authority
        return connection_authority(store)
    module = import_module('.' + MODULES[platform], __package__)
    return {'platform': platform, **module.connection_authority(store.connection(platform))}


def discover(store, discovery, urls):
    from .bilibili import BilibiliSourceError
    from .xiaohongshu import XiaohongshuSourceError
    from .weibo import WeiboSourceError
    try:
        return _discover(store, discovery, urls)
    except (BilibiliSourceError, XiaohongshuSourceError, WeiboSourceError) as error:
        raise CollectionError(str(error)) from error


def _discover(store, discovery, urls):
    platforms = {platform_for_url(url) for url in urls}
    if len(urls) < 2 or len(platforms) != 1 or None in platforms:
        raise CollectionError('同题处理需要至少两条同一平台的内容链接，暂不支持跨平台。')
    platform = next(iter(platforms))
    if platform == 'douyin':
        result = discovery.discover(urls)
        if len(result['scopes']) != 1 or result['scopes'][0].kind != 'same_topic':
            raise CollectionError('同题处理请提交单条内容链接，主页或合集请单独投递。')
        members = result['scopes'][0].members
        authority = result['scopes'][0].authority
    elif platform == 'bilibili':
        result = discovery.discover(urls)
        if len(result['scopes']) != len(set(urls)) or any(len(s.members) != 1 for s in result['scopes']):
            raise CollectionError('同题处理请提交单个视频或分P链接，合集请单独投递。')
        members = tuple(s.members[0] for s in result['scopes'])
        authority = authority_for(store, platform)
    elif platform in MODULES:
        authority = authority_for(store, platform)
        members = tuple(_leaf(store, platform, url, authority) for url in urls)
        if authority_for(store, platform) != authority:
            raise CollectionError('collection_connection_changed')
    else:
        raise CollectionError('暂不支持这个平台的同题处理。')
    unique = {}
    for member in members:
        old = unique.get(member.item_id)
        if old is not None and old.version != member.version:
            raise CollectionError('collection_scope_changed')
        unique.setdefault(member.item_id, member)
    if len(unique) < 2:
        raise CollectionError('去除重复链接后不足两条内容，请分别蒸馏或补充其他内容。')
    members = tuple(sorted(unique.values(), key=lambda m: m.item_id))
    scope = Scope('same_topic', _digest({'platform': platform, 'members': [m.item_id for m in members]}),
                  '同题内容', None, members, datetime.now(UTC).isoformat(), authority)
    return {'scopes': [scope], 'choices': [], 'single_url': None}


def _leaf(store, platform, url, authority):
    module = import_module('.' + MODULES[platform], __package__)
    if platform in {'youtube', 'x'}:
        key, canonical = getattr(module, 'youtube_identity' if platform == 'youtube' else 'xpost_identity')(url)
        title = ('YouTube 视频 ' if platform == 'youtube' else 'X 帖文 ') + key
    elif platform == 'zhihu':
        kind, key, _ = module.zhihu_identity(url)
        title = {'answer': '知乎回答 ', 'article': '知乎文章 ', 'pin': '知乎想法 '}[kind] + key
        key, canonical = kind + ':' + key, url
    else:
        # Short links and Weibo base62 aliases require native resolution so the
        # frozen membership uses the same identity as the existing source adapter.
        if platform == 'xiaohongshu':
            key, canonical = module.xiaohongshu_input(url)
            session = module.XiaohongshuSession()
        else:
            key, uid = module.weibo_identity(url)
            session = module.WeiboSession()
        state = session.read(url, authority['browser_context'])
        if state.get('contextId') != authority['browser_context']:
            raise ChromeSessionError(platform + '_connection_changed')
        if platform == 'xiaohongshu':
            if key is None:
                key, canonical = module.xiaohongshu_identity(state.get('pageUrl', ''))
            note, _ = module.qualify_note(state, key)
            title = note.get('title') or '小红书笔记 ' + key
            # Keep the submitted security token that the ordinary adapter needs.
            canonical = url
        else:
            _, key, body, canonical = module.qualify_weibo(state, key, uid)
            title = body.splitlines()[0][:120] if body else '微博内容 ' + key
    return BilibiliMember(key, title, True, 0, CAPTURE_ON_PROCESSING, canonical)
