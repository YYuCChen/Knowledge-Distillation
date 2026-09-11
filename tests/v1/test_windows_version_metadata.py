"""Version resources must follow the candidate, never a previous Windows release."""
import json
from types import SimpleNamespace

import pytest

from knowledge_distiller.v1 import updates


def test_windows_candidate_reads_its_own_version(tmp_path, monkeypatch):
    monkeypatch.setattr(updates, 'sys', SimpleNamespace(platform='win32', frozen=True, _MEIPASS=str(tmp_path)))
    (tmp_path / 'windows-version.json').write_text(json.dumps({'version': '2026.09.11.8', 'product_version': '1.11'}))
    info = updates.bundle_info()
    assert info['version'] == '2026.09.11.8'
    assert info['display_version'] == '1.11'
    assert info['manual_update_only'] is True
    assert info['feed_url'] == ''


@pytest.mark.parametrize('payload', [None, '{}', '[]', '{invalid', '{"version":"bad","product_version":"1.11"}'])
def test_windows_missing_or_invalid_version_is_not_reported_as_old_release(tmp_path, monkeypatch, payload):
    monkeypatch.setattr(updates, 'sys', SimpleNamespace(platform='win32', frozen=True, _MEIPASS=str(tmp_path)))
    if payload is not None:
        (tmp_path / 'windows-version.json').write_text(payload)
    with pytest.raises(updates.UpdateError, match='Windows 发行版本信息无效'):
        updates.bundle_info()


def test_windows_source_is_identified_as_development(monkeypatch):
    monkeypatch.setattr(updates, 'sys', SimpleNamespace(platform='win32'))
    assert updates.bundle_info()['display_version'] == '开发版本'
