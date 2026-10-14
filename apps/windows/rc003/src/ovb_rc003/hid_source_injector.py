"""One-shot, consented Xiaomi injector for the source development entry.

Only the fixed launcher and verified current HID host are accepted. Reuse the
existing same-user Windows pipe transport, with a separate namespace and a
single start/result exchange; no installed task, request files or arbitrary
elevated commands. The desktop process remains unelevated.
"""
from __future__ import annotations

import argparse
import ctypes as C
from ctypes import wintypes as W
from dataclasses import dataclass
import hmac
import json
import logging
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import threading
import time

from . import chromecast_pipe_windows as transport
from . import dev_session, hid_injection_diagnostics as diagnostics

FLAG = "--rc003-source-injector"
AUTHORIZATION_TIMEOUT = 60.0
WORKER_TIMEOUT = 45.0


class SourceInjectionError(RuntimeError):
    def __init__(self, detail, diagnostic=None):
        super().__init__(detail)
        self.injection_diagnostic = diagnostic or {}


@dataclass(frozen=True)
class Identity:
    generation: str
    token: str

    def __post_init__(self):
        if not (re.fullmatch(r"[a-f0-9]{32}", self.generation)
                and re.fullmatch(r"[a-f0-9]{64}", self.token)):
            raise ValueError("invalid_source_injector_identity")


class ResultPipe(transport.Pipe):
    NAME_PREFIX = "\\\\.\\pipe\\RemoteMic.SourceHid."


def _frame(identity, kind):
    return json.dumps(dict(generation=identity.generation, token=identity.token,
                           kind=kind), separators=(",", ":")).encode("ascii")


def _verify_frame(data, identity, kind):
    # Exact bytes also reject duplicate fields and unexpected commands.
    if not isinstance(data, bytes) or not hmac.compare_digest(data, _frame(identity, kind)):
        raise SourceInjectionError("source_injector_channel_invalid")


def build_command(parent, identity, pid):
    if getattr(sys, "frozen", False):
        raise SourceInjectionError("source_injector_source_only")
    launcher = Path(__file__).resolve().parents[1] / "launcher.py"
    return parent.image, dev_session.mark_command([
        str(launcher), FLAG, str(parent.pid), str(parent.born),
        identity.generation, identity.token, str(pid),
    ])


def _launch(parent, identity, pid):
    executable, arguments = build_command(parent, identity, pid)
    shell = C.WinDLL("shell32", use_last_error=True)
    transport._bind(shell, "ShellExecuteExW", W.BOOL, C.POINTER(transport.ShellInfo))
    info = transport.ShellInfo()
    info.size, info.mask, info.show = C.sizeof(info), 0x40 | 0x100, 0
    info.verb, info.file = "runas", executable
    info.parameters = subprocess.list2cmdline(arguments)
    if not shell.ShellExecuteExW(C.byref(info)):
        error = C.get_last_error()
        raise SourceInjectionError(
            "source_authorization_cancelled" if error == 1223 else "source_authorization_failed",
            dict(stage="child_launch", winerror=error, error_type="OSError"))
    if not info.process:
        raise SourceInjectionError("source_authorization_failed", dict(stage="child_launch"))
    return info.process


def _dispose_process(handle):
    k, _ = transport.api()
    try:
        state = k.WaitForSingleObject(handle, 0)
        if state == 258:
            transport._bind(k, "TerminateProcess", W.BOOL, W.HANDLE, W.UINT)
            k.TerminateProcess(handle, 5)
            if k.WaitForSingleObject(handle, 2000) != 0:
                raise SourceInjectionError("source_injector_exit_unconfirmed")
        elif state != 0:
            raise SourceInjectionError("source_injector_exit_unconfirmed")
    finally:
        k.CloseHandle(handle)


class _PendingLaunch:
    """A late UAC response cannot start injection after the request was cancelled."""
    def __init__(self, parent, identity, pid):
        self.lock, self.done = threading.Lock(), threading.Event()
        self.handle = self.error = None
        self.closed = False
        self.thread = threading.Thread(target=self._run, args=(parent, identity, pid),
                                       name="source-hid-authorization", daemon=True)
        self.thread.start()

    def _run(self, parent, identity, pid):
        try:
            handle = _launch(parent, identity, pid)
            with self.lock:
                if not self.closed:
                    self.handle = handle
                    handle = None
            if handle is not None:
                _dispose_process(handle)
        except Exception as exc:
            self.error = exc
        finally:
            self.done.set()

    def close(self):
        with self.lock:
            self.closed = True
            handle, self.handle = self.handle, None
        if handle is not None:
            _dispose_process(handle)


def _read(pipe, deadline, cancel, process=None):
    while not cancel.is_set() and time.monotonic() < deadline:
        data = pipe.read()
        if data is not None:
            return data
        if process is not None and pipe.k.WaitForSingleObject(process, 0) != 258:
            raise SourceInjectionError("source_injector_exited")
        cancel.wait(.02)
    raise SourceInjectionError("source_injector_cancelled" if cancel.is_set()
                               else "source_injector_timeout")


