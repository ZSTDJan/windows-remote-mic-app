"""Bounded fallback for a Google HID host that rejects Frida's temporary agent.

Only the already elevated Google worker may install this fixed Gadget.  A
persisted authenticated endpoint lets a later worker reconnect without loading
a second runtime into the same WUDF host.
"""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
from dataclasses import dataclass
import json
import lzma
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import struct
import threading
import time
from typing import Callable

from . import frida_hid_tap_runtime as assets
from . import hid_elevation_windows as security
from .frida_hid_tap_injector import enable_debug_privilege, inject_library


DLL_NAME = assets.GOOGLE_GADGET_DLL_NAME
CONFIG_NAME = "RemoteMicGoogleHidTap.config"
ATTEMPT_NAME = "RemoteMicGoogleHidTap.attempt.json"
_TOKEN = re.compile(r"[0-9a-f]{64}\Z")
_IDENTITY_SCRIPT = "rpc.exports={identity(){return Process.id}}"


class GadgetAttachError(RuntimeError):
    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


@dataclass(frozen=True)
class Endpoint:
    dll: Path
    port: int
    token: str


@dataclass(frozen=True)
class HostSnapshot:
    born: int
    modules: tuple[Path, ...]


def _runtime_paths() -> tuple[Path, Path, Path]:
    sid = security.current_user_sid()
    program_files = security._program_files_root()
    owner = security.protected_runtime_owner_root(sid, program_files_root=program_files)
    root = owner / f"google-listen1-{assets.GADGET_VERSION}-{assets.GADGET_DLL_SHA256[:12]}"
    return program_files, owner, root


def _config(port: int, token: str) -> dict:
    return {
        "interaction": {
            "type": "listen", "address": "127.0.0.1", "port": port,
            "token": token, "on_load": "resume", "on_port_conflict": "fail",
        },
        "runtime": "qjs", "teardown": "minimal",
    }


def _read_config(path: Path) -> tuple[int, str]:
    try:
        raw = path.read_bytes()
        if len(raw) > 1024:
            raise ValueError("oversized")
        config = json.loads(raw.decode("utf-8"))
        interaction = config["interaction"]
        port, token = interaction["port"], interaction["token"]
        if (type(port) is not int or not 39000 <= port <= 58000
                or type(token) is not str or not _TOKEN.fullmatch(token)
                or config != _config(port, token)):
            raise ValueError("invalid")
        return port, token
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
        raise GadgetAttachError("gadget_config_invalid") from exc


def _assert_runtime_security(program_files: Path, owner: Path, root: Path) -> None:
    sid = security.current_user_sid()
    for path in (owner, root, root / DLL_NAME, root / CONFIG_NAME):
        security.assert_no_reparse_points(path, trusted_root=program_files)
        directory = path in (owner, root)
        if not (path.is_dir() if directory else path.is_file()):
            raise GadgetAttachError("gadget_runtime_missing")
        if not security.validate_path_security_sddl(
            security._read_path_security_sddl(path), user_sid=sid,
            directory=directory, read_execute_sids=(security.LOCAL_SERVICE_SID,),
            include_user=path != root and path.name != CONFIG_NAME,
        ):
            raise GadgetAttachError("gadget_runtime_acl_invalid")
    if assets.sha256_file(root / DLL_NAME) != assets.GADGET_DLL_SHA256:
        raise GadgetAttachError("gadget_runtime_hash_invalid")


def existing_endpoint() -> Endpoint | None:
    """A prior fallback owns this path; never turn an invalid one into direct attach."""
    program_files, owner, root = _runtime_paths()
    config = root / CONFIG_NAME
    security.assert_no_reparse_points(config, trusted_root=program_files)
    if not config.exists():
        return None
    _assert_runtime_security(program_files, owner, root)
    port, token = _read_config(config)
    return Endpoint(root / DLL_NAME, port, token)


def _choose_port() -> int:
    for _ in range(32):
        port = 39000 + secrets.randbelow(19001)
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
        return port
    raise GadgetAttachError("gadget_port_unavailable")


