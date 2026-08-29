"""Optional launcher for third-party voice-input programs.

The bridge does not depend on any provider. A configured provider may be
discovered and started on explicit request or at bridge startup, but every
failure is reported as a provider status rather than a bridge startup error.
"""

from __future__ import annotations

import base64
import ctypes
import os
import re
import subprocess
import sys
import uuid
from ctypes import wintypes
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional, Sequence

VOICE_PROGRAM_NONE = "none"
VOICE_PROGRAM_SOGOU = "sogou"
VOICE_PROGRAM_WETYPE = "wetype"
VOICE_PROGRAM_WINDOWS_DICTATION = "windows_dictation"
VOICE_PROGRAM_CUSTOM = "custom"

VOICE_PROGRAM_PROVIDER_ORDER = (
    VOICE_PROGRAM_NONE,
    VOICE_PROGRAM_SOGOU,
    VOICE_PROGRAM_WETYPE,
    VOICE_PROGRAM_WINDOWS_DICTATION,
    VOICE_PROGRAM_CUSTOM,
)

VOICE_PROGRAM_PROVIDER_NAMES = {
    VOICE_PROGRAM_NONE: "不管理",
    VOICE_PROGRAM_SOGOU: "搜狗语音输入",
    VOICE_PROGRAM_WETYPE: "微信输入法",
    VOICE_PROGRAM_WINDOWS_DICTATION: "Windows 语音输入（Win+H）",
    VOICE_PROGRAM_CUSTOM: "自定义程序",
}

_SOGOU_PROCESS_NAME = "sogou_voice_assistant.exe"
_SOGOU_RUN_VALUE_NAMES = ("搜狗语音输入法",)
_SOGOU_UNINSTALL_SUBKEY = "Sogou Input"
_SOGOU_TOOLBOX_PROCESS_NAME = "SOGOUSmartAssistant.exe"
_SOGOU_TOOLBOX_ARGUMENTS = "--from=menutool"
_WETYPE_SERVER_NAME = "wetype_server.exe"
_WETYPE_PROCESS_NAMES = (_WETYPE_SERVER_NAME, "wetype_service.exe")
_WETYPE_SETTINGS_EXE = "wetype_update.exe"
_WETYPE_SETTINGS_ARGUMENTS = "-showsetting"
_WINDOWS_SPEECH_SETTINGS_URI = "ms-settings:speech"
_SYSTEM_MANAGED_PROVIDERS = frozenset(
    {VOICE_PROGRAM_WETYPE, VOICE_PROGRAM_WINDOWS_DICTATION}
)
_LAUNCH_ELEVATED_DEFAULTS = {
    VOICE_PROGRAM_SOGOU: True,
    VOICE_PROGRAM_CUSTOM: False,
}
_ALLOWED_EXECUTABLE_SUFFIXES = frozenset({".exe", ".lnk"})
_ERROR_CANCELLED = 1223
_COINIT_APARTMENTTHREADED = 0x2
_CLSCTX_INPROC_SERVER = 0x1
_RPC_E_CHANGED_MODE = ctypes.c_int32(0x80010106).value
_SLGP_RAWPATH = 0x4
_STGM_READ = 0

