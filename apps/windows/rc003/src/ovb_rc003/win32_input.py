"""Real Win32 input for ordinary actions and the voice shortcut.

Windows-only. Kept as thin as possible and separated from win32_keys.py's
pure VK-code resolution so the mapping logic stays unit-testable everywhere
while only the actual syscall needs ctypes/user32.

Batching/rollback contract (fixed after XRBM-014 review RETRY P1 #4 - see
XRBM-014's independent review; extended by XRBM-019/XRBM-020 - see
XRBM-019's independent review): a multi-key combo is submitted to
``SendInput`` as a single batched call (one array, one syscall) rather than
one call per key, so there is only one narrow window in which a partial
delivery could happen at all. If ``SendInput`` reports it queued fewer
events than requested, the exact keys that *did* go down are immediately
released before the failure is raised. A generic exception raised by the
sender AFTER submission is treated the same way - delivery is unknown, not
"nothing landed" - so every key that may still be down gets its own
best-effort release attempt before the failure is raised too. This applies
to all three combo helpers, including the cleanup-path ``send_key_combo_up``:
its failures (partial or generic) are best-effort retried and then RAISED as
an observable ``OSError``, never swallowed - the sole exception is
``Win32InputUnavailableError`` ("not running on Windows"), a pre-submission
platform-availability signal re-raised as-is with no rollback attempted,
since nothing could have landed.

WeType compatibility is deliberately narrower than the ordinary mapping
path: separate virtual-key ``SendInput`` batches for key-down and key-up,
an 80 ms hold between them,
``wScan=0``, no ``KEYEVENTF_SCANCODE``, and ``dwExtraInfo=0``. Other providers
retain the marked ``keybd_event`` voice path required by the existing Doubao
compatibility layer.

Testability: every public function accepts an optional ``_sender`` keyword
(a callable matching ``RawSender``) used only by tests. Production callers
never pass it, so the real ``ctypes``/``user32.SendInput`` path is used -
but tests/test_win32_input_batching.py can inject a fake sender that
simulates partial delivery and assert the exact rollback calls that result,
without needing ``ctypes.windll`` (which does not exist off Windows) at all.
"""

from __future__ import annotations

import ctypes
import os
import sys
import time
from ctypes import wintypes
from typing import Callable, List, Optional, Sequence, Tuple

from . import raw_input_windows, voice_key_physicalizer_windows, win32_keys

VOICE_EVENT_EXTRA_INFO = voice_key_physicalizer_windows.VOICE_EVENT_EXTRA_INFO
_VOICE_EVENT_CONFIRM_TIMEOUT_SECONDS = 1.0

_INPUT_KEYBOARD = 1
_INPUT_MOUSE = 0
_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_EXTENDEDKEY = 0x0001
_KEYEVENTF_SCANCODE = 0x0008
_MOUSEEVENTF_LEFTDOWN = 0x0002
_MOUSEEVENTF_LEFTUP = 0x0004
_MOUSEEVENTF_RIGHTDOWN = 0x0008
_MOUSEEVENTF_RIGHTUP = 0x0010
_MOUSEEVENTF_MIDDLEDOWN = 0x0020
_MOUSEEVENTF_MIDDLEUP = 0x0040
_MOUSEEVENTF_XDOWN = 0x0080
_MOUSEEVENTF_XUP = 0x0100
_MOUSEEVENTF_WHEEL = 0x0800
_XBUTTON1 = 0x0001
_XBUTTON2 = 0x0002
_WHEEL_DELTA = 120
_VK_LBUTTON = 0x01
_VK_RBUTTON = 0x02
_VK_MBUTTON = 0x04
_VK_XBUTTON1 = 0x05
_VK_XBUTTON2 = 0x06

# Real x64 Win32 ``INPUT`` struct shape (fixed after XRBM-014 review round 2
# P1 #1: the union previously declared only ``KEYBDINPUT``, so
# ``ctypes.sizeof(INPUT)`` was smaller than the real ``sizeof(INPUT)``
# Windows expects in ``SendInput``'s ``cbSize`` argument - Microsoft
# documents that ``SendInput`` fails outright when ``cbSize`` does not match
# the real struct size. The real ``INPUT`` union is
# ``MOUSEINPUT | KEYBDINPUT | HARDWAREINPUT`` (``MOUSEINPUT`` is the largest
# member, which is what actually determines ``sizeof(INPUT)`` on x64), and
# ``dwExtraInfo`` is a ``ULONG_PTR`` (a pointer-*sized* integer, not a
# pointer-to-``ULONG``) - using a pointer type there previously happened to
# be the same width on x64 but was the wrong C type and would have been
# wrong on x86.
#
# ``ctypes.wintypes`` is importable on any OS (it defines plain ctypes
# aliases, no ``windll`` linkage) - but ``wintypes.DWORD``/``LONG`` are
# aliases for ``ctypes.c_ulong``/``c_long``, whose *width* tracks the HOST
# platform's C ``long`` (4 bytes on Windows' LLP64 model, but 8 bytes on
# 64-bit macOS/Linux's LP64 model). Using them here would make
# ``ctypes.sizeof()`` correct only when actually run on Windows. These
# fields are therefore declared with explicit fixed-width types
# (``c_uint32``/``c_int32``/``c_uint16``) that match the real Win32 ABI on
# every host - which is also what makes it possible to assert
# ``ctypes.sizeof(INPUT) == 40`` in a cross-platform test (see
# tests/test_win32_input_abi.py), not only a Windows-only one.
_ULONG_PTR = ctypes.c_size_t  # pointer-sized on every host/target pair this
# project supports (32-bit ULONG_PTR on x86 Windows, 64-bit on x64 Windows).


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_int32),
        ("dy", ctypes.c_int32),
        ("mouseData", ctypes.c_uint32),
        ("dwFlags", ctypes.c_uint32),
        ("time", ctypes.c_uint32),
        ("dwExtraInfo", _ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_uint16),
        ("wScan", ctypes.c_uint16),
        ("dwFlags", ctypes.c_uint32),
        ("time", ctypes.c_uint32),
        ("dwExtraInfo", _ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", ctypes.c_uint32),
        ("wParamL", ctypes.c_uint16),
        ("wParamH", ctypes.c_uint16),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_uint32), ("union", _INPUT_UNION)]