def prepare_secure_runtime() -> Endpoint:
    if not security.query_process_elevated():
        raise GadgetAttachError("gadget_elevation_required")
    present = existing_endpoint()
    if present is not None:
        return present
    archive = assets.gadget_archive_path()
    if assets.sha256_file(archive) != assets.GADGET_ARCHIVE_SHA256:
        raise GadgetAttachError("gadget_archive_hash_invalid")
    sid = security.current_user_sid()
    program_files, owner, root = _runtime_paths()
    security.ensure_protected_directory(
        root, user_sid=sid, trusted_root=program_files, security_root=owner,
        read_execute_sids=(security.LOCAL_SERVICE_SID,),
    )
    # The shared owner remains readable for RC003; this Google child must not
    # expose even the temporary token file to the ordinary desktop user.
    security._apply_path_security(
        root, user_sid=sid, directory=True,
        read_execute_sids=(security.LOCAL_SERVICE_SID,), include_user=False,
    )
    if not security.validate_path_security_sddl(
        security._read_path_security_sddl(root), user_sid=sid, directory=True,
        read_execute_sids=(security.LOCAL_SERVICE_SID,), include_user=False,
    ):
        raise GadgetAttachError("gadget_runtime_acl_invalid")
    dll = root / DLL_NAME
    security.assert_no_reparse_points(dll, trusted_root=program_files)
    if not dll.is_file() or assets.sha256_file(dll) != assets.GADGET_DLL_SHA256:
        temporary = root / f"{DLL_NAME}.{os.getpid()}.tmp"
        security.assert_no_reparse_points(temporary, trusted_root=program_files)
        try:
            with lzma.open(archive, "rb") as source, temporary.open("wb") as target:
                shutil.copyfileobj(source, target)
            if assets.sha256_file(temporary) != assets.GADGET_DLL_SHA256:
                raise GadgetAttachError("gadget_runtime_hash_invalid")
            security._apply_path_security(
                temporary, user_sid=sid, directory=False,
                read_execute_sids=(security.LOCAL_SERVICE_SID,),
            )
            os.replace(temporary, dll)
        finally:
            temporary.unlink(missing_ok=True)
    security._apply_path_security(
        dll, user_sid=sid, directory=False,
        read_execute_sids=(security.LOCAL_SERVICE_SID,),
    )
    config = root / CONFIG_NAME
    security.assert_no_reparse_points(config, trusted_root=program_files)
    port, token = _choose_port(), secrets.token_hex(32)
    assets._write_verified_text(
        config, json.dumps(_config(port, token), separators=(",", ":")) + "\n",
        user_sid=sid,
        include_user=False,
    )
    _assert_runtime_security(program_files, owner, root)
    return Endpoint(dll, *_read_config(config))


def _bind(dll, name, result, *args):
    function = getattr(dll, name)
    function.restype, function.argtypes = result, args
    return function


