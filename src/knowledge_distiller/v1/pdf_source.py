"""Exact PDF protection/metadata and Docling structured page extraction."""
from __future__ import annotations

import hashlib
import io
from xml.etree import ElementTree as ET

from pypdf import PdfReader

from .source_parsing import ParsedSource, SourceReadError


def qualify_pdf(content: bytes) -> None:
    if not content.startswith(b"%PDF-"):
        raise SourceReadError("pdf_wrong_type")
    try:
        reader = PdfReader(io.BytesIO(content), strict=True)
        if reader.is_encrypted:
            raise SourceReadError("pdf_protected")
        # Encryption lives in the trailer; do not attempt passwords/decryption.
        if "/Root" not in reader.trailer:
            raise SourceReadError("pdf_protection_unknown")
    except SourceReadError:
        raise
    except Exception as error:
        raise SourceReadError("pdf_protection_unknown") from error


def parse_pdf(content: bytes, label: str, source_key: str, *, converter=None, ocr=None) -> ParsedSource:
    if hashlib.sha256(content).hexdigest() != source_key:
        raise SourceReadError("file_snapshot_mismatch")
    qualify_pdf(content)
    reader = PdfReader(io.BytesIO(content), strict=True)
    root = reader.trailer["/Root"]
    if any(key in root for key in ("/AcroForm", "/OCProperties", "/OpenAction", "/AA")):
        raise SourceReadError("pdf_content_unsupported")
    seen = set()
    for page in reader.pages:
        reference = page.indirect_reference
        identity = (reference.idnum, reference.generation) if reference else id(page)
        if identity in seen:
            raise SourceReadError("pdf_reading_order_uncertain")
        seen.add(identity)
    metadata = _metadata(reader)
    from .document_source import convert_document, compose_document
    converted = convert_document(content, 'pdf', converter)
    if converted.page_count != len(reader.pages):
        raise SourceReadError('pdf_pages_incomplete')
    composed = compose_document(converted, ocr=ocr)
    snapshot = composed.snapshot
    if not snapshot.strip() or (not any(any(c.isalnum() for c in e.text) for e in converted.entries) and not any(i['lines'] for i in composed.images)):
        raise SourceReadError('pdf_empty_content')
    metadata.update({'submitted_name': label, 'byte_length': len(content),
                     'parser': 'docling', 'parser_version': converted.runtime_version})
    return ParsedSource(snapshot, metadata,
        {'version': 2, 'kind': 'pdf-pages', 'source_key': source_key,
         'snapshot_sha256': hashlib.sha256(snapshot.encode()).hexdigest(),
         'spans': composed.spans, 'image_ocr': composed.images,
         'pages': [{'page': p.page, 'width': p.width, 'height': p.height, 'member_id': f'page-{p.page}'} for p in converted.page_images],
         'parser': 'docling', 'parser_version': converted.runtime_version}, composed.media, composed.uncertainties)


def _metadata(reader) -> dict:
    declarations = []
    info = reader.metadata
    fields = {"/Title": "title", "/Author": "author", "/Subject": "subject", "/Keywords": "keywords",
              "/CreationDate": "creation", "/ModDate": "modification", "/Creator": "creator_tool", "/Producer": "producer"}
    if info is not None:
        for field, role in fields.items():
            value = info.get(field)
            if value is None:
                continue
            if not isinstance(value, str):
                raise SourceReadError("pdf_metadata_invalid")
            declarations.append({"structure": "Info", "field": field, "role": role, "value": value,
                                 "provenance": "document-declared"})
    xmp = reader.trailer["/Root"].get("/Metadata")
    if xmp is not None:
        raw = xmp.get_object().get_data()
        if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
            raise SourceReadError("pdf_metadata_invalid")
        try:
            document = ET.fromstring(raw)
        except ET.ParseError as error:
            raise SourceReadError("pdf_metadata_invalid") from error
        rdf = "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}RDF"
        if document.tag != rdf and document.find(f".//{rdf}") is None:
            raise SourceReadError("pdf_metadata_invalid")
        namespaces = {"http://purl.org/dc/elements/1.1/": {"title": "title", "creator": "author", "description": "subject", "language": "language", "source": "origin"},
                      "http://ns.adobe.com/xap/1.0/": {"CreateDate": "creation", "ModifyDate": "modification", "CreatorTool": "creator_tool"},
                      "http://ns.adobe.com/pdf/1.3/": {"Keywords": "keywords", "Producer": "producer"}}
        for node in document.iter():
            fields_to_read = [(node.tag, "".join(node.itertext()))] if len(node) == 0 else []
            fields_to_read.extend(node.attrib.items())
            if node.tag.startswith("{http://purl.org/dc/elements/1.1/}") and len(node):
                values = ["".join(item.itertext()) for item in node.iter() if item.tag == "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}li"]
                fields_to_read.append((node.tag, values))
            for field, value in fields_to_read:
                if not field.startswith("{"):
                    continue
                namespace, local = field[1:].split("}", 1)
                role = namespaces.get(namespace, {}).get(local)
                if role:
                    declarations.append({"structure": "XMP", "field": field, "role": role, "value": value,
                                         "provenance": "document-declared"})
    for role in ("author", "origin"):
        claims = [tuple(c["value"] if isinstance(c["value"], list) else [c["value"]]) for c in declarations if c["role"] == role and c["value"]]
        if len(set(claims)) > 1:
            raise SourceReadError("pdf_metadata_conflict")
    result = {"document_declared": declarations}
    for role, field in (("title", "source_title"), ("author", "author")):
        claim = next((c["value"] for c in declarations if c["role"] == role and c["value"]), None)
        if claim is not None:
            display = "、".join(claim) if isinstance(claim, list) else claim
            result[field] = {"display_name": display, "provenance": "document-declared"} if role == "author" else display
    return result
