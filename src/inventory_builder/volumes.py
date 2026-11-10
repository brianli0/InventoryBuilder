"""Read only the selected path's containing volume; never enumerate devices."""

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import os
from pathlib import Path


@dataclass(frozen=True)
class Volume:
    mount: Path
    guid: str
    serial: str
    filesystem: str
    capacity: int
    label: str = ''


def selected_volume(path):
    if os.name != 'nt':
        raise ValueError('Physical volume binding currently requires Windows or a test adapter')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetVolumePathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    kernel.GetVolumeNameForVolumeMountPointW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    kernel.GetVolumeInformationW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.DWORD]
    kernel.GetDiskFreeSpaceExW.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulonglong), ctypes.c_void_p]
    mount = ctypes.create_unicode_buffer(32768)
    if not kernel.GetVolumePathNameW(str(path), mount, len(mount)):
        raise ctypes.WinError(ctypes.get_last_error())
    guid = ctypes.create_unicode_buffer(1024)
    if not kernel.GetVolumeNameForVolumeMountPointW(mount.value, guid, len(guid)):
        raise ctypes.WinError(ctypes.get_last_error())
    label, filesystem = ctypes.create_unicode_buffer(1024), ctypes.create_unicode_buffer(1024)
    serial, maximum, flags = wintypes.DWORD(), wintypes.DWORD(), wintypes.DWORD()
    if not kernel.GetVolumeInformationW(mount.value, label, len(label), ctypes.byref(serial), ctypes.byref(maximum), ctypes.byref(flags), filesystem, len(filesystem)):
        raise ctypes.WinError(ctypes.get_last_error())
    total = ctypes.c_ulonglong()
    if not kernel.GetDiskFreeSpaceExW(mount.value, None, ctypes.byref(total), None):
        raise ctypes.WinError(ctypes.get_last_error())
    return Volume(Path(mount.value), guid.value, f'{serial.value:08X}', filesystem.value, total.value, label.value)


def reject_case_sensitive(path):
    """Fail closed on directories with Windows per-directory case sensitivity."""
    if os.name != 'nt':
        return
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    handle = kernel.CreateFileW(str(path), 0x80, 7, None, 3, 0x02200000, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    kernel.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    try:
        flags = wintypes.DWORD()
        if not kernel.GetFileInformationByHandleEx(handle, 23, ctypes.byref(flags), ctypes.sizeof(flags)):
            error = ctypes.get_last_error()
            if error not in (1, 50, 87):  # FAT/exFAT do not support this information class.
                raise ctypes.WinError(error)
        elif flags.value & 1:
            raise ValueError('Case-sensitive directories require explicit support; refusing ambiguous path comparisons')
    finally:
        kernel.CloseHandle(handle)
