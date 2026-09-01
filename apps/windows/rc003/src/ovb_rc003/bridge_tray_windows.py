"""Native Windows notification-area control for the background bridge.

The bridge owns an asyncio loop on its main thread, so the tray runs a small
Win32 message loop on a helper thread. Menu callbacks never touch asyncio
objects directly: the application-provided exit callback hops back to the
owning event loop with ``call_soon_threadsafe``.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import sys
import threading
from typing import Callable

from . import device_catalog, product_identity


WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_COMMAND = 0x0111
WM_CONTEXTMENU = 0x007B
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_APP = 0x8000
WM_TRAY_CALLBACK = WM_APP + 1

NIM_ADD = 0x00000000
NIM_DELETE = 0x00000002
NIM_SETVERSION = 0x00000004
NOTIFYICON_VERSION_4 = 4
NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004

MF_STRING = 0x00000000
MF_SEPARATOR = 0x00000800
TPM_RIGHTBUTTON = 0x0002
TPM_RETURNCMD = 0x0100

MENU_OPEN_SETTINGS = 1001
MENU_EXIT_BRIDGE = 1002
BRIDGE_TRAY_WINDOW_TITLE = "Remote Mic RC003 bridge tray"


def dispatch_menu_command(
    command_id: int,
    *,
    on_open_settings: Callable[[], None],
    on_exit_requested: Callable[[], None],
) -> bool:
    if command_id == MENU_OPEN_SETTINGS:
        on_open_settings()
        return True
    if command_id == MENU_EXIT_BRIDGE:
        on_exit_requested()
        return True
    return False


class GUID(ctypes.Structure):
    _fields_ = (
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    )


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = (
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uTimeoutOrVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", GUID),
        ("hBalloonIcon", wintypes.HICON),
    )


LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(
    LRESULT,
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
)


class WNDCLASSW(ctypes.Structure):
    _fields_ = (
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HANDLE),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    )


class BridgeTray:
    def __init__(
        self,
        *,
        on_open_settings: Callable[[], None],
        on_exit_requested: Callable[[], None],
        status_handler: Callable[[str], None] | None = None,
        tooltip: str = (
            f"{product_identity.DISPLAY_NAME} · {device_catalog.RC003_DISPLAY_NAME}"
        ),
        show_icon: bool = True,
    ) -> None:
        self._on_open_settings = on_open_settings
        self._on_exit_requested = on_exit_requested
        self._status_handler = status_handler or (lambda _message: None)
        self._tooltip = tooltip[:127]
        self._show_icon = bool(show_icon)
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._stop_requested = threading.Event()
        self._startup_error = ""
        self._hwnd: int | None = None
        self._wndproc = None
        self._nid: NOTIFYICONDATAW | None = None
        self._owned_icons: list[int] = []
        self._user32 = None

    @property
    def startup_error(self) -> str:
        return self._startup_error

    def start(self, timeout: float = 5.0) -> bool:
        if os.name != "nt":
            self._startup_error = "notification area is only available on Windows"
            return False
        if self._thread is not None and self._thread.is_alive():
            return True
        self._ready.clear()
        self._stop_requested.clear()
        self._startup_error = ""
        self._thread = threading.Thread(
            target=self._thread_main,
            name="rc003-bridge-notification-area",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=max(0.1, timeout)):
            self._startup_error = "notification area startup timed out"
            return False
        return not self._startup_error and self._thread.is_alive()

    def stop(self, timeout: float = 5.0) -> bool:
        thread = self._thread
        if thread is None:
            return True
        self._stop_requested.set()
        hwnd = self._hwnd
        if hwnd and self._user32 is not None:
            self._user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        thread.join(timeout=max(0.1, timeout))
        stopped = not thread.is_alive()
        if stopped:
            self._thread = None
        return stopped

    def _report(self, message: str) -> None:
        try:
            self._status_handler(message)
        except Exception:
            pass

    def _thread_main(self) -> None:
        try:
            self._run_message_loop()
        except Exception as exc:  # noqa: BLE001 - tray failure must not kill bridge
            self._startup_error = f"notification area error: {type(exc).__name__}"
            self._report(self._startup_error)
        finally:
            self._ready.set()
            self._hwnd = None
            self._user32 = None

    def _configure_apis(self):
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        user32.DefWindowProcW.argtypes = (
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        )
        user32.DefWindowProcW.restype = LRESULT
        user32.RegisterClassW.argtypes = (ctypes.POINTER(WNDCLASSW),)
        user32.RegisterClassW.restype = wintypes.ATOM
        user32.UnregisterClassW.argtypes = (
            wintypes.LPCWSTR,
            wintypes.HINSTANCE,
        )
        user32.UnregisterClassW.restype = wintypes.BOOL
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
        user32.DestroyWindow.argtypes = (wintypes.HWND,)
        user32.DestroyWindow.restype = wintypes.BOOL
        user32.PostMessageW.argtypes = (
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        )
        user32.PostMessageW.restype = wintypes.BOOL
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
        user32.DispatchMessageW.restype = LRESULT
        user32.CreatePopupMenu.restype = wintypes.HMENU
        user32.AppendMenuW.argtypes = (
            wintypes.HMENU,
            wintypes.UINT,
            ctypes.c_size_t,
            wintypes.LPCWSTR,
        )
        user32.AppendMenuW.restype = wintypes.BOOL
        user32.GetCursorPos.argtypes = (ctypes.POINTER(wintypes.POINT),)
        user32.GetCursorPos.restype = wintypes.BOOL
        user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
        user32.SetForegroundWindow.restype = wintypes.BOOL
        user32.TrackPopupMenu.argtypes = (
            wintypes.HMENU,
            wintypes.UINT,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HWND,
            wintypes.LPVOID,
        )
        user32.TrackPopupMenu.restype = wintypes.UINT
        user32.DestroyMenu.argtypes = (wintypes.HMENU,)
        user32.DestroyMenu.restype = wintypes.BOOL
        user32.RegisterWindowMessageW.argtypes = (wintypes.LPCWSTR,)
        user32.RegisterWindowMessageW.restype = wintypes.UINT
        user32.PostQuitMessage.argtypes = (ctypes.c_int,)
        user32.PostQuitMessage.restype = None
        user32.DestroyIcon.argtypes = (wintypes.HICON,)
        user32.DestroyIcon.restype = wintypes.BOOL
        user32.LoadIconW.argtypes = (wintypes.HINSTANCE, ctypes.c_void_p)
        user32.LoadIconW.restype = wintypes.HICON
        shell32.Shell_NotifyIconW.argtypes = (
            wintypes.DWORD,
            ctypes.POINTER(NOTIFYICONDATAW),
        )
        shell32.Shell_NotifyIconW.restype = wintypes.BOOL
        shell32.ExtractIconExW.argtypes = (
            wintypes.LPCWSTR,
            ctypes.c_int,
            ctypes.POINTER(wintypes.HICON),
            ctypes.POINTER(wintypes.HICON),
            wintypes.UINT,
        )
        shell32.ExtractIconExW.restype = wintypes.UINT
        kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        return user32, shell32, kernel32

    def _run_message_loop(self) -> None:
        user32, shell32, kernel32 = self._configure_apis()
        self._user32 = user32
        instance = kernel32.GetModuleHandleW(None)
        class_name = f"RemoteMicRC003BridgeTray.{os.getpid()}"
        taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")

        @WNDPROC
        def wndproc(hwnd, message, wparam, lparam):
            if message == WM_TRAY_CALLBACK:
                event_code = int(lparam) & 0xFFFF
                if event_code == WM_LBUTTONDBLCLK:
                    self._invoke_open_settings()
                    return 0
                if event_code in {WM_RBUTTONUP, WM_CONTEXTMENU}:
                    self._show_menu(user32, hwnd)
                    return 0
            if taskbar_created and message == taskbar_created:
                if self._show_icon:
                    self._add_icon(user32, shell32, hwnd)
                return 0
            if message == WM_COMMAND:
                self._dispatch_command(int(wparam) & 0xFFFF, hwnd, user32)
                return 0
            if message == WM_CLOSE:
                user32.DestroyWindow(hwnd)
                return 0
            if message == WM_DESTROY:
                self._remove_icon(shell32)
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, message, wparam, lparam)

        self._wndproc = wndproc
        window_class = WNDCLASSW()
        window_class.lpfnWndProc = wndproc
        window_class.hInstance = instance
        window_class.lpszClassName = class_name
        atom = user32.RegisterClassW(ctypes.byref(window_class))
        if not atom:
            raise ctypes.WinError(ctypes.get_last_error())

        hwnd = None
        try:
            hwnd = user32.CreateWindowExW(
                0,
                class_name,
                BRIDGE_TRAY_WINDOW_TITLE,
                0,
                0,
                0,
                0,
                0,
                None,
                None,
                instance,
                None,
            )
            if not hwnd:
                raise ctypes.WinError(ctypes.get_last_error())
            self._hwnd = int(hwnd)
            if self._show_icon:
                self._add_icon(user32, shell32, hwnd)
            self._ready.set()
            self._report(
                "notification area icon ready"
                if self._show_icon
                else "background bridge control ready"
            )

            if self._stop_requested.is_set():
                user32.DestroyWindow(hwnd)

            message = wintypes.MSG()
            while True:
                result = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result == -1:
                    raise ctypes.WinError(ctypes.get_last_error())
                if result == 0:
                    break
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
        finally:
            if hwnd:
                user32.DestroyWindow(hwnd)
            self._hwnd = None
            self._remove_icon(shell32)
            for handle in self._owned_icons:
                user32.DestroyIcon(handle)
            self._owned_icons.clear()
            user32.UnregisterClassW(class_name, instance)

    def _load_icon(self, user32, shell32) -> int:
        large = wintypes.HICON()
        small = wintypes.HICON()
        extracted = shell32.ExtractIconExW(
            sys.executable,
            0,
            ctypes.byref(large),
            ctypes.byref(small),
            1,
        )
        for icon in (large, small):
            if icon.value and int(icon.value) not in self._owned_icons:
                self._owned_icons.append(int(icon.value))
        if extracted and small.value:
            return int(small.value)
        if extracted and large.value:
            return int(large.value)
        fallback = user32.LoadIconW(None, ctypes.c_void_p(32512))
        if not fallback:
            raise ctypes.WinError(ctypes.get_last_error())
        return int(fallback)

    def _add_icon(self, user32, shell32, hwnd) -> None:
        if self._nid is None:
            nid = NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            nid.hWnd = hwnd
            nid.uID = 1
            nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
            nid.uCallbackMessage = WM_TRAY_CALLBACK
            nid.hIcon = self._load_icon(user32, shell32)
            nid.szTip = self._tooltip
            self._nid = nid
        else:
            self._nid.hWnd = hwnd
        if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self._nid)):
            raise ctypes.WinError(ctypes.get_last_error())
        self._nid.uTimeoutOrVersion = NOTIFYICON_VERSION_4
        shell32.Shell_NotifyIconW(NIM_SETVERSION, ctypes.byref(self._nid))

    def _remove_icon(self, shell32) -> None:
        if self._nid is not None:
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
            self._nid = None

    def _show_menu(self, user32, hwnd) -> None:
        menu = user32.CreatePopupMenu()
        if not menu:
            return
        try:
            user32.AppendMenuW(menu, MF_STRING, MENU_OPEN_SETTINGS, "打开设置")
            user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            user32.AppendMenuW(menu, MF_STRING, MENU_EXIT_BRIDGE, "退出桥接")
            point = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(point))
            user32.SetForegroundWindow(hwnd)
            command_id = user32.TrackPopupMenu(
                menu,
                TPM_RIGHTBUTTON | TPM_RETURNCMD,
                point.x,
                point.y,
                0,
                hwnd,
                None,
            )
            if command_id:
                self._dispatch_command(int(command_id), hwnd, user32)
        finally:
            user32.DestroyMenu(menu)

    def _dispatch_command(self, command_id: int, hwnd, user32) -> None:
        should_close = command_id == MENU_EXIT_BRIDGE
        try:
            dispatch_menu_command(
                command_id,
                on_open_settings=self._invoke_open_settings,
                on_exit_requested=self._on_exit_requested,
            )
        finally:
            if should_close:
                user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)

    def _invoke_open_settings(self) -> None:
        try:
            self._on_open_settings()
        except Exception as exc:  # noqa: BLE001 - tray remains usable
            self._report(f"open settings failed: {type(exc).__name__}")
