"""Typed candidates and bounded POSIX transport; no acceptance or model client.

Source proof and exact token measurement are application-owned capabilities.
There is deliberately no default proof, tokenizer estimate, or source-complete
boolean. The caller must still verify staging changes and publication.
"""
from __future__ import annotations

import codecs
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import stat
import time

from .wiki_support import CATEGORIES as _SUPPORT_CATEGORIES, FIELDS as _SUPPORT_FIELDS

CONTRACT = 'r08-wiki-outcomes-v1'
CHECK_CONTRACT = 'r08-no-knowledge-check-v1'
SUPPORT_CONTRACT = 'r14-typed-support-v1'
APPLICATION_UTF8_POLICY = 'application_utf8_v1'
EXACT_TOKEN_POLICY = 'exact_tokens_v1'
STDOUT_LIMIT = 8 * 1024 * 1024
LINE_LIMIT = 256 * 1024
STDERR_LIMIT = 256 * 1024
FINAL_LIMIT = 1024 * 1024
INPUT_LIMIT = 32 * 1024 * 1024
CHECK_TIMEOUT = 300
GENERATION_TIMEOUT = 900
DIMENSIONS = ('definition', 'method', 'reference_lead', 'relations')
SHA = re.compile(r'[0-9a-f]{64}\Z')
ID = re.compile(r'[0-9a-f]{32}\Z')
RAW_ID = re.compile(r'R-\d{8}-\d{4}\Z')


class TypedError(ValueError):
    """Only application-owned fixed codes, never source or exception text."""

    def __init__(self, code):
        allowed = {'typed_input_invalid', 'typed_input_limit', 'typed_output_invalid',
                   'typed_protocol_invalid', 'typed_binding_invalid', 'typed_coverage_invalid',
                   'typed_path_invalid', 'source_proof_unavailable', 'input_budget_unavailable',
                   'group_over_budget', 'agent_failed', 'interrupted', 'runner_output_limit',
                   'runner_timeout', 'config_required', 'model_unavailable',
                   'runner_unavailable', 'vault_busy', 'input_policy_invalid',
                   'context_limit_observed', 'context_compaction_observed'}
        super().__init__(code if type(code) is str and code in allowed else 'typed_input_invalid')


@dataclass(frozen=True)
class TypedRunnerResult:
    error_code: str | None
    usage: tuple[tuple[str, int], ...] = ()
    final_bytes: bytes | None = None
    final_sha256: str | None = None
    input_binding: InputBinding | None = None

    @property
    def succeeded(self):
        # This is structural transport success, NEVER normal/accepted/published.
        return self.error_code is None and self.final_bytes is not None


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def digest(value):
    return hashlib.sha256(value).hexdigest()


def _object(value, keys):
    if type(value) is not dict or set(value) != set(keys):
        raise TypedError('typed_protocol_invalid')


def _hash(value):
    if type(value) is not str or SHA.fullmatch(value) is None:
        raise TypedError('typed_binding_invalid')


def _text(value):
    if type(value) is not str or not value.strip() or len(value) > 2000:
        raise TypedError('typed_protocol_invalid')


def _reason(value):
    from .wiki_outcomes import _specific_reason
    _text(value)
    if not _specific_reason(value):
        raise TypedError('typed_protocol_invalid')


def strict_json(content):
    if type(content) is not bytes or not content or len(content) > FINAL_LIMIT:
        raise TypedError('typed_output_invalid')
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise TypedError('typed_protocol_invalid')
            result[key] = value
        return result
    def invalid(_value):
        raise TypedError('typed_protocol_invalid')
    try:
        return json.loads(content.decode('utf-8'), object_pairs_hook=pairs,
                          parse_constant=invalid, parse_float=invalid)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise TypedError('typed_protocol_invalid') from error


def validate_binding(binding):
    _object(binding, ('task_id', 'attempt_id', 'batch_no', 'boundary_sha256', 'input_sha256'))
    if (any(type(binding[k]) is not str or ID.fullmatch(binding[k]) is None
            for k in ('task_id', 'attempt_id')) or type(binding['batch_no']) is not int
            or binding['batch_no'] < 1):
        raise TypedError('typed_binding_invalid')
    for key in ('boundary_sha256', 'input_sha256'):
        _hash(binding[key])


def _schema_object(properties):
    return dict(type='object', additionalProperties=False,
                required=list(properties), properties=properties)


def _string(values=None):
    return dict(type='string', enum=list(values)) if values else dict(type='string', minLength=1, maxLength=2000)


_HASH_SCHEMA = dict(type='string', pattern='^[0-9a-f]{64}$')
_BINDING_SCHEMA = _schema_object({
    'task_id': dict(type='string', pattern='^[0-9a-f]{32}$'),
    'attempt_id': dict(type='string', pattern='^[0-9a-f]{32}$'),
    'batch_no': dict(type='integer', minimum=1),
    'boundary_sha256': _HASH_SCHEMA, 'input_sha256': _HASH_SCHEMA})
_RAW_SCHEMA = dict(type='string', pattern='^R-[0-9]{8}-[0-9]{4}$')
_DOCUMENT_SCHEMA = _schema_object({'path': _string(), 'sha256': _HASH_SCHEMA})
PROPOSAL_SCHEMA = _schema_object({
    'contract': _string((CONTRACT,)), 'schema_revision': dict(type='integer', enum=[1]),
    'binding': _BINDING_SCHEMA,
    'outcomes': dict(type='array', minItems=1, items=_schema_object({
        'raw_id': _RAW_SCHEMA, 'content_sha256': _HASH_SCHEMA,
        'ordinal': dict(type='integer', minimum=1),
        'status': _string(('processed_with_knowledge', 'processed_no_knowledge', 'unknown')),
        'reason_code': _string(('knowledge_proposed', 'non_substantive', 'no_distinct_claim',
                               'support_only', 'context_incomplete', 'classification_uncertain')),
        'reason': _string(), 'documents': dict(type='array', items=_DOCUMENT_SCHEMA)}))})
_EVIDENCE_SCHEMA = _schema_object({
    'raw_id': _RAW_SCHEMA, 'content_sha256': _HASH_SCHEMA,
    'start': dict(type='integer', minimum=0), 'end': dict(type='integer', minimum=1), 'text': _string()})
CHECK_SCHEMA = _schema_object({
    'contract': _string((CHECK_CONTRACT,)), 'schema_revision': dict(type='integer', enum=[1]),
    'binding': _BINDING_SCHEMA, 'proposal_sha256': _HASH_SCHEMA, 'changes_sha256': _HASH_SCHEMA,
    'reviews': dict(type='array', minItems=1, items=_schema_object({
        'raw_id': _RAW_SCHEMA, 'content_sha256': _HASH_SCHEMA,
        'status': _string(('verified', 'unsupported', 'unknown')), 'reason': _string(),
        'source_check': _schema_object({'status': _string(('complete', 'incomplete', 'unknown')),
                                      'reason': _string(), 'evidence_sha256': _HASH_SCHEMA}),
        'dimensions': dict(type='array', minItems=4, maxItems=4, items=_schema_object({
            'dimension': _string(DIMENSIONS), 'status': _string(('absent', 'present', 'support_only', 'unknown')),
            'reason': _string(), 'evidence': dict(type='array', items=_EVIDENCE_SCHEMA),
            'related_raw_ids': dict(type='array', items=_RAW_SCHEMA)}))}))})


