"""Application-owned, exact uploaded file copies; never a new knowledge authority."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import subprocess
import tempfile

FILE_KINDS = frozenset({'markdown', 'pdf', 'epub'})


class SourceCopyError(ValueError):
    pass


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
                os.fchmod(output.fileno(), 0o444)
            try:
                os.link(temporary, target)
            except FileExistsError:
                pass
            # Existing files are never overwritten, even after a failed DB commit.
            read_copy(data_root, kind, key, label)
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
        subprocess.run(['/usr/bin/open', str(target)], check=True, capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as error:
        raise SourceCopyError('系统未能打开原文件副本，请检查该文件格式的默认应用。') from error


def open_directory(data_root: Path) -> None:
    root = data_root.resolve() / 'source-files'
    try:
        if root.is_symlink():
            raise SourceCopyError('副本目录被替换，不能安全打开。')
        root.mkdir(parents=True, exist_ok=True)
        subprocess.run(['/usr/bin/open', str(root)], check=True, capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as error:
        raise SourceCopyError('未能打开原文件副本目录，请检查应用数据目录。') from error
