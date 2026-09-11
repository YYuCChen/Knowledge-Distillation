import asyncio
import copy

import pytest

from knowledge_distiller.v1.douyin_collections import CollectionError, DouyinCollections, _pages
from knowledge_distiller.v1.store import Store

PROFILE='test-profile-123'


def video(key='101', **changes):
    d={'aweme_id':key,'aweme_type':0,'desc':'原始标题','duration':10000,'video':{'play_addr':{'uri':'media-'+key}},'mix_info':{'mix_id':'900'}}
    d.update(changes)
    return d


def page(rows, more=0, cursor=0):
    return {'raw': {'status_code':0,'has_more':more,'cursor':cursor,'aweme_list':rows}}


class Client:
    def __init__(self,cookies):pass
    async def __aenter__(self):return self
    async def __aexit__(self,*args):pass
    async def get_video_detail(self,key):return video(key)
    async def get_user_info(self,key):return {'sec_uid':key,'nickname':'作者'}
    async def get_user_mix(self,key,cursor):return {'raw':{'status_code':0,'has_more':0,'mix_infos':[{'mix_id':'900','mix_name':'合集一'},{'mix_id':'901','mix_name':'合集二'}]}}
    async def get_user_post(self,key,cursor):return page([video('101')],1,1) if cursor==0 else page([video('102',images=[{'uri':'image'}],aweme_type=68)])
    async def get_mix_detail(self,key):return {'mix_id':key,'mix_name':'合集'+key,'author':{'sec_uid':PROFILE}}
    async def get_mix_aweme(self,key,cursor):return page([video(key+'1',mix_info={'mix_id':key})])


@pytest.fixture
def adapter(tmp_path):
    store=Store(tmp_path/'isolated.sqlite3');store.initialize();store.save_connection('douyin',None)
    class Session:
        def cookies(self):return {'test':'fixture'}
    return DouyinCollections(store,Session(),Client)


def test_same_topic_is_unique_unordered_set_and_single_returns_single_path(adapter):
    a=adapter.discover(['https://www.douyin.com/video/102','https://www.douyin.com/video/101','https://www.douyin.com/video/101'])['scopes'][0]
    b=adapter.discover(['https://www.douyin.com/video/101','https://www.douyin.com/video/102'])['scopes'][0]
    assert a.signature==b.signature and [m.item_id for m in a.members]==['101','102']
    single=adapter.discover(['https://www.douyin.com/video/101']*2)
    assert single['single_url']=='https://www.douyin.com/video/101'
    assert not adapter.store.recent_items()


def test_profile_selection_and_full_scope_keep_unsupported_members(adapter):
    url='https://www.douyin.com/user/'+PROFILE
    choice=adapter.discover([url]);assert not choice['scopes'] and len(choice['choices'])==2
    full=adapter.discover([url],selected=['full_profile'])['scopes'][0]
    assert [m.item_id for m in full.members]==['101','102']
    assert [m.supported for m in full.members]==[True,False]
    all_scopes=adapter.discover([url],selected=['all_collections'])['scopes']
    assert [s.key for s in all_scopes]==['900','901']
    assert all(s.kind=='creator_collection' for s in all_scopes)
    assert not adapter.store.recent_items()


@pytest.mark.parametrize('raw',[{}, {'status_code':0,'aweme_list':[]},
    {'status_code':0,'has_more':1,'cursor':0,'aweme_list':[video()]},
    {'status_code':0,'has_more':1,'cursor':1,'aweme_list':[]}])
def test_unknown_or_stuck_pagination_never_means_complete(raw):
    async def fetch(cursor):return {'raw':raw}
    with pytest.raises(CollectionError,match='collection_membership_incomplete'):
        asyncio.run(_pages(fetch,keys=['aweme_list']))


def test_collection_members_must_belong_to_exact_mix(adapter):
    class Wrong(Client):
        async def get_mix_aweme(self,key,cursor):return page([video(mix_info={'mix_id':'999'})])
    adapter.client_factory=Wrong
    with pytest.raises(CollectionError,match='collection_identity_mismatch'):
        adapter.discover(['https://www.douyin.com/collection/900'])


def test_native_content_change_affects_version_but_not_membership(adapter):
    urls=['https://www.douyin.com/video/101','https://www.douyin.com/video/102']
    old=adapter.discover(urls)['scopes'][0]
    class Edited(Client):
        async def get_video_detail(self,key):return video(key,desc='已经编辑')
    adapter.client_factory=Edited
    new=adapter.discover(urls)['scopes'][0]
    assert old.signature==new.signature and old.content_signature!=new.content_signature


def test_session_change_while_discovering_invalidates_confirmation(adapter):
    class Reconnected(Client):
        async def get_video_detail(self,key):
            adapter.store.save_connection('douyin',None)
            return video(key)
    adapter.client_factory=Reconnected
    with pytest.raises(CollectionError,match='collection_connection_changed'):
        adapter.discover(['https://www.douyin.com/video/101'])


def test_missing_video_identity_does_not_become_known_unsupported(adapter):
    class Incomplete(Client):
        async def get_video_detail(self,key):return video(key,video={})
    adapter.client_factory=Incomplete
    with pytest.raises(CollectionError,match='collection_membership_incomplete'):
        adapter.discover(['https://www.douyin.com/video/101'])


def test_browser_error_payload_cannot_masquerade_as_native_json():
    from knowledge_distiller.v1.douyin_collection_browser import BrowserCollectionClient
    class Page:
        def call(self,*args,**kwargs):return {'result':{'value':{'message':'fetch failed'}},'exceptionDetails':{'text':'Error'}}
    client=BrowserCollectionClient(object());client.page=Page()
    with pytest.raises(CollectionError,match='collection_upstream_failed'):
        asyncio.run(client.get_mix_aweme('900',0))


