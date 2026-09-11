from pathlib import Path
import pytest
from knowledge_distiller.v1.bilibili import bilibili_identity, qualify, BilibiliSourceError

KEY='BV1TEST00001'
@pytest.mark.parametrize('tail,key',[('/',KEY),('/?vd_source=tracking&spm_id_from=x',KEY),('/?p=2&vd_source=x',KEY+'_p2')])
def test_identity_ignores_sharing_params_but_preserves_part(tail,key):
    assert bilibili_identity('https://www.bilibili.com/video/'+KEY+tail)[0]==key

@pytest.mark.parametrize('url',[
    'https://www.bilibili.com/video/'+KEY+'/?p=0',
    'https://www.bilibili.com/video/'+KEY+'/?p=1&p=2',
    'https://www.bilibili.com.evil.test/video/'+KEY,
    'https://user:secret@www.bilibili.com/video/'+KEY,
])
def test_invalid_or_ambiguous_identity(url):
    with pytest.raises(ValueError):bilibili_identity(url)

@pytest.mark.parametrize('change,code',[
    ({'_type':'playlist','entries':[]},'scope_required'),
    ({'id':KEY+'_p2'},'identity_mismatch'),
    ({'duration':float('nan')},'incomplete'),
    ({'duration':True},'incomplete'),
    ({'is_preview':True},'incomplete'),
    ({'is_live':True},'incomplete'),
])
def test_refuses_wrong_part_partial_and_scope(change,code):
    with pytest.raises(BilibiliSourceError,match='bilibili_'+code):
        qualify({'id':KEY,'duration':257,**change},KEY)


def test_web_uses_bilibili_capture_not_douyin(monkeypatch,tmp_path):
    from knowledge_distiller.v1.web import create_app
    from knowledge_distiller.v1.store import Store
    from knowledge_distiller.v1 import bilibili
    seen=[]
    def extract(url):
        seen.append(url)
        return {'id':KEY,'title':'真实标题字段','duration':257},None
    monkeypatch.setattr(bilibili,'extract_bilibili',extract)
    store=Store(tmp_path/'isolated.sqlite3');client=create_app(store,object()).test_client()
    response=client.post('/submissions',data={'content':f'https://www.bilibili.com/video/{KEY}/?vd_source=x'})
    assert response.status_code==302 and len(seen)==1
    row=store.item_bundle(1)
    assert row['submitted_url']==f'https://www.bilibili.com/video/{KEY}/'
    assert row['submitted_title']=='真实标题字段'


def video(key=KEY, **changes):
    return {'id':key,'duration':257,'title':'节目标题','cid':9001,**changes}


def playlist(*entries, **changes):
    return {'_type':'playlist','id':KEY,'title':'多成员范围','entries':list(entries),
            '_kd_complete':True,**changes}


def branch(cid, edge):
    import json
    return video(KEY+'_'+str(cid), cid=cid, graph_version=9,
        description=json.dumps({str(edge):{'cid':cid,'title':'原生分支','choices':[]}})+'\n作者原文')


def test_discovery_preserves_parts_and_public_authority():
    from knowledge_distiller.v1.bilibili import BilibiliDiscovery
    data=playlist(video(KEY+'_p1'),video(KEY+'_p2',cid=9002))
    result=BilibiliDiscovery(lambda url:(data,None)).discover([f'https://www.bilibili.com/video/{KEY}/'])
    scope=result['scopes'][0]
    assert scope.kind=='bilibili_range' and result['single_url'] is None
    assert [m.item_id for m in scope.members]==[KEY+'_p1',KEY+'_p2']
    assert scope.members[1].url==f'https://www.bilibili.com/video/{KEY}/?p=2'
    assert scope.authority=={'platform':'bilibili','class':'PUBLIC'}


def test_interactive_cids_with_identical_page_url_remain_distinct():
    from knowledge_distiller.v1.bilibili import BilibiliDiscovery
    data=playlist(branch(100,1),branch(200,2))
    result=BilibiliDiscovery(lambda url:(data,None)).discover([f'https://www.bilibili.com/video/{KEY}/'])
    members=result['scopes'][0].members
    assert [m.item_id for m in members]==[KEY+'_100',KEY+'_200']
    assert members[1].url==f'https://www.bilibili.com/video/{KEY}/#kd-source={KEY}_200'


