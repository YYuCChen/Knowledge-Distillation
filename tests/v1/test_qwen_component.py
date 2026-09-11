import io
import json
from pathlib import Path
import sys
import tarfile
import threading

import pytest

from knowledge_distiller.primary import (QWEN_MODEL_ID, QwenPrimaryAdapter, QwenRuntimeUnavailable,
                                        PrimaryFailure, StandardAudio)
from knowledge_distiller.v1 import qwen_component as module
from knowledge_distiller.v1.settings import SettingsService, SettingsError
from knowledge_distiller.v1.store import Store


@pytest.fixture
def component(tmp_path,monkeypatch):
    # This fixture exercises the Mac tar installer on every host; Windows has
    # its own wheel/embedded-runtime installer tests.
    monkeypatch.setattr(module.QwenComponent,'windows',property(lambda self:False))
    monkeypatch.setattr(module.QwenComponent,'supported',property(lambda self:True))
    result=module.QwenComponent(tmp_path/'qwen')
    def download(path):
        with tarfile.open(path,'w:gz') as archive:
            content=b'python-fixture'
            info=tarfile.TarInfo('python/bin/python3');info.size=len(content);info.mode=0o755
            archive.addfile(info,io.BytesIO(content))
    def run(command,root,**kwargs):
        if 'pip' in command:
            assert '--require-hashes' in command and '--only-binary=:all:' in command
        elif 'transcribe' in command:
            Path(command[-1]).write_text(json.dumps({'text':'This is a local speech recognition test.','truncated':False}))
        else:
            model=root/'model';model.mkdir(exist_ok=True)
            (model/'config.json').write_text('{}')
            (model/'model.safetensors').write_bytes(b'fixture-weight')
    monkeypatch.setattr(result,'_download_python',download)
    monkeypatch.setattr(result,'_run',run)
    return result


def finish(component):
    component._thread.join(5)
    assert not component._thread.is_alive()


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows reader sharing semantics')
def test_atomic_state_write_waits_for_windows_status_reader(tmp_path):
    import ctypes
    from ctypes import wintypes
    path = tmp_path/'state.json'
    module._atomic_json(path, {'state':'downloading_runtime'})
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    # Exact native condition: a reader permits READ/WRITE but not DELETE.
    handle = kernel.CreateFileW(str(path), 0x80000000, 3, None, 3, 0x80, None)
    assert handle != ctypes.c_void_p(-1).value
    errors, done = [], threading.Event()
    def update():
        try:
            module._atomic_json(path, {'state':'ready'})
        except Exception as error:
            errors.append(error)
        finally:
            done.set()
    writer = threading.Thread(target=update)
    writer.start()
    try:
        assert not done.wait(.05)
        assert module._read(path) == {'state':'downloading_runtime'}
    finally:
        kernel.CloseHandle(handle)
    writer.join(3)
    assert done.is_set() and not errors
    assert module._read(path) == {'state':'ready'}
    assert not path.with_suffix('.tmp').exists()


def test_install_is_explicit_complete_and_does_not_activate(component,tmp_path):
    store=Store(tmp_path/'store.sqlite3');store.initialize()
    store.set_settings({'asr_model':'volc.seedasr.auc','asr_state':'configured'})
    settings=SettingsService(store,qwen_component=component)
    assert component.status()['state']=='not_installed' and not component.root.exists()
    with pytest.raises(SettingsError): settings.activate_asr()
    component.start();finish(component)
    assert component.status()['ready']
    assert store.setting('asr_model')=='volc.seedasr.auc'
    before=(component.active/'component.json').read_bytes()
    component.start()
    assert (component.active/'component.json').read_bytes()==before
    settings.activate_asr()
    assert store.setting('asr_model')==QWEN_MODEL_ID


def test_concurrent_click_and_restart_observe_same_installation(component,monkeypatch):
    entered=threading.Event();release=threading.Event();original=component._run
    def blocked(command,root,**kwargs):
        entered.set();release.wait(5);original(command,root,**kwargs)
    monkeypatch.setattr(component,'_run',blocked)
    component.start();assert entered.wait(5)
    other=module.QwenComponent(component.root)
    assert other.status()['busy'];other.start()
    assert other._thread is None
    release.set();finish(component)
    assert other.ready()


