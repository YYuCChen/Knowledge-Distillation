"""Real historical25 -> current26 sources, fresh P2 owners, no models.

Frozen history uses the existing explicit KD_SCHEMA_HISTORY_REPO resolver.
Negative owned-byte corruption changes only disposable data, never guards or
canonical proof. Spies observe real gates; they do not supply green authority.
"""
from contextlib import contextmanager
from dataclasses import replace
import hashlib
from pathlib import Path
import sqlite3
from unittest.mock import Mock

import pytest

from knowledge_distiller.v1 import database, raw, raw_migration as migration
from knowledge_distiller.v1.captures import Captures
from knowledge_distiller.v1.database import connect
from knowledge_distiller.v1.feishu_inbox import bind_item
from knowledge_distiller.v1.ingestion import Ingestion, envelope_fields
from knowledge_distiller.v1.store import Store
from .test_capture_legacy_writer_veto import checkpoint, receive, bound
from .test_source_schema26_compat import historical_module, material, PNG

PENDING = 'local_source_qualification_pending'
SCHEMA = 'candidate_schema_rebuild_required'


def make_world(tmp_path, *, version=26, labels=('old',)):
    root = tmp_path.resolve()
    root.chmod(0o700)
    data = root / 'data'
    data.mkdir(mode=0o700)
    old_db = historical_module('database', '58bc8ee',
        'ded7f2c87da1da9a67b5c5970bd49b59714a3691295643d4a9fd505dbf7e49ea')
    old_store = historical_module('store', '58bc8ee',
        'f81696db31880c49c0e67df4b82290ff069e43d4e38b4704e9aa96a9ddcf594b')
    path = data / 'knowledge.sqlite3'
    old_db.initialize(path)  # genuine25, never lowering a current DB version
    store = old_store.Store(path)
    vault = root / 'vault'
    vault.mkdir(mode=0o700)
    store.set_setting('vault_path', str(vault))
    sources = [material((store, vault, Ingestion(store)), label, write_raw=False)[:2] for label in labels]
    if version == 26:
        database.initialize(path)  # real migration, no canonical event re-signing
    with connect(path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == version, 'main must use the fixed25/26 closure'
    # Existing cooperative lock metadata is not source/ledger authority.
    (data / '.instance.lock').touch(mode=0o600)
    actual = Store(path)
    return (actual, vault, Captures(actual), raw.RawLedger(actual)), sources


@pytest.fixture
def world(tmp_path):
    return make_world(tmp_path)


def planned(world):
    w, _ = world
    version, entries, failures = migration.plan(w[0].path, w[1])
    assert version == 26 and not failures and len(entries) == 1
    return entries[0]


def replay(world, entry):
    w, _ = world
    return migration._replay_one(w[0].path, w[1], *entry)


def stop_effects(monkeypatch):
    spies = []
    for obj, name in ((migration, '_write'), (migration, '_media_bytes'),
                      (raw, 'render_material'), (raw, 'insert'), (raw, 'allocate'), (raw, 'place')):
        spy = Mock(side_effect=AssertionError('unexpected ' + name))
        monkeypatch.setattr(obj, name, spy)
        spies.append(spy)
    return spies


def unchanged(w, before, spies=()):
    assert checkpoint(w) == before
    for spy in spies:
        spy.assert_not_called()


@pytest.mark.parametrize('version', [25, 26])
def test_real_legacy_full_png_and_body_replay_is_idempotent(tmp_path, version):
    w, sources = make_world(tmp_path, version=version)
    before = checkpoint(w)
    actual, entries, failures = migration.plan(w[0].path, w[1])
    assert actual == version and not failures and len(entries) == 1
    assert checkpoint(w) == before  # plan allocates no ledger/counter/file
    entry = entries[0]
    assert migration._replay_one(w[0].path, w[1], *entry) == 'placed'
    document = entry[2]
    assert (w[1] / document.relative_path).read_bytes() == document.content.encode()
    for attachment in document.attachments:
        assert (w[1] / '附件' / 'raw' / entry[1] / attachment.filename).read_bytes() == PNG
    settled = checkpoint(w)
    assert migration._replay_one(w[0].path, w[1], *entry) == 'already'
    assert checkpoint(w) == settled
    assert migration.plan(w[0].path, w[1]) == (version, [], [])
    assert checkpoint(w) == settled
    with connect(w[0].path) as db:
        row = db.execute('SELECT * FROM raw_records WHERE raw_id=?', (entry[1],)).fetchone()
        assert row['subject_id'] == sources[0][1] and row['origin'] == 'migration'


def test_dryrun_original_report_shape_has_no_raw_writes(world):
    w, _ = world
    before = checkpoint(w)
    report = migration.run(w[0].path.parent, w[1], dry_run=True)
    assert set(report) == {'database_schema', 'vault', 'dry_run', 'planned', 'failures', 'results'}
    assert report['database_schema'] == 26 and report['planned'] == 1 and not report['failures']
    assert set(report['results'][0]) == {'material_id', 'raw_id', 'path', 'attachments'}
    assert checkpoint(w) == before


@pytest.mark.parametrize('route', ['second_part', 'target', 'adjacency'])
def test_actual_bound_dependency_after_plan_vetoes_before_render_or_bytes(world, monkeypatch, route):
    w, sources = world
    entry = planned(world)
    root = receive(w, 'root', at=1790000001000)
    other = receive(w, 'other', at=1790000000000 if route == 'adjacency' else 1790003600000)
    owner = bound(w)  # current P2 public API, never the historical bare local API
    if route == 'second_part':
        with connect(w[0].path) as db:
            bind_item(db, ('synthetic', 'root', 0), sources[0][0])
            bind_item(db, ('synthetic', 'root', 1), owner)
    else:
        w[2]._link(other, owner)
        if route == 'target':
            w[2].decide(root, 'annotation', target='other')
        with connect(w[0].path) as db:
            bind_item(db, ('synthetic', 'root', 0), sources[0][0])
    before = checkpoint(w)
    spies = stop_effects(monkeypatch)
    with pytest.raises(raw.LegacySourceVeto, match='^' + PENDING + '$'):
        replay(world, entry)
    unchanged(w, before, spies)


def test_plan_pending_skips_only_that_source_and_keeps_independent_legacy(tmp_path):
    w, sources = make_world(tmp_path, labels=('blocked', 'independent'))
    receive(w, 'delivery', link=True)
    owner = bound(w)
    with connect(w[0].path) as db:
        bind_item(db, ('synthetic', 'delivery', 0), sources[0][0])
        bind_item(db, ('synthetic', 'delivery', 1), owner)
    before = checkpoint(w)
    _, entries, failures = migration.plan(w[0].path, w[1])
    assert [e[0] for e in entries] == [sources[1][1]]
    assert failures == [{'material_id': sources[0][1], 'error': PENDING}]
    assert checkpoint(w) == before
    assert migration._replay_one(w[0].path, w[1], *entries[0]) == 'placed'


def test_run_late_pending_isolated_without_claiming_completion(tmp_path, monkeypatch):
    w, sources = make_world(tmp_path, labels=('blocked', 'independent'))
    _, entries, _ = migration.plan(w[0].path, w[1])
    receive(w, 'delivery', link=True)
    owner = bound(w)
    with connect(w[0].path) as db:
        bind_item(db, ('synthetic', 'delivery', 0), sources[0][0])
        bind_item(db, ('synthetic', 'delivery', 1), owner)
    # Replay the genuine previously-created plan; only the planner is frozen,
    # never the current qualification or placement predicates.
    monkeypatch.setattr(migration, 'plan', lambda *_: (26, entries, []))
    report = migration.run(w[0].path.parent, w[1])
    assert [r['outcome'] for r in report['results']] == [PENDING, 'placed']
    with connect(w[0].path) as db:
        assert [r[0] for r in db.execute('SELECT subject_id FROM raw_records')] == [sources[1][1]]


@pytest.mark.parametrize('entrypoint', ['plan', 'replay', 'run'])
def test_unknown_guard_schema_propagates_with_no_source_effects(world, monkeypatch, entrypoint):
    w, _ = world
    entry = planned(world)
    with connect(w[0].path) as db:
        db.execute('DROP TRIGGER ingestion_events_proof_unavailable')  # disposable damage, not canonical proof
    before = checkpoint(w)
    spies = stop_effects(monkeypatch)
    with pytest.raises(raw.LegacySourceVeto, match='^' + SCHEMA + '$'):
        if entrypoint == 'plan':
            migration.plan(w[0].path, w[1])
        elif entrypoint == 'replay':
            replay(world, entry)
        else:
            migration.run(w[0].path.parent, w[1])
    unchanged(w, before, spies)


def test_unsupported_real_old_database_never_takes_fs_only_path(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    root.chmod(0o700)
    data = root / 'data'; data.mkdir(mode=0o700)
    vault = root / 'vault'; vault.mkdir(mode=0o700)
    (data / '.instance.lock').touch(mode=0o600)
    # An actually empty SQLite DB starts at version0. No version is lowered.
    db = sqlite3.connect(data / 'knowledge.sqlite3'); db.close()
    before = (data / 'knowledge.sqlite3').read_bytes()
    spies = stop_effects(monkeypatch)
    with pytest.raises(raw.LegacySourceVeto, match='^' + SCHEMA + '$'):
        migration.run(data, vault)
    assert (data / 'knowledge.sqlite3').read_bytes() == before
    assert list(vault.iterdir()) == []
    for spy in spies:
        spy.assert_not_called()


@pytest.mark.parametrize('bad', ['missing_subject', 'missing_ref', 'bad_id'])
def test_invalid_actual_root_or_reference_does_not_render(world, monkeypatch, bad):
    w, _ = world
    mid, rid, document = planned(world)
    if bad == 'missing_subject':
        mid = 987654321
    elif bad == 'bad_id':
        rid = 'not-a-raw-id'
    else:
        fields = envelope_fields(document.content.encode())
        fields['邻接'] = [{'编号': 'R-20261008-9999'}]
        document = replace(document, content='\n'.join(raw.envelope(fields.items())) + '\n' +
                           document.content.split('\n---\n', 1)[1])
    before = checkpoint(w)
    spies = stop_effects(monkeypatch)
    expected = 'raw_migration_plan_invalid' if bad == 'bad_id' else PENDING
    with pytest.raises(raw.RawError, match='^' + expected + '$'):
        replay(world, (mid, rid, document))
    unchanged(w, before, spies)


@pytest.mark.parametrize('field', ['relative_path', 'content', 'attachments'])
def test_full_raw_document_cas_before_blob_or_placement(world, monkeypatch, field):
    w, _ = world
    mid, rid, document = planned(world)
    value = {'relative_path': 'raw/外部/changed.md', 'content': document.content + '变动正文\n',
             'attachments': (replace(document.attachments[0], mime_type='image/jpeg'),)}[field]
    document = replace(document, **{field: value})
    byte_spy = Mock(side_effect=AssertionError('BLOB before full-render CAS'))
    fs_spy = Mock(side_effect=AssertionError('FS before full-render CAS'))
    monkeypatch.setattr(migration, '_media_bytes', byte_spy)
    monkeypatch.setattr(migration, '_write', fs_spy)
    before = checkpoint(w)
    with pytest.raises(raw.RawError, match='^raw_migration_source_changed$'):
        replay(world, (mid, rid, document))
    unchanged(w, before, (byte_spy, fs_spy))


@pytest.mark.parametrize('owned', ['source_text', 'media_blob'])
def test_actual_owned_bytes_corruption_is_freshly_rejected(world, monkeypatch, owned):
    w, _ = world
    entry = planned(world)
    payload = w[0].path.read_bytes()
    old, new = (('完整合成正文 old'.encode(), '篡改合成正文 old'.encode()) if owned == 'source_text'
                else (PNG, PNG[:-1] + bytes([PNG[-1] ^ 1])))
    assert len(old) == len(new) and old in payload
    if owned == 'media_blob':
        assert payload.count(old) == 1
    # External damage to a disposable owned SQLite payload; SQL guards and
    # schema remain intact. No API claims this is a legitimate source revision.
    w[0].path.write_bytes(payload.replace(old, new))
    before = checkpoint(w)
    place = Mock(side_effect=AssertionError('damaged source must not reach FS'))
    monkeypatch.setattr(migration, '_write', place)
    expected = 'raw_migration_source_changed' if owned == 'source_text' else 'raw_attachment_unavailable'
    with pytest.raises(raw.RawError, match='^' + expected + '$'):
        replay(world, entry)
    unchanged(w, before, (place,))


def test_real_new_head_after_plan_rejects_before_render(world, monkeypatch):
    w, _ = world
    entry = planned(world)
    w[3].ensure_material(entry[0], reserved='R-20261008-8888')  # actual ledger API, not fabricated SQL
    before = checkpoint(w)
    spies = stop_effects(monkeypatch)
    with pytest.raises(raw.RawError, match='^raw_id_collision$'):
        replay(world, entry)
    unchanged(w, before, spies)


def test_planned_id_owned_by_other_actual_subject_rejects_before_render(tmp_path, monkeypatch):
    w, sources = make_world(tmp_path, labels=('first', 'second'))
    _, entries, _ = migration.plan(w[0].path, w[1])
    w[3].ensure_material(sources[1][1], reserved=entries[0][1])
    before = checkpoint(w)
    spies = stop_effects(monkeypatch)
    with pytest.raises(raw.RawError, match='^raw_id_collision$'):
        migration._replay_one(w[0].path, w[1], *entries[0])
    unchanged(w, before, spies)


def test_unindexed_same_byte_file_is_gated_and_indexed_not_envelope_skipped(world):
    w, _ = world
    entry = planned(world)
    target = w[1] / entry[2].relative_path
    target.parent.mkdir(parents=True, mode=0o700)
    target.write_bytes(entry[2].content.encode())
    inode = target.stat().st_ino
    _, next_entries, failures = migration.plan(w[0].path, w[1])
    assert not failures and len(next_entries) == 1  # a literal envelope is not indexed authority
    # Public replanning respects existing vault IDs and proposes another ID;
    # replay of the genuine original plan can still confirm its own exact file.
    assert replay(world, entry) == 'already'
    assert target.stat().st_ino == inode
    with connect(w[0].path) as db:
        assert db.execute('SELECT subject_id FROM raw_records WHERE raw_id=?', (entry[1],)).fetchone()[0] == entry[0]


@pytest.mark.parametrize('target', ['body', 'attachment'])
def test_existing_ledger_requires_complete_fs_readback(world, target):
    w, _ = world
    entry = planned(world)
    assert replay(world, entry) == 'placed'
    document = entry[2]
    path = (w[1] / document.relative_path if target == 'body' else
            w[1] / '附件' / 'raw' / entry[1] / document.attachments[0].filename)
    path.write_bytes(b'damaged controlled-synthetic file')
    before = checkpoint(w)
    with pytest.raises(raw.RawError, match='^raw_migration_readback_mismatch$'):
        replay(world, entry)
    with pytest.raises(raw.RawError, match='^raw_migration_readback_mismatch$'):
        migration.plan(w[0].path, w[1])
    unchanged(w, before)


def test_existing_app_ledger_is_not_silently_relabelled_as_migration(world):
    w, _ = world
    entry = planned(world)
    record = w[3].ensure_material(entry[0], reserved=entry[1])
    assert w[3].write(record, w[1]) in {'placed', 'already'}
    before = checkpoint(w)
    with pytest.raises(raw.RawError, match='^raw_migration_record_conflict$'):
        replay(world, entry)
    unchanged(w, before)


def test_db_lock_held_over_real_attachment_reads_and_placement(world, monkeypatch):
    w, _ = world
    entry = planned(world)
    original = raw._attachment_bytes
    reads = []
    def observe(store, record, attachment, *, db=None):
        assert db is not None and db.in_transaction
        other = sqlite3.connect(w[0].path, timeout=0)
        try:
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                other.execute('BEGIN IMMEDIATE')
        finally:
            other.close()
        reads.append(attachment['member_id'])
        return original(store, record, attachment, db=db)
    monkeypatch.setattr(raw, '_attachment_bytes', observe)
    place = raw.place
    def observe_place(*args):
        other = sqlite3.connect(w[0].path, timeout=0)
        try:
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                other.execute('BEGIN IMMEDIATE')
        finally:
            other.close()
        return place(*args)
    monkeypatch.setattr(raw, 'place', observe_place)
    assert replay(world, entry) == 'placed'
    assert reads == [entry[2].attachments[0].member_id]


def test_body_conflict_keeps_attachment_but_no_ledger_or_counter(world):
    w, _ = world
    entry = planned(world)
    body = w[1] / entry[2].relative_path
    body.parent.mkdir(parents=True, mode=0o700)
    body.write_bytes(b'independent user conflict bytes')
    with connect(w[0].path) as db:
        counters = tuple(tuple(r) for r in db.execute('SELECT * FROM raw_counters'))
    assert replay(world, entry) == 'conflict'
    assert body.read_bytes() == b'independent user conflict bytes'
    assert (w[1] / '附件' / 'raw' / entry[1] / entry[2].attachments[0].filename).read_bytes() == PNG
    with connect(w[0].path) as db:
        assert db.execute('SELECT COUNT(*) FROM raw_records').fetchone()[0] == 0
        assert tuple(tuple(r) for r in db.execute('SELECT * FROM raw_counters')) == counters


def test_real_commit_failure_retains_fs_and_never_reports_success(world, monkeypatch):
    w, _ = world
    entry = planned(world)
    # A real deferred FK violation creates COMMIT failure, then original
    # database.connect rollback. No green proof or fake commit exception.
    original = migration.connect
    observed = []
    @contextmanager
    def fail_commit(path):
        with original(path) as db:
            yield db
            db.execute('PRAGMA defer_foreign_keys=ON')
            db.execute('INSERT INTO capture_state(capture_id) VALUES (?)', (987654321,))
            observed.append('deferred_row_inserted')
    monkeypatch.setattr(migration, 'connect', fail_commit)
    # Freeze only the already-real plan to observe run's success-list timing.
    monkeypatch.setattr(migration, 'plan', lambda *_: (26, [entry], []))
    with pytest.raises(sqlite3.IntegrityError):
        migration.run(w[0].path.parent, w[1])
    assert observed == ['deferred_row_inserted']  # INSERT succeeded; failure belongs to actual COMMIT
    assert (w[1] / entry[2].relative_path).read_bytes() == entry[2].content.encode()
    assert (w[1] / '附件' / 'raw' / entry[1] / entry[2].attachments[0].filename).read_bytes() == PNG
    with connect(w[0].path) as db:
        assert db.execute('SELECT COUNT(*) FROM raw_records').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM raw_counters').fetchone()[0] == 0
        assert db.execute('SELECT 1 FROM capture_state WHERE capture_id=987654321').fetchone() is None
    monkeypatch.setattr(migration, 'connect', original)
    assert replay(world, entry) == 'already'  # actual gate/readback again, no file replacement