@pytest.mark.parametrize('data,code',[
    (playlist(video(),_kd_complete=False),'incomplete'),
    (playlist(video(),playlist_count=2),'incomplete'),
    (playlist(video(),None),'incomplete'),
    (playlist(),'empty'),
    (playlist(video(),video(title='不一致的同一身份')),'scope_changed'),
])
def test_discovery_rejects_partial_empty_and_conflicting_ranges(data,code):
    from knowledge_distiller.v1.bilibili import BilibiliDiscovery
    with pytest.raises(BilibiliSourceError,match='bilibili_'+code):
        BilibiliDiscovery(lambda url:(data,None)).discover([f'https://www.bilibili.com/video/{KEY}/'])


def test_range_limit_is_failure_not_silent_subset(monkeypatch):
    from knowledge_distiller.v1 import bilibili
    monkeypatch.setattr(bilibili,'MAX_MEMBERS',2)
    data=playlist(video(KEY+'_p1'),video(KEY+'_p2'),video(KEY+'_p3'))
    with pytest.raises(BilibiliSourceError,match='range_too_large'):
        bilibili.BilibiliDiscovery(lambda url:(data,None)).discover([f'https://www.bilibili.com/video/{KEY}/'])


def test_download_selects_verified_cid_not_first_entry(monkeypatch,tmp_path):
    from knowledge_distiller.v1 import bilibili
    data=playlist(branch(100,1),branch(200,2));requests=[]
    media=tmp_path/'selected.m4a';media.write_bytes(b'media')
    def worker(request,timeout):
        requests.append(request)
        if 'url' in request:return {'info':data}
        return {'info':request['leaf'],'media':str(media)}
    monkeypatch.setattr(bilibili,'_run_worker',worker)
    info,path=bilibili.extract_bilibili(f'https://www.bilibili.com/video/{KEY}/#kd-source={KEY}_200',work_dir=tmp_path)
    assert info['id']==KEY+'_200' and path==media
    assert len(requests)==2 and requests[1]['leaf']['id']==KEY+'_200'
    assert 'entries' not in requests[1]['leaf']


def test_unknown_cid_never_downloads_any_member(monkeypatch,tmp_path):
    from knowledge_distiller.v1 import bilibili
    requests=[]
    def worker(request,timeout):requests.append(request);return {'info':playlist(branch(100,1))}
    monkeypatch.setattr(bilibili,'_run_worker',worker)
    with pytest.raises(BilibiliSourceError,match='identity_mismatch'):
        bilibili.extract_bilibili(f'https://www.bilibili.com/video/{KEY}/#kd-source={KEY}_200',work_dir=tmp_path)
    assert len(requests)==1 and not list(tmp_path.iterdir())


def test_unselected_range_never_downloads_first_video(monkeypatch,tmp_path):
    from knowledge_distiller.v1 import bilibili
    requests=[]
    def worker(request,timeout):requests.append(request);return {'info':playlist(video(KEY+'_p1'),video(KEY+'_p2'))}
    monkeypatch.setattr(bilibili,'_run_worker',worker)
    with pytest.raises(BilibiliSourceError,match='scope_required'):
        bilibili.extract_bilibili(f'https://www.bilibili.com/video/{KEY}/',work_dir=tmp_path)
    assert len(requests)==1


def test_capture_identity_version_and_branch_locator_match_discovery(monkeypatch,tmp_path):
    from knowledge_distiller.v1 import bilibili
    info=branch(200,2)
    member=bilibili.BilibiliDiscovery(lambda url:(info,None)).discover([bilibili.member_url(KEY+'_200')])['scopes'][0].members[0]
    media=tmp_path/'source.m4a';media.write_bytes(b'media')
    audio=tmp_path/'standard.wav';audio.write_bytes(b'audio')
    monkeypatch.setattr(bilibili,'normalize_downloaded_audio',lambda *args:(audio,257))
    captured=bilibili.BilibiliSource(downloader=lambda url,**kw:(info,media)).capture(member.url,tmp_path)
    assert captured.source_key==member.item_id
    assert captured.metadata['native_content_version']==member.version
    assert captured.canonical_url==f'https://www.bilibili.com/video/{KEY}/'
    assert captured.metadata['original_description']=='作者原文'
    assert captured.metadata['interactive_branch']['edges']['2']['cid']==200
    assert captured.metadata['interactive_branch']['platform_deep_link_available'] is False


def test_content_version_ignores_volatile_urls_but_tracks_native_changes():
    from knowledge_distiller.v1.bilibili import content_version
    baseline=content_version(video())
    assert content_version(video(formats=[{'url':'https://cdn.test/a?token=new'}],view_count=99))==baseline
    assert content_version(video(cid=9002))!=baseline
    assert content_version(video(description='新正文'))!=baseline


