from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from html.parser import HTMLParser
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid5

from .chrome import ChromeSessionError
from .opencli_session import read_opencli
from .xiaohongshu import CapturedNote


class ZhihuSourceError(RuntimeError):
    pass


def zhihu_identity(value):
    try:
        url=urlsplit(value.strip())
        if url.scheme not in {'http','https'} or url.username or url.password or url.port:raise ValueError
        if url.hostname in {'zhihu.com','www.zhihu.com'}:
            match=re.fullmatch(r'/(?:question/([0-9]+)/)?answer/([0-9]+)/?',url.path)
            if match:return 'answer',match[2],match[1]
            match=re.fullmatch(r'/pin/([0-9]+)/?',url.path)
            if match:return 'pin',match[1],None
        if url.hostname=='zhuanlan.zhihu.com':
            match=re.fullmatch(r'/p/([0-9]+)/?',url.path)
            if match:return 'article',match[1],None
        raise ValueError
    except ValueError as error:
        raise ValueError('请提交一条知乎回答、文章或想法的完整链接。') from error


def connection_authority(row):
    if row is None or row['state']=='unconfigured' or not row['browser_context']:
        raise ChromeSessionError('zhihu_not_configured')
    if row['state']!='connected':raise ChromeSessionError('zhihu_login_required')
    context=row['browser_context']
    instance=uuid5(NAMESPACE_URL,f"zhihu:{row['connected_at']}:{row['generation']}:{context}").hex
    return {'class':'EXISTING_BROWSER_OWNED','connection_instance_id':instance,
            'session_authority_ref':f'browser:{instance}','generation':row['generation'],
            'connected_at':row['connected_at'],'browser_context':context}


class ZhihuSession:
    def read(self,url,context=None):return read_opencli('zhihu','zhihu',url,context)
    def verify(self):return self.read('https://www.zhihu.com/')['contextId']


class _TextOnlyHTML(HTMLParser):
    BLOCKS={'p','div','section','article','h1','h2','h3','h4','h5','h6','blockquote','li','ul','ol','tr','pre','figcaption'}
    EXCLUDED={'script','style','video','audio','iframe','object','svg','canvas'}
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts=[];self.excluded=[]
    def handle_starttag(self,tag,attrs):
        if self.excluded:
            if tag in self.EXCLUDED:self.excluded.append(tag)
            return
        if tag in self.EXCLUDED:self.excluded.append(tag);return
        if tag in self.BLOCKS or tag=='br':self.parts.append('\n')
        elif tag in {'td','th'}:self.parts.append('\t')
    def handle_endtag(self,tag):
        if self.excluded:
            if tag==self.excluded[-1]:self.excluded.pop()
            return
        if tag in self.BLOCKS:self.parts.append('\n')
    def handle_data(self,data):
        if not self.excluded:self.parts.append(data)


def primary_text(html):
    if not isinstance(html,str):raise ZhihuSourceError('zhihu_text_incomplete')
    parser=_TextOnlyHTML()
    parser.feed(html);parser.close()
    if parser.excluded:raise ZhihuSourceError('zhihu_text_incomplete')
    value=''.join(parser.parts)
    # Preserve authored whitespace within blocks; only collapse structural gaps.
    value=re.sub(r'\n{3,}','\n\n',value).strip('\n')
    if not value.strip():raise ZhihuSourceError('zhihu_text_incomplete')
    return value


