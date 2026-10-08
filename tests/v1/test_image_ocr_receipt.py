"""Synthetic native boundary only; these tests do not attest real Apple OCR."""
from contextlib import nullcontext
from copy import copy, deepcopy
from dataclasses import replace
from io import BytesIO
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace as NS

import pytest
from PIL import Image

from knowledge_distiller.v1 import ocr, vision_ocr as vision
from knowledge_distiller.v1.image_source import image_source_fact


def png(*, mode='RGB', color='white', exif=False):
    image = Image.new(mode, (8, 6), color)
    info = Image.Exif()
    if exif:
        info[274] = 6
    output = BytesIO()
    image.save(output, 'PNG', exif=info)
    return output.getvalue()


def observation(text='重复 é\r\n原文', *, score=.95, left=.1):
    candidates = [NS(string=lambda: text, confidence=lambda: score),
                  NS(string=lambda: '同分', confidence=lambda: score)]
    points = [NS(x=x, y=y) for x, y in ((left, .9), (.8, .9), (.8, .5), (left, .5))]
    return NS(topCandidates_=lambda count: candidates, topLeft=lambda: points[0],
              topRight=lambda: points[1], bottomRight=lambda: points[2], bottomLeft=lambda: points[3])


@pytest.fixture
def native(monkeypatch):
    state = NS(observations=[observation(), observation()], calls=0, config={}, drift=False,
               config_drift=False, succeeded=True, error=None, unknown=False, payload=None)
    request = NS(
        setRevision_=lambda v: state.config.update(revision=v),
        setRecognitionLevel_=lambda v: state.config.update(level=v),
        setRecognitionLanguages_=lambda v: state.config.update(languages=v),
        setUsesLanguageCorrection_=lambda v: state.config.update(correction=v),
        setAutomaticallyDetectsLanguage_=lambda v: state.config.update(automatic=v),
        revision=lambda: state.config['revision'], recognitionLevel=lambda: state.config['level'],
        recognitionLanguages=lambda: state.config['languages'],
        usesLanguageCorrection=lambda: state.config['correction'],
        automaticallyDetectsLanguage=lambda: state.config['automatic'],
        results=lambda: state.observations)

    def perform(requests, error):
        state.calls += 1
        if state.config_drift:
            state.config['correction'] = True
        return state.succeeded, state.error

    def handler(data, orientation, options):
        state.payload = data
        assert orientation == 1 and options is None
        return NS(performRequests_error_=perform)

    monkeypatch.setitem(sys.modules, 'objc', NS(autorelease_pool=nullcontext))
    monkeypatch.setitem(sys.modules, 'Vision', NS(VNRequestTextRecognitionLevelAccurate=0,
        VNRecognizeTextRequest=NS(alloc=lambda: NS(init=lambda: request)),
        VNImageRequestHandler=NS(alloc=lambda: NS(initWithData_orientation_options_=handler))))
    monkeypatch.setitem(sys.modules, 'Foundation', NS(
        NSData=NS(dataWithBytes_length_=lambda data, length: data[:length])))
    environment = {'platform': 'darwin', 'os_product_version': 'synthetic-OS',
        'os_build': '26Synthetic', 'bridges': [{'distribution': 'synthetic-private-boundary'}],
        'pillow_version': 'synthetic', 'framework': {'identifier': 'com.apple.VN',
        'path': '/System/Library/Frameworks/Vision.framework', 'version': 'unknown',
        'provenance': 'system-framework'}}

    def observed_environment(*args):
        if state.unknown:
            raise ValueError('synthetic unavailable provenance')
        value = deepcopy(environment)
        if state.drift and state.calls:
            value['os_build'] = '27Changed'
        return value

    # Explicit private-boundary fake; not a production bridge/OS qualification.
    monkeypatch.setattr(vision, '_environment', observed_environment)
    return state


def bound_result(native, *, data=None, member_id='image-1'):
    data = png() if data is None else data
    result = vision.VisionOcrRunner().recognize_member(data, 'image/png', member_id)
    return data, result


def member(data, member_id='image-1'):
    return {'member_id': member_id, 'content': data, 'mime_type': 'image/png',
            'sha256': hashlib.sha256(data).hexdigest()}


