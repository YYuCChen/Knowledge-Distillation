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


def test_restart_reuses_completed_images_and_preserves_new_source_offsets(tmp_path):
    first, second = member(1), member(2)
    second['content'] = b'different image'
    with pytest.raises(OcrError) as caught:
        image_source_fact('原文', [first, second],
            Runner([result('已识别'), OcrError('ocr_inference_failed')]), checkpoint_dir=tmp_path)
    assert caught.value.completed_images[0]['lines'][0]['text'] == '已识别'
    fact, lineage = image_source_fact('新原文', [first, second],
        Runner([result('第二张')]), checkpoint_dir=tmp_path)
    assert '已识别' in fact.snapshot and '第二张' in fact.snapshot
    for image in lineage['image_ocr']:
        for line in image['lines']:
            assert fact.snapshot[line['start']:line['end']] == line['text']


def test_changed_image_and_corrupt_checkpoint_require_recognition(tmp_path):
    image = member(1)
    image_source_fact('', [image], Runner([result('第一版')]), checkpoint_dir=tmp_path)
    image['content'] = b'changed content'
    fact, _ = image_source_fact('', [image], Runner([result('第二版')]), checkpoint_dir=tmp_path)
    assert '第二版' in fact.snapshot and '第一版' not in fact.snapshot
    for path in tmp_path.glob('*.json'):
        path.write_text('{}')
    fact, _ = image_source_fact('', [image], Runner([result('重新识别')]), checkpoint_dir=tmp_path)
    assert '重新识别' in fact.snapshot
