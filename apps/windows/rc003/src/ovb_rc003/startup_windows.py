"""User-controlled Windows login startup for the Remote Mic desktop shell."""

from __future__ import annotations

import subprocess
import sys
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Callable, Optional, Sequence

from . import dev_session


RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE_NAME = "RemoteMicRC003"
BACKGROUND_START_FLAG = "--background"


@dataclass(frozen=True)
class StartupState:
    enabled: bool
    error: str = ""


CommandLineParser = Callable[[str], Sequence[str]]


def build_startup_command(
    *,
    frozen: Optional[bool] = None,
    executable: Optional[str] = None,
    source_launcher: Optional[str] = None,
) -> list[str]:
    if frozen is None:
        frozen = bool(getattr(sys, "frozen", False))
    if executable is None:
        executable = sys.executable
    if not executable:
        raise ValueError("sys.executable is empty")
    if frozen:
        return dev_session.mark_command([executable, BACKGROUND_START_FLAG])
    launcher = source_launcher or str(
        Path(__file__).resolve().parents[1] / "launcher.py"
    )
    return dev_session.mark_command(
        [executable, launcher, BACKGROUND_START_FLAG]
    )


def command_line(command: Optional[Sequence[str]] = None) -> str:
    resolved = list(command) if command is not None else build_startup_command()
    if not resolved or not str(resolved[0]).strip():
        raise ValueError("startup command is empty")
    return subprocess.list2cmdline([str(part) for part in resolved])


def _load_winreg():
    import winreg

    return winreg


def _parse_windows_command_line(command: str) -> list[str]:
    """Parse one Windows command line with the system's argv rules."""

    import ctypes

    if not command or "\0" in command:
        raise ValueError("startup command is invalid")
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32.CommandLineToArgvW.argtypes = (
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_int),
    )
    shell32.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
    kernel32.LocalFree.restype = ctypes.c_void_p
    argument_count = ctypes.c_int(0)
    arguments = shell32.CommandLineToArgvW(
        command,
        ctypes.byref(argument_count),
    )
    if not arguments:
        raise OSError(ctypes.get_last_error(), "CommandLineToArgvW failed")
    try:
        return [str(arguments[index]) for index in range(argument_count.value)]
    finally:
        kernel32.LocalFree(arguments)


def _owned_frozen_startup_command(
    value: object,
    *,
    parser: CommandLineParser,
) -> bool:
    if not isinstance(value, str):
        return False
    try:
        arguments = [str(argument) for argument in parser(value)]
    except (OSError, ValueError, TypeError):
        return False
    if len(arguments) != 2 or arguments[1] != BACKGROUND_START_FLAG:
        return False
    executable = PureWindowsPath(arguments[0])
    if (
        not executable.is_absolute()
        or executable.name.casefold() != "remotemicrc003.exe"
    ):
        return False
    return command_line(arguments) == value


def rebind_owned_frozen_startup(
    *,
    platform: Optional[str] = None,
    frozen: Optional[bool] = None,
    executable: Optional[str] = None,
    current_command: Optional[str] = None,
    winreg_module=None,
    command_parser: Optional[CommandLineParser] = None,
) -> StartupState:
    """Move this product's existing login entry to the current frozen copy."""

    current_platform = sys.platform if platform is None else platform
    current_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else bool(frozen)
    if current_platform != "win32" or not current_frozen:
        return StartupState(False)
    current_executable = PureWindowsPath(executable or sys.executable)
    if (
        not current_executable.is_absolute()
        or current_executable.name.casefold() != "remotemicrc003.exe"
    ):
        return StartupState(False, "ValueError")
    expected = current_command or command_line(
        [str(current_executable), BACKGROUND_START_FLAG]
    )
    parser = command_parser or _parse_windows_command_line
    try:
        winreg = winreg_module or _load_winreg()
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            RUN_KEY_PATH,
            0,
            winreg.KEY_QUERY_VALUE,
        ) as key:
            value, value_type = winreg.QueryValueEx(key, RUN_VALUE_NAME)
        if value_type != winreg.REG_SZ:
            return StartupState(False)
        if str(value) == expected:
            return StartupState(True)
        if not _owned_frozen_startup_command(value, parser=parser):
            return StartupState(False)
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER,
            RUN_KEY_PATH,
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            winreg.SetValueEx(
                key,
                RUN_VALUE_NAME,
                0,
                winreg.REG_SZ,
                expected,
            )
        return StartupState(True)
    except FileNotFoundError:
        return StartupState(False)
    except (OSError, ValueError, TypeError) as exc:
        return StartupState(False, type(exc).__name__)


def read_startup_state(
    *,
    platform: Optional[str] = None,
    expected_command: Optional[str] = None,
    winreg_module=None,
) -> StartupState:
    current_platform = sys.platform if platform is None else platform
    if current_platform != "win32":
        return StartupState(False, "仅 Windows 支持随系统启动。")
    try:
        winreg = winreg_module or _load_winreg()
        expected = expected_command or command_line()
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            RUN_KEY_PATH,
            0,
            winreg.KEY_QUERY_VALUE,
        ) as key:
            value, value_type = winreg.QueryValueEx(key, RUN_VALUE_NAME)
    except FileNotFoundError:
        return StartupState(False)
    except (OSError, ValueError, TypeError) as exc:
        return StartupState(False, type(exc).__name__)
    enabled = value_type == winreg.REG_SZ and str(value) == expected
    return StartupState(enabled)


def set_startup_enabled(
    enabled: bool,
    *,
    platform: Optional[str] = None,
    startup_command: Optional[str] = None,
    winreg_module=None,
) -> StartupState:
    current_platform = sys.platform if platform is None else platform
    if current_platform != "win32":
        return StartupState(False, "仅 Windows 支持随系统启动。")
    try:
        winreg = winreg_module or _load_winreg()
        if enabled:
            value = startup_command or command_line()
            with winreg.CreateKeyEx(
                winreg.HKEY_CURRENT_USER,
                RUN_KEY_PATH,
                0,
                winreg.KEY_SET_VALUE,
            ) as key:
                winreg.SetValueEx(key, RUN_VALUE_NAME, 0, winreg.REG_SZ, value)
            return StartupState(True)

        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                RUN_KEY_PATH,
                0,
                winreg.KEY_SET_VALUE,
            ) as key:
                winreg.DeleteValue(key, RUN_VALUE_NAME)
        except FileNotFoundError:
            pass
        return StartupState(False)
    except (OSError, ValueError, TypeError) as exc:
        return StartupState(not bool(enabled), type(exc).__name__)