@pytest.mark.parametrize('kind,expected',[('BiliBiliBangumi','bangumi_ep21495'),('BilibiliCheese','cheese_ep21495')])
def test_episode_namespaces_do_not_collide(kind,expected):
    from knowledge_distiller.v1.bilibili import member_url
    assert qualify(video('21495',extractor_key=kind))==expected
    assert '/'+expected.split('_')[0]+'/play/ep21495' in member_url(expected)


def test_av_resolves_to_bv_canonical_identity(monkeypatch,tmp_path):
    from knowledge_distiller.v1 import bilibili
    audio=tmp_path/'audio.wav';audio.write_bytes(b'audio')
    monkeypatch.setattr(bilibili,'normalize_downloaded_audio',lambda *args:(audio,257))
    captured=bilibili.BilibiliSource(downloader=lambda url,**kw:(video(),audio)).capture('https://www.bilibili.com/video/av123',tmp_path)
    assert captured.source_key==KEY and captured.canonical_url==bilibili.member_url(KEY)


@pytest.mark.parametrize('url',[
    'https://www.bilibili.com/bangumi/play/ep21495/',
    'https://www.bilibili.com/bangumi/play/ss26801',
    'https://www.bilibili.com/bangumi/media/md24097891',
    'https://www.bilibili.com/cheese/play/ss5918',
    'https://space.bilibili.com/2142762/lists/3662502?type=season',
    'https://space.bilibili.com/84912/favlist?fid=1103407912&ftype=create',
    'https://www.bilibili.com/list/1958703906?sid=547718',
    'https://space.bilibili.com/3985676/video',
])
def test_native_range_routes_are_accepted(url):
    assert bilibili_identity(url)[1].startswith('https://')


def test_metadata_process_deadline_is_typed_and_has_no_browser_credentials(monkeypatch):
    import subprocess
    from knowledge_distiller.v1 import bilibili
    killed=[]
    class Process:
        pid=12345
        calls=0
        def communicate(self,data=None,timeout=None):
            self.calls+=1
            if self.calls==1:
                assert timeout==3 and 'cookies' not in data
                raise subprocess.TimeoutExpired('owned worker',3)
            return '', ''
    def popen(command,**kwargs):
        assert kwargs['start_new_session'] is True
        return Process()
    monkeypatch.setattr(bilibili.subprocess,'Popen',popen)
    monkeypatch.setattr(bilibili.os,'killpg',lambda pid,sig:killed.append(pid))
    with pytest.raises(BilibiliSourceError,match='bilibili_timeout'):
        bilibili._run_worker({'url':f'https://www.bilibili.com/video/{KEY}/'},3)
    assert killed==[12345]


@pytest.mark.parametrize('message,code',[
    ('HTTP Error 412: signed url secret','rate_limited'),
    ('You need to purchase the course','payment_required'),
    ('Login required','login_required'),
    ('This video may be deleted or geo-restricted','unavailable'),
])
def test_upstream_failures_are_precise_and_do_not_expose_response(message,code):
    from knowledge_distiller.v1.bilibili import _error_code
    assert _error_code(RuntimeError(message))=='bilibili_'+code


def test_course_season_does_not_silently_omit_paid_members(monkeypatch):
    import yt_dlp
    from yt_dlp.extractor import bilibili as upstream
    from knowledge_distiller.v1.bilibili import _extract_worker
    class FakeYDL:
        def __init__(self,options):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def extract_info(self,*args,**kwargs):
            return list(upstream.BilibiliCheeseSeasonIE._get_cheese_entries(None,{'episodes':[
                {'id':1,'episode_can_view':True,'playable':True},
                {'id':2,'episode_can_view':False,'playable':False}]}))
    monkeypatch.setattr(yt_dlp,'YoutubeDL',FakeYDL)
    with pytest.raises(BilibiliSourceError,match='payment_required'):
        _extract_worker({'url':'https://www.bilibili.com/cheese/play/ss5918'})


def test_pagelist_failure_cannot_masquerade_as_single_video(monkeypatch):
    import yt_dlp
    from yt_dlp.extractor import bilibili as upstream
    from knowledge_distiller.v1.bilibili import _extract_worker
    monkeypatch.setattr(upstream.BilibiliBaseIE,'_download_json',lambda *args,**kwargs:None)
    class FakeYDL:
        def __init__(self,options):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def extract_info(self,*args,**kwargs):
            upstream.BilibiliBaseIE._download_json(None,'https://api.bilibili.com/x/player/pagelist',KEY,query={'bvid':KEY})
            return video()
    monkeypatch.setattr(yt_dlp,'YoutubeDL',FakeYDL)
    with pytest.raises(BilibiliSourceError,match='incomplete'):
        _extract_worker({'url':f'https://www.bilibili.com/video/{KEY}/'})


