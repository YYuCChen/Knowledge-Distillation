"""Real disposable P2/history APIs and default writer vetoes; no model/App.

Callback-late refusals roll back DB allocation, not arbitrary callback files.
Scope/proof/guards are never mocked into a ready or canonical state.
"""
from types import SimpleNamespace
from unittest.mock import Mock
import hashlib
import json
import sqlite3

import pytest

from knowledge_distiller.v1 import database, pipeline, raw
from knowledge_distiller.v1.captures import Captures
from knowledge_distiller.v1.database import connect
from knowledge_distiller.v1.file_sources import prepare_direct_text, parse_submitted_source
from knowledge_distiller.v1.ingestion import Ingestion
from knowledge_distiller.v1.intake_binding import build_local_binding
from knowledge_distiller.v1.store import Store
from knowledge_distiller.v1.temporary_artifacts import TemporaryArtifacts
from .test_legacy_source_scope import full_state, receive, literal_record
from .test_source_schema26_compat import historical_module, material


@pytest.fixture
def current(tmp_path):
    root = tmp_path.resolve()
    root.chmod(0o700)
    store = Store(root / 'synthetic.sqlite')
    store.initialize()
    vault = root / 'vault'
    vault.mkdir(mode=0o700)
    store.set_setting('vault_path', str(vault))
    return store, vault, Captures(store), raw.RawLedger(store, version='synthetic')


def checkpoint(world):
    store, vault = world[:2]
    state = full_state(store, vault)
    files = {p.relative_to(vault.parent).as_posix(): (p.stat().st_ino, hashlib.sha256(p.read_bytes()).hexdigest())
             for p in vault.parent.rglob('*') if p.is_file()}
    return state, files


def bound(world, *, fact=False):
    store = world[0]
    source = prepare_direct_text('新合成本地bound输入')
    item = store.submit_local_bound_source(source, envelope_json=build_local_binding(source).envelope_json)
    if fact:
        assert store.claim_next_work() == ('item', item)
        row = store.item_bundle(item)
        parsed = parse_submitted_source(source)
        review = {'schema': 1, 'snapshot': parsed.snapshot, 'uncertainties': parsed.uncertainties,
                  'lineage': parsed.lineage}
        store.establish_submitted_fact(item, source, parsed, expected_revision=row['review_revision'],
                                       review_result=review)
        assert store.item_bundle(item)['source_fact_id'] is not None
    return item


def pending_capture(world, name, *, app='synthetic', at=1790000000000):
    cid = receive(world, name, app=app, at=at)
    world[2].decide(cid, 'my_thought')
    capture = world[2].get(cid)
    record = world[2].ensure_raw(capture, world[2].identity(cid), [], None, ledger=world[3])
    return cid, record


def deny_calls(monkeypatch, names):
    spies = []
    for obj, name in names:
        spy = Mock(side_effect=AssertionError('unexpected ' + name))
        monkeypatch.setattr(obj, name, spy)
        spies.append(spy)
    return spies


def assert_uncalled(spies):
    for spy in spies:
        spy.assert_not_called()


def service(world, root):
    no_model = SimpleNamespace(capture=Mock(side_effect=AssertionError('source')),
        derive=Mock(side_effect=AssertionError('derive')), review=Mock(side_effect=AssertionError('review')),
        for_item=Mock(side_effect=AssertionError('model scope')))
    return pipeline.Distiller(store=world[0], source=no_model, normalizer=no_model,
        recognizer=no_model, reviewer=no_model, confirmation_clipper=no_model,
        knowledge_model=no_model, runtime_root=root, vault=world[1], ocr=no_model, documents=no_model)