def run(pid, *, timeout=30.0, cancel=None):
    if type(pid) is not int or not 0 < pid <= 0xFFFFFFFF:
        raise ValueError("invalid_source_injector_target")
    if sys.platform != "win32" or getattr(sys, "frozen", False):
        raise SourceInjectionError("source_injector_source_only")
    cancel = cancel if cancel is not None else threading.Event()
    if cancel.is_set():
        raise SourceInjectionError("source_injector_cancelled")
    parent = transport.inspect_peer(os.getpid())
    identity = Identity(secrets.token_hex(16), secrets.token_hex(32))
    pipe = ResultPipe(identity, server=True)
    pending = None
    try:
        pending = _PendingLaunch(parent, identity, pid)
        deadline = time.monotonic() + AUTHORIZATION_TIMEOUT
        while not pending.done.is_set() and not cancel.is_set() and time.monotonic() < deadline:
            pending.done.wait(.02)
        if cancel.is_set():
            raise SourceInjectionError("source_injector_cancelled")
        if not pending.done.is_set():
            raise SourceInjectionError("source_authorization_timeout")
        if pending.error is not None:
            raise pending.error
        process = pending.handle
        launched = transport.inspect_peer(pipe.k.GetProcessId(process))
        deadline = time.monotonic() + timeout
        pipe.connect(deadline, cancel)
        actual = transport.inspect_peer(pipe.peer_pid())
        transport.verify_peer(actual, launched)
        transport.verify_same_user_image(actual, parent)
        if cancel.is_set():
            raise SourceInjectionError("source_injector_cancelled")
        pipe.write(_frame(identity, "start"))
        payload = _read(pipe, deadline, cancel, process)
        value = json.loads(payload)
        code = value.get("exit_code") if isinstance(value, dict) else None
        if type(code) is not int:
            raise SourceInjectionError("source_injector_result_invalid")
        record = diagnostics.decode(payload, exit_code=code, target_pid=pid)
        if (record.get("result_status") != "received"
                or record.get("target_pid") != pid
                or record.get("worker_pid") != actual.pid):
            raise SourceInjectionError("source_injector_result_invalid")
        pipe.write(_frame(identity, "received"))
        if pipe.k.WaitForSingleObject(process, 2000) != 0:
            raise SourceInjectionError("source_injector_exit_unconfirmed")
        exit_code = W.DWORD()
        if not pipe.k.GetExitCodeProcess(process, C.byref(exit_code)) or exit_code.value != code:
            raise SourceInjectionError("source_injector_result_invalid")
        logging.getLogger("ovb_rc003.hid_tap").info(
            "Source HID authorization result: %s", json.dumps(record, ensure_ascii=True))
        return code, record
    finally:
        try:
            pipe.close()
        finally:
            if pending is not None:
                pending.close()


def _watch_parent(pipe, parent, finished):
    deadline = time.monotonic() + WORKER_TIMEOUT
    while not finished.wait(.1):
        try:
            transport.verify_peer(transport.inspect_peer(parent.pid), parent)
            if time.monotonic() >= deadline or pipe.read() is not None:
                break
        except Exception:
            break
    else:
        return
    os._exit(5)  # Only this one-shot worker; never terminate the HID host.


def main(argv=None):
    if sys.platform != "win32" or getattr(sys, "frozen", False):
        return 2
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("parent_pid", type=int)
    parser.add_argument("parent_born", type=int)
    parser.add_argument("generation")
    parser.add_argument("token")
    parser.add_argument("pid", type=int)
    args = parser.parse_args(argv)
    pipe = None
    finished = threading.Event()
    watcher = None
    try:
        if args.parent_born <= 0 or not 0 < args.pid <= 0xFFFFFFFF:
            return 2
        identity = Identity(args.generation, args.token)
        parent = transport.inspect_peer(args.parent_pid)
        if parent.born != args.parent_born:
            raise SourceInjectionError("source_injector_parent_changed")
        transport.verify_same_user_image(parent, transport.inspect_peer(os.getpid()))
        pipe = ResultPipe(identity, server=False)
        deadline = time.monotonic() + 10.0
        pipe.connect(deadline, finished)
        transport.verify_peer(transport.inspect_peer(pipe.peer_pid()), parent)
        _verify_frame(_read(pipe, deadline, finished), identity, "start")
        transport.verify_peer(transport.inspect_peer(parent.pid), parent)
        watcher = threading.Thread(target=_watch_parent, args=(pipe, parent, finished), daemon=True)
        watcher.start()
        from .frida_hid_tap_injector import injection_result
        code, record = injection_result(args.pid)
        finished.set()
        watcher.join()
        pipe.write(diagnostics.encode(record))
        _verify_frame(_read(pipe, time.monotonic() + 2.0, threading.Event()), identity, "received")
        return code
    except Exception:
        return 5
    finally:
        finished.set()
        if watcher is not None:
            watcher.join()
        if pipe is not None:
            pipe.close()