_SOGOU_SETTINGS_AUTOMATION_SCRIPT = r"""
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type @'
using System;
using System.Text;
using System.Runtime.InteropServices;

public static class RemoteMicSogouSettingsNative {
    public delegate bool EnumWindowsProc(IntPtr hwnd, IntPtr lparam);

    [StructLayout(LayoutKind.Sequential)]
    public struct Point {
        public int X;
        public int Y;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct Rect {
        public int Left;
        public int Top;
        public int Right;
        public int Bottom;
    }

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    public static extern IntPtr FindWindow(string className, string windowName);

    [DllImport("user32.dll")]
    public static extern bool EnumWindows(EnumWindowsProc callback, IntPtr lparam);

    [DllImport("user32.dll")]
    public static extern bool IsWindowVisible(IntPtr hwnd);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    public static extern int GetClassName(IntPtr hwnd, StringBuilder text, int size);

    [DllImport("user32.dll")]
    public static extern bool GetWindowRect(IntPtr hwnd, out Rect rect);

    [DllImport("user32.dll")]
    public static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint processId);

    [DllImport("user32.dll")]
    public static extern bool ShowWindowAsync(IntPtr hwnd, int command);

    [DllImport("user32.dll")]
    public static extern bool SetForegroundWindow(IntPtr hwnd);

    [DllImport("user32.dll")]
    public static extern bool GetCursorPos(out Point point);

    [DllImport("user32.dll")]
    public static extern bool SetCursorPos(int x, int y);

    [DllImport("user32.dll")]
    public static extern void mouse_event(
        uint flags,
        uint dx,
        uint dy,
        uint data,
        UIntPtr extra
    );

    [DllImport("user32.dll")]
    public static extern void keybd_event(byte key, byte scan, uint flags, UIntPtr extra);
}
'@

function Show-ExistingSettingsWindow {
    $window = [RemoteMicSogouSettingsNative]::FindWindow(
        "Chrome_WidgetWin_1",
        "搜狗语音输入法-设置"
    )
    if ($window -eq [IntPtr]::Zero) {
        return $false
    }
    [void][RemoteMicSogouSettingsNative]::ShowWindowAsync($window, 9)
    [void][RemoteMicSogouSettingsNative]::SetForegroundWindow($window)
    return $true
}

function Find-SogouTrayButton {
    $taskbar = [RemoteMicSogouSettingsNative]::FindWindow("Shell_TrayWnd", $null)
    if ($taskbar -eq [IntPtr]::Zero) {
        return $null
    }
    $root = [System.Windows.Automation.AutomationElement]::FromHandle($taskbar)
    $items = $root.FindAll(
        [System.Windows.Automation.TreeScope]::Descendants,
        [System.Windows.Automation.Condition]::TrueCondition
    )
    for ($index = 0; $index -lt $items.Count; $index++) {
        $item = $items.Item($index)
        try {
            $name = $item.Current.Name.Trim()
            $className = $item.Current.ClassName
        } catch {
            continue
        }
        if ($className -eq "SystemTray.NormalButton" -and
                $name.StartsWith("搜狗语音输入法")) {
            return $item
        }
    }
    return $null
}

function Open-SogouTrayMenu {
    param($trayButton)
    try {
        $bounds = $trayButton.Current.BoundingRectangle
    } catch {
        return $false
    }
    if ($bounds.Width -le 0 -or $bounds.Height -le 0) {
        return $false
    }

    $original = New-Object RemoteMicSogouSettingsNative+Point
    if (-not [RemoteMicSogouSettingsNative]::GetCursorPos([ref]$original)) {
        return $false
    }
    $slotWidth = [Math]::Min(
        $bounds.Width,
        [Math]::Max(24, [Math]::Round($bounds.Height * 2 / 3))
    )
    $x = [int]($bounds.Left + ($slotWidth / 2))
    $y = [int]($bounds.Top + ($bounds.Height / 2))
    if (-not [RemoteMicSogouSettingsNative]::SetCursorPos($x, $y)) {
        return $false
    }
    $script:trayMenuOriginalCursor = $original
    $script:trayMenuClickX = $x
    $script:trayMenuClickY = $y
    Start-Sleep -Milliseconds 100
    [RemoteMicSogouSettingsNative]::mouse_event(
        0x0008,
        0,
        0,
        0,
        [UIntPtr]::Zero
    )
    [RemoteMicSogouSettingsNative]::mouse_event(
        0x0010,
        0,
        0,
        0,
        [UIntPtr]::Zero
    )
    return $true
}

function Restore-CursorAfterTrayMenu {
    if ($null -eq $script:trayMenuOriginalCursor) {
        return
    }
    $current = New-Object RemoteMicSogouSettingsNative+Point
    if ([RemoteMicSogouSettingsNative]::GetCursorPos([ref]$current) -and
            [Math]::Abs($current.X - $script:trayMenuClickX) -le 8 -and
            [Math]::Abs($current.Y - $script:trayMenuClickY) -le 8) {
        [void][RemoteMicSogouSettingsNative]::SetCursorPos(
            $script:trayMenuOriginalCursor.X,
            $script:trayMenuOriginalCursor.Y
        )
    }
    $script:trayMenuOriginalCursor = $null
}

function Find-SettingsMenuItem {
    $script:settingsMenuResult = $null
    $script:sogouProcessIds = @(
        Get-Process -Name "sogou_voice_assistant" -ErrorAction SilentlyContinue |
            ForEach-Object { $_.Id }
    )
    [RemoteMicSogouSettingsNative]::EnumWindows({
        param($window, $unused)
        if (-not [RemoteMicSogouSettingsNative]::IsWindowVisible($window)) {
            return $true
        }
        [uint32]$processId = 0
        [void][RemoteMicSogouSettingsNative]::GetWindowThreadProcessId(
            $window,
            [ref]$processId
        )
        if ($script:sogouProcessIds -notcontains [int]$processId) {
            return $true
        }
        $className = New-Object System.Text.StringBuilder 128
        [void][RemoteMicSogouSettingsNative]::GetClassName(
            $window,
            $className,
            $className.Capacity
        )
        $rect = New-Object RemoteMicSogouSettingsNative+Rect
        [void][RemoteMicSogouSettingsNative]::GetWindowRect($window, [ref]$rect)
        if (($rect.Right - $rect.Left) -gt 800 -or
                ($rect.Bottom - $rect.Top) -gt 1000) {
            return $true
        }
        try {
            $root = [System.Windows.Automation.AutomationElement]::FromHandle($window)
            $nameCondition = New-Object System.Windows.Automation.PropertyCondition(
                [System.Windows.Automation.AutomationElement]::NameProperty,
                "设置"
            )
            $candidate = $root.FindFirst(
                [System.Windows.Automation.TreeScope]::Subtree,
                $nameCondition
            )
            if ($null -ne $candidate) {
                $script:settingsMenuResult = $candidate
                return $false
            }
        } catch {
        }
        return $true
    }, [IntPtr]::Zero) | Out-Null
    return $script:settingsMenuResult
}

function Invoke-SettingsMenuItem {
    param($settingsItem)
    try {
        $invoke = $settingsItem.GetCurrentPattern(
            [System.Windows.Automation.InvokePattern]::Pattern
        )
        $invoke.Invoke()
        return $true
    } catch {
    }
    try {
        $legacy = $settingsItem.GetCurrentPattern(
            [System.Windows.Automation.LegacyIAccessiblePattern]::Pattern
        )
        $legacy.DoDefaultAction()
        return $true
    } catch {
    }
    return $false
}

if (Show-ExistingSettingsWindow) {
    Write-Output "opened"
    exit 0
}

$trayButton = $null
for ($attempt = 0; $attempt -lt 12 -and $null -eq $trayButton; $attempt++) {
    $trayButton = Find-SogouTrayButton
    if ($null -eq $trayButton) {
        Start-Sleep -Milliseconds 350
    }
}
if ($null -eq $trayButton) {
    Write-Output "tray-not-found"
    exit 11
}

$settingsItem = $null
for ($attempt = 0; $attempt -lt 3 -and $null -eq $settingsItem; $attempt++) {
    [RemoteMicSogouSettingsNative]::keybd_event(27, 0, 0, [UIntPtr]::Zero)
    [RemoteMicSogouSettingsNative]::keybd_event(27, 0, 2, [UIntPtr]::Zero)
    if (-not (Open-SogouTrayMenu $trayButton)) {
        continue
    }
    Start-Sleep -Milliseconds 650
    $settingsItem = Find-SettingsMenuItem
    if ($null -eq $settingsItem) {
        Restore-CursorAfterTrayMenu
    }
}
if ($null -eq $settingsItem) {
    Write-Output "tray-menu-open-failed"
    exit 12
}

if (-not (Invoke-SettingsMenuItem $settingsItem)) {
    Restore-CursorAfterTrayMenu
    Write-Output "settings-menu-invoke-failed"
    exit 13
}
Restore-CursorAfterTrayMenu

for ($attempt = 0; $attempt -lt 40; $attempt++) {
    Start-Sleep -Milliseconds 150
    if (Show-ExistingSettingsWindow) {
        Write-Output "opened"
        exit 0
    }
}
Write-Output "settings-window-not-found"
exit 14
"""


