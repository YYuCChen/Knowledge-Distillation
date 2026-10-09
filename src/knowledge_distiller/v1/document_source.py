"""Project structured document output without losing its format provenance."""
from dataclasses import asdict, dataclass
import hashlib
import posixpath
import json
import math
import weakref
from .source_parsing import ParsedMedia, SourceReadError


def convert_document(content, kind, converter=None):
    from .docling_source import DoclingSourceConverter, DoclingSourceError
    try:
        return (converter or DoclingSourceConverter()).convert_bytes(content, kind)
    except DoclingSourceError as error:
        raise SourceReadError(str(error), retryable=str(error) in {
            'docling_runtime_unavailable', 'docling_conversion_failed',
            'docling_component_missing', 'docling_component_corrupt',
            'docling_component_unreadable', 'docling_component_unsafe_path'}) from error


@dataclass(frozen=True)
class ComposedDocument:
    snapshot: str
    spans: list
    media: tuple
    images: list
    uncertainties: tuple


def page_member_id(page, *, context=None):
    """Keep PDF IDs; scope EPUB page occurrences by actual spine/resource."""
    if context is None:
        return f'page-{page}'
    if not isinstance(context, dict):
        raise SourceReadError('docling_invalid_output')
    spine, resource = context.get('spine'), context.get('resource')
    if (type(spine) is not int or spine < 1 or type(page) is not int or page < 1
            or type(resource) is not str or not resource or '\x00' in resource
            or '\\' in resource or resource.startswith('/')
            or resource in {'.', '..'} or resource.startswith('../')
            or posixpath.normpath(resource) != resource):
        raise SourceReadError('docling_invalid_output')
    try:
        resource_sha = hashlib.sha256(resource.encode('utf-8', errors='strict')).hexdigest()
    except UnicodeError as error:
        raise SourceReadError('docling_invalid_output') from error
    return f'page-epub-{spine}-{resource_sha}-{page}'


def compose_document(converted, *, context=None, media_offset=0, ocr=None, _collector=None):
    from .ocr import default_ocr_runner
    ocr = ocr or default_ocr_runner()
    pieces, spans, media = [], [], []
    cursor = 0
    images, uncertainties = [], []
    for index, entry in enumerate(converted.entries):
        locator = {**(context or {}), 'entry': index, 'ref': entry.ref,
                   'kind': entry.kind, 'label': entry.label,
                   'original_text': entry.original_text,
                   'provenance': [asdict(p) for p in entry.provenance]}
        if entry.provenance and entry.provenance[0].page is not None:
            locator['physical_page'] = entry.provenance[0].page
        if entry.table_data is not None:
            locator['table_data'] = entry.table_data
        if entry.image_bytes is not None:
            member_id = f'image-{media_offset + len(media) + 1}'
            media.append(ParsedMedia(member_id, entry.mime, entry.image_bytes))
            locator['member_id'] = member_id
        text = entry.text
        image_lineage, image_uncertainties = [], []
        if entry.kind == 'image':
            from .image_source import image_source_fact
            import hashlib
            executions = [] if _collector is not None else None
            fact, lineage = image_source_fact('', [{'member_id': member_id,
                'sha256': hashlib.sha256(entry.image_bytes).hexdigest(),
                'mime_type': entry.mime, 'content': entry.image_bytes}], ocr,
                execution_results=executions)
            if _collector is not None:
                _collector.image(executions, entry.image_bytes, entry.mime, member_id)
            text = fact.snapshot if fact.snapshot != '[原始图片来源]' else f"[原图 {member_id}]"
            image_lineage = lineage['image_ocr']
            image_uncertainties = fact.uncertainties
        if not text:
            if _collector is not None:
                _collector.entry(index, entry, locator, text, None, None, [])
            continue
        if pieces:
            pieces.append('\n\n'); cursor += 2
        start = cursor
        pieces.append(text); cursor += len(text)
        for image in image_lineage:
            for line in image['lines']:
                line['start'] += start; line['end'] += start
            images.append(image)
        uncertainties.extend({**u, 'start': u['start']+start, 'end': u['end']+start} for u in image_uncertainties)
        ranges = [(locator, 0, len(text))]
        if entry.kind == 'text' and entry.provenance and entry.provenance[0].page is not None:
            ranges = []
            for provenance in entry.provenance:
                begin, end = provenance.charspan
                if begin >= end or end > len(text):
                    raise SourceReadError('docling_invalid_output')
                ranges.append(({**locator, 'physical_page': provenance.page,
                                'provenance': [asdict(provenance)]}, begin, end))
        for part, begin, end in ranges:
            spans.append({**part, 'start': start+begin, 'end': start+end,
                          'page_local_start': 0, 'page_local_end': end-begin,
                          'occurrence': f"{(context or {}).get('resource', 'document')}/{entry.ref}/{index}/{begin}"})
        if _collector is not None:
            _collector.entry(index, entry, locator, text, start, cursor,
                             [{'start': start+b, 'end': start+e} for _, b, e in ranges])
    for page in converted.page_images:
        media.append(ParsedMedia(page_member_id(page.page, context=context), page.mime, page.image_bytes))
    return ComposedDocument(''.join(pieces), spans, tuple(media), images, tuple(uncertainties))


