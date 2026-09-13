"""Windows UI metadata without WMI calls from HTTP worker threads."""
import ctypes
import sys


def filesystem_path(path):
    """Use Win32 extended paths without requiring machine-wide policy changes.

    abspath is lexical: resolving junctions here would hide them from the
    caller's subsequent ordinary-file and reparse-point checks.
    """
    import os
    from pathlib import Path
    if os.name != 'nt':
        return Path(path)
    value = os.path.abspath(path)
    if not value.startswith('\\\\?\\'):
        value = ('\\\\?\\UNC\\' + value[2:]) if value.startswith('\\\\') else '\\\\?\\' + value
    return Path(value)


def machine():
    # GetNativeSystemInfo's first WORD is wProcessorArchitecture. A buffer of
    # the documented SYSTEM_INFO size preserves the remaining native fields.
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetNativeSystemInfo.argtypes = [ctypes.c_void_p]
    kernel.GetNativeSystemInfo.restype = None
    info = ctypes.create_string_buffer(48 if ctypes.sizeof(ctypes.c_void_p) == 8 else 36)
    kernel.GetNativeSystemInfo(info)
    architecture = int.from_bytes(info.raw[:2], 'little')
    return {0: 'x86', 9: 'AMD64', 12: 'ARM64'}.get(architecture, 'unknown')


def system_label():
    version = sys.getwindowsversion()
    release = '11' if version.major == 10 and version.build >= 22000 else str(version.major)
    return f'Windows {release} · {machine()}'


def is_link_or_reparse(path):
    """Python 3.11 has no Path.is_junction; reject native reparse points too."""
    import stat
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)
