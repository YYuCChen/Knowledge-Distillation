"""Exact submitted sources and the deliberately narrow Markdown 9A-1 profile.

Markdown is preserved as literal source, never rendered or dereferenced here.
YAML is composed into safe nodes; constructors never execute.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import PurePosixPath
import re
from typing import Mapping

import yaml
from yaml.nodes import MappingNode, ScalarNode, SequenceNode
from yaml.tokens import AliasToken, AnchorToken, TagToken, DirectiveToken

from .source_parsing import ParsedSource, SourceReadError


class SourceIntakeError(ValueError):
    pass


@dataclass(frozen=True)
class SubmittedSource:
    source_kind: str
    source_key: str
    label: str
    content: bytes
    metadata: Mapping[str, object]


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _direct_key(content: bytes, declarations: Mapping[str, str]) -> str:
    envelope = json.dumps(
        {"kind": "direct_text", "text": content.decode("utf-8"),
         "user_declared": dict(declarations)},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return _digest(envelope)


def prepare_direct_text(value: str, declarations: Mapping[str, str] | None = None) -> SubmittedSource:
    if not isinstance(value, str) or not value or all(_direct_whitespace(c) for c in value):
        raise SourceIntakeError("请输入要蒸馏的正文。")
    claims = dict(declarations or {})
    if any(key not in {"author", "origin", "original_title"} or not isinstance(v, str)
           for key, v in claims.items()):
        raise SourceIntakeError("来源声明只支持作者、出处和原始标题文本。")
    try:
        content = value.encode("utf-8", errors="strict")
        key = _direct_key(content, claims)
    except UnicodeError as error:
        raise SourceIntakeError("正文或来源声明包含无法完整解码的 Unicode。") from error
    return SubmittedSource("direct_text", key, "直接文本", content,
                           {"user_declared": claims})


def prepare_file(filename: str, content: bytes) -> SubmittedSource:
    label = PurePosixPath(filename.replace("\\", "/")).name
    kind = {".pdf": "pdf", ".epub": "epub", ".md": "markdown", ".markdown": "markdown"}.get(PurePosixPath(label).suffix.lower())
    if not label or kind is None:
        raise SourceIntakeError("请选择单个 PDF、EPUB、.md 或 .markdown 文件。")
    if not isinstance(content, bytes):
        raise SourceIntakeError("无法完整读取文件内容。")
    try:
        if kind == "pdf":
            from .pdf_source import qualify_pdf
            qualify_pdf(content)
        elif kind == "epub":
            from .epub_source import qualify_epub
            qualify_epub(content)
    except SourceReadError as error:
        raise SourceIntakeError("文件无法确认完整且未受加密保护，请选择可读取的原文件。") from error
    return SubmittedSource(kind, _digest(content), label, content, {})


def parse_submitted_source(source: SubmittedSource, *, converter=None, ocr=None) -> ParsedSource:
    if not isinstance(source.content, bytes):
        raise SourceReadError("source_snapshot_unavailable")
    if source.source_kind == "direct_text":
        try:
            value = source.content.decode("utf-8", errors="strict")
            claims = source.metadata.get("user_declared", {})
            prepared = prepare_direct_text(value, claims)
        except (UnicodeError, ValueError, TypeError) as error:
            raise SourceReadError("direct_snapshot_mismatch") from error
        if prepared.source_key != source.source_key:
            raise SourceReadError("direct_snapshot_mismatch")
        metadata: dict[str, object] = {"submitted_name": "直接文本", "user_declared": dict(claims)}
        if claims.get("original_title"):
            metadata["source_title"] = claims["original_title"]
        if claims.get("author"):
            metadata["author"] = {"display_name": claims["author"], "provenance": "user-declared"}
        return ParsedSource(value, metadata, _lineage(source, value, 0, 0))
    if source.source_kind == "image":
        from .feishu_images import parse
        return parse(source, ocr)
    if source.source_kind not in {"markdown", "pdf", "epub"}:
        raise SourceReadError("material_source_mismatch")
    if _digest(source.content) != source.source_key:
        raise SourceReadError("file_snapshot_mismatch")
    if source.source_kind == "pdf":
        from .pdf_source import parse_pdf
        return parse_pdf(source.content, source.label, source.source_key, converter=converter, ocr=ocr)
    if source.source_kind == "epub":
        from .epub_source import parse_epub
        return parse_epub(source.content, source.label, source.source_key, converter=converter, ocr=ocr)
    return _parse_markdown(source)


def _lineage(source: SubmittedSource, body: str, byte_start: int, decoded_start: int) -> dict:
    # For any [start:end], raw bytes are byte_start + UTF8 lengths of
    # body[:start] and body[:end]. No renderer or first-match search is involved.
    return {"version": 1, "kind": "unicode-span" if source.source_kind == "direct_text" else "markdown-raw",
            "source_key": source.source_key, "snapshot_sha256": _digest(body.encode("utf-8")),
            "raw_byte_start": byte_start, "decoded_start": decoded_start,
            "raw_byte_length": len(source.content), "mapping": "literal-utf8-prefix",
            "spans": [{"start": 0, "end": len(body), "occurrence": "body",
                       "raw_byte_start": byte_start, "raw_byte_end": len(source.content),
                       "decoded_start": decoded_start, "decoded_end": decoded_start + len(body)}]}


def _parse_markdown(source: SubmittedSource) -> ParsedSource:
    try:
        decoded = source.content.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise SourceReadError("markdown_invalid_utf8") from error
    bom = decoded.startswith("\ufeff")
    raw = decoded[1:] if bom else decoded
    if "\ufeff" in raw:
        raise SourceReadError("markdown_invalid_bom")
    # NUL encodings (including BOM-less UTF-16/32 ASCII) are not UTF-8 text.
    if "\x00" in raw:
        raise SourceReadError("markdown_invalid_utf8")
    body, start, declarations = _frontmatter(raw, 3 if bom else 0)
    if not body or all(_direct_whitespace(c) for c in body):
        raise SourceReadError("markdown_empty_content")
    if _unsafe_markdown(body):
        raise SourceReadError("markdown_unsupported_syntax")
    if _conflicting(declarations, {"author", "authors", "creator", "creators"}) or _conflicting(declarations, {"source", "origin"}):
        raise SourceReadError("markdown_metadata_conflict")
    metadata: dict[str, object] = {"submitted_name": source.label, "byte_length": len(source.content),
                                   "document_declared": declarations}
    metadata["semantic_claims"] = _semantic_claims(declarations)
    titles = _claims(declarations, {"title"}, allow_list=False)
    authors = _claims(declarations, {"author", "authors", "creator", "creators"})
    if titles:
        metadata["source_title"] = titles[0]
    if authors:
        metadata["author"] = {"display_name": "、".join(authors), "provenance": "document-declared"}
    byte_start = (3 if bom else 0) + len(raw[:start].encode("utf-8"))
    return ParsedSource(body, metadata, _lineage(source, body, byte_start, start))


def _frontmatter(raw: str, bom_bytes: int) -> tuple[str, int, list[dict]]:
    # Logical lines use CR/LF only, not str.splitlines' Unicode separator set.
    lines = re.findall(r"[^\r\n]*(?:\r\n|\r|\n|$)", raw)
    if not lines or lines[0].rstrip("\r\n") != "---":
        return raw, 0, []
    closing = next((i for i, line in enumerate(lines[1:], 1) if line.rstrip("\r\n") == "---"), None)
    if closing is None:
        raise SourceReadError("markdown_frontmatter_invalid")
    offset = len(lines[0])
    section = "".join(lines[1:closing])
    start = sum(map(len, lines[:closing + 1]))
    try:
        if any(isinstance(token, (AliasToken, AnchorToken, TagToken, DirectiveToken)) for token in yaml.scan(section, Loader=yaml.SafeLoader)):
            raise SourceReadError("markdown_frontmatter_invalid")
        root = yaml.compose(section, Loader=yaml.SafeLoader)
        if not isinstance(root, MappingNode):
            raise SourceReadError("markdown_frontmatter_invalid")
        declarations = []
        keys = set()
        for key, value in root.value:
            if not isinstance(key, ScalarNode) or key.tag != "tag:yaml.org,2002:str" or not key.value or key.value == "<<":
                raise SourceReadError("markdown_frontmatter_invalid")
            _scalar(key, section)
            if key.value in keys:
                raise SourceReadError("markdown_frontmatter_invalid")
            keys.add(key.value)
            if isinstance(value, SequenceNode):
                parsed = [_scalar(item, section) for item in value.value]
                scalars = value.value
            else:
                parsed = _scalar(value, section)
                scalars = [value]
            begin, end = offset + key.start_mark.index, offset + value.end_mark.index
            declarations.append({"key": key.value, "raw_key": section[key.start_mark.index:key.end_mark.index],
                                 "value": parsed, "raw": raw[begin:end], "start": begin, "end": end,
                                 "raw_byte_start": bom_bytes + len(raw[:begin].encode("utf-8")),
                                 "raw_byte_end": bom_bytes + len(raw[:end].encode("utf-8")),
                                 "provenance": "document-declared",
                                 "scalars": [{"value": _scalar(item, section), "raw": section[item.start_mark.index:item.end_mark.index],
                                              "start": offset + item.start_mark.index, "end": offset + item.end_mark.index}
                                             for item in scalars]})
    except (yaml.YAMLError, RecursionError) as error:
        raise SourceReadError("markdown_frontmatter_invalid") from error
    return raw[start:], start, declarations


def _scalar(node, section: str) -> str | None:
    allowed = {"str", "int", "float", "bool", "null", "timestamp"}
    if not isinstance(node, ScalarNode) or node.tag.removeprefix("tag:yaml.org,2002:") not in allowed or node.style in {"|", ">"}:
        raise SourceReadError("markdown_frontmatter_invalid")
    raw = section[node.start_mark.index:node.end_mark.index]
    if any(c in raw or c in node.value for c in ("\r", "\n", "\x85", "\u2028", "\u2029")):
        raise SourceReadError("markdown_frontmatter_invalid")
    return None if node.tag == "tag:yaml.org,2002:null" else node.value


def _ascii_lower(value: str) -> str:
    return value.translate(str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"))


def _claims(declarations, aliases: set[str], *, allow_list: bool = True) -> list[str]:
    values = []
    for claim in declarations:
        if _ascii_lower(claim["key"]) in aliases:
            value = claim["value"]
            if isinstance(value, str):
                values.append(value)
            elif allow_list and isinstance(value, list):
                values.extend(item for item in value if item is not None)
    return values



def _semantic_claims(declarations) -> list[dict]:
    roles = {
        "title": "original_title", "author": "creator", "authors": "creator",
        "creator": "creator", "creators": "creator", "source": "origin", "origin": "origin",
        "published": "publication", "publication_date": "publication", "created": "creation",
        "date": "unqualified_date", "modified": "modification", "updated": "modification",
        "last_modified": "modification", "language": "language", "lang": "language",
        "tag": "category", "tags": "category",
    }
    claims = []
    for index, declaration in enumerate(declarations):
        role = roles.get(_ascii_lower(declaration["key"]))
        value = declaration["value"]
        if role is None or (isinstance(value, list) and role not in {"creator", "origin", "language", "category"}):
            continue
        for item in value if isinstance(value, list) else [value]:
            if item is not None:
                claims.append({"role": role, "value": item, "declaration_index": index,
                               "provenance": "document-declared"})
    return claims

def _conflicting(declarations, aliases: set[str]) -> bool:
    groups = []
    for claim in declarations:
        if _ascii_lower(claim["key"]) in aliases:
            value = claim["value"]
            group = tuple(v for v in (value if isinstance(value, list) else [value]) if v is not None)
            if group:
                groups.append(group)
    return len(set(groups)) > 1


def _unsafe_markdown(value: str) -> bool:
    if re.search(r"<%|\{\{\s*(?:include|embed|query)\b|(?:`{3,}|~{3,})[ \t]*(?:dataview(?:js)?|templater)\b", value, re.I):
        return True
    # Code is literal, including angle brackets and example HTML.
    visible = _without_code_blocks(value)
    visible = re.sub(r"(`+)(?!`).*?\1(?!`)", "", visible, flags=re.S)
    if re.search(r"<!--|<![A-Z]|<\?|<!\[CDATA\[", visible, re.I):
        return True
    # Formatting-only tags without attributes can be proved inert and order-preserving.
    attribute = r"[A-Za-z_:][A-Za-z0-9_.:-]*(?:\s*=\s*(?:[^\s\"'=<>`]+|'[^']*'|\"[^\"]*\"))?"
    opening = rf"<([A-Za-z][A-Za-z0-9-]*)(?:\s+{attribute})*\s*/?>"
    closing = r"</([A-Za-z][A-Za-z0-9-]*)\s*>"
    for match in re.finditer(rf"{opening}|{closing}", visible):
        tag = (match.group(1) or match.group(2)).lower()
        if tag not in {"b", "strong", "i", "em", "u", "s", "del", "code", "span"} or not re.fullmatch(r"</?[A-Za-z]+\s*/?>", match.group()):
            return True
    return False



def _without_code_blocks(value: str) -> str:
    visible = []
    fence = None
    for line in re.findall(r"[^\r\n]*(?:\r\n|\r|\n|$)", value):
        if fence is not None:
            if re.fullmatch(rf" {{0,3}}{re.escape(fence[0])}{{{len(fence)},}}[ \t]*(?:\r\n|\r|\n)?", line):
                fence = None
            continue
        opener = re.match(r"^ {0,3}(`{3,}|~{3,})([^\r\n]*)", line)
        if opener and (opener[1][0] != "`" or "`" not in opener[2]):
            fence = opener[1]
            continue
        if line.startswith(("    ", "\t")):
            continue
        visible.append(line)
    return "".join(visible)

def _direct_whitespace(value: str) -> bool:
    code = ord(value)
    return (0x09 <= code <= 0x0D or code in {0x20, 0x85, 0xA0, 0x1680, 0x2028, 0x2029, 0x202F, 0x205F, 0x3000}
            or 0x2000 <= code <= 0x200A)
