import hashlib
import sqlite3

import pytest

from knowledge_distiller.v1.database import connect
from knowledge_distiller.v1.domain import CapturedMaterial, SourceFact
from knowledge_distiller.v1.media_lifecycle import release_completed, compact, preview
from knowledge_distiller.v1.store import Store


def captured(tmp_path, *, kind='douyin'):
    store = Store(tmp_path / 'isolated.sqlite3')
    store.initialize()
    item = store.create_item('https://www.douyin.com/video/123')
    raw = tmp_path / 'owned.raw'
    raw.write_bytes(b'capture')
    material = store.attach_material(item, CapturedMaterial(kind, '123', 'url', 'url', {}, raw, 1))
    content = b'large original image' * 100000
    with connect(store.path) as db:
        db.execute('INSERT INTO source_media VALUES (?,?,?,?,?,?)',
                   (material, 'image-1', 0, 'image/png', hashlib.sha256(content).hexdigest(), content))
    fact = store.establish_source_fact(material, SourceFact('永远保留的来源文本'))
    return store, item, material, fact, content


def test_completed_releases_content_not_identity_and_reclaims_disk(tmp_path):
    store, item, material, fact, content = captured(tmp_path)
    before = store.path.stat().st_size
    store.mark_succeeded(item)
    assert release_completed(store.path) == len(content)
    assert store.media_members(material) == []
    assert store.media_manifest(material)[0] == dict(member_id='image-1', position=0,
        mime_type='image/png', sha256=hashlib.sha256(content).hexdigest(), content_available=0)
    assert store.item_bundle(item)['snapshot'] == '永远保留的来源文本'
    # Logical release is deliberately distinct from file compaction.
    assert store.path.stat().st_size >= before
    assert compact(store.path) > len(content) // 2
    assert store.path.stat().st_size < before // 2
    store.initialize()
    assert release_completed(store.path) == compact(store.path) == 0
    with connect(store.path) as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
        for sql, args in [('UPDATE source_media SET sha256=?', ('changed',)),
                          ('UPDATE source_media SET content=?', (content,)),
                          ('DELETE FROM source_media', ())]:
            with pytest.raises(sqlite3.IntegrityError, match='immutable'):
                db.execute(sql, args)


@pytest.mark.parametrize('state', ['queued', 'working', 'failed', 'waiting_user'])
def test_unfinished_and_shared_references_protect_bytes(tmp_path, state):
    store, item, material, fact, content = captured(tmp_path)
    store.mark_succeeded(item)
    shared = store.create_item('https://www.douyin.com/video/456')
    with connect(store.path) as db:
        db.execute('UPDATE distill_items SET material_id=?,state=? WHERE item_id=?', (material, state, shared))
    assert release_completed(store.path) == 0
    assert store.media_members(material)[0]['content'] == content
    # SQL guard protects the same boundary, not only the sweep query.
    with connect(store.path) as db:
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            db.execute("UPDATE source_media SET content=X''")
    store.mark_failed(shared, 'reviewing', 'temporary')
    store.dismiss_item(shared)
    assert release_completed(store.path) == len(content)


def test_pending_confirmation_protects_even_dismissed_or_succeeded(tmp_path):
    store, item, material, fact, content = captured(tmp_path)
    store.mark_succeeded(item)
    with connect(store.path) as db:
        db.execute("UPDATE distill_items SET confirmation_json='{}' WHERE item_id=?", (item,))
    assert release_completed(store.path) == 0


def test_upgrade_preserves_history_preview_does_not_write(tmp_path):
    store, item, material, fact, content = captured(tmp_path)
    store.mark_succeeded(item)
    # Construct the previous schema using its exact immutable update guard.
    with connect(store.path) as db:
        db.execute('DROP TABLE media_lifecycle')
        db.execute('DROP TRIGGER source_media_no_update')
        db.execute("""CREATE TRIGGER source_media_no_update BEFORE UPDATE ON source_media
            WHEN EXISTS(SELECT 1 FROM source_facts WHERE material_id=OLD.material_id)
            BEGIN SELECT RAISE(ABORT,'SourceFact media is immutable'); END""")
        db.execute("DROP TABLE IF EXISTS group_decisions")
        db.execute("DROP TABLE IF EXISTS manual_cards")
        db.execute('PRAGMA user_version=15')
    before = store.path.read_bytes()
    report = preview(store.path)
    assert report[0]['bytes'] == len(content) and report[0]['disposition'] == 'eligible'
    assert store.path.read_bytes() == before
    store.initialize()
    assert release_completed(store.path) == 0
    assert store.media_members(material)[0]['content'] == content
    with connect(store.path) as db:
        assert db.execute('SELECT legacy_material_id FROM media_lifecycle').fetchone()[0] == material


