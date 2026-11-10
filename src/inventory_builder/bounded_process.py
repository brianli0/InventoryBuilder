"""Timeout/output-bounded subprocess execution; no shell or orphaned workers."""

import os
import subprocess
import threading


def windows_job(process):
    if os.name!='nt':
        return None
    import ctypes
    from ctypes import wintypes
    class Basic(ctypes.Structure):
        _fields_ = [('process_time',ctypes.c_longlong),('job_time',ctypes.c_longlong),('flags',wintypes.DWORD),('minimum',ctypes.c_size_t),('maximum',ctypes.c_size_t),('active',wintypes.DWORD),('affinity',ctypes.c_size_t),('priority',wintypes.DWORD),('scheduling',wintypes.DWORD)]
    class IO(ctypes.Structure):
        _fields_ = [(name,ctypes.c_ulonglong) for name in ('reads','writes','other','read_bytes','write_bytes','other_bytes')]
    class Extended(ctypes.Structure):
        _fields_ = [('basic',Basic),('io',IO),('process_memory',ctypes.c_size_t),('job_memory',ctypes.c_size_t),('peak_process',ctypes.c_size_t),('peak_job',ctypes.c_size_t)]
    kernel = ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p,wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE,wintypes.HANDLE]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateJobObjectW(None,None)
    limits = Extended()
    limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not handle or not kernel.SetInformationJobObject(handle,9,ctypes.byref(limits),ctypes.sizeof(limits)) or not kernel.AssignProcessToJobObject(handle,wintypes.HANDLE(int(process._handle))):
        error = ctypes.get_last_error()
        if handle:
            kernel.CloseHandle(handle)
        process.kill()
        process.wait()
        raise ctypes.WinError(error)
    return lambda:kernel.CloseHandle(handle)


def terminate(process):
    if process.poll() is None:
        if os.name == 'nt':
            try:
                subprocess.run(['taskkill.exe', '/PID', str(process.pid), '/T', '/F'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
            except (OSError, subprocess.TimeoutExpired):
                process.kill()
            if process.poll() is None:
                process.kill()
        else:
            process.kill()
    process.wait(timeout=10)


def run_bounded(command, *, timeout=30, output_limit=16*1024*1024, heartbeat=None, env=None):
    flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False, creationflags=flags,env=env)
    try:
        close_job = windows_job(process)
    except BaseException:
        if process.poll() is None:
            process.kill()
        process.wait()
        process.stdout.close()
        process.stderr.close()
        raise
    output, errors = bytearray(), bytearray()
    exceeded = threading.Event()

    def read(pipe, destination, limit):
        while chunk := pipe.read(65536):
            available = max(0, limit-len(destination))
            destination.extend(chunk[:available])
            if len(chunk) > available:
                exceeded.set()
                # Keep draining until the controlling thread terminates the process.

    readers = [threading.Thread(target=read, args=(process.stdout, output, output_limit), daemon=True), threading.Thread(target=read, args=(process.stderr, errors, 262144), daemon=True)]
    for thread in readers:
        thread.start()
    import time
    deadline = time.monotonic()+timeout
    last_heartbeat = 0
    try:
        while process.poll() is None or any(thread.is_alive() for thread in readers):
            if exceeded.wait(.02):
                raise ValueError('Subprocess output limit exceeded')
            if time.monotonic() >= deadline:
                raise TimeoutError('Subprocess timeout')
            if heartbeat is not None and time.monotonic()-last_heartbeat>=1:
                heartbeat()
                last_heartbeat = time.monotonic()
        for thread in readers:
            thread.join(timeout=5)
        if exceeded.is_set():
            raise ValueError('Subprocess output limit exceeded')
        return process.returncode, bytes(output), bytes(errors)
    finally:
        if close_job is not None:
            close_job()  # Kills the entire owned process tree, including wrappers.
        if process.poll() is None:
            terminate(process)
        for thread in readers:
            thread.join(timeout=5)
        process.stdout.close()
        process.stderr.close()
