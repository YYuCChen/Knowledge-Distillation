from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Mapping

from .domain import Evidence, Knowledge, Point, validate_knowledge


@dataclass(frozen=True)
class RenderedMarkdown:
    text: str
    source_blocks: int
    evidence_links: int


@dataclass(frozen=True)
class _SourceBlock:
    start: int
    end: int
    text: str
    anchor: str


def render_markdown(
    *,
    source_kind: str,
    source_key: str,
    submitted_url: str,
    canonical_url: str,
    metadata: Mapping[str, object],
    source_fact_id: int,
    snapshot: str,
    knowledge_result_id: int,
    knowledge: Knowledge,
) -> RenderedMarkdown:
    validate_knowledge(snapshot, knowledge)
    literal_source = source_kind in {"direct_text", "markdown", "pdf", "epub", "xiaohongshu", "x", "zhihu", "weibo"}
    lineage = metadata.get('source_lineage', {})
    if lineage.get('image_ocr'):
        from .reading import build_reading
        blocks = tuple(_SourceBlock(block.start, block.end, block.text, f'source-{index}')
                       for index, block in enumerate(build_reading(snapshot, lineage), 1))
    else:
        blocks = _source_blocks(snapshot)
    evidence = {item.evidence_id: item for item in knowledge.evidence}
    targets = {
        item.evidence_id: item.member_id or _target_block(item, blocks).anchor
        for item in knowledge.evidence
    }
    lines = [
        "---",
        "cssclasses: kd-reading",
        f"kd_source_kind: {_yaml(source_kind)}",
        f"kd_source_key: {_yaml(source_key)}",
        f"kd_source_fact_id: {source_fact_id}",
        f"kd_knowledge_result_id: {knowledge_result_id}",
        "---",
        "",
        f"# {knowledge.title}",
        "",
        knowledge.subtitle,
        "",
        *_reading_header(source_kind, metadata, submitted_url if source_kind == "xiaohongshu" else canonical_url or submitted_url),
        "[[#内容概览|概览]]　/　[[#观点与依据|观点与依据]]　/　[[#来源全文|来源全文]]",
        "",
        "## 内容概览",
        "",
        knowledge.summary,
        "",
    ]
    evidence_links = 0
    lines.extend(["## 观点与依据", ""])
    if knowledge.core_points:
        lines.extend(["### 核心观点", ""])
    for point in knowledge.core_points:
        evidence_links += _append_point(lines, point, evidence, targets)
    if knowledge.other_points:
        lines.extend(["## 更多正式观点", ""])
        for point in knowledge.other_points:
            evidence_links += _append_point(lines, point, evidence, targets)

    lines.extend(["## 来源全文", ""])
    published_media = {m['member_id']: m for m in metadata.get('published_media', [])}
    positioned = set()
    for block in blocks:
        related = [span for span in lineage.get('spans', []) if span['start'] < block.end and span['end'] > block.start]
        image_ids = [span['member_id'] for span in related if span.get('member_id')]
        image_ids += [image['member_id'] for image in lineage.get('image_ocr', [])
                      if (image.get('source_start', block.end) < block.end
                          and image.get('source_end', block.start) > block.start)
                      or any(line['start'] < block.end and line['end'] > block.start for line in image['lines'])]
        for member_id in dict.fromkeys(image_ids):
            if member_id not in positioned:
                if member_id in published_media:_append_image(lines,published_media[member_id])
                else:lines.extend(['> [!kd-source] 来源定位', '> 原图 '+member_id.removeprefix('image-')+'：请通过原来源链接核对。', '', '^'+member_id, ''])
                positioned.add(member_id)
        rendered = _render_source_block(block.text, source_kind, related)
        lines.extend(["> [!kd-source] 来源原文", *_quote("\n".join(rendered)), "", f"^{block.anchor}", ""])

    for member_id, member in published_media.items():
        if member_id not in positioned:
            if member_id.startswith('page-'):
                lines.extend([f"### 原文件第 {member_id[5:]} 页", ""])
            _append_image(lines, member)

    for member_id in dict.fromkeys(e.member_id for e in knowledge.evidence if e.member_id):
        if member_id not in published_media and member_id not in positioned:
            label='原视频定位' if member_id=='video-1' else '原图 '+member_id.removeprefix('image-')+' 定位'
            lines.extend([label+'：请通过原来源链接核对。 ^'+member_id, ''])

    if any(snapshot[block.start:block.end] != block.text for block in blocks):
        fence = '`' * max(3, 1 + max((len(m[0]) for m in re.finditer(r'`+', snapshot)), default=0))
        lines.extend(['<details>', '<summary>原始 OCR 文本（核对）</summary>', '',
                      fence + 'text', snapshot, fence, '', '</details>', ''])

    lines.extend(["## 来源说明", "", f"- 来源：{source_kind}"])
    if metadata.get('source_scope'):
        _append_metadata(lines, '来源范围', metadata['source_scope'])
    author = metadata.get("author")
    if isinstance(author, Mapping):
        _append_metadata(lines, "声明作者" if literal_source else "作者", author.get("display_name"))
    _append_metadata(lines, "声明标题" if literal_source else "原平台标题", metadata.get("source_title"))
    if source_kind not in {"xiaohongshu", "x", "zhihu", "weibo"} and not (
            source_kind == 'douyin' and metadata.get('note_kind') == 'normal'):
        _append_metadata(lines, "原平台描述", metadata.get("original_description"))
    if source_kind == "zhihu":
        lines.append("- 来源范围：仅原始文本，图片与视频不在此来源范围内。")
    elif source_kind == "weibo":
        lines.append("- 来源范围：原生文章正文与静态原图。" if metadata.get("native_kind") == "article"
                     else "- 来源范围：完整主帖文本，不含引用或转发内容。")
    elif source_kind == "x":
        lines.append("- 来源范围：仅主帖正文与静态图片，不含引用或转发内容。")
    _append_metadata(lines, "发布时间", metadata.get("published_at"))
    if not literal_source or source_kind in {'xiaohongshu', 'x', 'zhihu', 'weibo'}:
        locator = submitted_url if source_kind == 'xiaohongshu' else canonical_url or submitted_url
        lines.extend([f"- 原链接：<{locator}>", ""])
    else:
        _append_metadata(lines, "提交名称", metadata.get("submitted_name"))
        declarations = metadata.get("user_declared", {})
        if isinstance(declarations, Mapping):
            _append_metadata(lines, "声明出处", declarations.get("origin"))
        for claim in metadata.get("semantic_claims", []):
            if claim.get("role") == "origin":
                _append_metadata(lines, "声明出处", claim.get("value"))
        lines.append("")
    return RenderedMarkdown("\n".join(lines), len(blocks), evidence_links)


