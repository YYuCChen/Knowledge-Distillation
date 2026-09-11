from dataclasses import replace
import json
import sqlite3

import pytest

from knowledge_distiller.v1.collections import Collections, PreviewChanged
from knowledge_distiller.v1.douyin_collections import CollectionError, Scope, Member, connection_authority
from knowledge_distiller.v1.domain import CapturedMaterial,SourceFact,Knowledge,Point,Evidence
from knowledge_distiller.v1.database import connect
from knowledge_distiller.v1.publisher import publish
from knowledge_distiller.v1.store import Store
from knowledge_distiller.v1.worker import SingleWorker


class Discovery:
    def __init__(self,scope):self.scope=scope
    def discover(self,urls,selected=None):return {'scopes':[self.scope],'choices':[]}


@pytest.fixture
def setup(tmp_path):
    store=Store(tmp_path/'isolated.sqlite3');store.initialize();store.save_connection('douyin',None)
    scope=Scope('creator_collection','900','合集','creator',(Member('101','作品一',True,0,'v1'),Member('102','作品二',True,0,'v2')),
                '2026-09-06T00:00:00+00:00',connection_authority(store))
    discovery=Discovery(scope);collections=Collections(store,discovery)
    return store,collections,discovery,tmp_path


def accept(collections):
    preview=collections.preview(['https://www.douyin.com/collection/900'])
    return collections.confirm(preview['token'],[s.signature for s in preview['scopes']])[0],preview


class Model:
    def __init__(self):self.calls=0;self.invalid=False
    def derive_collection(self,basis):
        self.calls+=1
        return {'qualified':True,'title':'集合综合','subtitle':'两个来源的共同认识','summary':'有依据的摘要',
                'points':[{'id':'c1','statement':'具体背景中的共同判断','argument':'保持原有条件',
                           'supports':[{'knowledge_result_id':999999 if self.invalid else item['knowledge_result_id'],'point_id':'p1'} for item in basis]}]}


class Boundary:
    """Coordinator test double; SQLite, worker, evidence and publisher are real."""
    def __init__(self,store,root):
        self.store=store;self.root=root;self.vault=root/'vault';self.vault.mkdir(exist_ok=True)
        self.knowledge_model=Model();self.fail=set();self.wait=set();self.calls=[];self.after=None
    def run(self,item):
        row=self.store.item_bundle(item)
        if row['published_path']:
            self.store.mark_succeeded(item);return
        key=row['submitted_url'].rsplit('/',1)[1];self.calls.append(key)
        if key in self.fail:
            self.store.mark_failed(item,'collecting','temporary');return
        if key in self.wait:
            with connect(self.store.path) as db:
                db.execute("UPDATE distill_items SET state='waiting_user',confirmation_json='{}' WHERE item_id=?",(item,))
            return
        with connect(self.store.path) as db:
            member=db.execute('SELECT * FROM collection_members WHERE item_id=?',(item,)).fetchone()
        path=self.root/(key+'.mp4');path.write_bytes(key.encode())
        metadata={'original_description':'描述'+key,'native_content_version':member['native_version'] if member else 'standalone'}
        material=self.store.attach_material(item,CapturedMaterial('douyin',key,row['submitted_url'],row['submitted_url'],metadata,path,1))
        text='来源正文'+key;fact=self.store.establish_source_fact(material,SourceFact(text))
        self.store.establish_knowledge(fact,Knowledge('标题'+key,'具体副标题','摘要',(Point('p1','原始观点','具体论证',('e1',)),),(),(Evidence('e1',0,len(text),text),)))
        publish(self.store,item,self.vault);self.store.mark_succeeded(item)
        if self.after:self.after(item)


def drain(worker,limit=10):
    for _ in range(limit):
        if worker.run_one() is None:return
    raise AssertionError('collection did not settle')


def test_preview_dismiss_and_drift_never_accept_partial_scope(setup):
    store,c,d,_=setup
    p=c.preview(['url']);assert store.recent_items()==()
    c.dismiss(p['token'])
    with pytest.raises(CollectionError,match='expired'):c.confirm(p['token'],[d.scope.signature])
    p=c.preview(['url']);d.scope=replace(d.scope,members=d.scope.members+(Member('103','新成员',True,0,'v3'),))
    with pytest.raises(PreviewChanged):c.confirm(p['token'],[p['scopes'][0].signature])
    assert c.list()==[] and store.recent_items()==()


