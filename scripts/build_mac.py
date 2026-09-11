"""Build a local Apple Silicon release candidate into an empty owned directory."""
import argparse
import hashlib
from importlib.metadata import version
import json
import os
import runpy
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile

parser=argparse.ArgumentParser(description='构建知识蒸馏器 Mac 发行候选')
parser.add_argument('--output',type=Path,required=True)
parser.add_argument('--version', required=True, help='递增构建号 YYYY.MM.DD.N')
parser.add_argument('--product-version', default='1.1')
parser.add_argument('--signing-config', type=Path, help='显式本地证书 identity/certificate_sha1；省略则仅 ad-hoc 手动候选')
parser.add_argument('--manual-update-only', action='store_true', help='显式构建仅手动更新候选；正式稳定签名版本默认启用差量安装')
parser.add_argument('--sparkle-sdk',type=Path)
parser.add_argument('--update-config',type=Path,help='公钥、更新源；隔离验收可指定test_data_root')
parser.add_argument('--docling-models',type=Path,required=True,help='预下载并带 manifest.json 的完整 Docling 模型目录')
args=parser.parse_args()
if bool(args.sparkle_sdk) != bool(args.update_config):
    parser.error('--sparkle-sdk 与 --update-config 必须同时提供')
if sys.platform!='darwin' or platform.machine()!='arm64':
    parser.error('请在 Apple Silicon Mac 上构建')
for name in ('node','ffmpeg','ffprobe','opencli'):
    if not shutil.which(name):parser.error('构建机缺少 '+name)
output=args.output.expanduser().resolve()
if output.exists() and any(output.iterdir()):parser.error('输出目录必须为空，以保留已有候选')
output.mkdir(parents=True,exist_ok=True)
project=Path(__file__).resolve().parents[1]
models=args.docling_models.expanduser().resolve()
runpy.run_path(str(project/'packaging/docling_models.py'))['model_datas'](models)
os.environ['KD_BUILD_DOCLING_MODELS']=str(models)
signing = runpy.run_path(str(project/'packaging/mac_signing.py'))
signing['version_info'](args.product_version, args.version)
os.environ['KD_BUILD_VERSION']=args.version
os.environ['KD_BUILD_PRODUCT_VERSION']=args.product_version
work=Path(tempfile.mkdtemp(prefix='knowledge-distiller-build-'))

def source_fingerprints():
    files = [project/'pyproject.toml', Path(__file__).resolve()]
    for folder in ('src','packaging'):
        files.extend(p for p in (project/folder).rglob('*') if p.is_file()
                     and '__pycache__' not in p.parts and p.suffix != '.pyc')
    return {str(p.relative_to(project)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(files)}

manifest={'architecture':platform.machine(),'build_macos':platform.mac_ver()[0],
          'version':args.version, 'product_version':args.product_version,
          'git_head':subprocess.check_output(['git','rev-parse','HEAD'],cwd=project,text=True).strip(),
          'git_dirty':bool(subprocess.check_output(['git','status','--porcelain'],cwd=project,text=True).strip()),
          'packages':{name:version(name) for name in ('pyinstaller','pyobjc-framework-Cocoa',
              'douyin-downloader','tos','pyobjc-framework-Vision',
              'docling','docling-slim','docling-core','docling-parse','docling-ibm-models','rapidocr',
              'onnxruntime','numpy','opencv-python','torch',
              'torchvision','transformers','lark-oapi','websockets','pycryptodome')},
          'docling_models':json.loads((models/'manifest.json').read_text()),
          'work_directory':str(work),'status':'building','source_sha256':source_fingerprints()}
manifest_path=output/'build-manifest.json'
manifest_path.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
try:
    with (output/'build.log').open('w') as log:
        subprocess.run([sys.executable,'-m','PyInstaller','--noconfirm','--distpath',str(output),
                        '--workpath',str(work),str(project/'packaging/KnowledgeDistiller.spec')],
                       cwd=project,stdout=log,stderr=subprocess.STDOUT,check=True)
    app=output/'知识蒸馏器.app'
    if args.sparkle_sdk:
        config=json.loads(args.update_config.read_text())
        runpy.run_path(str(project/'packaging/sparkle.py'))['attach'](app,args.sparkle_sdk,config,project)
        manifest['update_configuration']=config
    signing_config = json.loads(args.signing_config.read_text()) if args.signing_config else {}
    # Keep ad-hoc builds manual; stable signed releases support the updater.
    import plistlib
    info_path = app/'Contents/Info.plist'
    info = plistlib.loads(info_path.read_bytes())
    info.update(signing['update_policy'](signing_config, config if args.sparkle_sdk else {},
                                          manual=args.manual_update_only))
    manifest['manual_update_only'] = info['KDManualUpdateOnly']
    info_path.write_bytes(plistlib.dumps(info))
    manifest['code_signing'] = signing['sign_bundle'](app, signing_config)
    subprocess.run(['/usr/bin/codesign','--verify','--deep','--strict',str(app)],check=True)
    manifest['status']='built'
except Exception:
    manifest['status']='failed'
    raise
finally:
    after=source_fingerprints()
    manifest['changed_during_build']=[p for p in sorted(set(after)|set(manifest['source_sha256']))
                                     if after.get(p)!=manifest['source_sha256'].get(p)]
    manifest_path.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
print(app)

if manifest["status"] == "built" and not manifest["changed_during_build"]:
    shutil.rmtree(work)
    # BUNDLE is self-contained; COLLECT is only an intermediate duplicate.
    shutil.rmtree(output/"KnowledgeDistiller")
