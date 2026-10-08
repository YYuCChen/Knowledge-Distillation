"""Application-owned, exact uploaded file copies; never a new knowledge authority."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import stat
from contextlib import contextmanager

FILE_KINDS = frozenset({'markdown', 'pdf', 'epub'})


class SourceCopyError(ValueError):
    pass


@contextmanager
def _bound_directory(data_root, kind, key, label, *, create=False):
    # Validate data labels using the existing contract, then keep the lexical
    # root: resolve() would erase an ancestor symlink before verification.
    copy_path(data_root, kind, key, label)
    root = Path(data_root).absolute()
    if '..' in root.parts or not hasattr(os, 'O_NOFOLLOW') or not hasattr(os, 'getuid'):
        raise SourceCopyError('bound_copy_unsupported')
    target = root / 'source-files' / kind / key / label
    descriptors = []
    anchors = []
    try:
        fd = os.open(root.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(fd)
        path = Path(root.anchor)
        for component in (*root.parts[1:], 'source-files', kind, key):
            path /= component
            if create and (path == root / 'source-files' or path.parent == root / 'source-files'
                           or path.parent == root / 'source-files' / kind):
                try:
                    os.mkdir(component, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            descriptors.append(fd)
            info = os.fstat(fd)
            if (path == root or root in path.parents) and info.st_uid != os.getuid():
                raise SourceCopyError('bound_copy_owner_invalid')
            anchors.append((path, info.st_dev, info.st_ino, info.st_mode, info.st_uid))
        yield fd, target
        for path, device, inode, mode, uid in anchors:
            current = path.lstat()
            if (current.st_dev, current.st_ino, current.st_mode, current.st_uid) != (device, inode, mode, uid):
                raise SourceCopyError('bound_copy_path_changed')
    except OSError as error:
        raise SourceCopyError('bound_copy_unavailable') from error
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def read_bound_copy(data_root, kind, key, label, *, expected_bytes):
    """Read an owned regular single-link file without following any symlink."""
    if type(expected_bytes) is not int or expected_bytes < 0:
        raise SourceCopyError('bound_copy_length_invalid')
    with _bound_directory(data_root, kind, key, label) as (parent, target):
        before = os.stat(label, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_uid != os.getuid():
            raise SourceCopyError('bound_copy_identity_invalid')
        if before.st_size != expected_bytes:
            raise SourceCopyError('bound_copy_length_mismatch')
        descriptor = os.open(label, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            opened = os.fstat(descriptor)
            identity = lambda s: (s.st_dev, s.st_ino, s.st_mode, s.st_uid, s.st_nlink,
                                  s.st_size, s.st_mtime_ns, s.st_ctime_ns)
            if identity(opened) != identity(before):
                raise SourceCopyError('bound_copy_changed')
            chunks, count, eof = [], 0, False
            # Unbuffered reads have no hidden read-ahead. Observe EOF while
            # consuming at most the original length plus one growth sentinel.
            with os.fdopen(descriptor, 'rb', buffering=0, closefd=False) as stream:
                while count < expected_bytes + 1:
                    chunk = stream.read(expected_bytes + 1 - count)
                    if not chunk:
                        eof = True
                        break
                    chunks.append(chunk)
                    count += len(chunk)
            if count != expected_bytes or not eof:
                raise SourceCopyError('bound_copy_length_mismatch')
            content = b''.join(chunks)
            if (identity(os.fstat(descriptor)) != identity(opened)
                    or identity(os.stat(label, dir_fd=parent, follow_symlinks=False)) != identity(opened)
                    or identity(target.lstat()) != identity(opened)):
                raise SourceCopyError('bound_copy_changed')
        finally:
            os.close(descriptor)
        if hashlib.sha256(content).hexdigest() != key:
            raise SourceCopyError('bound_copy_hash_mismatch')
        return content


def retain_bound_copy(data_root, kind, key, label, content):
    """Use an anchored directory and unlink our temporary hardlink before readback."""
    if hashlib.sha256(content).hexdigest() != key:
        raise SourceCopyError('bound_copy_hash_mismatch')
    with _bound_directory(data_root, kind, key, label, create=True) as (parent, target):
        from uuid import uuid4
        name = '.upload-' + uuid4().hex
        descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=parent)
        try:
            with os.fdopen(descriptor, 'wb') as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
                os.fchmod(output.fileno(), 0o444)
            try:
                os.link(name, label, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
            except FileExistsError:
                pass
        finally:
            os.unlink(name, dir_fd=parent)
        os.fsync(parent)
    # No permissive two-link phase escapes into the public reader.
    actual = read_bound_copy(data_root, kind, key, label, expected_bytes=len(content))
    if actual != content:
        raise SourceCopyError('bound_copy_changed')
    return target


def copy_path(data_root: Path, kind: str, key: str, label: str) -> Path:
    if (kind not in FILE_KINDS or not re.fullmatch(r'[0-9a-f]{64}', key)
            or not label or Path(label).name != label or '\\' in label or label in {'.', '..'}):
        raise SourceCopyError('原文件副本的定位信息无效。')
    root = data_root.resolve() / 'source-files'
    target = root / kind / key / label
    # Stored ownership is exact; a symlink must not redirect reads or writes.
    for part in (root, root / kind, target.parent, target):
        if part.is_symlink():
            raise SourceCopyError('原文件副本位置被替换，不能安全打开。')
    return target


def read_copy(data_root: Path, kind: str, key: str, label: str) -> bytes:
    target = copy_path(data_root, kind, key, label)
    try:
        content = target.read_bytes()
    except OSError as error:
        raise SourceCopyError('原文件副本不可用，请重新上传相同文件补回。') from error
    if hashlib.sha256(content).hexdigest() != key:
        raise SourceCopyError('原文件副本已被修改，请先保留你的修改，再移走该副本并重新上传原文件。')
    return content


def retain_copy(data_root: Path, kind: str, key: str, label: str, content: bytes) -> Path:
    if hashlib.sha256(content).hexdigest() != key:
        raise SourceCopyError('文件内容与原提交不一致，未保存副本。')
    target = copy_path(data_root, kind, key, label)
    if target.exists():
        read_copy(data_root, kind, key, label)
        return target
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix='.upload-', dir=target.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, 'wb') as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
                if sys.platform != 'win32':
                    os.fchmod(output.fileno(), 0o444)
            try:
                os.link(temporary, target)
            except FileExistsError:
                pass
            # Existing files are never overwritten, even after a failed DB commit.
            read_copy(data_root, kind, key, label)
            if sys.platform != 'win32':
                directory = os.open(target.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)
    except OSError as error:
        raise SourceCopyError('无法保存原文件副本，本次未接收文件，请检查应用数据目录。') from error
    return target


def open_copy(data_root: Path, kind: str, key: str, label: str) -> None:
    read_copy(data_root, kind, key, label)
    target = copy_path(data_root, kind, key, label)
    try:
        _open(target)
    except (OSError, subprocess.SubprocessError) as error:
        raise SourceCopyError('系统未能打开原文件副本，请检查该文件格式的默认应用。') from error


def open_directory(data_root: Path) -> None:
    root = data_root.resolve() / 'source-files'
    try:
        if root.is_symlink():
            raise SourceCopyError('副本目录被替换，不能安全打开。')
        root.mkdir(parents=True, exist_ok=True)
        _open(root)
    except (OSError, subprocess.SubprocessError) as error:
        raise SourceCopyError('未能打开原文件副本目录，请检查应用数据目录。') from error


def _open(path: Path) -> None:
    if sys.platform == 'win32':
        os.startfile(str(path))
    else:
        subprocess.run(['/usr/bin/open', str(path)], check=True, capture_output=True, timeout=10)
