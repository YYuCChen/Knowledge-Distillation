import pytest

from knowledge_distiller.growth_modeling import HistoricalRecallAdapter, RelationInsightAdapter
from knowledge_distiller.organization_models import EventStatus
from knowledge_distiller.organization_service import OrganizationStartKind
from knowledge_distiller.v1.database import connect
from knowledge_distiller.v1.file_sources import prepare_direct_text
from knowledge_distiller.v1.organization import build_organization
from tests.test_organization_service import JsonRuntime, SeedTopicIndexer
from tests.fixtures.growth import empty_growth_plan_payload
from .test_topics import library


class StableTopicIndexer(SeedTopicIndexer):
    def organize(self, points, existing_topics):
        if not existing_topics:
            return super().organize(points, existing_topics)
        from knowledge_distiller.topic_indexing import TopicDraft, TopicPlan, TopicIndexing
        members = {p for topic in existing_topics for p in topic.members}
        return TopicIndexing.succeeded(TopicPlan(tuple(TopicDraft(t.topic_id, None, t.name, t.scope, t.members) for t in existing_topics),
            tuple(p.reference for p in points if p.reference not in members)))


def organization(store, *, fail=None, growth=None):
    growth = growth if growth is not None else empty_growth_plan_payload()
    if not growth['new_input_reviews']:
        growth['new_input_reviews'] = [dict(knowledge_result_id=i, outcome='considered_no_formal_result', reason_text='已完整阅读并核对来源。') for i in (1,2)]
    runtime = JsonRuntime(growth)
    return build_organization(store,topic_indexer=StableTopicIndexer(),
        recall_planner=HistoricalRecallAdapter(JsonRuntime({}),expand_all=True),
        relation_insight_planner=RelationInsightAdapter(runtime),failure_injector=fail), runtime


def test_full_organization_covers_sources_and_commits_topic(tmp_path):
    lib,store,worker = library(tmp_path)
    service,runtime = organization(store)
    start=service.start_or_reuse()
    assert start.kind is OrganizationStartKind.STARTED
    assert service.start_or_reuse().event_id == start.event_id
    result=service.drive(start.event_id)
    assert result.event.status is EventStatus.SUCCEEDED
    assert len(lib.snapshot()['topics']) == 1
    assert lib.snapshot()['knowledge_count'] == 2
    assert service.start_or_reuse().kind is OrganizationStartKind.EMPTY
    assert runtime.calls == 1


def test_failure_after_topic_replace_rolls_back_everything(tmp_path):
    lib,store,worker = library(tmp_path)
    injected = []
    def failure(point,connection):
        if point == 'after_topic_replace':
            injected.append(point)
            import sqlite3
            raise sqlite3.IntegrityError('simulated crash before full commit')
    service,_ = organization(store,fail=failure)
    started=service.start_or_reuse()
    result=service.drive(started.event_id)
    assert result.event.status is EventStatus.FAILED
    assert injected == ['after_topic_replace']
    assert lib.snapshot() == {'topics':[],'knowledge_count':0}
    with connect(store.path) as db:
        assert db.execute('SELECT COUNT(*) FROM organization_event_coverages').fetchone()[0] == 0
    assert service.start_or_reuse().kind is OrganizationStartKind.STARTED


def test_later_sources_wait_for_next_organization(tmp_path):
    lib,store,worker = library(tmp_path)
    service,runtime=organization(store)
    started=service.start_or_reuse()
    store.submit_source(prepare_direct_text('后来进入的正文'))
    worker.run_one()
    result=service.drive(started.event_id)
    assert result.event.status is EventStatus.SUCCEEDED
    assert lib.snapshot()['knowledge_count'] == 2
    new=service.start_or_reuse()
    with connect(store.path) as db:
        ids=[row[0] for row in db.execute("SELECT knowledge_result_id FROM organization_event_source_boundary WHERE event_id=? AND boundary_role='frozen_new'", (new.event_id,))]
    assert ids == [3]


