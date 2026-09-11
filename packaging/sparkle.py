"""Attach the pinned Sparkle runtime after PyInstaller's bundle assembly."""
import hashlib
from pathlib import Path
import plistlib
import subprocess
import sys
import shutil
import tempfile

SDK_SHA256 = '52bf9e88cdd972fc0c81501377a880e90d47031bd8ca5462488f843e2609e192'


def attach(app, sdk, config, project):
    sdk, app = Path(sdk), Path(app)
    sdk_info = plistlib.loads((sdk/'Sparkle.framework/Resources/Info.plist').read_bytes())
    if sdk_info['CFBundleShortVersionString'] != '2.9.6':
        raise ValueError('Sparkle 2.9.6 is required')
    archive = sdk/'Sparkle-2.9.6.tar.xz'
    if SDK_SHA256 and hashlib.sha256(archive.read_bytes()).hexdigest() != SDK_SHA256:
        raise ValueError('Sparkle SDK checksum mismatch')
    contents = app/'Contents'
    subprocess.run(['ditto', str(sdk/'Sparkle.framework'), str(contents/'Frameworks/Sparkle.framework')], check=True)
    source = project/'packaging/sparkle-cli'
    helper = contents/'Helpers/Updater.app/Contents'
    (helper/'MacOS').mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='kd-update-build-') as temp:
        info = plistlib.loads((source/'Info.plist').read_bytes())
        info.update(CFBundleExecutable='update-cli', CFBundleIdentifier='local.knowledge-distiller.update-cli',
                    CFBundleName='Knowledge Distiller Updater', CFBundleShortVersionString='2.9.6',
                    CFBundleVersion='20906', LSMinimumSystemVersion='14.0')
        plist = Path(temp)/'Info.plist'; plist.write_bytes(plistlib.dumps(info))
        (helper/'Info.plist').write_bytes(plistlib.dumps(info))
        subprocess.run(['clang', '-fobjc-arc', '-fmodules', '-DSPU_OBJC_DIRECT_MEMBERS=', '-DSPU_OBJC_DIRECT=',
                        '-mmacosx-version-min=14.0', '-F', str(sdk), '-framework', 'Sparkle', '-framework', 'Cocoa',
                        '-Wl,-rpath,@executable_path/../../../../Frameworks', '-sectcreate', '__TEXT', '__info_plist', str(plist),
                        *map(str, sorted(source.glob('*.m'))), '-o', str(helper/'MacOS/update-cli')], check=True)
    # Invoke the separate NSBundle directly. A signed shell shim would create
    # code-signature extended attributes that Sparkle BinaryDelta cannot diff.
    (contents/'MacOS/update-cli').unlink(missing_ok=True)
    subprocess.run(['swiftc', '-parse-as-library', '-O', str(project/'packaging/update_verify.swift'),
                    '-o', str(contents/'MacOS/update-verify')], check=True)
    with tempfile.TemporaryDirectory(prefix='kd-installer-build-') as temp:
        work = Path(temp)
        with (work/'build.log').open('w') as log:
            subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--onefile',
                '--name', 'update-helper', '--paths', str(project/'src'),
                '--distpath', str(work/'dist'), '--workpath', str(work/'work'), '--specpath', str(work),
                str(project/'packaging/update_entry.py')], stdout=log, stderr=subprocess.STDOUT, check=True)
        shutil.copy2(work/'dist/update-helper', contents/'MacOS/update-helper')
    plist = contents/'Info.plist'
    info = plistlib.loads(plist.read_bytes())
    info.update(SUPublicEDKey=config['public_key'], SUFeedURL=config['feed_url'],
                SURequireSignedFeed=True, SUVerifyUpdateBeforeExtraction=True,
                SUEnableAutomaticChecks=False, SUAutomaticallyUpdate=False)
    if config.get('test_data_root'):
        info.update(CFBundleIdentifier='local.knowledge-distiller.updater-test', KDUpdateTestDataRoot=config['test_data_root'])
    plist.write_bytes(plistlib.dumps(info))
    notices = contents/'Resources/update-licenses.txt'
    notices.write_text((source/'LICENSE').read_text())
    # The build entry signs all components once, after attachment is complete.
