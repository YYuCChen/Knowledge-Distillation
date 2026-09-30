"""raw/ interface, app side (docs/engineering/raw-interface.md §9). Synthetic data only."""
import hashlib
import json
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest
import yaml

from knowledge_distiller.v1 import raw
from knowledge_distiller.v1.database import connect
from knowledge_distiller.v1.domain import SourceFact
from knowledge_distiller.v1.markdown import _source_blocks
from knowledge_distiller.v1.raw import RawLedger, parse_envelope
from knowledge_distiller.v1.store import Store

PNG = b'\x89PNG\r\n\x1a\n' + b'synthetic image bytes' * 20


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path / 'isolated.sqlite3')
    result.initialize()
    vault = tmp_path / 'vault'
    vault.mkdir()
    result.set_setting('vault_path', str(vault))
    return result


def material(store, kind, snapshot, *, key='k1', url='https://example.org/a', metadata=None, lineage=None,
             uncertainties=(), media=(), created_at='2026-09-29T02:10:05+00:00'):
    """A material with its finished source fact, as the V1 pipeline leaves it."""
    with connect(store.path) as db:
        material_id = db.execute('''INSERT INTO materials(source_kind, source_key, submitted_url, canonical_url,
                metadata_json, created_at) VALUES (?,?,?,?,?,?)''',
            (kind, key, url, url if url.startswith('http') else '', json.dumps(metadata or {}, ensure_ascii=False),
             created_at)).lastrowid
        for position, (member, content) in enumerate(media):
            db.execute('INSERT INTO source_media VALUES (?,?,?,?,?,?)',
                       (material_id, member, position, 'image/png', hashlib.sha256(content).hexdigest(), content))
        # The finished fact exactly as the V1 pipeline stores it (capture checks are not under test).
        db.execute('''INSERT INTO source_facts(material_id, snapshot, uncertainties_json, created_at, lineage_json)
                      VALUES (?,?,?,?,?)''', (material_id, snapshot, json.dumps(list(uncertainties), ensure_ascii=False),
                                              created_at, json.dumps(lineage or {}, ensure_ascii=False)))
    return material_id


def written(store, material_id, version='2.0 (测试)'):
    ledger = RawLedger(store, version=version)
    record = ledger.ensure_material(material_id)
    assert ledger.write(record) in {'placed', 'already'}
    record = ledger.record(record['raw_id'])
    vault = Path(store.setting('vault_path'))
    return record, (vault / record['relative_path']).read_text(encoding='utf-8'), vault


def split(text):
    front, rest = text[4:].split('\n---\n', 1)
    return yaml.safe_load(front), rest


def paragraphs(body):
    """(anchor, text) pairs as written: text, blank, ^anchor."""
    lines, result = body.strip('\n').split('\n'), []
    for index, line in enumerate(lines):
        if line.startswith('^source-'):
            result.append((line[1:], lines[index - 2]))
    return result


CHANNELS = [
    ('douyin', '抖音'), ('xiaohongshu', '小红书'), ('zhihu', '知乎'), ('weibo', '微博'), ('x', 'X'),
    ('youtube', 'YouTube'), ('bilibili', 'B站'), ('direct_text', '直接文本'), ('markdown', 'Markdown'),
    ('pdf', 'PDF'), ('epub', 'EPUB'), ('image', '图片'),
]


