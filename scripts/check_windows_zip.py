"""Extract and verify the delivered ZIP, then exercise a read-only install."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import urllib.request
import zipfile

import win32api
import win32con
import win32security
import ntsecuritycon


def launch(app, data, output):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(('PYTHON', 'CONDA', 'VIRTUAL_ENV', 'KNOWLEDGE_DISTILLER'))}
    env.update(PATH=str(Path(os.environ['SystemRoot']) / 'System32'),
               LOCALAPPDATA=str(output / 'LocalAppData'),
               HF_HOME=str(output / 'empty-cache'), HF_HUB_OFFLINE='1')
    with (output / 'launch.log').open('ab') as log:
        p = subprocess.Popen([str(app / 'KnowledgeDistiller.exe'), '--data-dir', str(data),
                              '--no-open', '--smoke-seconds', '12'], cwd=output,
                             env=env, stdout=log, stderr=log)
        try:
            state = data / '.desktop-instance.json'
            deadline = time.monotonic() + 30
            while not state.is_file():
                assert p.poll() is None, p.returncode
                assert time.monotonic() < deadline, 'startup timeout'
                time.sleep(.1)
            instance = json.loads(state.read_text(encoding='utf-8'))
            for route in ('/', '/topics', '/insights', '/settings'):
                with urllib.request.urlopen(f"http://127.0.0.1:{instance['port']}" + route, timeout=15) as r:
                    assert r.status == 200
                    assert '知识蒸馏器' in r.read().decode('utf-8')
            assert p.wait(timeout=40) == 0
            assert not state.exists()
        finally:
            if p.poll() is None:
                p.terminate()
                p.wait(timeout=15)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--zip', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--previous-app', type=Path, required=True)
    parser.add_argument('--reuse-verified-output', action='store_true',
                        help='Resume lifecycle checks after a recorded failed check with verified extraction')
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    output = args.output.resolve()
    assert output.is_relative_to(project / '.windows-build'), 'Disposable test root required'
    app = output / '知识蒸馏器'
    if args.reuse_verified_output:
        previous = json.loads((output / 'zip-check.json').read_text(encoding='utf-8'))
        assert previous['ok'] is False
        assert Path(previous['zip']).resolve() == args.zip.resolve()
        hashes = json.loads((app / 'FILES-SHA256.json').read_text(encoding='utf-8'))
        assert previous['extracted_files_verified'] == len(hashes)
    else:
        assert not output.exists(), 'Use a fresh directory'
        output.mkdir(parents=True)
        with zipfile.ZipFile(args.zip) as archive:
            for entry in archive.infolist():
                assert (output / entry.filename).resolve().is_relative_to(output)
            archive.extractall(output)
        hashes = json.loads((app / 'FILES-SHA256.json').read_text(encoding='utf-8'))
        for relative, expected in hashes.items():
            path = app / relative
            with path.open('rb') as source:
                assert hashlib.file_digest(source, 'sha256').hexdigest() == expected, relative
    report = {'zip': str(args.zip.resolve()), 'extracted_files_verified': len(hashes),
              'developer_path': False, 'ok': False}
    if args.reuse_verified_output:
        report['reused_evidence'] = 'Recorded extraction and full hash verification; only lifecycle/ACL checks repeated'
    data = output / '中文 用户数据'
    launch(args.previous_app.resolve(), data, output)
    database = data / 'knowledge.sqlite3'
    original_inode = database.stat().st_ino
    marker = data / 'upgrade-preserved.txt'
    marker.write_text('独立升级验收', encoding='utf-8')

    # Deny writes on this newly extracted, owned program tree only. Preserve its DACL.
    security = win32security.GetNamedSecurityInfo(str(app), win32security.SE_FILE_OBJECT,
                                                 win32security.DACL_SECURITY_INFORMATION)
    original = security.GetSecurityDescriptorDacl()
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    acl = win32security.ACL()
    acl.AddAccessDeniedAceEx(win32security.ACL_REVISION,
                            win32con.OBJECT_INHERIT_ACE | win32con.CONTAINER_INHERIT_ACE,
                            ntsecuritycon.FILE_WRITE_DATA | ntsecuritycon.FILE_APPEND_DATA |
                            ntsecuritycon.FILE_WRITE_EA | ntsecuritycon.FILE_WRITE_ATTRIBUTES |
                            ntsecuritycon.DELETE |
                            ntsecuritycon.FILE_DELETE_CHILD, sid)
    for i in range(original.GetAceCount()):
        (kind, flags), mask, trustee = original.GetAce(i)
        if kind == win32security.ACCESS_ALLOWED_ACE_TYPE:
            acl.AddAccessAllowedAceEx(win32security.ACL_REVISION, flags, mask, trustee)
        elif kind == win32security.ACCESS_DENIED_ACE_TYPE:
            acl.AddAccessDeniedAceEx(win32security.ACL_REVISION, flags, mask, trustee)
        else:
            raise ValueError('Unsupported inherited ACE; no permissions changed')
    try:
        win32security.SetNamedSecurityInfo(str(app), win32security.SE_FILE_OBJECT,
                                          win32security.DACL_SECURITY_INFORMATION,
                                          None, None, acl, None)
        for folder in (app, app / '_internal'):
            try:
                (folder / 'write-denied-probe').write_bytes(b'probe')
            except PermissionError:
                pass
            else:
                raise AssertionError('Program directory is writable')
        launch(app, data, output)
        launch(app, data, output)
        assert database.stat().st_ino == original_inode
        assert marker.read_text(encoding='utf-8') == '独立升级验收'
        report.update(ok=True, readonly='NTFS write-deny verified at root and _internal',
                      upgrade='same database inode and user file retained across program paths',
                      lifecycle='four pages, two extracted launches, clean exit')
    finally:
        win32security.SetNamedSecurityInfo(str(app), win32security.SE_FILE_OBJECT,
                                          win32security.DACL_SECURITY_INFORMATION,
                                          None, None, original, None)
        for folder in (app, app / '_internal'):
            probe = folder / 'permission-restored-probe'
            with probe.open('xb') as stream:
                stream.write(b'disposable permission check')
            probe.unlink()
        report['permissions_restored'] = True
        (output / 'zip-check.json').write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                              encoding='utf-8')
    print(output / 'zip-check.json')


if __name__ == '__main__':
    main()
