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
_RALT_STALE_OWNER_GRACE_SECONDS = 0.250
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
_PHYSICAL_KEY_REVISIONS: dict[int, int] = {}
_PHYSICAL_KEY_LAST_EDGE_AT: dict[int, float] = {}
_PHYSICAL_RALT_CALLBACKS_IN_FLIGHT = 0
_PHYSICAL_RALT_CALLBACK_REVISION = 0
_PHYSICAL_TRACKER_ACTIVE = False
_PHYSICAL_TRACKER_DRAINING = False
_PHYSICAL_TRACKER_GENERATION = 0
_PHYSICAL_TRACKER_INSTALLATION_EPOCH = 0
_PHYSICAL_TRACKER_OWNER: Optional["VoiceKeyPhysicalizer"] = None
_PHYSICAL_TRACKER_HOOK_HANDLE = 0
_PHYSICAL_TRACKER_OWNER_THREAD_ID = 0
_PHYSICAL_TRACKER_OWNER_PYTHON_IDENT = 0
_PHYSICAL_TRACKER_CALLBACK_ENTRIES = 0
_PHYSICAL_TRACKER_MARKER_CALLBACKS = 0
_PHYSICAL_TRACKER_MARKER_MATCHES = 0
_PHYSICAL_TRACKER_MARKER_MISMATCHES: dict[str, int] = {}
_PHYSICAL_TRACKER_LAST_CALLBACK_AT = 0.0
_PHYSICAL_TRACKER_LAST_MARKER_AT = 0.0
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
        installation_epoch: int,
        key_up: bool,
        callback_entries: int,
        marker_callbacks: int,
        marker_matches: int,
    ) -> None:
        self.marker = int(marker)
        self.generation = int(generation)
        self.installation_epoch = int(installation_epoch)
        self.key_up = bool(key_up)
        self.created_at = time.monotonic()
        self.callback_entries = int(callback_entries)
        self.marker_callbacks = int(marker_callbacks)
        self.marker_matches = int(marker_matches)
        self.event = threading.Event()
        self.marker_seen = False
        self.downstream_completed = False
        self.downstream_result = 0
        self.downstream_error = False
        self.cancelled = False
        self.confirmed = False


@dataclass(frozen=True)
class PhysicalizerHealthSnapshot:
    active: bool
    draining: bool
    generation: int
    installation_epoch: int
    hook_handle: int
    owner_thread_id: int
    owner_python_ident: int
    callback_entries: int
    marker_callbacks: int
    marker_matches: int
    marker_mismatches: tuple[tuple[str, int], ...]
    last_callback_age_ms: int
    last_marker_age_ms: int
    pending_confirmations: int
    receipt_generation: int = -1
    receipt_installation_epoch: int = -1
    receipt_marker: int = 0
    receipt_key_up: bool = False
    receipt_age_ms: int = -1
    receipt_callback_entry_delta: int = -1
    receipt_marker_callback_delta: int = -1
    receipt_marker_match_delta: int = -1
    owner_stack: tuple[str, ...] = ()

    @property
    def accepting_new_down(self) -> bool:
        return bool(self.active and not self.draining)

    def trace_fields(self) -> dict[str, object]:
        return {
            "tracker_active": self.active,
            "tracker_draining": self.draining,
            "tracker_generation": self.generation,
            "tracker_installation_epoch": self.installation_epoch,
            "tracker_hook_handle": self.hook_handle,
            "tracker_owner_thread_id": self.owner_thread_id,
            "tracker_callback_entries": self.callback_entries,
            "tracker_marker_callbacks": self.marker_callbacks,
            "tracker_marker_matches": self.marker_matches,
            "tracker_marker_mismatches": dict(self.marker_mismatches),
            "tracker_last_callback_age_ms": self.last_callback_age_ms,
            "tracker_last_marker_age_ms": self.last_marker_age_ms,
            "tracker_pending_confirmations": self.pending_confirmations,
            "receipt_generation": self.receipt_generation,
            "receipt_installation_epoch": self.receipt_installation_epoch,
            "receipt_marker": self.receipt_marker,
            "receipt_edge": "up" if self.receipt_key_up else "down",
            "receipt_age_ms": self.receipt_age_ms,
            "receipt_callback_entry_delta": self.receipt_callback_entry_delta,
            "receipt_marker_callback_delta": self.receipt_marker_callback_delta,
            "receipt_marker_match_delta": self.receipt_marker_match_delta,
            "tracker_owner_stack": list(self.owner_stack),
        }