# Keys Windows treats as "extended" for SendInput purposes.
_EXTENDED_KEYS = frozenset(
    {
        win32_keys.VK_CODES[name]
        for name in ("up", "down", "left", "right", "rctrl", "ralt", "rwin")
    }
)

# Modifier VK codes are intentionally emitted as physical scan-code events.
# This keeps generic modifiers on their left-side physical key and preserves
# left/right identity for directional modifiers. The boolean records whether
# the scan code carries the E0 extended prefix.
_PHYSICAL_SCAN_CODES = {
    win32_keys.VK_CODES["up"]: (0x48, True),
    win32_keys.VK_CODES["down"]: (0x50, True),
    win32_keys.VK_CODES["left"]: (0x4B, True),
    win32_keys.VK_CODES["right"]: (0x4D, True),
    win32_keys.VK_CODES["ctrl"]: (0x1D, False),
    win32_keys.VK_CODES["lctrl"]: (0x1D, False),
    win32_keys.VK_CODES["rctrl"]: (0x1D, True),
    win32_keys.VK_CODES["shift"]: (0x2A, False),
    win32_keys.VK_CODES["lshift"]: (0x2A, False),
    win32_keys.VK_CODES["rshift"]: (0x36, False),
    win32_keys.VK_CODES["alt"]: (0x38, False),
    win32_keys.VK_CODES["lalt"]: (0x38, False),
    win32_keys.VK_CODES["ralt"]: (0x38, True),
    win32_keys.VK_CODES["win"]: (0x5B, True),
    win32_keys.VK_CODES["rwin"]: (0x5C, True),
}

RawSender = Callable[[Sequence[Tuple[int, bool]]], int]
MouseEvent = Tuple[int, int]
MouseSender = Callable[[Sequence[MouseEvent]], int]
MouseButtonDownQuery = Callable[[str], bool]
PhysicalKeyDownQuery = Callable[[int], bool]
VoiceSender = Callable[[int, bool], None]

_MOUSE_BUTTON_EVENTS = {
    "left": (_MOUSEEVENTF_LEFTDOWN, _MOUSEEVENTF_LEFTUP, 0),
    "right": (_MOUSEEVENTF_RIGHTDOWN, _MOUSEEVENTF_RIGHTUP, 0),
    "middle": (_MOUSEEVENTF_MIDDLEDOWN, _MOUSEEVENTF_MIDDLEUP, 0),
    "x1": (_MOUSEEVENTF_XDOWN, _MOUSEEVENTF_XUP, _XBUTTON1),
    "x2": (_MOUSEEVENTF_XDOWN, _MOUSEEVENTF_XUP, _XBUTTON2),
}

_MOUSE_BUTTON_VK_CODES = {
    "left": _VK_LBUTTON,
    "right": _VK_RBUTTON,
    "middle": _VK_MBUTTON,
    "x1": _VK_XBUTTON1,
    "x2": _VK_XBUTTON2,
}

_voice_backend: Optional[str] = None


class Win32InputUnavailableError(Exception):
    """Raised when SendInput is invoked on a non-Windows platform."""


def _require_live_input_allowed() -> None:
    if os.environ.get("RC003_DISABLE_LIVE_INPUT") == "1":
        raise Win32InputUnavailableError(
            "live Windows input is disabled for this process"
        )


class InputCleanupIncompleteError(OSError):
    """Raised when delivery failed and a compensating input-up could not be
    confirmed.

    Callers must retain enough state to retry a release later. Treating this
    as an ordinary delivery failure can strand Alt/Ctrl/Win logically down
    while the application forgets that it still owes cleanup.
    """


class MouseButtonInUseError(OSError):
    """Raised when a real mouse already owns the requested button."""


class PhysicalKeyInUseError(OSError):
    """Raised before injection when a real keyboard key already owns a VK."""


def _require_windows() -> None:
    if sys.platform != "win32":
        raise Win32InputUnavailableError(
            "SendInput key injection is only available on Windows"
        )


def _build_input_array(events: Sequence[Tuple[int, bool]]):
    array = (INPUT * len(events))()
    for index, (vk, key_up) in enumerate(events):
        flags = _KEYEVENTF_KEYUP if key_up else 0
        if vk in _EXTENDED_KEYS:
            flags |= _KEYEVENTF_EXTENDEDKEY
        physical_scan = _PHYSICAL_SCAN_CODES.get(vk)
        if physical_scan is not None:
            scan_code, is_extended = physical_scan
            flags |= _KEYEVENTF_SCANCODE
            if is_extended:
                flags |= _KEYEVENTF_EXTENDEDKEY
            keybd_input = KEYBDINPUT(
                wVk=0,
                wScan=scan_code,
                dwFlags=flags,
                time=0,
                dwExtraInfo=0,
            )
        else:
            keybd_input = KEYBDINPUT(
                wVk=vk,
                wScan=0,
                dwFlags=flags,
                time=0,
                dwExtraInfo=0,
            )
        array[index] = INPUT(type=_INPUT_KEYBOARD, union=_INPUT_UNION(ki=keybd_input))
    return array, INPUT


