"""Whole synthetic containers, actual opaque issuers, explicitly fake libraries.

No real Docling model, Apple Vision or production qualification is exercised.
The private dependency boundaries reuse the independently reviewed D1/D2 fakes.
"""
from copy import copy
from dataclasses import FrozenInstanceError, replace
import gc
import hashlib
import io
import json
import posixpath
from types import SimpleNamespace as NS
import zipfile
from xml.etree import ElementTree as ET

import pytest
from pypdf import PdfReader, PdfWriter

from knowledge_distiller.v1 import document_source as aggregate
from knowledge_distiller.v1 import docling_source as conversion
from knowledge_distiller.v1 import ocr, vision_ocr
from knowledge_distiller.v1.file_sources import prepare_file, parse_submitted_source
from knowledge_distiller.v1.source_parsing import SourceReadError

from .test_document_conversion_receipt import default_adapter, Cell
from .test_image_ocr_receipt import native, png
from .test_epub_page_member_identity import epub_bytes, OPF, XHTML, BODY


def pdf_bytes(pages=2):
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=80, height=60)
    writer.add_metadata({'/Title': '原声明 é\r\nPDF'})
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def whole_epub():
    """Both equal-content chapters remain distinct, including linear=no."""
    original = epub_bytes(png(), paths=('one/ch.xhtml', 'two/ch.xhtml'))
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(original)) as source, zipfile.ZipFile(output, 'w') as target:
        for info in source.infolist():
            raw = source.read(info.filename)
            if info.filename == 'OEBPS/book.opf':
                tree = ET.fromstring(raw)
                list(tree.find(f'{{{OPF}}}spine'))[1].set('linear', 'no')
                raw = ET.tostring(tree, encoding='utf-8')
            elif info.filename.endswith('.xhtml'):
                tree = ET.fromstring(raw)
                body = tree.find(f'{{{XHTML}}}body')
                ET.SubElement(body, f'{{{XHTML}}}p').text = ' \n '
                raw = ET.tostring(tree, encoding='utf-8')
            target.writestr(info, raw)
    return output.getvalue()


@pytest.fixture
def controlled(default_adapter, native, monkeypatch):
    """D1/D2 issue normally; only upstream libraries/native environment are fake.

    The fake translator below does not prove that PDF text is semantically right.
    D3 must consequently retain coverage=unverified even with a valid receipt.
    """
    adapter, state, inventory = default_adapter
    state.mode = 'native'
    adapter.convert_bytes(b'%PDF-synthetic', 'pdf')  # initialize reviewed controlled handle
    handle = adapter._converter
    captures = []
    settings = NS(images=False, empty_entry=False)

    def convert(stream, raises_on_error):
        assert raises_on_error
        raw = stream.stream.getvalue()
        is_pdf = stream.name.endswith('pdf')
        if is_pdf:
            page_count = len(PdfReader(io.BytesIO(raw), strict=True).pages)
            texts = [f'PDF page {page}: 原文 é\r\n重复' for page in range(1, page_count+1)]
        else:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                package = ET.fromstring(archive.read('OEBPS/book.opf'))
                refs = list(package.find(f'{{{OPF}}}spine'))
                assert len(refs) == 1
                manifest = {item.get('id'): item for item in package.find(f'{{{OPF}}}manifest')}
                resource = posixpath.normpath('OEBPS/'+manifest[refs[0].get('idref')].get('href'))
                document = ET.fromstring(archive.read(resource))
                texts = [''.join(document.find(f'{{{XHTML}}}body').itertext())]
            page_count = 1
        entries = [conversion.DocumentEntry('text', text,
            (conversion.DocumentProvenance(page=i, charspan=(0, len(text))),), f'#text-{i}',
            table_data={'cells': ['actual translated diagnostic']}) for i, text in enumerate(texts, 1)]
        if settings.images:
            entries.append(conversion.DocumentEntry('image', '', (), '#picture-1', png(), 'image/png'))
        if settings.empty_entry:
            entries.append(conversion.DocumentEntry('text', '', (), '#empty'))
        translated = conversion.DoclingSourceResult(tuple(entries), page_count,
            {'declaration': {'value': '原文声明'}}, page_images=tuple(
                conversion.DocumentPageImage(i, 8, 6, png()) for i in range(1, page_count+1)))
        result = NS(status='success', errors=[], document=translated)
        if is_pdf:
            fmt = handle.formats['pdf']
            pipeline = fmt.pipeline_cls(fmt.pipeline_options)
            pipeline._create_run_ctx()
            backend = NS(is_valid=lambda: True,
                         get_visible_text_cells=lambda: [Cell('native 原文')],
                         get_text_cells=lambda: [Cell('native 原文')])
            pages = [NS(page_no=i, _backend=backend, size=NS(width=8, height=6),
                        parsed_page=NS(), cells=[Cell(text)]) for i, text in enumerate(texts, 1)]
            actual_pages = list(fmt.backend(pages).iter_pages())
            model = pipeline._make_ocr_model(inventory.active)
            assert list(model(result, actual_pages)) == pages
        captures.append((raw, translated))
        return result

    monkeypatch.setattr(handle.converter, 'convert', convert)
    # No converter/ocr argument is supplied to the new public parse entry point.
    # These are explicit test-only replacements of the default dependency closure.
    monkeypatch.setattr(conversion, 'DoclingSourceConverter', lambda: adapter)
    monkeypatch.setattr(ocr, 'default_ocr_runner', lambda: vision_ocr.VisionOcrRunner())
    return NS(adapter=adapter, captures=captures, settings=settings, native=native)