def test_confirm_replay_remains_same_work_after_remote_drift_and_restart(setup):
    store,c,d,_=setup;op,p=accept(c)
    d.scope=replace(d.scope,members=(Member('103','新成员',True,0,'v3'),))
    assert c.confirm(p['token'],[])==[op]
    assert Collections(store).confirm(p['token'],[])==[op]
    assert len(c.list())==1


def test_complete_set_creates_separate_combined_result_with_real_lineage(setup):
    store,c,d,root=setup;op,_=accept(c);boundary=Boundary(store,root);drain(SingleWorker(store,boundary))
    result=c.detail(op)
    assert result['state']=='succeeded' and result['consequence']=='complete'
    assert boundary.calls==['101','102'] and boundary.knowledge_model.calls==1
    refs=result['result']['points'][0]['supports'];assert {r['native_id'] for r in refs}=={'101','102'}
    assert all(r['evidence'][0]['text'].startswith('来源正文') for r in refs)
    with connect(store.path) as db:
        assert db.execute('SELECT COUNT(*) FROM source_facts').fetchone()[0]==2
        assert db.execute('SELECT COUNT(*) FROM knowledge_results').fetchone()[0]==2
        assert db.execute('SELECT COUNT(*) FROM collection_results').fetchone()[0]==1
        with pytest.raises(sqlite3.IntegrityError):db.execute("UPDATE collection_operations SET manifest_json='{}'")
        with pytest.raises(sqlite3.IntegrityError):db.execute('DELETE FROM collection_members')
        with pytest.raises(sqlite3.IntegrityError):db.execute("UPDATE collection_results SET payload_json='{}'")


def test_failure_isolated_then_retry_preserves_successful_member(setup):
    store,c,d,root=setup;op,_=accept(c);b=Boundary(store,root);b.fail={'101'};worker=SingleWorker(store,b)
    drain(worker);info=c.detail(op)
    assert info['state']=='partial' and info['result'] is None and b.knowledge_model.calls==0
    success=store.item_bundle(info['members'][1]['item_id'])['knowledge_result_id']
    b.fail.clear();c.resume(op,info['revision']);drain(worker)
    assert c.detail(op)['state']=='succeeded'
    assert b.calls==['101','102','101']
    assert store.item_bundle(info['members'][1]['item_id'])['knowledge_result_id']==success


def test_queued_cancel_and_same_snapshot_submit_do_not_restart(setup):
    store,c,d,root=setup;op,p=accept(c);c.cancel(op,1);b=Boundary(store,root);worker=SingleWorker(store,b)
    assert worker.run_one() is None
    assert c.confirm(p['token'],[])==[op] and c.detail(op)['state']=='cancelled'
    c.resume(op,2);drain(worker);assert c.detail(op)['state']=='succeeded'


def test_active_cancel_finishes_current_boundary_and_preserves_remaining(setup):
    store,c,d,root=setup;op,_=accept(c);b=Boundary(store,root);b.after=lambda item:c.cancel(op,c.detail(op)['revision'])
    worker=SingleWorker(store,b);worker.run_one();info=c.detail(op)
    assert info['state']=='cancelled' and [m['state'] for m in info['members']]==['succeeded','queued']
    assert info['consequence'] is None and info['result'] is None
    b.after=None;c.resume(op,info['revision']);drain(worker)
    assert b.calls==['101','102'] and c.detail(op)['state']=='succeeded'


def test_unsupported_members_stay_visible_and_do_not_enter_retry(setup):
    store,c,d,root=setup;d.scope=replace(d.scope,members=d.scope.members+(Member('103','图文',False,68,'v3'),))
    op,_=accept(c);b=Boundary(store,root);drain(SingleWorker(store,b));info=c.detail(op)
    assert info['state']=='partial' and info['members'][2]['known_unsupported']==1
    assert b.calls==['101','102'] and info['result'] is None
    with pytest.raises(CollectionError,match='nothing_to_retry'):c.resume(op,info['revision'])


