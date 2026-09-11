import plistlib
from pathlib import Path
import subprocess
import sys
import zipfile
import pytest

@pytest.mark.parametrize('manual', [True, False])
def test_mac_archive_requires_enabled_signed_updater(tmp_path, manual):
    archive=tmp_path/'app.zip'
    info={'CFBundleVersion':'2026.09.11.12','KDManualUpdateOnly':manual,
          'KDCodeSigningMode':'local-certificate','SUPublicEDKey':'key','SURequireSignedFeed':True}
    with zipfile.ZipFile(archive,'w') as output:
        output.writestr('App.app/Contents/Info.plist',plistlib.dumps(info))
    result=subprocess.run([sys.executable,str(Path(__file__).resolve().parents[1]/'scripts/verify_archive.py'),
                           str(archive),'--version','2026.09.11.12','--commit','test'],capture_output=True,text=True)
    assert (result.returncode==0) is (not manual)