def parse_proposal(content, binding, rows):
    validate_binding(binding)
    value = strict_json(content)
    _object(value, ('contract', 'schema_revision', 'binding', 'outcomes'))
    if (value['contract'] != CONTRACT or type(value['schema_revision']) is not int
            or value['schema_revision'] != 1 or value['binding'] != binding):
        raise TypedError('typed_binding_invalid')
    validate_binding(value['binding'])
    outcomes = value['outcomes']
    if type(outcomes) is not list or len(outcomes) != len(rows) or not rows:
        raise TypedError('typed_coverage_invalid')
    for outcome, (raw, _content) in zip(outcomes, rows):
        _object(outcome, ('raw_id', 'content_sha256', 'ordinal', 'status', 'reason_code', 'reason', 'documents'))
        if (outcome['raw_id'] != raw.raw_id or outcome['content_sha256'] != raw.content_sha256
                or type(outcome['ordinal']) is not int or outcome['ordinal'] != raw.ordinal):
            raise TypedError('typed_coverage_invalid')
        status, code = outcome['status'], outcome['reason_code']
        allowed = {'processed_with_knowledge': {'knowledge_proposed'},
                   'processed_no_knowledge': {'non_substantive', 'no_distinct_claim', 'support_only'},
                   'unknown': {'context_incomplete', 'classification_uncertain'}}
        if type(status) is not str or status not in allowed or type(code) is not str or code not in allowed[status]:
            raise TypedError('typed_protocol_invalid')
        _text(outcome['reason'])
        if status == 'processed_no_knowledge':
            from .wiki_outcomes import _specific_reason
            if not _specific_reason(outcome['reason']):
                raise TypedError('typed_protocol_invalid')
        documents = outcome['documents']
        if type(documents) is not list or (status == 'unknown') != (not documents):
            raise TypedError('typed_protocol_invalid')
        seen = set()
        for document in documents:
            _object(document, ('path', 'sha256'))
            path = document['path']
            if (type(path) is not str or not path.startswith('wiki/') or not path.endswith('.md')
                    or ':' in path or '\\' in path or any(p in {'', '.', '..'} for p in path.split('/'))
                    or path in seen):
                raise TypedError('typed_path_invalid')
            seen.add(path)
            _hash(document['sha256'])
    return value


def parse_check(content, binding, rows, *, proposal_sha256, changes_sha256, source_proof_sha256,
                full_context=None):
    """Validate the check, then resolve only legal misplaced exact quotations.

    Original model bytes stay untouched. An already exact range is retained;
    otherwise only a unique literal match in the named frozen raw can resolve it.
    """
    validate_binding(binding)
    value = deepcopy(strict_json(content))
    _object(value, ('contract', 'schema_revision', 'binding', 'proposal_sha256', 'changes_sha256', 'reviews'))
    if (value['contract'] != CHECK_CONTRACT or type(value['schema_revision']) is not int
            or value['schema_revision'] != 1 or value['binding'] != binding
            or value['proposal_sha256'] != proposal_sha256 or value['changes_sha256'] != changes_sha256):
        raise TypedError('typed_binding_invalid')
    validate_binding(value['binding'])
    for sha in (proposal_sha256, changes_sha256, source_proof_sha256):
        _hash(sha)
    reviews = value['reviews']
    if type(reviews) is not list or len(reviews) != len(rows) or not rows:
        raise TypedError('typed_coverage_invalid')
    evidence_rows = rows if full_context is None else full_context
    by_id = {raw.raw_id: (raw, data.decode('utf-8')) for raw, data in evidence_rows}
    if (len(by_id) != len(evidence_rows)
            or any(by_id.get(raw.raw_id) != (raw, data.decode('utf-8')) for raw, data in rows)):
        raise TypedError('typed_coverage_invalid')
    quotations = []
    for review, (raw, _data) in zip(reviews, rows):
        _object(review, ('raw_id', 'content_sha256', 'status', 'reason', 'source_check', 'dimensions'))
        if review['raw_id'] != raw.raw_id or review['content_sha256'] != raw.content_sha256:
            raise TypedError('typed_coverage_invalid')
        if type(review['status']) is not str or review['status'] not in {'verified', 'unsupported', 'unknown'}:
            raise TypedError('typed_protocol_invalid')
        _reason(review['reason'])
        source = review['source_check']
        _object(source, ('status', 'reason', 'evidence_sha256'))
        if (type(source['status']) is not str or source['status'] not in {'complete', 'incomplete', 'unknown'}
                or source['evidence_sha256'] != source_proof_sha256):
            raise TypedError('typed_binding_invalid')
        _reason(source['reason'])
        dimensions = review['dimensions']
        if type(dimensions) is not list or len(dimensions) != 4:
            raise TypedError('typed_protocol_invalid')
        seen = set()
        for dimension in dimensions:
            _object(dimension, ('dimension', 'status', 'reason', 'evidence', 'related_raw_ids'))
            name = dimension['dimension']
            if type(name) is not str or name not in DIMENSIONS or name in seen:
                raise TypedError('typed_protocol_invalid')
            seen.add(name)
            if type(dimension['status']) is not str or dimension['status'] not in {'absent', 'present', 'support_only', 'unknown'}:
                raise TypedError('typed_protocol_invalid')
            _reason(dimension['reason'])
            related = dimension['related_raw_ids']
            if (type(related) is not list or any(type(r) is not str or r not in by_id for r in related)
                    or len(set(related)) != len(related) or type(dimension['evidence']) is not list):
                raise TypedError('typed_coverage_invalid')
            for evidence in dimension['evidence']:
                _object(evidence, ('raw_id', 'content_sha256', 'start', 'end', 'text'))
                rid = evidence['raw_id']
                if type(rid) is not str or rid not in by_id:
                    raise TypedError('typed_coverage_invalid')
                evidence_raw, text = by_id[rid]
                start, end = evidence['start'], evidence['end']
                _text(evidence['text'])
                if (evidence['content_sha256'] != evidence_raw.content_sha256
                        or type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text)):
                    raise TypedError('typed_binding_invalid')
                quotations.append((evidence, text))
        # Still only a model candidate, but even its claim of verified cannot
        # contradict its own source or dimensions. No SourceFact is created.
        if review['status'] == 'verified' and (source['status'] != 'complete' or any(
                d['status'] in {'present', 'unknown'} for d in dimensions)):
            raise TypedError('typed_protocol_invalid')
    # All structural/source/binding checks precede normalization. Count a
    # second occurrence from start+1 so overlapping matches remain ambiguous.
    for evidence, text in quotations:
        quote = evidence['text']
        start, end = evidence['start'], evidence['end']
        if quote != text[start:end]:
            start = text.find(quote)
            if start < 0 or text.find(quote, start + 1) >= 0:
                raise TypedError('typed_binding_invalid')
            end = start + len(quote)
            evidence['start'], evidence['end'] = start, end
        if not 0 <= start < end <= len(text) or quote != text[start:end]:
            raise TypedError('typed_binding_invalid')
    return value


def _directory(path):
    path = Path(os.path.abspath(os.fspath(path)))
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        info = current.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise TypedError('typed_path_invalid')
    return path


