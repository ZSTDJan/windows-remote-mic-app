"""Own Remote Mic's process-wide low-level keyboard hook.

Some voice-input hosts ignore keyboard events that still carry Windows'
injected flags.  Remote Mic marks its legacy ``keybd_event`` voice edges and
this hook removes those flags before forwarding the event.  The same hook also
correlates exact RC003 direction edges reported by the device-scoped HID tap,
so a translated Windows arrow cannot leak beside the mapped arrow action.
"""

from __future__ import annotations

import ctypes
import os
import sys
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass
from typing import Callable, Optional

from . import diagnostic_trace


WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
LLKHF_INJECTED = 0x00000010
LLKHF_LOWER_IL_INJECTED = 0x00000002
LLKHF_EXTENDED = 0x00000001
PM_NOREMOVE = 0x0000
PM_REMOVE = 0x0001
WM_QUIT = 0x0012
VK_RMENU = 0xA5
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_LSHIFT = 0xA0
VK_RSHIFT = 0xA1
VK_LCONTROL = 0xA2
VK_RCONTROL = 0xA3
VK_LMENU = 0xA4
VK_LWIN = 0x5B
VK_RWIN = 0x5C

# Pointer-sized Remote Mic marker plus a per-edge ticket. Only win32_input's
# marked voice path emits it; the hook clears it before forwarding the event.
if ctypes.sizeof(ctypes.c_size_t) >= 8:
    _VOICE_EVENT_MARKER_PREFIX = 0x524D494300000000
    _VOICE_EVENT_MARKER_PREFIX_MASK = 0xFFFFFFFF00000000
    _VOICE_EVENT_MARKER_SEQUENCE_MASK = 0x00000000FFFFFFFF
else:
    _VOICE_EVENT_MARKER_PREFIX = 0x524D0000
    _VOICE_EVENT_MARKER_PREFIX_MASK = 0xFFFF0000
    _VOICE_EVENT_MARKER_SEQUENCE_MASK = 0x0000FFFF
VOICE_EVENT_EXTRA_INFO = _VOICE_EVENT_MARKER_PREFIX | 1
VOICE_EVENT_MARKER_PREFIX = _VOICE_EVENT_MARKER_PREFIX
VOICE_EVENT_MARKER_PREFIX_MASK = _VOICE_EVENT_MARKER_PREFIX_MASK
VOICE_EVENT_MARKER_SEQUENCE_MASK = _VOICE_EVENT_MARKER_SEQUENCE_MASK
_TRACKING_LOST_CALLBACK_TIMEOUT_SECONDS = 3.0
_TRACKING_LOST_PUMP_INTERVAL_SECONDS = 0.005
_RC003_DIRECTION_EDGE_LIFETIME_SECONDS = 0.180
_RC003_DIRECTION_HOLD_LIFETIME_SECONDS = 10.0
_RC003_DIRECTION_RELEASE_LIFETIME_SECONDS = 0.180
_RC003_DIRECTION_VKS = frozenset({0x25, 0x26, 0x27, 0x28})
_TRACE_VK_TO_BUTTON = {
    0x25: "left",
    0x26: "up",
    0x27: "right",
    0x28: "down",
    0x5D: "menu",
    0xC0: "tv",
}

_diagnostic_trace: Optional[diagnostic_trace.DiagnosticTrace] = None


def set_diagnostic_trace(trace: Optional[diagnostic_trace.DiagnosticTrace]) -> None:
    global _diagnostic_trace
    _diagnostic_trace = trace

_PHYSICAL_KEY_STATE_LOCK = threading.Lock()
_PHYSICAL_KEYS_DOWN: set[int] = set()
_PHYSICAL_KEYS_TOUCHED: set[int] = set()
_PHYSICAL_TRACKER_ACTIVE = False
_PHYSICAL_TRACKER_DRAINING = False
_PHYSICAL_TRACKER_GENERATION = 0
_GENERIC_KEY_VARIANTS = {
    VK_SHIFT: (VK_LSHIFT, VK_RSHIFT),
    VK_CONTROL: (VK_LCONTROL, VK_RCONTROL),
    VK_MENU: (VK_LMENU, VK_RMENU),
}
_TRACKED_PHYSICAL_KEYS = frozenset(
    {
        VK_LSHIFT,
        VK_RSHIFT,
        VK_LCONTROL,
        VK_RCONTROL,
        VK_LMENU,
        VK_RMENU,
        VK_LWIN,
        VK_RWIN,
    }
)


