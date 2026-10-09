"""Closed adapter semantics with narrow fake library dependencies, not model QA.

The default construction path is exercised against synthetic inventory bytes.
No fake construction here is evidence of real Docling/model completeness.
"""
from dataclasses import FrozenInstanceError, replace
from copy import copy
import hashlib
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace as NS
import sys
import threading

import pytest

from knowledge_distiller.v1 import docling_source as source
from knowledge_distiller.v1 import docling_component as component


class Cell:
    def __init__(self, text):
        self.text = text

    def model_dump(self, **kwargs):
        return {'text': self.text, 'from_ocr': False}


class Rect:
    def __init__(self, **values):
        self.values = values

    def model_dump(self, **kwargs):
        return self.values

    def area(self):
        return (self.values['r'] - self.values['l']) * abs(self.values['b'] - self.values['t'])


class Boxes(list):
    def tolist(self):
        return list(self)


def stopped_stage():
    thread = threading.Thread(target=lambda: None)
    thread.start()
    thread.join()
    return NS(_thread=thread)


@pytest.fixture
def default_adapter(tmp_path, monkeypatch):
    files = {f'RapidOcr/{task}.onnx': b'synthetic ' + task.encode() for task in ('det', 'cls', 'rec')}
    manifest = {'docling_version': source.DOCLING_VERSION, 'files': {
        name: {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()} for name, data in files.items()}}
    monkeypatch.setattr(component, 'trusted_manifest', lambda: manifest)
    inventory = component.DoclingComponent(tmp_path)
    for name, data in files.items():
        path = inventory.active / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    state = NS(mode='scan', empty=False, fail=False, skip_call=False, skip_post=False,
               drift=False, unknown_native=False, bad_config=False, stages=None, active=None,
               post_fail=False, invalid_backend=False, zero_region=False,
               translated=source.DoclingSourceResult((source.DocumentEntry('text', '原文 é\r\n原文',
                   (source.DocumentProvenance(page=1, charspan=(0, len('原文 é\r\n原文'))),), '#text-1',
                   table_data={'cells': ['unchanged']}),), 1,
                   {'nested': {'value': '原声明'}}, page_images=(source.DocumentPageImage(1, 2, 2, b'PNG-one'),)))

    def module(name, **attributes):
        value = ModuleType(name)
        value.__dict__.update(attributes)
        monkeypatch.setitem(sys.modules, name, value)

    for name in ('docling', 'docling.datamodel', 'docling.pipeline', 'docling.models',
                 'docling.models.stages', 'docling.models.stages.ocr', 'docling.backend',
                 'docling_core', 'docling_core.types'):
        module(name, __path__=[])

    class Options:
        def __init__(self, **values):
            self.__dict__.update(values)

        def model_dump(self, **kwargs):
            def freeze(value):
                if isinstance(value, Path):
                    return str(value)
                if isinstance(value, Options):
                    return value.model_dump()
                if isinstance(value, dict):
                    return {k: freeze(v) for k, v in value.items()}
                return value
            excluded = kwargs.get('exclude', set())
            return {k: freeze(v) for k, v in self.__dict__.items() if k not in excluded}

    class PdfOptions(Options):
        def __init__(self, **values):
            values.setdefault('accelerator_options', Options(
                num_threads=4, device='auto', cuda_use_flash_attention2=False))
            super().__init__(**values)

    class RapidOptions(Options):
        def __init__(self, **values):
            super().__init__(scale=3.0, use_det=True, use_cls=True, use_rec=True, **values)

    class Reader:
        def __init__(self):
            self.cfg = {'Global': {'model_root_dir': str(inventory.active / 'RapidOcr')}}
            for task, attr in (('Det', 'text_det'), ('Cls', 'text_cls'), ('Rec', 'text_rec')):
                path = str(inventory.active / 'RapidOcr' / (task.lower() + '.onnx'))
                self.cfg[task] = {'model_path': path}
                setattr(self, attr, NS(session=NS(session=NS(_model_path=path,
                    get_providers=lambda: ['CPUExecutionProvider']))))
            if state.bad_config:
                self.cfg['Det']['model_path'] = str(tmp_path / 'not-in-inventory.onnx')

        def __call__(self, pixels, **kwargs):
            if state.fail:
                raise RuntimeError('synthetic private message')
            if state.empty:
                return NS(boxes=None)
            return NS(boxes=Boxes([[[0, 0], [2, 0], [2, 2], [0, 2]]]),
                      txts=['原文'], scores=[0.9])

    class Backend:
        def is_valid(self):
            return not state.invalid_backend

        def get_visible_text_cells(self):
            return [] if state.mode == 'scan' else [Cell('native 原文')]

        def get_text_cells(self):
            return [Cell('fallback 原文')]

    class PdfBackend:
        def __init__(self, pages):
            self.pages = pages

        def iter_pages(self):
            yield from self.pages

    class BaseOcr:
        def __init__(self, options, enabled, **kwargs):
            self.options, self.enabled, self.scale = options, enabled, options.scale
            self.reader = Reader()

        def get_ocr_rects(self, page):
            return []  # Native text is delegated without scheduling OCR.

        def post_process_cells(self, cells, page, conv_res, priority=None):
            if state.post_fail:
                raise RuntimeError('synthetic post-process failure')
            page.cells = list(cells) + page.cells

        def __call__(self, conv_res, page_batch):
            for page in page_batch:
                if not self.enabled or not page._backend.is_valid():
                    yield page
                    continue
                rectangles = self.get_ocr_rects(page)
                cells = []
                for rectangle in rectangles:
                    if rectangle.area() == 0 or state.skip_call:
                        continue
                    result = self.reader('fake pixels', use_det=True, use_cls=True, use_rec=True)
                    if result.boxes is not None:
                        cells.append(Cell(result.txts[0]))
                if not state.skip_post:
                    self.post_process_cells(cells, page, conv_res)
                yield page

    class Pipeline:
        def __init__(self, options):
            self.pipeline_options = options

        def _create_run_ctx(self):
            return NS(stages=state.stages if state.stages is not None else [stopped_stage()])

    class PdfFormat(Options):
        def __init__(self, backend=PdfBackend, pipeline_cls=Pipeline, **values):
            super().__init__(backend=backend, pipeline_cls=pipeline_cls, **values)

    class EpubFormat(Options):
        def __init__(self, **values):
            super().__init__(backend=PdfBackend, pipeline_cls=Pipeline, **values)

    class Converter:
        def __init__(self, format_options, **kwargs):
            self.format_to_options = format_options
            self.allowed_formats = kwargs['allowed_formats']

        def convert(self, stream, raises_on_error):
            assert raises_on_error and stream.stream.getvalue() in (b'%PDF-synthetic', b'PKsynthetic')
            result = NS(status='success', errors=[], document=state.translated)
            if stream.name.endswith('pdf'):
                fmt = self.format_to_options['pdf']
                pipeline = fmt.pipeline_cls(fmt.pipeline_options)
                pipeline._create_run_ctx()
                page = NS(page_no=1, _backend=Backend(), size=NS(width=0 if state.zero_region else 2, height=2),
                          parsed_page=None if state.unknown_native else NS(),
                          cells=[] if state.mode == 'scan' else [Cell('native 原文')])
                pages = list(fmt.backend([page]).iter_pages())
                ocr = pipeline._make_ocr_model(inventory.active)
                state.active = ocr
                assert list(ocr(result, pages)) == [page]
                if state.drift:
                    fmt.pipeline_options.images_scale = 7
            return result

    module('docling.datamodel.base_models', InputFormat=NS(PDF='pdf', EPUB='epub'),
           DocumentStream=lambda **kwargs: NS(**kwargs))
    module('docling.document_converter', DocumentConverter=Converter, PdfFormatOption=PdfFormat, EpubFormatOption=EpubFormat)
    module('docling.datamodel.pipeline_options', PdfPipelineOptions=PdfOptions, RapidOcrOptions=RapidOptions)
    module('docling.datamodel.backend_options', EpubBackendOptions=Options)
    module('docling.datamodel.settings', settings=NS(cache_dir=tmp_path / 'unused-cache'))
    module('docling.backend.docling_parse_backend', ThreadedDoclingParseDocumentBackend=PdfBackend)
    module('docling.pipeline.standard_pdf_pipeline', StandardPdfPipeline=Pipeline)
    module('docling.models.stages.ocr.rapid_ocr_model', RapidOcrModel=BaseOcr)
    module('docling_core.types.doc', BoundingBox=Rect, CoordOrigin=NS(TOPLEFT='TOPLEFT'))
    module('rapidocr')
    module('onnxruntime')
    module('omegaconf', OmegaConf=NS(to_container=lambda cfg, **kwargs: cfg))
    monkeypatch.setattr(source, 'version', lambda name: source.DOCLING_VERSION if name == 'docling' else 'synthetic-locked')
    monkeypatch.setattr(source, '_translate', lambda document, kind: document)
    monkeypatch.setattr(source, '_normalize_tableformer_config_paths', lambda: None)
    return source.DoclingSourceConverter(components_root=tmp_path), state, inventory