def test_pdf_whole_original_references_entries_and_all_page_images(controlled):
    source = prepare_file('Chapter:1.pdf', pdf_bytes())
    parsed = aggregate.parse_document_with_receipt(source)
    audit = aggregate.validate_document_receipt(parsed, source)
    assert len(controlled.captures) == 1 and controlled.captures[0][0] == source.content
    assert audit['input']['key'] == source.source_key and audit['input']['label'] == source.label
    unit, = audit['units']
    assert unit['input']['sha256'] == source.source_key
    assert [r['physical_page'] for r in unit['ordered_page_references']] == [1, 2]
    reader = PdfReader(io.BytesIO(source.content), strict=True)
    assert [(r['object_number'], r['generation']) for r in unit['ordered_page_references']] == [
        (p.indirect_reference.idnum, p.indirect_reference.generation) for p in reader.pages]
    assert [m.member_id for m in parsed.media] == ['page-1', 'page-2']
    assert parsed.media[0].content == parsed.media[1].content == png()
    assert [row['member_id'] for row in unit['page_images']] == ['page-1', 'page-2']
    for entry in unit['entries']:
        assert parsed.snapshot[entry['start']:entry['end']] == entry['output_text']
        assert entry['ref'] == entry['input_entry']['ref']
    page, _ = unit['conversion']['pages']
    assert page['native_cell_count'] == 1 and page['native_cells_sha256']
    assert 'native_cells' not in page and unit['native_occurrences'] is None
    assert audit['coverage'] == 'unverified' and 'native_cells_not_captured' in audit['limitations']
    assert not hasattr(parsed, 'qualified') and 'source_complete' not in audit


def test_epub_exact_generated_chapters_linear_no_and_independent_native_inventory(controlled):
    source = prepare_file('two.epub', whole_epub())
    parsed = aggregate.parse_document_with_receipt(source)
    audit = aggregate.validate_document_receipt(parsed, source)
    assert len(audit['units']) == len(controlled.captures) == 2
    assert [u['context']['linear'] for u in audit['units']] == ['yes', 'no']
    assert [u['context']['resource'] for u in audit['units']] == ['OEBPS/one/ch.xhtml', 'OEBPS/two/ch.xhtml']
    assert [u['context']['spine'] for u in audit['units']] == [1, 2]
    assert audit['units'][0]['input'] != audit['units'][1]['input']
    assert parsed.media[0].content == parsed.media[1].content
    assert parsed.media[0].member_id != parsed.media[1].member_id
    assert [c['pages'][0]['physical_page'] for c in parsed.lineage['chapters']] == [1, 1]
    with zipfile.ZipFile(io.BytesIO(source.content)) as archive:
        assert [row['name'] for row in audit['zip_inventory']] == archive.namelist()
        for row in audit['zip_inventory']:
            raw = archive.read(row['name'])
            assert row['actual_size'] == len(raw) and row['sha256'] == hashlib.sha256(raw).hexdigest()
    for unit, (raw, _) in zip(audit['units'], controlled.captures, strict=True):
        assert unit['input']['sha256'] == hashlib.sha256(raw).hexdigest()
        with zipfile.ZipFile(io.BytesIO(raw)) as chapter:
            package = ET.fromstring(chapter.read('OEBPS/book.opf'))
            ref, = list(package.find(f'{{{OPF}}}spine'))
            assert ref.attrib == unit['declared_spine']
        assert parsed.snapshot[unit['global_start']:unit['global_end']] == unit['snapshot']
        matched = [row for row in unit['native_occurrences'] if row['local_start'] is not None]
        assert any(BODY == row['native_text'] for row in matched)
        assert any(row['locator_capability'] == 'whitespace_or_separator' for row in unit['native_occurrences'])
        assert len({r['inventory_occurrence'] for r in unit['native_occurrences']}) == len(unit['native_occurrences'])
        for row in matched:
            assert unit['snapshot'][row['local_start']:row['local_end']] == row['matched_snapshot']
            assert parsed.snapshot[row['global_start']:row['global_end']] == row['matched_snapshot']
        assert unit['conversion']['pages'] == []  # Simple EPUB recipe is not PDF OCR.
    old = parse_submitted_source(source)
    assert replace(parsed, receipt=None) == old  # No images: exact old values/native spans/page IDs.