@dataclass(frozen=True)
class PhysicalReleaseGuard:
    """One current, owner-free tracker state for a release observation."""

    generation: int
    installation_epoch: int
    callback_revision: int


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
) -> Optional[VoiceEventConfirmation]:
    """Claim one ticket and clear its right-Alt injection metadata.

    Matching the marker proves only that this hook saw the edge.  Completion is
    signalled separately after ``CallNextHookEx`` returns so callers cannot
    clear release ownership while downstream hooks are still processing it.
    """

    marker = int(event.dwExtraInfo)
    if not _is_voice_event_marker(marker):
        return None
    global _PHYSICAL_TRACKER_MARKER_CALLBACKS
    global _PHYSICAL_TRACKER_MARKER_MATCHES, _PHYSICAL_TRACKER_LAST_MARKER_AT

    with _PHYSICAL_KEY_STATE_LOCK:
        _PHYSICAL_TRACKER_MARKER_CALLBACKS += 1
        _PHYSICAL_TRACKER_LAST_MARKER_AT = time.monotonic()
        if not (int(event.flags) & LLKHF_INJECTED):
            _record_marker_mismatch_locked("not_injected")
            return None
        if int(event.vkCode) != VK_RMENU:
            _record_marker_mismatch_locked("wrong_key")
            return None
        if not _PHYSICAL_TRACKER_ACTIVE:
            _record_marker_mismatch_locked("inactive")
            return None
        generation = _PHYSICAL_TRACKER_GENERATION
        with _VOICE_CONFIRMATION_LOCK:
            confirmation = _VOICE_CONFIRMATIONS.get(marker)
            if confirmation is None:
                _record_marker_mismatch_locked("missing_receipt")
                return None
            if confirmation.generation != generation:
                _record_marker_mismatch_locked("stale_generation")
                return None
            if confirmation.key_up != bool(key_up):
                _record_marker_mismatch_locked("wrong_edge")
                return None
            if confirmation.marker_seen:
                _record_marker_mismatch_locked("duplicate_receipt")
                return None
            event.flags = int(event.flags) & ~(
                LLKHF_INJECTED | LLKHF_LOWER_IL_INJECTED
            )
            event.dwExtraInfo = 0
            confirmation.marker_seen = True
            _PHYSICAL_TRACKER_MARKER_MATCHES += 1
            return confirmation


def complete_marked_voice_event(
    confirmation: VoiceEventConfirmation,
    *,
    downstream_result: int = 0,
    downstream_error: bool = False,
) -> None:
    """Complete a claimed receipt after the downstream hook chain returns."""

    with _PHYSICAL_KEY_STATE_LOCK:
        current_generation = _PHYSICAL_TRACKER_GENERATION
        current_epoch = _PHYSICAL_TRACKER_INSTALLATION_EPOCH
        tracker_current = bool(
            _PHYSICAL_TRACKER_ACTIVE
            and confirmation.generation == current_generation
            and confirmation.installation_epoch == current_epoch
        )
        with _VOICE_CONFIRMATION_LOCK:
            current = _VOICE_CONFIRMATIONS.get(confirmation.marker)
            if current is not confirmation:
                return
            _VOICE_CONFIRMATIONS.pop(confirmation.marker, None)
            confirmation.downstream_completed = True
            confirmation.downstream_result = int(downstream_result)
            confirmation.downstream_error = bool(downstream_error)
            confirmation.confirmed = bool(
                tracker_current
                and confirmation.marker_seen
                and not confirmation.cancelled
                and not confirmation.downstream_error
            )
            confirmation.event.set()


