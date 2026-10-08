"""Presentation of legacy Chinese phrase candidates without changing stored decisions."""
import re
from os.path import commonprefix


def english_assistance(text):
    return len(re.findall(r'[A-Za-z]+', text)) > len(re.findall(r'[\u3400-\u9fff]', text))


def local_choices(concern):
    original = concern['text']
    choices = list(dict.fromkeys([original, *concern.get('candidates', [])]))
    prefix = commonprefix(choices) if len(original) > 4 and len(choices) > 1 else ''
    while prefix and prefix[-1].isascii() and prefix[-1].isalnum():
        prefix = prefix[:-1]
    rest = [s[len(prefix):] for s in choices]
    suffix = commonprefix([s[::-1] for s in rest])[::-1] if len(original) > 4 and len(choices) > 1 else ''
    while suffix and suffix[0].isascii() and suffix[0].isalnum():
        suffix = suffix[1:]
    stop = len(original) - len(suffix) if suffix else len(original)
    if stop <= len(prefix):
        prefix = suffix = ''
        stop = len(original)
    labels = [s[len(prefix):len(s)-len(suffix) if suffix else len(s)] for s in concern.get('candidates', [])]
    return {'start': concern['start'] + len(prefix), 'end': concern['start'] + stop,
            'text': original[len(prefix):stop], 'prefix': prefix, 'suffix': suffix,
            'choices': list(zip(labels, concern.get('candidates', [])))}


def concern_total(pending):
    """Original review size, including decisions already saved by either client."""
    if 'concern_total' in pending:
        return pending['concern_total']
    resolved=sum(1 for entry in pending.get('resolved', [])
                 if entry.get('action') != 'local_transcription')
    return len(pending.get('concerns', []))+len(pending.get('deferred_concerns', []))+resolved


def english_candidate_display(snapshot, concern):
    """Project a verified sentence without changing candidate callback values.

    Sentence boundaries, translations and independent bases are prepared by the
    producer. This function neither guesses boundaries nor generates evidence.
    A missing/inconsistent contract raises ValueError for the caller to handle.
    """
    import hashlib
    import json

    if not isinstance(snapshot, str) or not isinstance(concern, dict):
        raise ValueError('invalid English display source')
    span = concern.get('sentence_span')
    if not isinstance(span, dict) or set(span) != {'start', 'end'}:
        raise ValueError('invalid sentence span')
    left, right = span['start'], span['end']
    start, end = concern.get('start'), concern.get('end')
    if (any(type(value) is not int for value in (left, right, start, end))
            or not 0 <= left <= start < end <= right <= len(snapshot)):
        raise ValueError('invalid sentence span')
    original = concern.get('text')
    if not isinstance(original, str) or snapshot[start:end] != original:
        raise ValueError('concern does not match sentence source')
    clusters = _clusters(snapshot)
    boundaries = {0, len(snapshot), *(a for a, _ in clusters),
                  *(b for _, b in clusters)}
    if any(value not in boundaries for value in (left, right, start, end)):
        raise ValueError('sentence span splits a display cluster')
    uid = concern.get('concern_uid')
    values = concern.get('candidates')
    if (not isinstance(uid, str) or not uid.strip()
            or not isinstance(values, list) or not values
            or any(not isinstance(value, str) or not value.strip() for value in values)):
        raise ValueError('invalid candidate identity or values')
    if len(set(values)) != len(values) or original not in values:
        raise ValueError('candidate set must include the original exactly once')
    translations = concern.get('candidate_translations')
    bases = concern.get('candidate_basis')
    for mapping in (translations, bases):
        if (not isinstance(mapping, dict) or set(mapping) != set(values)
                or any(not isinstance(mapping[value], str) or not mapping[value].strip()
                       for value in values)):
            raise ValueError('incomplete candidate translation or basis mapping')
    sentence = snapshot[left:right]
    candidates = []
    for value in values:
        text = sentence[:start-left] + value + sentence[end-left:]
        meaning, basis = translations[value], bases[value]
        # No ordinal/offset/token in the identity: unrelated decisions or a
        # candidate reorder do not collapse unchanged evidence. Changed semantic
        # content receives a different identity rather than inheriting old state.
        payload = [uid, value, sentence, text, meaning, basis]
        key = hashlib.sha256(json.dumps(payload, ensure_ascii=False,
                                       separators=(',', ':')).encode()).hexdigest()
        candidates.append({'value': value, 'text': text, 'meaning': meaning,
                           'basis': basis, 'key': key})
    return {'sentence': sentence, 'candidates': candidates}


