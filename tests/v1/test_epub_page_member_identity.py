"""Real synthetic EPUB/PDF containers; explicitly fake conversion, no models."""
import hashlib
import io
import posixpath
import re
import zipfile
from xml.etree import ElementTree as ET

import pytest
from PIL import Image

from knowledge_distiller.v1.docling_source import (
    DoclingSourceResult, DocumentEntry, DocumentPageImage, DocumentProvenance,
)
from knowledge_distiller.v1.document_source import compose_document, page_member_id
from knowledge_distiller.v1.file_sources import prepare_file, parse_submitted_source
from knowledge_distiller.v1.ocr import OcrResult
from knowledge_distiller.v1.source_parsing import SourceReadError
from .test_local_intake_storage import _synthetic_document_bytes


OPF = 'http://www.idpf.org/2007/opf'
XHTML = 'http://www.w3.org/1999/xhtml'
BODY = 'Repeated source.'


@pytest.fixture
def png():
    output = io.BytesIO()
    Image.new('RGB', (4, 3), (17, 31, 47)).save(output, format='PNG')
    return output.getvalue()


def epub_bytes(png, paths=('one.xhtml', 'two.xhtml'), order=(0, 1), *, pictures=False):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        archive.writestr('mimetype', 'application/epub+zip')
        archive.writestr('META-INF/container.xml',
            '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OEBPS/book.opf" '
            'media-type="application/oebps-package+xml"/></rootfiles></container>')
        manifest = ''.join(f'<item id="chapter-{i}" href="{path}" media-type="application/xhtml+xml"/>'
                           for i, path in enumerate(paths))
        spine = ''.join(f'<itemref idref="chapter-{i}" linear="yes"/>' for i in order)
        archive.writestr('OEBPS/book.opf',
            f'<package xmlns="{OPF}" xmlns:dc="http://purl.org/dc/elements/1.1/" version="3.0">'
            '<metadata><dc:title>Synthetic page identity</dc:title></metadata>'
            f'<manifest>{manifest}</manifest><spine>{spine}</spine></package>')
        picture_resources = set()
        for i, path in enumerate(paths):
            picture = '<img src="picture.png" alt="Original picture"/>' if pictures else ''
            archive.writestr('OEBPS/' + path,
                f'<html xmlns="{XHTML}"><head><title>Chapter {i}</title></head>'
                f'<body><p id="anchor-{i}">{BODY}</p>{picture}</body></html>')
            if pictures:
                picture_resource = posixpath.join('OEBPS', posixpath.dirname(path), 'picture.png')
                if picture_resource not in picture_resources:
                    archive.writestr(picture_resource, png)
                    picture_resources.add(picture_resource)
    return output.getvalue()


class FakeConverter:
    def __init__(self, png, *, physical_page=1):
        self.png = png
        self.physical_page = physical_page
        self.calls = []

    def convert_bytes(self, content, kind):
        if kind == 'epub':
            # Read the actual per-chapter container, not a call-count guess.
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                package = ET.fromstring(archive.read('OEBPS/book.opf'))
                refs = list(package.find(f'{{{OPF}}}spine'))
                assert len(refs) == 1
                item = next(n for n in package.find(f'{{{OPF}}}manifest')
                            if n.get('id') == refs[0].get('idref'))
                resource = posixpath.join('OEBPS', item.get('href'))
                document = ET.fromstring(archive.read(resource))
                paragraph = document.find(f'.//{{{XHTML}}}p')
                text = paragraph.text
                picture = document.find(f'.//{{{XHTML}}}img') is not None
                self.calls.append(resource)
        else:
            assert kind == 'pdf' and content.startswith(b'%PDF-')
            text, picture = 'Synthetic original source.', False
            self.calls.append('pdf')
        entries = [DocumentEntry('text', text,
            (DocumentProvenance(page=self.physical_page, charspan=(0, len(text))),), '#text-1')]
        if picture:
            entries.append(DocumentEntry('image', '', (), '#picture-1', self.png, 'image/png'))
        return DoclingSourceResult(tuple(entries), self.physical_page, runtime_version='synthetic-converter-1',
            page_images=(DocumentPageImage(self.physical_page, 4, 3, self.png),))


class FakeOcr:
    def __init__(self, png, *, allow=False):
        self.png, self.allow, self.calls = png, allow, 0

    def recognize_bytes(self, content, mime):
        assert self.allow, 'text-only fixture must not request OCR'
        assert content == self.png and mime == 'image/png'
        self.calls += 1
        return OcrResult(4, 3, (), engine='synthetic-no-text', runtime_version='fixture-1')


def parsed_epub(png, paths=('one.xhtml', 'two.xhtml'), order=(0, 1), *, physical_page=1, pictures=False):
    source = prepare_file('synthetic.epub', epub_bytes(png, paths, order, pictures=pictures))
    converter, ocr = FakeConverter(png, physical_page=physical_page), FakeOcr(png, allow=pictures)
    parsed = parse_submitted_source(source, converter=converter, ocr=ocr)
    return source, parsed, converter, ocr


