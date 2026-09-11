import pytest
from knowledge_distiller.v1.image_source import image_source_fact
from knowledge_distiller.v1.ocr import OcrLine, OcrResult, OcrError


class Runner:
    def __init__(self, results): self.results = iter(results)
    def recognize_bytes(self, content, mime):
        result = next(self.results)
        if isinstance(result, Exception): raise result
        return result


def member(number):
    return dict(member_id=f'image-{number}',sha256=str(number)*64,
                content=b'fixture',mime_type='image/png')


def result(text, confidence=.99):
    return OcrResult(120,80,(OcrLine(text,((1,2),(100,2),(100,30),(1,30)),confidence),))


def test_ordered_duplicate_text_has_distinct_exact_offsets_and_image_provenance():
    fact,lineage=image_source_fact('原生正文',[member(1),member(2)],Runner([result('同文'),result('同文',.5)]))
    images=lineage['image_ocr']
    assert [i['member_id'] for i in images]==['image-1','image-2']
    first,second=[i['lines'][0] for i in images]
    assert first['end'] < second['start']
    for image in images:
        line=image['lines'][0]
        assert fact.snapshot[line['start']:line['end']]==line['text']=='同文'
        assert image['engine']=='paddleocr' and image['width']==120
        assert line['polygon']==((1,2),(100,2),(100,30),(1,30))
    assert fact.uncertainties[0]['start']==second['start']
    assert fact.uncertainties[0]['member_id']=='image-2'


def test_blank_image_retains_provenance_without_fabricated_words():
    fact,lineage=image_source_fact('',[member(1)],Runner([OcrResult(120,80,())]))
    assert fact.snapshot=='[原始图片来源]'
    assert lineage['image_ocr'][0]['lines']==[]


def test_second_image_failure_never_returns_partial_source():
    with pytest.raises(OcrError,match='ocr_inference_failed'):
        image_source_fact('原文',[member(1),member(2)],Runner([result('已识别'),OcrError('ocr_inference_failed')]))
