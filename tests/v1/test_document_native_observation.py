"""Finite v2 observations: actual data classes, explicitly synthetic execution.

No parser/model/native inference is run. A synthetic PageParseResult is populated
without its native decoder constructor; cached segmented data is provided by this
fixture. These are object/field association tests, not real source qualification.
"""
import ast
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from types import SimpleNamespace as NS

import numpy as np
import pytest
from docling_core.types.doc.base import BoundingBox, CoordOrigin, Size
from docling_core.types.doc.page import (TextCell, PdfTextCell, BoundingRectangle,
    PdfCellRenderingMode, PdfPageGeometry, PdfPageBoundaryType, SegmentedPdfPage)
from docling.datamodel.base_models import Page
from docling.backend.docling_parse_backend import ThreadedDoclingParsePageBackend
from docling_parse.pdf_parser import PageParseResult
from rapidocr.utils.output import RapidOCROutput

from knowledge_distiller.v1 import docling_source as source
from .test_document_conversion_receipt import default_adapter


def cell(index=7, *, hidden=False):
    return PdfTextCell(index=index, text='重复 é\r\n原文', orig='重复 é\r\n原文',
        rect=BoundingRectangle(r_x0=.123456789123, r_y0=0., r_x1=4., r_y1=0.,
            r_x2=4., r_y2=2., r_x3=.123456789123, r_y3=2., coord_origin=CoordOrigin.TOPLEFT),
        confidence=.987654321123, from_ocr=False,
        rendering_mode=PdfCellRenderingMode.INVISIBLE if hidden else PdfCellRenderingMode.FILL_TEXT,
        widget=False, font_key='F1', font_name='合成字体')


def typed_page(cells=None, *, page_no=1):
    cells = [cell(7), cell(8, hidden=True)] if cells is None else cells
    box = BoundingBox(l=0., t=0., r=8., b=6., coord_origin=CoordOrigin.TOPLEFT)
    geometry = PdfPageGeometry(angle=0., rect=BoundingRectangle(r_x0=0., r_y0=0.,
        r_x1=8., r_y1=0., r_x2=8., r_y2=6., r_x3=0., r_y3=6., coord_origin=CoordOrigin.TOPLEFT),
        boundary_type=PdfPageBoundaryType.CROP_BOX, art_bbox=box, bleed_bbox=box,
        crop_bbox=box, media_bbox=box, trim_bbox=box)
    segmented = SegmentedPdfPage(dimension=geometry, char_cells=[], word_cells=[],
                                 textline_cells=cells, has_lines=True)
    # Explicit synthetic decoder boundary: never invoke raw_result.get/native parser.
    parse = object.__new__(PageParseResult)
    parse.page_number, parse.success, parse.doc_key = page_no, True, 'synthetic-parser-document'
    backend = ThreadedDoclingParsePageBackend(parse)
    backend._seg_page = segmented
    page = Page(page_no=page_no, size=Size(width=8., height=6.), parsed_page=segmented)
    page._backend = backend
    return page, backend


@pytest.fixture
def known_runtime(default_adapter, monkeypatch):
    """Retain original fake constructor/model closure; supply real known type exports."""
    import sys
    monkeypatch.setattr(sys.modules['docling.datamodel.base_models'], 'Page', Page, raising=False)
    monkeypatch.setattr(sys.modules['docling.backend.docling_parse_backend'],
                        'ThreadedDoclingParsePageBackend', ThreadedDoclingParsePageBackend, raising=False)
    return default_adapter


def observed_page():
    page, backend = typed_page()
    parent = NS(doc_key=backend._result.doc_key)
    conversion = NS(input=NS(_backend=parent))
    audit = source._ConversionAudit()
    audit.begin()
    audit.backend_pages.append((parent, backend))
    record = audit.enter(conversion, page)
    selected = backend.get_visible_text_cells()
    source._observe_native(audit, record, page, selected, 'visible_cells')
    return audit, record, page, backend, selected


