"""Explicit physical-remote selection shared by BLE, Raw Input and the UI.

Windows ContainerId joins the interfaces belonging to one physical device.
Only its SHA-256 digest is saved in config.json; raw IDs/paths stay in memory.
The paired-device scan runs in a disposable process using the existing bounded
diagnostics runner, including in windowed builds. It never pairs or opens GATT.
"""

from __future__ import annotations

import asyncio
import ctypes
from ctypes import wintypes
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import remote_scan_bootstrap as scan_bootstrap

KEY = "remote_selection"
SCAN_FLAG = "--list-remote-devices"
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_MAX_DEVICES = 128
RC003_PROFILE = "xiaomi-rc003"
CHROMECAST_PROFILE = "chromecast-remote"
KNOWN_PROFILES = frozenset({RC003_PROFILE, CHROMECAST_PROFILE})
_SCAN_STAGES = scan_bootstrap.STAGES
_SCAN_COUNTS = scan_bootstrap.COUNTS
_ScanDiagnostic = scan_bootstrap.ScanDiagnostic
_SCAN_SPAWN_LOCK = threading.Lock()


def _read_scan_diagnostic(path):
    """Only allow fixed markers and numbers across the child/log boundary."""
    def unavailable(reason, error=None):
        result = {"evidence_unavailable": True, "evidence_reason": reason}
        if error is not None:
            result["evidence_errors"] = scan_bootstrap.error_fields(error)["errors"]
        return result
    try:
        path = Path(path)
        # Read with a real bound even if the child file changed after stat().
        with path.open("rb") as stream:
            data = stream.read(65537)
        if len(data) > 65536:
            return unavailable("too_large")
        if not data:
            return unavailable("empty")
        values, invalid = [], 0
        for line in data.splitlines():
            try:
                row = json.loads(line)
                if (not isinstance(row, dict) or type(row.get("schema")) is not int
                        or row.get("schema") not in (1, 2)
                        or not isinstance(row.get("stage"), str) or row.get("stage") not in _SCAN_STAGES
                        or not isinstance(row.get("state"), str)
                        or row.get("state") not in {"running", "success", "failed"}):
                    raise ValueError("invalid diagnostic schema")
                values.append(row)
            except (ValueError, UnicodeError):
                invalid += 1
        if not values:
            return unavailable("invalid_record")
        value = values[-1]
        result = {"child_stage": value["stage"], "child_state": value["state"]}
        if invalid:
            result["evidence_partial"] = True
        result["child_steps"] = list(dict.fromkeys(row["stage"] for row in values))
        if value.get("failure_stage") in _SCAN_STAGES:
            result["failure_stage"] = value["failure_stage"]
        for key in (*_SCAN_COUNTS, "write_failures"):
            count = value.get(key)
            if type(count) is int and 0 <= count <= 0xffffffff:
                result[key] = count
        for key in ("errors", "identity_errors", "write_errors", "channel_errors"):
            errors = value.get(key)
            if not isinstance(errors, list) or len(errors) > 2:
                continue
            safe = []
            for error in errors:
                if not isinstance(error, dict):
                    continue
                name = error.get("error_type")
                row = {"error_type": name if isinstance(name, str) and
                       re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,79}", name) else "Exception"}
                for code in ("winerror", "hresult", "errno"):
                    number = error.get(code)
                    if type(number) is int and -(2**31) <= number <= 0xffffffff:
                        row[code] = number
                frames = error.get("frames", [])
                if isinstance(frames, list) and len(frames) <= 6:
                    safe_frames = []
                    for frame in frames:
                        if not isinstance(frame, dict):
                            continue
                        module, line = frame.get("module"), frame.get("line")
                        if (isinstance(module, str) and len(module) <= 120
                                and re.fullmatch(r"(?:ovb_rc003\.[a-z_0-9.]+|remote_scan_bootstrap|pyi_rth_[a-z_0-9]+)", module)
                                and type(line) is int and 0 < line <= 1000000):
                            safe_frames.append({"module": module, "line": line})
                    if safe_frames:
                        row["frames"] = safe_frames
                safe.append(row)
            result["child_errors" if key == "errors" else key] = safe
        return result
    except FileNotFoundError as exc:
        return unavailable("missing", exc)
    except OSError as exc:
        return unavailable("read_failed", exc)
    except Exception as exc:
        return unavailable("collector_failed", exc)