class _Guid(ctypes.Structure):
    _fields_ = (
        ("data1", wintypes.DWORD),
        ("data2", wintypes.WORD),
        ("data3", wintypes.WORD),
        ("data4", ctypes.c_ubyte * 8),
    )


def _guid(value: str) -> _Guid:
    parsed = uuid.UUID(value)
    return _Guid(
        parsed.time_low,
        parsed.time_mid,
        parsed.time_hi_version,
        (ctypes.c_ubyte * 8)(*parsed.bytes[8:]),
    )


_CLSID_SHELL_LINK = _guid("00021401-0000-0000-c000-000000000046")
_IID_ISHELL_LINK_W = _guid("000214f9-0000-0000-c000-000000000046")
_IID_IPERSIST_FILE = _guid("0000010b-0000-0000-c000-000000000046")


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
    match_executable: Optional[Path] = None

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


@dataclass(frozen=True)
class VoiceProgramSettingsTarget:
    provider_id: str
    display_name: str
    kind: str
    target: str = ""
    arguments: str = ""

    @property
    def available(self) -> bool:
        return bool(self.target)


def normalize_voice_program_settings(raw: object) -> dict[str, object]:
    """Return the stable persisted shape for optional provider management."""

    data = raw if isinstance(raw, Mapping) else {}
    provider_id = str(data.get("provider", VOICE_PROGRAM_NONE)).strip().lower()
    if provider_id not in VOICE_PROGRAM_PROVIDER_ORDER:
        provider_id = VOICE_PROGRAM_NONE
    executable = str(data.get("custom_executable", "")).strip()
    enabled = provider_id != VOICE_PROGRAM_NONE
    raw_elevation_preferences = data.get("launch_elevated_by_provider")
    if isinstance(raw_elevation_preferences, Mapping):
        elevation_preferences = dict(_LAUNCH_ELEVATED_DEFAULTS)
        for candidate_provider in _LAUNCH_ELEVATED_DEFAULTS:
            if candidate_provider in raw_elevation_preferences:
                elevation_preferences[candidate_provider] = (
                    raw_elevation_preferences.get(candidate_provider) is True
                )
    elif "launch_elevated" in data:
        # Schema 7 and older stored one shared switch. Preserve that exact
        # choice for both launchable providers instead of applying a new
        # default over an existing user's configuration.
        legacy_elevated = data.get("launch_elevated") is True
        elevation_preferences = {
            candidate_provider: legacy_elevated
            for candidate_provider in _LAUNCH_ELEVATED_DEFAULTS
        }
    else:
        elevation_preferences = dict(_LAUNCH_ELEVATED_DEFAULTS)

    if provider_id in elevation_preferences:
        current_elevated = elevation_preferences[provider_id]
    else:
        # System-managed/disabled selections have no applicable switch. Keep
        # the compatibility mirror so switching away and back does not erase
        # the most recently selected launchable provider's preference.
        current_elevated = data.get("launch_elevated") is True
    return {
        "provider": provider_id,
        "custom_executable": executable,
        "launch_on_bridge_start": (
            enabled
            and not is_system_managed_provider(provider_id)
            and data.get("launch_on_bridge_start") is True
        ),
        "launch_elevated": current_elevated,
        "launch_elevated_by_provider": elevation_preferences,
    }


