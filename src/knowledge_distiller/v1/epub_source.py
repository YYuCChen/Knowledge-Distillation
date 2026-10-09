"""Single EPUB container, complete spine and deterministic XML text occurrences."""
from __future__ import annotations

import hashlib
import io
import posixpath
import re
from urllib.parse import unquote, urlsplit
import zipfile
from xml.etree import ElementTree as ET

from .source_parsing import ParsedSource, SourceReadError
from .ocr import OcrError


OPF = "http://www.idpf.org/2007/opf"
DC = "http://purl.org/dc/elements/1.1/"
XHTML = "http://www.w3.org/1999/xhtml"
ET.register_namespace('', XHTML)


def qualify_epub(content: bytes) -> None:
    if not content.startswith(b"PK"):
        raise SourceReadError("epub_wrong_type")
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise SourceReadError("epub_protection_unknown")
            if any(item.flag_bits & 1 for item in archive.infolist()) or any(name.lower() in {"meta-inf/encryption.xml", "meta-inf/rights.xml"} for name in names):
                raise SourceReadError("epub_protected")
            if archive.read("mimetype") != b"application/epub+zip":
                raise SourceReadError("epub_wrong_type")
            # A readable canonical directory is needed to know this is one EPUB.
            container = _xml(archive.read("META-INF/container.xml"))
            roots = [node.get("full-path", "") for node in container.iter() if _local(node.tag) == "rootfile"]
            if len(roots) != 1 or roots[0] not in names:
                raise SourceReadError("epub_protection_unknown")
            if archive.testzip() is not None:
                raise SourceReadError("epub_protection_unknown")
    except SourceReadError:
        raise
    except (KeyError, zipfile.BadZipFile, RuntimeError, OSError, ET.ParseError, ValueError, NotImplementedError, EOFError) as error:
        raise SourceReadError("epub_protection_unknown") from error