def finish_observation(record, page):
    source._observe_prepost(record, page, [], before=True)
    for i, c in enumerate(page.cells):
        c.index = i  # Exactly the upstream in-place index behavior under test.
    source._observe_prepost(record, page, [], before=False)
    record['observation']['status'] = 'captured' if not record['observation']['reason_codes'] else 'unknown'


@pytest.fixture
def closed_known(known_runtime, monkeypatch):
    adapter, state, inventory = known_runtime
    adapter.convert_bytes(b'%PDF-synthetic', 'pdf')
    handle = adapter._converter
    import sys
    base = sys.modules['docling.models.stages.ocr.rapid_ocr_model'].RapidOcrModel
    def post(self, cells, page, conv_res, priority=None):
        final = list(cells)+list(page.cells)
        for i, c in enumerate(final):
            c.index = i
        page.parsed_page.textline_cells = final
    monkeypatch.setattr(base, 'post_process_cells', post)
    calls = []
    def convert(stream, raises_on_error):
        assert raises_on_error and stream.stream.getvalue() == b'%PDF-synthetic'
        page, backend = typed_page()
        fmt = handle.formats['pdf']
        parent = fmt.backend([backend])
        parent.doc_key = backend._result.doc_key
        result = NS(status='success', errors=[], document=state.translated,
                    input=NS(_backend=parent))
        assert list(parent.iter_pages()) == [backend]
        pipeline = fmt.pipeline_cls(fmt.pipeline_options)
        pipeline._create_run_ctx()
        model = pipeline._make_ocr_model(inventory.active)
        assert list(model(result, [page])) == [page]
        calls.append((page, backend))
        return result
    monkeypatch.setattr(handle.converter, 'convert', convert)
    return adapter, calls


def test_controlled_v2_signs_actual_known_ordered_observations_without_sourcecomplete(closed_known):
    adapter, calls = closed_known
    result = adapter.convert_bytes(b'%PDF-synthetic', 'pdf')
    audit = source.validate_conversion_receipt(result, b'%PDF-synthetic', 'pdf')
    assert audit['protocol'] == audit['recipe']['adapter_revision'] == 'document-conversion-execution-v2'
    observation = audit['pages'][0]['observation']
    assert observation['status'] == 'captured' and observation['reason_codes'] == []
    native, post = observation['native'], observation['prepost']
    assert len(native['all_textline_cells']) == 2 and len(native['selected_cells']) == 1
    assert native['selected_all_ordinals'] == [0]
    assert native['all_textline_cells'][0]['local_ref'] != native['all_textline_cells'][1]['local_ref']
    assert native['all_textline_cells'][0]['fields']['text'] == native['all_textline_cells'][1]['fields']['text']
    assert [r['fields']['index'] for r in post['primary_cells']] == [7, 8]
    assert [r['fields']['index'] for r in post['final_cells']] == [0, 1]
    assert [r['local_ref'] for r in post['primary_cells']] == [r['local_ref'] for r in post['final_cells']]
    assert native['all_textline_cells'][1]['fields']['rendering_mode'] == 3
    assert calls[0][0].cells[0].index == 0
    assert result.entries and not hasattr(result, 'qualified') and 'source_complete' not in audit


def test_default_old_fake_cells_and_backend_stay_unknown_without_changing_result(default_adapter):
    adapter, state, _ = default_adapter
    result = adapter.convert_bytes(b'%PDF-synthetic', 'pdf')
    audit = source.validate_conversion_receipt(result, b'%PDF-synthetic', 'pdf')
    observation = audit['pages'][0]['observation']
    assert observation['status'] == 'unknown' and observation['native'] is None
    assert observation['prepost'] is None and observation['reason_codes']
    assert audit['pages'][0]['region_calls'][0]['reader_output']['status'] == 'unknown'
    assert result.entries == state.translated.entries


