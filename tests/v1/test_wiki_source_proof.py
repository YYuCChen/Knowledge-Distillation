"""Schema25 synthetic API fixtures; run in the pinned 58bc8ee closure.

No proof is mocked, no SQL inserts raw_verified, no preparation/acceptance
flags are invented. Only the existing source fact, raw writer and capture APIs
produce provenance. These tests do not exercise models or production paths.
"""
import base64
from dataclasses import replace
import hashlib
from pathlib import Path
import shutil

import pytest

from knowledge_distiller.v1 import raw, wiki_source_proof as proof
from knowledge_distiller.v1.captures import record_capture
from knowledge_distiller.v1.database import connect, INGESTION_CONTRACT
from knowledge_distiller.v1.file_sources import prepare_direct_text, SubmittedSource
from knowledge_distiller.v1.ingestion import Ingestion
from knowledge_distiller.v1.source_parsing import ParsedMedia, ParsedSource
from knowledge_distiller.v1.store import Store
from knowledge_distiller.v1.wiki_lock import VaultWriteLock, vault_key
from knowledge_distiller.v1.wiki_staging import StagingSnapshot, SnapshotFile
from knowledge_distiller.v1.wiki_tasks import FrozenRaw, WikiBatch, WikiTask, _boundary

PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jV1sAAAAASUVORK5CYII=')
TEXT = '完整合成来源；不是实际资料。'


def sha(data):
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def world(tmp_path):
    root = tmp_path.resolve()  # macOS /private path, not a symlink root
    root.chmod(0o700)
    store = Store(root / 'synthetic.sqlite3')
    store.initialize()  # only this disposable fixture, never the producer
    vault = root / 'vault'
    vault.mkdir(mode=0o700)
    store.set_setting('vault_path', str(vault))
    return store, vault, Ingestion(store)


def material(world, *, images=True, canonical=True, kind='direct_text', write_raw=True):
    store, vault, ingestion = world
    source = prepare_direct_text(TEXT)
    if kind != 'direct_text':
        # Explicit controlled parser output; this is not a PDF completeness
        # claim and does not call a converter/model or manufacture proof.
        source = SubmittedSource(kind, sha(b'synthetic original'), 'synthetic.pdf',
                                 b'synthetic original', {})
    item = store.submit_source(source, ingestion_contract=INGESTION_CONTRACT,
        source_binding_sha256=sha(source.content),
        relation_binding_sha256=sha(b'synthetic no selected relations'))
    parsed = ParsedSource(TEXT, {'source_title': 'SECRET-TITLE', 'author': 'SECRET-AUTHOR'},
                          {'fixture_contract': 'controlled-synthetic-source-v1'},
                          (ParsedMedia('image-1', 'image/png', PNG),) if images else ())
    store.establish_submitted_fact(item, source, parsed)
    mid = store.item_bundle(item)['material_id']
    if not write_raw:
        return item, mid, None
    if canonical:
        receipt = ingestion.material(mid, vault, item_id=item)
        record = ingestion.ledger.record(receipt.raw_id)
    else:
        record = ingestion.ledger.ensure_material(mid)
        assert ingestion.ledger.write(record, vault) in {'placed', 'already'}
    return item, mid, record


def frozen(world, records):
    """Actual copied staging files, including the otherwise missing附件 tree.

    Task/snapshot dataclasses freeze the application's source boundary; no task
    acceptance, publication or source-complete row is fabricated.
    """
    _, vault, _ = world
    root = vault.parent / 'staging'
    root.mkdir(mode=0o700)
    workspace = root / 'workspace'
    workspace.mkdir(mode=0o700)
    entries = []
    for branch in ('raw', '附件'):
        if (vault / branch).exists():
            shutil.copytree(vault / branch, workspace / branch)
    for path in sorted(workspace.rglob('*')):
        if path.is_file():
            data = path.read_bytes()
            relative = path.relative_to(workspace).as_posix()
            entries.append(SnapshotFile(relative, 'raw' if relative.startswith('raw/') else 'attachment',
                                        len(data), sha(data)))
    rows = tuple(FrozenRaw(r['relative_path'], r['raw_id'], r['identity'],
        len(r['content'].encode()), r['content_sha256'], n, 1) for n, r in enumerate(records, 1))
    task = WikiTask('a' * 32, str(vault), vault_key(vault), 'manual', 'synthetic',
        'codex', 'generation-unchanged', 'medium', 'synthetic-kit', 'b' * 64,
        _boundary((rows,)), 'running', len(rows), 1, 0, None, 'active', 'running', '', '',
        (WikiBatch(1, 'running', len(rows), None),), rows)
    snapshot = StagingSnapshot(task.task_id, root, workspace, root / 'control', root / 'backup',
                               tuple(entries), tuple(r.raw_id for r in rows))
    context = tuple((r, (vault / r.relative_path).read_bytes()) for r in rows)
    return task, snapshot, context


