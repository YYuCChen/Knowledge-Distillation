from contextlib import nullcontext
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
import hashlib
import sys
import tomllib

import pytest
from PIL import Image, ImageDraw, ImageFont

from knowledge_distiller.v1 import ocr, vision_ocr
from knowledge_distiller.v1.image_source import image_source_fact


def png(mode='RGB', color='white', *, exif=None):
    output = BytesIO()
    image = Image.new(mode, (100, 80), color)
    image.save(output, 'PNG', **({'exif': exif} if exif else {}))
    return output.getvalue()


def observation(text='原文 English', confidence=.95, points=None):
    points = points or ((.1, .9), (.8, .9), (.8, .6), (.1, .6))
    corners = [SimpleNamespace(x=x, y=y) for x, y in points]
    candidate = SimpleNamespace(string=lambda: text, confidence=lambda: confidence)
    return SimpleNamespace(topCandidates_=lambda count: [candidate],
        topLeft=lambda: corners[0], topRight=lambda: corners[1],
        bottomRight=lambda: corners[2], bottomLeft=lambda: corners[3])


@pytest.fixture
def native(monkeypatch):
    calls = {}
    request = SimpleNamespace(
        setRevision_=lambda value: calls.update(revision=value),
        setRecognitionLevel_=lambda value: calls.update(level=value),
        setRecognitionLanguages_=lambda value: calls.update(languages=value),
        setUsesLanguageCorrection_=lambda value: calls.update(correction=value),
        setAutomaticallyDetectsLanguage_=lambda value: calls.update(auto_language=value),
        results=lambda: [observation()])
    def initialize(data, orientation, options):
        calls.update(data=data, orientation=orientation, options=options)
        return SimpleNamespace(performRequests_error_=lambda requests, error: (True, None))
    module = SimpleNamespace(VNRequestTextRecognitionLevelAccurate=0,
        VNRecognizeTextRequest=SimpleNamespace(alloc=lambda: SimpleNamespace(init=lambda: request)),
        VNImageRequestHandler=SimpleNamespace(alloc=lambda: SimpleNamespace(initWithData_orientation_options_=initialize)))
    monkeypatch.setitem(sys.modules, 'Vision', module)
    monkeypatch.setitem(sys.modules, 'objc', SimpleNamespace(autorelease_pool=nullcontext))
    monkeypatch.setitem(sys.modules, 'Foundation', SimpleNamespace(
        NSData=SimpleNamespace(dataWithBytes_length_=lambda data, length: data[:length])))
    return calls, module


@pytest.mark.parametrize("mode,color", [("RGBA", (0,0,0,0)), ("RGB", "white")])
def test_vision_preserves_original_plane_and_normalizes_transparency(native,mode,color):
    calls, _ = native
    exif = Image.Exif(); exif[274] = 6
    result = vision_ocr.VisionOcrRunner().recognize_bytes(png(mode, color, exif=exif), 'image/png')
    assert calls['revision'] == 3 and calls['languages'] == ['zh-Hans', 'zh-Hant', 'en-US']
    assert calls['correction'] is False and calls['auto_language'] is False
    assert calls['orientation'] == 1
    with Image.open(BytesIO(calls['data'])) as decoded:
        assert decoded.size == (100, 80) and decoded.getpixel((0, 0)) == (255, 255, 255)
        assert not decoded.getexif()
    assert result.engine == 'apple_vision' and result.text == '原文 English'
    assert result.detection_model == result.recognition_model == 'VNRecognizeTextRequestRevision3'
    for actual, expected in zip(result.lines[0].polygon, ((10, 8), (80, 8), (80, 32), (10, 32))):
        assert actual == pytest.approx(expected)


def test_skewed_vision_quad_and_exact_text_are_not_rewritten():
    result = vision_ocr._result([observation(' 原文  ', points=((.1,.9),(.8,.8),(.8,.5),(.1,.6)))],100,80)
    assert result.text == ' 原文  '
    for actual, expected in zip(result.lines[0].polygon, ((10,8),(80,16),(80,40),(10,32))):
        assert actual == pytest.approx(expected)