def test_default_closed_path_binds_input_output_and_observed_execution(default_adapter):
    adapter, state, _ = default_adapter
    result = adapter.convert_bytes(b'%PDF-synthetic', 'pdf')
    assert result.entries == state.translated.entries and result.metadata == state.translated.metadata
    audit = source.validate_conversion_receipt(result, b'%PDF-synthetic', 'pdf')
    assert audit['input'] == {'kind': 'pdf', 'byte_count': 14,
                             'sha256': hashlib.sha256(b'%PDF-synthetic').hexdigest()}
    page, = audit['pages']
    assert page['scan_decision'] == 'scan' and page['stage_outcome'] == 'completed'
    assert len(page['planned_regions']) == len(page['region_calls']) == 1
    assert page['region_calls'][0]['status'] == 'completed'
    assert page['post_process']['ocr_count'] == page['post_process']['final_count'] == 1
    assert adapter.receipt_diagnostic == 'issued'


def test_native_page_no_regions_is_distinct_from_missing_call(default_adapter):
    adapter, state, _ = default_adapter
    state.mode = 'native'
    result = adapter.convert_bytes(b'%PDF-synthetic', 'pdf')
    page, = source.validate_conversion_receipt(result, b'%PDF-synthetic', 'pdf')['pages']
    assert page['scan_decision'] == 'native' and page['stage_outcome'] == 'not_scheduled'
    assert page['planned_regions'] == page['region_calls'] == []
    assert page['native_cell_count'] == 1


