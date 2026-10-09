"""R09 display persistence over synthetic collections; no UI integration."""
import hashlib

import pytest

from knowledge_distiller.v1.collection_visibility import CollectionVisibility, CollectionVisibilityError
from knowledge_distiller.v1.database import connect, SCHEMA_VERSION
from knowledge_distiller.v1.store import Store
from knowledge_distiller.v1.worker import SingleWorker
from .test_collections import setup, accept, Boundary, drain


def snapshot(store, vault):
    with connect(store.path) as db:
        schema = tuple(tuple(r) for r in db.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name'))
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name!='settings'")]
        rows = {table: sorted((tuple(r) for r in db.execute('SELECT * FROM "' + table + '"')), key=repr)
                for table in tables}
    files = {str(p.relative_to(vault)): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in vault.rglob('*') if p.is_file()}
    return schema, rows, files


def test_hide_restore_preserve_all_data_and_restart_readback(setup):
    from .test_topic_web import establish_historical_knowledge
    store, collections, _, root = setup
    operation, _ = accept(collections)
    boundary = Boundary(store, root)
    drain(SingleWorker(store, boundary))
    assert collections.detail(operation)['state'] == 'succeeded'
    establish_historical_knowledge(store, '历史正文与知识仍可读取。', boundary.vault)
    before = snapshot(store, boundary.vault)
    settings = store.settings()
    visibility = CollectionVisibility(store)
    assert visibility.visibility(operation) == 'visible'
    assert store.settings() == settings
    key = f'collection_visibility:{operation}'
    assert visibility.hide(operation) == 'hidden'
    assert visibility.hide(operation) == 'hidden'
    assert store.settings() == {**settings, key: 'hidden'}
    assert snapshot(store, boundary.vault) == before
    restarted = Store(store.path)
    restarted.initialize()
    fresh = CollectionVisibility(restarted)
    assert fresh.visibility(operation) == 'hidden'
    assert collections.detail(operation)['members']
    assert fresh.restore(operation) == 'visible'
    assert fresh.restore(operation) == 'visible'
    assert restarted.settings() == {**settings, key: 'visible'}
    assert snapshot(restarted, boundary.vault) == before


@pytest.mark.parametrize('state', ['queued', 'working', 'waiting_user'])
def test_active_operations_cannot_be_hidden(setup, state):
    store, collections, _, root = setup
    operation, _ = accept(collections)
    with connect(store.path) as db:
        db.execute('UPDATE collection_operations SET state=? WHERE operation_id=?', (state, operation))
    before = snapshot(store, root / 'vault')
    settings = store.settings()
    visibility = CollectionVisibility(store)
    with pytest.raises(CollectionVisibilityError, match='collection_visibility_active'):
        visibility.hide(operation)
    assert visibility.visibility(operation) == 'visible'
    assert store.settings() == settings
    assert snapshot(store, root / 'vault') == before


@pytest.mark.parametrize('state', ['cancelled', 'partial', 'failed'])
def test_stopped_operations_require_explicit_choice_and_can_restore(setup, state):
    store, collections, _, root = setup
    operation, _ = accept(collections)
    with connect(store.path) as db:
        db.execute('UPDATE collection_operations SET state=? WHERE operation_id=?', (state, operation))
    visibility = CollectionVisibility(store)
    settings = store.settings()
    before = snapshot(store, root / 'vault')
    assert visibility.visibility(operation) == 'visible'
    assert store.settings() == settings
    visibility.hide(operation)
    assert visibility.visibility(operation) == 'hidden'
    assert snapshot(store, root / 'vault') == before
    # Other execution APIs may resume it; a stored display choice is not stop.
    if state == 'cancelled':
        collections.resume(operation, collections.detail(operation)['revision'])
        assert collections.detail(operation)['state'] == 'queued'
        assert visibility.visibility(operation) == 'hidden'
    visibility.restore(operation)
    assert visibility.visibility(operation) == 'visible'


def test_cancel_request_with_working_member_is_not_stopped(setup):
    store, collections, _, _ = setup
    operation, _ = accept(collections)
    with connect(store.path) as db:
        db.execute("UPDATE collection_operations SET state='cancelled' WHERE operation_id=?", (operation,))
        db.execute("UPDATE distill_items SET state='working' WHERE item_id=?",
                   (collections.detail(operation)['members'][0]['item_id'],))
    with pytest.raises(CollectionVisibilityError, match='collection_visibility_active'):
        CollectionVisibility(store).hide(operation)


@pytest.mark.parametrize('operation', [True, False, 0, -1, '1', None, 1.0, 2**63, 999999])
def test_wrong_ids_rejected_without_setting_writes(setup, operation):
    store, collections, _, _ = setup
    accept(collections)
    before = store.settings()
    visibility = CollectionVisibility(store)
    for method in (visibility.visibility, visibility.hide, visibility.restore):
        with pytest.raises(CollectionVisibilityError):
            method(operation)
    assert store.settings() == before


def test_hidden_setting_is_specific_to_operation_and_bad_value_is_not_hidden(setup):
    from dataclasses import replace
    store, collections, discovery, _ = setup
    first, _ = accept(collections)
    collections.cancel(first, collections.detail(first)['revision'])
    discovery.scope = replace(discovery.scope, key='901')
    second, _ = accept(collections)
    visibility = CollectionVisibility(store)
    visibility.hide(first)
    assert visibility.visibility(second) == 'visible'
    store.set_setting(f'collection_visibility:{second}', 'broken')
    with pytest.raises(CollectionVisibilityError, match='collection_visibility_invalid_setting'):
        visibility.visibility(second)
    visibility.restore(second)
    assert visibility.visibility(second) == 'visible'


def test_real_schema21_upgrade_keeps_explicit_hidden_choice(tmp_path):
    from .test_data_upgrade_probe_versions import _prepare
    from knowledge_distiller.v1.collections import Collections
    path, vault, _ = _prepare(tmp_path, 21)
    store = Store(path)
    collections = Collections(store)
    collections.cancel(71, 1)
    before = collections.detail(71)
    settings = store.settings()
    files = {str(p.relative_to(vault)): p.read_bytes() for p in vault.rglob('*') if p.is_file()}
    visibility = CollectionVisibility(store)
    visibility.hide(71)
    store.initialize()
    with connect(path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == SCHEMA_VERSION
    assert CollectionVisibility(Store(path)).visibility(71) == 'hidden'
    after = Collections(Store(path)).detail(71)
    assert {key: after[key] for key in before} == before
    assert {key: after[key] for key in after.keys() - before.keys()} == {
        'ingestion_contract': 'legacy',
        'source_binding_sha256': None,
        'relation_binding_sha256': None,
    }
    assert store.settings() == {**settings, 'collection_visibility:71': 'hidden'}
    assert {str(p.relative_to(vault)): p.read_bytes() for p in vault.rglob('*') if p.is_file()} == files
    assert visibility.restore(71) == 'visible'
