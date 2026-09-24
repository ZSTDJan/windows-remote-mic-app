"""Narrowly-scoped x64 DLL injector for the RC003 WUDF host.

This is adapted from remote-bridge-hub's Xiaomi injector. Injection is only
attempted inside an already-elevated process: either the fixed pre-authorized
HID helper or an explicitly elevated source/debug process. The normal Remote
Mic process never elevates itself.
"""

from __future__ import annotations

import argparse
import ctypes
from contextlib import contextmanager
from ctypes import wintypes
import os
from pathlib import Path

from .frida_hid_tap_runtime import (
    GADGET_DLL_SHA256,
    find_rc003_hidogatt_host_pid,
    prepare_secure_runtime,
    sha256_file,
)
from .hid_host_reload_windows import ensure_reload_capable_host
from . import hid_injection_diagnostics as injection_diagnostics


PROCESS_CREATE_THREAD = 0x0002
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_OPERATION = 0x0008
PROCESS_VM_WRITE = 0x0020
PROCESS_VM_READ = 0x0010
MEM_COMMIT = 0x1000
MEM_RESERVE = 0x2000
MEM_RELEASE = 0x8000
PAGE_READWRITE = 0x04
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
WAIT_FAILED = 0xFFFFFFFF
TOKEN_ADJUST_PRIVILEGES = 0x0020
TOKEN_QUERY = 0x0008
SE_PRIVILEGE_ENABLED = 0x00000002
ERROR_NOT_ALL_ASSIGNED = 1300


class HidInjectionStageError(RuntimeError):
    """Sanitized injection-stage failure for the elevated helper."""

    def __init__(self, detail, **fields):
        super().__init__(detail)
        stage = {
            'hid_helper_host_changed': 'host_recheck',
            'hid_helper_debug_privilege_failed': 'debug_privilege',
            'hid_helper_target_process_open_failed': 'target_open',
            'hid_helper_target_validation_failed': 'target_identity',
            'hid_helper_runtime_preparation_failed': 'runtime_prepare',
            'hid_helper_host_restarted': 'host_reload',
            'hid_helper_legacy_runtime_restart_required': 'host_reload',
            'hid_helper_remote_thread_failed': 'remote_thread',
            'hid_helper_remote_load_timeout': 'remote_wait',
        }.get(detail, 'unknown')
        self.injection_diagnostic = dict(stage=stage, **fields) if 'stage' not in fields else fields


def _win_error(api):
    # Capture before cleanup or any subsequent Windows API can overwrite it.
    error = ctypes.WinError(ctypes.get_last_error())
    error.injection_diagnostic = {'api': api}
    return error


@contextmanager
def _diagnostic_stage(stage):
    try:
        yield
    except Exception as exc:
        if not getattr(exc, 'injection_diagnostic', None):
            exc.injection_diagnostic = {'stage': stage}
        raise


class LUID(ctypes.Structure):
    _fields_ = (("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG))


class LUID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = (("Luid", LUID), ("Attributes", wintypes.DWORD))


class TOKEN_PRIVILEGES(ctypes.Structure):
    _fields_ = (
        ("PrivilegeCount", wintypes.DWORD),
        ("Privileges", LUID_AND_ATTRIBUTES * 1),
    )


def enable_debug_privilege() -> None:
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.argtypes = ()
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    advapi32.OpenProcessToken.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    )
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.LookupPrivilegeValueW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        ctypes.POINTER(LUID),
    )
    advapi32.LookupPrivilegeValueW.restype = wintypes.BOOL
    advapi32.AdjustTokenPrivileges.argtypes = (
        wintypes.HANDLE,
        wintypes.BOOL,
        ctypes.POINTER(TOKEN_PRIVILEGES),
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
    )
    advapi32.AdjustTokenPrivileges.restype = wintypes.BOOL

    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(),
        TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY,
        ctypes.byref(token),
    ):
        raise _win_error('OpenProcessToken')
    try:
        luid = LUID()
        if not advapi32.LookupPrivilegeValueW(
            None, "SeDebugPrivilege", ctypes.byref(luid)
        ):
            raise _win_error('LookupPrivilegeValueW')
        privileges = TOKEN_PRIVILEGES()
        privileges.PrivilegeCount = 1
        privileges.Privileges[0].Luid = luid
        privileges.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED
        ctypes.set_last_error(0)
        if not advapi32.AdjustTokenPrivileges(
            token, False, ctypes.byref(privileges), 0, None, None
        ):
            raise _win_error('AdjustTokenPrivileges')
        error = ctypes.get_last_error()
        if error == ERROR_NOT_ALL_ASSIGNED:
            failure = PermissionError("SeDebugPrivilege is not assigned")
            failure.winerror = error
            failure.injection_diagnostic = {'api': 'AdjustTokenPrivileges'}
            raise failure
        if error:
            failure = ctypes.WinError(error)
            failure.injection_diagnostic = {'api': 'AdjustTokenPrivileges'}
            raise failure
    finally:
        kernel32.CloseHandle(token)