@pytest.mark.parametrize('final_state', ['failed', 'ready'])
def test_status_observes_installation_finishing_during_lock_check(component,monkeypatch,final_state):
    component.root.mkdir(parents=True)
    module._atomic_json(component.root/'state.json', {'state':'downloading_runtime'})
    ready = [False]
    monkeypatch.setattr(component, 'ready', lambda: ready[0])
    def finishes_before_lock_check():
        module._atomic_json(component.root/'state.json', {'state':final_state})
        ready[0] = final_state == 'ready'
        return False
    monkeypatch.setattr(component, '_locked', finishes_before_lock_check)
    status = component.status()
    assert status['state'] == final_state
    assert status['label'] == module.LABELS[final_state]
    assert not status['busy']
    assert status['ready'] == (final_state == 'ready')


def test_retry_is_not_lost_while_terminal_installer_releases_lock(component,monkeypatch):
    from knowledge_distiller.v1 import file_lock
    component.root.mkdir(parents=True)
    module._atomic_json(component.root/'state.json', {'state':'failed'})
    original = file_lock.acquire
    attempts = []
    def finishing(path):
        attempts.append(path)
        if len(attempts) == 1:
            raise BlockingIOError('previous installer finishing')
        return original(path)
    monkeypatch.setattr(file_lock, 'acquire', finishing)
    component.start()
    finish(component)
    assert len(attempts) == 2
    assert component.ready()


def test_failure_retry_reuses_runtime_and_recovers_after_restart(component,monkeypatch):
    original=component._run
    monkeypatch.setattr(component,'_run',lambda *args,**kwargs: (_ for _ in ()).throw(OSError('network')))
    component.start();finish(component)
    assert component.status()['state']=='failed' and not component.ready()
    assert (component.root/'installing/python-ready').exists()
    monkeypatch.setattr(component,'_download_python',lambda path:pytest.fail('Runtime should be reused'))
    monkeypatch.setattr(component,'_run',original)
    module._atomic_json(component.root/'state.json',{'state':'downloading_model'})
    assert module.QwenComponent(component.root).status()['state']=='interrupted'
    component.start();finish(component)
    assert component.ready()


def test_repeated_corrupt_installation_can_be_repaired(component):
    for _ in range(3):
        component.start();finish(component)
        assert component.ready()
        (component.active/'model/config.json').unlink()
        assert component.status()['state']=='failed'
    component.start();finish(component)
    assert component.ready()


def test_archive_cannot_escape_install_root(component,monkeypatch,tmp_path):
    def malicious(path):
        with tarfile.open(path,'w:gz') as archive:
            info=tarfile.TarInfo('../../escaped');info.size=1
            archive.addfile(info,io.BytesIO(b'x'))
    monkeypatch.setattr(component,'_download_python',malicious)
    component.start();finish(component)
    assert component.status()['state']=='failed' and not (tmp_path/'escaped').exists()


def test_download_checksum_is_required_before_extract(tmp_path,monkeypatch):
    component=module.QwenComponent(tmp_path)
    response=io.BytesIO(b'wrong artifact');response.headers={}
    monkeypatch.setattr(module.urllib.request,'urlopen',lambda *a,**k:response)
    with pytest.raises(module.ComponentError,match='checksum'):
        component._download_python(tmp_path/'python.tar.gz')
    assert not (tmp_path/'python.tar.gz').exists()


def test_component_transcription_uses_offline_isolated_protocol(component,tmp_path,monkeypatch):
    with pytest.raises(QwenRuntimeUnavailable):
        module.ComponentQwenRuntime(component).transcribe(tmp_path/'audio.wav')
    component.start();finish(component)
    def transcribe(command,root,**options):
        assert options=={'offline':True,'quiet':True} and command[1]=='-I'
        assert root==component.active and command[3]=='transcribe'
        Path(command[-1]).write_text(json.dumps(dict(text='source',language='English',finish_reason='eos',truncated=False,
            chunks=[dict(start=0,end=1,text='source',language='English',finish_reason='eos',truncated=False)])))
    monkeypatch.setattr(component,'_run',transcribe)
    adapter=QwenPrimaryAdapter(module.ComponentQwenRuntime(component))
    audio=type('Audio',(),{'path':tmp_path/'audio.wav'})()
    result=adapter.recognize(audio)
    assert result.recovery.text=='source' and result.recovery.chunks[0].end_seconds==1
    assert not list(component.root.glob('transcription-*'))


