"""User-triggered Qwen installation, isolated from the app and formal task queue."""
import contextlib
import errno
import urllib.error
import time
import hashlib
import json
import os
from pathlib import Path
import platform
import signal
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import urllib.request

from knowledge_distiller.primary import (
    QWEN_MODEL_ID, QWEN_MODEL_REVISION, QWEN_RUNTIME_VERSION,
    QwenRuntimeResult, QwenRuntimeUnavailable, QwenRuntimeFailure,
)

PYTHON_URL = ('https://github.com/astral-sh/python-build-standalone/releases/download/20260901/'
              'cpython-3.11.16%2B20260901-aarch64-apple-darwin-install_only_stripped.tar.gz')
PYTHON_SHA256 = '768f05cf200273bbdda9a5955a5a6892a4b22f2a0b1e4b0a9160f5c7fce86816'
ASSETS = Path(__file__).parent / 'adapters'
BUSY = {'downloading_runtime', 'installing_runtime', 'downloading_model', 'verifying_runtime'}
LABELS = {'not_installed':'尚未安装', 'downloading_runtime':'正在下载运行组件',
          'installing_runtime':'正在安装运行组件', 'downloading_model':'正在下载模型权重',
          'verifying_runtime':'正在进行本机识别自检',
          'ready':'已安装，可以启用', 'failed':'安装失败，可重试',
          'interrupted':'安装已中断，可继续', 'unsupported':'此平台的本地组件尚未提供'}


class ComponentError(RuntimeError):
    pass


