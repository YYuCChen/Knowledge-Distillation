"""Private publication readback only; real kit/files/publisher, synthetic support."""
from dataclasses import replace
import json
from pathlib import Path

import pytest

from knowledge_distiller.v1 import wiki_support as ws
from knowledge_distiller.v1.ingestion import digest
from knowledge_distiller.v1.wiki_outcomes import OutcomeError, WikiOutcomes
from knowledge_distiller.v1.wiki_publish import publish_wiki, PublishExpectation, PublishState
from knowledge_distiller.v1.wiki_staging import WikiStagingError
from .test_wiki_outcomes import _publish_fixture, Checker, no_knowledge
from .test_wiki_support import FakeClient


@pytest.fixture
def h(tmp_path):
    outcomes = WikiOutcomes(tmp_path / 'candidate.sqlite3')
    outcomes.initialize()
    vault, store, task, lock, snapshot, journal, rid, expected = _publish_fixture(tmp_path, outcomes)
    try:
        yield dict(outcomes=outcomes, vault=vault, store=store, task=task, lock=lock,
                   snapshot=snapshot, journal=journal, rid=rid, expected=expected)
    finally:
        lock.close()


def readback(h, **kwargs):
    return h['outcomes']._verified_publication(h['rid'], task_store=h['store'],
        snapshot=h['snapshot'], journal=h['journal'], lock=h['lock'], **kwargs)


def publish(h):
    result = publish_wiki(h['vault'], h['snapshot'].workspace, h['journal'],
                          h['expected'], lock=h['lock'])
    assert result.state == PublishState.COMMITTED
    return result


def not_accepted(h):
    with pytest.raises(OutcomeError, match='receipt_missing'):
        h['outcomes'].get(h['rid'], 'accepted')


@pytest.mark.parametrize('state,completed,allowed', [
    ('publishing', True, False), ('publishing', False, True),
    ('succeeded', True, True), ('succeeded', False, False),
    ('failed', True, False), ('failed', False, False),
])
def test_actual_batch_state_boundary_and_readonly_result(h, state, completed, allowed):
    publish(h)
    if state == 'succeeded':
        h['store'].mark_batch_readback_succeeded(h['task'].task_id, 1)
    elif state == 'failed':
        h['store'].fail_batch(h['task'].task_id, 1, 'agent_failed')
    candidate_before = h['outcomes'].get(h['rid'])
    task_before = h['store'].get(h['task'].task_id)
    journal_before = (h['journal'] / 'journal.json').read_bytes()
    if allowed:
        result = readback(h, require_completed_batch=completed)
        assert result['journal_sha256'] == digest(journal_before)
        assert result['published_after'] == {p: e.after_sha256 for p, e in h['expected'].items()}
    else:
        with pytest.raises(OutcomeError, match='readback_required'):
            readback(h, require_completed_batch=completed)
    assert h['outcomes'].get(h['rid']) == candidate_before
    assert h['store'].get(h['task'].task_id) == task_before
    assert (h['journal'] / 'journal.json').read_bytes() == journal_before
    not_accepted(h)


def test_accept_has_no_publishing_bypass_and_retains_exact_saved_receipt(h):
    publish(h)
    verified = readback(h, require_completed_batch=False)
    with pytest.raises(OutcomeError, match='readback_required'):
        h['outcomes'].accept(h['rid'], task_store=h['store'], snapshot=h['snapshot'],
                             journal=h['journal'], lock=h['lock'])
    not_accepted(h)
    h['store'].mark_batch_readback_succeeded(h['task'].task_id, 1)
    assert readback(h) == verified
    accepted = h['outcomes'].accept(h['rid'], task_store=h['store'], snapshot=h['snapshot'],
                                    journal=h['journal'], lock=h['lock'])
    assert accepted == verified == h['outcomes'].get(h['rid'], 'accepted')
    assert WikiOutcomes(h['outcomes'].path).accept(h['rid'], task_store=h['store'],
        snapshot=h['snapshot'], journal=h['journal'], lock=h['lock']) == accepted


def test_missing_journal_cannot_verify_publishing_batch(h):
    with pytest.raises(ValueError):
        readback(h, require_completed_batch=False)
    not_accepted(h)