def _build_virtual_key_input_array(events: Sequence[Tuple[int, bool]]):
    """Build unmarked virtual-key events for WeType's global shortcut."""

    array = (INPUT * len(events))()
    for index, (vk, key_up) in enumerate(events):
        flags = _KEYEVENTF_KEYUP if key_up else 0
        if vk in _EXTENDED_KEYS:
            flags |= _KEYEVENTF_EXTENDEDKEY
        keybd_input = KEYBDINPUT(
            wVk=vk,
            wScan=0,
            dwFlags=flags,
            time=0,
            dwExtraInfo=0,
        )
        array[index] = INPUT(type=_INPUT_KEYBOARD, union=_INPUT_UNION(ki=keybd_input))
    return array, INPUT


def _build_mouse_input_array(events: Sequence[MouseEvent]):
    array = (INPUT * len(events))()
    for index, (flags, mouse_data) in enumerate(events):
        mouse_input = MOUSEINPUT(
            dx=0,
            dy=0,
            mouseData=ctypes.c_uint32(mouse_data).value,
            dwFlags=flags,
            time=0,
            dwExtraInfo=0,
        )
        array[index] = INPUT(type=_INPUT_MOUSE, union=_INPUT_UNION(mi=mouse_input))
    return array, INPUT


def _real_send_input_batch_with_builder(events, builder) -> int:
    """Submit one input batch using the requested INPUT-array builder."""

    _require_live_input_allowed()
    _require_windows()
    if not events:
        return 0

    array, input_type = builder(events)
    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    # Declared explicitly (XRBM-014 review round 2 P1 #8) rather than left at
    # ctypes defaults: without an explicit restype, ctypes assumes a 32-bit
    # ``int`` return, and without argtypes the pointer/size arguments are
    # marshaled less predictably on 64-bit Windows.
    user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(input_type), ctypes.c_int)
    user32.SendInput.restype = wintypes.UINT
    # XRBM-018 RETRY 1 P1 #1: with ``argtypes`` declared as
    # ``POINTER(INPUT)`` (a pointer to one element), ctypes only accepts the
    # array instance itself here (it implicitly decays to a pointer to its
    # first element, exactly like a C array passed where a pointer is
    # expected) - ``ctypes.byref(array)`` instead produces a pointer *to the
    # array object* (a distinct, incompatible pointer type from ctypes' point
    # of view: ``LP_INPUT_Array_N``, not ``LP_INPUT``) and raises
    # ``ArgumentError`` before the call ever reaches Windows. ``byref()`` is
    # only correct for a pointer to a single instance, never to an array.
    sent = user32.SendInput(len(events), array, ctypes.sizeof(input_type))
    return int(sent)


def _real_send_input_batch(events: Sequence[Tuple[int, bool]]) -> int:
    """Submit ordinary scan-aware keyboard events in one real SendInput call."""

    return _real_send_input_batch_with_builder(events, _build_input_array)


def _real_send_virtual_key_input_batch(events: Sequence[Tuple[int, bool]]) -> int:
    """Submit unmarked, virtual-key-only events in one real SendInput call."""

    return _real_send_input_batch_with_builder(events, _build_virtual_key_input_array)


def _real_send_mouse_input_batch(events: Sequence[MouseEvent]) -> int:
    """Submit ordinary mouse events in one real SendInput call."""

    return _real_send_input_batch_with_builder(events, _build_mouse_input_array)


def _mouse_button_event(button: str, *, key_up: bool) -> MouseEvent:
    try:
        down_flag, up_flag, mouse_data = _MOUSE_BUTTON_EVENTS[button]
    except KeyError as exc:
        raise ValueError(f"unsupported mouse button: {button}") from exc
    return (up_flag if key_up else down_flag, mouse_data)


def _real_mouse_button_is_down(button: str) -> bool:
    try:
        vk_code = _MOUSE_BUTTON_VK_CODES[button]
    except KeyError as exc:
        raise ValueError(f"unsupported mouse button: {button}") from exc
    _require_windows()
    _require_live_input_allowed()
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
    user32.GetAsyncKeyState.restype = ctypes.c_short
    return bool(user32.GetAsyncKeyState(vk_code) & 0x8000)


def _best_effort_mouse_release(button: str, sender: MouseSender) -> bool:
    try:
        sent = sender([_mouse_button_event(button, key_up=True)])
    except Exception:
        return False
    return sent == 1


def send_mouse_button_click(
    button: str,
    *,
    _sender: Optional[MouseSender] = None,
    _button_down_query: Optional[MouseButtonDownQuery] = None,
) -> None:
    """Click one physical mouse button at the current pointer position.

    Down and up are submitted together. If submission is partial or raises
    after it may have reached Windows, a separate up is attempted immediately.
    An incomplete compensating release is surfaced distinctly so the caller
    can retain ownership and retry before another action.
    """

    sender = _sender or _real_send_mouse_input_batch
    events = [
        _mouse_button_event(button, key_up=False),
        _mouse_button_event(button, key_up=True),
    ]
    button_down_query = _button_down_query
    if button_down_query is None and _sender is None:
        button_down_query = _real_mouse_button_is_down
    if button_down_query is not None and button_down_query(button):
        raise MouseButtonInUseError(
            f"mouse {button} is already held by physical input"
        )
    try:
        sent = sender(events)
    except Win32InputUnavailableError:
        raise
    except Exception as exc:
        cleanup_complete = _best_effort_mouse_release(button, sender)
        error_type = _delivery_error_type(cleanup_complete)
        raise error_type(f"mouse {button} click delivery failed: {exc}") from exc
    if sent < len(events):
        cleanup_complete = sent == 0 or _best_effort_mouse_release(button, sender)
        error_type = _delivery_error_type(cleanup_complete)
        raise error_type(
            f"SendInput delivered only {sent}/{len(events)} events for mouse "
            f"{button} click; safety release attempted"
        )