def test_worker_resumes_durable_organization(tmp_path):
    from knowledge_distiller.v1.worker import SingleWorker
    from knowledge_distiller.organization_service import read_event
    lib,store,worker = library(tmp_path)
    service,_ = organization(store)
    started = service.start_or_reuse()
    resumed,_ = organization(store)
    new_worker = SingleWorker(store, worker.distiller, organization=resumed)
    assert new_worker.run_organization()
    assert read_event(store.path,started.event_id).status is EventStatus.SUCCEEDED
    assert not new_worker.run_organization()


def test_invalid_tags_do_not_cover_sources(tmp_path):
    from tests.test_organization_service import _productive_plan
    lib,store,_ = library(tmp_path)
    service,_ = organization(store,growth=_productive_plan())
    result = service.drive(service.start_or_reuse().event_id)
    assert result.event.status is EventStatus.FAILED
    assert not lib.snapshot()['topics']
    with connect(store.path) as db:
        assert db.execute('SELECT COUNT(*) FROM organization_event_coverages').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM insight_versions').fetchone()[0] == 0


def test_productive_failure_rolls_back_relations_candidates_and_topics(tmp_path):
    from tests.test_organization_service import _productive_plan
    plan = _productive_plan()
    plan['candidate_versions'][0]['payload']['scan_tags'] = ['来源核对','证据边界','认知增量']
    lib,store,_ = library(tmp_path)
    reached = []
    def failure(point, db):
        if point == 'after_topic_replace':
            reached.append(db.execute('SELECT COUNT(*) FROM insight_versions').fetchone()[0])
            import sqlite3
            raise sqlite3.IntegrityError('fixture rollback')
    service,_ = organization(store,growth=plan,fail=failure)
    result = service.drive(service.start_or_reuse().event_id)
    assert result.event.status is EventStatus.FAILED
    assert reached == [1]
    with connect(store.path) as db:
        for table in ('relation_versions','insight_versions','organization_event_coverages','topic_entries'):
            assert db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == 0


def test_v6_upgrade_preserves_source_knowledge_and_topic_snapshot(tmp_path):
    from knowledge_distiller.v1.database import SCHEMA, SUBMITTED_SCHEMA, TOPIC_STATEMENTS
    from knowledge_distiller.v1.store import Store
    from knowledge_distiller.v1.pipeline import Distiller
    from knowledge_distiller.v1.worker import SingleWorker
    from knowledge_distiller.v1.topics import TopicLibrary
    from .test_topics import plan_for
    from .test_submitted_sources import Model, ForbiddenAudio
    store=Store(tmp_path/'upgrade.sqlite3')
    with connect(store.path) as db:
        db.executescript(SCHEMA+SUBMITTED_SCHEMA)
        for statement in TOPIC_STATEMENTS:
            db.execute(statement)
        db.execute('PRAGMA user_version=6')
    audio=ForbiddenAudio(); vault=tmp_path/'vault';vault.mkdir()
    worker=SingleWorker(store,Distiller(store=store,source=audio,normalizer=audio,recognizer=audio,
        reviewer=audio,confirmation_clipper=audio,knowledge_model=Model(),runtime_root=tmp_path/'runtime',vault=vault))
    for body in ('正文甲。','正文乙。'):
        item=store.submit_source(prepare_direct_text(body));worker.distiller.run(item)
    lib=TopicLibrary(store)
    points,old,guard=lib.prepare();lib.commit(plan_for(points),guard)
    snapshot=lib.snapshot()
    with connect(store.path) as db:
        before=[tuple(row) for row in db.execute('SELECT * FROM knowledge_results')]
    store.initialize()
    assert lib.snapshot()==snapshot
    with connect(store.path) as db:
        assert [tuple(row) for row in db.execute('SELECT * FROM knowledge_results')]==before
        assert db.execute('PRAGMA user_version').fetchone()[0]==18
        assert db.execute('PRAGMA foreign_key_check').fetchall()==[]
    service,_=organization(store)
    assert service.drive(service.start_or_reuse().event_id).event.status is EventStatus.SUCCEEDED


