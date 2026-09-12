"""Source-bound edit acceptance. Model output is a proposal, never the source.

Text-only evidence cannot establish arbitrary semantic equivalence. Automatic
repairs are therefore limited to conservative spelling variants corroborated by
another occurrence in this source. Other edits retain their original literal.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from difflib import SequenceMatcher

RULE_VERSION = 'source-operations-v1.2-1'
# Quotes may be typographic variants; operators, signs, numbers and separators
# are deliberately not normalized. Whitespace is meaningful in code and units.
_QUOTES = str.maketrans({'“': '"', '”': '"', '‘': "'", '’': "'"})
_PROTECTED = re.compile(r'\d|[+−=<>*/%]|\b(?:not|no|never|unless|without|if|because|must|may)\b|[不无未否非因若]|(?<!\w)-(?!\w)', re.I)


def evidence_present(source, evidence):
    return bool(evidence) and evidence.translate(_QUOTES) in source.translate(_QUOTES)


def lexical_repair_supported(source, original, replacement, evidence):
    """This accepts a narrow class, not a claim of semantic verification."""
    if original == replacement:
        return True
    if _PROTECTED.search(original) or _PROTECTED.search(replacement):
        return False
    # Adding/removing words to repair grammar is not transcription evidence.
    a, b = re.findall(r'\w+', original), re.findall(r'\w+', replacement)
    if len(a) != len(b) or not a:
        return False
    parts = evidence if isinstance(evidence, list) else [evidence]
    if not parts or not all(evidence_present(source, part) for part in parts):
        return False
    if not any(replacement in part for part in parts):
        return False
    # Evidence must include the exact original span as well as corroboration;
    # an unrelated sentence cannot license a change elsewhere.
    if not any(original in part for part in parts):
        return False
    return (SequenceMatcher(None, original.casefold(), replacement.casefold(), autojunk=False).ratio() >= .8
            and len(original) >= 4 and len(replacement) >= 4)


def validate_response(source, raw):
    from .faithful_review import FaithfulReviewCandidate, ReviewConcern, _parse_concern, _exact_occurrence_starts
    version = hashlib.sha256(source.encode()).hexdigest()
    diagnostics = []

    def report(index, field, code):
        diagnostics.append({'operation': index, 'field': field, 'code': code,
                            'action': 'retained_original', 'source_sha256': version,
                            'rule_version': RULE_VERSION})

    try:
        payload = json.loads(raw)
        if (not isinstance(payload, dict) or not isinstance(payload.get('candidate_text'), str)
                or not payload['candidate_text'].strip() or not isinstance(payload.get('issues'), list)
                or not isinstance(payload.get('repairs', []), list)):
            raise ValueError
    except (TypeError, ValueError):
        report(None, 'response', 'invalid_json_or_shape')
        return FaithfulReviewCandidate(source, (), (), tuple(diagnostics))

    proposed = payload['candidate_text']
    located = []
    for index, row in enumerate(payload.get('repairs', [])):
        if (not isinstance(row, dict) or any(not isinstance(row.get(k), str) or not row[k].strip()
                for k in ('original_text', 'replacement', 'reason'))
                or any(type(row.get(k)) is not int or row[k] < 0 for k in ('source_occurrence', 'occurrence'))
                or type(row.get('meaning_may_change')) is not bool):
            report(index, 'repair', 'invalid_fields'); continue
        starts = _exact_occurrence_starts(source, row['original_text'])
        if row['source_occurrence'] >= len(starts):
            report(index, 'source_occurrence', 'source_not_found'); continue
        start = starts[row['source_occurrence']]; end = start + len(row['original_text'])
        if (('source_start' in row and row['source_start'] != start)
                or ('source_end' in row and row['source_end'] != end)
                or ('source_sha256' in row and row['source_sha256'] != version)):
            report(index, 'source_span', 'source_version_or_position_mismatch'); continue
        located.append((start, end, index, row))

    conflicted = set()
    for a, first in enumerate(located):
        for second in located[a+1:]:
            if first[0] < second[1] and second[0] < first[1]:
                conflicted.update((first[2], second[2]))
    declared = source
    declared_positions = {}; displacement = 0
    for start, end, index, row in sorted(located):
        if index not in conflicted:
            declared_positions[index] = start + displacement
            displacement += len(row['replacement']) - (end-start)
    for start, end, index, row in sorted(located, reverse=True):
        if index not in conflicted:
            declared = declared[:start] + row['replacement'] + declared[end:]
    accepted = []
    for start, end, index, row in sorted(located):
        if index in conflicted:
            report(index, 'source_span', 'overlap'); continue
        # Bind both sides to the same diff, not independent occurrence counts.
        target = _exact_occurrence_starts(proposed, row['replacement'])
        if row['occurrence'] >= len(target):
            report(index, 'occurrence', 'replacement_not_found'); continue
        left = target[row['occurrence']]
        if declared != proposed or declared_positions.get(index) != left:
            report(index, 'occurrence', 'replacement_position_mismatch'); continue
        evidence = row.get('evidence')
        spans = row.get('evidence_spans', [])
        if spans:
            if not isinstance(spans,list) or any(not isinstance(span,dict)
                    or type(span.get('start')) is not int or type(span.get('end')) is not int
                    or not 0 <= span['start'] < span['end'] <= len(source)
                    or not isinstance(span.get('text'),str)
                    or source[span['start']:span['end']] != span['text'] for span in spans):
                report(index, 'evidence_spans', 'invalid_evidence_span'); continue
            evidence = [span['text'] for span in spans]
        elif not isinstance(evidence,str) or not evidence_present(source, evidence):
            report(index, 'evidence', 'evidence_not_contiguous'); continue
        if row['meaning_may_change'] or not lexical_repair_supported(source, row['original_text'], row['replacement'], evidence):
            report(index, 'replacement', 'insufficient_source_support'); continue
        accepted.append((start, end, index, row))

    result = source
    for start, end, index, row in reversed(accepted):
        result = result[:start] + row['replacement'] + result[end:]
    if result != proposed:
        report(None, 'candidate_text', 'unaccepted_candidate_changes')
    repairs = []; shift = 0
    for start, end, index, row in accepted:
        repairs.append({**row, 'source_start': start, 'source_end': end,
                        'source_sha256': version, 'rule_version': RULE_VERSION,
                        'start': start+shift, 'end': start+shift+len(row['replacement']),
                        'text': row['replacement'], 'by': 'ai', 'status': 'repaired'})
        shift += len(row['replacement']) - (end-start)

    concerns = []
    blocks = SequenceMatcher(None, proposed, result, autojunk=False).get_matching_blocks()
    for index, issue in enumerate(payload['issues']):
        if isinstance(issue, dict) and issue.get('kind') in {'content_relevance', 'editorial', 'opinion'}:
            continue
        concern = _parse_concern(issue, proposed)
        if concern is None:
            report(index, 'issue', 'invalid_issue')
            # Do not discard a potentially critical unresolved source question.
            concerns.append(ReviewConcern(0, len(result), result, '疑点位置无效，原文已保留，请核对原来源。', True))
            continue
        mapped = next((b + concern.start_offset-a for a,b,n in blocks
                       if a <= concern.start_offset and concern.end_offset <= a+n), None)
        if mapped is None:
            report(index, 'issue', 'issue_mapping_failed')
            if concern.meaning_may_change:
                concerns.append(ReviewConcern(0, len(result), result, concern.reason, True))
        else:
            concerns.append(replace(concern, start_offset=mapped, end_offset=mapped+len(concern.text)))
    # Conflicting questions become one source question; no overlapping edits.
    concerns.sort(key=lambda c: c.start_offset)
    if any(a.end_offset > b.start_offset for a,b in zip(concerns, concerns[1:])):
        concerns = [ReviewConcern(0, len(result), result, '多个疑点位置冲突，原文已保留，请核对原来源。', True)]
    return FaithfulReviewCandidate(result, tuple(concerns), tuple(repairs), tuple(diagnostics))