class VoiceEventConfirmation:
    def __init__(
        self,
        marker: int,
        generation: int,
        key_up: bool,
    ) -> None:
        self.marker = int(marker)
        self.generation = int(generation)
        self.key_up = bool(key_up)
        self.event = threading.Event()
        self.confirmed = False


@dataclass(frozen=True)
class _ArmedDirectionEdge:
    vk_code: int
    scan_code: int
    extended: bool
    expires_at: float


_VOICE_CONFIRMATION_LOCK = threading.Lock()
_VOICE_CONFIRMATION_SEQUENCE = 1
_VOICE_CONFIRMATIONS: dict[int, VoiceEventConfirmation] = {}


class VoiceKeyPhysicalizerUnavailableError(Exception):
    """Raised when the Windows low-level keyboard hook cannot be started."""


def _require_windows() -> None:
    if sys.platform != "win32":
        raise VoiceKeyPhysicalizerUnavailableError(
            "voice key physicalizer is only available on Windows"
        )


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


def physicalize_injected_event(
    event: KBDLLHOOKSTRUCT,
    key_up: bool,
) -> bool:
    """Consume one current ticket and clear its right-Alt injection metadata."""

    if not (int(event.flags) & LLKHF_INJECTED):
        return False
    if int(event.vkCode) != VK_RMENU:
        return False
    marker = int(event.dwExtraInfo)
    if not _is_voice_event_marker(marker):
        return False
    with _PHYSICAL_KEY_STATE_LOCK:
        if not _PHYSICAL_TRACKER_ACTIVE:
            return False
        generation = _PHYSICAL_TRACKER_GENERATION
        with _VOICE_CONFIRMATION_LOCK:
            confirmation = _VOICE_CONFIRMATIONS.get(marker)
            if (
                confirmation is None
                or confirmation.generation != generation
                or confirmation.key_up != bool(key_up)
            ):
                return False
            _VOICE_CONFIRMATIONS.pop(marker, None)
            event.flags = int(event.flags) & ~(
                LLKHF_INJECTED | LLKHF_LOWER_IL_INJECTED
            )
            event.dwExtraInfo = 0
            confirmation.confirmed = True
            confirmation.event.set()
            return True


def _is_voice_event_marker(value: int) -> bool:
    normalized = int(value)
    return (
        normalized & _VOICE_EVENT_MARKER_PREFIX_MASK
    ) == _VOICE_EVENT_MARKER_PREFIX and bool(
        normalized & _VOICE_EVENT_MARKER_SEQUENCE_MASK
    )


def begin_marked_voice_event(key_up: bool) -> VoiceEventConfirmation:
    """Reserve one exact hook acknowledgement for a marked right-Alt edge."""

    global _VOICE_CONFIRMATION_SEQUENCE

    with _PHYSICAL_KEY_STATE_LOCK:
        if not _PHYSICAL_TRACKER_ACTIVE:
            raise VoiceKeyPhysicalizerUnavailableError(
                "voice key physicalizer is not running"
            )
        if _PHYSICAL_TRACKER_DRAINING and not key_up:
            raise VoiceKeyPhysicalizerUnavailableError(
                "voice key physicalizer is draining and rejects new key-downs"
            )
        generation = _PHYSICAL_TRACKER_GENERATION
    with _VOICE_CONFIRMATION_LOCK:
        sequence = _VOICE_CONFIRMATION_SEQUENCE
        while True:
            sequence = (sequence + 1) & _VOICE_EVENT_MARKER_SEQUENCE_MASK
            if sequence == 0:
                continue
            marker = _VOICE_EVENT_MARKER_PREFIX | sequence
            if marker not in _VOICE_CONFIRMATIONS:
                break
        _VOICE_CONFIRMATION_SEQUENCE = sequence
        confirmation = VoiceEventConfirmation(marker, generation, key_up)
        _VOICE_CONFIRMATIONS[marker] = confirmation
        return confirmation


def cancel_marked_voice_event(confirmation: VoiceEventConfirmation) -> None:
    with _VOICE_CONFIRMATION_LOCK:
        current = _VOICE_CONFIRMATIONS.get(confirmation.marker)
        if current is confirmation:
            _VOICE_CONFIRMATIONS.pop(confirmation.marker, None)
            confirmation.event.set()