def is_system_managed_provider(provider_id: object) -> bool:
    return str(provider_id).strip().lower() in _SYSTEM_MANAGED_PROVIDERS


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
    if status.provider_id == VOICE_PROGRAM_WINDOWS_DICTATION:
        return "Windows 内置语音输入；按 Win+H 打开，由系统管理。"
    if status.provider_id == VOICE_PROGRAM_WETYPE:
        if status.code == "not_found":
            return "未找到微信输入法；正常安装后会自动识别，无需手选路径。"
        if status.code == "stopped":
            return "已找到微信输入法；由 Windows 管理，当前未检测到后台进程。"
        if status.code == "running":
            return "微信输入法已安装并正在运行（由 Windows 管理）。"
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
        "system_managed": "该语音程序由 Windows 管理，Remote Mic 不单独启动它。",
    }
    return messages.get(result.code, "语音程序状态未知。")


def resolve_voice_program(
    settings: Mapping[str, object],
    *,
    platform: Optional[str] = None,
    process_iter: Optional[Callable[[], Iterable[ProcessInfo]]] = None,
    run_value_reader: Optional[Callable[[], Iterable[str]]] = None,
    wetype_install_value_reader: Optional[Callable[[], Iterable[str]]] = None,
    wetype_shortcut_iter: Optional[Callable[[], Iterable[Path]]] = None,
    shortcut_resolver: Optional[Callable[[Path], Optional[Path]]] = None,
) -> ResolvedVoiceProgram:
    normalized = normalize_voice_program_settings(settings)
    provider_id = str(normalized["provider"])
    display_name = VOICE_PROGRAM_PROVIDER_NAMES[provider_id]
    configured_path = _validated_configured_path(normalized["custom_executable"])

    if provider_id == VOICE_PROGRAM_NONE:
        return ResolvedVoiceProgram(provider_id, display_name, None, (), "disabled")
    if provider_id == VOICE_PROGRAM_WINDOWS_DICTATION:
        return ResolvedVoiceProgram(provider_id, display_name, None, (), "system")
    if provider_id == VOICE_PROGRAM_CUSTOM:
        match_executable = configured_path
        if configured_path is not None and configured_path.suffix.casefold() == ".lnk":
            match_executable = (shortcut_resolver or _resolve_shortcut_target)(
                configured_path
            )
            if match_executable is None:
                return ResolvedVoiceProgram(
                    provider_id, display_name, None, (), "shortcut_unresolved"
                )
        process_names = (
            (match_executable.name.casefold(),)
            if match_executable is not None
            else ()
        )
        return ResolvedVoiceProgram(
            provider_id,
            display_name,
            configured_path,
            process_names,
            "configured" if configured_path is not None else "missing",
            match_executable,
        )

    if provider_id == VOICE_PROGRAM_WETYPE:
        executable = discover_wetype_executable(
            platform=platform,
            process_iter=process_iter,
            install_value_reader=wetype_install_value_reader,
            shortcut_iter=wetype_shortcut_iter,
            shortcut_resolver=shortcut_resolver,
        )
        return ResolvedVoiceProgram(
            provider_id,
            display_name,
            executable,
            _WETYPE_PROCESS_NAMES,
            "discovered" if executable is not None else "missing",
            executable,
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
        executable,
    )