def test_invalid_combined_cannot_commit_and_retry_does_not_redo_items(setup):
    store,c,d,root=setup;op,_=accept(c);b=Boundary(store,root);b.knowledge_model.invalid=True;worker=SingleWorker(store,b)
    drain(worker);info=c.detail(op)
    assert info['state']=='failed' and info['consequence']=='complete' and info['result'] is None
    assert info['error_code']=='collection_combined_invalid'
    b.knowledge_model.invalid=False;c.resume(op,info['revision']);drain(worker)
    assert b.calls==['101','102'] and c.detail(op)['state']=='succeeded'


def test_waiting_member_does_not_stop_other_items_or_become_partial(setup):
    store,c,d,root=setup;op,_=accept(c);b=Boundary(store,root);b.wait={'101'};drain(SingleWorker(store,b));info=c.detail(op)
    assert [m['state'] for m in info['members']]==['waiting_user','succeeded']
    assert info['state']=='waiting_user' and info['consequence'] is None
    assert b.knowledge_model.calls==0


def test_restart_keeps_collection_ahead_of_later_standalone(setup):
    store,c,d,root=setup;op,_=accept(c);standalone=store.create_item('https://www.douyin.com/video/999')
    assert store.claim_next_work()==('collection',op)
    first=c.detail(op)['members'][0]['item_id'];store.mark_working(first,'collecting');store.requeue_interrupted()
    assert store.claim_next_work()==('collection',op)
    store.requeue_interrupted();b=Boundary(store,root);drain(SingleWorker(store,b))
    assert b.calls==['101','102','999']


def test_multi_confirmation_keeps_scope_ordinals_and_reports_interrupted_group(setup):
    from knowledge_distiller.v1.collections import PartialConfirmation
    store,c,d,_=setup
    scopes=[d.scope,replace(d.scope,key='901')]
    d.discover=lambda urls,selected=None:{'scopes':scopes,'choices':[]}
    p=c.preview(['url'])
    operations=c.confirm(p['token'],[s.signature for s in scopes])
    assert len(set(operations))==2
    assert Collections(store).confirm(p['token'],[])==operations
    with connect(store.path) as db:
        assert [r[0] for r in db.execute('SELECT ordinal FROM collection_confirmations ORDER BY ordinal')]==[0,1]
    p=c.preview(['url'])
    c._accept(scopes[0],p['token']+':0',2)
    with pytest.raises(PartialConfirmation) as error:
        Collections(store).confirm(p['token'],[])
    assert error.value.operations==operations[:1] and error.value.expected==2


def test_web_preview_confirm_cancel_resume_and_combined_evidence(setup):
    from knowledge_distiller.v1.web import create_app
    store,c,d,root=setup;b=Boundary(store,root)
    app=create_app(store,b,collection_service=c);app.config['TESTING']=True;client=app.test_client()
    response=client.post('/submissions',data={'content':'https://www.douyin.com/collection/900'})
    assert response.status_code==302 and '/collections/preview/' in response.location
    page=client.get(response.location)
    assert page.status_code==200 and '共 2 条' in page.text and c.list()==[]
    token=response.location.rsplit('/',1)[1]
    response=client.post('/collections/confirm',data={'token':token,'signature':c._draft(token)['scopes'][0].signature})
    assert response.status_code==302
    op=c.list()[0]['operation_id'];detail=response.location
    assert client.get(detail).status_code==200 and '等待中' in client.get('/').text
    assert client.post(f'{detail}/cancel',data={'revision':c.detail(op)['revision']}).status_code==302
    assert '已停止' in client.get(detail).text
    assert client.post(f'{detail}/resume',data={'revision':c.detail(op)['revision']}).status_code==302
    drain(SingleWorker(store,b))
    page=client.get(detail)
    assert page.status_code==200 and '来源正文101' in page.text and '集合综合' in page.text
    assert client.get('/').status_code==200
    assert client.get('/static/icons/topic-chevron.svg').status_code==200


