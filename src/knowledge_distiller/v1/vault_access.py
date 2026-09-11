"""Read-only Vault/archived-note status and explicit OS open actions."""
import json
import os
from pathlib import Path
import subprocess
from urllib.parse import quote


def vault_registration(vault):
    registry = Path.home() / 'Library/Application Support/obsidian/obsidian.json'
    try:
        data = json.loads(registry.read_text(encoding='utf-8'))
        entries = data.get('vaults', {})
        if not isinstance(entries, dict):
            return None, 'registry_unavailable'
        for key, entry in entries.items():
            if not isinstance(entry, dict) or not isinstance(entry.get('path'), str):
                continue
            if isinstance(key, str) and key and Path(entry['path']).expanduser().resolve() == vault:
                return key, 'registered'
    except FileNotFoundError:
        return None, 'unregistered'
    except (OSError, ValueError, TypeError, AttributeError, RuntimeError):
        return None, 'registry_unavailable'
    return None, 'unregistered'


def vault_status(value):
    result = {'state': 'unconfigured', 'registration': 'unregistered', 'openable': False,
              'message': '尚未选择笔记保存位置。'}
    if not value or value == '-':
        return result
    try:
        vault = Path(value).expanduser().resolve()
        if not vault.is_dir():
            return {**result, 'state': 'problem', 'message': '保存位置不存在或无法访问，请检查磁盘和目录。'}
        writable = os.access(vault, os.R_OK | os.W_OK | os.X_OK)
        _, registration = vault_registration(vault)
        message = {
            'registered': '保存位置可用，已在 Obsidian 中识别，可跳转已归档笔记。',
            'unregistered': '保存位置已设置；尚未在 Obsidian 中识别。请在 Obsidian 选择“打开文件夹作为仓库”，选中上方同一个目录，再点重新检查。',
            'registry_unavailable': '保存位置已设置，但暂时无法读取 Obsidian 仓库登记信息。请打开 Obsidian 后重新检查。',
        }[registration]
        if not writable:
            message = '保存目录存在，但当前没有读写权限，请检查目录权限。'
        return {'state': 'configured' if writable else 'problem', 'registration': registration,
                'openable': True, 'message': message}
    except (OSError, ValueError, RuntimeError):
        return {**result, 'state': 'problem', 'message': '保存位置无法访问，请检查目录。'}


def publication_status(vault_path, published_path):
    result = {'state': 'unpublished', 'message': '尚无归档位置记录。',
              'file': None, 'folder': None, 'url': None}
    if not vault_path or not published_path:
        return result
    try:
        vault = Path(vault_path).expanduser().resolve()
        relative = Path(published_path)
        target = (vault / relative).resolve()
        if relative.is_absolute() or not target.is_relative_to(vault):
            return {**result, 'state': 'invalid_path', 'message': '归档路径无效，无法打开。'}
        if not vault.is_dir():
            return {**result, 'state': 'vault_missing', 'message': '原保存文件夹不可用，请检查磁盘或原路径。'}
        result['folder'] = vault
        if not target.is_file():
            return {**result, 'state': 'file_missing', 'message': '归档文件未找到，请检查原保存文件夹；不会自动重写笔记。'}
        result['file'] = target
        identifier, registration = vault_registration(vault)
        if not identifier:
            return {**result, 'state': registration, 'message': (
                '笔记已保存；暂时无法读取 Obsidian 仓库登记信息。' if registration == 'registry_unavailable'
                else '笔记已保存；请在 Obsidian 中将原保存文件夹打开为仓库。')}
        return {**result, 'state': 'ready', 'message': '笔记已保存，可以在 Obsidian 中打开。',
                'url': 'obsidian://open?vault='+quote(identifier, safe='')+'&file='+quote(relative.as_posix(), safe='')}
    except (OSError, ValueError, RuntimeError):
        return {**result, 'state': 'unavailable', 'message': '暂时无法访问原归档位置。'}


def open_saved_location(vault_path, published_path=None, *, reveal=False):
    if published_path is not None:
        status = publication_status(vault_path, published_path)
        target = status['file'] if reveal else status['folder']
    else:
        status = vault_status(vault_path)
        target = Path(vault_path).expanduser().resolve() if status['openable'] else None
    if target is None:
        raise ValueError('原保存位置不可用，请检查文件或磁盘；已有归档记录保持不变。')
    try:
        subprocess.run(['/usr/bin/open', *(['-R'] if reveal else []), str(target)],
                       check=True, capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError('系统未能打开保存位置，请在 Finder 中检查该目录。') from error