def _is_voice_event_marker(value: int) -> bool:
    normalized = int(value)
    return (
        normalized & _VOICE_EVENT_MARKER_PREFIX_MASK
    ) == _VOICE_EVENT_MARKER_PREFIX and bool(
        normalized & _VOICE_EVENT_MARKER_SEQUENCE_MASK
    )


def _record_marker_mismatch_locked(reason: str) -> None:
    _PHYSICAL_TRACKER_MARKER_MISMATCHES[reason] = (
        _PHYSICAL_TRACKER_MARKER_MISMATCHES.get(reason, 0) + 1
    )


def _record_hook_callback_entry() -> None:
    global _PHYSICAL_TRACKER_CALLBACK_ENTRIES, _PHYSICAL_TRACKER_LAST_CALLBACK_AT

    with _PHYSICAL_KEY_STATE_LOCK:
        _PHYSICAL_TRACKER_CALLBACK_ENTRIES += 1
        _PHYSICAL_TRACKER_LAST_CALLBACK_AT = time.monotonic()


def _owner_stack(owner_python_ident: int) -> tuple[str, ...]:
    if not owner_python_ident:
        return ()
    try:
        frame = sys._current_frames().get(int(owner_python_ident))
    except Exception:
        return ()
    locations = []
    while frame is not None and len(locations) < 32:
        module = str(frame.f_globals.get("__name__", ""))
        locations.append(f"{module}:{frame.f_code.co_name}:{frame.f_lineno}")
        frame = frame.f_back
    return tuple(locations)


def snapshot_health(
    confirmation: Optional[VoiceEventConfirmation] = None,
    *,
    include_owner_stack: bool = False,
) -> PhysicalizerHealthSnapshot:
    """Return a bounded, immutable tracker snapshot for diagnostics."""

    now = time.monotonic()
    with _PHYSICAL_KEY_STATE_LOCK:
        active = _PHYSICAL_TRACKER_ACTIVE
        draining = _PHYSICAL_TRACKER_DRAINING
        generation = _PHYSICAL_TRACKER_GENERATION
        installation_epoch = _PHYSICAL_TRACKER_INSTALLATION_EPOCH
        hook_handle = _PHYSICAL_TRACKER_HOOK_HANDLE
        owner_thread_id = _PHYSICAL_TRACKER_OWNER_THREAD_ID
        owner_python_ident = _PHYSICAL_TRACKER_OWNER_PYTHON_IDENT
        callback_entries = _PHYSICAL_TRACKER_CALLBACK_ENTRIES
        marker_callbacks = _PHYSICAL_TRACKER_MARKER_CALLBACKS
        marker_matches = _PHYSICAL_TRACKER_MARKER_MATCHES
        marker_mismatches = tuple(
            sorted(_PHYSICAL_TRACKER_MARKER_MISMATCHES.items())
        )
        last_callback_at = _PHYSICAL_TRACKER_LAST_CALLBACK_AT
        last_marker_at = _PHYSICAL_TRACKER_LAST_MARKER_AT
        with _VOICE_CONFIRMATION_LOCK:
            pending_confirmations = sum(
                item.generation == generation
                for item in _VOICE_CONFIRMATIONS.values()
            )

    receipt_generation = -1
    receipt_installation_epoch = -1
    receipt_marker = 0
    receipt_key_up = False
    receipt_age_ms = -1
    receipt_callback_entry_delta = -1
    receipt_marker_callback_delta = -1
    receipt_marker_match_delta = -1
    if confirmation is not None:
        receipt_generation = int(confirmation.generation)
        receipt_installation_epoch = int(confirmation.installation_epoch)
        receipt_marker = int(confirmation.marker)
        receipt_key_up = bool(confirmation.key_up)
        receipt_age_ms = max(0, int((now - confirmation.created_at) * 1000))
        receipt_callback_entry_delta = max(
            0, callback_entries - confirmation.callback_entries
        )
        receipt_marker_callback_delta = max(
            0, marker_callbacks - confirmation.marker_callbacks
        )
        receipt_marker_match_delta = max(
            0, marker_matches - confirmation.marker_matches
        )

    return PhysicalizerHealthSnapshot(
        active=bool(active),
        draining=bool(draining),
        generation=int(generation),
        installation_epoch=int(installation_epoch),
        hook_handle=int(hook_handle),
        owner_thread_id=int(owner_thread_id),
        owner_python_ident=int(owner_python_ident),
        callback_entries=int(callback_entries),
        marker_callbacks=int(marker_callbacks),
        marker_matches=int(marker_matches),
        marker_mismatches=marker_mismatches,
        last_callback_age_ms=(
            max(0, int((now - last_callback_at) * 1000))
            if last_callback_at
            else -1
        ),
        last_marker_age_ms=(
            max(0, int((now - last_marker_at) * 1000))
            if last_marker_at
            else -1
        ),
        pending_confirmations=int(pending_confirmations),
        receipt_generation=receipt_generation,
        receipt_installation_epoch=receipt_installation_epoch,
        receipt_marker=receipt_marker,
        receipt_key_up=receipt_key_up,
        receipt_age_ms=receipt_age_ms,
        receipt_callback_entry_delta=receipt_callback_entry_delta,
        receipt_marker_callback_delta=receipt_marker_callback_delta,
        receipt_marker_match_delta=receipt_marker_match_delta,
        owner_stack=(
            _owner_stack(owner_python_ident) if include_owner_stack else ()
        ),
    )


