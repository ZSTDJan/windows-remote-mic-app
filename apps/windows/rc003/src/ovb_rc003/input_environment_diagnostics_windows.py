"""Opt-in local permission/clipboard metadata. No input or clipboard content access.

Only the diagnostic writer and explicitly started receiver own this sampler.
Provider name candidates come from voice_program_manager; they are evidence,
never authorization to control a process or proof of an input delivery failure.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import PureWindowsPath
import sys
import time


def _integrity_rid(buffer, returned: int) -> int | None:
    """Validate an in-buffer mandatory-label SID before reading its last RID."""
    if not ctypes.sizeof(ctypes.c_void_p) <= returned <= ctypes.sizeof(buffer):
        return None
    address = ctypes.c_void_p.from_buffer(buffer).value
    offset = (address or 0) - ctypes.addressof(buffer)
    if offset < ctypes.sizeof(ctypes.c_void_p) + 4 or offset + 8 > returned:
        return None
    raw = bytes(buffer)
    count = raw[offset + 1]
    end = offset + 8 + count * 4
    if raw[offset] != 1 or not 1 <= count <= 15 or end > returned:
        return None
    if raw[offset + 2:offset + 8] != b'\0\0\0\0\0\x10':
        return None
    return int.from_bytes(raw[end - 4:end], 'little')


def process_security(process_id: int) -> dict:
    result = dict(pid=process_id, status="unavailable", executable="", elevated=None,
                  integrity_rid=None, ui_access=None, session_id=None, created_filetime=None)
    if sys.platform != "win32":
        return dict(result, status="unsupported_platform")
    handle = None
    token = wintypes.HANDLE()
    try:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle.restype = wintypes.BOOL
        kernel.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
        kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
        kernel.GetProcessTimes.restype = wintypes.BOOL
        advapi.OpenProcessToken.argtypes = (wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE))
        advapi.OpenProcessToken.restype = wintypes.BOOL
        advapi.GetTokenInformation.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
        advapi.GetTokenInformation.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x1000, False, process_id)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return dict(result, status="process_open_failed", winerror=ctypes.get_last_error())
        name = ctypes.create_unicode_buffer(32768)
        length = wintypes.DWORD(len(name))
        if kernel.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(length)):
            basename = PureWindowsPath(name.value).name
            if len(basename) <= 128 and not any(ord(c) < 32 for c in basename):
                result['executable'] = basename
        times = [wintypes.FILETIME() for _ in range(4)]
        if kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
            result['created_filetime'] = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        if not advapi.OpenProcessToken(handle, 0x0008, ctypes.byref(token)):  # TOKEN_QUERY
            return dict(result, status="token_open_failed", winerror=ctypes.get_last_error())
        errors = {}
        for field, kind in (('elevated', 20), ('ui_access', 26), ('session_id', 12)):
            value, returned = wintypes.DWORD(), wintypes.DWORD()
            if advapi.GetTokenInformation(token, kind, ctypes.byref(value), ctypes.sizeof(value), ctypes.byref(returned)) and returned.value == ctypes.sizeof(value):
                result[field] = int(value.value) if field == 'session_id' else bool(value.value)
            else:
                errors[field] = ctypes.get_last_error()
        needed = wintypes.DWORD()
        advapi.GetTokenInformation(token, 25, None, 0, ctypes.byref(needed))
        if 0 < needed.value <= 4096:
            label = ctypes.create_string_buffer(needed.value)
            if advapi.GetTokenInformation(token, 25, label, len(label), ctypes.byref(needed)):
                result['integrity_rid'] = _integrity_rid(label, needed.value)
        if result['integrity_rid'] is None:
            errors['integrity_rid'] = ctypes.get_last_error()
        result.update(status="partial" if errors else "captured", field_errors=errors)
        return result
    except Exception:
        return dict(result, status="query_failed")
    finally:
        if token.value:
            kernel.CloseHandle(token)
        if handle:
            kernel.CloseHandle(handle)


def clipboard_metadata() -> dict:
    if sys.platform != 'win32':
        return dict(status='unsupported_platform', sequence=None)
    try:
        user = ctypes.WinDLL('user32', use_last_error=True)
        user.GetClipboardSequenceNumber.argtypes = ()
        user.GetClipboardSequenceNumber.restype = wintypes.DWORD
        sequence = int(user.GetClipboardSequenceNumber())
        if not sequence:
            return dict(status='unavailable', sequence=None)
        user.GetClipboardOwner.argtypes = ()
        user.GetClipboardOwner.restype = wintypes.HWND
        user.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
        user.GetWindowThreadProcessId.restype = wintypes.DWORD
        user.IsClipboardFormatAvailable.argtypes = (wintypes.UINT,)
        user.IsClipboardFormatAvailable.restype = wintypes.BOOL
        owner = user.GetClipboardOwner()
        pid = wintypes.DWORD()
        owner_known = bool(owner and user.GetWindowThreadProcessId(owner, ctypes.byref(pid)))
        text_format = bool(user.IsClipboardFormatAvailable(13))  # CF_UNICODETEXT; no GetClipboardData
        stable = sequence == int(user.GetClipboardSequenceNumber()) and owner == user.GetClipboardOwner()
        return dict(status='captured' if stable else 'changed_during_sample', sequence=sequence,
                    owner_hwnd=int(owner) if stable and owner else None,
                    owner_pid=int(pid.value) if stable and owner_known else None,
                    unicode_text_available=text_format if stable else None,
                    owner_is_writer='not_guaranteed', submission_confirmation='unknown')
    except Exception:
        return dict(status='query_failed', sequence=None)


def _voice_candidates():
    from .voice_program_manager import diagnostic_voice_processes
    return diagnostic_voice_processes()


class EnvironmentSampler:
    """Refresh roles on foreground change or every five seconds, never on key callbacks."""
    def __init__(self, *, processes=None, security=None, clipboard=None, clock=None, response=None):
        from .voice_response_diagnostics_windows import ResponseSampler
        self.processes = processes or _voice_candidates
        self.security = security or process_security
        self.clipboard = clipboard or clipboard_metadata
        self.clock = clock or time.monotonic
        self.response = response or ResponseSampler(clock=self.clock)
        self.next_process_sample = 0.0
        self.foreground_identity = None
        self.process_fields = {}
        self.clipboard_sequence = None
        self.clipboard_owner_identity = None
        self.clipboard_owner_fields = {}

    def sample(self, context: dict) -> dict:
        now = self.clock()
        foreground = (context.get('foreground_pid'), context.get('foreground_executable'))
        if foreground != self.foreground_identity or now >= self.next_process_sample:
            self.foreground_identity = foreground
            self.next_process_sample = now + 5
            candidates, inventory = [], 'captured'
            try:
                candidates = list(self.processes())
            except Exception:
                inventory = 'query_failed'
            roles = [('observer', os.getpid(), '')]
            if isinstance(foreground[0], int) and foreground[0] > 0:
                roles.append(('foreground', foreground[0], foreground[1] or ''))
            roles.extend(candidates[:32])
            sampled, rows = {}, []
            for role, pid, expected_name in roles:
                if pid not in sampled:
                    try:
                        sampled[pid] = self.security(pid)
                    except Exception:
                        sampled[pid] = dict(pid=pid, status='query_failed')
                security = dict(sampled[pid])
                actual_name = security.get('executable', '')
                if expected_name and actual_name.casefold() != expected_name.casefold():
                    security = dict(pid=pid, status='identity_unverified',
                                    query_status=security.get('status', 'unavailable'))
                rows.append(dict(role=role, **security))
            self.process_fields = dict(process_security=rows, voice_inventory_status=inventory,
                                       voice_inventory_truncated=len(candidates) > 32,
                                       process_sample_monotonic_ms=int(now * 1000),
                                       process_refresh_interval_ms=5000,
                                       permission_failure_cause='not_inferred')
        try:
            clipboard = self.clipboard()
        except Exception:
            clipboard = dict(status='query_failed', sequence=None)
        sequence = clipboard.get('sequence')
        owner_identity = (sequence, clipboard.get('owner_pid'))
        if owner_identity != self.clipboard_owner_identity:
            self.clipboard_owner_identity = owner_identity
            self.clipboard_owner_fields = {}
            owner_pid = clipboard.get('owner_pid')
            if type(owner_pid) is int and owner_pid > 0:
                from .voice_interaction_diagnostics_windows import _process_basename
                try:
                    name, status = _process_basename(owner_pid)
                except Exception:
                    name, status = '', 'query_failed'
                self.clipboard_owner_fields = dict(owner_executable=name, owner_query_status=status)
        previous = self.clipboard_sequence
        # Absence and sequence changes are evidence only, never proof of copying or submission.
        self.clipboard_sequence = sequence
        try:
            response_fields = self.response.sample(self.process_fields.get('process_security', []))
        except Exception:
            response_fields = dict(response_status='query_failed')
        return dict(self.process_fields, **response_fields, clipboard=dict(clipboard, **self.clipboard_owner_fields,
                    changed_since_previous_sample=None if previous is None or sequence is None else sequence != previous,
                    content_observed=False, change_origin='unknown'))