def wait_for_marked_voice_event(
    confirmation: VoiceEventConfirmation,
    timeout: float,
) -> bool:
    confirmation.event.wait(timeout=max(0.0, float(timeout)))
    with _VOICE_CONFIRMATION_LOCK:
        current = _VOICE_CONFIRMATIONS.get(confirmation.marker)
        if current is confirmation:
            _VOICE_CONFIRMATIONS.pop(confirmation.marker, None)
        return bool(confirmation.confirmed)


def _cancel_voice_confirmations(generation: int) -> None:
    with _VOICE_CONFIRMATION_LOCK:
        cancelled = [
            confirmation
            for confirmation in _VOICE_CONFIRMATIONS.values()
            if confirmation.generation == int(generation)
        ]
        for confirmation in cancelled:
            _VOICE_CONFIRMATIONS.pop(confirmation.marker, None)
            confirmation.event.set()


def record_physical_key_event(
    event: KBDLLHOOKSTRUCT,
    message: int,
) -> bool:
    """Track only real keyboard edges; injected Remote Mic edges stay out."""

    if int(event.flags) & LLKHF_INJECTED:
        return False
    vk_code = _normalize_modifier_vk(event)
    if vk_code is None:
        return False
    with _PHYSICAL_KEY_STATE_LOCK:
        if not _PHYSICAL_TRACKER_ACTIVE:
            return False
        _PHYSICAL_KEYS_TOUCHED.add(vk_code)
        if int(message) in (WM_KEYDOWN, WM_SYSKEYDOWN):
            _PHYSICAL_KEYS_DOWN.add(vk_code)
        elif int(message) in (WM_KEYUP, WM_SYSKEYUP):
            _PHYSICAL_KEYS_DOWN.discard(vk_code)
        else:
            return False
    return True


def _normalize_modifier_vk(event: KBDLLHOOKSTRUCT) -> Optional[int]:
    """Return one side-specific modifier VK, or ``None`` for ordinary keys."""

    vk_code = int(event.vkCode)
    if vk_code == VK_SHIFT:
        return VK_RSHIFT if int(event.scanCode) == 0x36 else VK_LSHIFT
    if vk_code == VK_CONTROL:
        return VK_RCONTROL if int(event.flags) & LLKHF_EXTENDED else VK_LCONTROL
    if vk_code == VK_MENU:
        return VK_RMENU if int(event.flags) & LLKHF_EXTENDED else VK_LMENU
    if vk_code in _TRACKED_PHYSICAL_KEYS:
        return vk_code
    return None


def _real_async_key_is_down(vk_code: int) -> bool:
    _require_windows()
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
    user32.GetAsyncKeyState.restype = ctypes.c_short
    return bool(user32.GetAsyncKeyState(int(vk_code)) & 0x8000)


def physical_key_is_down(
    vk_code: int,
) -> bool:
    """Return only physical state confirmed by the live low-level hook.

    ``GetAsyncKeyState`` cannot distinguish a physical hold from a key that
    Remote Mic itself injected.  Falling back to it while releasing a shortcut
    can therefore suppress the matching key-up and leave the host key stuck.
    When the hook is unavailable, release paths deliberately report no
    confirmed physical owner so the injected key-up is still sent.
    """

    normalized = int(vk_code)
    variants = _GENERIC_KEY_VARIANTS.get(normalized)
    if variants is None:
        if normalized not in _TRACKED_PHYSICAL_KEYS:
            return False
        variants = (normalized,)
    with _PHYSICAL_KEY_STATE_LOCK:
        if _PHYSICAL_TRACKER_ACTIVE:
            return any(variant in _PHYSICAL_KEYS_DOWN for variant in variants)
    return False


def physical_key_is_down_before_injection(
    vk_code: int,
    *,
    _query: Optional[Callable[[int], bool]] = None,
) -> bool:
    """Check physical ownership before Remote Mic injects a new key-down.

    Before submission there is no self-injected state to confuse the fallback,
    so ``GetAsyncKeyState`` also guards keys whose event has not reached the
    hook yet. It must not be used by the matching release path.
    """

    normalized = int(vk_code)
    variants = _GENERIC_KEY_VARIANTS.get(normalized)
    if variants is None:
        if normalized not in _TRACKED_PHYSICAL_KEYS:
            return False
        variants = (normalized,)
    with _PHYSICAL_KEY_STATE_LOCK:
        if _PHYSICAL_TRACKER_ACTIVE and any(
            variant in _PHYSICAL_KEYS_DOWN for variant in variants
        ):
            return True
    query = _query or _real_async_key_is_down
    return any(bool(query(variant)) for variant in variants)