def test_invalid_transcription_payload_is_failure(component,tmp_path,monkeypatch):
    component.start();finish(component)
    monkeypatch.setattr(component,'_run',lambda command,*a,**k:Path(command[-1]).write_text('{"text":"partial"}'))
    result=QwenPrimaryAdapter(module.ComponentQwenRuntime(component)).recognize(type('Audio',(),{'path':tmp_path/'x'})())
    assert result.failure is PrimaryFailure.RUNTIME_FAILED


def test_subprocesses_are_stopped_and_loader_environment_is_clean(tmp_path,monkeypatch):
    component=module.QwenComponent(tmp_path)
    monkeypatch.setenv('PYTHONPATH','/application/site-packages')
    monkeypatch.setenv('DYLD_LIBRARY_PATH','/application/native')
    monkeypatch.setenv('__PYVENV_LAUNCHER__','/application/python')
    monkeypatch.setenv('HF_TOKEN','private')
    env=module._environment(tmp_path,offline=True)
    assert not {'PYTHONPATH','DYLD_LIBRARY_PATH','__PYVENV_LAUNCHER__','HF_TOKEN'} & env.keys()
    assert env['HF_HUB_OFFLINE']=='1' and env['HF_HOME']==str(tmp_path/'cache')
    errors=[]
    def run():
        try: component._run([sys.executable,'-c','import time; time.sleep(60)'],tmp_path)
        except module.ComponentError:errors.append(True)
    thread=threading.Thread(target=run);thread.start()
    import time
    deadline=time.monotonic()+5
    while component._process is None and time.monotonic()<deadline:time.sleep(.01)
    process=component._process;assert process is not None
    component.close();thread.join(5)
    assert not thread.is_alive() and process.poll() is not None and errors


def test_base_release_excludes_qwen_but_keeps_document_runtime():
    import ast,tomllib
    root=Path(__file__).resolve().parents[2]
    project=tomllib.loads((root/'pyproject.toml').read_text())['project']
    assert not any('mlx' in dependency for dependency in project['dependencies'])
    spec=ast.parse((root/'packaging/KnowledgeDistiller.spec').read_text())
    analysis=next(n for n in ast.walk(spec) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id=='Analysis')
    excluded=ast.literal_eval(next(k.value for k in analysis.keywords if k.arg=='excludes'))
    assert {'mlx','mlx_qwen3_asr'} <= set(excluded)
    assert not {'docling','torch','transformers','rapidocr','onnxruntime'} & set(excluded)


