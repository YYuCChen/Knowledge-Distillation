"""Format 2: publisher-authenticated archive, exact trees, per-file native patch.

Authentication is performed by Updates.download before this staging layer.
The embedded old manifest is a compatibility transition; its digest is part of
the signed archive, never inferred from an arbitrary local inventory.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile
import time
import zipfile
import zlib

from .updates import UpdateError
from .windows_delta import MANIFEST, digest, inventory, safe_name
from .windows_platform import filesystem_path
from . import windows_binary_patch as binary

PLATFORM = 'windows-x86_64'
MAX_TOTAL = 20*1024**3


def canonical(value):
    return json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()


def manifest_digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def _deflated_size(path):
    compressor = zlib.compressobj(6,zlib.DEFLATED,-15)
    size = 0
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024**2),b''):
            size += len(compressor.compress(chunk))
    return size + len(compressor.flush())


def build_payload(target, output, base=None, *, tools_dir=None):
    target, output = filesystem_path(target), filesystem_path(output)
    base = filesystem_path(base) if base is not None else None
    files, old = inventory(target), inventory(base) if base else {}
    _validate_files(files); _validate_files(old,allow_empty=True)
    version = json.loads((target/'_internal/windows-version.json').read_text(encoding='utf-8'))['version']
    previous = json.loads((base/'_internal/windows-version.json').read_text(encoding='utf-8'))['version'] if base else None
    operations = {}
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='delta-encode-',dir=output.parent) as directory:
        with zipfile.ZipFile(output,'x',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
            for index,(name,record) in enumerate(files.items()):
                if record == old.get(name):
                    operations[name] = {'kind':'reuse'}
                    continue
                operation = {'kind':'full','reason':'new_or_small_file'}
                if (name in old and binary.MIN_PATCH_FILE <= record['size'] <= binary.MAX_FILE
                        and old[name]['size'] <= binary.MAX_FILE):
                    patch = Path(directory)/f'{index}.patch'
                    metrics = binary.create_patch(base/name,target/name,patch,directory=tools_dir)
                    full_size, patch_size = _deflated_size(target/name), patch.stat().st_size
                    operation.update(full_encoded_size=full_size,patch_encoded_size=patch_size,
                                     encode_seconds=metrics['seconds'],encode_peak_rss=metrics['peak_rss'])
                    if (patch_size <= full_size*binary.MAX_PATCH_RATIO
                            and full_size-patch_size >= binary.MIN_SAVINGS):
                        asset = f'patches/{index}.hdiff'
                        operation.update(kind='patch',reason='encoded_savings',algorithm=binary.ALGORITHM,
                            old=old[name],asset=asset,patch={'size':patch_size,'sha256':digest(patch)})
                        archive.write(patch,asset,compress_type=zipfile.ZIP_STORED)
                    else:
                        operation['reason'] = 'insufficient_encoded_savings'
                    patch.unlink()
                if operation['kind'] == 'full':
                    operation['asset'] = 'files/'+name
                    archive.write(target/name,operation['asset'])
                operations[name] = operation
            manifest = {'format':2,'platform':PLATFORM,'minimum_updater':2,
                'version':version,'base_version':previous,'base_manifest_sha256':manifest_digest(old),
                'base':old,'files':files,'operations':operations}
            archive.writestr(MANIFEST,canonical(manifest))
    return {'version':version,'base_version':previous,'format':2,'size':output.stat().st_size,
            'seconds':time.monotonic()-started,'operations':operations,'total_files':len(files)}


def _validate_files(files, *, allow_empty=False):
    if not isinstance(files,dict) or (not files and not allow_empty) or len(files)>100000:
        raise UpdateError('更新文件清单无效。')
    folded = set()
    for name,record in files.items():
        if not isinstance(name,str):
            raise UpdateError('更新文件路径无效。')
        safe_name(name)
        if (name.casefold() in folded or not isinstance(record,dict)
                or type(record.get('size')) is not int or not 0<=record['size']<=MAX_TOTAL
                or not isinstance(record.get('sha256'),str) or not re.fullmatch('[0-9a-f]{64}',record['sha256'])):
            raise UpdateError('更新文件清单无效。')
        folded.add(name.casefold())
    if sum(r['size'] for r in files.values())>MAX_TOTAL:
        raise UpdateError('更新展开空间超限。')
    if any('/'.join(n.split('/')[:i]) in folded for n in folded for i in range(1,len(n.split('/')))):
        raise UpdateError('更新路径文件与目录冲突。')


def _unique_object(pairs):
    result = {}
    for key,value in pairs:
        if key in result:
            raise UpdateError('更新清单有重复字段。')
        result[key] = value
    return result


def _copy_verified(source, target, record):
    digestor = hashlib.sha256()
    count = 0
    with target.open('xb') as output:
        for chunk in iter(lambda:source.read(1024**2),b''):
            count += len(chunk)
            if count > record['size']:
                raise UpdateError('更新文件输出超限。')
            digestor.update(chunk); output.write(chunk)
    if count != record['size'] or digestor.hexdigest()!=record['sha256']:
        raise UpdateError('重建后的更新文件校验失败。')


def stage_payload(archive_path,installed,stage,*,version,current,tools_dir=None):
    installed,stage=filesystem_path(installed),filesystem_path(stage)
    archive_path=filesystem_path(archive_path)
    if stage.exists() or stage.is_symlink():
        raise UpdateError('上次更新暂存目录仍存在，请处理后重试。')
    started=time.monotonic()
    with zipfile.ZipFile(archive_path) as archive:
        if archive.getinfo(MANIFEST).file_size>32*1024**2:
            raise UpdateError('更新文件清单过大。')
        manifest=json.loads(archive.read(MANIFEST),object_pairs_hook=_unique_object)
        if (manifest.get('format')!=2 or manifest.get('platform')!=PLATFORM
                or type(manifest.get('minimum_updater')) is not int or not 1<=manifest['minimum_updater']<=2
                or manifest.get('version')!=version or manifest.get('base_version') not in (None,current)):
            raise UpdateError('更新协议、平台或基线不匹配。')
        files,old,operations=manifest.get('files'),manifest.get('base'),manifest.get('operations')
        _validate_files(files);_validate_files(old,allow_empty=True)
        if manifest.get('base_manifest_sha256')!=manifest_digest(old):
            raise UpdateError('已签名基线清单摘要不匹配。')
        if not isinstance(operations,dict) or set(operations)!=set(files):
            raise UpdateError('更新操作清单无效。')
        expected={MANIFEST}
        for name,operation in operations.items():
            if not isinstance(operation,dict) or operation.get('kind') not in ('reuse','full','patch'):
                raise UpdateError('未知更新操作。')
            kind=operation['kind']
            if kind=='reuse':
                if files[name]!=old.get(name):raise UpdateError('复用文件与基线不一致。')
                continue
            asset=operation.get('asset')
            if not isinstance(asset,str) or safe_name(asset) in expected:
                raise UpdateError('补丁载荷路径无效。')
            expected.add(asset)
            if kind=='patch':
                if (operation.get('algorithm')!=binary.ALGORITHM or name not in old
                        or operation.get('old')!=old[name] or max(old[name]['size'],files[name]['size'])>binary.MAX_FILE):
                    raise UpdateError('差分算法或旧文件身份不匹配。')
                _validate_files({'patch':operation.get('patch')})
                if operation['patch']['size']>binary.MAX_FILE:raise UpdateError('差分载荷超限。')
            length=operation['patch']['size'] if kind=='patch' else files[name]['size']
            if archive.getinfo(asset).file_size!=length:raise UpdateError('更新载荷长度不一致。')
        if len(archive.infolist())!=len(expected) or set(archive.namelist())!=expected:
            raise UpdateError('更新归档含未声明或重复文件。')
        if any(((e.external_attr>>16)&0o170000)==0o120000 for e in archive.infolist()):
            raise UpdateError('更新归档含链接。')
        if manifest['base_version']:
            if inventory(installed)!=old:raise UpdateError('当前应用不匹配受信基线，请选择修复路径。')
        elif old or any(o['kind']!='full' for o in operations.values()):
            raise UpdateError('首装清单不能依赖未知基线。')
        baseline_seconds=time.monotonic()-started
        if shutil.disk_usage(stage.parent).free < sum(r['size'] for r in files.values())+binary.MAX_FILE+256*1024**2:
            raise UpdateError('重建更新所需空间不足，当前应用未改变。')
        stage.mkdir()
        metrics=[]
        try:
            with tempfile.TemporaryDirectory(prefix='delta-decode-',dir=stage.parent) as temporary:
                for index,(name,record) in enumerate(files.items()):
                    dest=stage/name;dest.parent.mkdir(parents=True,exist_ok=True)
                    operation=operations[name];kind=operation['kind'];tick=time.monotonic()
                    if kind=='reuse':
                        with (installed/name).open('rb') as source:_copy_verified(source,dest,record)
                    elif kind=='full':
                        with archive.open(operation['asset']) as source:_copy_verified(source,dest,record)
                    else:
                        patch=Path(temporary)/f'{index}.hdiff'
                        with archive.open(operation['asset']) as source:_copy_verified(source,patch,operation['patch'])
                        if digest(installed/name)!=operation['old']['sha256']:
                            raise UpdateError('差分旧文件在重建前变化。')
                        binary.apply_patch(installed/name,patch,dest,old_size=operation['old']['size'],
                                           target_size=record['size'],directory=tools_dir)
                        if dest.stat().st_size!=record['size'] or digest(dest)!=record['sha256']:
                            raise UpdateError('差分目标核验失败。')
                        patch.unlink()
                    metrics.append({'file':name,'kind':kind,'seconds':time.monotonic()-tick})
            metadata=json.loads((stage/'_internal/windows-version.json').read_text(encoding='utf-8'))
            if metadata['version']!=version or not (stage/'KnowledgeDistiller.exe').is_file():
                raise UpdateError('更新应用版本无效。')
            # Construction visits the exact target whitelist and verifies every
            # write. No second full-tree hash scan is needed in this same stage.
        except BaseException:
            shutil.rmtree(stage)
            raise
    return {**manifest,'metrics':{'baseline_seconds':baseline_seconds,'files':metrics,
                                 'total_seconds':time.monotonic()-started}}