def test_web_short_single_and_same_topic_authorization(setup):
    from knowledge_distiller.v1.web import create_app
    store,c,d,root=setup;app=create_app(store,Boundary(store,root),collection_service=c)
    client=app.test_client()
    d.discover=lambda urls,selected=None:{'scopes':[],'choices':[],'single_url':'https://www.douyin.com/video/123'}
    response=client.post('/submissions',data={'content':'https://v.douyin.com/short/'})
    assert response.status_code==302 and store.item_bundle(1)['state']=='queued' and c.list()==[]
    d.scope=replace(d.scope,kind='same_topic')
    d.discover=lambda urls,selected=None:{'scopes':[d.scope],'choices':[]}
    response=client.post('/submissions',data={'content':'https://www.douyin.com/video/101 https://www.douyin.com/video/102', 'processing_mode':'same_topic'})
    token=response.location.rsplit('/',1)[1]
    assert '我确认这些作品属于同一话题' in client.get(response.location).text
    data={'token':token,'signature':c._draft(token)['scopes'][0].signature}
    assert client.post('/collections/confirm',data=data).status_code==400 and c.list()==[]
    data['same_topic']='yes'
    assert client.post('/collections/confirm',data=data).status_code==302 and len(c.list())==1


def test_cancel_during_combined_generation_preserves_intent_on_model_failure(setup):
    store,c,d,root=setup;op,_=accept(c);b=Boundary(store,root);worker=SingleWorker(store,b)
    worker.run_one();worker.run_one()
    def fail(basis):
        info=c.detail(op)
        assert info['state']=='working'
        c.cancel(op,info['revision'])
        raise RuntimeError('model interrupted')
    b.knowledge_model.derive_collection=fail
    worker.run_one()
    assert c.detail(op)['state']=='cancelled' and c.detail(op)['result'] is None
    assert all(m['state']=='succeeded' for m in c.detail(op)['members'])


def test_changed_member_cannot_retry_within_frozen_scope(setup):
    store,c,d,root=setup;op,_=accept(c)
    member=c.detail(op)['members'][0]
    store.mark_failed(member['item_id'],'collecting','collection_member_changed')
    with pytest.raises(ValueError,match='重新投递'):
        store.retry_item(member['item_id'])
    assert store.item_bundle(member['item_id'])['state']=='failed'


def test_stop_at_last_member_boundary_can_resume_only_combined(setup):
    store,c,d,root=setup;op,_=accept(c);b=Boundary(store,root);worker=SingleWorker(store,b)
    worker.run_one()
    b.after=lambda item:c.cancel(op,c.detail(op)['revision'])
    worker.run_one()
    info=c.detail(op)
    assert info['state']=='cancelled' and info['consequence']=='complete'
    c.resume(op,info['revision']);worker.run_one()
    assert c.detail(op)['state']=='succeeded' and b.calls==['101','102']


def test_first_post_reports_partial_acceptance_instead_of_zero_submission(setup):
    from knowledge_distiller.v1.web import create_app
    store,c,d,root=setup
    scopes=[d.scope,replace(d.scope,key='901',members=(Member('103','图文',False,68,'v3'),))]
    d.discover=lambda urls,selected=None:{'scopes':scopes,'choices':[]}
    p=c.preview(['url']);client=create_app(store,Boundary(store,root),collection_service=c).test_client()
    response=client.post('/collections/confirm',data={'token':p['token'],'signature':[s.signature for s in scopes]})
    assert response.status_code==409 and '已接收 1 / 2 个合集' in response.text
    assert len(c.list())==1


def test_profile_selection_and_preview_cancel_create_no_formal_work(setup):
    from knowledge_distiller.v1.web import create_app
    store,c,d,root=setup
    def discover(urls,selected=None):
        return {'scopes':[d.scope] if selected else [],'choices':[{'key':'900','title':'测试合集'}],'profile_title':'测试作者'}
    d.discover=discover;client=create_app(store,Boundary(store,root),collection_service=c).test_client()
    draft = '  https://www.douyin.com/user/creator\n\n'
    response=client.post('/submissions',data={'content':draft})
    page=client.get(response.location)
    assert all(label in page.text for label in ('主页全部内容','所有合集','选择合集'))
    token=response.location.rsplit('/',1)[1]
    response=client.post('/collections/select',data={'token':token,'mode':'selected','collection':'900'})
    assert '共 2 条' in client.get(response.location).text
    token=response.location.rsplit('/',1)[1]
    cancelled = client.post('/collections/dismiss',data={'token':token})
    assert cancelled.status_code == 200
    assert '>' + draft + '</textarea>' in cancelled.text
    with pytest.raises(CollectionError):
        c._draft(token)
    assert c.list()==[] and store.recent_items()==()