def verify(world, inputs, **options):
    store, vault, _ = world
    with VaultWriteLock.acquire(vault) as lock:
        return proof.verify_wiki_sources(store, *inputs, lock, **options)


def source(result):
    return result.manifest['sources'][0]


def test_canonical_actual_event_full_attachment_determinism_and_private_descriptor(world):
    _, _, record = material(world)
    inputs = frozen(world, (record,))
    first, second = verify(world, inputs), verify(world, inputs)
    assert first == second and first.digest == sha(first.manifest_bytes)
    s = source(first)
    assert {'canonical_ingestion_event', 'canonical_source_binding', 'declared_capture_verified'} <= set(s['capabilities'])
    assert s['gaps'] == [] and s['event_keys']
    assert s['attachments'][0]['sha256'] == sha(PNG)
    assert s['attachments'][0]['byte_count'] == len(PNG)
    assert not s['scope']['platform_total_verified']
    for secret in (TEXT, 'SECRET-TITLE', 'SECRET-AUTHOR', 'created_at', 'updated_at', 'synthetic original'):
        assert secret.encode() not in first.manifest_bytes
    mutated_view = first.manifest
    mutated_view['sources'].clear()
    assert first.manifest['sources']
    store, vault, _ = world
    with VaultWriteLock.acquire(vault) as lock:
        callback = proof.trusted_source_callback(store, lock)
        kw = dict(zip(('task', 'snapshot', 'context'), inputs))
        assert callback(**kw) == callback.verify(**kw).digest == first.digest


def test_legacy_record_without_internal_event_is_not_upgraded(world):
    _, _, record = material(world, canonical=False)
    s = source(verify(world, frozen(world, (record,))))
    assert s['subject']['kind'] == 'material'
    assert s['gaps'] == ['canonical_event_unavailable']
    assert 'canonical_ingestion_event' not in s['capabilities']


@pytest.mark.parametrize('damage', ['missing', 'ineffective_with_comment'])
def test_exact_proof_guard_required_without_changing_existing_provenance(world, damage):
    _, _, record = material(world)
    inputs = frozen(world, (record,))
    old = verify(world, inputs)
    assert 'canonical_ingestion_event' in source(old)['capabilities']
    store, vault, _ = world
    def rows():
        with connect(store.path) as db:
            return {table: tuple(tuple(r) for r in db.execute('SELECT * FROM ' + table))
                    for table in ('raw_records', 'source_facts', 'source_media',
                                  'distill_items', 'ingestion_events')}
    retained = rows()
    # Deliberately damage only this disposable DB's schema, after genuine
    # Ingestion proof production. Do not insert/update any proof or owner row.
    with connect(store.path) as db:
        db.execute('DROP TRIGGER ingestion_events_proof_unavailable')
        if damage == 'ineffective_with_comment':
            db.execute('''CREATE TRIGGER ingestion_events_proof_unavailable
                BEFORE INSERT ON ingestion_events
                BEGIN SELECT 1; /* ingestion_proof: not an enforced guard */ END''')
    assert rows() == retained
    before = {p: (sha(p.read_bytes()), p.stat().st_ino)
              for p in vault.parent.rglob('*') if p.is_file()}
    result = verify(world, inputs)
    s = source(result)
    assert result.digest != old.digest
    assert s['capabilities'] == ['declared_attachments_readback', 'literal_text']
    assert 'internal_event_guard_unavailable' in s['gaps']
    assert s['event_keys'] == [] and s['source_binding_sha256'] is None
    assert rows() == retained
    assert {p: (sha(p.read_bytes()), p.stat().st_ino)
            for p in vault.parent.rglob('*') if p.is_file()} == before


