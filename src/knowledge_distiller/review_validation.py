"""Source-bound edit acceptance. Model output is a proposal, never the source.

Text-only evidence cannot establish arbitrary semantic equivalence. Each AI
correction retains its concrete assessment; form alone is not a truth claim.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from difflib import SequenceMatcher

RULE_VERSION = 'source-operations-v1.2-3'
# Quotes may be typographic variants; operators, signs, numbers and separators
# are deliberately not normalized. Whitespace is meaningful in code and units.
_QUOTES = str.maketrans({'“': '"', '”': '"', '‘': "'", '’': "'"})


def evidence_present(source, evidence):
    return bool(evidence) and evidence.translate(_QUOTES) in source.translate(_QUOTES)


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
    accepted = []
    for start, end, index, row in sorted(located):
        if index in conflicted:
            report(index, 'source_span', 'overlap'); continue
        # Final positions are constructed below from source operations. A bad
        # model-supplied full candidate cannot invalidate an unrelated good edit.
        evidence = row.get('evidence')
        spans = row.get('evidence_spans', [])
        quotes = row.get('evidence_quotes')
        if quotes is not None:
            if not isinstance(quotes, list) or not quotes or any(
                    not isinstance(q, str) or not q.strip() or q not in source for q in quotes):
                report(index, 'evidence_quotes', 'invalid_evidence_quote'); continue
            evidence = quotes
        elif spans:
            if not isinstance(spans,list) or any(not isinstance(span,dict)
                    or type(span.get('start')) is not int or type(span.get('end')) is not int
                    or not 0 <= span['start'] < span['end'] <= len(source)
                    or not isinstance(span.get('text'),str)
                    or source[span['start']:span['end']] != span['text'] for span in spans):
                report(index, 'evidence_spans', 'invalid_evidence_span'); continue
            evidence = [span['text'] for span in spans]
        elif not isinstance(evidence,str) or not evidence_present(source, evidence):
            report(index, 'evidence', 'evidence_not_contiguous'); continue
        from .semantic_support import assess_correction
        failure = assess_correction(source, row['original_text'], row['replacement'],
            evidence if isinstance(evidence, list) else [evidence], row.get('assessment'))
        if row['meaning_may_change'] or failure:
            report(index, failure or 'meaning_may_change', 'insufficient_source_support'); continue
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
    for start, end, index, row in located:
        if row['meaning_may_change']:
            shift = sum(len(r['replacement'])-(b-a) for a,b,_,r in accepted if b <= start)
            concerns.append(ReviewConcern(start+shift,end+shift,row['original_text'],row['reason'],True,
                (row['replacement'],)))
    blocks = SequenceMatcher(None, proposed, result, autojunk=False).get_matching_blocks()
    for index, issue in enumerate(payload['issues']):
        if isinstance(issue, dict) and issue.get('kind') in {'content_relevance', 'editorial', 'opinion'}:
            continue
        concern = _parse_concern(issue, proposed)
        if concern is None:
            report(index, 'issue', 'invalid_issue')
            diagnostics[-1]['issue'] = issue
            # Do not discard a potentially critical unresolved source question.
            # Invalid protocol requires review recovery, not a fabricated source concern.
            continue
        mapped = next((b + concern.start_offset-a for a,b,n in blocks
                       if a <= concern.start_offset and concern.end_offset <= a+n), None)
        if mapped is None:
            report(index, 'issue', 'issue_mapping_failed')
            diagnostics[-1]['issue'] = issue
        else:
            concerns.append(replace(concern, start_offset=mapped, end_offset=mapped+len(concern.text)))
    concerns = merge_local_concerns(result, concerns)
    return FaithfulReviewCandidate(result, tuple(concerns), tuple(repairs), tuple(diagnostics))


def merge_local_concerns(text, concerns):
    """Only connected overlapping spans share a local question, never the page."""
    result = []
    for concern in sorted(concerns, key=lambda c: (c.start_offset, c.end_offset)):
        if result and result[-1].end_offset > concern.start_offset:
            prior = result.pop()
            end = max(prior.end_offset, concern.end_offset)
            result.append(replace(prior, end_offset=end, text=text[prior.start_offset:end],
                reason='；'.join(dict.fromkeys((prior.reason, concern.reason))),
                meaning_may_change=prior.meaning_may_change or concern.meaning_may_change,
                candidate_readings=(), candidate_explanations=()))
        else:
            result.append(concern)
    return result


def _map_span(start, end, repairs, *, inverse=False):
    """Map exact operation intervals; do not infer identity from a fuzzy diff."""
    shift_start = shift_end = 0
    mapped_start = mapped_end = None
    for repair in repairs:
        a, b, c, d = (repair['source_start'], repair['source_end'], repair['start'], repair['end'])
        if inverse:
            a, b, c, d = c, d, a, b
        if b <= start:
            shift_start += (d-c)-(b-a)
        elif a <= start < b:
            mapped_start = c
        if b <= end:
            shift_end += (d-c)-(b-a)
        elif a < end < b:
            mapped_end = d
    return (start+shift_start if mapped_start is None else mapped_start,
            end+shift_end if mapped_end is None else mapped_end)


def issue_identity(source, start, end):
    return hashlib.sha256(json.dumps([hashlib.sha256(source.encode()).hexdigest(), start, end]).encode()).hexdigest()


def retry_context(source, candidate):
    rows = list(candidate.diagnostics)
    for concern in candidate.concerns:
        start, end = _map_span(concern.start_offset, concern.end_offset, candidate.repairs, inverse=True)
        rows.append({'field':'issue', 'code':'unresolved_issue', 'issue_id':issue_identity(source,start,end),
                     'source_text':source[start:end], 'reason':concern.reason,
                     'source_start':start, 'source_end':end})
    return rows


def merge_retry(source, first, revised, raw):
    """Omission is not resolution; explicit source-backed dismissal is retained."""
    try:
        resolutions = json.loads(raw).get('resolutions', [])
    except (ValueError, TypeError, AttributeError):
        resolutions = []
    if not isinstance(resolutions, list):
        resolutions = []
    pending, events = [], []
    for concern in first.concerns:
        start, end = _map_span(concern.start_offset, concern.end_offset, first.repairs, inverse=True)
        identity = issue_identity(source, start, end)
        matches = [row for row in resolutions if isinstance(row, dict) and row.get('issue_id') == identity]
        row = matches[0] if len(matches) == 1 else {}
        quotes = row.get('evidence_quotes')
        if (row.get('action') in {'dismissed', 'resolved'} and row.get('original_issue_possible') is False
                and row.get('retained_reading') == source[start:end]
                and all(isinstance(row.get(k), str) and row[k].strip() for k in ('reason','question_analysis'))
                and isinstance(quotes, list) and quotes
                and all(isinstance(q, str) and q.strip() and q in source for q in quotes)
                and any(source[start:end] in q for q in quotes)):
            events.append({'field':'issue','code':'issue_resolution','issue_id':identity,
                           'source_start':start,'source_end':end, **row})
        else:
            pending.append((start,end,concern))
    # An unresolved old question cannot be edited away by a format retry.
    repairs = [r for r in revised.repairs if not any(
        a < r['source_end'] and r['source_start'] < b for a,b,_ in pending)]
    text = source
    for r in reversed(repairs):
        text = text[:r['source_start']] + r['replacement'] + text[r['source_end']:]
    shift = 0
    for i,r in enumerate(repairs):
        start = r['source_start'] + shift
        repairs[i] = {**r,'start':start,'end':start+len(r['replacement'])}
        shift += len(r['replacement']) - (r['source_end']-r['source_start'])
    concerns = []
    for concern in revised.concerns:
        a,b = _map_span(concern.start_offset, concern.end_offset, revised.repairs, inverse=True)
        start,end = _map_span(a,b,repairs)
        concerns.append(replace(concern,start_offset=start,end_offset=end,text=text[start:end]))
    for a,b,concern in pending:
        start,end = _map_span(a,b,repairs)
        if not any(c.start_offset == start and c.end_offset == end and c.reason == concern.reason for c in concerns):
            concerns.append(replace(concern,start_offset=start,end_offset=end,text=text[start:end]))
    diagnostics = list(revised.diagnostics) + events
    for diagnostic in first.diagnostics:
        issue = diagnostic.get('issue')
        # Empty protocol placeholders may be corrected. A substantive question
        # remains incomplete until its location is recovered, not merely omitted.
        if (isinstance(issue, dict) and isinstance(issue.get('reason'), str) and issue['reason'].strip()
                and isinstance(issue.get('issue_text'), str) and issue['issue_text'].strip()
                and not any(c.text == issue['issue_text'] and c.reason == issue['reason'] for c in concerns)):
            diagnostics.append(diagnostic)
    return replace(revised,text=text,repairs=tuple(repairs),
                   concerns=tuple(merge_local_concerns(text,concerns)),diagnostics=tuple(diagnostics))
