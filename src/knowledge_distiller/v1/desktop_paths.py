"""Small native desktop discovery helpers; never inspect browser user profiles."""
import os
from pathlib import Path
import shutil
import sys


def chrome_executable() -> Path:
    if sys.platform != 'win32':
        return Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
    candidates = []
    for variable in ('PROGRAMFILES', 'PROGRAMFILES(X86)', 'LOCALAPPDATA'):
        if os.environ.get(variable):
            candidates.append(Path(os.environ[variable]) / 'Google/Chrome/Application/chrome.exe')
    found = shutil.which('chrome.exe')
    if found:
        candidates.append(Path(found))
    return next((path for path in candidates if path.is_file()), Path('chrome.exe'))


def choose_windows_folder() -> Path | None:
    # Run the native dialog on its own STA thread: Flask request threads need not
    # have a COM apartment, and Tk would add an otherwise unnecessary runtime.
    import ctypes
    from ctypes import wintypes
    import threading
    result, errors = [], []

    def choose():
        ole = ctypes.OleDLL('ole32')
        shell = ctypes.WinDLL('shell32')
        class BrowseInfo(ctypes.Structure):
            _fields_ = [('owner', wintypes.HWND), ('root', ctypes.c_void_p),
                        ('display', wintypes.LPWSTR), ('title', wintypes.LPCWSTR),
                        ('flags', wintypes.UINT), ('callback', ctypes.c_void_p),
                        ('param', wintypes.LPARAM), ('image', ctypes.c_int)]
        shell.SHBrowseForFolderW.argtypes = [ctypes.POINTER(BrowseInfo)]
        shell.SHBrowseForFolderW.restype = ctypes.c_void_p
        shell.SHGetPathFromIDListW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR]
        shell.SHGetPathFromIDListW.restype = wintypes.BOOL
        ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        initialized = False
        try:
            ole.CoInitializeEx(None, 2)
            initialized = True
            display = ctypes.create_unicode_buffer(260)
            info = BrowseInfo(None, None, ctypes.cast(display, wintypes.LPWSTR),
                              '选择 Obsidian Vault', 0x41, None, 0, 0)
            item = shell.SHBrowseForFolderW(ctypes.byref(info))
            if not item:
                result.append(None)
                return
            try:
                path = ctypes.create_unicode_buffer(260)
                if not shell.SHGetPathFromIDListW(item, path):
                    raise OSError('vault_picker_failed')
                result.append(Path(path.value))
            finally:
                ole.CoTaskMemFree(item)
        except Exception as error:
            errors.append(error)
        finally:
            if initialized:
                ole.CoUninitialize()
    thread = threading.Thread(target=choose)
    thread.start()
    thread.join()
    if errors:
        raise OSError('vault_picker_failed') from errors[0]
    return result[0]
