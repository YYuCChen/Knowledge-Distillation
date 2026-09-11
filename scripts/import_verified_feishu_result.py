"""Transfer one completed, verified acceptance receipt without re-running models.

Call only while both application writers are stopped, after backing up the target.
No credentials or platform connection configuration are copied.
"""
import json
from pathlib import Path
import sqlite3


def transfer(source_path,target_path,message_id):
    from knowledge_distiller.v1.database import connect
    from knowledge_distiller.v1.store import Store
    source=sqlite3.connect(Path(source_path).resolve().as_uri()+'?mode=ro',uri=True)
    source.row_factory=sqlite3.Row
    try:
        receipt=source.execute('SELECT * FROM feishu_receipts WHERE message_id=?',(message_id,)).fetchone()
        if receipt is None or receipt['state']!='accepted':raise ValueError('receipt not complete')
        others=source.execute('SELECT text FROM feishu_receipts WHERE message_id!=?',(message_id,)).fetchall()
        if any(row[0].strip()!='test' for row in others):raise ValueError('additional user receipts require a complete transfer plan')
        binding=source.execute('SELECT * FROM feishu_binding WHERE app_id=?',(receipt['app_id'],)).fetchone()
        parts=source.execute('SELECT * FROM feishu_parts WHERE app_id=? AND message_id=?',(receipt['app_id'],message_id)).fetchall()
        if len(parts)!=1 or parts[0]['item_id'] is None:raise ValueError('only a single completed source can transfer')
        item=source.execute('SELECT * FROM distill_items WHERE item_id=?',(parts[0]['item_id'],)).fetchone()
        if item['state']!='succeeded' or item['confirmation_json']:raise ValueError('pending decisions must finish before transfer')
        material=source.execute('SELECT * FROM materials WHERE material_id=?',(item['material_id'],)).fetchone()
        fact=source.execute('SELECT * FROM source_facts WHERE material_id=?',(material['material_id'],)).fetchone()
        knowledge=source.execute('SELECT * FROM knowledge_results WHERE source_fact_id=?',(fact['source_fact_id'],)).fetchone()
        if knowledge is None or json.loads(fact['lineage_json'] or '{}'):raise ValueError('source lineage needs separate review')
        store=Store(Path(target_path));store.initialize()
        with connect(store.path) as target:
            target.execute('BEGIN IMMEDIATE')
            existing=target.execute('SELECT item_id FROM feishu_parts WHERE app_id=? AND message_id=?',(receipt['app_id'],message_id)).fetchone()
            if existing:return existing[0]
            bindings=target.execute('SELECT * FROM feishu_binding').fetchall()
            if bindings:raise ValueError('target already bound; preserve its history')
            if target.execute('SELECT 1 FROM materials WHERE source_kind=? AND source_key=? AND snapshot_key=?',
                              (material['source_kind'],material['source_key'],material['snapshot_key'])).fetchone():
                raise ValueError('target source already exists; explicit deduplication required')
            def insert(table,row,omit=(),replace=None):
                data={k:row[k] for k in row.keys() if k not in omit};data.update(replace or {})
                names=','.join(data);marks=','.join('?' for _ in data)
                return target.execute(f'INSERT INTO {table} ({names}) VALUES ({marks})',tuple(data.values())).lastrowid
            mid=insert('materials',material,('material_id',))
            for media in source.execute('SELECT * FROM source_media WHERE material_id=?',(material['material_id'],)):
                insert('source_media',media,replace={'material_id':mid})
            fid=insert('source_facts',fact,('source_fact_id',),{'material_id':mid})
            insert('knowledge_results',knowledge,('knowledge_result_id',),{'source_fact_id':fid,'published_path':None,'published_vault':None,'published_at':None})
            iid=insert('distill_items',item,('item_id',),{'material_id':mid})
            insert('feishu_binding',binding)
            insert('feishu_receipts',receipt)
            insert('feishu_parts',parts[0],replace={'item_id':iid})
            return iid
    finally:source.close()
