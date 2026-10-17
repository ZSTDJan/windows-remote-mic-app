"""Bounded RC003 connection evidence, through the existing async app logger.

No device operations, timers, background owners or exception text. A failed
observer must never change the result, cancellation or cleanup it observes.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import json
import logging
import os
import re
import time
import uuid

LOGGER = logging.getLogger("ovb_rc003.ble_transport_winrt")
MESSAGE = "BLE diagnostic: %s"
_STAGES = frozenset({"connect", "open_device", "connection_handler", "services",
    "discover_candidates", "select_candidate",
    "characteristics_tx", "characteristics_audio", "characteristics_control",
    "notification_handlers", "subscribe_audio", "subscribe_control", "start_worker",
    "get_capabilities", "tx_write", "disconnect_status", "close", "unsubscribe_audio",
    "unsubscribe_control", "remove_audio_handler", "remove_control_handler",
    "remove_connection_handler", "close_service", "close_device", "candidate_probe"})
_STATES = frozenset({"begin", "success", "failed", "exception", "cancelled", "timeout"})
_REASONS = frozenset({"device_unavailable", "service_query_failed", "service_not_found",
    "characteristic_query_failed", "characteristic_not_found", "subscription_failed",
    "tx_write_failed", "native_status_read_failed", "reported_disconnected",
    "reported_connected", "other_status", "probe_timeout_or_busy", "probe_unreachable",
    "cleanup_incomplete", "diagnostic_unavailable", "probe_request_invalid", "probe_busy", "probe_exit_failed"})
_NUMBERS = frozenset({"status", "protocol_error", "item_count", "properties", "handle",
    "connection_status", "generation", "pid", "elapsed_ms", "winerror", "hresult", "errno",
    "probe_pid", "exit_code", "process_exited"})
_MARKER = re.compile(r"[A-Za-z_][A-Za-z_0-9]{0,79}\Z")
_HEX = re.compile(r"[a-f0-9]{32}\Z")
_STATUS = {0: "success", 1: "unreachable", 2: "protocol_error", 3: "access_denied"}


def exception_fields(error):
    """Only real numeric codes and type names; never format native exceptions."""
    rows, seen = [], set()
    for _ in range(2):
        if error is None or id(error) in seen:
            break
        seen.add(id(error))
        name = type(error).__name__
        row = {"error_type": name if _MARKER.fullmatch(name) else "Exception"}
        for key in ("winerror", "hresult", "errno"):
            try:
                value = getattr(error, key, None)
                if type(value) is int and -(2 ** 31) <= value <= 0xffffffff:
                    row[key] = value
            except Exception:
                pass
        rows.append(row)
        try:
            error = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
        except Exception:
            break
    return {"errors": rows, "native_text": "not_collected", "error_chain_limited": error is not None}


def read_number(fields, key, source, attribute=None):
    try:
        value = source if attribute is None else getattr(source, attribute)
        if value is None:
            fields[key] = None
        else:
            value = int(value)
            if not -(2 ** 31) <= value <= 0xffffffff:
                raise ValueError("diagnostic number outside bounds")
            fields[key] = value
    except Exception:
        fields[key] = None
        fields["evidence_partial"] = True
    if key == "status":
        fields["status_name"] = _STATUS.get(fields.get(key), "unavailable" if fields.get(key) is None else "unknown")


def result_fields(fields, result, collection=None):
    read_number(fields, "status", result, "status")
    read_number(fields, "protocol_error", result, "protocol_error")
    if collection:
        try:
            fields["item_count"] = len(getattr(result, collection))
        except Exception:
            fields.update(item_count=None, evidence_partial=True)


def valid_record(row):
    """The disposable probe may only return this fixed, bounded evidence."""
    if not isinstance(row, dict) or len(row) > 24:
        return False
    required = {"event", "attempt", "scope", "stage", "state", "pid", "elapsed_ms"}
    allowed = required | _NUMBERS | {"selected_ref", "reason", "errors", "native_text", "evidence_partial", "status_name", "error_chain_limited"}
    if not required <= row.keys() or not row.keys() <= allowed:
        return False
    if (row["event"] != "ble_connection_step" or row["scope"] not in {"connect", "probe"}
            or row["stage"] not in _STAGES or row["state"] not in _STATES
            or not isinstance(row["attempt"], str) or not _HEX.fullmatch(row["attempt"])):
        return False
    for key, value in row.items():
        if key in _NUMBERS and value is not None:
            if type(value) is not int or not -(2 ** 31) <= value <= 0xffffffff:
                return False
    if "reason" in row and row["reason"] not in _REASONS:
        return False
    if "status_name" in row and row["status_name"] not in {*_STATUS.values(), "unavailable", "unknown"}:
        return False
    if "selected_ref" in row and (not isinstance(row["selected_ref"], str)
            or not re.fullmatch(r"[a-f0-9]{12}", row["selected_ref"])):
        return False
    if "evidence_partial" in row and type(row["evidence_partial"]) is not bool:
        return False
    if "error_chain_limited" in row and type(row["error_chain_limited"]) is not bool:
        return False
    if "native_text" in row and row["native_text"] != "not_collected":
        return False
    errors = row.get("errors", [])
    if not isinstance(errors, list) or len(errors) > 2:
        return False
    for error in errors:
        if (not isinstance(error, dict) or not {"error_type"} <= error.keys()
                or not error.keys() <= {"error_type", "winerror", "errno", "hresult"}
                or not isinstance(error["error_type"], str) or not _MARKER.fullmatch(error["error_type"])):
            return False
        if any(type(v) is not int or not -(2 ** 31) <= v <= 0xffffffff
               for k, v in error.items() if k != "error_type"):
            return False
    return True


def replay(row):
    """Also used by the probe's parent; the existing writer owns disk I/O."""
    try:
        if not valid_record(row):
            return
        extra = {}
        if row["state"] in {"failed", "exception", "timeout"}:
            extra = dict(failure_key="ble_connection:" + row["stage"],
                         failure_id=row["attempt"] + ":" + row["stage"])
        LOGGER.info(MESSAGE, json.dumps(row, ensure_ascii=True, separators=(",", ":")), extra=extra)
    except Exception:
        pass


class ConnectionDiagnostics:
    def __init__(self, scope="connect", selected_key=None):
        self.context = dict(event="ble_connection_step", attempt=uuid.uuid4().hex,
                            scope=scope, pid=os.getpid())
        self.select_key(selected_key)

    def select_key(self, selected_key):
        if isinstance(selected_key, str) and re.fullmatch(r"[a-f0-9]{64}", selected_key):
            self.context["selected_ref"] = selected_key[:12]

    def emit(self, stage, state, *, started=None, error=None, **fields):
        try:
            row = dict(self.context, stage=stage, state=state,
                       elapsed_ms=0 if started is None else min(0xffffffff, max(0, round((time.monotonic()-started)*1000))),
                       **fields)
            if error is not None:
                row.update(exception_fields(error))
            replay(row)
        except Exception:
            pass

    @contextmanager
    def step(self, stage, **fields):
        started = time.monotonic()
        self.emit(stage, "begin", **fields)
        try:
            yield fields
        except asyncio.CancelledError as error:
            fields.pop("_state", None)
            self.emit(stage, "cancelled", started=started, error=error, **fields)
            raise
        except Exception as error:
            fields.pop("_state", None)
            state = "timeout" if isinstance(error, TimeoutError) else "failed" if "reason" in fields else "exception"
            self.emit(stage, state, started=started, error=error, **fields)
            raise
        else:
            self.emit(stage, fields.pop("_state", "success"), started=started, **fields)