def test_two_chapters_page_one_same_png_preserve_both_occurrences(png):
    source, parsed, converter, ocr = parsed_epub(png)
    assert converter.calls == ['OEBPS/one.xhtml', 'OEBPS/two.xhtml'] and ocr.calls == 0
    assert parsed.snapshot == BODY + '\n\n' + BODY
    assert parsed.lineage['version'] == 3 and parsed.lineage['source_key'] == source.source_key
    assert parsed.lineage['snapshot_sha256'] == hashlib.sha256(parsed.snapshot.encode()).hexdigest()
    assert len(parsed.media) == 2 and len({m.member_id for m in parsed.media}) == 2
    for position, (member, chapter, span) in enumerate(zip(parsed.media, parsed.lineage['chapters'], parsed.lineage['spans'])):
        assert member.content == png and member.mime_type == 'image/png'
        with Image.open(io.BytesIO(member.content)) as image:
            assert image.format == 'PNG' and image.size == (4, 3)
        assert re.fullmatch(r'page-epub-[1-9][0-9]*-[0-9a-f]{64}-1', member.member_id)
        assert chapter['pages'] == [{'member_id': member.member_id, 'physical_page': 1, 'width': 4, 'height': 3}]
        assert chapter['spine'] == position + 1 and chapter['resource'] == converter.calls[position]
        assert chapter['linear'] == 'yes' and chapter['anchors'] == [f'anchor-{position}'] and chapter['links'] == []
        assert span['resource'] == chapter['resource'] and span['physical_page'] == 1
        assert parsed.snapshot[span['start']:span['end']] == BODY
        native, = span['native_occurrences']
        assert native['native_text'] == BODY and native['spine'] == position + 1
        assert native['resource'] == chapter['resource'] and native['element_path'] == 'body/p[0]'
        assert native['start'] == 0 and native['end'] == len(BODY)  # chapter-local, unchanged
    assert [s['start'] for s in parsed.lineage['spans']] == [0, len(BODY) + 2]
    assert parsed.uncertainties == () and parsed.lineage['image_ocr'] == []


def test_actual_resource_spine_and_physical_page_define_deterministic_identity(png):
    _, first, _, _ = parsed_epub(png)
    _, replay, _, _ = parsed_epub(png)
    assert first.media == replay.media and first.lineage == replay.lineage
    _, renamed, _, _ = parsed_epub(png, paths=('one.xhtml', 'parts/第二章.xhtml'))
    assert renamed.media[0].member_id == first.media[0].member_id
    assert renamed.media[1].member_id != first.media[1].member_id
    _, reordered, converter, _ = parsed_epub(png, order=(1, 0))
    assert converter.calls == ['OEBPS/two.xhtml', 'OEBPS/one.xhtml']
    assert reordered.media[0].member_id != first.media[1].member_id
    assert reordered.media[1].member_id != first.media[0].member_id
    _, different_page, _, _ = parsed_epub(png, physical_page=2)
    assert all(a.member_id != b.member_id for a, b in zip(first.media, different_page.media))
    assert all(c['pages'][0]['physical_page'] == 2 for c in different_page.lineage['chapters'])
    assert all(p.snapshot == first.snapshot for p in (renamed, reordered, different_page))


def test_picture_ids_media_offset_and_full_original_order_unchanged(png):
    _, parsed, _, ocr = parsed_epub(png, pictures=True)
    assert ocr.calls == 2
    assert len(parsed.media) == 4
    assert parsed.media[0].member_id == 'image-1' and parsed.media[2].member_id == 'image-3'
    assert [c['pages'][0]['member_id'] for c in parsed.lineage['chapters']] == [
        parsed.media[1].member_id, parsed.media[3].member_id]
    assert [m.content for m in parsed.media] == [png] * 4  # never dedup equal bytes
    assert [i['member_id'] for i in parsed.lineage['image_ocr']] == ['image-1', 'image-3']
    assert parsed.snapshot == BODY + '\n\n[原图 image-1]\n\n' + BODY + '\n\n[原图 image-3]'


def test_pdf_no_context_preserves_original_page_member_and_lineage(png):
    source = prepare_file('synthetic.pdf', _synthetic_document_bytes('pdf'))
    converter, ocr = FakeConverter(png), FakeOcr(png)
    parsed = parse_submitted_source(source, converter=converter, ocr=ocr)
    assert converter.calls == ['pdf'] and ocr.calls == 0
    assert parsed.snapshot == 'Synthetic original source.'
    assert [m.member_id for m in parsed.media] == ['page-1'] and parsed.media[0].content == png
    assert parsed.lineage['version'] == 2
    assert parsed.lineage['pages'] == [{'page': 1, 'width': 4, 'height': 3, 'member_id': 'page-1'}]


@pytest.mark.parametrize('context', [
    {}, {'spine': True, 'resource': 'OEBPS/one.xhtml'}, {'spine': 0, 'resource': 'OEBPS/one.xhtml'},
    {'spine': 1, 'resource': ''}, {'spine': 1, 'resource': '../one.xhtml'},
    {'spine': 1, 'resource': '/one.xhtml'}, {'spine': 1, 'resource': 'OEBPS/../one.xhtml'},
    {'spine': 1, 'resource': 'OEBPS\\one.xhtml'}, {'spine': 1, 'resource': 'OEBPS/one\x00.xhtml'},
    {'spine': 1, 'resource': 'OEBPS/\ud800.xhtml'},
])
def test_bad_epub_context_cannot_mint_page_identity(png, context):
    converted = DoclingSourceResult((), 1, page_images=(DocumentPageImage(1, 4, 3, png),))
    with pytest.raises(SourceReadError, match='^docling_invalid_output$'):
        compose_document(converted, context=context, ocr=FakeOcr(png))


@pytest.mark.parametrize('page', [True, 0, -1, '1'])
def test_epub_physical_page_must_be_typed_positive(page):
    with pytest.raises(SourceReadError, match='^docling_invalid_output$'):
        page_member_id(page, context={'spine': 1, 'resource': 'OEBPS/one.xhtml'})