def test_observer_unknown_does_not_replace_successful_delegate_output(closed_known, monkeypatch):
    adapter, calls = closed_known
    def missing(*args, **kwargs):
        raise ValueError('synthetic private observer detail')
    monkeypatch.setattr(source, '_cell_rows', missing)
    result = adapter.convert_bytes(b'%PDF-synthetic', 'pdf')
    audit = source.validate_conversion_receipt(result, b'%PDF-synthetic', 'pdf')
    assert len(calls) == 1 and [c.index for c in calls[0][0].cells] == [0, 1]
    assert result.entries and audit['pages'][0]['observation']['status'] == 'unknown'
    assert 'invalid_cell_fields' in audit['pages'][0]['observation']['reason_codes']
    assert 'private' not in result.receipt.audit_json


def test_real_delegate_post_failure_keeps_handle_unusable_and_never_signs(closed_known, monkeypatch):
    import sys
    adapter, calls = closed_known
    def fail(*args, **kwargs):
        raise RuntimeError('synthetic delegate failure')
    base = sys.modules['docling.models.stages.ocr.rapid_ocr_model'].RapidOcrModel
    monkeypatch.setattr(base, 'post_process_cells', fail)
    with pytest.raises(source.DoclingSourceError, match='^docling_conversion_failed$'):
        adapter.convert_bytes(b'%PDF-synthetic', 'pdf')
    assert adapter._converter.unusable and not calls
    record, = adapter._converter.audit.records
    assert record['post_process'] is None
    assert record['observation']['prepost']['final_cells'] is None  # Reached actual pre-delegate stage.


def test_reader_delegate_failure_propagates_before_any_output_observation():
    audit = source._ConversionAudit(); audit.begin()
    record = audit.enter(NS(), NS(page_no=1))
    original = RuntimeError('synthetic reader failure')
    def fail(*args, **kwargs): raise original
    with pytest.raises(RuntimeError) as caught:
        source._AuditedReader(fail, audit)('pixels')
    assert caught.value is original and record['region_calls'][0]['status'] == 'failed'
    assert 'reader_output' not in record['region_calls'][0]
    audit.leave()


def test_raw_finite_fields_preserve_unicode_crlf_precision_without_serializer_rounding():
    original = cell()
    record = {'_cell_refs': []}
    row, = source._cell_rows(record, [original])
    assert row['fields']['text'] == '重复 é\r\n原文'
    assert row['fields']['rect']['r_x0'] == .123456789123
    assert row['fields']['confidence'] == .987654321123
    assert original.model_dump(mode='json')['rect']['r_x0'] == .123456789123
    rounded = original.model_dump(mode='json', context={'coord_prec': 2, 'confid_prec': 2})
    assert rounded['rect']['r_x0'] == .12 and rounded['confidence'] == .99
    assert source._cell_rows(record, [original])[0]['fields'] == row['fields']


@pytest.mark.parametrize('bad', ['subclass', 'fake', 'nan', 'inf', 'bool_index', 'bool_confidence', 'int_confidence', 'int_coordinate', 'nul', 'surrogate', 'nested', 'enum'])
def test_unknown_or_invalid_cell_does_not_become_known_capture(bad):
    value = cell()
    if bad == 'subclass':
        class Child(PdfTextCell): pass
        value = Child(**value.model_dump())
    elif bad == 'fake': value = NS(**value.model_dump())
    elif bad == 'nan': object.__setattr__(value.rect, 'r_x0', float('nan'))
    elif bad == 'inf': object.__setattr__(value, 'confidence', float('inf'))
    elif bad == 'bool_index': object.__setattr__(value, 'index', True)
    elif bad == 'bool_confidence': object.__setattr__(value, 'confidence', True)
    elif bad == 'int_confidence': object.__setattr__(value, 'confidence', 1)
    elif bad == 'int_coordinate': object.__setattr__(value.rect, 'r_x0', 0)
    elif bad == 'nul': object.__setattr__(value, 'text', 'bad\x00')
    elif bad == 'surrogate': object.__setattr__(value, 'orig', '\ud800')
    elif bad == 'nested': object.__setattr__(value, 'rgba', {'r': 0})
    else: object.__setattr__(value, 'text_direction', 'left_to_right')
    with pytest.raises((ValueError, TypeError, UnicodeError)):
        source._cell_rows({'_cell_refs': []}, [value])


