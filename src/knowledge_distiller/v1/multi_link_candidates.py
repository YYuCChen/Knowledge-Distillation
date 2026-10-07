"""Pure R06 input candidates. Structural validity is never capture success."""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import re
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt
from markdown_it.helpers import parseLinkDestination

from .intake import PLATFORM_HOSTS


@dataclass(frozen=True)
class LinkInput:
    namespace: tuple[str, ...]
    version: str
    raw_utf8: bytes
    sha256: str
    kind: str = 'markdown'  # Or Feishu post JSON, never a flattened label+href.


@dataclass(frozen=True)
class Evidence:
    granularity: str
    line_range: tuple[int, int] | None = None  # Zero-based, end-exclusive block.
    json_path: tuple[str | int, ...] | None = None
    href_value: str | None = None
    label_value: str | None = None


@dataclass(frozen=True)
class Diagnostic:
    reason: str
    evidence: Evidence


@dataclass(frozen=True)
class LinkOccurrence:
    position: int
    kind: str
    href: str | None
    label: str | None
    evidence: Evidence
    status: str
    reason: str | None = None
    platform: str | None = None
    native_id: str | None = None
    transport_url: str | None = None
    duplicate_of: int | None = None
    related_to: tuple[int, ...] = ()


@dataclass(frozen=True)
class MultiLinkCandidates:
    source: LinkInput  # Full bytes retained even when admission fails.
    occurrences: tuple[LinkOccurrence, ...]
    diagnostics: tuple[Diagnostic, ...]
    mode: str
    group_eligibility: str
    group_reasons: tuple[str, ...]
    collection_succeeded: bool = False


_START = re.compile(r'https?://', re.I)
_ID = re.compile(r'(?:BV[0-9A-Za-z]{10}|av[0-9]+)')
_CONTROL = re.compile(r'[\x00-\x1f\x7f]')
_MALFORMED = re.compile(r'\]\s*[\[(]|\[[^\]]*https?://', re.I)


def _locator(value):
    """Return status/reason/platform/native/transport without any resolution."""
    if _CONTROL.search(value) or _CONTROL.search(unquote(value)):
        return 'invalid', 'url_control_character', None, None, value
    if _ID.fullmatch(value):
        from .bilibili import bilibili_identity
        key, canonical = bilibili_identity(value)
        return 'valid', None, 'bilibili', key, canonical
    try:
        url = urlsplit(value)
        if (url.scheme not in {'http', 'https'} or not url.hostname
                or any(ch.isspace() for ch in value)):
            return 'invalid', 'url_structure_invalid', None, None, value
        # Reject explicit ports including :0, :443 and an empty port suffix.
        if url.username is not None or url.password is not None or '@' in url.netloc:
            return 'invalid', 'url_userinfo_forbidden', None, None, value
        if ':' in url.netloc:
            return 'invalid', 'url_port_or_literal_host_forbidden', None, None, value
        host = url.hostname.lower()
        if (len(host) > 253 or any(len(part) > 63 or not re.fullmatch(
                r'[a-z0-9](?:[a-z0-9-]*[a-z0-9])?', part) for part in host.split('.'))):
            return 'invalid', 'url_host_invalid', None, None, value
        platform = next((p for p, hosts in PLATFORM_HOSTS.items() if host in hosts), None)
        if platform is None:
            return 'pending_route', 'unknown_host_requires_route', None, None, value
        if platform == 'youtube':
            from .youtube import youtube_identity
            key, _ = youtube_identity(value)
        elif platform == 'xiaohongshu':
            from .xiaohongshu import xiaohongshu_input
            key, _ = xiaohongshu_input(value)
            if key is None:
                return 'needs_resolution', 'short_link_unresolved', platform, None, value
        elif platform == 'weibo':
            from .weibo import weibo_identity
            key, _ = weibo_identity(value)
        elif platform == 'zhihu':
            from .zhihu import zhihu_identity
            kind, key, _ = zhihu_identity(value)
            key = kind + ':' + key
        elif platform == 'x':
            from .xpost import xpost_identity
            key, _ = xpost_identity(value)
        elif platform == 'bilibili':
            from .bilibili import bilibili_identity
            key, _ = bilibili_identity(value)
            if key.startswith('short:'):
                return 'needs_resolution', 'short_link_unresolved', platform, None, value
            if (key.startswith('range:') or host == 'space.bilibili.com'
                    or (not url.fragment.startswith('kd-source=') and re.fullmatch(
                        r'/(?:bangumi|cheese)/play/ss[1-9][0-9]*/?', url.path))):
                return 'needs_scope', 'native_range_requires_scope', platform, key, value
        else:
            # The legacy Douyin parser uses substring/search patterns. Apply
            # the existing single-work intake's full path guard before reuse.
            if host == 'v.douyin.com':
                if not re.fullmatch(r'/[A-Za-z0-9_-]+/?', url.path):
                    raise ValueError('invalid_short_path')
                return 'needs_resolution', 'short_link_unresolved', platform, None, value
            match = re.fullmatch(r'/(video|note|gallery|slides|article)/([0-9]+)/?', url.path)
            if not match:
                if re.fullmatch(r'/(?:user/[A-Za-z0-9_-]+|(?:collection|mix)/[0-9]+)/?', url.path):
                    return 'needs_scope', 'native_range_requires_scope', platform, None, value
                raise ValueError('invalid_work_path')
            from .douyin import parse_work_url
            parsed = parse_work_url(value)
            if not parsed or str(parsed.get('aweme_id')) != match[2]:
                raise ValueError('work_identity_mismatch')
            key = ('article:' if match[1] == 'article' else 'work:') + match[2]
        return 'valid', None, platform, key, value
    except ValueError as error:
        return 'invalid', 'platform_locator_invalid:' + str(error), locals().get('platform'), None, value
    except ImportError:
        return 'pending_route', 'local_validator_unavailable', locals().get('platform'), None, value