def mark_required_confirmation_failed(
    confirmation: VoiceEventConfirmation,
) -> tuple[bool, PhysicalizerHealthSnapshot]:
    """Close new DOWN admission for the still-current failed transaction."""

    global _PHYSICAL_TRACKER_DRAINING

    owner: Optional[VoiceKeyPhysicalizer] = None
    applied = False
    with _PHYSICAL_KEY_STATE_LOCK:
        if (
            not confirmation.confirmed
            and _PHYSICAL_TRACKER_ACTIVE
            and confirmation.generation == _PHYSICAL_TRACKER_GENERATION
            and confirmation.installation_epoch
            == _PHYSICAL_TRACKER_INSTALLATION_EPOCH
        ):
            _PHYSICAL_TRACKER_DRAINING = True
            owner = _PHYSICAL_TRACKER_OWNER
            applied = True
    snapshot = snapshot_health(
        confirmation,
        include_owner_stack=True,
    )
    if applied and owner is not None:
        owner._mark_required_confirmation_failed(
            "required_confirmation_timeout",
            snapshot,
        )
    return applied, snapshot


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
        installation_epoch = _PHYSICAL_TRACKER_INSTALLATION_EPOCH
        callback_entries = _PHYSICAL_TRACKER_CALLBACK_ENTRIES
        marker_callbacks = _PHYSICAL_TRACKER_MARKER_CALLBACKS
        marker_matches = _PHYSICAL_TRACKER_MARKER_MATCHES
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
        confirmation = VoiceEventConfirmation(
            marker,
            generation,
            installation_epoch,
            key_up,
            callback_entries,
            marker_callbacks,
            marker_matches,
        )
        _VOICE_CONFIRMATIONS[marker] = confirmation
        return confirmation


def _pending_voice_marker_for_diagnostics(key_up: bool) -> int:
    # Observation only: a same-edge event without our marker cannot acknowledge
    # this receipt. Do not log ordinary typing or unrelated right-Alt activity.
    with _VOICE_CONFIRMATION_LOCK:
        pending = [item.marker for item in _VOICE_CONFIRMATIONS.values()
                   if item.key_up == key_up and not item.cancelled and not item.marker_seen]
    return pending[0] if len(pending) == 1 else 0


