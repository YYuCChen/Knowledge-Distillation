"""Validate complete candidate archives before they can be collected."""
import argparse
import json
import plistlib
from pathlib import Path
import zipfile


def main():
    p=argparse.ArgumentParser()
    p.add_argument('archive',type=Path)
    p.add_argument('--version',required=True)
    p.add_argument('--commit',required=True)
    args=p.parse_args()
    with zipfile.ZipFile(args.archive) as archive:
        names=archive.namelist()
        for name in names:
            path=Path(name)
            if path.is_absolute() or '..' in path.parts:raise ValueError('Unsafe archive path')
        failure=archive.testzip()
        if failure:raise ValueError('Corrupt ZIP member: '+failure)
        windows=[name for name in names if name.endswith('/_internal/windows-version.json')]
        mac=[name for name in names if name.endswith('.app/Contents/Info.plist') and name.count('/')==2]
        if windows:
            if len(windows)!=1:raise ValueError('Ambiguous Windows candidate')
            meta=json.loads(archive.read(windows[0]))
            assert meta['version']==args.version and meta['source_commit']==args.commit
        elif mac:
            if len(mac)!=1:raise ValueError('Ambiguous Mac candidate')
            meta=plistlib.loads(archive.read(mac[0]));assert meta['CFBundleVersion']==args.version
            assert meta.get('KDManualUpdateOnly') is False and not meta.get('KDUpdateTestDataRoot')
            assert meta.get('KDCodeSigningMode')=='local-certificate'
            assert meta.get('SUPublicEDKey') and meta.get('SURequireSignedFeed')
        else:raise ValueError('Candidate metadata absent')
    print(json.dumps({'ok':True,'version':args.version,'members':len(names)}))


if __name__=='__main__':main()
