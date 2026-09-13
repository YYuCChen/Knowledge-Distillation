"""Signed Windows update payloads: complete file manifest plus changed files only."""
from __future__ import annotations
from .windows_platform import is_link_or_reparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import zipfile

from .updates import UpdateError

MANIFEST = 'windows-update.json'


def safe_name(name):
    parts = PurePosixPath(name).parts
    if not parts or name != '/'.join(parts) or any(p in ('.', '..') or ':' in p or '\\' in p or p.endswith((' ', '.')) for p in parts):
        raise UpdateError('更新文件路径无效。')
    if name.startswith('/') or any(p.split('.')[0].upper() in {'CON','PRN','AUX','NUL',*[f'COM{i}' for i in range(1,10)],*[f'LPT{i}' for i in range(1,10)]} for p in parts):
        raise UpdateError('更新文件路径无效。')
    return name


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def inventory(root):
    root = Path(root)
    if is_link_or_reparse(root):
        raise UpdateError('应用根目录含链接，不能进行差量更新。')
    result = {}
    for path in sorted(root.rglob('*')):
        if is_link_or_reparse(path):
            raise UpdateError('应用目录含链接，不能进行差量更新。')
        if path.is_file():
            name = safe_name(path.relative_to(root).as_posix())
            result[name] = {'size': path.stat().st_size, 'sha256': digest(path)}
    return result


def build_payload(target, output, base=None, *, format_version=1, tools_dir=None):
    if format_version == 2:
        from .windows_delta_v2 import build_payload as build_v2
        return build_v2(target, output, base, tools_dir=tools_dir)
    if format_version != 1:
        raise UpdateError('未知更新格式。')
    target, output = Path(target), Path(output)
    files = inventory(target)
    old = inventory(base) if base else {}
    version = json.loads((target/'_internal/windows-version.json').read_text(encoding='utf-8'))['version']
    previous = json.loads((Path(base)/'_internal/windows-version.json').read_text(encoding='utf-8'))['version'] if base else None
    changed = [name for name in files if files[name] != old.get(name)]
    manifest = {'format':1, 'version':version, 'base_version':previous, 'base':old, 'files':files, 'changed':changed}
    with zipfile.ZipFile(output, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        archive.writestr(MANIFEST, json.dumps(manifest, separators=(',', ':')))
        for name in changed:
            archive.write(target/name, 'files/'+name)
    return {'version':version, 'base_version':previous, 'changed_files':len(changed), 'total_files':len(files), 'size':output.stat().st_size}


def stage_payload(archive_path, installed, stage, *, version, current, tools_dir=None):
    """Verify source baseline and every result byte before touching the running app."""
    installed, stage = Path(installed), Path(stage)
    if stage.exists():
        raise UpdateError('上次更新暂存目录仍存在，请处理后重试。')
    with zipfile.ZipFile(archive_path) as archive:
        if MANIFEST not in archive.namelist():
            return stage_full(archive,stage,version)
        entry = archive.getinfo(MANIFEST)
        if entry.file_size > 32*1024*1024:
            raise UpdateError('更新文件清单过大。')
        manifest = json.loads(archive.read(entry))
        if manifest.get('format') == 2:
            from .windows_delta_v2 import stage_payload as stage_v2
            return stage_v2(archive_path,installed,stage,version=version,current=current,tools_dir=tools_dir)
        files, old, changed = manifest['files'], manifest['base'], manifest['changed']
        if manifest.get('format') != 1 or manifest['version'] != version or manifest['base_version'] not in (None,current):
            raise UpdateError('差量更新基线不匹配，当前版本已保留。')
        if not isinstance(files, dict) or not files or len(files)>100000 or not isinstance(old,dict) or not isinstance(changed,list):
            raise UpdateError('更新文件清单无效。')
        for mapping in (files, old):
            folded = set()
            for name, record in mapping.items():
                safe_name(name)
                if name.casefold() in folded or not isinstance(record,dict) or type(record.get('size')) is not int or not 0<=record['size']<=20*1024**3 or not isinstance(record.get('sha256'),str) or len(record['sha256'])!=64:
                    raise UpdateError('更新文件清单无效。')
                folded.add(name.casefold())
        if len(set(changed)) != len(changed) or set(changed) != {n for n in files if files[n] != old.get(n)}:
            raise UpdateError('更新差量文件清单不一致。')
        expected = {MANIFEST, *('files/'+safe_name(n) for n in changed)}
        if len(archive.infolist()) != len(expected) or set(archive.namelist()) != expected:
            raise UpdateError('更新归档含有未声明文件。')
        for item in archive.infolist():
            if ((item.external_attr >> 16) & 0o170000) == 0o120000:
                raise UpdateError('更新归档含有链接。')
        if manifest['base_version']:
            if inventory(installed) != old:
                raise UpdateError('当前应用文件与差量基线不一致，请重新检查并选择完整更新。')
        elif old or set(changed)!=set(files):
            raise UpdateError('完整更新清单无效。')
        stage.mkdir()
        try:
            for name, record in files.items():
                dest=stage/name
                dest.parent.mkdir(parents=True,exist_ok=True)
                if name in changed:
                    member=archive.getinfo('files/'+name)
                    if member.file_size!=record['size']:
                        raise UpdateError('更新文件长度不一致。')
                    with archive.open(member) as source, dest.open('xb') as output:
                        shutil.copyfileobj(source,output)
                else:
                    shutil.copy2(installed/name,dest)
                if dest.stat().st_size!=record['size'] or digest(dest)!=record['sha256']:
                    raise UpdateError('重建后的更新文件校验失败。')
            metadata=json.loads((stage/'_internal/windows-version.json').read_text(encoding='utf-8'))
            if metadata['version']!=version or not (stage/'KnowledgeDistiller.exe').is_file():
                raise UpdateError('更新应用版本无效。')
        except BaseException:
            shutil.rmtree(stage)
            raise
    return manifest


def stage_full(archive,stage,version):
    prefix='知识蒸馏器/'
    names=archive.namelist()
    if len(names)!=len(set(n.casefold() for n in names)) or not names or len(names)>100000:
        raise UpdateError('完整更新归档无效。')
    for member in archive.infolist():
        if not member.filename.startswith(prefix) or member.is_dir() or ((member.external_attr>>16)&0o170000)==0o120000:
            raise UpdateError('完整更新归档路径无效。')
        safe_name(member.filename[len(prefix):])
    stage.mkdir()
    try:
        for member in archive.infolist():
            dest=stage/member.filename[len(prefix):]
            dest.parent.mkdir(parents=True,exist_ok=True)
            with archive.open(member) as source,dest.open('xb') as output:
                shutil.copyfileobj(source,output)
        metadata=json.loads((stage/'_internal/windows-version.json').read_text(encoding='utf-8'))
        if metadata['version']!=version or not (stage/'KnowledgeDistiller.exe').is_file():
            raise UpdateError('完整更新版本不一致。')
        return {'version':version,'base_version':None}
    except BaseException:
        shutil.rmtree(stage)
        raise