def test_controlled_request_binds_original_pixels_png_and_ordered_output(native):
    data, result = bound_result(native)
    audit = vision.validate_image_receipt(result, data, 'image/png', 'image-1')
    assert native.calls == 1 and audit['protocol'] == 'image-ocr-execution-v1'
    assert audit['input']['byte_count'] == len(data)
    assert audit['input']['sha256'] == hashlib.sha256(data).hexdigest()
    assert audit['pixel_plane']['sha256'] == hashlib.sha256(b'\xff' * (8 * 6 * 3)).hexdigest()
    assert audit['pixel_plane']['handler_png_sha256'] == hashlib.sha256(native.payload).hexdigest()
    assert audit['request']['language_correction'] is False
    assert audit['request']['languages'] == ['zh-Hans', 'zh-Hant', 'en-US']
    assert [line['text'] for line in audit['output']['lines']] == ['重复 é\r\n原文'] * 2
    assert audit['output']['lines'][0]['alternatives'] == ['同分']
    assert result.framework_version == 'unknown'
    assert audit['environment']['framework']['version'] == 'unknown'
    assert 'weight_sha256' not in audit['environment']['framework']


def test_transparency_exif_and_encoded_bytes_remain_separate_evidence(native):
    first = png(mode='RGBA', color=(0, 0, 0, 0), exif=True)
    second = png()
    a = vision.VisionOcrRunner().recognize_member(first, 'image/png', 'image-1')
    aa = vision.validate_image_receipt(a, first, 'image/png', 'image-1')
    b = vision.VisionOcrRunner().recognize_member(second, 'image/png', 'image-2')
    bb = vision.validate_image_receipt(b, second, 'image/png', 'image-2')
    assert aa['input']['sha256'] != bb['input']['sha256']
    assert aa['pixel_plane']['sha256'] == bb['pixel_plane']['sha256']
    with Image.open(BytesIO(native.payload)) as image:
        assert image.mode == 'RGB' and image.size == (8, 6) and not image.getexif()


@pytest.mark.parametrize('mismatch', ['bytes', 'mime', 'member', 'copy', 'replace', 'dict', 'json'])
def test_same_result_receipt_cannot_be_rebound_or_reconstructed(native, mismatch):
    data, result = bound_result(native)
    mime, member_id = 'image/png', 'image-1'
    if mismatch == 'bytes': data += b'extra'
    elif mismatch == 'mime': mime = 'image/jpeg'
    elif mismatch == 'member': member_id = 'image-2'
    elif mismatch == 'copy': result = copy(result)
    elif mismatch == 'replace': result = replace(result)
    elif mismatch == 'dict': result = replace(result, receipt=json.loads(result.receipt.audit_json))
    elif mismatch == 'json': result = replace(result, receipt=result.receipt.audit_json)
    with pytest.raises(ocr.OcrError, match='^ocr_invalid_output$'):
        vision.validate_image_receipt(result, data, mime, member_id)


@pytest.mark.parametrize('field', ['text', 'confidence', 'polygon', 'alternatives', 'original_polygon'])
def test_deep_output_change_invalidates_original_result(native, field):
    data, result = bound_result(native)
    line = result.lines[0]
    changes = {'text': '改文', 'confidence': .5, 'polygon': ((0., 0.),) * 4,
               'alternatives': ('changed',), 'original_polygon': ((-1., 0.),) * 4}
    object.__setattr__(line, field, changes[field])
    with pytest.raises(ocr.OcrError, match='^ocr_invalid_output$'):
        vision.validate_image_receipt(result, data, 'image/png', 'image-1')


@pytest.mark.parametrize('flag', ['drift', 'config_drift', 'unknown'])
def test_environment_or_effective_config_gap_never_signs(native, flag):
    setattr(native, flag, True)
    data, result = bound_result(native)
    assert native.calls == 1 and result.receipt is None
    assert result.text == '重复 é\r\n原文\n重复 é\r\n原文'


def test_real_environment_boundary_rejects_fake_modules(native):
    # Call the actual verifier without the private fixture override; a fake is
    # rejected before any OS query or native operation.
    with pytest.raises((ValueError, AttributeError)):
        _REAL_ENVIRONMENT(sys.modules['objc'], sys.modules['Vision'], sys.modules['Foundation'])