def resolve_voice_program_settings_target(
    settings: Mapping[str, object],
    *,
    platform: Optional[str] = None,
    process_iter: Optional[Callable[[], Iterable[ProcessInfo]]] = None,
    run_value_reader: Optional[Callable[[], Iterable[str]]] = None,
    sogou_install_value_reader: Optional[Callable[[], Iterable[str]]] = None,
    wetype_install_value_reader: Optional[Callable[[], Iterable[str]]] = None,
    wetype_shortcut_iter: Optional[Callable[[], Iterable[Path]]] = None,
    shortcut_resolver: Optional[Callable[[Path], Optional[Path]]] = None,
) -> VoiceProgramSettingsTarget:
    """Resolve the provider-owned settings entry without opening it."""

    normalized = normalize_voice_program_settings(settings)
    provider_id = str(normalized["provider"])
    display_name = VOICE_PROGRAM_PROVIDER_NAMES[provider_id]
    current_platform = sys.platform if platform is None else platform
    if current_platform != "win32":
        return VoiceProgramSettingsTarget(
            provider_id, display_name, "unsupported"
        )
    if provider_id == VOICE_PROGRAM_SOGOU:
        processes = tuple((process_iter or _iter_windows_processes)())
        process_snapshot = lambda: processes
        executable = discover_sogou_voice_executable(
            platform=current_platform,
            process_iter=process_snapshot,
            run_value_reader=run_value_reader,
        )
        if executable is not None:
            return VoiceProgramSettingsTarget(
                provider_id,
                display_name,
                "sogou_tray",
                str(executable),
            )
        toolbox = discover_sogou_ai_toolbox_executable(
            platform=current_platform,
            process_iter=process_snapshot,
            run_value_reader=run_value_reader,
            install_value_reader=sogou_install_value_reader,
        )
        if toolbox is not None:
            return VoiceProgramSettingsTarget(
                provider_id,
                display_name,
                "sogou_toolbox",
                str(toolbox),
                _SOGOU_TOOLBOX_ARGUMENTS,
            )
        return VoiceProgramSettingsTarget(
            provider_id, display_name, "missing"
        )
    if provider_id == VOICE_PROGRAM_WINDOWS_DICTATION:
        return VoiceProgramSettingsTarget(
            provider_id,
            display_name,
            "uri",
            _WINDOWS_SPEECH_SETTINGS_URI,
        )
    if provider_id == VOICE_PROGRAM_WETYPE:
        resolved = resolve_voice_program(
            normalized,
            platform=current_platform,
            process_iter=process_iter,
            wetype_install_value_reader=wetype_install_value_reader,
            wetype_shortcut_iter=wetype_shortcut_iter,
            shortcut_resolver=shortcut_resolver,
        )
        executable = resolved.executable
        settings_executable = (
            executable.parent / _WETYPE_SETTINGS_EXE
            if executable is not None
            else None
        )
        if settings_executable is not None and settings_executable.is_file():
            return VoiceProgramSettingsTarget(
                provider_id,
                display_name,
                "executable",
                str(settings_executable),
                _WETYPE_SETTINGS_ARGUMENTS,
            )
        return VoiceProgramSettingsTarget(provider_id, display_name, "missing")
    return VoiceProgramSettingsTarget(provider_id, display_name, "unsupported")


def inspect_voice_program(
    settings: Mapping[str, object],
    *,
    platform: Optional[str] = None,
    process_iter: Optional[Callable[[], Iterable[ProcessInfo]]] = None,
    run_value_reader: Optional[Callable[[], Iterable[str]]] = None,
    wetype_install_value_reader: Optional[Callable[[], Iterable[str]]] = None,
    wetype_shortcut_iter: Optional[Callable[[], Iterable[Path]]] = None,
    shortcut_resolver: Optional[Callable[[Path], Optional[Path]]] = None,
) -> VoiceProgramStatus:
    resolved = resolve_voice_program(
        settings,
        platform=platform,
        process_iter=process_iter,
        run_value_reader=run_value_reader,
        wetype_install_value_reader=wetype_install_value_reader,
        wetype_shortcut_iter=wetype_shortcut_iter,
        shortcut_resolver=shortcut_resolver,
    )
    if resolved.provider_id == VOICE_PROGRAM_WINDOWS_DICTATION:
        return VoiceProgramStatus(
            resolved.provider_id,
            resolved.display_name,
            True,
            False,
            None,
            None,
            "stopped",
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
    wetype_install_value_reader: Optional[Callable[[], Iterable[str]]] = None,
    wetype_shortcut_iter: Optional[Callable[[], Iterable[Path]]] = None,
    start_file: Optional[Callable[[str, str, str], None]] = None,
    shortcut_resolver: Optional[Callable[[Path], Optional[Path]]] = None,
) -> VoiceProgramLaunchResult:
    """Start the configured provider without making it a bridge dependency."""

    normalized = normalize_voice_program_settings(settings)
    resolved = resolve_voice_program(
        normalized,
        platform=platform,
        process_iter=process_iter,
        run_value_reader=run_value_reader,
        wetype_install_value_reader=wetype_install_value_reader,
        wetype_shortcut_iter=wetype_shortcut_iter,
        shortcut_resolver=shortcut_resolver,
    )
    if resolved.provider_id == VOICE_PROGRAM_NONE:
        return VoiceProgramLaunchResult(resolved.provider_id, False, False, "disabled")
    if resolved.provider_id == VOICE_PROGRAM_WINDOWS_DICTATION:
        return VoiceProgramLaunchResult(
            resolved.provider_id, False, False, "system_managed"
        )
    if resolved.executable is None:
        return VoiceProgramLaunchResult(resolved.provider_id, False, False, "not_found")

    processes = list((process_iter or _iter_windows_processes)())
    matches = _matching_processes(resolved, processes)
    if is_system_managed_provider(resolved.provider_id):
        return VoiceProgramLaunchResult(
            resolved.provider_id,
            False,
            bool(matches),
            "system_managed",
            elevated=_combined_elevation(matches),
        )
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


def open_voice_program_settings(
    executable: Path,
    arguments: str,
    *,
    platform: Optional[str] = None,
    start_file: Optional[Callable[[str, str, str, str], None]] = None,
) -> None:
    """Open a provider-owned settings executable with explicit arguments."""

    current_platform = platform or sys.platform
    if current_platform != "win32":
        raise OSError("voice program settings require Windows")
    path = Path(executable)
    launcher = start_file or _default_start_file_with_arguments
    launcher(str(path), "open", str(arguments), str(path.parent))