def _file_key(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_uid,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def read_final(directory, name, *, limit=FINAL_LIMIT, private=True):
    """Bounded ordinary-file read; only frozen input reads may allow 0644."""
    return _read_regular(directory, name, limit=limit, private=private)


def _read_regular(directory, name, *, limit, private, digest_only=False):
    """Shared FD/identity contract; hash mode keeps only one bounded chunk."""
    if type(name) is not str or name in {'', '.', '..'} or any(c in name for c in '/\\:\x00'):
        raise TypedError('typed_path_invalid')
    root = _directory(directory)
    parents = [(p, (p.lstat().st_dev, p.lstat().st_ino, p.lstat().st_mode)) for p in (root, *root.parents)]
    path = root / name
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid()
            or (private and stat.S_IMODE(info.st_mode) & 0o077)):
        raise TypedError('typed_output_invalid')
    if limit is None:
        if not digest_only:
            raise TypedError('typed_input_invalid')
        limit = info.st_size  # Continuous growth cannot extend the read bound.
    if info.st_size > limit:
        raise TypedError('runner_output_limit')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        held = os.fstat(fd)
        if not stat.S_ISREG(held.st_mode) or _file_key(held) != _file_key(info):
            raise TypedError('typed_output_invalid')
        chunks, size, hasher = [], 0, hashlib.sha256()
        while True:
            chunk = os.read(fd, min(65536, limit + 1 - size))
            if not chunk:
                break
            if digest_only:
                hasher.update(chunk)
            else:
                chunks.append(chunk)
            size += len(chunk)
            if size > limit:
                raise TypedError('runner_output_limit')
        if (_file_key(os.fstat(fd)) != _file_key(info) or _file_key(path.lstat()) != _file_key(info)
                or any((p.lstat().st_dev, p.lstat().st_ino, p.lstat().st_mode) != key for p, key in parents)):
            raise TypedError('typed_output_invalid')
        return (hasher.hexdigest(), size) if digest_only else b''.join(chunks)
    finally:
        os.close(fd)


def validate_layout(snapshot, runtime_root, binding):
    validate_binding(binding)
    runtime = _directory(runtime_root)
    task_root = _directory(snapshot.task_root)
    expected = runtime / 'wiki-tasks' / binding['task_id'] / 'attempts' / binding['attempt_id']
    if (task_root != expected or snapshot.task_id != binding['task_id']
            or _directory(snapshot.workspace) != task_root / 'workspace'
            or _directory(snapshot.control) != task_root / 'control'):
        raise TypedError('typed_path_invalid')
    control = task_root / 'control'
    info = control.lstat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise TypedError('typed_path_invalid')
    return control


def artifact_directory(snapshot, runtime_root, binding, *, checker=False):
    control = validate_layout(snapshot, runtime_root, binding)
    # Unique application-owned invocation; never accept an old -o file.
    import tempfile
    return Path(tempfile.mkdtemp(prefix=('check-' if checker else 'proposal-'), dir=control))


def write_schema(directory, schema):
    path = directory / 'final.schema.json'
    content = encoded(schema)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())
    if read_final(directory, path.name) != content:
        raise TypedError('typed_output_invalid')
    return path


def require_budget(prompt, schema, measure, *, schema_absent=False):
    if type(schema_absent) is not bool or (schema_absent and schema is not None):
        raise TypedError('typed_input_invalid')
    if len(prompt) > INPUT_LIMIT:
        raise TypedError('typed_input_limit')
    if measure is None:
        raise TypedError('input_budget_unavailable')
    # Trusted program capability must measure the ENTIRE actual prompt/schema.
    # Return (tokenizer/version, exact input tokens, hard available tokens).
    version, count, budget = measure(prompt, b'' if schema_absent else encoded(schema))
    if (type(version) is not str or not version.strip() or type(count) is not int
            or type(budget) is not int or count < 0 or budget <= 0):
        raise TypedError('input_budget_unavailable')
    if count > budget:
        raise TypedError('group_over_budget')
    return version, count, budget


@dataclass(frozen=True)
class InputBinding:
    """Independent exact application-byte hashes; no self-hashing prompt.

    count/limit have the explicit unit. Bytes branch has no tokenizer identity.
    Tokens branch additionally keeps genuine capability measurement. This host
    transport binding does not add fields to the existing model envelopes.
    """
    policy: str
    unit: str
    count: int
    limit: int
    tokenizer_version: str | None
    prompt_bytes: int
    prompt_sha256: str
    schema_bytes: int
    schema_sha256: str
    max_application_input_bytes: int


def validate_input_policy(input_policy, measure, max_application_input_bytes):
    if (type(max_application_input_bytes) is not int
            or not 0 < max_application_input_bytes <= INPUT_LIMIT
            or (input_policy is not None and type(input_policy) is not str)
            or input_policy not in (None, APPLICATION_UTF8_POLICY, EXACT_TOKEN_POLICY)):
        raise TypedError('input_policy_invalid')
    if input_policy == APPLICATION_UTF8_POLICY:
        if measure is not None:
            raise TypedError('input_policy_invalid')
    elif max_application_input_bytes != INPUT_LIMIT:
        raise TypedError('input_policy_invalid')


def admit_input(prompt, schema, measure, *, input_policy=None,
                max_application_input_bytes=INPUT_LIMIT, schema_bytes=None, schema_absent=False):
    """Exact FULL stdin + independent encoded schema, before model probe/spawn.

    application_utf8_v1 proves only application bytes, not CLI internal tools,
    instructions, remote tokens/context or invisible server compaction.
    None policy retains the exact-measure requirement; never a byte fallback.
    """
    validate_input_policy(input_policy, measure, max_application_input_bytes)
    if type(schema_absent) is not bool or (schema_absent and schema is not None):
        raise TypedError('typed_input_invalid')
    if type(prompt) is not bytes:
        raise TypedError('typed_input_invalid')
    try:
        prompt.decode('utf-8', errors='strict')
        canonical = b'' if schema_absent else encoded(schema)
    except (UnicodeError, TypeError, ValueError):
        raise TypedError('typed_input_invalid') from None
    if schema_bytes is not None and (type(schema_bytes) is not bytes or schema_bytes != canonical):
        raise TypedError('typed_binding_invalid')
    total = len(prompt) + len(canonical)
    if total > max_application_input_bytes:
        raise TypedError('typed_input_limit')
    if input_policy == APPLICATION_UTF8_POLICY:
        policy, unit, count, limit, version = input_policy, 'utf8_bytes', total, max_application_input_bytes, None
    else:
        version, count, limit = require_budget(prompt, schema, measure, schema_absent=schema_absent)
        policy, unit = EXACT_TOKEN_POLICY, 'tokens'
    return InputBinding(policy, unit, count, limit, version, len(prompt), digest(prompt),
                        len(canonical), digest(canonical), max_application_input_bytes)


def _input_bytes(root, relative, limit):
    path = _input_path(root, relative)
    try:
        return read_final(path.parent, path.name, limit=limit, private=False)
    except TypedError as error:
        if str(error) == 'runner_output_limit':
            raise TypedError('typed_input_limit') from None
        raise