_SOURCE_SEAL = object()


def _json_bytes(value):
    """Finite JSON only; never serialize an opaque receipt or an arbitrary object."""
    def check(node):
        if node is None or type(node) in (bool, int):
            return
        if type(node) is float and math.isfinite(node):
            return
        if type(node) is str:
            node.encode('utf-8', errors='strict')
            if '\x00' not in node:
                return
        if type(node) in (list, tuple):
            for child in node:
                check(child)
            return
        if type(node) is dict and all(type(k) is str for k in node):
            for key, child in node.items():
                check(key); check(child)
            return
        raise ValueError('document_receipt_invalid')
    check(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                      allow_nan=False).encode('utf-8', errors='strict')


def _bytes_binding(data):
    if type(data) is not bytes:
        raise ValueError('document_receipt_invalid')
    return {'byte_count': len(data), 'sha256': hashlib.sha256(data).hexdigest()}


def _submitted_binding(source):
    from .file_sources import SubmittedSource
    if (type(source) is not SubmittedSource or source.source_kind not in {'pdf', 'epub'}
            or type(source.source_key) is not str or type(source.label) is not str
            or source.source_key != _bytes_binding(source.content)['sha256']):
        raise ValueError('document_receipt_invalid')
    return {'kind': source.source_kind, 'key': source.source_key, 'label': source.label,
            'content': _bytes_binding(source.content), 'metadata': source.metadata}


def _parsed_binding(parsed):
    from .source_parsing import ParsedSource, ParsedMedia
    if type(parsed) is not ParsedSource or type(parsed.snapshot) is not str:
        raise ValueError('document_receipt_invalid')
    if type(parsed.media) is not tuple or any(type(m) is not ParsedMedia for m in parsed.media):
        raise ValueError('document_receipt_invalid')
    return {'snapshot': parsed.snapshot, 'metadata': parsed.metadata, 'lineage': parsed.lineage,
            'uncertainties': parsed.uncertainties,
            'media': [{'member_id': m.member_id, 'mime_type': m.mime_type,
                       **_bytes_binding(m.content)} for m in parsed.media]}


def _entry_binding(entry):
    value = asdict(entry)
    raw = value.pop('image_bytes')
    value['image_bytes'] = _bytes_binding(raw) if raw is not None else None
    return value


@dataclass(frozen=True, slots=True, init=False)
class DocumentSourceReceipt:
    """Same-object primary parse evidence, deliberately not completeness authority."""
    _seal: object
    _audit: bytes
    _result: object
    _conversions: tuple
    _images: tuple
    _whole: bytes

    def __init__(self, seal, audit, result, conversions, images, whole):
        if seal is not _SOURCE_SEAL:
            raise TypeError('document_receipt_invalid')
        for name, value in (('_seal', seal), ('_audit', audit), ('_result', weakref.ref(result)),
                            ('_conversions', tuple(conversions)), ('_images', tuple(images)),
                            ('_whole', whole)):
            object.__setattr__(self, name, value)

    @property
    def audit_json(self):
        return self._audit.decode('utf-8')