def physical_key_tracking_available(vk_code: int) -> bool:
    """Return whether the hook can distinguish this modifier after injection."""

    normalized = int(vk_code)
    variants = _GENERIC_KEY_VARIANTS.get(normalized)
    if variants is None:
        if normalized not in _TRACKED_PHYSICAL_KEYS:
            return False
        variants = (normalized,)
    with _PHYSICAL_KEY_STATE_LOCK:
        return bool(
            _PHYSICAL_TRACKER_ACTIVE and not _PHYSICAL_TRACKER_DRAINING
        )


def _begin_physical_tracker_drain() -> None:
    global _PHYSICAL_TRACKER_DRAINING

    with _PHYSICAL_KEY_STATE_LOCK:
        if _PHYSICAL_TRACKER_ACTIVE:
            _PHYSICAL_TRACKER_DRAINING = True


def _set_physical_tracker_active(
    active: bool,
    *,
    _query: Optional[Callable[[int], bool]] = None,
) -> None:
    global _PHYSICAL_TRACKER_ACTIVE, _PHYSICAL_TRACKER_DRAINING
    global _PHYSICAL_TRACKER_GENERATION

    with _PHYSICAL_KEY_STATE_LOCK:
        previous_generation = _PHYSICAL_TRACKER_GENERATION
        _PHYSICAL_TRACKER_GENERATION += 1
        generation = _PHYSICAL_TRACKER_GENERATION
        _PHYSICAL_KEYS_DOWN.clear()
        _PHYSICAL_KEYS_TOUCHED.clear()
        _PHYSICAL_TRACKER_ACTIVE = bool(active)
        _PHYSICAL_TRACKER_DRAINING = False
    if not active:
        _cancel_voice_confirmations(previous_generation)
        return
    query = _query or _real_async_key_is_down
    initially_down = set()
    for vk_code in _TRACKED_PHYSICAL_KEYS:
        try:
            if query(vk_code):
                initially_down.add(vk_code)
        except Exception:
            continue
    with _PHYSICAL_KEY_STATE_LOCK:
        if (
            _PHYSICAL_TRACKER_ACTIVE
            and _PHYSICAL_TRACKER_GENERATION == generation
        ):
            _PHYSICAL_KEYS_DOWN.update(
                initially_down.difference(_PHYSICAL_KEYS_TOUCHED)
            )


