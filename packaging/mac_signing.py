"""Explicit inside-out bundle signing. Never creates identities or changes trust."""
from datetime import datetime
from pathlib import Path
import plistlib
import re
import struct
import subprocess
import tempfile

MACHO = {bytes.fromhex(value) for value in ('feedface', 'cefaedfe', 'feedfacf', 'cffaedfe', 'cafebabe', 'bebafeca', 'cafebabf', 'bfbafeca')}


def version_info(product, build):
    if not re.fullmatch(r'\d+\.\d+(?:\.\d+)?', product):
        raise ValueError('产品版本应为 major.minor[.patch]')
    if not re.fullmatch(r'\d{4}\.\d{2}\.\d{2}\.\d+', build):
        raise ValueError('构建号须为 YYYY.MM.DD.N，兼容既有更新比较')
    datetime.strptime(build.rsplit('.', 1)[0], '%Y.%m.%d')
    if tuple(map(int, build.split('.'))) <= (2026, 9, 9, 12):
        raise ValueError('构建号必须高于正式 V1.0 的 2026.09.09.12')
    return {'CFBundleShortVersionString': product, 'CFBundleVersion': build}


def update_policy(signing_config, update_config, *, manual=False):
    """Automatic replacement requires stable signing and an authenticated feed."""
    stable = signing_config.get('identity', '-') != '-'
    if stable:
        requirement('local.validation', signing_config.get('certificate_sha1', ''))
    enabled = stable and bool(update_config) and not manual
    if enabled and not (update_config.get('public_key') and update_config.get('feed_url')):
        raise ValueError('自动更新需要签名公钥和更新源')
    return {'KDManualUpdateOnly': not enabled,
            'KDCodeSigningMode': 'local-certificate' if stable else 'ad-hoc'}


def requirement(identifier, certificate):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', identifier) or not re.fullmatch(r'[a-fA-F0-9]{40}', certificate):
        raise ValueError('签名标识或证书 SHA1 格式无效')
    return f'designated => identifier "{identifier}" and certificate leaf = H"{certificate.lower()}"'


def components(app):
    app = Path(app)
    found = [app]
    for path in app.rglob('*'):
        if path.is_symlink():
            continue
        if path.is_dir() and path.suffix in ('.app', '.xpc', '.framework'):
            found.append(path)
        elif path.is_file():
            with path.open('rb') as stream:
                if stream.read(4) in MACHO:
                    found.append(path)
    return sorted(set(found), key=lambda p: (-len(p.parts), str(p)))


def sign_bundle(app, config=None, *, run=subprocess.run):
    app = Path(app)
    config = config or {}
    identity = config.get('identity', '-')
    certificate = config.get('certificate_sha1', '')
    if identity != '-':
        requirement('local.validation', certificate)
    plan = components(app)
    with tempfile.TemporaryDirectory(prefix='kd-signing-') as temp:
        for index, target in enumerate(plan):
            command = ['codesign', '--force', '--sign', identity, '--timestamp=none']
            # Read entitlements from this component before replacing its signature.
            existing = run(['codesign', '--display', '--entitlements', ':-', str(target)],
                           capture_output=True, check=False)
            if existing.returncode and b'code object is not signed at all' not in existing.stderr:
                raise subprocess.CalledProcessError(existing.returncode, existing.args, existing.stdout, existing.stderr)
            entitlements = existing.stdout.strip()
            if identity != '-':
                host = target.is_dir() and target.suffix in ('.app', '.xpc')
                if target.is_file():
                    with target.open('rb') as stream:
                        header = stream.read(16)
                    if len(header) == 16 and header[:4] in (b'\xcf\xfa\xed\xfe', b'\xce\xfa\xed\xfe'):
                        host = struct.unpack('<I', header[12:16])[0] == 2  # MH_EXECUTE
                if host:
                    # A local certificate has no Apple Team ID. Its executable
                    # hosts must be able to load the independently verified,
                    # bundled Python/Sparkle libraries under hardened runtime.
                    values = plistlib.loads(entitlements) if entitlements else {}
                    values['com.apple.security.cs.disable-library-validation'] = True
                    if target.relative_to(app).as_posix() in ('Contents/Frameworks/bin/node', 'Contents/Resources/bin/node'):
                        values['com.apple.security.cs.allow-jit'] = True
                    entitlements = plistlib.dumps(values)
            if entitlements:
                plistlib.loads(entitlements)  # Malformed data must stop signing.
                path = Path(temp)/f'{index}.plist'; path.write_bytes(entitlements)
                command += ['--entitlements', str(path)]
            if identity != '-':
                metadata = run(['codesign', '--display', '--verbose=2', str(target)],
                               capture_output=True, check=False)
                if metadata.returncode and b'code object is not signed at all' not in metadata.stderr:
                    raise subprocess.CalledProcessError(metadata.returncode, metadata.args, metadata.stdout, metadata.stderr)
                match = re.search(rb'^Identifier=(.+)$', metadata.stderr, re.MULTILINE)
                bundle_info = next((p for p in (target/'Contents/Info.plist', target/'Resources/Info.plist') if p.is_file()), None) if target.is_dir() else None
                if target.relative_to(app).as_posix() == 'Contents/MacOS/update-helper':
                    # PyInstaller's ad-hoc identifier embeds the build UUID.
                    # Keep this executable's identity stable across updates.
                    identifier = 'local.knowledge-distiller.update-helper'
                elif bundle_info:
                    identifier = plistlib.loads(bundle_info.read_bytes())['CFBundleIdentifier']
                elif match and re.fullmatch(rb'[A-Za-z0-9_.-]+', match[1]):
                    identifier = match[1].decode()
                elif match or metadata.returncode:
                    import hashlib
                    relative = str(target.relative_to(app))
                    identifier = 'local.knowledge-distiller.code.' + hashlib.sha256(relative.encode()).hexdigest()[:24]
                else:
                    raise ValueError(f'无法确定组件签名标识: {target}')
                command += ['--identifier', identifier, '--options', 'runtime',
                            '--requirements', '=' + requirement(identifier, certificate)]
            run(command+[str(target)], check=True)
            verification = ['codesign', '--verify', '--strict']
            if identity != '-':
                verification += ['--test-requirement', '=' + requirement(identifier, certificate).removeprefix('designated => ')]
            run(verification+[str(target)], check=True)
    return {'mode': 'ad-hoc' if identity == '-' else 'local-certificate',
            'certificate_sha1': certificate or None,
            'components': [str(p.relative_to(Path(app).parent)) for p in plan]}