def test_epub_recipe_does_not_claim_pdf_ocr(default_adapter):
    adapter, _, _ = default_adapter
    result = adapter.convert_bytes(b'PKsynthetic', 'epub')
    audit = source.validate_conversion_receipt(result, b'PKsynthetic', 'epub')
    assert audit['recipe']['format'] == 'epub' and audit['pages'] == []
    assert audit['recipe']['effective_format_options'] == {'backend_options': {
        'fetch_images': True, 'enable_local_fetch': True, 'enable_remote_fetch': False}}


@pytest.mark.parametrize('flag', ['empty', 'skip_call', 'skip_post', 'unknown_native', 'bad_config',
                                  'drift', 'invalid_backend', 'zero_region'])
def test_unknown_incomplete_or_changed_execution_never_signs(default_adapter, flag):
    adapter, state, _ = default_adapter
    setattr(state, flag, True)
    result = adapter.convert_bytes(b'%PDF-synthetic', 'pdf')
    assert result.receipt is None and result.entries == state.translated.entries
    assert adapter.receipt_diagnostic == 'execution_unverified'


def test_public_dict_self_signed_json_and_result_copies_cannot_restore_authority(default_adapter):
    adapter, _, _ = default_adapter
    result = adapter.convert_bytes(b'%PDF-synthetic', 'pdf')
    receipt = result.receipt
    assert receipt is not None
    for replacement in (json.loads(receipt.audit_json), receipt.audit_json, None, receipt):
        with pytest.raises(source.DoclingSourceError, match='^docling_receipt_invalid$'):
            source.validate_conversion_receipt(replace(result, receipt=replacement), b'%PDF-synthetic', 'pdf')
    with pytest.raises(TypeError, match='opaque_document_receipt'):
        source.DocumentConversionReceipt()
    with pytest.raises(FrozenInstanceError):
        receipt._audit = b'{}'
    with pytest.raises(source.DoclingSourceError):
        source.validate_conversion_receipt(result, b'%PDF-changed', 'pdf')
    with pytest.raises(source.DoclingSourceError):
        source.validate_conversion_receipt(result, b'%PDF-synthetic', 'epub')