def _host_snapshot(pid: int) -> HostSnapshot:
    """Fail closed on missing modules, host exit, or an unexpected image."""
    kernel = C.WinDLL("kernel32", use_last_error=True)
    psapi = C.WinDLL("psapi", use_last_error=True)
    _bind(kernel, "OpenProcess", W.HANDLE, W.DWORD, W.BOOL, W.DWORD)
    _bind(kernel, "CloseHandle", W.BOOL, W.HANDLE)
    _bind(kernel, "WaitForSingleObject", W.DWORD, W.HANDLE, W.DWORD)
    _bind(kernel, "GetProcessTimes", W.BOOL, W.HANDLE, *([C.POINTER(W.FILETIME)] * 4))
    _bind(kernel, "QueryFullProcessImageNameW", W.BOOL, W.HANDLE, W.DWORD, W.LPWSTR, C.POINTER(W.DWORD))
    _bind(psapi, "EnumProcessModulesEx", W.BOOL, W.HANDLE, C.POINTER(W.HMODULE), W.DWORD,
          C.POINTER(W.DWORD), W.DWORD)
    _bind(psapi, "GetModuleFileNameExW", W.DWORD, W.HANDLE, W.HMODULE, W.LPWSTR, W.DWORD)
    handle = kernel.OpenProcess(0x0400 | 0x0010 | 0x100000, False, pid)
    if not handle:
        raise GadgetAttachError("gadget_host_unreadable")
    try:
        if kernel.WaitForSingleObject(handle, 0) != 258:
            raise GadgetAttachError("gadget_host_changed")
        times = [W.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *[C.byref(value) for value in times]):
            raise GadgetAttachError("gadget_host_unreadable")
        image = C.create_unicode_buffer(32768)
        size = W.DWORD(len(image))
        if not kernel.QueryFullProcessImageNameW(handle, 0, image, C.byref(size)):
            raise GadgetAttachError("gadget_host_unreadable")
        expected = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WUDFHost.exe"
        if os.path.normcase(image.value) != os.path.normcase(str(expected)):
            raise GadgetAttachError("gadget_host_invalid")
        modules = (W.HMODULE * 4096)()
        needed = W.DWORD()
        if not psapi.EnumProcessModulesEx(handle, modules, C.sizeof(modules), C.byref(needed), 3):
            raise GadgetAttachError("gadget_modules_unreadable")
        if needed.value > C.sizeof(modules):
            raise GadgetAttachError("gadget_modules_unreadable")
        paths = []
        for module in modules[:needed.value // C.sizeof(W.HMODULE)]:
            path = C.create_unicode_buffer(32768)
            length = psapi.GetModuleFileNameExW(handle, module, path, len(path))
            if not length or length >= len(path):
                raise GadgetAttachError("gadget_modules_unreadable")
            paths.append(Path(path.value))
        return HostSnapshot(times[0].dwLowDateTime | (times[0].dwHighDateTime << 32), tuple(paths))
    finally:
        kernel.CloseHandle(handle)


def _process_birth_if_alive(pid: int) -> int | None:
    """Protect a previous still-live host before replacing its attempt marker."""
    kernel = C.WinDLL("kernel32", use_last_error=True)
    _bind(kernel, "OpenProcess", W.HANDLE, W.DWORD, W.BOOL, W.DWORD)
    _bind(kernel, "CloseHandle", W.BOOL, W.HANDLE)
    _bind(kernel, "WaitForSingleObject", W.DWORD, W.HANDLE, W.DWORD)
    _bind(kernel, "GetProcessTimes", W.BOOL, W.HANDLE, *([C.POINTER(W.FILETIME)] * 4))
    handle = kernel.OpenProcess(0x1000 | 0x100000, False, pid)
    if not handle:
        if C.get_last_error() == 87:  # ERROR_INVALID_PARAMETER: PID no longer exists.
            return None
        raise GadgetAttachError("gadget_host_unreadable")
    try:
        state = kernel.WaitForSingleObject(handle, 0)
        if state == 0:
            return None
        if state != 258:
            raise GadgetAttachError("gadget_host_unreadable")
        times = [W.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *[C.byref(value) for value in times]):
            raise GadgetAttachError("gadget_host_unreadable")
        return times[0].dwLowDateTime | (times[0].dwHighDateTime << 32)
    finally:
        kernel.CloseHandle(handle)


def _check_modules(snapshot: HostSnapshot, endpoint: Endpoint) -> bool:
    own = []
    for module in snapshot.modules:
        name = module.name.casefold()
        if name == DLL_NAME.casefold():
            own.append(module)
        elif (name in {assets.GADGET_DLL_NAME.casefold(), "frida-agent.dll"}
              or "frida" in name and "gadget" in name and name.endswith(".dll")):
            raise GadgetAttachError("gadget_shared_host")
    if len(own) > 1 or (own and os.path.normcase(str(own[0])) != os.path.normcase(str(endpoint.dll))):
        raise GadgetAttachError("gadget_runtime_conflict")
    return bool(own)


def _rc003_host_member(pid: int) -> bool:
    """Do not make a shared host unusable by installing a second Gadget."""
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, assets.BTHLE_ENUM_KEY) as root:
            for index in range(winreg.QueryInfoKey(root)[0]):
                service_name = winreg.EnumKey(root, index)
                folded = service_name.casefold()
                if (not folded.startswith(assets.HID_SERVICE_PREFIX)
                        or assets.RC003_HARDWARE_TOKEN not in folded):
                    continue
                with winreg.OpenKey(root, service_name) as service:
                    for member in range(winreg.QueryInfoKey(service)[0]):
                        instance = winreg.EnumKey(service, member)
                        try:
                            with winreg.OpenKey(service, instance + "\\" + assets.WUDF_DIAGNOSTIC_SUFFIX) as info:
                                host, _kind = winreg.QueryValueEx(info, "HostPid")
                        except FileNotFoundError:
                            continue
                        if type(host) is not int or host <= 0:
                            raise GadgetAttachError("gadget_host_membership_unreadable")
                        if host == pid:
                            return True
    except OSError as exc:
        raise GadgetAttachError("gadget_host_membership_unreadable") from exc
    return False