def parse_epub(content: bytes, label: str, source_key: str, *, converter=None, ocr=None) -> ParsedSource:
    if hashlib.sha256(content).hexdigest() != source_key:
        raise SourceReadError("file_snapshot_mismatch")
    qualify_epub(content)
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            container = _xml(archive.read("META-INF/container.xml"))
            package_path = next(node.get("full-path") for node in container.iter() if _local(node.tag) == "rootfile")
            package = _xml(archive.read(package_path))
            if package.tag != f"{{{OPF}}}package":
                raise SourceReadError("epub_package_invalid")
            manifests = package.findall(f"{{{OPF}}}manifest")
            spines = package.findall(f"{{{OPF}}}spine")
            if len(manifests) != 1 or len(spines) != 1:
                raise SourceReadError("epub_spine_invalid")
            items = list(manifests[0])
            manifest = {item.get("id"): item.attrib for item in items}
            references = list(spines[0])
            if not items or None in manifest or "" in manifest or len(manifest) != len(items) or not references:
                raise SourceReadError("epub_spine_invalid")
            if any(node.tag != f"{{{OPF}}}item" for node in items) or any(node.tag != f"{{{OPF}}}itemref" for node in references):
                raise SourceReadError("epub_spine_invalid")
            metadata = _metadata(package)
            documents = []
            document_ids = {}
            resources = set()
            seen = set()
            for ordinal, reference in enumerate(references, 1):
                item_id = reference.get("idref")
                item = manifest.get(item_id)
                if item is None or item_id in seen or reference.get("linear", "yes") not in {"yes", "no"}:
                    raise SourceReadError("epub_spine_invalid")
                seen.add(item_id)
                if (item.get("media-type") != "application/xhtml+xml" or "scripted" in item.get("properties", "").split()
                        or "rendition:layout-pre-paginated" in reference.get("properties", "").split()):
                    raise SourceReadError("epub_content_unsupported")
                resource = _resource(package_path, item.get("href", ""))
                if resource in resources:
                    raise SourceReadError("epub_spine_invalid")
                resources.add(resource)
                try:
                    document = _xml(archive.read(resource))
                except KeyError as error:
                    raise SourceReadError("epub_chapter_missing") from error
                _validate_document(document, archive, resource)
                ids = [node.get("id") for node in document.iter() if node.get("id")]
                if len(ids) != len(set(ids)):
                    raise SourceReadError("epub_chapter_invalid")
                document_ids[resource] = set(ids)
                documents.append((ordinal, resource, document, reference.get("linear", "yes")))
            # Supplemental spine entries (linear=no) are read in declared order,
            # including endnotes. A footnote cannot silently refer to excluded text.
            for _, resource, document, _ in documents:
                for node in document.iter():
                    if _local(node.tag) != "a":
                        continue
                    epub_type = node.get("{http://www.idpf.org/2007/ops}type", "").split()
                    role = node.get("role", "")
                    if "noteref" not in epub_type and role != "doc-noteref":
                        continue
                    href = node.get("href", "")
                    parts = urlsplit(href)
                    target = _resource(resource, parts.path) if parts.path else resource
                    if parts.scheme or parts.netloc or not parts.fragment or target not in document_ids or unquote(parts.fragment) not in document_ids[target]:
                        raise SourceReadError("epub_footnote_missing")
            from .document_source import convert_document, compose_document
            from .ocr import default_ocr_runner
            ocr = ocr or default_ocr_runner()
            pieces, spans, media, images, uncertainties, chapters = [], [], [], [], [], []
            cursor = 0
            for ordinal, resource, document, linear in documents:
                body = document.find(f"{{{XHTML}}}body")
                if body is None:
                    raise SourceReadError("epub_chapter_invalid")
                converted = convert_document(_chapter_container(archive, package_path, package, ordinal, resource, document), 'epub', converter)
                composed = compose_document(converted, context={'spine': ordinal, 'resource': resource, 'linear': linear},
                                            media_offset=len(media), ocr=ocr)
                native_spans = _bind_occurrences(body, composed.snapshot, ordinal, resource, linear)
                chapters.append({'spine': ordinal, 'resource': resource, 'linear': linear,
                                 'anchors': [node.get('id') for node in document.iter() if node.get('id')],
                                 'links': [node.get('href') for node in document.iter() if _local(node.tag) == 'a' and node.get('href')]})
                if pieces:
                    pieces.append("\n\n"); cursor += 2
                pieces.append(composed.snapshot)
                for span in composed.spans:
                    spans.append({**span, 'start': span['start']+cursor, 'end': span['end']+cursor,
                                  'native_occurrences': [x for x in native_spans if x['start'] < span['end'] and x['end'] > span['start']]})
                for image in composed.images:
                    image['resource'] = resource
                    for line in image['lines']:
                        line['start'] += cursor; line['end'] += cursor
                    images.append(image)
                uncertainties.extend({**u, 'start': u['start']+cursor, 'end': u['end']+cursor} for u in composed.uncertainties)
                media.extend(composed.media)
                cursor += len(composed.snapshot)
    except (SourceReadError, OcrError):
        raise
    except (KeyError, zipfile.BadZipFile, ET.ParseError, ValueError, RuntimeError, OSError, RecursionError) as error:
        raise SourceReadError("epub_malformed") from error
    snapshot = "".join(pieces)
    if not snapshot.strip():
        raise SourceReadError("epub_empty_content")
    metadata.update({"submitted_name": label, "byte_length": len(content), "package": package_path,
                     "package_version": package.get("version"), "unique_identifier": package.get("unique-identifier"),
                     "parser": "docling", "parser_version": converted.runtime_version})
    return ParsedSource(snapshot, metadata,
                        {"version": 2, "kind": "epub-spine", "source_key": source_key, "package": package_path,
                         "snapshot_sha256": hashlib.sha256(snapshot.encode()).hexdigest(), "spans": spans,
                         "image_ocr": images, "chapters": chapters, "parser": "docling", "parser_version": converted.runtime_version},
                        tuple(media), tuple(uncertainties))


def _xml(raw: bytes):
    # ElementTree never fetches an external DTD. Standard XHTML declarations
    # are harmless; internal subsets/entities are outside this parser profile.
    if b"<!ENTITY" in raw.upper() or re.search(br"<!DOCTYPE[^>]*\[", raw, flags=re.I):
        raise SourceReadError("epub_content_unsupported")
    return ET.fromstring(raw)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _resource(base: str, href: str) -> str:
    parts = urlsplit(href)
    if parts.scheme or parts.netloc or parts.query or not parts.path or parts.path.startswith("/"):
        raise SourceReadError("epub_content_unsupported")
    path = posixpath.normpath(posixpath.join(posixpath.dirname(base), unquote(parts.path)))
    if path.startswith("../") or path == ".." or "\\" in path:
        raise SourceReadError("epub_content_unsupported")
    return path


