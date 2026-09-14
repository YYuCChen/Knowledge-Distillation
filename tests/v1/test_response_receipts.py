"""SC-JSON-ENVELOPE identity, interruption and late-response counterexamples."""
import json
from concurrent.futures import ThreadPoolExecutor
import pytest
from knowledge_distiller.v1.response_receipts import ResponseReceipts


def record(tmp_path, **changes):
    return ResponseReceipts(tmp_path, **dict(operation='test', source='source',
        contract={'schema': 1}, model_identity={'model': 'fixture'}, **changes))


def receive(r, text='{}'):
    request = r.begin()
    r.receive(request, text)
    return request


def test_received_prepared_and_restart_reparse_without_claiming_commit(tmp_path):
    r = record(tmp_path)
    receive(r)
    assert record(tmp_path).pending() == '{}'
    r.mark('{}', 'prepared')
    assert record(tmp_path).pending() == '{}'
    assert json.loads((tmp_path / 'pending.json').read_text())['state'] == 'prepared'
    with pytest.raises(ValueError):
        r.mark('{}', 'committed')


@pytest.mark.parametrize('field,value', [
    ('source', 'other'), ('source_version_id', 'other-version'), ('operation', 'other'),
    ('contract', {'schema': 2}), ('model_identity', {'model': 'other'}),
    ('requested_fields', ('title',)), ('parent_response_hash', 'a' * 64),
])
def test_changed_identity_never_reuses(tmp_path, field, value):
    receive(record(tmp_path))
    args = dict(operation='test', source='source', contract={'schema': 1}, model_identity={'model': 'fixture'})
    args[field] = value
    changed = ResponseReceipts(tmp_path, **args)
    assert changed.pending() is None
    assert changed.last_diagnostic == 'response_identity_unproven'


@pytest.mark.parametrize('count', [1, 3])
def test_orphan_files_never_guessed(tmp_path, count):
    r = record(tmp_path)
    for _ in range(count):
        receive(r)
    (tmp_path / 'pending.json').unlink()
    assert r.pending() is None
    assert len(list(tmp_path.glob('*.json'))) == count + 1


def test_saved_bytes_before_pointer_failure_not_reported_recoverable(tmp_path, monkeypatch):
    r = record(tmp_path)
    request = r.begin()
    original = r._write
    def fail(name, value):
        if name == 'pending.json':
            raise OSError('injected interrupted publication')
        original(name, value)
    monkeypatch.setattr(r, '_write', fail)
    with pytest.raises(OSError):
        r.receive(request, '{}')
    assert (tmp_path / (request + '.json')).exists()
    assert record(tmp_path).pending() is None


def test_late_old_response_cannot_take_over_new_request(tmp_path):
    r = record(tmp_path)
    old, current = r.begin(), r.begin()
    r.receive(current, '{"new":true}')
    with pytest.raises(ValueError, match='superseded'):
        r.receive(old, '{"old":true}')
    assert r.pending() == '{"new":true}'


def test_same_request_second_result_cannot_replace_first(tmp_path):
    r = record(tmp_path)
    request = receive(r)
    r.receive(request, '{}')
    with pytest.raises(ValueError, match='already_received'):
        r.receive(request, '[]')
    assert r.pending() == '{}'


def test_same_failure_is_not_an_infinite_reparse_loop(tmp_path):
    r = record(tmp_path)
    receive(r, 'bad json')
    r.mark('bad json', 'parse_failed', category='json_syntax_invalid')
    assert record(tmp_path).pending() is None
    # A changed validator may retry the exact proven response, once.
    changed = record(tmp_path, validator_version='2')
    assert changed.pending() == 'bad json'
    changed.mark('bad json', 'parse_failed')
    assert changed.pending() is None


def test_hash_tamper_and_old_pending_identity_are_rejected(tmp_path):
    r = record(tmp_path)
    request = receive(r)
    path = tmp_path / (request + '.json')
    saved = json.loads(path.read_text())
    saved['text'] = '[]'
    path.write_text(json.dumps(saved))
    assert r.pending() is None
    (tmp_path / 'pending.json').write_text(json.dumps({'identity': 'old', 'response': 'a'*64}))
    assert r.pending() is None


def test_concurrent_duplicate_receive_is_single_owned_response(tmp_path):
    r = record(tmp_path)
    request = r.begin()
    def attempt():
        try:
            record(tmp_path).receive(request, '{}')
            return 'received'
        except BlockingIOError:
            return 'busy'
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: attempt(), range(2)))
    assert 'received' in results
    assert r.pending() == '{}'
    assert len(list(tmp_path.glob(request + '.json'))) == 1