def test_plain_text_cell_and_new_ocr_cell_keep_known_type_original_fields():
    digital = TextCell(**cell().model_dump(exclude={'rendering_mode', 'widget', 'font_key', 'font_name'}))
    actual_ocr = TextCell(index=-1, rect=cell().rect, text='OCR é\r\n', orig='OCR é\r\n',
                          confidence=.91, from_ocr=True)
    rows = source._cell_rows({'_cell_refs': []}, [digital, actual_ocr])
    assert [r['type'] for r in rows] == ['docling_core.types.doc.page.TextCell']*2
    assert [r['fields']['from_ocr'] for r in rows] == [False, True]
    assert rows[1]['fields']['index'] == -1 and rows[1]['fields']['orig'] == 'OCR é\r\n'
    assert 'rendering_mode' not in rows[0]['fields'] and rows[0]['local_ref'] != rows[1]['local_ref']


@pytest.mark.parametrize('change', ['missing_segmented', 'not_materialized', 'unregistered', 'wrong_parent', 'wrong_page', 'zero_based', 'wrong_doc_key', 'duplicate_all', 'borrowed_selected'])
def test_page_and_selection_association_gaps_are_unknown_not_algorithm_edits(known_runtime, change):
    audit, _, page, backend, selected = observed_page()
    audit.leave()
    other = NS(input=NS(_backend=audit.backend_pages[0][0]))
    fresh = source._ConversionAudit(); fresh.begin()
    fresh.backend_pages = list(audit.backend_pages)
    record = fresh.enter(other, page)
    if change == 'missing_segmented': backend._seg_page = None
    elif change == 'not_materialized': backend._seg_page.has_lines = False
    elif change == 'unregistered': fresh.backend_pages.clear()
    elif change == 'wrong_parent': other.input._backend = NS(doc_key=backend._result.doc_key)
    elif change == 'wrong_page': record['_page'] = NS(page_no=page.page_no)
    elif change == 'zero_based': backend._result.page_number = 0
    elif change == 'wrong_doc_key': other.input._backend.doc_key = 'other parser document'
    elif change == 'duplicate_all': backend._seg_page.textline_cells.append(selected[0])
    else: selected = [cell()]
    before = [c.text for c in page.cells]
    source._observe_native(fresh, record, page, selected, 'visible_cells')
    assert record['observation']['status'] == 'unknown' and record['observation']['reason_codes']
    assert record['observation']['native'] is None
    assert [c.text for c in page.cells] == before
    fresh.leave()


def test_selected_fallback_and_empty_but_present_native_keep_actual_occurrences(known_runtime):
    audit, record, page, backend, _ = observed_page()
    source._observe_native(audit, record, page, list(page.cells), 'fallback_cells')
    assert record['observation']['native']['selected_all_ordinals'] == [0, 1]
    assert record['observation']['native']['selection_basis'] == 'fallback_cells'
    page.parsed_page.textline_cells = []
    source._observe_native(audit, record, page, [], 'visible_cells')
    assert record['observation']['native']['all_textline_cells'] == []
    assert record['observation']['reason_codes'] == []
    audit.leave()


def test_new_final_object_is_not_falsely_mapped_by_repeated_text(known_runtime):
    audit, record, page, _, _ = observed_page()
    source._observe_prepost(record, page, [], before=True)
    page.parsed_page.textline_cells = [cell(0)]
    source._observe_prepost(record, page, [], before=False)
    post = record['observation']['prepost']
    assert post['primary_cells'][0]['fields']['text'] == post['final_cells'][0]['fields']['text']
    assert post['primary_cells'][0]['local_ref'] != post['final_cells'][0]['local_ref']
    audit.leave()