def send_mouse_button_up(
    button: str, *, _sender: Optional[MouseSender] = None
) -> None:
    """Release one mouse button, retrying once if delivery is uncertain."""

    sender = _sender or _real_send_mouse_input_batch
    event = _mouse_button_event(button, key_up=True)
    try:
        sent = sender([event])
    except Win32InputUnavailableError:
        raise
    except Exception as exc:
        cleanup_complete = _best_effort_mouse_release(button, sender)
        error_type = _delivery_error_type(cleanup_complete)
        raise error_type(f"mouse {button} up delivery failed: {exc}") from exc
    if sent < 1:
        cleanup_complete = _best_effort_mouse_release(button, sender)
        error_type = _delivery_error_type(cleanup_complete)
        raise error_type(
            f"SendInput did not deliver mouse {button} up; retry attempted"
        )


def send_mouse_wheel(
    clicks: int, *, _sender: Optional[MouseSender] = None
) -> None:
    """Scroll vertically by an integral number of Windows wheel clicks."""

    if not isinstance(clicks, int) or isinstance(clicks, bool) or clicks == 0:
        raise ValueError("mouse wheel clicks must be a non-zero integer")
    sender = _sender or _real_send_mouse_input_batch
    events = [(_MOUSEEVENTF_WHEEL, clicks * _WHEEL_DELTA)]
    try:
        sent = sender(events)
    except Win32InputUnavailableError:
        raise
    except Exception as exc:
        raise OSError(f"mouse wheel delivery failed: {exc}") from exc
    if sent < 1:
        raise OSError("SendInput did not deliver the mouse wheel event")


def _best_effort_release(
    vk_codes: Sequence[int],
    sender: RawSender,
    key_down_query: Optional[PhysicalKeyDownQuery] = None,
) -> bool:
    """Attempt every requested key-up and report whether all were confirmed.

    The caller still owns the observable delivery exception. This helper
    deliberately continues after a failed release so one stuck modifier does
    not prevent the remaining keys from being released too.
    """

    complete = True
    for vk in vk_codes:
        if key_down_query is not None:
            try:
                if key_down_query(vk):
                    continue
            except Exception:
                complete = False
                continue
        try:
            sent = sender([(vk, True)])
        except Exception:
            complete = False
        else:
            if sent != 1:
                complete = False
    return complete


def _delivery_error_type(cleanup_complete: bool):
    return OSError if cleanup_complete else InputCleanupIncompleteError


def _ensure_keys_not_physically_down(
    vk_codes: Sequence[int],
    query: PhysicalKeyDownQuery,
) -> None:
    try:
        held = [vk for vk in dict.fromkeys(vk_codes) if query(vk)]
    except Exception as exc:
        raise Win32InputUnavailableError(
            "physical keyboard state is unavailable"
        ) from exc
    if held:
        formatted = ",".join(f"0x{vk:02x}" for vk in held)
        raise PhysicalKeyInUseError(
            f"physical keyboard input already holds requested key(s): {formatted}"
        )


def _physical_query_for_sender(
    sender_was_injected: bool,
    query: Optional[PhysicalKeyDownQuery],
    *,
    before_injection: bool = False,
) -> Optional[PhysicalKeyDownQuery]:
    if query is not None:
        return query
    if sender_was_injected:
        return None
    if before_injection:
        return _physical_key_is_down_before_injection
    return _physical_key_is_down


def _physical_key_is_down(vk_code: int) -> bool:
    return bool(
        raw_input_windows.physical_key_is_down(vk_code)
        or voice_key_physicalizer_windows.physical_key_is_down(vk_code)
    )


def _physical_key_is_down_before_injection(vk_code: int) -> bool:
    if _physical_key_is_down(vk_code):
        return True
    return raw_input_windows.physical_key_is_down_before_injection(vk_code)


def can_begin_tracked_hold(tokens: Sequence[str]) -> bool:
    """Return whether every key can retain physical ownership after injection."""

    vk_codes = win32_keys.resolve_vk_codes(tokens)
    raw_tracking = raw_input_windows.physical_keyboard_tracking_available()
    return all(
        raw_tracking
        or voice_key_physicalizer_windows.physical_key_tracking_available(vk)
        for vk in vk_codes
    )


def _ensure_tracked_hold_available(vk_codes: Sequence[int]) -> None:
    raw_tracking = raw_input_windows.physical_keyboard_tracking_available()
    unavailable = [
        vk
        for vk in dict.fromkeys(vk_codes)
        if not raw_tracking
        and not voice_key_physicalizer_windows.physical_key_tracking_available(vk)
    ]
    if unavailable:
        formatted = ",".join(f"0x{vk:02x}" for vk in unavailable)
        raise Win32InputUnavailableError(
            "physical keyboard tracking is unavailable for held key(s): "
            f"{formatted}"
        )


