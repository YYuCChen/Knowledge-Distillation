"""Explicit local release check; never loads settings or creates a database."""
import json
import os
from pathlib import Path
import subprocess
import sys
import traceback
from collections import Counter
from importlib.metadata import version


def check(destination, audio=None, *, ocr_image=None, pdf=None, epub=None, component_root=None):
    report = {'frozen':bool(getattr(sys,'frozen',False)),'checks':{}}
    stage = 'binaries'
    try:
        for tool in ('node','ffmpeg','ffprobe'):
            result=subprocess.run([tool,'--version' if tool=='node' else '-version'],capture_output=True,text=True,check=True,timeout=15)
            report['checks'][tool]=result.stdout.splitlines()[0]
        stage = 'native_imports'
        from core.api_client import DouyinAPIClient
        from AppKit import NSApplication
        report['checks']['python_adapters']='loaded'
        if getattr(sys, 'frozen', False):
            import plistlib
            contents = Path(sys.executable).resolve().parents[1]
            info_path = contents/'Info.plist'
            if info_path.is_file() and plistlib.loads(info_path.read_bytes()).get('SUPublicEDKey'):
                stage = 'update_runtime'
                subprocess.run([str(contents/'MacOS/update-helper'), '--probe'],
                               check=True, capture_output=True, timeout=30)
                if not (contents/'Helpers/Updater.app/Contents/MacOS/update-cli').is_file():
                    raise RuntimeError('Update driver is missing')
                report['checks']['update_runtime'] = 'independent installer loaded'
        import tos
        from .codex import executable
        report['checks']['tos']='loaded'
        stage = 'feishu_runtime'
        import lark_oapi
        from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse
        from Crypto.Cipher import AES
        report['checks']['feishu_runtime']={'sdk':version('lark-oapi'),'websockets':version('websockets')}
        stage = 'document_runtime'
        # Read package metadata only. OCR/document models stay lazy until an
        # explicit disposable sample is supplied; no engine selection or self-test.
        report['checks']['document_runtime'] = {
            name: version(name) for name in ('docling','docling-core','rapidocr','onnxruntime','torch','transformers')}
        report['checks']['image_ocr_runtime'] = ({'engine': 'apple_vision',
            'bridge_version': version('pyobjc-framework-Vision')} if sys.platform == 'darwin'
            else {'engine': 'paddleocr', 'runtime_version': version('paddleocr')})
        try:
            report['checks']['codex_executable']=executable()
        except Exception:
            report['checks']['codex_executable']='not installed'
        stage = 'opencli'
        root=Path(sys._MEIPASS)/'opencli'/'dist/src/browser/page.js'
        result=subprocess.run(['node','--input-type=module','-e','await import('+json.dumps(root.as_uri())+'); console.log("loaded")'],capture_output=True,text=True,check=True,timeout=15)
        report['checks']['opencli']=result.stdout.strip()
        if audio:
            stage = 'asr'
            from .qwen_component import QwenComponent, ComponentQwenRuntime
            if component_root is None:
                raise ValueError('Explicit component root is required for ASR checks')
            result=ComponentQwenRuntime(QwenComponent(component_root)).transcribe(Path(audio))
            report['checks']['asr']={'text':result.text,'chunks':len(result.chunks or [])}
        if ocr_image:
            stage = 'ocr'
            report['checks']['ocr'] = _check_ocr(Path(ocr_image))
        if pdf or epub:
            from .docling_source import DoclingSourceConverter
            converter = DoclingSourceConverter()
            for kind, filename in (('pdf',pdf),('epub',epub)):
                if filename:
                    stage = kind
                    report['checks'][kind] = _check_document(Path(filename),kind,converter)
        report['ok']=True
    except Exception as error:
        # Explicit diagnostic mode uses only caller-supplied disposable inputs;
        # retain the chained exception so frozen import failures are actionable.
        traceback.print_exc()
        report.update(ok=False,failed_stage=stage,error_type=type(error).__name__)
        if isinstance(getattr(error,'code',None),str):
            report['error_code']=error.code
        if isinstance(error,subprocess.CalledProcessError):
            report['diagnostic']=error.stderr[-1500:]
    Path(destination).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return 0 if report['ok'] else 1


def _check_ocr(path):
    from dataclasses import asdict
    from .ocr import default_ocr_runner
    mime = {'.png':'image/png','.jpg':'image/jpeg','.jpeg':'image/jpeg',
            '.webp':'image/webp','.bmp':'image/bmp','.tiff':'image/tiff','.tif':'image/tiff'}.get(path.suffix.lower())
    result = default_ocr_runner().recognize_bytes(path.read_bytes(),mime)
    return {'input':str(path),'result':asdict(result),'text':result.text}


def _check_document(path,kind,converter):
    result = converter.convert_bytes(path.read_bytes(),kind)
    return {'input':str(path),'runtime_version':result.runtime_version,'page_count':result.page_count,
        'entry_count':len(result.entries),'kinds':dict(Counter(e.kind for e in result.entries)),
        'text_characters':sum(len(e.text) for e in result.entries),
        'image_entries':sum(bool(e.image_bytes) for e in result.entries),
        'page_images':len(result.page_images),
        'formula_samples':[e.text for e in result.entries if e.kind=='formula'][:5],
        'samples':[{'kind':e.kind,'label':e.label,'text':e.text[:200],
                    'provenance':[{'page':p.page,'bbox':p.bbox,'charspan':p.charspan,
                                   'original_charspan':p.original_charspan} for p in e.provenance]}
                   for e in result.entries[:8]]}
