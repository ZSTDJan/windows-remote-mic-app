"""Migrate the fixed legacy Gadget by restarting only an exclusive RC003 node.

Called solely by the already-elevated injector. No caller-supplied command,
device ID or DLL is accepted. Never unload Gadget in place or terminate a host.
The SID-owned runtime directory identifies the reload-capable lifecycle; old
runtime files remain inert and are not deleted by this migration.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import logging
import os
from pathlib import Path
import subprocess
import sys
import time

from . import frida_hid_tap_runtime as runtime


_LOGGER = logging.getLogger("ovb_rc003.hid_tap")


def loaded_gadget_paths(pid: int) -> list[Path]:
    """Read module identities; fail closed if the snapshot is incomplete."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    psapi.EnumProcessModulesEx.argtypes = (
        wintypes.HANDLE, ctypes.POINTER(wintypes.HMODULE), wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), wintypes.DWORD,
    )
    psapi.EnumProcessModulesEx.restype = wintypes.BOOL
    psapi.GetModuleFileNameExW.argtypes = (
        wintypes.HANDLE, wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD,
    )
    psapi.GetModuleFileNameExW.restype = wintypes.DWORD
    process = kernel.OpenProcess(0x0400 | 0x0010, False, pid)
    if not process:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        modules = (wintypes.HMODULE * 4096)()
        needed = wintypes.DWORD()
        if not psapi.EnumProcessModulesEx(
            process, modules, ctypes.sizeof(modules), ctypes.byref(needed), 3
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if needed.value > ctypes.sizeof(modules):
            raise OSError("module snapshot exceeded capacity")
        paths = []
        for module in modules[:needed.value // ctypes.sizeof(wintypes.HMODULE)]:
            name = ctypes.create_unicode_buffer(32768)
            length = psapi.GetModuleFileNameExW(process, module, name, len(name))
            if not length or length >= len(name):
                raise OSError("module path unavailable")
            path = Path(name.value)
            if path.name.casefold() == runtime.GADGET_DLL_NAME.casefold():
                paths.append(path)
        return paths
    finally:
        kernel.CloseHandle(process)


def _instance_host_pid(instance: str) -> int:
    key = rf"SYSTEM\CurrentControlSet\Enum\{instance}\{runtime.WUDF_DIAGNOSTIC_SUFFIX}"
    with runtime.winreg.OpenKey(runtime.winreg.HKEY_LOCAL_MACHINE, key) as node:
        value, _kind = runtime.winreg.QueryValueEx(node, "HostPid")
    return int(value)


def _exclusive_instance(pid: int, *, selected_key: str | None = None) -> str | None:
    # The existing ownership guard scans every enumerator and rejects unknown
    # or shared hosts. Do not loosen it for the upgrade path.
    if not runtime.rc003_hidogatt_host_is_exclusive(pid, selected_key=selected_key):
        return None
    registry = runtime.winreg
    matches = []
    with registry.OpenKey(registry.HKEY_LOCAL_MACHINE, runtime.BTHLE_ENUM_KEY) as root:
        for i in range(registry.QueryInfoKey(root)[0]):
            service = registry.EnumKey(root, i)
            folded = service.casefold()
            if not folded.startswith(runtime.HID_SERVICE_PREFIX) or runtime.RC003_HARDWARE_TOKEN not in folded:
                continue
            with registry.OpenKey(root, service) as node:
                for j in range(registry.QueryInfoKey(node)[0]):
                    instance = rf"BTHLEDevice\{service}\{registry.EnumKey(node, j)}"
                    try:
                        if _instance_host_pid(instance) == pid:
                            matches.append(instance)
                    except FileNotFoundError:
                        continue
    return matches[0] if len(matches) == 1 else None


def _is_present_and_started(instance: str) -> bool:
    cfg = ctypes.WinDLL("cfgmgr32", use_last_error=True)
    cfg.CM_Locate_DevNodeW.argtypes = (
        ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.ULONG,
    )
    cfg.CM_Locate_DevNodeW.restype = wintypes.ULONG
    cfg.CM_Get_DevNode_Status.argtypes = (
        ctypes.POINTER(wintypes.ULONG), ctypes.POINTER(wintypes.ULONG),
        wintypes.DWORD, wintypes.ULONG,
    )
    cfg.CM_Get_DevNode_Status.restype = wintypes.ULONG
    devnode = wintypes.DWORD()
    name = ctypes.create_unicode_buffer(instance)
    if cfg.CM_Locate_DevNodeW(ctypes.byref(devnode), name, 0) != 0:
        return False
    status, problem = wintypes.ULONG(), wintypes.ULONG()
    return (
        cfg.CM_Get_DevNode_Status(ctypes.byref(status), ctypes.byref(problem), devnode, 0) == 0
        and bool(status.value & 0x00000008) and problem.value == 0
    )


def _restart_device(instance: str) -> int:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetSystemDirectoryW.argtypes = (wintypes.LPWSTR, wintypes.UINT)
    kernel.GetSystemDirectoryW.restype = wintypes.UINT
    buffer = ctypes.create_unicode_buffer(32768)
    length = kernel.GetSystemDirectoryW(buffer, len(buffer))
    if not length or length >= len(buffer):
        raise OSError("system directory unavailable")
    result = subprocess.run(
        [str(Path(buffer.value) / "pnputil.exe"), "/restart-device", instance],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW, timeout=15, check=False,
    )
    return result.returncode


def ensure_reload_capable_host(pid: int, dll_path: Path, *, selected_key: str | None = None) -> str:
    """Return fresh/loaded/restarted/restart_required without injecting twice."""
    paths = loaded_gadget_paths(pid)
    if not paths:
        return "fresh"
    canonical = lambda path: os.path.normcase(os.path.abspath(path))
    if len(paths) == 1 and canonical(paths[0]) == canonical(dll_path):
        return "loaded"
    # Recognize only this SID's exact legacy path; never restart a host on the
    # basis of an unrelated module with the same filename.
    legacy = dll_path.parent.with_name(dll_path.parent.name.removesuffix("-reload")) / dll_path.name
    if len(paths) != 1 or canonical(paths[0]) != canonical(legacy):
        _LOGGER.warning("HID runtime reload blocked: unrecognized runtime")
        return "restart_required"
    if sys.getwindowsversion().build < 19041:
        return "restart_required"
    try:
        instance = _exclusive_instance(pid, selected_key=selected_key)
        if instance is None or not _is_present_and_started(instance):
            _LOGGER.warning("HID runtime reload blocked: exclusive live RC003 node not confirmed")
            return "restart_required"
        # Revalidate the fixed target immediately before the single restart.
        if (runtime.find_rc003_hidogatt_host_pid(selected_key=selected_key) != pid
                or _exclusive_instance(pid, selected_key=selected_key) != instance):
            return "restart_required"
        code = _restart_device(instance)
        if code != 0:  # Includes ERROR_SUCCESS_REBOOT_REQUIRED (3010).
            _LOGGER.warning("HID runtime reload failed: pnputil exit=%s", code)
            return "restart_required"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                replacement = _instance_host_pid(instance)
            except FileNotFoundError:
                replacement = 0
            if replacement > 0 and replacement != pid and _is_present_and_started(instance):
                _LOGGER.info("HID runtime host changed: old_pid=%s new_pid=%s", pid, replacement)
                return "restarted"
            time.sleep(0.1)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        _LOGGER.warning("HID runtime reload failed: device restart or verification unavailable")
    return "restart_required"
