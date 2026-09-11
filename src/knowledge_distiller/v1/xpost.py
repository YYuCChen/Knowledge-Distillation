from __future__ import annotations

import json
import re
import shutil
from datetime import UTC, datetime
from urllib.parse import urlsplit, parse_qs, urlencode, urlunsplit
from uuid import NAMESPACE_URL, uuid5

from .chrome import ChromeSessionError
from .opencli_session import read_opencli
from .xiaohongshu import CapturedNote, verify_media, XiaohongshuSourceError


class XPostSourceError(RuntimeError):
    pass


def xpost_identity(value):
    try:
        url = urlsplit(value.strip())
        match = re.fullmatch(r'/(?:[A-Za-z0-9_]{1,15}|i)/status/([0-9]{1,25})/?', url.path)
        if (url.scheme not in {'http','https'} or url.hostname not in
                {'x.com','www.x.com','twitter.com','www.twitter.com','mobile.twitter.com'}
                or url.username or url.password or url.port or not match):
            raise ValueError
        return match[1], f'https://x.com/i/status/{match[1]}'
    except ValueError as error:
        raise ValueError('请提交一条 X 普通帖文链接；暂不支持视频、GIF 或 Article。') from error


def connection_authority(row):
    if row is None or row['state']=='unconfigured' or not row['browser_context']:
        raise ChromeSessionError('x_not_configured')
    if row['state']!='connected':
        raise ChromeSessionError('x_login_required')
    context=row['browser_context']
    instance=uuid5(NAMESPACE_URL,f"x:{row['connected_at']}:{row['generation']}:{context}").hex
    return {'class':'EXISTING_BROWSER_OWNED','connection_instance_id':instance,
            'session_authority_ref':f'browser:{instance}','generation':row['generation'],
            'connected_at':row['connected_at'],'browser_context':context}


class XPostSession:
    def read(self,url,context=None):
        return read_opencli('xpost','x',url,context)

    def verify(self):
        return self.read('https://x.com/home')['contextId']


def qualify_post(state,key):
    if state.get('loggedIn') is not True:
        raise ChromeSessionError('x_login_required')
    if state.get('requestedId')!=key:
        raise XPostSourceError('x_identity_mismatch')
    raw=state.get('raw') or {}
    if raw.get('errors'):
        raise XPostSourceError('x_upstream_failed')
    instructions=raw.get('data',{}).get('threaded_conversation_with_injections_v2',{}).get('instructions',[])
    matches=[]
    for instruction in instructions:
        for entry in instruction.get('entries',[]):
            content=entry.get('content',{})
            items=[content.get('itemContent',{})]
            items.extend(i.get('item',{}).get('itemContent',{}) for i in content.get('items',[]))
            for item in items:
                result=item.get('tweet_results',{}).get('result',{})
                post=result.get('tweet',result)
                if post.get('rest_id')==key:
                    matches.append(post)
    if not matches:
        raise XPostSourceError('x_identity_mismatch')
    post=matches[0]
    if any(p!=post for p in matches[1:]):
        raise XPostSourceError('x_snapshot_unknown')
    legacy=post.get('legacy')
    if not isinstance(legacy,dict) or legacy.get('id_str')!=key or post.get('__typename')!='Tweet':
        raise XPostSourceError('x_snapshot_unknown')
    if post.get('article') or legacy.get('retweeted_status_result') or post.get('retweeted_status_result'):
        raise XPostSourceError('x_input_unsupported')
    note=post.get('note_tweet',{}).get('note_tweet_results',{}).get('result',{})
    text=note.get('text') if note else legacy.get('full_text')
    if not isinstance(text,str) or (legacy.get('truncated') and not note):
        raise XPostSourceError('x_text_incomplete')
    # Inline long-post structures must be closed by the same ordered media set.
    if note.get('media',{}).get('inline_media'):
        raise XPostSourceError('x_snapshot_unknown')
    media=legacy.get('extended_entities',{}).get('media',[])
    described=legacy.get('entities',{}).get('media',[])
    if not isinstance(media,list) or (described and len(described)!=len(media)):
        raise XPostSourceError('x_media_incomplete')
    urls=[]
    for member in media:
        if member.get('type')!='photo':
            raise XPostSourceError('x_input_unsupported')
        url=member.get('media_url_https')
        if not isinstance(url,str):
            raise XPostSourceError('x_media_incomplete')
        parsed=urlsplit(url)
        if parsed.scheme!='https' or parsed.hostname!='pbs.twimg.com' or parsed.username or parsed.password or parsed.port:
            raise XPostSourceError('x_media_incomplete')
        query=parse_qs(parsed.query)
        query['name']=['orig']
        urls.append(urlunsplit(parsed._replace(query=urlencode(query,doseq=True))))
    if not text.strip() and not urls:
        raise XPostSourceError('x_text_incomplete')
    return post,text,urls


