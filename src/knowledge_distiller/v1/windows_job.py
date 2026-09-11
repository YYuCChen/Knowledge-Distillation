"""Own future child processes so a forced application exit cannot orphan them."""
_job = None


def own_children():
    global _job
    import ctypes as c
    from ctypes import wintypes as w
    class Basic(c.Structure):
        _fields_ = [('process_time', c.c_longlong), ('job_time', c.c_longlong),
                    ('flags', w.DWORD), ('minimum', c.c_size_t), ('maximum', c.c_size_t),
                    ('process_limit', w.DWORD), ('affinity', c.c_size_t),
                    ('priority', w.DWORD), ('scheduling', w.DWORD)]
    class IO(c.Structure):
        _fields_ = [(name, c.c_ulonglong) for name in ('read_ops','write_ops','other_ops','read_bytes','write_bytes','other_bytes')]
    class Extended(c.Structure):
        _fields_ = [('basic', Basic), ('io', IO), ('process_memory', c.c_size_t),
                    ('job_memory', c.c_size_t), ('peak_process', c.c_size_t), ('peak_job', c.c_size_t)]
    kernel = c.WinDLL('kernel32', use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [c.c_void_p, w.LPCWSTR]
    kernel.CreateJobObjectW.restype = w.HANDLE
    kernel.SetInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
    kernel.GetCurrentProcess.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    job = kernel.CreateJobObjectW(None, None)
    limits = Extended()
    limits.basic.flags = 0x2000 | 0x0800  # KILL_ON_JOB_CLOSE | BREAKAWAY_OK
    # Ordinary descendants remain owned. Only the explicit desktop restart
    # uses CREATE_BREAKAWAY_FROM_JOB; silent breakaway is never enabled.
    if not job:
        raise c.WinError(c.get_last_error())
    if not kernel.SetInformationJobObject(job, 9, c.byref(limits), c.sizeof(limits)) or not kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess()):
        error = c.get_last_error()
        kernel.CloseHandle(job)
        raise c.WinError(error)
    # Noninheritable, retained for process lifetime. Only descendants launched
    # after assignment join this job; existing user Chrome/Codex never join.
    _job = job


def wait_for_exit(pid, timeout_ms=60000):
    """Wait for the previous desktop before taking its instance lock."""
    import ctypes as c
    from ctypes import wintypes as w
    kernel = c.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenProcess.restype = w.HANDLE
    kernel.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
    kernel.WaitForSingleObject.restype = w.DWORD
    kernel.CloseHandle.argtypes = [w.HANDLE]
    handle = kernel.OpenProcess(0x100000, False, pid)  # SYNCHRONIZE only
    if not handle:
        error = c.get_last_error()
        if error == 87:  # Process already exited.
            return
        raise c.WinError(error)
    try:
        result = kernel.WaitForSingleObject(handle, timeout_ms)
        if result == 258:
            raise TimeoutError('Previous desktop did not exit; restart cancelled')
        if result != 0:
            raise c.WinError(c.get_last_error())
    finally:
        kernel.CloseHandle(handle)