def send_key_combo_down(
    tokens: Sequence[str],
    *,
    _sender: Optional[RawSender] = None,
    _key_down_query: Optional[PhysicalKeyDownQuery] = None,
    _release_key_down_query: Optional[PhysicalKeyDownQuery] = None,
) -> None:
    """Presses every key in ``tokens`` down, in one batched call.

    On partial delivery, releases exactly the keys that did go down, then
    raises - callers must not assume the combo is active after an exception.

    XRBM-020 (fixing the XRBM-019 REPLAN gap - see
    XRBM-019's independent review round 2): a generic exception
    raised by the sender AFTER submission does not prove zero events reached
    Windows - delivery is unknown, not "nothing landed". Every key in the
    batch is therefore treated as possibly down and gets its own best-effort
    release attempt before the failure is surfaced as ``OSError`` chained
    from the original exception. ``Win32InputUnavailableError`` is a
    PRE-submission platform-availability signal (raised by
    ``_require_windows()`` before any event is built), so it is re-raised
    as-is with no rollback attempted - nothing could have landed.
    """

    vk_codes = win32_keys.resolve_vk_codes(tokens)
    if (
        _sender is None
        and _key_down_query is None
        and _release_key_down_query is None
    ):
        _ensure_tracked_hold_available(vk_codes)
    preflight_query = _physical_query_for_sender(
        _sender is not None,
        _key_down_query,
        before_injection=True,
    )
    release_query = _physical_query_for_sender(
        _sender is not None,
        (
            _release_key_down_query
            if _release_key_down_query is not None
            else _key_down_query
        ),
    )
    if preflight_query is not None:
        _ensure_keys_not_physically_down(vk_codes, preflight_query)
    sender = _sender or _real_send_input_batch
    events: List[Tuple[int, bool]] = [(vk, False) for vk in vk_codes]
    try:
        sent = sender(events)
    except Win32InputUnavailableError:
        raise
    except Exception as exc:
        cleanup_complete = _best_effort_release(
            list(reversed(vk_codes)), sender, release_query
        )
        error_type = _delivery_error_type(cleanup_complete)
        raise error_type(f"key-down delivery failed: {exc}") from exc
    if sent < len(events):
        stuck_down = [vk for vk, _key_up in events[:sent]]
        cleanup_complete = _best_effort_release(
            list(reversed(stuck_down)), sender, release_query
        )
        error_type = _delivery_error_type(cleanup_complete)
        raise error_type(
            f"SendInput delivered only {sent}/{len(events)} key-down events; rolled back"
        )


def send_key_combo_up(
    tokens: Sequence[str],
    *,
    _sender: Optional[RawSender] = None,
    _key_down_query: Optional[PhysicalKeyDownQuery] = None,
) -> None:
    """Releases every key in ``tokens`` (reverse order), in one batched call.

    This is a cleanup-path primitive: besides "SendInput is not available on
    this OS" (``Win32InputUnavailableError``, re-raised so callers can log
    it once), every other failure still gets a best-effort retry of whatever
    key(s) may not have landed - but (XRBM-019 review round 1 P1 #4) it now
    RAISES ``OSError`` afterward instead of swallowing the failure, generic
    or partial. A caller doing multi-step cleanup (see app.py's
    ``_cleanup_once``) must still be able to attempt its other independent
    steps after this raises - that is done by wrapping this call, not by
    this function silently reporting success it cannot back up. Silently
    swallowing a failed key-up here previously meant a host key could be
    left physically down while the caller's own state already recorded it as
    released.

    XRBM-020 (fixing the XRBM-019 REPLAN gap - see
    XRBM-019's independent review round 2): the generic-exception
    branch used to raise immediately with no rollback attempt at all - an
    exception raised by the sender AFTER submission does not prove zero
    key-ups landed, so every key in the batch is now given its own
    best-effort release attempt (matching the partial-delivery branch)
    before ``OSError`` is raised, chained from the original exception.
    """

    sender = _sender or _real_send_input_batch
    vk_codes = list(reversed(win32_keys.resolve_vk_codes(tokens)))
    key_down_query = _physical_query_for_sender(
        _sender is not None,
        _key_down_query,
    )
    physical_query_complete = True
    if key_down_query is not None:
        releasable = []
        for vk in vk_codes:
            try:
                held_physically = bool(key_down_query(vk))
            except Exception:
                physical_query_complete = False
                continue
            if not held_physically:
                releasable.append(vk)
        vk_codes = releasable
    if not vk_codes:
        if not physical_query_complete:
            raise InputCleanupIncompleteError(
                "physical keyboard state could not be confirmed for key-up"
            )
        return
    events: List[Tuple[int, bool]] = [(vk, True) for vk in vk_codes]
    try:
        sent = sender(events)
    except Win32InputUnavailableError:
        raise
    except Exception as exc:
        cleanup_complete = _best_effort_release(vk_codes, sender, key_down_query)
        error_type = _delivery_error_type(cleanup_complete)
        raise error_type(f"key-up delivery failed: {exc}") from exc
    if sent < len(events):
        remaining = [vk for vk, _key_up in events[sent:]]
        cleanup_complete = _best_effort_release(remaining, sender, key_down_query)
        error_type = _delivery_error_type(cleanup_complete)
        raise error_type(
            f"SendInput delivered only {sent}/{len(events)} key-up events; "
            "best-effort release attempted for the rest"
        )
    if not physical_query_complete:
        raise InputCleanupIncompleteError(
            "some key-ups were deferred because physical keyboard state "
            "could not be confirmed"
        )


