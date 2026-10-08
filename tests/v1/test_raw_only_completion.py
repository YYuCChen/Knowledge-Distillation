"""Raw-only owner/collection completion, real disposable sources and readback."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from knowledge_distiller.v1 import database, raw
from knowledge_distiller.v1.collections import Collections
from knowledge_distiller.v1.database import connect, RAW_OWNER_COLUMNS
from knowledge_distiller.v1.domain import CapturedMaterial, SourceFact
from knowledge_distiller.v1.douyin_collections import Scope, Member, connection_authority
from knowledge_distiller.v1.file_sources import prepare_direct_text
from knowledge_distiller.v1.ingestion import Ingestion, IngestionError
from knowledge_distiller.v1.intake_binding import build_local_binding
from knowledge_distiller.v1.source_parsing import ParsedSource, ParsedMedia
from knowledge_distiller.v1.store import Store
from .test_source_schema26_compat import historical_module, PNG
from .test_web_article import network


@pytest.fixture
def world(tmp_path):
    tmp_path.chmod(0o700)
    store = Store(tmp_path / 'synthetic.sqlite3'); store.initialize()
    vault = tmp_path / 'vault'; vault.mkdir(mode=0o700)
    store.set_setting('vault_path', str(vault))
    return SimpleNamespace(store=store, vault=vault, root=tmp_path)


def source_item(store, text='合成短定义', *, media=True):
    source = prepare_direct_text(text)
    item = store.submit_source(source)
    parsed = ParsedSource(text, {'source_title': 'synthetic'},
        {'fixture_contract': 'controlled-synthetic-source-v1'},
        (ParsedMedia('image-1', 'image/png', PNG),) if media else ())
    store.establish_submitted_fact(item, source, parsed)
    return item


def owner(store, item):
    with connect(store.path) as db:
        return dict(db.execute('SELECT * FROM distill_items WHERE item_id=?', (item,)).fetchone())


def claim(world, item):
    world.store.mark_working(item, 'collecting')
    return owner(world.store, item)['review_revision']


def proof(world, item):
    with connect(world.store.path) as db:
        db.execute('PRAGMA query_only=ON')
        return raw.RawLedger(world.store).read_item(db, item, world.vault)


@pytest.mark.parametrize('version', [25, 26, 27])
def test_actual_legacy_owner_completes_without_rebinding_or_knowledge(tmp_path, version):
    tmp_path.chmod(0o700)
    old_db = historical_module('database', '58bc8ee',
        'ded7f2c87da1da9a67b5c5970bd49b59714a3691295643d4a9fd505dbf7e49ea')
    old_store = historical_module('store', '58bc8ee',
        'f81696db31880c49c0e67df4b82290ff069e43d4e38b4704e9aa96a9ddcf594b')
    path = tmp_path / 'historical.sqlite3'; old_db.initialize(path)
    setup = old_store.Store(path)
    vault = tmp_path / 'vault'; vault.mkdir(mode=0o700)
    setup.set_setting('vault_path', str(vault))
    item = source_item(setup)
    if version == 26:
        db26 = historical_module('database', '2fb751a17abb28620d2dee2ebe1086914675eb6a',
            'e1fc6ae6cd0bfb90b56a4314a53e8c098947f53edef809647f3d03fad82eccf2')
        db26.initialize(path)
    elif version == 27:
        database.initialize(path)
    w = SimpleNamespace(store=Store(path), vault=vault)
    revision = claim(w, item); before = owner(w.store, item)
    receipt = w.store.complete_raw_item(item, expected_revision=revision)
    after = owner(w.store, item)
    assert after['state'] == 'succeeded' and after['phase'] == 'done'
    assert after['ingestion_contract'] == 'legacy'
    assert all(after[k] == before[k] for k in RAW_OWNER_COLUMNS
               if k not in {'state', 'phase', 'updated_at', 'review_revision'})
    assert after['review_revision'] == revision + 1  # existing state-change trigger
    assert proof(w, item) == receipt
    assert receipt.attachments and (vault / receipt.attachments[0][0]).read_bytes() == PNG
    with pytest.raises(raw.RawError, match='^raw_terminal_stale$'):
        w.store.complete_raw_item(item, expected_revision=revision)
    assert w.store.complete_raw_item(item, expected_revision=after['review_revision']) == receipt
    assert owner(w.store, item) == after
    with connect(path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == version
        assert db.execute('SELECT count(*) FROM raw_records').fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM ingestion_events WHERE kind='raw_verified'").fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM knowledge_results').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM wiki_tasks').fetchone()[0] == 0


@pytest.mark.parametrize('damage', ['placement', 'attachment', 'body', 'missing', 'revision'])
def test_failure_never_finishes_or_trusts_written_at(world, monkeypatch, damage):
    item = source_item(world.store); revision = claim(world, item)
    before = owner(world.store, item)
    if damage in {'attachment', 'body', 'missing'}:
        receipt = world.store.complete_raw_item(item)
        record = raw.RawLedger(world.store).record(receipt.raw_id)
        assert record['written_at'] is not None
        if damage == 'attachment':
            (world.vault / receipt.attachments[0][0]).write_bytes(PNG + b'damage')
        elif damage == 'body':
            (world.vault / receipt.relative_path).write_bytes(b'damaged')
        else:
            (world.vault / receipt.relative_path).unlink()
        with pytest.raises((raw.RawError, IngestionError)):
            proof(world, item)
        if damage == 'missing':
            assert world.store.complete_raw_item(item) == receipt  # exact original bytes recovered
        else:
            with pytest.raises((raw.RawError, IngestionError)):
                world.store.complete_raw_item(item)
    else:
        if damage == 'placement':
            monkeypatch.setattr(raw, 'place', Mock(side_effect=OSError('synthetic refusal')))
        with pytest.raises((raw.RawError, IngestionError)):
            world.store.complete_raw_item(item, expected_revision=revision + (damage == 'revision'))
        assert owner(world.store, item) == before


def test_bound_owner_refuses_before_allocation(world, monkeypatch):
    source = prepare_direct_text('合成本地绑定来源')
    item = world.store.submit_local_bound_source(source,
        envelope_json=build_local_binding(source).envelope_json)
    claim(world, item)
    before = owner(world.store, item)
    allocate = Mock(side_effect=AssertionError('bound owner allocated'))
    monkeypatch.setattr(raw, 'allocate', allocate)
    with pytest.raises(raw.LegacySourceVeto, match='^local_source_qualification_pending$'):
        world.store.complete_raw_item(item)
    assert owner(world.store, item) == before
    allocate.assert_not_called()


def test_state_commit_failure_replays_same_raw(world, monkeypatch):
    item = source_item(world.store); claim(world, item)
    original = world.store._finish_raw_item
    monkeypatch.setattr(world.store, '_finish_raw_item', Mock(side_effect=RuntimeError('synthetic crash')))
    with pytest.raises(RuntimeError, match='synthetic crash'):
        world.store.complete_raw_item(item)
    assert owner(world.store, item)['state'] == 'working'
    ledger = raw.RawLedger(world.store)
    record = ledger.current('material', owner(world.store, item)['material_id'])
    body = (world.vault / record['relative_path']).read_bytes()
    monkeypatch.setattr(world.store, '_finish_raw_item', original)
    receipt = world.store.complete_raw_item(item)
    assert receipt.raw_id == record['raw_id']
    assert (world.vault / receipt.relative_path).read_bytes() == body


def test_owner_change_after_write_does_not_clear_confirmation(world, monkeypatch):
    item = source_item(world.store); claim(world, item)
    adapter = raw._item_ingestion(world.store)
    real = adapter._material
    changed = []
    def write_then_change(*args, **kwargs):
        result = real(*args, **kwargs)
        world.store.mark_waiting(item, {'synthetic_new_concern': True})
        changed.append(owner(world.store, item))
        return result
    monkeypatch.setattr(adapter, '_material', write_then_change)
    monkeypatch.setattr(raw, '_item_ingestion', lambda _: adapter)
    with pytest.raises(raw.RawError, match='^raw_terminal_stale$'):
        world.store.complete_raw_item(item)
    assert owner(world.store, item)['state'] == 'waiting_user'
    assert owner(world.store, item) == changed[0]
    assert json.loads(changed[0]['confirmation_json'])['synthetic_new_concern'] is True


def test_actual_migration_record_adopted_without_rewriting(world):
    from knowledge_distiller.v1.raw_migration import _render, _replay_one
    item = source_item(world.store); claim(world, item)
    mid = owner(world.store, item)['material_id']
    with connect(world.store.path) as db:
        rid = raw.allocate(db, '20261008', world.vault)
        document = _render(db, mid, rid)
    assert _replay_one(world.store.path, world.vault, mid, rid, document) == 'placed'
    original = (world.vault / document.relative_path).read_bytes()
    receipt = world.store.complete_raw_item(item)
    assert receipt.raw_id == rid and proof(world, item) == receipt
    assert (world.vault / document.relative_path).read_bytes() == original
    assert raw.RawLedger(world.store).record(rid)['origin'] == 'migration'


@pytest.mark.parametrize('terminal', ['succeeded', 'raw_saved'])
def test_explicit_contract_and_existing_raw_saved_do_not_need_rebinding(tmp_path, terminal):
    from .test_ingestion_schema27_compat import historical
    world = historical(tmp_path)
    database.initialize(world.store.path)
    world.store = Store(world.store.path)
    candidate = Ingestion(world.store)
    item, mid = world.item, world.mid
    revision = claim(world, item)
    if terminal == 'raw_saved':
        candidate.complete_raw_owner(item, world.vault, subject_kind='material',
                                     subject_id=mid, expected_revision=revision)
        before = owner(world.store, item)
    receipt = world.store.complete_raw_item(item)
    assert owner(world.store, item)['state'] == terminal
    assert owner(world.store, item)['ingestion_contract'] == 'raw-verified-v1'
    assert proof(world, item) == receipt
    if terminal == 'raw_saved':
        assert owner(world.store, item) == before


def test_legacy_knowledge_and_publication_are_preserved(world):
    from .test_store import knowledge
    item = source_item(world.store, '持续切换会带来额外损耗。')
    bundle = world.store.item_bundle(item)
    result = world.store.establish_knowledge(bundle['source_fact_id'], knowledge())
    world.store.mark_published(result, 'legacy.md', vault=world.vault)
    (world.vault / 'legacy.md').write_bytes(b'original legacy knowledge')
    with connect(world.store.path) as db:
        before = tuple(db.execute('SELECT * FROM knowledge_results').fetchone())
    claim(world, item)
    receipt = world.store.complete_raw_item(item)
    assert proof(world, item) == receipt
    with connect(world.store.path) as db:
        assert tuple(db.execute('SELECT * FROM knowledge_results').fetchone()) == before
    assert (world.vault / 'legacy.md').read_bytes() == b'original legacy knowledge'


@pytest.mark.parametrize('damage', [None, 'html', 'wire', 'body', 'extraction', 'missing_html'])
def test_web_original_members_are_retained_and_read_back(world, damage):
    from knowledge_distiller.v1.web_article import WebArticleSource, WebArticle, WebCapture
    originals = {'html-1': b'<html><body>synthetic original</body></html>',
                 'wire-1': b'controlled wire entity',
                 'body-1': '合成网页正文'.encode(),
                 'extraction-1': b'<body><p>synthetic original</p></body>'}
    url = 'https://article.example/synthetic'
    article = WebArticle(WebCapture(url, url, (), originals['html-1'], originals['wire-1']),
        ParsedSource(originals['body-1'].decode(), {'source_title': 'synthetic'},
                     {'extracted_body_xml': originals['extraction-1']}))
    source = WebArticleSource(reader=lambda _: article)
    captured = source.capture(url, world.root / 'web-work')
    assert {m.member_id for m in captured.members} == set(originals)
    item = world.store.create_item(url); claim(world, item)
    mid = world.store.attach_material(item, captured)
    world.store.establish_source_fact(mid, SourceFact(captured.metadata['original_description']),
                                      lineage=captured.metadata['web_lineage'])
    receipt = world.store.complete_raw_item(item)
    record = raw.RawLedger(world.store).record(receipt.raw_id)
    attachments = {a['member_id']: a for a in json.loads(record['attachments_json'])}
    assert set(attachments) == set(originals)
    for member, content in originals.items():
        path = world.vault / f"附件/raw/{receipt.raw_id}/{attachments[member]['filename']}"
        assert path.read_bytes() == content
    assert attachments['html-1']['mime_type'] == 'text/html'
    assert attachments['html-1']['filename'] == 'html-1.html'
    assert proof(world, item) == receipt
    if damage:
        member = 'html-1' if damage == 'missing_html' else damage + '-1'
        path = world.vault / f"附件/raw/{receipt.raw_id}/{attachments[member]['filename']}"
        if damage == 'missing_html':
            path.unlink()
        else:
            path.write_bytes(b'controlled original corruption')
        with pytest.raises(IngestionError):
            proof(world, item)
    else:
        assert world.store.complete_raw_item(item) == receipt
        assert_typed_source(world, item, receipt)


def test_original_attachment_identity_cannot_escape_raw_directory(world, monkeypatch):
    source = prepare_direct_text('合成原件安全边界')
    item = world.store.submit_source(source)
    world.store.establish_submitted_fact(item, source, ParsedSource(source.content.decode(), {}, {},
        (ParsedMedia('../../outside', 'text/html', b'<html>synthetic</html>'),)))
    claim(world, item)
    place = Mock(side_effect=AssertionError('unsafe original reached filesystem'))
    monkeypatch.setattr(raw, 'place', place)
    with pytest.raises(raw.RawError, match='^raw_path_unsafe$'):
        world.store.complete_raw_item(item)
    assert owner(world.store, item)['state'] == 'working'
    with connect(world.store.path) as db:
        assert db.execute('SELECT count(*) FROM raw_records').fetchone()[0] == 0
    place.assert_not_called()


@pytest.mark.parametrize('identity', ['my_thought', 'annotation'])
def test_voice_owner_is_saved_as_capture_before_success(world, identity):
    import io
    import wave
    from knowledge_distiller.v1.captures import Captures
    from knowledge_distiller.v1.feishu_inbox import FeishuInbox, Message
    from knowledge_distiller.v1.feishu_intake import FeishuIntake
    from .test_ingestion_schema27_compat import self_text
    world.ingestion = Ingestion(world.store)
    app, target, _, _ = self_text(world, 'target', app='synthetic-voice')
    stream = io.BytesIO()
    with wave.open(stream, 'wb') as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(16000)
        wav.writeframes(b'\0\0' * 16000)
    captures = Captures(world.store, api=SimpleNamespace(download_message_file=lambda *_: stream.getvalue()))
    inbox = FeishuInbox(world.store, app)
    message = Message('voice', 'synthetic-chat', 'synthetic-user', 'user', 'p2p',
        1790003600000, 'audio', json.dumps({'file_key': 'synthetic-audio', 'duration': 1000}), (),
        {'fixture_contract': 'controlled-authenticated-synthetic-message-v1'})
    assert inbox.receive(message)['state'] == 'received'
    capture = captures.for_message(app, 'voice')
    captures.decide(capture['capture_id'], 'my_thought')
    if identity == 'annotation':
        captures.decide(capture['capture_id'], identity, target=target)
    assert FeishuIntake(inbox, links=None, wake=None, api=None, jev=None).process('voice') == 'accepted'
    capture = captures.get(capture['capture_id'])
    item = capture['item_id']; claim(world, item)
    mid = world.store.attach_material(item, CapturedMaterial('feishu_voice', 'voice',
        'feishu-voice://synthetic-voice/voice', 'feishu-voice://synthetic-voice/voice',
        {'fixture_contract': 'synthetic-transcribed-audio'}, Path(capture['audio_path']), 1))
    world.store.establish_source_fact(mid, SourceFact('合成已确认语音全文'))
    receipt = world.store.complete_raw_item(item)
    assert receipt.subject_kind == 'capture' and receipt.subject_id == capture['capture_id']
    assert receipt.identity == ('本人附言' if identity == 'annotation' else '本人')
    assert proof(world, item) == receipt
    assert owner(world.store, item)['state'] == 'succeeded'
    assert Path(capture['audio_path']).read_bytes() == stream.getvalue()


class Discovery:
    def __init__(self, scope): self.scope = scope
    def discover(self, *args, **kwargs): return {'scopes': [self.scope], 'choices': []}


def collection(world):
    world.store.save_connection('douyin', None)
    scope = Scope('creator_collection', '900', '合成集合', 'creator',
        (Member('101', '一', True, 0, 'v1'), Member('102', '二', True, 0, 'v2')),
        '2026-10-08T00:00:00+00:00', connection_authority(world.store))
    service = Collections(world.store, Discovery(scope))
    preview = service.preview(['https://www.douyin.com/collection/900'])
    op = service.confirm(preview['token'], [scope.signature])[0]
    return service, op


def collect_member(world, member):
    item = member['item_id']; claim(world, item)
    key = member['native_id']
    path = world.root / (key + '.mp4'); path.write_bytes(b'synthetic video')
    metadata = {'original_description': '合成描述', 'native_content_version': member['native_version'],
                'session_authority': connection_authority(world.store)}
    mid = world.store.attach_material(item, CapturedMaterial('douyin', key,
        'https://www.douyin.com/video/' + key, 'https://www.douyin.com/video/' + key,
        metadata, path, 1))
    world.store.establish_source_fact(mid, SourceFact('合成短定义 ' + key))
    return world.store.complete_raw_item(item)


def test_collection_finishes_without_knowledge_or_combined_and_keeps_relationship(world, monkeypatch):
    import knowledge_distiller.v1.collection_model as model
    combined = Mock(side_effect=AssertionError('old combined called'))
    monkeypatch.setattr(model, 'derive_combined', combined)
    service, op = collection(world)
    original = service.detail(op)['manifest']
    for member in service.detail(op)['members']:
        collect_member(world, member)
    service.run(op, SimpleNamespace(knowledge_model=Mock()))
    result = service.detail(op)
    assert result['state'] == 'succeeded' and result['consequence'] == 'complete'
    assert result['manifest'] == original and result['result'] is None
    assert all(m['knowledge_result_id'] is None for m in result['members'])
    combined.assert_not_called()
    with connect(world.store.path) as db:
        assert db.execute('SELECT count(*) FROM knowledge_results').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM collection_results').fetchone()[0] == 0


def test_collection_cannot_trust_success_without_raw_or_corrupt_raw(world):
    service, op = collection(world)
    members = service.detail(op)['members']
    receipt = collect_member(world, members[0])
    world.store.mark_succeeded(members[1]['item_id'])  # actual legacy state, no source/raw proof
    assert service._reconcile(op) == 'partial'
    (world.vault / receipt.relative_path).write_bytes(b'corrupt')
    assert service._reconcile(op) == 'failed'
    assert service.detail(op)['consequence'] == 'failed'


def test_source_fact_without_raw_head_is_caught_per_collection_member(world):
    service, op = collection(world)
    members = service.detail(op)['members']
    collect_member(world, members[0])
    item = members[1]['item_id']
    path = world.root / 'missing-raw.mp4'; path.write_bytes(b'synthetic video')
    mid = world.store.attach_material(item, CapturedMaterial('douyin', '102',
        'https://www.douyin.com/video/102', 'https://www.douyin.com/video/102',
        {'fixture_contract': 'synthetic-missing-raw', 'native_content_version': members[1]['native_version'],
         'session_authority': connection_authority(world.store)}, path, 1))
    world.store.establish_source_fact(mid, SourceFact('合成有来源但无raw'))
    world.store.mark_succeeded(item)
    with pytest.raises(IngestionError, match='^raw_heads_ambiguous$'):
        proof(world, item)
    assert service._reconcile(op) == 'partial'
    service.run(op, SimpleNamespace())
    assert service.detail(op)['state'] == 'partial'


def assert_typed_source(world, item, receipt):
    from dataclasses import replace
    from knowledge_distiller.v1.wiki_lock import VaultWriteLock
    from knowledge_distiller.v1.wiki_source_proof import trusted_source_callback
    from knowledge_distiller.v1.wiki_typed import freeze_input
    from .test_source_schema26_compat import frozen
    assert owner(world.store, item)['ingestion_contract'] == 'legacy'
    record = raw.RawLedger(world.store).record(receipt.raw_id)
    task, snapshot, context = frozen((world.store, world.vault, Ingestion(world.store)), record)
    runtime = world.root / 'runtime'; runtime.mkdir(mode=0o700)
    attempt = runtime / 'wiki-tasks' / task.task_id / 'attempts' / ('c' * 32)
    attempt.parent.mkdir(parents=True, mode=0o700)
    snapshot.task_root.rename(attempt)
    (attempt / 'control').mkdir(mode=0o700)
    snapshot = replace(snapshot, task_root=attempt, workspace=attempt / 'workspace',
                       control=attempt / 'control', backup=attempt / 'backup')
    with VaultWriteLock.acquire(world.vault) as lock:
        callback = trusted_source_callback(world.store, lock)
        verified = callback.verify(task=task, snapshot=snapshot, context=context)
        assert callback(task=task, snapshot=snapshot, context=context) == verified.digest
        manifest = verified.manifest
        binding, rows, payload = freeze_input(task, snapshot, 1, callback, runtime_root=runtime)
        assert rows == context and binding['input_sha256']
        assert payload['source_proof_sha256'] == verified.digest
    source = manifest['sources'][0]
    assert {'canonical_source_binding', 'canonical_ingestion_event'} <= set(source['capabilities'])
    assert 'current_source_unqualified' not in source['gaps']
    assert source['event_keys'] and source['source_binding_sha256']


def test_new_legacy_owner_proof_is_admitted_by_real_typed_source_callback(world):
    item = source_item(world.store); claim(world, item)
    receipt = world.store.complete_raw_item(item)
    assert_typed_source(world, item, receipt)


@pytest.mark.parametrize('busy', [False, True])
def test_web_submission_real_worker_finish_and_wiki_source_proof(network, monkeypatch, busy):
    from contextlib import nullcontext
    from knowledge_distiller.v1 import web_article as web
    from knowledge_distiller.v1.web import create_app
    from knowledge_distiller.v1.worker import SingleWorker
    from knowledge_distiller.v1.wiki_lock import VaultWriteLock
    from knowledge_distiller.v1.wiki_source_proof import trusted_source_callback
    from .test_web_intake_integration import build, synthetic_store
    from .test_web_article import HTML, PUBLIC, injected, response
    from .test_source_schema26_compat import frozen
    root = network[3]
    store = synthetic_store(root / 'real-web-worker.sqlite3')
    vault = root / 'synthetic-vault'; vault.mkdir(mode=0o700)
    store.set_setting('vault_path', str(vault))
    reader = lambda url: web.read_web_article(url, resolver=lambda h, p: [PUBLIC], extractor=injected)
    distiller = build(store, root, reader)
    distiller.vault = vault
    worker = SingleWorker(store, distiller)
    import knowledge_distiller.v1.worker as worker_module
    clock = [100.0]
    monkeypatch.setattr(worker_module.time, 'monotonic', lambda: clock[0])
    with VaultWriteLock.acquire(vault) if busy else nullcontext():
        result = create_app(store, object()).test_client().post('/submissions',
            data={'content': '[合成网页](https://article.example/page)'})
        assert result.status_code == 302
        assert owner(store, 1)['ingestion_contract'] == 'legacy'
        network[0].append(response())
        assert worker.run_one() == 1
        if busy:
            pending = store.item_bundle(1)
            assert pending['state'] == 'queued' and pending['error_code'] is None
            assert pending['phase'] == 'publishing'
            assert pending['source_fact_id'] and pending['knowledge_result_id'] is None
            assert worker.run_one() is None and worker.run_one() is None
            assert len(network[1]) == 1
            with connect(store.path) as db:
                assert db.execute('SELECT count(*) FROM raw_records').fetchone()[0] == 0
    if busy:
        clock[0] += 1.01
        assert worker.run_one() == 1
        assert store.item_bundle(1)['source_fact_id'] == pending['source_fact_id']
    row = store.item_bundle(1)
    assert row['state'] == 'succeeded' and row['phase'] == 'done'
    assert row['source_fact_id'] and row['knowledge_result_id'] is None
    world = SimpleNamespace(store=store, vault=vault)
    receipt = proof(world, 1)
    record = raw.RawLedger(store).record(receipt.raw_id)
    attachments = json.loads(record['attachments_json'])
    original = next(a for a in attachments if a['member_id'] == 'html-1')
    assert (vault / '附件' / 'raw' / receipt.raw_id / original['filename']).read_bytes() == HTML
    task, snapshot, context = frozen((store, vault, Ingestion(store)), record)
    with VaultWriteLock.acquire(vault) as lock:
        callback = trusted_source_callback(store, lock)
        verified = callback.verify(task=task, snapshot=snapshot, context=context)
        assert callback(task=task, snapshot=snapshot, context=context) == verified.digest
    source = verified.manifest['sources'][0]
    assert {'canonical_source_binding', 'canonical_ingestion_event'} <= set(source['capabilities'])
    assert 'current_source_unqualified' not in source['gaps']
    assert len(network[1]) == 1
    with connect(store.path) as db:
        assert db.execute('SELECT count(*) FROM knowledge_results').fetchone()[0] == 0


def test_collection_pending_and_cancelled_are_not_complete(world):
    service, op = collection(world)
    members = service.detail(op)['members']
    collect_member(world, members[0])
    assert service._reconcile(op) == 'queued'
    world.store.mark_waiting(members[1]['item_id'], {'synthetic_concern': True})
    assert service._reconcile(op) == 'waiting_user'
    service.cancel(op, service.detail(op)['revision'])
    assert service._reconcile(op) == 'cancelled'


@pytest.mark.parametrize('in_collection', [False, True])
def test_worker_vault_busy_retains_sources_and_defers_without_blocking_intake(world, monkeypatch, in_collection):
    from knowledge_distiller.v1.worker import SingleWorker
    from knowledge_distiller.v1.wiki_lock import VaultWriteLock
    from knowledge_distiller.v1.feishu_inbox import FeishuInbox, Message
    import knowledge_distiller.v1.worker as worker_module
    clock = [100.0]
    monkeypatch.setattr(worker_module.time, 'monotonic', lambda: clock[0])
    acquisitions = []; recognitions = []; attempts = []
    class Collector:
        def run(self, item):
            attempts.append(item)
            row = world.store.item_bundle(item)
            if row['source_fact_id'] is None:
                acquisitions.append(item)
                path = world.root / (str(item) + '.mp4'); path.write_bytes(b'synthetic recording')
                metadata = {'fixture_contract': 'synthetic-lock-contention'}
                with connect(world.store.path) as db:
                    member = db.execute('SELECT * FROM collection_members WHERE item_id=?', (item,)).fetchone()
                if member:
                    metadata.update(native_content_version=member['native_version'],
                                    session_authority=connection_authority(world.store))
                mid = world.store.attach_material(item, CapturedMaterial('douyin',
                    member['native_id'] if member else str(item), row['submitted_url'],
                    row['submitted_url'], metadata, path, 1))
                recognitions.append(item)
                world.store.establish_source_fact(mid, SourceFact('合成已识别来源'))
            world.store.complete_raw_item(item)
    worker = SingleWorker(world.store, Collector())
    if in_collection:
        service, operation = collection(world)
        ids = [m['item_id'] for m in service.detail(operation)['members']]
    else:
        ids = [world.store.create_item('https://www.douyin.com/video/101')]
    inbox = FeishuInbox(world.store, 'synthetic-busy')
    inbox.bind(bot_open_id='bot', user_open_id='user', chat_id='chat', start_ms=0)
    with VaultWriteLock.acquire(world.vault):
        for _ in ids:
            assert worker.run_one() is not None
        assert all(owner(world.store, item)['state'] == 'queued' for item in ids)
        assert all(owner(world.store, item)['error_code'] is None for item in ids)
        retained = [world.store.item_bundle(item)['source_fact_id'] for item in ids]
        assert all(retained)
        assert worker.run_one() is None and worker.run_one() is None
        assert attempts == ids  # No retry spin while the monotonic delay is active.
        delivered = Message('busy-text', 'chat', 'user', 'user', 'p2p', 1790000000000,
            'text', json.dumps({'text': '合成锁内新投递'}), (), {'fixture_contract': 'synthetic'})
        assert inbox.receive(delivered)['state'] == 'received'
        incoming = world.store.create_item('https://www.douyin.com/video/999')
        assert worker.run_one() == incoming  # A deferred head cannot strand new collection/source work.
        assert world.store.item_bundle(incoming)['source_fact_id']
        assert owner(world.store, incoming)['state'] == 'queued'
        with connect(world.store.path) as db:
            assert db.execute('SELECT count(*) FROM raw_records').fetchone()[0] == 0
            assert db.execute('SELECT count(*) FROM knowledge_results').fetchone()[0] == 0
    assert worker.run_one() is None
    clock[0] += 1.01
    for _ in (*ids, incoming):
        assert worker.run_one() is not None
    assert all(owner(world.store, item)['state'] == 'succeeded' for item in (*ids, incoming))
    assert acquisitions == [*ids, incoming] and recognitions == acquisitions
    assert [world.store.item_bundle(item)['source_fact_id'] for item in ids] == retained
    for item in (*ids, incoming):
        proof(world, item)
    if in_collection:
        assert service.detail(operation)['state'] == 'succeeded'
    with connect(world.store.path) as db:
        assert db.execute('SELECT count(*) FROM knowledge_results').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM collection_results').fetchone()[0] == 0