@pytest.mark.parametrize('change,code', [
    ('uncommitted', 'committed_publish_required'),
    ('unverified', 'committed_publish_required'),
    ('duplicate_path', 'journal_duplicate_path'),
    ('duplicate_key', 'journal_duplicate_key'),
    ('nested_duplicate_key', 'journal_duplicate_key'),
    ('float', 'journal_invalid'), ('bool_version', 'journal_invalid'),
    ('unknown_field', 'journal_invalid'), ('item_wrong_type', 'journal_invalid'),
])
def test_actual_journal_mutation_is_rejected_without_private_accept(h, change, code):
    publish(h)
    path = h['journal'] / 'journal.json'
    data = json.loads(path.read_bytes())
    if change == 'uncommitted':
        data['state'] = 'publishing'
    elif change == 'unverified':
        data['items'][0]['status'] = 'replaced'
    elif change == 'duplicate_path':
        data['items'].append(dict(data['items'][0]))
    elif change == 'float':
        data['items'][0]['before_mode'] = 1.5
    elif change == 'bool_version':
        data['version'] = True
    elif change == 'unknown_field':
        data['green'] = True
    elif change == 'item_wrong_type':
        data['items'][0]['status'] = {'verified': True}
    content = json.dumps(data).encode()
    if change == 'duplicate_key':
        content = b'{"state":"publishing",' + content[1:]
    elif change == 'nested_duplicate_key':
        content = content.replace(b'"status":', b'"status":"pending","status":', 1)
    path.write_bytes(content)
    with pytest.raises(OutcomeError, match=code):
        readback(h, require_completed_batch=False)
    assert path.read_bytes() == content
    not_accepted(h)


@pytest.mark.parametrize('kind', ['document', 'raw'])
def test_actual_formal_bytes_drift_is_not_repaired(h, kind):
    publish(h)
    relative = 'wiki/log.md' if kind == 'document' else h['task'].raw[0].relative_path
    path = h['vault'] / relative
    changed = path.read_bytes() + b'\nUnapproved synthetic tail.\n'
    path.write_bytes(changed)
    with pytest.raises(WikiStagingError, match='publish_conflict|raw_changed'):
        readback(h, require_completed_batch=False)
    assert path.read_bytes() == changed
    not_accepted(h)


def test_candidate_database_cannot_be_task_database(h):
    original = h['store'].database_path
    try:
        h['store'].database_path = h['outcomes'].path
        with pytest.raises(OutcomeError, match='candidate_database_required'):
            readback(h, require_completed_batch=False)
    finally:
        h['store'].database_path = original
    not_accepted(h)


def test_second_journal_read_detects_change_after_real_formal_verification(h, monkeypatch):
    from knowledge_distiller.v1 import wiki_outcomes as module
    publish(h)
    original = module.verify_recovered_publish
    path = h['journal'] / 'journal.json'
    def verify_then_change(*args, **kwargs):
        result = original(*args, **kwargs)
        path.write_bytes(path.read_bytes() + b'\n')
        return result
    monkeypatch.setattr(module, 'verify_recovered_publish', verify_then_change)
    with pytest.raises(OutcomeError, match='journal_changed'):
        readback(h, require_completed_batch=False)
    not_accepted(h)


def test_actual_r14_receipt_drift_rejects_publication_readback(h, tmp_path):
    raw = h['task'].raw[0]
    raw_bytes = (h['vault'] / raw.relative_path).read_bytes()
    path = 'wiki/来源/合成支持.md'
    content = ('## 核心论点\n\n合成素材陈述已保留原话'
               f'（{raw.relative_path}#^source-1）。\n').encode()
    target = h['snapshot'].workspace / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    changes = [ws.DocumentChange(path, None, None, content, digest(content))]
    for relative, expectation in h['expected'].items():
        before = (h['vault'] / relative).read_bytes()
        if relative == 'wiki/log.md':
            log = before.replace('# 变更日志'.encode(), b'# log', 1)
            log += ('\n## 结果\n\nSynthetic private no-knowledge process receipt.'
                    f'（{raw.relative_path}#^source-1）。\n').encode()
            (h['snapshot'].workspace / relative).write_bytes(log)
            expectation = PublishExpectation(expectation.before_sha256, digest(log))
            h['expected'][relative] = expectation
        after = (h['snapshot'].workspace / relative).read_bytes()
        changes.append(ws.DocumentChange(relative, before, expectation.before_sha256,
                                          after, expectation.after_sha256))
    registry = ws.build_registry(h['snapshot'].workspace, tuple(changes),
        (ws.FrozenRaw(raw.relative_path, raw.raw_id, raw_bytes, raw.content_sha256),))
    gate = ws.WikiSupportGate(tmp_path / 'support', 'publication_readback', registry, 'e' * 64)
    client = FakeClient()
    proposed = replace(no_knowledge(h['task'], raw), status='processed_with_knowledge',
        reason_code='', reason='', documents=tuple((c.path, c.after_sha256) for c in changes))
    h['rid'] = h['outcomes'].validate(h['task'], 1, {raw.raw_id: raw_bytes}, (proposed,),
        checker=Checker(), support_gate=gate, support_client=client)
    assert client.calls and registry.claims
    h['expected'][path] = PublishExpectation(None, digest(content))
    publish(h)
    readback(h, require_completed_batch=False)
    receipt = Path(h['outcomes'].get(h['rid'])['support']['receipt_path'])
    changed = receipt.read_bytes() + b'\nSynthetic receipt drift.\n'
    receipt.write_bytes(changed)
    with pytest.raises(OutcomeError, match='source_support_receipt_changed'):
        readback(h, require_completed_batch=False)
    assert receipt.read_bytes() == changed
    not_accepted(h)
