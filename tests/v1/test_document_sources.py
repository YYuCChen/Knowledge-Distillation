from __future__ import annotations

import hashlib
import io
import json
import zipfile

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, FloatObject, NameObject, NumberObject, TextStringObject

from knowledge_distiller.v1.pdf_source import parse_pdf, qualify_pdf
from knowledge_distiller.v1.epub_source import parse_epub, qualify_epub
from knowledge_distiller.v1.source_parsing import SourceReadError


def pdf_bytes(text="Machine readable PDF", *, image=False, nested_image=False, unused_image=False,
              xmp=None, author=None, encrypted=False, stream_suffix=b"", second_page=None):
    writer = PdfWriter()
    for page_text in [text] + ([second_page] if second_page is not None else []):
        page = writer.add_blank_page(612, 792)
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
        commands = (f"BT /F1 12 Tf 72 720 Td ({page_text}) Tj ET".encode() if page_text else b"") + stream_suffix
        if image or nested_image or unused_image:
            picture = DecodedStreamObject()
            picture.set_data(b"\x00\x00\x00")
            picture.update({NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Image"), NameObject("/Width"): NumberObject(1), NameObject("/Height"): NumberObject(1), NameObject("/ColorSpace"): NameObject("/DeviceRGB"), NameObject("/BitsPerComponent"): NumberObject(8)})
            picture_ref = writer._add_object(picture)
            if nested_image:
                form = DecodedStreamObject()
                form.set_data(b"/Scan Do")
                form.update({NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Form"), NameObject("/BBox"): ArrayObject([FloatObject(0), FloatObject(0), FloatObject(500), FloatObject(700)]), NameObject("/Resources"): DictionaryObject({NameObject("/XObject"): DictionaryObject({NameObject("/Scan"): picture_ref})})})
                picture_ref = writer._add_object(form)
            page["/Resources"][NameObject("/XObject")] = DictionaryObject({NameObject("/Image"): picture_ref})
            if not unused_image:
                commands += b" q 500 0 0 650 50 20 cm /Image Do Q"
        stream = DecodedStreamObject()
        stream.set_data(commands)
        page[NameObject("/Contents")] = writer._add_object(stream)
    if author is not None:
        writer.add_metadata({"/Author": author})
    if xmp is not None:
        stream = DecodedStreamObject()
        stream.set_data(xmp.encode())
        stream.update({NameObject("/Type"): NameObject("/Metadata"), NameObject("/Subtype"): NameObject("/XML")})
        writer._root_object[NameObject("/Metadata")] = writer._add_object(stream)
    if encrypted:
        writer.encrypt("password")
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def epub_bytes(body="中文来源正文", *, extra_body=None, linear="yes", missing=False, protected=False,
               metadata="", css=None, body_attributes="", extra_spine="", extra_manifest="", duplicate_resource=False):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/book.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        archive.writestr("OEBPS/book.opf", '<package xmlns="http://www.idpf.org/2007/opf" xmlns:dc="http://purl.org/dc/elements/1.1/" version="3.0"><metadata><dc:title>测试书</dc:title>' + metadata + '</metadata><manifest><item id="first" href="one.xhtml" media-type="application/xhtml+xml"/>' + ('<item id="second" href="two.xhtml" media-type="application/xhtml+xml"/>' if extra_body is not None else '') + extra_manifest + '</manifest><spine><itemref idref="first"/>' + (f'<itemref idref="second" linear="{linear}"/>' if extra_body is not None else '') + extra_spine + '</spine></package>')
        head = '<head><link rel="stylesheet" href="style.css"/></head>' if css is not None else '<head/>'
        if css is not None:
            archive.writestr("OEBPS/style.css", css)
        if not missing:
            archive.writestr("OEBPS/one.xhtml", f'<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">{head}<body {body_attributes}>{body}</body></html>')
        if extra_body is not None:
            archive.writestr("OEBPS/two.xhtml", f'<html xmlns="http://www.w3.org/1999/xhtml"><body>{extra_body}</body></html>')
        if protected:
            archive.writestr("META-INF/encryption.xml", '<encryption/>')
        if duplicate_resource:
            archive.writestr("OEBPS/one.xhtml", '<html/>')
    return output.getvalue()


from knowledge_distiller.v1.docling_source import DoclingSourceConverter
_TEST_CONVERTER = DoclingSourceConverter()


def parsed_pdf(content):
    return parse_pdf(content, "source.pdf", hashlib.sha256(content).hexdigest(), converter=_TEST_CONVERTER)


def parsed_epub(content):
    return parse_epub(content, "source.epub", hashlib.sha256(content).hexdigest(), converter=_TEST_CONVERTER)


def test_real_pdf_reader_builds_physical_page_occurrence_spans():
    raw = pdf_bytes("Repeated source", second_page="Repeated source")
    qualify_pdf(raw)
    result = parsed_pdf(raw)
    assert result.snapshot.count("Repeated source") == 2
    assert [span["physical_page"] for span in result.lineage["spans"]] == [1, 2]
    assert result.lineage["snapshot_sha256"] == hashlib.sha256(result.snapshot.encode()).hexdigest()
    for span in result.lineage["spans"]:
        text = result.snapshot[span["start"]:span["end"]]
        assert text == "Repeated source"
        assert span["page_local_end"] == len(text)
    json.dumps(result.metadata)


@pytest.mark.parametrize("options", [{"image": True}, {"nested_image": True}])
def test_pdf_retains_visual_body_with_original_page_and_native_text(options):
    result=parsed_pdf(pdf_bytes(**options))
    assert 'Machine readable PDF' in result.snapshot
    assert any(m.member_id=='page-1' for m in result.media)
    assert result.metadata['parser']=='docling'


def test_scan_with_no_recognizable_text_is_not_a_textual_source():
    with pytest.raises(SourceReadError,match='pdf_empty_content'):
        parsed_pdf(pdf_bytes(text='',image=True))


def test_unused_image_resource_does_not_mean_missing_body():
    assert parsed_pdf(pdf_bytes(unused_image=True)).snapshot == "Machine readable PDF"


def test_pdf_encryption_and_unknown_protection_fail_preaccept():
    for raw, code in [(pdf_bytes(encrypted=True), "pdf_protected"), (b"%PDF-1.4\nbroken", "pdf_protection_unknown"), (b"wrong", "pdf_wrong_type")]:
        with pytest.raises(SourceReadError, match=code):
            qualify_pdf(raw)


def test_empty_pdf_never_yields_a_partial_source():
    with pytest.raises(SourceReadError, match="pdf_empty_content"):
        parsed_pdf(pdf_bytes(text=""))


def test_pdf_visible_shape_is_preserved_in_page_render():
    result=parsed_pdf(pdf_bytes(stream_suffix=b" 0 0 100 100 re f"))
    assert 'Machine readable PDF' in result.snapshot
    assert result.media and result.lineage['pages'][0]['page']==1


def test_pdf_layout_recovers_physical_order_instead_of_stream_order():
    result=parsed_pdf(pdf_bytes(stream_suffix=b" BT /F1 12 Tf 300 750 Td (reordered) Tj ET"))
    assert 'reordered' in result.snapshot and 'Machine readable PDF' in result.snapshot
    assert all(s['physical_page']==1 for s in result.lineage['spans'])


def xmp_author(value):
    return f'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:creator><rdf:Seq><rdf:li>{value}</rdf:li></rdf:Seq></dc:creator></rdf:Description></rdf:RDF></x:xmpmeta>'


def test_pdf_info_and_xmp_claims_remain_distinct_and_conflicts_fail():
    result = parsed_pdf(pdf_bytes(author="Alice", xmp=xmp_author("Alice")))
    claims = [c for c in result.metadata["document_declared"] if c["role"] == "author"]
    assert [c["structure"] for c in claims] == ["Info", "XMP"]
    with pytest.raises(SourceReadError, match="pdf_metadata_conflict"):
        parsed_pdf(pdf_bytes(author="Alice", xmp=xmp_author("Bob")))


def test_epub_direct_div_tail_and_repeated_text_all_have_unique_occurrences():
    raw = epub_bytes('开头<div>重复<b>强调</b>尾部<p>重复</p>收尾</div>正文尾', extra_body='<p>结束章节</p>', linear="no")
    qualify_epub(raw)
    result = parsed_epub(raw)
    for fragment in ["开头", "重复", "强调", "尾部", "收尾", "正文尾", "结束章节"]:
        assert fragment in result.snapshot
    assert result.snapshot.count("重复") == 2
    spans = [x for block in result.lineage['spans'] for x in block['native_occurrences']]
    assert len({span["occurrence"] for span in spans}) == len(spans)
    repeated = [span for span in spans if span["native_text"] == "重复"]
    assert len(repeated) == 2
    assert repeated[0]["element_path"] != repeated[1]["element_path"]
    assert spans[-1]["linear"] == "no" and spans[-1]["resource"] == "OEBPS/two.xhtml"
    assert result.lineage["snapshot_sha256"] == hashlib.sha256(result.snapshot.encode()).hexdigest()
    json.dumps(result.metadata)


def test_epub_multiple_creators_and_roles_are_preserved_without_guessing_contributors():
    result = parsed_epub(epub_bytes(metadata='<dc:creator id="a">甲</dc:creator><dc:creator>乙</dc:creator><dc:contributor>译者</dc:contributor><meta refines="#a" property="role">aut</meta>'))
    assert result.metadata["author"]["display_name"] == "甲、乙"
    assert any(c["key"] == "contributor" and c["value"] == "译者" for c in result.metadata["document_declared"])
    with pytest.raises(SourceReadError, match="epub_metadata_conflict"):
        parsed_epub(epub_bytes(metadata='<dc:creator id="a">甲</dc:creator><meta refines="#a" property="role">aut</meta><meta refines="#a" property="role">trl</meta>'))


@pytest.mark.parametrize("options,code", [({"missing": True}, "epub_chapter_missing"), ({"body": ""}, "epub_empty_content"), ({"extra_spine": '<itemref idref="first"/>'}, "epub_spine_invalid"), ({"body": '<img src="scan.png"/>'}, "epub_image_missing"), ({"body": '页眉<img src="scan.png"/>'}, "epub_image_missing"), ({"body": '<script>generate()</script>'}, "epub_content_unsupported"), ({"body_attributes": 'hidden="hidden"'}, "epub_content_unsupported"), ({"css": "p { display:none }"}, "epub_content_unsupported"), ({"css": "p:before {content:'generated'}"}, "epub_content_unsupported"), ({"css": "p { columns:2 }"}, "epub_content_unsupported")])
def test_epub_whole_source_fails_for_incomplete_hidden_or_generated_content(options, code):
    with pytest.raises(SourceReadError, match=code):
        parsed_epub(epub_bytes(**options))


def test_epub_typography_css_and_explicit_decorative_image_are_supported():
    result = parsed_epub(epub_bytes(body='<p>正文<img role="presentation" alt="" src="ornament.png"/></p>', css="p { font-size: 12pt; margin: 1em; text-align: left; }"))
    assert "正文" in result.snapshot


def test_epub_footnote_must_bind_existing_in_scope_occurrence():
    body = '正文<a epub:type="noteref" href="two.xhtml#n1">1</a>'
    assert "脚注" in parsed_epub(epub_bytes(body, extra_body='<aside id="n1">脚注</aside>', linear="no")).snapshot
    with pytest.raises(SourceReadError, match="epub_footnote_missing"):
        parsed_epub(epub_bytes(body, extra_body='<aside id="wrong">脚注</aside>'))


def test_epub_protection_and_unknown_container_fail_preaccept():
    for raw, code in [(epub_bytes(protected=True), "epub_protected"), (b"PKbroken", "epub_protection_unknown"), (b"wrong", "epub_wrong_type")]:
        with pytest.raises(SourceReadError, match=code):
            qualify_epub(raw)


def test_document_snapshot_integrity_is_checked():
    for parser, raw in [(parse_pdf, pdf_bytes()), (parse_epub, epub_bytes())]:
        with pytest.raises(SourceReadError, match="file_snapshot_mismatch"):
            parser(raw, "name", "wrong-key")


def test_pdf_declared_artifact_rule_does_not_block_readable_body():
    result = parsed_pdf(pdf_bytes(stream_suffix=b" /Artifact BMC 72 710 m 540 710 l S EMC"))
    assert result.snapshot == "Machine readable PDF"


def test_pdf_plain_link_and_page_rotation_preserve_body_order():
    reader = PdfReader(io.BytesIO(pdf_bytes()))
    writer = PdfWriter()
    writer.append(reader)
    page = writer.pages[0]
    page.rotate(90)
    link = DictionaryObject({NameObject("/Type"): NameObject("/Annot"), NameObject("/Subtype"): NameObject("/Link"), NameObject("/Rect"): ArrayObject([FloatObject(v) for v in [72, 700, 200, 730]]), NameObject("/A"): DictionaryObject({NameObject("/S"): NameObject("/URI"), NameObject("/URI"): TextStringObject("https://example.com")})})
    page[NameObject("/Annots")] = ArrayObject([writer._add_object(link)])
    output = io.BytesIO()
    writer.write(output)
    assert parsed_pdf(output.getvalue()).snapshot == "Machine readable PDF"


def test_epub_body_style_is_validated_but_never_source_prose():
    result = parsed_epub(epub_bytes('<style>p { font-size: 12pt; }</style><p>正文</p>尾部'))
    assert "font-size" not in result.snapshot
    assert "正文" in result.snapshot and "尾部" in result.snapshot


def test_pdf_separated_same_row_columns_are_not_silently_interleaved():
    result=parsed_pdf(pdf_bytes("Left", stream_suffix=b" BT /F1 12 Tf 350 720 Td (Right) Tj ET"))
    assert result.snapshot.index('Left') < result.snapshot.index('Right')
    assert all(span['physical_page']==1 for span in result.lineage['spans'])


def test_epub_origin_conflicts_fail_while_multiple_authors_are_legal():
    with pytest.raises(SourceReadError, match="epub_metadata_conflict"):
        parsed_epub(epub_bytes(metadata='<dc:source>one</dc:source><dc:source>two</dc:source>'))


def test_epub_fixed_layout_declared_metadata_fails():
    with pytest.raises(SourceReadError, match="epub_content_unsupported"):
        parsed_epub(epub_bytes(metadata='<meta property="rendition:layout">pre-paginated</meta>'))


def test_epub_duplicate_zip_member_is_ambiguous_before_accept():
    with pytest.warns(UserWarning):
        raw = epub_bytes(duplicate_resource=True)
    with pytest.raises(SourceReadError, match="epub_protection_unknown"):
        qualify_epub(raw)
