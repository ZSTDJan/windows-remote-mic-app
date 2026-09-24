"""Version-locked Sogou normal finish, never cancel or toggle.

Owned by the voice host; also used by the manually confirmed diagnostic script.
The product runs native UIA in a disposable child with a hard time budget.
Temporary request/result metadata is removed by the parent; no speech is saved.
Unknown structure, package or capture identity always means no operation.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

FLAG = "--sogou-normal-submit"
TIMEOUT_SECONDS = 2.5


# This SHA-256 is the locally inspected package whose renderer source shows
# action[2] = cancel and action[3] = normal submit. A Sogou update must fail
# closed and be re-reviewed; do not silently apply this structural rule there.
APP_ASAR_SHA256 = "6B15B9752608881713F9B0552BFE99BEDB11FFA04E5B450CE559800AC6AE9BA3"
EXPECTED_EXE = "sogou_voice_assistant.exe"


@dataclass
class Candidate:
    control: object
    pid: int
    hwnd: int
    route: tuple[int, ...]
    rect: tuple[int, int, int, int]

    def record(self) -> dict[str, object]:
        return {
            "pid": self.pid,
            "hwnd": self.hwnd,
            "route": list(self.route),
            "rect": list(self.rect),
            "chosen": "right_of_verified_pair",
        }


def _rect(control):
    try:
        value = control.BoundingRectangle
        rect = tuple(int(getattr(value, side)) for side in ("left", "top", "right", "bottom"))
    except Exception:
        return None
    return rect if rect[2] > rect[0] and rect[3] > rect[1] else None


def _inside(outer, inner):
    return outer[0] <= inner[0] <= inner[2] <= outer[2] and outer[1] <= inner[1] <= inner[3] <= outer[3]


def _children(control):
    # A provider read failure must not look like an empty / disappeared tree.
    return tuple(control.GetChildren())


def _control_type(control):
    try:
        return str(control.ControlTypeName or "")
    except Exception:
        return ""


def _value(control, name):
    try:
        return str(getattr(control, name) or "")
    except Exception:
        return None  # A failed read is not proof of an unnamed action.


def _visible_enabled(control):
    try:
        return bool(control.IsEnabled) and not bool(control.IsOffscreen)
    except Exception:
        return False


def _has_invoke(control, pattern_id):
    try:
        return bool(control.GetPattern(pattern_id))
    except Exception:
        return False


def _walk(root, *, max_nodes=96, max_depth=14):
    """Bounded descendants of this one popup only; no desktop search."""
    pending, rows = [(root, ())], []
    while pending:
        control, route = pending.pop()
        if len(rows) >= max_nodes:
            return (), "tree_budget"
        rows.append((control, route))
        if len(route) >= max_depth:
            if _children(control):
                return (), "tree_depth"
            continue
        children = _children(control)
        pending.extend((child, route + (index,)) for index, child in reversed(list(enumerate(children))))
    return tuple(rows), ""


def find_normal_submit(root, invoke_pattern_id, *, diagnostics=None):
    """Return only the right half of the exact inspected action pair.

    This recognises no visible text and no absolute screen position. The
    structure is intentionally stricter than needed: any renderer/UIA drift
    makes it return an explanatory no-op instead of guessing.
    """
    rows, reason = _walk(root)
    if diagnostics is not None:
        diagnostics["node_count"] = len(rows)
    if reason:
        return None, reason
    semantic_roots = [(control, route) for control, route in rows
                      if _control_type(control) == "GroupControl"
                      and _value(control, "AutomationId") == "root"
                      and _visible_enabled(control)]
    if not semantic_roots:
        return None, "semantic_root_unavailable"
    if len(semantic_roots) != 1:
        return None, "root_ambiguous"
    semantic_root, root_route = semantic_roots[0]
    root_rect = _rect(semantic_root)
    root_children = _children(semantic_root)
    if root_rect is None or len(root_children) != 1:
        return None, "action_bar_missing"
    action_bar = root_children[0]
    bar_rect = _rect(action_bar)
    if (_control_type(action_bar) != "GroupControl" or not _visible_enabled(action_bar)
            or bar_rect is None or not _inside(root_rect, bar_rect)):
        return None, "action_bar_invalid"
    root_width, root_height = root_rect[2] - root_rect[0], root_rect[3] - root_rect[1]
    bar_width, bar_height = bar_rect[2] - bar_rect[0], bar_rect[3] - bar_rect[1]
    if not (root_width * .20 <= bar_width <= root_width * .75
            and 24 <= bar_height <= root_height * .30
            and abs(bar_rect[3] - root_rect[3]) <= 4):
        return None, "action_bar_geometry"
    parts = _children(action_bar)
    if diagnostics is not None:
        # Types only: do not log text, including recognition results or tooltips.
        diagnostics["bar_child_types"] = [_control_type(item) for item in parts[:12]]
    # Saved real snapshots show the content switches from TextControl to a
    # GroupControl during recognition. The two action siblings remain 2 and 3.
    if (len(parts) != 4 or _control_type(parts[0]) != "GroupControl"
            or _control_type(parts[1]) not in ("TextControl", "GroupControl")
            or any(_has_invoke(item, invoke_pattern_id) for item in parts[:2])):
        return None, "action_pair_shape"
    left, right = parts[2], parts[3]
    if not all(_control_type(item) == "GroupControl" and _visible_enabled(item)
                   and _value(item, "Name") == "" and _value(item, "AutomationId") == ""
                   and _has_invoke(item, invoke_pattern_id) for item in (left, right)):
        return None, "action_pair_not_unique"
    left_rect, right_rect = _rect(left), _rect(right)
    if left_rect is None or right_rect is None or not (_inside(bar_rect, left_rect) and _inside(bar_rect, right_rect)):
        return None, "action_pair_geometry"
    left_width, left_height = left_rect[2] - left_rect[0], left_rect[3] - left_rect[1]
    right_width, right_height = right_rect[2] - right_rect[0], right_rect[3] - right_rect[1]
    if not (left_rect[0] < right_rect[0] and left_rect[2] <= right_rect[0]
            and abs(left_rect[1] - right_rect[1]) <= 3
            and abs(left_width - right_width) <= 3 and abs(left_height - right_height) <= 3
            and 20 <= left_width <= 96 and 20 <= left_height <= 96):
        return None, "action_pair_geometry"
    return right, root_route + (0, 3)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest().upper()


def installed_sogou():
    from ovb_rc003 import voice_program_manager
    executable = voice_program_manager.discover_sogou_voice_executable()
    if executable is None or executable.name.casefold() != EXPECTED_EXE:
        return None, "sogou_executable_missing"
    package = executable.parent / "resources" / "app.asar"
    try:
        actual = sha256(package)
    except OSError:
        return None, "sogou_package_unreadable"
    if actual != APP_ASAR_SHA256:
        return None, "sogou_package_changed"
    return executable, ""


def _process_path(pid):
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                     ctypes.POINTER(wintypes.DWORD))
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buffer))
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return Path(buffer.value)
    finally:
        kernel32.CloseHandle(handle)
    return None


def matching_sogou_pids(executable: Path):
    from ovb_rc003 import voice_program_manager
    expected = os.path.normcase(os.path.abspath(executable))
    pids = []
    for provider, pid, name in voice_program_manager.diagnostic_voice_processes():
        if provider != "sogou" or str(name).casefold() != EXPECTED_EXE:
            continue
        path = _process_path(pid)
        if path is not None and os.path.normcase(os.path.abspath(path)) == expected:
            pids.append(int(pid))
    return tuple(sorted(set(pids)))


def visible_windows(pids):
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = (callback_type, wintypes.LPARAM)
    user32.EnumWindows.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.IsWindowVisible.argtypes = (wintypes.HWND,)
    user32.IsWindowVisible.restype = wintypes.BOOL
    found = []

    @callback_type
    def visit(hwnd, _):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids and user32.IsWindowVisible(hwnd):
            found.append((int(hwnd), int(pid.value)))
        return True

    if not user32.EnumWindows(visit, 0):
        raise OSError(ctypes.get_last_error(), "EnumWindows failed")
    return tuple(found)


def _capture_active(pid):
    from ovb_rc003 import voice_playback_session_windows as audio
    try:
        return any(session.pid == pid and session.state == 1 for session in audio.read_capture_sessions({pid}))
    except Exception:
        return None


def _scan_windows(executable, auto):
    """Caller owns the UIA context, including any later Invoke on a match."""
    pids = matching_sogou_pids(executable)
    if not pids:
        return {"state": "sogou_process_missing", "action_pair_present": None}, None
    windows = visible_windows(set(pids))
    matches, observations = [], []
    for hwnd, pid in windows:
        observation = {"hwnd": hwnd, "pid": pid, "capture_active": _capture_active(pid)}
        observations.append(observation)
        try:
            control, detail = find_normal_submit(
                auto.ControlFromHandle(hwnd), auto.PatternId.InvokePattern, diagnostics=observation)
            observation["reason"] = "matched" if control is not None else detail
            if control is not None:
                matches.append(Candidate(control, pid, hwnd, detail, _rect(control)))
        except Exception as exc:
            observation["reason"] = "uia_read_" + type(exc).__name__
    metadata = {"windows": observations, "visible_window_count": len(windows)}
    if len(matches) > 1:
        return {"state": "candidate_ambiguous", "action_pair_present": None, **metadata}, None
    if len(matches) == 1:
        candidate = matches[0]
        # Re-read just before returning; earlier capture metadata is diagnostic.
        active = _capture_active(candidate.pid)
        metadata.update(action_pair_present=True, capture_active=active, **candidate.record())
        if active is True:
            return {"state": "ready", **metadata}, candidate
        return {"state": "verified_pair_inactive" if active is False else "capture_unknown", **metadata}, None
    # An opaque or changed visible tree does not prove the pair disappeared.
    return {"state": "submit_not_identified" if windows else "no_visible_window",
            "action_pair_present": None if windows else False, **metadata}, None


def _inspect_with_executable(executable):
    try:
        import uiautomation as auto
        with auto.UIAutomationInitializerInThread():
            return _scan_windows(executable, auto)
    except Exception as exc:
        return {"state": "uia_read_" + type(exc).__name__, "action_pair_present": None}, None


def inspect_once():
    """Read only metadata. A successful result has exactly one safe candidate."""
    executable, reason = installed_sogou()
    if executable is None:
        return {"state": reason}, None
    return _inspect_with_executable(executable)


def invoke_once(executable, expected, *, capture_identity=None):
    """Revalidate a fresh candidate, then call its one normal-submit pattern."""
    try:
        import uiautomation as auto
        with auto.UIAutomationInitializerInThread():
            observation, candidate = _scan_windows(executable, auto)
            if candidate is None:
                return observation
            if candidate.record() != expected:
                return {**observation, "state": "candidate_changed"}
            # GroupControl has no GetInvokePattern convenience method. Use the
            # same generic API that successfully detected the pattern above.
            pattern = candidate.control.GetPattern(auto.PatternId.InvokePattern)
            if pattern is None:
                return {"state": "invoke_pattern_missing"}
            if _capture_active(candidate.pid) is not True:
                return {"state": "capture_changed_before_invoke"}
            if capture_identity is not None and (candidate.pid != capture_identity[2]
                    or not _same_capture_active(capture_identity)):
                return {"state": "capture_identity_changed"}
            if not bool(pattern.Invoke(waitTime=0)):
                return {"state": "invoke_failed", **candidate.record()}
            return {"state": "invoked_once", **candidate.record()}
    except Exception as exc:
        return {"state": "uia_invoke_" + type(exc).__name__}



def _valid_identity(value):
    return (isinstance(value, (tuple, list)) and len(value) == 3
            and all(isinstance(part, str) and 0 < len(part) <= 4096 for part in value[:2])
            and type(value[2]) is int and 0 < value[2] <= 0xffffffff)


def _same_capture_active(identity):
    from . import voice_playback_session_windows as audio
    try:
        active = {s.identity for s in audio.read_capture_sessions({identity[2]}) if s.state == 1}
        return active == {tuple(identity)}
    except Exception:
        return False


def finish_bound_capture(identity):
    """Only used in the bounded child: revalidate this attempt, then invoke once."""
    if not _valid_identity(identity):
        return {"state": "capture_identity_invalid"}
    executable, reason = installed_sogou()
    if executable is None:
        return {"state": reason}
    if not _same_capture_active(identity):
        return {"state": "capture_identity_changed"}
    observation, candidate = _inspect_with_executable(executable)
    if candidate is None:
        return {"state": observation["state"]}
    if candidate.pid != identity[2]:
        return {"state": "capture_identity_changed"}
    result = invoke_once(executable, candidate.record(), capture_identity=identity)
    return {"state": result["state"]}


def child_main(args):
    # Hidden entry point, never falls through to starting a desktop or service.
    if len(args) != 2:
        return 2
    try:
        request, result = map(Path, args)
        if (not request.is_absolute() or not result.is_absolute()
                or request == result or result.exists() or request.stat().st_size > 16384):
            return 2
        identity = json.loads(request.read_text(encoding="utf-8"))
        if not _valid_identity(identity):
            return 2
        outcome = finish_bound_capture(tuple(identity))
        with result.open("x", encoding="utf-8") as stream:
            json.dump(outcome, stream)
        return 0
    except Exception:
        return 1


def _read_result(path, returncode):
    if returncode != 0:
        return "helper_failed"
    with open(path, encoding="utf-8") as stream:
        raw = stream.read(257)
    if len(raw) > 256:
        return "helper_result_invalid"
    value = json.loads(raw)
    if (not isinstance(value, dict) or set(value) != {"state"}
            or not isinstance(value["state"], str) or not value["state"]
            or len(value["state"]) > 80
            or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_0123456789" for c in value["state"])):
        return "helper_result_invalid"
    return value["state"]


def finish_with_timeout(identity, *, cancel_event):
    """One bounded request; timeout may mean Invoke happened, so never retry."""
    from . import windows_diagnostics
    if not _valid_identity(identity):
        return "capture_identity_unavailable"
    try:
        with tempfile.TemporaryDirectory(prefix="remote-mic-sogou-finish-") as directory:
            request, result = Path(directory) / "request.json", Path(directory) / "result.json"
            request.write_text(json.dumps(identity), encoding="utf-8")
            command = [sys.executable]
            if not getattr(sys, "frozen", False):
                command += ["-m", "ovb_rc003"]
            command += [FLAG, str(request), str(result)]
            return windows_diagnostics._run_ble_diagnostics_subprocess(
                command, result_path=str(result), cancel_event=cancel_event,
                timeout=TIMEOUT_SECONDS, terminate_wait=.25, kill_wait=.25,
                result_reader=_read_result)
    except windows_diagnostics.BleDiscoveryCancelledError:
        return "helper_cancelled_or_timed_out"
    except Exception:
        return "helper_result_unknown"