@pytest.mark.parametrize('kind,channel', CHANNELS)
def test_each_channel_writes_a_valid_envelope_and_v1_paragraphs(store, kind, channel):
    snapshot = '第一段原文：先说结论。\n\n第二段 *原样* 保留 [[不是链接]] 的字符。\n\n```\ncode block\n\nstays whole\n```'
    url = 'https://example.org/a' if kind not in {'direct_text', 'markdown', 'pdf', 'epub', 'image'} else '提交的文件.md'
    metadata = {'source_title': '来源标题：带冒号', 'author': {'display_name': '作者甲'},
                'published_at': '2026-08-03T20:14:00+08:00'}
    material_id = material(store, kind, snapshot, url=url, metadata=metadata)
    record, text, vault = written(store, material_id)
    envelope, body = split(text)
    assert envelope['编号'] == record['raw_id'] and raw.ID_RE.fullmatch(record['raw_id'])
    assert (envelope['格式版本'], envelope['身份'], envelope['渠道']) == (1, '第三方', channel)
    assert envelope['标题'] == '来源标题：带冒号' and envelope['作者'] == '作者甲'
    assert str(envelope['产生于']).startswith('2026-08-03')
    collected = datetime.fromisoformat('2026-09-29T02:10:05+00:00').astimezone()
    assert envelope['收录于'] == collected and envelope['收录于'].utcoffset() == collected.utcoffset()
    assert record['raw_id'].startswith('R-' + collected.strftime('%Y%m%d') + '-')
    assert record['relative_path'] == f"raw/外部/{collected:%Y/%m}/{record['raw_id']}.md"
    assert ('原链接' in envelope) is url.startswith('http')
    assert envelope['应用记录']['material_id'] == material_id
    # Same paragraphs, same ^source-N as the V1 note (markdown._source_blocks).
    expected = [(block.anchor, block.text) for block in _source_blocks(snapshot)]
    assert paragraphs(body) != [] and [a for a, _ in paragraphs(body)] == [a for a, _ in expected]
    for anchor, block in expected:
        assert f'{block}\n\n^{anchor}\n' in body  # Verbatim original, never escaped or summarized.
    assert parse_envelope(text)['身份'] == '第三方' and parse_envelope(text)['标题'] == '来源标题：带冒号'
    assert '\r' not in text and text.endswith('\n')


def test_image_sources_embed_bytes_at_their_place_and_list_released_ones(store):
    snapshot = '配文第一段。\n\n[图片 image-1 OCR]\n图片里的文字'
    start = snapshot.index('图片里的文字')
    lineage = {'image_ocr': [{'member_id': 'image-1', 'engine': 'apple_vision',
                              'recognition_model': 'VNRecognizeTextRequestRevision3', 'runtime_version': '27.0',
                              'lines': [{'text': '图片里的文字', 'start': start, 'end': start + 6,
                                         'polygon': [[0, 0], [60, 0], [60, 20], [0, 20]]}]}]}
    material_id = material(store, 'xiaohongshu', snapshot, lineage=lineage,
                           media=[('image-1', PNG), ('image-2', PNG + b'2')])  # image-2: no located text.
    record, text, vault = written(store, material_id)
    envelope, body = split(text)
    assert (vault / f"附件/raw/{record['raw_id']}/image-1.png").read_bytes() == PNG
    assert (vault / f"附件/raw/{record['raw_id']}/image-2.png").read_bytes() == PNG + b'2'
    assert body.index('![[附件/raw/') < body.index('图片里的文字')
    assert '^image-1' in body and '^image-2' in body
    assert envelope['取得方式']['识别'].startswith('apple_vision VNRecognizeTextRequestRevision3')


def test_released_image_is_named_not_invented(store):
    snapshot = '正文。'
    material_id = material(store, 'weibo', snapshot, media=[('image-1', PNG)])
    with connect(store.path) as db:
        db.execute('DROP TRIGGER source_media_no_update')
        db.execute("UPDATE source_media SET content=X'' WHERE material_id=?", (material_id,))
    record, text, vault = written(store, material_id)
    envelope, body = split(text)
    assert envelope['未保留附件'] == ['image-1'] and '![[' not in body
    assert not (vault / '附件/raw').exists()