def _input_path(root, relative):
    if (type(relative) is not str or any(c in relative for c in '\\:\x00')
            or any(p in {'', '.', '..'} for p in relative.split('/'))):
        raise TypedError('typed_path_invalid')
    return root / relative


def _input_digest(root, relative):
    path = _input_path(root, relative)
    return _read_regular(path.parent, path.name, limit=None, private=False, digest_only=True)


def freeze_input(task, snapshot, batch_no, source_proof, *, runtime_root):
    """Read real frozen files; delegate canonical source qualification.

    source_proof is a TRUSTED application capability, not a material manifest
    or caller boolean. It must verify the frozen canonical proof/attachments
    and return its SHA. No production implementation is supplied here.
    """
    from dataclasses import asdict
    from .wiki_outcomes import frozen_context, full_frozen_context
    if source_proof is None:
        raise TypedError('source_proof_unavailable')
    if (type(batch_no) is not int or snapshot.task_id != task.task_id
            or type(task.task_id) is not str or ID.fullmatch(task.task_id) is None
            or type(snapshot.task_root.name) is not str or ID.fullmatch(snapshot.task_root.name) is None):
        raise TypedError('typed_binding_invalid')
    validate_layout(snapshot, runtime_root, {
        'task_id': task.task_id, 'attempt_id': snapshot.task_root.name,
        'batch_no': batch_no, 'boundary_sha256': task.boundary_sha256, 'input_sha256': '0' * 64})
    rows = tuple(raw for raw in task.raw if raw.batch_no == batch_no)
    if any(type(raw.byte_count) is not int or raw.byte_count <= 0 for raw in rows):
        raise TypedError('typed_binding_invalid')
    if sum(raw.byte_count for raw in task.raw) > INPUT_LIMIT:
        raise TypedError('typed_input_limit')
    baseline = {item.relative_path: item for item in snapshot.files}
    if len(baseline) != len(snapshot.files):
        raise TypedError('typed_binding_invalid')
    for raw in task.raw:
        item = baseline.get(raw.relative_path)
        if (item is None or item.role != 'raw' or type(item.byte_count) is not int
                or item.byte_count != raw.byte_count or item.sha256 != raw.content_sha256):
            raise TypedError('typed_binding_invalid')
    contents = {raw.raw_id: _input_bytes(snapshot.workspace, raw.relative_path, raw.byte_count) for raw in task.raw}
    whole = full_frozen_context(task, contents)
    context = frozen_context(task, batch_no, {r.raw_id: contents[r.raw_id] for r in rows})
    proof = source_proof(task=task, snapshot=snapshot, context=whole)
    _hash(proof)
    _hash(task.kit_manifest_sha256)
    payload = {'task_id': task.task_id, 'attempt_id': snapshot.task_root.name,
               'batch_no': batch_no, 'boundary_sha256': task.boundary_sha256,
               'kit_manifest_sha256': task.kit_manifest_sha256,
               'configuration': {'backend': task.backend, 'model': task.model,
                                 'effort': task.effort, 'schema_revision': 1},
               'task_raw': [asdict(raw) for raw in task.raw],
               'source_proof_sha256': proof,
               'baseline': [asdict(item) for item in snapshot.files],
               'raw': [{'frozen': asdict(raw), 'full_raw': content.decode('utf-8')}
                       for raw, content in context],
               'context_raw': [{'frozen': asdict(raw), 'full_raw': content.decode('utf-8')}
                               for raw, content in whole]}
    binding = {key: payload[key] for key in ('task_id', 'attempt_id', 'batch_no', 'boundary_sha256')}
    binding['input_sha256'] = digest(encoded(payload))
    validate_binding(binding)
    return binding, context, payload


def checked_documents(snapshot, changes):
    """Read caller's explicit changed wiki files, not a scan or staging writer.

    Complete change-set provenance and before/after staging protection remain
    the later caller's obligation. This only verifies supplied file bytes.
    """
    if type(changes) is not dict or not changes:
        raise TypedError('typed_binding_invalid')
    documents, size = [], 0
    for path, sha in sorted(changes.items()):
        if (type(path) is not str or not path.startswith('wiki/') or not path.endswith('.md')
                or ':' in path or '\\' in path or any(p in {'', '.', '..'} for p in path.split('/'))):
            raise TypedError('typed_path_invalid')
        _hash(sha)
        info = (snapshot.workspace / path).lstat()
        size += info.st_size
        if size > INPUT_LIMIT:
            raise TypedError('typed_input_limit')
        content = _input_bytes(snapshot.workspace, path, info.st_size)
        if digest(content) != sha:
            raise TypedError('typed_binding_invalid')
        documents.append({'path': path, 'sha256': sha, 'content': content.decode('utf-8')})
    return documents


# Transport schema only: parse_checks remains the sole R14 support judgment.
SUPPORT_SCHEMA = _schema_object({
    'contract': _string((SUPPORT_CONTRACT,)), 'schema_revision': {'type': 'integer', 'enum': [1]},
    'binding': _BINDING_SCHEMA, 'registry_sha256': _HASH_SCHEMA,
    'candidate_sha256': _HASH_SCHEMA,
    'checks': {'type': 'array', 'items': _schema_object({
        'claim_id': _string(), 'status': _string(('supported', 'unsupported', 'uncertain')),
        'reason': _string(), 'issues': {'type': 'array', 'items': _schema_object({
            'field': _string(sorted(_SUPPORT_FIELDS)),
            'category': _string(sorted(_SUPPORT_CATEGORIES)),
            'reason': _string()})}})}})


@dataclass(frozen=True)
class SupportInput:
    binding: dict
    registry_sha256: str
    candidate_sha256: str
    prompt: bytes
    model_config_hash: str
    measurement: InputBinding


def _support_bytes(value):
    """Canonical full JSON with a transport byte cap, never token estimation."""
    output = bytearray()
    encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    for text in encoder.iterencode(value):
        piece = text.encode('utf-8')
        if len(output) + len(piece) > INPUT_LIMIT:
            raise TypedError('typed_input_limit')
        output.extend(piece)
    return bytes(output)