def open_sogou_voice_settings(
    executable: Path,
    *,
    launch_elevated: bool,
    platform: Optional[str] = None,
    process_iter: Optional[Callable[[], Iterable[ProcessInfo]]] = None,
    start_file: Optional[Callable[[str, str, str], None]] = None,
    automation_opener: Optional[Callable[[], None]] = None,
) -> None:
    """Open Sogou Voice's own settings through its tray-owned menu."""

    current_platform = platform or sys.platform
    if current_platform != "win32":
        raise OSError("Sogou voice settings require Windows")
    path = Path(executable)
    running = any(
        process.name.casefold() == _SOGOU_PROCESS_NAME
        for process in (process_iter or _iter_windows_processes)()
    )
    if not running:
        operation = "runas" if launch_elevated else "open"
        launcher = start_file or _default_start_file
        launcher(str(path), operation, str(path.parent))
    (automation_opener or _run_sogou_settings_automation)()


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


def discover_sogou_ai_toolbox_executable(
    *,
    platform: Optional[str] = None,
    process_iter: Optional[Callable[[], Iterable[ProcessInfo]]] = None,
    run_value_reader: Optional[Callable[[], Iterable[str]]] = None,
    install_value_reader: Optional[Callable[[], Iterable[str]]] = None,
) -> Optional[Path]:
    current_platform = sys.platform if platform is None else platform
    if current_platform != "win32":
        return None

    processes = tuple((process_iter or _iter_windows_processes)())
    for process in processes:
        if (
            process.name.casefold() == _SOGOU_TOOLBOX_PROCESS_NAME.casefold()
            and process.executable is not None
            and process.executable.is_file()
        ):
            return process.executable

    component_dirs: list[Path] = []
    for process in processes:
        executable = process.executable
        if executable is None:
            continue
        for parent in executable.parents:
            if parent.name.casefold() == "components":
                component_dirs.append(parent)
                break
        for parent in executable.parents[:4]:
            candidate = parent / "Components"
            if candidate.is_dir():
                component_dirs.append(candidate)

    for command in (run_value_reader or _read_sogou_run_values)():
        manager_path = _command_executable(command)
        if manager_path is not None:
            component_dirs.append(manager_path.parent)

    for raw_value in (install_value_reader or _read_sogou_install_values)():
        text = os.path.expandvars(str(raw_value).strip())
        if not text:
            continue
        path = (
            _command_executable(text)
            if ".exe" in text.casefold()
            else Path(text.strip('"'))
        )
        if path is None:
            continue
        root = path.parent if path.suffix else path
        for parent in (root, *root.parents[:3]):
            candidate = parent / "Components"
            if candidate.is_dir():
                component_dirs.append(candidate)

    candidates: list[Path] = []
    for components_dir in dict.fromkeys(component_dirs):
        candidates.extend(
            components_dir.glob(
                f"IChat/*/{_SOGOU_TOOLBOX_PROCESS_NAME}"
            )
        )
    existing = [path for path in dict.fromkeys(candidates) if path.is_file()]
    if not existing:
        return None
    return max(existing, key=_sogou_toolbox_version_key)


def discover_wetype_executable(
    *,
    platform: Optional[str] = None,
    process_iter: Optional[Callable[[], Iterable[ProcessInfo]]] = None,
    install_value_reader: Optional[Callable[[], Iterable[str]]] = None,
    shortcut_iter: Optional[Callable[[], Iterable[Path]]] = None,
    shortcut_resolver: Optional[Callable[[Path], Optional[Path]]] = None,
) -> Optional[Path]:
    current_platform = sys.platform if platform is None else platform
    if current_platform != "win32":
        return None

    for process in (process_iter or _iter_windows_processes)():
        if (
            process.name.casefold() == _WETYPE_SERVER_NAME
            and process.executable is not None
            and process.executable.is_file()
        ):
            return process.executable

    candidates: list[Path] = []
    for raw_value in (install_value_reader or _read_wetype_install_values)():
        candidates.extend(_wetype_candidates_from_value(raw_value))

    resolver = shortcut_resolver or _resolve_shortcut_target
    for shortcut in (shortcut_iter or _iter_wetype_shortcuts)():
        target = resolver(shortcut)
        if target is None:
            continue
        candidates.extend(_wetype_candidates_from_path(target.parent))
        candidates.extend(_wetype_candidates_from_path(target.parent.parent))

    for root in _wetype_default_roots():
        candidates.extend(_wetype_candidates_from_path(root))

    existing = list(dict.fromkeys(path for path in candidates if path.is_file()))
    if not existing:
        return None
    return max(existing, key=_wetype_version_key)


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


def _sogou_toolbox_version_key(path: Path) -> tuple[int, ...]:
    numbers = tuple(int(item) for item in re.findall(r"\d+", path.parent.name))
    return numbers or (0,)


def _wetype_candidates_from_value(raw: object) -> list[Path]:
    text = os.path.expandvars(str(raw).strip())
    if not text:
        return []
    if ".exe" in text.casefold():
        candidate = _command_executable(text)
        if candidate is None:
            return []
        return _wetype_candidates_from_path(candidate)
    return _wetype_candidates_from_path(Path(text.strip('"')))


