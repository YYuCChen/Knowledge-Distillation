import json
import pytest
from .test_image_confirmation import review
from knowledge_distiller.v1.image_confirmation import pending_review
from knowledge_distiller.v1.pipeline import Distiller


def engine(store, root):
    return Distiller(store=store, source=None, normalizer=None, recognizer=None, reviewer=None,
                    confirmation_clipper=None, knowledge_model=None, runtime_root=root, vault=None)


def test_different_concerns_merge_and_same_decision_replays_after_completion(review, tmp_path):
    from knowledge_distiller.v1.confirmation_revision import revision
    store,item,_,_,fact,lineage=review
    store.mark_waiting(item,pending_review(fact,lineage))
    pending=json.loads(store.item_bundle(item)['confirmation_json'])
    first,second=pending['concerns']
    service=engine(store,tmp_path)
    a=dict(token=pending['token'],concern_id=first['audio_name'],concern_revision=revision(pending,first))
    b=dict(token=pending['token'],concern_id=second['audio_name'],concern_revision=revision(pending,second))
    assert service.resolve(item,'manual','甲句已纠正',**a).state=='waiting_user'
    assert service.resolve(item,'manual','乙句',**b).state=='queued'
    assert service.resolve(item,'manual','甲句已纠正',**a).state=='waiting_user'
    with pytest.raises(ValueError,match='另一项决定'):
        service.resolve(item,'manual','其他文字',**a)
    assert store.item_bundle(item)['snapshot']=='甲句已纠正\n乙句'


def test_simultaneous_different_concerns_rebase_in_transaction(review,tmp_path,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier,Lock
    from knowledge_distiller.v1.confirmation_revision import revision
    store,item,_,_,fact,lineage=review
    store.mark_waiting(item,pending_review(fact,lineage))
    pending=json.loads(store.item_bundle(item)['confirmation_json'])
    service=engine(store,tmp_path)
    barrier=Barrier(2);lock=Lock();calls=0
    commit=store.resolve_confirmation
    def concurrent(*args,**kwargs):
        nonlocal calls
        with lock:
            calls+=1;n=calls
        if n<=2:barrier.wait(timeout=3)
        return commit(*args,**kwargs)
    monkeypatch.setattr(store,'resolve_confirmation',concurrent)
    def apply(concern):
        return service.resolve(item,'manual',concern['text']+'修正',token=pending['token'],
                               concern_id=concern['audio_name'],concern_revision=revision(pending,concern))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(apply,pending['concerns']))
    assert {r.state for r in results}=={'waiting_user','queued'}
    assert store.item_bundle(item)['snapshot']=='甲句修正\n乙句修正'
