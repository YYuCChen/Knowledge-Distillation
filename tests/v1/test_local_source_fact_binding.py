"""Synthetic disposable Store tests. No Factory, OCR/model or product caller."""
from copy import deepcopy
from dataclasses import replace
import hashlib
import json

import pytest

from knowledge_distiller.v1 import store as store_module
from knowledge_distiller.v1.database import connect
from knowledge_distiller.v1.file_sources import prepare_direct_text, prepare_file, parse_submitted_source
from knowledge_distiller.v1.intake_binding import build_local_binding
from knowledge_distiller.v1.source_files import copy_path, SourceCopyError
from knowledge_distiller.v1.source_parsing import ParsedMedia
from knowledge_distiller.v1.store import Store, SourceReviewConflict
from .test_local_intake_storage import _synthetic_document_bytes


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / 'disposable' / 'state.sqlite')
    value.initialize()
    return value


def rows(store, table):
    with connect(store.path) as db:
        return [dict(r) for r in db.execute(f'SELECT * FROM {table} ORDER BY rowid')]


def owner(store, item):
    return next(r for r in rows(store, 'distill_items') if r['item_id'] == item)


def working(store, source):
    binding = build_local_binding(source)
    item = store.submit_local_bound_source(source, envelope_json=binding.envelope_json)
    assert store.claim_next_work() == ('item', item)
    return item, owner(store, item)['review_revision']


def review(parsed):
    return {'schema': 1, 'snapshot': parsed.snapshot, 'uncertainties': parsed.uncertainties,
            'lineage': deepcopy(parsed.lineage)}


def commit(store, item, revision, source, parsed, result=None):
    return store.establish_submitted_fact(item, source, parsed, expected_revision=revision,
                                        review_result=review(parsed) if result is None else result)


TABLES = ('distill_items', 'submitted_sources', 'ingestion_events', 'materials',
          'source_media', 'source_facts', 'source_review_results', 'confirmation_decisions',
          'group_decisions', 'raw_records', 'knowledge_results')


def snapshot(store):
    return {table: rows(store, table) for table in TABLES}


