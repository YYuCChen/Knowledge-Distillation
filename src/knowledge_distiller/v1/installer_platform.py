"""Narrow native picker and accepted-install desktop operations."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from uuid import uuid4

_PICKER = threading.Lock()
_MARKER = 'Knowledge Distiller managed installer shortcut v1'
KINDS = {'folder_app', 'folder_data', 'manifest_file'}


def pick_path(kind):
    if kind not in KINDS: raise ValueError('unknown_picker_kind')
    if not _PICKER.acquire(blocking=False):
        return {'status':'unavailable','message':'文件选择器正在使用，请稍后重试或手填路径。'}
    try:
        path = _mac_picker(kind) if sys.platform == 'darwin' else _windows_picker(kind) if sys.platform == 'win32' else None
        if path is None: return {'status':'cancelled'}
        if not Path(path).is_absolute(): raise ValueError('picker_returned_relative_path')
        return {'status':'selected','path':str(path)}
    except Exception:
        return {'status':'unavailable','message':'系统选择器暂不可用，请手填完整路径。'}
    finally: _PICKER.release()


def _mac_picker(kind):
    from AppKit import NSOpenPanel, NSModalResponseOK, NSApplication
    from Foundation import NSOperationQueue
    completed = threading.Event()
    result = {}
    def present():
        try:
            NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
            panel = NSOpenPanel.openPanel()
            panel.setCanChooseFiles_(kind == 'manifest_file')
            panel.setCanChooseDirectories_(kind != 'manifest_file')
            panel.setAllowsMultipleSelection_(False)
            panel.setCanCreateDirectories_(kind != 'manifest_file')
            panel.setTitle_('选择发行清单' if kind == 'manifest_file' else '选择安装位置' if kind == 'folder_app' else '选择知识与设置位置')
            result['path'] = str(panel.URL().path()) if panel.runModal() == NSModalResponseOK else None
        except Exception as error: result['error'] = error
        finally: completed.set()
    if threading.current_thread() is threading.main_thread(): present()
    else:
        NSOperationQueue.mainQueue().addOperationWithBlock_(present)
        completed.wait()  # main() runs Cocoa event loop, not blocking WSGI on main.
    if 'error' in result: raise result['error']
    return result.get('path')


def _windows_picker(kind):
    import pythoncom
    from win32com.shell import shell, shellcon
    import win32gui
    pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
    try:
        if kind == 'manifest_file':
            try:
                result = win32gui.GetOpenFileNameW(Filter='发行清单\0*.json\0所有文件\0*.*\0',
                    Flags=0x1000 | 0x80000, Title='选择离线发行清单')
                return result[0]
            except win32gui.error as error:
                if error.winerror == 0: return None
                raise
        selected = shell.SHBrowseForFolder(0,None,'选择本机文件夹',
            shellcon.BIF_RETURNONLYFSDIRS | shellcon.BIF_NEWDIALOGSTYLE)
        return shell.SHGetPathFromIDList(selected[0]).decode('utf-8') if selected and selected[0] else None
    finally: pythoncom.CoUninitialize()


def manual_command(target, data_root):
    executable = Path(target) / 'KnowledgeDistiller.exe'
    return subprocess.list2cmdline([str(executable),'--data-dir',str(Path(data_root).absolute())])


def create_shortcut(target, data_root):
    if sys.platform != 'win32': return {'status':'not_applicable'}
    import pythoncom
    from win32com.client import Dispatch
    from win32com.shell import shell
    target, data_root = Path(target).absolute(), Path(data_root).absolute()
    executable = target/'KnowledgeDistiller.exe'
    pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
    temporary = None
    try:
        desktop = Path(shell.SHGetKnownFolderPath('{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}',0,None))
        desktop.mkdir(parents=True,exist_ok=True)
        engine=Dispatch('WScript.Shell')
        def owned(path):
            if not path.is_file() or path.is_symlink(): return False
            link=engine.CreateShortcut(str(path))
            return (link.Description == _MARKER and Path(link.TargetPath).name.casefold() == 'knowledgedistiller.exe'
                    and '--data-dir' in link.Arguments)
        destination=desktop/'知识蒸馏器.lnk'
        warning=''
        if destination.exists() and not owned(destination):
            destination=desktop/('知识蒸馏器-'+uuid4().hex[:8]+'.lnk')
            warning='同名非本产品入口已保留，使用新的桌面入口名称。'
        temporary=desktop/('.kd-install-'+uuid4().hex+'.lnk')
        link=engine.CreateShortcut(str(temporary))
        link.TargetPath=str(executable)
        link.Arguments=subprocess.list2cmdline(['--data-dir',str(data_root)])
        link.WorkingDirectory=str(target)
        link.IconLocation=str(executable)+',0'
        link.Description=_MARKER
        link.Save()
        check=engine.CreateShortcut(str(temporary))
        if (Path(check.TargetPath) != executable or check.Arguments != link.Arguments
                or Path(check.WorkingDirectory) != target or check.Description != _MARKER):
            raise OSError('shortcut_readback_mismatch')
        if destination.exists() and not owned(destination): raise OSError('shortcut_destination_changed')
        os.replace(temporary,destination)
        return {'status':'complete','path':str(destination),'warning':warning}
    finally:
        if temporary: temporary.unlink(missing_ok=True)
        pythoncom.CoUninitialize()
