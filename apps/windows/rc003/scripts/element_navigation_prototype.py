"""Experimental keyboard-driven UI Automation spatial navigator.

This developer-only prototype is intentionally outside ``ovb_rc003``. It is
not imported by Remote Mic, included in candidate builds, or connected to the
RC003 input/configuration path. Its job is to answer one question cheaply:
does spatial element navigation feel useful in the user's fixed Windows apps?

Controls:
    Ctrl+Alt+N  scan the foreground window and enter/leave navigation
    Arrow keys  move the highlighted target
    Enter       invoke the highlighted target, then leave navigation
    Esc         leave navigation without touching the target
    Ctrl+Alt+Q  quit the prototype
"""

from __future__ import annotations

import argparse
import ctypes
import queue
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Optional, Sequence


STRUCTURAL_CONTROL_TYPES = frozenset(
    {"CustomControl", "PaneControl", "GroupControl", "ImageControl"}
)


class Direction(str, Enum):
    UP = "up"
    DOWN = "down"
    LEFT = "left"
    RIGHT = "right"


@dataclass(frozen=True)
class Rect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return max(0, self.right - self.left)

    @property
    def height(self) -> int:
        return max(0, self.bottom - self.top)

    @property
    def center_x(self) -> float:
        return (self.left + self.right) / 2

    @property
    def center_y(self) -> float:
        return (self.top + self.bottom) / 2

    def intersects(self, other: "Rect") -> bool:
        return not (
            self.right <= other.left
            or self.left >= other.right
            or self.bottom <= other.top
            or self.top >= other.bottom
        )

    def contains(self, other: "Rect") -> bool:
        return (
            self.left <= other.left
            and self.top <= other.top
            and self.right >= other.right
            and self.bottom >= other.bottom
        )


@dataclass(frozen=True)
class TargetSnapshot:
    rect: Rect
    name: str
    control_type: str
    automation_id: str = ""


def _axis_gap(a_start: int, a_end: int, b_start: int, b_end: int) -> int:
    if b_start > a_end:
        return b_start - a_end
    if a_start > b_end:
        return a_start - b_end
    return 0


def _axis_overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> int:
    return max(0, min(a_end, b_end) - max(a_start, b_start))


def direction_score(
    current: Rect, candidate: Rect, direction: Direction
) -> Optional[tuple[float, float, float, float, int, int]]:
    """Return a stable spatial-navigation score, or None for wrong direction.

    This is an independent prototype heuristic based on the same general
    geometry used by TV and CSS spatial navigation: stay in the requested
    half-plane, prefer the smallest forward gap, strongly penalize leaving the
    current row/column, and reward overlap on the perpendicular axis.
    """

    if current == candidate:
        return None

    if direction == Direction.RIGHT:
        if candidate.center_x <= current.center_x:
            return None
        primary_gap = max(0, candidate.left - current.right)
        perpendicular_gap = _axis_gap(
            current.top, current.bottom, candidate.top, candidate.bottom
        )
        center_offset = abs(candidate.center_y - current.center_y)
        overlap = _axis_overlap(
            current.top, current.bottom, candidate.top, candidate.bottom
        )
    elif direction == Direction.LEFT:
        if candidate.center_x >= current.center_x:
            return None
        primary_gap = max(0, current.left - candidate.right)
        perpendicular_gap = _axis_gap(
            current.top, current.bottom, candidate.top, candidate.bottom
        )
        center_offset = abs(candidate.center_y - current.center_y)
        overlap = _axis_overlap(
            current.top, current.bottom, candidate.top, candidate.bottom
        )
    elif direction == Direction.DOWN:
        if candidate.center_y <= current.center_y:
            return None
        primary_gap = max(0, candidate.top - current.bottom)
        perpendicular_gap = _axis_gap(
            current.left, current.right, candidate.left, candidate.right
        )
        center_offset = abs(candidate.center_x - current.center_x)
        overlap = _axis_overlap(
            current.left, current.right, candidate.left, candidate.right
        )
    else:
        if candidate.center_y >= current.center_y:
            return None
        primary_gap = max(0, current.top - candidate.bottom)
        perpendicular_gap = _axis_gap(
            current.left, current.right, candidate.left, candidate.right
        )
        center_offset = abs(candidate.center_x - current.center_x)
        overlap = _axis_overlap(
            current.left, current.right, candidate.left, candidate.right
        )

    score = (
        primary_gap
        + perpendicular_gap * 2.5
        + center_offset * 0.25
        - overlap * 0.15
    )
    return (
        score,
        float(primary_gap),
        float(perpendicular_gap),
        center_offset,
        candidate.top,
        candidate.left,
    )


