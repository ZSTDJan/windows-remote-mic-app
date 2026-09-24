"""Bounded, metadata-only injection results. Never used to authorize injection.

Direct children return one stdout record; the protected helper's transport is
owned by hid_elevation_windows. Only the parent writes normal application logs.
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import sys
import uuid

from . import __version__

RESULT_LIMIT = 4096
RESULT_SCHEMA = 1
STAGES = frozenset({
    'unknown', 'host_lookup', 'debug_privilege', 'target_open', 'target_identity',
    'runtime_prepare', 'runtime_hash', 'host_reload', 'host_recheck',
    'remote_allocate', 'remote_write', 'load_library_lookup', 'remote_thread',
    'remote_wait', 'remote_exit', 'complete', 'child_launch', 'child_wait',
    'helper_identity', 'helper_task', 'consumer_registration',
})
APIS = frozenset({
    'OpenProcessToken', 'LookupPrivilegeValueW', 'AdjustTokenPrivileges',
    'OpenProcess', 'QueryFullProcessImageNameW', 'VirtualAllocEx',
    'WriteProcessMemory', 'GetModuleHandleW', 'GetProcAddress',
    'CreateRemoteThread', 'WaitForSingleObject', 'GetExitCodeThread',
})
ERROR_TYPES = frozenset({
    'OSError', 'PermissionError', 'FileNotFoundError', 'FileExistsError',
    'NotADirectoryError', 'IsADirectoryError', 'TimeoutError', 'TimeoutExpired',
    'RuntimeError', 'ValueError', 'TypeError', 'COMError', 'HidElevationError',
    'HidInjectionStageError', 'HidTapInjectionError', 'Exception',
})
REASONS = frozenset({
    'runtime_archive_hash_mismatch', 'runtime_dll_hash_mismatch',
    'runtime_directory_acl_invalid', 'runtime_file_acl_invalid',
})
_INTEGER_FIELDS = frozenset({'winerror', 'errno', 'hresult', 'return_value',
                           'expected_bytes', 'actual_bytes', 'target_pid', 'worker_pid',
                           'exit_code'})


def instance_id(value):
    if not isinstance(value, str) or len(value) > 38:
        return None
    try:
        return uuid.UUID(value).hex
    except ValueError:
        return None


def sanitize(value):
    """Drop arbitrary strings, nested objects, paths and exception messages."""
    if not isinstance(value, dict):
        return {}
    result = {}
    for key in _INTEGER_FIELDS:
        item = value.get(key)
        if item is None or (type(item) is int and -(2**63) <= item < 2**64):
            result[key] = item
    for key in ('success', 'worker_elevated', 'worker_frozen'):
        if type(value.get(key)) is bool or value.get(key) is None:
            result[key] = value.get(key)
    for key, choices, default in (('stage', STAGES, 'unknown'), ('api', APIS, None),
                                  ('error_type', ERROR_TYPES, None),
                                  ('reason', REASONS, None)):
        item = value.get(key)
        result[key] = item if isinstance(item, str) and item in choices else default
    version = value.get('worker_version')
    if isinstance(version, str) and re.fullmatch(r'[0-9][0-9A-Za-z.+-]{0,63}', version):
        result['worker_version'] = version
    return result


def failure(exc, *, stage='unknown'):
    """Use captured exception attributes only; never read a late GetLastError."""
    result = dict(stage=stage, error_type=None, winerror=None, errno=None, hresult=None)
    seen = set()
    for _ in range(8):
        if exc is None or id(exc) in seen:
            break
        seen.add(id(exc))
        fields = getattr(exc, 'injection_diagnostic', None)
        if isinstance(fields, dict):
            result.update({key: value for key, value in sanitize(fields).items()
                           if value is not None and (key != 'stage' or value != 'unknown')})
        name = type(exc).__name__
        if not (isinstance(fields, dict) and isinstance(fields.get('error_type'), str)
                and fields['error_type'] in ERROR_TYPES):
            if result['error_type'] is None or name not in {
                    'HidTapInjectionError', 'HidElevationError', 'HidInjectionStageError'}:
                result['error_type'] = name if name in ERROR_TYPES else 'Exception'
        for key in ('winerror', 'errno', 'hresult'):
            number = getattr(exc, key, None)
            if type(number) is int:
                result[key] = number
        exc = exc.__cause__
    return sanitize(result)


def result_record(*, success, target_pid=None, exit_code, exc=None, stage='unknown'):
    fields = failure(exc, stage=stage) if exc is not None else dict(stage='complete')
    try:
        from .hid_elevation_windows import is_process_elevated
        elevated = is_process_elevated()
    except Exception:
        elevated = None
    fields.update(success=success, target_pid=target_pid or fields.get('target_pid'),
                  exit_code=exit_code, worker_pid=os.getpid(), worker_version=__version__,
                  worker_frozen=bool(getattr(sys, 'frozen', False)), worker_elevated=elevated)
    return dict(schema=RESULT_SCHEMA, **sanitize(fields))


def encode(record):
    return (json.dumps(dict(schema=RESULT_SCHEMA, **sanitize(record)),
                       ensure_ascii=True, separators=(',', ':')) + '\n').encode('ascii')


def decode(payload, *, exit_code, target_pid=None):
    if not payload:
        return {'result_status': 'missing'}
    if not isinstance(payload, bytes) or len(payload) > RESULT_LIMIT:
        return {'result_status': 'invalid'}
    try:
        value = json.loads(payload)
        if (not isinstance(value, dict) or type(value.get('schema')) is not int
                or value['schema'] != RESULT_SCHEMA
                or type(value.get('exit_code')) is not int or value['exit_code'] != exit_code
                or type(value.get('success')) is not bool
                or value['success'] != (exit_code == 0)
                or not isinstance(value.get('stage'), str) or value['stage'] not in STAGES
                or type(value.get('worker_pid')) is not int or value['worker_pid'] <= 0
                or (value.get('target_pid') is not None and
                    (type(value['target_pid']) is not int or value['target_pid'] <= 0))
                or (target_pid is not None and value.get('target_pid') not in (None, target_pid))):
            return {'result_status': 'invalid'}
        return dict(sanitize(value), result_status='received')
    except (ValueError, TypeError, RecursionError):
        return {'result_status': 'invalid'}


def write_stdout(record):
    """PyInstaller's windowed child can have sys.stdout=None; use its OS handle."""
    try:
        payload = encode(record)
        if os.name == 'nt':
            from ctypes import wintypes
            kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel32.GetStdHandle.argtypes = (wintypes.DWORD,)
            kernel32.GetStdHandle.restype = wintypes.HANDLE
            kernel32.WriteFile.argtypes = (wintypes.HANDLE, ctypes.c_void_p,
                                           wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p)
            kernel32.WriteFile.restype = wintypes.BOOL
            written = wintypes.DWORD()
            kernel32.WriteFile(kernel32.GetStdHandle(-11), payload, len(payload),
                               ctypes.byref(written), None)
        else:
            os.write(1, payload)
    except Exception:
        pass  # Diagnostic delivery must never change the original exit code.
