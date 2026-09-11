"""Fetch the complete deployed Docling model assets before a release build."""
import argparse
import hashlib
from importlib.metadata import version
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if version('docling') != '2.126.0':
        raise RuntimeError('Use the declared Docling release to prepare models')
    from huggingface_hub import HfApi, snapshot_download
    from docling.datamodel.pipeline_options import LayoutObjectDetectionOptions
    from docling.models.stages.ocr.rapid_ocr_model import RapidOcrModel
    layout = LayoutObjectDetectionOptions().model_spec
    repos = [(layout.repo_id, layout.revision),
             ('docling-project/docling-models', 'v2.3.0'),
             ('docling-project/CodeFormulaV2', 'main')]
    sources = []
    for repo, revision in repos:
        commit = HfApi().model_info(repo, revision=revision).sha
        print('Downloading', repo, commit, flush=True)
        snapshot_download(repo, revision=commit, local_dir=output/repo.replace('/', '--'))
        sources.append({'repo':repo, 'revision':commit})
    print('Downloading RapidOCR onnxruntime:ch assets', flush=True)
    RapidOcrModel.download_models(backend='onnxruntime', lang='ch',
                                 local_dir=output/'RapidOcr', force=True)
    sources.append({'runtime':'rapidocr', 'version':version('rapidocr'), 'backend':'onnxruntime', 'lang':'ch'})
    files = {}
    for path in sorted(output.rglob('*')):
        if not path.is_file() or '.cache' in path.parts or path.name=='manifest.json':
            continue
        with path.open('rb') as source:
            digest = hashlib.file_digest(source, 'sha256').hexdigest()
        files[str(path.relative_to(output))] = {'size':path.stat().st_size,'sha256':digest}
    (output/'manifest.json').write_text(json.dumps({'docling_version':version('docling'),
        'sources':sources,'files':files}, ensure_ascii=False, indent=2), encoding='utf-8')
    print('Prepared', len(files), 'files;', sum(v['size'] for v in files.values()), 'bytes', flush=True)


if __name__ == '__main__':
    main()