def test_empty_collections_are_visible_but_create_no_operations(setup):
    from knowledge_distiller.v1.web import create_app
    store,c,d,root=setup
    def discover(urls,selected=None):
        return {'scopes':[d.scope] if selected else [],
                'choices':[{'key':'900','title':'有内容','member_count':2},
                           {'key':'901','title':'空的合集','member_count':0}],
                'empty_collections':[{'key':'901','title':'空的合集'}] if selected else [],
                'profile_title':'测试作者'}
    d.discover=discover
    client=create_app(store,Boundary(store,root),collection_service=c).test_client()
    response=client.post('/submissions',data={'content':'https://www.douyin.com/user/creator'})
    page=client.get(response.location)
    assert '空的合集（暂无内容）' in page.text and 'data-empty="true"' in page.text
    token=response.location.rsplit('/',1)[1]
    response=client.post('/collections/select',data={'token':token,'mode':'all_collections'})
    assert '本次不创建任务：空的合集' in client.get(response.location).text
    token=response.location.rsplit('/',1)[1]
    assert client.post('/collections/confirm',data={'token':token,'signature':c._draft(token)['scopes'][0].signature}).status_code==302
    assert [x['source_key'] for x in c.list()]==['900']
    assert len(c.detail(c.list()[0]['operation_id'])['members'])==2


def test_empty_collection_gaining_content_requires_new_confirmation(setup):
    store,c,d,_=setup
    empty={'key':'901','title':'原本为空'}
    d.discover=lambda urls,selected=None:{'scopes':[d.scope],'choices':[], 'empty_collections':[empty]}
    preview=c.preview(['url'],selected=['all_collections'])
    added=replace(d.scope,key='901',members=(Member('103','新作品',True,0,'v1'),))
    d.discover=lambda urls,selected=None:{'scopes':[d.scope,added],'choices':[], 'empty_collections':[]}
    with pytest.raises(PreviewChanged) as error:
        c.confirm(preview['token'],[d.scope.signature])
    assert len(error.value.preview['scopes'])==2
    assert c.list()==[] and store.recent_items()==()



def test_switching_to_selected_keeps_empty_collection_disabled():
    import subprocess
    from pathlib import Path
    script=Path(__file__).parents[2]/'src/knowledge_distiller/v1/static/collections.js'
    program="""
const fs=require('fs'),vm=require('vm');
const handlers={};
const inputs=[{disabled:true,dataset:{}},{disabled:true,dataset:{empty:'true'}}];
const document={addEventListener:(name,fn)=>handlers[name]=fn,querySelectorAll:()=>inputs};
vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),{document});
handlers.change({target:{name:'mode',value:'selected'}});
if(inputs[0].disabled || !inputs[1].disabled) throw Error('empty choice became selectable');
handlers.change({target:{name:'mode',value:'all_collections'}});
if(inputs.some(input=>!input.disabled)) throw Error('choices enabled outside selected mode');
"""
    subprocess.run(['node','-e',program,str(script)],check=True,capture_output=True,text=True)


def test_same_topic_finishes_each_source_without_calling_synthesis(setup):
    store,c,d,root=setup
    d.scope=replace(d.scope,kind='same_topic')
    preview=c.preview(['https://www.douyin.com/video/101','https://www.douyin.com/video/102'])
    operation=c.confirm(preview['token'],[d.scope.signature],same_topic=True)[0]
    boundary=Boundary(store,root)
    drain(SingleWorker(store,lambda:boundary))
    info=c.detail(operation)
    assert info['state']=='succeeded'
    assert info['result'] is None and boundary.knowledge_model.calls==0
    assert all(store.item_bundle(m['item_id'])['published_path'] for m in info['members'])
    with connect(store.path) as db:
        assert db.execute('SELECT COUNT(*) FROM collection_results').fetchone()[0]==0