def test_corrections_keep_every_original_recognition(store):
    snapshot = '今天讲十神。\n\n数据上报量要核对。\n\n这里听不清楚。'
    ai_start = snapshot.index('上报量')
    uncertainties = [
        {'text': '十身', 'replacement': '十神', 'by': 'human'},
        {'text': '采购量', 'replacement': '上报量', 'by': 'ai', 'status': 'repaired', 'original_text': '采购量',
         'start': ai_start, 'end': ai_start + 3},
        {'text': '要合对', 'replacement': '要核对', 'by': 'human', 'action': 'local_transcription'},
        {'start': snapshot.index('听不清楚'), 'end': snapshot.index('听不清楚') + 4, 'text': '听不清楚',
         'original_text': '挺不清楚', 'reason': '无法确认', 'status': 'unresolved', 'by': 'human'},
        {'start': 0, 'end': 2, 'text': '今天', 'reason': '轻微不确定'},
    ]
    material_id = material(store, 'douyin', snapshot, uncertainties=uncertainties,
                           lineage={'primary_asr': {'text': 'asr', 'chunks': []}})
    envelope, _ = split(written(store, material_id)[1])
    assert envelope['订正'] == [
        {'片段': 'source-1', '原识别': '十身', '订正为': '十神', '方式': '用户核对'},
        {'片段': 'source-2', '原识别': '采购量', '订正为': '上报量', '方式': '模型修复'},
        {'片段': 'source-2', '原识别': '要合对', '订正为': '要核对', '方式': '用户订正'},
    ]
    assert envelope['存疑'] == [{'片段': 'source-3', '文字': '听不清楚'}]
    assert envelope['取得方式']['识别']  # ASR recorded, never left blank.


def test_repeated_delivery_reuses_the_id_and_writes_once(store, tmp_path):
    material_id = material(store, 'douyin', '一段原文。')
    first, text, vault = written(store, material_id)
    second, again, _ = written(store, material_id)
    assert first['raw_id'] == second['raw_id'] and text == again
    assert len(list((vault / 'raw').rglob('*.md'))) == 1


def test_existing_target_is_never_overwritten(store):
    material_id = material(store, 'douyin', '一段原文。')
    ledger = RawLedger(store, version='2.0 (测试)')
    record = ledger.ensure_material(material_id)
    target = Path(store.setting('vault_path')) / record['relative_path']
    target.parent.mkdir(parents=True)
    target.write_text('用户或其他程序放在这里的不同内容', encoding='utf-8')
    assert ledger.write(record) == 'raw_target_conflict'
    assert target.read_text(encoding='utf-8') == '用户或其他程序放在这里的不同内容'
    pending = ledger.record(record['raw_id'])
    assert pending['written_at'] is None and pending['last_error'] == 'raw_target_conflict'
    target.write_text(record['content'], encoding='utf-8')  # Identical bytes count as written.
    assert ledger.write(record) == 'already' and ledger.record(record['raw_id'])['written_at']


def test_unavailable_vault_is_backfilled_with_the_same_bytes(store, tmp_path):
    material_id = material(store, 'x', '正文。', media=[('image-1', PNG)])
    store.set_setting('vault_path', str(tmp_path / 'unmounted'))
    ledger = RawLedger(store, version='2.0 (测试)')
    record = ledger.ensure_material(material_id)
    assert ledger.write(record) == 'vault_unavailable' and ledger.write_pending() == {}
    (tmp_path / 'unmounted').mkdir()
    assert ledger.write_pending() == {record['raw_id']: 'placed'}
    target = tmp_path / 'unmounted' / record['relative_path']
    assert target.read_bytes() == record['content'].encode('utf-8')
    assert (tmp_path / 'unmounted' / f"附件/raw/{record['raw_id']}/image-1.png").read_bytes() == PNG


def test_supersede_writes_a_new_file_and_keeps_the_old(store):
    material_id = material(store, 'douyin', '一段原文。')
    old, old_text, vault = written(store, material_id)
    ledger = RawLedger(store, version='2.0 (测试)')
    def corrected(raw_id, now):
        text = old_text.replace(old['raw_id'], raw_id, 1).replace('\n---\n', f"\n取代: {old['raw_id']}\n---\n", 1)
        return raw.RawDocument(raw.relative_path(raw_id, '第三方', now), text)
    new = ledger.supersede(old['raw_id'], corrected, identity='第三方')
    assert ledger.write(new) == 'placed'
    assert (vault / old['relative_path']).read_text(encoding='utf-8') == old_text
    assert parse_envelope((vault / new['relative_path']).read_text(encoding='utf-8'))['取代'] == old['raw_id']
    assert ledger.current('material', material_id)['raw_id'] == new['raw_id']
    with pytest.raises(raw.RawError, match='already_superseded'):
        ledger.supersede(old['raw_id'], corrected, identity='第三方')