class SelectionError(ValueError):
    """An absent/invalid selection must never fall back to another remote."""


class _RawIdentityError(SelectionError):
    """Fixed metadata only; Configuration Manager returns CONFIGRET, not WinError."""

    def __init__(self, message, stage, code, *, kind=None, size=None):
        super().__init__(message)
        self.diagnostic = {"stage": stage, "configret": int(code)}
        if kind is not None:
            self.diagnostic.update(property_type=int(kind), property_size=int(size))


def container_key(value: object) -> str:
    try:
        container = uuid.UUID(str(value).strip("{}"))
    except (ValueError, TypeError, AttributeError) as exc:
        raise SelectionError("无法确认设备身份，请重新检查设备。") from exc
    if container.int in (0, 1):
        raise SelectionError("无法确认独立设备身份。")
    return hashlib.sha256(b"RemoteMic physical remote v1\0" + container.bytes).hexdigest()


def valid_key(value: object) -> bool:
    return isinstance(value, str) and _DIGEST.fullmatch(value) is not None


def empty_selection() -> dict:
    return {"schema": 1, "devices": [], "active": ""}


def normalize(value: object) -> dict:
    if value is None:
        return empty_selection()
    if not isinstance(value, dict) or set(value) != {"schema", "devices", "active"}:
        raise SelectionError("已保存的设备列表格式不正确。")
    if type(value["schema"]) is not int or value["schema"] != 1:
        raise SelectionError("设备列表版本不受支持。")
    devices, active = value["devices"], value["active"]
    if not isinstance(devices, list) or len(devices) > _MAX_DEVICES:
        raise SelectionError("设备列表格式不正确。")
    keys = set()
    records = []
    for row in devices:
        if (not isinstance(row, dict) or set(row) != {"key", "profile"}
                or not valid_key(row.get("key"))
                or row.get("profile") not in KNOWN_PROFILES or row["key"] in keys):
            raise SelectionError("设备记录不受支持或重复。")
        keys.add(row["key"])
        records.append(dict(row))
    if not isinstance(active, str) or (active and active not in keys):
        raise SelectionError("当前设备不在已添加列表中。")
    return {"schema": 1, "devices": records, "active": active}


def active_key(settings: dict) -> str:
    return normalize(settings.get(KEY))["active"]


def require_active(settings: dict) -> str:
    key = active_key(settings)
    if not key:
        raise SelectionError("请先在设备页点击“选择设备”，确认要使用的遥控器。")
    return key


def saved_active_key() -> str:
    from . import config
    path = config.config_path(config.config_root())
    if not path.is_file():
        return ""
    if path.stat().st_size > 1024 * 1024:
        raise SelectionError("设备设置文件过大。")
    stored = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(stored, dict):
        raise SelectionError("设备设置格式不正确。")
    return active_key(stored)


def profile_for_key(selection: dict, key: str) -> str:
    return next((row["profile"] for row in normalize(selection)["devices"] if row["key"] == key), "")


def active_profile(settings: dict) -> str:
    selection = normalize(settings.get(KEY))
    return profile_for_key(selection, selection["active"])


def runtime_ready(profile: str) -> bool:
    # Capability only, not a claim that live source/permission checks passed.
    return profile in (RC003_PROFILE, CHROMECAST_PROFILE)


def label(key: str, profile: str = RC003_PROFILE, peers=()) -> str:
    from . import device_catalog
    if not key:
        return "未选择设备"
    size = 6
    while size < 64 and any(other != key and other[:size] == key[:size] for other in peers):
        size += 2
    name = device_catalog.profile_for(profile).display_name
    return f"{name} · {key[:size].upper()}"


