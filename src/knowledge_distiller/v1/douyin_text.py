"""Strict native Douyin gallery/article parsing; no network or image interpretation.

Article shape: karin-plugin-kkk d8b92435d35e56693ceb37f18c451ef51ef86bee,
ArticleWork.tsx and platform/douyin/douyin.ts. Gallery/live shape also follows
installed douyin-downloader 848bcaf7bf5c5bebbe028e8ccec76e30ad1bef6b.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlsplit

from markdown_it import MarkdownIt


class DouyinTextError(ValueError):
    def __init__(self, code: str = 'douyin_text_invalid'):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class DouyinTextImage:
    member_id: str
    identity: str
    url: str
    markdown_url: str | None = None


@dataclass(frozen=True)
class DouyinText:
    kind: str
    item_id: str
    canonical_url: str
    title: str
    body: str
    images: tuple[DouyinTextImage, ...]
    native_content_version: str
    original_markdown: str | None = None
    motion_omitted: tuple[str, ...] = ()


def parse_douyin_text(detail: Mapping[str, object]) -> DouyinText | None:
    kind = detail.get('aweme_type')
    # The same article is reported as type 0 by Chrome's web endpoint and
    # type 163 by the installed HTTP client. Complete article_info is decisive.
    if kind in (0, 163) and detail.get('article_info'):
        kind = 163
    elif kind == 0 and detail.get('images'):
        # Chrome web detail uses 0 for native static galleries as well.
        kind = 68
    if kind not in (68, 163):
        if (detail.get('images') or detail.get('image_post_info')
                or detail.get('article_info') or kind in (2, 150)):
            raise DouyinTextError('douyin_input_unsupported')
        return None
    item_id = detail.get('aweme_id')
    if not isinstance(item_id, str) or not item_id.isascii() or not item_id.isdigit():
        raise DouyinTextError()
    if kind == 163:
        title, body, images, markdown = _article(detail)
        name, path = 'article', 'article'
    else:
        title = ''  # desc is caption, not an independently supplied native title.
        body = _string(detail.get('desc', ''))
        if '版本过低，升级后可展示全部信息' in body:
            raise DouyinTextError('douyin_gallery_incomplete')
        images = _gallery(detail)
        markdown = None
        name, path = 'gallery', 'note'
    motion_omitted = tuple(f'image-{index}' for index, entry in enumerate(detail.get('images') or [], 1)
                           if any(entry.get(key) for key in ('video', 'video_play_addr', 'video_download_addr'))) if name == 'gallery' else ()
    version_data = {
        'id': item_id, 'kind': name, 'title': title, 'body': body,
        'markdown': _version_markdown(markdown, images),
        'images': [(_stable_reference(image.identity) if name == 'article' else image.identity,
                    _stable_url(image.url) if name == 'article' else None)
                   for image in images],
    }
    if motion_omitted:
        version_data['motion_omitted'] = motion_omitted
    version = hashlib.sha256(json.dumps(version_data, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return DouyinText(name, item_id, f'https://www.douyin.com/{path}/{item_id}',
                      title, body, images, version, markdown, motion_omitted)


def _gallery(detail):
    entries = detail.get('images')
    if not isinstance(entries, list) or not entries:
        raise DouyinTextError()
    images = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise DouyinTextError()
        # Static originals are the selected core, including Live Photos.
        # Their motion is explicitly recorded as omitted by the caller.
        identity = _string(entry.get('uri'))
        urls = entry.get('url_list')
        if not identity or not isinstance(urls, list) or not urls:
            raise DouyinTextError()
        # Validate every declared alternative; do not silently omit malformed members.
        for url in urls:
            _url(url)
        images.append(DouyinTextImage(f'image-{len(images) + 1}', identity, urls[0]))
    return tuple(images)


def _article(detail):
    info = detail.get('article_info')
    if not isinstance(info, dict):
        raise DouyinTextError()
    if info.get('has_more') is True:
        raise DouyinTextError('douyin_article_incomplete')
    if info.get('has_more') not in (None, False):
        raise DouyinTextError()
    title = _string(info.get('article_title'))
    content = _object_json(info.get('article_content'))
    markdown = _string(content.get('markdown'))
    frontend = _object_json(info.get('fe_data'))
    entries = frontend.get('image_list', [])
    if not isinstance(entries, list) or not title.strip() or not markdown.strip():
        raise DouyinTextError()
    bindings = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise DouyinTextError()
        reference = _string(entry.get('markdown_url'))
        original = _url(entry.get('origin_image_url'))
        if not reference or reference in bindings:
            raise DouyinTextError()
        bindings[reference] = original
    normalized = re.sub(r'!\[([^\]]*)\]\(([^\s)]+)(?:\s+width=\d+)?(?:\s+height=\d+)?\)',
                        r'![\1](\2)', markdown)
    blocks, images, used = [], [], set()
    parser = MarkdownIt('commonmark').enable('table')
    for token in parser.parse(normalized):
        if token.type in ('html_block', 'html_inline'):
            blocks.append(_br_text(token.content))
            continue
        if token.type in ('fence', 'code_block'):
            blocks.append(token.content.rstrip('\n'))
        if token.type != 'inline':
            continue
        text = []
        for child in token.children or []:
            if child.type == 'image':
                reference = child.attrGet('src')
                if reference not in bindings:
                    raise DouyinTextError()
                used.add(reference)
                image = DouyinTextImage(f'image-{len(images) + 1}', reference,
                                        bindings[reference], reference)
                images.append(image)
                text.append(f'〔图片 {len(images)}〕' + child.content)
            elif child.type == 'html_inline':
                text.append(_br_text(child.content))
            elif child.type in ('text', 'code_inline'):
                if child.type == 'text' and '![' in child.content:
                    raise DouyinTextError()
                text.append(child.content)
            elif child.type in ('softbreak', 'hardbreak'):
                text.append('\n')
        blocks.append(''.join(text))
    # An unmatched declared image is also incomplete, not an optional cover guess.
    if used != set(bindings):
        raise DouyinTextError()
    body = '\n\n'.join(blocks).strip()
    if not body:
        raise DouyinTextError()
    return title, body, tuple(images), markdown


def _string(value):
    if not isinstance(value, str):
        raise DouyinTextError()
    return value


def _object_json(value):
    try:
        result = json.loads(_string(value))
    except (ValueError, TypeError) as error:
        raise DouyinTextError() from error
    if not isinstance(result, dict):
        raise DouyinTextError()
    return result


def _url(value):
    value = _string(value)
    try:
        parts = urlsplit(value)
        if parts.scheme not in ('https', 'http') or not parts.hostname or parts.username or parts.password:
            raise ValueError()
    except ValueError as error:
        raise DouyinTextError() from error
    return value


def _stable_url(value):
    parts = urlsplit(value)
    host = 'douyinpic.com' if (parts.hostname or '').endswith('.douyinpic.com') else parts.netloc
    return (parts.scheme, host, parts.path)


def _br_text(content):
    # Native editor line breaks have no attributes or other HTML semantics.
    if not re.fullmatch(r'(?:\s*<br\s*/?>\s*)+', content, flags=re.IGNORECASE):
        raise DouyinTextError('douyin_input_unsupported')
    return '\n' * len(re.findall(r'<br\s*/?>', content, flags=re.IGNORECASE))


def _stable_reference(value):
    parts = urlsplit(value)
    if parts.scheme not in ('http', 'https'):
        return value
    host = 'douyinpic.com' if (parts.hostname or '').endswith('.douyinpic.com') else parts.netloc
    return parts._replace(netloc=host, query='', fragment='').geturl()


def _version_markdown(markdown, images):
    if markdown is None:
        return None
    # Only bound image URLs are canonicalized, and only in the version input.
    # Body text is hashed separately, so a visible URL edit remains a native edit.
    for reference in sorted({image.markdown_url for image in images}, key=len, reverse=True):
        markdown = markdown.replace(reference, _stable_reference(reference))
    return markdown
