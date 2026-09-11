import json
import builtins
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from knowledge_distiller.v1 import runtime_probe as probe


@pytest.fixture
def runtime(monkeypatch,tmp_path):
    tensor=SimpleNamespace(item=lambda:3.0)
    mx=SimpleNamespace(array=lambda x:x,sum=lambda x:tensor)
    modules={
        'mlx':SimpleNamespace(core=mx),'mlx.core':mx,
        'mlx_qwen3_asr':SimpleNamespace(transcribe=lambda:None),
        'core.api_client':SimpleNamespace(DouyinAPIClient=object),
        'AppKit':SimpleNamespace(NSApplication=object),'tos':SimpleNamespace(),
        'paddle':SimpleNamespace(sum=lambda x:tensor,to_tensor=lambda x:x),
        'paddleocr':SimpleNamespace(),
        'torch':SimpleNamespace(tensor=lambda x:SimpleNamespace(sum=lambda:tensor)),
        'rapidocr':SimpleNamespace(),'onnxruntime':SimpleNamespace(),
        'docling.document_converter':SimpleNamespace(DocumentConverter=object),
    }
    for name,value in modules.items():monkeypatch.setitem(sys.modules,name,value)
    monkeypatch.setattr(sys,'_MEIPASS',str(tmp_path),raising=False)
    monkeypatch.setattr(probe,'version',lambda name:'verified')
    monkeypatch.setattr(probe.subprocess,'run',lambda *args,**kwargs:SimpleNamespace(stdout='loaded\n'))
    return tmp_path/'probe.json'


def test_existing_two_argument_probe_stays_compatible_without_eager_engines(runtime,monkeypatch):
    original = builtins.__import__
    def guarded(name,*args,**kwargs):
        if name.split('.')[0] in {'paddle','paddleocr','torch','rapidocr','onnxruntime','docling'}:
            raise AssertionError('Default probe must not initialize engines: '+name)
        return original(name,*args,**kwargs)
    monkeypatch.setattr(builtins,'__import__',guarded)
    assert probe.check(runtime,None) == 0
    report=json.loads(runtime.read_text())
    assert report['checks']['document_runtime']['docling'] == 'verified'
    assert 'paddleocr' not in report['checks']['document_runtime']
    assert report['checks']['image_ocr_runtime']['engine'] == ('apple_vision' if sys.platform == 'darwin' else 'paddleocr')
    assert 'ocr' not in report['checks'] and 'pdf' not in report['checks']


def test_explicit_image_failure_is_reported_not_success(runtime,monkeypatch):
    from knowledge_distiller.v1.ocr import OcrError
    def failed(path):raise OcrError('ocr_invalid_image')
    monkeypatch.setattr(probe,'_check_ocr',failed)
    assert probe.check(runtime,ocr_image=Path('broken.png')) == 1
    report=json.loads(runtime.read_text())
    assert report['failed_stage'] == 'ocr' and report['error_code'] == 'ocr_invalid_image'


def test_only_explicit_document_inputs_are_converted(runtime,monkeypatch):
    from knowledge_distiller.v1 import docling_source
    monkeypatch.setattr(docling_source,'DoclingSourceConverter',lambda:object())
    calls=[]
    def convert(path,kind,converter):
        calls.append((str(path),kind));return {'entry_count':2}
    monkeypatch.setattr(probe,'_check_document',convert)
    assert probe.check(runtime,pdf=Path('fixture.pdf'),epub=Path('fixture.epub')) == 0
    assert calls == [('fixture.pdf','pdf'),('fixture.epub','epub')]


def test_document_report_retains_formula_and_provenance(tmp_path):
    from knowledge_distiller.v1.docling_source import (DocumentEntry,DocumentProvenance,
        DoclingSourceResult,DocumentPageImage)
    path=tmp_path/'sample.pdf';path.write_bytes(b'%PDF-fixture')
    result=DoclingSourceResult((DocumentEntry('formula','a^{2}',
        (DocumentProvenance(page=1,bbox=(0,0,10,10),charspan=(0,5),original_charspan=(0,2)),),
        '#/texts/0',label='formula'),),1,page_images=(DocumentPageImage(1,20,20,b'png'),))
    report=probe._check_document(path,'pdf',SimpleNamespace(convert_bytes=lambda *args:result))
    assert report['formula_samples'] == ['a^{2}'] and report['page_images'] == 1
    assert report['samples'][0]['provenance'][0]['original_charspan'] == (0,2)
