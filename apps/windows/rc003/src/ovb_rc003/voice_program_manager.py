"""Optional launcher for third-party voice-input programs.

The bridge does not depend on any provider. A configured provider may be
discovered and started on explicit request or at bridge startup, but every
failure is reported as a provider status rather than a bridge startup error.
"""

from __future__ import annotations

import ctypes
import os
import re
import sys
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional, Sequence

VOICE_PROGRAM_NONE = "none"
VOICE_PROGRAM_SOGOU = "sogou"
VOICE_PROGRAM_CUSTOM = "custom"

VOICE_PROGRAM_PROVIDER_ORDER = (
    VOICE_PROGRAM_NONE,
    VOICE_PROGRAM_SOGOU,
    VOICE_PROGRAM_CUSTOM,
)

VOICE_PROGRAM_PROVIDER_NAMES = {
    VOICE_PROGRAM_NONE: "不管理",
    VOICE_PROGRAM_SOGOU: "搜狗语音输入",
    VOICE_PROGRAM_CUSTOM: "自定义程序",
}

_SOGOU_PROCESS_NAME = "sogou_voice_assistant.exe"
_SOGOU_RUN_VALUE_NAMES = ("搜狗语音输入法",)
_ALLOWED_EXECUTABLE_SUFFIXES = frozenset({".exe", ".lnk"})
_ERROR_CANCELLED = 1223


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    name: str
    executable: Optional[Path] = None
    elevated: Optional[bool] = None


@dataclass(frozen=True)
class ResolvedVoiceProgram:
    provider_id: str
    display_name: str
    executable: Optional[Path]
    process_names: tuple[str, ...]
    source: str

    @property
    def available(self) -> bool:
        return self.executable is not None


@dataclass(frozen=True)
class VoiceProgramStatus:
    provider_id: str
    display_name: str
    available: bool
    running: bool
    elevated: Optional[bool]
    executable: Optional[Path]
    code: str


@dataclass(frozen=True)
class VoiceProgramLaunchResult:
    provider_id: str
    started: bool
    already_running: bool
    code: str
    elevated: Optional[bool] = None


def normalize_voice_program_settings(raw: object) -> dict[str, object]:
    """Return the stable persisted shape for optional provider management."""

    data = raw if isinstance(raw, Mapping) else {}
    provider_id = str(data.get("provider", VOICE_PROGRAM_NONE)).strip().lower()
    if provider_id not in VOICE_PROGRAM_PROVIDER_ORDER:
        provider_id = VOICE_PROGRAM_NONE
    executable = str(data.get("custom_executable", "")).strip()
    enabled = provider_id != VOICE_PROGRAM_NONE
    return {
        "provider": provider_id,
        "custom_executable": executable,
        "launch_on_bridge_start": (
            enabled and data.get("launch_on_bridge_start") is True
        ),
        "launch_elevated": enabled and data.get("launch_elevated") is True,
    }


def provider_options() -> list[str]:
    return [VOICE_PROGRAM_PROVIDER_NAMES[item] for item in VOICE_PROGRAM_PROVIDER_ORDER]


def provider_id_for_index(index: int) -> str:
    if 0 <= index < len(VOICE_PROGRAM_PROVIDER_ORDER):
        return VOICE_PROGRAM_PROVIDER_ORDER[index]
    return VOICE_PROGRAM_NONE


def provider_index(provider_id: object) -> int:
    normalized = str(provider_id).strip().lower()
    try:
        return VOICE_PROGRAM_PROVIDER_ORDER.index(normalized)
    except ValueError:
        return 0


def status_text(status: VoiceProgramStatus) -> str:
    if status.code == "disabled":
        return "未启用；Remote Mic 不会管理语音程序。"
    if status.code == "not_found":
        return f"未找到{status.display_name}。"
    if status.code == "stopped":
        return "已找到，当前未运行。"
    if status.code == "running":
        if status.elevated is True:
            return "正在运行（管理员权限）。"
        if status.elevated is False:
            return "正在运行（普通权限）。"
        return "正在运行（权限状态未知）。"
    return "状态未知。"


def launch_result_text(result: VoiceProgramLaunchResult) -> str:
    messages = {
        "disabled": "未启用语音程序管理。",
        "not_found": "没有找到可启动的语音程序。",
        "started": (
            "已请求以管理员权限启动语音程序。"
            if result.elevated is True
            else "已启动语音程序。"
        ),
        "already_running": "语音程序已经在运行。",
        "restart_elevated_required": (
            "语音程序正以普通权限运行；请先退出它，再用管理员方式启动。"
        ),
        "cancelled": "已取消管理员启动。",
        "launch_failed": "语音程序启动失败。",
        "not_requested": "没有设置随桥接启动。",
    }
    return messages.get(result.code, "语音程序状态未知。")