def send_key_combo_tap(
    tokens: Sequence[str],
    *,
    _sender: Optional[RawSender] = None,
    _key_down_query: Optional[PhysicalKeyDownQuery] = None,
) -> None:
    """Presses and releases every key in ``tokens`` as ONE batched SendInput
    call (all key-downs in order, then all key-ups in reverse order).

    On partial delivery, rolls back whichever keys are still down (either
    because the down half didn't fully land, or because the down half fully
    landed but part of the up half didn't) before raising.

    XRBM-020 (fixing the XRBM-019 REPLAN gap - see
    XRBM-019's independent review round 2): a generic exception
    raised by the sender AFTER submission does not prove zero events
    (either half) reached Windows - every key in this tap is treated as
    possibly still down and gets its own best-effort release attempt before
    ``OSError`` is raised, chained from the original exception.
    ``Win32InputUnavailableError`` is re-raised as-is with no rollback, same
    as the other two helpers - it is a pre-submission signal.
    """

    vk_codes = win32_keys.resolve_vk_codes(tokens)
    preflight_query = _physical_query_for_sender(
        _sender is not None,
        _key_down_query,
        before_injection=True,
    )
    release_query = _physical_query_for_sender(
        _sender is not None,
        _key_down_query,
    )
    if preflight_query is not None:
        _ensure_keys_not_physically_down(vk_codes, preflight_query)
    sender = _sender or _real_send_input_batch
    down_events: List[Tuple[int, bool]] = [(vk, False) for vk in vk_codes]
    up_events: List[Tuple[int, bool]] = [(vk, True) for vk in reversed(vk_codes)]
    events = down_events + up_events
    try:
        sent = sender(events)
    except Win32InputUnavailableError:
        raise
    except Exception as exc:
        cleanup_complete = _best_effort_release(
            list(reversed(vk_codes)), sender, release_query
        )
        error_type = _delivery_error_type(cleanup_complete)
        raise error_type(f"key tap delivery failed: {exc}") from exc
    if sent < len(events):
        if sent < len(down_events):
            # Not every key-down made it; release exactly the ones that did.
            stuck_down = [vk for vk, _key_up in down_events[:sent]]
            cleanup_complete = _best_effort_release(
                list(reversed(stuck_down)), sender, release_query
            )
        else:
            # All key-downs landed; finish releasing whatever key-ups didn't.
            remaining_index = sent - len(down_events)
            remaining_ups = [vk for vk, _key_up in up_events[remaining_index:]]
            cleanup_complete = _best_effort_release(
                remaining_ups, sender, release_query
            )
        error_type = _delivery_error_type(cleanup_complete)
        raise error_type(
            f"SendInput delivered only {sent}/{len(events)} events for a key tap; rolled back"
        )


def _real_keybd_event(
    vk: int,
    key_up: bool,
    *,
    _extra_info: int = VOICE_EVENT_EXTRA_INFO,
) -> None:
    """Emit one voice shortcut edge through the legacy Win32 keyboard API.

    Doubao registers its global voice shortcut as a virtual-key shortcut.  The
    upstream RC003 bridge uses ``keybd_event`` with the virtual key populated;
    sending the same edge as a scan-code-only ``SendInput`` event is accepted
    by Windows but is not recognized reliably by Doubao.
    """

    _require_live_input_allowed()
    _require_windows()
    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    user32.MapVirtualKeyW.argtypes = (wintypes.UINT, wintypes.UINT)
    user32.MapVirtualKeyW.restype = wintypes.UINT
    user32.keybd_event.argtypes = (
        wintypes.BYTE,
        wintypes.BYTE,
        wintypes.DWORD,
        _ULONG_PTR,
    )
    user32.keybd_event.restype = None

    scan_code = int(user32.MapVirtualKeyW(vk, 0))
    flags = _KEYEVENTF_EXTENDEDKEY if vk in _EXTENDED_KEYS else 0
    if key_up:
        flags |= _KEYEVENTF_KEYUP
    user32.keybd_event(vk, scan_code, flags, int(_extra_info))


def voice_backend_name() -> str:
    """Return the transport selected for the current voice session."""

    return _voice_backend or "unselected"


def _real_voice_event(vk: int, key_up: bool) -> None:
    """Emit a marked voice edge through the legacy ``keybd_event`` path.

    The optional compatibility hook clears the injected flag only for the
    right-Alt target. Other user-configured combinations remain normal
    injected key sequences.
    """

    global _voice_backend
    if _voice_backend is None:
        _voice_backend = "keybd_event_physicalized"
    if int(vk) != win32_keys.VK_CODES["ralt"]:
        _real_keybd_event(vk, key_up)
        return
    try:
        confirmation = voice_key_physicalizer_windows.begin_marked_voice_event(
            key_up
        )
    except voice_key_physicalizer_windows.VoiceKeyPhysicalizerUnavailableError as exc:
        raise Win32InputUnavailableError(
            "marked right-Alt physicalizer is unavailable"
        ) from exc
    try:
        _real_keybd_event(
            vk,
            key_up,
            _extra_info=confirmation.marker,
        )
    except BaseException:
        voice_key_physicalizer_windows.cancel_marked_voice_event(confirmation)
        raise
    if not voice_key_physicalizer_windows.wait_for_marked_voice_event(
        confirmation,
        _VOICE_EVENT_CONFIRM_TIMEOUT_SECONDS,
    ):
        raise InputCleanupIncompleteError(
            "marked right-Alt edge was not confirmed by the physicalizer hook"
        )


def _best_effort_voice_up(
    vk_codes: Sequence[int],
    sender: VoiceSender,
    key_down_query: Optional[PhysicalKeyDownQuery] = None,
) -> bool:
    complete = True
    for vk in reversed(vk_codes):
        if key_down_query is not None:
            try:
                if key_down_query(vk):
                    continue
            except Exception:
                complete = False
                continue
        try:
            sender(vk, True)
        except Exception:
            complete = False
    return complete


