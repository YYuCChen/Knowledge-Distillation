import multiprocessing
import os
import sys


def restore_worker_streams():
    """The windowed bootloader clears Python streams, but retains redirected pipes."""
    import ctypes
    import msvcrt
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetStdHandle.argtypes = [wintypes.DWORD]
    kernel.GetStdHandle.restype = wintypes.HANDLE
    for name, identifier, mode in [('stdin', -10, 'r'), ('stdout', -11, 'w'), ('stderr', -12, 'w')]:
        if getattr(sys, name) is not None:
            continue
        handle = kernel.GetStdHandle(identifier & 0xffffffff)
        if not handle or handle == ctypes.c_void_p(-1).value:
            if name != 'stderr':
                raise RuntimeError('Worker requires redirected standard input and output')
            stream = open(os.devnull, 'w', encoding='utf-8')
        else:
            flags = (os.O_RDONLY if mode == 'r' else os.O_WRONLY) | os.O_BINARY
            stream = os.fdopen(msvcrt.open_osfhandle(handle, flags), mode,
                               encoding='utf-8', buffering=1)
        setattr(sys, name, stream)


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ('--feishu-worker', '--bilibili-worker'):
        restore_worker_streams()
        from knowledge_distiller.v1.windows_app import configure_runtime
        configure_runtime()
        if sys.argv[1] == '--feishu-worker':
            from knowledge_distiller.v1.feishu_socket import worker_main
            try:
                return worker_main()
            except Exception as error:
                # Background protocol failures must exit for the owner's retry,
                # rather than block the windowed bootloader on an error dialog.
                print('Feishu worker stopped (' + type(error).__name__ + ')', file=sys.stderr)
                return 1
        from knowledge_distiller.v1.bilibili import _worker_main
        return _worker_main()
    from knowledge_distiller.v1.windows_app import main as desktop_main
    return desktop_main()

if __name__ == '__main__':
    multiprocessing.freeze_support()
    from knowledge_distiller.v1.adapters.python_policy import check_current
    check_current()
    raise SystemExit(main())