def next_target_index(
    targets: Sequence[TargetSnapshot], current_index: int, direction: Direction
) -> int:
    if not targets or not 0 <= current_index < len(targets):
        return current_index
    current = targets[current_index].rect
    scored = []
    for index, target in enumerate(targets):
        if index == current_index:
            continue
        score = direction_score(current, target.rect, direction)
        if score is not None:
            scored.append((score, index))
    if not scored:
        return current_index
    scored.sort(key=lambda item: item[0])
    return scored[0][1]


def initial_target_index(
    targets: Sequence[TargetSnapshot],
    focused_rect: Optional[Rect],
    window_rect: Rect,
) -> int:
    if not targets:
        return -1
    if focused_rect is not None:
        focused_x = focused_rect.center_x
        focused_y = focused_rect.center_y
        containing = [
            (target.rect.width * target.rect.height, index)
            for index, target in enumerate(targets)
            if target.rect.left <= focused_x <= target.rect.right
            and target.rect.top <= focused_y <= target.rect.bottom
        ]
        if containing:
            containing.sort()
            return containing[0][1]

    origin_x = window_rect.left
    origin_y = window_rect.top
    return min(
        range(len(targets)),
        key=lambda index: (
            max(0, targets[index].rect.top - origin_y),
            max(0, targets[index].rect.left - origin_x),
            targets[index].rect.width * targets[index].rect.height,
        ),
    )


def nested_container_keep_indices(targets: Sequence[TargetSnapshot]) -> list[int]:
    """Drop structural wrappers when they contain a more specific target."""

    keep = []
    for index, target in enumerate(targets):
        if target.control_type not in STRUCTURAL_CONTROL_TYPES:
            keep.append(index)
            continue
        area = target.rect.width * target.rect.height
        contains_specific_target = any(
            other_index != index
            and target.rect != other.rect
            and target.rect.contains(other.rect)
            and area
            > (other.rect.width * other.rect.height) * 1.5
            for other_index, other in enumerate(targets)
        )
        if not contains_specific_target:
            keep.append(index)
    return keep


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-depth", type=int, default=16)
    parser.add_argument("--max-elements", type=int, default=300)
    parser.add_argument("--max-nodes", type=int, default=4000)
    parser.add_argument(
        "--scan-only",
        action="store_true",
        help="scan the current foreground window once and print the targets",
    )
    return parser.parse_args(argv)