def resolve_voice_program(
    settings: Mapping[str, object],
    *,
    platform: Optional[str] = None,
    process_iter: Optional[Callable[[], Iterable[ProcessInfo]]] = None,
    run_value_reader: Optional[Callable[[], Iterable[str]]] = None,
) -> ResolvedVoiceProgram:
    normalized = normalize_voice_program_settings(settings)
    provider_id = str(normalized["provider"])
    display_name = VOICE_PROGRAM_PROVIDER_NAMES[provider_id]
    configured_path = _validated_configured_path(normalized["custom_executable"])

    if provider_id == VOICE_PROGRAM_NONE:
        return ResolvedVoiceProgram(provider_id, display_name, None, (), "disabled")
    if provider_id == VOICE_PROGRAM_CUSTOM:
        process_names = (
            (configured_path.name.casefold(),)
            if configured_path is not None
            and configured_path.suffix.casefold() == ".exe"
            else ()
        )
        return ResolvedVoiceProgram(
            provider_id,
            display_name,
            configured_path,
            process_names,
            "configured" if configured_path is not None else "missing",
        )

    executable = discover_sogou_voice_executable(
        platform=platform,
        process_iter=process_iter,
        run_value_reader=run_value_reader,
    )
    return ResolvedVoiceProgram(
        provider_id,
        display_name,
        executable,
        (_SOGOU_PROCESS_NAME,),
        "discovered" if executable is not None else "missing",
    )


def inspect_voice_program(
    settings: Mapping[str, object],
    *,
    platform: Optional[str] = None,
    process_iter: Optional[Callable[[], Iterable[ProcessInfo]]] = None,
    run_value_reader: Optional[Callable[[], Iterable[str]]] = None,
) -> VoiceProgramStatus:
    resolved = resolve_voice_program(
        settings,
        platform=platform,
        process_iter=process_iter,
        run_value_reader=run_value_reader,
    )
    if resolved.provider_id == VOICE_PROGRAM_NONE:
        return VoiceProgramStatus(
            resolved.provider_id,
            resolved.display_name,
            False,
            False,
            None,
            None,
            "disabled",
        )
    if not resolved.available:
        return VoiceProgramStatus(
            resolved.provider_id,
            resolved.display_name,
            False,
            False,
            None,
            None,
            "not_found",
        )

    processes = list((process_iter or _iter_windows_processes)())
    matches = _matching_processes(resolved, processes)
    elevated = _combined_elevation(matches)
    return VoiceProgramStatus(
        resolved.provider_id,
        resolved.display_name,
        True,
        bool(matches),
        elevated,
        resolved.executable,
        "running" if matches else "stopped",
    )


def launch_voice_program(
    settings: Mapping[str, object],
    *,
    platform: Optional[str] = None,
    process_iter: Optional[Callable[[], Iterable[ProcessInfo]]] = None,
    run_value_reader: Optional[Callable[[], Iterable[str]]] = None,
    start_file: Optional[Callable[[str, str, str], None]] = None,
) -> VoiceProgramLaunchResult:
    """Start the configured provider without making it a bridge dependency."""

    normalized = normalize_voice_program_settings(settings)
    resolved = resolve_voice_program(
        normalized,
        platform=platform,
        process_iter=process_iter,
        run_value_reader=run_value_reader,
    )
    if resolved.provider_id == VOICE_PROGRAM_NONE:
        return VoiceProgramLaunchResult(resolved.provider_id, False, False, "disabled")
    if resolved.executable is None:
        return VoiceProgramLaunchResult(resolved.provider_id, False, False, "not_found")

    processes = list((process_iter or _iter_windows_processes)())
    matches = _matching_processes(resolved, processes)
    request_elevation = normalized["launch_elevated"] is True
    running_elevation = _combined_elevation(matches)
    if matches:
        if request_elevation and running_elevation is False:
            return VoiceProgramLaunchResult(
                resolved.provider_id,
                False,
                True,
                "restart_elevated_required",
                elevated=False,
            )
        return VoiceProgramLaunchResult(
            resolved.provider_id,
            False,
            True,
            "already_running",
            elevated=running_elevation,
        )

    operation = "runas" if request_elevation else "open"
    launcher = start_file or _default_start_file
    try:
        launcher(str(resolved.executable), operation, str(resolved.executable.parent))
    except OSError as exc:
        if getattr(exc, "winerror", None) == _ERROR_CANCELLED:
            return VoiceProgramLaunchResult(
                resolved.provider_id, False, False, "cancelled"
            )
        return VoiceProgramLaunchResult(resolved.provider_id, False, False, "launch_failed")
    except Exception:
        return VoiceProgramLaunchResult(resolved.provider_id, False, False, "launch_failed")
    return VoiceProgramLaunchResult(
        resolved.provider_id,
        True,
        False,
        "started",
        elevated=True if request_elevation else None,
    )


def launch_configured_at_bridge_start(
    config_data: Mapping[str, object],
    *,
    launcher: Optional[
        Callable[[Mapping[str, object]], VoiceProgramLaunchResult]
    ] = None,
) -> VoiceProgramLaunchResult:
    settings = normalize_voice_program_settings(config_data.get("voice_program"))
    if settings["launch_on_bridge_start"] is not True:
        return VoiceProgramLaunchResult(str(settings["provider"]), False, False, "not_requested")
    return (launcher or launch_voice_program)(settings)


