"""R05 release probe regressions; synthetic inputs and disposable directories only."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import urllib.request

import pytest

from knowledge_distiller.v1 import r05_runtime_probe as probe


NOTICE_ROOT = Path(__file__).resolve().parents[2] / 'packaging/r05-notices'


def _copy_notices(tmp_path):
    return Path(shutil.copytree(NOTICE_ROOT, tmp_path / 'notices'))


def test_real_pinned_probe_is_offline_and_does_not_claim_frozen_acceptance(monkeypatch):
    # Real installed Trafilatura is mandatory here: no injected extractor/skip.
    import httpx
    originals = socket.getaddrinfo, socket.socket.connect, urllib.request.urlopen, httpx.Client.send
    monkeypatch.setattr(os, 'system', lambda *a, **k: pytest.fail('source instruction executed'))
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: pytest.fail('unexpected process/model/app'))
    result = probe.check_r05()
    assert (socket.getaddrinfo, socket.socket.connect, urllib.request.urlopen, httpx.Client.send) == originals
    assert result['ok'] is True
    assert result['frozen'] is False
    assert result['scope'] == 'installed-dependencies-only'
    assert result['versions'] == probe.GATE_VERSIONS
    assert result['network_attempts'] == []
    assert result['resource_hashes_checked'] == {
        'trafilatura': 1, 'justext': 100, 'babel': 5, 'tld': 2}
    assert result['extraction'] == {
        'normal': 'static-candidate-completeness-unverified',
        'long_adjacent_repetition': 'web_repetition_loss', 'fake_http_requests': 2}
    assert 'dateparser.data.date_translation_data.zh' in result['module_origins']
    assert set(result['metadata_origins']) == set(probe.GATE_VERSIONS)


def test_frozen_requirement_cannot_be_satisfied_by_a_dev_environment():
    with pytest.raises(probe.R05ProbeError, match='r05_frozen_required'):
        probe.check_r05(require_frozen=True)


def test_controlled_notices_include_original_source_and_full_licenses():
    entries = probe.validated_notice_datas(NOTICE_ROOT)
    data = json.loads((NOTICE_ROOT / 'manifest.json').read_text())
    assert len(entries) == len(data['files']) + 1
    paths = {Path(source).relative_to(NOTICE_ROOT).as_posix() for source, dest in entries}
    assert 'sources/tld-0.13.2.tar.gz' in paths
    assert 'licenses/PSL/MPL-2.0.txt' in paths
    assert 'licenses/babel/LICENSE.unicode' in paths
    assert 'licenses/tld/licenses/LICENSE_MPL_1.1.txt' in paths
    assert 'licenses/tld/licenses/LICENSE_GPL2.0.txt' in paths
    assert hashlib.sha256((NOTICE_ROOT / 'sources/tld-0.13.2.tar.gz').read_bytes()).hexdigest() == (
        'd983fa92b9d717400742fca844e29d5e18271079c7bcfabf66d01b39b4a14345')
    assert hashlib.sha256((NOTICE_ROOT / 'licenses/PSL/MPL-2.0.txt').read_bytes()).hexdigest() == (
        '3f3d9e0024b1921b067d6f7f88deb4a60cbe7a78e76c64e3f1d7fc3b779b9d04')


@pytest.mark.parametrize('change', ['unknown-file', 'unknown-directory', 'hash', 'missing', 'link'])
def test_notice_collection_rejects_drift_before_packaging(tmp_path, change):
    root = _copy_notices(tmp_path)
    if change == 'unknown-file':
        (root / 'private.txt').write_text('synthetic unexpected input')
    elif change == 'unknown-directory':
        (root / 'unexpected').mkdir()
    elif change == 'hash':
        path = root / 'NOTICE.txt'
        original = path.read_bytes()
        path.write_bytes(b'X' + original[1:])  # same-size tampering
    elif change == 'missing':
        (root / 'sources/tld-0.13.2.tar.gz').unlink()
    else:
        (root / 'NOTICE.txt').unlink()
        (root / 'NOTICE.txt').symlink_to(NOTICE_ROOT / 'NOTICE.txt')
    with pytest.raises(probe.R05ProbeError):
        probe.validated_notice_datas(root)


@pytest.mark.parametrize('path', [
    '../escape', '/absolute', 'licenses\\escape', './NOTICE.txt',
    'C:/absolute', 'C:drive-relative', 'licenses/invalid\x00name',
])
def test_manifest_paths_cannot_escape_the_owned_directory(tmp_path, path):
    root = _copy_notices(tmp_path)
    manifest = root / 'manifest.json'
    data = json.loads(manifest.read_text())
    data['files'][0]['path'] = path
    manifest.write_text(json.dumps(data))
    with pytest.raises(probe.R05ProbeError, match='r05_manifest_path'):
        probe.validated_notice_datas(root)


def test_resource_hash_tamper_fails_before_extraction(tmp_path):
    root = _copy_notices(tmp_path)
    manifest = root / 'manifest.json'
    data = json.loads(manifest.read_text())
    data['resources']['babel'][0]['sha256'] = '0' * 64
    manifest.write_text(json.dumps(data))
    with pytest.raises(probe.R05ProbeError, match='r05_resource_hash') as caught:
        probe.check_r05(notice_root=root)
    assert caught.value.stage == 'resources'
    assert caught.value.attempts == []


def test_version_gate_failure_restores_network_functions(monkeypatch):
    import httpx
    originals = socket.getaddrinfo, socket.socket.connect, urllib.request.urlopen, httpx.Client.send
    real_version = probe.version
    monkeypatch.setattr(probe, 'version', lambda name: '0.bad' if name == 'trafilatura' else real_version(name))
    with pytest.raises(probe.R05ProbeError, match='r05_dependency_version') as caught:
        probe.check_r05()
    assert caught.value.stage == 'versions'
    assert caught.value.attempts == []
    assert (socket.getaddrinfo, socket.socket.connect, urllib.request.urlopen, httpx.Client.send) == originals


def test_dns_urllib_and_unregistered_httpx_are_blocked_and_recorded():
    import httpx
    originals = socket.getaddrinfo, urllib.request.urlopen, httpx.Client.send
    with probe._offline() as (attempts, allowed, module):
        for call in (lambda: socket.getaddrinfo('fixture.example', 443),
                     lambda: urllib.request.urlopen('https://fixture.example'),
                     lambda: module.get('https://fixture.example')):
            with pytest.raises(probe.R05ProbeError, match='r05_network_attempt'):
                call()
        assert attempts == ['socket.getaddrinfo', 'urllib.urlopen', 'httpx.Client.send']
    assert (socket.getaddrinfo, urllib.request.urlopen, httpx.Client.send) == originals


def test_real_socket_is_blocked_and_exception_restores_guard():
    original = socket.socket.connect
    with pytest.raises(ValueError, match='synthetic failure'):
        with probe._offline() as (attempts, allowed, httpx):
            with socket.socket() as sock:
                with pytest.raises(probe.R05ProbeError, match='r05_network_attempt'):
                    sock.connect(('93.184.216.34', 443))
            assert attempts == ['socket.socket.connect']
            raise ValueError('synthetic failure')
    assert socket.socket.connect is original


def test_guard_restores_even_when_dependency_import_fails(monkeypatch):
    original_dns, original_urlopen = socket.getaddrinfo, urllib.request.urlopen
    original_import = probe.importlib.import_module

    def missing(name, *args, **kwargs):
        if name == 'httpx':
            raise ImportError('synthetic missing dependency')
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(probe.importlib, 'import_module', missing)
    with pytest.raises(probe.R05ProbeError) as caught:
        probe.check_r05()
    assert caught.value.stage == 'offline'
    assert socket.getaddrinfo is original_dns
    assert urllib.request.urlopen is original_urlopen


def test_frozen_origin_refuses_dev_path_and_allows_internal_pyz_origin(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, '_MEIPASS', str(tmp_path / 'bundle'), raising=False)
    probe._inside_bundle(tmp_path / 'bundle/trafilatura/__init__.py')
    with pytest.raises(probe.R05ProbeError, match='r05_origin_outside_bundle'):
        probe._inside_bundle(tmp_path / 'external/trafilatura/__init__.py')


def test_runtime_probe_reports_r05_stage_without_starting_later_checks(tmp_path, monkeypatch):
    from knowledge_distiller.v1 import runtime_probe
    from knowledge_distiller.v1.adapters import python_policy
    monkeypatch.setattr(python_policy, 'check_current', lambda: {'version': 'synthetic'})

    def fail():
        raise probe.R05ProbeError('r05_resource_hash', 'resources')

    monkeypatch.setattr(probe, 'check_r05', fail)
    monkeypatch.setattr(runtime_probe.subprocess, 'run', lambda *a, **k: pytest.fail('later stage executed'))
    destination = tmp_path / 'runtime.json'
    assert runtime_probe.check(destination) == 1
    data = json.loads(destination.read_text())
    assert data['failed_stage'] == 'r05_runtime'
    assert data['error_code'] == 'r05_resource_hash'
    assert data['error_diagnostic']['stage'] == 'resources'
