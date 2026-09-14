"""One-shot manual voice-hotkey comparison, outside the app runtime.

Owned by RC003 diagnostics; this tracked script is not a second runtime backend
and is not packaged. It reuses the existing raw event builders, without starting
BLE, audio, or input-profile switching. Only the explicit doubao-compat method
temporarily starts the existing local hook and verified Doubao Frida hook, then
stops both. No driver or configuration changes are made. A plain keybd_event test omits the
application's marker/compatibility processing; it does not reproduce that chain.

Without --send, only the planned events are printed. Live runs require an
explicit foreground process ID and an unused JSON output path. The operator
selects the input method and observes the bubble; API return counts cannot prove
that a bubble appeared. Keep temporary result files outside Git and remove them
after the investigation. Run with the RC003 .venv Python and PYTHONPATH=src.

The scan mode uses the modifier encoding and 80 ms edge spacing documented
in ATTRIBUTION.md, which owns the exact upstream reference.
"""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from contextlib import contextmanager, nullcontext
from dataclasses import asdict
from datetime import datetime
import json
import math
import os
from pathlib import Path
import sys
import time

from ovb_rc003 import (
    diagnostic_trace, doubao_rpc, hid_elevation_windows, single_instance,
    voice_key_physicalizer_windows, win32_input, win32_keys,
)
from ovb_rc003.hotkey import HotkeySpec
from ovb_rc003.voice_interaction_diagnostics_windows import capture_focus_snapshot


EDGE_GAP_SECONDS = 0.080
METHODS = ("sendinput-vk", "sendinput-scan", "keybd-event", "doubao-compat")


def planned_edge(method: str, vk: int, key_up: bool) -> dict:
    if method in ("keybd-event", "doubao-compat"):
        return {
            "vk": vk, "key_up": key_up,
            "extra_info": "per-edge ticket; see trace" if method == "doubao-compat" else 0,
            "scan_code": "MapVirtualKeyW(vk, 0)",
            "flags": (2 if key_up else 0)
            | (1 if vk in win32_input._EXTENDED_KEYS else 0),
        }
    builder = (
        win32_input._build_input_array
        if method == "sendinput-scan"
        else win32_input._build_virtual_key_input_array
    )
    array, _ = builder([(vk, key_up)])
    event = array[0].union.ki
    return {
        "vk": int(event.wVk), "scan_code": int(event.wScan),
        "flags": int(event.dwFlags), "extra_info": int(event.dwExtraInfo),
        "key_up": key_up,
    }


def send_edge(method: str, vk: int, key_up: bool) -> int | None:
    if method == "doubao-compat":
        win32_input._real_voice_event(vk, key_up)
        return None
    if method == "keybd-event":
        win32_input._real_keybd_event(vk, key_up, _extra_info=0)
        return None  # This API has no delivery-count return value.
    sender = (
        win32_input._real_send_input_batch
        if method == "sendinput-scan"
        else win32_input._real_send_virtual_key_input_batch
    )
    return sender([(vk, key_up)])


def start_doubao_target(target):
    # Use the production discovery, verification and resource ownership path.
    if not target.start([0xA5]):
        raise RuntimeError(f"Doubao compatibility unavailable: {target.status}: {target.error}")


