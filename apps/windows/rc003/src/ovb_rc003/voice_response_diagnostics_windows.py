"""Read-only voice-host evidence owned by the bounded diagnostic timeline.

Window metadata is not bubble recognition. Privacy history is not a live audio
session or a binding to the current PID. Neither changes voice-control policy.
No titles, clipboard contents, recognition text, audio or registry paths leave
this module. Only explicitly identified voice-process names/PIDs are queried.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from pathlib import PureWindowsPath
import sys
import time


def window_activity(processes: dict[int, str]) -> dict:
    result = dict(status="unavailable", windows=[], complete=False,
                  bubble_confirmation="unknown")
    if sys.platform != "win32":
        return dict(result, status="unsupported_platform")
    if not processes:
        return dict(result, status="no_verified_processes")
    try:
        user = ctypes.WinDLL("user32", use_last_error=True)
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        for name, args, restype in (
            ("EnumWindows", (callback_type, wintypes.LPARAM), wintypes.BOOL),
            ("GetWindowThreadProcessId", (wintypes.HWND, ctypes.POINTER(wintypes.DWORD)), wintypes.DWORD),
            ("GetClassNameW", (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int), ctypes.c_int),
            ("IsWindowVisible", (wintypes.HWND,), wintypes.BOOL),
            ("IsIconic", (wintypes.HWND,), wintypes.BOOL),
            ("GetWindowRect", (wintypes.HWND, ctypes.POINTER(wintypes.RECT)), wintypes.BOOL),
        ):
            function = getattr(user, name)
            function.argtypes, function.restype = args, restype
        try:
            dwm = ctypes.WinDLL("dwmapi")
            dwm.DwmGetWindowAttribute.argtypes = (wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD)
            dwm.DwmGetWindowAttribute.restype = ctypes.c_long
        except OSError:
            dwm = None
        windows, hidden, scanned, stopped, failed = [], {}, 0, False, False
        limited = False
        deadline = time.monotonic() + 0.05

        def visit(hwnd, _):
            nonlocal scanned, stopped, failed, limited
            try:
                scanned += 1
                if scanned > 2048 or time.monotonic() > deadline:
                    stopped = True
                    return False
                pid = wintypes.DWORD()
                if not user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)):
                    return True
                if pid.value not in processes:
                    return True
                visible = bool(user.IsWindowVisible(hwnd))
                provider = processes[pid.value]
                if visible and sum(row['provider'] == provider for row in windows) >= 8:
                    limited = True
                    return True
                name, rect = ctypes.create_unicode_buffer(129), wintypes.RECT()
                copied = user.GetClassNameW(hwnd, name, len(name))
                if not visible:
                    key = (provider, name.value if 0 < copied < 128 else '')
                    if key in hidden or len(hidden) < 32:
                        hidden[key] = hidden.get(key, 0) + 1
                    else:
                        limited = True
                    return True
                rect_ok = bool(user.GetWindowRect(hwnd, ctypes.byref(rect)))
                cloaked, cloak_value = None, wintypes.DWORD()
                if dwm is not None and dwm.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloak_value), ctypes.sizeof(cloak_value)) >= 0:
                    cloaked = bool(cloak_value.value)
                # Recheck ownership after sampling a window that can disappear/reuse.
                checked = wintypes.DWORD()
                if not user.GetWindowThreadProcessId(hwnd, ctypes.byref(checked)) or checked.value != pid.value:
                    failed = True
                    return True
                windows.append(dict(hwnd=int(hwnd), pid=int(pid.value), provider=provider,
                    class_name=name.value if 0 < copied < 128 else "", class_captured=0 < copied < 128,
                    visible_style=visible, minimized=bool(user.IsIconic(hwnd)),
                    cloaked=cloaked, width=int(rect.right-rect.left) if rect_ok else None,
                    height=int(rect.bottom-rect.top) if rect_ok else None))
                return True
            except Exception:
                failed = True
                return False

        ok = bool(user.EnumWindows(callback_type(visit), 0))
        return dict(result, windows=sorted(windows, key=lambda row: row['hwnd']),
                    hidden_classes=[dict(provider=k[0], class_name=k[1], count=v) for k,v in sorted(hidden.items())],
                    status="captured" if ok and not failed and not limited else "partial" if windows or hidden or stopped or limited else "query_failed",
                    complete=ok and not stopped and not failed and not limited, truncated=stopped or limited)
    except Exception:
        return dict(result, status="query_failed")


def microphone_history(names: dict[str, str]) -> dict:
    """Bounded Windows privacy-history hints, explicitly not live mic confirmation."""
    result = dict(status="unavailable", source="windows_privacy_history", entries=[],
                  process_binding="executable_name_only", live_capture_confirmation="unknown")
    if sys.platform != "win32":
        return dict(result, status="unsupported_platform")
    if not names:
        return dict(result, status="no_verified_processes")
    try:
        import winreg
        location = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone\NonPackaged"
        latest, complete, partial = {}, False, False
        deadline = time.monotonic() + 0.05
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, location, 0, winreg.KEY_READ) as root:
            for index in range(512):
                if time.monotonic() > deadline:
                    partial = True
                    break
                try:
                    child = winreg.EnumKey(root, index)
                except OSError as exc:
                    complete = getattr(exc, 'winerror', None) == 259
                    partial = not complete
                    break
                name = PureWindowsPath(child.replace('#', '\\')).name.casefold()
                if name not in names:
                    continue
                values = {}
                try:
                    with winreg.OpenKey(root, child, 0, winreg.KEY_READ) as key:
                        for field, value_name in (("last_start", "LastUsedTimeStart"), ("last_stop", "LastUsedTimeStop")):
                            try:
                                value, _ = winreg.QueryValueEx(key, value_name)
                                values[field] = value if type(value) is int and 0 <= value < 2**64 else None
                            except OSError:
                                values[field] = None
                except OSError:
                    partial = True
                    continue
                row = dict(provider=names[name], executable=name, **values)
                if name not in latest or (values.get('last_start') or 0) > (latest[name].get('last_start') or 0):
                    latest[name] = row
        return dict(result, status="captured" if complete and not partial else "partial",
                    complete=complete, entries=[latest[n] for n in sorted(latest)])
    except FileNotFoundError:
        return dict(result, status="history_unavailable")
    except Exception:
        return dict(result, status="query_failed")


class ResponseSampler:
    def __init__(self, *, windows=None, microphone=None, clock=None):
        self.windows = windows or window_activity
        self.microphone = microphone or microphone_history
        self.clock = clock or time.monotonic
        self.next_sample = 0.0
        self.identity = None
        self.fields = {}

    def sample(self, security: list[dict]) -> dict:
        # Candidate enumeration alone is insufficient: require a verified image name.
        rows = [r for r in security if r.get('role') in {'sogou', 'wetype', 'doubao_ime'}
                and r.get('status') in {'captured', 'partial', 'token_open_failed'} and r.get('executable')]
        identity = tuple((r['role'], r['pid'], r.get('created_filetime'), r['executable']) for r in rows)
        now = self.clock()
        if identity != self.identity or now >= self.next_sample:
            self.identity, self.next_sample = identity, now + 0.5
            processes = {r['pid']: r['role'] for r in rows}
            names = {r['executable'].casefold(): r['role'] for r in rows}
            fields = {}
            for field, function, argument in (("voice_windows", self.windows, processes),
                    ("voice_microphone_history", self.microphone, names)):
                try:
                    fields[field] = function(argument)
                except Exception:
                    fields[field] = dict(status="query_failed")
            self.fields = dict(fields, response_sample_monotonic_ms=int(now * 1000),
                               response_sampling_interval_ms=500)
        return self.fields