def test_compose_images_use_original_d2_objects_and_keep_each_entry(controlled):
    controlled.settings.images = True
    controlled.settings.empty_entry = True
    source = prepare_file('image.pdf', pdf_bytes(1))
    parsed = aggregate.parse_document_with_receipt(source)
    audit = aggregate.validate_document_receipt(parsed, source)
    unit, = audit['units']
    assert [e['ref'] for e in unit['entries']] == ['#text-1', '#picture-1', '#empty']
    assert unit['entries'][-1]['start'] is None and unit['entries'][-1]['end'] is None
    assert [m.member_id for m in parsed.media] == ['image-1', 'page-1']
    assert controlled.native.calls == 1
    result, data, mime, member, _ = parsed.receipt._images[0]
    assert unit['image_executions'][0]['audit'] == vision_ocr.validate_image_receipt(result, data, mime, member)
    assert member == 'image-1' and data == parsed.media[0].content
    object.__setattr__(result.lines[0], 'text', 'changed after parse')
    with pytest.raises(SourceReadError, match='^document_receipt_invalid$'):
        aggregate.validate_document_receipt(parsed, source)


@pytest.mark.parametrize('part', ['chapter_order', 'span_omission', 'native_local', 'snapshot_sha'])
def test_epub_original_chapters_occurrences_and_snapshot_cannot_drift(controlled, part):
    source = prepare_file('whole.epub', whole_epub())
    parsed = aggregate.parse_document_with_receipt(source)
    if part == 'chapter_order': parsed.lineage['chapters'].reverse()
    elif part == 'span_omission': parsed.lineage['spans'].pop()
    elif part == 'native_local': parsed.lineage['spans'][0]['native_occurrences'][0]['start'] += 1
    else: parsed.lineage['snapshot_sha256'] = '0'*64
    with pytest.raises(SourceReadError, match='^document_receipt_invalid$'):
        aggregate.validate_document_receipt(parsed, source)


@pytest.mark.parametrize('change', ['label', 'metadata', 'whole_bytes', 'kind', 'key'])
def test_original_whole_input_declarations_cannot_be_rebound(controlled, change):
    source = prepare_file('original.pdf', pdf_bytes())
    parsed = aggregate.parse_document_with_receipt(source)
    changes = {'label': {'label': 'other.pdf'}, 'metadata': {'metadata': {'declared': 'other'}},
               'whole_bytes': {'content': pdf_bytes(1)}, 'kind': {'source_kind': 'epub'},
               'key': {'source_key': '0'*64}}
    with pytest.raises(SourceReadError, match='^document_receipt_invalid$'):
        aggregate.validate_document_receipt(parsed, replace(source, **changes[change]))


