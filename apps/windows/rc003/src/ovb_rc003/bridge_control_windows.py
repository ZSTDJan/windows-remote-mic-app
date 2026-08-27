"""Requests a graceful exit from the running bridge notification-area window."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import sys
import time
from typing import Callable, Optional

from . import bridge_tray_windows, single_instance


DEFAULT_EXIT_TIMEOUT_SECONDS = 5.0
DEFAULT_POLL_INTERVAL_SECONDS = 0.1


@dataclass(frozen=True)
class BridgeExitResult:
    requested: bool
    stopped: bool
    error: str = ""


def _find_bridge_window() -> int:
    if sys.platform != "win32":
        return 0
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.FindWindowW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR)
    user32.FindWindowW.restype = wintypes.HWND
    hwnd = user32.FindWindowW(None, bridge_tray_windows.BRIDGE_TRAY_WINDOW_TITLE)
    return int(hwnd) if hwnd else 0


def _post_exit_command(hwnd: int) -> bool:
    if sys.platform != "win32":
        return False
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.PostMessageW.argtypes = (
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    )
    user32.PostMessageW.restype = wintypes.BOOL
    return bool(
        user32.PostMessageW(
            hwnd,
            bridge_tray_windows.WM_COMMAND,
            bridge_tray_windows.MENU_EXIT_BRIDGE,
            0,
        )
    )


def request_bridge_exit(
    *,
    timeout: float = DEFAULT_EXIT_TIMEOUT_SECONDS,
    poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
    platform: Optional[str] = None,
    find_window: Callable[[], int] = _find_bridge_window,
    post_exit_command: Callable[[int], bool] = _post_exit_command,
    bridge_running: Callable[[], bool] = single_instance.bridge_instance_running,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> BridgeExitResult:
    """Ask the existing bridge to use its normal tray cleanup path and wait."""

    current_platform = sys.platform if platform is None else platform
    if current_platform != "win32":
        return BridgeExitResult(False, False, "仅 Windows 支持自动停止遥控器服务。")
    try:
        if not bridge_running():
            return BridgeExitResult(False, True)
    except Exception:
        return BridgeExitResult(False, False, "无法确认遥控器服务是否正在运行。")

    try:
        hwnd = int(find_window())
    except Exception:
        hwnd = 0
    if not hwnd:
        return BridgeExitResult(
            False,
            False,
            "没有找到遥控器服务的通知区域控制入口，请从通知区域手动退出后再测试。",
        )
    try:
        requested = bool(post_exit_command(hwnd))
    except Exception:
        requested = False
    if not requested:
        return BridgeExitResult(
            False,
            False,
            "无法请求遥控器服务退出；若服务以管理员权限运行，请从通知区域手动退出。",
        )

    deadline = monotonic() + max(0.1, float(timeout))
    while monotonic() < deadline:
        try:
            if not bridge_running():
                return BridgeExitResult(True, True)
        except Exception:
            return BridgeExitResult(
                True,
                False,
                "已请求退出，但无法确认遥控器服务是否已经停止。",
            )
        sleep(max(0.01, float(poll_interval)))
    try:
        if not bridge_running():
            return BridgeExitResult(True, True)
    except Exception:
        return BridgeExitResult(
            True,
            False,
            "已请求退出，但无法确认遥控器服务是否已经停止。",
        )
    return BridgeExitResult(
        True,
        False,
        "遥控器服务没有在限定时间内停止，本次未开始声音通道测试。",
    )