def _occurrence(position, kind, href, label, evidence, *, reason=None):
    if href is None:
        return LinkOccurrence(position, kind, None, label, evidence, 'ambiguous', reason)
    status, why, platform, native, transport = _locator(href)
    if reason:
        status, why = 'ambiguous', reason
    elif label and (_START.match(label) or _ID.fullmatch(label)):
        ls, _, lp, ln, _ = _locator(label)
        if native is not None and ln is not None and (platform, native) != (lp, ln):
            status, why = 'ambiguous', 'label_href_identity_conflict'
        elif label != href and (ls != 'valid' or status != 'valid'):
            status, why = 'ambiguous', 'label_href_identity_unverified'
    return LinkOccurrence(position, kind, href, label, evidence, status, why, platform, native, transport)


def _text_occurrences(text, evidence, add):
    """Only ordinary text-token values, never link labels or consumed code."""
    cursor = 0
    for match in _START.finditer(text):
        if match.start() < cursor:
            continue
        # Do not split Chinese punctuation out of a potentially valid query.
        maximum = len(text)
        result = parseLinkDestination(text, match.start(), maximum)
        if not result.ok:
            add('bare', None, None, evidence, reason='bare_destination_unbalanced')
            cursor = maximum
            continue
        href = result.str
        reason = 'adjacent_locators_without_boundary' if len(_START.findall(href)) > 1 else None
        add('bare', href, None, replace(evidence, href_value=href), reason=reason)
        cursor = result.pos
    # Native IDs are admitted only as an entire independent text token/line.
    if not _START.search(text):
        for line in text.splitlines():
            if _ID.fullmatch(line.strip()):
                value = line.strip()
                add('platform_id', value, None, replace(evidence, href_value=value))


def _markdown(text, add, diagnostics):
    env = {}
    tokens = MarkdownIt('commonmark').parse(text, env)
    for block in tokens:
        if block.type != 'inline':
            continue  # Definitions, fences/code/HTML blocks are not submissions.
        evidence = Evidence('block_lines', tuple(block.map) if block.map else None)
        children = block.children or []
        if any(token.type == 'html_inline' for token in children):
            diagnostics.append(Diagnostic('html_inline_block_not_submitted', evidence))
            continue  # HTML inner text is not an independently pasted locator.
        outside = []
        depth = 0
        for token in children:
            if token.type == 'link_open':
                depth += 1
            elif token.type == 'link_close':
                depth -= 1
            elif not depth and token.type == 'text':
                outside.append(token.content)
        # A failed Markdown link stays one unresolved block. Do not extract
        # its display URL and malformed destination as two fake valid URLs.
        if any(_MALFORMED.search(value) and _START.search(value) for value in outside):
            diagnostics.append(Diagnostic('malformed_markdown_block', evidence))
            add('markdown', None, None, evidence, reason='malformed_markdown_block')
            continue
        i = 0
        while i < len(children):
            token = children[i]
            if token.type == 'link_open':
                href = token.attrGet('href')
                label_parts = []
                j = i + 1
                while j < len(children) and children[j].type != 'link_close':
                    child = children[j]
                    if child.type in {'text', 'code_inline'}:
                        label_parts.append(child.content)
                    elif child.type in {'softbreak', 'hardbreak'}:
                        label_parts.append('\n')
                    j += 1
                label = ''.join(label_parts)
                # This is a local adjacent-token ambiguity, not a prose rule.
                tail = children[j + 1] if j + 1 < len(children) else None
                reason = ('numeric_tail_after_markdown_link' if tail and tail.type == 'text'
                          and re.match(r'[0-9]', tail.content) else None)
                ev = replace(evidence, href_value=href, label_value=label)
                add('autolink' if token.markup == 'autolink' else 'markdown', href, label, ev, reason=reason)
                i = j + 1
                continue
            if token.type == 'text':
                _text_occurrences(token.content, evidence, add)
            elif token.type in {'code_inline', 'html_inline', 'image'}:
                diagnostics.append(Diagnostic('non_submission_token:' + token.type, evidence))
            i += 1