def _wetype_candidates_from_path(path: Path) -> list[Path]:
    if path.name.casefold() == _WETYPE_SERVER_NAME:
        return [path]
    if path.suffix:
        path = path.parent
    candidates = [path / _WETYPE_SERVER_NAME]
    try:
        candidates.extend(path.glob(f"*/{_WETYPE_SERVER_NAME}"))
    except OSError:
        pass
    return candidates


def _wetype_version_key(path: Path) -> tuple[int, ...]:
    for part in (path.parent.name, path.parent.parent.name):
        numbers = tuple(int(item) for item in re.findall(r"\d+", part))
        if numbers:
            return numbers
    return (0,)


def _matching_processes(
    resolved: ResolvedVoiceProgram, processes: Sequence[ProcessInfo]
) -> list[ProcessInfo]:
    match_executable = resolved.match_executable or resolved.executable
    expected_path = (
        _normalized_executable_path(match_executable) if match_executable else ""
    )
    expected_names = {name.casefold() for name in resolved.process_names}
    matches: list[ProcessInfo] = []
    for process in processes:
        if process.executable is not None and expected_path:
            if _normalized_executable_path(process.executable) == expected_path:
                matches.append(process)
            continue
        if process.name.casefold() in expected_names:
            matches.append(process)
    return matches


def _normalized_executable_path(path: Path) -> str:
    try:
        path = path.resolve(strict=False)
    except OSError:
        pass
    return os.path.normcase(os.path.normpath(str(path)))


def _com_method(interface, index: int, result_type, *argument_types):
    vtable = ctypes.cast(
        interface, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))
    ).contents
    return ctypes.WINFUNCTYPE(
        result_type, ctypes.c_void_p, *argument_types
    )(vtable[index])


def _release_com_interface(interface: ctypes.c_void_p) -> None:
    if not interface.value:
        return
    try:
        release = _com_method(interface, 2, wintypes.ULONG)
        release(interface)
    except Exception:
        pass


def _read_windows_shortcut_target(shortcut: Path) -> Optional[Path]:
    if sys.platform != "win32":
        return None

    ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    ole32.CoInitializeEx.restype = ctypes.c_long
    ole32.CoCreateInstance.argtypes = [
        ctypes.POINTER(_Guid),
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_Guid),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    ole32.CoCreateInstance.restype = ctypes.c_long
    ole32.CoUninitialize.argtypes = []
    ole32.CoUninitialize.restype = None

    shell_link = ctypes.c_void_p()
    persist_file = ctypes.c_void_p()
    should_uninitialize = False
    try:
        result = int(ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED))
        if result in (0, 1):
            should_uninitialize = True
        elif result != _RPC_E_CHANGED_MODE:
            return None

        result = int(
            ole32.CoCreateInstance(
                ctypes.byref(_CLSID_SHELL_LINK),
                None,
                _CLSCTX_INPROC_SERVER,
                ctypes.byref(_IID_ISHELL_LINK_W),
                ctypes.byref(shell_link),
            )
        )
        if result < 0 or not shell_link.value:
            return None

        query_interface = _com_method(
            shell_link,
            0,
            ctypes.c_long,
            ctypes.POINTER(_Guid),
            ctypes.POINTER(ctypes.c_void_p),
        )
        result = int(
            query_interface(
                shell_link,
                ctypes.byref(_IID_IPERSIST_FILE),
                ctypes.byref(persist_file),
            )
        )
        if result < 0 or not persist_file.value:
            return None

        load = _com_method(
            persist_file, 5, ctypes.c_long, wintypes.LPCWSTR, wintypes.DWORD
        )
        if int(load(persist_file, str(shortcut), _STGM_READ)) < 0:
            return None

        target_buffer = ctypes.create_unicode_buffer(32768)
        get_path = _com_method(
            shell_link,
            3,
            ctypes.c_long,
            wintypes.LPWSTR,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        )
        result = int(
            get_path(
                shell_link,
                target_buffer,
                len(target_buffer),
                None,
                _SLGP_RAWPATH,
            )
        )
        target_text = os.path.expandvars(target_buffer.value.strip())
        if result < 0 or not target_text:
            return None
        return Path(target_text)
    except Exception:
        return None
    finally:
        _release_com_interface(persist_file)
        _release_com_interface(shell_link)
        if should_uninitialize:
            ole32.CoUninitialize()


@lru_cache(maxsize=32)
def _resolve_shortcut_target_cached(
    shortcut_text: str, modified_ns: int, file_size: int
) -> Optional[Path]:
    del modified_ns, file_size
    target = _read_windows_shortcut_target(Path(shortcut_text))
    if target is None or target.suffix.casefold() != ".exe":
        return None
    try:
        return target.resolve(strict=True)
    except OSError:
        return None