class VoiceKeyPhysicalizer:
    def __init__(self) -> None:
        self._thread: Optional[threading.Thread] = None
        self._ready_event = threading.Event()
        self._stop_event = threading.Event()
        self._start_error: Optional[BaseException] = None
        self._unexpected_exit_event = threading.Event()
        self._thread_id = wintypes.DWORD(0)
        self._hook = None
        self._hookproc_keepalive = None
        self._on_tracking_lost: Optional[Callable[[], None]] = None
        self._direction_edges_lock = threading.Lock()
        self._armed_direction_edges: list[_ArmedDirectionEdge] = []
        self._owned_direction_holds: dict[tuple[int, int, bool], float] = {}

    def set_tracking_lost_callback(
        self,
        callback: Optional[Callable[[], None]],
    ) -> None:
        self._on_tracking_lost = callback

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def record_rc003_direction_edge(
        self,
        vk_code: int,
        scan_code: int,
        extended: bool,
        is_pressed: bool,
    ) -> bool:
        """Arm one exact RC003 direction edge before its mapped action runs."""

        identity = (int(vk_code), int(scan_code), bool(extended))
        if identity[0] not in _RC003_DIRECTION_VKS:
            return False
        with self._direction_edges_lock:
            now = time.monotonic()
            self._purge_direction_edges_locked(now)
            if is_pressed:
                self._armed_direction_edges.append(
                    _ArmedDirectionEdge(
                        identity[0],
                        identity[1],
                        identity[2],
                        now + _RC003_DIRECTION_EDGE_LIFETIME_SECONDS,
                    )
                )
                if len(self._armed_direction_edges) > 32:
                    self._armed_direction_edges = self._armed_direction_edges[-32:]
                return True

            pending_down = any(
                (edge.vk_code, edge.scan_code, edge.extended) == identity
                for edge in self._armed_direction_edges
            )
            if identity in self._owned_direction_holds:
                self._owned_direction_holds[identity] = (
                    now + _RC003_DIRECTION_RELEASE_LIFETIME_SECONDS
                )
                return True
            if pending_down:
                self._armed_direction_edges = [
                    _ArmedDirectionEdge(
                        edge.vk_code,
                        edge.scan_code,
                        edge.extended,
                        (
                            now + _RC003_DIRECTION_RELEASE_LIFETIME_SECONDS
                            if (
                                edge.vk_code,
                                edge.scan_code,
                                edge.extended,
                            )
                            == identity
                            else edge.expires_at
                        ),
                    )
                    for edge in self._armed_direction_edges
                ]
                return True
            return False

    def consume_rc003_direction_event(
        self,
        event: KBDLLHOOKSTRUCT,
        is_pressed: bool,
    ) -> bool:
        """Consume only a physical arrow correlated with the RC003 HID tap."""

        if int(event.flags) & LLKHF_INJECTED:
            return False
        identity = (
            int(event.vkCode),
            int(event.scanCode),
            bool(int(event.flags) & LLKHF_EXTENDED),
        )
        if identity[0] not in _RC003_DIRECTION_VKS:
            return False
        with self._direction_edges_lock:
            now = time.monotonic()
            self._purge_direction_edges_locked(now)
            if is_pressed:
                for index, edge in enumerate(self._armed_direction_edges):
                    if (
                        edge.vk_code,
                        edge.scan_code,
                        edge.extended,
                    ) == identity:
                        del self._armed_direction_edges[index]
                        self._owned_direction_holds[identity] = (
                            now + _RC003_DIRECTION_HOLD_LIFETIME_SECONDS
                        )
                        return True
                if identity in self._owned_direction_holds:
                    self._owned_direction_holds[identity] = (
                        now + _RC003_DIRECTION_HOLD_LIFETIME_SECONDS
                    )
                    return True
                return False

            if identity not in self._owned_direction_holds:
                for index, edge in enumerate(self._armed_direction_edges):
                    if (
                        edge.vk_code,
                        edge.scan_code,
                        edge.extended,
                    ) == identity:
                        del self._armed_direction_edges[index]
                        break
                return False
            self._owned_direction_holds.pop(identity, None)
            return True

    def clear_rc003_direction_edges(self) -> None:
        with self._direction_edges_lock:
            self._armed_direction_edges.clear()
            self._owned_direction_holds.clear()

    def _purge_direction_edges_locked(self, now: float) -> None:
        self._armed_direction_edges = [
            edge for edge in self._armed_direction_edges if edge.expires_at > now
        ]
        self._owned_direction_holds = {
            identity: expires_at
            for identity, expires_at in self._owned_direction_holds.items()
            if expires_at > now
        }

    def start(
        self,
        *,
        start_timeout: float = 5.0,
        _run_target: Optional[Callable[[], None]] = None,
    ) -> None:
        if self.is_running:
            raise VoiceKeyPhysicalizerUnavailableError(
                "voice key physicalizer is already running; call stop() first"
            )
        if _run_target is None:
            if os.environ.get("RC003_DISABLE_LIVE_INPUT") == "1":
                raise VoiceKeyPhysicalizerUnavailableError(
                    "live Windows keyboard hooks are disabled for this process"
                )
            _require_windows()
        self._ready_event.clear()
        self._stop_event.clear()
        self._unexpected_exit_event.clear()
        self._start_error = None
        self.clear_rc003_direction_edges()
        self._thread = threading.Thread(
            target=_run_target or self._run,
            name="remote-mic-voice-key-physicalizer",
            daemon=True,
        )
        self._thread.start()
        if not self._ready_event.wait(timeout=start_timeout):
            self._stop_event.set()
            self._join_thread_or_report_alive()
            raise VoiceKeyPhysicalizerUnavailableError(
                f"voice key physicalizer did not become ready within {start_timeout}s"
            )
        if self._start_error is not None:
            error = self._start_error
            self._stop_event.set()
            self._join_thread_or_report_alive()
            raise error
        if self._unexpected_exit_event.is_set() or not self.is_running:
            self._stop_event.set()
            self._join_thread_or_report_alive()
            raise VoiceKeyPhysicalizerUnavailableError(
                "voice key physicalizer exited immediately after reporting ready"
            )

    def _join_thread_or_report_alive(self) -> bool:
        if self._thread is None:
            return True
        self._thread.join(timeout=2.0)
        if self._thread.is_alive():
            return False
        self._thread = None
        self._thread_id = wintypes.DWORD(0)
        return True

    def stop(self) -> None:
        self._stop_event.set()
        self.clear_rc003_direction_edges()
        if self._thread is None:
            return
        if sys.platform == "win32" and self._thread_id.value:
            user32 = ctypes.windll.user32  # type: ignore[attr-defined]
            user32.PostThreadMessageW.argtypes = (
                wintypes.DWORD,
                wintypes.UINT,
                wintypes.WPARAM,
                wintypes.LPARAM,
            )
            user32.PostThreadMessageW.restype = wintypes.BOOL
            user32.PostThreadMessageW(self._thread_id.value, 0x0012, 0, 0)
        if not self._join_thread_or_report_alive():
            raise VoiceKeyPhysicalizerUnavailableError(
                "voice key physicalizer thread did not stop within 2.0s"
            )

    def _run(self) -> None:
        ready_announced = False
        unexpected_exit = False
        try:
            user32 = ctypes.windll.user32  # type: ignore[attr-defined]
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

            hook_proc_type = ctypes.WINFUNCTYPE(
                ctypes.c_ssize_t,
                ctypes.c_int,
                wintypes.WPARAM,
                wintypes.LPARAM,
            )
            hookproc = hook_proc_type(self._hookproc)
            self._hookproc_keepalive = hookproc

            kernel32.GetCurrentThreadId.argtypes = ()
            kernel32.GetCurrentThreadId.restype = wintypes.DWORD
            self._thread_id = wintypes.DWORD(kernel32.GetCurrentThreadId())

            kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
            kernel32.GetModuleHandleW.restype = wintypes.HMODULE
            user32.SetWindowsHookExW.argtypes = (
                ctypes.c_int,
                hook_proc_type,
                wintypes.HINSTANCE,
                wintypes.DWORD,
            )
            user32.SetWindowsHookExW.restype = wintypes.HHOOK
            user32.UnhookWindowsHookEx.argtypes = (wintypes.HHOOK,)
            user32.UnhookWindowsHookEx.restype = wintypes.BOOL
            user32.PeekMessageW.argtypes = (
                ctypes.POINTER(wintypes.MSG),
                wintypes.HWND,
                wintypes.UINT,
                wintypes.UINT,
                wintypes.UINT,
            )
            user32.PeekMessageW.restype = wintypes.BOOL

            self._hook = user32.SetWindowsHookExW(
                WH_KEYBOARD_LL,
                hookproc,
                kernel32.GetModuleHandleW(None),
                0,
            )
            if not self._hook:
                raise VoiceKeyPhysicalizerUnavailableError(
                    "SetWindowsHookExW failed"
                )

            _set_physical_tracker_active(True)

            # Ensure the message queue exists before start() reports ready.
            # PeekMessageW requires a writable MSG pointer even when no
            # message is removed.
            queue_probe = wintypes.MSG()
            user32.PeekMessageW(
                ctypes.byref(queue_probe),
                None,
                0,
                0,
                PM_NOREMOVE,
            )
            self._ready_event.set()
            ready_announced = True

            msg = wintypes.MSG()
            user32.GetMessageW.argtypes = (
                ctypes.POINTER(wintypes.MSG),
                wintypes.HWND,
                wintypes.UINT,
                wintypes.UINT,
            )
            user32.GetMessageW.restype = ctypes.c_int
            while not self._stop_event.is_set():
                if user32.GetMessageW(ctypes.byref(msg), None, 0, 0) <= 0:
                    unexpected_exit = not self._stop_event.is_set()
                    break
        except BaseException as exc:  # noqa: BLE001 - surfaced to start()
            if not ready_announced:
                self._start_error = exc
            else:
                unexpected_exit = not self._stop_event.is_set()
            self._ready_event.set()
        finally:
            if unexpected_exit:
                self._unexpected_exit_event.set()
                _begin_physical_tracker_drain()
                self._notify_tracking_lost_while_hook_active(
                    self._build_message_pump(user32) if self._hook else None
                )
            _set_physical_tracker_active(False)
            self.clear_rc003_direction_edges()
            if self._hook:
                try:
                    ctypes.windll.user32.UnhookWindowsHookEx(self._hook)  # type: ignore[attr-defined]
                except Exception:
                    pass
                self._hook = None

    def _build_message_pump(self, user32):
        user32.TranslateMessage.argtypes = (ctypes.POINTER(wintypes.MSG),)
        user32.TranslateMessage.restype = wintypes.BOOL
        user32.DispatchMessageW.argtypes = (ctypes.POINTER(wintypes.MSG),)
        user32.DispatchMessageW.restype = ctypes.c_ssize_t

        def pump_once() -> None:
            msg = wintypes.MSG()
            while user32.PeekMessageW(
                ctypes.byref(msg),
                None,
                0,
                0,
                PM_REMOVE,
            ):
                if int(msg.message) == WM_QUIT:
                    continue
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))

        return pump_once

    def _notify_tracking_lost_while_hook_active(
        self,
        pump_once: Optional[Callable[[], None]],
    ) -> None:
        callback = self._on_tracking_lost
        if callback is None:
            return
        finished = threading.Event()

        def invoke_callback() -> None:
            try:
                callback()
            except BaseException:
                pass
            finally:
                finished.set()

        worker = threading.Thread(
            target=invoke_callback,
            name="remote-mic-voice-key-physicalizer-loss",
            daemon=True,
        )
        worker.start()
        deadline = time.monotonic() + _TRACKING_LOST_CALLBACK_TIMEOUT_SECONDS
        while not finished.is_set() and time.monotonic() < deadline:
            if pump_once is not None:
                try:
                    pump_once()
                except BaseException:
                    pump_once = None
            finished.wait(_TRACKING_LOST_PUMP_INTERVAL_SECONDS)

    def _hookproc(self, n_code, w_param, l_param):
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        user32.CallNextHookEx.argtypes = (
            wintypes.HHOOK,
            ctypes.c_int,
            wintypes.WPARAM,
            wintypes.LPARAM,
        )
        user32.CallNextHookEx.restype = ctypes.c_ssize_t
        if n_code >= 0 and int(w_param) in (
            WM_KEYDOWN,
            WM_KEYUP,
            WM_SYSKEYDOWN,
            WM_SYSKEYUP,
        ):
            event = KBDLLHOOKSTRUCT.from_address(int(l_param))
            original_flags = int(event.flags)
            original_extra_info = int(event.dwExtraInfo)
            key_up = int(w_param) in (WM_KEYUP, WM_SYSKEYUP)
            consumed = self.consume_rc003_direction_event(event, not key_up)
            physicalized = False
            if not consumed:
                record_physical_key_event(event, int(w_param))
                physicalized = physicalize_injected_event(event, key_up)
            trace = _diagnostic_trace
            if trace is not None:
                try:
                    trace.observe_submission_key(int(event.vkCode), key_up, original_flags)
                except Exception:
                    pass  # Metadata observation cannot consume or delay key delivery.
            button_id = _TRACE_VK_TO_BUTTON.get(int(event.vkCode), "")
            voice_marker = _is_voice_event_marker(original_extra_info)
            if trace is not None and (button_id or voice_marker):
                try:
                    gesture_id = (
                        trace.current_gesture(button_id) if button_id else None
                    )
                    trace.emit(
                        "low_level_hook",
                        gesture_id=gesture_id or "",
                        button_id=button_id,
                        edge="up" if key_up else "down",
                        message=int(w_param),
                        vk=int(event.vkCode),
                        scan_code=int(event.scanCode),
                        flags=original_flags,
                        extra_info=original_extra_info,
                        injected=bool(original_flags & LLKHF_INJECTED),
                        lower_integrity=bool(
                            original_flags & LLKHF_LOWER_IL_INJECTED
                        ),
                        matched=bool(gesture_id or voice_marker),
                        decision=(
                            "consume"
                            if consumed
                            else "physicalize"
                            if physicalized
                            else "pass"
                        ),
                        reason=(
                            "rc003_direction_owned"
                            if consumed
                            else "voice_marker"
                            if physicalized
                            else "no_matching_ownership"
                        ),
                    )
                except BaseException:
                    pass
            if consumed:
                return 1
            if physicalized:
                try:
                    return user32.CallNextHookEx(
                        self._hook,
                        n_code,
                        w_param,
                        int(l_param),
                    )
                finally:
                    event.flags = original_flags
                    event.dwExtraInfo = original_extra_info
        return user32.CallNextHookEx(self._hook, n_code, w_param, l_param)
