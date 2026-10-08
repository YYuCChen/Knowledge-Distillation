"""Disposable inputs/SQLite only; no source parsing, models, Vault or app."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import zipfile

import pytest

from knowledge_distiller.v1 import database, source_files
from knowledge_distiller.v1.database import connect
from knowledge_distiller.v1.file_sources import prepare_file, prepare_direct_text, SubmittedSource
from knowledge_distiller.v1.intake_binding import build_local_binding, IntakeBindingError
from knowledge_distiller.v1.source_files import SourceCopyError, copy_path
from knowledge_distiller.v1.store import Store


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / 'owned' / 'state.sqlite')
    value.initialize()
    return value


def submit(store, source):
    return store.submit_local_bound_source(source, envelope_json=build_local_binding(source).envelope_json)


def rows(store, table):
    with connect(store.path) as db:
        return [dict(r) for r in db.execute(f'SELECT * FROM {table} ORDER BY rowid')]


def _synthetic_document_bytes(kind):
    # Minimal complete variants of test_document_sources.pdf_bytes/epub_bytes.
    # Do not import that test module: it constructs a Docling converter at import.
    output = io.BytesIO()
    if kind == 'pdf':
        from pypdf import PdfWriter
        from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
        writer = PdfWriter()
        page = writer.add_blank_page(612, 792)
        font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
                                 NameObject('/Subtype'): NameObject('/Type1'),
                                 NameObject('/BaseFont'): NameObject('/Helvetica')})
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'):
            DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
        stream = DecodedStreamObject()
        stream.set_data(b'BT /F1 12 Tf 72 720 Td (Synthetic original source.) Tj ET')
        page[NameObject('/Contents')] = writer._add_object(stream)
        writer.write(output)
    elif kind == 'epub':
        with zipfile.ZipFile(output, 'w') as archive:
            archive.writestr('mimetype', 'application/epub+zip')
            archive.writestr('META-INF/container.xml',
                '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">'
                '<rootfiles><rootfile full-path="OEBPS/content.opf" '
                'media-type="application/oebps-package+xml"/></rootfiles></container>')
            archive.writestr('OEBPS/content.opf',
                '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">'
                '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
                '<dc:identifier id="id">synthetic-storage-fixture</dc:identifier>'
                '<dc:title>合成原件</dc:title><dc:language>zh</dc:language>'
                '<meta property="dcterms:modified">2026-10-08T00:00:00Z</meta></metadata>'
                '<manifest><item id="first" href="one.xhtml" media-type="application/xhtml+xml"/>'
                '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
                '</manifest><spine><itemref idref="first"/></spine></package>')
            archive.writestr('OEBPS/one.xhtml',
                '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>合成原件</title></head>'
                '<body><p>合成完整正文。</p></body></html>')
            archive.writestr('OEBPS/nav.xhtml',
                '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">'
                '<head><title>目录</title></head><body><nav epub:type="toc"><ol>'
                '<li><a href="one.xhtml">合成原件</a></li></ol></nav></body></html>')
    else:
        raise ValueError('synthetic document kind unsupported')
    return output.getvalue()


@pytest.mark.parametrize('kind', ['markdown', 'pdf', 'epub', 'direct_text'])
def test_exact_input_persisted_without_qualification(store, kind):
    source = (prepare_direct_text('重复\r\n重复 é', {'author': '声明作者'}) if kind == 'direct_text'
              else prepare_file('Chapter:1.' + {'markdown': 'md', 'pdf': 'pdf', 'epub': 'epub'}[kind],
                                _synthetic_document_bytes(kind) if kind in {'pdf', 'epub'}
                                else b'synthetic\x00\xff\r\n'))
    item = submit(store, source)
    actual, binding = store.local_intake_binding(item)
    assert actual == source and binding == build_local_binding(source)
    event, = rows(store, 'ingestion_events')
    assert event['kind'] == 'raw_pending'
    assert json.loads(event['detail_json'])['manifest'] == {'intake_envelope_json': binding.envelope_json}
    assert rows(store, 'materials') == rows(store, 'source_facts') == rows(store, 'raw_records') == []
    assert rows(store, 'knowledge_results') == []
    owner, = rows(store, 'distill_items')
    assert (owner['state'], owner['phase'], owner['material_id']) == ('queued', 'collecting', None)
    assert json.loads(binding.relation_json)['intent']['identity'] == 'unresolved'


def test_replay_keeps_owner_terminal_queue_and_human_json(store):
    source = prepare_direct_text('合成原文')
    item = submit(store, source)
    human = '{"human":{"action":"unable","original_text":"合成"}}'
    with connect(store.path) as db:
        db.execute("UPDATE distill_items SET state='failed',error_code='review_incomplete',confirmation_json=? WHERE item_id=?",
                   (human, item))
    before = rows(store, 'distill_items')
    assert submit(store, source) == item
    assert rows(store, 'distill_items') == before
    assert len(rows(store, 'ingestion_events')) == 1


def test_colon_label_version_and_legacy_coexist(store):
    first = prepare_file('Chapter:1.md', b'synthetic')
    legacy = store.submit_source(first)
    a = submit(store, first)
    second = prepare_file('Chapter:2.md', first.content)
    b = submit(store, second)
    assert len({legacy, a, b}) == 3
    assert first.source_key == second.source_key
    assert store.submit_source(second) == legacy  # original legacy first-label behavior
    assert store.local_intake_binding(a)[0] == first
    assert store.local_intake_binding(b)[0] == second
    assert store.submitted_source(legacy).label == first.label
    assert build_local_binding(first).source_binding_sha256 != build_local_binding(second).source_binding_sha256
    assert rows(store, 'submitted_sources')[0]['binding_scope'] == 'legacy'
    with pytest.raises(IntakeBindingError):
        store.local_intake_binding(legacy)


def test_declared_metadata_versions_keep_identical_body(store):
    first = prepare_direct_text('相同正文', {'author': '甲'})
    second = prepare_direct_text('相同正文', {'author': '乙'})
    a, b = submit(store, first), submit(store, second)
    assert a != b and first.content == second.content and first.source_key != second.source_key
    assert store.local_intake_binding(a)[0] == first
    assert store.local_intake_binding(b)[0] == second


@pytest.mark.parametrize('different', [False, True])
def test_concurrent_descriptor_ownership(store, different):
    sources = [prepare_file('A.md' if not different or n % 2 == 0 else 'B.md', b'synthetic') for n in range(8)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(lambda s: submit(store, s), sources))
    assert len(set(ids)) == (2 if different else 1)
    assert len(rows(store, 'submitted_sources')) == len(set(ids))
    assert len(rows(store, 'ingestion_events')) == len(set(ids))
    for item, source in zip(ids, sources):
        assert store.local_intake_binding(item)[0] == source


@pytest.mark.parametrize('field,value', [('input_label','B.md'), ('input_kind','pdf'), ('input_key','x'),
    ('input_metadata','{} '), ('content', b'changed'), ('binding_scope','legacy'),
    ('retain_until','2099'), ('retryable',0)])
def test_bound_tuple_sql_mutation_rejected(store, field, value):
    item = submit(store, prepare_file('A.md', b'synthetic'))
    before = rows(store, 'submitted_sources')
    with pytest.raises(sqlite3.IntegrityError):
        with connect(store.path) as db:
            db.execute(f'UPDATE submitted_sources SET {field}=? WHERE item_id=?', (value, item))
    assert rows(store, 'submitted_sources') == before
    with pytest.raises(sqlite3.IntegrityError):
        with connect(store.path) as db:
            db.execute('DELETE FROM submitted_sources WHERE item_id=?', (item,))


def test_normal_connection_cannot_insert_scope_or_event(store):
    original = submit(store, prepare_direct_text('合成'))
    event, = rows(store, 'ingestion_events')
    with connect(store.path) as db:
        assert db.execute("SELECT local_intake_insert(1,2,3,4,5,6,7),local_intake_event(1,2,3,4,5,6,7)").fetchone()[:] == (0,0)
    other = store.create_item('synthetic', ingestion_contract='raw-verified-v1',
                              source_binding_sha256='a'*64, relation_binding_sha256='b'*64)
    with pytest.raises(sqlite3.IntegrityError):
        with connect(store.path) as db:
            db.execute('''INSERT INTO submitted_sources(item_id,input_kind,input_key,input_label,
                input_metadata,content,binding_scope) VALUES (?,'direct_text','fake','直接文本','{}',?,'fake')''',
                       (other, b'synthetic'))
    altered = dict(event, event_key='f'*64, subject_id=other, item_id=other)
    with pytest.raises(sqlite3.IntegrityError):
        with connect(store.path) as db:
            keys = tuple(altered)
            db.execute(f"INSERT INTO ingestion_events({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})",
                       tuple(altered[k] for k in keys))
    with pytest.raises(ValueError):
        store.append_ingestion_event(original, kind='raw_pending', code='intake_frozen')


def test_bare_digest_owner_keeps_legacy_scope_and_old_observations(store):
    source = prepare_direct_text('旧 backend 候选')
    item = store.submit_source(source, ingestion_contract='raw-verified-v1',
                               source_binding_sha256='a'*64, relation_binding_sha256='b'*64)
    store.append_ingestion_event(item, kind='raw_pending', code='writer_pending')
    assert rows(store, 'submitted_sources')[0]['binding_scope'] == 'legacy'
    with pytest.raises(IntakeBindingError):
        store.local_intake_binding(item)
    assert submit(store, source) != item


@pytest.mark.parametrize('damage', ['missing','bytes','symlink','hardlink','parent_symlink'])
def test_bad_owned_file_never_uses_db_fallback_or_initialize_repair(store, damage, tmp_path):
    source = prepare_file('A.md', b'synthetic')
    item = submit(store, source)
    path = copy_path(store.path.parent, source.source_kind, source.source_key, source.label)
    if damage == 'missing':
        path.unlink()
    elif damage == 'bytes':
        path.chmod(0o600)
        path.write_bytes(b'changed')
    elif damage == 'symlink':
        path.unlink()
        foreign = tmp_path / 'foreign'; foreign.write_bytes(source.content)
        path.symlink_to(foreign)
    elif damage == 'hardlink':
        os.link(path, tmp_path / 'extra-link')
    else:
        parent = path.parent
        moved = parent.with_name(parent.name + '-moved')
        parent.rename(moved)
        parent.symlink_to(moved, target_is_directory=True)
    before = rows(store, 'distill_items')
    store.initialize()
    with pytest.raises(SourceCopyError):
        store.local_intake_binding(item)
    with pytest.raises(SourceCopyError):
        submit(store, source)
    assert rows(store, 'distill_items') == before
    if damage == 'missing':
        assert not path.exists()


def test_file_directory_replaced_during_read_is_rejected(store, monkeypatch):
    source = prepare_file('A.md', b'synthetic')
    item = submit(store, source)
    path = copy_path(store.path.parent, 'markdown', source.source_key, source.label)
    original_open = os.open
    changed = False
    def racing_open(name, flags, *args, **kwargs):
        nonlocal changed
        fd = original_open(name, flags, *args, **kwargs)
        if name == source.label and flags & os.O_NONBLOCK and not changed:
            changed = True
            path.parent.rename(path.parent.with_name(path.parent.name + '-moved'))
            path.parent.mkdir()
        return fd
    monkeypatch.setattr(source_files.os, 'open', racing_open)
    with pytest.raises(SourceCopyError):
        store.local_intake_binding(item)
    assert changed


@pytest.mark.parametrize('expected', [True, False, -1, 1.0, None, '9'])
def test_bound_reader_requires_typed_original_length(store, expected):
    source = prepare_file('A.md', b'synthetic')
    submit(store, source)
    with pytest.raises(SourceCopyError, match='bound_copy_length_invalid'):
        source_files.read_bound_copy(store.path.parent, 'markdown', source.source_key,
                                     source.label, expected_bytes=expected)


@pytest.mark.parametrize('changed', [b'', b'x'*10000])
def test_size_mismatch_rejected_before_stream_open(store, monkeypatch, changed):
    source = prepare_file('A.md', b'synthetic')
    item = submit(store, source)
    path = copy_path(store.path.parent, 'markdown', source.source_key, source.label)
    path.chmod(0o600)
    path.write_bytes(changed)
    def forbidden(*args, **kwargs):
        raise AssertionError('size mismatch must precede stream read')
    monkeypatch.setattr(source_files.os, 'fdopen', forbidden)
    with pytest.raises(SourceCopyError, match='bound_copy_length_mismatch'):
        store.local_intake_binding(item)


@pytest.mark.parametrize('grow', [False, True])
def test_held_fd_reads_are_bounded_and_require_eof(store, monkeypatch, grow):
    source = prepare_file('A.md', b'synthetic')
    item = submit(store, source)
    path = copy_path(store.path.parent, 'markdown', source.source_key, source.label)
    original = os.fdopen
    requests, returned = [], []
    class CheckedStream:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            self.stream.__enter__()
            return self
        def __exit__(self, *args):
            return self.stream.__exit__(*args)
        def read(self, size):
            assert type(size) is int and 0 < size <= len(source.content)+1
            if grow and not requests:
                path.chmod(0o600)
                with path.open('ab') as output:
                    output.write(b'x'*10000)
            requests.append(size)
            chunk = self.stream.read(size)
            returned.append(len(chunk))
            return chunk
    def checked(fd, mode, **kwargs):
        assert mode == 'rb' and kwargs['buffering'] == 0
        return CheckedStream(original(fd, mode, **kwargs))
    monkeypatch.setattr(source_files.os, 'fdopen', checked)
    if grow:
        with pytest.raises(SourceCopyError, match='bound_copy_length_mismatch'):
            store.local_intake_binding(item)
        assert sum(returned) == len(source.content)+1
    else:
        assert store.local_intake_binding(item)[0] == source
        assert sum(returned) == len(source.content) and returned[-1] == 0
    assert requests[0] == len(source.content)+1


def test_zero_byte_expected_is_valid_without_unbounded_read(store):
    content = b''
    key = hashlib.sha256(content).hexdigest()
    source_files.retain_bound_copy(store.path.parent, 'markdown', key, 'empty.md', content)
    assert source_files.read_bound_copy(store.path.parent, 'markdown', key, 'empty.md',
                                        expected_bytes=0) == b''


@pytest.mark.parametrize('fault', ['duplicate','trailing','contract','digest'])
def test_invalid_envelope_rejected_before_storage(store, fault):
    source = prepare_direct_text('合成正文')
    envelope = build_local_binding(source).envelope_json
    if fault == 'duplicate':
        envelope = '{"contract":"intake-binding-proposal-v1",' + envelope[1:]
    elif fault == 'trailing':
        envelope += '{}'
    elif fault == 'contract':
        envelope = envelope.replace('intake-source-v1','unknown-source-v1')
    else:
        value = json.loads(envelope); value['source_binding_sha256'] = '0'*64
        envelope = json.dumps(value)
    with pytest.raises(IntakeBindingError):
        store.submit_local_bound_source(source, envelope_json=envelope)
    assert rows(store, 'distill_items') == rows(store, 'ingestion_events') == []


def test_changed_real_source_rejected_without_acceptance(store):
    source = prepare_direct_text('原文', {'author':'甲'})
    envelope = build_local_binding(source).envelope_json
    source.metadata['user_declared']['author'] = '乙'
    with pytest.raises(IntakeBindingError):
        store.submit_local_bound_source(source, envelope_json=envelope)
    assert rows(store, 'distill_items') == []


@pytest.mark.parametrize('kind', ['image','link','voice','group'])
def test_unsupported_kind_has_no_storage_side_effect(store, kind):
    source = SubmittedSource(kind, 'synthetic', 'synthetic', b'synthetic', {})
    with pytest.raises(IntakeBindingError):
        store.submit_local_bound_source(source, envelope_json='{}')
    assert rows(store, 'distill_items') == rows(store, 'submitted_sources') == []


@pytest.mark.parametrize('damage', ['duplicate','trailing','contract','owner','digest'])
def test_persisted_event_corruption_is_not_rebuilt_from_hashes(store, damage):
    item = submit(store, prepare_direct_text('原始合成正文'))
    event, = rows(store, 'ingestion_events')
    detail = json.loads(event['detail_json'])
    envelope = detail['manifest']['intake_envelope_json']
    if damage == 'duplicate':
        envelope = '{"contract":"intake-binding-proposal-v1",' + envelope[1:]
    elif damage == 'trailing':
        envelope += '{}'
    elif damage == 'contract':
        envelope = envelope.replace('intake-source-v1','unknown-source-v1')
    elif damage == 'digest':
        detail['source_binding_sha256'] = '0'*64
    detail['manifest']['intake_envelope_json'] = envelope
    # Deliberate synthetic corruption bypasses only the append-only guard;
    # public reads must detect it, even though the original input still exists.
    with connect(store.path) as db:
        db.execute('DROP TRIGGER ingestion_events_no_update')
        if damage == 'owner':
            db.execute('UPDATE ingestion_events SET subject_id=subject_id+1 WHERE event_id=?', (event['event_id'],))
        else:
            db.execute('UPDATE ingestion_events SET detail_json=? WHERE event_id=?',
                       (json.dumps(detail), event['event_id']))
    with pytest.raises(IntakeBindingError):
        store.local_intake_binding(item)
    with pytest.raises(IntakeBindingError):
        submit(store, prepare_direct_text('原始合成正文'))


def test_db_bytes_cannot_disagree_with_safe_file_readback(store):
    source = prepare_file('A.md', b'synthetic')
    item = submit(store, source)
    with connect(store.path) as db:
        db.execute('DROP TRIGGER submitted_sources_ingestion_no_release')
        db.execute('DROP TRIGGER submitted_sources_local_tuple')
        db.execute('UPDATE submitted_sources SET content=? WHERE item_id=?', (b'changed', item))
    with pytest.raises(IntakeBindingError):
        store.local_intake_binding(item)


def test_database_owner_unique_is_independent_of_trusted_callback(store):
    first = submit(store, prepare_direct_text('原文'))
    source_row, = rows(store, 'submitted_sources')
    other = store.create_item('synthetic', ingestion_contract='raw-verified-v1',
                              source_binding_sha256='a'*64, relation_binding_sha256='b'*64)
    with pytest.raises(sqlite3.IntegrityError):
        with connect(store.path) as db:
            db.create_function('local_intake_insert', 7, lambda *_: 1)
            db.execute('''INSERT INTO submitted_sources(item_id,input_kind,input_key,input_label,
                input_metadata,content,binding_scope) VALUES (?,?,?,?,?,?,?)''',
                (other, *(source_row[k] for k in ('input_kind','input_key','input_label',
                                                 'input_metadata','content','binding_scope'))))
    event, = rows(store, 'ingestion_events')
    with pytest.raises(sqlite3.IntegrityError):
        with connect(store.path) as db:
            db.create_function('local_intake_event', 7, lambda *_: 1)
            db.execute('''INSERT INTO ingestion_events(event_key,contract,subject_kind,subject_id,
                item_id,kind,binding_sha256,detail_json,created_at) VALUES (?,?,?,?,?,?,?,?,?)''',
                ('f'*64, *(event[k] for k in ('contract','subject_kind','subject_id','item_id',
                                            'kind','binding_sha256','detail_json','created_at'))))
    assert store.local_intake_binding(first)[0].content == '原文'.encode()


def test_mutable_caller_metadata_not_retained(store, monkeypatch):
    source = prepare_direct_text('原文', {'author':'甲'})
    original = Store._local_scope
    def mutate(binding):
        source.metadata['user_declared']['author'] = '乙'
        return original(binding)
    monkeypatch.setattr(Store, '_local_scope', staticmethod(mutate))
    item = submit(store, source)
    assert store.local_intake_binding(item)[0].metadata == {'user_declared':{'author':'甲'}}


def test_event_failure_rolls_back_owner_and_retry_reuses_exact_copy(store, monkeypatch):
    source = prepare_file('A.md', b'synthetic')
    original = Store._local_event
    def fail(*args):
        raise RuntimeError('synthetic-before-event')
    with monkeypatch.context() as patch:
        patch.setattr(Store, '_local_event', staticmethod(fail))
        with pytest.raises(RuntimeError, match='synthetic-before-event'):
            submit(store, source)
    assert rows(store, 'distill_items') == rows(store, 'submitted_sources') == rows(store, 'ingestion_events') == []
    path = copy_path(store.path.parent, 'markdown', source.source_key, source.label)
    inode = path.stat().st_ino
    item = submit(store, source)
    assert path.stat().st_ino == inode
    assert store.local_intake_binding(item)[0] == source


def historical_root(tmp_path, version):
    fixtures = Path(__file__).with_name('fixtures')
    path = tmp_path / f'schema-{version}.sqlite'
    with closing(sqlite3.connect(path)) as db:
        db.executescript((fixtures/'wiki-schema21.sql').read_text())
        if version >= 22:
            db.executescript((fixtures/'wiki-schema22.sql').read_text())
        db.executescript((fixtures/'upgrade-probe-seed.sql').read_text())
        if version >= 23:
            db.executescript((fixtures/'upgrade-probe-schema23.sql').read_text())
        if version >= 24:
            db.execute('PRAGMA foreign_keys=OFF')
            db.execute('BEGIN IMMEDIATE')
            database.migrate_v24(db)
            db.execute('PRAGMA user_version=24')
            if version >= 25:
                database.migrate_v25(db)
                db.execute('PRAGMA user_version=25')
            db.commit()
    return path


def snapshot(path):
    with closing(sqlite3.connect(path)) as db:
        tables = {}
        for name, in db.execute("SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name").fetchall():
            columns = tuple(r[1] for r in db.execute(f'PRAGMA table_info("{name}")'))
            tables[name] = (columns, [tuple(r) for r in db.execute(f'SELECT * FROM "{name}" ORDER BY rowid')])
        catalog = db.execute('SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name').fetchall()
        return db.execute('PRAGMA user_version').fetchone()[0], tables, catalog


@pytest.mark.parametrize('version', [21,22,23,24,25])
def test_genuine_migration_preserves_all_old_typed_rows_and_references(tmp_path, version):
    path = historical_root(tmp_path, version)
    old_version, before, _ = snapshot(path)
    assert old_version == version
    database.initialize(path)
    current, after, catalog = snapshot(path)
    assert current == 26
    for table, (names, old_rows) in before.items():
        new_names, new_rows = after[table]
        positions = [new_names.index(n) for n in names]
        assert [tuple(row[i] for i in positions) for row in new_rows] == old_rows, table
    assert after['submitted_sources'][0][-1] == 'binding_scope'
    assert all(r[-1] == 'legacy' for r in after['submitted_sources'][1])
    with connect(path) as db:
        assert list(db.execute('PRAGMA foreign_key_check')) == []
        assert db.execute('PRAGMA foreign_keys').fetchone()[0] == 1
    database.initialize(path)
    assert snapshot(path) == (current, after, catalog)


@pytest.mark.parametrize('version', [21,22,23,24,25])
def test_v26_postcheck_failure_rolls_back_entire_upgrade(tmp_path, version, monkeypatch):
    path = historical_root(tmp_path, version)
    before = snapshot(path)
    actual = database.migrate_v26
    def fail(db):
        actual(db)
        raise RuntimeError('synthetic-final-postcheck')
    monkeypatch.setattr(database, 'migrate_v26', fail)
    with pytest.raises(RuntimeError, match='synthetic-final-postcheck'):
        database.initialize(path)
    assert snapshot(path) == before


@pytest.mark.parametrize('damage', ['trigger','index','ddl','bad_type'])
def test_unknown_input_schema_or_bad_types_fail_closed(tmp_path, damage):
    path = historical_root(tmp_path, 25)
    with closing(sqlite3.connect(path)) as db:
        if damage == 'trigger':
            db.execute("CREATE TRIGGER unknown_input AFTER INSERT ON submitted_sources BEGIN SELECT 1; END")
        elif damage == 'index':
            db.execute('CREATE INDEX unknown_input ON submitted_sources(input_label)')
        elif damage == 'ddl':
            db.execute('ALTER TABLE submitted_sources ADD COLUMN unknown TEXT')
        else:
            db.execute("UPDATE submitted_sources SET content='not-a-BLOB'")
        db.commit()
    before = snapshot(path)
    with pytest.raises(RuntimeError):
        database.initialize(path)
    assert snapshot(path) == before


def test_reinitialize26_does_not_reinstall_missing_guard(store):
    with connect(store.path) as db:
        db.execute('DROP TRIGGER submitted_sources_local_insert')
    database.initialize(store.path)
    with connect(store.path) as db:
        assert db.execute("SELECT 1 FROM sqlite_schema WHERE name='submitted_sources_local_insert'").fetchone() is None
