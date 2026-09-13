"""Adapt the existing growth domain to v1 source facts and frozen Topics."""
from dataclasses import asdict, replace
from urllib.parse import urlsplit
from types import SimpleNamespace

from knowledge_distiller.organization_models import SourceKnowledgeInput, SourcePointInput, sha256_json
from knowledge_distiller.organization_service import OrganizationService
from .database import connect
from .topics import TopicLibrary, read_formal_points


def load_sources(connection):
    # Use the same strict source knowledge decoder as Topic and Search.
    points = read_formal_points(connection)
    grouped = {}
    for point in points:
        grouped.setdefault(point['knowledge_result_id'], []).append(point)
    result = []
    for key, values in grouped.items():
        first = values[0]
        formal = tuple(SourcePointInput(key, p['point_id'], p['role'], p['statement'], p['argument']) for p in values)
        signature = sha256_json({'knowledge_result_id':key, 'source_fact_id':first['source_fact_id'],
            'title':first['title'], 'summary':first['summary'], 'points':[asdict(p) for p in formal]})
        result.append(SourceKnowledgeInput(key, first['source_fact_id'], first['title'], first['summary'],
            formal, 'eligible_history', signature))
    return tuple(result)


class OrganizationTopics:
    def __init__(self, store):
        self.library = TopicLibrary(store)

    def guard(self, connection):
        return self.library.snapshot(connection)

    def planning(self, connection):
        snapshot = self.guard(connection)
        topics = [dict(topic_id=t['id'], name=t['name'], scope=t['scope'], members=t['members']) for t in snapshot['topics']]
        before = {'codec':'topic-safety-snapshot-v1', 'state':'current', 'topics':topics,
                  'uncovered_count':0}
        return SimpleNamespace(before_payload=before, before_signature=sha256_json(before),
                               guard_signature=sha256_json(snapshot))

    def replace(self, connection, plan, boundary):
        ids = {s.knowledge_result_id for s in (*boundary.frozen_new, *boundary.eligible_history)}
        # The domain already requalified frozen inputs and compared the stored
        # Topic baseline under this write transaction; later sources stay outside.
        _, _, guard = self.library.prepare(connection, ids)
        return self.library.commit(plan, guard, connection, ids)


