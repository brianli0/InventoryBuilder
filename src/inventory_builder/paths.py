"""Versioned Windows ordinal path comparison (also used by SQLite indexes)."""

import ctypes
import os
import platform
import posixpath

MATCHER_VERSION = 'win-ordinal-v1-' + platform.version() if os.name == 'nt' else 'posix-v1'


def compare(left, right):
    if os.name == 'nt':
        function = ctypes.windll.kernel32.CompareStringOrdinal
        function.argtypes = [ctypes.c_wchar_p, ctypes.c_int, ctypes.c_wchar_p, ctypes.c_int, ctypes.c_int]
        function.restype = ctypes.c_int
        result = function(left, -1, right, -1, True)
        if not result:
            raise OSError('Windows ordinal path comparison failed')
        return result - 2
    return (left > right) - (left < right)


def relative_key(value):
    text = str(value).replace('\\', '/')
    if text.startswith('/') or ':' in text or '\0' in text:
        raise ValueError('Expected a safe volume-relative path')
    text = posixpath.normpath(text)
    if text == '..' or text.startswith('../'):
        raise ValueError('Path escapes selected root')
    return '' if text == '.' else text


def contains(parent, child):
    """Component comparison: Old contains Old/x, never Older."""
    a, b = relative_key(parent).split('/'), relative_key(child).split('/')
    if a == ['']:
        return True
    return len(a) <= len(b) and all(compare(x, y) == 0 for x, y in zip(a, b))


def within(child, parent):
    a, b = parent.parts, child.parts
    return len(a) <= len(b) and all(compare(x, y) == 0 for x, y in zip(a, b))


def relative_to(child, parent):
    from pathlib import Path
    if not within(child, parent):
        raise ValueError(f'{child} is outside {parent}')
    return Path(*child.parts[len(parent.parts):])
