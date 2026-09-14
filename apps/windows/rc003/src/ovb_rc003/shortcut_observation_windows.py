"""Explicit, bounded receiver diagnostics; never starts the desktop or sends input.

Owned by RC003 diagnostics, dispatched before normal startup. The operator names
keys/duration/output; only those keys plus modifier edges are observed. Output is
local diagnostic evidence, not configuration, and can be deleted after use.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import json
from pathlib import Path
import queue
import sys
import threading
import time

from . import __version__, win32_keys
from .diagnostic_trace import foreground_context
from .input_environment_diagnostics_windows import EnvironmentSampler

FLAG = "--observe-shortcut"
MAX_EVENTS = 2000
MODIFIERS = frozenset(range(0xA0, 0xA6)) | {0x5B, 0x5C, 0x10, 0x11, 0x12}
_LIVE_CALLBACKS = []  # Keep a failed-to-unhook callback valid until this collector process exits.


class _KeyboardEvent(ctypes.Structure):
    _fields_ = [("vk", wintypes.DWORD), ("scan", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("extra", ctypes.c_size_t)]


def observed_edge(code: int, message: int, event: _KeyboardEvent, allowed: set[int]) -> dict | None:
    if code < 0 or message not in (0x100, 0x101, 0x104, 0x105) or event.vk not in allowed:
        return None
    return dict(event="received_key_edge", vk=int(event.vk), scan_code=int(event.scan),
                edge="up" if message in (0x101, 0x105) else "down", flags=int(event.flags),
                injected=bool(event.flags & 0x10), lower_integrity=bool(event.flags & 2),
                system_event_ms=int(event.time), origin="unknown", target_response="unknown")


class _EvidenceWriter:
    """No filesystem writes or process queries inside the low-level callback."""
    def __init__(self, stream):
        self.stream = stream
        self.queue = queue.Queue(maxsize=256)
        self.dropped = 0
        self.failed = False
        self.sampling_closed = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True, name="shortcut-observation-writer")
        self.thread.start()

    def emit(self, item):
        payload = dict(wall_time=time.time(), monotonic_ns=time.monotonic_ns(), **item)
        try:
            self.queue.put_nowait(payload)
        except queue.Full:
            self.dropped += 1

    def _run(self):
        environment, previous = None, None
        try:
            while True:
                item = self.queue.get()
                if item is None:
                    return
                if item.get('event') == '_sample_context':
                    if self.sampling_closed.is_set():
                        continue
                    # All process/token work stays off the hook's message-pump thread.
                    try:
                        if environment is None:
                            environment = EnvironmentSampler()
                        context = foreground_context()
                        context.update(environment.sample(context))
                    except Exception:
                        context = {'environment_status': 'capture_failed'}
                    if context == previous:
                        continue
                    previous = context
                    item = dict(event='receiver_context', wall_time=time.time(),
                                monotonic_ns=time.monotonic_ns(), **context)
                self.stream.write(json.dumps(item, ensure_ascii=True, separators=(",", ":")) + "\n")
                self.stream.flush()
        except OSError:
            self.failed = True

    def close(self):
        # Bound shutdown even when storage stalls; hook is already uninstalled.
        self.sampling_closed.set()
        try:
            self.queue.put(None, timeout=0.5)
        except queue.Full:
            self.failed = True
        self.thread.join(timeout=1)
        if self.thread.is_alive():
            self.failed = True


def observe(allowed: set[int], seconds: int, writer: _EvidenceWriter) -> str:
    """Runs a hook/message loop on the calling thread; all events pass onward."""
    if sys.platform != "win32":
        return "unsupported_platform"
    user = ctypes.WinDLL("user32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
    user.SetWindowsHookExW.argtypes = (ctypes.c_int, callback_type, wintypes.HINSTANCE, wintypes.DWORD)
    user.SetWindowsHookExW.restype = wintypes.HANDLE
    user.CallNextHookEx.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
    user.CallNextHookEx.restype = ctypes.c_ssize_t
    user.UnhookWindowsHookEx.argtypes = (wintypes.HANDLE,)
    user.UnhookWindowsHookEx.restype = wintypes.BOOL
    user.PeekMessageW.argtypes = (ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT)
    user.PeekMessageW.restype = wintypes.BOOL
    user.TranslateMessage.argtypes = (ctypes.POINTER(wintypes.MSG),)
    user.DispatchMessageW.argtypes = (ctypes.POINTER(wintypes.MSG),)
    user.DispatchMessageW.restype = ctypes.c_ssize_t
    kernel.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
    kernel.GetModuleHandleW.restype = wintypes.HMODULE
    count = 0
    def callback(code, message, pointer):
        nonlocal count
        try:
            if code >= 0 and count < MAX_EVENTS:
                event = ctypes.cast(pointer, ctypes.POINTER(_KeyboardEvent)).contents
                item = observed_edge(code, int(message), event, allowed)
                if item is not None:
                    count += 1
                    writer.emit(item)
        except Exception:
            pass
        return user.CallNextHookEx(None, code, message, pointer)
    callback_ref = callback_type(callback)
    handle = user.SetWindowsHookExW(13, callback_ref, kernel.GetModuleHandleW(None), 0)
    if not handle:
        return "hook_install_failed"
    result = "completed"
    try:
        deadline, next_sample = time.monotonic() + seconds, 0.0
        message = wintypes.MSG()
        while time.monotonic() < deadline and not writer.failed and count < MAX_EVENTS:
            # Limit each pump batch so a continuous message stream cannot bypass the deadline.
            for _ in range(64):
                if not user.PeekMessageW(ctypes.byref(message), None, 0, 0, 1):
                    break
                user.TranslateMessage(ctypes.byref(message))
                user.DispatchMessageW(ctypes.byref(message))
            if time.monotonic() >= next_sample:
                next_sample = time.monotonic() + 0.25
                writer.emit(dict(event='_sample_context'))
            time.sleep(0.005)
        if count >= MAX_EVENTS:
            result = "event_limit_reached"
        elif writer.failed:
            result = "writer_failed"
    finally:
        if not user.UnhookWindowsHookEx(handle):
            _LIVE_CALLBACKS.append(callback_ref)
            result = "unhook_failed_process_exit_required"
    return result


def main(args: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="限时只读接收采集，不启动无线麦服务；不记录文本。")
    parser.add_argument("--keys", required=True, help="需要对照的键，例如 ralt+i；始终包括修饰键")
    parser.add_argument("--seconds", type=int, default=30)
    parser.add_argument("--output", required=True, type=Path, help="不存在的绝对 JSONL 文件路径")
    parser.add_argument("--dry-run", action="store_true", help="只生成计划，不安装按键钩子")
    options = parser.parse_args(args)
    try:
        tokens = options.keys.split("+")
        if not 1 <= len(tokens) <= 6 or not 1 <= options.seconds <= 120:
            return 2
        allowed = set(win32_keys.resolve_vk_codes(tokens)) | set(MODIFIERS)
        if not options.output.is_absolute() or options.output.suffix.lower() != ".jsonl":
            return 2
        # Exclusive creation protects user files and keeps this outside app config/log ownership.
        with options.output.open("x", encoding="utf-8") as stream:
            writer = _EvidenceWriter(stream)
            writer.emit(dict(event="observation_started", schema_version=1, app_version=__version__,
                             scope="specified_keys_and_modifiers_only", keys=sorted(allowed),
                             duration_seconds=options.seconds, event_limit=MAX_EVENTS,
                             utc_started_at=datetime.now(timezone.utc).isoformat(),
                             dry_run=options.dry_run, origin_attribution="not_available"))
            result = "dry_run"
            try:
                if not options.dry_run:
                    result = observe(allowed, options.seconds, writer)
            except (OSError, ValueError, AttributeError):
                result = "observation_failed"
            except KeyboardInterrupt:
                result = "cancelled"
            finally:
                writer.emit(dict(event="observation_finished", result=result, dropped=writer.dropped))
                writer.close()
            return 0 if result in ("completed", "dry_run", "cancelled", "event_limit_reached") and not writer.failed else 1
    except (OSError, ValueError, KeyError):
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