def _unsupported_label(name: object, key: str) -> str:
    value = name if isinstance(name, str) else ""
    value = " ".join("".join(c for c in value if c.isprintable() or c.isspace()).split())[:64]
    return f"{value or ('未命名蓝牙设备 · ' + key[:6].upper())}（暂不支持）"


def add_device(selection: dict, row: dict) -> dict:
    result = normalize(selection)
    if row.get("profile") not in KNOWN_PROFILES or not valid_key(row.get("key")):
        raise SelectionError("这台设备暂未适配，不能添加。")
    if any(record["key"] == row["key"] for record in result["devices"]):
        return result
    if len(result["devices"]) >= _MAX_DEVICES:
        raise SelectionError("已添加设备过多，请先移除不再使用的设备。")
    result["devices"].append({"key": row["key"], "profile": row["profile"]})
    return result


def remove_device(selection: dict, key: str) -> dict:
    result = normalize(selection)
    result["devices"] = [row for row in result["devices"] if row["key"] != key]
    if result["active"] == key:
        result["active"] = ""
    return result


def select_device(selection: dict, key: str) -> dict:
    result = normalize(selection)
    if not any(row["key"] == key for row in result["devices"]):
        raise SelectionError("请先添加这台设备。")
    result["active"] = key
    return result


def select_paired_device(selection: dict, key: str, paired: list[dict]) -> tuple[dict, bool]:
    """Prepare registration + selection without writing or activating anything.

    The legacy-owner decision uses all known Xiaomi identities, not just the
    single row being registered by this confirmation. Never infer ownership
    from an incomplete/failed scan (the caller must require a successful scan).
    """
    result = normalize(selection)
    row = next((item for item in paired if item["key"] == key), None)
    if row is None or row.get("profile") not in KNOWN_PROFILES:
        raise SelectionError("未找到这台已配对且支持的设备，请刷新后重新选择。")
    saved_profile = profile_for_key(result, key)
    if saved_profile and saved_profile != row["profile"]:
        raise SelectionError("设备型号信息与已保存记录不一致，请重新检查。")
    known_xiaomi = {item["key"] for item in result["devices"] + paired
                    if item.get("profile") == RC003_PROFILE}
    result = select_device(add_device(result, row), key)
    return result, row["profile"] == RC003_PROFILE and known_xiaomi == {key}


def candidate_key(info: object) -> str:
    from winrt.system import unbox_guid
    properties = getattr(info, "properties", {})
    value = properties.get("System.Devices.Aep.ContainerId")
    if value is None:
        raise SelectionError("无法读取所选蓝牙设备的身份。")
    return container_key(value if isinstance(value, (str, uuid.UUID)) else unbox_guid(value))


class _PropertyKey(ctypes.Structure):
    _fields_ = [("fmtid", ctypes.c_ubyte * 16), ("pid", wintypes.DWORD)]


def _property_key(guid: str, pid: int) -> _PropertyKey:
    return _PropertyKey((ctypes.c_ubyte * 16).from_buffer_copy(uuid.UUID(guid).bytes_le), pid)