def freeze_support(task, snapshot, registry, source_proof, measure, *, runtime_root, model, effort,
                   input_policy=None, max_application_input_bytes=INPUT_LIMIT):
    """Trusted caller admission before constructing/reviewing an R14 Gate.

    Reads EVERY task raw and the whole current tree. Snapshot before hashes
    are controller-owned baseline authority; backup is initially empty. The
    proof/measure capabilities have no production implementation/default here.
    This is a bounded readback/CAS observation, not protection from same-UID
    arbitrary writes or an atomic filesystem snapshot.
    """
    from .wiki_staging import _all_files, REGENERABLE_GRAPH, WikiStagingError
    from .wiki_support import SYSTEM, WikiSupportError, CONTRACT_VERSION, EXTRACTOR_VERSION, RECOVERY_VERSION
    from dataclasses import asdict
    if source_proof is None:
        raise TypedError('source_proof_unavailable')
    validate_input_policy(input_policy, measure, max_application_input_bytes)
    if measure is None and input_policy != APPLICATION_UTF8_POLICY:
        raise TypedError('input_budget_unavailable')
    if os.name != 'posix':
        raise TypedError('runner_unavailable')
    if (model != task.model or effort != task.effort or task.backend != 'codex_cli'
            or type(model) is not str or not model or type(effort) is not str or not effort):
        raise TypedError('typed_binding_invalid')
    if registry.staging_root != snapshot.workspace:
        raise TypedError('typed_binding_invalid')
    payload, all_raw, batches = None, [], []
    for batch in task.batches:
        binding, context, frozen = freeze_input(task, snapshot, batch.batch_no, source_proof,
                                               runtime_root=runtime_root)
        if payload is None:
            payload = dict(frozen)
        batches.append({'binding': binding, 'source_proof_sha256': frozen['source_proof_sha256']})
        all_raw.extend(frozen['raw'])
    if payload is None or len(all_raw) != len(task.raw):
        raise TypedError('typed_coverage_invalid')
    try:
        paths = _all_files(snapshot.workspace)
    except WikiStagingError:
        raise TypedError('typed_binding_invalid') from None
    expected_raw = {(r.relative_path, r.raw_id, r.content_sha256) for r in task.raw}
    if (len(registry.raws) != len(expected_raw)
            or {(r.path, r.stable_id, r.sha256) for r in registry.raws} != expected_raw):
        raise TypedError('typed_coverage_invalid')
    baseline = {f.relative_path: f for f in snapshot.files}
    changes = {c.path: c for c in registry.changes}
    if len(changes) != len(registry.changes) or set(baseline) - set(paths):
        raise TypedError('typed_coverage_invalid')
    current, changed = [], set()
    for path in sorted(paths):
        sha, size = _input_digest(snapshot.workspace, path)
        current.append({'path': path, 'sha256': sha, 'byte_count': size})
        old = baseline.get(path)
        differs = old is None or old.sha256 != sha or old.byte_count != size
        if differs and path.startswith('wiki/'):
            if not path.endswith('.md'):
                raise TypedError('typed_path_invalid')
            changed.add(path)
        elif differs and path not in REGENERABLE_GRAPH:
            raise TypedError('typed_binding_invalid')
        change = changes.get(path)
        if change is not None:
            content = _input_bytes(snapshot.workspace, path, min(size, INPUT_LIMIT))
            if change.after != content or change.after_sha256 != sha:
                raise TypedError('typed_binding_invalid')
            if old is None:
                if change.before is not None or change.before_sha256 is not None:
                    raise TypedError('typed_binding_invalid')
            elif (type(change.before) is not bytes or digest(change.before) != old.sha256
                  or change.before_sha256 != old.sha256 or len(change.before) != old.byte_count):
                raise TypedError('typed_binding_invalid')
    if changed != set(changes):
        raise TypedError('typed_coverage_invalid')
    try:
        registry.verify()
    except WikiSupportError:
        raise TypedError('typed_binding_invalid') from None
    # Re-scan names after readback: concurrent added/missing files are drift.
    try:
        if paths != _all_files(snapshot.workspace):
            raise TypedError('typed_binding_invalid')
    except WikiStagingError:
        raise TypedError('typed_binding_invalid') from None
    payload.update(raw=all_raw, batches=batches, current_files=current, current_task=asdict(task),
                   registry_binding_sha256=registry.binding_hash,
                   registry=registry.payload(), support_contract=SUPPORT_CONTRACT)
    binding = {key: payload[key] for key in ('task_id', 'attempt_id', 'batch_no', 'boundary_sha256')}
    binding['input_sha256'] = digest(_support_bytes(payload))
    registry_sha = digest(_support_bytes(registry.payload()))
    # The R14 SYSTEM is byte-for-byte first; canonical Registry payload stays
    # byte-for-byte nested as registry. Everything after SYSTEM is material.
    prompt = (SYSTEM + '\n\nTransport: return ONLY the fixed SUPPORT_SCHEMA envelope; '
              'binding/registry_sha256/candidate_sha256 must equal the supplied values. '
              'The envelope wraps the original checks; no tools or edits.\n').encode() + _support_bytes({
                  'binding': binding, 'registry_sha256': registry_sha,
                  'candidate_sha256': registry.candidate_hash, 'inputs': payload})
    measurement = admit_input(prompt, SUPPORT_SCHEMA, measure, input_policy=input_policy,
                              max_application_input_bytes=max_application_input_bytes)
    # Policy identity excludes candidate/prompt/input hash and measured count.
    # 8192 is Gate request policy, NOT a Codex CLI hard output token limit.
    policy = {'contract': SUPPORT_CONTRACT, 'schema': SUPPORT_SCHEMA,
              'provider': 'knowledge_subscription', 'transport_policy': 'readonly-schema-final-v1',
              'input_byte_limit': INPUT_LIMIT, 'final_byte_limit': FINAL_LIMIT,
              'r14_versions': [CONTRACT_VERSION, EXTRACTOR_VERSION, RECOVERY_VERSION],
              'system_sha256': digest(SYSTEM.encode()), 'model': model, 'effort': effort,
              'tokenizer_version': measurement.tokenizer_version, 'max_tokens': 8192,
              'input_policy': measurement.policy, 'measurement_unit': measurement.unit,
              'max_application_input_bytes': measurement.max_application_input_bytes,
              'source_boundary': {'task_id': task.task_id, 'attempt_id': snapshot.task_root.name,
                  'boundary_sha256': task.boundary_sha256, 'task_raw': payload['task_raw'],
                  'baseline': payload['baseline'],
                  'source_proofs': [b['source_proof_sha256'] for b in batches]},
              'kit_version': task.kit_version,
              'kit_manifest_sha256': task.kit_manifest_sha256, 'timeout': CHECK_TIMEOUT}
    return SupportInput(binding, registry_sha, registry.candidate_hash, prompt,
                        digest(encoded(policy)), measurement)


def parse_support(content, prepared, registry):
    from .wiki_support import parse_checks, WikiSupportError
    value = strict_json(content)
    _object(value, ('contract', 'schema_revision', 'binding', 'registry_sha256', 'candidate_sha256', 'checks'))
    if value['contract'] != SUPPORT_CONTRACT or type(value['schema_revision']) is not int or value['schema_revision'] != 1:
        raise TypedError('typed_protocol_invalid')
    validate_binding(value['binding'])
    if (value['binding'] != prepared.binding or value['registry_sha256'] != prepared.registry_sha256
            or value['candidate_sha256'] != prepared.candidate_sha256):
        raise TypedError('typed_binding_invalid')
    unwrapped = encoded({'checks': value['checks']}).decode()
    try:
        parse_checks(registry, unwrapped)
    except WikiSupportError:
        raise TypedError('typed_coverage_invalid') from None
    return unwrapped