def test_unexpected_organization_failure_leaves_worker_usable(tmp_path):
    from knowledge_distiller.v1.worker import SingleWorker
    from knowledge_distiller.organization_service import read_event
    _,store,worker=library(tmp_path)
    service,_=organization(store)
    event=service.start_or_reuse()
    def unavailable():
        raise RuntimeError('fixture construction failure')
    new_worker=SingleWorker(store,worker.distiller,organization=unavailable)
    assert new_worker.run_organization()
    assert read_event(store.path,event.event_id).status is EventStatus.FAILED
    item=store.submit_source(prepare_direct_text('后来的正文仍继续处理。'))
    assert not new_worker.run_organization()
    assert new_worker.run_one()==item
    assert store.item_bundle(item)['state']=='succeeded'


@pytest.mark.parametrize('kind,claim', [('question', '是否应该继续思考？'), ('hypothesis', '能否建立联系？')])
def test_v1_rejects_question_instead_of_insight(tmp_path, kind, claim):
    from tests.test_organization_service import _productive_plan
    plan = _productive_plan()
    payload = plan['candidate_versions'][0]['payload']
    payload.update(claim_kind=kind, claim=claim, scan_tags=['品牌经营','风险管理','判断依据'])
    _, store, _ = library(tmp_path)
    service, _ = organization(store, growth=plan)
    result = service.drive(service.start_or_reuse().event_id)
    assert result.event.status is EventStatus.FAILED
    with connect(store.path) as db:
        assert db.execute('SELECT COUNT(*) FROM insight_versions').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM organization_event_coverages').fetchone()[0] == 0


def test_v1_runtime_sends_one_consistent_candidate_schema():
    import json
    from knowledge_distiller.growth_modeling import _RELATION_INSIGHT_SYSTEM_PROMPT
    from knowledge_distiller.v1.organization import GrowthRuntime
    from knowledge_distiller.v1.organization_contracts import growth_contract
    payload = {'codec': 'growth-planner-input-v1', 'frozen_new_ids': [4]}
    class Client:
        model = 'fixture'
        base_url = 'fixture://local'
        def complete(self, **kwargs):
            self.call = kwargs
            return json.dumps({'new_input_reviews': {'4': {'outcome': 'considered_no_formal_result', 'reason_text': '没有独立跨来源依据。'}},
                **{key: [] for key in ('relation_reviews', 'accepted_disqualifications', 'new_relations', 'candidate_versions', 'rejected_outputs')}})
    client = Client()
    result = GrowthRuntime(client).complete(system_prompt=_RELATION_INSIGHT_SYSTEM_PROMPT, input_payload=payload, max_tokens=8192)
    decoded = json.loads(result.text)
    assert decoded['codec'] == 'growth-plan-v1'
    assert decoded['new_input_reviews'][0]['knowledge_result_id'] == 4
    schema = growth_contract(payload)
    assert schema['properties']['new_input_reviews']['required'] == ['4']
    assert '"codec":"insight-v1","claim_kind"' not in client.call['system']


def test_deepseek_organization_steps_disable_implicit_thinking(monkeypatch):
    import knowledge_distiller.v1.organization as module
    from knowledge_distiller.v1.llm import OpenAIResponsesClient
    captured = {}
    def build(store, **kwargs):
        captured.update(kwargs)
        return kwargs
    monkeypatch.setattr(module, 'build_organization', build)
    original = OpenAIResponsesClient('https://api.deepseek.com/v1', 'deepseek-v4-flash', lambda: 'private')
    module.configured_organization(None, original)
    assert captured['topic_indexer'].client.reasoning_effort == 'none'
    assert original.reasoning_effort is None