def _attempt_marker(endpoint: Endpoint) -> Path:
    return endpoint.dll.parent / ATTEMPT_NAME


def _read_attempt(endpoint: Endpoint) -> tuple[int, int] | None:
    marker = _attempt_marker(endpoint)
    program_files, _owner, _root = _runtime_paths()
    security.assert_no_reparse_points(marker, trusted_root=program_files)
    if not marker.exists():
        return None
    sid = security.current_user_sid()
    if not marker.is_file() or not security.validate_path_security_sddl(
        security._read_path_security_sddl(marker), user_sid=sid,
        directory=False, read_execute_sids=(security.LOCAL_SERVICE_SID,),
    ):
        raise GadgetAttachError("gadget_inject_uncertain")
    try:
        raw = marker.read_bytes()
        if len(raw) > 128:
            raise ValueError("oversized")
        item = json.loads(raw)
        if (set(item) != {"pid", "born"} or type(item["pid"]) is not int
                or type(item["born"]) is not int or item["pid"] <= 0 or item["born"] <= 0):
            raise ValueError("invalid")
        return item["pid"], item["born"]
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise GadgetAttachError("gadget_inject_uncertain") from exc


def _write_attempt(endpoint: Endpoint, pid: int, born: int) -> None:
    marker = _attempt_marker(endpoint)
    program_files, _owner, _root = _runtime_paths()
    security.assert_no_reparse_points(marker, trusted_root=program_files)
    assets._write_verified_text(
        marker, json.dumps({"pid": pid, "born": born}, separators=(",", ":")) + "\n",
        user_sid=security.current_user_sid(),
    )


def pending_injection_for_host(pid: int) -> bool:
    """An earlier LoadLibrary may still complete after its caller timed out."""
    endpoint = existing_endpoint()
    if endpoint is None:
        return False
    snapshot = _host_snapshot(pid)
    return _read_attempt(endpoint) == (pid, snapshot.born)


def loaded_or_pending_for_host(pid: int) -> bool:
    """Choose a prior Gadget without enabling SeDebug before direct attach."""
    program_files, _owner, root = _runtime_paths()
    endpoint = existing_endpoint()
    if endpoint is None:
        # Preparation writes the config before any LoadLibrary attempt. A
        # half-built root without an attempt marker can safely retry direct.
        marker = root / ATTEMPT_NAME
        security.assert_no_reparse_points(marker, trusted_root=program_files)
        if os.path.lexists(marker):
            raise GadgetAttachError("gadget_config_invalid")
        return False
    owner = _listener_owner_pid(endpoint.port)
    if owner == pid:
        return True
    # A timed-out LoadLibrary can finish later. Without a live listener we
    # cannot prove this is the same host before SeDebug, so do not add an agent.
    marker = _read_attempt(endpoint)
    if marker is not None and marker[0] == pid:
        try:
            born = _process_birth_if_alive(pid)
        except GadgetAttachError:
            born = marker[1]  # Unreadable: keep the conservative stop.
        if born is not None and born != marker[1]:
            return False  # PID was reused by a different host.
        raise GadgetAttachError("gadget_inject_uncertain")
    return False