@pytest.mark.parametrize('fact', [False, True])
@pytest.mark.parametrize('entry', ['run', 'establish', 'partial', 'finish', 'write_raw'])
def test_bound_pipeline_default_and_private_entries_preserve_actual_state(current, monkeypatch, fact, entry):
    item = bound(current, fact=fact)
    row = current[0].item_bundle(item)
    distiller = service(current, current[1].parent / 'runtime')
    spies = deny_calls(monkeypatch, [(TemporaryArtifacts, 'clean_item'), (pipeline, 'publish'),
        (raw, 'allocate'), (raw, 'render_material'), (raw, 'insert'), (raw, 'place'),
        (current[0], 'mark_working'), (current[0], 'mark_failed'), (current[0], 'mark_succeeded')])
    before = checkpoint(current)
    if entry == 'run':
        assert distiller.run(item) == pipeline.DistillResult(item, row['state'])
    else:
        with pytest.raises(raw.LegacySourceVeto, match='^local_source_qualification_pending$'):
            if entry == 'establish':
                distiller._establish_source(item, row)
            elif entry == 'partial':
                distiller._finish_partial_source(item, row, {'snapshot': 'synthetic', 'uncertainties': []})
            elif entry == 'finish':
                distiller._finish(item)
            else:
                distiller._write_raw(row)
    assert checkpoint(current) == before
    assert_uncalled(spies)
    for value in vars(distiller.source).values():
        value.assert_not_called()


def test_bound_published_stale_projection_cannot_skip_early_actual_gate(current, monkeypatch):
    item = bound(current)
    distiller = service(current, current[1].parent / 'runtime')
    row = {**current[0].item_bundle(item), 'published_path': 'synthetic-stale.md'}
    # A stale projection is explicitly not a SQL-manufactured published bound state.
    monkeypatch.setattr(distiller, '_item', lambda _: row)
    spies = deny_calls(monkeypatch, [(current[0], 'mark_succeeded'), (TemporaryArtifacts, 'clean_item')])
    before = checkpoint(current)
    assert distiller.run(item).state == row['state']
    assert checkpoint(current) == before
    assert_uncalled(spies)


@pytest.mark.parametrize('entry', ['ensure', 'ensure_hook', 'row', 'insert'])
def test_bound_fact_default_raw_gate_before_allocate_or_render(current, monkeypatch, entry):
    item = bound(current, fact=True)
    mid = current[0].item_bundle(item)['material_id']
    spies = deny_calls(monkeypatch, [(raw, 'allocate'), (raw, 'render_material'), (raw, 'place')])
    before = checkpoint(current)
    hook = Mock()
    with pytest.raises(raw.LegacySourceVeto, match='^local_source_qualification_pending$'):
        if entry == 'ensure':
            current[3].ensure_material(mid)
        elif entry == 'ensure_hook':
            current[3].ensure_material(mid, check_source=hook)
        else:
            with connect(current[0].path) as db:
                if entry == 'row':
                    raw.material_row(db, mid)
                else:
                    raw.insert(db, 'R-20261008-0001', 'material', mid, '第三方',
                        raw.RawDocument('raw/外部/R-20261008-0001.md', '---\n取代: null\n---\nsynthetic'), origin='app')
    assert checkpoint(current) == before
    assert_uncalled(spies)
    hook.assert_not_called()


@pytest.mark.parametrize('entry', ['ensure', 'insert'])
def test_missing_actual_subject_rejects_without_side_effects(current, entry):
    before = checkpoint(current)
    with pytest.raises(raw.LegacySourceVeto, match='^local_source_qualification_pending$'):
        if entry == 'ensure':
            current[3].ensure_material(987654321)
        else:
            with connect(current[0].path) as db:
                raw.insert(db, 'R-20261008-0001', 'capture', 987654321, '本人',
                    raw.RawDocument('raw/自述/R-20261008-0001.md', '---\n取代: null\n---\nsynthetic'), origin='app')
    assert checkpoint(current) == before


def test_existing_literal_raw_bound_later_is_not_written_or_attempted(current, monkeypatch):
    cid, record = pending_capture(current, 'root')
    current[2]._link(cid, bound(current))  # no canonical ingestion event existed
    spies = deny_calls(monkeypatch, [(raw, 'place'), (raw, '_attachment_bytes')])
    before = checkpoint(current)
    with pytest.raises(raw.LegacySourceVeto, match='^local_source_qualification_pending$'):
        current[3].write(record, current[1])
    assert checkpoint(current) == before
    assert_uncalled(spies)


