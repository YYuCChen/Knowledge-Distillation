"""Application bytes only; fake CLI events are not observed real exec samples."""
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shlex
import sys

import pytest

from knowledge_distiller.v1 import wiki_typed as typed, wiki_support as ws
from knowledge_distiller.v1.wiki_runner import CodexWikiRunner
from knowledge_distiller.v1.wiki_staging import SnapshotFile
from knowledge_distiller.v1.wiki_tasks import _boundary
from .test_wiki_typed_runner import fixture as typed_files, proposal, proof as source_proof, measure
from .test_wiki_support_runner import h as support_files, proof as support_proof, envelope

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='typed transport requires POSIX')
POLICY = typed.APPLICATION_UTF8_POLICY


@pytest.fixture
def text_files(typed_files):
    tmp, runtime, snapshot, task = typed_files
    rows, files = [], []
    for raw in task.raw:
        path = snapshot.workspace / raw.relative_path
        content = path.read_bytes() + ('\r\nEnglish 中文😀\r\n重复全文块。' * 3 + '末字节Z').encode()
        path.write_bytes(content)
        rows.append(replace(raw, byte_count=len(content), content_sha256=typed.digest(content)))
        files.append(SnapshotFile(raw.relative_path, 'raw', len(content), typed.digest(content)))
    task = replace(task, raw=tuple(rows), boundary_sha256=_boundary((tuple(rows),)))
    return tmp, runtime, replace(snapshot, files=tuple(files)), task


def make_runner(tmp):
    probes = []
    def resolve():
        probes.append('resolve')
        return str(tmp / 'fake-cli')
    def models():
        probes.append('models')
        return [{'model': 'fake', 'efforts': ['medium']}]
    return CodexWikiRunner(executable_resolver=resolve, model_probe=models), probes


def configure(tmp, value, *, event=None, mutate=''):
    script = tmp / 'fake-cli.py'
    body = ('import json,sys\nfrom pathlib import Path\n'
        'argv=sys.argv[1:]\n'
        'prompt=sys.stdin.buffer.read()\n'
        'schema=Path(argv[argv.index("--output-schema")+1]).read_bytes()\n'
        f'Path({str(tmp / "input.bin")!r}).write_bytes(prompt)\n'
        f'Path({str(tmp / "schema.bin")!r}).write_bytes(schema)\n'
        f'Path({str(tmp / "argv.json")!r}).write_text(json.dumps(argv))\n'
        f'p=Path({str(tmp / "calls")!r});p.write_text((p.read_text() if p.exists() else "")+"1")\n'
        f'Path(argv[argv.index("-o")+1]).write_bytes({typed.encoded(value)!r})\n' + mutate)
    if event is not None:
        body += f'print({json.dumps(event, ensure_ascii=False)!r},flush=True)\n'
    script.write_text(body)
    wrapper = tmp / 'fake-cli'
    wrapper.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' ' + shlex.quote(str(script)) + ' "$@"\n')
    wrapper.chmod(0o700)


def call_count(tmp):
    p = tmp / 'calls'
    return len(p.read_text()) if p.exists() else 0


def prepare_text(files, kind):
    tmp, runtime, snapshot, task = files
    r, probes = make_runner(tmp)
    value = proposal(files, unknown=True)
    if kind == 'check':
        changes = {'wiki/log.md': typed.digest((snapshot.workspace / 'wiki/log.md').read_bytes())}
        documents = typed.checked_documents(snapshot, changes)
        final = dict(contract=typed.CHECK_CONTRACT, schema_revision=1, binding=value['binding'],
            proposal_sha256=typed.digest(typed.encoded(value)), changes_sha256=typed.digest(typed.encoded(documents)),
            reviews=[dict(raw_id=raw.raw_id, content_sha256=raw.content_sha256, status='unknown',
                reason='Synthetic checker, semantic support unknown.',
                source_check=dict(status='unknown', reason='Synthetic source completeness not certified.',
                                  evidence_sha256=typed.digest(b'synthetic-only-proof')),
                dimensions=[dict(dimension=n, status='unknown', reason='Not semantically tested.',
                                 evidence=[], related_raw_ids=[]) for n in typed.DIMENSIONS]) for raw in task.raw])
    else:
        final = value
    configure(tmp, final)
    def invoke(**kwargs):
        params = dict(task=task, batch_no=1, model='fake', effort='medium', source_proof=source_proof,
                      input_policy=POLICY)
        params.update(kwargs)
        if kind == 'check':
            return r.check_json(snapshot, runtime, proposal=typed.encoded(value), changes=changes, **params)
        return r.run_outcomes(snapshot, runtime, **params)
    return r, probes, invoke, final