@pytest.mark.parametrize('form', ['copy', 'replace', 'json', 'dict', 'none'])
def test_public_copy_and_json_never_restore_original_object_seal(controlled, form):
    source = prepare_file('source.pdf', pdf_bytes())
    parsed = aggregate.parse_document_with_receipt(source)
    if form == 'copy':
        candidate = copy(parsed)
    elif form == 'replace':
        candidate = replace(parsed)
    else:
        public = parsed.receipt.audit_json
        candidate = replace(parsed, receipt={'json': public, 'dict': json.loads(public), 'none': None}[form])
    with pytest.raises(SourceReadError, match='^document_receipt_invalid$'):
        aggregate.validate_document_receipt(candidate, source)
    with pytest.raises(FrozenInstanceError):
        parsed.receipt._audit = b'{}'
    assert aggregate.validate_document_receipt(parsed, source)['coverage'] == 'unverified'


@pytest.mark.parametrize('part', ['snapshot', 'metadata', 'span', 'media', 'uncertainties', 'child_metadata', 'child_table'])
def test_deep_mutations_are_rejected_by_parent_and_actual_child_validators(controlled, part):
    source = prepare_file('source.pdf', pdf_bytes())
    parsed = aggregate.parse_document_with_receipt(source)
    child = parsed.receipt._conversions[0][0]
    if part == 'snapshot': object.__setattr__(parsed, 'snapshot', 'other')
    elif part == 'metadata': parsed.metadata['source_title'] = 'other'
    elif part == 'span': parsed.lineage['spans'][0]['ref'] = '#other'
    elif part == 'media': object.__setattr__(parsed.media[0], 'content', b'other')
    elif part == 'uncertainties': object.__setattr__(parsed, 'uncertainties', ({'start': 0, 'end': 1},))
    elif part == 'child_metadata': child.metadata['declaration']['value'] = 'other'
    else: child.entries[0].table_data['cells'].append('other')
    with pytest.raises(SourceReadError, match='^document_receipt_invalid$'):
        aggregate.validate_document_receipt(parsed, source)


@pytest.mark.parametrize('kind', ['pdf', 'epub'])
def test_borrowed_issued_child_from_outside_whole_or_wrong_chapter_is_unsigned(controlled, monkeypatch, kind):
    original = prepare_file('one.'+kind, pdf_bytes(1) if kind == 'pdf' else whole_epub())
    if kind == 'pdf':
        outside = original.content+b'\n%outside original submitted bytes\n'
        borrowed = controlled.adapter.convert_bytes(outside, 'pdf')
        assert conversion.validate_conversion_receipt(borrowed, outside, 'pdf')
    else:
        aggregate.parse_document_with_receipt(original)
        borrowed = controlled.adapter.convert_bytes(controlled.captures[0][0], 'epub')
    monkeypatch.setattr(aggregate, 'convert_document', lambda *args, **kwargs: borrowed)
    parsed = aggregate.parse_document_with_receipt(original)
    assert parsed.receipt is None
    with pytest.raises(SourceReadError, match='^document_receipt_invalid$'):
        aggregate.validate_document_receipt(parsed, original)


def test_cropped_pdf_issued_child_is_rejected_before_composition(controlled, monkeypatch):
    source = prepare_file('whole.pdf', pdf_bytes(2))
    cropped = pdf_bytes(1)
    child = controlled.adapter.convert_bytes(cropped, 'pdf')
    assert conversion.validate_conversion_receipt(child, cropped, 'pdf')
    monkeypatch.setattr(aggregate, 'convert_document', lambda *args, **kwargs: child)
    with pytest.raises(SourceReadError, match='^pdf_pages_incomplete$'):
        aggregate.parse_document_with_receipt(source)
    assert source.content == pdf_bytes(2)


@pytest.mark.parametrize('edit', ['omit', 'reverse', 'ref', 'span_ref', 'media_bytes', 'fullslice'])
def test_compose_cannot_omit_reorder_or_relabel_original_entries(controlled, monkeypatch, edit):
    source = prepare_file('source.pdf', pdf_bytes())
    original = aggregate.compose_document
    def changed(converted, **kwargs):
        if edit in {'span_ref', 'media_bytes', 'fullslice'}:
            composed = original(converted, **kwargs)
            if edit == 'span_ref':
                composed.spans[0]['ref'] = '#invented'
            elif edit == 'media_bytes':
                composed = replace(composed, media=(replace(composed.media[0], content=b'other'), *composed.media[1:]))
            else:
                composed = replace(composed, snapshot='other snapshot')
            return composed
        entries = converted.entries
        if edit == 'omit': entries = entries[:1]
        elif edit == 'reverse': entries = tuple(reversed(entries))
        else: entries = (replace(entries[0], ref='#invented'), *entries[1:])
        return original(replace(converted, entries=entries), **kwargs)
    monkeypatch.setattr(aggregate, 'compose_document', changed)
    with pytest.raises(SourceReadError, match='^document_receipt_invalid$'):
        aggregate.parse_document_with_receipt(source)


