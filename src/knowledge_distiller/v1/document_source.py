"""Project structured document output without losing its format provenance."""
from dataclasses import asdict, dataclass
from .source_parsing import ParsedMedia, SourceReadError


def convert_document(content, kind, converter=None):
    from .docling_source import DoclingSourceConverter, DoclingSourceError
    try:
        return (converter or DoclingSourceConverter()).convert_bytes(content, kind)
    except DoclingSourceError as error:
        raise SourceReadError(str(error), retryable=str(error) in {
            'docling_runtime_unavailable', 'docling_conversion_failed'}) from error


@dataclass(frozen=True)
class ComposedDocument:
    snapshot: str
    spans: list
    media: tuple
    images: list
    uncertainties: tuple


def compose_document(converted, *, context=None, media_offset=0, ocr=None):
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
            fact, lineage = image_source_fact('', [{'member_id': member_id,
                'sha256': hashlib.sha256(entry.image_bytes).hexdigest(),
                'mime_type': entry.mime, 'content': entry.image_bytes}], ocr)
            text = fact.snapshot if fact.snapshot != '[原始图片来源]' else f"[原图 {member_id}]"
            image_lineage = lineage['image_ocr']
            image_uncertainties = fact.uncertainties
        if not text:
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
    for page in converted.page_images:
        media.append(ParsedMedia(f'page-{page.page}', page.mime, page.image_bytes))
    return ComposedDocument(''.join(pieces), spans, tuple(media), images, tuple(uncertainties))