@pytest.mark.parametrize('part', ['metadata', 'table'])
def test_deep_result_change_invalidates_issued_receipt(default_adapter, part):
    adapter, _, _ = default_adapter
    result = adapter.convert_bytes(b'%PDF-synthetic', 'pdf')
    assert result.receipt is not None
    if part == 'metadata':
        result.metadata['nested']['value'] = 'changed'
    else:
        result.entries[0].table_data['cells'].append('changed')
    with pytest.raises(source.DoclingSourceError, match='^docling_receipt_invalid$'):
        source.validate_conversion_receipt(result, b'%PDF-synthetic', 'pdf')


def test_injected_factory_diagnostic_result_has_no_receipt(default_adapter):
    _, state, _ = default_adapter
    adapter = source.DoclingSourceConverter(converter_factory=lambda: NS(convert=lambda *a, **k:
        NS(status='success', errors=[], document=state.translated)))
    assert adapter.convert_bytes(b'%PDF-synthetic', 'pdf').receipt is None
    assert adapter.receipt_diagnostic == 'injected'


def test_exception_poisoned_handle_cannot_reenter_even_after_fake_worker_recovers(default_adapter):
    adapter, state, _ = default_adapter
    state.fail = True
    with pytest.raises(source.DoclingSourceError, match='^docling_conversion_failed$'):
        adapter.convert_bytes(b'%PDF-synthetic', 'pdf')
    assert adapter._converter.unusable
    state.fail = False
    with pytest.raises(source.DoclingSourceError, match='^docling_conversion_failed$'):
        adapter.convert_bytes(b'%PDF-synthetic', 'pdf')


def test_post_process_failure_cannot_sign_a_reader_success(default_adapter):
    adapter, state, _ = default_adapter
    state.post_fail = True
    with pytest.raises(source.DoclingSourceError, match='^docling_conversion_failed$'):
        adapter.convert_bytes(b'%PDF-synthetic', 'pdf')
    assert adapter._converter.unusable
    assert adapter._converter.audit.records[0]['region_calls'][0]['status'] == 'completed'
    assert adapter._converter.audit.records[0]['post_process'] is None
    state.post_fail = False
    with pytest.raises(source.DoclingSourceError, match='^docling_conversion_failed$'):
        adapter.convert_bytes(b'%PDF-synthetic', 'pdf')


def test_nonblocking_handle_lock_rejects_concurrent_call(default_adapter):
    adapter, _, _ = default_adapter
    adapter.convert_bytes(b'%PDF-synthetic', 'pdf')
    handle = adapter._converter
    handle.lock.acquire()
    try:
        with pytest.raises(source.DoclingSourceError, match='^docling_conversion_failed$'):
            adapter.convert_bytes(b'%PDF-synthetic', 'pdf')
        assert not handle.unusable
    finally:
        handle.lock.release()


def test_live_stage_thread_blocks_receipt_and_poisoned_handle(default_adapter):
    adapter, state, _ = default_adapter
    release = threading.Event()
    thread = threading.Thread(target=release.wait)
    thread.start()
    state.stages = [NS(_thread=thread)]
    try:
        with pytest.raises(source.DoclingSourceError, match='^docling_conversion_failed$'):
            adapter.convert_bytes(b'%PDF-synthetic', 'pdf')
        assert adapter._converter.unusable
    finally:
        release.set()
        thread.join()


def test_default_inventory_readback_and_injected_inventory_rejection(default_adapter):
    _, _, inventory = default_adapter
    assert inventory.verify() == inventory.active
    root, description, identities = inventory._trusted_description()
    assert root == inventory.active and identities
    assert json.loads(description)['inventory_identity'] == inventory.identity
    injected = component.DoclingComponent(inventory.root.parent, manifest=inventory.manifest)
    assert injected.verify() == root
    with pytest.raises(component.DoclingComponentError, match='docling_component_untrusted_inventory'):
        injected._trusted_description()