_REAL_ENVIRONMENT = vision._environment


@pytest.mark.parametrize('fault', [None, 'empty_build', 'bad_build', 'bundle_id', 'bundle_path', 'class'])
def test_environment_verifier_requires_system_identity_and_actual_build(monkeypatch, tmp_path, fault):
    root = tmp_path / 'synthetic-framework'; root.mkdir()
    other = tmp_path / 'other-framework'; other.mkdir()
    class Request: pass
    class Handler: pass
    module = NS(VNRecognizeTextRequest=Request, VNImageRequestHandler=Handler)
    if fault == 'class': module.VNRecognizeTextRequest = NS()
    bundle = NS(bundlePath=lambda: str(other if fault == 'bundle_path' else root),
                bundleIdentifier=lambda: 'wrong' if fault == 'bundle_id' else 'com.apple.VN',
                infoDictionary=lambda: {})
    foundation = NS(NSBundle=NS(bundleForClass_=lambda cls: bundle))
    monkeypatch.setattr(vision.sys, 'platform', 'darwin')
    monkeypatch.setattr(vision, 'Path', lambda p: root if str(p) == '/System/Library/Frameworks/Vision.framework' else Path(p))
    monkeypatch.setattr(vision, '_bridge_origin', lambda module, name: {'distribution': name, 'version': '12.2.2'})
    monkeypatch.setattr(vision, 'distribution', lambda name: NS(version='synthetic-Pillow'))
    monkeypatch.setattr(vision.platform, 'mac_ver', lambda: ('synthetic-OS', (), 'arm64'))
    calls = []
    def system_query(argv, **kwargs):
        calls.append((argv, kwargs))
        return NS(stdout='' if fault == 'empty_build' else 'bad build!' if fault == 'bad_build' else '26Synthetic\n')
    monkeypatch.setattr(vision.subprocess, 'run', system_query)
    if fault:
        with pytest.raises(ValueError):
            _REAL_ENVIRONMENT(NS(objc_class=type), module, foundation)
    else:
        environment = _REAL_ENVIRONMENT(NS(objc_class=type), module, foundation)
        assert environment['os_build'] == '26Synthetic'
        assert environment['framework']['version'] == 'unknown'
        assert environment['framework']['path'] == str(root)
        assert calls == [(['/usr/bin/sw_vers', '-buildVersion'],
                          {'capture_output': True, 'text': True, 'check': True, 'timeout': 2})]
    if fault in ('bundle_id', 'bundle_path', 'class'):
        assert not calls