def _listener_owner_pid(port: int) -> int | None:
    """Read the OS TCP owner before sending a Frida command to localhost."""
    ip = C.WinDLL("iphlpapi", use_last_error=True)
    _bind(ip, "GetExtendedTcpTable", W.DWORD, C.c_void_p, C.POINTER(W.DWORD),
          W.BOOL, W.DWORD, W.DWORD, W.DWORD)
    size = W.DWORD()
    status = ip.GetExtendedTcpTable(None, C.byref(size), False, 2, 3, 0)
    if status != 122 or not 4 <= size.value <= 1024 * 1024:
        raise GadgetAttachError("gadget_listener_unreadable")
    buffer = C.create_string_buffer(size.value)
    if ip.GetExtendedTcpTable(buffer, C.byref(size), False, 2, 3, 0) != 0:
        raise GadgetAttachError("gadget_listener_unreadable")
    count = struct.unpack_from("<I", buffer, 0)[0]
    if 4 + count * 24 > size.value:
        raise GadgetAttachError("gadget_listener_unreadable")
    loopback = struct.unpack("<I", socket.inet_aton("127.0.0.1"))[0]
    owners = set()
    for index in range(count):
        state, address, raw_port, _peer, _peer_port, owner = struct.unpack_from(
            "<6I", buffer, 4 + index * 24)
        if (state == 2 and address == loopback
                and socket.ntohs(raw_port & 0xffff) == port):
            owners.add(owner)
    if len(owners) > 1:
        raise GadgetAttachError("gadget_listener_conflict")
    return next(iter(owners)) if owners else None


def _connect(frida, endpoint: Endpoint, pid: int, born: int):
    manager = frida.get_device_manager()
    address = f"127.0.0.1:{endpoint.port}"
    deadline = time.monotonic() + 8
    cancellable = frida.Cancellable()
    timer = threading.Timer(8, cancellable.cancel)
    timer.daemon = True
    timer.start()
    try:
        while time.monotonic() < deadline:
            owner = _listener_owner_pid(endpoint.port)
            if owner is None:
                time.sleep(.1)
                continue
            if owner != pid or _host_snapshot(pid).born != born:
                raise GadgetAttachError("gadget_listener_conflict")
            device = session = script = None
            matched = False
            cleanup_failed = False
            try:
                device = manager.add_remote_device(address, token=endpoint.token,
                                                   cancellable=cancellable)
                processes = device.enumerate_processes(cancellable=cancellable)
                if len(processes) != 1 or processes[0].name != "Gadget":
                    raise GadgetAttachError("gadget_identity_mismatch")
                session = device.attach(processes[0].pid, cancellable=cancellable)
                script = session.create_script(_IDENTITY_SCRIPT, cancellable=cancellable)
                script.load(cancellable=cancellable)
                with cancellable:
                    matched = script.exports_sync.identity() == pid
                if not matched:
                    raise GadgetAttachError("gadget_identity_mismatch")
            except GadgetAttachError:
                raise
            except Exception as exc:
                raise GadgetAttachError("gadget_connect_failed") from exc
            finally:
                if script is not None:
                    try:
                        script.unload(cancellable=cancellable)
                    except Exception:
                        matched = False
                        cleanup_failed = True
                if session is not None and not matched:
                    try:
                        session.detach(cancellable=cancellable)
                    except Exception:
                        cleanup_failed = True
                if device is not None and not matched:
                    try:
                        manager.remove_remote_device(address, cancellable=cancellable)
                    except Exception:
                        cleanup_failed = True
                if cleanup_failed:
                    raise GadgetAttachError("gadget_cleanup_failed")
            if matched:
                return session, device, address
    finally:
        timer.cancel()
        timer.join(timeout=.5)
    raise GadgetAttachError("gadget_connect_failed")