def _post(text, add, diagnostics):
    data = json.loads(text)
    prefix = ()
    if not isinstance(data, dict):
        raise ValueError('post_object_required')
    if 'content' not in data:
        locale = next((key for key in ('zh_cn', 'en_us') if key in data), None)
        if locale is None:
            raise ValueError('post_locale_unrecognized')
        data, prefix = data[locale], (locale,)
    if not isinstance(data, dict) or not isinstance(data.get('content'), list):
        raise ValueError('post_content_rows_required')
    for row_index, row in enumerate(data['content']):
        ev = Evidence('json_path', json_path=(*prefix, 'content', row_index))
        if not isinstance(row, list):
            diagnostics.append(Diagnostic('post_row_invalid', ev))
            continue
        for index, entry in enumerate(row):
            ev = replace(ev, json_path=(*prefix, 'content', row_index, index))
            if not isinstance(entry, dict):
                diagnostics.append(Diagnostic('post_entry_invalid', ev))
                continue
            if entry.get('tag') == 'a':
                href, label = entry.get('href'), entry.get('text', '')
                if not isinstance(href, str) or not isinstance(label, str):
                    add('post_anchor', None, None, ev, reason='post_anchor_fields_invalid')
                else:
                    add('post_anchor', href, label, replace(ev, href_value=href, label_value=label))
            elif entry.get('tag') == 'text' and isinstance(entry.get('text'), str):
                value = entry['text']
                if _MALFORMED.search(value) and _START.search(value):
                    add('post_text', None, None, ev, reason='malformed_markdown_post_text')
                else:
                    _text_occurrences(value, ev, add)
            else:
                diagnostics.append(Diagnostic('post_entry_not_plain_text_or_anchor', ev))


def prepare_multi_link_input(source: LinkInput, *, explicit_same_topic: bool = False) -> MultiLinkCandidates:
    """Prepare ordered candidates only. No queue, source ID or receipt actions."""
    if type(source.raw_utf8) is not bytes:
        raise TypeError('raw_utf8_must_be_bytes')
    occurrences, diagnostics = [], []
    def add(kind, href, label, evidence, *, reason=None):
        occurrences.append(_occurrence(len(occurrences), kind, href, label, evidence, reason=reason))
    try:
        if (type(source.namespace) is not tuple or not source.namespace
                or not all(isinstance(v, str) and v for v in source.namespace)
                or not isinstance(source.version, str) or not source.version):
            raise ValueError('source_identity_invalid')
        if hashlib.sha256(source.raw_utf8).hexdigest() != source.sha256:
            raise ValueError('source_hash_mismatch')
        text = source.raw_utf8.decode('utf-8')
        if any(ord(ch) < 32 and ch not in '\r\n\t' for ch in text) or '\x7f' in text:
            raise ValueError('input_control_character')
        if source.kind == 'markdown':
            _markdown(text, add, diagnostics)
        elif source.kind == 'feishu_post':
            _post(text, add, diagnostics)
        else:
            raise ValueError('input_kind_invalid')
    except (ValueError, UnicodeError) as error:
        diagnostics.append(Diagnostic('input_admission_failed:' + str(error), Evidence('whole_input')))
        # Never expose partially prepared values after global admission failure.
        occurrences.clear()
    first_transport, same_native = {}, {}
    for i, value in enumerate(occurrences):
        duplicate = first_transport.get(value.transport_url) if value.transport_url is not None else None
        native_key = (value.platform, value.native_id)
        related = tuple(same_native.get(native_key, ())) if value.native_id is not None else ()
        occurrences[i] = replace(value, duplicate_of=duplicate, related_to=related)
        if value.transport_url is not None:
            first_transport.setdefault(value.transport_url, i)
        if value.native_id is not None:
            same_native.setdefault(native_key, []).append(i)
    group_reasons = []
    if explicit_same_topic:
        if diagnostics:
            group_reasons.append('input_has_diagnostics')
        if any(o.status != 'valid' for o in occurrences):
            group_reasons.append('members_not_all_valid_leaves')
        if len({(o.platform, o.native_id) for o in occurrences if o.status == 'valid'}) < 2:
            group_reasons.append('fewer_than_two_distinct_native_candidates')
        if len({o.platform for o in occurrences}) != 1:
            group_reasons.append('same_platform_required')
    eligibility = ('blocked' if group_reasons else 'eligible') if explicit_same_topic else 'not_requested'
    return MultiLinkCandidates(source, tuple(occurrences), tuple(diagnostics),
        'explicit_same_topic' if explicit_same_topic else 'independent', eligibility, tuple(group_reasons))