def support_client(files, r, **kwargs):
    params = dict(task=files['task'], registry=files['registry'], model='fake', effort='medium',
                  source_proof=support_proof, input_policy=POLICY)
    params.update(kwargs)
    return r.support_client(files['snapshot'], files['runtime'], **params)


@pytest.mark.parametrize('kind', ['outcomes', 'check'])
@pytest.mark.parametrize('offset', [-1, 0, 1])
def test_actual_complete_utf8_stdin_plus_schema_boundary(text_files, kind, offset):
    tmp, _runtime, _snapshot, task = text_files
    r, probes, invoke, _final = prepare_text(text_files, kind)
    first = invoke()
    assert first.succeeded
    actual, schema = (tmp / 'input.bin').read_bytes(), (tmp / 'schema.bin').read_bytes()
    assert 'English 中文😀'.encode() in actual and b'Z' in actual
    # JSON escaped CRLF is still part of the complete actual application bytes.
    assert b'\\r\\n' in actual and actual.count('重复全文块。'.encode()) >= 6
    for raw in task.raw:
        assert raw.content_sha256.encode() in actual
    b = first.input_binding
    assert b.policy == POLICY and b.unit == 'utf8_bytes' and b.tokenizer_version is None
    assert b.prompt_bytes == len(actual) and b.schema_bytes == len(schema)
    assert b.prompt_sha256 == typed.digest(actual) and b.schema_sha256 == typed.digest(schema)
    assert b.count == len(actual) + len(schema)
    before = call_count(tmp), len(probes)
    # Total is B+1/B/B-1 respectively: schema cannot be omitted to fit B.
    result = invoke(max_application_input_bytes=b.count + offset)
    if offset < 0:
        assert result.error_code == 'typed_input_limit'
        assert (call_count(tmp), len(probes)) == before
    else:
        assert result.succeeded and call_count(tmp) == before[0] + 1
        assert result.input_binding.limit == b.count + offset
    argv = json.loads((tmp / 'argv.json').read_text())
    assert argv[-1] == '-' and 'resume' not in argv and '--output-schema' in argv
    assert 'forced_login_method="chatgpt"' in argv and '--model' in argv


@pytest.mark.parametrize('offset', [-1, 0, 1])
def test_support_full_input_cap_before_any_gate_reservation(support_files, offset):
    h = support_files
    r, probes = make_runner(h['tmp'])
    first = support_client(h, r)
    b = first.prepared.measurement
    configure(h['tmp'], envelope(first))
    if offset < 0:
        with pytest.raises(typed.TypedError, match='typed_input_limit'):
            support_client(h, r, max_application_input_bytes=b.count + offset)
        assert probes == [] and call_count(h['tmp']) == 0
        assert not (h['tmp'] / 'checkpoint').exists()
    else:
        c = support_client(h, r, max_application_input_bytes=b.count + offset)
        gate = ws.WikiSupportGate(h['tmp'] / 'checkpoint', 'byte-policy', c.registry, c.model_config_hash)
        assert gate.review(c).status == 'supported_candidate_not_published'
        assert call_count(h['tmp']) == 1
        assert len(list(gate.directory.glob('response-*.json'))) == 1


def test_old_none_measure_denies_and_exact_token_capability_keeps_its_unit(text_files):
    tmp, _runtime, _snapshot, _task = text_files
    _r, probes, invoke, _final = prepare_text(text_files, 'outcomes')
    missing = invoke(input_policy=None)
    assert missing.error_code == 'input_budget_unavailable'
    assert probes == [] and call_count(tmp) == 0
    result = invoke(input_policy=None, measure=measure)
    assert result.succeeded and call_count(tmp) == 1
    b = result.input_binding
    assert b.unit == 'tokens' and b.policy == typed.EXACT_TOKEN_POLICY
    assert b.tokenizer_version == 'fake-cli-byte-alphabet-v1'  # synthetic only
    assert b.max_application_input_bytes == typed.INPUT_LIMIT


@pytest.mark.parametrize('policy,cap,has_measure', [
    ('unknown', 10, False), (True, 10, False), (POLICY, True, False),
    (POLICY, 0, False), (POLICY, -1, False), (POLICY, float('inf'), False),
    (POLICY, typed.INPUT_LIMIT + 1, False), (POLICY, 10, True), (None, 10, True)])
