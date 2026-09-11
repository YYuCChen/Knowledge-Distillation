import pytest
from knowledge_distiller.v1.reading import build_reading


def source(texts, *, gap=5):
    snapshot='';lines=[]
    for i,text in enumerate(texts):
        if snapshot:snapshot+='\n'
        start=len(snapshot);snapshot+=text
        lines.append({'start':start,'end':len(snapshot),'text':text,
                      'polygon':[[0,i*(20+gap)],[500,i*(20+gap)],[500,i*(20+gap)+20],[0,i*(20+gap)+20]]})
    return snapshot,{'image_ocr':[{'member_id':'image-1','width':550,'lines':lines}]}


def test_wraps_preserve_numbers_negation_and_exact_spans():
    snapshot,lineage=source(['不应认为价格达到10','00元就一定更好。需要比较','质量、成本与适用条件。'])
    blocks=build_reading(snapshot,lineage)
    # Cross-line numeric fragments remain distinguishable; never guess a number.
    assert blocks[0].text=='不应认为价格达到10\n00元就一定更好。需要比较质量、成本与适用条件。'
    assert (blocks[0].start,blocks[0].end)==(0,len(snapshot))
    assert snapshot=='\n'.join(line['text'] for line in lineage['image_ocr'][0]['lines'])
    assert build_reading(snapshot,lineage)==blocks


def test_geometry_preserves_paragraphs_and_headings():
    snapshot,lineage=source(['标题','第一段正文保持','原本的判断。','第二段正文。'])
    lines=lineage['image_ocr'][0]['lines']
    lines[0]['polygon']=[[0,0],[100,0],[100,20],[0,20]]
    lines[-1]['polygon']=[[0,130],[500,130],[500,150],[0,150]]
    assert [b.text for b in build_reading(snapshot,lineage)]==['标题','第一段正文保持原本的判断。','第二段正文。']


def test_multimage_order_and_native_text_are_retained():
    a,la=source(['第一张完整','正文。']);b,lb=source(['第二张完整','正文。'])
    snapshot='作者配文\n\n'+a+'\n\n'+b
    for line in la['image_ocr'][0]['lines']:
        line['start']+=len('作者配文\n\n');line['end']+=len('作者配文\n\n')
    for line in lb['image_ocr'][0]['lines']:
        line['start']+=len('作者配文\n\n'+a+'\n\n');line['end']+=len('作者配文\n\n'+a+'\n\n')
    blocks=build_reading(snapshot,{'image_ocr':la['image_ocr']+lb['image_ocr']})
    assert [b.text for b in blocks]==['作者配文','第一张完整正文。','第二张完整正文。']


def test_stale_human_confirmation_offsets_cannot_render_wrong_source():
    snapshot,lineage=source(['原始识别','内容'])
    with pytest.raises(ValueError,match='reading_source_mismatch'):
        build_reading(snapshot.replace('原始','人工确认'),lineage)


def test_latin_word_boundary_and_punctuation_remain():
    snapshot,lineage=source(['Large language','models are not','always right.'])
    assert build_reading(snapshot,lineage)[0].text=='Large language models are not always right.'


def test_ocr_marker_before_trailing_newline_is_not_lost():
    text,lineage=source(['第一行','第二行'])
    prefix='作者原文\n\n[图片 image-1 OCR]\n'
    for line in lineage['image_ocr'][0]['lines']:
        line['start']+=len(prefix);line['end']+=len(prefix)
    blocks=build_reading(prefix+text,lineage)
    assert blocks[1].text=='[图片 image-1 OCR]'
    assert blocks[2].start==len(prefix)
