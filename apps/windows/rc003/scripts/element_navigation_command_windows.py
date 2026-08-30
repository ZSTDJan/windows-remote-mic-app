"""Win32 command window shared by standalone and embedded navigation hosts."""

from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes
from typing import Callable, Optional


ELEMENT_NAVIGATION_WINDOW_CLASS = "ElementNavigation.Command.v1"
ELEMENT_NAVIGATION_WINDOW_TITLE = "Element Navigation Command"
ELEMENT_NAVIGATION_MESSAGE_NAME = "ElementNavigation.Command.v1"

ELEMENT_NAVIGATION_COMMAND_TOGGLE = 1
ELEMENT_NAVIGATION_COMMAND_QUIT = 2


def _require_windows() -> None:
    import sys

    if sys.platform != "win32":
        raise OSError("element navigation command channel is only available on Windows")


class ElementNavigationCommandServer:
    """Invisible Win32 window that forwards tiny commands to the Qt loop."""

    _WM_CLOSE = 0x0010
    _WM_DESTROY = 0x0002

    def __init__(self, callback: Callable[[int, int], None]) -> None:
        self._callback = callback
        self._ready = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="element-navigation-command-window",
            daemon=True,
        )
        self._hwnd = 0
        self._startup_error: Optional[BaseException] = None
        self._window_proc = None

    def start(self, timeout_seconds: float = 3.0) -> None:
        self._thread.start()
        if not self._ready.wait(timeout_seconds):
            raise RuntimeError("元素导航命令窗口启动超时")
        if self._startup_error is not None or self._hwnd <= 0:
            raise RuntimeError("元素导航命令窗口启动失败") from self._startup_error

    def stop(self) -> None:
        if self._hwnd > 0 and self._thread.is_alive():
            try:
                user32 = ctypes.WinDLL("user32", use_last_error=True)
                user32.PostMessageW.argtypes = (
                    wintypes.HWND,
                    wintypes.UINT,
                    wintypes.WPARAM,
                    wintypes.LPARAM,
                )
                user32.PostMessageW.restype = wintypes.BOOL
                user32.PostMessageW(self._hwnd, self._WM_CLOSE, 0, 0)
            except Exception:
                pass
        self._thread.join(timeout=2.0)

    def _run(self) -> None:
        atom = 0
        hinstance = 0
        user32 = None
        try:
            _require_windows()
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            lresult = ctypes.c_ssize_t
            window_proc_type = ctypes.WINFUNCTYPE(
                lresult,
                wintypes.HWND,
                wintypes.UINT,
                wintypes.WPARAM,
                wintypes.LPARAM,
            )

            class WindowClass(ctypes.Structure):
                _fields_ = [
                    ("cbSize", wintypes.UINT),
                    ("style", wintypes.UINT),
                    ("lpfnWndProc", window_proc_type),
                    ("cbClsExtra", ctypes.c_int),
                    ("cbWndExtra", ctypes.c_int),
                    ("hInstance", wintypes.HINSTANCE),
                    ("hIcon", wintypes.HICON),
                    ("hCursor", wintypes.HANDLE),
                    ("hbrBackground", wintypes.HBRUSH),
                    ("lpszMenuName", wintypes.LPCWSTR),
                    ("lpszClassName", wintypes.LPCWSTR),
                    ("hIconSm", wintypes.HICON),
                ]

            user32.RegisterWindowMessageW.argtypes = (wintypes.LPCWSTR,)
            user32.RegisterWindowMessageW.restype = wintypes.UINT
            message_id = int(
                user32.RegisterWindowMessageW(ELEMENT_NAVIGATION_MESSAGE_NAME)
            )
            if not message_id:
                raise OSError("RegisterWindowMessageW failed")

            user32.DefWindowProcW.argtypes = (
                wintypes.HWND,
                wintypes.UINT,
                wintypes.WPARAM,
                wintypes.LPARAM,
            )
            user32.DefWindowProcW.restype = lresult
            user32.DestroyWindow.argtypes = (wintypes.HWND,)
            user32.DestroyWindow.restype = wintypes.BOOL
            user32.PostQuitMessage.argtypes = (ctypes.c_int,)
            user32.PostQuitMessage.restype = None
            user32.GetMessageW.argtypes = (
                ctypes.POINTER(wintypes.MSG),
                wintypes.HWND,
                wintypes.UINT,
                wintypes.UINT,
            )
            user32.GetMessageW.restype = wintypes.BOOL
            user32.TranslateMessage.argtypes = (ctypes.POINTER(wintypes.MSG),)
            user32.TranslateMessage.restype = wintypes.BOOL
            user32.DispatchMessageW.argtypes = (ctypes.POINTER(wintypes.MSG),)
            user32.DispatchMessageW.restype = lresult
            user32.RegisterClassExW.argtypes = (ctypes.POINTER(WindowClass),)
            user32.RegisterClassExW.restype = wintypes.ATOM
            user32.CreateWindowExW.argtypes = (
                wintypes.DWORD,
                wintypes.LPCWSTR,
                wintypes.LPCWSTR,
                wintypes.DWORD,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                wintypes.HWND,
                wintypes.HMENU,
                wintypes.HINSTANCE,
                wintypes.LPVOID,
            )
            user32.CreateWindowExW.restype = wintypes.HWND
            user32.UnregisterClassW.argtypes = (
                wintypes.LPCWSTR,
                wintypes.HINSTANCE,
            )
            user32.UnregisterClassW.restype = wintypes.BOOL
            kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
            kernel32.GetModuleHandleW.restype = wintypes.HMODULE
            hinstance = int(kernel32.GetModuleHandleW(None))

            @window_proc_type
            def window_proc(hwnd, message, wparam, lparam):
                if int(message) == message_id:
                    try:
                        self._callback(int(wparam), int(lparam))
                    except Exception:
                        return 0
                    return 1
                if int(message) == self._WM_CLOSE:
                    user32.DestroyWindow(hwnd)
                    return 0
                if int(message) == self._WM_DESTROY:
                    user32.PostQuitMessage(0)
                    return 0
                return user32.DefWindowProcW(hwnd, message, wparam, lparam)

            self._window_proc = window_proc
            window_class = WindowClass()
            window_class.cbSize = ctypes.sizeof(WindowClass)
            window_class.lpfnWndProc = window_proc
            window_class.hInstance = hinstance
            window_class.lpszClassName = ELEMENT_NAVIGATION_WINDOW_CLASS
            atom = int(user32.RegisterClassExW(ctypes.byref(window_class)))
            if not atom:
                raise OSError("RegisterClassExW failed")
            handle = user32.CreateWindowExW(
                0,
                ELEMENT_NAVIGATION_WINDOW_CLASS,
                ELEMENT_NAVIGATION_WINDOW_TITLE,
                0,
                0,
                0,
                0,
                0,
                None,
                None,
                hinstance,
                None,
            )
            self._hwnd = int(handle) if handle else 0
            if self._hwnd <= 0:
                raise OSError("CreateWindowExW failed")
            self._ready.set()
            message = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
        except BaseException as exc:
            self._startup_error = exc
            self._ready.set()
        finally:
            self._hwnd = 0
            if atom and hinstance and user32 is not None:
                try:
                    user32.UnregisterClassW(
                        ELEMENT_NAVIGATION_WINDOW_CLASS,
                        hinstance,
                    )
                except Exception:
                    pass


__all__ = (
    "ELEMENT_NAVIGATION_COMMAND_QUIT",
    "ELEMENT_NAVIGATION_COMMAND_TOGGLE",
    "ELEMENT_NAVIGATION_MESSAGE_NAME",
    "ELEMENT_NAVIGATION_WINDOW_CLASS",
    "ELEMENT_NAVIGATION_WINDOW_TITLE",
    "ElementNavigationCommandServer",
)