def test_invalid_selection_units_and_limits_cannot_probe(text_files, policy, cap, has_measure):
    tmp, _runtime, _snapshot, _task = text_files
    _r, probes, invoke, _final = prepare_text(text_files, 'outcomes')
    result = invoke(input_policy=policy, max_application_input_bytes=cap,
                    measure=measure if has_measure else None)
    assert result.error_code == 'input_policy_invalid' and probes == [] and call_count(tmp) == 0


def test_exact_bytes_include_crlf_emoji_repeated_blocks_and_last_byte():
    prompt = ('English 中文😀\r\n重复块\r\n' * 5).encode() + b'Z'
    schema = {'type': 'object', 'description': '中文😀\r\n完整schema', 'additionalProperties': False}
    b = typed.admit_input(prompt, schema, None, input_policy=POLICY)
    assert b.count == len(prompt) + len(typed.encoded(schema))
    assert b.prompt_sha256 == hashlib.sha256(prompt).hexdigest()
    assert b.schema_sha256 == hashlib.sha256(typed.encoded(schema)).hexdigest()
    changed = typed.admit_input(prompt[:-1] + b'X', schema, None, input_policy=POLICY)
    assert b.count == changed.count and b.prompt_sha256 != changed.prompt_sha256
    with pytest.raises(typed.TypedError, match='typed_input_invalid'):
        typed.admit_input(prompt + b'\xff', schema, None, input_policy=POLICY)


def test_actual_schema_readback_drift_rejects_before_model_probe(text_files, monkeypatch):
    tmp, _runtime, _snapshot, _task = text_files
    _r, probes, invoke, _final = prepare_text(text_files, 'outcomes')
    original = typed.write_schema
    def corrupt(directory, schema):
        p = original(directory, schema)
        p.write_bytes(p.read_bytes() + b' ')
        return p
    monkeypatch.setattr(typed, 'write_schema', corrupt)
    assert invoke().error_code == 'typed_binding_invalid'
    assert probes == [] and call_count(tmp) == 0


def test_source_and_policy_drift_reject_without_fallback_or_source_loss(support_files):
    h = support_files
    r, probes = make_runner(h['tmp'])
    c = support_client(h, r)
    c.max_application_input_bytes -= 1
    assert r.check_support_json(c).error_code == 'typed_binding_invalid'
    assert probes == [] and call_count(h['tmp']) == 0
    c = support_client(h, r)
    raw = h['snapshot'].workspace / h['task'].raw[0].relative_path
    old = raw.read_bytes()
    configure(h['tmp'], envelope(c), mutate=f'Path({str(raw)!r}).write_bytes({old + b"changed"!r})\n')
    result = r.check_support_json(c)
    assert not result.succeeded and call_count(h['tmp']) == 1
    assert raw.read_bytes() == old + b'changed'  # failure preserves; never repairs/removes
    assert (h['tmp'] / 'input.bin').read_bytes() == c.prepared.prompt
    assert b'full_raw' in (h['tmp'] / 'input.bin').read_bytes()


def test_policy_config_stable_for_candidate_change_but_distinct_for_unit_or_cap(support_files):
    h = support_files
    r, _probes = make_runner(h['tmp'])
    c = support_client(h, r)
    exact = support_client(h, r, input_policy=None, measure=measure)
    smaller = support_client(h, r, max_application_input_bytes=typed.INPUT_LIMIT - 1)
    assert len({c.model_config_hash, exact.model_config_hash, smaller.model_config_hash}) == 3
    registry = h['registry']
    change = registry.changes[0]
    after = change.after.replace(b'10', b'11')
    (h['snapshot'].workspace / change.path).write_bytes(after)
    new_change = replace(change, after=after, after_sha256=typed.digest(after))
    newer = ws.build_registry(registry.staging_root, (new_change,), registry.raws)
    d = support_client(h, r, registry=newer)
    assert d.model_config_hash == c.model_config_hash
    assert d.prepared.measurement.prompt_sha256 != c.prepared.measurement.prompt_sha256
    assert d.prepared.binding != c.prepared.binding


