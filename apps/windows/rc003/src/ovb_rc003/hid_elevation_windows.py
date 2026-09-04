"""Pre-authorized elevated helper lifecycle for the RC003 HID tap.

The desktop application stays at normal integrity.  A user-approved setup
copies the narrow helper to Program Files and registers one on-demand Task
Scheduler action for the current administrator account.  The task accepts no
target PID or executable path: the elevated helper resolves and validates the
current RC003 WUDFHost itself before injecting the pinned Gadget.
"""

from __future__ import annotations

import argparse
import csv
import ctypes
import hashlib
import html
import locale
import os
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence


TASK_NAME = r"\RemoteMic\RC003\HidTapInjector"
HELPER_EXE_NAME = "RemoteMicRC003HidHelper.exe"
HELPER_BUNDLE_RELATIVE_PATH = Path("_internal") / HELPER_EXE_NAME
INSTALL_FLAG = "--install-task"
UNINSTALL_FLAG = "--uninstall-task"
INJECT_FLAG = "--inject"
TASK_EXECUTION_LIMIT = "PT30S"
TASK_FILE_RELATIVE_PATH = Path("RemoteMic") / "RC003" / "HidTapInjector"

HELPER_EXIT_OK = 0
HELPER_EXIT_REQUIRES_ADMIN = 3
HELPER_EXIT_VALIDATION_FAILED = 4
HELPER_EXIT_UNEXPECTED_FAILURE = 5

TASK_XML_NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"


class HidElevationError(RuntimeError):
    """Sanitized failure raised to the normal-integrity bridge."""


@dataclass(frozen=True)
class HidHelperState:
    available: bool
    detail: str = ""


def _is_windows() -> bool:
    return os.name == "nt"


def is_process_elevated() -> bool:
    if not _is_windows():
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
    except Exception:
        return False


def _program_files_root(environ: Optional[dict[str, str]] = None) -> Path:
    values = os.environ if environ is None else environ
    raw = values.get("PROGRAMW6432") or values.get("PROGRAMFILES")
    if not raw:
        raise HidElevationError("program_files_unavailable")
    return Path(raw)


def protected_helper_path(
    *, environ: Optional[dict[str, str]] = None
) -> Path:
    return (
        _program_files_root(environ)
        / "RemoteMic"
        / "RC003"
        / "HidHelper"
        / HELPER_EXE_NAME
    )


def bundled_helper_path(
    *, frozen: Optional[bool] = None, executable: Optional[str] = None
) -> Optional[Path]:
    if frozen is None:
        frozen = bool(getattr(sys, "frozen", False))
    if not frozen:
        return None
    base = Path(executable or sys.executable).resolve().parent
    return base / HELPER_BUNDLE_RELATIVE_PATH


