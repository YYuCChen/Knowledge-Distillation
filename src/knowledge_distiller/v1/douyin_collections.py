"""Exact Douyin scope discovery; no queue writes before scope confirmation."""
from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

from .chrome import ChromeSessionError


class CollectionError(RuntimeError):
    pass


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


@dataclass(frozen=True)
class Member:
    item_id: str
    title: str
    supported: bool
    native_kind: int
    version: str


@dataclass(frozen=True)
class Scope:
    kind: str
    key: str
    title: str
    creator_id: str | None
    members: tuple[Member, ...]
    captured_at: str
    authority: dict
    capture_nonce: str = ''

    @property
    def signature(self):
        # Display titles, capture time, and browser generation aren't membership.
        return _digest({'kind': self.kind, 'key': self.key, 'creator': self.creator_id,
                        'members': [(m.item_id, m.native_kind) for m in self.members]})

    @property
    def content_signature(self):
        versions = [(m.item_id, m.version) for m in self.members]
        return _digest({'members': versions, 'capture_nonce': self.capture_nonce}) if self.capture_nonce else _digest(versions)

    def to_dict(self):
        return {**asdict(self), 'signature': self.signature, 'content_signature': self.content_signature}


def connection_authority(store):
    row = store.connection('douyin')
    if row is None or row['state'] == 'unconfigured':
        raise ChromeSessionError('douyin_not_configured')
    if row['state'] != 'connected':
        raise ChromeSessionError('douyin_login_required')
    return {'platform': 'douyin', 'class': 'EXISTING_BROWSER_OWNED',
            'generation': row['generation'], 'connected_at': row['connected_at']}


def _identity(detail, *, strict=False):
    if not isinstance(detail, dict):
        raise CollectionError('collection_membership_incomplete')
    key = str(detail.get('aweme_id') or '')
    native_kind = detail.get('aweme_type')
    if not key.isdigit() or not isinstance(native_kind, int):
        raise CollectionError('collection_membership_incomplete')
    video = detail.get('video') or {}
    play = video.get('play_addr') or {}
    # The native media URI excludes expiring CDN signatures. Changes in authored
    # description, media identity, kind or duration invalidate content reuse.
    media_id = video.get('vid') or play.get('uri')
    if native_kind in {0, 4} and not detail.get('images') and not media_id:
        raise CollectionError('collection_membership_incomplete')
    supported = native_kind in {0, 4} and not detail.get('images') and bool(media_id)
    from .douyin import content_version
    from .douyin_text import parse_douyin_text, DouyinTextError
    try:
        supported = supported or parse_douyin_text(detail) is not None
    except DouyinTextError as error:
        if strict:
            raise CollectionError(str(error)) from error
        supported = False
    return Member(key, str(detail.get('desc') or ''), supported, native_kind, content_version(detail))


async def _pages(fetch, *, keys):
    cursor = 0
    seen_cursors = {cursor}
    result = []
    while True:
        response = await fetch(cursor)
        raw = response.get('raw') if isinstance(response, dict) else None
        if not isinstance(raw, dict) or raw.get('status_code') != 0 or raw.get('has_more') not in (0, 1):
            raise CollectionError('collection_membership_incomplete')
        rows = next((raw[k] for k in keys if isinstance(raw.get(k), list)), None)
        # The native series list explicitly reports total=0 with a null list
        # for creators who only have legacy mixes. Missing/unknown is not empty.
        if (rows is None and raw.get('total') == 0 and type(raw.get('total')) is int
                and raw['has_more'] == 0 and any(k in raw and raw[k] is None for k in keys)):
            rows = []
        if rows is None:
            raise CollectionError('collection_membership_incomplete')
        result.extend(rows)
        if not raw['has_more']:
            return result
        cursor = raw.get('max_cursor', raw.get('cursor'))
        if not isinstance(cursor, int) or cursor in seen_cursors:
            raise CollectionError('collection_membership_incomplete')
        seen_cursors.add(cursor)


def _members(rows):
    members = {}
    for row in rows:
        member = _identity(row)
        old = members.get(member.item_id)
        if old is not None and old != member:
            raise CollectionError('collection_scope_changed')
        members.setdefault(member.item_id, member)
    if not members or not any(m.supported for m in members.values()):
        raise CollectionError('collection_no_supported_members')
    return tuple(members.values())


