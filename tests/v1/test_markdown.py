from knowledge_distiller.v1.domain import Evidence, Knowledge, Point
from knowledge_distiller.v1.markdown import render_markdown


def test_rendered_markdown_keeps_source_identity_and_evidence_navigation() -> None:
    snapshot = "持续切换会带来额外损耗。\n\n主动设定边界可以保护注意力。"
    knowledge = Knowledge(
        "注意力需要边界",
        "说明边界如何保护有限注意力。",
        "作者主张用明确边界减少注意力损耗。",
        (
            Point(
                "p1",
                "边界保护注意力。",
                "持续切换会产生额外损耗，因此需要主动设定边界。",
                ("e1", "e2"),
            ),
        ),
        (),
        (
            Evidence("e1", 0, 7, "持续切换会带来"),
            Evidence("e2", 14, 20, "主动设定边界"),
        ),
    )

    result = render_markdown(
        source_kind="douyin",
        source_key="123",
        submitted_url="https://v.douyin.com/a/",
        canonical_url="https://www.douyin.com/video/123",
        metadata={
            "author": {"display_name": "测试作者"},
            "published_at": "2026-01-02T03:04:05+00:00",
        },
        source_fact_id=2,
        snapshot=snapshot,
        knowledge_result_id=3,
        knowledge=knowledge,
    )

    assert result.source_blocks == 2
    assert result.evidence_links == 2
    assert "kd_source_key: \"123\"" in result.text
    assert "[[#^source-1|来源：持续切换会带来]]" in result.text
    assert "[[#^source-2|来源：主动设定边界]]" in result.text
    assert "持续切换会带来额外损耗。\n\n^source-1" in result.text
    assert "- 作者：测试作者" in result.text
    assert "- 原链接：<https://www.douyin.com/video/123>" in result.text


def render_source(snapshot, kind='direct_text', metadata=None):
    text='正文'
    start=snapshot.index(text)
    knowledge=Knowledge('标题','副标题','摘要',(Point('p1','观点','论证',('e1',)),),(),
                        (Evidence('e1',start,start+2,text),))
    return render_markdown(source_kind=kind,source_key='key',submitted_url='source',canonical_url='',
        metadata=metadata or {},source_fact_id=1,snapshot=snapshot,knowledge_result_id=1,knowledge=knowledge)


def test_native_paragraphs_have_separate_navigation_without_whole_document_code_fence():
    result=render_source('第一段。\n\n正文在第二段。')
    assert result.source_blocks==2
    assert '[[#^source-2|' in result.text
    assert '```text' not in result.text
    assert '正文在第二段。\n\n^source-2' in result.text


def test_douyin_native_body_is_not_duplicated_as_platform_description():
    result = render_source('正文全文', 'douyin', {
        'note_kind': 'normal', 'native_kind': 'article',
        'original_description': '正文全文', 'source_title': '原始标题'})
    assert '原平台描述' not in result.text
    assert '原平台标题：原始标题' in result.text
    assert '正文全文\n\n^source-1' in result.text


def test_article_image_without_ocr_text_stays_at_its_native_position():
    from types import SimpleNamespace
    from knowledge_distiller.v1.image_source import image_source_fact
    class EmptyOcr:
        def recognize_bytes(self, content, mime):
            return SimpleNamespace(width=1,height=1,engine='paddleocr',runtime_version='3.7.0',
                detection_model='det',recognition_model='rec',lines=())
    native='正文开头。\n\n〔图片 1〕\n\n图片之后的正文。'
    fact,lineage=image_source_fact(native,[{'member_id':'image-1','sha256':'hash',
        'mime_type':'image/png','content':b'fixture'}],EmptyOcr(),inline_images=True)
    assert fact.snapshot==native
    image=lineage['image_ocr'][0]
    assert native[image['source_start']:image['source_end']]=='〔图片 1〕'
    text=render_source(fact.snapshot,'douyin',{'source_lineage':lineage,
        'published_media':[{'member_id':'image-1','filename':'original.png'}]}).text
    assert text.index('![[original.png]]') < text.index('图片之后的正文。')


def test_markdown_safe_structure_and_inert_embeds_keep_exact_source():
    snapshot='# 原标题\n\n正文\n\n![[private-note]]\n\n```mermaid\ngraph LR\n\nA-->B\n```'
    result=render_source(snapshot,'markdown')
    assert '# 原标题\n\n^source-1' in result.text
    unquoted='\n'.join(line[2:] if line.startswith('> ') else '' if line=='>' else line for line in result.text.splitlines())
    assert '```text\n![[private-note]]\n```' in unquoted
    assert '````text\n```mermaid\ngraph LR\n\nA-->B\n```\n````' in unquoted
    assert result.source_blocks==4


def test_docling_table_native_and_image_ocr_stay_next_to_source_block():
    snapshot='正文\n\n| A | B |\n| --- | --- |\n| 1 | 2 |'
    begin=snapshot.index('|')
    metadata={'source_lineage':{'spans':[{'start':begin,'end':len(snapshot),'kind':'table'}],
        'image_ocr':[{'member_id':'image-1','lines':[{'start':0,'end':2}]}]},
        'published_media':[{'member_id':'image-1','filename':'source.png'}]}
    result=render_source(snapshot,'pdf',metadata)
    assert '| A | B |\n> | --- | --- |' in result.text
    assert result.text.count('![[source.png]]')==1
    assert result.text.index('![[source.png]]') < result.text.index('正文\n\n^source-1')


def test_static_live_photo_scope_is_separate_from_original_text():
    result = render_source('正文全文', 'douyin', {'note_kind': 'normal',
        'source_scope': '仅处理静态原图和配文；动态部分及其声音未处理。'})
    assert '- 来源范围：仅处理静态原图和配文；动态部分及其声音未处理。' in result.text
    assert result.text.index('正文全文\n\n^source-1') < result.text.index('## 来源说明')


def test_platform_internal_author_id_is_not_displayed_or_mutated():
    metadata = {"author": {"display_name": "测试作者", "platform_account_id": "MS4wLjABAAAA-internal-sec-uid"}}
    result = render_source("正文全文", "douyin", metadata)
    assert "- 作者：测试作者" in result.text
    assert "平台账号" not in result.text
    assert "MS4wLjABAAAA-internal-sec-uid" not in result.text
    assert metadata["author"]["platform_account_id"] == "MS4wLjABAAAA-internal-sec-uid"