def test_real_synthetic_asset_drift_cannot_be_hidden_by_cached_converter(default_adapter, monkeypatch):
    from unittest.mock import Mock

    adapter, _, inventory = default_adapter
    first = adapter.convert_bytes(b'%PDF-synthetic', 'pdf')
    assert first.receipt is not None
    first_receipt = first.receipt
    first_audit = source.validate_conversion_receipt(first, b'%PDF-synthetic', 'pdf')
    handle = adapter._converter
    convert_spy = Mock(wraps=handle.converter.convert)
    run_spy = Mock(wraps=handle.run)
    monkeypatch.setattr(handle.converter, 'convert', convert_spy)
    monkeypatch.setattr(handle, 'run', run_spy)
    path = inventory.active / 'RapidOcr/det.onnx'
    path.write_bytes(b'changed synthetic model')
    with pytest.raises(source.DoclingSourceError, match='^docling_component_corrupt$') as caught:
        adapter.convert_bytes(b'%PDF-synthetic', 'pdf')
    assert caught.value.code == 'docling_component_corrupt'
    assert adapter.readiness['state'] == 'unavailable'
    convert_spy.assert_not_called()
    run_spy.assert_not_called()  # No conversion run reached the receipt issuer.
    assert first.receipt is first_receipt
    assert source.validate_conversion_receipt(first, b'%PDF-synthetic', 'pdf') == first_audit


def test_development_environment_cache_is_diagnostic_even_with_matching_inventory(default_adapter, monkeypatch):
    _, _, inventory = default_adapter
    monkeypatch.setattr(source, '_bundled_artifacts', lambda: inventory.active)
    adapter = source.DoclingSourceConverter()
    result = adapter.convert_bytes(b'%PDF-synthetic', 'pdf')
    assert result.receipt is None and adapter.receipt_diagnostic == 'component_or_config_unverified'


def test_format_mapping_drift_rejects_cached_handle_qualification(default_adapter):
    adapter, _, _ = default_adapter
    assert adapter.convert_bytes(b'%PDF-synthetic', 'pdf').receipt is not None
    handle = adapter._converter
    handle.converter.format_to_options['pdf'] = copy(handle.formats['pdf'])
    result = adapter.convert_bytes(b'%PDF-synthetic', 'pdf')
    assert result.receipt is None


def test_successive_runs_do_not_reuse_page_records_or_old_receipts(default_adapter):
    adapter, _, _ = default_adapter
    first = adapter.convert_bytes(b'%PDF-synthetic', 'pdf')
    second = adapter.convert_bytes(b'%PDF-synthetic', 'pdf')
    assert first.receipt is not second.receipt
    for result in (first, second):
        pages = source.validate_conversion_receipt(result, b'%PDF-synthetic', 'pdf')['pages']
        assert len(pages) == 1 and pages[0]['region_calls'][0]['ordinal'] == 0


def test_thread_local_page_association_and_strong_conversion_identity():
    audit = source._ConversionAudit()
    conv = NS()
    pages = [NS(page_no=1), NS(page_no=2)]
    barrier = threading.Barrier(2)
    errors = []

    def work(page):
        try:
            record = audit.enter(conv, page)
            barrier.wait(timeout=5)
            assert audit.local.record is record
            assert record['_conv'] is conv and record['_page'] is page
            record['region_calls'].append({'ordinal': 0, 'page': page.page_no})
            audit.leave()
        except BaseException as error:
            errors.append(error)

    audit.begin()
    threads = [threading.Thread(target=work, args=(page,)) for page in pages]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not errors and not any(thread.is_alive() for thread in threads)
    records = audit.close(conv)
    assert [r['region_calls'][0]['page'] for r in records] == [1, 2]


def test_wrong_conversion_or_duplicate_actual_page_identity_rejected():
    audit = source._ConversionAudit()
    conv, page = NS(), NS(page_no=1)
    audit.begin()
    audit.enter(conv, page)
    audit.leave()
    with pytest.raises(ValueError):
        audit.enter(conv, page)
    with pytest.raises(ValueError):
        audit.enter(NS(), NS(page_no=2))
    assert audit.close(conv)[0]['physical_page'] == 1


def test_producer_thread_still_alive_is_not_mistaken_for_joined():
    audit = source._ConversionAudit()
    audit.begin()
    release = threading.Event()
    thread = threading.Thread(target=release.wait)
    thread.start()
    audit.producers.append(thread)
    try:
        with pytest.raises(ValueError):
            audit.close(NS())
    finally:
        release.set()
        thread.join()