def test_raw_and_pipeline_gate_actual_annotation_dependency_before_writes(current, monkeypatch):
    target, reference = pending_capture(current, 'target')
    root = receive(current, 'root', at=1790003600000)
    current[2].decide(root, 'annotation', target='target')
    item = current[0].submit_source(prepare_direct_text('独立legacy输入'))
    current[2]._link(root, item)
    record = current[2].ensure_raw(current[2].get(root), current[2].identity(root), [],
                                  reference['raw_id'], ledger=current[3])
    current[2]._link(target, bound(current))
    spies = deny_calls(monkeypatch, [(raw, 'place'), (TemporaryArtifacts, 'clean_item'),
                                    (current[0], 'mark_failed'), (current[0], 'mark_succeeded')])
    before = checkpoint(current)
    with pytest.raises(raw.LegacySourceVeto, match='^local_source_qualification_pending$'):
        current[3].write(record, current[1])
    assert service(current, current[1].parent / 'runtime').run(item).state == 'queued'
    assert checkpoint(current) == before
    assert_uncalled(spies)


def test_late_actual_graph_veto_is_not_failed_swallowed_or_cleaned(current, monkeypatch):
    target, reference = pending_capture(current, 'target')
    root = receive(current, 'root', at=1790003600000)
    current[2].decide(root, 'annotation', target='target')
    source = prepare_direct_text('现有legacy合同合成正文')
    item = current[0].submit_source(source)
    current[0].establish_submitted_fact(item, source, parse_submitted_source(source))
    current[2]._link(root, item)
    owner = bound(current)  # unrelated until the real first link below
    distiller = service(current, current[1].parent / 'runtime')
    original = distiller._write_raw
    after_link = []
    def write_raw(row):
        current[2]._link(target, owner)
        after_link.append(checkpoint(current))
        return original(row)
    monkeypatch.setattr(distiller, '_write_raw', write_raw)
    spies = deny_calls(monkeypatch, [(TemporaryArtifacts, 'clean_item'), (current[0], 'mark_failed'),
                                    (raw, 'place'), (pipeline, 'publish')])
    result = distiller.run(item)
    assert after_link and checkpoint(current) == after_link[0]
    assert result.state == current[0].item_bundle(item)['state']
    assert_uncalled(spies)
    distiller.knowledge_model.derive.assert_not_called()


def test_write_holds_actual_db_writer_lock_during_filesystem_placement(current, monkeypatch):
    _, record = pending_capture(current, 'legacy')
    original = raw.place
    blocked = []
    def place(vault, relative, content):
        contender = sqlite3.connect(current[0].path, timeout=0)
        try:
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                contender.execute('BEGIN IMMEDIATE')
            blocked.append(relative)
        finally:
            contender.close()
        return original(vault, relative, content)
    monkeypatch.setattr(raw, 'place', place)
    assert current[3].write(record, current[1]) == 'placed'
    assert blocked == [record['relative_path']]


def test_collision_preserves_existing_files_and_reports_original_retry_code(current):
    _, record = pending_capture(current, 'legacy')
    other = current[1] / 'raw' / '外部' / (record['raw_id'] + '.md')
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_bytes(b'controlled existing collision')
    assert current[3].write(record, current[1]) == 'raw_id_collision'
    assert other.read_bytes() == b'controlled existing collision'
    assert not (current[1] / record['relative_path']).exists()
    updated = current[3].record(record['raw_id'])
    assert updated['attempts'] == record['attempts'] + 1 and updated['last_error'] == 'raw_id_collision'


def test_sync_known_id_collision_is_not_marked_complete(current):
    _, record = pending_capture(current, 'legacy')
    wrong = current[1] / 'raw' / '外部' / (record['raw_id'] + '.md')
    wrong.parent.mkdir(parents=True, exist_ok=True)
    wrong.write_bytes(record['content'].encode())
    before = checkpoint(current)
    with pytest.raises(raw.RawError, match='^raw_id_collision$'):
        current[3].sync(current[1])
    assert checkpoint(current) == before
    assert current[0].setting('raw_synced_vault') is None


@pytest.mark.parametrize('field,value', [('content', 'forged'), ('relative_path', 'raw/forged.md'),
    ('subject_id', 987654321), ('raw_id', 'R-20990101-9999')])
def test_write_rejects_forged_immutable_caller_binding(current, monkeypatch, field, value):
    _, record = pending_capture(current, 'root')
    candidate = {**dict(record), field: value}
    spies = deny_calls(monkeypatch, [(raw, 'place'), (raw, '_attachment_bytes')])
    before = checkpoint(current)
    with pytest.raises(raw.RawError, match='^raw_record_changed$'):
        current[3].write(candidate, current[1])
    assert checkpoint(current) == before
    assert_uncalled(spies)


