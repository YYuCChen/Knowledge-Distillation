"""Pure synthetic positions and actual human/AI entry points; no DB/models."""
from copy import deepcopy
import hashlib
import json

import pytest

from knowledge_distiller.v1.domain import SourceFact
from knowledge_distiller.v1.ocr import OcrError
from knowledge_distiller.v1.ocr_review_policy import relocate_ocr, review_ocr
from knowledge_distiller.v1.image_confirmation import pending_review, resolve_review


def sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def fixture(text='重复\r\n重复😀 é', specs=((0, 2, 'image-1'), (4, 6, 'image-1'))):
    uncertainties, images = [], {}
    for index, (start, end, member) in enumerate(specs):
        uncertainty = {'start': start, 'end': end, 'text': text[start:end], 'by': 'ocr',
            'status': 'unresolved', 'member_id': member, 'reason': 'synthetic uncertainty',
            'concern_uid': f'uid-{index}', 'source_version_id': 'original-source-version',
            'review_round_id': 'original-round', 'original_span': [start, end]}
        uncertainties.append(uncertainty)
        image = images.setdefault(member, {'member_id': member, 'width': 40, 'height': 20,
            'sha256': '1'*64, 'source_start': 0, 'source_end': len(text), 'lines': []})
        image['lines'].append({'start': start, 'end': end, 'text': text[start:end],
            'polygon': [[0,0],[10,0],[10,5],[0,5]], 'confidence': .5})
    lineage = {'snapshot_sha256': sha(text), 'image_ocr': list(images.values()), 'spans': [
        {'start': 0, 'end': len(text), 'physical_page': 1, 'ref': '#container',
         'provenance': [{'page':1, 'bbox':[0,0,40,20], 'charspan':[0,len(text)],
                         'original_charspan':[0,len(text)]}],
         'page_local_start':0, 'page_local_end':len(text)}]}
    return SourceFact(text, tuple(uncertainties)), lineage


def edit(fact, index, replacement, *, by='ai'):
    u = fact.uncertainties[index]
    return {'start':u['start'], 'end':u['end'], 'text':u['text'], 'replacement':replacement,
            'member_id':u['member_id'], 'by':by, 'evidence': 'synthetic explicit source'}


def test_repeated_occurrence_unicode_crlf_and_container_stretch():
    fact, lineage = fixture()
    before = deepcopy((fact, lineage))
    final, updated, uncertainties, _, _ = relocate_ocr(fact.snapshot, lineage, fact.uncertainties,
                                                      [edit(fact, 1, '指定第二处')])
    assert final == '重复\r\n指定第二处😀 é'
    assert uncertainties[0]['text'] == '重复' and uncertainties[0]['start'] == 0
    assert (uncertainties[1]['start'], uncertainties[1]['end'], uncertainties[1]['text']) == (4, 9, '指定第二处')
    assert [u['concern_uid'] for u in uncertainties] == ['uid-0', 'uid-1']
    assert [u['original_span'] for u in uncertainties] == [[0,2], [4,6]]
    image = updated['image_ocr'][0]
    assert image['source_start'] == 0 and image['source_end'] == len(final)
    assert image['lines'][1]['polygon'] == lineage['image_ocr'][0]['lines'][1]['polygon']
    assert updated['spans'][0]['end'] == len(final)
    assert updated['spans'][0]['provenance'] == lineage['spans'][0]['provenance']
    assert updated['spans'][0]['page_local_end'] == len(fact.snapshot)  # original locator
    assert updated['snapshot_sha256'] == sha(final)
    assert updated['ocr_primary_snapshot_sha256'] == sha(fact.snapshot)
    assert (fact, lineage) == before


class FakeClient:
    def __init__(self, rows):
        self.rows, self.calls = rows, []
    def complete(self, **kwargs):
        self.calls.append(kwargs)
        return json.dumps({'decisions':self.rows}, ensure_ascii=False)


def decision(index, replacement, source, *, reliable=True):
    return {'index':index, 'affects_core':False, 'reliable':reliable, 'replacement':replacement,
        'evidence':source, 'reason':'Explicit synthetic assessment', 'assessment':{
            'kind':'recognition_error', 'original_reading_possible':False,
            'original_reading_analysis':'Fixture identifies this exact occurrence',
            'same_referent_analysis':'Fixture retains the same designated occurrence',
            'source_support_analysis':'Explicit fixture data for relocation testing',
            'alternatives_analysis':'Synthetic case specifies the intended edit',
            'competing_readings':[], 'meaning_changes':[]}}


