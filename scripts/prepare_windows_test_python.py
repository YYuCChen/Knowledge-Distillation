"""Owned test interpreter with the same UTF-8 Windows manifest as the release."""
from pathlib import Path
import zipfile
import win32api

project = Path(__file__).resolve().parents[1]
target = project / '.windows-build/utf8-python'
with zipfile.ZipFile(project / '.windows-build/tools/python-3.12.10-embed-amd64.zip') as archive:
    archive.extractall(target)
handle = win32api.BeginUpdateResource(str(target / 'python.exe'), False)
win32api.UpdateResource(handle, 24, 1, (project / 'packaging/windows.manifest').read_bytes(), 0)
win32api.EndUpdateResource(handle, False)
(target / 'python312._pth').write_text('python312.zip\n.\n' + str(project / 'src') + '\n' + str(project) + '\n' + str(project / '.venv-windows/Lib/site-packages') + '\nimport site\n', encoding='utf-8')