def _observable_failure(event):
    """Only structural event discriminants, NEVER words inside source/text.

    Exec error/turn.failed shapes and candidate App Server event shapes are
    explicit fixtures, not proof that installed exec exposes all compaction.
    Unknown remote errors are fixed failures; no message text is retained.
    """
    if type(event) is not dict:
        return None
    kind = event.get('type') or event.get('method')
    if type(kind) is not str:
        return None
    item = event.get('item')
    if kind in ('item/started', 'item/completed'):
        params = event.get('params')
        item = params.get('item') if type(params) is dict else None
    item_kind = item.get('type') if type(item) is dict else None
    if (kind in ('context_compaction', 'context.compacted', 'thread.compacted', 'thread/compacted')
            or (kind in ('item.started', 'item.completed', 'item/started', 'item/completed')
                and item_kind in ('context_compaction', 'contextCompaction'))):
        return 'context_compaction_observed'
    error = event.get('error')
    if kind == 'error' and type(event.get('params')) is dict:
        error = event['params'].get('error')
    code = (error.get('codexErrorInfo') or error.get('code')) if type(error) is dict else None
    if (kind in ('context_window_exceeded', 'context_length_exceeded')
            or (kind in ('error', 'turn.failed')
                and code in ('context_window_exceeded', 'context_length_exceeded', 'contextWindowExceeded',
                             'sessionBudgetExceeded'))):
        return 'context_limit_observed'
    if (kind in ('error', 'turn.failed')
            or (kind in ('item.started', 'item.completed') and item_kind == 'error')):
        return 'agent_failed'
    return None


def _metadata_fallback_warning(event, requested_model):
    """rust-v0.157.1 Warning mapping, exactly bound to the requested slug.

    turn_context.rs emits this complete message as Warning; the JSONL
    processor maps it to an error item while retaining Running status.
    Never apply this exception to turn.failed, top-level error, or other text.
    """
    if (type(requested_model) is not str or not requested_model
            or type(event) is not dict or set(event) != {'type', 'item'}
            or event['type'] != 'item.completed'):
        return False
    item = event['item']
    return (type(item) is dict and set(item) == {'id', 'type', 'message'}
            and type(item['id']) is str and bool(item['id']) and item['type'] == 'error'
            and item['message'] == f'Model metadata for `{requested_model}` not found. '
                'Defaulting to fallback metadata; this can degrade performance and cause issues.')


def _jsonl_item_diagnostic(event, requested_model):
    """rust-v0.157.1 completed ErrorItem is a Running diagnostic.

    Warning/ConfigWarning/DeprecationNotice/ModelRerouted use this shape.
    Actual Error uses top-level error; failed turns use turn.failed. A reroute
    remains a request-model violation even though the CLI regards it Running.
    """
    if (type(event) is not dict or set(event) != {'type', 'item'}
            or event['type'] != 'item.completed'):
        return False
    item = event['item']
    if (type(item) is not dict or set(item) != {'id', 'type', 'message'}
            or type(item['id']) is not str or not item['id'] or item['type'] != 'error'
            or type(item['message']) is not str or not item['message']):
        return False
    message = item['message']
    if message.startswith('model rerouted: '):
        raise TypedError('agent_failed')
    # A known metadata warning naming another model cannot silently qualify
    # this request. This is a model-binding guard, not a warning allow-list.
    model = re.fullmatch(r'Model metadata for `([^`]+)` not found\. Defaulting to fallback metadata; '
                        r'this can degrade performance and cause issues\.', message)
    if model is not None and model[1] != requested_model:
        raise TypedError('agent_failed')
    return True


def pump(process, prompt, final, *, timeout, cancelled, terminate, recording_call=None,
         requested_model=None, diagnostic=None):
    """Nonblocking POSIX pipe pump; bounded memory and no communicate()."""
    deadline = time.monotonic() + timeout
    from .wiki_exec_recording import RecordingError
    cleanup_reserve = 0 if recording_call is None else min(2.0, recording_call.timeout_seconds / 4)
    if recording_call is not None:
        deadline = min(deadline, recording_call.deadline - cleanup_reserve)
    def remaining():
        value = deadline - time.monotonic()
        return value if recording_call is None else min(value, recording_call.remaining_seconds - cleanup_reserve)
    usage, line, offset = {}, bytearray(), 0
    totals = {'out': 0, 'err': 0}
    decoders = {k: codecs.getincrementaldecoder('utf-8')('strict') for k in totals}
    def consume_line(data):
        from .codex import _usage
        try:
            try:
                event = json.loads(data)
            except (ValueError, UnicodeError):
                event = None
            warning = _jsonl_item_diagnostic(event, requested_model)
            if warning and diagnostic is not None:
                # Raw event is already in stdout.jsonl.raw. Persist only a
                # finite private category here, never arbitrary message text.
                category = ('model_metadata_fallback' if _metadata_fallback_warning(event, requested_model)
                            else 'cli_item_diagnostic')
                warnings = diagnostic.setdefault('observed_warnings', [])
                if category not in warnings:
                    warnings.append(category)
            failure = None if warning else _observable_failure(event)
            if failure is not None:
                raise TypedError(failure)
            usage.update(_usage(data.decode('utf-8')))
            if recording_call is not None:
                recording_call.progress(usage=dict(usage))
        except RecursionError:
            raise TypedError('agent_failed') from None
    def check_final():
        if final.exists() or final.is_symlink():
            info = final.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise TypedError('typed_output_invalid')
            if info.st_size > FINAL_LIMIT:
                raise TypedError('runner_output_limit')
    with selectors.DefaultSelector() as selector:
        streams = ((process.stdin, selectors.EVENT_WRITE, 'in'),
                   (process.stdout, selectors.EVENT_READ, 'out'),
                   (process.stderr, selectors.EVENT_READ, 'err'))
        for stream, event, tag in streams:
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, event, tag)
        try:
            while selector.get_map():
                if cancelled.is_set():
                    raise TypedError('interrupted')
                if remaining() <= 0:
                    raise TypedError('runner_timeout')
                check_final()
                for key, _events in selector.select(min(.05, max(0, remaining()))):
                    stream, tag = key.fileobj, key.data
                    if tag == 'in':
                        try:
                            wrote = os.write(stream.fileno(), prompt[offset:offset + 65536])
                            offset += wrote
                            if recording_call is not None:
                                recording_call.progress(stdin_written=offset)
                        except BlockingIOError:
                            continue
                        except BrokenPipeError:
                            if offset != len(prompt):
                                raise TypedError('agent_failed') from None
                        if offset == len(prompt):
                            selector.unregister(stream)
                            stream.close()
                        continue
                    try:
                        data = os.read(stream.fileno(), 65536)
                    except BlockingIOError:
                        continue
                    if not data:
                        if recording_call is not None:
                            recording_call.progress(stdout_eof=(tag == 'out'), stderr_eof=(tag == 'err'))
                        decoders[tag].decode(b'', final=True)
                        if tag == 'out' and line:
                            consume_line(bytes(line))
                        selector.unregister(stream)
                        stream.close()
                        continue
                    if recording_call is not None:
                        recording_call.write(tag, data)
                    totals[tag] += len(data)
                    if totals[tag] > (STDOUT_LIMIT if tag == 'out' else STDERR_LIMIT):
                        raise TypedError('runner_output_limit')
                    decoders[tag].decode(data)
                    if tag == 'out':
                        for part in data.splitlines(keepends=True):
                            line.extend(part)
                            if len(line) > LINE_LIMIT:
                                raise TypedError('runner_output_limit')
                            if line.endswith(b'\n'):
                                consume_line(bytes(line))
                                line.clear()
            while process.poll() is None:
                if cancelled.is_set():
                    raise TypedError('interrupted')
                if remaining() <= 0:
                    raise TypedError('runner_timeout')
                check_final()
                time.sleep(.01)
            if cancelled.is_set():
                raise TypedError('interrupted')
            if process.returncode:
                raise TypedError('agent_failed')
            return tuple(sorted(usage.items()))
        except (TypedError, UnicodeError, OSError, RecordingError):
            terminate(process)
            raise
        finally:
            for stream, _event, _tag in streams:
                if not stream.closed:
                    stream.close()