@contextmanager
def doubao_compatibility(report, trace_root: Path):
    win32_input._require_live_input_allowed()
    require_app_stopped()
    trace_root.mkdir(exist_ok=False)
    trace = diagnostic_trace.DiagnosticTrace(trace_root, enabled=True)
    local = voice_key_physicalizer_windows.VoiceKeyPhysicalizer()
    target = doubao_rpc.DoubaoPhysicalizer()
    status = report["compatibility"] = {"trace_file": str(trace.path)}
    voice_key_physicalizer_windows.set_diagnostic_trace(trace)
    doubao_rpc.set_diagnostic_trace(trace)
    attempt = trace.begin_attempt(None, "doubao_ime_probe")

    def sender(method, vk, key_up):
        target.expect_markers("up" if key_up else "down", 1)
        return send_edge(method, vk, key_up)

    try:
        local.start()
        status["local_hook_running"] = local.is_running
        start_doubao_target(target)
        status["target_hook_status"] = target.status
        yield sender
    finally:
        errors = []
        # Keep the local confirmation hook alive until target cleanup is done.
        for name, component in (("target", target), ("local", local)):
            try:
                component.stop()
                status[name + "_stopped"] = True
            except BaseException as exc:
                status[name + "_stopped"] = False
                errors.append(f"{name}: {type(exc).__name__}: {exc}")
        status["cleanup_errors"] = errors
        trace.end_attempt(attempt, "context_finished", cleanup_complete=not errors)
        voice_key_physicalizer_windows.set_diagnostic_trace(None)
        doubao_rpc.set_diagnostic_trace(None)
        trace.close()
        if errors:
            raise RuntimeError("Compatibility cleanup incomplete; see cleanup_errors")