def raw_path_key(path: str) -> str:
    """Read interface -> devnode -> ContainerId using synchronous PnP APIs."""
    cfg = ctypes.WinDLL("cfgmgr32", use_last_error=True)
    interface_key = _property_key("78c34fc8-104a-4aca-9ea4-524d52996e57", 256)
    container_property = _property_key("8c7ed206-3f8a-4827-b3ab-ae9e1faefc6c", 2)
    property_args = (ctypes.POINTER(_PropertyKey), ctypes.POINTER(wintypes.ULONG),
                     ctypes.c_void_p, ctypes.POINTER(wintypes.ULONG), wintypes.ULONG)
    cfg.CM_Get_Device_Interface_PropertyW.argtypes = (wintypes.LPCWSTR,) + property_args
    cfg.CM_Get_Device_Interface_PropertyW.restype = wintypes.ULONG
    cfg.CM_Locate_DevNodeW.argtypes = (ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.ULONG)
    cfg.CM_Locate_DevNodeW.restype = wintypes.ULONG
    cfg.CM_Get_DevNode_PropertyW.argtypes = (wintypes.DWORD,) + property_args
    cfg.CM_Get_DevNode_PropertyW.restype = wintypes.ULONG
    name = ctypes.create_unicode_buffer(4096)
    kind, size = wintypes.ULONG(), wintypes.ULONG(ctypes.sizeof(name))
    code = cfg.CM_Get_Device_Interface_PropertyW(
        path, ctypes.byref(interface_key), ctypes.byref(kind), name, ctypes.byref(size), 0)
    if code or kind.value != 0x12 or size.value > ctypes.sizeof(name):
        raise _RawIdentityError("无法核实所选设备的按键接口。", "interface_property", code,
                                kind=kind.value, size=size.value)
    node = wintypes.DWORD()
    code = cfg.CM_Locate_DevNodeW(ctypes.byref(node), name, 0)
    if code:
        raise _RawIdentityError("所选设备的按键接口未连接。", "locate_devnode", code)
    buffer = (ctypes.c_ubyte * 16)()
    size = wintypes.ULONG(16)
    code = cfg.CM_Get_DevNode_PropertyW(
        node, ctypes.byref(container_property), ctypes.byref(kind), buffer, ctypes.byref(size), 0)
    if code or kind.value != 0x0D or size.value != 16:
        raise _RawIdentityError("无法核实所选设备的按键身份。", "container_property", code,
                                kind=kind.value, size=size.value)
    return container_key(uuid.UUID(bytes_le=bytes(buffer)))


def selected_raw_path(paths: list[str], key: str, *, resolve=None, diagnostic: dict | None = None) -> str:
    from . import hid_identity
    details = {"matching_paths": 0, "selected_paths": 0, "identity_mismatches": 0,
               "identity_failures": 0, "error_samples": [], "errors_omitted": 0}
    if diagnostic is not None:
        diagnostic.update(details, reason="no_selected_device")
    if not valid_key(key):
        raise hid_identity.NoDevicePathFoundError("no selected remote")
    resolve = resolve or raw_path_key
    matches = []
    for path in paths:
        if not hid_identity.device_path_matches_rc003(path):
            continue
        details["matching_paths"] += 1
        try:
            if resolve(path) == key:
                matches.append(path)
            else:
                details["identity_mismatches"] += 1
        except (SelectionError, OSError) as exc:
            details["identity_failures"] += 1
            if len(details["error_samples"]) < 3:
                error = {"stage": "container_identity", "error_type": "SelectionError"}
                if isinstance(exc, _RawIdentityError):
                    error.update(exc.diagnostic)
                elif isinstance(exc, OSError):
                    error.update(error_type="OSError", winerror=getattr(exc, "winerror", None),
                                 errno=exc.errno)
                details["error_samples"].append(error)
            else:
                details["errors_omitted"] += 1
            continue
    details["selected_paths"] = len(matches)
    if diagnostic is not None:
        reason = ("ambiguous_selected_interface" if len(matches) > 1 else
                  "selected_interface_found" if matches else
                  "identity_read_failed" if details["identity_failures"] else
                  "selected_identity_mismatch" if details["identity_mismatches"] else
                  "no_matching_interface")
        diagnostic.update(details, reason=reason)
    return hid_identity.select_single_device_path(matches)


