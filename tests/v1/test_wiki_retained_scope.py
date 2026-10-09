"""Real retained source observation; missing historical events are not coverage."""
import sys

import pytest

from knowledge_distiller.v1.store import Store
from knowledge_distiller.v1.wiki_lock import VaultWriteLock
from knowledge_distiller.v1.wiki_outcomes import retained_no_knowledge_source
from knowledge_distiller.v1.wiki_source_proof import trusted_source_callback
from knowledge_distiller.v1.wiki_staging import prepare_staging
from knowledge_distiller.v1.wiki_tasks import WikiTaskStore
from knowledge_distiller.v1.wiki_typed import CONTRACT, freeze_input
from .test_wiki_staging import KIT, _install


@pytest.mark.parametrize('declaration,eligible,gap', [
    ('渠道: 直接文本\n', True, 'ledger_record_missing'),
    ('渠道: 网页\n覆盖范围: {status: full, scope: 明确保存的原始单页全文}\n', True, 'ledger_record_missing'),
    ('渠道: 直接文本\n截断: true\n', False, 'capture_truncated'),
    ('渠道: 直接文本\n未保留附件: [original.pdf]\n', False, 'known_missing_attachment'),
    ('渠道: 网页\n', False, 'capture_scope_unknown'),
    ('渠道: 网页\n覆盖范围: {status: partial, scope: 第一页}\n', False, None),
    ('渠道: 直接文本\n身份判定: {结果: unknown}\n', False, 'identity_unresolved'),
    ('渠道: 直接文本\n邻接未定: true\n', False, 'relation_unresolved'),
])
def test_actual_retained_scope_without_any_historical_ledger(tmp_path, declaration, eligible, gap):
    root = tmp_path.resolve()
    store = Store(root / 'synthetic.sqlite3'); store.initialize()
    vault, runtime = root / 'vault', root / 'runtime'
    vault.mkdir(); runtime.mkdir(mode=0o700); _install(vault)
    raw = vault / 'raw/外部/2026/10/R-20261008-0001.md'
    raw.parent.mkdir(parents=True)
    raw.write_text('---\n编号: R-20261008-0001\n格式版本: 1\n身份: 第三方\n'
        '收录于: 2026-10-08T12:00:00+08:00\n' + declaration + '---\n\n谢谢。\n\n^source-1\n')
    tasks = WikiTaskStore(store.path, kit_root=KIT, python_executable=sys.executable)
    task = tasks.create_or_reuse(vault, request_kind='all', trigger_source='cli', backend='codex_cli',
        model='fake', effort='medium', outcome_contract=CONTRACT)
    with VaultWriteLock.acquire(vault) as lock:
        snapshot = prepare_staging(vault, runtime, task.task_id, task.raw,
            python_executable=sys.executable, source_kit_root=KIT, lock=lock)
        callback = trusted_source_callback(store, lock)
        _, rows, payload = freeze_input(task, snapshot, 1, callback, runtime_root=runtime)
        source = callback.verify(task=task, snapshot=snapshot, context=rows).manifest['sources'][0]
        assert source['event_keys'] == [] and 'canonical_ingestion_event' not in source['capabilities']
        if gap:
            assert gap in source['gaps']
        assert retained_no_knowledge_source(source) is eligible