def inject_library(pid: int, dll_path: Path) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.VirtualAllocEx.argtypes = (
        wintypes.HANDLE,
        wintypes.LPVOID,
        ctypes.c_size_t,
        wintypes.DWORD,
        wintypes.DWORD,
    )
    kernel32.VirtualAllocEx.restype = wintypes.LPVOID
    kernel32.WriteProcessMemory.argtypes = (
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.LPCVOID,
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_size_t),
    )
    kernel32.WriteProcessMemory.restype = wintypes.BOOL
    kernel32.VirtualFreeEx.argtypes = (
        wintypes.HANDLE,
        wintypes.LPVOID,
        ctypes.c_size_t,
        wintypes.DWORD,
    )
    kernel32.VirtualFreeEx.restype = wintypes.BOOL
    kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    kernel32.GetProcAddress.argtypes = (wintypes.HMODULE, wintypes.LPCSTR)
    kernel32.GetProcAddress.restype = wintypes.LPVOID
    kernel32.CreateRemoteThread.argtypes = (
        wintypes.HANDLE,
        wintypes.LPVOID,
        ctypes.c_size_t,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    kernel32.CreateRemoteThread.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetExitCodeThread.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    )
    kernel32.GetExitCodeThread.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    rights = (
        PROCESS_CREATE_THREAD
        | PROCESS_QUERY_INFORMATION
        | PROCESS_VM_OPERATION
        | PROCESS_VM_WRITE
        | PROCESS_VM_READ
    )
    process = kernel32.OpenProcess(rights, False, pid)
    if not process:
        raise HidInjectionStageError(
            "hid_helper_target_process_open_failed"
        ) from _win_error('OpenProcess')
    remote_path = None
    thread = None
    remote_thread_completed = False
    try:
        encoded = (str(dll_path.resolve()) + "\0").encode("utf-16-le")
        remote_path = kernel32.VirtualAllocEx(
            process, None, len(encoded), MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE
        )
        if not remote_path:
            raise HidInjectionStageError(
                "hid_helper_remote_memory_failed", stage='remote_allocate'
            ) from _win_error('VirtualAllocEx')
        buffer = ctypes.create_string_buffer(encoded)
        written = ctypes.c_size_t()
        if not kernel32.WriteProcessMemory(
            process, remote_path, buffer, len(encoded), ctypes.byref(written)
        ):
            raise HidInjectionStageError(
                "hid_helper_remote_memory_failed", stage='remote_write'
            ) from _win_error('WriteProcessMemory')
        if written.value != len(encoded):
            raise HidInjectionStageError("hid_helper_remote_memory_failed", stage='remote_write',
                                         api='WriteProcessMemory', expected_bytes=len(encoded),
                                         actual_bytes=written.value)
        kernel = kernel32.GetModuleHandleW("kernel32.dll")
        if not kernel:
            raise HidInjectionStageError("hid_helper_remote_load_failed", stage='load_library_lookup'
                                         ) from _win_error('GetModuleHandleW')
        load_library = kernel32.GetProcAddress(kernel, b"LoadLibraryW")
        if not load_library:
            raise HidInjectionStageError(
                "hid_helper_remote_load_failed", stage='load_library_lookup'
            ) from _win_error('GetProcAddress')
        thread_id = wintypes.DWORD()
        thread = kernel32.CreateRemoteThread(
            process,
            None,
            0,
            load_library,
            remote_path,
            0,
            ctypes.byref(thread_id),
        )
        if not thread:
            raise HidInjectionStageError(
                "hid_helper_remote_thread_failed"
            ) from _win_error('CreateRemoteThread')
        wait_result = int(kernel32.WaitForSingleObject(thread, 20_000))
        if wait_result == WAIT_TIMEOUT:
            raise HidInjectionStageError("hid_helper_remote_load_timeout", api='WaitForSingleObject',
                                         return_value=wait_result)
        if wait_result == WAIT_FAILED:
            raise HidInjectionStageError(
                "hid_helper_remote_load_failed", stage='remote_wait', return_value=wait_result
            ) from _win_error('WaitForSingleObject')
        if wait_result != WAIT_OBJECT_0:
            raise HidInjectionStageError("hid_helper_remote_load_failed", stage='remote_wait',
                                         api='WaitForSingleObject', return_value=wait_result)
        remote_thread_completed = True
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeThread(thread, ctypes.byref(exit_code)):
            raise HidInjectionStageError(
                "hid_helper_remote_load_failed", stage='remote_exit'
            ) from _win_error('GetExitCodeThread')
        if exit_code.value == 0:
            # A remote LoadLibraryW return value is not this process's last error.
            raise HidInjectionStageError("hid_helper_remote_load_failed", stage='remote_exit',
                                         api='GetExitCodeThread', return_value=exit_code.value)
    finally:
        if thread:
            kernel32.CloseHandle(thread)
        # Never free the remote UTF-16 path while LoadLibraryW may still be
        # reading it. A timeout/status failure leaves this small allocation
        # behind in WUDFHost, which is safer than a remote use-after-free.
        if remote_path and (thread is None or remote_thread_completed):
            kernel32.VirtualFreeEx(process, remote_path, 0, MEM_RELEASE)
        kernel32.CloseHandle(process)


