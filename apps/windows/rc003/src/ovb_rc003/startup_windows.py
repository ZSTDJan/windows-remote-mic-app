"""User-controlled Windows login startup for the Remote Mic desktop shell."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from . import dev_session


RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE_NAME = "RemoteMicRC003"
BACKGROUND_START_FLAG = "--background"


@dataclass(frozen=True)
class StartupState:
    enabled: bool
    error: str = ""


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