def cancel_marked_voice_event(confirmation: VoiceEventConfirmation) -> None:
    with _VOICE_CONFIRMATION_LOCK:
        current = _VOICE_CONFIRMATIONS.get(confirmation.marker)
        if current is confirmation:
            _VOICE_CONFIRMATIONS.pop(confirmation.marker, None)
            confirmation.cancelled = True
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
            confirmation.cancelled = True
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
            confirmation.cancelled = True
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
    message = int(message)
    if message not in (WM_KEYDOWN, WM_SYSKEYDOWN, WM_KEYUP, WM_SYSKEYUP):
        return False
    with _PHYSICAL_KEY_STATE_LOCK:
        if not _PHYSICAL_TRACKER_ACTIVE:
            return False
        changed_at = time.monotonic()
        _PHYSICAL_KEYS_TOUCHED.add(vk_code)
        _PHYSICAL_KEY_REVISIONS[vk_code] = _PHYSICAL_KEY_REVISIONS.get(vk_code, 0) + 1
        _PHYSICAL_KEY_LAST_EDGE_AT[vk_code] = changed_at
        if message in (WM_KEYDOWN, WM_SYSKEYDOWN):
            _PHYSICAL_KEYS_DOWN.add(vk_code)
        else:
            _PHYSICAL_KEYS_DOWN.discard(vk_code)
    return True


def _begin_physical_ralt_callback(event: KBDLLHOOKSTRUCT) -> Optional[int]:
    """Fence a real right-Alt callback until its state update completes."""

    global _PHYSICAL_RALT_CALLBACKS_IN_FLIGHT, _PHYSICAL_RALT_CALLBACK_REVISION

    if int(event.flags) & LLKHF_INJECTED or _normalize_modifier_vk(event) != VK_RMENU:
        return None
    with _PHYSICAL_KEY_STATE_LOCK:
        if not _PHYSICAL_TRACKER_ACTIVE:
            return None
        generation = _PHYSICAL_TRACKER_GENERATION
        _PHYSICAL_RALT_CALLBACKS_IN_FLIGHT += 1
        _PHYSICAL_RALT_CALLBACK_REVISION += 1
        return generation