def _validate_document(document, archive, resource):
    if document.tag != f"{{{XHTML}}}html" or len(document.findall(f"{{{XHTML}}}body")) != 1:
        raise SourceReadError("epub_chapter_invalid")
    visual = {"img", "svg", "image", "canvas", "video", "audio", "object", "embed", "math"}
    for node in document.iter():
        name = _local(node.tag)
        if not node.tag.startswith(f"{{{XHTML}}}"):
            raise SourceReadError("epub_content_unsupported")
        if name in visual:
            # Only explicitly decorative images can be discarded safely.
            if name == "img" and node.get("alt") == "" and node.get("role") in {"presentation", "none"}:
                continue
            if name == "img":
                try:
                    archive.read(_resource(resource, node.get('src', '')))
                except KeyError as error:
                    raise SourceReadError('epub_image_missing') from error
                continue
            raise SourceReadError("epub_visual_only")
        if name in {"script", "iframe", "form", "input", "button", "select", "textarea", "template", "details", "noscript", "ruby", "s", "del", "ins"}:
            raise SourceReadError("epub_content_unsupported")
        if any(key.lower().startswith("on") for key in node.attrib) or "hidden" in node.attrib or node.get("aria-hidden") == "true":
            raise SourceReadError("epub_content_unsupported")
        if name == "meta" and node.get("http-equiv"):
            raise SourceReadError("epub_content_unsupported")
        if "style" in node.attrib:
            _css(node.attrib["style"], declarations_only=True)
        if name == "style":
            _css("".join(node.itertext()))
        if name == "link" and "stylesheet" in node.get("rel", "").split():
            target = _resource(resource, node.get("href", ""))
            try:
                css = archive.read(target).decode("utf-8", errors="strict")
            except (KeyError, UnicodeError) as error:
                raise SourceReadError("epub_content_unsupported") from error
            _css(css)
        if name == "table" and any(_local(item.tag) in {"td", "th"} and (item.get("rowspan", "1") != "1" or item.get("colspan", "1") != "1") for item in node.iter()):
            raise SourceReadError("epub_content_unsupported")


def _css(value: str, *, declarations_only=False):
    value = re.sub(r"/\*.*?\*/", "", value, flags=re.S)
    if "@" in value or "\\" in value or re.search(r"url\s*\(|expression\s*\(|var\s*\(", value, re.I):
        raise SourceReadError("epub_content_unsupported")
    if declarations_only:
        blocks = [value]
    else:
        if re.search(r"::?(?:before|after|first-letter|first-line)\b", value, re.I):
            raise SourceReadError("epub_content_unsupported")
        blocks = re.findall(r"[^{}]+\{([^{}]*)\}", value)
        remainder = re.sub(r"[^{}]+\{[^{}]*\}", "", value).strip()
        if remainder:
            raise SourceReadError("epub_content_unsupported")
    allowed = {"font", "font-family", "font-size", "font-weight", "font-style", "line-height", "text-align", "text-indent", "text-decoration",
               "color", "background-color", "margin", "margin-top", "margin-bottom", "margin-left", "margin-right",
               "padding", "padding-top", "padding-bottom", "padding-left", "padding-right", "border", "border-top", "border-bottom",
               "border-left", "border-right", "border-collapse", "page-break-before", "page-break-after", "break-before", "break-after"}
    for block in blocks:
        for declaration in block.split(";"):
            if not declaration.strip():
                continue
            key, colon, val = declaration.partition(":")
            key, val = key.strip().lower(), val.strip().lower()
            if not colon or key not in allowed or not val or "transparent" in val or "rgba" in val or (key in {"font-size", "line-height", "font"} and re.match(r"0(?:\D|$)", val)):
                raise SourceReadError("epub_content_unsupported")