def _append_point(
    lines: list[str],
    point: Point,
    evidence: Mapping[str, Evidence],
    targets: Mapping[str, str],
) -> int:
    lines.extend([f"### {point.statement}", "", point.argument, "", "> [!quote]- 来源依据"])
    for evidence_id in point.evidence_ids:
        # Full evidence is already saved; rendering never regenerates analysis.
        lines.extend(_quote('\n'.join(_render_source_block(evidence[evidence_id].text,'direct_text',[]))))
        lines.append(">")
    for evidence_id in point.evidence_ids:
        label = _evidence_label(evidence[evidence_id].text)
        if evidence[evidence_id].member_id == 'video-1':
            item = evidence[evidence_id]
            label += f'（视频 {item.start_seconds:.1f}–{item.end_seconds:.1f} 秒）'
        lines.append(f"> - [[#^{targets[evidence_id]}|{label}]]")
    lines.append("")
    return len(point.evidence_ids)


def _source_blocks(snapshot: str) -> tuple[_SourceBlock, ...]:
    blocks = []
    start = cursor = 0
    fence = None
    for line in snapshot.splitlines(keepends=True):
        match = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if match:
            marker = match[1]
            if fence is None:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence):
                fence = None
        if not line.strip() and fence is None:
            end = cursor
            while end > start and snapshot[end-1] in '\r\n':
                end -= 1
            if snapshot[start:end].strip():
                blocks.append(_SourceBlock(start,end,snapshot[start:end],f'source-{len(blocks)+1}'))
            start = cursor + len(line)
        cursor += len(line)
    if snapshot[start:].strip():
        blocks.append(_SourceBlock(start,len(snapshot),snapshot[start:],f'source-{len(blocks)+1}'))
    if len(blocks) == 1:
        blocks = [_SourceBlock(0,len(snapshot),snapshot,'source-1')]
    return tuple(blocks)


def _append_image(lines, member):
    lines.extend([f"![[{member['filename']}]]", f"^{member['member_id']}", ""])


def _render_source_block(text, kind, spans):
    if kind == 'direct_text' and re.search(r'!?\[\[|!\[|<|`{3,}|~{3,}', text):
        fence = '`' * max(3,1+max((len(m[0]) for m in re.finditer(r'`+',text)),default=0))
        return [fence+'text',text,fence]
    if kind == 'markdown':
        if re.search(r'!?\[\[|!\[|<|\$|javascript:|data:|(?:`{3,}|~{3,})\s*(?:mermaid|dataview)', text, re.I):
            fence = '`' * max(3,1+max((len(m[0]) for m in re.finditer(r'`+',text)),default=0))
            return [fence+'text',text,fence]
        return text.split('\n')
    if any(s.get('kind') == 'table' for s in spans):
        return text.replace('<','&lt;').replace('[[','\\[\\[').replace('![','\\![').split('\n')
    if any(s.get('kind') == 'formula' for s in spans):
        # This is Docling's mathematical transcription, never executable source code.
        return ['$$',text,'$$']
    literal = re.sub(r'([\\`*_{}\[\]<>!#|])',r'\\\1',text)
    literal = literal.replace(r'\[听辨不清\]', '[听辨不清]')
    if any(s.get('label') in {'title','section_header'} for s in spans):
        return ['### '+literal.replace('\n',' ')]
    return literal.split('\n')


def _target_block(evidence: Evidence, blocks: tuple[_SourceBlock, ...]) -> _SourceBlock:
    for block in blocks:
        if block.start < evidence.end and evidence.start < block.end:
            return block
    raise ValueError("evidence start has no source paragraph")


def _quote(value: str) -> list[str]:
    return [f"> {line}" if line else ">" for line in value.split("\n")]


def _append_metadata(lines: list[str], label: str, value: object) -> None:
    if isinstance(value, str) and value.strip():
        literal = re.sub(r"([\\`*_{}\[\]<>()!#|])", r"\\\1", value.strip())
        literal = literal.replace("\r", "\\r").replace("\n", "\\n")
        lines.append(f"- {label}：{literal}")


def _evidence_label(value: str) -> str:
    compact = " ".join(value.split())
    preview = compact[:32] + ("…" if len(compact) > 32 else "")
    return ("来源：" + preview).replace("\\", "\\\\").replace("|", "\\|")


def _yaml(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _reading_header(kind, metadata, url):
    from .reading_metadata import source_header
    return source_header(kind, metadata, url)
