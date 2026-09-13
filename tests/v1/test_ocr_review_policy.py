import json
from types import SimpleNamespace
from knowledge_distiller.v1.domain import SourceFact


def test_noncore_ocr_repair_and_uncertainty_do_not_block_but_keep_evidence():
    from knowledge_distiller.v1.ocr_review_policy import review_ocr
    fact=SourceFact('甲错字\n乙字\n数值三',tuple({'by':'ocr','status':'unresolved','start':s,'end':e,'text':t,'member_id':'image-1'}
                     for s,e,t in [(0,3,'甲错字'),(4,6,'乙字'),(7,10,'数值三')]))
    lineage={'image_ocr':[{'member_id':'image-1','lines':[{'start':u['start'],'end':u['end'],'text':u['text']} for u in fact.uncertainties]}]}
    answer={'decisions':[
        {'index':0,'affects_core':False,'reliable':True,'replacement':'甲正字','evidence':'甲错字\n乙字','reason':'上下文明确'},
        {'index':1,'affects_core':False,'reliable':False,'replacement':'乙字','evidence':'','reason':'局部字形无法确定，但不影响主旨'},
        {'index':2,'affects_core':True,'reliable':False,'replacement':'数值三','evidence':'','reason':'数值影响结论'}]}
    result,trace=review_ocr(fact,lineage,SimpleNamespace(complete=lambda **kwargs:json.dumps(answer)))
    assert result.snapshot==fact.snapshot
    # P05: rejecting an unsupported edit cannot promote a non-core question.
    assert [u['status'] for u in result.uncertainties]==['advisory','advisory','unresolved']
    assert trace['ocr_primary_snapshot']==fact.snapshot
    assert trace['ocr_review_diagnostics'][0]['operation']==0
