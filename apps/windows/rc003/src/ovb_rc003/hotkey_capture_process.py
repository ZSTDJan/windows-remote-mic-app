"""Disposable settings recorder, separate from the Raw Input owner.

Windows can bypass the entire low-level keyboard hook chain when its head
belongs to the foreground process and that process registered keyboard Raw
Input. Keep the existing recorder in a child without Raw Input registration.
Only inherited anonymous pipes carry one chord and fixed diagnostics; no files,
desktop window, settings, device connection or elevated privileges are used.
Closing the parent pipe ends capture, including abrupt parent exit.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading

from .hotkey_capture_windows import HotkeyCapture, HotkeyCaptureUnavailableError

FLAG = "--capture-hotkey-worker"
_MAX_LINE = 8192
_LOGGER = logging.getLogger("ovb_rc003")


def _command(accept_injected):
    if getattr(sys, "frozen", False):
        command = [sys.executable, FLAG]
    else:
        # Windows venv python.exe is a redirector with another child. Own the
        # actual interpreter so terminate/wait also owns the installed hook.
        # This worker and its key resolver use only the standard library.
        command = [getattr(sys, "_base_executable", sys.executable), "-c",
                   "import sys; from ovb_rc003.hotkey_capture_process import child_main; "
                   "raise SystemExit(child_main(sys.argv[1:]))"]
    if accept_injected:
        command.append("--accept-injected")
    return command


class ProcessCapture:
    def __init__(self, on_captured, *, accept_injected=False, on_error=None):
        self._on_captured = on_captured
        self._on_error = on_error
        self._accept_injected = accept_injected
        self._process = None
        self._thread = None
        self._ready = threading.Event()
        self._stopping = threading.Event()
        self._error = None
        self._lock = threading.Lock()
        self.diagnostics = {}

    @property
    def is_running(self):
        return bool(self._thread and self._thread.is_alive()) or bool(
            self._process and self._process.poll() is None
        )

    def start(self, *, start_timeout=5.0):
        if sys.platform != "win32":
            raise HotkeyCaptureUnavailableError("keyboard shortcut capture is only available on Windows")
        if self.is_running:
            raise HotkeyCaptureUnavailableError("keyboard shortcut capture is already running")
        self._ready.clear()
        self._stopping.clear()
        self._error = None
        self.diagnostics = {}
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if not self._ready.wait(start_timeout):
            _LOGGER.error("settings hotkey capture: child failed reason=start_timeout",
                          extra={"failure_key": "hotkey_capture_diagnostics"})
            self.stop()
            raise HotkeyCaptureUnavailableError("keyboard shortcut capture child startup timed out")
        if self._error:
            self.stop()
            raise HotkeyCaptureUnavailableError(self._error)

    def _close_input(self):
        with self._lock:
            process = self._process
            if process and process.stdin and not process.stdin.closed:
                process.stdin.close()

    def _record_diagnostics(self, state):
        if not isinstance(state, dict):
            raise ValueError("invalid child diagnostics")
        self.diagnostics = dict(state, child_pid=self._process.pid)
        failed = bool(state.get("callback_errors") or state.get("result_delivery_errors")
                      or state.get("diagnostic_log_errors")
                      or state.get("worker_error_type", "none") != "none"
                      or state.get("message_result") == -1
                      or state.get("unhook_succeeded") is False)
        _LOGGER.log(logging.ERROR if failed else logging.INFO,
                    "settings hotkey capture: diagnostics reason=child_exit state=%s",
                    json.dumps(self.diagnostics, separators=(",", ":")),
                    extra={"failure_key": "hotkey_capture_diagnostics" if failed else ""})

    def stop(self):
        self._stopping.set()
        self._close_input()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(2.5)
        process = self._process
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(1)
        if thread and thread is not threading.current_thread():
            thread.join(1)
            if thread.is_alive():
                raise HotkeyCaptureUnavailableError("keyboard shortcut capture child did not stop")

    def _run(self):
        process = None
        was_ready = False
        failure_stage = "launch"
        try:
            env = os.environ.copy()
            if not getattr(sys, "frozen", False):
                # Also works from a compiled native stage or an arbitrary CWD.
                env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
            process = subprocess.Popen(
                _command(self._accept_injected), stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                env=env, bufsize=0,
            )
            with self._lock:
                self._process = process
            failure_stage = "protocol"
            if self._stopping.is_set():
                self._close_input()
            assert process.stdout is not None
            delivered = False
            while True:
                line = process.stdout.readline(_MAX_LINE + 1)
                if not line:
                    break
                if len(line) > _MAX_LINE or not line.endswith(b"\n"):
                    raise ValueError("invalid child response")
                packet = json.loads(line)
                kind = packet.get("kind")
                if kind == "ready":
                    if was_ready:
                        raise ValueError("duplicate child ready")
                    was_ready = True
                    _LOGGER.info("settings hotkey capture: child ready pid=%s", process.pid)
                    self._ready.set()
                elif kind == "result" and was_ready and not delivered:
                    chord = packet.get("chord")
                    if not isinstance(chord, str) or not chord or len(chord) > 2048:
                        raise ValueError("invalid child chord")
                    delivered = True
                    self._close_input()
                    if not self._stopping.is_set():
                        self._on_captured(chord)
                elif kind == "diagnostics":
                    self._record_diagnostics(packet["state"])
                elif kind == "error":
                    self._record_diagnostics(packet.get("state", {}))
                    raise RuntimeError("keyboard shortcut capture child failed")
                else:
                    raise ValueError("unexpected child response")
            if not self._ready.is_set():
                failure_stage = "exit_before_ready"
                raise RuntimeError("keyboard shortcut capture child exited before ready")
            if not delivered and not self._stopping.is_set():
                failure_stage = "exit_during_recording"
                raise RuntimeError("keyboard shortcut capture child exited during recording")
        except Exception as exc:
            self._error = f"keyboard shortcut capture child: {type(exc).__name__}"
            _LOGGER.error("settings hotkey capture: child failed reason=%s error_type=%s",
                          failure_stage, type(exc).__name__,
                          extra={"failure_key": "hotkey_capture_diagnostics"})
        finally:
            self._close_input()
            if process:
                try:
                    process.wait(2.5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(1)
                if process.stdout:
                    process.stdout.close()
            if self._error and was_ready and not self._stopping.is_set() and self._on_error:
                try:
                    self._on_error("快捷键录入已中断，请重新点击录入。")
                except Exception:
                    _LOGGER.exception("settings hotkey capture: error receiver failed")
            self._ready.set()


def _pipe_stream(name, std_id, mode):
    stream = getattr(sys, name, None)
    if stream is not None:
        return stream.buffer
    # PyInstaller's windowed bootloader sets sys.stdin/stdout to None even
    # when Popen supplied real inherited standard handles.
    import ctypes
    from ctypes import wintypes
    import msvcrt
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetStdHandle.argtypes = [wintypes.DWORD]
    kernel.GetStdHandle.restype = wintypes.HANDLE
    handle = kernel.GetStdHandle(std_id)
    if not handle or handle == ctypes.c_void_p(-1).value:
        raise OSError("capture pipe unavailable")
    fd = msvcrt.open_osfhandle(handle, os.O_BINARY | (os.O_RDONLY if mode == "rb" else os.O_WRONLY))
    return os.fdopen(fd, mode, buffering=0)


def child_main(args):
    if args not in ([], ["--accept-injected"]) or sys.platform != "win32":
        return 2
    incoming = _pipe_stream("stdin", -10, "rb")
    outgoing = _pipe_stream("stdout", -11, "wb")
    events = queue.SimpleQueue()
    stopped = threading.Event()

    def write(kind, **values):
        outgoing.write((json.dumps(dict(kind=kind, **values)) + "\n").encode("utf8"))
        outgoing.flush()

    def watch_parent():
        try:
            # No buffered-reader lock survives interpreter shutdown after a
            # successful chord. EOF covers an abruptly terminated parent.
            os.read(incoming.fileno(), 1)
        except OSError:
            pass
        finally:
            try:
                incoming.close()
            finally:
                stopped.set()

    threading.Thread(target=watch_parent, daemon=True).start()
    capture = HotkeyCapture(events.put, accept_injected=bool(args))
    try:
        if stopped.is_set():
            return 0
        capture.start()
        if not stopped.is_set():
            write("ready")
        while not stopped.wait(.01):
            try:
                chord = events.get_nowait()
            except queue.Empty:
                if not capture.is_running:
                    break
                continue
            write("result", chord=chord)
            break
        capture.stop()
        write("diagnostics", state=capture._diagnostics)
        return 0
    except Exception:
        try:
            write("error", state=capture._diagnostics)
        except OSError:
            pass
        return 1
    finally:
        try:
            capture.stop()
        finally:
            # Only the reader closes stdin: closing its CRT descriptor from
            # another thread can wait forever behind that thread's read.
            outgoing.close()
