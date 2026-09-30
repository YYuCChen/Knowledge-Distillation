"""vault-kit/tools/kb.py against raw files written by the app (raw-interface §8)."""
import datetime as dt
import json
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

KB = Path(__file__).resolve().parents[2] / 'vault-kit' / 'tools' / 'kb.py'


@pytest.fixture
def vault(tmp_path):
    root = tmp_path / 'vault'
    (root / 'tools').mkdir(parents=True)
    (root / 'tools' / 'kb.py').write_bytes(KB.read_bytes())
    run(root, 'init')
    return root


def run(root, *args):
    result = subprocess.run([sys.executable, str(root / 'tools' / 'kb.py'), *args],
                            capture_output=True, text=True, cwd=root)
    return result


def raw(root, folder, raw_id, *, identity='第三方', title='标题', blocks=('source-1',), extra=''):
    day = raw_id[2:10]
    path = root / 'raw' / folder / day[:4] / day[4:6] / f'{raw_id}.md'
    path.parent.mkdir(parents=True, exist_ok=True)
    body = '\n\n'.join(f'第 {n} 段原文。\n\n^{n}' for n in blocks)
    path.write_text(f'---\n编号: {raw_id}\n格式版本: 1\n身份: {identity}\n标题: "{title}"\n{extra}---\n\n{body}\n',
                    encoding='utf-8')
    return path.relative_to(root).as_posix()


def page(root, folder, title, text):
    path = root / 'wiki' / folder / f'{title}.md'
    path.write_text(text, encoding='utf-8')
    return path


def report(root):
    return (root / '.graph' / '检查结果.md').read_text(encoding='utf-8')


SOURCE_PAGE = '''---
主题: [AI]
作者: 某某
平台: 抖音
发布日期: 2026-09-01
素材类型: 社媒
原始文件: {raw}
---

## 摘要
作者认为结果滞后于行动。
## 核心论点
- 作者认为不必纠结短期反馈（{cite}）
## 引发的想法
'''


def test_paragraph_references_count_as_processed_and_are_checked(vault):
    rel = raw(vault, '外部', 'R-20260929-0001', blocks=('source-1', 'source-2'))
    page(vault, '来源', '来源：滞后', SOURCE_PAGE.format(raw=rel, cite=rel + '#^source-2'))
    result = run(vault)
    assert result.returncode == 0, result.stdout + report(vault)
    text = report(vault)
    assert '外部：0 份' in text and '段落不存在' not in text
    page(vault, '来源', '来源：滞后', SOURCE_PAGE.format(raw=rel, cite=rel + '#^source-9'))
    result = run(vault)
    assert result.returncode == 1 and '段落不存在' in report(vault) and 'source-9' in report(vault)


def test_wiki_link_form_to_raw_is_not_a_broken_page_link(vault):
    rel = raw(vault, '外部', 'R-20260929-0001')
    page(vault, '来源', '来源：滞后', SOURCE_PAGE.format(raw=rel, cite=f'[[{rel}#^source-1|原文]]'))
    result = run(vault)
    assert result.returncode == 0, report(vault)
    assert '断链' not in report(vault)


def test_pending_lists_title_and_identity_and_follows_supersede(vault):
    old = raw(vault, '自述', 'R-20260929-0002', identity='本人', title='最初的想法')
    new = raw(vault, '外部', 'R-20260930-0001', title='其实是转来的', extra='取代: R-20260929-0002\n')
    run(vault)
    text = report(vault)
    assert f'{new} · 第三方 · 其实是转来的（取代 R-20260929-0002）' in text
    assert old not in text.split('## 待处理素材')[1]  # The newest file is the one to process.
    page(vault, '来源', '来源：旧引用', SOURCE_PAGE.format(raw=old, cite=old))
    run(vault)
    assert '引用了被取代的素材' in report(vault)


def test_raw_id_takes_the_next_number_across_app_and_agent_files(vault):
    raw(vault, '外部', 'R-20260929-0003')
    raw(vault, '自述', 'R-20260929-0007', identity='本人')
    result = run(vault, 'raw-id', '--date', '2026-09-29')
    assert result.returncode == 0 and result.stdout.strip() == 'R-20260929-0008'
    assert 'raw/自述/2026/09/R-20260929-0008.md' in result.stderr
    fresh = run(vault, 'raw-id', '--date', '2026-10-01')
    assert fresh.stdout.strip() == 'R-20261001-0001'


def test_duplicate_raw_ids_are_an_error(vault):
    raw(vault, '外部', 'R-20260929-0001')
    raw(vault, '自述', 'R-20260929-0001', identity='本人')
    assert run(vault).returncode == 1 and '素材编号重复' in report(vault)


def test_kb_never_modifies_raw(vault):
    rel = raw(vault, '外部', 'R-20260929-0001')
    before = (vault / rel).read_bytes()
    page(vault, '来源', '来源：滞后', SOURCE_PAGE.format(raw=rel, cite=rel + '#^source-1'))
    run(vault)
    assert (vault / rel).read_bytes() == before


def test_kb_reads_envelopes_written_by_the_app(vault, tmp_path):
    """The two sides of the interface agree: app writer → kb.py reader."""
    from knowledge_distiller.v1.raw import RawLedger
    from knowledge_distiller.v1.store import Store
    from tests.v1.test_raw import material
    store = Store(tmp_path / 'app.sqlite3')
    store.initialize()
    store.set_setting('vault_path', str(vault))
    material_id = material(store, 'douyin', '第一段：带冒号的原文。\n\n第二段。',
                           metadata={'source_title': '标题: 含 "引号" 与冒号'})
    ledger = RawLedger(store, version='2.0 (测试)')
    record = ledger.ensure_material(material_id)
    assert ledger.write(record) == 'placed'
    run(vault)
    text = report(vault)
    assert f"{record['relative_path']} · 第三方 · 抖音 · 标题: 含 \"引号\" 与冒号" in text
    cite = record['relative_path'] + '#^source-2'
    page(vault, '来源', '来源：应用写入', SOURCE_PAGE.format(raw=record['relative_path'], cite=cite))
    result = run(vault)
    assert result.returncode == 0, report(vault)


def test_unknown_publication_date_is_allowed(vault):
    # Found in the phase 3 end-to-end run: pasted text has no 产生于, so the page says 未知.
    rel = raw(vault, '外部', 'R-20260929-0001')
    page(vault, '来源', '来源：粘贴', SOURCE_PAGE.replace('发布日期: 2026-09-01', '发布日期: 未知')
         .format(raw=rel, cite=rel + '#^source-1'))
    result = run(vault)
    assert result.returncode == 0 and '日期格式' not in report(vault)