CONTEXT_ALGORITHM = 'context-v1-conservative-clusters'


def _clusters(text):
    """Conservative display boundaries; keep marks, joiners and flag pairs intact.

    This is deliberately not a complete Unicode segmentation implementation.
    Unknown combining sequences stay with their base rather than being cut.
    Coordinates returned here remain Python code-point offsets.
    """
    import unicodedata
    result = []
    regional_run = 0
    for index, char in enumerate(text):
        code = ord(char)
        regional = 0x1F1E6 <= code <= 0x1F1FF
        previous = text[index - 1] if index else ''
        attached = bool(index and (
            unicodedata.category(char).startswith('M') or char == '\u200d'
            or previous == '\u200d' or 0x1F3FB <= code <= 0x1F3FF
            or 0xE0020 <= code <= 0xE007F
            or (regional and regional_run % 2 == 1)
            or (previous == '\r' and char == '\n')
            or 'VIRAMA' in unicodedata.name(previous, '')
        ))
        if attached:
            result[-1] = (result[-1][0], index + 1)
        else:
            result.append((index, index + 1))
        regional_run = regional_run + 1 if regional else 0
    return result


def _allowances(left, right, budget):
    a, b = min(left, budget), min(right, budget)
    if a < budget:
        b = min(right, 2 * budget - a)
    if b < budget:
        a = min(left, 2 * budget - b)
    return a, b


def context_window(snapshot, concern, *, full_context_ref=''):
    """Shared full marked context for Local Web and Feishu; never edits choices."""
    import hashlib
    start, end = concern['start'], concern['end']
    if not isinstance(start, int) or not isinstance(end, int) or not 0 <= start <= end <= len(snapshot):
        raise ValueError('invalid concern span')
    if snapshot[start:end] != concern['text']:
        raise ValueError('concern does not match source span')
    clusters = _clusters(snapshot)
    boundaries = {0, len(snapshot), *(a for a, _ in clusters), *(b for _, b in clusters)}
    if start not in boundaries or end not in boundaries:
        raise ValueError('concern span splits a display cluster')
    before, after = snapshot[:start], snapshot[end:]
    # Select language from the immediate context, never whole-document totals.
    nearby = before[-96:] + snapshot[start:end][:96] + after[:96]
    english = bool(re.search(r'[A-Za-z]', nearby)) and not re.search(r'[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]', nearby)
    if english:
        pattern = r"[^\W_]+(?:['’][^\W_]+)*"
        left_words = list(re.finditer(pattern, before))
        right_words = list(re.finditer(pattern, after))
        left_n, right_n = _allowances(len(left_words), len(right_words), 24)
        left = left_words[-left_n].start() if left_n and left_n < len(left_words) else 0
        right = end + right_words[right_n - 1].end() if right_n and right_n < len(right_words) else len(snapshot)
        # A word's final combining marks belong to that word.
        for a, b in clusters:
            if a < left < b:
                left = a
            if a < right < b:
                right = b
    else:
        left_clusters = [(a, b) for a, b in clusters if b <= start]
        right_clusters = [(a, b) for a, b in clusters if a >= end]
        left_n, right_n = _allowances(len(left_clusters), len(right_clusters), 48)
        left = left_clusters[-left_n][0] if left_n else start
        right = right_clusters[right_n - 1][1] if right_n else end
    return {
        'source_hash': hashlib.sha256(snapshot.encode('utf-8')).hexdigest(),
        'concern_uid': concern.get('concern_uid', ''),
        'before': snapshot[left:start], 'marked': snapshot[start:end],
        'after': snapshot[end:right], 'span': [start, end],
        'omitted_before': left > 0, 'omitted_after': right < len(snapshot),
        'full_context_ref': full_context_ref, 'algorithm': CONTEXT_ALGORITHM,
        'unit': 'word' if english else 'display_cluster',
    }