def is_installed_distribution(
    *, frozen: Optional[bool] = None, executable: Optional[str] = None
) -> bool:
    if frozen is None:
        frozen = bool(getattr(sys, "frozen", False))
    if not frozen:
        return False
    base = Path(executable or sys.executable).resolve().parent
    return (base / "unins000.exe").is_file()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def current_user_sid(
    *, _run: Callable[..., subprocess.CompletedProcess] = subprocess.run
) -> str:
    if not _is_windows():
        raise HidElevationError("windows_only")
    completed = _run(
        ["whoami.exe", "/user", "/fo", "csv", "/nh"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding=locale.getpreferredencoding(False),
        errors="replace",
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode != 0:
        raise HidElevationError("current_user_sid_unavailable")
    try:
        row = next(csv.reader([completed.stdout.strip()]))
        sid = row[1].strip()
    except (IndexError, StopIteration, csv.Error) as exc:
        raise HidElevationError("current_user_sid_unavailable") from exc
    if not sid.startswith("S-1-"):
        raise HidElevationError("current_user_sid_invalid")
    return sid


def task_definition_xml(helper_path: Path, user_sid: str) -> str:
    path_text = html.escape(str(Path(helper_path).resolve()))
    sid_text = html.escape(str(user_sid).strip())
    if not sid_text.startswith("S-1-"):
        raise ValueError("task user SID is invalid")
    return f"""<?xml version=\"1.0\" encoding=\"UTF-16\"?>
<Task version=\"1.4\" xmlns=\"{TASK_XML_NAMESPACE}\">
  <RegistrationInfo>
    <Description>Remote Mic RC003 narrow HID report injector.</Description>
    <URI>{html.escape(TASK_NAME)}</URI>
  </RegistrationInfo>
  <Principals>
    <Principal id=\"Author\">
      <UserId>{sid_text}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>false</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>true</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>{TASK_EXECUTION_LIMIT}</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context=\"Author\">
    <Exec>
      <Command>{path_text}</Command>
      <Arguments>{INJECT_FLAG}</Arguments>
      <WorkingDirectory>{html.escape(str(Path(helper_path).resolve().parent))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _schtasks_command(*parts: str) -> list[str]:
    return ["schtasks.exe", *parts]


def _run_schtasks(
    parts: Sequence[str],
    *,
    _run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> subprocess.CompletedProcess:
    kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "check": False,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return _run(_schtasks_command(*parts), **kwargs)


def _task_xml_value(root: ET.Element, path: str) -> str:
    node = root.find(path, {"t": TASK_XML_NAMESPACE})
    return "" if node is None or node.text is None else node.text.strip()


def validate_registered_task_xml(
    xml_text: str,
    *,
    helper_path: Path,
    user_sid: str,
) -> bool:
    try:
        root = ET.fromstring(xml_text.lstrip("\ufeff"))
    except (ET.ParseError, TypeError, ValueError):
        return False
    namespace = {"t": TASK_XML_NAMESPACE}
    if root.tag != f"{{{TASK_XML_NAMESPACE}}}Task":
        return False
    exec_nodes = root.findall("./t:Actions/t:Exec", namespace)
    principal_nodes = root.findall("./t:Principals/t:Principal", namespace)
    if len(exec_nodes) != 1 or len(principal_nodes) != 1:
        return False
    command = _task_xml_value(root, ".//t:Actions/t:Exec/t:Command")
    arguments = _task_xml_value(root, ".//t:Actions/t:Exec/t:Arguments")
    working_directory = _task_xml_value(
        root, ".//t:Actions/t:Exec/t:WorkingDirectory"
    )
    principal = _task_xml_value(root, ".//t:Principals/t:Principal/t:UserId")
    run_level = _task_xml_value(root, ".//t:Principals/t:Principal/t:RunLevel")
    logon_type = _task_xml_value(root, ".//t:Principals/t:Principal/t:LogonType")
    allow_demand = _task_xml_value(root, ".//t:Settings/t:AllowStartOnDemand")
    enabled = _task_xml_value(root, ".//t:Settings/t:Enabled")
    multiple_instances = _task_xml_value(
        root, ".//t:Settings/t:MultipleInstancesPolicy"
    )
    execution_limit = _task_xml_value(
        root, ".//t:Settings/t:ExecutionTimeLimit"
    )
    task_uri = _task_xml_value(root, ".//t:RegistrationInfo/t:URI")
    expected = Path(helper_path).resolve()
    return (
        os.path.normcase(command) == os.path.normcase(str(expected))
        and arguments == INJECT_FLAG
        and os.path.normcase(working_directory)
        == os.path.normcase(str(expected.parent))
        and principal.casefold() == str(user_sid).strip().casefold()
        and run_level == "HighestAvailable"
        and logon_type == "InteractiveToken"
        and allow_demand.casefold() == "true"
        and enabled.casefold() == "true"
        and multiple_instances == "IgnoreNew"
        and execution_limit == TASK_EXECUTION_LIMIT
        and task_uri == TASK_NAME
        and root.find(".//t:Triggers", namespace) is None
    )


def _decode_schtasks_xml(output: object) -> str:
    if isinstance(output, str):
        return output
    if not isinstance(output, (bytes, bytearray)):
        raise HidElevationError("hid_helper_task_query_invalid")
    raw = bytes(output)
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    if b"\x00" in raw[:8]:
        return raw.decode("utf-16-le")
    return raw.decode(locale.getpreferredencoding(False), errors="replace")


def _query_task_xml(
    *,
    _run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> str:
    completed = _run_schtasks(
        ["/Query", "/TN", TASK_NAME, "/XML"], _run=_run
    )
    if completed.returncode != 0:
        raise HidElevationError("hid_helper_task_missing")
    return _decode_schtasks_xml(completed.stdout)


def inspect_installed_helper(
    *,
    frozen: Optional[bool] = None,
    executable: Optional[str] = None,
    _run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    user_sid: Optional[str] = None,
    protected_path: Optional[Path] = None,
) -> HidHelperState:
    if not _is_windows():
        return HidHelperState(False, "windows_only")
    bundled = bundled_helper_path(frozen=frozen, executable=executable)
    if bundled is None:
        return HidHelperState(False, "source_runtime")
    if not bundled.is_file():
        return HidHelperState(False, "bundled_helper_missing")
    target = protected_path or protected_helper_path()
    if not target.is_file():
        return HidHelperState(False, "protected_helper_missing")
    try:
        if _sha256(bundled) != _sha256(target):
            return HidHelperState(False, "protected_helper_version_mismatch")
        sid = user_sid or current_user_sid(_run=_run)
        task_xml = _query_task_xml(_run=_run)
    except HidElevationError as exc:
        return HidHelperState(False, str(exc))
    except OSError:
        return HidHelperState(False, "hid_helper_task_query_failed")
    if not validate_registered_task_xml(
        task_xml, helper_path=target, user_sid=sid
    ):
        return HidHelperState(False, "hid_helper_task_invalid")
    return HidHelperState(True)


def run_registered_injector(
    expected_pid: int,
    *,
    _run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    _host_pid: Optional[Callable[[], Optional[int]]] = None,
) -> None:
    if not isinstance(expected_pid, int) or isinstance(expected_pid, bool) or expected_pid <= 0:
        raise ValueError("expected PID must be a positive integer")
    if not _is_windows():
        raise HidElevationError("windows_only")
    if _host_pid is None:
        from .frida_hid_tap_runtime import find_rc003_hidogatt_host_pid

        _host_pid = find_rc003_hidogatt_host_pid
    if _host_pid() != expected_pid:
        raise HidElevationError("hid_helper_host_changed")
    state = inspect_installed_helper(_run=_run)
    if not state.available:
        raise HidElevationError(state.detail or "hid_helper_unavailable")
    completed = _run_schtasks(["/Run", "/TN", TASK_NAME], _run=_run)
    if completed.returncode != 0:
        raise HidElevationError("hid_helper_task_start_failed")


def task_file_path(
    *, environ: Optional[dict[str, str]] = None
) -> Path:
    values = os.environ if environ is None else environ
    windows_root = values.get("WINDIR") or values.get("SYSTEMROOT")
    if not windows_root:
        raise HidElevationError("windows_root_unavailable")
    return Path(windows_root) / "System32" / "Tasks" / TASK_FILE_RELATIVE_PATH


def _copy_verified_helper(source: Path, destination: Path) -> None:
    source = source.resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f".{destination.name}.{os.getpid()}.{time.time_ns()}.tmp"
    )
    try:
        shutil.copy2(source, temporary)
        if _sha256(source) != _sha256(temporary):
            raise RuntimeError("helper copy hash mismatch")
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def install_task(
    *,
    source_executable: Optional[Path] = None,
    target_path: Optional[Path] = None,
    user_sid: Optional[str] = None,
    _run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> None:
    if not _is_windows() or not is_process_elevated():
        raise PermissionError("administrator elevation required")
    source = Path(source_executable or sys.executable)
    target = target_path or protected_helper_path()
    sid = user_sid or current_user_sid(_run=_run)
    _copy_verified_helper(source, target)
    xml_text = task_definition_xml(target, sid)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=".hid-task-", suffix=".xml", dir=target.parent
    )
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        temporary.write_text(xml_text, encoding="utf-16")
        completed = _run_schtasks(
            ["/Create", "/TN", TASK_NAME, "/XML", str(temporary), "/F"],
            _run=_run,
        )
        if completed.returncode != 0:
            raise HidElevationError("hid_helper_task_registration_failed")
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    registered = _query_task_xml(_run=_run)
    if not validate_registered_task_xml(
        registered, helper_path=target, user_sid=sid
    ):
        raise HidElevationError("hid_helper_task_verification_failed")


def uninstall_task(
    *,
    target_path: Optional[Path] = None,
    _run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> None:
    if not _is_windows() or not is_process_elevated():
        raise PermissionError("administrator elevation required")
    task_path = task_file_path()
    task_existed = task_path.is_file()
    completed = _run_schtasks(
        ["/Delete", "/TN", TASK_NAME, "/F"], _run=_run
    )
    if completed.returncode != 0 and task_existed:
        raise HidElevationError("hid_helper_task_removal_failed")
    if task_path.is_file():
        raise HidElevationError("hid_helper_task_removal_failed")
    target = target_path or protected_helper_path()
    try:
        target.unlink()
    except FileNotFoundError:
        pass
    except PermissionError as exc:
        raise HidElevationError("protected_helper_removal_failed") from exc
    try:
        target.parent.rmdir()
        target.parent.parent.rmdir()
        target.parent.parent.parent.rmdir()
    except OSError:
        pass


class _ShellExecuteInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint32),
        ("fMask", ctypes.c_ulong),
        ("hwnd", ctypes.c_void_p),
        ("lpVerb", ctypes.c_wchar_p),
        ("lpFile", ctypes.c_wchar_p),
        ("lpParameters", ctypes.c_wchar_p),
        ("lpDirectory", ctypes.c_wchar_p),
        ("nShow", ctypes.c_int),
        ("hInstApp", ctypes.c_void_p),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", ctypes.c_wchar_p),
        ("hkeyClass", ctypes.c_void_p),
        ("dwHotKey", ctypes.c_uint32),
        ("hIconOrMonitor", ctypes.c_void_p),
        ("hProcess", ctypes.c_void_p),
    ]


def _run_elevated_and_wait(
    executable: Path,
    arguments: str,
    timeout_seconds: float,
) -> int:
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = (ctypes.POINTER(_ShellExecuteInfo),)
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetExitCodeProcess.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    )
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    info = _ShellExecuteInfo()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x00000040  # SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = "runas"
    info.lpFile = str(executable)
    info.lpParameters = str(arguments)
    info.lpDirectory = str(Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32")
    info.nShow = 0
    ctypes.set_last_error(0)
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        error = int(ctypes.get_last_error())
        if error == 1223:
            raise HidElevationError("uac_cancelled")
        raise HidElevationError("hid_helper_setup_launch_failed")
    if not info.hProcess:
        raise HidElevationError("hid_helper_setup_process_missing")
    try:
        timeout_ms = max(1, min(0xFFFFFFFE, int(float(timeout_seconds) * 1000)))
        wait_result = int(kernel32.WaitForSingleObject(info.hProcess, timeout_ms))
        if wait_result == 0x00000102:
            raise HidElevationError("hid_helper_setup_timeout")
        if wait_result != 0:
            raise HidElevationError("hid_helper_setup_wait_failed")
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(exit_code)):
            raise HidElevationError("hid_helper_setup_result_unavailable")
        return int(exit_code.value)
    finally:
        kernel32.CloseHandle(info.hProcess)


def request_install_elevation(
    *,
    helper_path: Optional[Path] = None,
    timeout_seconds: float = 120.0,
    _launch: Callable[[Path, str, float], int] = _run_elevated_and_wait,
    _inspect: Callable[[], HidHelperState] = inspect_installed_helper,
) -> HidHelperState:
    if not _is_windows():
        return HidHelperState(False, "windows_only")
    source = helper_path or bundled_helper_path()
    if source is None or not source.is_file():
        return HidHelperState(False, "bundled_helper_missing")
    try:
        exit_code = _launch(source, INSTALL_FLAG, timeout_seconds)
    except HidElevationError as exc:
        return HidHelperState(False, str(exc))
    except (OSError, ValueError):
        return HidHelperState(False, "hid_helper_setup_launch_failed")
    if exit_code != HELPER_EXIT_OK:
        return HidHelperState(False, f"hid_helper_setup_exit_{exit_code}")
    return _inspect()


def _inject_once() -> None:
    from .frida_hid_tap_injector import inject_current_process
    from .frida_hid_tap_runtime import find_rc003_hidogatt_host_pid

    pid = find_rc003_hidogatt_host_pid()
    if pid is None:
        raise RuntimeError("RC003 WUDFHost is unavailable")
    inject_current_process(pid)


def helper_main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(INSTALL_FLAG, action="store_true")
    group.add_argument(UNINSTALL_FLAG, action="store_true")
    group.add_argument(INJECT_FLAG, action="store_true")
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
        if getattr(args, INSTALL_FLAG[2:].replace("-", "_")):
            install_task()
        elif getattr(args, UNINSTALL_FLAG[2:].replace("-", "_")):
            uninstall_task()
        else:
            _inject_once()
        return HELPER_EXIT_OK
    except PermissionError:
        return HELPER_EXIT_REQUIRES_ADMIN
    except (HidElevationError, OSError, RuntimeError, ValueError):
        return HELPER_EXIT_VALIDATION_FAILED
    except Exception:
        return HELPER_EXIT_UNEXPECTED_FAILURE
