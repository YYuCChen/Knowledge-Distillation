"""Local structured PDF/EPUB conversion; no VLM descriptions or invented provenance.

Callers retain exact input bytes and EPUB spine/link completeness qualification.
Docling does not retain EPUB chapter anchors; chapter=None explicitly reflects it.
Frozen releases use the explicitly selected, verified external model component.
Source development retains upstream caches unless an explicit artifacts path is supplied.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
import hashlib
import json
from importlib.metadata import version, PackageNotFoundError
from io import BytesIO
from itertools import chain
import math
from pathlib import Path
import sys
import threading
import weakref
from numbers import Real
from typing import Callable


DOCLING_VERSION = "2.126.0"
_RECEIPT_SEAL = object()


class DoclingSourceError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class DocumentProvenance:
    page: int | None = None
    bbox: tuple[float, float, float, float] | None = None
    coord_origin: str | None = None
    chapter: str | None = None
    charspan: tuple[int, int] | None = None
    original_charspan: tuple[int, int] | None = None


@dataclass(frozen=True)
class DocumentEntry:
    kind: str
    text: str
    provenance: tuple[DocumentProvenance, ...]
    ref: str
    image_bytes: bytes | None = None
    mime: str | None = None
    table_data: dict | None = None
    label: str = ""
    content_layer: str = "body"
    original_text: str | None = None


@dataclass(frozen=True)
class DocumentPageImage:
    page: int
    width: int
    height: int
    image_bytes: bytes
    mime: str = "image/png"


@dataclass(frozen=True)
class DoclingSourceResult:
    entries: tuple[DocumentEntry, ...]
    page_count: int | None
    metadata: dict = field(default_factory=dict)
    runtime_version: str = DOCLING_VERSION
    page_images: tuple[DocumentPageImage, ...] = ()
    receipt: DocumentConversionReceipt | None = None


def _json_bytes(value):
    def check(node):
        if node is None or type(node) in (bool, int):
            return
        if type(node) is str:
            if '\x00' in node:
                raise ValueError
            node.encode('utf-8', errors='strict')
        elif type(node) is float:
            if not math.isfinite(node):
                raise ValueError
        elif type(node) in (list, tuple):
            for child in node:
                check(child)
        elif type(node) is dict:
            for key, child in node.items():
                if type(key) is not str:
                    raise ValueError
                check(key)
                check(child)
        else:
            raise ValueError
    check(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _result_fingerprints(result):
    def binary(data):
        if data is None:
            return None
        if type(data) is not bytes:
            raise ValueError
        return {'sha256': _sha(data), 'byte_count': len(data)}
    entries = []
    for entry in result.entries:
        row = asdict(entry)
        row['image_bytes'] = binary(entry.image_bytes)
        entries.append(row)
    images = []
    for image in result.page_images:
        row = asdict(image)
        row['image_bytes'] = binary(image.image_bytes)
        images.append(row)
    return {'ordered_entries_sha256': _sha(_json_bytes(entries)),
            'ordered_page_images_sha256': _sha(_json_bytes(images)),
            'translated_result_sha256': _sha(_json_bytes({'entries': entries,
                'page_images': images, 'page_count': result.page_count,
                'metadata': result.metadata, 'runtime_version': result.runtime_version}))}


@dataclass(frozen=True, slots=True, init=False)
class DocumentConversionReceipt:
    """Opaque in-process authority; audit JSON is never a reconstruction API.

    This does not attest against arbitrary code controlling this Python process.
    Nor does a conversion receipt qualify a complete source, identity or raw.
    """
    _seal: object
    _audit: bytes
    _result: object

    def __init__(self):
        raise TypeError('opaque_document_receipt')

    @property
    def audit_json(self):
        return self._audit.decode('utf-8')


def validate_conversion_receipt(result, data, kind):
    """Read-only validation of the original issued result, not self-signed JSON."""
    try:
        receipt = result.receipt
        if (type(result) is not DoclingSourceResult or type(data) is not bytes
                or type(receipt) is not DocumentConversionReceipt
                or receipt._seal is not _RECEIPT_SEAL or receipt._result() is not result):
            raise ValueError
        audit = json.loads(receipt.audit_json)
        protocol = _check_execution_protocol(audit)
        if protocol == 'document-conversion-execution-v2':
            _validate_observations(audit)
        if (audit['input'] != {'kind': kind, 'sha256': _sha(data), 'byte_count': len(data)}
                or audit['output'] != _result_fingerprints(result)):
            raise ValueError
        return audit
    except (AttributeError, TypeError, ValueError, KeyError) as error:
        raise DoclingSourceError('docling_receipt_invalid') from error


def _check_execution_protocol(audit):
    protocol = audit.get('protocol')
    if (protocol not in {'document-conversion-execution-v1', 'document-conversion-execution-v2'}
            or audit['recipe'].get('adapter_revision') != protocol
            or set(audit) != {'protocol', 'input', 'recipe', 'pages', 'output'}):
        raise ValueError
    return protocol


_OBSERVATION_REASONS = {'unsupported_backend', 'missing_segmented_page',
    'unsupported_cell_type', 'invalid_cell_fields', 'association_mismatch',
    'selection_identity_unknown', 'prepost_not_observed'}
_RECT_KEYS = {'r_x0', 'r_y0', 'r_x1', 'r_y1', 'r_x2', 'r_y2', 'r_x3', 'r_y3', 'coord_origin'}
_CELL_KEYS = {'index', 'rgba', 'rect', 'text', 'orig', 'text_direction', 'confidence', 'from_ocr'}
_PDF_CELL_KEYS = {'rendering_mode', 'widget', 'font_key', 'font_name'}
_GEOMETRY_KEYS = {'angle', 'rect', 'boundary_type', 'art_bbox', 'bleed_bbox',
                  'crop_bbox', 'media_bbox', 'trim_bbox'}


def _finite(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError
    return value


def _finite_float(value):
    if type(value) is not float:
        raise ValueError
    return _finite(value)


def _check_rect(value, *, box=False):
    keys = {'l', 't', 'r', 'b', 'coord_origin'} if box else _RECT_KEYS
    if type(value) is not dict or set(value) != keys or value['coord_origin'] not in {'TOPLEFT', 'BOTTOMLEFT'}:
        raise ValueError
    for key in keys-{'coord_origin'}:
        _finite_float(value[key])


def _check_cell(fields, name):
    pdf = name == 'docling_core.types.doc.page.PdfTextCell'
    if name not in {'docling_core.types.doc.page.TextCell', 'docling_core.types.doc.page.PdfTextCell'}:
        raise ValueError
    if type(fields) is not dict or set(fields) != _CELL_KEYS | (_PDF_CELL_KEYS if pdf else set()):
        raise ValueError
    if type(fields['index']) is not int or type(fields['from_ocr']) is not bool:
        raise ValueError
    if (any(type(fields[k]) is not str for k in ('text', 'orig'))
            or fields['text_direction'] not in {'left_to_right', 'right_to_left', 'unspecified'}
            or not 0 <= _finite_float(fields['confidence']) <= 1):
        raise ValueError
    _check_rect(fields['rect'])
    rgba = fields['rgba']
    if (type(rgba) is not dict or set(rgba) != {'r', 'g', 'b', 'a'}
            or any(type(v) is not int or not 0 <= v <= 255 for v in rgba.values())):
        raise ValueError
    if pdf and (type(fields['rendering_mode']) is not int or fields['rendering_mode'] not in range(-1, 8)
            or type(fields['widget']) is not bool or fields['from_ocr']
            or any(type(fields[k]) is not str for k in ('font_key', 'font_name'))):
        raise ValueError
    _json_bytes(fields)


def _raw_rect(value, cls, *, box=False):
    from docling_core.types.doc.base import CoordOrigin
    if type(value) is not cls or type(value.coord_origin) is not CoordOrigin:
        raise ValueError
    keys = {'l', 't', 'r', 'b'} if box else _RECT_KEYS-{'coord_origin'}
    if set(value.__dict__) != keys | {'coord_origin'}:
        raise ValueError
    result = {k: _finite(getattr(value, k)) for k in keys}
    result['coord_origin'] = value.coord_origin.value
    _check_rect(result, box=box)
    return result


def _cell_rows(record, cells):
    from docling_core.types.doc.page import (TextCell, PdfTextCell, BoundingRectangle,
        ColorRGBA, TextDirection, PdfCellRenderingMode)
    rows = []
    for ordinal, cell in enumerate(cells):
        if type(cell) not in (TextCell, PdfTextCell):
            raise TypeError('unsupported_cell_type')
        if set(cell.__dict__) != _CELL_KEYS | (_PDF_CELL_KEYS if type(cell) is PdfTextCell else set()):
            raise ValueError
        if type(cell.rgba) is not ColorRGBA or type(cell.text_direction) is not TextDirection:
            raise ValueError
        if set(cell.rgba.__dict__) != {'r', 'g', 'b', 'a'}:
            raise ValueError
        fields = {k: getattr(cell, k) for k in ('index', 'text', 'orig', 'confidence', 'from_ocr')}
        fields.update(rgba={k: getattr(cell.rgba, k) for k in ('r', 'g', 'b', 'a')},
            rect=_raw_rect(cell.rect, BoundingRectangle), text_direction=cell.text_direction.value)
        if type(cell) is PdfTextCell:
            if type(cell.rendering_mode) is not PdfCellRenderingMode:
                raise ValueError
            fields.update(rendering_mode=cell.rendering_mode.value, widget=cell.widget,
                          font_key=cell.font_key, font_name=cell.font_name)
        name = 'docling_core.types.doc.page.'+type(cell).__name__
        _check_cell(fields, name)
        refs = record['_cell_refs']
        found = next((i for i, existing in enumerate(refs) if existing is cell), None)
        if found is None:
            found = len(refs); refs.append(cell)
        rows.append({'ordinal': ordinal, 'local_ref': found, 'type': name, 'fields': fields})
    # Freeze before upstream can mutate/re-index the held instances.
    return json.loads(_json_bytes(rows))


def _observation_unknown(record, reason):
    observation = record['observation']
    observation['status'] = 'unknown'
    if reason not in observation['reason_codes']:
        observation['reason_codes'].append(reason)


def _observe_native(audit, record, page, selected, basis):
    reason = 'unsupported_backend'
    try:
        from docling.backend.docling_parse_backend import ThreadedDoclingParsePageBackend
        from docling_parse.pdf_parser import PageParseResult
        from docling.datamodel.base_models import Page
        from docling_core.types.doc.page import SegmentedPdfPage, PdfPageGeometry, BoundingRectangle
        from docling_core.types.doc.base import BoundingBox
        backend = page._backend
        if type(page) is not Page or type(backend) is not ThreadedDoclingParsePageBackend:
            raise ValueError
        reason = 'association_mismatch'
        parse = backend._result
        document_backend = record['_conv'].input._backend
        with audit.lock:
            registered = any(parent is document_backend and child is backend
                             for parent, child in audit.backend_pages)
        if (not registered or record['_page'] is not page or type(parse) is not PageParseResult
                or type(page.page_no) is not int or page.page_no < 1
                or type(backend.page_no) is not int or type(parse.page_number) is not int
                or page.page_no != backend.page_no or page.page_no != parse.page_number
                or type(parse.doc_key) is not str or not parse.doc_key or parse.doc_key != document_backend.doc_key):
            raise ValueError
        reason = 'missing_segmented_page'
        segmented = backend._seg_page  # Already read by the real selected-cells getter.
        if (type(segmented) is not SegmentedPdfPage or type(segmented.dimension) is not PdfPageGeometry
                or segmented.has_lines is not True):
            raise ValueError
        reason = 'invalid_cell_fields'
        dimension = segmented.dimension
        from docling_core.types.doc.page import PdfPageBoundaryType
        if type(dimension.boundary_type) is not PdfPageBoundaryType or set(dimension.__dict__) != _GEOMETRY_KEYS:
            raise ValueError
        geometry = {'angle': _finite_float(dimension.angle), 'rect': _raw_rect(dimension.rect, BoundingRectangle),
                    'boundary_type': dimension.boundary_type.value}
        for key in _GEOMETRY_KEYS-{'angle', 'rect', 'boundary_type'}:
            geometry[key] = _raw_rect(getattr(dimension, key), BoundingBox, box=True)
        association = {'page_no': page.page_no, 'backend_page_no': backend.page_no,
            'parser_page_number': parse.page_number, 'numbering': 'docling-threaded-physical-one-based',
            'parser_document_key': parse.doc_key, 'page_size': {
                'width': _finite_float(page.size.width), 'height': _finite_float(page.size.height)}, 'geometry': geometry}
        if any(v <= 0 for v in association['page_size'].values()):
            raise ValueError
        record['observation']['association'] = json.loads(_json_bytes(association))
        all_cells = list(segmented.textline_cells)
        reason = 'selection_identity_unknown'
        positions = []
        for cell in selected:
            matches = [i for i, candidate in enumerate(all_cells) if candidate is cell]
            if len(matches) != 1:
                raise ValueError
            positions.append(matches[0])
        reason = 'invalid_cell_fields'
        native = {'selection_basis': basis, 'all_textline_cells': _cell_rows(record, all_cells),
                  'selected_cells': _cell_rows(record, selected), 'selected_all_ordinals': positions}
        record['observation']['native'] = native
    except Exception as error:
        _observation_unknown(record, 'unsupported_cell_type' if isinstance(error, TypeError)
                             and str(error) == 'unsupported_cell_type' else reason)


def _observe_prepost(record, page, cells, *, before):
    try:
        if record['_page'] is not page:
            raise ValueError
        if before:
            record['observation']['prepost'] = {'primary_cells': _cell_rows(record, list(page.cells)),
                'ocr_cells': _cell_rows(record, list(cells)), 'final_cells': None}
        else:
            value = record['observation']['prepost']
            if value is None:
                raise ValueError
            value['final_cells'] = _cell_rows(record, list(page.cells))
    except Exception as error:
        record['observation']['prepost'] = None
        _observation_unknown(record, 'unsupported_cell_type' if isinstance(error, TypeError)
                             and str(error) == 'unsupported_cell_type' else 'invalid_cell_fields')


def _reader_observation(result):
    value = {'scope': 'consumed_fields_only', 'status': 'unknown', 'fields': None}
    try:
        from rapidocr.utils.output import RapidOCROutput
        if type(result) is not RapidOCROutput:
            return value
        if result.boxes is None or len(result.boxes) == 0:
            value['status'] = 'empty'
            return value
        fields = {'boxes': result.boxes.tolist(), 'txts': list(result.txts),
                  'scores': result.scores.tolist() if hasattr(result.scores, 'tolist') else list(result.scores)}
        _check_reader_fields(fields)
        value.update(status='captured', fields=json.loads(_json_bytes(fields)))
    except Exception:
        pass
    return value


def _check_reader_fields(fields):
    if (type(fields) is not dict or set(fields) != {'boxes', 'txts', 'scores'}
            or any(type(v) is not list for v in fields.values())
            or not len(fields['boxes']) == len(fields['txts']) == len(fields['scores'])
            or not fields['boxes']):
        raise ValueError
    for box, text, score in zip(fields['boxes'], fields['txts'], fields['scores']):
        if (type(box) is not list or len(box) != 4 or type(text) is not str
                or not 0 <= _finite_float(score) <= 1):
            raise ValueError
        for point in box:
            if type(point) is not list or len(point) != 2:
                raise ValueError
            for number in point:
                _finite(number)
    _json_bytes(fields)


def _validate_observations(audit):
    if audit['recipe']['format'] not in {'pdf', 'epub'} or type(audit['pages']) is not list:
        raise ValueError
    if audit['recipe']['format'] == 'epub' and audit['pages']:
        raise ValueError
    if audit['recipe']['format'] != audit['input']['kind']:
        raise ValueError
    if audit['recipe']['format'] == 'pdf' and (not audit['pages'] or any(
            type(p['physical_page']) is not int or p['physical_page'] != i
            for i, p in enumerate(audit['pages'], 1))):
        raise ValueError
    for page in audit['pages']:
        observation = page['observation']
        if (type(observation) is not dict or set(observation) != {'status', 'reason_codes', 'association', 'native', 'prepost'}
                or observation['status'] not in {'captured', 'unknown'}
                or type(observation['reason_codes']) is not list
                or any(type(r) is not str or r not in _OBSERVATION_REASONS for r in observation['reason_codes'])
                or len(set(observation['reason_codes'])) != len(observation['reason_codes'])):
            raise ValueError
        if observation['status'] == 'captured':
            if observation['reason_codes'] or any(observation[k] is None for k in ('association', 'native', 'prepost')):
                raise ValueError
        elif not observation['reason_codes']:
            raise ValueError
        association = observation['association']
        if association is not None:
            if (type(association) is not dict or set(association) != {'page_no', 'backend_page_no', 'parser_page_number',
                    'numbering', 'parser_document_key', 'page_size', 'geometry'}
                    or any(type(association[k]) is not int or association[k] != page['physical_page']
                           for k in ('page_no', 'backend_page_no', 'parser_page_number'))
                    or page['physical_page'] < 1 or association['numbering'] != 'docling-threaded-physical-one-based'
                    or type(association['parser_document_key']) is not str):
                raise ValueError
            size, geometry = association['page_size'], association['geometry']
            if type(size) is not dict or set(size) != {'width', 'height'}:
                raise ValueError
            for v in size.values():
                if _finite_float(v) <= 0:
                    raise ValueError
            if (type(geometry) is not dict or set(geometry) != _GEOMETRY_KEYS
                    or geometry['boundary_type'] not in {'art_box', 'bleed_box', 'crop_box', 'media_box', 'trim_box'}):
                raise ValueError
            _finite_float(geometry['angle']); _check_rect(geometry['rect'])
            for k in _GEOMETRY_KEYS-{'angle', 'rect', 'boundary_type'}:
                _check_rect(geometry[k], box=True)
        refs = {}
        def rows(value):
            if type(value) is not list:
                raise ValueError
            for i, row in enumerate(value):
                if (type(row) is not dict or set(row) != {'ordinal', 'local_ref', 'type', 'fields'}
                        or type(row['ordinal']) is not int or row['ordinal'] != i
                        or type(row['local_ref']) is not int or row['local_ref'] < 0):
                    raise ValueError
                _check_cell(row['fields'], row['type'])
                prior = refs.setdefault(row['local_ref'], row['type'])
                if prior != row['type']:
                    raise ValueError
        native = observation['native']
        if native is not None:
            if (type(native) is not dict or set(native) != {'selection_basis', 'all_textline_cells', 'selected_cells', 'selected_all_ordinals'}
                    or native['selection_basis'] not in {'visible_cells', 'fallback_cells'}):
                raise ValueError
            rows(native['all_textline_cells']); rows(native['selected_cells'])
            positions = native['selected_all_ordinals']
            if type(positions) is not list or len(positions) != len(native['selected_cells']):
                raise ValueError
            for selected, i in zip(native['selected_cells'], positions):
                if (type(i) is not int or not 0 <= i < len(native['all_textline_cells'])
                        or selected['local_ref'] != native['all_textline_cells'][i]['local_ref']
                        or sum(r['local_ref'] == selected['local_ref'] for r in native['all_textline_cells']) != 1
                        or selected['type'] != native['all_textline_cells'][i]['type']
                        or selected['fields'] != native['all_textline_cells'][i]['fields']):
                    raise ValueError
            if (page['native_cell_count'] != len(native['selected_cells'])
                    or page['native_cells_sha256'] != _sha(_json_bytes([r['fields'] for r in native['selected_cells']]))):
                raise ValueError
        prepost = observation['prepost']
        if prepost is not None:
            if type(prepost) is not dict or set(prepost) != {'primary_cells', 'ocr_cells', 'final_cells'}:
                raise ValueError
            for value in prepost.values():
                rows(value)
            for key, prefix in (('primary_cells', 'native'), ('ocr_cells', 'ocr'), ('final_cells', 'final')):
                if (page['post_process'][prefix+'_count'] != len(prepost[key])
                        or page['post_process'][prefix+'_sha256'] != _sha(_json_bytes([r['fields'] for r in prepost[key]]))):
                    raise ValueError
        for call in page['region_calls']:
            reader = call['reader_output']
            if (type(reader) is not dict or set(reader) != {'scope', 'status', 'fields'}
                    or reader['scope'] != 'consumed_fields_only' or reader['status'] not in {'captured', 'unknown', 'empty'}):
                raise ValueError
            if reader['status'] == 'captured':
                _check_reader_fields(reader['fields'])
                fields = reader['fields']
                legacy_output = {'boxes': fields['boxes'], 'texts': fields['txts'],
                                 'scores': fields['scores']}
                if (call['status'] != 'completed' or call['output_count'] != len(reader['fields']['txts'])
                        or call['output_sha256'] != _sha(_json_bytes(legacy_output))):
                    raise ValueError
            elif reader['fields'] is not None:
                raise ValueError
        _json_bytes(observation)


def _component_config(value, root, files):
    """Freeze actual effective options; absolute paths require verified assets."""
    if type(value) is dict:
        return {key: _component_config(child, root, files) for key, child in value.items()}
    if type(value) in (list, tuple):
        return [_component_config(child, root, files) for child in value]
    if type(value) is str and Path(value).is_absolute():
        path = Path(value).resolve(strict=True)
        relative = path.relative_to(root).as_posix()
        if relative != '.' and relative not in files and not any(
                name.startswith(relative + '/') for name in files):
            raise ValueError
        return {'component_path': relative}
    _json_bytes(value)
    return value


class _ConversionAudit:
    def __init__(self):
        self.lock = threading.Lock()
        self.local = threading.local()
        self.records = []
        self.active = 0
        self.open = False
        self.component = None
        self.stages = []
        self.producers = []
        self.backend_pages = []

    def begin(self):
        with self.lock:
            if self.open or self.active:
                raise ValueError
            self.records = []
            self.stages = []
            self.producers = []
            self.backend_pages = []
            self.open = True

    def enter(self, conv_res, page):
        with self.lock:
            if not self.open or getattr(self.local, 'record', None) is not None:
                raise ValueError
            if any(r['_page'] is page or (r['_conv'] is not conv_res) for r in self.records):
                raise ValueError
            record = {'_conv': conv_res, '_page': page, 'physical_page': page.page_no,
                      '_cell_refs': [],
                      'observation': {'status': 'unknown', 'reason_codes': [],
                          'association': None, 'native': None, 'prepost': None},
                      'planned_regions': [], 'region_calls': [], 'post_process': None,
                      'scan_decision': 'unknown', 'stage_outcome': 'unknown'}
            self.records.append(record)  # Strong references survive all worker calls.
            self.active += 1
            self.local.record = record
            return record

    def leave(self):
        with self.lock:
            self.active -= 1
            self.local.record = None

    def close(self, conv_res):
        with self.lock:
            self.open = False
            if (self.active or any(r['_conv'] is not conv_res for r in self.records)
                    or any(getattr(stage, '_thread', None) is None or stage._thread.is_alive()
                           for stage in self.stages)
                    or any(thread is not threading.current_thread() and thread.is_alive()
                           for thread in self.producers)):
                raise ValueError
            return [{k: v for k, v in r.items() if not k.startswith('_')}
                    for r in sorted(self.records, key=lambda r: r['physical_page'])]


class _ControlledHandle:
    def __init__(self, converter, formats, component_reader, audit):
        self.converter, self.formats = converter, formats
        self.component_reader, self.audit = component_reader, audit
        self.lock = threading.Lock()
        self.unusable = False
        self.diagnostic = 'unverified'
        self.baseline = {}
        for kind in ('pdf', 'epub'):
            try:
                self.baseline[kind] = self._descriptor(kind)
            except Exception:
                self.baseline[kind] = None

    def _descriptor(self, kind):
        root, description, identities = self.component_reader()
        root = root.resolve(strict=True)
        component = json.loads(description)
        files = {r['name'] for r in component['files']}
        fmt = self.formats[kind]
        from docling.datamodel.base_models import InputFormat
        actual_format = InputFormat.PDF if kind == 'pdf' else InputFormat.EPUB
        # Read the actual objects retained by DocumentConverter, not defaults.
        if self.converter.format_to_options.get(actual_format) is not fmt:
            raise ValueError
        options = fmt.model_dump(mode='json', exclude={'pipeline_cls', 'backend'})
        distributions = {name: version(name) for name in ('docling', 'docling-core',
            'docling-parse', 'docling-ibm-models', 'rapidocr', 'onnxruntime')}
        if distributions['docling'] != DOCLING_VERSION:
            raise ValueError
        recipe = {'adapter_revision': 'document-conversion-execution-v2', 'format': kind,
            'backend': fmt.backend.__module__ + '.' + fmt.backend.__qualname__,
            'pipeline': fmt.pipeline_cls.__module__ + '.' + fmt.pipeline_cls.__qualname__,
            'allowed_formats': [getattr(value, 'value', value) for value in self.converter.allowed_formats],
            'distributions': distributions,
            'effective_format_options': _component_config(options, root, files),
            'component': component}
        return root, files, _json_bytes(recipe), identities

    def run(self, data, kind):
        if not self.lock.acquire(blocking=False):
            raise DoclingSourceError('docling_conversion_failed')
        try:
            if self.unusable:
                raise DoclingSourceError('docling_conversion_failed')
            self.diagnostic = 'unverified'
            try:
                before = self._descriptor(kind)
                if self.baseline[kind] is None or before != self.baseline[kind]:
                    raise ValueError
            except Exception:
                before = None  # Development caches remain diagnostic conversions.
                self.diagnostic = 'component_or_config_unverified'
            self.audit.component = None if before is None else (before[0], frozenset(before[1]))
            self.audit.begin()
            from docling.datamodel.base_models import DocumentStream
            converted = self.converter.convert(DocumentStream(name='source.' + kind,
                stream=BytesIO(data)), raises_on_error=True)
            pages = self.audit.close(converted)
            raw_status = getattr(converted, 'status', None)
            status = getattr(raw_status, 'value', raw_status)
            if status != 'success' or getattr(converted, 'errors', []):
                raise DoclingSourceError('docling_incomplete')
            result = replace(_translate(converted.document, kind), receipt=None)
            if before is None:
                return result
            try:
                after = self._descriptor(kind)
                if before != after:
                    raise ValueError
                recipe = json.loads(before[2])
                if kind == 'pdf':
                    if (not self.audit.stages or not self.audit.producers
                            or type(result.page_count) is not int or result.page_count < 1
                            or [r['physical_page'] for r in pages] != list(range(1, result.page_count + 1))
                            or any(type(r['physical_page']) is not int for r in pages)):
                        raise ValueError
                    for record in pages:
                        if (record['stage_outcome'] not in {'completed', 'not_scheduled'}
                                or record['scan_decision'] == 'unknown' or record['post_process'] is None
                                or any(call['status'] != 'completed' for call in record['region_calls'])
                                or len(record['region_calls']) != len(record['planned_regions'])):
                            raise ValueError
                        if not record['region_calls'] and (record['scan_decision'] != 'native'
                                or record.get('native_cell_count', 0) < 1):
                            raise ValueError
                        if (record.get('configuration') is None
                                or record['configuration'] != record.get('configuration_after')):
                            raise ValueError
                elif pages:
                    raise ValueError
                audit = {'protocol': 'document-conversion-execution-v2',
                    'input': {'kind': kind, 'sha256': _sha(data), 'byte_count': len(data)},
                    'recipe': recipe, 'pages': pages, 'output': _result_fingerprints(result)}
                _check_execution_protocol(audit)
                _validate_observations(audit)
                receipt = object.__new__(DocumentConversionReceipt)
                result = replace(result, receipt=receipt)
                object.__setattr__(receipt, '_seal', _RECEIPT_SEAL)
                object.__setattr__(receipt, '_audit', _json_bytes(audit))
                object.__setattr__(receipt, '_result', weakref.ref(result))
                self.diagnostic = 'issued'
                return result
            except Exception:
                self.diagnostic = 'execution_unverified'
                return result
        except Exception as error:
            # A timeout/exception is not proof that Docling's worker threads ended.
            self.unusable = True
            self.diagnostic = 'conversion_failed_unusable'
            with self.audit.lock:
                self.audit.open = False
            if isinstance(error, DoclingSourceError):
                raise
            raise DoclingSourceError('docling_conversion_failed') from error
        finally:
            self.lock.release()


class DoclingSourceConverter:
    def __init__(self, *, converter_factory: Callable | None = None, components_root=None):
        self._factory = converter_factory or (lambda: _build_converter(components_root))
        self._injected = converter_factory is not None
        self._init_lock = threading.Lock()
        self._converter = None
        self._components_root = components_root
        self._readiness = {'state': 'unchecked', 'message': ''}
        self._check_lock = threading.Lock()

    @property
    def readiness(self):
        return dict(self._readiness)

    @property
    def receipt_diagnostic(self):
        if not self._injected and type(self._converter) is _ControlledHandle:
            return self._converter.diagnostic
        return 'injected' if self._injected else 'unverified'

    def check_component(self):
        if self._components_root is None:
            return
        from .docling_component import DoclingComponent, DoclingComponentError
        with self._check_lock:
            self._readiness = {'state': 'checking', 'message': '正在检查本地文档模型，完成前文档任务会等待。'}
            try:
                DoclingComponent(self._components_root).verify()
            except (DoclingComponentError, OSError) as error:
                self._readiness = {'state': 'unavailable', 'message':
                    '本地文档模型缺失或校验失败。请使用知识蒸馏器安装器，选择当前程序和数据目录重新检查并修复，然后重试文档任务。'}
                code = str(error) if isinstance(error, DoclingComponentError) else 'docling_component_unreadable'
                raise DoclingSourceError(code) from error
            self._readiness = {'state': 'ready', 'message': ''}

    def begin_component_check(self):
        # Startup stays responsive. Conversion waits for this same check lock,
        # then verifies again so a previously loaded converter cannot hide damage.
        def run():
            try:
                self.check_component()
            except DoclingSourceError:
                pass
        threading.Thread(target=run, daemon=True, name='document-component-check').start()

    def convert_bytes(self, data: bytes, kind: str) -> DoclingSourceResult:
        if kind not in {"pdf", "epub"} or not isinstance(data, bytes) or not data:
            raise DoclingSourceError("docling_invalid_input")
        if (kind == "pdf" and not data.startswith(b"%PDF-")) or (kind == "epub" and not data.startswith(b"PK")):
            raise DoclingSourceError("docling_invalid_input")
        if (not self._injected and type(self._converter) is _ControlledHandle
                and self._converter.lock.locked()):
            raise DoclingSourceError('docling_conversion_failed')
        if self._components_root is not None and (getattr(sys, 'frozen', False) or
                (Path(self._components_root) / 'docling').exists()):
            self.check_component()
        if self._converter is None:
            if not self._init_lock.acquire(blocking=False):
                raise DoclingSourceError('docling_conversion_failed')
            try:
                if self._converter is None:
                    self._converter = self._factory()
            finally:
                self._init_lock.release()
        if not self._injected and type(self._converter) is _ControlledHandle:
            return self._converter.run(data, kind)
        try:
            from docling.datamodel.base_models import DocumentStream
        except ImportError as error:
            raise DoclingSourceError("docling_runtime_unavailable") from error
        try:
            converted = self._converter.convert(
                DocumentStream(name="source." + kind, stream=BytesIO(data)), raises_on_error=True)
        except Exception as error:
            raise DoclingSourceError("docling_conversion_failed") from error
        raw_status = getattr(converted, "status", None)
        status = getattr(raw_status, "value", raw_status)
        if status != "success" or getattr(converted, "errors", []):
            raise DoclingSourceError("docling_incomplete")
        return replace(_translate(getattr(converted, "document", None), kind), receipt=None)


def _bundled_artifacts():
    if not getattr(sys, "frozen", False):
        import os
        explicit = os.environ.get('KNOWLEDGE_DISTILLER_DOCLING_MODELS')
        if explicit:
            root = Path(explicit).resolve()
            if not (root / 'manifest.json').is_file():
                raise DoclingSourceError('docling_runtime_unavailable')
            return root
        return None
    root = Path(sys._MEIPASS) / "docling-models"
    if not (root / "manifest.json").is_file():
        raise DoclingSourceError("docling_runtime_unavailable")
    return root


def _build_converter(components_root=None):
    from .docling_component import DoclingComponent, DoclingComponentError
    component = None
    diagnostic_fallback = components_root is None and not getattr(sys, 'frozen', False)
    if components_root is not None:
        component = DoclingComponent(components_root)
        try:
            artifacts = component.verify()
        except DoclingComponentError as error:
            if getattr(sys, 'frozen', False) or component.root.exists():
                raise DoclingSourceError(str(error)) from error
            artifacts = _bundled_artifacts()
            diagnostic_fallback = True
    else:
        artifacts = _bundled_artifacts()
    if artifacts is not None and component is None:
        component = DoclingComponent(Path(artifacts).parent)

    def component_reader():
        if component is None or artifacts is None or diagnostic_fallback:
            raise ValueError
        return component._trusted_description(artifacts)

    audit = _ConversionAudit()
    try:
        if version("docling") != DOCLING_VERSION:
            raise DoclingSourceError("docling_runtime_unavailable")
        from docling.document_converter import DocumentConverter, PdfFormatOption, EpubFormatOption
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions, RapidOcrOptions
        from docling.datamodel.backend_options import EpubBackendOptions
        from docling.datamodel.settings import settings
        from docling.backend.docling_parse_backend import ThreadedDoclingParseDocumentBackend
        import onnxruntime  # noqa: F401
        import rapidocr  # noqa: F401
    except DoclingSourceError:
        raise
    except (ImportError, PackageNotFoundError, OSError) as error:
        raise DoclingSourceError("docling_runtime_unavailable") from error
    _normalize_tableformer_config_paths()
    class ObservedPdfBackend(ThreadedDoclingParseDocumentBackend):
        def iter_pages(self):
            with audit.lock:
                if not audit.open:
                    raise DoclingSourceError('docling_conversion_failed')
                audit.producers.append(threading.current_thread())
            for page_backend in super().iter_pages():
                with audit.lock:
                    audit.backend_pages.append((self, page_backend))
                yield page_backend

    options = PdfPipelineOptions()
    options.artifacts_path = artifacts
    options.do_ocr = True
    # This fixed Docling release resolves Chinese to PP-OCRv6 small, not a VLM.
    options.ocr_options = RapidOcrOptions(backend="onnxruntime", lang=["ch"],
        rapidocr_params={"Global.model_root_dir": str(artifacts / "RapidOcr" if artifacts else settings.cache_dir / "rapidocr")})
    options.do_table_structure = True
    options.generate_page_images = True
    options.generate_picture_images = True
    options.images_scale = 2
    options.enable_remote_services = False
    options.do_picture_description = False
    options.do_picture_classification = False
    options.do_chart_extraction = False
    options.do_formula_enrichment = True
    options.do_code_enrichment = False
    formats = {
        InputFormat.PDF: PdfFormatOption(pipeline_options=options, pipeline_cls=_scan_aware_pipeline(audit),
                                        backend=ObservedPdfBackend),
        InputFormat.EPUB: EpubFormatOption(backend_options=EpubBackendOptions(
            fetch_images=True, enable_local_fetch=True, enable_remote_fetch=False)),
    }
    converter = DocumentConverter(allowed_formats=[InputFormat.PDF, InputFormat.EPUB], format_options=formats)
    return _ControlledHandle(converter, {'pdf': formats[InputFormat.PDF],
        'epub': formats[InputFormat.EPUB]}, component_reader, audit)


def _normalize_tableformer_config_paths():
    """Adapt Docling 2.126's string join to Windows extended-path syntax.

    Tableformer appends '/tm_config.json' to a Path string. Windows extended
    paths do not accept that mixed separator. Keep the adapter confined to
    its config reader; never replace process-wide open or resolve junctions.
    """
    if sys.platform != 'win32':
        return
    from docling_ibm_models.tableformer import common
    if getattr(common.read_config, '_kd_normalized_paths', False):
        return
    original = common.read_config

    def read_config(filename):
        return original(Path(filename))

    read_config._kd_normalized_paths = True
    common.read_config = read_config


def _cell_fingerprint(cells):
    return _sha(_json_bytes([cell.model_dump(mode='json') for cell in cells]))


def _ocr_configuration(model, audit):
    if audit.component is None:
        return None
    try:
        from omegaconf import OmegaConf
        root, files = audit.component
        reader = model.reader.delegate
        cfg = OmegaConf.to_container(reader.cfg, resolve=True, enum_to_str=True)
        engines = []
        for name, section in (('text_det', 'Det'), ('text_cls', 'Cls'), ('text_rec', 'Rec')):
            session = getattr(reader, name).session.session
            path = Path(session._model_path).resolve(strict=True)
            relative = path.relative_to(root).as_posix()
            if relative not in files or str(path) != str(Path(cfg[section]['model_path']).resolve(strict=True)):
                raise ValueError
            engines.append({'task': section, 'component_path': relative,
                            'providers': session.get_providers()})
        configuration = {'rapidocr': _component_config(cfg, root, files), 'engines': engines,
            'options': _component_config(model.options.model_dump(mode='json'), root, files),
            'scale': model.scale}
        _json_bytes(configuration)
        return configuration
    except Exception:
        return None  # Unknown effective config is not repaired from defaults.


class _AuditedReader:
    def __init__(self, delegate, audit):
        self.delegate, self.audit = delegate, audit

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    def __call__(self, *args, **kwargs):
        record = getattr(self.audit.local, 'record', None)
        if record is None:
            raise DoclingSourceError('docling_conversion_failed')
        call = {'ordinal': len(record['region_calls']), 'status': 'failed'}
        record['region_calls'].append(call)
        result = self.delegate(*args, **kwargs)
        try:
            call['options'] = dict(kwargs)
            _json_bytes(call['options'])
            if result is None or result.boxes is None or len(result.boxes) == 0:
                call['status'] = 'empty'
            else:
                boxes = result.boxes.tolist()
                texts = list(result.txts)
                scores = result.scores.tolist() if hasattr(result.scores, 'tolist') else list(result.scores)
                if not len(boxes) == len(texts) == len(scores):
                    raise ValueError
                call.update(status='completed', output_count=len(texts),
                    output_sha256=_sha(_json_bytes({'boxes': boxes, 'texts': texts, 'scores': scores})))
        except Exception:
            call['status'] = 'unknown'
        call['reader_output'] = _reader_observation(result)
        return result  # Observation never substitutes OCR output.


def _scan_aware_pipeline(audit=None):
    """Use page OCR only for actual scans, retaining native PDF cells elsewhere."""
    from docling.pipeline.standard_pdf_pipeline import StandardPdfPipeline
    from docling.models.stages.ocr.rapid_ocr_model import RapidOcrModel
    from docling_core.types.doc import BoundingBox, CoordOrigin

    class ScanAwareOcr(RapidOcrModel):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            if audit is not None and self.enabled:
                self.reader = _AuditedReader(self.reader, audit)

        def __call__(self, conv_res, page_batch):
            if audit is None:
                yield from super().__call__(conv_res, page_batch)
                return
            for page in page_batch:
                record = audit.enter(conv_res, page)
                try:
                    record['backend_valid'] = bool(page._backend is not None and page._backend.is_valid())
                    record['configuration'] = _ocr_configuration(self, audit) if self.enabled else None
                    count = 0
                    for returned in super().__call__(conv_res, (page,)):
                        if returned is not page or count:
                            raise ValueError
                        count += 1
                        record['stage_outcome'] = ('disabled' if not self.enabled else
                            'invalid_backend' if not record['backend_valid'] else
                            'completed' if record['region_calls'] else 'not_scheduled')
                        yield returned
                    if count != 1:
                        raise ValueError
                    record['configuration_after'] = _ocr_configuration(self, audit) if self.enabled else None
                    observation = record['observation']
                    if observation['prepost'] is None:
                        _observation_unknown(record, 'prepost_not_observed')
                    if not observation['reason_codes'] and all(observation[k] is not None
                            for k in ('association', 'native', 'prepost')):
                        observation['status'] = 'captured'
                except Exception:
                    record['stage_outcome'] = 'failed'
                    raise
                finally:
                    audit.leave()

        def get_ocr_rects(self, page):
            record = getattr(audit.local, 'record', None) if audit is not None else None
            if record is None:
                scan = _is_scan(page)
            elif page._backend is None or page.size is None:
                scan = False
            else:
                cells = page._backend.get_visible_text_cells()
                basis = 'visible_cells'
                if cells is None:
                    cells = page._backend.get_text_cells()
                    basis = 'fallback_cells'
                cells = list(cells)
                scan = not any(cell.text.strip() for cell in cells)
                record['scan_basis'] = basis
                record['native_cell_count'] = len(cells)
                record['native_cells_sha256'] = _cell_fingerprint(cells)
                _observe_native(audit, record, page, cells, basis)
                # An absent segmented parse returning [] cannot prove a scan.
                if record['backend_valid'] and page.parsed_page is not None:
                    record['scan_decision'] = 'scan' if scan else 'native'
            if scan:
                rects = [BoundingBox(l=0, t=0, r=page.size.width, b=page.size.height,
                                    coord_origin=CoordOrigin.TOPLEFT)]
            else:
                rects = super().get_ocr_rects(page)
            if record is not None:
                record['planned_regions'] = [rect.model_dump(mode='json') for rect in rects]
            return rects

        def post_process_cells(self, ocr_cells, page, conv_res, priority=None):
            record = getattr(audit.local, 'record', None) if audit is not None else None
            before = {'ocr_count': len(ocr_cells), 'ocr_sha256': _cell_fingerprint(ocr_cells),
                      'native_count': len(page.cells), 'native_sha256': _cell_fingerprint(page.cells)} if record is not None else None
            if record is not None:
                _observe_prepost(record, page, ocr_cells, before=True)
            result = super().post_process_cells(ocr_cells, page, conv_res, priority)
            if record is not None:
                record['post_process'] = {**before, 'final_count': len(page.cells),
                                         'final_sha256': _cell_fingerprint(page.cells)}
                _observe_prepost(record, page, ocr_cells, before=False)
            return result

    class ScanAwarePipeline(StandardPdfPipeline):
        def _create_run_ctx(self):
            context = super()._create_run_ctx()
            if audit is not None:
                with audit.lock:
                    if not audit.open:
                        raise DoclingSourceError('docling_conversion_failed')
                    audit.stages.extend(context.stages)
            return context

        def _make_ocr_model(self, art_path):
            return ScanAwareOcr(options=self.pipeline_options.ocr_options,
                enabled=self.pipeline_options.do_ocr, artifacts_path=art_path,
                accelerator_options=self.pipeline_options.accelerator_options)

    return ScanAwarePipeline


def _is_scan(page):
    if page._backend is None or page.size is None:
        return False
    cells = page._backend.get_visible_text_cells()
    if cells is None:
        cells = page._backend.get_text_cells()
    return not any(cell.text.strip() for cell in cells)


def _number(value):
    if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value):
        raise ValueError
    return float(value)


def _png(image):
    if image is None or image.width <= 0 or image.height <= 0:
        raise ValueError
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _provenance(item, pages, kind):
    result = []
    for prov in item.prov:
        page = prov.page_no
        if isinstance(page, bool) or not isinstance(page, int) or page < 1 or page not in pages:
            raise ValueError
        box = tuple(_number(getattr(prov.bbox, key)) for key in ("l", "t", "r", "b"))
        origin = getattr(prov.bbox.coord_origin, "value", prov.bbox.coord_origin)
        if origin not in {"TOPLEFT", "BOTTOMLEFT"}:
            raise ValueError
        l, t, r, b = box
        width, height = _number(pages[page].size.width), _number(pages[page].size.height)
        if not (0 <= l < r <= width + .01 and 0 <= min(t, b) < max(t, b) <= height + .01):
            raise ValueError
        charspan = getattr(prov, "charspan", None)
        if (not isinstance(charspan, (tuple, list)) or len(charspan) != 2
                or any(isinstance(n, bool) or not isinstance(n, int) for n in charspan)):
            raise ValueError
        start, end = charspan
        text = getattr(item, "text", "")
        original = getattr(item, "orig", text)
        if not isinstance(original, str) or not isinstance(text, str):
            raise ValueError
        if not 0 <= start <= end <= len(original):
            raise ValueError
        mapped = (start, end)
        if original != text:
            if len(item.prov) != 1:
                raise ValueError  # Cannot invent cross-page alignment after enrichment.
            mapped = (0, len(text))
        result.append(DocumentProvenance(page, box, origin, charspan=mapped,
                                         original_charspan=(start, end)))
    if kind == "pdf" and not result:
        raise ValueError
    # None does not invent an EPUB chapter identity that Docling discarded.
    return tuple(result) if result else (DocumentProvenance(),)


def _translate(document, kind):
    try:
        from docling_core.types.doc import PictureItem, TableItem, TextItem, ContentLayer
        pages = document.pages
        if kind == "pdf" and (not pages or sorted(pages) != list(range(1, len(pages) + 1))):
            raise ValueError
        entries, page_images = [], []
        for page_no, page in sorted(pages.items()):
            image = page.image.pil_image if page.image else None
            page_images.append(DocumentPageImage(page_no, image.width, image.height, _png(image)))
        refs = set()
        items = chain(document.iterate_items(traverse_pictures=True, included_content_layers=set(ContentLayer)),
                      document.iterate_items(root=document.furniture, traverse_pictures=True,
                                             included_content_layers=set(ContentLayer)))
        for item, _level in items:
            if not isinstance(item, (PictureItem, TableItem, TextItem)):
                raise ValueError
            if not item.self_ref or item.self_ref in refs:
                raise ValueError
            refs.add(item.self_ref)
            layer = getattr(item.content_layer, "value", item.content_layer)
            provenance = _provenance(item, pages, kind)
            if isinstance(item, PictureItem):
                entries.append(DocumentEntry("image", "", provenance, item.self_ref,
                    _png(item.get_image(document)), "image/png", label="picture", content_layer=layer))
            elif isinstance(item, TableItem):
                table_data = item.data.model_dump(mode="json")
                if not item.data.table_cells or any(not isinstance(cell.text, str) for cell in item.data.table_cells):
                    raise ValueError
                text = item.export_to_markdown(doc=document)
                if not isinstance(text, str) or not text.strip():
                    raise ValueError
                entries.append(DocumentEntry("table", text, provenance, item.self_ref,
                    table_data=table_data, label="table", content_layer=layer))
            else:
                if not isinstance(item.text, str):
                    raise ValueError
                label = getattr(item.label, "value", item.label)
                entry_kind = "formula" if label == "formula" else "text"
                original = getattr(item, "orig", item.text)
                if not item.text.strip():
                    # Empty formula text cannot be interpreted as recognized mathematics.
                    if entry_kind != "formula":
                        raise ValueError
                    entries.append(DocumentEntry("formula", "", provenance, item.self_ref,
                        _png(item.get_image(document)), "image/png", label=label, content_layer=layer,
                        original_text=original))
                else:
                    entries.append(DocumentEntry(entry_kind, item.text, provenance, item.self_ref,
                                                 label=label, content_layer=layer, original_text=original))
        origin = document.origin.model_dump(mode="json") if document.origin else None
        return DoclingSourceResult(tuple(entries), len(pages) if kind == "pdf" else None,
            {"document_name": document.name, "origin": origin,
             "epub_chapter_provenance": "unavailable" if kind == "epub" else None,
             "formula_enrichment": kind == "pdf",
             "formula_model": "docling-project/CodeFormulaV2" if kind == "pdf" else None},
            page_images=tuple(page_images))
    except DoclingSourceError:
        raise
    except Exception as error:
        raise DoclingSourceError("docling_invalid_output") from error