def literal(world, **fields):
    _, vault, _ = world
    rid = 'R-20261008-9999'
    relative = 'raw/外部/2026/10/' + rid + '.md'
    content = '\n'.join(raw.envelope(list({'编号': rid, '身份': '第三方', '格式版本': 1,
        '渠道': '直接文本', '应用记录': {'material_id': 999999}, **fields}.items())) + ['', TEXT, ''])
    path = vault / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='utf-8')
    return {'raw_id': rid, 'relative_path': relative, 'identity': '第三方',
            'content': content, 'content_sha256': sha(content.encode())}


@pytest.mark.parametrize('fields,gap', [
    ({'覆盖范围': {'status': 'partial', 'scope': 'authorized first page'}}, None),
    ({'截断': True}, 'capture_truncated'),
    ({'未保留附件': ['image-absent']}, 'known_missing_attachment'),
    ({'身份判定': {'结果': 'unknown'}}, 'identity_unresolved'),
    ({'渠道': 'PDF'}, 'original_file_unproven'),
])
def test_no_record_partial_and_known_gaps_never_guess_subject(world, fields, gap):
    s = source(verify(world, frozen(world, (literal(world, **fields),))))
    assert s['subject'] is None and 'ledger_record_missing' in s['gaps']
    assert s['event_keys'] == [] and s['source_binding_sha256'] is None
    if gap:
        assert gap in s['gaps']
    else:
        assert s['scope']['kind'] == 'declared_partial'
        assert 'capture_truncated' not in s['gaps']
    assert 'declared_capture_verified' not in s['capabilities']


def test_canonical_pdf_text_does_not_prove_original_pdf_or_no_knowledge(world):
    _, _, record = material(world, images=False, kind='pdf')
    s = source(verify(world, frozen(world, (record,))))
    assert 'canonical_ingestion_event' in s['capabilities']
    assert 'original_file_unproven' in s['gaps']
    assert 'declared_capture_verified' not in s['capabilities']


@pytest.mark.parametrize('damage', ['formal_raw', 'staged_raw', 'attachment', 'missing', 'extra', 'symlink'])
def test_actual_copies_and_complete_attachment_inventory_reject_damage(world, damage):
    _, _, record = material(world)
    inputs = frozen(world, (record,))
    _, snapshot, _ = inputs
    attachment = next(f.relative_path for f in snapshot.files if f.role == 'attachment')
    vault = world[1]
    target = (vault / record['relative_path'] if damage == 'formal_raw' else
              snapshot.workspace / record['relative_path'] if damage == 'staged_raw' else
              snapshot.workspace / attachment)
    if damage == 'missing':
        target.unlink()
    elif damage == 'extra':
        (target.parent / 'unregistered.png').write_bytes(PNG)
    elif damage == 'symlink':
        target.unlink()
        target.symlink_to(vault / attachment)
    else:
        target.write_bytes(b'changed synthetic bytes')
    with pytest.raises(proof.SourceProofError):
        verify(world, inputs)


def test_duplicate_id_and_duplicate_frozen_context_reject(world):
    _, _, record = material(world, images=False)
    inputs = frozen(world, (record,))
    task, snapshot, context = inputs
    with pytest.raises(proof.SourceProofError, match='source_boundary_invalid'):
        verify(world, (task, snapshot, context + context))
    other = world[1] / 'raw/自述/2026/10' / (record['raw_id'] + '.md')
    other.parent.mkdir(parents=True)
    other.write_bytes(context[0][1])
    with pytest.raises(proof.SourceProofError, match='source_identity_invalid'):
        verify(world, inputs)


def test_duplicate_declared_attachment_rejects_instead_of_deduplicating(world):
    record = literal(world)
    relative = '附件/raw/' + record['raw_id'] + '/image-1.png'
    content = record['content'] + ('![[' + relative + ']]\n') * 2
    record = {**record, 'content': content, 'content_sha256': sha(content.encode())}
    (world[1] / record['relative_path']).write_text(content, encoding='utf-8')
    with pytest.raises(proof.SourceProofError, match='source_attachment_set_invalid'):
        verify(world, frozen(world, (record,)))


def test_full_task_context_cannot_be_reduced_to_a_green_prefix(world):
    _, _, first = material(world, images=False)
    _, second = capture(world, 'second-source')
    task, snapshot, context = frozen(world, (first, second))
    assert len(verify(world, (task, snapshot, context)).manifest['sources']) == 2
    with pytest.raises(proof.SourceProofError, match='source_boundary_invalid'):
        verify(world, (task, snapshot, context[:1]))