class _DocumentCollector:
    """Private, finite PDF/EPUB execution collector; no cache or JSON authority."""
    def __init__(self, source, enabled):
        self.input = _json_bytes(_submitted_binding(source))
        self.enabled = enabled
        self.units, self.conversions, self.images = [], [], []
        self.current = None
        self.inventory = None

    def begin(self, data, kind, converted, context):
        from .docling_source import validate_conversion_receipt, DoclingSourceError
        audit = None
        try:
            audit = validate_conversion_receipt(converted, data, kind)
        except DoclingSourceError:
            self.enabled = False
        self.conversions.append((converted, data, kind, _json_bytes(audit)))
        self.current = {'input': _bytes_binding(data), 'kind': kind, 'context': context,
                        'conversion': audit, 'entries': [], 'image_executions': [],
                        'page_images': [{'page': p.page, 'width': p.width, 'height': p.height,
                            'member_id': page_member_id(p.page, context=context or None),
                            'mime_type': p.mime, **_bytes_binding(p.image_bytes)}
                            for p in converted.page_images]}
        self.units.append(self.current)

    def image(self, executions, data, mime, member_id):
        from .ocr import OcrError
        from .vision_ocr import validate_image_receipt
        if len(executions) != 1 or executions[0][0] != member_id:
            self.enabled = False
            return
        result = executions[0][1]
        audit = None
        try:
            audit = validate_image_receipt(result, data, mime, member_id)
        except OcrError:
            self.enabled = False
        self.images.append((result, data, mime, member_id, _json_bytes(audit)))
        self.current['image_executions'].append({'member_id': member_id, 'audit': audit})

    def entry(self, index, entry, locator, text, start, end, ranges):
        self.current['entries'].append({'ordinal': index, 'ref': entry.ref,
            'locator': locator, 'input_entry': _entry_binding(entry),
            'input_text': entry.text, 'output_text': text,
            'ocr_execution': ('same_run' if entry.kind == 'image' else
                              'not_requested' if entry.image_bytes is not None else 'not_applicable'),
            'start': start, 'end': end, 'ranges': ranges})

    def finish(self, composed, start, *, native=None, references=None):
        unit = self.current
        unit.update({'snapshot': composed.snapshot, 'global_start': start,
                     'global_end': start+len(composed.snapshot), 'native_occurrences': native,
                     'ordered_page_references': references, 'spans': composed.spans,
                     'media': [{'member_id': m.member_id, 'mime_type': m.mime_type,
                                **_bytes_binding(m.content)} for m in composed.media]})
        span_index = 0
        expected_media = []
        for row in unit['entries']:
            raw = row['input_entry']['image_bytes']
            if raw is not None:
                expected_media.append({'member_id': row['locator'].get('member_id'),
                    'mime_type': row['input_entry']['mime'], **raw})
            a, b = row['start'], row['end']
            if a is None:
                if row['output_text']:
                    raise SourceReadError('document_receipt_invalid')
                continue
            if (type(a) is not int or type(b) is not int or not 0 <= a <= b <= len(composed.snapshot)
                    or composed.snapshot[a:b] != row['output_text']):
                raise SourceReadError('document_receipt_invalid')
            for part in row['ranges']:
                if not a <= part['start'] < part['end'] <= b:
                    raise SourceReadError('document_receipt_invalid')
                if span_index >= len(composed.spans):
                    raise SourceReadError('document_receipt_invalid')
                span = composed.spans[span_index]
                if (span.get('entry') != row['ordinal'] or span.get('ref') != row['ref']
                        or span.get('start') != part['start'] or span.get('end') != part['end']):
                    raise SourceReadError('document_receipt_invalid')
                span_index += 1
        expected_media.extend({'member_id': p['member_id'], 'mime_type': p['mime_type'],
                               'byte_count': p['byte_count'], 'sha256': p['sha256']}
                              for p in unit['page_images'])
        if span_index != len(composed.spans) or _json_bytes(expected_media) != _json_bytes(unit['media']):
            raise SourceReadError('document_receipt_invalid')

    def issue(self, parsed, source):
        if not self.enabled:
            return parsed
        if self.input != _json_bytes(_submitted_binding(source)):
            raise SourceReadError('document_receipt_invalid')
        for unit, (converted, _, _, _) in zip(self.units, self.conversions, strict=True):
            if (len(unit['entries']) != len(converted.entries)
                    or _json_bytes([row['input_entry'] for row in unit['entries']])
                       != _json_bytes([_entry_binding(entry) for entry in converted.entries])
                    or parsed.snapshot[unit['global_start']:unit['global_end']] != unit['snapshot']):
                raise SourceReadError('document_receipt_invalid')
        expected_spans = [{**span, 'start': span['start']+unit['global_start'],
                           'end': span['end']+unit['global_start']}
                          for unit in self.units for span in unit['spans']]
        actual_spans = [{k: v for k, v in span.items() if k != 'native_occurrences'}
                        for span in parsed.lineage['spans']]
        if (_json_bytes(expected_spans) != _json_bytes(actual_spans)
                or _json_bytes([m for unit in self.units for m in unit['media']])
                   != _json_bytes(_parsed_binding(parsed)['media'])):
            raise SourceReadError('document_receipt_invalid')
        audit = _json_bytes({'contract': 'document-primary-receipt-v1',
            'coverage': 'unverified', 'limitations': ['native_cells_not_captured',
                'table_cell_mapping_unverified', 'ordered_compacted_text_not_original_byte_ranges'],
            'input': json.loads(self.input), 'output': _parsed_binding(parsed),
            'units': self.units, 'zip_inventory': self.inventory})
        receipt = DocumentSourceReceipt(_SOURCE_SEAL, audit, parsed, self.conversions, self.images, source.content)
        object.__setattr__(parsed, 'receipt', receipt)
        validate_document_receipt(parsed, source)
        return parsed


