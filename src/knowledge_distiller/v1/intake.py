"""Shared URL classification; unknown sources never inherit a platform."""
import re
from urllib.parse import urlsplit

URL_RE = re.compile(r"https?://[^\s<>]+")
PLATFORM_HOSTS = {
    'douyin': {'douyin.com', 'www.douyin.com', 'v.douyin.com', 'm.douyin.com'},
    'youtube': {'youtube.com', 'www.youtube.com', 'm.youtube.com', 'youtu.be'},
    'xiaohongshu': {'xiaohongshu.com', 'www.xiaohongshu.com', 'xhslink.com'},
    'weibo': {'weibo.com', 'www.weibo.com', 'm.weibo.cn', 'weibo.cn'},
    'zhihu': {'zhihu.com', 'www.zhihu.com', 'zhuanlan.zhihu.com'},
    'x': {'x.com', 'www.x.com', 'twitter.com', 'www.twitter.com', 'mobile.twitter.com'},
    'bilibili': {'bilibili.com', 'www.bilibili.com', 'm.bilibili.com', 'space.bilibili.com', 'www.b23.tv', 'b23.tv'},
}
LABELS = {'image': '飞书图片', 'feishu_voice': '飞书语音', 'douyin': '抖音', 'youtube': 'YouTube', 'xiaohongshu': '小红书',
          'weibo': '微博', 'zhihu': '知乎', 'x': 'X', 'bilibili': 'B 站',
          'direct_text': '直接文本', 'markdown': 'Markdown', 'pdf': 'PDF', 'epub': 'EPUB'}


def platform_for_url(value):
    try:
        url = urlsplit(value.strip())
        if url.scheme not in {'http', 'https'} or url.username or url.password or url.port:
            return None
        return next((key for key, hosts in PLATFORM_HOSTS.items() if (url.hostname or '').lower() in hosts), None)
    except ValueError:
        return None


def links_in(value):
    return _candidate_links(value, 'markdown')


def message_links(value, *, message_type='text', content=None):
    # Keep structured hrefs separate from labels. raw_json already retains the
    # original receipt; no new wire table or guessed text identity is needed.
    return (_candidate_links(content, 'feishu_post') if message_type == 'post'
            else links_in(value))


def message_needs_content_choice(value, *, message_type='text', content=None):
    if message_type != 'post':
        return needs_content_choice(value)
    import json
    data = json.loads(content)
    if 'content' not in data:
        data = data.get('zh_cn') or data.get('en_us') or next(iter(data.values()))
    prose = [data.get('title') or '']
    for row in data['content']:
        for entry in row:
            if entry.get('tag') == 'a':
                continue  # Only the structured href's display label is packaging.
            if isinstance(entry.get('text'), str):
                prose.append(entry['text'])
            elif entry.get('tag') == 'at':
                prose.append('@' + (entry.get('user_name') or ''))
    return bool(message_links(value, message_type='post', content=content)
                and has_prose('\n'.join(prose)))


def _candidate_links(value, kind):
    import hashlib
    from .multi_link_candidates import LinkInput, prepare_multi_link_input
    raw = value.encode('utf-8')
    digest = hashlib.sha256(raw).hexdigest()
    result = prepare_multi_link_input(LinkInput(('intake',), digest, raw, digest, kind))
    links = []
    for occurrence in result.occurrences:
        # Preserve invalid positions so a bad member never disappears or causes
        # its neighbor to inherit a different receipt_key on replay.
        if occurrence.status == 'ambiguous' or occurrence.href is None:
            links.append('')
            continue
        candidate = occurrence.transport_url or occurrence.href
        if occurrence.kind == 'bare' and not urlsplit(candidate).query:
            candidate = re.split(r'[，。；、！【】〖〗]', candidate)[0].rstrip(',.;')
            if platform_for_url(candidate) == 'douyin' and urlsplit(candidate).hostname == 'v.douyin.com':
                candidate = re.sub(r'^(https?://v\.douyin\.com/[A-Za-z0-9_-]+/):[^?#]*$', r'\1', candidate)
        links.append(candidate)
    return links


def source_route(value):
    """Dedicated hosts stay dedicated, even when their locator is invalid."""
    try:
        host = (urlsplit(value).hostname or '').lower()
    except ValueError:
        return None
    dedicated = next((key for key, hosts in PLATFORM_HOSTS.items() if host in hosts), None)
    if dedicated:
        return dedicated if platform_for_url(value) == dedicated else None
    # Reddit needs the separately authorized API/export contract, not a silent
    # generic-page fallback.
    if host == 'redd.it' or host == 'reddit.com' or host.endswith('.reddit.com'):
        return None
    from .web_article import validate_web_url, WebReadError
    try:
        validate_web_url(value)
    except WebReadError:
        return None
    return 'web_article'


def has_prose(value):
    # A complete Douyin-generated share line is link packaging, not additional
    # user prose. Anchor both ends so an appended/prefixed comment is preserved.
    share = r'\d+(?:\.\d+)?\s+复制打开抖音[，,]\s*看看【[^\n】]+的作品】[^\n]*?https?://v\.douyin\.com/[A-Za-z0-9]+/?[ \t]+[A-Za-z0-9@#%./:;_+=\- \t]*'
    lines=[line.strip() for line in value.splitlines() if line.strip()]
    if lines and all(re.fullmatch(share,line) for line in lines):
        return False
    # Link labels are packaging, not separately submitted user prose. Keep
    # genuine text outside Markdown links so the existing text/link choice
    # remains available for a user's paragraph containing a reference URL.
    from markdown_it import MarkdownIt
    outside = []
    for block in MarkdownIt('commonmark').parse(value):
        if block.type != 'inline':
            continue
        depth = 0
        for token in block.children or ():
            if token.type == 'link_open':
                depth += 1
            elif token.type == 'link_close':
                depth -= 1
            elif not depth and token.type in {'text', 'code_inline'}:
                outside.append(token.content)
    remainder = URL_RE.sub('', ' '.join(outside)).strip(' \r\n\t，。；、,.;:：')
    return bool(remainder and remainder not in {'分享', '看看这个', '分享链接'})


def content_platform(value):
    """Use existing adapter validators, not share copy, to recognize inputs.

    Short links are candidates: resolution and source availability are checked
    by normal intake, and failures must never fall back to processing the prose.
    """
    platform = source_route(value)
    if platform is None:
        return None
    if platform == 'web_article':
        return None  # Preserve the existing prose-versus-reference choice.
    from .youtube import youtube_identity
    from .xiaohongshu import xiaohongshu_input
    from .weibo import weibo_identity
    from .zhihu import zhihu_identity
    from .xpost import xpost_identity
    from .bilibili import bilibili_identity
    if platform == 'douyin':
        from .douyin import parse_work_url
        if urlsplit(value).hostname == 'v.douyin.com':
            return platform if re.fullmatch(r'/[A-Za-z0-9_-]+/?', urlsplit(value).path) else None
        parsed = parse_work_url(value)
        return platform if parsed and parsed.get('type') in {'video', 'gallery', 'article', 'user', 'collection'} else None
    try:
        {'youtube': youtube_identity, 'xiaohongshu': xiaohongshu_input,
         'weibo': weibo_identity, 'zhihu': zhihu_identity,
         'x': xpost_identity, 'bilibili': bilibili_identity}[platform](value)
    except ValueError:
        return None
    return platform


def needs_content_choice(value):
    urls = links_in(value)
    return bool(urls and not any(content_platform(url) for url in urls) and has_prose(value))