def test_ids_skip_numbers_already_used_in_the_vault(store):
    vault = Path(store.setting('vault_path'))
    day = datetime.fromisoformat('2026-09-29T02:10:05+00:00').astimezone().strftime('%Y%m%d')
    agent = vault / 'raw/自述' / day[:4] / day[4:6] / f'R-{day}-0003.md'
    agent.parent.mkdir(parents=True)
    agent.write_text('---\n编号: x\n---\n', encoding='utf-8')  # e.g. written by kb.py raw-id
    record, _, _ = written(store, material(store, 'douyin', '原文。'))
    assert record['raw_id'] == f'R-{day}-0004'
    assert agent.read_text(encoding='utf-8') == '---\n编号: x\n---\n'


def test_records_and_files_are_immutable_in_the_database(store):
    record, _, _ = written(store, material(store, 'douyin', '原文。'))
    with connect(store.path) as db:
        for sql in ("UPDATE raw_records SET content='changed'", 'DELETE FROM raw_records',
                    "UPDATE raw_records SET written_at='later'"):
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(sql)


def test_pipeline_writes_raw_before_distillation_and_never_blocks_the_note(tmp_path):
    from .test_pipeline import distiller
    from knowledge_distiller.v1.knowledge_model import KnowledgeModelError
    service, store, source, model, vault = distiller(tmp_path)
    store.set_setting('vault_path', str(vault))
    item = store.create_item('https://v.douyin.com/a/')
    assert service.run(item).state == 'succeeded'
    files = list((vault / 'raw').rglob('*.md'))
    assert len(files) == 1 and '持续切换会带来额外损耗。' in files[0].read_text(encoding='utf-8')
    envelope = parse_envelope(files[0].read_text(encoding='utf-8'))
    assert envelope['身份'] == '第三方' and envelope['渠道'] == '抖音'
    # A knowledge failure still leaves the finished material in raw/.
    class Rejecting:
        def derive(self, *args, **kwargs):
            raise KnowledgeModelError('knowledge_not_qualified', rejection_reason='不足以形成知识')
    other = tmp_path / 'second'
    other.mkdir()
    service2, store2, *_ , vault2 = distiller(other)
    store2.set_setting('vault_path', str(vault2))
    service2.knowledge_model = Rejecting()
    item2 = store2.create_item('https://v.douyin.com/b/')
    assert service2.run(item2).state == 'failed'
    assert len(list((vault2 / 'raw').rglob('*.md'))) == 1


def test_raw_conflict_does_not_block_v1_publication(tmp_path, monkeypatch):
    from .test_pipeline import distiller
    service, store, source, model, vault = distiller(tmp_path)
    store.set_setting('vault_path', str(vault))
    monkeypatch.setattr(raw, 'place', lambda *args: 'conflict')
    item = store.create_item('https://v.douyin.com/a/')
    assert service.run(item).state == 'succeeded'
    with connect(store.path) as db:
        assert db.execute('SELECT last_error FROM raw_records').fetchone()[0] == 'raw_target_conflict'


# ───────────────────────── Existing data (migration) ─────────────────────────

def legacy_database(tmp_path, count=3):
    """A schema-19 V1.3 database: the same tables minus the raw index."""
    store = Store(tmp_path / 'data' / 'knowledge.sqlite3')
    store.initialize()
    ids = []
    for index, moment in enumerate(['2026-09-06T22:01:45+00:00', '2026-09-05T08:00:00+00:00',
                                    '2026-09-06T23:30:00+00:00'][:count]):
        ids.append(material(store, 'douyin', f'第 {index} 条原文。\n\n第二段。', key=f'k{index}',
                            media=[('image-1', PNG + bytes([index]))], created_at=moment))
    with connect(store.path) as db:
        db.execute('DROP TABLE raw_records')
        db.execute('DROP TABLE raw_counters')
        db.execute('PRAGMA user_version = 19')
    return store, ids