def parse_document_with_receipt(source, *, converter=None, ocr=None):
    """Parse one whole submitted PDF/EPUB; unsigned dependencies stay diagnostic.

    This primary receipt grants neither source completeness nor raw qualification.
    """
    try:
        collector = _DocumentCollector(source, converter is None and ocr is None)
    except (ValueError, TypeError, UnicodeError) as error:
        raise SourceReadError('document_receipt_invalid') from error
    if source.source_kind == 'pdf':
        from .pdf_source import parse_pdf
        parser = parse_pdf
    else:
        from .epub_source import parse_epub
        parser = parse_epub
    try:
        parsed = parser(source.content, source.label, source.source_key,
                        converter=converter, ocr=ocr, _collector=collector)
        return collector.issue(parsed, source)
    except (ValueError, TypeError, UnicodeError) as error:
        raise SourceReadError('document_receipt_invalid') from error


def validate_document_receipt(parsed, source):
    """Pure same-object validation, including held issued children; never rerun IO/OCR."""
    from .docling_source import validate_conversion_receipt, DoclingSourceError
    from .ocr import OcrError
    from .vision_ocr import validate_image_receipt
    try:
        receipt = parsed.receipt
        if (type(receipt) is not DocumentSourceReceipt or receipt._seal is not _SOURCE_SEAL
                or receipt._result() is not parsed or type(receipt._audit) is not bytes):
            raise ValueError('document_receipt_invalid')
        audit = json.loads(receipt._audit)
        if (audit['contract'] != 'document-primary-receipt-v1' or audit['coverage'] != 'unverified'
                or _json_bytes(audit['input']) != _json_bytes(_submitted_binding(source))
                or source.content != receipt._whole
                or _json_bytes(audit['output']) != _json_bytes(_parsed_binding(parsed))
                or len(audit['units']) != len(receipt._conversions)):
            raise ValueError('document_receipt_invalid')
        for unit, (result, data, kind, original) in zip(audit['units'], receipt._conversions, strict=True):
            current = _json_bytes(validate_conversion_receipt(result, data, kind))
            if (current != original or current != _json_bytes(unit['conversion'])
                    or _bytes_binding(data) != unit['input'] or kind != unit['kind']):
                raise ValueError('document_receipt_invalid')
        image_audits = [image for unit in audit['units'] for image in unit['image_executions']]
        if len(image_audits) != len(receipt._images):
            raise ValueError('document_receipt_invalid')
        for image, (result, data, mime, member_id, original) in zip(image_audits, receipt._images, strict=True):
            current = _json_bytes(validate_image_receipt(result, data, mime, member_id))
            if (current != original or current != _json_bytes(image['audit'])
                    or member_id != image['member_id']):
                raise ValueError('document_receipt_invalid')
        return audit
    except (AttributeError, KeyError, TypeError, ValueError, UnicodeError,
            DoclingSourceError, OcrError) as error:
        raise SourceReadError('document_receipt_invalid') from error