def test_owner_human_pending_invalidates_old_green(world):
    item, _, record = material(world)
    inputs = frozen(world, (record,))
    old = verify(world, inputs)
    world[0].mark_waiting(item, {'kind': 'manual', 'snapshot': TEXT, 'concerns': [], 'resolved': []})
    changed = verify(world, inputs)
    assert changed.digest != old.digest
    assert 'canonical_ingestion_event' not in source(changed)['capabilities']
    assert 'current_source_unqualified' in source(changed)['gaps']


def capture(world, message, decision='my_thought', target=None, *, item_id=None):
    store, vault, ingestion = world
    with connect(store.path) as db:
        record_capture(db, 'synthetic-app', message, message_type='text',
                       created_ms=1790000000000, received_ms=1790000000000,
                       text='合成本人原话 ' + message, vault=vault)
    cid = ingestion.captures.for_message('synthetic-app', message)['capture_id']
    if item_id is not None:
        assert ingestion.captures.get(cid)['item_id'] is None
        assert ingestion.events('capture:' + str(cid)) == []
        ingestion.captures._link(cid, item_id)  # first binding, before proof
        assert ingestion.captures.get(cid)['item_id'] == item_id
    ingestion.captures.decide(cid, decision, target=target)
    if item_id is not None:
        assert ingestion.captures.get(cid)['item_id'] == item_id
    receipt = ingestion.capture(cid, vault)
    if item_id is not None:
        assert ingestion.captures.get(cid)['item_id'] == item_id
        assert any(e[0] == 'raw_verified' for e in ingestion.events('capture:' + str(cid)))
    return cid, ingestion.ledger.record(receipt.raw_id)


@pytest.mark.parametrize('change', ['target', 'head'])
def test_actual_capture_target_decision_and_head_drift_change_proof(world, change):
    capture(world, 'target-one')
    capture(world, 'target-two')
    cid, record = capture(world, 'annotation', 'annotation', 'target-one')
    inputs = frozen(world, (record,))
    old = verify(world, inputs)
    assert 'canonical_ingestion_event' in source(old)['capabilities']
    if change == 'target':
        world[2].captures.decide(cid, 'annotation', target='target-two')
    else:
        world[2].captures.decide(cid, 'my_thought')  # real supersession API
    new = verify(world, inputs)
    assert old.digest != new.digest
    assert 'canonical_ingestion_event' not in source(new)['capabilities']


def test_capture_media_budget_checks_actual_owner_before_loading_blob(world, monkeypatch):
    item, mid, record_before = material(world, write_raw=False)
    assert record_before is None and world[2].ledger.heads('material', mid) == ()
    assert world[0].item_bundle(item)['source_fact_id'] is not None
    assert world[0].media_members(mid)[0]['content'] == PNG
    capture(world, 'unrelated-capture')
    cid, record = capture(world, 'owned-capture', item_id=item)
    assert cid != mid  # querying by capture ID would incorrectly see zero bytes
    ingestion = world[2]
    assert ingestion.captures.get(cid)['item_id'] == item
    assert world[0].item_bundle(item)['material_id'] == mid
    assert ingestion.ledger.heads('material', mid) == ()
    events_before = ingestion.events('capture:' + str(cid))
    original = ingestion._rows
    blob_reads = []
    def observing(db, sql, parameters=()):
        if sql.startswith('SELECT * FROM source_media'):
            blob_reads.append(parameters)
        return original(db, sql, parameters)
    monkeypatch.setattr(ingestion, '_rows', observing)
    with connect(world[0].path) as db:
        with pytest.raises(proof.SourceProofError, match='source_input_limit'):
            proof._record_observation(ingestion, db, record, len(PNG) - 1)
    assert blob_reads == []  # actual owner sum rejects before the BLOB SELECT
    assert ingestion.events('capture:' + str(cid)) == events_before
    assert ingestion.captures.get(cid)['item_id'] == item
    assert world[0].item_bundle(item)['material_id'] == mid
    assert ingestion.ledger.heads('material', mid) == ()