def download_image(url,path):
    import httpx
    try:
        with httpx.stream('GET',url,timeout=60) as response:
            response.raise_for_status()
            with path.open('xb') as output:
                for chunk in response.iter_bytes():output.write(chunk)
    except (httpx.HTTPError,OSError) as error:
        raise XPostSourceError('x_media_incomplete') from error


class XPostSource:
    def __init__(self,store,session=None,downloader=download_image,verifier=verify_media):
        self.store=store;self.session=session or XPostSession()
        self.downloader=downloader;self.verifier=verifier

    def capture(self,submitted_url,work_dir,*,expected_authority):
        key,canonical=xpost_identity(submitted_url)
        authority=connection_authority(self.store.connection('x'))
        if authority!=expected_authority:raise ChromeSessionError('x_connection_changed')
        try:
            state=self.session.read(canonical,authority['browser_context'])
            if state.get('contextId')!=authority['browser_context']:raise ChromeSessionError('x_connection_changed')
            post,text,urls=qualify_post(state,key)
        except ChromeSessionError as error:
            if str(error)=='x_login_required':self.store.require_relogin('x')
            raise
        root=work_dir/'x-media'
        if root.exists():shutil.rmtree(root)
        root.mkdir(parents=True)
        members=[]
        try:
            for index,url in enumerate(urls,1):
                member_id=f'image-{index}';path=root/member_id
                self.downloader(url,path)
                members.append(self.verifier(path,member_id,'image'))
            if connection_authority(self.store.connection('x'))!=authority:raise ChromeSessionError('x_connection_changed')
        except XiaohongshuSourceError as error:
            shutil.rmtree(root)
            raise XPostSourceError('x_media_invalid') from error
        except Exception:
            shutil.rmtree(root);raise
        user=post.get('core',{}).get('user_results',{}).get('result',{})
        metadata={'note_kind':'normal','source_title':'','original_description':text,
            'scope':'PRIMARY_PAYLOAD_ONLY/NESTED_EXCLUDED','captured_at':datetime.now(UTC).isoformat(),
            'session_authority':authority,'media_members':[m.manifest() for m in members],
            'author':{'display_name':user.get('core',{}).get('name') or user.get('legacy',{}).get('name'),
                      'platform_account_id':user.get('rest_id')},
            'published_at':post['legacy'].get('created_at'),'edit_control':post.get('edit_control')}
        return CapturedNote(key,submitted_url,canonical,metadata,tuple(members),source_kind='x')

    def reuse_retained(self,*,source_key,submitted_url,canonical_url,metadata,work_dir,expected_authority):
        authority=connection_authority(self.store.connection('x'))
        if authority!=expected_authority:raise ChromeSessionError('x_connection_changed')
        if metadata.get('session_authority')!=authority:return None
        try:
            age=(datetime.now(UTC)-datetime.fromisoformat(metadata['captured_at'])).total_seconds()
            if not 0<=age<=72*3600:return None
        except (KeyError,TypeError,ValueError):return None
        members=[]
        for row in metadata.get('media_members',[]):
            if not re.fullmatch(r'image-[1-9][0-9]*',row['member_id']):raise XPostSourceError('x_media_invalid')
            path=work_dir/'x-media'/row['member_id']
            if not path.exists():return None
            if path.is_symlink():raise XPostSourceError('x_media_invalid')
            try:member=self.verifier(path,row['member_id'],'image')
            except XiaohongshuSourceError as error:raise XPostSourceError('x_media_invalid') from error
            if member.manifest()!=row:raise XPostSourceError('x_media_invalid')
            members.append(member)
        return CapturedNote(source_key,submitted_url,canonical_url,metadata,tuple(members),source_kind='x')