@pytest.mark.parametrize("initial_model", [None, "volc.seedasr.auc"])
def test_install_then_explicit_activation_in_browser(component,tmp_path,monkeypatch,initial_model):
    from playwright.sync_api import sync_playwright,expect
    from werkzeug.serving import make_server
    from knowledge_distiller.v1.web import create_app
    store=Store(tmp_path/'web.sqlite3');store.initialize()
    if initial_model:
        store.set_settings({'asr_model':initial_model,'asr_state':'configured'})
    settings=SettingsService(store,qwen_component=component)
    original=component._run;fail=[True]
    def fail_once(*args,**kwargs):
        if fail[0]:fail[0]=False;raise OSError('interrupted network')
        return original(*args,**kwargs)
    monkeypatch.setattr(component,'_run',fail_once)
    server=make_server('127.0.0.1',0,create_app(store,object(),settings),threaded=True)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with sync_playwright() as pw:
            if not Path(pw.chromium.executable_path).exists():pytest.skip('Browser not installed')
            browser=pw.chromium.launch();page=browser.new_page(viewport={'width':1440,'height':1024})
            page.goto(f'http://127.0.0.1:{server.server_port}/settings?open=models')
            page.locator('.asr-setting > summary').click()
            page.locator('#asr-provider').select_option('qwen')
            expect(page.locator('#asr-save')).to_be_disabled()
            page.locator('#qwen-install').click()
            expect(page.locator('#qwen-component-status')).to_have_text('安装失败，可重试')
            assert store.setting('asr_model') == initial_model
            page.locator('#qwen-install').click()
            expect(page.locator('#qwen-component-status')).to_have_text('已安装，可以启用')
            expect(page.locator('#asr-save')).to_be_enabled()
            expect(page.locator('#qwen-install')).to_be_hidden()
            assert store.setting('asr_model') == initial_model
            page.locator('#asr-save').click()
            assert store.setting('asr_model')==QWEN_MODEL_ID
            page.reload()
            expect(page.locator('#qwen-component-status')).to_have_text('已安装，可以启用')
            for width in (1440,946,390):
                page.set_viewport_size({'width':width,'height':1024})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.locator('#asr-provider').select_option('doubao')
            expect(page.locator('#doubao-credentials')).to_be_visible()
            for width in (1440, 946, 390):
                page.set_viewport_size({'width': width, 'height': 1024})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            expect(page.locator('#qwen-component')).to_be_hidden()
            browser.close()
    finally:
        server.shutdown();server.server_close();thread.join(5);component.close()


def test_runtime_progress_uses_actual_received_bytes_and_retains_checksum(tmp_path,monkeypatch):
    content=b'a'*1500000
    response=io.BytesIO(content);response.headers={'Content-Length':str(len(content))}
    monkeypatch.setattr(module.urllib.request,'urlopen',lambda *a,**k:response)
    monkeypatch.setattr(module,'PYTHON_SHA256',module.hashlib.sha256(content).hexdigest())
    component=module.QwenComponent(tmp_path)
    component._download_python(tmp_path/'python.tar.gz')
    value=json.loads((tmp_path/'state.json').read_text())['progress']
    assert 0<value['completed_bytes']<=len(content)
    assert value['total_bytes']==len(content)
    assert (tmp_path/'python.tar.gz').read_bytes()==content


def test_model_progress_counts_complete_and_partial_bytes_not_cache_metadata(component):
    model=component.root/'installing/model';model.mkdir(parents=True)
    (model/'config.json').write_bytes(b'12345')
    cache=model/'.cache/huggingface/download';cache.mkdir(parents=True)
    (cache/'weights.incomplete').write_bytes(b'1234567890')
    (cache/'config.metadata').write_bytes(b'x'*100)
    module._atomic_json(component.root/'model-download.json',{'total_bytes':100})
    assert component._model_progress()=={'completed_bytes':15,'total_bytes':100}
    (component.root/'model-download.json').write_text('broken')
    assert component._model_progress()=={}


def test_failed_install_explains_disk_problem_without_raw_exception(component,monkeypatch):
    import errno
    monkeypatch.setattr(component,'_run',lambda *a,**k:(_ for _ in ()).throw(OSError(errno.ENOSPC,'private detail')))
    component.start();finish(component)
    status=component.status()
    assert status['state']=='failed' and '磁盘空间不足' in status['detail']
    assert 'private detail' not in json.dumps(status)
    assert status['can_install']


def test_selftest_failure_does_not_offer_activation_and_retry_reuses_download(component,monkeypatch):
    original=component._run
    def reject(command,*args,**kwargs):
        if 'transcribe' in command:
            Path(command[-1]).write_text(json.dumps({'text':'','truncated':False}))
        else:original(command,*args,**kwargs)
    monkeypatch.setattr(component,'_run',reject)
    component.start();finish(component)
    assert not component.ready()
    assert '自检未通过' in component.status()['detail']
    monkeypatch.setattr(component,'_download_python',lambda *a:pytest.fail('must reuse download'))
    monkeypatch.setattr(component,'_run',original)
    component.start();finish(component)
    assert component.status()['self_test_passed']