def _target_process_name(pid: int) -> str:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    )
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    process = kernel32.OpenProcess(0x1000, False, pid)
    if not process:
        raise _win_error('OpenProcess')
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        length = wintypes.DWORD(len(buffer))
        if not kernel32.QueryFullProcessImageNameW(
            process, 0, buffer, ctypes.byref(length)
        ):
            raise _win_error('QueryFullProcessImageNameW')
        return Path(buffer.value[: length.value]).name.casefold()
    finally:
        kernel32.CloseHandle(process)


def inject_current_process(pid: int, *, selected_key: str | None = None) -> None:
    """Inject only when this process already has the required rights.

    The desktop application deliberately does not request elevation. Installed
    builds call this function inside the fixed scheduled helper; source/debug
    callers may instead start the process explicitly from an elevated terminal.
    """

    if os.name != "nt":
        raise PermissionError("RC003 injector requires Windows administrator elevation")
    with _diagnostic_stage('host_lookup'):
        expected_pid = find_rc003_hidogatt_host_pid(selected_key=selected_key)
    if expected_pid != pid:
        raise HidInjectionStageError("hid_helper_host_changed", stage='host_lookup')
    # WUDFHost denies even limited process queries until the elevated injector
    # enables SeDebugPrivilege.  Validate the target only after that succeeds.
    try:
        enable_debug_privilege()
    except (OSError, PermissionError) as exc:
        raise HidInjectionStageError(
            "hid_helper_debug_privilege_failed"
        ) from exc
    try:
        target_name = _target_process_name(pid)
    except OSError as exc:
        raise HidInjectionStageError(
            "hid_helper_target_process_open_failed"
        ) from exc
    if target_name != "wudfhost.exe":
        raise HidInjectionStageError("hid_helper_target_validation_failed")
    try:
        dll_path = prepare_secure_runtime()
        with _diagnostic_stage('runtime_hash'):
            dll_hash = sha256_file(dll_path)
    except (OSError, RuntimeError, ValueError) as exc:
        raise HidInjectionStageError(
            "hid_helper_runtime_preparation_failed"
        ) from exc
    if dll_hash != GADGET_DLL_SHA256:
        raise HidInjectionStageError("hid_helper_runtime_preparation_failed", stage='runtime_hash',
                                     reason='runtime_dll_hash_mismatch')
    try:
        with _diagnostic_stage('host_reload'):
            lifecycle = ensure_reload_capable_host(pid, dll_path, selected_key=selected_key)
        if lifecycle == "restarted":
            # The parent must discover and authenticate the new host itself.
            raise HidInjectionStageError("hid_helper_host_restarted")
        if lifecycle == "restart_required":
            raise HidInjectionStageError("hid_helper_legacy_runtime_restart_required")
        if lifecycle == "loaded":
            return  # Updating the protected script triggers Gadget's reloader.
        if lifecycle != "fresh":
            raise HidInjectionStageError("hid_helper_runtime_preparation_failed")
        with _diagnostic_stage('host_recheck'):
            current_pid = find_rc003_hidogatt_host_pid(selected_key=selected_key)
        if current_pid != pid:
            raise HidInjectionStageError("hid_helper_host_changed")
        inject_library(pid, dll_path)
    except HidInjectionStageError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise HidInjectionStageError("hid_helper_injection_failed") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--pid", type=int, required=True)
    args = parser.parse_args(argv)
    failure = None
    exit_code = 0
    try:
        from . import remote_selection
        inject_current_process(args.pid, selected_key=remote_selection.saved_active_key())
    except PermissionError as exc:
        failure, exit_code = exc, 3
    except HidInjectionStageError as exc:
        failure = exc
        exit_code = {
            "hid_helper_host_restarted": 6,
            "hid_helper_legacy_runtime_restart_required": 7,
        }.get(str(exc), 4)
    except (OSError, RuntimeError, ValueError) as exc:
        failure, exit_code = exc, 4
    except Exception as exc:  # noqa: BLE001 - preserve the stable exit code
        failure, exit_code = exc, 5
    try:
        injection_diagnostics.write_stdout(injection_diagnostics.result_record(
            success=exit_code == 0, target_pid=args.pid, exit_code=exit_code, exc=failure))
    except Exception:
        pass
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