def bounded_remote_cleanup(frida, operation: Callable, *, seconds: float = 5) -> None:
    cancellable = frida.Cancellable()
    timer = threading.Timer(seconds, cancellable.cancel)
    timer.daemon = True
    timer.start()
    try:
        operation(cancellable)
    finally:
        timer.cancel()
        timer.join(timeout=.5)


def attach(frida, pid: int, address: str, host_check: Callable[[], tuple[int, str]],
           cancelled: Callable[[], bool] = lambda: False):
    """Return a Frida session, Gadget device, and its authenticated address."""
    if not security.query_process_elevated():
        raise GadgetAttachError("gadget_elevation_required")
    if cancelled():
        raise GadgetAttachError("gadget_cancelled")
    if host_check() != (pid, address):
        raise GadgetAttachError("gadget_host_changed")
    enable_debug_privilege()
    result = []
    def operation():
        initial = _host_snapshot(pid)
        for module in initial.modules:
            name = module.name.casefold()
            if (name in {assets.GADGET_DLL_NAME.casefold(), "frida-agent.dll"}
                    or name != DLL_NAME.casefold() and "frida" in name
                    and "gadget" in name and name.endswith(".dll")):
                raise GadgetAttachError("gadget_shared_host")
        endpoint = prepare_secure_runtime()
        loaded = _check_modules(initial, endpoint)
        prior = _read_attempt(endpoint)
        if not loaded:
            if prior == (pid, initial.born):
                raise GadgetAttachError("gadget_inject_uncertain")
            if prior is not None and _process_birth_if_alive(prior[0]) == prior[1]:
                raise GadgetAttachError("gadget_listener_conflict")
            if _rc003_host_member(pid):
                raise GadgetAttachError("gadget_rc003_host_conflict")
            owner = _listener_owner_pid(endpoint.port)
            if owner is not None:
                raise GadgetAttachError("gadget_listener_conflict")
            if (cancelled() or host_check() != (pid, address)
                    or _host_snapshot(pid).born != initial.born):
                if cancelled():
                    raise GadgetAttachError("gadget_cancelled")
                raise GadgetAttachError("gadget_host_changed")
            if assets.sha256_file(endpoint.dll) != assets.GADGET_DLL_SHA256:
                raise GadgetAttachError("gadget_runtime_hash_invalid")
            if cancelled():
                raise GadgetAttachError("gadget_cancelled")
            _write_attempt(endpoint, pid, initial.born)
            try:
                inject_library(pid, endpoint.dll)
            except Exception as exc:
                raise GadgetAttachError("gadget_inject_failed") from exc
        if host_check() != (pid, address):
            raise GadgetAttachError("gadget_host_changed")
        latest = _host_snapshot(pid)
        if latest.born != initial.born or not _check_modules(latest, endpoint):
            raise GadgetAttachError("gadget_host_changed")
        result.append((initial, endpoint))
    try:
        security._run_serialized_helper_operation(operation, lock_timeout_seconds=5)
    except security.HidElevationError as exc:
        detail = ("gadget_lock_unavailable" if str(exc) in
                  {"hid_helper_operation_busy", "hid_helper_operation_unavailable"}
                  else "gadget_runtime_prepare_failed")
        raise GadgetAttachError(detail) from exc
    initial, endpoint = result[0]
    if cancelled():
        raise GadgetAttachError("gadget_cancelled")
    session, device, device_address = _connect(frida, endpoint, pid, initial.born)
    if host_check() != (pid, address) or _host_snapshot(pid).born != initial.born:
        cleanup_failed = False
        for operation in (
            lambda cancel: session.detach(cancellable=cancel),
            lambda cancel: frida.get_device_manager().remove_remote_device(
                device_address, cancellable=cancel),
        ):
            try:
                bounded_remote_cleanup(frida, operation)
            except Exception:
                cleanup_failed = True
        if cleanup_failed:
            raise GadgetAttachError("gadget_cleanup_failed")
        raise GadgetAttachError("gadget_host_changed")
    return session, device, device_address