def test_actual_ai_entry_unsorted_concerns_multiple_positive_negative_deltas():
    fact, lineage = fixture('甲甲\r\n乙乙\r\n丙丙', ((8,10,'image-1'), (0,2,'image-1'), (4,6,'image-1')))
    client = FakeClient([decision(0,'丙',fact.snapshot), decision(1,'甲甲甲甲',fact.snapshot),
                         decision(2,'乙乙',fact.snapshot,reliable=False)])
    result, updated = review_ocr(fact,lineage,client)
    assert len(client.calls) == 1
    assert json.loads(client.calls[0]['user'])['snapshot'] == fact.snapshot
    assert result.snapshot == '甲甲甲甲\r\n乙乙\r\n丙'
    assert [(u['start'],u['end'],u['text']) for u in result.uncertainties] == [(10,11,'丙'), (0,4,'甲甲甲甲'), (6,8,'乙乙')]
    assert [u['status'] for u in result.uncertainties] == ['repaired','repaired','advisory']
    assert updated['snapshot_sha256'] == sha(result.snapshot)
    assert [e['start'] for e in updated['ocr_relocations'][0]['edits']] == [0,8]
    for line in updated['image_ocr'][0]['lines']:
        assert result.snapshot[line['start']:line['end']] == line['text']


@pytest.mark.parametrize('fault', ['wrong_text','bool_start','cross_member','overlap','partial_span','table'])
def test_unproven_edit_is_rejected_without_mutation(fault):
    fact, lineage = fixture('abcdef', ((0,2,'image-1'), (4,6,'image-1')))
    edits = [edit(fact,0,'replacement')]
    if fault == 'wrong_text': edits[0]['text'] = 'xx'
    elif fault == 'bool_start': edits[0]['start'] = False
    elif fault == 'cross_member': edits[0]['member_id'] = 'image-2'
    elif fault == 'overlap': edits.append(deepcopy(edits[0]))
    elif fault == 'partial_span': lineage['spans'].append({'start':1,'end':3,'ref':'#partial'})
    else: lineage['spans'][0]['table_data'] = {'table_cells':[{'text':'abcdef'}]}
    original = deepcopy((lineage, edits))
    with pytest.raises(OcrError, match='^ocr_review_incomplete$'):
        relocate_ocr(fact.snapshot,lineage,fact.uncertainties,edits)
    assert (lineage, edits) == original


def test_epub_native_chapter_local_and_global_domains_remain_distinct():
    fact, lineage = fixture('甲甲\r\n乙乙', ((0,2,'image-1'), (4,6,'image-2')))
    lineage['kind'] = 'epub-spine'
    lineage['spans'] = []
    for ordinal, (start,end,member) in enumerate(((0,2,'image-1'),(4,6,'image-2')),1):
        lineage['spans'].append({'start':start,'end':end,'spine':ordinal,'resource':f'OEBPS/{ordinal}.xhtml',
            'member_id':member,'page_local_start':0,'page_local_end':2,'physical_page':1,
            'provenance':[{'bbox':[0,0,4,3],'charspan':[0,2],'original_charspan':[0,2]}],
            'native_occurrences':[{'start':0,'end':2,'native_text':fact.snapshot[start:end],
                'spine':ordinal,'resource':f'OEBPS/{ordinal}.xhtml','occurrence':f'spine/{ordinal}/body/p[0]/text'}]})
    final, updated, _, _, _ = relocate_ocr(fact.snapshot,lineage,fact.uncertainties,[edit(fact,0,'甲甲甲')])
    assert final == '甲甲甲\r\n乙乙'
    for old,new in zip(lineage['spans'],updated['spans']):
        assert new['provenance'] == old['provenance']
        native = new['native_occurrences'][0]
        assert native['start'] == 0 and native['end'] == 2 and native['native_text'] == old['native_occurrences'][0]['native_text']
    first,second = [s['native_occurrences'][0]['derived_final_range'] for s in updated['spans']]
    assert (first['start'],first['end'],first['chapter_local_end']) == (0,3,3)
    assert (second['start'],second['end'],second['chapter_global_start']) == (5,7,5)
    assert (second['chapter_local_start'],second['chapter_local_end']) == (0,2)