@pytest.mark.parametrize('values', [None, [observation('')], [observation(confidence=float('nan'))],
    [observation(confidence=1.1)], [observation(points=((0,1),(2,1),(2,0),(0,0)))],
    [SimpleNamespace(topCandidates_=lambda count: [])]])
def test_invalid_native_output_never_becomes_blank_success(values):
    with pytest.raises(ocr.OcrError, match='ocr_invalid_output'):
        vision_ocr._result(values,100,80)


def test_explicit_blank_result_is_valid():
    assert vision_ocr._result([],100,80).lines == ()


def test_native_request_failure_is_reported_without_paddle_fallback(native,monkeypatch):
    _, module = native
    module.VNImageRequestHandler.alloc = lambda: SimpleNamespace(
        initWithData_orientation_options_=lambda *args: SimpleNamespace(
            performRequests_error_=lambda *args: (False, 'native error')))
    monkeypatch.setattr(ocr.sys, 'platform', 'darwin')
    monkeypatch.setattr(ocr, 'PaddleOcrRunner', lambda: pytest.fail('No engine fallback'))
    with pytest.raises(ocr.OcrError, match='ocr_inference_failed'):
        ocr.default_ocr_runner().recognize_bytes(png(),'image/png')


def test_missing_bridge_and_invalid_input_fail_distinctly(monkeypatch):
    monkeypatch.setitem(sys.modules,'Vision',None)
    with pytest.raises(ocr.OcrError, match='ocr_runtime_unavailable'):
        vision_ocr.VisionOcrRunner().recognize_bytes(png(),'image/png')
    with pytest.raises(ocr.OcrError, match='ocr_invalid_image'):
        vision_ocr.VisionOcrRunner().recognize_bytes(b'bad','image/png')


def test_windows_keeps_lazy_paddle_runner(monkeypatch):
    monkeypatch.setattr(ocr.sys,'platform','win32')
    monkeypatch.setitem(sys.modules,'Vision',None)
    runner = ocr.default_ocr_runner()
    assert isinstance(runner,ocr.PaddleOcrRunner) and runner._predictor is None


def test_platform_dependency_markers_keep_docling_and_windows_paddle():
    from packaging.requirements import Requirement
    from packaging.markers import default_environment
    project = Path(__file__).resolve().parents[2]
    config = tomllib.loads((project/'pyproject.toml').read_text())
    requirements = [Requirement(value) for value in config['project']['dependencies']]
    def names(platform):
        environment = {**default_environment(), 'sys_platform': platform}
        return {r.name for r in requirements if r.marker is None or r.marker.evaluate(environment)}
    mac, windows = names('darwin'), names('win32')
    paddle = {'paddleocr','paddlex','paddlepaddle','opencv-contrib-python'}
    assert not mac & paddle and paddle <= windows
    assert 'pyobjc-framework-Vision' in mac and 'pyobjc-framework-Vision' not in windows
    assert {'docling','rapidocr','onnxruntime','torch','torchvision','transformers','opencv-python'} <= mac & windows