def qualify_zhihu(state,kind,key,question_id=None):
    if state.get('loggedIn') is not True:raise ChromeSessionError('zhihu_login_required')
    if state.get('requestedId')!=key or state.get('kind')!=kind:raise ZhihuSourceError('zhihu_identity_mismatch')
    try:data=json.loads(state['body'])
    except (ValueError,TypeError,KeyError) as error:raise ZhihuSourceError('zhihu_snapshot_unknown') from error
    if state.get('format') == 'initial_state':
        try:
            resolved_kind,resolved_key,_=zhihu_identity(state['pageUrl'])
            if (resolved_kind,resolved_key)!=(kind,key) or kind!='article':raise ValueError
            data=data['initialState']['entities']['articles'][key]
        except (ValueError,KeyError,TypeError) as error:
            raise ZhihuSourceError('zhihu_identity_mismatch') from error
    if not isinstance(data,dict) or str(data.get('id'))!=key or data.get('type')!=kind:
        raise ZhihuSourceError('zhihu_identity_mismatch')
    if data.get('error') or data.get('content_need_truncated') or data.get('contentNeedTruncated') or data.get('is_deleted'):
        raise ZhihuSourceError('zhihu_text_incomplete')
    native=data.get('content')
    if kind=='pin':
        if data.get('source_pin_id') not in {None,0,'0',''} or data.get('is_guide_app'):
            raise ZhihuSourceError('zhihu_snapshot_unknown')
        if not isinstance(native,list):raise ZhihuSourceError('zhihu_text_incomplete')
        blocks=[]
        for member in native:
            if not isinstance(member,dict):raise ZhihuSourceError('zhihu_snapshot_unknown')
            if member.get('type')=='text':blocks.append(primary_text(member.get('content')))
            elif member.get('type') not in {'image','video','link','quote'}:
                raise ZhihuSourceError('zhihu_snapshot_unknown')
        text='\n\n'.join(blocks)
    else:text=primary_text(native)
    if not text.strip():raise ZhihuSourceError('zhihu_text_incomplete')
    if kind=='answer':
        question=data.get('question') or {}
        qid=str(question.get('id') or '')
        if not re.fullmatch(r'[0-9]+',qid) or (question_id is not None and qid!=question_id):
            raise ZhihuSourceError('zhihu_identity_mismatch')
        canonical=f'https://www.zhihu.com/question/{qid}/answer/{key}'
        title=question.get('title')
    elif kind=='article':
        title=data.get('title')
        if not isinstance(title,str) or not title.strip():raise ZhihuSourceError('zhihu_text_incomplete')
        text=title+'\n\n'+text
        canonical=f'https://zhuanlan.zhihu.com/p/{key}'
    else:
        title=None;canonical=f'https://www.zhihu.com/pin/{key}'
    return data,text,title,canonical


class ZhihuSource:
    def __init__(self,store,session=None):self.store=store;self.session=session or ZhihuSession()

    def capture(self,submitted_url,work_dir,*,expected_authority):
        kind,key,question=zhihu_identity(submitted_url)
        authority=connection_authority(self.store.connection('zhihu'))
        if authority!=expected_authority:raise ChromeSessionError('zhihu_connection_changed')
        try:
            state=self.session.read(submitted_url,authority['browser_context'])
            if state.get('contextId')!=authority['browser_context']:raise ChromeSessionError('zhihu_connection_changed')
            data,text,title,canonical=qualify_zhihu(state,kind,key,question)
        except ChromeSessionError as error:
            if str(error)=='zhihu_login_required':self.store.require_relogin('zhihu')
            raise
        if connection_authority(self.store.connection('zhihu'))!=authority:raise ChromeSessionError('zhihu_connection_changed')
        metadata={'note_kind':'normal','item_kind':kind,'platform_item_id':key,'source_title':title or '',
            'original_description':text,'native_content':data['content'],'media_members':[],
            'scope':'TEXT_ONLY/VISUAL_EXCLUDED','captured_at':datetime.now(UTC).isoformat(),'session_authority':authority,
            'author':{'display_name':(data.get('author') or {}).get('name')},
            'published_at':data.get('created_time') or data.get('created'),'updated_at':data.get('updated_time') or data.get('updated')}
        return CapturedNote(kind+':'+key,submitted_url,canonical,metadata,(),source_kind='zhihu')

    def reuse_retained(self,*,source_key,submitted_url,canonical_url,metadata,work_dir,expected_authority):
        authority=connection_authority(self.store.connection('zhihu'))
        if authority!=expected_authority:raise ChromeSessionError('zhihu_connection_changed')
        if metadata.get('session_authority')!=authority:return None
        try:
            age=(datetime.now(UTC)-datetime.fromisoformat(metadata['captured_at'])).total_seconds()
            if not 0<=age<=72*3600:return None
        except (KeyError,TypeError,ValueError):return None
        return CapturedNote(source_key,submitted_url,canonical_url,metadata,(),source_kind='zhihu')
