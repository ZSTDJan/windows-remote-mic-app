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
from pathlib import Path
import re
import sys
import tempfile
import threading
import uuid

KEY = "remote_selection"
SCAN_FLAG = "--list-remote-devices"
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_MAX_DEVICES = 128


class SelectionError(ValueError):
    """An absent/invalid selection must never fall back to another remote."""


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
                or row.get("profile") != "xiaomi-rc003" or row["key"] in keys):
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
        raise SelectionError("请先在设备页点击“选择设备”，添加并选择要使用的遥控器。")
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


def label(key: str) -> str:
    from . import device_catalog
    return f"{device_catalog.RC003_DISPLAY_NAME} · {key[:6].upper()}" if key else "未选择设备"


def _unsupported_label(name: object, key: str) -> str:
    value = name if isinstance(name, str) else ""
    value = " ".join("".join(c for c in value if c.isprintable() or c.isspace()).split())[:64]
    return f"{value or ('未命名蓝牙设备 · ' + key[:6].upper())}（暂不支持）"


def add_device(selection: dict, row: dict) -> dict:
    result = normalize(selection)
    if row.get("profile") != "xiaomi-rc003" or not valid_key(row.get("key")):
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
        raise SelectionError("无法核实所选设备的按键接口。")
    node = wintypes.DWORD()
    if cfg.CM_Locate_DevNodeW(ctypes.byref(node), name, 0):
        raise SelectionError("所选设备的按键接口未连接。")
    buffer = (ctypes.c_ubyte * 16)()
    size = wintypes.ULONG(16)
    code = cfg.CM_Get_DevNode_PropertyW(
        node, ctypes.byref(container_property), ctypes.byref(kind), buffer, ctypes.byref(size), 0)
    if code or kind.value != 0x0D or size.value != 16:
        raise SelectionError("无法核实所选设备的按键身份。")
    return container_key(uuid.UUID(bytes_le=bytes(buffer)))


def selected_raw_path(paths: list[str], key: str, *, resolve=None) -> str:
    from . import hid_identity
    if not valid_key(key):
        raise hid_identity.NoDevicePathFoundError("no selected remote")
    resolve = resolve or raw_path_key
    matches = []
    for path in paths:
        if not hid_identity.device_path_matches_rc003(path):
            continue
        try:
            if resolve(path) == key:
                matches.append(path)
        except (SelectionError, OSError):
            continue
    return hid_identity.select_single_device_path(matches)


async def _scan_paired() -> list[dict]:
    from . import ble_transport_winrt, identity
    winrt = ble_transport_winrt._import_winrt()
    selector = winrt.bluetooth_le_device.get_device_selector_from_pairing_state(True)
    devices = await winrt.device_information.find_all_async_aqs_filter_and_additional_properties(
        selector, ["System.Devices.Aep.ContainerId"])
    rows = {}
    for info in devices:
        try:
            key = candidate_key(info)
        except (SelectionError, OSError, TypeError, ValueError):
            continue
        name = getattr(info, "name", "") or ""
        supported = identity.matches_rc003_name(name)
        # Unsupported names are display-only; saved selection still contains only digests/profiles.
        row = {"key": key, "profile": "xiaomi-rc003" if supported else "",
               "label": label(key) if supported else _unsupported_label(name, key)}
        if key in rows and rows[key]["profile"] != row["profile"]:
            raise SelectionError("同一设备的类型信息存在冲突，请重新检查。")
        rows[key] = row
    if len(rows) > _MAX_DEVICES:
        raise SelectionError("配对设备过多，无法完整列出。")
    return sorted(rows.values(), key=lambda row: (not bool(row["profile"]), row["key"]))


def scan_main(args: list[str]) -> int:
    if len(args) != 1 or not args[0]:
        return 2
    try:
        rows = asyncio.run(_scan_paired())
        Path(args[0]).write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    except Exception:  # no platform identity/exception escapes the disposable scan
        return 1
    return 0


def scan_paired(*, cancel_event: threading.Event | None = None) -> list[dict]:
    from . import windows_diagnostics
    with tempfile.TemporaryDirectory(prefix="remote-mic-devices-") as directory:
        result = Path(directory) / "result.json"
        command = [sys.executable]
        if not getattr(sys, "frozen", False):
            command += ["-m", "ovb_rc003"]
        command += [SCAN_FLAG, str(result)]
        return windows_diagnostics._run_ble_diagnostics_subprocess(
            command, result_path=str(result), cancel_event=cancel_event or threading.Event(),
            timeout=windows_diagnostics.BLE_DISCOVERY_TIMEOUT_SECONDS,
            result_reader=_read_scan_result)


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
                or row.get("profile") not in ("", "xiaomi-rc003")):
            raise SelectionError("设备列表读取失败，请重试。")
        keys.add(row["key"])
        if row["profile"]:
            row["label"] = label(row["key"])
        else:
            name = row.get("label")
            if isinstance(name, str):
                name = name.removesuffix("（暂不支持）")
            row["label"] = _unsupported_label(name, row["key"])
    return rows
