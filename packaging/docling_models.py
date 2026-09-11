"""Validate the release model inventory and feed the same files to PyInstaller."""
import hashlib
import json
from pathlib import Path


def model_datas(root):
    root = Path(root).resolve()
    manifest = json.loads((root/'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('docling_version') != '2.126.0':
        raise ValueError('Docling model manifest version mismatch')
    files = manifest['files']
    required = {'docling-project--docling-layout-heron', 'docling-project--docling-models',
                'docling-project--CodeFormulaV2', 'RapidOcr'}
    if not required <= {Path(name).parts[0] for name in files}:
        raise ValueError('Incomplete Docling model families')
    datas = [(str(root/'manifest.json'), 'docling-models')]
    for name, entry in files.items():
        path = root/name
        if not path.resolve().is_relative_to(root) or not path.is_file():
            raise ValueError('Missing or invalid Docling model asset: '+name)
        with path.open('rb') as source:
            digest = hashlib.file_digest(source, 'sha256').hexdigest()
        if path.stat().st_size != entry['size'] or digest != entry['sha256']:
            raise ValueError('Docling model asset checksum mismatch: '+name)
        datas.append((str(path), str(Path('docling-models')/Path(name).parent)))
    return datas
