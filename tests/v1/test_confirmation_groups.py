"""K03 frozen positive-basis and atomic group edit counterexamples."""
from copy import deepcopy
import pytest
from knowledge_distiller.v1.confirmation_groups import form_groups, plan_group
from knowledge_distiller.v1.confirmation_revision import ConfirmationConflict


def sample(count=8, gap=''):
    sentence = '这里的食神指表达能力。'
    source = (sentence + gap) * count
    concerns = []
    start = 0
    for i in range(count):
        start = source.index('食神', start)
        concerns.append({'start':start,'end':start+2,'text':'食神', 'reason':'同音术语待确认',
            'candidates':['食神','识神'], 'audio_name':f'concern-{i+1}.wav','member_id':'primary-audio'})
        start += 2
    return {'snapshot':source,'concerns':concerns,'uncertainties':[], 'resolved':[], 'review_required':False,
            'review_identity':'synthetic-round'}


def request(pending, action='candidate', value='识神', selection=None):
    group = pending['groups'][0]
    return {'request_id':'synthetic-request','group_id':group['group_id'],'group_revision':group['group_revision'],
            'selected_member_uids':selection or group['member_uids'], 'action':action,'value':value}


def test_eight_definition_occurrences_form_one_group_and_eight_audits():
    pending = form_groups(sample(), 1)
    assert len(pending['groups']) == 1
    plan = plan_group(pending, request(pending), actor='local')
    assert plan.pending['snapshot'].count('识神') == 8
    assert not plan.pending['concerns']
    assert len(plan.audit) == 8
    assert len({r['concern_uid'] for r in plan.audit}) == 8
    assert plan.can_establish_fact


def test_published_members_never_silently_expand():
    pending = form_groups(sample(2), 1)
    added = deepcopy(pending['concerns'][0]);added.pop('concern_uid')
    added['audio_name'] = 'concern-3.wav';added['start'] = len(pending['snapshot'])+3;added['end']=added['start']+2
    pending['snapshot'] += '这里的食神指表达能力。';pending['concerns'].append(added)
    new = form_groups(pending, 1)
    assert len(new['groups']) == 2
    assert len(new['groups'][0]['member_uids']) == 2


def test_unable_preserves_unresolved_and_cannot_establish_fact():
    pending = form_groups(sample(), 1)
    selected = [pending['concerns'][0]['concern_uid']]
    plan = plan_group(pending, request(pending,'unable','',selected),actor='local')
    assert len(plan.pending['concerns']) == 7
    assert len(plan.pending['deferred_concerns']) == 1
    assert plan.pending['review_required']
    assert not plan.can_establish_fact


@pytest.mark.parametrize('count,expected',[ (1,[1]),(8,[8]),(32,[32]),(33,[32,1]) ])
def test_resource_bound_and_no_hardcoded_eight(count,expected):
    assert [len(g['member_uids']) for g in form_groups(sample(count),1)['groups']] == expected


def test_cross_chunk_repeated_explicit_definition_is_grouped():
    pending = form_groups(sample(8,'无关背景。'*300),1)
    assert len(pending['snapshot'])>2400
    assert len(pending['groups']) == 1
    assert len(pending['groups'][0]['equivalence_basis']['members']) == 8


@pytest.mark.parametrize('change',[ 'meaning','negation','quote','candidate','media','source','audio' ])
def test_literal_similarity_cannot_swallow_conflicting_member(change):
    raw=sample(2)
    member=raw['concerns'][1]
    if change in {'meaning','negation','quote'}:
        sentence={'meaning':'电影里的食神是主角。','negation':'这里的食神不是表达能力。','quote':'他说“食神指表达能力”。'}[change]
        raw['snapshot']=raw['snapshot'][:member['start']-3]+sentence
        member['start']=raw['snapshot'].rindex('食神');member['end']=member['start']+2
    elif change=='candidate':member['candidates']=['食神','失神']
    elif change=='media':member['member_id']='other-audio'
    elif change=='source':member['source_version_id']='other-source'
    else:member['audio_conflict']=True
    assert len(form_groups(raw,1)['groups'])==2


def test_partial_selection_same_snapshot_maps_other_members_and_retains_group_id():
    pending=form_groups(sample(3),1)
    selected=[pending['concerns'][1]['concern_uid']]
    req=request(pending,'manual','表达的能力',selected)
    result=plan_group(pending,req,actor='feishu')
    assert len(result.pending['concerns'])==2
    assert result.pending['groups'][0]['group_id']==pending['groups'][0]['group_id']
    assert result.pending['snapshot'][result.pending['concerns'][1]['start']:result.pending['concerns'][1]['end']]=='食神'
    assert result.audit[0]['actor']=='feishu'
    assert result.audit[0]['original_span']==pending['concerns'][1]['original_span']


def test_unselected_member_version_change_rejects_every_edit():
    pending=form_groups(sample(2),1)
    req=request(pending,selection=[pending['concerns'][0]['concern_uid']])
    changed=deepcopy(pending);changed['concerns'][1]['candidates'].append('不同候选')
    with pytest.raises(ConfirmationConflict,match='group_revision_conflict'):
        plan_group(changed,req,actor='local')
    assert changed['snapshot']==pending['snapshot']