def test_source_owner_binding_drift_and_two_pass_cas(world, monkeypatch):
    item, _, record = material(world, images=False)
    world[0].mark_failed(item, phase='collecting', error_code='synthetic_source_drift_fixture')
    owner = world[0].item_bundle(item)
    assert owner['state'] == 'failed' and owner['dismissed_at'] is None
    inputs = frozen(world, (record,))
    raw_before = (world[1] / record['relative_path']).read_bytes()
    original = Ingestion._validate_context
    observe = proof._record_observation
    observations = []
    def observing(*args):
        result = observe(*args)
        observations.append(result[1])
        return result
    monkeypatch.setattr(proof, '_record_observation', observing)
    calls = []
    def drifting(self, *args):
        result = original(self, *args)
        if not calls:
            calls.append(True)
            self.store.dismiss_item(item)  # public mutation between observations
            assert self.store.item_bundle(item)['dismissed_at'] is not None
        return result
    monkeypatch.setattr(Ingestion, '_validate_context', drifting)
    with pytest.raises(proof.SourceProofError, match='source_changed'):
        verify(world, inputs)
    assert calls == [True]
    assert len(observations) == 2 and observations[0] != observations[1]
    assert (world[1] / record['relative_path']).read_bytes() == raw_before
    assert (inputs[1].workspace / record['relative_path']).read_bytes() == raw_before


def test_fresh_read_budget_and_no_lazy_cache(world, monkeypatch):
    _, _, record = material(world, images=False, canonical=False)
    inputs = frozen(world, (record,))
    original = proof.read_regular
    reads = []
    def reading(root, path):
        reads.append(path)
        return original(root, path)
    monkeypatch.setattr(proof, 'read_regular', reading)
    with pytest.raises(proof.SourceProofError, match='source_input_limit'):
        verify(world, inputs, max_bytes=1)
    assert reads == []  # size refusal before raw bytes are loaded
    first = verify(world, inputs)
    count = len(reads)
    assert verify(world, inputs) == first and len(reads) > count
    target = inputs[1].workspace / record['relative_path']
    target.write_bytes(target.read_bytes() + b'changed')
    with pytest.raises(proof.SourceProofError):
        verify(world, inputs)


def test_file_changed_between_size_check_and_read_is_not_certified(world, monkeypatch):
    _, _, record = material(world, images=False)
    inputs = frozen(world, (record,))
    original = proof.read_regular
    done = []
    def changing(root, relative):
        if not done:
            done.append(True)
            target = root / relative
            target.write_bytes(target.read_bytes() + b'changed')
        return original(root, relative)
    monkeypatch.setattr(proof, 'read_regular', changing)
    with pytest.raises(proof.SourceProofError, match='source_changed'):
        verify(world, inputs)


def test_verification_never_initializes_allocates_writes_events_or_releases(world, monkeypatch):
    _, _, record = material(world)
    inputs = frozen(world, (record,))
    store, vault, ingestion = world
    def inventory():
        return {p.relative_to(vault.parent).as_posix(): (sha(p.read_bytes()), p.stat().st_ino)
                for p in vault.parent.rglob('*') if p.is_file()}
    before = inventory()
    events = ingestion.events('material:' + str(record['subject_id']))
    def forbidden(*args, **kwargs):
        pytest.fail('read-only proof called a writer')
    for cls, names in ((Store, ('initialize', 'submit_source', 'establish_submitted_fact',
                               'append_ingestion_event', 'set_setting')),
                       (Ingestion, ('initialize', '_insert_proven', '_complete', 'release_material', 'release_capture')),
                       (raw.RawLedger, ('ensure_material', 'supersede', 'write'))):
        for name in names:
            monkeypatch.setattr(cls, name, forbidden)
    monkeypatch.setattr(raw, 'allocate', forbidden)
    verify(world, inputs)
    assert inventory() == before
    assert ingestion.events('material:' + str(record['subject_id'])) == events


def test_path_escape_and_closed_lock_reject_without_database_creation(world):
    _, _, record = material(world, images=False)
    task, snapshot, context = frozen(world, (record,))
    bad = replace(task.raw[0], relative_path='../outside.md')
    changed_task = replace(task, raw=(bad,), boundary_sha256=_boundary(((bad,),)))
    with pytest.raises(proof.SourceProofError):
        verify(world, (changed_task, snapshot, ((bad, context[0][1]),)))
    with VaultWriteLock.acquire(world[1]) as lock:
        lock.close()
        with pytest.raises(proof.SourceProofError, match='source_boundary_invalid'):
            proof.verify_wiki_sources(world[0], task, snapshot, context, lock)
    absent = Store(world[1].parent / 'absent.sqlite3')
    with VaultWriteLock.acquire(world[1]) as lock:
        with pytest.raises(proof.SourceProofError, match='source_database_unavailable'):
            proof.verify_wiki_sources(absent, task, snapshot, context, lock)
    assert not absent.path.exists()