# Host audit evidence, independently bounded. Not a model/token/input budget.
STAGE_METADATA_LIMIT = 32 * 1024 * 1024
STAGE_CONTRACT = 'wiki-outcome-stage-v1'
REFRESH_CONTRACT = 'wiki-outcome-hash-refresh-v1'


@dataclass(frozen=True)
class FrozenOutcomeStage:
    binding_json: bytes
    proposal: bytes
    check_result: TypedRunnerResult
    changes_json: bytes
    documents_json: bytes
    tree_json: bytes
    source_sha256: str
    receipt_json: bytes


@dataclass(frozen=True)
class RefreshedOutcomeCandidate:
    proposal: bytes
    receipt_json: bytes
    changes_json: bytes
    documents_json: bytes
    tree_json: bytes
    reusable_check: TypedRunnerResult | None


def _stage_decode(content):
    """Only canonical host JSON; model final parsing keeps its own limit."""
    if type(content) is not bytes or not content or len(content) > STAGE_METADATA_LIMIT:
        raise TypedError('typed_input_limit')
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise TypedError('typed_binding_invalid')
            result[key] = value
        return result
    def invalid(_value):
        raise TypedError('typed_binding_invalid')
    try:
        value = json.loads(content.decode('utf-8'), object_pairs_hook=pairs,
                           parse_constant=invalid, parse_float=invalid)
        if encoded(value) != content:
            raise TypedError('typed_binding_invalid')
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise TypedError('typed_binding_invalid') from None


def _stage_size(*parts):
    if any(type(p) is not bytes for p in parts) or sum(map(len, parts)) > STAGE_METADATA_LIMIT:
        raise TypedError('typed_input_limit')


def _stage_path(path):
    if (type(path) is not str or path.startswith('/') or ':' in path or '\\' in path
            or '\0' in path or any(p in {'', '.', '..'} for p in path.split('/'))):
        raise TypedError('typed_path_invalid')


def _stage_changes(snapshot, tree):
    """Reconstruct full D from one complete tree and the same pre-batch baseline."""
    from .wiki_staging import REGENERABLE_GRAPH
    baseline = {f.relative_path: f for f in snapshot.files}
    if len(baseline) != len(snapshot.files) or type(tree) is not list:
        raise TypedError('typed_binding_invalid')
    current, changes = {}, []
    for row in tree:
        if type(row) is not list or len(row) != 3:
            raise TypedError('typed_binding_invalid')
        path, sha, size = row
        _stage_path(path)
        _hash(sha)
        if path in current or type(size) is not int or size < 0:
            raise TypedError('typed_binding_invalid')
        current[path] = (sha, size)
        before = baseline.get(path)
        if before is not None and (before.sha256, before.byte_count) == (sha, size):
            continue
        if ((before is not None and before.role == 'raw')
                or (not path.startswith('wiki/') and path not in REGENERABLE_GRAPH)
                or (path.startswith('wiki/') and not path.endswith('.md'))):
            raise TypedError('typed_binding_invalid')
        changes.append({'path': path, 'before_sha256': before.sha256 if before else None,
                        'after_sha256': sha, 'byte_count': size})
    if set(baseline) - set(current) or list(current) != sorted(current):
        raise TypedError('typed_binding_invalid')
    return changes


def _outcome_tree(snapshot, task, batch_no, validated):
    from .wiki_staging import _all_files, ValidatedBatch, WikiStagingError
    if (not isinstance(validated, ValidatedBatch) or type(batch_no) is not int
            or type(validated.batch_no) is not int
            or validated.task_id != task.task_id or validated.batch_no != batch_no
            or validated.staging_vault != snapshot.workspace):
        raise TypedError('typed_binding_invalid')
    batch_paths = {r.relative_path for r in task.raw if r.batch_no == batch_no}
    if (not batch_paths or not batch_paths <= set(snapshot.pending_before)
            or len(set(snapshot.pending_before)) != len(snapshot.pending_before)
            or len(set(validated.pending_after)) != len(validated.pending_after)
            or set(validated.pending_after) != set(snapshot.pending_before) - batch_paths):
        raise TypedError('typed_binding_invalid')
    for path in (*snapshot.pending_before, *validated.pending_after):
        _stage_path(path)
    def observe():
        try:
            paths = _all_files(snapshot.workspace)
            tree = [[p, *_input_digest(snapshot.workspace, p)] for p in sorted(paths)]
            content = encoded(tree)
            _stage_size(content)
            if paths != _all_files(snapshot.workspace):
                raise TypedError('typed_binding_invalid')
            return content
        except (WikiStagingError, OSError):
            raise TypedError('typed_binding_invalid') from None
    tree = observe()
    changes = _stage_changes(snapshot, _stage_decode(tree))
    for change in validated.changes:
        _stage_path(change.relative_path)
        _hash(change.after_sha256)
        if change.before_sha256 is not None:
            _hash(change.before_sha256)
    supplied = sorted((c.relative_path, c.before_sha256, c.after_sha256) for c in validated.changes)
    if (len({c.relative_path for c in validated.changes}) != len(validated.changes)
            or supplied != [(c['path'], c['before_sha256'], c['after_sha256']) for c in changes]
            or observe() != tree):
        raise TypedError('typed_binding_invalid')
    return encoded(changes), tree


def _stage_documents(snapshot, changes_json):
    changes = _stage_decode(changes_json)
    wiki = {c['path']: c['after_sha256'] for c in changes if c['path'].startswith('wiki/')}
    try:
        result = encoded(checked_documents(snapshot, wiki))
    except (OSError, UnicodeError):
        raise TypedError('typed_binding_invalid') from None
    _stage_size(result)
    return result


def _stage_check(task, result, binding, rows, payload, proposal, documents):
    if (not isinstance(result, TypedRunnerResult) or not result.succeeded
            or type(result.final_bytes) is not bytes
            or result.final_sha256 != digest(result.final_bytes)
            or not isinstance(result.input_binding, InputBinding)
            or result.input_binding.schema_sha256 != digest(encoded(CHECK_SCHEMA))
            or result.input_binding.schema_bytes != len(encoded(CHECK_SCHEMA))):
        raise TypedError('typed_binding_invalid')
    content = {c['frozen']['raw_id']: c['full_raw'].encode('utf-8') for c in payload['context_raw']}
    whole = tuple((r, content[r.raw_id]) for r in task.raw)
    review = parse_check(result.final_bytes, binding, rows,
        proposal_sha256=digest(proposal), changes_sha256=digest(documents),
        source_proof_sha256=payload['source_proof_sha256'], full_context=whole)
    candidate = parse_proposal(proposal, binding, rows)
    for outcome, result_review in zip(candidate['outcomes'], review['reviews']):
        dimensions = result_review['dimensions']
        if (result_review['source_check']['status'] != 'complete'
                or any(d['status'] == 'unknown' for d in dimensions)):
            raise TypedError('typed_protocol_invalid')
        if outcome['status'] == 'processed_no_knowledge':
            if result_review['status'] != 'verified':
                raise TypedError('typed_protocol_invalid')
        elif outcome['status'] == 'processed_with_knowledge':
            if (result_review['status'] != 'unsupported'
                    or not any(d['status'] == 'present' for d in dimensions)):
                raise TypedError('typed_protocol_invalid')
        else:
            raise TypedError('typed_protocol_invalid')