def test_overlapping_edit_or_uncertainty_is_rejected_without_loss():
    pending=form_groups(sample(2),1)
    first=pending['concerns'][0]
    pending['uncertainties']=[{'start':first['start']-1,'end':first['end'],'status':'advisory','text':'的食神'}]
    with pytest.raises(ConfirmationConflict,match='overlap'):
        plan_group(pending,request(pending),actor='local')
    assert pending['uncertainties'][0]['text']=='的食神'


def test_key_uncertainty_without_concerns_does_not_form_fact():
    pending=form_groups(sample(2),1)
    pending['uncertainties']=[{'start':0,'end':1,'status':'unresolved','text':'这'}]
    plan=plan_group(pending,request(pending),actor='local')
    assert not plan.pending['concerns'] and not plan.can_establish_fact
    assert plan.pending['review_required']


def test_keep_action_is_explicit_and_preserves_every_original():
    pending=form_groups(sample(2),1)
    plan=plan_group(pending,request(pending,'keep',''),actor='local')
    assert plan.pending['snapshot']==pending['snapshot']
    assert all(a['action']=='keep' and a['before']==a['after'] for a in plan.audit)


def test_audio_revision_and_offset_only_changes_do_not_invalidate_group():
    pending=form_groups(sample(2),1);req=request(pending)
    changed=deepcopy(pending);changed['snapshot']='前言。'+changed['snapshot']
    for c in changed['concerns']:
        c['start']+=3;c['end']+=3;c['audio_file']='rebuilt.wav'
    assert plan_group(changed,req,actor='local').can_establish_fact


def test_model_hint_must_have_actual_definition_and_each_member_reference():
    from types import SimpleNamespace
    import json
    raw=sample(2)
    # Different contexts prevent deterministic repeated-definition grouping.
    raw['snapshot']='这里的食神指表达能力。食神表达能力用于输出。'
    raw['concerns'][1].update(start=raw['snapshot'].rindex('食神'),end=raw['snapshot'].rindex('食神')+2)
    calls=[]
    def complete(**kwargs):
        data=json.loads(kwargs['user']);calls.append(data)
        members=data['members']
        return json.dumps({'groups':[{'member_uids':[m['concern_uid'] for m in members],
            'semantic_key':'表达能力','definition_quote':'这里的食神指表达能力。',
            'members':[{'concern_uid':m['concern_uid'],'quote':m['quote']} for m in members]}]},ensure_ascii=False)
    grouped=form_groups(raw,1,SimpleNamespace(complete=complete))
    assert len(calls)==1 and len(grouped['groups'])==1
    def invalid(**kwargs):
        return complete(**kwargs).replace('表达能力','虚构定义')
    assert len(form_groups(raw,1,SimpleNamespace(complete=invalid))['groups'])==2


def test_adjacent_unequal_replacements_map_all_locations_from_one_snapshot():
    # Atomic edit mapper is also used for an explicitly scoped single-member
    # group; a neighbouring advisory span must survive code-point length change.
    raw=sample(1);raw['snapshot']='😀'+raw['snapshot']+'尾部'
    raw['concerns'][0]['start']+=1;raw['concerns'][0]['end']+=1
    raw['uncertainties']=[{'start':len(raw['snapshot'])-2,'end':len(raw['snapshot']),'text':'尾部','status':'advisory'}]
    pending=form_groups(raw,1)
    plan=plan_group(pending,request(pending,'manual','更长的字词'),actor='local')
    u=plan.pending['uncertainties'][0]
    assert plan.pending['snapshot'][u['start']:u['end']]=='尾部'
    a=plan.audit[0]
    assert plan.pending['snapshot'][a['result_span'][0]:a['result_span'][1]]=='更长的字词'
    assert a['submitted_span'][0]==4  # Python code points, not UTF-16.


def test_model_hint_cannot_merge_negated_member_with_same_semantic_words():
    from types import SimpleNamespace
    import json
    raw=sample(2)
    raw['snapshot']='这里的食神指表达能力。食神没有表达能力。'
    raw['concerns'][1].update(start=raw['snapshot'].rindex('食神'),end=raw['snapshot'].rindex('食神')+2)
    def complete(**kwargs):
        members=json.loads(kwargs['user'])['members']
        return json.dumps({'groups':[{'member_uids':[m['concern_uid'] for m in members],
            'semantic_key':'表达能力','definition_quote':'这里的食神指表达能力。',
            'members':[{'concern_uid':m['concern_uid'],'quote':m['quote']} for m in members]}]})
    assert len(form_groups(raw,1,SimpleNamespace(complete=complete))['groups'])==2


def test_changed_definition_requires_individual_recheck_even_with_current_revision():
    pending=form_groups(sample(2),1)
    pending['snapshot']=pending['snapshot'].replace('表达能力','电影角色')
    with pytest.raises(ConfirmationConflict,match='equivalence_unproven'):
        plan_group(pending,request(pending),actor='local')