def _resolve_shortcut_target(shortcut: Path) -> Optional[Path]:
    try:
        stat = shortcut.stat()
    except OSError:
        return None
    resolved = _resolve_shortcut_target_cached(
        str(shortcut), stat.st_mtime_ns, stat.st_size
    )
    if resolved is None:
        # A shortcut target can be restored without changing the .lnk file.
        # Do not make a transient failure last until the application restarts.
        _resolve_shortcut_target_cached.cache_clear()
    return resolved


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


def _read_sogou_install_values() -> Iterable[str]:
    if sys.platform != "win32":
        return ()
    try:
        import winreg

        subkey = (
            "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\"
            + _SOGOU_UNINSTALL_SUBKEY
        )
        values: list[str] = []
        views = tuple(
            dict.fromkeys(
                (
                    0,
                    getattr(winreg, "KEY_WOW64_64KEY", 0),
                    getattr(winreg, "KEY_WOW64_32KEY", 0),
                )
            )
        )
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for view in views:
                try:
                    with winreg.OpenKey(
                        hive,
                        subkey,
                        0,
                        winreg.KEY_READ | view,
                    ) as key:
                        for name in ("InstallLocation", "DisplayIcon"):
                            try:
                                value, _ = winreg.QueryValueEx(key, name)
                            except OSError:
                                continue
                            text = str(value).strip()
                            if text:
                                values.append(text)
                except OSError:
                    continue
        return tuple(dict.fromkeys(values))
    except OSError:
        return ()


def _read_wetype_install_values() -> Iterable[str]:
    if sys.platform != "win32":
        return ()
    try:
        import winreg

        subkey = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\WeType"
        values: list[str] = []
        views = tuple(
            dict.fromkeys(
                (
                    0,
                    getattr(winreg, "KEY_WOW64_64KEY", 0),
                    getattr(winreg, "KEY_WOW64_32KEY", 0),
                )
            )
        )
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for view in views:
                try:
                    with winreg.OpenKey(
                        hive,
                        subkey,
                        0,
                        winreg.KEY_READ | view,
                    ) as key:
                        for name in ("DisplayIcon", "InstallLocation"):
                            try:
                                value, _ = winreg.QueryValueEx(key, name)
                            except OSError:
                                continue
                            text = str(value).strip()
                            if text:
                                values.append(text)
                except OSError:
                    continue
        return tuple(dict.fromkeys(values))
    except OSError:
        return ()


def _iter_wetype_shortcuts() -> Iterable[Path]:
    candidates: list[Path] = []
    for variable, suffix in (
        ("APPDATA", Path("Microsoft", "Windows", "Start Menu", "Programs")),
        ("PROGRAMDATA", Path("Microsoft", "Windows", "Start Menu", "Programs")),
    ):
        root_text = os.environ.get(variable)
        if not root_text:
            continue
        root = Path(root_text) / suffix
        candidates.extend(
            (
                root / "微信输入法" / "微信输入法.lnk",
                root / "微信输入法.lnk",
                root / "WeType" / "WeType.lnk",
            )
        )
    return tuple(path for path in candidates if path.is_file())


def _wetype_default_roots() -> Iterable[Path]:
    roots: list[Path] = []
    for variable in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        root_text = os.environ.get(variable)
        if root_text:
            roots.append(Path(root_text) / "Tencent" / "WeType")
    return tuple(dict.fromkeys(roots))


def _default_start_file(path: str, operation: str, cwd: str) -> None:
    if sys.platform != "win32" or not hasattr(os, "startfile"):
        raise OSError("voice program launch requires Windows")
    os.startfile(path, operation, cwd=cwd)  # type: ignore[attr-defined,call-arg]


def _default_start_file_with_arguments(
    path: str,
    operation: str,
    arguments: str,
    cwd: str,
) -> None:
    if sys.platform != "win32" or not hasattr(os, "startfile"):
        raise OSError("voice program settings require Windows")
    os.startfile(  # type: ignore[attr-defined,call-arg]
        path,
        operation,
        arguments,
        cwd,
    )


def _run_sogou_settings_automation(
    *,
    runner: Optional[Callable[..., subprocess.CompletedProcess[str]]] = None,
) -> None:
    encoded_script = base64.b64encode(
        _SOGOU_SETTINGS_AUTOMATION_SCRIPT.encode("utf-16-le")
    ).decode("ascii")
    command = (
        "powershell.exe",
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-EncodedCommand",
        encoded_script,
    )
    actual_runner = runner or subprocess.run
    kwargs: dict[str, object] = {
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": 20,
        "check": False,
    }
    if actual_runner is subprocess.run and sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = actual_runner(command, **kwargs)
    except subprocess.TimeoutExpired as exc:
        raise OSError("等待搜狗语音设置窗口超时") from exc
    if completed.returncode == 0:
        return
    detail = str(completed.stdout or completed.stderr or "").strip()
    messages = {
        11: "未找到搜狗语音托盘图标",
        12: "无法打开搜狗语音托盘菜单",
        13: "无法调用搜狗语音托盘菜单中的设置项",
        14: "搜狗语音设置窗口没有出现",
    }
    message = messages.get(completed.returncode, "无法打开搜狗语音设置")
    if detail:
        message = f"{message}（{detail}）"
    raise OSError(message)


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
