"""Sign a locally verified component manifest with the existing publisher key.

The recipe lists immutable HTTPS assets and exact local files. This command does
not upload files, create releases, or change Latest. Full installation acceptance
is still required before publication.
"""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile

from knowledge_distiller.v1.component_release import parse_release
from knowledge_distiller.v1.program_tree import identity


def prepare(release, assets, target, build_manifest, output, public_key, signer):
    output = Path(output)
    if output.exists():
        raise ValueError('Use a new signed manifest output')
    build = json.loads(Path(build_manifest).read_text(encoding='utf-8'))
    if (build.get('git_head') != release['source_commit'] or
            build.get('version') != release['version'] or
            build.get('python') != release['python_version'] or
            build.get('git_dirty') or build.get('changed_during_build') or
            build.get('status') not in {'built', 'built-not-yet-accepted'}):
        raise ValueError('Release and actual build provenance differ')
    if identity(Path(target), release['platform']) != release['target_identity']:
        raise ValueError('Final target differs from release identity')
    if release['platform'] == 'macos-arm64':
        subprocess.run(['codesign', '--verify', '--deep', '--strict', str(target)], check=True)
    listed = [release['docling'], release['base'], *release['deltas']]
    if set(assets) != {entry['sha256'] for entry in listed}:
        raise ValueError('Asset inventory must exactly cover the release')
    for entry in listed:
        path = Path(assets[entry['sha256']])
        with path.open('rb') as stream:
            if path.stat().st_size != entry['size'] or hashlib.file_digest(stream, 'sha256').hexdigest() != entry['sha256']:
                raise ValueError('Asset bytes differ: ' + path.name)
    raw = json.dumps(release, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    with tempfile.TemporaryDirectory(prefix='kd-component-sign-') as temporary:
        payload = Path(temporary) / 'release.payload'
        payload.write_bytes(raw)
        signature = signer(payload)
    envelope = json.dumps({'payload': base64.b64encode(raw).decode(), 'signature': signature}, separators=(',', ':')).encode()
    verified = parse_release(envelope, public_key, platform=release['platform'])
    if verified != release:
        raise ValueError('Signed release readback differs')
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('xb') as destination:
        destination.write(envelope)
    return {'file': str(output), 'sha256': hashlib.sha256(envelope).hexdigest(),
            'version': release['version'], 'source_commit': release['source_commit'], 'published': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--recipe', type=Path, required=True, help='JSON: release and assets (SHA256 to local path)')
    parser.add_argument('--target', type=Path, required=True)
    parser.add_argument('--build-manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--sdk', type=Path, required=True)
    parser.add_argument('--account', default='knowledge-distiller-updates')
    parser.add_argument('--update-config', type=Path, default=Path(__file__).parents[1] / 'packaging/update_config.json')
    args = parser.parse_args()
    config = json.loads(args.update_config.read_text())
    recipe = json.loads(args.recipe.read_text())
    def sign(path):
        return subprocess.check_output([str(args.sdk / 'bin/sign_update'), '--account', args.account, '-p', str(path)], text=True).strip()
    print(json.dumps(prepare(recipe['release'], recipe['assets'], args.target, args.build_manifest,
                            args.output, config['public_key'], sign), indent=2))


if __name__ == '__main__':
    main()
