"""Deterministic OCR reading paragraphs with exact spans into immutable text."""
from dataclasses import dataclass
import re
from statistics import median


@dataclass(frozen=True)
class ReadingBlock:
    start: int
    end: int
    text: str
    kind: str = 'paragraph'


def _bounds(line):
    polygon = line.get('polygon') or []
    if not polygon:
        return None
    xs, ys = zip(*polygon)
    return min(xs), min(ys), max(xs), max(ys)


def _plain(snapshot, start, end):
    blocks = []
    for match in re.finditer(r'[^\n]+(?:\n(?![ \t]*\n)[^\n]+)*', snapshot[start:end]):
        raw = match.group()
        text = raw.strip()
        if text:
            offset = start + match.start() + len(raw) - len(raw.lstrip())
            blocks.append(ReadingBlock(offset, offset + len(text), text))
    return blocks


def _separator(previous, current):
    # Preserve Latin word boundaries, digits, and original hyphenation.
    if previous[-1:].isdigit() and current[:1].isdigit():
        return '\n'
    return ' ' if re.search(r'[A-Za-z0-9]$', previous) and re.match(r'[A-Za-z0-9]', current) else ''


def _new_paragraph(previous, current, typical_height, image_width):
    a, b = _bounds(previous), _bounds(current)
    if not a or not b:
        # No layout evidence: join only obvious unfinished full-length lines.
        return len(previous['text']) < 20 or bool(re.search(r'[。！？!?：:]$', previous['text']))
    ah, bh = a[3] - a[1], b[3] - b[1]
    if b[1] < a[3] - min(ah, bh) * .3:  # same row/another column
        return True
    if b[1] - a[3] > typical_height * .9:
        return True
    if max(ah, bh) > typical_height * 1.4:
        return True
    if image_width and (a[2] - a[0]) < image_width * .45:
        return True
    if re.match(r'^(?:[一二三四五六七八九十]+[、．.]|\d+[、．.](?!\d)|[-*]\s|[•●▪])', current['text']):
        return True
    # An indented next line after a completed sentence is a paragraph break.
    return bool(re.search(r'[。！？!?]$', previous['text']) and b[0] - a[0] > typical_height * .6)


def build_reading(snapshot, lineage):
    """Reflow OCR soft wraps only; no words, punctuation or numbers are rewritten."""
    images = [image for image in lineage.get('image_ocr', []) if image.get('lines')]
    if not images:
        return tuple(_plain(snapshot, 0, len(snapshot)))
    blocks, cursor = [], 0
    for image in images:
        lines = [{**line, 'text': line.get('text', snapshot[line['start']:line['end']])}
                 for line in image['lines']]
        first, last = lines[0]['start'], lines[-1]['end']
        if first < cursor or last > len(snapshot):
            raise ValueError('reading_source_mismatch')
        blocks.extend(_plain(snapshot, cursor, first))
        heights = [b[3] - b[1] for line in lines if (b := _bounds(line)) and b[3] > b[1]]
        typical = median(heights) if heights else 1
        group, text = [], ''
        for line in lines:
            start, end = line['start'], line['end']
            if not 0 <= start < end <= len(snapshot) or snapshot[start:end] != line['text']:
                raise ValueError('reading_source_mismatch')
            if group:
                gap = snapshot[group[-1]['end']:start]
                if start < group[-1]['end'] or gap.strip():
                    raise ValueError('reading_source_mismatch')
                if _new_paragraph(group[-1], line, typical, image.get('width', 0)):
                    blocks.append(ReadingBlock(group[0]['start'], group[-1]['end'], text))
                    group, text = [], ''
            if group:
                text += _separator(text, line['text'])
            text += line['text']
            group.append(line)
        if group:
            blocks.append(ReadingBlock(group[0]['start'], group[-1]['end'], text))
        cursor = last
    blocks.extend(_plain(snapshot, cursor, len(snapshot)))
    # One content conservation check at the reading boundary, not in each renderer.
    if re.sub(r'\s', '', ''.join(block.text for block in blocks)) != re.sub(r'\s', '', snapshot):
        raise ValueError('reading_content_mismatch')
    return tuple(blocks)
