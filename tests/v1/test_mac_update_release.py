"""Release preparation must reject a silently manual target before signing."""
import plistlib
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[2]

def test_manual_target_cannot_be_published_as_delta_release(tmp_path):
    app=tmp_path/'App.app';(app/'Contents').mkdir(parents=True)
    (app/'Contents/Info.plist').write_bytes(plistlib.dumps({'KDManualUpdateOnly':True}))
    notes=tmp_path/'notes.md';notes.write_text('notes')
    command=[sys.executable,str(ROOT/'scripts/prepare_mac_update.py'),'--app',str(app),
        '--output',str(tmp_path/'out'),'--sdk',str(tmp_path/'sdk'),'--notes',str(notes)]
    result=subprocess.run(command,capture_output=True,text=True)
    assert result.returncode != 0
    assert '正式发行目标仍禁止差量安装' in result.stderr
    assert not list((tmp_path/'out').iterdir())


def test_release_requires_explicit_previous_baseline(tmp_path):
    app=tmp_path/'App.app';(app/'Contents').mkdir(parents=True)
    (app/'Contents/Info.plist').write_bytes(plistlib.dumps({'KDManualUpdateOnly':False}))
    notes=tmp_path/'notes.md';notes.write_text('notes')
    result=subprocess.run([sys.executable,str(ROOT/'scripts/prepare_mac_update.py'),'--app',str(app),
        '--output',str(tmp_path/'out'),'--sdk',str(tmp_path/'sdk'),'--notes',str(notes)],capture_output=True,text=True)
    assert result.returncode != 0
    assert '正式更新缺少差量基线' in result.stderr
    assert not list((tmp_path/'out').iterdir())