def test_local_document_bytes_are_not_platform_media(tmp_path):
    store, item, material, fact, content = captured(tmp_path, kind='pdf')
    store.mark_succeeded(item)
    assert release_completed(store.path) == 0
    assert preview(store.path)[0]['disposition'] == 'protected'
    assert store.media_members(material)[0]['content'] == content


def test_vault_preview_preserves_user_changes_and_shared_references(tmp_path):
    from knowledge_distiller.v1.media_lifecycle import preview_vault
    store, item, material, fact, content = captured(tmp_path)
    vault = tmp_path / 'vault'
    name = f"sf-{fact}-image-1-{hashlib.sha256(content).hexdigest()}.png"
    target = vault / '知识蒸馏器' / '附件' / '示例--kr-1' / name
    target.parent.mkdir(parents=True)
    target.write_bytes(content)
    note = vault / '知识蒸馏器' / '示例--kr-1.md'
    note.write_text(f'用户批注\n![[{name}]]')
    other = vault / '自己的笔记.md'
    other.write_text(f'![[{name}]]')
    with connect(store.path) as db:
        db.execute('''INSERT INTO knowledge_results(source_fact_id,payload_json,published_path,
            published_vault,published_at,created_at) VALUES (?,'{}',?,?,?,?)''',
            (fact, note.relative_to(vault).as_posix(), str(vault), 'now', 'now'))
    before = store.path.read_bytes()
    report = preview_vault(store.path, vault)
    assert report[0]['disposition'] == 'known_copy'
    assert sorted(report[0]['references']) == sorted([note.relative_to(vault).as_posix(), other.name])
    target.write_bytes(b'user replacement')
    assert preview_vault(store.path, vault)[0]['disposition'] == 'protected_modified'
    target.unlink()
    target.symlink_to(other)
    assert preview_vault(store.path, vault)[0]['disposition'] == 'protected_symlink'
    assert store.path.read_bytes() == before
    assert note.read_text().startswith('用户批注') and other.exists()


def test_duplicate_capture_reuses_text_fact_after_media_release(tmp_path):
    store, item, material, fact, content = captured(tmp_path)
    store.mark_succeeded(item)
    release_completed(store.path)
    duplicate = store.create_item('https://www.douyin.com/video/123')
    same = store.attach_material(duplicate, CapturedMaterial('douyin', '123', 'url', 'url', {}, tmp_path/'owned.raw', 1))
    assert same == material
    assert store.item_bundle(duplicate)['source_fact_id'] == fact
    assert store.media_members(material) == []
    assert store.media_manifest(material)[0]['content_available'] == 0


def test_failed_compaction_retries_after_restart_without_releasing_twice(tmp_path, monkeypatch):
    from knowledge_distiller.v1.temporary_artifacts import TemporaryArtifacts
    import knowledge_distiller.v1.media_lifecycle as module
    store, item, material, fact, content = captured(tmp_path)
    store.mark_succeeded(item)
    real_compact = module.compact
    monkeypatch.setattr(module, 'compact', lambda path: (_ for _ in ()).throw(sqlite3.OperationalError('busy')))
    TemporaryArtifacts(store, tmp_path/'runtime').sweep()
    assert store.setting('platform_media_cleanup') == 'OperationalError'
    assert store.media_manifest(material)[0]['content_available'] == 0
    store.initialize()
    monkeypatch.setattr(module, 'compact', real_compact)
    TemporaryArtifacts(store, tmp_path/'runtime').sweep()
    assert store.setting('platform_media_cleanup') is None
    with connect(store.path) as db:
        row = db.execute('SELECT released_bytes,compacted_bytes FROM media_lifecycle').fetchone()
        assert tuple(row) == (len(content), len(content))
