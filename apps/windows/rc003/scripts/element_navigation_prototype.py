"""Experimental keyboard-driven UI Automation spatial navigator.

This developer-only prototype is intentionally outside ``ovb_rc003``. It is
not imported by Remote Mic, included in candidate builds, or connected to the
RC003 input/configuration path. Its job is to answer one question cheaply:
does spatial element navigation feel useful in the user's fixed Windows apps?

Controls:
    Ctrl+Alt+N  scan the foreground window and enter/leave navigation
    Arrow keys  move the highlighted target
    Enter       enter/expand a group, or invoke a leaf target
    Esc         return to the parent group, or leave at the root
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
PRIMARY_ACTION_CONTROL_TYPES = frozenset(
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
        "SliderControl",
        "SpinnerControl",
    }
)
WRAPPER_CONTROL_TYPES = STRUCTURAL_CONTROL_TYPES | frozenset(
    {"ListItemControl", "DataItemControl"}
)
NOISE_NAME_PREFIXES = ("跳转到用户消息 ", "Jump to user message ")
CHROMIUM_RENDERER_CLASS = "Chrome_RenderWidgetHostHWND"
CHROMIUM_MIN_SCAN_DEPTH = 32
LIST_CONTAINER_TYPES = frozenset(
    {"ListControl", "TreeControl", "TableControl", "DataGridControl"}
)
ITEM_CONTAINER_TYPES = frozenset({"ListItemControl", "TreeItemControl"})


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

    def contains_point(self, point: tuple[int, int]) -> bool:
        x, y = point
        return self.left <= x <= self.right and self.top <= y <= self.bottom


@dataclass(frozen=True)
class TargetSnapshot:
    rect: Rect
    name: str
    control_type: str
    automation_id: str = ""
    path: tuple[int, ...] = ()
    depth: int = 0
    keyboard_focusable: bool = False
    has_action_pattern: bool = False
    supports_expand: bool = False


def physical_screen_rect(logical_rect: Rect, device_pixel_ratio: float) -> Rect:
    return Rect(
        logical_rect.left,
        logical_rect.top,
        logical_rect.left + round(logical_rect.width * device_pixel_ratio),
        logical_rect.top + round(logical_rect.height * device_pixel_ratio),
    )


def physical_to_screen_logical_rect(
    target: Rect, physical_screen: Rect, device_pixel_ratio: float
) -> Rect:
    return Rect(
        round((target.left - physical_screen.left) / device_pixel_ratio),
        round((target.top - physical_screen.top) / device_pixel_ratio),
        round((target.right - physical_screen.left) / device_pixel_ratio),
        round((target.bottom - physical_screen.top) / device_pixel_ratio),
    )


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
) -> Optional[tuple[int, float, float, float, float, int, int]]:
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
    # TV-style navigation should stay in the current visual lane when one
    # exists. Without this beam priority, a nearer sidebar item can beat a
    # farther control directly above or below the current target.
    beam_rank = 0 if overlap > 0 else 1
    return (
        beam_rank,
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
    cursor_point: Optional[tuple[int, int]] = None,
) -> int:
    if not targets:
        return -1
    if cursor_point is not None and window_rect.contains_point(cursor_point):
        containing = [
            (target.rect.width * target.rect.height, index)
            for index, target in enumerate(targets)
            if target.rect.contains_point(cursor_point)
        ]
        if containing:
            containing.sort()
            return containing[0][1]

        cursor_x, cursor_y = cursor_point

        def point_distance(index: int) -> tuple[int, float, int]:
            rect = targets[index].rect
            gap_x = max(rect.left - cursor_x, 0, cursor_x - rect.right)
            gap_y = max(rect.top - cursor_y, 0, cursor_y - rect.bottom)
            center_distance = abs(rect.center_x - cursor_x) + abs(
                rect.center_y - cursor_y
            )
            return (
                gap_x * gap_x + gap_y * gap_y,
                center_distance,
                rect.width * rect.height,
            )

        return min(range(len(targets)), key=point_distance)

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
    """Drop wrappers and secondary descendants around a primary action."""

    keep = []
    for index, target in enumerate(targets):
        area = target.rect.width * target.rect.height
        if (
            target.control_type in WRAPPER_CONTROL_TYPES
            and not target.supports_expand
        ):
            contains_specific_target = any(
                other_index != index
                and target.rect != other.rect
                and target.rect.contains(other.rect)
                and (
                    (
                        target.control_type
                        in {"ListItemControl", "DataItemControl"}
                        and other.control_type in PRIMARY_ACTION_CONTROL_TYPES
                        and other.path[: len(target.path)] == target.path
                    )
                    or area
                    > (other.rect.width * other.rect.height) * 1.5
                )
                for other_index, other in enumerate(targets)
            )
            if contains_specific_target:
                continue

        nested_under_primary_action = any(
            other_index != index
            and other.path
            and len(other.path) < len(target.path)
            and target.path[: len(other.path)] == other.path
            and other.control_type in PRIMARY_ACTION_CONTROL_TYPES
            and other.rect.contains(target.rect)
            for other_index, other in enumerate(targets)
        )
        if not nested_under_primary_action:
            keep.append(index)
    return keep


def target_quality_rank(target: TargetSnapshot) -> tuple[int, int, int, int]:
    """Rank same-rectangle candidates by how directly they can be operated."""

    primary_type = target.control_type in PRIMARY_ACTION_CONTROL_TYPES
    return (
        int(target.has_action_pattern),
        int(target.keyboard_focusable),
        int(primary_type),
        target.depth,
    )


def discover_group_scopes(
    targets: Sequence[TargetSnapshot],
    node_types: dict[tuple[int, ...], str],
) -> dict[tuple[int, ...], int]:
    """Map a folder-like scope path to its expandable representative target."""

    groups: dict[tuple[int, ...], int] = {}
    for index, target in enumerate(targets):
        if (
            not target.supports_expand
            or not target.path
            or target.rect.width < 120
        ):
            continue
        parent_path = target.path[:-1]
        inside_item_container = target.control_type == "TreeItemControl" or any(
            node_types.get(parent_path[:depth]) in ITEM_CONTAINER_TYPES
            for depth in range(1, len(parent_path) + 1)
        )
        if not inside_item_container:
            continue
        has_list_child = any(
            len(path) == len(parent_path) + 1
            and path[:-1] == parent_path
            and control_type in LIST_CONTAINER_TYPES
            for path, control_type in node_types.items()
        )
        has_child_target = any(
            other_index != index
            and len(other.path) > len(parent_path)
            and other.path[: len(parent_path)] == parent_path
            and not (
                len(other.path) >= len(target.path)
                and other.path[: len(target.path)] == target.path
            )
            for other_index, other in enumerate(targets)
        )
        if has_list_child and has_child_target:
            groups[parent_path] = index
    return groups


def scope_target_indices(
    targets: Sequence[TargetSnapshot],
    groups: dict[tuple[int, ...], int],
    scope_path: tuple[int, ...] = (),
) -> list[int]:
    """Return targets visible at one navigation level."""

    visible: list[int] = []
    current_representative = groups.get(scope_path)
    for index, target in enumerate(targets):
        if scope_path and (
            len(target.path) <= len(scope_path)
            or target.path[: len(scope_path)] != scope_path
        ):
            continue
        if index == current_representative:
            continue

        hidden_by_child_group = False
        for group_path, representative in groups.items():
            if group_path == scope_path:
                continue
            is_nested_group = (
                len(group_path) > len(scope_path)
                and group_path[: len(scope_path)] == scope_path
            )
            if (
                is_nested_group
                and len(target.path) > len(group_path)
                and target.path[: len(group_path)] == group_path
                and index != representative
            ):
                hidden_by_child_group = True
                break
        if not hidden_by_child_group:
            visible.append(index)
    return visible


def restore_target_index(
    targets: Sequence[TargetSnapshot], previous: TargetSnapshot
) -> int:
    if not targets:
        return -1

    def score(index: int) -> tuple[int, float, float]:
        candidate = targets[index]
        if (
            previous.automation_id
            and candidate.automation_id == previous.automation_id
            and candidate.control_type == previous.control_type
        ):
            identity_rank = 0
        elif (
            previous.name
            and candidate.name == previous.name
            and candidate.control_type == previous.control_type
        ):
            identity_rank = 1
        elif candidate.control_type == previous.control_type:
            identity_rank = 2
        else:
            identity_rank = 3
        center_distance = abs(candidate.rect.center_x - previous.rect.center_x) + abs(
            candidate.rect.center_y - previous.rect.center_y
        )
        size_distance = abs(candidate.rect.width - previous.rect.width) + abs(
            candidate.rect.height - previous.rect.height
        )
        return identity_rank, center_distance, size_distance

    return min(range(len(targets)), key=score)


def is_navigation_noise(name: str) -> bool:
    return any(name.startswith(prefix) for prefix in NOISE_NAME_PREFIXES)


def effective_scan_depth(configured_depth: int, has_chromium_renderer: bool) -> int:
    if has_chromium_renderer:
        return max(configured_depth, CHROMIUM_MIN_SCAN_DEPTH)
    return configured_depth


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
    lresult = ctypes.c_ssize_t

    kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    user32.GetCursorPos.restype = wintypes.BOOL
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
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
        auto.PatternId.LegacyIAccessiblePattern,
    )
    direct_action_pattern_ids = action_pattern_ids[:-1]
    wm_getobject = 0x003D
    smto_abortifhung = 0x0002
    accessibility_object_ids = (-25, -4)

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

    def supports_any_pattern(control: Any, pattern_ids: Sequence[int]) -> bool:
        for pattern_id in pattern_ids:
            try:
                if control.GetPattern(pattern_id) is not None:
                    return True
            except Exception:
                continue
        return False

    def window_class_name(hwnd: int) -> str:
        buffer = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buffer, len(buffer))
        return buffer.value

    def activate_embedded_chromium_accessibility(hwnd: int) -> bool:
        """Ask Chromium renderers to publish their UI Automation tree."""

        handles = [hwnd]

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def collect_child(child: int, _lparam: int) -> bool:
            handles.append(child)
            return True

        user32.EnumChildWindows(hwnd, collect_child, 0)
        has_renderer = any(
            window_class_name(handle) == CHROMIUM_RENDERER_CLASS
            for handle in handles
        )
        if not has_renderer:
            return False

        for handle in handles:
            for object_id in accessibility_object_ids:
                result = ctypes.c_size_t()
                user32.SendMessageTimeoutW(
                    handle,
                    wm_getobject,
                    0,
                    object_id,
                    smto_abortifhung,
                    100,
                    ctypes.byref(result),
                )

        # Chromium enables renderer accessibility asynchronously after the
        # probe. A short bounded wait keeps the first scan from racing it.
        time.sleep(0.15)
        return True

    def enumerate_targets(
        hwnd: int,
    ) -> tuple[
        list[RuntimeTarget],
        dict[tuple[int, ...], str],
        Rect,
        str,
        int,
    ]:
        has_chromium_renderer = activate_embedded_chromium_accessibility(hwnd)
        scan_depth = effective_scan_depth(args.max_depth, has_chromium_renderer)
        root = auto.ControlFromHandle(hwnd)
        if root is None:
            raise RuntimeError("无法从当前窗口建立 UI Automation 根元素")
        window_rect = rect_from_control(root)
        window_name = str(root.Name or "未命名窗口")
        pending = deque([(root, 0, ())])
        by_rect: dict[Rect, RuntimeTarget] = {}
        node_types: dict[tuple[int, ...], str] = {}
        visited = 0

        while pending and visited < args.max_nodes and len(by_rect) < args.max_elements:
            control, depth, path = pending.popleft()
            visited += 1
            if depth > 0:
                try:
                    control_type = str(control.ControlTypeName or "")
                    node_types[path] = control_type
                    enabled = bool(control.IsEnabled)
                    offscreen = bool(control.IsOffscreen)
                    rect = rect_from_control(control)
                    name = str(control.Name or "").strip()
                    automation_id = str(control.AutomationId or "").strip()
                    valid_size = 16 <= rect.width <= 1800 and 16 <= rect.height <= 1400
                    standard = control_type in interactive_types
                    structural = control_type in STRUCTURAL_CONTROL_TYPES
                    keyboard_focusable = bool(control.IsKeyboardFocusable)
                    action_pattern = supports_any_pattern(control, action_pattern_ids)
                    direct_action_pattern = supports_any_pattern(
                        control, direct_action_pattern_ids
                    )
                    actionable = (
                        standard and (keyboard_focusable or action_pattern)
                    ) or (
                        structural
                        and bool(name or automation_id)
                        and (keyboard_focusable or direct_action_pattern)
                    )
                    if (
                        actionable
                        and enabled
                        and not offscreen
                        and valid_size
                        and not is_navigation_noise(name)
                        and rect.intersects(window_rect)
                    ):
                        candidate = RuntimeTarget(
                            TargetSnapshot(
                                rect,
                                name,
                                control_type,
                                automation_id,
                                path,
                                depth,
                                keyboard_focusable,
                                action_pattern,
                                control.GetPattern(auto.PatternId.ExpandCollapsePattern)
                                is not None,
                            ),
                            control,
                        )
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

            if depth >= scan_depth:
                continue
            try:
                for child_index, child in enumerate(control.GetChildren()):
                    pending.append((child, depth + 1, path + (child_index,)))
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
        return targets, node_types, window_rect, window_name, visited

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

    def expand_state(control: Any) -> Optional[int]:
        try:
            pattern = control.GetPattern(auto.PatternId.ExpandCollapsePattern)
            if pattern is None:
                return None
            return int(pattern.ExpandCollapseState)
        except Exception:
            return None

    def set_expanded(control: Any, expanded: bool) -> Optional[str]:
        try:
            pattern = control.GetPattern(auto.PatternId.ExpandCollapsePattern)
            if pattern is None:
                return None
            state = int(pattern.ExpandCollapseState)
            if expanded and state == 0:
                pattern.Expand(waitTime=0)
                return "Expand"
            if not expanded and state in (1, 2):
                pattern.Collapse(waitTime=0)
                return "Collapse"
            return "Expanded" if state in (1, 2) else "Collapsed"
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
            self.all_targets: list[RuntimeTarget] = []
            self.targets: list[RuntimeTarget] = []
            self.node_types: dict[tuple[int, ...], str] = {}
            self.groups: dict[tuple[int, ...], int] = {}
            self.scope_stack: list[TargetSnapshot] = []
            self.scope_path: tuple[int, ...] = ()
            self.selected = -1
            self.hwnd = 0
            self.window_rect = Rect(0, 0, 0, 0)
            self.window_name = ""
            self.visited = 0
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

        @staticmethod
        def _same_identity(first: TargetSnapshot, second: TargetSnapshot) -> bool:
            if (
                first.automation_id
                and first.automation_id == second.automation_id
                and first.control_type == second.control_type
            ):
                return True
            if (
                first.name
                and first.name == second.name
                and first.control_type == second.control_type
            ):
                return True
            return first.control_type == second.control_type and first.rect == second.rect

        def _enumerate(self, hwnd: int) -> None:
            (
                self.all_targets,
                self.node_types,
                self.window_rect,
                self.window_name,
                self.visited,
            ) = enumerate_targets(hwnd)
            self.groups = discover_group_scopes(
                [target.snapshot for target in self.all_targets], self.node_types
            )
            self.hwnd = hwnd

        def _resolve_scope_path(self) -> tuple[int, ...]:
            resolved_path: tuple[int, ...] = ()
            resolved_frames: list[TargetSnapshot] = []
            for frame in self.scope_stack:
                candidates = [
                    (path, self.all_targets[index].snapshot)
                    for path, index in self.groups.items()
                    if len(path) > len(resolved_path)
                    and path[: len(resolved_path)] == resolved_path
                ]
                matching = [
                    (path, snapshot)
                    for path, snapshot in candidates
                    if self._same_identity(frame, snapshot)
                ]
                if not matching:
                    break
                resolved_path, resolved_snapshot = min(
                    matching,
                    key=lambda item: (
                        abs(item[1].rect.center_x - frame.rect.center_x)
                        + abs(item[1].rect.center_y - frame.rect.center_y)
                    ),
                )
                resolved_frames.append(resolved_snapshot)
            self.scope_stack = resolved_frames
            return resolved_path

        def _apply_scope(
            self,
            restore: Optional[TargetSnapshot] = None,
            focused: Optional[Rect] = None,
            use_cursor: bool = False,
        ) -> None:
            self.scope_path = self._resolve_scope_path()
            snapshots = [target.snapshot for target in self.all_targets]
            visible_indices = scope_target_indices(
                snapshots, self.groups, self.scope_path
            )
            self.targets = [self.all_targets[index] for index in visible_indices]
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

        def _selection_payload(self) -> dict[str, Any]:
            snapshots = [target.snapshot for target in self.targets]
            return {
                "target": snapshots[self.selected],
                "selected": self.selected,
                "count": len(snapshots),
                "scope_depth": len(self.scope_stack),
            }

        def _emit_selection(self) -> None:
            if self.targets and self.selected >= 0:
                self.events.put(("selection", self._selection_payload()))

        def _group_path_for_target(
            self, target: RuntimeTarget
        ) -> Optional[tuple[int, ...]]:
            for path, index in self.groups.items():
                if self.all_targets[index].snapshot.path == target.snapshot.path:
                    return path
            for path, index in self.groups.items():
                if self._same_identity(
                    self.all_targets[index].snapshot, target.snapshot
                ):
                    return path
            return None

        def _enter_group(self, target: RuntimeTarget) -> bool:
            group_path = self._group_path_for_target(target)
            if group_path is None:
                return False
            self.scope_stack.append(target.snapshot)
            self._apply_scope(focused=target.snapshot.rect)
            if self.selected < 0:
                self.scope_stack.pop()
                self._apply_scope(restore=target.snapshot)
                return False
            self._emit_selection()
            return True

        def _scan(self, hwnd: int) -> None:
            started = time.perf_counter()
            self.scope_stack = []
            self._enumerate(hwnd)
            self._apply_scope(focused=focused_rect(), use_cursor=True)
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
                        "elapsed": elapsed,
                    },
                )
            )

        def _refresh_after_expand(
            self, previous: TargetSnapshot, enter_group: bool
        ) -> None:
            frames = list(self.scope_stack)
            time.sleep(0.18)
            self._enumerate(self.hwnd)
            self.scope_stack = frames
            self._apply_scope(restore=previous)
            if not self.targets or self.selected < 0:
                self._emit_selection()
                return
            refreshed = self.targets[self.selected]
            if enter_group and self._enter_group(refreshed):
                return
            self._emit_selection()

        def _activate(self) -> None:
            if not self.targets or self.selected < 0:
                return
            target = self.targets[self.selected]
            group_path = self._group_path_for_target(target)
            state = expand_state(target.control) if target.snapshot.supports_expand else None
            if state == 0:
                method = set_expanded(target.control, True)
                self._refresh_after_expand(target.snapshot, enter_group=True)
                self.events.put(
                    (
                        "expanded",
                        {"target": target.snapshot, "method": method or "Expand"},
                    )
                )
                return
            if group_path is not None and self._enter_group(target):
                return
            if state in (1, 2):
                method = set_expanded(target.control, False)
                self._refresh_after_expand(target.snapshot, enter_group=False)
                self.events.put(
                    (
                        "expanded",
                        {"target": target.snapshot, "method": method or "Collapse"},
                    )
                )
                return

            method = smart_invoke(target.control)
            self.events.put(
                (
                    "activated",
                    {"target": target.snapshot, "method": method},
                )
            )

        def _back(self) -> None:
            if not self.scope_stack:
                self.events.put(("exit_requested", None))
                return
            parent_target = self.scope_stack.pop()
            self._apply_scope(restore=parent_target)
            self._emit_selection()

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
                            self._emit_selection()
                        elif command == "activate" and self.targets and self.selected >= 0:
                            self._activate()
                        elif command == "back":
                            self._back()
                    except Exception as exc:
                        self.events.put(("error", str(exc)))
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
            self, target: TargetSnapshot, selected: int, count: int
        ) -> None:
            self._target = target
            self._position = f"{selected + 1}/{count}  {target.name or target.control_type}"
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
            targets, node_types, _rect, window_name, visited = enumerate_targets(hwnd)
            snapshots = [target.snapshot for target in targets]
            groups = discover_group_scopes(snapshots, node_types)
            visible = [targets[index] for index in scope_target_indices(snapshots, groups)]
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
            if active.is_set():
                worker.post("back")
        elif action == "activate" and active.is_set():
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
                    f"已扫描 {payload['window']}: 根层 {len(targets)} 个，"
                    f"全部 {payload['all_count']} 个，"
                    f"访问 {payload['visited']} 个节点，耗时 {payload['elapsed']:.2f}s"
                )
                if selected < 0:
                    leave_navigation()
                    print("没有找到可导航元素。")
                else:
                    active.set()
                    overlay.show_target(targets[selected], selected, len(targets))
            elif event == "selection":
                active.set()
                overlay.show_target(
                    payload["target"], payload["selected"], payload["count"]
                )
            elif event == "expanded":
                target = payload["target"]
                print(
                    f"已切换: {target.name or target.control_type} "
                    f"({payload['method']})"
                )
            elif event == "activated":
                target = payload["target"]
                print(
                    f"已执行: {target.name or target.control_type} "
                    f"({payload['method']})"
                )
                leave_navigation()
            elif event == "exit_requested":
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
    print(
        "Ctrl+Alt+N 开始/退出，方向键移动，Enter 进入/执行，"
        "Esc 返回/退出，Ctrl+Alt+Q 关闭。"
    )
    return int(app.exec())


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    if sys.platform != "win32":
        print("这个原型只支持 Windows。", file=sys.stderr)
        return 2
    return _run_windows(args)


if __name__ == "__main__":
    raise SystemExit(main())