@pytest.mark.parametrize('dependency', ['converter', 'ocr'])
def test_explicit_injected_dependencies_remain_diagnostic_even_with_issued_children(controlled, dependency):
    source = prepare_file('source.pdf', pdf_bytes())
    kwargs = {'converter': controlled.adapter} if dependency == 'converter' else {'ocr': vision_ocr.VisionOcrRunner()}
    parsed = aggregate.parse_document_with_receipt(source, **kwargs)
    assert parsed.snapshot and parsed.receipt is None


def test_aggregate_holds_original_children_after_adapter_and_cache_references_drop(controlled):
    source = prepare_file('source.pdf', pdf_bytes())
    parsed = aggregate.parse_document_with_receipt(source)
    controlled.captures.clear()
    gc.collect()
    child, actual_input, kind, _ = parsed.receipt._conversions[0]
    assert actual_input is source.content and kind == 'pdf'
    assert conversion.validate_conversion_receipt(child, actual_input, kind)
    assert aggregate.validate_document_receipt(parsed, source)['output']['snapshot'] == parsed.snapshot


@pytest.mark.parametrize('form', ['copy', 'json', 'cache'])
def test_unissued_or_reconstructed_conversion_result_is_diagnostic(controlled, monkeypatch, form):
    source = prepare_file('source.pdf', pdf_bytes())
    child = controlled.adapter.convert_bytes(source.content, 'pdf')
    if form == 'copy': candidate = copy(child)
    elif form == 'json': candidate = replace(child, receipt=json.loads(child.receipt.audit_json))
    else: candidate = replace(child, receipt=None)
    monkeypatch.setattr(aggregate, 'convert_document', lambda *args, **kwargs: candidate)
    parsed = aggregate.parse_document_with_receipt(source)
    assert parsed.snapshot and parsed.receipt is None


def test_unsigned_image_result_cannot_be_promoted_by_public_image_lineage(controlled, monkeypatch):
    controlled.settings.images = True
    source = prepare_file('source.pdf', pdf_bytes(1))
    unsigned = ocr.OcrResult(8, 6, (), engine='synthetic-cache')
    runner = NS(recognize_bytes=lambda *args: unsigned)
    monkeypatch.setattr(ocr, 'default_ocr_runner', lambda: runner)
    parsed = aggregate.parse_document_with_receipt(source)
    assert parsed.snapshot and parsed.receipt is None
    assert controlled.native.calls == 0
    assert 'execution_audit' not in parsed.lineage['image_ocr'][0]


def test_validation_is_pure_and_returned_public_audit_is_not_authority(controlled, monkeypatch):
    source = prepare_file('source.pdf', pdf_bytes())
    parsed = aggregate.parse_document_with_receipt(source)
    def forbidden(*args, **kwargs):
        raise AssertionError('validation must not run conversion/OCR')
    monkeypatch.setattr(aggregate, 'convert_document', forbidden)
    monkeypatch.setattr(conversion, 'DoclingSourceConverter', forbidden)
    monkeypatch.setattr(ocr, 'default_ocr_runner', forbidden)
    public = aggregate.validate_document_receipt(parsed, source)
    public['coverage'] = 'complete'
    public['units'].clear()
    assert aggregate.validate_document_receipt(parsed, source)['coverage'] == 'unverified'
    assert len(controlled.captures) == 1


def test_old_default_parse_stays_unsigned_and_unsupported_whole_kind_is_rejected(controlled):
    source = prepare_file('source.pdf', pdf_bytes())
    old = parse_submitted_source(source)
    assert old.receipt is None
    new = aggregate.parse_document_with_receipt(source)
    assert replace(new, receipt=None) == old
    with pytest.raises(SourceReadError, match='^document_receipt_invalid$'):
        aggregate.parse_document_with_receipt(replace(source, source_kind='markdown'))
