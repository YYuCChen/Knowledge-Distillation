"""Exact platform program identity, excluding timestamps and machine ownership."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import zipfile

from .updates import UpdateError


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()


def inventory(root, platform):
    root = Path(root)
    if platform == 'windows-x86_64':
        from .windows_delta import inventory as windows_inventory
        return windows_inventory(root)
    if platform != 'macos-arm64' or root.is_symlink() or not root.is_dir():
        raise UpdateError('程序目录或平台无效。')
    files = {}
    for directory, dirs, names in os.walk(root, followlinks=False):
        for name in sorted(dirs + names):
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                link = os.readlink(path)
                _safe_link(relative, link)
                try:
                    if not path.resolve().is_relative_to(root.resolve()):
                        raise UpdateError('程序链接解析后越界。')
                except RuntimeError as error:
                    raise UpdateError('程序链接包含循环。') from error
                files[relative] = {'kind': 'link', 'target': link}
            elif stat.S_ISDIR(mode):
                files[relative] = {'kind': 'directory', 'mode': stat.S_IMODE(mode)}
            elif stat.S_ISREG(mode):
                with path.open('rb') as source:
                    digest = hashlib.file_digest(source, 'sha256').hexdigest()
                files[relative] = {'kind': 'file', 'mode': stat.S_IMODE(mode),
                                   'size': path.stat().st_size, 'sha256': digest}
            else:
                raise UpdateError('程序目录包含不支持的文件类型。')
    return files


def identity(root, platform):
    return hashlib.sha256(_canonical(inventory(root, platform))).hexdigest()


def _safe_name(name):
    path = PurePosixPath(name)
    if (not name or path.is_absolute() or '..' in path.parts or '\\' in name
            or ':' in name or str(path) != name):
        raise UpdateError('程序基座路径无效。')
    return name


def _safe_link(name, target):
    if not isinstance(target, str) or '\\' in target or ':' in target or target.startswith('/'):
        raise UpdateError('程序基座链接无效。')
    destination = os.path.normpath(str(PurePosixPath(name).parent / target))
    if destination == '..' or destination.startswith('../'):
        raise UpdateError('程序基座链接越界。')


def extract_mac_base(archive, stage, *, expected_identity, maximum_bytes):
    """Installer base format uses paths relative to the .app, with links last."""
    stage = Path(stage)
    if stage.exists() or stage.is_symlink():
        raise UpdateError('程序暂存目录已存在。')
    with zipfile.ZipFile(archive) as package:
        entries = package.infolist()
        names, links = set(), {}
        if len(entries) > 100000 or sum(e.file_size for e in entries) > maximum_bytes:
            raise UpdateError('程序基座展开大小超限。')
        for entry in entries:
            name = _safe_name(entry.filename.rstrip('/'))
            if name.casefold() in names or entry.flag_bits & 1:
                raise UpdateError('程序基座含重复或加密条目。')
            names.add(name.casefold())
            mode = entry.external_attr >> 16
            if stat.S_ISLNK(mode):
                if entry.file_size > 4096:
                    raise UpdateError('程序基座链接过长。')
                target = package.read(entry).decode('utf-8')
                _safe_link(name, target)
                links[name] = target
            elif not (entry.is_dir() or stat.S_ISREG(mode)):
                raise UpdateError('程序基座文件类型无效。')
        for entry in entries:
            parts = PurePosixPath(entry.filename.rstrip('/')).parents
            if any(str(parent) in links for parent in parts):
                raise UpdateError('程序基座不能通过链接写入。')
        stage.mkdir()
        try:
            for entry in entries:
                name = entry.filename.rstrip('/')
                if name in links:
                    continue
                path = stage / name
                path.parent.mkdir(parents=True, exist_ok=True)
                mode = (entry.external_attr >> 16) & 0o777
                if entry.is_dir():
                    path.mkdir(exist_ok=True)
                else:
                    with package.open(entry) as source, path.open('xb') as target:
                        remaining = entry.file_size
                        while block := source.read(min(1024**2, remaining + 1)):
                            remaining -= len(block)
                            if remaining < 0:
                                raise UpdateError('程序基座大小不符。')
                            target.write(block)
                        if remaining:
                            raise UpdateError('程序基座未完整下载。')
                path.chmod(mode)
            for name, link in links.items():
                path = stage / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.symlink_to(link)
            if identity(stage, 'macos-arm64') != expected_identity:
                raise UpdateError('程序基座内容身份不符。')
        except BaseException:
            import shutil
            shutil.rmtree(stage)
            raise
    return stage