class RecordedOrganizationService(OrganizationService):
    def __init__(self, *args, diagnostics=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.diagnostics = diagnostics

    def drive(self, event_id):
        result = super().drive(event_id)
        if self.diagnostics:
            for stage, record in self.diagnostics.records.items():
                record['event_id'] = event_id
                record['event_status'] = str(result.event.status)
                record['failure_code'] = str(result.event.failure_code) if result.event.failure_code else None
                if str(result.event.status) == 'succeeded':
                    self.diagnostics.accept(stage)
                else:
                    self.diagnostics.save(stage)
        return result


def build_organization(store, *, topic_indexer, recall_planner, relation_insight_planner, failure_injector=None, diagnostics=None):
    return RecordedOrganizationService(store.path, topic_indexer=topic_indexer, recall_planner=recall_planner,
        relation_insight_planner=PresentationPlanner(relation_insight_planner), failure_injector=failure_injector,
        source_loader=load_sources, topic_store=OrganizationTopics(store), diagnostics=diagnostics)


def organization_status(store):
    with connect(store.path) as db:
        db.execute('BEGIN')
        covered = {r[0] for r in db.execute('SELECT knowledge_result_id FROM organization_event_coverages')}
        pending = sum(source.knowledge_result_id not in covered for source in load_sources(db))
        latest = db.execute('SELECT event_id,status,failure_code FROM organization_events ORDER BY event_id DESC LIMIT 1').fetchone()
        return dict(pending=pending, running=bool(latest and latest['status']=='running'),
                    failed=bool(latest and latest['status']=='failed'))


class GrowthRuntime:
    def __init__(self, client, calls=None):
        from .structured_calls import StructuredCalls
        self.client = client
        self.calls = calls or StructuredCalls(client)

    def is_available(self):
        return bool(self.client.base_url.strip() and self.client.model.strip())

    def complete(self, *, system_prompt, input_payload, max_tokens):
        import json
        from knowledge_distiller.growth_modeling import GrowthRuntimeResult, GrowthRuntimeFailed
        from .llm import LLMRequestError
        try:
            if input_payload.get('codec') == 'growth-planner-input-v1':
                system_prompt += INSIGHT_READING_PROMPT
            from .organization_contracts import recall_contract, growth_contract, growth_plan
            from knowledge_distiller.growth_modeling import GROWTH_PLAN_OUTPUT_CONTRACT
            if input_payload.get('codec') == 'historical-recall-input-v1':
                system_prompt = system_prompt.split('只返回')[0] + '返回schema规定的四个ID列表；codec由程序填写，不要输出。'
                value = self.calls.complete('recall', system_prompt, input_payload, recall_contract(input_payload), max_tokens)
                text = json.dumps({'codec': 'historical-recall-v1', **value}, ensure_ascii=False)
            else:
                system_prompt = system_prompt.replace(GROWTH_PLAN_OUTPUT_CONTRACT, '')
                system_prompt = '本阶段处理跨来源关系和新知，不能把单篇素材的不同观点重新概括为新知。每个候选必须有至少两条独立来源谱系；没有真实跨来源增量时返回零候选并给出未产生结果的理由。\n' + system_prompt
                system_prompt += '\n输出结构以schema为准：topic_change_assessments是由程序指定topic_ref为键的对象，只评估列出的项目，schema未要求此字段时不要输出，由程序填充空列表。new_input_reviews是以每个frozen_new_id为键的对象，必须逐项给出判断；所有层级codec由程序填写，不要输出。'
                value = self.calls.complete('growth', system_prompt, input_payload, growth_contract(input_payload), max_tokens)
                from .insight_presentation import prepare_labels
                value = prepare_labels(value, self.calls)
                text = json.dumps(growth_plan(value), ensure_ascii=False)
        except LLMRequestError as error:
            raise GrowthRuntimeFailed() from error
        return GrowthRuntimeResult(text, 'end_turn')


def configured_organization(store, client):
    from knowledge_distiller.growth_modeling import HistoricalRecallAdapter, RelationInsightAdapter
    from .topic_model import TopicModel
    from .llm import OpenAIResponsesClient
    # Organization has bounded JSON outputs; DeepSeek's default thinking can
    # exhaust those budgets before returning a plan, including in later steps.
    if (isinstance(client, OpenAIResponsesClient)
            and urlsplit(client.base_url).hostname == 'api.deepseek.com'
            and client.model in {'deepseek-v4-flash', 'deepseek-v4-pro'}):
        client = replace(client, reasoning_effort='none')
    from .structured_calls import StructuredCalls
    calls = StructuredCalls(client, store.path.parent / "runtime" / "organization" if store else None)
    runtime = GrowthRuntime(client, calls)
    return build_organization(store, topic_indexer=TopicModel(client, calls),
        recall_planner=HistoricalRecallAdapter(runtime), relation_insight_planner=RelationInsightAdapter(runtime), diagnostics=calls)


class PresentationPlanner:
    def __init__(self, planner):
        self.planner = planner

    def is_available(self):
        return self.planner.is_available()

    def plan(self, *args, **kwargs):
        from knowledge_distiller.growth_modeling import GrowthPlanning, GrowthModelFailure
        result = self.planner.plan(*args, **kwargs)
        if result.plan is not None and any(len(c.payload.scan_tags) != 3 or c.payload.claim_kind == 'question' or c.payload.claim.rstrip().endswith(('?', '？')) for c in result.plan.candidate_versions):
            return GrowthPlanning.failed(GrowthModelFailure.INVALID_OUTPUT)
        return result


INSIGHT_READING_PROMPT = '''
candidate提出值得继续思考的增量判断：这些材料放在一起，比单独阅读其中任一来源多理解了什么。只在依据能够支撑这一增量时形成候选，否则返回零候选。claim_kind为judgment或hypothesis。
claim直接陈述新增判断，保留独立理解所需的具体对象、情境和关键条件。标题让读者一眼知道在讨论什么、发现了什么；假设以条件或可能性表达。
scan_tags在每个candidate的payload内，三个互不相同、各四个汉字的字符串，辨认本条实际场景、对象和话题；不用通用流程词凑数。
short_discussion围绕标题的判断写成一段简洁、连贯的论述。以理解判断所需的背景进入问题，解释它成立的机制或联系，落到由此增加的理解。每句话推进同一个判断，篇幅以讲清这一步为足。条件与不确定性融入其影响推理的位置，使表达的确定程度与依据相符；正文沿判断展开，不逐份汇报材料，也不以固定的验证声明收尾。
选择实际支撑论述的来源，完整保留谱系、前提、连接理由和限制于对应结构字段。来源标题与跳转由页面提供，正文不另列来源说明。论述是面向读者的解释，不是隐藏思考过程，也不代表来源作者或用户已经认可该新增判断。
'''