def _end_physical_ralt_callback(generation: Optional[int]) -> None:
    global _PHYSICAL_RALT_CALLBACKS_IN_FLIGHT, _PHYSICAL_RALT_CALLBACK_REVISION

    if generation is None:
        return
    with _PHYSICAL_KEY_STATE_LOCK:
        if generation != _PHYSICAL_TRACKER_GENERATION:
            return
        if _PHYSICAL_RALT_CALLBACKS_IN_FLIGHT > 0:
            _PHYSICAL_RALT_CALLBACKS_IN_FLIGHT -= 1
        _PHYSICAL_RALT_CALLBACK_REVISION += 1


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
        active = _PHYSICAL_TRACKER_ACTIVE
        generation = _PHYSICAL_TRACKER_GENERATION
        draining = _PHYSICAL_TRACKER_DRAINING
        cached_down = tuple(
            variant for variant in variants if variant in _PHYSICAL_KEYS_DOWN
        )
        revisions = {
            variant: _PHYSICAL_KEY_REVISIONS.get(variant, 0)
            for variant in variants
        }
        changed_at = {
            variant: _PHYSICAL_KEY_LAST_EDGE_AT.get(variant, 0.0)
            for variant in cached_down
        }
        callback_revision = _PHYSICAL_RALT_CALLBACK_REVISION
        callbacks_in_flight = _PHYSICAL_RALT_CALLBACKS_IN_FLIGHT
        if active and normalized == VK_RMENU and callbacks_in_flight:
            return True
        if active and cached_down:
            # Only the marked voice right-Alt path may reconcile a missed hook
            # release. Every other modifier keeps the existing fail-closed rule.
            if normalized != VK_RMENU or draining:
                return True
            newest_edge = max(changed_at.values(), default=0.0)
            if time.monotonic() - newest_edge <= _RALT_STALE_OWNER_GRACE_SECONDS:
                return True
    query = _query or _real_async_key_is_down
    if any(bool(query(variant)) for variant in variants):
        return True
    if not active:
        return False

    reconciled = False
    age_ms = 0
    with _PHYSICAL_KEY_STATE_LOCK:
        if (
            not _PHYSICAL_TRACKER_ACTIVE
            or _PHYSICAL_TRACKER_GENERATION != generation
            or _PHYSICAL_TRACKER_DRAINING
        ):
            return True
        current_down = tuple(
            variant for variant in variants if variant in _PHYSICAL_KEYS_DOWN
        )
        current_revisions = {
            variant: _PHYSICAL_KEY_REVISIONS.get(variant, 0)
            for variant in variants
        }
        current_callback_revision = _PHYSICAL_RALT_CALLBACK_REVISION
        current_callbacks_in_flight = _PHYSICAL_RALT_CALLBACKS_IN_FLIGHT
        if normalized == VK_RMENU and current_callbacks_in_flight:
            return True
        if (
            current_down != cached_down
            or current_revisions != revisions
            or (
                normalized == VK_RMENU
                and current_callback_revision != callback_revision
            )
        ):
            return bool(current_down)
        if not cached_down:
            return False
        reconciled_at = time.monotonic()
        age_ms = max(
            0,
            int(
                1000
                * (reconciled_at - max(changed_at.values(), default=reconciled_at))
            ),
        )
        for variant in cached_down:
            _PHYSICAL_KEYS_DOWN.discard(variant)
            _PHYSICAL_KEY_REVISIONS[variant] = (
                _PHYSICAL_KEY_REVISIONS.get(variant, 0) + 1
            )
            _PHYSICAL_KEY_LAST_EDGE_AT[variant] = reconciled_at
        reconciled = True
    if reconciled:
        trace = _diagnostic_trace
        if trace is not None:
            try:
                trace.emit(
                    "input_physical_state_reconciled",
                    vk=normalized,
                    owner_count=len(cached_down),
                    age_ms=age_ms,
                    windows_down=False,
                    reason="stale_hook_owner",
                )
            except BaseException:
                pass
    return False


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