@pytest.mark.parametrize('fault', [None, 'version', 'origin', 'missing'])
def test_bridge_origin_requires_actual_distribution_file(monkeypatch, tmp_path, fault):
    path = tmp_path / '__init__.py'; path.write_bytes(b'synthetic bridge source')
    dist = NS(version='changed' if fault == 'version' else '12.2.2',
              files=[] if fault == 'origin' else ['Vision/__init__.py'],
              locate_file=lambda member: path)
    monkeypatch.setattr(vision, 'distribution', lambda name: dist)
    module = NS(__file__=str(tmp_path / 'missing' if fault == 'missing' else path))
    if fault:
        with pytest.raises((ValueError, OSError)):
            vision._bridge_origin(module, 'pyobjc-framework-Vision')
    else:
        observed = vision._bridge_origin(module, 'pyobjc-framework-Vision')
        assert observed['origin'] == 'Vision/__init__.py'
        assert observed['origin_sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert observed['origin_identity'][2] == len(b'synthetic bridge source')


def test_plain_result_legacy_runner_and_paddle_never_gain_authority(native):
    data = png()
    assert vision.VisionOcrRunner().recognize_bytes(data, 'image/png').receipt is None
    assert vision._result([observation()], 8, 6).receipt is None
    with pytest.raises(TypeError, match='opaque_image_ocr_receipt'):
        vision.ImageOcrReceipt()
    predictor = NS(predict=lambda pixels: [{'rec_texts': ['original'], 'rec_scores': [.9],
        'rec_polys': [[[0, 0], [4, 0], [4, 2], [0, 2]]]}])
    assert ocr.PaddleOcrRunner(predictor_factory=lambda: predictor).recognize_bytes(data, 'image/png').receipt is None


def test_actual_blank_execution_has_receipt_but_nil_or_failure_does_not(native):
    native.observations = []
    data, result = bound_result(native)
    audit = vision.validate_image_receipt(result, data, 'image/png', 'image-1')
    assert result.lines == () and audit['execution']['outcome'] == 'blank'
    native.observations = None
    with pytest.raises(ocr.OcrError, match='^ocr_invalid_output$'):
        bound_result(native)
    native.succeeded = False
    with pytest.raises(ocr.OcrError, match='^ocr_inference_failed$'):
        bound_result(native)


@pytest.mark.parametrize('score,left', [(float('nan'), .1), (.9, -2.)])
def test_invalid_score_or_coordinates_cannot_sign(native, score, left):
    native.observations = [observation(score=score, left=left)]
    with pytest.raises(ocr.OcrError, match='^ocr_invalid_output$'):
        bound_result(native)


def test_clamped_primary_polygon_preserved_and_collector_retains_occurrences(native):
    native.observations = [observation(left=-.1)]
    data = png()
    results = []
    fact, lineage = image_source_fact('', [member(data), member(data, 'image-2')],
        vision.VisionOcrRunner(), execution_results=results)
    assert native.calls == 2 and [r[0] for r in results] == ['image-1', 'image-2']
    assert results[0][1].receipt is not results[1][1].receipt
    for image, (member_id, result) in zip(lineage['image_ocr'], results, strict=True):
        assert image['framework_version'] == 'unknown' and image['framework']['provenance'] == 'system-framework'
        assert image['execution_audit'] == vision.validate_image_receipt(result, data, 'image/png', member_id)
        line = image['lines'][0]
        assert line['original_polygon'][0][0] == pytest.approx(-.8)
        assert line['polygon'][0][0] == 0
        assert fact.snapshot[line['start']:line['end']] == '重复 é\r\n原文'
    assert lineage['image_ocr'][0]['lines'][0]['end'] < lineage['image_ocr'][1]['lines'][0]['start']


def test_rehashed_cache_is_diagnostic_and_controlled_branch_reexecutes(native, tmp_path):
    data = png()
    runner = vision.VisionOcrRunner()
    image_source_fact('', [member(data)], runner, checkpoint_dir=tmp_path)
    path, = tmp_path.glob('*.json')
    record = json.loads(path.read_text())
    record['result']['lines'][0]['text'] = 'forged JSON'
    record['result']['receipt'] = {'protocol': 'self-signed'}
    record['sha256'] = hashlib.sha256(json.dumps(record['result'], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    path.write_text(json.dumps(record))
    fact, diagnostic = image_source_fact('', [member(data)], runner, checkpoint_dir=tmp_path)
    assert 'forged JSON' in fact.snapshot and native.calls == 1
    assert 'execution_audit' not in diagnostic['image_ocr'][0]
    results = []
    fact, lineage = image_source_fact('', [member(data)], runner, checkpoint_dir=tmp_path, execution_results=results)
    assert native.calls == 2 and 'forged JSON' not in fact.snapshot
    assert results[0][1].receipt is not None and 'execution_audit' in lineage['image_ocr'][0]
    payload = json.loads(path.read_text())['result']
    assert 'receipt' not in payload  # Serialization did not traverse the opaque object.


def test_injected_runner_and_wrong_declared_member_hash_cannot_sign(native):
    data = png()
    injected = NS(recognize_bytes=lambda data, mime: ocr.OcrResult(8, 6, ()))
    results = []
    _, lineage = image_source_fact('', [member(data)], injected, execution_results=results)
    assert results[0][1].receipt is None and 'execution_audit' not in lineage['image_ocr'][0]
    bad = member(data); bad['sha256'] = '0' * 64
    with pytest.raises(ocr.OcrError, match='^ocr_invalid_image$'):
        image_source_fact('', [bad], vision.VisionOcrRunner(), execution_results=[])
    assert native.calls == 0


@pytest.mark.parametrize('member_id', ['', '\x00', '\ud800'])
def test_invalid_member_identity_fails_before_request(native, member_id):
    with pytest.raises(ocr.OcrError, match='^ocr_invalid_image$'):
        vision.VisionOcrRunner().recognize_member(png(), 'image/png', member_id)
    assert native.calls == 0