def _text_occurrences(body):
    blocks = {"div", "section", "article", "aside", "h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "blockquote", "pre", "figcaption", "td", "th", "dt", "dd", "tr"}

    def visit(node, path):
        if _local(node.tag) == "style":
            return
        if node.text:
            yield node.text, path, "text"
        for index, child in enumerate(node):
            child_path = f"{path}/{_local(child.tag)}[{index}]"
            if _local(child.tag) in blocks | {"br"}:
                yield "\n\n", child_path, "separator"
            yield from visit(child, child_path)
            if _local(child.tag) in blocks:
                yield "\n\n", child_path, "separator"
            if child.tail:
                yield child.tail, child_path, "tail"

    yield from visit(body, "body")


def _metadata(package):
    metadata_nodes = package.findall(f"{{{OPF}}}metadata")
    if len(metadata_nodes) > 1:
        raise SourceReadError("epub_metadata_conflict")
    declarations = []
    refinements = {}
    metadata = metadata_nodes[0] if metadata_nodes else []
    for node in metadata:
        if node.tag == f"{{{OPF}}}meta" and node.get("refines") and node.get("property") == "role":
            refinements.setdefault(node.get("refines").lstrip("#"), []).append("".join(node.itertext()))
    accepted = {"title", "creator", "contributor", "publisher", "language", "identifier", "subject", "description", "date", "source"}
    for index, node in enumerate(metadata):
        name = _local(node.tag)
        if node.tag.startswith(f"{{{DC}}}") and name in accepted:
            value = "".join(node.itertext())
            roles = refinements.get(node.get("id"), [])
            if node.get(f"{{{OPF}}}role"):
                roles = [node.get(f"{{{OPF}}}role"), *roles]
            if len(set(roles)) > 1:
                raise SourceReadError("epub_metadata_conflict")
            declarations.append({"key": name, "value": value, "attributes": dict(node.attrib), "roles": roles,
                                 "occurrence": f"metadata/{index}", "provenance": "document-declared"})
        elif node.tag == f"{{{OPF}}}meta":
            value = "".join(node.itertext()) or node.get("content", "")
            if (node.get("property") == "rendition:layout" and value == "pre-paginated") or (node.get("name") == "fixed-layout" and value.lower() == "true"):
                raise SourceReadError("epub_content_unsupported")
            declarations.append({"key": node.get("property") or node.get("name") or "meta", "value": value,
                                 "attributes": dict(node.attrib), "occurrence": f"metadata/{index}",
                                 "provenance": "document-declared"})
    origins = [c["value"] for c in declarations if c["key"] == "source" and c["value"]]
    if len(set(origins)) > 1:
        raise SourceReadError("epub_metadata_conflict")
    unique = package.get("unique-identifier")
    if unique and len([c for c in declarations if c["key"] == "identifier" and c["attributes"].get("id") == unique]) != 1:
        raise SourceReadError("epub_package_invalid")
    result = {"document_declared": declarations}
    titles = [c["value"] for c in declarations if c["key"] == "title" and c["value"]]
    creators = [c["value"] for c in declarations if c["key"] == "creator" and c["value"] and (not c["roles"] or "aut" in c["roles"])]
    if titles:
        result["source_title"] = titles[0]
    if creators:
        result["author"] = {"display_name": "、".join(creators), "provenance": "document-declared"}
    return result


def _chapter_container(archive, package_path, package, ordinal, resource, document):
    """Docling receives one declared chapter; original source bytes stay authoritative."""
    import copy
    chapter_package = copy.deepcopy(package)
    spine = chapter_package.find(f'{{{OPF}}}spine')
    references = list(spine)
    for index, reference in enumerate(references, 1):
        if index != ordinal:
            spine.remove(reference)
    chapter = copy.deepcopy(document)
    # Explicitly decorative content need not exist and cannot become source prose.
    for parent in chapter.iter():
        for child in list(parent):
            if _local(child.tag) == 'style' or (_local(child.tag) == 'img' and child.get('alt') == '' and child.get('role') in {'presentation','none'}):
                tail = child.tail
                if tail:
                    index = list(parent).index(child)
                    if index:
                        previous = parent[index-1]; previous.tail = (previous.tail or '') + tail
                    else:
                        parent.text = (parent.text or '') + tail
                parent.remove(child)
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as target:
        for entry in archive.infolist():
            data = archive.read(entry.filename)
            if entry.filename == package_path:
                data = ET.tostring(chapter_package, encoding='utf-8', xml_declaration=True)
            elif entry.filename == resource:
                data = ET.tostring(chapter, encoding='utf-8', xml_declaration=True)
            target.writestr(copy.copy(entry), data)
    return output.getvalue()


def _bind_occurrences(body, snapshot, ordinal, resource, linear):
    """Require every native text occurrence in declared order, retaining exact paths."""
    indices = [i for i,c in enumerate(snapshot) if not c.isspace()]
    compact = ''.join(snapshot[i] for i in indices)
    cursor, spans = 0, []
    for text, path, slot in _text_occurrences(body):
        value = ''.join(c for c in text if not c.isspace())
        if slot == 'separator' or not value:
            continue
        start = compact.find(value, cursor)
        if start < 0:
            raise SourceReadError('epub_text_incomplete')
        end = start + len(value)
        spans.append({'spine': ordinal, 'resource': resource, 'linear': linear,
                      'element_path': path, 'text_slot': slot, 'native_text': text,
                      'start': indices[start], 'end': indices[end-1]+1,
                      'occurrence': f'spine/{ordinal}/{path}/{slot}'})
        cursor = end
    return spans