def discover_sogou_voice_executable(
    *,
    platform: Optional[str] = None,
    process_iter: Optional[Callable[[], Iterable[ProcessInfo]]] = None,
    run_value_reader: Optional[Callable[[], Iterable[str]]] = None,
) -> Optional[Path]:
    current_platform = sys.platform if platform is None else platform
    if current_platform != "win32":
        return None

    for process in (process_iter or _iter_windows_processes)():
        if (
            process.name.casefold() == _SOGOU_PROCESS_NAME
            and process.executable is not None
            and process.executable.is_file()
        ):
            return process.executable

    candidates: list[Path] = []
    for command in (run_value_reader or _read_sogou_run_values)():
        manager_path = _command_executable(command)
        if manager_path is None:
            continue
        components_dir = manager_path.parent
        voice_root = components_dir / "ai_voice_input"
        if not voice_root.is_dir():
            continue
        candidates.extend(
            voice_root.glob("*/bin/sogou_voice_assistant.exe")
        )
    existing = [path for path in candidates if path.is_file()]
    if not existing:
        return None
    return max(existing, key=_sogou_version_key)


def _validated_configured_path(raw: object) -> Optional[Path]:
    text = str(raw).strip()
    if not text:
        return None
    path = Path(text).expanduser()
    if path.suffix.casefold() not in _ALLOWED_EXECUTABLE_SUFFIXES:
        return None
    try:
        return path.resolve(strict=True)
    except OSError:
        return None


def _command_executable(command: str) -> Optional[Path]:
    match = re.match(r'^\s*(?:"([^"]+)"|(\S+))', str(command))
    if match is None:
        return None
    return Path(match.group(1) or match.group(2))


def _sogou_version_key(path: Path) -> tuple[int, ...]:
    version_text = path.parents[1].name if len(path.parents) > 1 else ""
    numbers = tuple(int(item) for item in re.findall(r"\d+", version_text))
    return numbers or (0,)


def _matching_processes(
    resolved: ResolvedVoiceProgram, processes: Sequence[ProcessInfo]
) -> list[ProcessInfo]:
    expected_path = (
        os.path.normcase(str(resolved.executable)) if resolved.executable else ""
    )
    expected_names = {name.casefold() for name in resolved.process_names}
    matches: list[ProcessInfo] = []
    for process in processes:
        if process.executable is not None and expected_path:
            if os.path.normcase(str(process.executable)) == expected_path:
                matches.append(process)
                continue
        if process.name.casefold() in expected_names:
            matches.append(process)
    return matches


def _combined_elevation(processes: Sequence[ProcessInfo]) -> Optional[bool]:
    values = [process.elevated for process in processes if process.elevated is not None]
    if not values:
        return None
    return any(values)


def _read_sogou_run_values() -> Iterable[str]:
    if sys.platform != "win32":
        return ()
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
        ) as key:
            values = []
            for name in _SOGOU_RUN_VALUE_NAMES:
                try:
                    value, _ = winreg.QueryValueEx(key, name)
                except OSError:
                    continue
                values.append(str(value))
            return tuple(values)
    except OSError:
        return ()


def _default_start_file(path: str, operation: str, cwd: str) -> None:
    if sys.platform != "win32" or not hasattr(os, "startfile"):
        raise OSError("voice program launch requires Windows")
    os.startfile(path, operation, cwd=cwd)  # type: ignore[attr-defined,call-arg]


def _iter_windows_processes() -> Iterable[ProcessInfo]:
    if sys.platform != "win32":
        return ()

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL

    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    invalid_handle = ctypes.c_void_p(-1).value
    if snapshot == invalid_handle:
        return ()

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    entries: list[ProcessInfo] = []
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    try:
        success = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while success:
            pid = int(entry.th32ProcessID)
            executable, elevated = _query_process_details(kernel32, advapi32, pid)
            entries.append(
                ProcessInfo(
                    pid=pid,
                    name=str(entry.szExeFile),
                    executable=executable,
                    elevated=elevated,
                )
            )
            success = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return tuple(entries)


def _query_process_details(kernel32, advapi32, pid: int) -> tuple[Optional[Path], Optional[bool]]:
    process = kernel32.OpenProcess(0x1000, False, pid)
    if not process:
        return None, None
    token = wintypes.HANDLE()
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        executable = None
        if kernel32.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(size)):
            executable = Path(buffer.value)

        elevated = None
        if advapi32.OpenProcessToken(process, 0x0008, ctypes.byref(token)):
            value = wintypes.DWORD()
            returned = wintypes.DWORD()
            if advapi32.GetTokenInformation(
                token,
                20,
                ctypes.byref(value),
                ctypes.sizeof(value),
                ctypes.byref(returned),
            ):
                elevated = bool(value.value)
        return executable, elevated
    finally:
        if token:
            kernel32.CloseHandle(token)
        kernel32.CloseHandle(process)
