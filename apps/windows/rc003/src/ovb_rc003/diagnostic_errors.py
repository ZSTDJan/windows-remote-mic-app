"""Bounded error meaning shared by persistent diagnostics, never raw messages.

This module only interprets an existing exception. It performs no I/O, process
inspection or input operations. Native text, paths, arguments and frame locals
never leave it; upstream error forms become fixed reasons and numeric codes.
"""
from __future__ import annotations

import hashlib
import re

_MARKER = re.compile(r"[A-Za-z_][A-Za-z_0-9.<>]{0,95}\Z")
_APIS = ("OpenProcess", "CreateRemoteThread", "VirtualAllocEx", "WriteProcessMemory",
         "LoadLibraryW", "LoadLibraryExW", "GetProcAddress", "SetNamedSecurityInfoW")
_API = re.compile(r"\b(" + "|".join(_APIS) + r")\b")
_CODE = re.compile(r"(?:returned|error|code|HRESULT)\s*(?:[=:]\s*)?(0x[0-9a-fA-F]{1,8}|[0-9]{1,10})\b", re.I)
_WINERROR = re.compile(r"\[(?:WinError|Errno)\s+([0-9]{1,10})\]", re.I)


def marker(value, fallback="unknown"):
    if value in ('<lambda>', '<module>'):
        return value
    return value if isinstance(value, str) and _MARKER.fullmatch(value) else fallback


def number(value):
    return value if type(value) is int and -(2**31) <= value <= 0xffffffff else None


def error_detail(error):
    """One exception's safe meaning; unrecognized text remains undisclosed."""
    result = {"error_type": marker(type(error).__name__, "Exception")}
    for key in ("winerror", "errno", "hresult", "gatt_status", "protocol_error"):
        try:
            value = number(getattr(error, key, None))
            if value is not None:
                result[key] = value
        except Exception:
            pass
    try:
        text = str(error)[:4096]
    except Exception:
        text = ""
    lowered = text.lower()
    family = "unclassified"
    if "unable to spawn helper executable" in lowered:
        family = "helper_start"
        # Frida's helper-launch form ends in a Windows code after a private
        # filename. Retain only that terminal code, never the filename.
        match = re.search(r":\s*(0x[0-9a-fA-F]{1,8})\s*$", text)
        if match:
            result["reported_code"] = int(match[1], 16)
    elif "refused to load frida-agent, or terminated during injection" in lowered:
        family = "agent_load_or_process_exit"
    elif "unable to access process" in lowered or "access is denied" in lowered or "access denied" in lowered:
        family = "access_denied"
    elif lowered.strip() == "the connection is closed":
        family = "connection_closed"
    elif "unable to communicate with remote frida-server" in lowered:
        family = "agent_transport"
    elif "timed out" in lowered or "timeout" in lowered or isinstance(error, TimeoutError):
        family = "timeout"
    elif "process not found" in lowered or "unable to find process with pid" in lowered:
        family = "process_missing"
    elif isinstance(error, PermissionError):
        family = "access_denied"
    elif isinstance(error, FileNotFoundError):
        family = "file_missing"
    elif isinstance(error, ImportError):
        family = "module_load"
    elif type(error).__name__ in ("CancelledError", "OperationCancelledError"):
        family = "cancelled"
    api = _API.search(text)
    if api:
        result["native_api"] = api[1]
        match = _CODE.search(text[api.end():])
        if match:
            value = int(match[1], 16 if match[1].lower().startswith("0x") else 10)
            if number(value) is not None:
                result["reported_code"] = value
    match = _WINERROR.search(text)
    if match and number(int(match[1])) is not None:
        result.setdefault("reported_code", int(match[1]))
    if family == "unclassified" and result.get("reported_code") in (5, 0xc0000022):
        family = "access_denied"
    result["error_family"] = family
    result["error_sha256"] = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
    return result


def exception_snapshot(error, *, traceback=None):
    """A bounded cause chain and first-party locations, without traceback text."""
    errors, seen, locations = [], set(), []
    current = error
    for _ in range(3):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        errors.append(error_detail(current))
        current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
    tb = traceback if traceback is not None else getattr(error, "__traceback__", None)
    scanned = 0
    while tb is not None and scanned < 96:
        scanned += 1
        module = tb.tb_frame.f_globals.get("__name__", "")
        if module.startswith("ovb_rc003.") or module == "remote_scan_bootstrap":
            locations.append({"module": marker(module), "function": marker(tb.tb_frame.f_code.co_name),
                              "line": max(0, min(65535, tb.tb_lineno))})
        tb = tb.tb_next
    return {"errors": errors, "locations": locations[-4:], "chain_limited": current is not None,
            "location_scan_limited": tb is not None,
            "native_text": "not_collected"}