@pytest.mark.parametrize('bad', ['unknown', 'nan', 'count', 'bool', 'empty'])
def test_reader_observes_only_actual_consumed_fields_and_preserves_delegate_return(bad):
    result = RapidOCROutput(boxes=np.array([[[0., 0.], [2., 0.], [2., 2.], [0., 2.]]]),
        txts=('原文 é\r\n',), scores=(.987654321123,))
    if bad == 'unknown': result = NS(boxes=result.boxes, txts=result.txts, scores=result.scores)
    elif bad == 'nan': result.scores = (float('nan'),)
    elif bad == 'count': result.txts = ('one', 'two')
    elif bad == 'bool': result.scores = (True,)
    else: result.boxes = None
    audit = source._ConversionAudit(); audit.begin()
    record = audit.enter(NS(), NS(page_no=1))
    wrapped = source._AuditedReader(lambda *args, **kwargs: result, audit)
    assert wrapped('synthetic pixels', use_det=True, use_cls=True, use_rec=True) is result
    observation = record['region_calls'][0]['reader_output']
    assert observation['status'] == ('empty' if bad == 'empty' else 'unknown')
    assert observation['fields'] is None and observation['scope'] == 'consumed_fields_only'
    audit.leave()


def test_reader_full_consumed_output_excludes_img_timing_and_visualization():
    result = RapidOCROutput(boxes=np.array([[[0., 0.], [2., 0.], [2., 2.], [0., 2.]]]),
        txts=('原文 é\r\n',), scores=(.987654321123,))
    audit = source._ConversionAudit(); audit.begin()
    record = audit.enter(NS(), NS(page_no=1))
    assert source._AuditedReader(lambda *args, **kwargs: result, audit)('pixels') is result
    value = record['region_calls'][0]['reader_output']
    assert value == {'scope': 'consumed_fields_only', 'status': 'captured', 'fields': {
        'boxes': result.boxes.tolist(), 'txts': list(result.txts), 'scores': list(result.scores)}}
    assert set(value['fields']) == {'boxes', 'txts', 'scores'}
    assert record['region_calls'][0]['output_sha256'] == source._sha(source._json_bytes({
        'boxes': value['fields']['boxes'], 'texts': value['fields']['txts'],
        'scores': value['fields']['scores']}))
    audit.leave()


