"""Explicit, bounded read of WeType's settings, without global input injection."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import time

from . import voice_program_manager


class SettingsReadError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class _SettingsAccess:
    def __init__(self, auto):
        self.auto = auto
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        for name, args, result in (
            ("EnumWindows", (self.callback_type, wintypes.LPARAM), wintypes.BOOL),
            ("IsWindowVisible", (wintypes.HWND,), wintypes.BOOL),
            ("GetWindowTextW", (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int), ctypes.c_int),
            ("GetWindowThreadProcessId", (wintypes.HWND, ctypes.POINTER(wintypes.DWORD)), wintypes.DWORD),
            ("GetForegroundWindow", (), wintypes.HWND),
            ("GetAncestor", (wintypes.HWND, wintypes.UINT), wintypes.HWND),
            ("ScreenToClient", (wintypes.HWND, ctypes.POINTER(wintypes.POINT)), wintypes.BOOL),
            ("ClientToScreen", (wintypes.HWND, ctypes.POINTER(wintypes.POINT)), wintypes.BOOL),
            ("GetClientRect", (wintypes.HWND, ctypes.POINTER(wintypes.RECT)), wintypes.BOOL),
            ("SendMessageTimeoutW", (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
                                     wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)), ctypes.c_ssize_t),
        ):
            fn = getattr(self.user32, name)
            fn.argtypes, fn.restype = args, result
        self.kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        self.kernel32.OpenProcess.restype = wintypes.HANDLE
        self.kernel32.QueryFullProcessImageNameW.argtypes = (
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
        )
        self.kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        self.kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        self.kernel32.CloseHandle.restype = wintypes.BOOL

    def foreground(self) -> int:
        return int(self.user32.GetForegroundWindow() or 0)

    def windows(self, executable: Path) -> tuple[int, ...]:
        expected = os.path.normcase(os.path.abspath(executable))
        handles = []

        @self.callback_type
        def visit(hwnd, _lparam):
            if not self.user32.IsWindowVisible(hwnd):
                return True
            title = ctypes.create_unicode_buffer(256)
            self.user32.GetWindowTextW(hwnd, title, len(title))
            if title.value != "设置":
                return True
            pid = wintypes.DWORD()
            self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            process = self.kernel32.OpenProcess(0x1000, False, pid.value)
            if not process:
                return True
            try:
                path = ctypes.create_unicode_buffer(32768)
                size = wintypes.DWORD(len(path))
                if self.kernel32.QueryFullProcessImageNameW(process, 0, path, ctypes.byref(size)):
                    if os.path.normcase(os.path.abspath(path.value)) == expected:
                        handles.append(int(hwnd))
            finally:
                self.kernel32.CloseHandle(process)
            return True

        if not self.user32.EnumWindows(visit, 0):
            raise OSError(ctypes.get_last_error(), "EnumWindows failed")
        return tuple(handles)

    def root(self, hwnd):
        return self.auto.ControlFromHandle(hwnd)

    def select_voice_page(self, hwnd, navigation, check):
        # Flutter's InvokePattern only focuses this icon. Send a paired click
        # to its own native view, never to the system mouse input queue.
        owner = navigation
        for _ in range(20):
            check()
            native = int(owner.NativeWindowHandle or 0)
            if native:
                break
            owner = owner.GetParentControl()
            if owner is None:
                break
        else:
            native = 0
        if not native or int(self.user32.GetAncestor(native, 2) or 0) != hwnd:
            raise SettingsReadError("navigation_unavailable", "无法确认微信语音菜单所属窗口；请手动录入。")
        rect = navigation.BoundingRectangle
        rectangle = (rect.left, rect.top, rect.right, rect.bottom)
        point = wintypes.POINT((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)
        screen_point = (point.x, point.y)
        bounds = wintypes.RECT()
        if (
            navigation.Name != "语音输入"
            or not navigation.IsEnabled or navigation.IsOffscreen
            or rect.right <= rect.left or rect.bottom <= rect.top
            or not self.user32.ScreenToClient(native, ctypes.byref(point))
            or not self.user32.GetClientRect(native, ctypes.byref(bounds))
            or not (0 <= point.x < min(bounds.right, 32768) and 0 <= point.y < min(bounds.bottom, 32768))
        ):
            raise SettingsReadError("navigation_unavailable", "微信语音菜单位置无法确认；请手动切到语音输入页后再刷新。")
        current_rect = navigation.BoundingRectangle
        verified_point = wintypes.POINT(point.x, point.y)
        if (
            rectangle != (current_rect.left, current_rect.top, current_rect.right, current_rect.bottom)
            or not self.user32.ClientToScreen(native, ctypes.byref(verified_point))
            or (verified_point.x, verified_point.y) != screen_point
        ):
            raise SettingsReadError("window_changed", "微信窗口位置已改变；本次未切换页面，请重试。")
        check()
        if self.foreground() != hwnd:
            raise SettingsReadError("focus_changed", "微信设置不在前台；本次不切换页面，请重试。")
        position = point.x | (point.y << 16)
        result = ctypes.c_size_t()
        def send(message, buttons):
            if int(self.user32.GetAncestor(native, 2) or 0) != hwnd:
                return False
            return bool(self.user32.SendMessageTimeoutW(native, message, buttons, position, 0x22, 400, ctypes.byref(result)))

        try:
            pressed = send(0x0201, 1)  # WM_LBUTTONDOWN, MK_LBUTTON
        finally:
            released = send(0x0202, 0)  # Paired release even after a failed down.
        if not pressed or not released:
            raise SettingsReadError("navigation_failed", "切换微信语音输入页失败；请手动切到该页后再刷新。")


def _snapshot(root, check, *, trigger="hold"):
    """Read only the selected window; never search the desktop or click a field."""
    pending = [(root, 0)]
    rows = []
    navigation = []
    voice_page = False
    count = 0
    expected_title = "启动语音输入" if trigger == "toggle" else "按住说话"
    while pending:
        check()
        control, depth = pending.pop()
        count += 1
        if count > 256 or depth > 18:
            raise SettingsReadError("unsupported_ui", "微信设置页面结构无法确认；请手动录入。")
        name = " ".join(str(control.Name or "").split())
        children = control.GetChildren()
        if name == "语音输入":
            if len(children) == 1 and children[0].ControlTypeName == "ButtonControl":
                navigation.append(control)
            elif {"输入", "语音输入"}.issubset({str(child.Name) for child in children}):
                voice_page = True
        for index, child in enumerate(children[:-1]):
            # WeType combines the title and a version-dependent description in
            # Name. Match the complete title, keeping the adjacent field and
            # unique-row checks below; never search arbitrary description text.
            label = " ".join(str(child.Name or "").split())
            if label == expected_title or label.startswith(expected_title + " "):
                field = children[index + 1]
                if field.ControlTypeName != "GroupControl" or not field.IsEnabled:
                    raise SettingsReadError("unavailable_shortcut", f"微信{expected_title}快捷键不可读取；请手动核对设置。")
                values = [str(item.Name).strip() for item in field.GetChildren() if str(item.Name or "").strip()]
                if len(values) != 1:
                    raise SettingsReadError("ambiguous_shortcut", f"无法唯一确认微信{expected_title}快捷键；请手动录入。")
                rows.append(values[0])
        pending.extend((child, depth + 1) for child in children)
    if len(rows) > 1 or len(navigation) > 1:
        raise SettingsReadError("ambiguous_ui", "微信设置中有多个匹配项；本次不读取，请手动录入。")
    return (rows[0] if voice_page and rows else ""), (navigation[0] if navigation else None), voice_page


def _read_with_access(access, cancel_event, *, timeout=5.0, trigger="hold"):
    deadline = time.monotonic() + timeout
    origin = access.foreground()
    hwnd = None

    def check():
        if cancel_event is not None and cancel_event.is_set():
            raise SettingsReadError("cancelled", "微信快捷键刷新已取消。")
        if time.monotonic() >= deadline:
            raise SettingsReadError("timeout", "未能及时读取微信快捷键；请重试或手动录入。")
        if access.foreground() not in {origin, hwnd}:
            raise SettingsReadError("focus_changed", "窗口已切换，本次停止读取微信快捷键；请重试或手动录入。")

    check()
    target = voice_program_manager.resolve_voice_program_settings_target({"provider": "wetype"})
    if not target.available or target.kind != "executable":
        raise SettingsReadError("not_found", "未找到微信设置程序；可以手动录入快捷键。")
    executable = Path(target.target)
    check()
    matches = access.windows(executable)
    if len(matches) > 1:
        raise SettingsReadError("ambiguous_window", "找到多个微信设置窗口；请关闭多余窗口后重试。")
    if matches:
        hwnd = matches[0]
    if hwnd is None or origin != hwnd:
        check()
        try:
            voice_program_manager.open_voice_program_settings(executable, target.arguments)
        except Exception as exc:
            raise SettingsReadError("open_failed", "无法打开微信设置；可以手动打开设置或录入快捷键。") from exc

    navigated = False
    previous = ""
    while True:
        # Discover the window before checking focus: the requested window may
        # itself have become foreground while the launcher was returning.
        matches = access.windows(executable)
        if len(matches) > 1 or (hwnd is not None and matches != (hwnd,)):
            raise SettingsReadError("window_changed", "微信设置窗口已关闭或改变；本次未读取快捷键。")
        if matches:
            hwnd = matches[0]
        check()
        if hwnd is not None:
            value, navigation, voice_page = (_snapshot(access.root(hwnd), check, trigger="toggle")
                                             if trigger == "toggle" else _snapshot(access.root(hwnd), check))
            check()
            if value and value == previous:
                if access.windows(executable) != (hwnd,):
                    raise SettingsReadError("window_changed", "微信设置窗口已改变；本次未读取快捷键。")
                check()
                return value
            previous = value
            if not voice_page and navigation is not None and not navigated:
                check()
                navigated = True
                access.select_voice_page(hwnd, navigation, check)
        time.sleep(0.1)


def read_hold_shortcut(*, cancel_event=None) -> str:
    import uiautomation as auto

    with auto.UIAutomationInitializerInThread():
        return _read_with_access(_SettingsAccess(auto), cancel_event)


def read_toggle_shortcut(*, cancel_event=None) -> str:
    import uiautomation as auto

    with auto.UIAutomationInitializerInThread():
        return _read_with_access(_SettingsAccess(auto), cancel_event, trigger="toggle")
