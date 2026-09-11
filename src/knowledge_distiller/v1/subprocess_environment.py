"""Prevent the frozen Windows DLL directory leaking into external Python."""
from contextlib import contextmanager
import os
import sys
import threading

_lock = threading.Lock()


@contextmanager
def external_process():
    if os.name != 'nt' or not getattr(sys, 'frozen', False):
        yield
        return
    import ctypes
    with _lock:
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.SetDllDirectoryW.argtypes = [ctypes.c_wchar_p]
        if not kernel.SetDllDirectoryW(None):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            yield
        finally:
            kernel.SetDllDirectoryW(str(sys._MEIPASS))
