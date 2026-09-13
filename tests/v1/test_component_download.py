import hashlib
import json
import httpx
import pytest

from knowledge_distiller.v1.component_download import ComponentDownloader
from knowledge_distiller.v1.updates import UpdateError


def asset(data):
    return {'url': 'https://example.com/asset.zip', 'sha256': hashlib.sha256(data).hexdigest(),
            'size': len(data), 'unpacked_size': len(data)}


@pytest.mark.parametrize('resume', [False, True, 'server_changed'])
def test_download_resume_and_verified_cache(tmp_path, resume):
    data = b'complete signed component bytes'
    record = asset(data)
    if resume:
        (tmp_path / (record['sha256'] + '.part')).write_bytes(data[:5])
        (tmp_path / (record['sha256'] + '.json')).write_text(json.dumps({'asset': record, 'etag': '"v1"'}))
    calls = []
    def handler(request):
        calls.append(request)
        if resume:
            assert request.headers['Range'] == 'bytes=5-'
            assert request.headers['If-Range'] == '"v1"'
        if resume is True:
            return httpx.Response(206, content=data[5:], headers={
                'etag': '"v1"', 'content-range': f'bytes 5-{len(data)-1}/{len(data)}'})
        return httpx.Response(200, content=data, headers={'etag': '"v2"'})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        downloader = ComponentDownloader(tmp_path, client=client)
        assert downloader.fetch(record).read_bytes() == data
        assert downloader.fetch(record).read_bytes() == data
    assert len(calls) == 1


@pytest.mark.parametrize('bad', ['etag', 'range', 'digest', 'oversized'])
def test_bad_response_does_not_activate(tmp_path, bad):
    data = b'expected component'
    record = asset(data)
    (tmp_path / (record['sha256'] + '.part')).write_bytes(data[:4])
    (tmp_path / (record['sha256'] + '.json')).write_text(json.dumps({'asset': record, 'etag': '"v1"'}))
    def handler(request):
        if bad in {'digest', 'oversized'}:
            return httpx.Response(200, content=b'X' * (len(data) + (bad == 'oversized')))
        return httpx.Response(206, content=data[4:], headers={
            'etag': '"bad"' if bad == 'etag' else '"v1"',
            'content-range': f'bytes {3 if bad == "range" else 4}-{len(data)-1}/{len(data)}'})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(UpdateError):
            ComponentDownloader(tmp_path, client=client).fetch(record)
    assert not (tmp_path / record['sha256']).exists()
