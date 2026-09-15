import os
from pathlib import Path
import sys
import pytest
from knowledge_distiller.v1.installer_platform import create_shortcut, manual_command


def test_manual_command_quotes_data_root():
    command=manual_command(Path('C:/应用 目录'),Path('C:/知识 空间'))
    assert '--data-dir' in command and '知识 空间' in command and '"' in command


@pytest.mark.skipif(sys.platform!='win32',reason='requires actual Windows COM links')
def test_native_shortcut_readback_conflict_and_custom_data(tmp_path,monkeypatch):
    import pythoncom
    from win32com.client import Dispatch
    from win32com.shell import shell
    desktop=tmp_path/'重定向 Desktop';desktop.mkdir()
    monkeypatch.setattr(shell,'SHGetKnownFolderPath',lambda *a:str(desktop))
    target=tmp_path/'应用 安装';target.mkdir();(target/'KnowledgeDistiller.exe').write_bytes(b'synthetic target not executed')
    root=tmp_path/'知识 设置';root.mkdir()
    conflict=desktop/'知识蒸馏器.lnk';conflict.write_bytes(b'foreign shortcut sentinel')
    outcome=create_shortcut(target,root)
    assert outcome['status']=='complete' and outcome['warning']
    assert conflict.read_bytes()==b'foreign shortcut sentinel'
    path=Path(outcome['path'])
    pythoncom.CoInitialize()
    try:
        link=Dispatch('WScript.Shell').CreateShortcut(str(path))
        assert Path(link.TargetPath)==target/'KnowledgeDistiller.exe'
        assert link.Arguments==__import__('subprocess').list2cmdline(['--data-dir',str(root)])
        assert Path(link.WorkingDirectory)==target
        assert str(target/'KnowledgeDistiller.exe') in link.IconLocation
    finally:
        link=None
        pythoncom.CoUninitialize()
    # Owned same-name link update keeps the exact new data-root arguments.
    conflict.unlink();path.rename(conflict)
    next_root=tmp_path/'新的 数据';next_root.mkdir()
    updated=create_shortcut(target,next_root)
    assert updated['path']==str(conflict)
