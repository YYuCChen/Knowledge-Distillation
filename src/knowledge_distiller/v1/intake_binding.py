"""Pure local input descriptors, not Store evidence or a completeness verdict.

Only a supplied, already accepted SubmittedSource is described. Original bytes
stay with that source; labels are its frozen first label, never a filesystem
location. No parsing, network, identity inference or legacy rebinding occurs.
"""
from dataclasses import dataclass
import hashlib
import json
from pathlib import PurePosixPath

from .file_sources import SubmittedSource, _digest, _direct_key, _direct_whitespace


SOURCE_CONTRACT = 'intake-source-v1'
RELATION_CONTRACT = 'intake-relation-v1'
ENVELOPE_CONTRACT = 'intake-binding-proposal-v1'
_KINDS = frozenset({'markdown', 'pdf', 'epub', 'direct_text'})


class IntakeBindingError(ValueError):
    def __init__(self, code='intake_binding_invalid'):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class LocalIntakeBinding:
    source_json: str
    relation_json: str
    source_binding_sha256: str
    relation_binding_sha256: str
    envelope_json: str


def _string(value):
    if not isinstance(value, str) or '\x00' in value:
        raise IntakeBindingError()
    value.encode('utf-8', errors='strict')


def _json_value(value):
    if value is None or type(value) in (bool, int):
        return
    if isinstance(value, str):
        _string(value)
    elif type(value) is list:
        for item in value:
            _json_value(item)
    elif type(value) is dict:
        for key, item in value.items():
            _string(key)
            _json_value(item)
    else:
        # Floats (finite or not), tuples, bytes and coercible objects are not
        # part of this exact JSON contract.
        raise IntakeBindingError()


def _canonical(value):
    _json_value(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def _sha(value):
    return hashlib.sha256(_canonical(value).encode('utf-8')).hexdigest()


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise IntakeBindingError()
        result[key] = value
    return result


def _reject_number(value):
    raise IntakeBindingError()


def _load(value):
    if type(value) is bytes:
        value = value.decode('utf-8', errors='strict')
    _string(value)
    result = json.loads(value, object_pairs_hook=_pairs,
                        parse_float=_reject_number, parse_constant=_reject_number)
    _json_value(result)
    return result


def _input(source):
    if not isinstance(source, SubmittedSource):
        raise IntakeBindingError()
    if source.source_kind not in _KINDS:
        raise IntakeBindingError('intake_binding_unsupported')
    if type(source.content) is not bytes:
        raise IntakeBindingError()
    _string(source.source_kind)
    _string(source.source_key)
    _string(source.label)
    if not source.label or any(v in source.label for v in ('/', '\\')):
        raise IntakeBindingError()
    if type(source.metadata) is not dict:
        raise IntakeBindingError()
    _json_value(source.metadata)
    if source.source_kind == 'direct_text':
        if (source.label != '直接文本' or set(source.metadata) != {'user_declared'}
                or type(source.metadata['user_declared']) is not dict):
            raise IntakeBindingError()
        declarations = source.metadata['user_declared']
        if any(k not in {'author', 'origin', 'original_title'} or not isinstance(v, str)
               for k, v in declarations.items()):
            raise IntakeBindingError()
        text = source.content.decode('utf-8', errors='strict')
        _string(text)
        if not text or all(_direct_whitespace(c) for c in text):
            raise IntakeBindingError()
        expected_key = _direct_key(source.content, declarations)
    else:
        suffix_kind = {'.md': 'markdown', '.markdown': 'markdown',
                       '.pdf': 'pdf', '.epub': 'epub'}
        if source.metadata or suffix_kind.get(PurePosixPath(source.label).suffix.lower()) != source.source_kind:
            raise IntakeBindingError()
        # Binary PDF/EPUB (and other unparsed file bytes) are only hashed, not
        # decoded/qualified. A descriptor cannot certify their readability.
        expected_key = _digest(source.content)
    if source.source_key != expected_key:
        raise IntakeBindingError('intake_binding_mismatch')
    return {'type': 'submitted_bytes', 'input_kind': source.source_kind,
            'input_key': source.source_key, 'input_label': source.label,
            'metadata': json.loads(_canonical(source.metadata)),
            'content_sha256': _digest(source.content),
            'content_byte_count': len(source.content)}


def _build(source):
    input_value = _input(source)
    source_value = {'contract': SOURCE_CONTRACT,
        'delivery': {'channel': 'local_web', 'receipt': None},
        'input': input_value, 'owner_selector': None}
    relation_value = {'contract': RELATION_CONTRACT,
        'intent': {'kind': 'standalone', 'identity': 'unresolved', 'annotation_target': None},
        'range': {'mode': 'single',
                  'selectors': [{'ordinal': 0, 'selector_sha256': _sha(input_value)}]},
        'adjacency': {'status': 'not_applicable', 'rows': []},
        'resolution': {'status': 'unresolved', 'raw_ids': []}}
    source_hash, relation_hash = _sha(source_value), _sha(relation_value)
    envelope = {'contract': ENVELOPE_CONTRACT, 'source': source_value,
                'relation': relation_value, 'source_binding_sha256': source_hash,
                'relation_binding_sha256': relation_hash}
    return LocalIntakeBinding(_canonical(source_value), _canonical(relation_value),
                             source_hash, relation_hash, _canonical(envelope))


def build_local_binding(source: SubmittedSource) -> LocalIntakeBinding:
    """Describe exact accepted local bytes; no persistence or green state."""
    try:
        return _build(source)
    except (UnicodeError, TypeError, RecursionError, OverflowError):
        raise IntakeBindingError() from None


def validate_local_binding(source: SubmittedSource, envelope_json: str | bytes) -> LocalIntakeBinding:
    """Rebuild from real source and compare all persisted fields and two hashes.

    JSON whitespace is accepted, but normalized output is canonical. Trailing
    data, duplicate keys, non-JSON types and unknown fields are rejected. No
    historical/legacy envelope is upgraded or rebound by this function.
    """
    try:
        value = _load(envelope_json)
        if type(value) is not dict:
            raise IntakeBindingError()
        if (value.get('contract') != ENVELOPE_CONTRACT
                or not isinstance(value.get('source'), dict)
                or not isinstance(value.get('relation'), dict)
                or value['source'].get('contract') != SOURCE_CONTRACT
                or value['relation'].get('contract') != RELATION_CONTRACT):
            raise IntakeBindingError('intake_binding_contract_unsupported')
        expected = _build(source)
        # Compare exact canonical serialization, not Python equality (False==0).
        if _canonical(value) != expected.envelope_json:
            raise IntakeBindingError('intake_binding_mismatch')
        return expected
    except (UnicodeError, TypeError, RecursionError, OverflowError, json.JSONDecodeError):
        raise IntakeBindingError() from None