@pytest.mark.parametrize('kind', ['direct_text', 'markdown'])
def test_actual_pure_parse_commits_full_fact_retains_original(store, kind):
    source = (prepare_direct_text('重复\r\n重复 é', {'author': '声明作者'}) if kind == 'direct_text'
              else prepare_file('Chapter:1.md', '\ufeff---\ntitle: 合成标题\n---\n重复\r\n重复 é'.encode()))
    item, revision = working(store, source)
    parsed = parse_submitted_source(source)
    original = snapshot(store)
    fact_id = commit(store, item, revision, source, parsed)
    fact, = rows(store, 'source_facts')
    material, = rows(store, 'materials')
    result, = rows(store, 'source_review_results')
    assert fact['source_fact_id'] == fact_id and fact['snapshot'] == parsed.snapshot
    assert json.loads(fact['lineage_json']) == parsed.lineage
    assert json.loads(material['metadata_json']) == parsed.metadata
    assert (material['source_kind'], material['source_key'], material['submitted_url']) == (
        source.source_kind, source.source_key, source.label)
    assert material['snapshot_key'].startswith('local-source-fact-v1:')
    saved = json.loads(result['result_json'])
    assert saved['snapshot'] == parsed.snapshot and saved['lineage'] == parsed.lineage
    parse_binding = saved['local_parse_binding']
    encoded = json.dumps(parse_binding['parse_input'], ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    assert hashlib.sha256(encoded.encode()).hexdigest() == parse_binding['parse_input_sha256']
    assert material['snapshot_key'].endswith(parse_binding['parse_input_sha256'])
    assert store.local_intake_binding(item) == (source, build_local_binding(source))
    assert rows(store, 'submitted_sources') == original['submitted_sources']
    assert rows(store, 'ingestion_events') == original['ingestion_events']
    assert rows(store, 'source_media') == rows(store, 'raw_records') == rows(store, 'knowledge_results') == []
    assert owner(store, item)['state'] == 'working'
    assert owner(store, item)['review_revision'] == revision + 1
    completed = snapshot(store)
    with pytest.raises(SourceReviewConflict):
        commit(store, item, owner(store, item)['review_revision'], source, parsed)
    assert snapshot(store) == completed


def test_same_bytes_distinct_labels_and_legacy_namespace(store):
    content = '重复\n重复'.encode()
    legacy = prepare_file('old.md', content)
    legacy_item = store.submit_source(legacy)
    legacy_fact = store.establish_submitted_fact(legacy_item, legacy, parse_submitted_source(legacy))
    ids = [legacy_fact]
    for label in ('Chapter:1.md', 'Chapter:2.md'):
        source = prepare_file(label, content)
        # Remove the legacy queued item from FIFO only in this synthetic fixture.
        with connect(store.path) as db:
            db.execute("UPDATE distill_items SET state='succeeded' WHERE item_id=?", (legacy_item,))
        item, revision = working(store, source)
        ids.append(commit(store, item, revision, source, parse_submitted_source(source)))
    assert len(set(ids)) == 3
    materials = rows(store, 'materials')
    assert [m['source_key'] for m in materials] == [legacy.source_key] * 3
    assert materials[0]['snapshot_key'] == 'legacy'
    assert len({m['snapshot_key'] for m in materials}) == 3


@pytest.mark.parametrize('revision_value', [None, True, -1, '1', 999])
def test_expected_revision_is_mandatory_typed_and_current(store, revision_value):
    source = prepare_direct_text('合成原文')
    item, _ = working(store, source)
    before = snapshot(store)
    with pytest.raises(SourceReviewConflict):
        commit(store, item, revision_value, source, parse_submitted_source(source))
    assert snapshot(store) == before


@pytest.mark.parametrize('field,value', [('state', 'queued'), ('phase', 'distilling'),
                                       ('confirmation_json', '{"resolved":[{"by":"human"}]}')])
def test_ownership_or_human_pending_cannot_be_overwritten(store, field, value):
    source = prepare_direct_text('人判保留')
    item, _ = working(store, source)
    with connect(store.path) as db:
        db.execute(f'UPDATE distill_items SET {field}=? WHERE item_id=?', (value, item))
    before = snapshot(store)
    with pytest.raises(SourceReviewConflict):
        commit(store, item, owner(store, item)['review_revision'], source, parse_submitted_source(source))
    assert snapshot(store) == before


@pytest.mark.parametrize('change', ['bytes', 'key', 'label', 'metadata', 'bytearray'])
def test_caller_source_must_match_whole_frozen_input(store, change):
    source = prepare_direct_text('完整来源')
    item, revision = working(store, source)
    altered = {'bytes': replace(source, content=b'changed'), 'key': replace(source, source_key='0'*64),
               'label': replace(source, label='different'), 'metadata': replace(source, metadata={'extra': 'x'}),
               'bytearray': replace(source, content=bytearray(source.content))}[change]
    before = snapshot(store)
    with pytest.raises(ValueError):
        commit(store, item, revision, altered, parse_submitted_source(source))
    assert snapshot(store) == before


@pytest.mark.parametrize('change', ['hash', 'key', 'span', 'bool_span', 'snapshot', 'metadata',
                                  'uncertainty', 'media', 'duplicate_media', 'nan', 'nul', 'surrogate', 'key_type'])
def test_full_parsed_output_cannot_be_replaced_by_self_consistent_forgery(store, change):
    source = prepare_direct_text('重复重复中文')
    item, revision = working(store, source)
    parsed = parse_submitted_source(source)
    if change in {'hash', 'key', 'span', 'bool_span', 'nan', 'key_type'}:
        lineage = deepcopy(parsed.lineage)
        if change == 'hash': lineage['snapshot_sha256'] = '0'*64
        elif change == 'key': lineage['source_key'] = '0'*64
        elif change == 'span': lineage['spans'][0]['end'] += 1
        elif change == 'bool_span': lineage['spans'][0]['start'] = False
        elif change == 'nan': lineage['geometry'] = float('nan')
        else: lineage[1] = 'invalid'
        parsed = replace(parsed, lineage=lineage)
    elif change in {'snapshot', 'nul', 'surrogate'}:
        parsed = replace(parsed, snapshot={'snapshot':'different', 'nul':'原\x00文', 'surrogate':'\ud800'}[change])
    elif change == 'metadata': parsed = replace(parsed, metadata={'submitted_name': 'forged'})
    elif change == 'uncertainty': parsed = replace(parsed, uncertainties=({'start':0, 'end':2, 'text':'重复'},))
    else:
        member = ParsedMedia('image-1', 'image/png', b'synthetic-media')
        parsed = replace(parsed, media=(member, member) if change == 'duplicate_media' else (member,))
    before = snapshot(store)
    with pytest.raises((ValueError, UnicodeError)):
        commit(store, item, revision, source, parsed)  # review agrees with forgery; still rejected
    assert snapshot(store) == before


@pytest.mark.parametrize('change', ['missing', 'bool_schema', 'snapshot', 'lineage', 'failure', 'extra'])
def test_complete_review_must_match_exact_final_parsed(store, change):
    source = prepare_direct_text('完整审阅')
    item, revision = working(store, source)
    parsed = parse_submitted_source(source)
    result = review(parsed)
    if change == 'missing': del result['uncertainties']
    elif change == 'bool_schema': result['schema'] = True
    elif change == 'snapshot': result['snapshot'] = '不同'
    elif change == 'lineage': result['lineage'] = {}
    else: result[change] = 'not accepted'
    before = snapshot(store)
    with pytest.raises(ValueError):
        commit(store, item, revision, source, parsed, result)
    assert snapshot(store) == before


@pytest.mark.parametrize('fault', ['missing', 'growth', 'final_readback', 'owner_readback'])
def test_actual_file_and_final_transaction_readback_failure_leaves_no_half_commit(store, monkeypatch, fault):
    source = prepare_file('original.md', b'synthetic original')
    item, revision = working(store, source)
    before = snapshot(store)
    path = copy_path(store.path.parent, source.source_kind, source.source_key, source.label)
    def replace_fixture_bytes(content):
        original_mode = path.stat().st_mode & 0o7777
        try:
            path.chmod(original_mode | 0o200)
            path.write_bytes(content)
        finally:
            path.chmod(original_mode)
    if fault == 'missing': path.unlink()
    elif fault == 'growth': replace_fixture_bytes(source.content + b'extra')
    elif fault == 'final_readback':
        real = store._local_intake_binding
        calls = []
        def fail_last(db, value):
            calls.append(value)
            if len(calls) == 2:
                replace_fixture_bytes(source.content + b'changed between actual readbacks')
            return real(db, value)
        monkeypatch.setattr(store, '_local_intake_binding', fail_last)
    else:
        real_sync = store_module._sync_manual_cards
        def drift(db, value):
            real_sync(db, value)
            db.execute("UPDATE distill_items SET submitted_title='synthetic drift' WHERE item_id=?", (value,))
        monkeypatch.setattr(store_module, '_sync_manual_cards', drift)
    with pytest.raises((ValueError, SourceCopyError)):
        commit(store, item, revision, source, parse_submitted_source(source))
    assert snapshot(store) == before


@pytest.mark.parametrize('kind', ['pdf', 'epub'])
def test_actual_document_parser_with_injected_converter_cannot_invent_execution_trace(store, kind):
    from knowledge_distiller.v1.docling_source import DoclingSourceResult, DocumentEntry, DocumentProvenance
    source = prepare_file('synthetic.' + kind, _synthetic_document_bytes(kind))
    item, revision = working(store, source)
    body = 'Synthetic original source.' if kind == 'pdf' else '合成完整正文。'
    class Converter:
        def convert_bytes(self, content, input_kind):
            assert input_kind == kind
            return DoclingSourceResult((DocumentEntry('text', body,
                (DocumentProvenance(page=1, charspan=(0,len(body))),) if kind == 'pdf' else (), '#text-1'),),
                1 if kind == 'pdf' else None, runtime_version='synthetic-fixture-1')
    class NoOcr:
        def __call__(self, *args, **kwargs):
            raise AssertionError('text-only fixture must not OCR')
    parsed = parse_submitted_source(source, converter=Converter(), ocr=NoOcr())
    assert parsed.snapshot == body
    before = snapshot(store)
    # D0 preserves decoded PDF Info values as plain strings. Both document
    # kinds still lack a trusted execution receipt and must hit the trace gate.
    code = 'local_parse_trace_required'
    with pytest.raises(ValueError, match='^' + code + '$'):
        commit(store, item, revision, source, parsed)
    assert snapshot(store) == before
    assert store.local_intake_binding(item)[0] == source


def test_distinct_declared_metadata_keeps_source_and_relation_identity(store):
    ids = []
    for author in ('声明甲', '声明乙'):
        source = prepare_direct_text('同一完整正文', {'author': author})
        item, revision = working(store, source)
        ids.append(commit(store, item, revision, source, parse_submitted_source(source)))
    assert ids[0] != ids[1]
    materials = rows(store, 'materials')
    assert len({m['snapshot_key'] for m in materials}) == 2
    assert [json.loads(m['metadata_json'])['author'] for m in materials] == [
        {'display_name': name, 'provenance': 'user-declared'} for name in ('声明甲', '声明乙')]
    assert all(json.loads(build_local_binding(store.local_intake_binding(r['item_id'])[0]).relation_json)
               ['intent']['identity'] == 'unresolved' for r in rows(store, 'distill_items'))


def test_bare_digest_owner_does_not_fallback_to_legacy_fact(store):
    source = prepare_direct_text('不是完整envelope')
    item = store.submit_source(source, ingestion_contract='raw-verified-v1',
                               source_binding_sha256='1'*64, relation_binding_sha256='2'*64)
    assert store.claim_next_work() == ('item', item)
    before = snapshot(store)
    with pytest.raises(ValueError):
        commit(store, item, owner(store, item)['review_revision'], source, parse_submitted_source(source))
    assert snapshot(store) == before


@pytest.mark.parametrize('table', ['confirmation_decisions', 'group_decisions'])
def test_existing_human_audit_even_without_pending_is_not_overwritten(store, table):
    source = prepare_direct_text('保留已保存判断')
    item, revision = working(store, source)
    with connect(store.path) as db:
        if table == 'confirmation_decisions':
            db.execute('INSERT INTO confirmation_decisions VALUES (?,?,?,?,?)',
                       (item, 'synthetic-human-revision', 'manual', '人工原话', 'saved'))
        else:
            db.execute('INSERT INTO group_decisions VALUES (?,?,?,?,?,?,?,?,?)',
                       (item, 'request-1', 'group-1', 'synthetic-human-revision', '1'*64,
                        '2'*64, '{"by":"human"}', '{"action":"unable"}', '2026-10-08T00:00:00+00:00'))
    before = snapshot(store)
    with pytest.raises(SourceReviewConflict):
        commit(store, item, revision, source, parse_submitted_source(source))
    assert snapshot(store) == before
