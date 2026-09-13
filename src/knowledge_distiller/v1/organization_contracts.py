"""Model wire contracts; internal codecs/IDs are assembled by the application."""
from copy import deepcopy
from knowledge_distiller.organization_models import InsightValueKind, LimitationKind

S = {"type": "string"}
I = {"type": "integer"}
B = {"type": "boolean"}

def obj(**properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}

def arr(items):
    return {"type": "array", "items": items}

def enum(values, kind="string"):
    return {"type": kind, "enum": list(values)} if values else {"type": kind}

def union(*items):
    return {"anyOf": list(items)}

def ids(values):
    return arr(enum(values, "integer")) if values else {**arr(I), "maxItems": 0}

def topic_contract(points, existing):
    schema = obj(
        topics=arr(obj(key=S, topic_id={"type": ["integer", "null"], "enum": [t.topic_id for t in existing] + [None]}, name=S, scope=S)),
        decisions=obj(**{str(n): arr(obj(topic_key=S, position=I)) for n, _ in enumerate(points)}))
    return schema

def topic_plan(value, points):
    if set(value['decisions']) != {str(n) for n, _ in enumerate(points)}:
        raise ValueError('topic decisions must cover exactly the input members')
    topics = {}
    for row in value['topics']:
        key = row['key']
        if key in topics or not key.strip():
            raise ValueError('duplicate or empty topic key')
        topics[key] = {'name': row['name'], 'scope': row['scope'], 'members': [],
            **({'topic_id': row['topic_id']} if row['topic_id'] is not None else {'new_topic_key': key})}
    order = {key: [] for key in topics}
    unassigned = []
    for n, point in enumerate(points):
        reference = {'knowledge_result_id': point.knowledge_result_id, 'point_id': point.point_id}
        decisions = value['decisions'][str(n)]
        if not decisions:
            unassigned.append(reference)
        used = set()
        for decision in decisions:
            key = decision['topic_key']
            if key not in topics or key in used:
                raise ValueError('unknown or repeated topic assignment')
            used.add(key)
            order[key].append((decision['position'], reference))
    for key, rows in order.items():
        positions = [p for p, _ in rows]
        if any(type(p) is not int or p < 0 for p in positions) or len(set(positions)) != len(positions):
            raise ValueError('topic ordering must be nonnegative and unique')
        # Numeric gaps or a one-based start do not change the selected order.
        # The stored member sequence supplies contiguous positions mechanically.
        topics[key]['members'] = [ref for _, ref in sorted(rows, key=lambda row: row[0])]
    return {'topics': list(topics.values()), 'unassigned_points': unassigned}

def recall_contract(payload):
    fields = {'source_knowledge_ids': ('eligible_history', 'knowledge_result_id'),
        'accepted_insight_version_ids': ('accepted_current', 'insight_version_id'),
        'current_relation_version_ids': ('relation_current', 'relation_version_id'),
        'reconsideration_hint_version_ids': ('reconsideration_hints', 'relation_version_id')}
    return obj(**{name: ids([row[key] for row in payload[source]]) for name, (source, key) in fields.items()})

def growth_contract(payload):
    limitation = obj(kind=enum([v.value for v in LimitationKind]), text=S)
    premise = obj(premise_id=S, text=S, supported_by=arr(S))
    common = dict(participant_key=S, contribution_text=S)
    participant = union(obj(**common, input_kind=enum(['source_knowledge']), knowledge_result_id=I, point_id=S),
        obj(**common, input_kind=enum(['accepted_insight']), accepted_insight_version_id=I))
    boundary_ref = obj(ref_kind=enum(['boundary_current', 'requalified_current']), relation_version_id=I, role_text=S)
    planned_ref = obj(ref_kind=enum(['planned_stable']), new_relation_key=S, role_text=S)
    relation_payload = obj(relation_statement=S, conditions=arr(S), limitations=arr(limitation), stable_value=S, required_premises=arr(premise))
    insight_payload = obj(claim_kind=enum(['judgment', 'hypothesis']), claim=S, short_discussion=S,
        value_kind=enum([v.value for v in InsightValueKind]), connection_reasons=arr(S),
        limitations=arr(limitation), required_premises=arr(premise), scan_tags=arr(S))
    definitions = {'participant': participant, 'boundary_ref': boundary_ref,
        'used_ref': union(boundary_ref, planned_ref), 'relation_payload': relation_payload,
        'insight_payload': insight_payload}
    def ref(name):
        return {'$ref': '#/$defs/' + name}
    def targets(kind, body, refs):
        common = {f'new_{kind}_key': S, 'payload': body, 'participants': arr(ref('participant')), 'used_relations': arr(refs)}
        variants = [obj(**common, target_kind=enum(['create_identity'])),
            obj(**common, target_kind=enum(['evolve_identity']), **{f'{kind}_id': I, f'previous_{kind}_version_id': I})]
        if kind == 'insight':
            variants += [obj(**v['properties'], replaces_insight_id=I) for v in variants[:]]
        return union(*variants)
    review_common = dict(relation_id=I, relation_version_id=I, reason_text=S, directly_affected=B)
    review = union(obj(**review_common, action=enum(['unchanged', 'basis_invalid', 'wrong'])),
        obj(**review_common, action=enum(['attention']), attention_state=enum(['activated', 'retired'])),
        obj(**review_common, action=enum(['evolved']), successor_key=S),
        obj(**review_common, action=enum(['replaced', 'wrong_and_replaced']), replacement_new_relation_key=S))
    before = {t['topic_id']: t for t in payload.get('topic_before', {}).get('topics', [])}
    assessments = {}
    for topic in payload.get('topic_plan', {}).get('topics', []):
        old = before.get(topic.get('topic_id'))
        if not old:
            continue
        members = lambda t: {(m['knowledge_result_id'], m['point_id']) for m in t['members']}
        if members(old) == members(topic) and (old['name'], old['scope']) != (topic['name'], topic['scope']):
            assessments['topic:' + str(topic['topic_id'])] = obj(changed=B, reason_text=S)
    schema = obj(
        new_input_reviews=obj(**{str(i): obj(outcome=enum(['participated', 'considered_no_formal_result']), reason_text=S)
            for i in payload['frozen_new_ids']}),
        relation_reviews=arr(review),
        accepted_disqualifications=arr(obj(insight_version_id=I, fact_kind=enum(['basis_invalid', 'refuted']), reason_text=S)),
        new_relations=arr(targets('relation', ref('relation_payload'), ref('boundary_ref'))),
        candidate_versions=arr(targets('insight', ref('insight_payload'), ref('used_ref'))),
        topic_change_assessments=obj(**assessments),
        rejected_outputs=arr(obj(output_kind=enum(['exploration_only', 'illegal', 'duplicate_relation', 'duplicate_candidate', 'qualification_rejected']), related_ids=arr(I), reason_code=S)))

    if not assessments:
        del schema['properties']['topic_change_assessments']
        schema['required'].remove('topic_change_assessments')
    schema['$defs'] = definitions
    return schema

def growth_plan(value):
    result = deepcopy(value)
    result['codec'] = 'growth-plan-v1'
    result['topic_change_assessments'] = [{'topic_ref': key, **assessment} for key, assessment in result.get('topic_change_assessments', {}).items()]
    result['new_input_reviews'] = [{'knowledge_result_id': int(key), **review}
        for key, review in result['new_input_reviews'].items()]
    for key, codec in [('new_relations', 'relation-v1'), ('candidate_versions', 'insight-v1')]:
        for row in result[key]:
            row['payload']['codec'] = codec
            for position, participant in enumerate(row['participants']):
                participant['position'] = position
    return result
