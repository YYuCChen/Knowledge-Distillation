import json
from pathlib import Path

import pytest

from knowledge_distiller.v1 import reading_style
from knowledge_distiller.v1.reading_metadata import source_header


def test_install_is_scoped_repeatable_and_preserves_other_preferences(tmp_path,monkeypatch):
    monkeypatch.setattr(reading_style,'content',lambda:b'.kd-reading{color:#282c32}')
    config=tmp_path/'.obsidian';config.mkdir()
    original={'cssTheme':'AnuPpuccin','baseFontSize':19,'enabledCssSnippets':['user-owned']}
    (config/'appearance.json').write_text(json.dumps(original))
    first=reading_style.install(tmp_path)
    reading_style.install(tmp_path)
    after=json.loads((config/'appearance.json').read_text())
    assert after=={**original,'enabledCssSnippets':['user-owned','kd-reading']}
    first.write_text('user modified CSS')
    with pytest.raises(ValueError):reading_style.install(tmp_path)
    assert first.read_text()=='user modified CSS'


def test_empty_vault_never_installs_into_cwd(tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError):reading_style.install('')
    assert list(tmp_path.iterdir())==[]


def test_source_metadata_keeps_timezone_and_escapes_multiple_authors():
    output=''.join(source_header('x',{'authors':[{'display_name':'甲<script>'},'乙'],
        'published_at':'2026-09-08T12:30:00+08:00'},'https://example.org/a'))
    assert '甲&lt;script&gt;、乙' in output and '<script>' not in output
    assert 'datetime="2026-09-08T12:30:00+08:00"' in output
    assert '2026年09月08日 12:30' in output
    unknown=''.join(source_header('pdf',{'created_at':'2026-09-08T12:30:00+08:00'},'file:///private/source.pdf'))
    assert '<time' not in unknown and 'file:' not in unknown


def test_native_x_timestamp_is_rendered_without_losing_offset():
    output=''.join(source_header('x',{'published_at':'Tue Sep 01 08:13:30 +0000 2026'},'https://x.com/i/status/1'))
    assert 'datetime="2026-09-01T08:13:30+00:00"' in output
    assert '<time ' in output