def test_paginated_range_refuses_missing_entries(monkeypatch):
    import yt_dlp
    from yt_dlp.extractor import bilibili as upstream
    from knowledge_distiller.v1.bilibili import _extract_worker
    class FakeYDL:
        def __init__(self,options):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def extract_info(self,*args,**kwargs):
            pages=[{'page':{'total':3},'rows':[video(KEY+'_p1'),video(KEY+'_p2')]},
                   {'page':{'total':3},'rows':[]}]
            _,entries=upstream.BilibiliSpaceBaseIE._extract_playlist(None,
                lambda n:pages[n],lambda p:{'page_count':2,'page_size':2},lambda p:p['rows'])
            return playlist(*entries)
    monkeypatch.setattr(yt_dlp,'YoutubeDL',FakeYDL)
    with pytest.raises(BilibiliSourceError,match='incomplete'):
        _extract_worker({'url':'https://space.bilibili.com/3985676/video'})


def test_bangumi_range_keeps_extra_sections(monkeypatch):
    import yt_dlp
    from yt_dlp.extractor import bilibili as upstream
    from knowledge_distiller.v1.bilibili import _extract_worker
    recorded=[]
    class Extractor:
        def _download_json(self,*args,**kwargs):
            return {'result':{'main_section':{'episodes':[{'id':1}]},
                              'section':[{'episodes':[{'id':2},{'id':1}]}]}}
        def url_result(self,url,ie,identity):recorded.append(identity);return url
    class FakeYDL:
        def __init__(self,options):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def sanitize_info(self,info):return info
        def extract_info(self,*args,**kwargs):
            list(upstream.BilibiliBaseIE._get_episodes_from_season(Extractor(),'1','https://www.bilibili.com/bangumi/play/ss1'))
            return video()
    monkeypatch.setattr(yt_dlp,'YoutubeDL',FakeYDL)
    _extract_worker({'url':'https://www.bilibili.com/bangumi/play/ss1'})
    assert recorded==['1','2']


@pytest.mark.parametrize('warning',[
    'Only preview format is available, you have to become a premium member to access full video.',
    'This is a supporter-only video, only the preview will be extracted.',
])
def test_playable_preview_is_not_accepted_as_full_source(monkeypatch,warning):
    import yt_dlp
    from knowledge_distiller.v1.bilibili import _extract_worker
    class FakeYDL:
        def __init__(self,options):self.log=options['logger']
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def extract_info(self,*args,**kwargs):self.log.warning(warning);return video()
    monkeypatch.setattr(yt_dlp,'YoutubeDL',FakeYDL)
    with pytest.raises(BilibiliSourceError,match='payment_required'):
        _extract_worker({'url':f'https://www.bilibili.com/video/{KEY}/'})


def test_episode_version_uses_media_cid_not_just_episode_number(monkeypatch):
    import yt_dlp
    from yt_dlp.extractor import bilibili as upstream
    from knowledge_distiller.v1.bilibili import _extract_worker
    monkeypatch.setattr(upstream.BilibiliBaseIE,'_download_json',lambda *a,**kw:{'code':0,'data':{'episodes':[{'id':229832,'cid':1277571373}]}})
    class FakeYDL:
        def __init__(self,options):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def sanitize_info(self,info):return info
        def extract_info(self,*args,**kwargs):
            upstream.BilibiliBaseIE._download_json(None,'https://api.bilibili.com/pugv/view/web/season?ep_id=229832','229832')
            return video('229832',cid=None,extractor_key='BilibiliCheese')
    monkeypatch.setattr(yt_dlp,'YoutubeDL',FakeYDL)
    assert _extract_worker({'url':'https://www.bilibili.com/cheese/play/ep229832'})['info']['cid']==1277571373


def test_exact_audio_download_uses_reported_file_not_recomputed_format_name(monkeypatch,tmp_path):
    import yt_dlp
    from knowledge_distiller.v1.bilibili import _extract_worker
    actual=tmp_path/'229832.m4a';actual.write_bytes(b'audio')
    class FakeYDL:
        def __init__(self,options):
            assert options['format']=='bestaudio/best'
            self.hooks=options['progress_hooks']
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def process_ie_result(self,info,download):
            assert download is True
            for hook in self.hooks:hook({'status':'finished','filename':str(actual)})
            return info
        def sanitize_info(self,info):return info
        def prepare_filename(self,info):return str(tmp_path/'229832.f100036.m4a')
    monkeypatch.setattr(yt_dlp,'YoutubeDL',FakeYDL)
    result=_extract_worker({'leaf':video('229832',extractor_key='BilibiliCheese'),'work_dir':str(tmp_path)})
    assert Path(result['media']).is_file() and result['media']==str(actual)