def send_voice_key_combo_down(
    tokens: Sequence[str],
    *,
    _sender: Optional[VoiceSender] = None,
    _key_down_query: Optional[PhysicalKeyDownQuery] = None,
) -> None:
    """Press a voice shortcut through the marked virtual-key path."""

    vk_codes = win32_keys.resolve_vk_codes(tokens)
    if _sender is None and _key_down_query is None:
        _ensure_tracked_hold_available(vk_codes)
    preflight_query = _physical_query_for_sender(
        _sender is not None,
        _key_down_query,
        before_injection=True,
    )
    release_query = _physical_query_for_sender(
        _sender is not None,
        _key_down_query,
    )
    if preflight_query is not None:
        _ensure_keys_not_physically_down(vk_codes, preflight_query)
    sender = _sender or _real_voice_event
    delivered: List[int] = []
    for vk in vk_codes:
        try:
            sender(vk, False)
        except Win32InputUnavailableError as exc:
            if delivered and not _best_effort_voice_up(
                delivered, sender, release_query
            ):
                raise InputCleanupIncompleteError(
                    "voice backend became unavailable and delivered keys could not be released"
                ) from exc
            raise
        except InputCleanupIncompleteError as exc:
            _best_effort_voice_up(
                [*delivered, vk], sender, release_query
            )
            raise InputCleanupIncompleteError(
                "voice key-down delivery could not be confirmed"
            ) from exc
        except Exception as exc:
            # A sender can raise after the native call returned control but
            # before the wrapper could prove whether this current edge
            # landed. Treat the current key as possibly down too.
            cleanup_complete = _best_effort_voice_up(
                [*delivered, vk], sender, release_query
            )
            error_type = _delivery_error_type(cleanup_complete)
            raise error_type(f"voice key-down delivery failed: {exc}") from exc
        delivered.append(vk)


def send_voice_key_combo_up(
    tokens: Sequence[str],
    *,
    _sender: Optional[VoiceSender] = None,
    _key_down_query: Optional[PhysicalKeyDownQuery] = None,
) -> None:
    """Release a voice shortcut through the selected voice transport."""

    sender = _sender or _real_voice_event
    resolved_vk_codes = win32_keys.resolve_vk_codes(tokens)
    key_down_query = _physical_query_for_sender(
        _sender is not None,
        _key_down_query,
    )
    vk_codes = list(reversed(resolved_vk_codes))
    physical_query_complete = True
    for vk in vk_codes:
        if key_down_query is not None:
            try:
                if key_down_query(vk):
                    continue
            except Exception:
                physical_query_complete = False
                continue
        try:
            sender(vk, True)
        except Win32InputUnavailableError as exc:
            if not _best_effort_voice_up(
                resolved_vk_codes, sender, key_down_query
            ):
                raise InputCleanupIncompleteError(
                    "voice backend became unavailable and key-up could not be confirmed"
                ) from exc
            raise
        except InputCleanupIncompleteError as exc:
            _best_effort_voice_up(
                resolved_vk_codes, sender, key_down_query
            )
            raise InputCleanupIncompleteError(
                "voice key-up delivery could not be confirmed"
            ) from exc
        except Exception as exc:
            # Releasing an already-up key is harmless. Retry every member so
            # a failure on one edge cannot strand later modifiers down.
            cleanup_complete = _best_effort_voice_up(
                resolved_vk_codes, sender, key_down_query
            )
            error_type = _delivery_error_type(cleanup_complete)
            raise error_type(f"voice key-up delivery failed: {exc}") from exc
    if not physical_query_complete:
        raise InputCleanupIncompleteError(
            "some voice key-ups were deferred because physical keyboard "
            "state could not be confirmed"
        )


def send_voice_key_combo_tap(
    tokens: Sequence[str],
    *,
    _sender: Optional[VoiceSender] = None,
    _key_down_query: Optional[PhysicalKeyDownQuery] = None,
) -> None:
    """Send a completed voice shortcut with the upstream 70 ms hold window."""

    sender = _sender or _real_voice_event
    vk_codes = win32_keys.resolve_vk_codes(tokens)
    if _sender is None and _key_down_query is None:
        _ensure_tracked_hold_available(vk_codes)
    preflight_query = _physical_query_for_sender(
        _sender is not None,
        _key_down_query,
        before_injection=True,
    )
    release_query = _physical_query_for_sender(
        _sender is not None,
        _key_down_query,
    )
    if preflight_query is not None:
        _ensure_keys_not_physically_down(vk_codes, preflight_query)
    send_voice_key_combo_down(
        tokens,
        _sender=sender,
        _key_down_query=lambda _vk: False,
    )
    try:
        time.sleep(0.07)
        send_voice_key_combo_up(
            tokens,
            _sender=sender,
            _key_down_query=release_query,
        )
    except BaseException as exc:
        cleanup_complete = _best_effort_voice_up(
            vk_codes, sender, release_query
        )
        if not cleanup_complete:
            raise InputCleanupIncompleteError(
                "voice key tap failed and final key-up could not be confirmed"
            ) from exc
        if isinstance(exc, InputCleanupIncompleteError):
            raise InputCleanupIncompleteError(
                "voice key tap delivery could not be confirmed"
            ) from exc
        raise


