"""Explicit synthetic document test; all socket connections are denied."""
import json
import os
from pathlib import Path
import socket
import traceback

root = Path(__file__).resolve().parents[1] / '.windows-build'
os.environ['HF_HOME'] = str(root / 'offline-cache')
os.environ['HF_HUB_OFFLINE'] = '1'
os.environ['HF_HUB_DISABLE_IMPLICIT_TOKEN'] = '1'
from knowledge_distiller.v1 import docling_source as source
from knowledge_distiller.v1.runtime_probe import _check_document

source._bundled_artifacts = lambda: root / 'docling-models'


def denied(*args, **kwargs):
    raise RuntimeError('network_disabled_for_offline_test')


socket.create_connection = denied
socket.socket.connect = denied
report = {'network': 'all Python socket connections denied', 'checks': {}}
try:
    converter = source.DoclingSourceConverter()
    for name, kind in [('native.pdf', 'pdf'), ('scan.pdf', 'pdf'), ('sample.epub', 'epub')]:
        print('Converting', name, flush=True)
        report['checks'][name] = _check_document(root / 'samples' / name, kind, converter)
        print(report['checks'][name], flush=True)
    report['ok'] = True
except Exception:
    traceback.print_exc()
    report['ok'] = False
finally:
    (root / 'docling-smoke.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
raise SystemExit(0 if report['ok'] else 1)