def _stage_receipt(binding, proposal, check, changes, documents, tree, source):
    return encoded({'contract': STAGE_CONTRACT, 'binding': binding,
        'proposal_sha256': digest(proposal), 'check_sha256': check.final_sha256,
        'check_input': check.input_binding.__dict__,
        'changes_sha256': digest(changes), 'documents_sha256': digest(documents),
        'protected_tree_sha256': digest(tree), 'source_proof_sha256': source})


def _old_stage_evidence(snapshot, stage, binding):
    _stage_size(stage.binding_json, stage.proposal, stage.check_result.final_bytes,
                stage.changes_json, stage.documents_json, stage.tree_json, stage.receipt_json)
    if _stage_decode(stage.binding_json) != binding:
        raise TypedError('typed_binding_invalid')
    tree = _stage_decode(stage.tree_json)
    changes = _stage_changes(snapshot, tree)
    if _stage_decode(stage.changes_json) != changes:
        raise TypedError('typed_binding_invalid')
    documents = _stage_decode(stage.documents_json)
    if type(documents) is not list:
        raise TypedError('typed_binding_invalid')
    wiki = {c['path']: c for c in changes if c['path'].startswith('wiki/')}
    seen = set()
    for doc in documents:
        _object(doc, ('path', 'sha256', 'content'))
        path = doc['path']
        _stage_path(path)
        if path in seen or path not in wiki or type(doc['content']) is not str:
            raise TypedError('typed_binding_invalid')
        seen.add(path)
        try:
            body = doc['content'].encode('utf-8')
        except UnicodeError:
            raise TypedError('typed_binding_invalid') from None
        if (digest(body) != doc['sha256'] or doc['sha256'] != wiki[path]['after_sha256']
                or len(body) != wiki[path]['byte_count']):
            raise TypedError('typed_binding_invalid')
    if [d['path'] for d in documents] != sorted(wiki):
        raise TypedError('typed_binding_invalid')
    expected = _stage_receipt(binding, stage.proposal, stage.check_result, stage.changes_json,
                              stage.documents_json, stage.tree_json, stage.source_sha256)
    if encoded(_stage_decode(stage.receipt_json)) != expected:
        raise TypedError('typed_binding_invalid')
    return {c['path']: c['after_sha256'] for c in changes}


def freeze_outcome_stage(snapshot, runtime_root, *, task, batch_no, proposal,
                         check_result, validated, source_proof):
    """Save P0/Q0/full D0 before health. This grants no publication capability.

    validated must come from actual validate_staging; this helper rechecks its
    full content boundary, not the kit's execution or health truthfulness.
    """
    binding, rows, payload = freeze_input(task, snapshot, batch_no, source_proof, runtime_root=runtime_root)
    parsed = parse_proposal(proposal, binding, rows)
    if any(o['status'] == 'unknown' for o in parsed['outcomes']):
        raise TypedError('typed_protocol_invalid')
    changes, tree = _outcome_tree(snapshot, task, batch_no, validated)
    documents = _stage_documents(snapshot, changes)
    wiki = {d['path']: d['sha256'] for d in _stage_decode(documents)}
    if any(wiki.get(d['path']) != d['sha256'] for o in parsed['outcomes'] for d in o['documents']):
        raise TypedError('typed_binding_invalid')
    _stage_check(task, check_result, binding, rows, payload, proposal, documents)
    receipt = _stage_receipt(binding, proposal, check_result, changes, documents, tree,
                             payload['source_proof_sha256'])
    stage = FrozenOutcomeStage(encoded(binding), proposal, check_result, changes, documents,
                               tree, payload['source_proof_sha256'], receipt)
    _old_stage_evidence(snapshot, stage, binding)
    if (freeze_input(task, snapshot, batch_no, source_proof, runtime_root=runtime_root)[2] != payload
            or _outcome_tree(snapshot, task, batch_no, validated) != (changes, tree)):
        raise TypedError('typed_binding_invalid')
    return stage


def refresh_outcome_documents(snapshot, runtime_root, *, task, batch_no, stage,
                              validated, source_proof):
    """Only replace existing cited hashes at identical paths; return unverified P1.

    Changed D always needs an independent final check and whole-D R14 support.
    The receipt is host evidence, not accepted/main DB authority. It is not a
    recovery journal and cannot resurrect old files from the new workspace.
    """
    if not isinstance(stage, FrozenOutcomeStage):
        raise TypedError('typed_binding_invalid')
    binding, rows, payload = freeze_input(task, snapshot, batch_no, source_proof, runtime_root=runtime_root)
    if encoded(binding) != stage.binding_json or payload['source_proof_sha256'] != stage.source_sha256:
        raise TypedError('typed_binding_invalid')
    parsed = parse_proposal(stage.proposal, binding, rows)
    if any(o['status'] == 'unknown' for o in parsed['outcomes']):
        raise TypedError('typed_protocol_invalid')
    _stage_check(task, stage.check_result, binding, rows, payload, stage.proposal, stage.documents_json)
    old = _old_stage_evidence(snapshot, stage, binding)
    changes, tree = _outcome_tree(snapshot, task, batch_no, validated)
    documents = _stage_documents(snapshot, changes)
    new = {d['path']: d['sha256'] for d in _stage_decode(documents)}
    mapping = []
    for outcome in parsed['outcomes']:
        for document in outcome['documents']:
            path, prior = document['path'], document['sha256']
            if path not in new or old.get(path) != prior:
                raise TypedError('typed_binding_invalid')
            mapping.append([path, prior, new[path]])
            document['sha256'] = new[path]
    unchanged = (changes, tree, documents) == (stage.changes_json, stage.tree_json, stage.documents_json)
    candidate = stage.proposal if unchanged else encoded(parsed)
    parse_proposal(candidate, binding, rows)
    receipt = encoded({'contract': REFRESH_CONTRACT, 'binding': binding,
        'parent_stage_sha256': digest(stage.receipt_json),
        'parent_proposal_sha256': digest(stage.proposal), 'parent_check_sha256': stage.check_result.final_sha256,
        'before_changes_sha256': digest(stage.changes_json), 'before_documents_sha256': digest(stage.documents_json),
        'before_protected_tree_sha256': digest(stage.tree_json),
        'final_changes_sha256': digest(changes), 'final_documents_sha256': digest(documents),
        'protected_tree_sha256': digest(tree), 'source_proof_sha256': stage.source_sha256,
        'proposal_sha256': digest(candidate), 'document_mapping': mapping})
    _stage_size(stage.binding_json, candidate, stage.check_result.final_bytes, changes, documents, tree, receipt)
    _stage_decode(receipt)
    if (freeze_input(task, snapshot, batch_no, source_proof, runtime_root=runtime_root)[2] != payload
            or _outcome_tree(snapshot, task, batch_no, validated) != (changes, tree)):
        raise TypedError('typed_binding_invalid')
    return RefreshedOutcomeCandidate(candidate, receipt, changes, documents, tree,
                                     stage.check_result if unchanged else None)