def send_wetype_voice_key_combo_down(
    tokens: Sequence[str],
    *,
    _sender: Optional[RawSender] = None,
    _key_down_query: Optional[PhysicalKeyDownQuery] = None,
) -> None:
    """Press WeType's shortcut through unmarked virtual-key SendInput."""

    if _sender is None and _key_down_query is None:
        _ensure_tracked_hold_available(win32_keys.resolve_vk_codes(tokens))
    preflight_query = (
        _key_down_query
        if _key_down_query is not None
        else (
            None
            if _sender is not None
            else _physical_key_is_down_before_injection
        )
    )
    release_query = (
        _key_down_query
        if _key_down_query is not None
        else (
            None
            if _sender is not None
            else _physical_key_is_down
        )
    )
    send_key_combo_down(
        tokens,
        _sender=_sender or _real_send_virtual_key_input_batch,
        _key_down_query=preflight_query,
        _release_key_down_query=release_query,
    )


def send_wetype_voice_key_combo_up(
    tokens: Sequence[str],
    *,
    _sender: Optional[RawSender] = None,
    _key_down_query: Optional[PhysicalKeyDownQuery] = None,
) -> None:
    """Release WeType's shortcut through the same virtual-key transport."""

    send_key_combo_up(
        tokens,
        _sender=_sender or _real_send_virtual_key_input_batch,
        _key_down_query=(
            _key_down_query
            if _key_down_query is not None
            else (
                None
                if _sender is not None
                else _physical_key_is_down
            )
        ),
    )


def send_wetype_voice_key_combo_tap(
    tokens: Sequence[str],
    *,
    _sender: Optional[RawSender] = None,
    _key_down_query: Optional[PhysicalKeyDownQuery] = None,
    _sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Send one 80 ms WeType shortcut tap."""

    sender = _sender or _real_send_virtual_key_input_batch
    vk_codes = win32_keys.resolve_vk_codes(tokens)
    if _sender is None and _key_down_query is None:
        _ensure_tracked_hold_available(vk_codes)
    preflight_query = _physical_query_for_sender(
        _sender is not None,
        _key_down_query,
        before_injection=True,
    )
    release_query = _physical_query_for_sender(
        _sender is not None,
        _key_down_query,
    )
    if preflight_query is not None:
        _ensure_keys_not_physically_down(vk_codes, preflight_query)
    send_wetype_voice_key_combo_down(
        tokens,
        _sender=sender,
        _key_down_query=lambda _vk: False,
    )
    try:
        _sleep(0.08)
        send_wetype_voice_key_combo_up(
            tokens,
            _sender=sender,
            _key_down_query=release_query,
        )
    except BaseException as exc:
        cleanup_complete = _best_effort_release(
            list(reversed(vk_codes)), sender, release_query
        )
        if not cleanup_complete:
            raise InputCleanupIncompleteError(
                "WeType key tap failed and final key-up could not be confirmed"
            ) from exc
        if isinstance(exc, InputCleanupIncompleteError):
            raise OSError(
                "WeType key tap failed but final safety key-up completed"
            ) from exc
        raise


def send_volume_up(*, _sender: Optional[RawSender] = None) -> None:
    _send_semantic_tap(("volume_up",), _sender=_sender)


def send_volume_down(*, _sender: Optional[RawSender] = None) -> None:
    _send_semantic_tap(("volume_down",), _sender=_sender)


# Semantic Windows actions.  These wrappers deliberately keep the action
# vocabulary out of ``app.py``'s platform plumbing: a configured ``方向上``
# action is an arrow action, not a UI string that happens to be translated to
# the token ``up``.  Tests can inject the same sender used by the low-level
# helpers, while production still emits one atomic SendInput batch.
def _send_semantic_tap(
    tokens: Sequence[str], *, _sender: Optional[RawSender] = None
) -> None:
    # Calling the default path without a keyword keeps these wrappers
    # compatible with simple injected senders used by callers/tests; the
    # explicit sender path remains available for ABI/batch tests.
    if _sender is None:
        send_key_combo_tap(tokens)
    else:
        send_key_combo_tap(tokens, _sender=_sender)


def send_escape(*, _sender: Optional[RawSender] = None) -> None:
    _send_semantic_tap(("escape",), _sender=_sender)


def send_return(*, _sender: Optional[RawSender] = None) -> None:
    _send_semantic_tap(("enter",), _sender=_sender)


def send_arrow_up(*, _sender: Optional[RawSender] = None) -> None:
    _send_semantic_tap(("up",), _sender=_sender)


def send_arrow_down(*, _sender: Optional[RawSender] = None) -> None:
    _send_semantic_tap(("down",), _sender=_sender)


def send_arrow_left(*, _sender: Optional[RawSender] = None) -> None:
    _send_semantic_tap(("left",), _sender=_sender)


def send_arrow_right(*, _sender: Optional[RawSender] = None) -> None:
    _send_semantic_tap(("right",), _sender=_sender)


def send_delete_backward(*, _sender: Optional[RawSender] = None) -> None:
    _send_semantic_tap(("backspace",), _sender=_sender)


def send_show_desktop(*, _sender: Optional[RawSender] = None) -> None:
    # Win+D is a toggle, so duplicate remote-button reports can immediately
    # restore every window. Win+M is idempotent for this action: repeating it
    # keeps windows minimized instead of making the desktop flash back.
    _send_semantic_tap(("win", "m"), _sender=_sender)


def send_context_menu(*, _sender: Optional[RawSender] = None) -> None:
    """Invoke the native Windows application/context-menu key."""

    _send_semantic_tap(("apps",), _sender=_sender)


def send_app_switcher(*, _sender: Optional[RawSender] = None) -> None:
    _send_semantic_tap(("alt", "tab"), _sender=_sender)


def send_volume_mute(*, _sender: Optional[RawSender] = None) -> None:
    _send_semantic_tap(("volume_mute",), _sender=_sender)


def send_play_pause(*, _sender: Optional[RawSender] = None) -> None:
    _send_semantic_tap(("media_play_pause",), _sender=_sender)