def test_anonymous_wbi_key_is_not_mistaken_for_login_failure(monkeypatch):
    import yt_dlp
    from yt_dlp.extractor import bilibili as upstream
    from knowledge_distiller.v1.bilibili import _extract_worker
    anonymous={'code':-101,'data':{'wbi_img':{'img_url':'public','sub_url':'public'}}}
    monkeypatch.setattr(upstream.BilibiliBaseIE,'_download_json',lambda *a,**kw:anonymous)
    class FakeYDL:
        def __init__(self,options):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def sanitize_info(self,info):return info
        def extract_info(self,*args,**kwargs):
            assert upstream.BilibiliBaseIE._download_json(None,'https://api.bilibili.com/x/web-interface/nav',KEY)==anonymous
            return video()
    monkeypatch.setattr(yt_dlp,'YoutubeDL',FakeYDL)
    assert _extract_worker({'url':f'https://www.bilibili.com/video/{KEY}/'})['info']['id']==KEY


@pytest.mark.parametrize('frozen', [True,False])
def test_worker_argv_supports_frozen_application_without_relaunching_ui(monkeypatch,frozen):
    import json
    from knowledge_distiller.v1 import bilibili
    seen=[]
    class Process:
        returncode=0
        def communicate(self,data,timeout):return json.dumps({'info':video()}),''
    def popen(argv,**kwargs):seen.append(argv);return Process()
    monkeypatch.setattr(bilibili.sys,'frozen',frozen,raising=False)
    monkeypatch.setattr(bilibili.sys,'executable','/app/Knowledge Distiller')
    monkeypatch.setattr(bilibili.subprocess,'Popen',popen)
    bilibili._run_worker({'url':bilibili.member_url(KEY)},3)
    assert seen[0][:2]==['/app/Knowledge Distiller','--bilibili-worker' if frozen else '-c']
    if frozen:assert len(seen[0])==2


def retained(tmp_path, **metadata_changes):
    import hashlib
    from datetime import UTC,datetime
    from knowledge_distiller.v1.bilibili import member_url
    media=tmp_path/'media'/'source.mka';media.parent.mkdir();media.write_bytes(b'verified audio')
    metadata={'native_source_id':KEY,'native_content_version':'a'*64,
              'captured_at':datetime.now(UTC).isoformat(),'audio_sha256':hashlib.sha256(media.read_bytes()).hexdigest(),
              'platform_duration_seconds':257,**metadata_changes}
    return {'source_key':KEY,'submitted_url':member_url(KEY),'canonical_url':member_url(KEY),
            'metadata':metadata,'work_dir':tmp_path}


def test_retry_reuses_verified_local_snapshot_without_downloading(tmp_path):
    from knowledge_distiller.v1.bilibili import BilibiliSource
    calls=[]
    class Verifier:
        def verify(self,path,**kwargs):calls.append(kwargs);return 257
    def download(*args,**kwargs):raise AssertionError('retry must not redownload owned verified audio')
    args=retained(tmp_path)
    captured=BilibiliSource(downloader=download,verifier=Verifier()).reuse_retained(**args)
    assert captured.source_key==KEY and captured.metadata==args['metadata']
    assert calls==[{'expected_duration_seconds':257,'complete_decode':True,'require_video':False}]


@pytest.mark.parametrize('change', ['expired','future','legacy'])
def test_stale_or_unqualified_retained_snapshot_is_not_reused(tmp_path,change):
    from datetime import UTC,datetime,timedelta
    from knowledge_distiller.v1.bilibili import BilibiliSource
    args=retained(tmp_path)
    if change=='legacy':args['metadata'].pop('native_source_id')
    else:args['metadata']['captured_at']=(datetime.now(UTC)+timedelta(hours=-73 if change=='expired' else 1)).isoformat()
    assert BilibiliSource().reuse_retained(**args) is None


@pytest.mark.parametrize('change,code',[('digest','media_invalid'),('identity','identity_mismatch'),('symlink','media_invalid')])
def test_retained_media_tampering_is_reported_not_overwritten(tmp_path,change,code):
    from knowledge_distiller.v1.bilibili import BilibiliSource
    args=retained(tmp_path)
    if change=='digest':args['metadata']['audio_sha256']='0'*64
    elif change=='identity':args['metadata']['native_source_id']=KEY+'_p2'
    else:
        media=tmp_path/'media'/'source.mka';media.unlink();media.symlink_to(tmp_path/'user-missing-original')
    with pytest.raises(BilibiliSourceError,match=code):BilibiliSource().reuse_retained(**args)
