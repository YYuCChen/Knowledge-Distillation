"""Offline legacy metadata qualification, using shipped upstream identities."""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import threading

from .windows_platform import is_link_or_reparse


class QwenLegacyMigration:
    def _legacy_manifest(self):
        from .qwen_component import _read
        value=_read(self.active/'component.json')
        if not isinstance(value,dict) or not value or ('python' in value and 'self_test_passed' in value):
            return None
        return value

    def _dependencies_match(self):
        from .qwen_component import ASSETS,_read,_environment
        if self.windows:
            expected={w['name']:w['version'] for w in _read(ASSETS/'qwen-windows-lock.json')['wheels']}
        else:
            expected=dict(re.findall(r'^([A-Za-z0-9_.-]+)==([^\s\\]+)',
                (ASSETS/'qwen-mac-requirements.txt').read_text(),re.M))
        code="import json,importlib.metadata as m;print(json.dumps({d.metadata['Name']:d.version for d in m.distributions()}))"
        from .subprocess_environment import external_process
        with external_process():
            result=subprocess.run([str(self.python_path(self.active)),'-I','-c',code],
                env=_environment(self.active,offline=True),capture_output=True,text=True,encoding='utf-8',
                timeout=30,check=True,**({'creationflags':subprocess.CREATE_NO_WINDOW} if self.windows else {}))
        normalize=lambda name:re.sub(r'[-_.]+','-',name).lower()
        actual={normalize(k):v for k,v in json.loads(result.stdout).items()}
        return bool(expected) and all(actual.get(normalize(k))==v for k,v in expected.items())

    def _verify_legacy_model(self,manifest):
        from .qwen_component import ASSETS,_read,ComponentError
        expected=_read(ASSETS/'qwen-model-manifests.json')['windows' if self.windows else 'mac']
        if manifest.get('revision')!=expected['revision'] or not isinstance(manifest.get('files'),dict):
            raise ComponentError('model_identity_mismatch')
        files=expected['files']
        if manifest['files']!={name:record['size'] for name,record in files.items()}:
            raise ComponentError('model_identity_mismatch')
        for root in (self.active,self.active/'python',self.active/'model'):
            if is_link_or_reparse(root):raise ComponentError('component_link')
        verified={}
        for name,record in files.items():
            if self._stop.is_set():raise ComponentError('interrupted')
            path=self.active/'model'/name
            if is_link_or_reparse(path) or path.stat().st_size!=record['size']:
                raise ComponentError('model_identity_mismatch')
            h=hashlib.sha256() if 'sha256' in record else hashlib.sha1()
            if 'git_blob_sha1' in record:h.update(f"blob {record['size']}\0".encode())
            with path.open('rb') as stream:
                for chunk in iter(lambda:stream.read(1024**2),b''):
                    if self._stop.is_set():raise ComponentError('interrupted')
                    h.update(chunk)
            value=h.hexdigest()
            if value!=record.get('sha256',record.get('git_blob_sha1')):
                raise ComponentError('model_identity_mismatch')
            verified[name]=record
        return verified

    def _migrate_existing(self):
        from .qwen_component import _read,_atomic_json,ComponentError
        manifest=self._legacy_manifest()
        if manifest is None:return False
        original_bytes=(self.active/'component.json').read_bytes()
        version,revision=self.identity()
        if manifest.get('version')!=version or manifest.get('revision')!=revision:
            return False
        self._state('validating_existing')
        probe=self._probe_python(self.active,force=True)
        if not self._dependencies_match():return False
        verified=self._verify_legacy_model(manifest)
        # The install lock excludes product transcriptions and concurrent
        # component installers; no active component is replaced on this path.
        self._verify_runtime(self.active)
        if self._stop.is_set():raise ComponentError('interrupted')
        if ((self.active/'component.json').read_bytes()!=original_bytes
                or self._probe_python(self.active,force=True)!=probe
                or not self._dependencies_match()
                or self._verify_legacy_model(manifest)!=verified):
            raise ComponentError('component_changed_during_validation')
        upgraded={**manifest,'python':probe,'self_test_passed':True,'verified_model_files':verified,
                  'migration':'offline-legacy-v1'}
        retained=self.root/'legacy-manifests'
        retained.mkdir(exist_ok=True)
        original=retained/(hashlib.sha256(original_bytes).hexdigest()+'.json')
        if not original.exists():
            with original.open('xb') as output:output.write(original_bytes)
        if original.read_bytes()!=original_bytes:raise ComponentError('legacy_retention_failed')
        _atomic_json(self.active/'component.json',upgraded)
        if _read(self.active/'component.json')!=upgraded:
            raise ComponentError('component_commit_readback_failed')
        self._state('ready')
        return True

    def begin_legacy_validation(self):
        """Schedule only existing metadata checks; never download on startup."""
        from .qwen_component import _read
        if not self.supported or self._legacy_manifest() is None:return
        saved=_read(self.root/'state.json')
        if saved.get('state') in ('failed','needs_upgrade'):return
        from .file_lock import acquire
        try:lock=acquire(self.root/'install.lock')
        except BlockingIOError:return
        self._stop.clear()
        self._state('validating_existing')
        self._thread=threading.Thread(target=self._validate_legacy_background,args=(lock,),
                                      daemon=True,name='qwen-legacy-validation')
        self._thread.start()

    def _validate_legacy_background(self,lock):
        from .qwen_component import ComponentError
        try:
            if not self._migrate_existing():self._state('needs_upgrade')
        except (OSError,ValueError,KeyError,ComponentError,subprocess.SubprocessError) as error:
            state='interrupted' if self._stop.is_set() else 'failed'
            if str(error) in ('python_version_mismatch','model_identity_mismatch'):
                state='needs_upgrade'
            self._state(state,detail=self._failure_detail(error,'verifying_runtime'))
        finally:lock.close()
