# Build on Apple Silicon; user data and optional Qwen stay outside the bundle.
from pathlib import Path
import runpy
import os
import shutil
import subprocess
from PyInstaller.utils.hooks import (collect_all, collect_data_files, collect_dynamic_libs,
                                    collect_submodules, copy_metadata)

project = Path(SPECPATH).parent
opencli = Path(shutil.which('opencli')).resolve()
while not (opencli/'dist/src/browser/page.js').is_file():
    if opencli.parent==opencli:raise RuntimeError('Build needs installed OpenCLI')
    opencli=opencli.parent

resources = runpy.run_path(str(project / 'packaging/resources.py'))
datas = resources['application_datas'](project) + resources['opencli_datas'](opencli)
binaries = [(shutil.which(name),'bin') for name in ('node','ffmpeg','ffprobe')]
hiddenimports = resources['installer_hiddenimports']('darwin')
for package in ('config','core','storage','utils','auth','tos'):
    data, binary, hidden = collect_all(package)
    datas += data; binaries += binary; hiddenimports += hidden
for package in ('douyin-downloader','yt-dlp','lark-oapi','websockets','pycryptodome'):
    datas += copy_metadata(package)
# torchvision 0.29 loads _C_stable.so by path; the bundled hook only knows _C.
binaries += collect_dynamic_libs('torchvision', search_patterns=['*.so', '*.dylib'])
for package in ('docling', 'docling_core', 'docling_ibm_models', 'docling_parse'):
    datas += collect_data_files(package)
    binaries += collect_dynamic_libs(package)
datas += collect_data_files('rapidocr', includes=['config.yaml', 'default_models.yaml'])
# Lazy registries and Hugging Face AutoModel load these exact deployed families.
for package in (
    'rapidocr.inference_engine.onnxruntime',
    'transformers.models.rt_detr_v2', 'transformers.models.rt_detr_resnet',
    'transformers.models.idefics3', 'transformers.models.llama',
):
    hiddenimports += collect_submodules(package)
hiddenimports += ['Vision', 'docling.models.plugins.defaults']
hiddenimports += ['lark_oapi.ws.client', 'Crypto.Cipher.AES',
                  'lark_oapi.event.callback.model.p2_card_action_trigger']
# Docling discovers defaults through docling-slim entry points.
# Metadata is not model weights; Vision uses the system framework.
for distribution in ('pyobjc-framework-Vision', 'opencv-python',
                     'pyclipper', 'pypdfium2', 'shapely',
                     'docling', 'docling-slim', 'docling-core', 'docling-parse',
                     'docling-ibm-models', 'rapidocr', 'onnxruntime'):
    datas += copy_metadata(distribution, recursive=True)
datas = list(dict.fromkeys(datas))
binaries = list(dict.fromkeys(binaries))
hiddenimports = sorted(set(hiddenimports))
a = Analysis([str(project/'packaging/mac_entry.py')],pathex=[str(project/'src')],
             binaries=binaries,datas=datas,hiddenimports=hiddenimports,
             excludes=['mlx', 'mlx_qwen3_asr', 'paddle', 'paddleocr', 'paddlex', 'knowledge_distiller.legacy','pytest','tkinter','IPython','matplotlib','torchaudio'])
# OpenCV's wheel embeds an older OpenSSL 3. PyInstaller otherwise aliases Node's
# newer OpenSSL dependency to that copy, making bundled Node fail at dyld load.
# Use the actual Node-linked ABI-compatible OpenSSL 3 pair for both destinations.
node_dependencies = subprocess.check_output(['otool', '-L', shutil.which('node')], text=True)
node_ssl = {Path(line.strip().split(' (', 1)[0]).name: line.strip().split(' (', 1)[0]
            for line in node_dependencies.splitlines()[1:]
            if Path(line.strip().split(' (', 1)[0]).name in ('libcrypto.3.dylib','libssl.3.dylib')}
for index, (dest, source, kind) in enumerate(a.binaries):
    if kind != 'SYMLINK' and Path(dest).name in node_ssl:
        replacement = node_ssl[Path(dest).name]
        if not Path(replacement).is_file():
            raise RuntimeError('Cannot resolve Node OpenSSL dependency: '+replacement)
        a.binaries[index] = (dest, replacement, kind)
pyz = PYZ(a.pure)
exe = EXE(pyz,a.scripts,[],exclude_binaries=True,name='KnowledgeDistiller',
          debug=False,strip=False,upx=False,console=False,target_arch='arm64')
coll = COLLECT(exe,a.binaries,a.datas,strip=False,upx=False,name='KnowledgeDistiller')
app = BUNDLE(coll,name='知识蒸馏器.app',icon=str(project/'packaging/assets/KnowledgeDistiller-transparent.icns'),bundle_identifier='local.knowledge-distiller.app',
             info_plist={'CFBundleDisplayName':'知识蒸馏器','NSHighResolutionCapable':True,
                         'CFBundleShortVersionString':os.environ['KD_BUILD_PRODUCT_VERSION'],
                         'CFBundleVersion':os.environ['KD_BUILD_VERSION'],
                         'NSHumanReadableCopyright':'Personal local knowledge tool',
                         'LSMinimumSystemVersion':'14.0'})