def test_controlled_v2_captured_reader_validates_legacy_hash_and_original_return(default_adapter, monkeypatch):
    import sys
    adapter, state, _ = default_adapter
    output = RapidOCROutput(boxes=np.array([[[0., 0.], [2., 0.], [2., 2.], [0., 2.]]]),
        txts=('原文 é\r\n',), scores=(.987654321123,))
    delegated, returned = [], []
    base = sys.modules['docling.models.stages.ocr.rapid_ocr_model'].RapidOcrModel
    original_init = base.__init__
    def reader(self, pixels, **kwargs):
        delegated.append((pixels, kwargs))
        return output
    def initialize(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        monkeypatch.setattr(type(self.reader), '__call__', reader)
    monkeypatch.setattr(base, '__init__', initialize)
    original_call = source._AuditedReader.__call__
    def observe_return(self, *args, **kwargs):
        result = original_call(self, *args, **kwargs)
        assert result is output
        returned.append(result)
        return result
    monkeypatch.setattr(source._AuditedReader, '__call__', observe_return)
    result = adapter.convert_bytes(b'%PDF-synthetic', 'pdf')
    assert delegated == [('fake pixels', {'use_det': True, 'use_cls': True, 'use_rec': True})]
    assert len(returned) == 1 and returned[0] is output
    assert adapter._converter.diagnostic == 'issued' and result.receipt is not None
    audit = source.validate_conversion_receipt(result, b'%PDF-synthetic', 'pdf')
    assert audit['protocol'] == audit['recipe']['adapter_revision'] == 'document-conversion-execution-v2'
    call = audit['pages'][0]['region_calls'][0]
    assert call['status'] == 'completed' and call['output_count'] == 1
    assert call['reader_output'] == {'scope': 'consumed_fields_only', 'status': 'captured',
        'fields': {'boxes': output.boxes.tolist(), 'txts': list(output.txts), 'scores': list(output.scores)}}
    expected = {'boxes': output.boxes.tolist(), 'texts': list(output.txts), 'scores': list(output.scores)}
    assert call['output_sha256'] == source._sha(source._json_bytes(expected))
    assert call['output_sha256'] != source._sha(source._json_bytes(call['reader_output']['fields']))
    assert audit['pages'][0]['observation']['status'] == 'unknown'
    assert result.entries == state.translated.entries and not hasattr(result, 'qualified')
    assert 'source_complete' not in audit


@pytest.mark.parametrize('change', ['unknown', 'mixed', 'missing', 'extra'])
def test_protocol_shape_rejects_unknown_mixed_missing_or_extra_without_issuing_authority(change):
    audit = {'protocol': 'document-conversion-execution-v2', 'recipe': {
        'adapter_revision': 'document-conversion-execution-v2'}, 'input': {}, 'output': {}, 'pages': []}
    if change == 'unknown': audit['protocol'] = 'document-conversion-execution-v3'
    elif change == 'mixed': audit['recipe']['adapter_revision'] = 'document-conversion-execution-v1'
    elif change == 'missing': del audit['protocol']
    else: audit['qualified'] = True
    with pytest.raises(ValueError): source._check_execution_protocol(audit)


@pytest.mark.parametrize('change', ['status', 'fields', 'ref', 'selected', 'page_no', 'counts', 'geometry'])
def test_v2_observation_schema_rejects_mutated_public_audit_without_seal_claim(closed_known, change):
    adapter, _ = closed_known
    result = adapter.convert_bytes(b'%PDF-synthetic', 'pdf')
    original = source.validate_conversion_receipt(result, b'%PDF-synthetic', 'pdf')
    changed = deepcopy(original)
    observation = changed['pages'][0]['observation']
    if change == 'status': observation['status'] = 'complete'
    elif change == 'fields': observation['native']['selected_cells'][0]['fields']['index'] = True
    elif change == 'ref': observation['native']['selected_cells'][0]['local_ref'] = 99
    elif change == 'selected': observation['native']['selected_all_ordinals'] = [1]
    elif change == 'page_no': observation['association']['backend_page_no'] = 0
    elif change == 'counts': changed['pages'][0]['post_process']['final_count'] += 1
    else: observation['association']['geometry']['rect']['r_x0'] = float('nan')
    with pytest.raises((ValueError, TypeError)): source._validate_observations(changed)
    assert source.validate_conversion_receipt(result, b'%PDF-synthetic', 'pdf') == original


# Fixed original issuer methods are inserted below from read-only git show 3317.
# No Receipt construction/seal assignment exists in the test harness itself.
_LEGACY_SOURCE_SHA256 = 'a238ca50ac5bb2a24c749448d2453024fb0d2646c4d19061ff7dd1db04d8f747'
_LEGACY_METHODS_SHA256 = '83fdf2a8efe3acb54a31196f317f7a5b1b0c957804f81b8d2fe3d4347a38913b'
_LEGACY_METHODS = "def _descriptor(self, kind):\n    root, description, identities = self.component_reader()\n    root = root.resolve(strict=True)\n    component = json.loads(description)\n    files = {r['name'] for r in component['files']}\n    fmt = self.formats[kind]\n    from docling.datamodel.base_models import InputFormat\n    actual_format = InputFormat.PDF if kind == 'pdf' else InputFormat.EPUB\n    # Read the actual objects retained by DocumentConverter, not defaults.\n    if self.converter.format_to_options.get(actual_format) is not fmt:\n        raise ValueError\n    options = fmt.model_dump(mode='json', exclude={'pipeline_cls', 'backend'})\n    distributions = {name: version(name) for name in ('docling', 'docling-core',\n        'docling-parse', 'docling-ibm-models', 'rapidocr', 'onnxruntime')}\n    if distributions['docling'] != DOCLING_VERSION:\n        raise ValueError\n    recipe = {'adapter_revision': 'document-conversion-execution-v1', 'format': kind,\n        'backend': fmt.backend.__module__ + '.' + fmt.backend.__qualname__,\n        'pipeline': fmt.pipeline_cls.__module__ + '.' + fmt.pipeline_cls.__qualname__,\n        'allowed_formats': [getattr(value, 'value', value) for value in self.converter.allowed_formats],\n        'distributions': distributions,\n        'effective_format_options': _component_config(options, root, files),\n        'component': component}\n    return root, files, _json_bytes(recipe), identities\n\ndef run(self, data, kind):\n    if not self.lock.acquire(blocking=False):\n        raise DoclingSourceError('docling_conversion_failed')\n    try:\n        if self.unusable:\n            raise DoclingSourceError('docling_conversion_failed')\n        self.diagnostic = 'unverified'\n        try:\n            before = self._descriptor(kind)\n            if self.baseline[kind] is None or before != self.baseline[kind]:\n                raise ValueError\n        except Exception:\n            before = None  # Development caches remain diagnostic conversions.\n            self.diagnostic = 'component_or_config_unverified'\n        self.audit.component = None if before is None else (before[0], frozenset(before[1]))\n        self.audit.begin()\n        from docling.datamodel.base_models import DocumentStream\n        converted = self.converter.convert(DocumentStream(name='source.' + kind,\n            stream=BytesIO(data)), raises_on_error=True)\n        pages = self.audit.close(converted)\n        raw_status = getattr(converted, 'status', None)\n        status = getattr(raw_status, 'value', raw_status)\n        if status != 'success' or getattr(converted, 'errors', []):\n            raise DoclingSourceError('docling_incomplete')\n        result = replace(_translate(converted.document, kind), receipt=None)\n        if before is None:\n            return result\n        try:\n            after = self._descriptor(kind)\n            if before != after:\n                raise ValueError\n            recipe = json.loads(before[2])\n            if kind == 'pdf':\n                if (not self.audit.stages or not self.audit.producers\n                        or type(result.page_count) is not int or result.page_count < 1\n                        or [r['physical_page'] for r in pages] != list(range(1, result.page_count + 1))\n                        or any(type(r['physical_page']) is not int for r in pages)):\n                    raise ValueError\n                for record in pages:\n                    if (record['stage_outcome'] not in {'completed', 'not_scheduled'}\n                            or record['scan_decision'] == 'unknown' or record['post_process'] is None\n                            or any(call['status'] != 'completed' for call in record['region_calls'])\n                            or len(record['region_calls']) != len(record['planned_regions'])):\n                        raise ValueError\n                    if not record['region_calls'] and (record['scan_decision'] != 'native'\n                            or record.get('native_cell_count', 0) < 1):\n                        raise ValueError\n                    if (record.get('configuration') is None\n                            or record['configuration'] != record.get('configuration_after')):\n                        raise ValueError\n            elif pages:\n                raise ValueError\n            audit = {'protocol': 'document-conversion-execution-v1',\n                'input': {'kind': kind, 'sha256': _sha(data), 'byte_count': len(data)},\n                'recipe': recipe, 'pages': pages, 'output': _result_fingerprints(result)}\n            receipt = object.__new__(DocumentConversionReceipt)\n            result = replace(result, receipt=receipt)\n            object.__setattr__(receipt, '_seal', _RECEIPT_SEAL)\n            object.__setattr__(receipt, '_audit', _json_bytes(audit))\n            object.__setattr__(receipt, '_result', weakref.ref(result))\n            self.diagnostic = 'issued'\n            return result\n        except Exception:\n            self.diagnostic = 'execution_unverified'\n            return result\n    except Exception as error:\n        # A timeout/exception is not proof that Docling's worker threads ended.\n        self.unusable = True\n        self.diagnostic = 'conversion_failed_unusable'\n        with self.audit.lock:\n            self.audit.open = False\n        if isinstance(error, DoclingSourceError):\n            raise\n        raise DoclingSourceError('docling_conversion_failed') from error\n    finally:\n        self.lock.release()\n"


def install_original_v1(handle, monkeypatch, *, original_descriptor=True):
    assert _LEGACY_SOURCE_SHA256 == 'a238ca50ac5bb2a24c749448d2453024fb0d2646c4d19061ff7dd1db04d8f747'
    assert hashlib.sha256(_LEGACY_METHODS.encode()).hexdigest() == _LEGACY_METHODS_SHA256
    tree = ast.parse(_LEGACY_METHODS)
    assert [n.name for n in tree.body] == ['_descriptor', 'run']
    namespace = dict(source.__dict__)  # Same actual classes/seal; no copied Receipt type.
    exec(compile(tree, '<fixed-3317-original-issuer-methods>', 'exec'), namespace)
    if original_descriptor:
        monkeypatch.setattr(type(handle), '_descriptor', namespace['_descriptor'])
    monkeypatch.setattr(type(handle), 'run', namespace['run'])
    handle.baseline = {kind: handle._descriptor(kind) for kind in ('pdf', 'epub')}


def test_fixed_original_v1_issuer_audit_is_validated_without_upgrade(default_adapter, monkeypatch):
    adapter, _, _ = default_adapter
    adapter.convert_bytes(b'%PDF-synthetic', 'pdf')
    install_original_v1(adapter._converter, monkeypatch)
    result = adapter.convert_bytes(b'%PDF-synthetic', 'pdf')
    before = result.receipt.audit_json
    audit = source.validate_conversion_receipt(result, b'%PDF-synthetic', 'pdf')
    assert audit['protocol'] == audit['recipe']['adapter_revision'] == 'document-conversion-execution-v1'
    assert result.receipt.audit_json == before and audit == json.loads(before)
    with pytest.raises(source.DoclingSourceError, match='^docling_receipt_invalid$'):
        source.validate_conversion_receipt(replace(result, receipt=json.loads(before)), b'%PDF-synthetic', 'pdf')
    with pytest.raises(source.DoclingSourceError, match='^docling_receipt_invalid$'):
        source.validate_conversion_receipt(replace(result), b'%PDF-synthetic', 'pdf')


def test_genuine_old_issuer_with_mixed_new_descriptor_is_rejected(default_adapter, monkeypatch):
    adapter, _, _ = default_adapter
    adapter.convert_bytes(b'%PDF-synthetic', 'pdf')
    install_original_v1(adapter._converter, monkeypatch, original_descriptor=False)
    result = adapter.convert_bytes(b'%PDF-synthetic', 'pdf')
    assert result.receipt is not None  # Original v1 issuer really signed, not a JSON stand-in.
    public = json.loads(result.receipt.audit_json)
    assert public['protocol'] == 'document-conversion-execution-v1'
    assert public['recipe']['adapter_revision'] == 'document-conversion-execution-v2'
    with pytest.raises(source.DoclingSourceError, match='^docling_receipt_invalid$'):
        source.validate_conversion_receipt(result, b'%PDF-synthetic', 'pdf')


def test_v2_epub_observations_are_not_fabricated_pdf_pages(default_adapter):
    adapter, _, _ = default_adapter
    result = adapter.convert_bytes(b'PKsynthetic', 'epub')
    audit = source.validate_conversion_receipt(result, b'PKsynthetic', 'epub')
    assert audit['protocol'] == audit['recipe']['adapter_revision'] == 'document-conversion-execution-v2'
    assert audit['pages'] == [] and audit['recipe']['format'] == 'epub'