async def _scan_paired(*, diagnostic=None) -> list[dict]:
    diagnostic = diagnostic or _ScanDiagnostic()
    diagnostic.stage("winrt_import")
    from . import ble_transport_winrt, identity
    winrt = ble_transport_winrt._import_winrt()
    diagnostic.stage("selector")
    selector = winrt.bluetooth_le_device.get_device_selector_from_pairing_state(True)
    diagnostic.stage("enumerate")
    devices = await winrt.device_information.find_all_async_aqs_filter_and_additional_properties(
        selector, ["System.Devices.Aep.ContainerId"])
    diagnostic.stage("device_rows")
    rows = {}
    for info in devices:
        diagnostic.record["enumerated"] += 1
        try:
            key = candidate_key(info)
        except (SelectionError, OSError, TypeError, ValueError) as exc:
            from .ble_connection_diagnostics import exception_fields
            diagnostic.record["identity_failed"] += 1
            if "identity_errors" not in diagnostic.record:
                diagnostic.record["identity_errors"] = exception_fields(exc)["errors"]
            continue
        name = getattr(info, "name", "") or ""
        profile = (RC003_PROFILE if identity.matches_rc003_name(name) else
                   CHROMECAST_PROFILE if str(name).strip().casefold() == "chromecast remote" else "")
        diagnostic.record["supported" if profile else "unsupported"] += 1
        # Unsupported names are display-only; saved selection still contains only digests/profiles.
        row = {"key": key, "profile": profile,
               "label": label(key, profile) if profile else _unsupported_label(name, key)}
        if key in rows and rows[key]["profile"] != row["profile"]:
            raise SelectionError("同一设备的类型信息存在冲突，请重新检查。")
        rows[key] = row
    if len(rows) > _MAX_DEVICES:
        raise SelectionError("配对设备过多，无法完整列出。")
    return sorted(rows.values(), key=lambda row: (not bool(row["profile"]), row["key"]))


def scan_main(args: list[str]) -> int:
    if len(args) not in (1, 2) or not args[0] or (len(args) == 2 and not args[1]):
        return 2
    diagnostic = scan_bootstrap.diagnostic_for(args[1] if len(args) == 2 else None)
    async def collect():
        try:
            return await _scan_paired(diagnostic=diagnostic)
        except Exception as exc:
            diagnostic.error(exc)
            raise
        finally:
            diagnostic.stage("loop_shutdown")
    try:
        diagnostic.stage("loop_init")
        rows = asyncio.run(collect())
        diagnostic.stage("result_write")
        Path(args[0]).write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:  # No native exception text or identity leaves the scan.
        diagnostic.error(exc)
        return 1
    else:
        diagnostic.record.update(stage="finished", state="success")
        return 0
    finally:
        diagnostic.save()
        diagnostic.close()