def test_migration_is_read_only_on_v13_databases_and_repeatable(tmp_path):
    from knowledge_distiller.v1.raw_migration import run
    store, ids = legacy_database(tmp_path)
    vault = tmp_path / 'vault'
    (vault / '知识蒸馏器').mkdir(parents=True)
    (vault / '知识蒸馏器' / '已有笔记.md').write_text('V1 笔记不动', encoding='utf-8')
    before = store.path.read_bytes()
    dry = run(store.path.parent, vault, dry_run=True)
    assert dry['planned'] == 3 and not (vault / 'raw').exists()
    report = run(store.path.parent, vault)
    assert [r['outcome'] for r in report['results']] == ['placed'] * 3
    assert store.path.read_bytes() == before  # Schema 19 stays openable by V1.3.
    files = sorted((vault / 'raw').rglob('*.md'))
    assert len({f.stem for f in files}) == 3
    # Ids follow materials.created_at, so the earliest material gets the day's first number.
    first = [f for f in files if f'material_id: {ids[1]},' in f.read_text(encoding='utf-8')][0]
    local = datetime.fromisoformat('2026-09-05T08:00:00+00:00').astimezone()
    assert first.stem == f"R-{local:%Y%m%d}-0001"
    snapshot = {f: f.read_bytes() for f in files}
    again = run(store.path.parent, vault)
    assert again['planned'] == 0 and {f: f.read_bytes() for f in sorted((vault / 'raw').rglob('*.md'))} == snapshot
    assert (vault / '知识蒸馏器' / '已有笔记.md').read_text(encoding='utf-8') == 'V1 笔记不动'
    acquisition = split(first.read_text(encoding='utf-8'))[0]['取得方式']
    assert acquisition['导出'] == 'V1 数据库存量导出' and acquisition['应用版本'] == '未记录（V1 数据库）'
    # A second copy of the same data produces the same bytes.
    copy_data, copy_vault = tmp_path / 'copy' / 'data', tmp_path / 'vault-copy'
    shutil.copytree(store.path.parent, copy_data)
    copy_vault.mkdir()
    run(copy_data, copy_vault)
    assert {f.relative_to(copy_vault): f.read_bytes() for f in (copy_vault / 'raw').rglob('*.md')} == \
           {f.relative_to(vault): f.read_bytes() for f in (vault / 'raw').rglob('*.md')}


def test_after_upgrade_the_app_reuses_migrated_ids(tmp_path):
    from knowledge_distiller.v1.raw_migration import run
    store, ids = legacy_database(tmp_path, count=1)
    vault = tmp_path / 'vault'
    vault.mkdir()
    run(store.path.parent, vault)
    migrated = next((vault / 'raw').rglob('*.md'))
    store.initialize()  # The 2.0 app upgrades to schema 20.
    store.set_setting('vault_path', str(vault))
    ledger = RawLedger(store, version='2.0 (测试)')
    assert ledger.write_pending() == {}  # Sync indexes the migrated file.
    record = ledger.ensure_material(ids[0])
    assert record['raw_id'] == migrated.stem and record['origin'] == 'vault'
    assert len(list((vault / 'raw').rglob('*.md'))) == 1


def test_migration_records_its_files_on_schema_20_and_refuses_a_running_app(tmp_path):
    import fcntl
    from knowledge_distiller.v1.raw_migration import run
    store, ids = legacy_database(tmp_path, count=2)
    store.initialize()
    vault = tmp_path / 'vault'
    vault.mkdir()
    with (store.path.parent / '.instance.lock').open('a') as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(SystemExit, match='请先退出应用'):
            run(store.path.parent, vault)
    run(store.path.parent, vault)
    with connect(store.path) as db:
        rows = db.execute("SELECT subject_id, origin, written_at FROM raw_records ORDER BY subject_id").fetchall()
    assert [(r['subject_id'], r['origin']) for r in rows] == [(ids[0], 'migration'), (ids[1], 'migration')]
    assert all(r['written_at'] for r in rows)
    assert run(store.path.parent, vault)['planned'] == 0


def test_packaged_command_line_runs_the_same_migration(tmp_path, monkeypatch):
    import knowledge_distiller.v1.mac_app as mac_app
    store, ids = legacy_database(tmp_path, count=1)
    vault = tmp_path / 'vault'
    vault.mkdir()
    monkeypatch.setattr(mac_app, 'configure_bundled_runtime', lambda: None)
    with pytest.raises(SystemExit) as done:
        mac_app.main(['--migrate-raw', '--data-dir', str(store.path.parent), '--vault', str(vault)])
    assert done.value.code == 0 and len(list((vault / 'raw').rglob('*.md'))) == 1