@pytest.mark.parametrize('event,code', [
    ({'type': 'error', 'message': 'Unknown remote sensitive text'}, 'agent_failed'),
    ({'type': 'turn.failed', 'error': {'message': 'Do not log material'}}, 'agent_failed'),
    ({'type': 'turn.failed', 'error': {'code': 'context_window_exceeded'}}, 'context_limit_observed'),
    ({'method': 'error', 'params': {'error': {'codexErrorInfo': 'contextWindowExceeded'}}}, 'context_limit_observed'),
    ({'method': 'error', 'params': {'error': {'codexErrorInfo': 'sessionBudgetExceeded'}}}, 'context_limit_observed'),
    ({'method': 'error', 'params': {'error': {'codexErrorInfo': 'usageLimitExceeded'}}}, 'agent_failed'),
    ({'method': 'error', 'params': {'error': {'codexErrorInfo': 'futureUnknownCode'}}}, 'agent_failed'),
    ({'type': 'item.completed', 'item': {'type': 'context_compaction'}}, 'context_compaction_observed'),
    ({'method': 'item/started', 'params': {'item': {'type': 'contextCompaction'}}}, 'context_compaction_observed'),
    ({'method': 'thread/compacted', 'params': {}}, 'context_compaction_observed')])
def test_observable_failure_refuses_even_exit_zero_and_valid_final(text_files, event, code):
    tmp, _runtime, snapshot, task = text_files
    _r, _probes, invoke, final = prepare_text(text_files, 'check')
    before = [(snapshot.workspace / r.relative_path).read_bytes() for r in task.raw]
    configure(tmp, final, event=event)
    result = invoke()
    assert result.error_code == code and not result.succeeded and result.final_bytes is None
    assert call_count(tmp) == 1 and result.input_binding.unit == 'utf8_bytes'
    assert [(snapshot.workspace / r.relative_path).read_bytes() for r in task.raw] == before
    assert b'full_raw' in (tmp / 'input.bin').read_bytes()


def test_source_strings_resembling_error_events_are_only_material(text_files):
    tmp, _runtime, _snapshot, _task = text_files
    _r, _probes, invoke, final = prepare_text(text_files, 'check')
    event = {'type': 'item.completed', 'item': {'type': 'agent_message',
        'text': 'error turn.failed contextWindowExceeded contextCompaction {"type":"error"}'}}
    configure(tmp, final, event=event)
    assert invoke().succeeded and call_count(tmp) == 1


def test_large_unsent_protected_binary_is_streamed_and_tail_drift_is_rejected(support_files, monkeypatch):
    h = support_files
    relative = 'raw/attachments/protected.bin'
    p = h['snapshot'].workspace / relative
    p.parent.mkdir()
    chunk = b'P' * 65536
    hasher = hashlib.sha256()
    with p.open('wb') as stream:
        for _ in range(typed.INPUT_LIMIT // len(chunk) + 1):
            stream.write(chunk); hasher.update(chunk)
    size = p.stat().st_size
    assert size > typed.INPUT_LIMIT
    h['snapshot'] = replace(h['snapshot'], files=h['snapshot'].files + (
        SnapshotFile(relative, 'raw', size, hasher.hexdigest()),))
    requests = []
    original = typed.os.read
    identity = p.stat()
    def read(fd, amount):
        held = typed.os.fstat(fd)
        if (held.st_dev, held.st_ino) == (identity.st_dev, identity.st_ino):
            requests.append(amount)
        return original(fd, amount)
    monkeypatch.setattr(typed.os, 'read', read)
    r, probes = make_runner(h['tmp'])
    c = support_client(h, r, max_application_input_bytes=65536)
    inputs = json.loads(c.prepared.prompt[c.prepared.prompt.index(b'{"binding"'):])['inputs']
    row = next(row for row in inputs['current_files'] if row['path'] == relative)
    assert row == {'path': relative, 'sha256': hasher.hexdigest(), 'byte_count': size}
    assert c.prepared.measurement.count < 65536 and c.prepared.measurement.unit == 'utf8_bytes'
    assert max(requests) <= 65536 and len(requests) > typed.INPUT_LIMIT // len(chunk)
    assert probes == []
    configure(h['tmp'], envelope(c))
    assert r.check_support_json(c).succeeded
    assert (h['tmp'] / 'input.bin').read_bytes() == c.prepared.prompt
    # Same size, different tail: full streaming hash must notice, not stat-only.
    with p.open('r+b') as stream:
        stream.seek(-1, 2); stream.write(b'Q')
    count = call_count(h['tmp'])
    result = r.check_support_json(c)
    assert result.error_code == 'typed_binding_invalid' and not result.succeeded
    assert call_count(h['tmp']) == count and p.stat().st_size == size
    with p.open('rb') as stream:
        stream.seek(-1, 2); assert stream.read() == b'Q'
