"""Persisted component JSON remains readable under a GBK desktop locale."""
import json
from pathlib import Path

import pytest

from knowledge_distiller.v1.component_install import recover, require_recovered
from knowledge_distiller.v1.local_records import write_record
from knowledge_distiller.v1.program_tree import identity
from knowledge_distiller.v1.updates import UpdateError


@pytest.fixture
def gbk_default(monkeypatch):
    original = Path.read_text
    def legacy_locale(path, *args, **kwargs):
        if not args and kwargs.get('encoding') is None:
            kwargs['encoding'] = 'gbk'
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', legacy_locale)


def test_chinese_install_journal_recovers_under_gbk(tmp_path, gbk_default):
    target = tmp_path / '知识蒸馏器'
    previous = target.with_name(target.name + '.component-previous')
    (previous / '_internal').mkdir(parents=True)
    (previous / 'KnowledgeDistiller.exe').write_bytes(b'old program')
    (previous / '_internal/windows-version.json').write_text('{"version":"1"}', encoding='utf-8')
    expected = identity(previous, 'windows-x86_64')
    root = tmp_path / '知识数据'
    (root / 'updates').mkdir(parents=True)
    journal = root / 'updates/component-install-journal.json'
    write_record(journal, {'target':str(target.resolve()), 'platform':'windows-x86_64',
                          'phase':'startup', 'target_identity':'0' * 64,
                          'had_target':True, 'had_database':False})
    assert '知识蒸馏器'.encode('utf-8') in journal.read_bytes()
    with pytest.raises(UpdateError, match='中断'):
        require_recovered(root)
    assert recover(target, root)['recovered']
    assert identity(target, 'windows-x86_64') == expected
    assert not journal.exists()


def test_component_helper_reads_chinese_plan_before_execution(tmp_path, gbk_default, monkeypatch):
    from knowledge_distiller.v1 import component_update_helper as helper
    root, target = tmp_path / '知识数据', tmp_path / '知识蒸馏器'
    plan = tmp_path / '计划.json'
    plan.write_text(json.dumps({'data_root':str(root), 'info':{'bundle':str(target)}},
                               ensure_ascii=False), encoding='utf-8')
    class PlanDecoded(BaseException):pass
    def validate(actual_root, actual_target):
        assert actual_root == root and actual_target == target
        raise PlanDecoded()
    monkeypatch.setattr(helper, 'validate_install_paths', validate)
    with pytest.raises(PlanDecoded):
        helper.run(plan)
