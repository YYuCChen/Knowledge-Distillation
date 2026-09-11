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
LABELS = {'image': '飞书图片', 'douyin': '抖音', 'youtube': 'YouTube', 'xiaohongshu': '小红书',
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
    links = []
    for match in URL_RE.findall(value):
        # Chinese share delimiters are outside supported content URLs. Preserve
        # query parameters, including XHS signatures and Bilibili part selectors.
        candidate = re.split(r'[，。；、！【】〖〗]', match)[0].rstrip(',.;)]}\"')
        if platform_for_url(candidate) == 'douyin' and urlsplit(candidate).hostname == 'v.douyin.com':
            candidate = re.sub(r'^(https?://v\.douyin\.com/[A-Za-z0-9_-]+/):[^?#]*$', r'\1', candidate)
        links.append(candidate)
    return links


def has_prose(value):
    # A complete Douyin-generated share line is link packaging, not additional
    # user prose. Anchor both ends so an appended/prefixed comment is preserved.
    share = r'\d+(?:\.\d+)?\s+复制打开抖音[，,]\s*看看【[^\n】]+的作品】[^\n]*?https?://v\.douyin\.com/[A-Za-z0-9]+/?[ \t]+[A-Za-z0-9@#%./:;_+=\- \t]*'
    lines=[line.strip() for line in value.splitlines() if line.strip()]
    if lines and all(re.fullmatch(share,line) for line in lines):
        return False
    remainder = URL_RE.sub('', value).strip(' \r\n\t，。；、,.;:：')
    return bool(remainder and remainder not in {'分享', '看看这个', '分享链接'})


def content_platform(value):
    """Use existing adapter validators, not share copy, to recognize inputs.

    Short links are candidates: resolution and source availability are checked
    by normal intake, and failures must never fall back to processing the prose.
    """
    platform = platform_for_url(value)
    if platform is None:
        return None
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
