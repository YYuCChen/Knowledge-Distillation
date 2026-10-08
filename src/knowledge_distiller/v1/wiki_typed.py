"""Typed candidates and bounded POSIX transport; no acceptance or model client.

Source proof and exact token measurement are application-owned capabilities.
There is deliberately no default proof, tokenizer estimate, or source-complete
boolean. The caller must still verify staging changes and publication.
"""
from __future__ import annotations

import codecs
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import stat
import time

CONTRACT = 'r08-wiki-outcomes-v1'
CHECK_CONTRACT = 'r08-no-knowledge-check-v1'
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
                   'runner_unavailable', 'vault_busy'}
        super().__init__(code if type(code) is str and code in allowed else 'typed_input_invalid')


@dataclass(frozen=True)
class TypedRunnerResult:
    error_code: str | None
    usage: tuple[tuple[str, int], ...] = ()
    final_bytes: bytes | None = None
    final_sha256: str | None = None

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


def parse_check(content, binding, rows, *, proposal_sha256, changes_sha256, source_proof_sha256):
    validate_binding(binding)
    value = strict_json(content)
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
    by_id = {raw.raw_id: (raw, data.decode('utf-8')) for raw, data in rows}
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
                        or type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text)
                        or type(evidence['text']) is not str or evidence['text'] != text[start:end]):
                    raise TypedError('typed_binding_invalid')
        # Still only a model candidate, but even its claim of verified cannot
        # contradict its own source or dimensions. No SourceFact is created.
        if review['status'] == 'verified' and (source['status'] != 'complete' or any(
                d['status'] in {'present', 'unknown'} for d in dimensions)):
            raise TypedError('typed_protocol_invalid')
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
    if type(name) is not str or name in {'', '.', '..'} or any(c in name for c in '/\\:\x00'):
        raise TypedError('typed_path_invalid')
    root = _directory(directory)
    parents = [(p, (p.lstat().st_dev, p.lstat().st_ino, p.lstat().st_mode)) for p in (root, *root.parents)]
    path = root / name
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid()
            or (private and stat.S_IMODE(info.st_mode) & 0o077)):
        raise TypedError('typed_output_invalid')
    if info.st_size > limit:
        raise TypedError('runner_output_limit')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        held = os.fstat(fd)
        if not stat.S_ISREG(held.st_mode) or _file_key(held) != _file_key(info):
            raise TypedError('typed_output_invalid')
        chunks, size = [], 0
        while True:
            chunk = os.read(fd, min(65536, limit + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > limit:
                raise TypedError('runner_output_limit')
        if (_file_key(os.fstat(fd)) != _file_key(info) or _file_key(path.lstat()) != _file_key(info)
                or any((p.lstat().st_dev, p.lstat().st_ino, p.lstat().st_mode) != key for p, key in parents)):
            raise TypedError('typed_output_invalid')
        return b''.join(chunks)
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


def require_budget(prompt, schema, measure):
    if len(prompt) > INPUT_LIMIT:
        raise TypedError('typed_input_limit')
    if measure is None:
        raise TypedError('input_budget_unavailable')
    # Trusted program capability must measure the ENTIRE actual prompt/schema.
    # Return (tokenizer/version, exact input tokens, hard available tokens).
    version, count, budget = measure(prompt, encoded(schema))
    if (type(version) is not str or not version.strip() or type(count) is not int
            or type(budget) is not int or count < 0 or budget <= 0):
        raise TypedError('input_budget_unavailable')
    if count > budget:
        raise TypedError('group_over_budget')


def _input_bytes(root, relative, limit):
    if (type(relative) is not str or any(c in relative for c in '\\:\x00')
            or any(p in {'', '.', '..'} for p in relative.split('/'))):
        raise TypedError('typed_path_invalid')
    path = root / relative
    try:
        return read_final(path.parent, path.name, limit=limit, private=False)
    except TypedError as error:
        if str(error) == 'runner_output_limit':
            raise TypedError('typed_input_limit') from None
        raise


def freeze_input(task, snapshot, batch_no, source_proof, *, runtime_root):
    """Read real frozen files; delegate canonical source qualification.

    source_proof is a TRUSTED application capability, not a material manifest
    or caller boolean. It must verify the frozen canonical proof/attachments
    and return its SHA. No production implementation is supplied here.
    """
    from dataclasses import asdict
    from .wiki_outcomes import frozen_context
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
    if sum(raw.byte_count for raw in rows) > INPUT_LIMIT:
        raise TypedError('typed_input_limit')
    baseline = {item.relative_path: item for item in snapshot.files}
    if len(baseline) != len(snapshot.files):
        raise TypedError('typed_binding_invalid')
    for raw in task.raw:
        item = baseline.get(raw.relative_path)
        if (item is None or item.role != 'raw' or type(item.byte_count) is not int
                or item.byte_count != raw.byte_count or item.sha256 != raw.content_sha256):
            raise TypedError('typed_binding_invalid')
    contents = {raw.raw_id: _input_bytes(snapshot.workspace, raw.relative_path, raw.byte_count) for raw in rows}
    context = frozen_context(task, batch_no, contents)
    proof = source_proof(task=task, snapshot=snapshot, context=context)
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
                       for raw, content in context]}
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


def pump(process, prompt, final, *, timeout, cancelled, terminate):
    """Nonblocking POSIX pipe pump; bounded memory and no communicate()."""
    deadline = time.monotonic() + timeout
    usage, line, offset = {}, bytearray(), 0
    totals = {'out': 0, 'err': 0}
    decoders = {k: codecs.getincrementaldecoder('utf-8')('strict') for k in totals}
    def consume_line(data):
        from .codex import _usage
        try:
            usage.update(_usage(data.decode('utf-8')))
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
                if time.monotonic() >= deadline:
                    raise TypedError('runner_timeout')
                check_final()
                for key, _events in selector.select(min(.05, max(0, deadline - time.monotonic()))):
                    stream, tag = key.fileobj, key.data
                    if tag == 'in':
                        try:
                            wrote = os.write(stream.fileno(), prompt[offset:offset + 65536])
                            offset += wrote
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
                        decoders[tag].decode(b'', final=True)
                        if tag == 'out' and line:
                            consume_line(bytes(line))
                        selector.unregister(stream)
                        stream.close()
                        continue
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
                if time.monotonic() >= deadline:
                    raise TypedError('runner_timeout')
                check_final()
                time.sleep(.01)
            if cancelled.is_set():
                raise TypedError('interrupted')
            if process.returncode:
                raise TypedError('agent_failed')
            return tuple(sorted(usage.items()))
        except (TypedError, UnicodeError, OSError):
            terminate(process)
            raise
        finally:
            for stream, _event, _tag in streams:
                if not stream.closed:
                    stream.close()
