"""Attempt-local Doubao hands-free finish control.

Doubao's hands-free mode starts from its configured ``voiceShortcut``.  A
bound active capture is finished with Doubao's verified ``VOICE_PRESS_STOP``
RPC message.  Its normal TSF notification path then finalizes and commits the
recognized text into the focused editor without synthesizing another key.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile

from . import doubao_rpc


FLAG = "--doubao-handsfree-finish"
TIMEOUT_SECONDS = 1.5


class DoubaoHandsFreeFinish:
    def __init__(
        self,
        capture_identity,
        *,
        reader,
        send_stop=doubao_rpc.send_voice_press_stop,
    ):
        self.capture_identity = capture_identity
        self.reader = reader
        self.send_stop = send_stop
        self.requested = False
        self.result = "not_requested"

    def _capture_active(self):
        if self.capture_identity is None:
            return None
        try:
            active = {
                item.identity
                for item in self.reader()
                if item.state == 1
            }
        except Exception:
            return None
        return active == {self.capture_identity}

    def request_finish(self):
        """Ask the bound Doubao capture to finish exactly once."""

        if self.requested:
            return self.result
        self.requested = True
        active = self._capture_active()
        if active is False:
            self.result = "capture_ended"
            return self.result
        if active is None:
            self.result = "capture_unconfirmed"
            return self.result
        try:
            self.send_stop()
        except Exception:
            self.result = "send_failed"
            return self.result
        self.result = "sent_once"
        return self.result


def _valid_identity(value):
    return (
        isinstance(value, (tuple, list))
        and len(value) == 3
        and all(isinstance(part, str) and 0 < len(part) <= 4096 for part in value[:2])
        and type(value[2]) is int
        and 0 < value[2] <= 0xFFFFFFFF
    )


def finish_bound_capture(
    identity,
    *,
    reader,
    send_stop=doubao_rpc.send_voice_press_stop,
):
    """Revalidate one exact capture and send one private stop request."""

    if not _valid_identity(identity):
        return "capture_identity_invalid"
    control = DoubaoHandsFreeFinish(
        identity,
        reader=reader,
        send_stop=send_stop,
    )
    return control.request_finish()


def child_main(args):
    if len(args) != 2:
        return 2
    try:
        request, result = map(Path, args)
        if (
            not request.is_absolute()
            or not result.is_absolute()
            or request == result
            or result.exists()
            or request.stat().st_size > 16384
        ):
            return 2
        identity = json.loads(request.read_text(encoding="utf-8"))
        if not _valid_identity(identity):
            return 2
        from .chromecast_host_activity import read_doubao_capture

        outcome = finish_bound_capture(tuple(identity), reader=read_doubao_capture)
        with result.open("x", encoding="utf-8") as stream:
            json.dump({"state": outcome}, stream)
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
    state = value.get("state") if isinstance(value, dict) else None
    if (
        not isinstance(value, dict)
        or set(value) != {"state"}
        or not isinstance(state, str)
        or not state
        or len(state) > 80
        or any(c not in "abcdefghijklmnopqrstuvwxyz_" for c in state)
    ):
        return "helper_result_invalid"
    return state


def finish_with_timeout(identity, *, cancel_event):
    """Run the private RPC request in a disposable child; never retry it."""

    from . import windows_diagnostics

    if not _valid_identity(identity):
        return "capture_identity_unavailable"
    try:
        with tempfile.TemporaryDirectory(prefix="remote-mic-doubao-finish-") as directory:
            request = Path(directory) / "request.json"
            result = Path(directory) / "result.json"
            request.write_text(json.dumps(identity), encoding="utf-8")
            command = [sys.executable]
            if not getattr(sys, "frozen", False):
                command += ["-m", "ovb_rc003"]
            command += [FLAG, str(request), str(result)]
            return windows_diagnostics._run_ble_diagnostics_subprocess(
                command,
                result_path=str(result),
                cancel_event=cancel_event,
                timeout=TIMEOUT_SECONDS,
                terminate_wait=0.25,
                kill_wait=0.25,
                result_reader=_read_result,
            )
    except windows_diagnostics.BleDiscoveryCancelledError:
        return "helper_cancelled_or_timed_out"
    except Exception:
        return "helper_result_unknown"