class DouyinCollections:
    def __init__(self, store, session, client_factory=None):
        self.store = store
        self.session = session
        self.client_factory = client_factory

    def discover(self, urls, *, selected=None):
        authority = connection_authority(self.store)
        try:
            result = asyncio.run(self._discover(urls, selected, authority))
        except ChromeSessionError as error:
            if str(error) == 'douyin_login_required':
                self.store.require_relogin('douyin')
            raise
        if connection_authority(self.store) != authority:
            raise CollectionError('collection_connection_changed')
        return result

    async def _discover(self, urls, selected, authority):
        from core.api_client import LoginRequiredError
        from .douyin_collection_browser import BrowserCollectionClient
        from core.url_parser import URLParser
        from .douyin import parse_work_url
        from utils.validators import is_short_url, normalize_short_url
        client_instance = self.client_factory(self.session.cookies()) if self.client_factory else BrowserCollectionClient(self.session)
        try:
            async with client_instance as client:
                parsed = []
                for url in urls:
                    parsed_url = urlsplit(url)
                    if parsed_url.scheme not in {'https', 'http'} or parsed_url.username or parsed_url.password or parsed_url.port:
                        raise CollectionError('collection_input_unsupported')
                    host = parsed_url.hostname or ''
                    if host not in {'www.douyin.com', 'douyin.com', 'v.douyin.com', 'www.iesdouyin.com', 'www.amemv.com'}:
                        raise CollectionError('collection_input_unsupported')
                    resolved = await client.resolve_short_url(normalize_short_url(url)) if is_short_url(url) else url
                    value = parse_work_url(resolved) if resolved else None
                    if not value or value.get('type') not in {'video', 'gallery', 'article', 'user', 'collection'}:
                        raise CollectionError('collection_input_unsupported')
                    parsed.append(value)
                now = datetime.now(UTC).isoformat()
                if all(p['type'] in {'video', 'gallery', 'article'} for p in parsed):
                    ids = sorted({p.get('aweme_id') for p in parsed})
                    rows = []
                    for key in ids:
                        detail = await client.get_video_detail(key)
                        if not isinstance(detail, dict) or str(detail.get('aweme_id')) != key:
                            raise CollectionError('collection_identity_mismatch')
                        if len(ids) == 1:
                            # A single work must retain its source failure instead
                            # of being reported as an empty collection.
                            _identity(detail, strict=True)
                        rows.append(detail)
                    members = tuple(sorted(_members(rows), key=lambda m: m.item_id))
                    scope = Scope('same_topic', _digest([m.item_id for m in members]), '同题内容', None, members, now, authority)
                    return {'scopes': [scope], 'choices': [], 'single_url': f'https://www.douyin.com/video/{members[0].item_id}' if len(members) == 1 else None}
                if len(parsed) != 1:
                    raise CollectionError('collection_input_unsupported')
                value = parsed[0]
                if value['type'] == 'collection':
                    return {'scopes': [await self._mix(client, value['mix_id'], None, now, authority)], 'choices': []}
                profile_id = value.get('sec_uid')
                profile = await client.get_user_info(profile_id)
                if not isinstance(profile, dict) or profile.get('sec_uid') != profile_id:
                    raise CollectionError('collection_identity_mismatch')
                mixes = await _pages(lambda cursor: client.get_user_mix(profile_id, cursor), keys=['mix_infos', 'mix_list'])
                choices = []
                for mix in mixes:
                    key = str(mix.get('mix_id') or '')
                    if not key.isdigit():
                        raise CollectionError('collection_membership_incomplete')
                    if key not in {m['key'] for m in choices}:
                        count = mix.get('member_count')
                        if count is not None and (type(count) is not int or count < 0):
                            raise CollectionError('collection_membership_incomplete')
                        choices.append({'key': key, 'title': str(mix.get('mix_name') or key), 'member_count': count})
                if selected is None:
                    return {'scopes': [], 'choices': choices, 'profile_id': profile_id, 'profile_title': profile.get('nickname') or profile_id}
                if selected == ['full_profile']:
                    rows = await _pages(lambda cursor: client.get_user_post(profile_id, cursor), keys=['aweme_list'])
                    return {'scopes': [Scope('full_profile', profile_id, str(profile.get('nickname') or profile_id), profile_id, _members(rows), now, authority)], 'choices': choices}
                ids = [m['key'] for m in choices] if selected == ['all_collections'] else list(dict.fromkeys(selected))
                if not ids or not set(ids) <= {m['key'] for m in choices}:
                    raise CollectionError('collection_scope_changed')
                empty = [{'key': m['key'], 'title': m['title']} for m in choices
                         if m['key'] in ids and m['member_count'] == 0]
                ids = [key for key in ids if key not in {m['key'] for m in empty}]
                if not ids:
                    raise CollectionError('collection_empty')
                scopes = [await self._mix(client, key, profile_id, now, authority) for key in ids]
                return {'scopes': scopes, 'choices': choices, 'empty_collections': empty}
        except LoginRequiredError as error:
            self.store.require_relogin('douyin')
            raise ChromeSessionError('douyin_login_required') from error

    async def _mix(self, client, key, profile_id, now, authority):
        detail = await client.get_mix_detail(key)
        if not isinstance(detail, dict) or str(detail.get('mix_id')) != key:
            raise CollectionError('collection_identity_mismatch')
        creator = (detail.get('author') or {}).get('sec_uid')
        if not creator or (profile_id is not None and creator != profile_id):
            raise CollectionError('collection_identity_mismatch')
        rows = await _pages(lambda cursor: client.get_mix_aweme(key, cursor), keys=['aweme_list'])
        expected = (detail.get('statis') or {}).get('updated_to_episode')
        for row in rows:
            if str((row.get('mix_info') or {}).get('mix_id')) != key:
                raise CollectionError('collection_identity_mismatch')
            current = ((row.get('mix_info') or {}).get('statis') or {}).get('updated_to_episode')
            if expected is not None and current != expected:
                raise CollectionError('collection_scope_changed')
        members = _members(rows)
        if expected is not None and (type(expected) is not int or expected != len(members)):
            raise CollectionError('collection_membership_incomplete')
        return Scope('creator_collection', key, str(detail.get('mix_name') or key), creator, members, now, authority)