@pytest.mark.skipif(sys.platform != 'darwin', reason='Real Apple Vision requires macOS')
def test_real_vision_worker_recognition_and_immutable_source_locator(tmp_path,monkeypatch):
    import builtins
    from concurrent.futures import ThreadPoolExecutor
    original = builtins.__import__
    def guarded(name,*args,**kwargs):
        if name.split('.')[0] in {'paddle','paddleocr','paddlex'}:
            pytest.fail('Mac image OCR must not load Paddle')
        return original(name,*args,**kwargs)
    monkeypatch.setattr(builtins,'__import__',guarded)
    image = Image.new('RGB',(960,360),'white')
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype('/System/Library/Fonts/PingFang.ttc',44)
    expected = ['知识蒸馏器','Source evidence 2026','来源定位 保留原文']
    boxes = []
    for text,y in zip(expected,(35,135,235)):
        draw.text((40,y),text,fill='black',font=font)
        boxes.append(draw.textbbox((40,y),text,font=font))
    raw = BytesIO(); image.save(raw,format='PNG'); data=raw.getvalue()
    path=tmp_path/'source.png';path.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    member = {'member_id':'image-1','sha256':digest,'mime_type':'image/png','content':data}
    runner=ocr.default_ocr_runner()
    with ThreadPoolExecutor(max_workers=1) as pool:
        fact,lineage=pool.submit(image_source_fact,'',[member],runner).result(timeout=30)
    evidence = lineage['image_ocr'][0]
    assert evidence['engine']=='apple_vision' and evidence['sha256']==digest
    assert (evidence['width'],evidence['height'])==(960,360)
    assert [line['text'] for line in evidence['lines']]==expected
    for line,box in zip(evidence['lines'],boxes):
        assert fact.snapshot[line['start']:line['end']]==line['text']
        xs,ys=zip(*line['polygon'])
        # Actual recognized region must lie at the rendered line, not its
        # vertically mirrored position or a resized/oriented coordinate plane.
        assert min(xs)==pytest.approx(box[0],abs=12) and max(xs)==pytest.approx(box[2],abs=12)
        assert min(ys)==pytest.approx(box[1],abs=12) and max(ys)==pytest.approx(box[3],abs=12)
    assert path.read_bytes()==data  # OCR never rewrites the source bytes.
    assert runner.recognize_bytes(png(),'image/png').lines==()


def test_vision_equal_score_candidates_are_an_explicit_review_signal():
    base=observation('原文',.5)
    first=SimpleNamespace(string=lambda:'原文',confidence=lambda:.5)
    second=SimpleNamespace(string=lambda:'原义',confidence=lambda:.5)
    base.topCandidates_=lambda count:[first,second]
    result=vision_ocr._result([base],100,80)
    assert result.lines[0].alternatives==('原义',)


def test_vision_half_score_alone_does_not_block_correct_text():
    result=vision_ocr._result([observation('1分钟前',.5)],100,80)
    class Runner:
        def recognize_bytes(self,*args):return result
    content=png()
    fact,lineage=image_source_fact('',[{'member_id':'image-1','content':content,'mime_type':'image/png',
        'sha256':hashlib.sha256(content).hexdigest()}],Runner())
    assert fact.uncertainties[0]['status']=='advisory'
    assert lineage['image_ocr'][0]['lines'][0]['confidence']==.5


@pytest.mark.parametrize('axis,bound', [('left',0),('right',100),('top',0),('bottom',80)])
def test_subpixel_boundary_roundoff_is_recorded(axis,bound):
    epsilon=0.0000021
    pixels=[(0,0),(100,0),(100,80),(0,80)]
    adjusted=[]
    for x,y in pixels:
        if axis=='left' and x==0: x=-epsilon
        if axis=='right' and x==100: x=100+epsilon
        if axis=='top' and y==0: y=-epsilon
        if axis=='bottom' and y==80: y=80+epsilon
        adjusted.append((x/100,1-y/80))
    line=vision_ocr._result([observation(points=adjusted)],100,80).lines[0]
    assert line.original_polygon
    assert all(0<=x<=100 and 0<=y<=80 for x,y in line.polygon)
    assert any(x<0 or x>100 or y<0 or y>80 for x,y in line.original_polygon)


@pytest.mark.parametrize('value',[-.001,float('nan'),float('inf'),-float('inf')])
def test_real_overflow_or_nonfinite_is_not_clamped(value):
    points=((value/100,.9),(.8,.9),(.8,.6),(value/100,.6))
    with pytest.raises(ocr.OcrError,match='ocr_invalid_output'):
        vision_ocr._result([observation(points=points)],100,80)


def test_clamping_must_not_make_degenerate_polygon_valid():
    with pytest.raises(ocr.OcrError,match='ocr_invalid_output'):
        vision_ocr._result([observation(points=((-1e-9,.9),(0,.9),(0,.6),(-1e-9,.6)))],100,80)
