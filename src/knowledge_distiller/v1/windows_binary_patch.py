"""Pinned native HDiffPatch adapter; all inputs are signed-plan-owned files.

The decoder runs out of process with a deadline and bounded output. Windows
assigns a memory-limited Job before resuming the suspended native process.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

from .updates import UpdateError

ALGORITHM = 'hdiffpatch-5.1.3-sdiff-zlib'
MAX_FILE = 2 * 1024**3
MAX_MEMORY = 512 * 1024**2
MAX_SECONDS = 900
MIN_PATCH_FILE = 1024**2
MIN_SAVINGS = 64 * 1024
MAX_PATCH_RATIO = .9
HASHES = {
    'hdiffz.exe':'f5ed7ac622a2daf4a31cc21ffa8ea1717f92323e79ef5ae695c5c9238a282f52',
    'hpatchz.exe':'9703c694b5955c576d9f0e26e98b60941f0bbb53b382b1f1988d75d461e580cb',
    'hdiffz':'f60f24e2fd3f77edac07dd2d5210dc4238cf97f5489db49dfe3d0519c17c6ba3',
    'hpatchz':'ace37088b29bd09074d4c76a066be53ec1ac67b2297d70dbf2f8432a58fe4ecb',
}


def tool_path(name, directory=None):
    filename = name + ('.exe' if sys.platform == 'win32' else '')
    root = Path(directory) if directory is not None else Path(getattr(sys, '_MEIPASS', Path(__file__).parent)) / 'tools'
    path = root / filename
    if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != HASHES[filename]:
        raise UpdateError('差分工具缺失或校验失败，请修复安装器。')
    return path


def _run(command, *, output=None, max_output=MAX_FILE, timeout=MAX_SECONDS):
    import psutil
    started = time.monotonic()
    peak = 0
    job = None
    with tempfile.TemporaryFile() as log:
        flags = 0x00000004 | subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
        process = subprocess.Popen([str(a) for a in command], stdout=log, stderr=subprocess.STDOUT,
                                   creationflags=flags, close_fds=True)
        try:
            if sys.platform == 'win32':
                import win32job
                job = win32job.CreateJobObject(None, '')
                limits = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
                limits['BasicLimitInformation']['LimitFlags'] = (
                    win32job.JOB_OBJECT_LIMIT_PROCESS_MEMORY | win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)
                limits['ProcessMemoryLimit'] = MAX_MEMORY
                win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, limits)
                win32job.AssignProcessToJobObject(job, int(process._handle))
                psutil.Process(process.pid).resume()
            while process.poll() is None:
                try:
                    peak = max(peak, psutil.Process(process.pid).memory_info().rss)
                except psutil.NoSuchProcess:
                    pass
                if (time.monotonic()-started > timeout or peak > MAX_MEMORY
                        or (output is not None and output.exists() and output.stat().st_size > max_output)):
                    raise UpdateError('差分重建超过资源限制，当前应用未改变。')
                time.sleep(.02)
            if output is not None and output.exists() and output.stat().st_size > max_output:
                raise UpdateError('差分重建输出超限。')
            log.seek(0)
            message = log.read(64*1024).decode('utf-8', errors='replace')
            if process.returncode:
                raise UpdateError('差分工具执行失败，当前应用未改变。')
            return {'seconds':time.monotonic()-started, 'peak_rss':peak, 'message':message}
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            if job is not None:
                job.Close()


def create_patch(old, target, patch, *, directory=None):
    if max(old.stat().st_size, target.stat().st_size) > MAX_FILE:
        raise UpdateError('文件超出差分生成上限。')
    return _run([tool_path('hdiffz',directory), '-s-64', '-SD-256k', '-p-1', '-c-zlib-6',
                 old, target, patch], output=patch)


def apply_patch(old, patch, output, *, old_size, target_size, directory=None):
    if (not 0 <= old_size <= MAX_FILE or not 0 <= target_size <= MAX_FILE
            or patch.stat().st_size > MAX_FILE or old.stat().st_size != old_size
            or output.exists() or old.resolve() == output.resolve()):
        raise UpdateError('差分输入、目标或资源限制不匹配。')
    decoder = tool_path('hpatchz',directory)
    info = _run([decoder,'-info',patch], timeout=30)['message']
    def field(name):
        found = re.findall(r'\b'+name+r'\s*:\s*(\d+)', info)
        if len(found) != 1:
            raise UpdateError('差分头无效。')
        return int(found[0])
    if (field('oldDataSize') != old_size or field('newDataSize') != target_size
            or field('stepMemSize') > 256*1024
            or not re.search(r'diffDataType:\s*SHDiff\s', info)
            or 'compressType: "zlib"' not in info):
        raise UpdateError('差分头与已签名目标不匹配。')
    try:
        return _run([decoder,'-s-8m','-p-1',old,patch,output], output=output,max_output=target_size)
    except BaseException:
        output.unlink(missing_ok=True)
        raise
