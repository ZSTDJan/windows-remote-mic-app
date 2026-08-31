"""Windows UI Automation, input, overlay, and lifecycle host.

The executable entry loads this module lazily after confirming Windows.
UI Automation and Qt remain imported inside ``_run_windows`` so DPI
awareness is configured first.
"""

from __future__ import annotations

import argparse
import ctypes
import queue
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, replace
from typing import Any, Callable, Optional, Sequence

from spatial_navigation_core import *
from element_targeting_core import *
from element_navigation_support import *
from element_navigation_command_windows import (
    ELEMENT_NAVIGATION_COMMAND_QUIT,
    ELEMENT_NAVIGATION_COMMAND_TOGGLE,
    ElementNavigationCommandServer,
)


def _run_windows(args: argparse.Namespace) -> int:
    # uiautomation opts into legacy system-DPI awareness during import. Set
    # per-monitor v2 first so Qt and UIA agree on mixed-DPI screen coordinates.
    dpi_user32 = ctypes.windll.user32
    dpi_user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    dpi_user32.SetProcessDpiAwarenessContext.restype = ctypes.c_bool
    dpi_user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))

    import uiautomation as auto
    from ctypes import wintypes
    from PySide6.QtCore import Qt, QRect, QTimer
    from PySide6.QtGui import QColor, QFont, QGuiApplication, QPainter, QPen
    from PySide6.QtWidgets import QApplication, QWidget
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    gdi32 = ctypes.windll.gdi32
    oleacc = ctypes.WinDLL("oleacc")
    oleaut32 = ctypes.WinDLL("oleaut32")
    ole32 = ctypes.WinDLL("ole32")
    dwmapi = ctypes.WinDLL("dwmapi")
    lresult = ctypes.c_ssize_t

    class VariantValue(ctypes.Union):
        _fields_ = [
            ("ll_value", ctypes.c_longlong),
            ("long_value", ctypes.c_long),
            ("unknown", ctypes.c_void_p),
            ("dispatch", ctypes.c_void_p),
        ]

    class Variant(ctypes.Structure):
        _anonymous_ = ("value",)
        _fields_ = [
            ("vt", ctypes.c_ushort),
            ("reserved1", ctypes.c_ushort),
            ("reserved2", ctypes.c_ushort),
            ("reserved3", ctypes.c_ushort),
            ("value", VariantValue),
        ]

    class GuiThreadInfo(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("hwndActive", wintypes.HWND),
            ("hwndFocus", wintypes.HWND),
            ("hwndCapture", wintypes.HWND),
            ("hwndMenuOwner", wintypes.HWND),
            ("hwndMoveSize", wintypes.HWND),
            ("hwndCaret", wintypes.HWND),
            ("rcCaret", wintypes.RECT),
        ]

    class BitmapInfoHeader(ctypes.Structure):
        _fields_ = [
            ("size", wintypes.DWORD),
            ("width", ctypes.c_long),
            ("height", ctypes.c_long),
            ("planes", wintypes.WORD),
            ("bit_count", wintypes.WORD),
            ("compression", wintypes.DWORD),
            ("image_size", wintypes.DWORD),
            ("x_pixels_per_meter", ctypes.c_long),
            ("y_pixels_per_meter", ctypes.c_long),
            ("colors_used", wintypes.DWORD),
            ("colors_important", wintypes.DWORD),
        ]

    class BitmapInfo(ctypes.Structure):
        _fields_ = [
            ("header", BitmapInfoHeader),
            ("colors", wintypes.DWORD * 3),
        ]

    kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetAncestor.restype = wintypes.HWND
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindow.restype = wintypes.HWND
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.ReleaseDC.restype = ctypes.c_int
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.BitBlt.argtypes = [
        wintypes.HDC,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.HDC,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.DWORD,
    ]
    gdi32.BitBlt.restype = wintypes.BOOL
    gdi32.GetDIBits.argtypes = [
        wintypes.HDC,
        wintypes.HBITMAP,
        wintypes.UINT,
        wintypes.UINT,
        ctypes.c_void_p,
        ctypes.POINTER(BitmapInfo),
        wintypes.UINT,
    ]
    gdi32.GetDIBits.restype = ctypes.c_int
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteObject.restype = wintypes.BOOL
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    gdi32.DeleteDC.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    ]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    user32.GetCursorPos.restype = wintypes.BOOL
    user32.GetDoubleClickTime.argtypes = []
    user32.GetDoubleClickTime.restype = wintypes.UINT
    user32.GetGUIThreadInfo.argtypes = [
        wintypes.DWORD,
        ctypes.POINTER(GuiThreadInfo),
    ]
    user32.GetGUIThreadInfo.restype = wintypes.BOOL
    user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    user32.SetCursorPos.restype = wintypes.BOOL
    user32.mouse_event.argtypes = [
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_size_t,
    ]
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    user32.GetAsyncKeyState.restype = ctypes.c_short
    user32.CallNextHookEx.restype = lresult
    user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
    user32.UnhookWindowsHookEx.restype = wintypes.BOOL
    user32.PostThreadMessageW.argtypes = [
        wintypes.DWORD,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    user32.PostThreadMessageW.restype = wintypes.BOOL
    user32.PeekMessageW.argtypes = [
        ctypes.POINTER(wintypes.MSG),
        wintypes.HWND,
        wintypes.UINT,
        wintypes.UINT,
        wintypes.UINT,
    ]
    user32.PeekMessageW.restype = wintypes.BOOL
    win_event_proc_type = ctypes.WINFUNCTYPE(
        None,
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.HWND,
        ctypes.c_long,
        ctypes.c_long,
        wintypes.DWORD,
        wintypes.DWORD,
    )
    user32.SetWinEventHook.argtypes = [
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HMODULE,
        win_event_proc_type,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    user32.SetWinEventHook.restype = wintypes.HANDLE
    user32.UnhookWinEvent.argtypes = [wintypes.HANDLE]
    user32.UnhookWinEvent.restype = wintypes.BOOL
    user32.SendMessageTimeoutW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
        wintypes.UINT,
        wintypes.UINT,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    user32.SendMessageTimeoutW.restype = lresult
    dwmapi.DwmGetWindowAttribute.argtypes = [
        wintypes.HWND,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long
    oleacc.AccessibleObjectFromPoint.argtypes = [
        wintypes.POINT,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(Variant),
    ]
    oleacc.AccessibleObjectFromPoint.restype = ctypes.c_long
    oleaut32.VariantClear.argtypes = [ctypes.POINTER(Variant)]
    oleaut32.VariantClear.restype = ctypes.c_long
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    ole32.CoInitializeEx.restype = ctypes.c_long
    ole32.CoUninitialize.argtypes = []

    interactive_types = frozenset(
        {
            "ButtonControl",
            "SplitButtonControl",
            "HyperlinkControl",
            "EditControl",
            "CheckBoxControl",
            "RadioButtonControl",
            "ComboBoxControl",
            "MenuItemControl",
            "TabItemControl",
            "ListItemControl",
            "TreeItemControl",
            "DataItemControl",
            "SliderControl",
            "SpinnerControl",
        }
    )
    direct_action_pattern_ids = (
        auto.PatternId.ExpandCollapsePattern,
        auto.PatternId.InvokePattern,
        auto.PatternId.TogglePattern,
        auto.PatternId.SelectionItemPattern,
    )
    wm_getobject = 0x003D
    smto_abortifhung = 0x0002
    accessibility_object_ids = (-25, -4)
    child_id_self = 0
    coinit_apartment_threaded = 0x2
    rpc_e_changed_mode = ctypes.c_long(0x80010106).value
    mouseeventf_leftdown = 0x0002
    mouseeventf_leftup = 0x0004
    mouseeventf_rightdown = 0x0008
    mouseeventf_rightup = 0x0010
    mouseeventf_wheel = 0x0800
    wheel_delta = 120
    gw_owner = 4
    ga_root = 2
    gwl_exstyle = -20
    process_query_limited_information = 0x1000
    synchronize_process = 0x00100000
    wait_timeout = 0x00000102
    dwmwa_cloaked = 14
    srccopy = 0x00CC0020
    bi_rgb = 0
    dib_rgb_colors = 0
    pm_noremove = 0
    gui_menu_mode_flags = 0x0004 | 0x0008 | 0x0010
    winevent_outofcontext = 0x0000
    winevent_skipownprocess = 0x0002
    awakened_chromium_windows: set[int] = set()

    @dataclass
    class RuntimeTarget:
        snapshot: TargetSnapshot
        control: Any
        click_point: Optional[tuple[int, int]] = None

    def rect_from_control(control: Any) -> Rect:
        bounds = control.BoundingRectangle
        return Rect(
            int(bounds.left),
            int(bounds.top),
            int(bounds.right),
            int(bounds.bottom),
        )

    def runtime_id_from_control(control: Any) -> tuple[int, ...]:
        try:
            runtime_id = control.GetRuntimeId()
            return tuple(int(value) for value in runtime_id) if runtime_id else ()
        except Exception:
            return ()

    def action_pattern_support(control: Any) -> tuple[bool, bool, bool]:
        for pattern_id in direct_action_pattern_ids:
            try:
                if control.GetPattern(pattern_id) is not None:
                    supports_expand = (
                        pattern_id == auto.PatternId.ExpandCollapsePattern
                    )
                    return True, True, supports_expand
            except Exception:
                continue
        try:
            legacy = control.GetPattern(auto.PatternId.LegacyIAccessiblePattern)
        except Exception:
            legacy = None
        return legacy is not None, False, False

    def control_supports_pattern(control: Any, pattern_id: int) -> bool:
        try:
            return control.GetPattern(pattern_id) is not None
        except Exception:
            return False

    def runtime_target_from_control(
        control: Any,
        window_rect: Rect,
        path: tuple[int, ...] = (),
        depth: int = 0,
        source: str = "uia",
        section_path: tuple[int, ...] = (),
        section_rect: Optional[Rect] = None,
        precomputed_rect: Optional[Rect] = None,
    ) -> Optional[RuntimeTarget]:
        try:
            control_type = str(control.ControlTypeName or "")
            standard = control_type in interactive_types
            structural = control_type in STRUCTURAL_CONTROL_TYPES
            if not standard and not structural:
                return None
            name = str(control.Name or "").strip()
            automation_id = str(control.AutomationId or "").strip()
            enabled = bool(control.IsEnabled)
            offscreen = bool(control.IsOffscreen)
            rect = (
                precomputed_rect
                if precomputed_rect is not None
                else rect_from_control(control)
            )
            valid_size = 16 <= rect.width <= 1800 and 16 <= rect.height <= 1400
            if not (
                enabled
                and not offscreen
                and valid_size
                and not is_navigation_noise(name)
                and rect.intersects(window_rect)
            ):
                return None
            keyboard_focusable = bool(control.IsKeyboardFocusable)
            (
                action_pattern,
                direct_action_pattern,
                supports_expand,
            ) = action_pattern_support(control)
            existing_structural_action = bool(
                structural
                and structural_target_has_actionable_semantics(
                    control_type,
                    name,
                    automation_id,
                    rect,
                    keyboard_focusable=keyboard_focusable,
                    has_direct_action_pattern=direct_action_pattern,
                    has_text_edit_pattern=False,
                )
            )
            has_text_edit_pattern = bool(
                structural
                and keyboard_focusable
                and not existing_structural_action
                and control_supports_pattern(
                    control,
                    auto.PatternId.TextEditPattern,
                )
            )
            actionable = (
                standard
                and standard_control_has_actionable_semantics(
                    control_type,
                    keyboard_focusable,
                    action_pattern,
                    direct_action_pattern,
                )
            ) or (
                structural
                and structural_target_has_actionable_semantics(
                    control_type,
                    name,
                    automation_id,
                    rect,
                    keyboard_focusable=keyboard_focusable,
                    has_direct_action_pattern=direct_action_pattern,
                    has_text_edit_pattern=has_text_edit_pattern,
                )
            )
            if not actionable:
                return None
            return RuntimeTarget(
                TargetSnapshot(
                    rect=rect,
                    name=name,
                    control_type=control_type,
                    automation_id=automation_id,
                    path=path,
                    depth=depth,
                    keyboard_focusable=keyboard_focusable,
                    has_action_pattern=action_pattern,
                    supports_expand=supports_expand,
                    runtime_id=runtime_id_from_control(control),
                    source=source,
                    section_path=section_path,
                    section_rect=section_rect,
                ),
                control,
            )
        except Exception:
            return None

    def _msaa_rect_at_point_core(point: tuple[int, int]) -> Optional[Rect]:
        initialized = False
        accessible = ctypes.c_void_p()
        child = Variant()
        try:
            init_hr = int(ole32.CoInitializeEx(None, coinit_apartment_threaded))
            initialized = init_hr >= 0
            if init_hr < 0 and init_hr != rpc_e_changed_mode:
                return None

            native_point = wintypes.POINT(point[0], point[1])
            hr = int(
                oleacc.AccessibleObjectFromPoint(
                    native_point, ctypes.byref(accessible), ctypes.byref(child)
                )
            )
            if hr < 0 or not accessible.value:
                return None

            vtable = ctypes.cast(
                accessible, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))
            ).contents
            acc_location_type = ctypes.WINFUNCTYPE(
                ctypes.c_long,
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_long),
                ctypes.POINTER(ctypes.c_long),
                ctypes.POINTER(ctypes.c_long),
                ctypes.POINTER(ctypes.c_long),
                Variant,
            )
            acc_location = acc_location_type(vtable[22])
            left = ctypes.c_long()
            top = ctypes.c_long()
            width = ctypes.c_long()
            height = ctypes.c_long()
            location_hr = int(
                acc_location(
                    accessible,
                    ctypes.byref(left),
                    ctypes.byref(top),
                    ctypes.byref(width),
                    ctypes.byref(height),
                    child,
                )
            )
            if location_hr < 0 or width.value <= 0 or height.value <= 0:
                return None
            return Rect(
                left.value,
                top.value,
                left.value + width.value,
                top.value + height.value,
            )
        except Exception:
            return None
        finally:
            try:
                oleaut32.VariantClear(ctypes.byref(child))
            except Exception:
                pass
            if accessible.value:
                try:
                    vtable = ctypes.cast(
                        accessible, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))
                    ).contents
                    release_type = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
                    release_type(vtable[2])(accessible)
                except Exception:
                    pass
            if initialized:
                try:
                    ole32.CoUninitialize()
                except Exception:
                    pass

    def msaa_rect_at_point(
        point: tuple[int, int], timeout_ms: int = 80
    ) -> Optional[Rect]:
        result: queue.Queue[Optional[Rect]] = queue.Queue(maxsize=1)

        def detect() -> None:
            try:
                result.put_nowait(_msaa_rect_at_point_core(point))
            except Exception:
                pass

        threading.Thread(
            target=detect,
            name="element-navigation-msaa-hit",
            daemon=True,
        ).start()
        try:
            return result.get(timeout=max(1, timeout_ms) / 1000)
        except queue.Empty:
            return None

    def click_point(point: tuple[int, int], button: str = "left") -> None:
        user32.SetCursorPos(point[0], point[1])
        if button == "right":
            down, up = mouseeventf_rightdown, mouseeventf_rightup
        else:
            down, up = mouseeventf_leftdown, mouseeventf_leftup
        user32.mouse_event(down, 0, 0, 0, 0)
        user32.mouse_event(up, 0, 0, 0, 0)

    def scroll_point(point: tuple[int, int], steps: int) -> None:
        user32.SetCursorPos(point[0], point[1])
        user32.mouse_event(
            mouseeventf_wheel,
            0,
            0,
            mouse_wheel_data(steps * wheel_delta),
            0,
        )

    def window_class_name(hwnd: int) -> str:
        buffer = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buffer, len(buffer))
        return buffer.value

    def window_text(hwnd: int) -> str:
        buffer = ctypes.create_unicode_buffer(512)
        if user32.GetWindowTextW(hwnd, buffer, len(buffer)) <= 0:
            return ""
        return buffer.value

    process_name_cache: dict[int, tuple[float, str]] = {}
    process_name_cache_lock = threading.Lock()
    process_name_cache_seconds = 2.0

    def process_name_from_id(process_id: int) -> str:
        if process_id <= 0:
            return ""
        now = time.perf_counter()
        with process_name_cache_lock:
            cached = process_name_cache.get(process_id)
            if cached is not None and now - cached[0] < process_name_cache_seconds:
                return cached[1]
        handle = kernel32.OpenProcess(
            process_query_limited_information,
            False,
            process_id,
        )
        if not handle:
            return ""
        process_name = ""
        try:
            size = wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(size.value)
            if kernel32.QueryFullProcessImageNameW(
                handle,
                0,
                buffer,
                ctypes.byref(size),
            ):
                process_name = normalized_process_name(buffer.value)
        finally:
            kernel32.CloseHandle(handle)
        with process_name_cache_lock:
            process_name_cache[process_id] = (now, process_name)
        return process_name

    def owner_process_is_alive(process_id: int) -> bool:
        if process_id <= 0:
            return True
        handle = kernel32.OpenProcess(
            synchronize_process,
            False,
            process_id,
        )
        if not handle:
            return False
        try:
            return int(kernel32.WaitForSingleObject(handle, 0)) == wait_timeout
        finally:
            kernel32.CloseHandle(handle)

    def window_process_id(hwnd: int) -> int:
        process_id = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
        return int(process_id.value)

    dirty_windows = DirtyWindowTracker()

    def window_owner(hwnd: int) -> int:
        return native_handle_value(user32.GetWindow(hwnd, gw_owner))

    def window_rect_from_handle(hwnd: int) -> Rect:
        native_rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(native_rect)):
            return Rect(0, 0, 0, 0)
        return Rect(
            int(native_rect.left),
            int(native_rect.top),
            int(native_rect.right),
            int(native_rect.bottom),
        )

    def window_is_cloaked(hwnd: int) -> bool:
        cloaked = wintypes.DWORD()
        result = int(
            dwmapi.DwmGetWindowAttribute(
                hwnd,
                dwmwa_cloaked,
                ctypes.byref(cloaked),
                ctypes.sizeof(cloaked),
            )
        )
        return bool(result >= 0 and cloaked.value)

    def associated_overlay_window_signature(
        root_hwnd: int,
        root_rect: Optional[Rect] = None,
        excluded_process_id: int = 0,
    ) -> tuple[tuple[int, Rect], ...]:
        if root_hwnd <= 0:
            return ()
        if root_rect is None:
            root_rect = window_rect_from_handle(root_hwnd)
        if root_rect.width <= 0 or root_rect.height <= 0:
            return ()
        root_process_id = window_process_id(root_hwnd)
        root_process_name = process_name_from_id(root_process_id)
        process_names = {root_process_id: root_process_name}
        quicker_associations = load_quicker_overlay_associations(
            args.quicker_state_file
        )
        associated: list[tuple[int, Rect]] = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def collect(hwnd: int, _lparam: int) -> bool:
            handle = native_handle_value(hwnd)
            if handle == root_hwnd:
                return False
            if window_class_name(handle) in NON_NAVIGATION_OVERLAY_CLASSES:
                return True
            process_id = window_process_id(handle)
            if excluded_process_id > 0 and process_id == excluded_process_id:
                return True
            exstyle = int(user32.GetWindowLongPtrW(handle, gwl_exstyle))
            process_name = process_names.get(process_id)
            if process_name is None:
                process_name = process_name_from_id(process_id)
                process_names[process_id] = process_name
            title = window_text(handle)
            is_quicker_float = bool(
                process_name == "quicker"
                and title in QUICKER_FLOAT_WINDOW_TITLES
            )
            explicitly_associated = quicker_overlay_matches_process(
                quicker_associations.get(handle),
                root_process_name,
            )
            related = overlay_window_is_related(
                process_id,
                root_process_id,
                candidate_owned_by_root=owner_chain_contains(
                    handle, root_hwnd, window_owner
                ),
                root_owned_by_candidate=owner_chain_contains(
                    root_hwnd, handle, window_owner
                ),
                extended_style=exstyle,
            ) or explicitly_associated or is_quicker_float
            rect = window_rect_from_handle(handle)
            if overlay_window_is_candidate(
                root_rect,
                rect,
                visible=bool(user32.IsWindowVisible(handle)),
                minimized=bool(user32.IsIconic(handle)),
                cloaked=window_is_cloaked(handle),
                related=related,
                explicitly_associated=explicitly_associated,
                trusted_small_overlay=(
                    is_quicker_float and title == "FloatButtonWindow"
                ),
            ):
                associated.append((handle, rect))
            return True

        user32.EnumWindows(collect, 0)
        return tuple(associated)

    def capture_window_rgb(
        window_rect: Rect,
        max_width: int = 720,
    ) -> Optional[tuple[bytes, int, int, int]]:
        width = window_rect.width
        height = window_rect.height
        if width <= 0 or height <= 0:
            return None
        screen_dc = user32.GetDC(None)
        if not screen_dc:
            return None
        memory_dc = gdi32.CreateCompatibleDC(screen_dc)
        bitmap = gdi32.CreateCompatibleBitmap(screen_dc, width, height)
        old_bitmap = None
        try:
            if not memory_dc or not bitmap:
                return None
            old_bitmap = gdi32.SelectObject(memory_dc, bitmap)
            if not gdi32.BitBlt(
                memory_dc,
                0,
                0,
                width,
                height,
                screen_dc,
                window_rect.left,
                window_rect.top,
                srccopy,
            ):
                return None
            source_stride = width * 4
            source = (ctypes.c_ubyte * (source_stride * height))()
            bitmap_info = BitmapInfo()
            bitmap_info.header.size = ctypes.sizeof(BitmapInfoHeader)
            bitmap_info.header.width = width
            bitmap_info.header.height = -height
            bitmap_info.header.planes = 1
            bitmap_info.header.bit_count = 32
            bitmap_info.header.compression = bi_rgb
            if not gdi32.GetDIBits(
                memory_dc,
                bitmap,
                0,
                height,
                source,
                ctypes.byref(bitmap_info),
                dib_rgb_colors,
            ):
                return None
            step = max(1, (width + max_width - 1) // max_width)
            sampled_width = (width + step - 1) // step
            sampled_height = (height + step - 1) // step
            sampled_stride = sampled_width * 3
            sampled = bytearray(sampled_stride * sampled_height)
            for sampled_y in range(sampled_height):
                source_y = min(height - 1, sampled_y * step)
                source_row = source_y * source_stride
                target_row = sampled_y * sampled_stride
                for sampled_x in range(sampled_width):
                    source_x = min(width - 1, sampled_x * step)
                    source_offset = source_row + source_x * 4
                    target_offset = target_row + sampled_x * 3
                    sampled[target_offset] = source[source_offset + 2]
                    sampled[target_offset + 1] = source[source_offset + 1]
                    sampled[target_offset + 2] = source[source_offset]
            return bytes(sampled), sampled_width, sampled_height, sampled_stride
        finally:
            if old_bitmap and memory_dc:
                gdi32.SelectObject(memory_dc, old_bitmap)
            if bitmap:
                gdi32.DeleteObject(bitmap)
            if memory_dc:
                gdi32.DeleteDC(memory_dc)
            user32.ReleaseDC(None, screen_dc)

    def native_menu_mode_active() -> bool:
        info = GuiThreadInfo()
        info.cbSize = ctypes.sizeof(info)
        return bool(
            user32.GetGUIThreadInfo(0, ctypes.byref(info))
            and int(info.flags) & gui_menu_mode_flags
        )

    def activate_embedded_chromium_accessibility(
        hwnd: int,
        deadline: Optional[float] = None,
        should_cancel: Optional[Callable[[], bool]] = None,
    ) -> bool:
        """Ask Chromium renderers to publish their UI Automation tree."""

        handles = [hwnd]

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def collect_child(child: int, _lparam: int) -> bool:
            handles.append(child)
            return True

        user32.EnumChildWindows(hwnd, collect_child, 0)
        renderer_handles = {
            handle
            for handle in handles
            if window_class_name(handle) == CHROMIUM_RENDERER_CLASS
        }
        if not renderer_handles:
            return False
        if hwnd in awakened_chromium_windows:
            return True

        probe_completed = True
        renderer_probe_succeeded = False
        for handle in handles:
            for object_id in accessibility_object_ids:
                if scan_should_stop(deadline, should_cancel):
                    probe_completed = False
                    break
                timeout_ms = bounded_scan_timeout_ms(deadline, 100)
                if timeout_ms <= 0:
                    probe_completed = False
                    break
                result = ctypes.c_size_t()
                succeeded = bool(
                    user32.SendMessageTimeoutW(
                        handle,
                        wm_getobject,
                        0,
                        object_id,
                        smto_abortifhung,
                        timeout_ms,
                        ctypes.byref(result),
                    )
                )
                if (
                    succeeded
                    and result.value != 0
                    and handle in renderer_handles
                ):
                    renderer_probe_succeeded = True
            if scan_should_stop(deadline, should_cancel):
                probe_completed = False
                break

        # Chromium enables renderer accessibility asynchronously after the
        # probe. A short bounded wait keeps the first scan from racing it.
        wait_seconds = 0.15
        if deadline is not None:
            wait_seconds = min(
                wait_seconds,
                max(0.0, deadline - time.perf_counter()),
            )
        if (
            renderer_probe_succeeded
            and wait_seconds > 0
            and not scan_should_stop(None, should_cancel)
        ):
            time.sleep(wait_seconds)
        if probe_completed and renderer_probe_succeeded:
            awakened_chromium_windows.add(hwnd)
        return True

    def normalize_runtime_targets(
        targets: Sequence[RuntimeTarget],
    ) -> list[RuntimeTarget]:
        by_rect: dict[Rect, RuntimeTarget] = {}
        for target in targets:
            rect = target.snapshot.rect
            existing = by_rect.get(rect)
            if existing is None or target_quality_rank(
                target.snapshot
            ) > target_quality_rank(existing.snapshot) or (
                target_quality_rank(target.snapshot)
                == target_quality_rank(existing.snapshot)
                and not existing.snapshot.name
                and bool(target.snapshot.name)
            ):
                by_rect[rect] = target
        normalized = list(by_rect.values())
        snapshots = [target.snapshot for target in normalized]
        normalized = [
            normalized[index]
            for index in nested_container_keep_indices(snapshots)
        ]
        normalized.sort(
            key=lambda item: (
                item.snapshot.rect.top,
                item.snapshot.rect.left,
                item.snapshot.rect.width * item.snapshot.rect.height,
            )
        )
        return normalized

    def collect_targets(
        root: Any,
        window_rect: Rect,
        root_path: tuple[int, ...],
        root_depth: int,
        max_relative_depth: int,
        deadline: Optional[float] = None,
        should_cancel: Optional[Callable[[], bool]] = None,
    ) -> tuple[
        list[RuntimeTarget],
        dict[tuple[int, ...], str],
        list[ElementSnapshot],
        int,
        bool,
    ]:
        pending = deque([(root, 0, root_path)])
        by_rect: dict[Rect, RuntimeTarget] = {}
        node_types: dict[tuple[int, ...], str] = {}
        node_rects: dict[tuple[int, ...], Rect] = {}
        split_controls_by_path: dict[tuple[int, ...], Any] = {}
        elements: list[ElementSnapshot] = []
        visited = 0
        interrupted = False

        while pending and visited < args.max_nodes and len(by_rect) < args.max_elements:
            if scan_should_stop(deadline, should_cancel):
                interrupted = True
                break
            control, relative_depth, path = pending.popleft()
            visited += 1
            try:
                control_type = str(control.ControlTypeName or "")
                control_rect = rect_from_control(control)
                name = str(control.Name or "").strip()
                automation_id = str(control.AutomationId or "").strip()
                enabled = bool(control.IsEnabled)
                offscreen = bool(control.IsOffscreen)
                keyboard_focusable = bool(control.IsKeyboardFocusable)
                repeated_content_action = False
                if (
                    enabled
                    and not offscreen
                    and control_rect.width >= 24
                    and control_rect.height >= 24
                    and control_rect.intersects(window_rect)
                    and control_type in REPEATED_CONTENT_ITEM_TYPES
                ):
                    repeated_content_action = control_supports_pattern(
                        control,
                        auto.PatternId.InvokePattern,
                    )
                    if (
                        not repeated_content_action
                        and control_type
                        in {"DataItemControl", "ListItemControl"}
                    ):
                        repeated_content_action = control_supports_pattern(
                            control,
                            auto.PatternId.SelectionItemPattern,
                        )
                elements.append(
                    ElementSnapshot(
                        rect=control_rect,
                        name=name,
                        control_type=control_type,
                        automation_id=automation_id,
                        path=path,
                        enabled=enabled,
                        offscreen=offscreen,
                        keyboard_focusable=keyboard_focusable,
                        has_direct_action_pattern=repeated_content_action,
                        has_legacy_pattern=(
                            control_type in VISUAL_SURFACE_CONTROL_TYPES
                            and control_supports_pattern(
                                control, auto.PatternId.LegacyIAccessiblePattern
                            )
                        ),
                        has_scroll_pattern=(
                            control_type in VISUAL_SURFACE_CONTROL_TYPES
                            and control_supports_pattern(
                                control, auto.PatternId.ScrollPattern
                            )
                        ),
                    )
                )
                if path:
                    node_types[path] = control_type
                    node_rects[path] = control_rect
                    if (
                        control_type == "GroupControl"
                        and not name
                        and not automation_id
                        and enabled
                        and not offscreen
                        and repeated_content_action
                        and 16 <= control_rect.width <= SPLIT_COMPANION_MAX_WIDTH
                        and 24 <= control_rect.height <= SPLIT_COMPANION_MAX_HEIGHT
                    ):
                        split_controls_by_path[path] = control
                if relative_depth > 0:
                    section_path = infer_navigation_section_path(
                        path,
                        node_rects,
                        window_rect,
                        node_types,
                    )
                    candidate = runtime_target_from_control(
                        control,
                        window_rect,
                        path=path,
                        depth=root_depth + relative_depth,
                        section_path=section_path,
                        section_rect=node_rects.get(section_path),
                        precomputed_rect=control_rect,
                    )
                    if candidate is not None:
                        rect = candidate.snapshot.rect
                        existing = by_rect.get(rect)
                        if existing is None or target_quality_rank(
                            candidate.snapshot
                        ) > target_quality_rank(existing.snapshot) or (
                            target_quality_rank(candidate.snapshot)
                            == target_quality_rank(existing.snapshot)
                            and not existing.snapshot.name
                            and bool(candidate.snapshot.name)
                        ):
                            by_rect[rect] = candidate
            except Exception:
                pass

            if relative_depth >= max_relative_depth:
                continue
            try:
                for child_index, child in enumerate(control.GetChildren()):
                    pending.append(
                        (child, relative_depth + 1, path + (child_index,))
                    )
            except Exception:
                continue

        targets = normalize_runtime_targets(list(by_rect.values()))
        for spec in repeated_content_target_specs(elements, window_rect):
            targets.append(RuntimeTarget(spec.snapshot, None, spec.click_point))
        for spec in split_button_companion_target_specs(elements, window_rect):
            snapshot = spec.snapshot
            section_path = infer_navigation_section_path(
                snapshot.path,
                node_rects,
                window_rect,
                node_types,
            )
            targets.append(
                RuntimeTarget(
                    replace(
                        snapshot,
                        depth=root_depth + len(snapshot.path) - len(root_path),
                        section_path=section_path,
                        section_rect=node_rects.get(section_path),
                    ),
                    split_controls_by_path.get(snapshot.path),
                    spec.click_point,
                )
            )
        targets = normalize_runtime_targets(targets)[: args.max_elements]
        return targets, node_types, elements, visited, interrupted

    def enumerate_window_targets(
        hwnd: int,
        deadline: Optional[float] = None,
        should_cancel: Optional[Callable[[], bool]] = None,
    ) -> tuple[
        list[RuntimeTarget],
        dict[tuple[int, ...], str],
        Rect,
        str,
        int,
        bool,
    ]:
        has_chromium_renderer = activate_embedded_chromium_accessibility(
            hwnd,
            deadline=deadline,
            should_cancel=should_cancel,
        )
        if scan_should_stop(deadline, should_cancel):
            return (
                [],
                {},
                Rect(0, 0, 0, 0),
                "未命名窗口",
                0,
                True,
            )
        scan_depth = effective_scan_depth(args.max_depth, has_chromium_renderer)
        root = auto.ControlFromHandle(hwnd)
        if root is None:
            raise RuntimeError("无法从当前窗口建立 UI Automation 根元素")
        window_rect = rect_from_control(root)
        window_name = str(root.Name or "未命名窗口")
        targets, node_types, elements, visited, interrupted = collect_targets(
            root,
            window_rect,
            (),
            0,
            scan_depth,
            deadline=deadline,
            should_cancel=should_cancel,
        )
        surfaces = opaque_visual_surfaces(
            elements,
            [target.snapshot for target in targets],
            window_rect,
        )
        can_capture_visuals = bool(
            surfaces
            and not interrupted
            and not scan_should_stop(deadline, should_cancel)
            and (
                deadline is None
                or deadline - time.perf_counter() >= 0.20
            )
        )
        if can_capture_visuals:
            capture = capture_window_rgb(window_rect)
            if capture is not None:
                rgb, image_width, image_height, bytes_per_line = capture
                for spec in visual_grid_target_specs(
                    rgb,
                    image_width,
                    image_height,
                    bytes_per_line,
                    window_rect,
                    surfaces,
                ):
                    targets.append(RuntimeTarget(spec.snapshot, None, spec.click_point))
                targets = normalize_runtime_targets(targets)[: args.max_elements]
        return (
            targets,
            node_types,
            window_rect,
            window_name,
            visited,
            interrupted,
        )

    def enumerate_targets(
        hwnd: int,
        deadline: Optional[float] = None,
        should_cancel: Optional[Callable[[], bool]] = None,
    ) -> tuple[
        list[RuntimeTarget],
        dict[tuple[int, ...], str],
        Rect,
        str,
        int,
        bool,
    ]:
        (
            root_targets,
            root_node_types,
            root_rect,
            root_name,
            visited,
            interrupted,
        ) = enumerate_window_targets(
            hwnd,
            deadline=deadline,
            should_cancel=should_cancel,
        )
        overlay_targets: list[RuntimeTarget] = []
        node_types = dict(root_node_types)
        overlay_signature = associated_overlay_window_signature(
            hwnd,
            root_rect,
            excluded_process_id=int(kernel32.GetCurrentProcessId()),
        )
        for overlay_index, (overlay_hwnd, _overlay_rect) in enumerate(
            overlay_signature
        ):
            if scan_should_stop(deadline, should_cancel):
                interrupted = True
                break
            try:
                (
                    targets,
                    overlay_node_types,
                    _rect,
                    _name,
                    overlay_visited,
                    overlay_interrupted,
                ) = enumerate_window_targets(
                    overlay_hwnd,
                    deadline=deadline,
                    should_cancel=should_cancel,
                )
            except Exception:
                continue
            if not targets:
                root_spec = root_only_overlay_target_spec(
                    _overlay_rect,
                    _name,
                )
                if root_spec is not None:
                    targets = [
                        RuntimeTarget(
                            root_spec.snapshot,
                            None,
                            root_spec.click_point,
                        )
                    ]
            scope = (2_000_000 + overlay_index,)
            for target in targets:
                snapshot = target.snapshot
                scoped_path = scope + snapshot.path
                scoped_section = (
                    scope + snapshot.section_path
                    if snapshot.section_path
                    else scope
                )
                target.snapshot = replace(
                    snapshot,
                    path=scoped_path,
                    section_path=scoped_section,
                    source=f"{snapshot.source}-overlay",
                )
                overlay_targets.append(target)
            node_types.update(
                {scope + path: control_type for path, control_type in overlay_node_types.items()}
            )
            visited += overlay_visited
            interrupted = interrupted or overlay_interrupted

        root_limit = max(0, args.max_elements - len(overlay_targets))
        combined = normalize_runtime_targets(
            [*root_targets[:root_limit], *overlay_targets[: args.max_elements]]
        )[: args.max_elements]
        return (
            combined,
            node_types,
            root_rect,
            root_name,
            visited,
            interrupted,
        )

    def focused_rect() -> Optional[Rect]:
        try:
            focused = auto.GetFocusedControl()
            return rect_from_control(focused) if focused is not None else None
        except Exception:
            return None

    def cursor_point() -> Optional[tuple[int, int]]:
        point = wintypes.POINT()
        if not user32.GetCursorPos(ctypes.byref(point)):
            return None
        return int(point.x), int(point.y)

    def point_hierarchy_targets(
        point: tuple[int, int],
        window_rect: Rect,
        existing_targets: Sequence[RuntimeTarget],
    ) -> list[RuntimeTarget]:
        """Return actionable UIA ancestors under the point, leaf first."""

        hierarchy: list[RuntimeTarget] = []
        seen_runtime_ids: set[tuple[int, ...]] = set()
        seen_geometry: set[tuple[Rect, str]] = set()
        saw_rejected_legacy_content = False
        try:
            control = auto.ControlFromPoint(point[0], point[1])
        except Exception:
            control = None

        for depth in range(40):
            if control is None:
                break
            try:
                control_type = str(control.ControlTypeName or "")
                if control_type in LEGACY_ONLY_WEAK_CONTROL_TYPES:
                    keyboard_focusable = bool(control.IsKeyboardFocusable)
                    (
                        action_pattern,
                        direct_action_pattern,
                        _supports_expand,
                    ) = action_pattern_support(control)
                    if not standard_control_has_actionable_semantics(
                        control_type,
                        keyboard_focusable,
                        action_pattern,
                        direct_action_pattern,
                    ):
                        saw_rejected_legacy_content = True
            except Exception:
                pass
            candidate = runtime_target_from_control(
                control,
                window_rect,
                depth=depth,
                source="uia-point",
            )
            if candidate is not None:
                match = hit_target_match_index(
                    [target.snapshot for target in existing_targets],
                    candidate.snapshot.rect,
                    candidate.snapshot.runtime_id,
                )
                if match >= 0:
                    candidate = existing_targets[match]
                identity = candidate.snapshot.runtime_id
                geometry = (
                    candidate.snapshot.rect,
                    candidate.snapshot.control_type,
                )
                if (
                    (not identity or identity not in seen_runtime_ids)
                    and geometry not in seen_geometry
                ):
                    hierarchy.append(candidate)
                    if identity:
                        seen_runtime_ids.add(identity)
                    seen_geometry.add(geometry)
            try:
                control = control.GetParentControl()
            except Exception:
                break

        if hierarchy:
            return hierarchy
        if saw_rejected_legacy_content:
            return []

        msaa_rect = msaa_rect_at_point(point)
        if msaa_rect is None or not msaa_rect.intersects(window_rect):
            return []
        snapshots = [target.snapshot for target in existing_targets]
        if msaa_wrapper_should_be_ignored(
            msaa_rect,
            window_rect,
            snapshots,
        ):
            return []
        match = hit_target_match_index(
            snapshots, msaa_rect
        )
        if match >= 0:
            return [existing_targets[match]]
        if msaa_rect.width < 16 or msaa_rect.height < 16:
            return []
        return [
            RuntimeTarget(
                TargetSnapshot(
                    rect=msaa_rect,
                    name="MSAA 元素",
                    control_type="LegacyControl",
                    has_action_pattern=True,
                    source="msaa",
                ),
                None,
            )
        ]

    def try_semantic_invoke(target: RuntimeTarget) -> Optional[str]:
        control = target.control
        if control is None:
            return None
        attempts: tuple[tuple[int, str, str], ...] = (
            (auto.PatternId.InvokePattern, "Invoke", "Invoke"),
            (auto.PatternId.TogglePattern, "Toggle", "Toggle"),
            (auto.PatternId.SelectionItemPattern, "Select", "Select"),
            (auto.PatternId.ExpandCollapsePattern, "Expand", "Expand"),
            (
                auto.PatternId.LegacyIAccessiblePattern,
                "DoDefaultAction",
                "Legacy default action",
            ),
        )
        for pattern_id, method_name, label in attempts:
            try:
                pattern = control.GetPattern(pattern_id)
                if pattern is None:
                    continue
                method = getattr(pattern, method_name)
                result = method(waitTime=0) if method_name != "DoDefaultAction" else method()
                if result is not False:
                    return label
            except Exception:
                continue
        return None

    class AutomationWorker:
        _PENDING_LIMITS = {
            "scan": 1,
            "prewarm": 1,
            "move": 2,
            "parent": 1,
            "child": 1,
            "activate": 2,
            "context": 1,
            "scroll_up": 2,
            "scroll_down": 2,
            "back": 1,
            "sync_window": 1,
            "refresh_content": 1,
            "follow_window": 1,
        }
        _NAVIGATION_COMMANDS = frozenset(
            {
                "move",
                "parent",
                "child",
                "activate",
                "context",
                "scroll_up",
                "scroll_down",
                "back",
                "sync_window",
                "refresh_content",
                "follow_window",
            }
        )
        _REFRESH_INTERRUPT_COMMANDS = frozenset(
            {
                "scan",
                "move",
                "parent",
                "child",
                "activate",
                "context",
                "scroll_up",
                "scroll_down",
                "back",
                "follow_window",
                "stop",
            }
        )
        _CACHE_TTL_SECONDS = 15.0
        _PREWARM_BUDGET_SECONDS = 1.5
        _SCROLL_BURST_SECONDS = 0.35
        _IDLE_REFRESH_POLL_SECONDS = 0.05

        def __init__(self, diagnostics_enabled: bool = False) -> None:
            self.commands: queue.Queue[tuple[str, Any, int]] = queue.Queue()
            self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
            self._post_lock = threading.Lock()
            self._scan_requested = threading.Event()
            self._refresh_cancel_requested = threading.Event()
            self._pending_refresh_interrupts = 0
            self._last_refresh_interrupt_at = 0.0
            self._refresh_interrupt_generation = 0
            self._generation = 0
            self._pending_counts: dict[str, int] = {}
            self.context_valid = False
            self.all_targets: list[RuntimeTarget] = []
            self.targets: list[RuntimeTarget] = []
            self.navigation_graph = NavigationGraph(())
            self.traversal = NavigationTraversal()
            self.node_types: dict[tuple[int, ...], str] = {}
            self.hierarchy: list[RuntimeTarget] = []
            self.hierarchy_index = -1
            self.invalid_targets: set[tuple[Any, ...]] = set()
            self.selected = -1
            self.hwnd = 0
            self.window_rect = Rect(0, 0, 0, 0)
            self.window_name = ""
            self.visited = 0
            self.cache_timestamp = 0.0
            self._background_refresh_requested = False
            self._background_refresh_retry_at = 0.0
            self._pending_follow_completion_hwnd = 0
            self._empty_follow_refresh_attempts = 0
            self._deferred_moves: deque[Direction] = deque(
                maxlen=self._PENDING_LIMITS["move"]
            )
            self._pointer_cache_token: Optional[tuple[Any, ...]] = None
            self._pointer_cache_point: Optional[tuple[int, int]] = None
            self._pointer_cache_at = 0.0
            self._scroll_cache_token: Optional[tuple[Any, ...]] = None
            self._scroll_cache_point: Optional[tuple[int, int]] = None
            self._scroll_cache_at = 0.0
            self._content_settle_until = 0.0
            self.diagnostics_enabled = diagnostics_enabled
            self._double_click_seconds = max(
                0.2, int(user32.GetDoubleClickTime()) / 1000
            )
            self._thread = threading.Thread(
                target=self._run,
                name="element-navigation-uia",
                daemon=True,
            )

        def start(self) -> None:
            self._thread.start()

        def post(self, command: str, value: Any = None) -> None:
            with self._post_lock:
                limit = self._PENDING_LIMITS.get(command)
                if limit is not None:
                    pending = self._pending_counts.get(command, 0)
                    if pending >= limit:
                        if command in self._REFRESH_INTERRUPT_COMMANDS:
                            self._refresh_interrupt_generation += 1
                            self._last_refresh_interrupt_at = time.perf_counter()
                            self._refresh_cancel_requested.set()
                        return
                    self._pending_counts[command] = pending + 1
                if command == "scan":
                    self._scan_requested.set()
                if command in self._REFRESH_INTERRUPT_COMMANDS:
                    self._refresh_interrupt_generation += 1
                    self._pending_refresh_interrupts += 1
                    self._last_refresh_interrupt_at = time.perf_counter()
                    self._refresh_cancel_requested.set()
                generation = self._generation
            self.commands.put((command, value, generation))

        def deactivate(self) -> None:
            with self._post_lock:
                self._generation += 1
                self.context_valid = False
                self._pending_counts["scan"] = 0
                self._pending_follow_completion_hwnd = 0
                self._empty_follow_refresh_attempts = 0
                self._deferred_moves.clear()
                self._refresh_cancel_requested.set()

        def _finish_refresh_interrupt(self, command: str) -> None:
            if command not in self._REFRESH_INTERRUPT_COMMANDS:
                return
            with self._post_lock:
                self._pending_refresh_interrupts = max(
                    0, self._pending_refresh_interrupts - 1
                )
                self._last_refresh_interrupt_at = time.perf_counter()
                if self._pending_refresh_interrupts == 0 and self.context_valid:
                    self._refresh_cancel_requested.clear()

        def stop(self) -> None:
            self.post("stop")
            self._thread.join(timeout=2)

        @staticmethod
        def _same_identity(first: TargetSnapshot, second: TargetSnapshot) -> bool:
            return same_target_identity(first, second)

        @staticmethod
        def _identity_token(target: TargetSnapshot) -> tuple[Any, ...]:
            if target.runtime_id:
                return ("runtime", target.runtime_id)
            return (
                "fallback",
                target.control_type,
                target.automation_id,
                target.name,
                target.rect,
            )

        def _merge_all_target(self, target: RuntimeTarget) -> RuntimeTarget:
            for existing in self.all_targets:
                if self._same_identity(existing.snapshot, target.snapshot):
                    return existing
            self.all_targets.append(target)
            return target

        def _select_target(self, target: RuntimeTarget) -> None:
            for index, existing in enumerate(self.targets):
                if self._same_identity(existing.snapshot, target.snapshot):
                    self.selected = index
                    return
            self.targets.append(target)
            self.selected = len(self.targets) - 1
            self._rebuild_navigation_graph()

        def _rebuild_navigation_graph(self) -> None:
            self.navigation_graph = NavigationGraph(
                [target.snapshot for target in self.targets]
            )
            self.traversal.reset()

        def _set_hierarchy(
            self,
            hierarchy: Sequence[RuntimeTarget],
            current: Optional[RuntimeTarget] = None,
        ) -> None:
            merged: list[RuntimeTarget] = []
            for target in hierarchy:
                target = self._merge_all_target(target)
                if not any(
                    self._same_identity(existing.snapshot, target.snapshot)
                    for existing in merged
                ):
                    merged.append(target)
            self.hierarchy = merged
            self.hierarchy_index = -1
            if current is not None:
                for index, target in enumerate(self.hierarchy):
                    if self._same_identity(target.snapshot, current.snapshot):
                        self.hierarchy_index = index
                        break
            if self.hierarchy_index < 0 and self.hierarchy:
                self.hierarchy_index = 0

        def _reset_hierarchy_for_selected(self) -> None:
            if not self.targets or not 0 <= self.selected < len(self.targets):
                self.hierarchy = []
                self.hierarchy_index = -1
                return
            current = self.targets[self.selected]
            center = (
                round(current.snapshot.rect.center_x),
                round(current.snapshot.rect.center_y),
            )
            hierarchy = point_hierarchy_targets(
                center, self.window_rect, self.all_targets
            )
            if not any(
                self._same_identity(target.snapshot, current.snapshot)
                for target in hierarchy
            ):
                hierarchy.insert(0, current)
            self._set_hierarchy(hierarchy, current)

        def _clear_hierarchy(self) -> None:
            self.hierarchy = []
            self.hierarchy_index = -1

        def _clear_input_cache(self) -> None:
            self._pointer_cache_token = None
            self._pointer_cache_point = None
            self._pointer_cache_at = 0.0
            self._scroll_cache_token = None
            self._scroll_cache_point = None
            self._scroll_cache_at = 0.0
            self._content_settle_until = 0.0

        def _remember_pointer_point(
            self, target: RuntimeTarget, point: tuple[int, int]
        ) -> None:
            self._pointer_cache_token = self._identity_token(target.snapshot)
            self._pointer_cache_point = point
            self._pointer_cache_at = time.perf_counter()

        def _cached_pointer_point(
            self, target: RuntimeTarget
        ) -> Optional[tuple[int, int]]:
            if (
                self._pointer_cache_point is not None
                and self._pointer_cache_token
                == self._identity_token(target.snapshot)
                and time.perf_counter() - self._pointer_cache_at
                <= self._double_click_seconds
            ):
                return self._pointer_cache_point
            return None

        def _remember_scroll_point(
            self, target: RuntimeTarget, point: tuple[int, int]
        ) -> None:
            now = time.perf_counter()
            self._scroll_cache_token = self._identity_token(target.snapshot)
            self._scroll_cache_point = point
            self._scroll_cache_at = now
            self._content_settle_until = now + self._SCROLL_BURST_SECONDS

        def _cached_scroll_point(
            self, target: RuntimeTarget
        ) -> Optional[tuple[int, int]]:
            if (
                self._scroll_cache_point is not None
                and self._scroll_cache_token == self._identity_token(target.snapshot)
                and time.perf_counter() - self._scroll_cache_at
                <= self._SCROLL_BURST_SECONDS
            ):
                return self._scroll_cache_point
            return None

        def _cycle_hierarchy(self, delta: int) -> None:
            if not self.targets or not 0 <= self.selected < len(self.targets):
                return
            current = self.targets[self.selected]
            if not self.hierarchy or not 0 <= self.hierarchy_index < len(
                self.hierarchy
            ) or not self._same_identity(
                self.hierarchy[self.hierarchy_index].snapshot, current.snapshot
            ):
                self._reset_hierarchy_for_selected()
            if not self.hierarchy:
                return
            next_index = max(
                0, min(len(self.hierarchy) - 1, self.hierarchy_index + delta)
            )
            if next_index == self.hierarchy_index:
                return
            self.hierarchy_index = next_index
            self._select_target(self.hierarchy[next_index])
            self.traversal.reset()
            self._emit_selection()

        def _target_is_exposed(
            self,
            target: RuntimeTarget,
            allow_semantic_bypass: bool = True,
        ) -> bool:
            if target.control is None:
                return True
            target.click_point = None
            try:
                if not self._update_live_target(target):
                    return False
                live_rect = target.snapshot.rect
                snapshots = [item.snapshot for item in self.targets]
                finer_targets = [
                    snapshot
                    for snapshot in snapshots
                    if snapshot is not target.snapshot
                    and target_is_action_descendant(target.snapshot, snapshot)
                ]
                finer_runtime_ids = {
                    snapshot.runtime_id
                    for snapshot in finer_targets
                    if snapshot.runtime_id
                }
                finer_named_geometry = {
                    (snapshot.rect, snapshot.control_type, snapshot.name)
                    for snapshot in finer_targets
                    if snapshot.name
                }
                finer_unnamed_geometry = {
                    (snapshot.rect, snapshot.control_type)
                    for snapshot in finer_targets
                    if not snapshot.name
                }

                for point in available_target_probe_points(
                    target.snapshot, snapshots
                ):
                    control = auto.ControlFromPoint(point[0], point[1])
                    intercepted_by_finer_target = False
                    for _depth in range(40):
                        if control is None:
                            break
                        runtime_id = runtime_id_from_control(control)
                        matches_target = bool(
                            target.snapshot.runtime_id
                            and runtime_id == target.snapshot.runtime_id
                        )
                        try:
                            control_type = str(control.ControlTypeName or "")
                            name = str(control.Name or "").strip()
                            rect = rect_from_control(control)
                        except Exception:
                            control_type = ""
                            name = ""
                            rect = Rect(0, 0, 0, 0)
                        if not matches_target and (
                            not target.snapshot.runtime_id
                            and control_type == target.snapshot.control_type
                            and rect == live_rect
                            and (not target.snapshot.name or name == target.snapshot.name)
                        ):
                            matches_target = True
                        if matches_target:
                            if intercepted_by_finer_target:
                                break
                            target.click_point = point
                            return True
                        if (
                            (runtime_id and runtime_id in finer_runtime_ids)
                            or (
                                name
                                and (rect, control_type, name)
                                in finer_named_geometry
                            )
                            or (
                                not name
                                and (rect, control_type)
                                in finer_unnamed_geometry
                            )
                        ):
                            intercepted_by_finer_target = True
                        control = control.GetParentControl()
            except Exception:
                return False
            return bool(
                allow_semantic_bypass
                and semantic_action_can_bypass_point_hit(target.snapshot)
            )

        def _update_live_target(self, target: RuntimeTarget) -> bool:
            if target.control is None:
                return True
            try:
                if not bool(target.control.IsEnabled) or bool(target.control.IsOffscreen):
                    return False
                live_rect = rect_from_control(target.control)
                if (
                    live_rect.width < 16
                    or live_rect.height < 16
                    or not live_rect.intersects(self.window_rect)
                ):
                    return False
                if live_rect != target.snapshot.rect:
                    target.snapshot = replace(target.snapshot, rect=live_rect)
                return True
            except Exception:
                return False

        def _target_is_navigable(self, target: RuntimeTarget) -> bool:
            if semantic_action_can_bypass_point_hit(target.snapshot):
                return self._update_live_target(target)
            return self._target_is_exposed(target)

        def _invalidate_navigation(self, reason: str) -> None:
            if not self.context_valid:
                return
            self.context_valid = False
            self.events.put(("navigation_invalidated", reason))

        def _content_geometry_is_current(self) -> bool:
            for index in geometry_anchor_indices(len(self.targets), self.selected):
                target = self.targets[index]
                if target.control is None:
                    continue
                try:
                    if bool(target.control.IsOffscreen):
                        return False
                    if rect_from_control(target.control) != target.snapshot.rect:
                        return False
                except Exception:
                    return False
            return True

        def _request_background_refresh(self) -> None:
            self._background_refresh_requested = True

        def _refresh_selected_geometry(self) -> bool:
            if not self.targets or not 0 <= self.selected < len(self.targets):
                return False
            target = self.targets[self.selected]
            previous_rect = target.snapshot.rect
            if not self._update_live_target(target):
                return False
            if target.snapshot.rect != previous_rect:
                self._rebuild_navigation_graph()
                self._clear_hierarchy()
                self._emit_selection()
            return True

        def _sync_window_geometry(self, allow_full_rescan: bool = False) -> bool:
            with self._post_lock:
                expected_generation = self._generation
                if not self.context_valid or not self.hwnd or not self.targets:
                    return False
            try:
                root = auto.ControlFromHandle(self.hwnd)
                if root is None:
                    self._invalidate_navigation("目标窗口已经关闭")
                    return False
                current_window_rect = rect_from_control(root)
            except Exception:
                self._invalidate_navigation("无法继续读取目标窗口")
                return False
            if current_window_rect == self.window_rect:
                if not self._content_geometry_is_current():
                    if not allow_full_rescan:
                        self._request_background_refresh()
                        return self._refresh_selected_geometry()
                    previous = (
                        self.targets[self.selected].snapshot
                        if 0 <= self.selected < len(self.targets)
                        else None
                    )
                    committed, _partial, _empty = self._enumerate(
                        self.hwnd,
                        expected_generation=expected_generation,
                    )
                    if not committed:
                        return False
                    self._apply_targets(restore=previous)
                    self._clear_hierarchy()
                    self._clear_input_cache()
                    self.events.put(("geometry_rescanned", None))
                    self._emit_selection()
                    return bool(self.targets and self.selected >= 0)
                return True

            if (
                current_window_rect.width == self.window_rect.width
                and current_window_rect.height == self.window_rect.height
            ):
                delta_x = current_window_rect.left - self.window_rect.left
                delta_y = current_window_rect.top - self.window_rect.top
                for target in self.all_targets:
                    target.snapshot = shifted_snapshot(
                        target.snapshot, delta_x, delta_y
                    )
                    if target.click_point is not None:
                        target.click_point = shifted_point(
                            target.click_point,
                            delta_x,
                            delta_y,
                        )
                self.window_rect = current_window_rect
                self.invalid_targets.clear()
                self._clear_input_cache()
                self.events.put(
                    (
                        "geometry_synced",
                        {"delta_x": delta_x, "delta_y": delta_y},
                    )
                )
                self._emit_selection()
                return True

            if not allow_full_rescan:
                self.window_rect = current_window_rect
                self.invalid_targets.clear()
                self._clear_input_cache()
                self._request_background_refresh()
                return self._refresh_selected_geometry()

            previous = (
                self.targets[self.selected].snapshot
                if 0 <= self.selected < len(self.targets)
                else None
            )
            committed, _partial, _empty = self._enumerate(
                self.hwnd,
                expected_generation=expected_generation,
            )
            if not committed:
                return False
            self._apply_targets(restore=previous)
            self._clear_hierarchy()
            self._clear_input_cache()
            self.events.put(("geometry_rescanned", None))
            self._emit_selection()
            return bool(self.targets and self.selected >= 0)

        def _enumerate(
            self,
            hwnd: int,
            activate_context: bool = True,
            deadline: Optional[float] = None,
            should_cancel: Optional[Callable[[], bool]] = None,
            allow_partial: bool = False,
            expected_generation: Optional[int] = None,
            commit_empty: bool = True,
        ) -> tuple[bool, bool, bool]:
            with self._post_lock:
                scan_generation = (
                    self._generation
                    if expected_generation is None
                    else expected_generation
                )
                if scan_generation != self._generation:
                    return False, False, False
            previous_hwnd = self.hwnd
            previous_process_id = (
                window_process_id(previous_hwnd) if previous_hwnd > 0 else 0
            )
            process_id = window_process_id(hwnd)
            dirty_windows.watch(hwnd, process_id)
            dirty_before_scan = dirty_windows.state(hwnd, process_id)
            try:
                result = enumerate_targets(
                    hwnd,
                    deadline=deadline,
                    should_cancel=should_cancel,
                )
            except Exception:
                dirty_windows.watch(previous_hwnd, previous_process_id)
                raise
            interrupted = bool(result[-1])
            empty = not bool(result[0])
            cancellation_requested = bool(
                should_cancel is not None and should_cancel()
            )
            with self._post_lock:
                cancellation_requested = cancellation_requested or bool(
                    should_cancel is not None and should_cancel()
                )
                committed, partial = scan_commit_decision(
                    scan_generation,
                    self._generation,
                    interrupted,
                    cancellation_requested,
                    allow_partial,
                )
                if committed and empty and not commit_empty:
                    committed = False
                    partial = False
                if committed:
                    (
                        self.all_targets,
                        self.node_types,
                        self.window_rect,
                        self.window_name,
                        self.visited,
                        _interrupted,
                    ) = result
                    if previous_hwnd != hwnd:
                        self._pending_follow_completion_hwnd = 0
                        self._empty_follow_refresh_attempts = 0
                        self._deferred_moves.clear()
                    if not partial and dirty_before_scan is not None:
                        dirty_windows.consume(
                            hwnd,
                            process_id,
                            through_generation=dirty_before_scan.generation,
                        )
                    self.hwnd = hwnd
                    self.invalid_targets.clear()
                    self._clear_input_cache()
                    self.context_valid = activate_context
                    self.cache_timestamp = time.perf_counter()
                    self._background_refresh_requested = partial
                    self._background_refresh_retry_at = 0.0
            if not committed:
                dirty_windows.watch(previous_hwnd, previous_process_id)
                return False, False, empty
            return True, partial, empty

        def _cache_is_reusable(self, hwnd: int) -> bool:
            if (
                hwnd != self.hwnd
                or not self.all_targets
                or time.perf_counter() - self.cache_timestamp
                > self._CACHE_TTL_SECONDS
            ):
                return False
            if dirty_windows.state(hwnd, window_process_id(hwnd)) is not None:
                return False
            try:
                root = auto.ControlFromHandle(hwnd)
                if root is None or rect_from_control(root) != self.window_rect:
                    return False
            except Exception:
                return False
            for index in geometry_anchor_indices(
                len(self.all_targets), len(self.all_targets) // 2
            ):
                target = self.all_targets[index]
                if target.control is None:
                    continue
                try:
                    if bool(target.control.IsOffscreen):
                        return False
                    if rect_from_control(target.control) != target.snapshot.rect:
                        return False
                except Exception:
                    return False
            return True

        def _prewarm(self, hwnd: int, expected_generation: int) -> None:
            if hwnd <= 0 or self._cache_is_reusable(hwnd):
                return
            started = time.perf_counter()
            cached, _partial, _empty = self._enumerate(
                hwnd,
                activate_context=False,
                deadline=started + self._PREWARM_BUDGET_SECONDS,
                should_cancel=self._scan_requested.is_set,
                expected_generation=expected_generation,
            )
            if not cached:
                if not self._scan_requested.is_set():
                    self.events.put(
                        (
                            "prewarm_skipped",
                            {"elapsed": time.perf_counter() - started},
                        )
                    )
                return
            self.events.put(
                (
                    "prewarm_done",
                    {
                        "window": self.window_name,
                        "elapsed": time.perf_counter() - started,
                    },
                )
            )

        def _apply_targets(
            self,
            restore: Optional[TargetSnapshot] = None,
            focused: Optional[Rect] = None,
            use_cursor: bool = False,
        ) -> None:
            snapshots = [target.snapshot for target in self.all_targets]
            visible_indices = flat_target_indices(snapshots)
            self.targets = [self.all_targets[index] for index in visible_indices]
            self._rebuild_navigation_graph()
            visible_snapshots = [target.snapshot for target in self.targets]
            if restore is not None:
                self.selected = restore_target_index(visible_snapshots, restore)
            else:
                self.selected = initial_target_index(
                    visible_snapshots,
                    focused,
                    self.window_rect,
                    cursor_point() if use_cursor else None,
                )

        def _refresh_targets(self, interruptible: bool = False) -> bool:
            with self._post_lock:
                expected_generation = self._generation
                if not self.context_valid or not self.hwnd:
                    return False
                retry_empty_follow = empty_follow_refresh_should_retry(
                    self._pending_follow_completion_hwnd,
                    self.hwnd,
                    self._empty_follow_refresh_attempts,
                )
            previous = (
                self.targets[self.selected].snapshot
                if 0 <= self.selected < len(self.targets)
                else None
            )
            committed, _partial, empty = self._enumerate(
                self.hwnd,
                should_cancel=(
                    self._refresh_cancel_requested.is_set
                    if interruptible
                    else None
                ),
                expected_generation=expected_generation,
                commit_empty=not retry_empty_follow,
            )
            if not committed:
                if empty and retry_empty_follow:
                    with self._post_lock:
                        if (
                            expected_generation == self._generation
                            and self.context_valid
                            and not self._refresh_cancel_requested.is_set()
                        ):
                            self._empty_follow_refresh_attempts += 1
                            self._background_refresh_requested = True
                            self._background_refresh_retry_at = (
                                time.perf_counter() + 0.25
                            )
                return False
            self._apply_targets(restore=previous)
            self._clear_hierarchy()
            if not self.targets or self.selected < 0:
                with self._post_lock:
                    self._pending_follow_completion_hwnd = 0
                    self._empty_follow_refresh_attempts = 0
                    self._deferred_moves.clear()
                self._invalidate_navigation("页面变化后没有找到可导航元素")
                return False
            with self._post_lock:
                self._pending_follow_completion_hwnd = 0
                self._empty_follow_refresh_attempts = 0
                deferred_moves = tuple(self._deferred_moves)
                self._deferred_moves.clear()
            self.events.put(("content_refreshed", self._selection_payload()))
            for direction in deferred_moves:
                if not self.context_valid:
                    break
                self._move(direction)
            return True

        def _refresh_if_idle(self) -> None:
            if (
                not self.context_valid
                or not self.hwnd
                or self._refresh_cancel_requested.is_set()
            ):
                return
            now = time.perf_counter()
            if now < self._background_refresh_retry_at:
                return
            process_id = window_process_id(self.hwnd)
            dirty_state = dirty_windows.state(self.hwnd, process_id)
            with self._post_lock:
                input_idle_for = now - self._last_refresh_interrupt_at
            if not background_refresh_due(
                dirty_state,
                now,
                now - self.cache_timestamp,
                self._background_refresh_requested,
                input_idle_for,
            ):
                return
            try:
                refreshed = self._refresh_targets(interruptible=True)
            except Exception as exc:
                self._background_refresh_retry_at = now + 1.0
                self.events.put(("refresh_failed", str(exc)))
                return
            if refreshed:
                self._background_refresh_requested = False
                self._background_refresh_retry_at = 0.0
            elif (
                self.context_valid
                and not self._refresh_cancel_requested.is_set()
            ):
                self._background_refresh_retry_at = now + 0.25

        def _defer_move(
            self,
            direction: Direction,
            expected_generation: int,
        ) -> None:
            with self._post_lock:
                if (
                    expected_generation == self._generation
                    and self.context_valid
                    and self._background_refresh_requested
                ):
                    self._deferred_moves.append(direction)

        def _move(
            self, direction: Direction, allow_geometry_retry: bool = True
        ) -> None:
            if not self._sync_window_geometry():
                return

            def load_candidates() -> tuple[
                int,
                Rect,
                list[TargetSnapshot],
                tuple[int, ...],
                tuple[int, ...],
                tuple[int, ...],
                bool,
                bool,
            ]:
                current = self.selected
                current_snapshots = [target.snapshot for target in self.targets]
                current_cell = self.traversal.current_cell(
                    current,
                    self.navigation_graph.anchor_rects[current],
                    direction,
                )
                plan = navigation_candidate_plan(
                    self.navigation_graph,
                    current, direction, current_cell
                )
                available_candidates = self.traversal.available(
                    current,
                    direction,
                    plan.ranked,
                    allow_previous_fallback=not plan.orthogonal_step_required,
                )
                return (
                    current,
                    current_cell,
                    current_snapshots,
                    plan.natural,
                    plan.ranked,
                    available_candidates,
                    plan.orthogonal_step_required,
                    plan.uses_xy_fallback,
                )

            (
                current_index,
                current_cell,
                snapshots,
                natural,
                ranked,
                candidates,
                orthogonal_step_required,
                uses_xy_fallback,
            ) = load_candidates()
            first_index = candidates[0] if candidates else None
            first_rect = (
                self.targets[first_index].snapshot.rect
                if first_index is not None
                else None
            )
            candidate_is_natural = bool(
                orthogonal_step_required
                or (first_index is not None and first_index in natural)
            )
            process_id = window_process_id(self.hwnd)
            dirty_state = dirty_windows.state(self.hwnd, process_id)
            now = time.perf_counter()
            suspicious_move = bool(
                not orthogonal_step_required
                and (
                    not candidate_is_natural
                    or move_should_refresh_dynamic_targets(
                        snapshots[current_index].rect,
                        first_rect,
                        direction,
                        self.window_rect,
                    )
                )
            )
            fallback_due = dynamic_refresh_fallback_due(
                now - self.cache_timestamp,
                suspicious_move,
            )
            if dirty_state is not None or fallback_due:
                self._request_background_refresh()
            invalid_cached: list[int] = []
            unhittable: list[int] = []

            def emit_diagnostic(
                outcome: str,
                selected_index: Optional[int] = None,
            ) -> None:
                if not self.diagnostics_enabled:
                    return
                diagnostic = build_navigation_diagnostic(
                    snapshots,
                    current_index,
                    direction,
                    current_rect=current_cell,
                    grid_rects=self.navigation_graph.grid_rects,
                    ranked_indices=ranked,
                    available_indices=candidates,
                    invalid_cached_indices=invalid_cached,
                    unhittable_indices=unhittable,
                    selected_index=selected_index,
                    outcome=outcome,
                )
                if diagnostic is not None:
                    self.events.put(("navigation_diagnostic", diagnostic))

            for next_index in candidates:
                target = self.targets[next_index]
                token = self._identity_token(target.snapshot)
                if token in self.invalid_targets:
                    invalid_cached.append(next_index)
                    continue
                previous_rect = target.snapshot.rect
                if not self._target_is_navigable(target):
                    self.invalid_targets.add(token)
                    unhittable.append(next_index)
                    self.events.put(("target_skipped", target.snapshot))
                    continue
                if target.snapshot.rect != previous_rect:
                    emit_diagnostic("geometry_changed")
                    self._request_background_refresh()
                    self._rebuild_navigation_graph()
                    if allow_geometry_retry:
                        self._move(direction, allow_geometry_retry=False)
                    else:
                        self._emit_selection()
                    return
                self.selected = next_index
                self.traversal.commit(
                    next_index,
                    navigation_contact_cell(
                        current_cell,
                        self.navigation_graph.grid_rects[next_index],
                        direction,
                    ),
                )
                self._clear_hierarchy()
                emit_diagnostic(
                    "selected_xy_fallback" if uses_xy_fallback else "selected",
                    next_index,
                )
                self._emit_selection()
                return
            emit_diagnostic(
                "orthogonal_step"
                if orthogonal_step_required
                else "no_candidate"
            )

        def _selection_payload(self) -> dict[str, Any]:
            snapshots = [target.snapshot for target in self.targets]
            return {
                "target": snapshots[self.selected],
                "selected": self.selected,
                "count": len(snapshots),
                "scope_depth": 0,
                "hierarchy_index": self.hierarchy_index,
                "hierarchy_count": len(self.hierarchy),
            }

        def _emit_selection(self) -> None:
            if self.targets and self.selected >= 0:
                self.events.put(("selection", self._selection_payload()))

        def _scan(
            self,
            hwnd: int,
            expected_generation: int,
            scan_token: int,
        ) -> None:
            started = time.perf_counter()
            with self._post_lock:
                if expected_generation != self._generation:
                    self.events.put(
                        ("scan_cancelled", {"scan_token": scan_token})
                    )
                    return
            point = cursor_point()
            process_id = window_process_id(hwnd)
            watcher_changed = dirty_windows.watch(hwnd, process_id)
            used_cache = not watcher_changed and self._cache_is_reusable(hwnd)
            if used_cache:
                with self._post_lock:
                    if expected_generation != self._generation:
                        self.events.put(
                            ("scan_cancelled", {"scan_token": scan_token})
                        )
                        return
                    self.context_valid = True
                    self.invalid_targets.clear()
            else:
                committed, _partial, _empty = self._enumerate(
                    hwnd,
                    should_cancel=lambda: self._generation != expected_generation,
                    expected_generation=expected_generation,
                )
                if not committed:
                    previous_process_id = (
                        window_process_id(self.hwnd) if self.hwnd > 0 else 0
                    )
                    dirty_windows.watch(self.hwnd, previous_process_id)
                    self.events.put(
                        ("scan_cancelled", {"scan_token": scan_token})
                    )
                    return
            hierarchy = (
                point_hierarchy_targets(point, self.window_rect, self.all_targets)
                if point is not None and self.window_rect.contains_point(point)
                else []
            )
            self._set_hierarchy(hierarchy)
            if self.hierarchy:
                self._apply_targets(restore=self.hierarchy[0].snapshot)
                if self.targets and self.selected >= 0:
                    current = self.targets[self.selected]
                    self._set_hierarchy(self.hierarchy, current)
            else:
                self._apply_targets(focused=focused_rect(), use_cursor=True)
                self._reset_hierarchy_for_selected()
            snapshots = [target.snapshot for target in self.targets]
            elapsed = time.perf_counter() - started
            self.events.put(
                (
                    "scan_done",
                    {
                        "targets": snapshots,
                        "selected": self.selected,
                        "window": self.window_name,
                        "visited": self.visited,
                        "all_count": len(self.all_targets),
                        "hit_source": (
                            self.hierarchy[0].snapshot.source
                            if self.hierarchy
                            else "geometry"
                        ),
                        "hierarchy_index": self.hierarchy_index,
                        "hierarchy_count": len(self.hierarchy),
                        "elapsed": elapsed,
                        "used_cache": used_cache,
                        "scan_token": scan_token,
                    },
                )
            )

        def _refresh_invalid_target(self, target: RuntimeTarget) -> None:
            self.invalid_targets.add(self._identity_token(target.snapshot))
            self.events.put(("target_skipped", target.snapshot))
            self._request_background_refresh()
            self._emit_selection()

        def _activate(self) -> None:
            if not self.targets or self.selected < 0:
                return
            started = time.perf_counter()
            target = self.targets[self.selected]
            cached_point = self._cached_pointer_point(target)
            if cached_point is not None:
                click_point(cached_point)
                self._remember_pointer_point(target, cached_point)
                self.events.put(
                    (
                        "activated",
                        {
                            "target": target.snapshot,
                            "method": "verified coordinate click (repeat)",
                            "repeat": True,
                            "elapsed": time.perf_counter() - started,
                        },
                    )
                )
                return
            if not self._sync_window_geometry():
                return
            target = self.targets[self.selected]
            if not self._update_live_target(target):
                self._refresh_invalid_target(target)
                return
            exposed = self._target_is_exposed(
                target, allow_semantic_bypass=False
            )
            point = target_pointer_point(
                target.snapshot,
                target.click_point,
                allow_rect_center=target.control is None,
            )
            if exposed and point is not None:
                click_point(point)
                self._remember_pointer_point(target, point)
                method = (
                    "MSAA coordinate click"
                    if target.control is None
                    else "verified coordinate click"
                )
            else:
                method = (
                    try_semantic_invoke(target)
                    if target.snapshot.has_action_pattern
                    else None
                )
                if method is None:
                    self._refresh_invalid_target(target)
                    return
            self.events.put(
                (
                    "activated",
                    {
                        "target": target.snapshot,
                        "method": method,
                        "repeat": False,
                        "elapsed": time.perf_counter() - started,
                    },
                )
            )

        def _context_click(self) -> None:
            if not self.targets or self.selected < 0:
                return
            started = time.perf_counter()
            if not self._sync_window_geometry():
                return
            target = self.targets[self.selected]
            if not self._target_is_exposed(
                target, allow_semantic_bypass=False
            ):
                self._refresh_invalid_target(target)
                return
            point = target_pointer_point(
                target.snapshot,
                target.click_point,
                allow_rect_center=target.control is None,
            )
            if point is None:
                self._refresh_invalid_target(target)
                return
            click_point(point, button="right")
            self.events.put(
                (
                    "contexted",
                    {
                        "target": target.snapshot,
                        "elapsed": time.perf_counter() - started,
                    },
                )
            )

        def _scroll(self, steps: int) -> None:
            if not self.targets or self.selected < 0:
                return
            started = time.perf_counter()
            target = self.targets[self.selected]
            point = self._cached_scroll_point(target)
            if point is None:
                if not self._sync_window_geometry():
                    return
                target = self.targets[self.selected]
                if not self._target_is_exposed(
                    target, allow_semantic_bypass=False
                ):
                    self._refresh_invalid_target(target)
                    return
                point = target_pointer_point(
                    target.snapshot,
                    target.click_point,
                    allow_rect_center=target.control is None,
                )
                if point is None:
                    self._refresh_invalid_target(target)
                    return
            scroll_point(point, steps)
            self._remember_scroll_point(target, point)
            self.events.put(
                (
                    "scrolled",
                    {
                        "target": target.snapshot,
                        "steps": steps,
                        "elapsed": time.perf_counter() - started,
                    },
                )
            )

        def _follow_window(self, hwnd: int, expected_generation: int) -> None:
            if hwnd <= 0:
                return
            if hwnd == self.hwnd:
                self._sync_window_geometry()
                return
            started = time.perf_counter()
            with self._post_lock:
                if expected_generation != self._generation:
                    return
                interrupt_generation = self._refresh_interrupt_generation
            committed, partial, _empty = self._enumerate(
                hwnd,
                deadline=started + FOLLOW_WINDOW_SCAN_BUDGET_SECONDS,
                should_cancel=lambda: (
                    self._generation != expected_generation
                    or self._refresh_interrupt_generation != interrupt_generation
                ),
                allow_partial=True,
                expected_generation=expected_generation,
            )
            if not committed:
                return
            with self._post_lock:
                if expected_generation != self._generation:
                    return
                self._pending_follow_completion_hwnd = hwnd
                self._empty_follow_refresh_attempts = 0
            self._apply_targets(use_cursor=True)
            self._clear_hierarchy()
            # A deadline-limited first pass can finish before an async provider
            # publishes the rest of its tree, even when it found some targets.
            self._request_background_refresh()
            if not self.targets or self.selected < 0:
                self.events.put(
                    (
                        "window_follow_pending",
                        {"window": self.window_name},
                    )
                )
                return
            self.events.put(
                (
                    "window_followed",
                    {
                        "window": self.window_name,
                        "partial": partial,
                        **self._selection_payload(),
                    },
                )
            )

        def _refresh_content(self) -> None:
            self._request_background_refresh()

        def _back(self) -> None:
            self.events.put(("exit_requested", None))

        def _run(self) -> None:
            auto.InitializeUIAutomationInCurrentThread()
            try:
                while True:
                    try:
                        command, value, generation = self.commands.get(
                            timeout=self._IDLE_REFRESH_POLL_SECONDS
                        )
                    except queue.Empty:
                        self._refresh_if_idle()
                        continue
                    try:
                        with self._post_lock:
                            if command in self._pending_counts:
                                self._pending_counts[command] = max(
                                    0, self._pending_counts[command] - 1
                                )
                            current_generation = self._generation
                        if (
                            command in self._NAVIGATION_COMMANDS
                            and generation != current_generation
                        ):
                            continue
                        if command == "stop":
                            return
                        if command == "scan":
                            self._scan_requested.clear()
                            scan_hwnd, scan_token = value
                            self._scan(
                                int(scan_hwnd),
                                generation,
                                int(scan_token),
                            )
                        elif command == "prewarm":
                            self._prewarm(int(value), generation)
                        elif command == "diagnostics":
                            self.diagnostics_enabled = bool(value)
                        elif command == "move" and self.context_valid:
                            direction = Direction(value)
                            if self.targets and self.selected >= 0:
                                self._move(direction)
                            else:
                                self._defer_move(direction, generation)
                        elif command == "parent" and self._sync_window_geometry():
                            self._cycle_hierarchy(1)
                        elif command == "child" and self._sync_window_geometry():
                            self._cycle_hierarchy(-1)
                        elif (
                            command == "activate"
                            and self.context_valid
                            and self.targets
                            and self.selected >= 0
                        ):
                            self._activate()
                        elif command == "context" and self.context_valid:
                            self._context_click()
                        elif command == "scroll_up" and self.context_valid:
                            self._scroll(1)
                        elif command == "scroll_down" and self.context_valid:
                            self._scroll(-1)
                        elif command == "back":
                            self._back()
                        elif command == "sync_window":
                            if (
                                not self._refresh_cancel_requested.is_set()
                                and time.perf_counter()
                                >= self._content_settle_until
                            ):
                                self._sync_window_geometry()
                        elif command == "refresh_content":
                            self._refresh_content()
                        elif command == "follow_window":
                            self._follow_window(int(value), generation)
                    except Exception as exc:
                        scan_token = (
                            int(value[1])
                            if command == "scan"
                            and isinstance(value, tuple)
                            and len(value) == 2
                            else 0
                        )
                        self.events.put(
                            (
                                "error",
                                {
                                    "command": command,
                                    "message": str(exc),
                                    "scan_token": scan_token,
                                },
                            )
                        )
                    finally:
                        self._finish_refresh_interrupt(command)
            finally:
                auto.UninitializeUIAutomationInCurrentThread()

    class NavigationOverlay(QWidget):
        def __init__(self) -> None:
            super().__init__()
            self._target: Optional[TargetSnapshot] = None
            self._position = ""
            self._physical_screen = Rect(0, 0, 1, 1)
            self._device_pixel_ratio = 1.0
            self.setWindowFlags(
                Qt.WindowType.Tool
                | Qt.WindowType.FramelessWindowHint
                | Qt.WindowType.WindowStaysOnTopHint
                | Qt.WindowType.WindowTransparentForInput
                | Qt.WindowType.WindowDoesNotAcceptFocus
            )
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

        @staticmethod
        def _screen_rects() -> list[tuple[Any, Rect]]:
            result = []
            for screen in QGuiApplication.screens():
                geometry = screen.geometry()
                logical = Rect(
                    geometry.left(),
                    geometry.top(),
                    geometry.right() + 1,
                    geometry.bottom() + 1,
                )
                result.append(
                    (
                        screen,
                        physical_screen_rect(logical, screen.devicePixelRatio()),
                    )
                )
            return result

        def _position_for_target(self, target: Rect) -> None:
            center = (round(target.center_x), round(target.center_y))
            screens = self._screen_rects()
            screen, physical = next(
                (
                    item
                    for item in screens
                    if item[1].contains_point(center)
                ),
                screens[0],
            )
            self._physical_screen = physical
            self._device_pixel_ratio = float(screen.devicePixelRatio())
            self.setGeometry(screen.geometry())

        def show_target(
            self,
            target: TargetSnapshot,
            hierarchy_index: int = -1,
            hierarchy_count: int = 0,
        ) -> None:
            self._target = target
            self._position = navigation_overlay_label(
                target,
                hierarchy_index,
                hierarchy_count,
            )
            self._position_for_target(target.rect)
            self.show()
            self.raise_()
            self.update()

        def clear_target(self) -> None:
            self._target = None
            self.hide()

        def paintEvent(self, _event: Any) -> None:
            if self._target is None:
                return
            target = physical_to_screen_logical_rect(
                self._target.rect,
                self._physical_screen,
                self._device_pixel_ratio,
            )
            local = QRect(
                target.left,
                target.top,
                target.width,
                target.height,
            )
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(QPen(QColor("#1687ff"), 4))
            painter.setBrush(QColor(22, 135, 255, 28))
            painter.drawRoundedRect(local.adjusted(1, 1, -1, -1), 4, 4)

            font = QFont("Segoe UI", 10)
            font.setBold(True)
            painter.setFont(font)
            metrics = painter.fontMetrics()
            label_width = min(520, metrics.horizontalAdvance(self._position) + 18)
            label_height = metrics.height() + 10
            label_x = max(0, min(local.left(), self.width() - label_width))
            label_y = local.top() - label_height - 4
            if label_y < 0:
                label_y = min(self.height() - label_height, local.bottom() + 4)
            label_rect = QRect(label_x, label_y, label_width, label_height)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(18, 22, 28, 230))
            painter.drawRoundedRect(label_rect, 4, 4)
            painter.setPen(QColor("#ffffff"))
            painter.drawText(
                label_rect.adjusted(9, 0, -9, 0),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                self._position,
            )

    class StructureChangeWatcher:
        HOOK_RANGES = ((0x8000, 0x8004), (0x800A, EVENT_OBJECT_LOCATIONCHANGE))
        WM_QUIT = 0x0012

        def __init__(self) -> None:
            self._hooks: list[int] = []
            self._callback = None
            self._thread_id = 0
            self._ready = threading.Event()
            self._thread = threading.Thread(
                target=self._run,
                name="element-navigation-structure-events",
                daemon=True,
            )

        def start(self) -> bool:
            self._thread.start()
            return bool(
                self._ready.wait(3)
                and len(self._hooks) == len(self.HOOK_RANGES)
            )

        def stop(self) -> None:
            if self._thread_id and self._thread.is_alive():
                if not user32.PostThreadMessageW(
                    self._thread_id, self.WM_QUIT, 0, 0
                ):
                    print("界面变化监听退出消息发送失败。", file=sys.stderr)
            self._thread.join(timeout=3)
            if self._thread.is_alive():
                print("界面变化监听未能及时退出。", file=sys.stderr)

        def _handle(
            self,
            _hook: Any,
            event_id: int,
            hwnd: int,
            object_id: int,
            _child_id: int,
            _event_thread: int,
            _event_time: int,
        ) -> None:
            if not hwnd or not navigation_structure_event_affects_targets(
                int(event_id), int(object_id)
            ):
                return
            event_hwnd = native_handle_value(hwnd)
            root_hwnd = native_handle_value(user32.GetAncestor(event_hwnd, ga_root))
            if root_hwnd <= 0:
                root_hwnd = event_hwnd
            dirty_windows.mark(root_hwnd, window_process_id(root_hwnd))

        def _run(self) -> None:
            self._thread_id = int(kernel32.GetCurrentThreadId())
            message = wintypes.MSG()
            user32.PeekMessageW(
                ctypes.byref(message), None, 0, 0, pm_noremove
            )
            self._callback = win_event_proc_type(self._handle)
            for event_min, event_max in self.HOOK_RANGES:
                hook = user32.SetWinEventHook(
                    event_min,
                    event_max,
                    None,
                    self._callback,
                    0,
                    0,
                    winevent_outofcontext | winevent_skipownprocess,
                )
                if hook:
                    self._hooks.append(native_handle_value(hook))
            self._ready.set()
            if not self._hooks:
                return
            try:
                while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                    user32.TranslateMessage(ctypes.byref(message))
                    user32.DispatchMessageW(ctypes.byref(message))
            finally:
                for hook in self._hooks:
                    user32.UnhookWinEvent(hook)
                self._hooks.clear()
                self._thread_id = 0

    class KeyboardHook:
        WH_KEYBOARD_LL = 13
        WM_KEYDOWN = 0x0100
        WM_KEYUP = 0x0101
        WM_SYSKEYDOWN = 0x0104
        WM_SYSKEYUP = 0x0105
        WM_QUIT = 0x0012
        LLKHF_INJECTED = 0x10
        VK_CONTROL = 0x11
        VK_MENU = 0x12

        def __init__(
            self,
            on_action: Callable[[str], None],
            active: threading.Event,
            intercepting: Optional[threading.Event] = None,
            *,
            include_developer_hotkeys: bool = True,
        ) -> None:
            ulong_ptr = wintypes.WPARAM

            class KbdLlHookStruct(ctypes.Structure):
                _fields_ = [
                    ("vkCode", wintypes.DWORD),
                    ("scanCode", wintypes.DWORD),
                    ("flags", wintypes.DWORD),
                    ("time", wintypes.DWORD),
                    ("dwExtraInfo", ulong_ptr),
                ]

            self._struct = KbdLlHookStruct
            self._proc_type = ctypes.WINFUNCTYPE(
                lresult,
                ctypes.c_int,
                wintypes.WPARAM,
                wintypes.LPARAM,
            )
            user32.SetWindowsHookExW.argtypes = [
                ctypes.c_int,
                self._proc_type,
                wintypes.HINSTANCE,
                wintypes.DWORD,
            ]
            user32.SetWindowsHookExW.restype = wintypes.HHOOK
            user32.CallNextHookEx.argtypes = [
                wintypes.HHOOK,
                ctypes.c_int,
                wintypes.WPARAM,
                wintypes.LPARAM,
            ]
            self._on_action = on_action
            self._active = active
            self._intercepting = intercepting or active
            self._include_developer_hotkeys = include_developer_hotkeys
            self._hook = None
            self._callback = None
            self._thread_id = 0
            self._ready = threading.Event()
            self._down: set[int] = set()
            self._swallowed: set[int] = set()
            self._passthrough: set[int] = set()
            self._direction_input_ownership = DirectionInputOwnership()
            self._thread = threading.Thread(
                target=self._run,
                name="element-navigation-keyboard-hook",
                daemon=True,
            )

        def start(self) -> None:
            self._thread.start()
            if not self._ready.wait(3) or not self._hook:
                raise RuntimeError("无法安装全局键盘钩子")

        def stop(self) -> None:
            if self._thread_id:
                user32.PostThreadMessageW(self._thread_id, self.WM_QUIT, 0, 0)
            self._thread.join(timeout=2)

        def _pressed(self, vk: int) -> bool:
            return bool(user32.GetAsyncKeyState(vk) & 0x8000)

        def _handle(self, code: int, wparam: int, lparam: int) -> int:
            if code < 0:
                return user32.CallNextHookEx(self._hook, code, wparam, lparam)
            message = int(wparam)
            is_down = message in (self.WM_KEYDOWN, self.WM_SYSKEYDOWN)
            is_up = message in (self.WM_KEYUP, self.WM_SYSKEYUP)
            if not (is_down or is_up):
                return user32.CallNextHookEx(self._hook, code, wparam, lparam)

            data = ctypes.cast(lparam, ctypes.POINTER(self._struct)).contents
            vk = int(data.vkCode)
            injected = bool(data.flags & self.LLKHF_INJECTED)
            was_down = vk in self._down
            if is_down:
                self._down.add(vk)
            else:
                self._down.discard(vk)

            if (
                is_up
                and not injected
                and self._direction_input_ownership.has_forwarded_down(vk)
            ):
                _downstream_owned, downstream_result = (
                    self._direction_input_ownership.route(
                        vk,
                        is_down=False,
                        is_up=True,
                        injected=False,
                        call_next=lambda: user32.CallNextHookEx(
                            self._hook, code, wparam, lparam
                        ),
                    )
                )
                self._passthrough.discard(vk)
                self._swallowed.discard(vk)
                return downstream_result or 1

            if vk in self._passthrough:
                if is_up:
                    self._passthrough.discard(vk)
                return user32.CallNextHookEx(
                    self._hook, code, wparam, lparam
                )

            if is_up and vk in self._swallowed:
                self._swallowed.discard(vk)
                return 1

            ctrl_alt = self._pressed(self.VK_CONTROL) and self._pressed(self.VK_MENU)
            hotkey_action = global_hotkey_action(
                vk,
                include_developer_actions=self._include_developer_hotkeys,
            )
            if is_down and ctrl_alt and hotkey_action is not None:
                self._swallowed.add(vk)
                if not was_down:
                    self._on_action(hotkey_action)
                return 1

            action = keyboard_navigation_action(vk)
            if self._intercepting.is_set() and action is not None:
                if self._active.is_set() and should_pass_through_native_menu(
                    vk, native_menu_mode_active()
                ):
                    if is_down:
                        self._passthrough.add(vk)
                    return user32.CallNextHookEx(
                        self._hook, code, wparam, lparam
                    )
                downstream_owned, downstream_result = (
                    self._direction_input_ownership.route(
                        vk,
                        is_down=is_down,
                        is_up=is_up,
                        injected=injected,
                        call_next=lambda: user32.CallNextHookEx(
                            self._hook, code, wparam, lparam
                        ),
                    )
                )
                if downstream_owned:
                    return downstream_result or 1
                self._swallowed.add(vk)
                if is_down and (vk in self._down):
                    if vk in (VK_RETURN, VK_APPS, VK_ESCAPE) and was_down:
                        return 1
                    self._on_action(action)
                return 1

            if is_down and action is not None:
                self._passthrough.add(vk)
            return user32.CallNextHookEx(self._hook, code, wparam, lparam)

        def _run(self) -> None:
            self._thread_id = int(kernel32.GetCurrentThreadId())
            self._callback = self._proc_type(self._handle)
            self._hook = user32.SetWindowsHookExW(
                self.WH_KEYBOARD_LL, self._callback, kernel32.GetModuleHandleW(None), 0
            )
            self._ready.set()
            if not self._hook:
                return
            message = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
            user32.UnhookWindowsHookEx(self._hook)
            self._hook = None

    if args.scan_only:
        hwnd = native_handle_value(
            args.window_handle or user32.GetForegroundWindow()
        )
        if hwnd <= 0:
            print("没有可扫描的前台窗口。", file=sys.stderr)
            return 2
        auto.InitializeUIAutomationInCurrentThread()
        try:
            started = time.perf_counter()
            (
                targets,
                node_types,
                _rect,
                window_name,
                visited,
                _interrupted,
            ) = enumerate_targets(hwnd)
            snapshots = [target.snapshot for target in targets]
            del node_types
            visible = [targets[index] for index in flat_target_indices(snapshots)]
            elapsed = time.perf_counter() - started
            print(
                f"窗口: {window_name}\n根层: {len(visible)}，全部元素: {len(targets)}，"
                f"访问节点: {visited}，"
                f"耗时: {elapsed:.2f}s"
            )
            for index, target in enumerate(visible[:80], 1):
                item = target.snapshot
                print(
                    f"{index:>3}. {item.control_type:<22} "
                    f"{item.rect.left},{item.rect.top},{item.rect.width}x{item.rect.height} "
                    f"{item.name or item.automation_id or '(未命名)'}"
                )
            return 0
        finally:
            auto.UninitializeUIAutomationInCurrentThread()

    app = QApplication(sys.argv[:1])
    app.setApplicationName("元素导航")
    prototype_process_id = int(kernel32.GetCurrentProcessId())
    overlay = NavigationOverlay()
    worker = AutomationWorker(diagnostics_enabled=bool(args.diagnostics))
    worker.start()
    keyboard_events: queue.Queue[tuple[str, int]] = queue.Queue()
    active = threading.Event()
    intercepting = threading.Event()
    scanning = False
    scan_token_counter = 0
    current_scan_token = 0
    shutting_down = False
    prewarm_observed_hwnd = 0
    prewarm_observed_at = 0.0
    prewarm_requested_hwnd = 0
    navigation_root_hwnd = 0
    navigation_process_id = 0
    navigation_overlay_signature: tuple[tuple[int, Rect], ...] = ()
    navigation_overlay_checked_at = 0.0
    overlay_signature_poll_seconds = 1.0
    diagnostics_enabled = bool(args.diagnostics)
    managed_companion = bool(getattr(args, "managed_companion", False))
    owner_pid = max(0, int(getattr(args, "owner_pid", 0) or 0))
    include_developer_hotkeys = not managed_companion or diagnostics_enabled

    def enqueue_keyboard_action(action: str) -> None:
        keyboard_events.put((action, 0))

    def enqueue_external_command(command: int, target_hwnd: int) -> None:
        if command == ELEMENT_NAVIGATION_COMMAND_TOGGLE:
            keyboard_events.put(("toggle", max(0, int(target_hwnd))))
        elif command == ELEMENT_NAVIGATION_COMMAND_QUIT:
            keyboard_events.put(("quit", 0))

    hook = KeyboardHook(
        enqueue_keyboard_action,
        active,
        intercepting,
        include_developer_hotkeys=include_developer_hotkeys,
    )
    hook.start()
    structure_watcher = StructureChangeWatcher()
    if not structure_watcher.start():
        print(
            "界面变化监听未完整启用，将按缓存时限兜底刷新。",
            file=sys.stderr,
        )
    command_server = ElementNavigationCommandServer(enqueue_external_command)
    try:
        command_server.start()
    except Exception:
        hook.stop()
        structure_watcher.stop()
        worker.stop()
        raise

    def leave_navigation() -> None:
        nonlocal navigation_root_hwnd, navigation_process_id
        nonlocal navigation_overlay_signature, navigation_overlay_checked_at
        nonlocal scanning, current_scan_token
        active.clear()
        intercepting.clear()
        scanning = False
        current_scan_token = 0
        worker.deactivate()
        overlay.clear_target()
        navigation_root_hwnd = 0
        navigation_process_id = 0
        navigation_overlay_signature = ()
        navigation_overlay_checked_at = 0.0

    def request_quit() -> None:
        nonlocal shutting_down
        if shutting_down:
            return
        shutting_down = True
        leave_navigation()
        app.quit()

    def refresh_navigation_overlay_signature(*, force: bool = False) -> bool:
        nonlocal navigation_overlay_signature, navigation_overlay_checked_at
        now = time.perf_counter()
        if not force and not periodic_check_due(
            now,
            navigation_overlay_checked_at,
            overlay_signature_poll_seconds,
        ):
            return False
        navigation_overlay_checked_at = now
        current_signature = associated_overlay_window_signature(
            navigation_root_hwnd,
            excluded_process_id=prototype_process_id,
        )
        if current_signature != navigation_overlay_signature:
            navigation_overlay_signature = current_signature
            if active.is_set():
                worker.post("refresh_content")
        return True

    def navigation_action_for_foreground(foreground: int) -> str:
        return navigation_foreground_action(
            foreground,
            worker.hwnd,
            navigation_root_hwnd,
            navigation_process_id,
            prototype_process_id,
            window_process_id,
            window_owner,
            tuple(handle for handle, _rect in navigation_overlay_signature),
        )

    def prepare_navigation_action() -> bool:
        foreground = native_handle_value(user32.GetForegroundWindow())
        foreground_action = navigation_action_for_foreground(foreground)
        if foreground_action == "leave":
            refresh_navigation_overlay_signature(force=True)
            foreground_action = navigation_action_for_foreground(foreground)
        if foreground_action == "leave":
            print("导航已暂停: 已切换到其它窗口，请重新按 Ctrl+Alt+N。")
            leave_navigation()
            return False
        if foreground_action == "follow":
            worker.post("follow_window", foreground)
        return True

    def handle_keyboard_action(action: str, target_hwnd: int = 0) -> None:
        nonlocal scanning, navigation_root_hwnd, navigation_process_id
        nonlocal navigation_overlay_signature, navigation_overlay_checked_at
        nonlocal diagnostics_enabled, scan_token_counter, current_scan_token
        if action == "quit":
            request_quit()
        elif action == "toggle_diagnostics":
            diagnostics_enabled = not diagnostics_enabled
            worker.post("diagnostics", diagnostics_enabled)
            print(
                "导航诊断已开启。每次方向移动都会解释候选排序。"
                if diagnostics_enabled
                else "导航诊断已关闭。"
            )
        elif action == "toggle":
            if active.is_set() or scanning:
                leave_navigation()
            else:
                hwnd = native_handle_value(
                    target_hwnd or user32.GetForegroundWindow()
                )
                if hwnd <= 0:
                    print("没有可扫描的前台窗口。")
                    return
                navigation_root_hwnd = hwnd
                navigation_process_id = window_process_id(hwnd)
                navigation_overlay_signature = associated_overlay_window_signature(
                    hwnd,
                    excluded_process_id=prototype_process_id,
                )
                navigation_overlay_checked_at = time.perf_counter()
                scan_token_counter += 1
                current_scan_token = scan_token_counter
                scanning = True
                intercepting.set()
                print("正在扫描当前窗口...")
                worker.post("scan", (hwnd, current_scan_token))
        elif action == "cancel":
            if active.is_set():
                worker.post("back")
            elif scanning:
                leave_navigation()
        elif action == "activate" and active.is_set():
            if prepare_navigation_action():
                worker.post("activate")
        elif action in {"context", "scroll_up", "scroll_down"} and active.is_set():
            if prepare_navigation_action():
                worker.post(action)
        elif action in {"parent", "child"} and active.is_set():
            if prepare_navigation_action():
                worker.post(action)
        elif action in {direction.value for direction in Direction} and active.is_set():
            if prepare_navigation_action():
                worker.post("move", action)

    def drain_events() -> None:
        nonlocal scanning, current_scan_token
        while True:
            try:
                action, target_hwnd = keyboard_events.get_nowait()
            except queue.Empty:
                break
            handle_keyboard_action(action, target_hwnd)

        while True:
            try:
                event, payload = worker.events.get_nowait()
            except queue.Empty:
                break
            if event == "scan_done":
                if not scan_event_is_current(
                    int(payload.get("scan_token", 0)),
                    current_scan_token,
                    scanning,
                ):
                    continue
                scanning = False
                current_scan_token = 0
                targets = payload["targets"]
                selected = payload["selected"]
                print(
                    f"已扫描 {payload['window']}: 根层 {len(targets)} 个，"
                    f"全部 {payload['all_count']} 个，"
                    f"访问 {payload['visited']} 个节点，耗时 {payload['elapsed']:.2f}s，"
                    f"命中 {payload['hit_source']} / {payload['hierarchy_count']} 层"
                    f"{'，使用预热缓存' if payload['used_cache'] else ''}"
                )
                if selected < 0:
                    leave_navigation()
                    print("没有找到可导航元素。")
                else:
                    active.set()
                    overlay.show_target(
                        targets[selected],
                        payload["hierarchy_index"],
                        payload["hierarchy_count"],
                    )
            elif event == "scan_cancelled":
                if not scan_event_is_current(
                    int(payload.get("scan_token", 0)),
                    current_scan_token,
                    scanning,
                ):
                    continue
                leave_navigation()
            elif event == "selection":
                if active.is_set():
                    overlay.show_target(
                        payload["target"],
                        payload["hierarchy_index"],
                        payload["hierarchy_count"],
                    )
            elif event == "activated":
                target = payload["target"]
                print(
                    f"已执行: {target.name or target.control_type} "
                    f"({payload['method']} / {payload['elapsed']:.3f}s)"
                )
                refresh_delay = content_refresh_delay_ms(
                    event, payload["repeat"]
                )
                if refresh_delay and active.is_set():
                    QTimer.singleShot(
                        refresh_delay, lambda: worker.post("refresh_content")
                    )
            elif event == "contexted":
                target = payload["target"]
                print(
                    f"已右击: {target.name or target.control_type} "
                    f"({payload['elapsed']:.3f}s)"
                )
                refresh_delay = content_refresh_delay_ms(event)
                if refresh_delay and active.is_set():
                    QTimer.singleShot(
                        refresh_delay, lambda: worker.post("refresh_content")
                    )
            elif event == "scrolled":
                target = payload["target"]
                print(
                    f"已滚动: {target.name or target.control_type} "
                    f"({payload['steps']:+d} / {payload['elapsed']:.3f}s)"
                )
                refresh_delay = content_refresh_delay_ms(event)
                if refresh_delay and active.is_set():
                    QTimer.singleShot(
                        refresh_delay, lambda: worker.post("refresh_content")
                    )
            elif event == "exit_requested":
                leave_navigation()
            elif event == "geometry_synced":
                print(
                    f"窗口位置已同步: {payload['delta_x']:+d}, {payload['delta_y']:+d}"
                )
            elif event == "geometry_rescanned":
                print("页面或窗口变化，已重新扫描。")
            elif event == "window_followed":
                if payload.get("partial"):
                    print(
                        f"已快速跟随同一软件窗口: {payload['window']}，"
                        "后台继续识别。"
                    )
                else:
                    print(f"已跟随同一软件窗口: {payload['window']}")
                if active.is_set():
                    overlay.show_target(
                        payload["target"],
                        payload["hierarchy_index"],
                        payload["hierarchy_count"],
                    )
            elif event == "window_follow_pending":
                print(
                    f"已切换到同一软件窗口: {payload['window']}，后台继续识别。"
                )
                if active.is_set():
                    overlay.clear_target()
            elif event == "content_refreshed":
                if active.is_set():
                    overlay.show_target(
                        payload["target"],
                        payload["hierarchy_index"],
                        payload["hierarchy_count"],
                    )
            elif event == "navigation_invalidated":
                print(f"导航已暂停: {payload}，请重新按 Ctrl+Alt+N。")
                leave_navigation()
            elif event == "prewarm_done":
                print(
                    f"已预识别 {payload['window']}，耗时 {payload['elapsed']:.2f}s。"
                )
            elif event == "prewarm_skipped":
                print(
                    f"预识别超过 {payload['elapsed']:.2f}s，已停止以免阻塞按键启动。"
                )
            elif event == "target_skipped":
                print(
                    f"已跳过当前无法命中的元素: "
                    f"{payload.name or payload.control_type}"
                )
            elif event == "navigation_diagnostic":
                print(format_navigation_diagnostic(payload), flush=True)
            elif event == "refresh_failed":
                print(f"后台刷新稍后重试: {payload}", file=sys.stderr)
            elif event == "error":
                command = payload["command"]
                message = payload["message"]
                if command == "prewarm":
                    print(f"预识别已跳过: {message}", file=sys.stderr)
                    continue
                if command == "scan":
                    if not scan_event_is_current(
                        int(payload.get("scan_token", 0)),
                        current_scan_token,
                        scanning,
                    ):
                        continue
                leave_navigation()
                print(f"操作失败: {message}", file=sys.stderr)

    timer = QTimer()
    timer.timeout.connect(drain_events)
    timer.start(20)

    def monitor_navigation_context() -> None:
        nonlocal prewarm_observed_hwnd, prewarm_observed_at, prewarm_requested_hwnd
        nonlocal navigation_overlay_signature, navigation_overlay_checked_at
        foreground = native_handle_value(user32.GetForegroundWindow())
        if not active.is_set():
            now = time.perf_counter()
            if foreground != prewarm_observed_hwnd:
                prewarm_observed_hwnd = foreground
                prewarm_observed_at = now
                return
            if scanning or foreground <= 0:
                return
            if window_process_id(foreground) == prototype_process_id:
                return
            if prewarm_request_due(
                foreground,
                prewarm_observed_hwnd,
                prewarm_observed_at,
                prewarm_requested_hwnd,
                now,
            ):
                prewarm_requested_hwnd = foreground
                worker.post("prewarm", foreground)
            return
        if foreground <= 0:
            return
        overlay_signature_checked = refresh_navigation_overlay_signature()
        foreground_action = navigation_action_for_foreground(foreground)
        if foreground_action == "leave" and not overlay_signature_checked:
            refresh_navigation_overlay_signature(force=True)
            foreground_action = navigation_action_for_foreground(foreground)
        if foreground_action == "ignore":
            return
        if foreground_action == "leave":
            print("导航已暂停: 已切换到其它窗口，请重新按 Ctrl+Alt+N。")
            leave_navigation()
            return
        if foreground_action == "follow":
            worker.post("follow_window", foreground)
        else:
            worker.post("sync_window")

    geometry_timer = QTimer()
    geometry_timer.timeout.connect(monitor_navigation_context)
    geometry_timer.start(250)

    owner_timer = QTimer()

    def monitor_owner_process() -> None:
        if owner_pid > 0 and not owner_process_is_alive(owner_pid):
            request_quit()

    if managed_companion and owner_pid > 0:
        owner_timer.timeout.connect(monitor_owner_process)
        owner_timer.start(500)

    cleanup_complete = False

    def cleanup() -> None:
        nonlocal cleanup_complete
        if cleanup_complete:
            return
        cleanup_complete = True
        command_server.stop()
        hook.stop()
        structure_watcher.stop()
        worker.stop()

    app.aboutToQuit.connect(cleanup)
    if managed_companion and owner_pid > 0:
        QTimer.singleShot(0, monitor_owner_process)
    if bool(getattr(args, "activate", False)):
        enqueue_external_command(
            ELEMENT_NAVIGATION_COMMAND_TOGGLE,
            int(getattr(args, "window_handle", 0) or 0),
        )
    print("元素导航已启动。")
    controls = (
        "Ctrl+Alt+N 开始/退出，方向键移动，PageUp/PageDown 切换父子元素，"
        "Enter 左击（快速两次为双击），菜单键右击，音量键滚动，Esc 退出。"
    )
    if include_developer_hotkeys:
        controls += " Ctrl+Alt+D 开关导航诊断，Ctrl+Alt+Q 关闭。"
    print(controls)
    if diagnostics_enabled:
        print("导航诊断已开启。每次方向移动都会解释候选排序。")
    try:
        return int(app.exec())
    finally:
        cleanup()


__all__ = ("_run_windows",)