def scan_paired(*, cancel_event: threading.Event | None = None, logger=None) -> list[dict]:
    from . import windows_diagnostics
    from . import __version__
    from .ble_connection_diagnostics import exception_fields
    cancel_event = cancel_event or threading.Event()
    started = time.monotonic()
    record = {"state": "failed", "stage": "temporary_directory",
              "app_version": __version__, "diagnostic_schema": 2,
              "runtime": "frozen" if getattr(sys, "frozen", False) else "source"}
    def read_result(path, returncode):
        record.update(stage="result_read", exit_code=returncode)
        return _read_scan_result(path, returncode)
    try:
        with tempfile.TemporaryDirectory(prefix="remote-mic-devices-") as directory:
            result = Path(directory) / "result.json"
            evidence = Path(directory) / "diagnostic.json"
            command = [sys.executable]
            if not getattr(sys, "frozen", False):
                command += ["-m", "remote_scan_bootstrap"]
            command += [SCAN_FLAG, str(result), str(evidence)]
            record.update(stage="subprocess", child_started=False)
            channel = None
            child = None
            try:
                channel = evidence.open("wb", buffering=0)
                record["evidence_channel"] = "ready"
            except OSError as exc:
                record.update(evidence_channel="open_failed",
                              evidence_channel_errors=exception_fields(exc)["errors"])
            def spawn(arguments, **options):
                nonlocal child
                environment = dict(os.environ)
                environment.pop(scan_bootstrap.HANDLE_ENV, None)
                if channel is not None:
                    if sys.platform == "win32":
                        import msvcrt
                        handle = msvcrt.get_osfhandle(channel.fileno())
                        info = subprocess.STARTUPINFO()
                        info.lpAttributeList = {"handle_list": [handle]}
                        options.update(startupinfo=info, close_fds=True)
                    else:
                        handle = channel.fileno()
                        options["pass_fds"] = (handle,)
                    environment[scan_bootstrap.HANDLE_ENV] = str(handle)
                options["env"] = environment
                # No unbounded pipe reader. Only this process inherits the
                # already-open temporary journal; the parent retains ownership.
                with _SCAN_SPAWN_LOCK:
                    if channel is not None and sys.platform == "win32":
                        try:
                            os.set_handle_inheritable(handle, True)
                        except OSError as exc:
                            options.pop("startupinfo", None)
                            environment.pop(scan_bootstrap.HANDLE_ENV, None)
                            record.update(evidence_channel="inherit_failed",
                                          evidence_channel_errors=exception_fields(exc)["errors"])
                    try:
                        child = subprocess.Popen(arguments, **options)
                    finally:
                        if channel is not None and sys.platform == "win32":
                            try:
                                os.set_handle_inheritable(handle, False)
                            except OSError as exc:
                                record["evidence_channel_errors"] = exception_fields(exc)["errors"]
                record.update(child_pid=child.pid, child_started=True)
                return child
            try:
                rows = windows_diagnostics._run_ble_diagnostics_subprocess(
                    command, result_path=str(result), cancel_event=cancel_event,
                    timeout=windows_diagnostics.BLE_DISCOVERY_TIMEOUT_SECONDS,
                    result_reader=read_result, popen=spawn)
            finally:
                if child is not None:
                    try:
                        code = child.poll()
                        record.update(child_exited=code is not None, child_exit_code=code)
                    except OSError as exc:
                        record["child_status_errors"] = exception_fields(exc)["errors"]
                if channel is not None:
                    try:
                        channel.close()
                    except OSError as exc:
                        record["evidence_channel_errors"] = exception_fields(exc)["errors"]
                record.update(_read_scan_diagnostic(evidence))
            record.update(stage="temporary_cleanup", result_count=len(rows))
        record.update(state="success", stage="finished")
        return rows
    except windows_diagnostics.BleDiscoveryCancelledError:
        record["state"] = "cancelled" if cancel_event.is_set() else "timeout"
        raise
    except Exception as exc:
        record.update(state="failed", **exception_fields(exc))
        raise
    finally:
        record["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        try:
            target = logger if logger is not None else logging.getLogger("ovb_rc003")
            target.log(logging.INFO if record["state"] in {"success", "cancelled"}
                       else logging.WARNING, "Remote device scan: %s",
                       json.dumps(record, separators=(",", ":"), sort_keys=True))
        except Exception:
            pass


def _read_scan_result(result_path: str, returncode: int) -> list[dict]:
    result = Path(result_path)
    if returncode != 0 or not result.is_file() or result.stat().st_size > 65536:
        raise SelectionError("设备列表读取失败，请重试。")
    rows = json.loads(result.read_text(encoding="utf-8"))
    if not isinstance(rows, list) or len(rows) > _MAX_DEVICES:
        raise SelectionError("设备列表读取失败，请重试。")
    keys = set()
    for row in rows:
        if (not isinstance(row, dict) or set(row) != {"key", "profile", "label"}
                or not valid_key(row.get("key")) or row["key"] in keys
                or row.get("profile") not in KNOWN_PROFILES | {""}):
            raise SelectionError("设备列表读取失败，请重试。")
        keys.add(row["key"])
    for row in rows:
        if row["profile"]:
            row["label"] = label(row["key"], row["profile"], keys)
        else:
            name = row.get("label")
            if isinstance(name, str):
                name = name.removesuffix("（暂不支持）")
            row["label"] = _unsupported_label(name, row["key"])
    return rows
