# Windows-native build; only explicit product resources and clean dependencies.
from pathlib import Path
import os
import runpy
from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules, collect_dynamic_libs, copy_metadata

project = Path(SPECPATH).parent
tools = Path(os.environ['KD_BUILD_WINDOWS_CACHE']) / 'tools'
resources = runpy.run_path(str(project / 'packaging/resources.py'))
datas = resources['application_datas'](project)
codec = Path(os.environ['KD_BUILD_HDIFFPATCH']) / 'hpatchz.exe'
datas += [(str(codec), 'tools')]
datas += [(os.environ['KD_BUILD_WINDOWS_VERSION'], '.')]
datas += [(str(project / 'packaging/assets/app-icon.ico'), 'assets')]
datas += resources['opencli_datas'](tools / 'opencli/node_modules/@jackwener/opencli')
modules = tools / 'opencli/node_modules'
datas += [(str(p), str(Path('opencli/node_modules') / p.relative_to(modules).parent))
          for p in modules.rglob('*') if p.is_file() and '@jackwener' not in p.relative_to(modules).parts]
paddle = Path(os.environ['KD_BUILD_PADDLE_MODELS'])
if not (paddle / 'manifest.json').is_file():
    raise RuntimeError('Verified Paddle model inventory required')
datas += [(str(p), str(Path('paddle-models') / p.relative_to(paddle).parent)) for p in paddle.rglob('*') if p.is_file()]
binaries = [(str(tools / 'bin' / name), 'bin') for name in ('node.exe', 'ffmpeg.exe', 'ffprobe.exe')]
vc_names = ('msvcp140.dll', 'msvcp140_1.dll', 'msvcp140_atomic_wait.dll',
            'vcruntime140.dll', 'vcruntime140_1.dll', 'vcruntime140_threads.dll')
binaries += [(str(tools / 'vc' / name), '.') for name in vc_names]
# torchvision 0.29 uses _C_stable/image_stable, newer than the PyInstaller hook.
binaries += collect_dynamic_libs('torchvision', search_patterns=['*.dll', '*.pyd'])
hiddenimports = ['lark_oapi', 'lark_oapi.event.callback.model.p2_card_action_trigger',
                 'knowledge_distiller.v1.feishu_socket', 'Crypto.Cipher.AES']
for package in ('paddle', 'paddleocr', 'paddlex'):
    # Importing every Paddle training/JIT plugin during collection crashes its
    # Windows native runtime. Preserve its package tree without executing it.
    datas += collect_data_files(package, include_py_files=True)
    binaries += collect_dynamic_libs(package, search_patterns=['*.dll', '*.pyd'])
    hiddenimports.append(package)
for package in ('config', 'core', 'storage', 'utils', 'auth', 'tos',
                'docling', 'docling_core', 'docling_ibm_models', 'docling_parse', 'rapidocr'):
    data, binary, hidden = collect_all(package)
    datas += data
    binaries += binary
    hiddenimports += hidden
for package in ('transformers.models.rt_detr_v2', 'transformers.models.rt_detr_resnet',
                'transformers.models.idefics3', 'transformers.models.llama'):
    hiddenimports += collect_submodules(package)
for distribution in ('douyin-downloader', 'yt-dlp', 'docling', 'docling-slim', 'paddleocr',
                     'paddlex', 'paddlepaddle', 'opencv-python', 'opencv-contrib-python', 'rapidocr',
                     'onnxruntime', 'torch', 'transformers', 'imagesize', 'pyclipper', 'lark-oapi', 'websockets',
                     'pypdfium2', 'python-bidi', 'shapely'):
    datas += copy_metadata(distribution, recursive=True)
datas = list(dict.fromkeys(datas))
binaries = list(dict.fromkeys(binaries))
a = Analysis([str(project / 'packaging/windows_entry.py')], pathex=[str(project / 'src')],
             datas=datas, binaries=binaries, hiddenimports=sorted(set(hiddenimports)),
             excludes=['knowledge_distiller.legacy', 'knowledge_distiller.v1.mac_app',
                       'mlx', 'mlx_qwen3_asr', 'qwen_asr', 'pytest', 'IPython', 'torchaudio'])
pyz = PYZ(a.pure)
# Keep a single, same-version Microsoft runtime set; Python's older embedded
# vcruntime must not replace the version required by torch_cpu.dll.
a.binaries = [entry for entry in a.binaries if entry[0].lower() not in vc_names]
a.binaries += [(name, str(tools / 'vc' / name), 'BINARY') for name in vc_names]
exe = EXE(pyz, a.scripts, [('X utf8', None, 'OPTION')], exclude_binaries=True, name='KnowledgeDistiller',
          debug=False, strip=False, upx=False, console=False, icon=str(project / 'packaging/assets/app-icon.ico'), manifest=str(project / 'packaging/windows.manifest'))
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='KnowledgeDistiller')

# A small onefile helper runs outside the replaced application directory.
u = Analysis([str(project / 'packaging/windows_update_entry.py')], pathex=[str(project / 'src')],
             datas=[(str(project/'src/knowledge_distiller/v1/adapters/update-codec-notices.txt'),'knowledge_distiller/v1/adapters'),
                    (str(project/'src/knowledge_distiller/v1/adapters/python-runtime.json'),'knowledge_distiller/v1/adapters'),
                    (str(codec),'tools'),
                    (str(project/'packaging/update_config.json'),'knowledge_distiller/v1/adapters'),
                    (str(project/'src/knowledge_distiller/v1/adapters/docling-models-manifest.json'),'knowledge_distiller/v1/adapters')],
             hiddenimports=['cryptography.hazmat.primitives.asymmetric.ed25519','win32job'],
             excludes=['torch','paddle','docling','flask','tkinter','pytest'])
upyz = PYZ(u.pure)
uexe = EXE(upyz,u.scripts,u.binaries,u.datas, [('X utf8',None,'OPTION')],
           name='update-helper',console=False,upx=False,strip=False)
import shutil
shutil.copy2(str(Path(DISTPATH)/'update-helper.exe'),str(Path(DISTPATH)/'KnowledgeDistiller'/'update-helper.exe'))