def _run_windows(args: argparse.Namespace) -> int:
    import uiautomation as auto
    from ctypes import wintypes
    from PySide6.QtCore import Qt, QRect, QTimer
    from PySide6.QtGui import QColor, QFont, QGuiApplication, QPainter, QPen
    from PySide6.QtWidgets import QApplication, QWidget

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    lresult = ctypes.c_ssize_t

    kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    user32.GetForegroundWindow.restype = wintypes.HWND
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
    action_pattern_ids = (
        auto.PatternId.InvokePattern,
        auto.PatternId.TogglePattern,
        auto.PatternId.SelectionItemPattern,
        auto.PatternId.ExpandCollapsePattern,
    )

    @dataclass
    class RuntimeTarget:
        snapshot: TargetSnapshot
        control: Any

    def rect_from_control(control: Any) -> Rect:
        bounds = control.BoundingRectangle
        return Rect(
            int(bounds.left),
            int(bounds.top),
            int(bounds.right),
            int(bounds.bottom),
        )

    def has_action_pattern(control: Any) -> bool:
        for pattern_id in action_pattern_ids:
            try:
                if control.GetPattern(pattern_id) is not None:
                    return True
            except Exception:
                continue
        return False

    def enumerate_targets(hwnd: int) -> tuple[list[RuntimeTarget], Rect, str, int]:
        root = auto.ControlFromHandle(hwnd)
        if root is None:
            raise RuntimeError("无法从当前窗口建立 UI Automation 根元素")
        window_rect = rect_from_control(root)
        window_name = str(root.Name or "未命名窗口")
        pending = deque([(root, 0)])
        by_rect: dict[Rect, RuntimeTarget] = {}
        visited = 0

        while pending and visited < args.max_nodes and len(by_rect) < args.max_elements:
            control, depth = pending.popleft()
            visited += 1
            if depth > 0:
                try:
                    control_type = str(control.ControlTypeName or "")
                    enabled = bool(control.IsEnabled)
                    offscreen = bool(control.IsOffscreen)
                    rect = rect_from_control(control)
                    name = str(control.Name or "").strip()
                    automation_id = str(control.AutomationId or "").strip()
                    valid_size = 10 <= rect.width <= 1800 and 10 <= rect.height <= 1400
                    standard = control_type in interactive_types
                    structural = control_type in STRUCTURAL_CONTROL_TYPES
                    actionable = standard or (
                        structural
                        and valid_size
                        and (bool(control.IsKeyboardFocusable) or has_action_pattern(control))
                    )
                    if (
                        actionable
                        and enabled
                        and not offscreen
                        and valid_size
                        and rect.intersects(window_rect)
                    ):
                        candidate = RuntimeTarget(
                            TargetSnapshot(rect, name, control_type, automation_id),
                            control,
                        )
                        existing = by_rect.get(rect)
                        if existing is None or (
                            not existing.snapshot.name and candidate.snapshot.name
                        ):
                            by_rect[rect] = candidate
                except Exception:
                    pass

            if depth >= args.max_depth:
                continue
            try:
                for child in control.GetChildren():
                    pending.append((child, depth + 1))
            except Exception:
                continue

        targets = list(by_rect.values())
        snapshots = [target.snapshot for target in targets]
        targets = [targets[index] for index in nested_container_keep_indices(snapshots)]
        targets.sort(
            key=lambda item: (
                item.snapshot.rect.top,
                item.snapshot.rect.left,
                item.snapshot.rect.width * item.snapshot.rect.height,
            )
        )
        return targets, window_rect, window_name, visited

    def focused_rect() -> Optional[Rect]:
        try:
            focused = auto.GetFocusedControl()
            return rect_from_control(focused) if focused is not None else None
        except Exception:
            return None

    def smart_invoke(control: Any) -> str:
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
        control.Click(simulateMove=False, waitTime=0)
        return "mouse fallback"

    class AutomationWorker:
        def __init__(self) -> None:
            self.commands: queue.Queue[tuple[str, Any]] = queue.Queue()
            self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
            self.targets: list[RuntimeTarget] = []
            self.selected = -1
            self.hwnd = 0
            self._thread = threading.Thread(
                target=self._run,
                name="element-navigation-uia",
                daemon=True,
            )

        def start(self) -> None:
            self._thread.start()

        def post(self, command: str, value: Any = None) -> None:
            self.commands.put((command, value))

        def stop(self) -> None:
            self.post("stop")
            self._thread.join(timeout=2)

        def _scan(self, hwnd: int) -> None:
            started = time.perf_counter()
            targets, window_rect, window_name, visited = enumerate_targets(hwnd)
            snapshots = [target.snapshot for target in targets]
            selected = initial_target_index(snapshots, focused_rect(), window_rect)
            self.targets = targets
            self.selected = selected
            self.hwnd = hwnd
            elapsed = time.perf_counter() - started
            self.events.put(
                (
                    "scan_done",
                    {
                        "targets": snapshots,
                        "selected": selected,
                        "window": window_name,
                        "visited": visited,
                        "elapsed": elapsed,
                    },
                )
            )

        def _run(self) -> None:
            auto.InitializeUIAutomationInCurrentThread()
            try:
                while True:
                    command, value = self.commands.get()
                    try:
                        if command == "stop":
                            return
                        if command == "scan":
                            self._scan(int(value))
                        elif command == "move" and self.targets and self.selected >= 0:
                            snapshots = [target.snapshot for target in self.targets]
                            self.selected = next_target_index(
                                snapshots, self.selected, Direction(value)
                            )
                            self.events.put(
                                (
                                    "selection",
                                    {
                                        "target": snapshots[self.selected],
                                        "selected": self.selected,
                                        "count": len(snapshots),
                                    },
                                )
                            )
                        elif command == "activate" and self.targets and self.selected >= 0:
                            target = self.targets[self.selected]
                            method = smart_invoke(target.control)
                            self.events.put(
                                (
                                    "activated",
                                    {
                                        "target": target.snapshot,
                                        "method": method,
                                    },
                                )
                            )
                    except Exception as exc:
                        self.events.put(("error", str(exc)))
            finally:
                auto.UninitializeUIAutomationInCurrentThread()

    class NavigationOverlay(QWidget):
        def __init__(self) -> None:
            super().__init__()
            self._desktop = self._virtual_desktop()
            self._target: Optional[TargetSnapshot] = None
            self._position = ""
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
            self.setGeometry(self._desktop)

        @staticmethod
        def _virtual_desktop() -> QRect:
            screens = QGuiApplication.screens()
            geometry = screens[0].geometry()
            for screen in screens[1:]:
                geometry = geometry.united(screen.geometry())
            return geometry

        def show_target(
            self, target: TargetSnapshot, selected: int, count: int
        ) -> None:
            self._target = target
            self._position = f"{selected + 1}/{count}  {target.name or target.control_type}"
            self.show()
            self.raise_()
            self.update()

        def clear_target(self) -> None:
            self._target = None
            self.hide()

        def paintEvent(self, _event: Any) -> None:
            if self._target is None:
                return
            target = self._target.rect
            local = QRect(
                target.left - self._desktop.left(),
                target.top - self._desktop.top(),
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

    class KeyboardHook:
        WH_KEYBOARD_LL = 13
        WM_KEYDOWN = 0x0100
        WM_KEYUP = 0x0101
        WM_SYSKEYDOWN = 0x0104
        WM_SYSKEYUP = 0x0105
        WM_QUIT = 0x0012
        VK_CONTROL = 0x11
        VK_MENU = 0x12
        VK_LEFT = 0x25
        VK_UP = 0x26
        VK_RIGHT = 0x27
        VK_DOWN = 0x28
        VK_RETURN = 0x0D
        VK_ESCAPE = 0x1B
        VK_N = 0x4E
        VK_Q = 0x51

        def __init__(
            self,
            on_action: Callable[[str], None],
            active: threading.Event,
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
            self._hook = None
            self._callback = None
            self._thread_id = 0
            self._ready = threading.Event()
            self._down: set[int] = set()
            self._swallowed: set[int] = set()
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
            was_down = vk in self._down
            if is_down:
                self._down.add(vk)
            else:
                self._down.discard(vk)

            if is_up and vk in self._swallowed:
                self._swallowed.discard(vk)
                return 1

            ctrl_alt = self._pressed(self.VK_CONTROL) and self._pressed(self.VK_MENU)
            if is_down and ctrl_alt and vk in (self.VK_N, self.VK_Q):
                self._swallowed.add(vk)
                if not was_down:
                    self._on_action("toggle" if vk == self.VK_N else "quit")
                return 1

            navigation = {
                self.VK_UP: "up",
                self.VK_DOWN: "down",
                self.VK_LEFT: "left",
                self.VK_RIGHT: "right",
                self.VK_RETURN: "activate",
                self.VK_ESCAPE: "cancel",
            }
            if self._active.is_set() and vk in navigation:
                self._swallowed.add(vk)
                if is_down and (vk in self._down):
                    action = navigation[vk]
                    if vk in (self.VK_RETURN, self.VK_ESCAPE) and was_down:
                        return 1
                    self._on_action(action)
                return 1

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
        hwnd = int(user32.GetForegroundWindow())
        auto.InitializeUIAutomationInCurrentThread()
        try:
            started = time.perf_counter()
            targets, _rect, window_name, visited = enumerate_targets(hwnd)
            elapsed = time.perf_counter() - started
            print(
                f"窗口: {window_name}\n元素: {len(targets)}，访问节点: {visited}，"
                f"耗时: {elapsed:.2f}s"
            )
            for index, target in enumerate(targets[:80], 1):
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
    app.setApplicationName("Remote Mic Element Navigation Prototype")
    overlay = NavigationOverlay()
    worker = AutomationWorker()
    worker.start()
    keyboard_events: queue.Queue[str] = queue.Queue()
    active = threading.Event()
    scanning = False
    shutting_down = False

    def enqueue_keyboard_action(action: str) -> None:
        keyboard_events.put(action)

    hook = KeyboardHook(enqueue_keyboard_action, active)
    hook.start()

    def leave_navigation() -> None:
        active.clear()
        overlay.clear_target()

    def request_quit() -> None:
        nonlocal shutting_down
        if shutting_down:
            return
        shutting_down = True
        leave_navigation()
        app.quit()

    def handle_keyboard_action(action: str) -> None:
        nonlocal scanning
        if action == "quit":
            request_quit()
        elif action == "toggle":
            if active.is_set():
                leave_navigation()
            elif not scanning:
                hwnd = int(user32.GetForegroundWindow())
                scanning = True
                print("正在扫描当前窗口...")
                worker.post("scan", hwnd)
        elif action == "cancel":
            leave_navigation()
        elif action == "activate" and active.is_set():
            active.clear()
            overlay.clear_target()
            worker.post("activate")
        elif action in {direction.value for direction in Direction} and active.is_set():
            worker.post("move", action)

    def drain_events() -> None:
        nonlocal scanning
        while True:
            try:
                action = keyboard_events.get_nowait()
            except queue.Empty:
                break
            handle_keyboard_action(action)

        while True:
            try:
                event, payload = worker.events.get_nowait()
            except queue.Empty:
                break
            if event == "scan_done":
                scanning = False
                targets = payload["targets"]
                selected = payload["selected"]
                print(
                    f"已扫描 {payload['window']}: {len(targets)} 个元素，"
                    f"访问 {payload['visited']} 个节点，耗时 {payload['elapsed']:.2f}s"
                )
                if selected < 0:
                    leave_navigation()
                    print("没有找到可导航元素。")
                else:
                    active.set()
                    overlay.show_target(targets[selected], selected, len(targets))
            elif event == "selection":
                overlay.show_target(
                    payload["target"], payload["selected"], payload["count"]
                )
            elif event == "activated":
                target = payload["target"]
                print(
                    f"已执行: {target.name or target.control_type} "
                    f"({payload['method']})"
                )
                leave_navigation()
            elif event == "error":
                scanning = False
                leave_navigation()
                print(f"操作失败: {payload}", file=sys.stderr)

    timer = QTimer()
    timer.timeout.connect(drain_events)
    timer.start(20)

    def cleanup() -> None:
        hook.stop()
        worker.stop()

    app.aboutToQuit.connect(cleanup)
    print("元素导航键盘原型已启动。")
    print("Ctrl+Alt+N 开始/退出，方向键移动，Enter 执行，Esc 退出，Ctrl+Alt+Q 关闭。")
    return int(app.exec())


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    if sys.platform != "win32":
        print("这个原型只支持 Windows。", file=sys.stderr)
        return 2
    return _run_windows(args)


if __name__ == "__main__":
    raise SystemExit(main())