def test_unproven_partial_native_locator_is_preserved_with_unknown_final_range():
    fact,lineage = fixture('abcdef', ((0,6,'image-1'),))
    native = {'start':1,'end':3,'native_text':'bc','spine':1,'resource':'OEBPS/one.xhtml','occurrence':'original-node'}
    lineage['spans'][0].update(spine=1,resource='OEBPS/one.xhtml',native_occurrences=[native])
    _,updated,_,_,_ = relocate_ocr(fact.snapshot,lineage,fact.uncertainties,[edit(fact,0,'replacement')])
    output = updated['spans'][0]['native_occurrences'][0]
    assert all(output[k] == v for k,v in native.items())
    assert output['derived_final_range']['status'] == 'unknown'
    assert output['primary_locator'] == native


def test_repeated_relocation_preserves_original_native_domain_after_large_shrink():
    fact,lineage = fixture('abcdef', ((0,6,'image-1'),))
    native = {'start':0,'end':6,'native_text':'abcdef','spine':1,'resource':'OEBPS/one.xhtml','occurrence':'original-node'}
    lineage['spans'][0].update(spine=1,resource='OEBPS/one.xhtml',native_occurrences=[native])
    first,first_lineage,first_uncertainties,_,_ = relocate_ocr(
        fact.snapshot,lineage,fact.uncertainties,[edit(fact,0,'a')])
    second,second_lineage,_,_,_ = relocate_ocr(first,first_lineage,first_uncertainties,
        [{'start':0,'end':1,'text':'a','replacement':'ab','member_id':'image-1','by':'human','action':'manual'}])
    assert second == 'ab' and second_lineage['ocr_primary_snapshot'] == 'abcdef'
    output = second_lineage['spans'][0]['native_occurrences'][0]
    assert output['start'] == 0 and output['end'] == 6 and output['native_text'] == 'abcdef'
    assert output['derived_final_range']['status'] == 'mapped'
    assert (output['derived_final_range']['start'],output['derived_final_range']['end']) == (0,2)
    assert len(second_lineage['ocr_relocations']) == 2


@pytest.mark.parametrize('field', ['snapshot_sha256','ocr_primary_snapshot_sha256'])
def test_bad_existing_hash_rejected_before_fake_model_call(field):
    fact,lineage = fixture()
    lineage.update(ocr_primary_snapshot=fact.snapshot,ocr_primary_snapshot_sha256=sha(fact.snapshot))
    lineage[field] = '0'*64
    client = FakeClient([])
    with pytest.raises(OcrError, match='^ocr_review_incomplete$'):
        review_ocr(fact,lineage,client)
    assert client.calls == []


def test_no_concerns_or_empty_edits_preserve_fields_without_model_call():
    fact,lineage = fixture()
    client = FakeClient([])
    plain = SourceFact(fact.snapshot,())
    assert review_ocr(plain,lineage,client) == (plain,lineage) and client.calls == []
    result = relocate_ocr(fact.snapshot,lineage,fact.uncertainties,[])
    assert result[:3] == (fact.snapshot,lineage,list(fact.uncertainties))
    legacy = deepcopy(lineage); legacy.pop('snapshot_sha256')
    final,changed,_,_,_ = relocate_ocr(fact.snapshot,legacy,fact.uncertainties,[edit(fact,1,'更正')])
    assert changed['ocr_primary_snapshot'] == fact.snapshot
    assert changed['ocr_primary_snapshot_sha256'] == sha(fact.snapshot) and changed['snapshot_sha256'] == sha(final)


class FakeStore:
    def __init__(self): self.calls = []
    def resolve_confirmation(self,*args,**kwargs):
        self.calls.append((args,kwargs)); return 'captured'