def corrected(world, record, new_id, *, target=None, supersedes=None):
    capture = world[2].get(record['subject_id'])
    return world[2].render(capture, world[2].identity(record['subject_id']),
        [{'编号': target, '间隔秒': 1}] if target else [], 0, None,
        raw_id=new_id, supersedes=supersedes)


@pytest.mark.parametrize('late', [False, True])
def test_supersede_explicit_or_late_bound_reference_rolls_back_db(current, late):
    _, original = pending_capture(current, 'original')
    ref_cid, reference = pending_capture(current, 'reference', app='other-app')
    current[2]._link(ref_cid, bound(current))
    callback = Mock(side_effect=lambda new_id, _: corrected(current, original, new_id,
        target=reference['raw_id'], supersedes=original['raw_id']))
    before = checkpoint(current)
    with pytest.raises(raw.LegacySourceVeto, match='^local_source_qualification_pending$'):
        current[3].supersede(original['raw_id'], callback, identity='本人',
            referenced_raw_ids=() if late else (reference['raw_id'],))
    assert callback.call_count == (1 if late else 0)
    assert checkpoint(current) == before  # counter rollback, not a 0-allocate claim


def test_supersede_late_callback_external_file_is_not_claimed_rolled_back(current):
    _, original = pending_capture(current, 'original')
    ref_cid, reference = pending_capture(current, 'reference', app='other-app')
    current[2]._link(ref_cid, bound(current))
    marker = current[1].parent / 'synthetic-callback-effect.txt'
    before = full_state(*current[:2])
    def callback(new_id, _):
        marker.write_text('controlled callback already ran', encoding='utf-8')
        return corrected(current, original, new_id, target=reference['raw_id'], supersedes=original['raw_id'])
    with pytest.raises(raw.LegacySourceVeto, match='^local_source_qualification_pending$'):
        current[3].supersede(original['raw_id'], callback, identity='本人')
    assert marker.read_text() == 'controlled callback already ran'
    assert full_state(*current[:2]) == before


def unindexed_file(world, cid, filename_id):
    capture = world[2].get(cid)
    document = world[2].render(capture, world[2].identity(cid), [], 0, None, raw_id=filename_id)
    path = world[1] / document.relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document.content, encoding='utf-8')
    return path


def test_sync_veto_rolls_back_batch_and_does_not_mark_complete(current):
    first = receive(current, 'first')
    current[2].decide(first, 'my_thought')
    unindexed_file(current, first, 'R-20261008-9001')
    second = receive(current, 'second', app='other-app')
    current[2].decide(second, 'my_thought')
    current[2]._link(second, bound(current))
    unindexed_file(current, second, 'R-20261008-9002')
    before = checkpoint(current)
    with pytest.raises(raw.LegacySourceVeto, match='^local_source_qualification_pending$'):
        current[3].sync(current[1])
    assert checkpoint(current) == before
    assert current[0].setting('raw_synced_vault') is None
    assert current[3].record('R-20261008-9001') is None


def test_sync_app_owned_missing_actual_subject_does_not_mark_complete(current):
    cid = receive(current, 'template')
    current[2].decide(cid, 'my_thought')
    path = unindexed_file(current, cid, 'R-20261008-9001')
    text = path.read_text()
    needle = 'capture_id: ' + str(cid)
    assert text.count(needle) == 1
    path.write_text(text.replace(needle, 'capture_id: 987654321'), encoding='utf-8')
    before = checkpoint(current)
    with pytest.raises(raw.LegacySourceVeto, match='^local_source_qualification_pending$'):
        current[3].sync(current[1])
    assert checkpoint(current) == before
    assert current[0].setting('raw_synced_vault') is None


def test_known_legacy_unindexed_file_rebuild_preserves_public_int(current):
    cid = receive(current, 'legacy')
    current[2].decide(cid, 'my_thought')
    path = unindexed_file(current, cid, 'R-20261008-9001')
    payload = path.read_bytes()
    assert current[3].sync(current[1]) == 1
    assert current[3].record(path.stem)['content'].encode() == payload
    assert path.read_bytes() == payload
    assert current[3].sync(current[1]) == 0
    assert current[0].setting('raw_synced_vault') == str(current[1].resolve())