def snapshot_physical_release_guard(
    vk_code: int,
) -> Optional[PhysicalReleaseGuard]:
    """Return a stable-token candidate only while a release query is safe.

    The caller must compare snapshots taken immediately before and after its
    Windows-state observation.  A physical owner, an in-flight right-Alt edge,
    or tracker replacement makes the observation unusable.  Draining blocks
    new DOWN admission, but an existing owner's completed cleanup UP must still
    be verifiable so recovery can finish.
    """

    normalized = int(vk_code)
    variants = _GENERIC_KEY_VARIANTS.get(normalized)
    if variants is None:
        if normalized not in _TRACKED_PHYSICAL_KEYS:
            return None
        variants = (normalized,)
    with _PHYSICAL_KEY_STATE_LOCK:
        if not _PHYSICAL_TRACKER_ACTIVE:
            return None
        if any(variant in _PHYSICAL_KEYS_DOWN for variant in variants):
            return None
        if normalized == VK_RMENU and _PHYSICAL_RALT_CALLBACKS_IN_FLIGHT:
            return None
        return PhysicalReleaseGuard(
            generation=int(_PHYSICAL_TRACKER_GENERATION),
            installation_epoch=int(_PHYSICAL_TRACKER_INSTALLATION_EPOCH),
            callback_revision=int(_PHYSICAL_RALT_CALLBACK_REVISION),
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
    owner: Optional["VoiceKeyPhysicalizer"] = None,
    hook_handle: int = 0,
    owner_thread_id: int = 0,
    owner_python_ident: int = 0,
) -> int:
    global _PHYSICAL_TRACKER_ACTIVE, _PHYSICAL_TRACKER_DRAINING
    global _PHYSICAL_TRACKER_GENERATION
    global _PHYSICAL_RALT_CALLBACKS_IN_FLIGHT, _PHYSICAL_RALT_CALLBACK_REVISION
    global _PHYSICAL_TRACKER_INSTALLATION_EPOCH, _PHYSICAL_TRACKER_OWNER
    global _PHYSICAL_TRACKER_HOOK_HANDLE, _PHYSICAL_TRACKER_OWNER_THREAD_ID
    global _PHYSICAL_TRACKER_OWNER_PYTHON_IDENT
    global _PHYSICAL_TRACKER_CALLBACK_ENTRIES
    global _PHYSICAL_TRACKER_MARKER_CALLBACKS, _PHYSICAL_TRACKER_MARKER_MATCHES
    global _PHYSICAL_TRACKER_LAST_CALLBACK_AT, _PHYSICAL_TRACKER_LAST_MARKER_AT

    with _PHYSICAL_KEY_STATE_LOCK:
        previous_generation = _PHYSICAL_TRACKER_GENERATION
        _PHYSICAL_TRACKER_GENERATION += 1
        generation = _PHYSICAL_TRACKER_GENERATION
        if active:
            _PHYSICAL_TRACKER_INSTALLATION_EPOCH += 1
        _PHYSICAL_KEYS_DOWN.clear()
        _PHYSICAL_KEYS_TOUCHED.clear()
        _PHYSICAL_KEY_REVISIONS.clear()
        _PHYSICAL_KEY_LAST_EDGE_AT.clear()
        _PHYSICAL_RALT_CALLBACKS_IN_FLIGHT = 0
        _PHYSICAL_RALT_CALLBACK_REVISION = 0
        _PHYSICAL_TRACKER_ACTIVE = bool(active)
        _PHYSICAL_TRACKER_DRAINING = False
        _PHYSICAL_TRACKER_OWNER = owner if active else None
        _PHYSICAL_TRACKER_HOOK_HANDLE = int(hook_handle) if active else 0
        _PHYSICAL_TRACKER_OWNER_THREAD_ID = (
            int(owner_thread_id) if active else 0
        )
        _PHYSICAL_TRACKER_OWNER_PYTHON_IDENT = (
            int(owner_python_ident) if active else 0
        )
        _PHYSICAL_TRACKER_CALLBACK_ENTRIES = 0
        _PHYSICAL_TRACKER_MARKER_CALLBACKS = 0
        _PHYSICAL_TRACKER_MARKER_MATCHES = 0
        _PHYSICAL_TRACKER_MARKER_MISMATCHES.clear()
        _PHYSICAL_TRACKER_LAST_CALLBACK_AT = 0.0
        _PHYSICAL_TRACKER_LAST_MARKER_AT = 0.0
    if not active:
        _cancel_voice_confirmations(previous_generation)
        return generation
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
            observed_at = time.monotonic()
            for vk_code in initially_down.difference(_PHYSICAL_KEYS_TOUCHED):
                _PHYSICAL_KEYS_DOWN.add(vk_code)
                _PHYSICAL_KEY_REVISIONS[vk_code] = (
                    _PHYSICAL_KEY_REVISIONS.get(vk_code, 0) + 1
                )
                _PHYSICAL_KEY_LAST_EDGE_AT[vk_code] = observed_at
    return generation


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
        self._on_health_failure: Optional[
            Callable[[str, PhysicalizerHealthSnapshot], None]
        ] = None
        self._degraded_event = threading.Event()
        self._health_notification_lock = threading.Lock()
        self._health_notification_started = False
        self._tracker_generation = 0
        self._installation_epoch = 0
        self._direction_edges_lock = threading.Lock()
        self._armed_direction_edges: list[_ArmedDirectionEdge] = []
        self._owned_direction_holds: dict[tuple[int, int, bool], float] = {}

    def set_tracking_lost_callback(
        self,
        callback: Optional[Callable[[], None]],
    ) -> None:
        self._on_tracking_lost = callback

    def set_health_failure_callback(
        self,
        callback: Optional[
            Callable[[str, PhysicalizerHealthSnapshot], None]
        ],
    ) -> None:
        self._on_health_failure = callback

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def accepts_new_down(self) -> bool:
        if not self.is_running or self._degraded_event.is_set():
            return False
        with _PHYSICAL_KEY_STATE_LOCK:
            return bool(
                _PHYSICAL_TRACKER_ACTIVE
                and not _PHYSICAL_TRACKER_DRAINING
                and _PHYSICAL_TRACKER_OWNER is self
            )

    @property
    def tracker_generation(self) -> int:
        return int(self._tracker_generation)

    @property
    def installation_epoch(self) -> int:
        return int(self._installation_epoch)

    def _mark_required_confirmation_failed(
        self,
        reason: str,
        snapshot: PhysicalizerHealthSnapshot,
    ) -> None:
        self._degraded_event.set()
        callback = self._on_health_failure
        if callback is None:
            return
        with self._health_notification_lock:
            if self._health_notification_started:
                return
            self._health_notification_started = True

        def notify() -> None:
            try:
                callback(str(reason), snapshot)
            except BaseException:
                pass

        threading.Thread(
            target=notify,
            name="remote-mic-voice-key-physicalizer-health",
            daemon=True,
        ).start()

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
        self._degraded_event.clear()
        with self._health_notification_lock:
            self._health_notification_started = False
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

            self._tracker_generation = _set_physical_tracker_active(
                True,
                owner=self,
                hook_handle=int(self._hook or 0),
                owner_thread_id=int(self._thread_id.value),
                owner_python_ident=threading.get_ident(),
            )
            self._installation_epoch = snapshot_health().installation_epoch

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
            _record_hook_callback_entry()
            callback_generation = _begin_physical_ralt_callback(event)
            try:
                original_flags = int(event.flags)
                original_extra_info = int(event.dwExtraInfo)
                key_up = int(w_param) in (WM_KEYUP, WM_SYSKEYUP)
                trace = _diagnostic_trace
                awaiting_marker = (
                    _pending_voice_marker_for_diagnostics(key_up)
                    if trace is not None and int(event.vkCode) == VK_RMENU else 0
                )
                consumed = self.consume_rc003_direction_event(event, not key_up)
                physicalization = None
                if not consumed:
                    record_physical_key_event(event, int(w_param))
                    physicalization = physicalize_injected_event(event, key_up)
                physicalized = physicalization is not None
                if trace is not None:
                    try:
                        trace.observe_submission_key(
                            int(event.vkCode), key_up, original_flags
                        )
                    except Exception:
                        pass  # Metadata observation cannot consume or delay key delivery.
                button_id = _TRACE_VK_TO_BUTTON.get(int(event.vkCode), "")
                voice_marker = _is_voice_event_marker(original_extra_info)
                if trace is not None and (button_id or voice_marker or awaiting_marker):
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
                            awaiting_marker=awaiting_marker,
                            receipt_correlation=(
                                "exact_marker" if awaiting_marker == original_extra_info and awaiting_marker
                                else "pending_edge_only" if awaiting_marker else "none"
                            ),
                            event_time_ms=int(event.time),
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
                        downstream_result = user32.CallNextHookEx(
                            self._hook,
                            n_code,
                            w_param,
                            int(l_param),
                        )
                    except BaseException:
                        if isinstance(physicalization, VoiceEventConfirmation):
                            complete_marked_voice_event(
                                physicalization,
                                downstream_error=True,
                            )
                        raise
                    else:
                        if isinstance(physicalization, VoiceEventConfirmation):
                            complete_marked_voice_event(
                                physicalization,
                                downstream_result=int(downstream_result),
                            )
                        return downstream_result
                    finally:
                        event.flags = original_flags
                        event.dwExtraInfo = original_extra_info
                return user32.CallNextHookEx(self._hook, n_code, w_param, l_param)
            finally:
                _end_physical_ralt_callback(callback_generation)
        return user32.CallNextHookEx(self._hook, n_code, w_param, l_param)
