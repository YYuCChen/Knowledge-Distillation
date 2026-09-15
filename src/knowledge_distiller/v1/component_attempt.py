"""Process-local authority for retiring exactly one accepted assembly attempt.

Persisted paths/UUIDs are diagnostics, never deletion authority. Windows retains
GENERIC_READ handles without FILE_SHARE_DELETE (READ_ATTRIBUTES alone does NOT
prevent rename). POSIX deletes relative to verified directory descriptors.
"""
import os
from pathlib import Path
import secrets
import stat
import threading

from .windows_platform import filesystem_path, is_link_or_reparse

_SESSION = secrets.token_hex(32)


def _identity(info):
    return info.st_dev, info.st_ino


class AttemptCapability:
    def __init__(self, root):
        self.root = root
        self.pid = os.getpid()
        self.session_nonce = _SESSION
        self.attempt_id = root.name
        self.candidate_identity = None
        self.candidate = None
        self.consumed = False
        self.lock = threading.Lock()
        self.handles = []
        self.identities = []
        self.deleted = 0
        self.seen = set()
        try:
            for path in [*reversed(root.parents), root]:
                if is_link_or_reparse(path):
                    raise ValueError('attempt_ancestor_link')
                handle = _win_open(path, delete=path == root) if os.name == 'nt' else os.open(
                    path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                self.handles.append(handle)
                info = path.lstat() if os.name == 'nt' else os.fstat(handle)
                self.identities.append((path, _identity(info)))
        except BaseException:
            self.close()
            raise

    def close(self):
        for handle in reversed(self.handles):
            if os.name == 'nt': _kernel().CloseHandle(handle)
            else: os.close(handle)
        self.handles.clear()

    def __del__(self):
        self.close()

    def verify(self):
        if not self.handles: raise ValueError('attempt_handles_unavailable')
        for path, expected in self.identities:
            if is_link_or_reparse(path) or _identity(path.lstat()) != expected:
                raise ValueError('attempt_location_changed')


def create_attempt(root, *, excluded):
    root = Path(os.path.abspath(root))
    resolved = root.resolve()
    for other in excluded:
        other = Path(other).resolve()
        if resolved.is_relative_to(other) or other.is_relative_to(resolved):
            raise ValueError('attempt_overlaps_protected_location')
    root.parent.mkdir(parents=True, exist_ok=True)
    root.mkdir()  # Atomic exclusive creation; never adopt existing directories.
    return AttemptCapability(root)


def bind_candidate(capability, candidate, candidate_identity):
    capability.verify()
    candidate = Path(candidate)
    if candidate.parent != capability.root or is_link_or_reparse(candidate):
        raise ValueError('candidate_outside_attempt')
    capability.candidate = candidate
    capability.candidate_identity = candidate_identity


def _kernel():
    import ctypes
    from ctypes import wintypes as w
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    k.CreateFileW.argtypes = [w.LPCWSTR,w.DWORD,w.DWORD,w.LPVOID,w.DWORD,w.DWORD,w.HANDLE]
    k.CreateFileW.restype = w.HANDLE
    k.CloseHandle.argtypes = [w.HANDLE]
    k.SetFileInformationByHandle.argtypes = [w.HANDLE,ctypes.c_int,w.LPVOID,w.DWORD]
    return k


def _win_open(path, *, delete):
    import ctypes
    from ctypes import wintypes as w
    # Generic read is required for sharing protection; lack of access is a
    # truthful pending/error, not a reason to retry with ineffective attributes.
    handle = _kernel().CreateFileW(str(filesystem_path(path)),0x80000000 | (0x10000 if delete else 0),
        3,None,3,0x02000000 | 0x00200000,None)
    if handle == w.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    return handle


def _win_delete(handle):
    import ctypes
    from ctypes import wintypes as w
    value = w.BOOL(1)
    if not _kernel().SetFileInformationByHandle(handle,4,ctypes.byref(value),ctypes.sizeof(value)):
        raise ctypes.WinError(ctypes.get_last_error())


def _count(cap, info):
    key = _identity(info)
    if stat.S_ISREG(info.st_mode) and key not in cap.seen:
        cap.seen.add(key)
        cap.deleted += info.st_size


def _posix_remove(cap, directory):
    for name in os.listdir(directory):
        cap.verify()
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            try:
                if _identity(os.fstat(child)) != _identity(info): raise ValueError('attempt_child_changed')
                _posix_remove(cap, child)
                cap.verify()
                if _identity(os.stat(name,dir_fd=directory,follow_symlinks=False)) != _identity(info):
                    raise ValueError('attempt_child_changed')
                os.rmdir(name,dir_fd=directory)
            finally: os.close(child)
        else:
            # unlink removes legal framework symlinks themselves, not targets.
            os.unlink(name,dir_fd=directory)
            _count(cap,info)


def _windows_preflight(path):
    for child in filesystem_path(path).iterdir():
        if is_link_or_reparse(child): raise ValueError('attempt_internal_reparse')
        if child.is_dir(): _windows_preflight(child)


def _windows_remove(cap, path):
    for child in filesystem_path(path).iterdir():
        cap.verify()
        handle = _win_open(child,delete=True)
        try:
            # The no-delete-sharing handle keeps this entry at its path while
            # checking its native file ID and performing handle-based deletion.
            if is_link_or_reparse(child): raise ValueError('attempt_internal_reparse')
            info = child.lstat()
            if stat.S_ISDIR(info.st_mode): _windows_remove(cap,child)
            cap.verify()
            _win_delete(handle)
            _count(cap,info)
        finally: _kernel().CloseHandle(handle)


def cleanup_attempt(capability, outcome):
    cap = capability
    if not isinstance(cap,AttemptCapability): return {'status':'pending','reason':'no_process_capability'}
    with cap.lock:
        if cap.pid != os.getpid() or cap.session_nonce != _SESSION:
            return {'status':'pending','reason':'different_process'}
        if cap.consumed: return {'status':'already_clean','deleted_logical_bytes':cap.deleted}
        if (outcome.get('accepted') is not True or not cap.candidate_identity
                or cap.candidate_identity != outcome.get('target_identity')
                or outcome.get('activation',{}).get('status') != 'ready'):
            return {'status':'pending','reason':'acceptance_or_activation_unproven'}
        try:
            cap.verify()
            if os.name == 'nt':
                _windows_preflight(cap.root)
                _windows_remove(cap,cap.root)
                cap.verify()
                _win_delete(cap.handles[-1])
            else:
                _posix_remove(cap,cap.handles[-1])
                cap.verify()
                os.rmdir(cap.root.name,dir_fd=cap.handles[-2])
            cap.consumed = True
            cap.close()
            return {'status':'clean','deleted_logical_bytes':cap.deleted,
                    'measurement':'logical bytes, hardlinks counted once; not volume free-space growth'}
        except Exception as error:
            return {'status':'pending','reason':type(error).__name__ + ': ' + str(error),
                    'deleted_logical_bytes':cap.deleted}