def _read(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _atomic_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    temporary.replace(path)


def _environment(root, *, offline=False):
    # Frozen application's loader paths must never leak into standalone Python.
    env = {k:v for k,v in os.environ.items() if not k.startswith(
        ('PYTHON', 'DYLD_', 'LD_LIBRARY', 'PIP_', 'HF_', 'HUGGINGFACE_'))}
    for key in ('__PYVENV_LAUNCHER__','VIRTUAL_ENV','_MEIPASS2'):
        env.pop(key,None)
    env.update(PYTHONUTF8='1', PYTHONNOUSERSITE='1', HF_HOME=str(root/'cache'),
               PIP_CACHE_DIR=str(root/'pip-cache'), PIP_CONFIG_FILE=os.devnull,
               HF_HUB_DISABLE_IMPLICIT_TOKEN='1', HF_HUB_DISABLE_TELEMETRY='1')
    if offline:
        env['HF_HUB_OFFLINE']='1'
    return env


class QwenComponent:
    def __init__(self, root):
        self.root = Path(root)
        self.active = self.root / 'installed'
        self._thread = None
        self._stop = threading.Event()
        self._process = None

    @property
    def supported(self):
        return sys.platform == 'darwin' and platform.machine() == 'arm64'

    def ready(self):
        manifest = _read(self.active/'component.json')
        if not isinstance(manifest, dict) or manifest.get('version') != QWEN_RUNTIME_VERSION or manifest.get('revision') != QWEN_MODEL_REVISION:
            return False
        if not (self.active/'python/bin/python3').is_file():
            return False
        files = manifest.get('files')
        if not isinstance(files, dict) or not files or 'config.json' not in files or not any(isinstance(name,str) and name.endswith('.safetensors') for name in files):
            return False
        for name,size in files.items():
            if not isinstance(name,str) or Path(name).is_absolute() or '..' in Path(name).parts:
                return False
            path = self.active/'model'/name
            try:
                if path.stat().st_size != size:
                    return False
            except OSError:
                return False
        return True

    def _disk_free(self):
        path=self.root
        while not path.exists() and path != path.parent:path=path.parent
        try:return shutil.disk_usage(path).free
        except OSError:return None

    def _model_progress(self):
        model=self.root/'installing/model'
        metadata=_read(self.root/'model-download.json')
        total=metadata.get('total_bytes') if isinstance(metadata,dict) else None
        if not isinstance(total,int) or total<=0:return {}
        received=0
        try:
            for path in model.rglob('*'):
                if path.is_file() and ('.cache' not in path.relative_to(model).parts or path.name.endswith('.incomplete')):
                    received+=path.stat().st_size
        except OSError:return {}
        return {'completed_bytes':min(received,total),'total_bytes':total}

    def status(self):
        if not self.supported:
            state = 'unsupported'
        elif self.ready():
            state = 'ready'
        else:
            saved = _read(self.root/'state.json')
            state = saved.get('state','not_installed') if isinstance(saved,dict) else 'failed'
            if state in BUSY and not self._locked():
                state = 'interrupted'
            if state == 'ready' or state not in LABELS:
                state = 'failed'
        saved=_read(self.root/'state.json')
        if not isinstance(saved,dict):saved={}
        progress=self._model_progress() if state=='downloading_model' else saved.get('progress',{}) if state=='downloading_runtime' else {}
        detail=saved.get('detail','') if state=='failed' else ''
        manifest=_read(self.active/'component.json')
        verified=state=='ready' and isinstance(manifest,dict) and manifest.get('self_test_passed') is True
        return dict(self_test_passed=verified,detail=detail,free_bytes=self._disk_free(),progress=progress,
                    state=state, label=LABELS[state], busy=state in BUSY,
                    ready=state=='ready', can_install=state in {'not_installed','failed','interrupted'})

    def _locked(self):
        if not (self.root/'install.lock').exists():
            return False
        import fcntl
        with (self.root/'install.lock').open('r') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return False
            except BlockingIOError:
                return True

    def start(self):
        if not self.supported:
            raise ComponentError('qwen_platform_unsupported')
        if self.ready():
            return
        import fcntl
        self.root.mkdir(parents=True,exist_ok=True)
        lock=(self.root/'install.lock').open('a')
        try:
            fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            return  # Repeated POST shares the existing installation.
        if self.ready():
            lock.close()
            return
        self._stop.clear()
        self._state('downloading_runtime')
        self._thread=threading.Thread(target=self._install,args=(lock,),daemon=True,name='qwen-install')
        self._thread.start()

    def _state(self,state,**extra):
        _atomic_json(self.root/'state.json',{'state':state,**extra})

    def _install(self,lock):
        try:
            staging=self.root/'installing'
            staging.mkdir(exist_ok=True)
            python=staging/'python/bin/python3'
            if not (staging/'python-ready').is_file():
                archive=self.root/'python.tar.gz'
                self._download_python(archive)
                # data filter rejects escaping paths and unsafe links/devices.
                with tarfile.open(archive) as source:
                    source.extractall(staging,filter='data')
                (staging/'python-ready').write_text(PYTHON_SHA256)
            self._state('installing_runtime')
            self._run([str(python),'-I','-m','pip','install','--disable-pip-version-check',
                       '--no-input','--only-binary=:all:','--require-hashes','--index-url','https://pypi.org/simple',
                       '-r',str(ASSETS/'qwen-mac-requirements.txt')],staging)
            self._state('downloading_model')
            with tempfile.TemporaryDirectory(dir=self.root,prefix='result-') as result_dir:
                self._run([str(python),'-I',str(ASSETS/'qwen_worker.py'),'install',str(staging/'model'),
                    QWEN_MODEL_ID,QWEN_MODEL_REVISION,QWEN_RUNTIME_VERSION,str(Path(result_dir)/'result.json')],staging)
            model=staging/'model'
            if not (model/'config.json').is_file() or not list(model.glob('*.safetensors')):
                raise ComponentError('incomplete_model')
            files={str(p.relative_to(model)):p.stat().st_size for p in model.rglob('*')
                   if p.is_file() and '.cache' not in p.relative_to(model).parts}
            self._state('verifying_runtime')
            self._verify_runtime(staging)
            _atomic_json(staging/'component.json',{'version':QWEN_RUNTIME_VERSION,
                          'revision':QWEN_MODEL_REVISION,'files':files,'self_test_passed':True})
            if self._stop.is_set():
                raise ComponentError('interrupted')
            # Standalone Python is relocatable; use its binary directly, never
            # pip-generated console scripts which embed the staging prefix.
            if self.active.exists():
                # Invalid installation is application-owned, retained for diagnosis.
                previous=self.root/'previous'
                if previous.exists():
                    shutil.rmtree(previous)  # Prior inactive, application-owned copy.
                self.active.rename(previous)
            try:
                staging.rename(self.active)
            except OSError:
                previous=self.root/'previous'
                if not self.active.exists() and previous.exists():
                    previous.rename(self.active)
                raise
            self._state('ready')
        except Exception as error:
            phase=_read(self.root/'state.json').get('state')
            self._state('interrupted' if self._stop.is_set() else 'failed',detail=self._failure_detail(error,phase))
            # Only a class name, never downloaded content, tokens, or transcripts.
            (self.root/'failure.txt').write_text(type(error).__name__)
        finally:
            lock.close()

    def _verify_runtime(self,root):
        with tempfile.TemporaryDirectory(dir=self.root,prefix='selftest-') as temporary:
            output=Path(temporary)/'result.json'
            self._run([str(root/'python/bin/python3'),'-I',str(ASSETS/'qwen_worker.py'),
                'transcribe',str(root/'model'),QWEN_MODEL_ID,QWEN_MODEL_REVISION,QWEN_RUNTIME_VERSION,
                str(ASSETS/'qwen-selftest.wav'),str(output)],root,offline=True,quiet=True)
            value=_read(output)
            words=str(value.get('text','')).lower() if isinstance(value,dict) else ''
            if not isinstance(value,dict) or value.get('truncated') is not False or not all(word in words.split() for word in ('local','speech')):
                raise ComponentError('selftest_failed')

    @staticmethod
    def _failure_detail(error,phase):
        if isinstance(error,OSError) and error.errno==errno.ENOSPC:
            return '磁盘空间不足。请释放空间后继续安装。'
        if isinstance(error,PermissionError):return '无法写入安装目录。请检查应用数据目录的访问权限。'
        if isinstance(error,(urllib.error.URLError,TimeoutError)):
            return '下载连接失败。请检查网络后继续安装。'
        if str(error)=='download_checksum_mismatch':return '运行组件校验未通过，重试会重新下载。'
        if phase=='verifying_runtime':return '文件已下载，但本机识别自检未通过。请关闭占用较多内存的程序后重试；已有下载保留。'
        if phase=='downloading_model':return '模型下载未完成。请检查网络和磁盘空间后继续；已有可用文件会复用。'
        if phase=='installing_runtime':return '运行组件安装未完成。请检查网络和磁盘空间后重试。'
        return '安装未完成。请检查网络和磁盘空间后重试。'

    def _download_python(self,path):
        if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest()==PYTHON_SHA256:
            return
        temporary=path.with_suffix('.part')
        with urllib.request.urlopen(PYTHON_URL,timeout=30) as response, temporary.open('wb') as out:
            raw_total=response.headers.get('Content-Length','')
            total=int(raw_total) if raw_total.isdigit() else None
            received=0;updated=0.0
            while chunk:=response.read(1024*1024):
                if self._stop.is_set():
                    raise ComponentError('interrupted')
                out.write(chunk)
                received+=len(chunk)
                if time.monotonic()-updated>=.5:
                    self._state('downloading_runtime',progress={'completed_bytes':received,'total_bytes':total})
                    updated=time.monotonic()
        if hashlib.sha256(temporary.read_bytes()).hexdigest()!=PYTHON_SHA256:
            raise ComponentError('download_checksum_mismatch')
        temporary.replace(path)

    def _run(self,command,root,*,offline=False,quiet=False):
        deadline=time.monotonic()+3600
        with (Path(os.devnull) if quiet else self.root/'install.log').open('ab') as log:
            process=subprocess.Popen(command,stdout=log,stderr=log,env=_environment(root,offline=offline),start_new_session=True)
            self._process=process
            try:
                while True:
                    if self._stop.is_set() or time.monotonic()>deadline:
                        raise ComponentError('interrupted')
                    try:
                        code=process.wait(timeout=.5)
                        if code:
                            raise ComponentError('installation_command_failed')
                        return
                    except subprocess.TimeoutExpired:
                        continue
            finally:
                self._terminate(process)
                self._process=None

    @staticmethod
    def _terminate(process):
        if process.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid,signal.SIGTERM)
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid,signal.SIGKILL)
                process.wait()

    def close(self):
        self._stop.set()
        if self._process is not None:
            self._terminate(self._process)
        if self._thread:
            self._thread.join(timeout=35)


class ComponentQwenRuntime:
    def __init__(self,component):
        self.component=component

    def transcribe(self,audio_path):
        if not self.component.supported or not self.component.ready():
            raise QwenRuntimeUnavailable
        active=self.component.active
        try:
            with tempfile.TemporaryDirectory(prefix='transcription-',dir=self.component.root) as temp:
                output=Path(temp)/'result.json'
                self.component._run([str(active/'python/bin/python3'),'-I',str(ASSETS/'qwen_worker.py'),
                    'transcribe',str(active/'model'),QWEN_MODEL_ID,QWEN_MODEL_REVISION,QWEN_RUNTIME_VERSION,
                    str(audio_path),str(output)],active,offline=True,quiet=True)
                value=json.loads(output.read_text())
                if not isinstance(value,dict) or not isinstance(value.get('text'),str) or not isinstance(value.get('truncated'),bool):
                    raise ValueError
                return QwenRuntimeResult(value['text'],value.get('language'),value.get('finish_reason'),
                                         value['truncated'],value.get('chunks'))
        except (OSError,ValueError,ComponentError) as error:
            raise QwenRuntimeFailure from error