def test_empty_page_with_advancing_native_cursor_continues_to_real_end():
    async def fetch(cursor):
        return {0:page([video('101')],1,1),1:page([],1,2),2:page([video('102')],0,3)}[cursor]
    result=asyncio.run(_pages(fetch,keys=['aweme_list']))
    assert [r['aweme_id'] for r in result]==['101','102']


@pytest.mark.parametrize('expected,ids,code', [
    (2, ['101'], 'collection_membership_incomplete'),
    (2, ['101', '101'], 'collection_membership_incomplete'),
    (1, ['101', '102'], 'collection_membership_incomplete'),
    (True, ['101'], 'collection_membership_incomplete'),
])
def test_ended_pagination_must_match_native_published_count(adapter, expected, ids, code):
    class Counted(Client):
        async def get_mix_detail(self, key):
            return {**await super().get_mix_detail(key), 'statis': {'updated_to_episode': expected}}
        async def get_mix_aweme(self, key, cursor):
            return page([video(i, mix_info={'mix_id': key, 'statis': {'updated_to_episode': expected}}) for i in ids])
    adapter.client_factory = Counted
    with pytest.raises(CollectionError, match=code):
        adapter.discover(['https://www.douyin.com/collection/900'])


def test_collection_published_count_changing_between_pages_invalidates_scope(adapter):
    class Changed(Client):
        async def get_mix_detail(self, key):
            return {**await super().get_mix_detail(key), 'statis': {'updated_to_episode': 2}}
        async def get_mix_aweme(self, key, cursor):
            return page([video('101', mix_info={'mix_id': key, 'statis': {'updated_to_episode': 3}})])
    adapter.client_factory = Changed
    with pytest.raises(CollectionError, match='collection_scope_changed'):
        adapter.discover(['https://www.douyin.com/collection/900'])


def test_series_max_cursor_enumerates_from_zero_to_native_end(adapter):
    class Complete(Client):
        async def get_mix_detail(self, key):
            return {**await super().get_mix_detail(key), 'statis': {'updated_to_episode': 2}}
        async def get_mix_aweme(self, key, cursor):
            assert cursor in (0, 1)
            return {'raw': {'status_code': 0, 'has_more': int(cursor == 0), 'max_cursor': cursor + 1,
                'aweme_list': [video(str(101 + cursor), mix_info={'mix_id': key, 'statis': {'updated_to_episode': 2}})]}}
    adapter.client_factory = Complete
    result = adapter.discover(['https://www.douyin.com/collection/900'])['scopes'][0]
    assert [m.item_id for m in result.members] == ['101', '102']


def test_known_empty_collections_remain_visible_and_are_explicitly_excluded(adapter):
    class Counted(Client):
        async def get_user_mix(self, key, cursor):
            return {'raw': {'status_code': 0, 'has_more': 0, 'mix_infos': [
                {'mix_id': '900', 'mix_name': '非空', 'member_count': 1},
                {'mix_id': '901', 'mix_name': '空合集', 'member_count': 0}]}}
        async def get_mix_detail(self, key):
            assert key == '900'
            return await super().get_mix_detail(key)
    adapter.client_factory = Counted
    url = 'https://www.douyin.com/user/' + PROFILE
    result = adapter.discover([url], selected=['all_collections'])
    assert [s.key for s in result['scopes']] == ['900']
    assert result['empty_collections'] == [{'key': '901', 'title': '空合集'}]
    assert [c['member_count'] for c in result['choices']] == [1, 0]
    with pytest.raises(CollectionError, match='collection_empty'):
        adapter.discover([url], selected=['901'])


def test_unknown_collection_counts_are_not_treated_as_empty(adapter):
    result = adapter.discover(['https://www.douyin.com/user/' + PROFILE], selected=['all_collections'])
    assert len(result['scopes']) == 2 and result['empty_collections'] == []
    assert all(c['member_count'] is None for c in result['choices'])


def test_native_explicit_empty_series_list_does_not_hide_other_collections():
    async def fetch(cursor):
        return {'raw': {'status_code': 0, 'has_more': 0, 'total': 0, 'series_infos': None}}
    assert asyncio.run(_pages(fetch, keys=['series_infos'])) == []


@pytest.mark.parametrize('change', [{'total': 1}, {'has_more': 1}, {'total': None}])
def test_null_list_without_explicit_empty_proof_still_fails(change):
    async def fetch(cursor):
        return {'raw': {'status_code': 0, 'has_more': 0, 'total': 0, 'series_infos': None, **change}}
    with pytest.raises(CollectionError, match='collection_membership_incomplete'):
        asyncio.run(_pages(fetch, keys=['series_infos']))


def test_single_gallery_short_link_accepts_live_photo_static_core(adapter):
    class Gallery(Client):
        async def resolve_short_url(self, url):
            return 'https://www.douyin.com/note/1234567890123456789'
        async def get_video_detail(self, key):
            return video(key, images=[{'uri': 'original', 'url_list': ['https://p3.douyinpic.com/original'],
                                      'clip_type': 1, 'video': {'duration': 2033}}])
    adapter.client_factory = Gallery
    result = adapter.discover(['https://v.douyin.com/example/'])
    assert result['single_url']
    assert result['scopes'][0].members[0].supported
    assert not adapter.store.recent_items()


def test_single_gallery_preserves_specific_missing_original_failure(adapter):
    class Broken(Client):
        async def get_video_detail(self, key):
            return video(key, images=[{'uri': 'original', 'video': {'duration': 2033}}])
    adapter.client_factory = Broken
    with pytest.raises(CollectionError, match='douyin_text_invalid'):
        adapter.discover(['https://www.douyin.com/note/101'])
    assert not adapter.store.recent_items()
