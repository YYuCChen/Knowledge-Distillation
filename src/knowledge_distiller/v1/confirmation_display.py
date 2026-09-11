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