@pytest.mark.parametrize('fault', ['hash','partial_span','table'])
def test_human_relocation_rejection_uses_existing_error_and_never_calls_store(fault):
    fact,lineage = fixture()
    if fault == 'hash': lineage['snapshot_sha256'] = '0'*64
    elif fault == 'partial_span': lineage['spans'].append({'start':1,'end':3,'ref':'#partial'})
    else: lineage['spans'][0]['table_data'] = {'table_cells':[{'text':fact.snapshot}]}
    pending = pending_review(fact,lineage)
    original = deepcopy(pending)
    store = FakeStore()
    with pytest.raises(ValueError, match='^来源确认已更新，请刷新后再操作。$'):
        resolve_review(store,{'item_id':17,'source_fact_id':None,'confirmation_json':'CAS'},
                       pending,'manual','更正首行','ocr-1')
    assert store.calls == [] and pending == original


def test_ai_mapping_failure_retains_actual_partial_review():
    fact,lineage = fixture()
    lineage['spans'][0]['table_data'] = {'table_cells':[{'text':fact.snapshot}]}
    client = FakeClient([decision(0,'更正首行',fact.snapshot),decision(1,'重复',fact.snapshot,reliable=False)])
    with pytest.raises(OcrError) as caught:
        review_ocr(fact,lineage,client)
    assert len(client.calls) == 1
    partial = caught.value.partial_review
    assert partial['source'] == fact.snapshot and partial['failed_operation'] == 'relocation'
    assert partial['decisions'][0]['replacement'] == '更正首行'
    assert len(partial['response_chain']) == 1


def test_new_edit_cannot_rewrite_saved_human_replacement():
    fact,lineage = fixture('abcdef', ((0,2,'image-1'),))
    saved = {'by':'human','start':0,'end':2,'text':'old','replacement':'ab',
             'member_id':'image-1','concern_uid':'saved-uid','action':'manual','original_span':[0,2]}
    before = deepcopy(saved)
    with pytest.raises(OcrError,match='^ocr_review_incomplete$'):
        relocate_ocr(fact.snapshot,lineage,fact.uncertainties,[edit(fact,0,'new')],resolved=[saved])
    assert saved == before


def test_actual_human_path_keeps_uids_decisions_and_resolved_then_final_fact():
    fact,lineage = fixture()
    pending = pending_review(fact,lineage)
    pending.update(review_round_id='original-round',source_version_id='original-source-version')
    old_human = {'by':'human','text':'historical','replacement':'preserved','action':'manual','concern_uid':'saved-uid'}
    pending['resolved'] = [old_human]
    before = deepcopy(pending)
    store = FakeStore()
    row = {'item_id':17,'source_fact_id':None,'confirmation_json':'actual-CAS-token'}
    human_decision = {'revision':'actual-CAS-token','action':'manual','value':'第二处更正'}
    assert resolve_review(store,row,pending,'manual','第二处更正','ocr-2',decision=human_decision) == 'captured'
    args,kwargs = store.calls[0]
    assert args == (17,'actual-CAS-token') and kwargs['decision'] is human_decision
    next_pending = kwargs['next_confirmation']
    assert pending == before and next_pending['snapshot'] == '重复\r\n第二处更正😀 é'
    assert next_pending['resolved'][0] == old_human
    saved = next_pending['resolved'][1]
    assert (saved['concern_uid'],saved['original_span'],saved['confirmed_span'],saved['action']) == ('uid-1',[4,6],[4,6],'manual')
    assert (saved['start'],saved['end']) == (4,9)
    assert next_pending['concerns'][0]['concern_uid'] == 'uid-0'
    assert next_pending['review_round_id'] == pending['review_round_id']
    resolve_review(store,row,next_pending,'manual','首处长更正','ocr-1',decision={'action':'manual'})
    _,final = store.calls[1]
    assert 'next_confirmation' not in final
    result = final['fact']
    assert result.snapshot == '首处长更正\r\n第二处更正😀 é'
    assert final['lineage']['snapshot_sha256'] == sha(result.snapshot)
    assert result.uncertainties[0] == old_human
    previously_saved = result.uncertainties[1]
    assert previously_saved['concern_uid'] == 'uid-1' and previously_saved['original_span'] == [4,6]
    assert result.snapshot[previously_saved['start']:previously_saved['end']] == previously_saved['replacement']
    assert [u['action'] for u in result.uncertainties] == ['manual','manual','manual']