def test_write_pending_only_skips_qualification_and_continues_known_legacy(current):
    bound_cid, blocked = pending_capture(current, 'blocked')
    current[2]._link(bound_cid, bound(current))
    _, legacy = pending_capture(current, 'legacy', app='other-app')
    before_row = dict(current[3].record(blocked['raw_id']))
    unindexed_file(current, bound_cid, 'R-20261008-9001')
    results = current[3].write_pending()
    assert results[blocked['raw_id']] == 'local_source_qualification_pending'
    assert results[legacy['raw_id']] in {'placed', 'already'}
    assert dict(current[3].record(blocked['raw_id'])) == before_row
    assert not (current[1] / blocked['relative_path']).exists()
    assert (current[1] / legacy['relative_path']).read_bytes() == legacy['content'].encode()
    assert current[0].setting('raw_synced_vault') is None


def test_schema_damage_is_not_swallowed_by_pending_or_pipeline(current, monkeypatch):
    cid, record = pending_capture(current, 'legacy')
    item = current[0].submit_source(prepare_direct_text('legacy pending item'))
    with connect(current[0].path) as db:
        db.execute('DROP TRIGGER ingestion_events_proof_unavailable')
    before = checkpoint(current)
    spies = deny_calls(monkeypatch, [(raw, 'place'), (TemporaryArtifacts, 'clean_item')])
    with pytest.raises(raw.LegacySourceVeto, match='^candidate_schema_rebuild_required$'):
        current[3].write_pending()
    distiller = service(current, current[1].parent / 'runtime')
    assert distiller.run(item).state == current[0].item_bundle(item)['state']
    assert checkpoint(current) == before
    assert_uncalled(spies)


@pytest.mark.parametrize('version', [25, 26])
def test_real_historical_material_attachment_write_and_correction_continue(tmp_path, monkeypatch, version):
    root = tmp_path.resolve()
    root.chmod(0o700)
    old_db = historical_module('database', '58bc8ee',
        'ded7f2c87da1da9a67b5c5970bd49b59714a3691295643d4a9fd505dbf7e49ea')
    old_store = historical_module('store', '58bc8ee',
        'f81696db31880c49c0e67df4b82290ff069e43d4e38b4704e9aa96a9ddcf594b')
    path = root / 'historical.sqlite'
    old_db.initialize(path)
    store = old_store.Store(path)
    vault = root / 'vault'
    vault.mkdir(mode=0o700)
    store.set_setting('vault_path', str(vault))
    _, mid, _ = material((store, vault, Ingestion(store)), write_raw=False)
    if version == 26:
        database.initialize(path)
    ledger = raw.RawLedger(store, version='synthetic')
    record = ledger.ensure_material(mid)
    original_attachment = raw._attachment_bytes
    checked = []
    def observe(store, record, attachment, *, db=None):
        assert db is not None and db.in_transaction
        checked.append(db)
        return original_attachment(store, record, attachment, db=db)
    monkeypatch.setattr(raw, '_attachment_bytes', observe)
    assert ledger.write(record, vault) == 'placed'
    assert checked
    assert ledger.write(ledger.record(record['raw_id']), vault) == 'already'
    assert (vault / record['relative_path']).read_bytes() == record['content'].encode()
    def correction(new_id, now):
        with connect(path) as db:
            row = raw.material_row(db, mid)
            members = raw.media(db, mid)
        return raw.render_material(row, members, new_id, app_version='synthetic', migrated=False,
            supersedes=record['raw_id'])
    newer = ledger.supersede(record['raw_id'], correction, identity='第三方')
    assert newer['supersedes'] == record['raw_id']
    assert ledger.write(newer, vault) == 'placed'
    assert (vault / record['relative_path']).read_bytes() == record['content'].encode()


def test_legacy_capture_and_annotation_default_paths_continue(current):
    cid, record = pending_capture(current, 'own')
    assert current[3].write(record, current[1]) == 'placed'
    annotation = receive(current, 'annotation', at=1790003600000)
    current[2].decide(annotation, 'annotation', target='own')
    document = literal_record(current, annotation, target=record['raw_id'])
    assert document['identity'] == '本人附言'
    assert current[3].write(current[3].record(document['raw_id']), current[1]) == 'already'