def run_hold(vks, method, hold_seconds, report, check_target, *,
             sender=send_edge, sleep=time.sleep):
    attempted = []
    release_errors = []

    def edge(vk, key_up):
        record = planned_edge(method, vk, key_up)
        record["started_monotonic_ns"] = time.monotonic_ns()
        report["edges"].append(record)
        try:
            returned = sender(method, vk, key_up)
            record["returned"] = returned
            if returned is not None and returned != 1:
                raise OSError(f"Only {returned}/1 events submitted")
        except BaseException as exc:
            record["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            record["finished_monotonic_ns"] = time.monotonic_ns()

    try:
        for index, vk in enumerate(vks):
            if index:
                sleep(EDGE_GAP_SECONDS)
            check_target()
            # Release even an ambiguous failed DOWN, but never an unattempted key.
            attempted.append(vk)
            edge(vk, False)
        sleep(hold_seconds)
    finally:
        for index, vk in enumerate(reversed(attempted)):
            try:
                if index:
                    sleep(EDGE_GAP_SECONDS)
            except BaseException as exc:
                release_errors.append(f"release delay: {type(exc).__name__}")
            try:
                edge(vk, True)
            except BaseException as exc:
                release_errors.append(f"VK {vk}: {type(exc).__name__}: {exc}")
        report["release_errors"] = release_errors
        if release_errors:
            raise OSError("Release was not fully confirmed; see release_errors")


def keys_down() -> list[int]:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
    user32.GetAsyncKeyState.restype = ctypes.c_short
    return [vk for vk in range(1, 255) if user32.GetAsyncKeyState(vk) & 0x8000]


def require_app_stopped() -> None:
    if (single_instance.application_instance_running()
            or single_instance.bridge_instance_running()
            or single_instance.element_navigation_instance_running()):
        raise RuntimeError("Exit Remote Mic and its navigation helper before this test")


def require_target(pid: int, focus_handle: int = 0):
    focus = capture_focus_snapshot()
    if (not focus.supported or focus.error or focus.foreground_pid != pid
            or not focus.focus_handle
            or (focus_handle and focus.focus_handle != focus_handle)):
        raise RuntimeError("Target text window lost focus; no further DOWN will be sent")
    return focus


def environment() -> dict:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = (
        wintypes.HWND, ctypes.POINTER(wintypes.DWORD),
    )
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetKeyboardLayout.argtypes = (wintypes.DWORD,)
    user32.GetKeyboardLayout.restype = wintypes.HANDLE
    thread = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
    return {
        "computer_name": os.environ.get("COMPUTERNAME", ""),
        "pid": os.getpid(), "python": sys.executable, "version": sys.version,
        "elevated": hid_elevation_windows.query_process_elevated(),
        "foreground_hkl": int(user32.GetKeyboardLayout(thread) or 0),
        "hkl_note": "HKL alone does not identify the active TSF input method",
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", required=True, choices=("wetype", "doubao"))
    parser.add_argument("--hotkey", required=True)
    parser.add_argument("--method", required=True, choices=METHODS)
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--target-pid", type=int)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--delay-seconds", type=float, default=7.0)
    parser.add_argument("--hold-seconds", type=float, default=1.2)
    args = parser.parse_args(argv)
    if (not math.isfinite(args.delay_seconds) or not 2 <= args.delay_seconds <= 30
            or not math.isfinite(args.hold_seconds) or not 0.2 <= args.hold_seconds <= 3):
        parser.error("delay must be 2..30 seconds; hold must be 0.2..3 seconds")
    try:
        spec = HotkeySpec.parse(args.hotkey)
        vks = win32_keys.resolve_vk_codes((*spec.modifiers, spec.key))
    except ValueError as exc:
        parser.error(str(exc))
    if len(vks) > 4:
        parser.error("at most four keys are supported")
    if args.method == "doubao-compat" and (args.provider != "doubao" or list(vks) != [0xA5]):
        parser.error("doubao-compat is restricted to the Doubao right-Alt test")
    report = {
        "mode": "live" if args.send else "plan_only",
        "provider_selected_by_operator": args.provider,
        "hotkey": spec.serialize(), "method": args.method,
        "edge_gap_seconds": EDGE_GAP_SECONDS, "hold_seconds": args.hold_seconds,
        "bubble_result": "not_observed_by_probe_requires_operator",
        "plan": [planned_edge(args.method, vk, up)
                 for up, keys in ((False, vks), (True, reversed(vks))) for vk in keys],
        "edges": [],
    }
    if not args.send:
        print(json.dumps(report, indent=2))
        return 0
    if sys.platform != "win32":
        parser.error("live tests require Windows")
    if not args.target_pid or args.target_pid <= 0 or args.output is None:
        parser.error("--send requires --target-pid and an unused --output path")

    # Reserve evidence output before sending; never overwrite an existing run.
    with args.output.open("x", encoding="utf-8") as output:
        result = 0
        report["started_at"] = datetime.now().astimezone().isoformat()
        try:
            win32_input._require_live_input_allowed()
            require_app_stopped()
            print(f"Focus PID {args.target_pid}; one hold begins in {args.delay_seconds:g}s.",
                  flush=True)
            time.sleep(args.delay_seconds)
            require_app_stopped()
            before = require_target(args.target_pid)
            report["focus_before"] = asdict(before)
            report["environment"] = environment()
            report["keys_down_before"] = keys_down()
            if report["keys_down_before"]:
                raise RuntimeError("Keys/buttons are still down; test cancelled without cleanup")
            context = (
                doubao_compatibility(report, args.output.with_suffix(".trace"))
                if args.method == "doubao-compat" else nullcontext(send_edge)
            )
            with context as sender:
                if keys_down():
                    raise RuntimeError("Keys/buttons changed during setup; no DOWN sent")
                run_hold(vks, args.method, args.hold_seconds, report,
                         lambda: require_target(args.target_pid, before.focus_handle),
                         sender=sender)
                if args.method == "doubao-compat":
                    time.sleep(0.85)  # Allow the existing 750 ms marker report to finish.
            time.sleep(0.05)
            report["focus_after"] = asdict(capture_focus_snapshot())
            report["keys_down_after"] = keys_down()
            if any(vk in report["keys_down_after"] for vk in vks):
                raise RuntimeError("A tested key remains down; stop further tests")
            report["status"] = "sequence_finished_bubble_unverified"
        except (Exception, KeyboardInterrupt) as exc:
            report["status"] = "aborted_or_failed"
            report["error"] = f"{type(exc).__name__}: {exc}"
            result = 1
        finally:
            report["finished_at"] = datetime.now().astimezone().isoformat()
            json.dump(report, output, ensure_ascii=False, indent=2)
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
        print(f"Result: {args.output}", flush=True)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
