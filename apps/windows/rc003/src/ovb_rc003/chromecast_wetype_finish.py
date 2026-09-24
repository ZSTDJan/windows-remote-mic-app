"""One normal finish for a Google-owned WeType toggle capture, never a toggle.

Private message ABI is pinned to inspected binaries. Unknown versions decline.
No text, process-memory reads, foreground changes or key injection are used.
"""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
from dataclasses import dataclass
import hashlib
from pathlib import Path
import time

from .chromecast_host_activity import read_wetype_capture
from .chromecast_pipe_windows import inspect_peer, _bind

_VERIFIED = frozenset({
    "7aae1bd693bd1e94fd2da9ccf577c8c92bfa88d87b719df3b9330fa81d19c292",  # 2.1.4.6: live + static
    "99ee9c3b517275c3be1ac6bc941a70d78ee7bc1139dba1bf9df9275d7de4d1ab",  # 2.1.4.10: static
})
STOP_WINDOW_SECONDS = 1.0
BIND_WINDOW_SECONDS = 1.0
BIND_POLL_SECONDS = .25
# In both verified binaries, StatusBarWnd and ConfigMenuWindow also carry
# WeTypeVoiceOwnUi. Only VoiceBridge's Flutter window is a finish target;
# the ordinary settings window shares this class but does not have the mark.
_VOICE_WINDOW_CLASS = "wetype.flutter.setting"


@dataclass(frozen=True)
class Target:
    identity: tuple
    peer: object
    hwnd: int
    image_stamp: tuple


def _api():
    u = C.WinDLL("user32", use_last_error=True)
    callback = C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
    _bind(u, "EnumWindows", W.BOOL, callback, W.LPARAM)
    _bind(u, "IsWindowVisible", W.BOOL, W.HWND)
    _bind(u, "GetWindowThreadProcessId", W.DWORD, W.HWND, C.POINTER(W.DWORD))
    _bind(u, "GetPropW", W.HANDLE, W.HWND, W.LPCWSTR)
    _bind(u, "GetClassNameW", C.c_int, W.HWND, W.LPWSTR, C.c_int)
    _bind(u, "SendMessageTimeoutW", C.c_ssize_t, W.HWND, W.UINT, W.WPARAM,
          W.LPARAM, W.UINT, W.UINT, C.POINTER(C.c_size_t))
    return u, callback


def _windows(pid):
    u, callback = _api()
    found = []
    class_unavailable = False

    @callback
    def visit(hwnd, _):
        nonlocal class_unavailable
        owner = W.DWORD()
        u.GetWindowThreadProcessId(hwnd, C.byref(owner))
        if (owner.value == pid and u.IsWindowVisible(hwnd)
                and u.GetPropW(hwnd, "WeTypeVoiceOwnUi") == 1):
            name = C.create_unicode_buffer(256)
            length = u.GetClassNameW(hwnd, name, len(name))
            if length <= 0 or length >= len(name) - 1:
                class_unavailable = True
                return False
            if name.value == _VOICE_WINDOW_CLASS:
                found.append(int(hwnd))
        return True

    if not u.EnumWindows(visit, 0) or class_unavailable:
        raise OSError("window enumeration failed")
    return tuple(found)


def _active():
    return {s.identity for s in read_wetype_capture() if s.state == 1}


def _stamp(path):
    s = path.stat()
    return s.st_size, s.st_mtime_ns


def bind(identity, *, expected_peer=None, expected_stamp=None):
    """Verify one target; a delayed window must retain the original owner."""
    if identity is None:
        return None, "capture_unbound"
    try:
        peer = inspect_peer(identity[2])
        if expected_peer is not None and peer != expected_peer:
            return None, "process_changed"
        path = Path(peer.image)
        if path.name.casefold() != "wetype_update.exe":
            return None, "process_unverified"
        before = _stamp(path)
        if expected_stamp is not None and before != expected_stamp:
            return None, "process_changed"
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest not in _VERIFIED or _stamp(path) != before:
            return None, "version_unverified"
        # Check capture continuity even when the window is missing, so a gap
        # cannot be mistaken for a reason to keep retrying.
        if _active() != {identity}:
            return None, "capture_mismatch"
        windows = _windows(peer.pid)
        if not windows:
            return None, "voice_window_missing"
        if len(windows) != 1:
            return None, "voice_window_ambiguous"
        if _active() != {identity}:
            return None, "capture_mismatch"
        if inspect_peer(peer.pid) != peer:
            return None, "process_changed"
        return Target(identity, peer, windows[0], before), "bound"
    except Exception:
        return None, "unavailable"


class TargetBinding:
    """Non-blocking window discovery for one uninterrupted capture only."""

    def __init__(self):
        self.target = None
        self.identity = None
        self.peer = None
        self.image_stamp = None
        self.deadline = None
        self.next_poll = 0.0
        self.done = False

    def poll(self, identity, state, now):
        if self.deadline is not None and (identity != self.identity or state != "tracking"):
            was_live = self.target is not None or not self.done
            self.take_target()
            return "capture_changed" if was_live else None
        if self.done or state != "tracking":
            return None
        if self.deadline is None:
            self.identity = identity
            self.deadline = now + BIND_WINDOW_SECONDS
            try:
                self.peer = inspect_peer(identity[2])
                self.image_stamp = _stamp(Path(self.peer.image))
            except Exception:
                self.done = True
                return "unavailable"
        if now >= self.deadline:
            self.done = True
            return "window_wait_expired"
        if now < self.next_poll:
            return None
        self.next_poll = now + BIND_POLL_SECONDS
        self.target, outcome = bind(
            identity, expected_peer=self.peer, expected_stamp=self.image_stamp)
        # Ambiguity, unknown identity/version and read failures are terminal.
        # Only a not-yet-visible window permits another bounded read.
        self.done = outcome != "voice_window_missing"
        return outcome

    def take_target(self):
        target, self.target = self.target, None
        self.done = True
        return target


def _send(hwnd):
    u, _ = _api()
    result = C.c_size_t()
    C.set_last_error(0)
    # WM_USER+100/event 8: force=false, hands_free_esc_stop, direct delivery.
    # This is not Esc injection, cancellation, event 3 (toggle), or WM_CLOSE.
    sent = u.SendMessageTimeoutW(hwnd, 0x464, 8, 0, 0x23, 100, C.byref(result))
    return bool(sent), C.get_last_error()


def finish(target, *, deadline, cancelled):
    """Return request and observation separately. Never retry an uncertain send."""
    if target is None:
        return "unbound", "unobserved", 0
    sent = False
    error = 0
    try:
        if cancelled() or time.monotonic() >= deadline:
            return "expired_or_cancelled", "unobserved", 0
        if (inspect_peer(target.peer.pid) != target.peer
                or _stamp(Path(target.peer.image)) != target.image_stamp):
            return "process_changed", "unobserved", 0
        if _windows(target.peer.pid) != (target.hwnd,) or _active() != {target.identity}:
            return "target_changed", "unobserved", 0
        if cancelled() or time.monotonic() >= deadline:
            return "expired_or_cancelled", "unobserved", 0
        sent, error = _send(target.hwnd)
        if not sent:
            return "send_unconfirmed", "unobserved", error
        # Observation only: no second message even if the host remains active.
        until = min(deadline, time.monotonic() + .35)
        while True:
            if target.identity not in _active():
                return "requested", "capture_ended", error
            if cancelled() or time.monotonic() >= until:
                return "requested", "capture_still_active", error
            time.sleep(.05)
    except Exception:
        return "requested" if sent else "unavailable", "unobserved", error
