"""Experimental keyboard-driven UI Automation spatial navigator.

This developer-only prototype is intentionally outside ``ovb_rc003``. It is
not imported by Remote Mic, included in candidate builds, or connected to the
RC003 input/configuration path. Its job is to answer one question cheaply:
does spatial element navigation feel useful in the user's fixed Windows apps?

Controls:
    Ctrl+Alt+N  scan the foreground window and enter/leave navigation
    Arrow keys  move the highlighted target
    PageUp/Down move to the parent/child element at the same location
    Enter       expand/collapse a group, or invoke a leaf target
    Esc         leave navigation
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
from dataclasses import dataclass, replace
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
PRESERVED_NESTED_ACTION_NAMES = frozenset(
    {
        "复制",
        "复制消息",
        "从这里创建聊天分支",
        "Copy",
        "Copy message",
        "Branch in new chat",
    }
)
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
    runtime_id: tuple[int, ...] = ()
    source: str = "uia"


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
        float(primary_gap),
        float(perpendicular_gap),
        score,
        center_offset,
        candidate.top,
        candidate.left,
    )


def next_target_index(
    targets: Sequence[TargetSnapshot], current_index: int, direction: Direction
) -> int:
    ranked = ranked_target_indices(targets, current_index, direction)
    return ranked[0] if ranked else current_index


def _common_path_prefix_length(
    first: tuple[int, ...], second: tuple[int, ...]
) -> int:
    length = 0
    for first_part, second_part in zip(first, second):
        if first_part != second_part:
            break
        length += 1
    return length


def horizontal_wrap_target_indices(
    targets: Sequence[TargetSnapshot], current_index: int, direction: Direction
) -> list[int]:
    """Wrap right/left to a nearby visual row when the current row ends."""

    if direction not in {Direction.RIGHT, Direction.LEFT}:
        return []
    if not targets or not 0 <= current_index < len(targets):
        return []
    current = targets[current_index]
    candidates: list[tuple[int, int, float, float, int]] = []
    for index, candidate in enumerate(targets):
        if index == current_index:
            continue
        vertical_delta = candidate.rect.center_y - current.rect.center_y
        minimum_row_delta = max(
            12.0, min(current.rect.height, candidate.rect.height) * 0.60
        )
        minimum_horizontal_reset = max(
            24.0, min(current.rect.width, candidate.rect.width) * 0.35
        )
        if direction == Direction.RIGHT:
            if (
                vertical_delta < minimum_row_delta
                or current.rect.center_x - candidate.rect.center_x
                < minimum_horizontal_reset
            ):
                continue
            row_gap = max(0, candidate.rect.top - current.rect.bottom)
            edge_order = candidate.rect.left
        else:
            if (
                vertical_delta > -minimum_row_delta
                or candidate.rect.center_x - current.rect.center_x
                < minimum_horizontal_reset
            ):
                continue
            row_gap = max(0, current.rect.top - candidate.rect.bottom)
            edge_order = -candidate.rect.right

        max_row_gap = max(
            96.0,
            min(
                320.0,
                max(current.rect.height, candidate.rect.height) * 4.0,
            ),
        )
        if row_gap > max_row_gap:
            continue
        common_prefix = _common_path_prefix_length(current.path, candidate.path)
        if current.path and candidate.path and common_prefix == 0:
            continue
        candidates.append(
            (
                row_gap,
                abs(vertical_delta),
                -common_prefix,
                edge_order,
                index,
            )
        )

    candidates.sort()
    return [index for *_score, index in candidates]


def ranked_target_indices(
    targets: Sequence[TargetSnapshot], current_index: int, direction: Direction
) -> list[int]:
    if not targets or not 0 <= current_index < len(targets):
        return []
    current = targets[current_index].rect
    scored: list[
        tuple[tuple[int, float, float, float, float, int, int], int, int]
    ] = []
    for index, target in enumerate(targets):
        if index == current_index:
            continue
        score = direction_score(current, target.rect, direction)
        if score is not None:
            common_prefix = _common_path_prefix_length(
                targets[current_index].path, target.path
            )
            scored.append((score, common_prefix, index))
    affinity_unit = max(
        48.0,
        min(120.0, max(current.width, current.height) * 0.55),
    )

    def rank_key(
        item: tuple[
            tuple[int, float, float, float, float, int, int], int, int
        ],
    ) -> tuple[float, ...]:
        score, common_prefix, _index = item
        path_bonus = min(common_prefix, 4) * (
            24.0 if score[0] == 0 else affinity_unit
        )
        return (
            float(score[0]),
            max(0.0, score[1] - path_bonus),
            score[2],
            score[3] - path_bonus,
            score[4],
            float(-common_prefix),
            float(score[5]),
            float(score[6]),
        )

    scored.sort(key=rank_key)
    if direction not in {Direction.RIGHT, Direction.LEFT}:
        return [index for _score, _prefix, index in scored]

    # Horizontal navigation behaves like a reading-order grid: finish the
    # current row, wrap to the adjacent row inside the same content branch,
    # then consider diagonal targets in the requested half-plane.
    in_row = [index for score, _prefix, index in scored if score[0] == 0]
    diagonal = [index for score, _prefix, index in scored if score[0] != 0]
    wrapped = horizontal_wrap_target_indices(targets, current_index, direction)
    ranked = list(in_row)
    ranked.extend(index for index in wrapped if index not in ranked)
    ranked.extend(index for index in diagonal if index not in ranked)
    return ranked


OPPOSITE_DIRECTION = {
    Direction.UP: Direction.DOWN,
    Direction.DOWN: Direction.UP,
    Direction.LEFT: Direction.RIGHT,
    Direction.RIGHT: Direction.LEFT,
}


class NavigationGraph:
    """Lazily cache four-way neighbors for one stable target layout."""

    def __init__(self, targets: Sequence[TargetSnapshot]) -> None:
        self.targets = tuple(targets)
        self._ranked: dict[tuple[int, Direction], tuple[int, ...]] = {}

    def candidates(self, current_index: int, direction: Direction) -> tuple[int, ...]:
        key = (current_index, direction)
        cached = self._ranked.get(key)
        if cached is not None:
            return cached

        ranked = tuple(
            ranked_target_indices(self.targets, current_index, direction)
        )
        self._ranked[key] = ranked
        if ranked:
            neighbor = ranked[0]
            reverse_key = (neighbor, OPPOSITE_DIRECTION[direction])
            if reverse_key not in self._ranked:
                reverse = tuple(
                    ranked_target_indices(
                        self.targets,
                        neighbor,
                        OPPOSITE_DIRECTION[direction],
                    )
                )
                if not reverse or reverse[0] == current_index:
                    self._ranked[reverse_key] = (
                        (current_index,) + tuple(
                            index for index in reverse if index != current_index
                        )
                    )
        return ranked


def geometry_anchor_indices(count: int, selected: int) -> list[int]:
    if count <= 0:
        return []
    candidates = [selected, 0, count // 2, count - 1]
    return list(dict.fromkeys(index for index in candidates if 0 <= index < count))


def shifted_snapshot(target: TargetSnapshot, delta_x: int, delta_y: int) -> TargetSnapshot:
    rect = target.rect
    return replace(
        target,
        rect=Rect(
            rect.left + delta_x,
            rect.top + delta_y,
            rect.right + delta_x,
            rect.bottom + delta_y,
        ),
    )


def target_probe_points(rect: Rect) -> list[tuple[int, int]]:
    """Return stable in-bounds hit-test points for sparse clickable regions."""

    inset_x = max(2, min(24, (rect.width - 1) // 4))
    inset_y = max(2, min(12, (rect.height - 1) // 4))
    center_x = round(rect.center_x)
    center_y = round(rect.center_y)
    points = [
        (center_x, center_y),
        (rect.left + inset_x, center_y),
        (rect.right - inset_x, center_y),
        (rect.left + inset_x, rect.top + inset_y),
        (rect.left + inset_x, rect.bottom - inset_y),
    ]
    return list(dict.fromkeys(points))


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


def hit_target_match_index(
    targets: Sequence[TargetSnapshot],
    hit_rect: Rect,
    runtime_id: tuple[int, ...] = (),
) -> int:
    """Map a point-hit element back to the enumerated navigation candidates."""

    if not targets:
        return -1
    if runtime_id:
        for index, target in enumerate(targets):
            if target.runtime_id == runtime_id:
                return index

    exact = [index for index, target in enumerate(targets) if target.rect == hit_rect]
    if exact:
        return max(exact, key=lambda index: target_quality_rank(targets[index]))

    hit_area = max(1, hit_rect.width * hit_rect.height)
    matches: list[tuple[float, float, int, int]] = []
    for index, target in enumerate(targets):
        intersection_width = max(
            0, min(hit_rect.right, target.rect.right) - max(hit_rect.left, target.rect.left)
        )
        intersection_height = max(
            0, min(hit_rect.bottom, target.rect.bottom) - max(hit_rect.top, target.rect.top)
        )
        intersection = intersection_width * intersection_height
        if intersection <= 0:
            continue
        target_area = max(1, target.rect.width * target.rect.height)
        overlap = intersection / min(hit_area, target_area)
        size_ratio = min(hit_area, target_area) / max(hit_area, target_area)
        if overlap < 0.70 or size_ratio < 0.45:
            continue
        edge_delta = (
            abs(hit_rect.left - target.rect.left)
            + abs(hit_rect.top - target.rect.top)
            + abs(hit_rect.right - target.rect.right)
            + abs(hit_rect.bottom - target.rect.bottom)
        )
        matches.append((overlap, size_ratio, -edge_delta, index))

    if not matches:
        return -1
    return max(matches)[-1]


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
            and target.name not in PRESERVED_NESTED_ACTION_NAMES
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


def flat_target_indices(targets: Sequence[TargetSnapshot]) -> list[int]:
    """Keep every currently visible target in one flat navigation surface."""

    return list(range(len(targets)))


def restore_target_index(
    targets: Sequence[TargetSnapshot], previous: TargetSnapshot
) -> int:
    if not targets:
        return -1

    def score(index: int) -> tuple[int, float, float]:
        candidate = targets[index]
        if (
            previous.runtime_id
            and candidate.runtime_id == previous.runtime_id
        ):
            identity_rank = 0
        elif (
            previous.automation_id
            and candidate.automation_id == previous.automation_id
            and candidate.control_type == previous.control_type
        ):
            identity_rank = 1
        elif (
            previous.name
            and candidate.name == previous.name
            and candidate.control_type == previous.control_type
        ):
            identity_rank = 2
        elif candidate.control_type == previous.control_type:
            identity_rank = 3
        else:
            identity_rank = 4
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


def structural_action_has_identity(
    control_type: str, name: str, automation_id: str
) -> bool:
    if control_type in {"GroupControl", "PaneControl"}:
        return bool(name)
    return bool(name or automation_id)


def semantic_action_can_bypass_point_hit(target: TargetSnapshot) -> bool:
    return bool(
        target.name
        and target.has_action_pattern
        and target.control_type in PRIMARY_ACTION_CONTROL_TYPES
    )


def path_is_in_branch(
    path: tuple[int, ...], branch_path: tuple[int, ...]
) -> bool:
    return bool(
        len(path) > len(branch_path)
        and path[: len(branch_path)] == branch_path
    )


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
    oleacc = ctypes.WinDLL("oleacc")
    oleaut32 = ctypes.WinDLL("oleaut32")
    ole32 = ctypes.WinDLL("ole32")
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

    kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    ]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    user32.GetCursorPos.restype = wintypes.BOOL
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

    def runtime_target_from_control(
        control: Any,
        window_rect: Rect,
        path: tuple[int, ...] = (),
        depth: int = 0,
        source: str = "uia",
    ) -> Optional[RuntimeTarget]:
        try:
            control_type = str(control.ControlTypeName or "")
            standard = control_type in interactive_types
            structural = control_type in STRUCTURAL_CONTROL_TYPES
            if not standard and not structural:
                return None
            name = str(control.Name or "").strip()
            automation_id = str(control.AutomationId or "").strip()
            if structural and not structural_action_has_identity(
                control_type, name, automation_id
            ):
                return None
            enabled = bool(control.IsEnabled)
            offscreen = bool(control.IsOffscreen)
            rect = rect_from_control(control)
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
            actionable = (
                standard and (keyboard_focusable or action_pattern)
            ) or (
                structural
                and (keyboard_focusable or direct_action_pattern)
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

    def click_rect_center(rect: Rect) -> None:
        click_point((round(rect.center_x), round(rect.center_y)))

    def click_point(point: tuple[int, int]) -> None:
        user32.SetCursorPos(point[0], point[1])
        user32.mouse_event(mouseeventf_leftdown, 0, 0, 0, 0)
        user32.mouse_event(mouseeventf_leftup, 0, 0, 0, 0)

    def window_class_name(hwnd: int) -> str:
        buffer = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buffer, len(buffer))
        return buffer.value

    def window_process_id(hwnd: int) -> int:
        process_id = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
        return int(process_id.value)

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
        if hwnd in awakened_chromium_windows:
            return True

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
    ) -> tuple[list[RuntimeTarget], dict[tuple[int, ...], str], int]:
        pending = deque([(root, 0, root_path)])
        by_rect: dict[Rect, RuntimeTarget] = {}
        node_types: dict[tuple[int, ...], str] = {}
        visited = 0

        while pending and visited < args.max_nodes and len(by_rect) < args.max_elements:
            control, relative_depth, path = pending.popleft()
            visited += 1
            try:
                control_type = str(control.ControlTypeName or "")
                if path:
                    node_types[path] = control_type
                if relative_depth > 0:
                    candidate = runtime_target_from_control(
                        control,
                        window_rect,
                        path=path,
                        depth=root_depth + relative_depth,
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
        return targets, node_types, visited

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
        targets, node_types, visited = collect_targets(
            root,
            window_rect,
            (),
            0,
            scan_depth,
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

    def point_hierarchy_targets(
        point: tuple[int, int],
        window_rect: Rect,
        existing_targets: Sequence[RuntimeTarget],
    ) -> list[RuntimeTarget]:
        """Return actionable UIA ancestors under the point, leaf first."""

        hierarchy: list[RuntimeTarget] = []
        seen_runtime_ids: set[tuple[int, ...]] = set()
        seen_geometry: set[tuple[Rect, str]] = set()
        try:
            control = auto.ControlFromPoint(point[0], point[1])
        except Exception:
            control = None

        for depth in range(40):
            if control is None:
                break
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

        msaa_rect = msaa_rect_at_point(point)
        if msaa_rect is None or not msaa_rect.intersects(window_rect):
            return []
        match = hit_target_match_index(
            [target.snapshot for target in existing_targets], msaa_rect
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

    def toggle_expanded(control: Any) -> Optional[tuple[str, bool]]:
        try:
            pattern = control.GetPattern(auto.PatternId.ExpandCollapsePattern)
            if pattern is None:
                return None
            state = int(pattern.ExpandCollapseState)
            if state == 0:
                pattern.Expand(waitTime=0)
                return "Expand", True
            if state in (1, 2):
                pattern.Collapse(waitTime=0)
                return "Collapse", False
            return None
        except Exception:
            return None

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

    def click_target(target: RuntimeTarget) -> str:
        control = target.control
        if control is None:
            click_rect_center(target.snapshot.rect)
            return "MSAA coordinate click"
        if target.click_point is not None:
            click_point(target.click_point)
            return "verified coordinate click"
        control.Click(simulateMove=False, waitTime=0)
        return "mouse fallback"

    class AutomationWorker:
        _PENDING_LIMITS = {
            "prewarm": 1,
            "move": 2,
            "parent": 1,
            "child": 1,
            "activate": 1,
            "back": 1,
            "sync_window": 1,
        }
        _NAVIGATION_COMMANDS = frozenset(
            {"move", "parent", "child", "activate", "back", "sync_window"}
        )
        _CACHE_TTL_SECONDS = 15.0

        def __init__(self) -> None:
            self.commands: queue.Queue[tuple[str, Any, int]] = queue.Queue()
            self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
            self._post_lock = threading.Lock()
            self._generation = 0
            self._pending_counts: dict[str, int] = {}
            self.context_valid = False
            self.all_targets: list[RuntimeTarget] = []
            self.targets: list[RuntimeTarget] = []
            self.navigation_graph = NavigationGraph(())
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
                        return
                    self._pending_counts[command] = pending + 1
                generation = self._generation
            self.commands.put((command, value, generation))

        def deactivate(self) -> None:
            with self._post_lock:
                self._generation += 1
                self.context_valid = False

        def stop(self) -> None:
            self.post("stop")
            self._thread.join(timeout=2)

        @staticmethod
        def _same_identity(first: TargetSnapshot, second: TargetSnapshot) -> bool:
            if first.runtime_id and second.runtime_id:
                return first.runtime_id == second.runtime_id
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

                for point in target_probe_points(live_rect):
                    control = auto.ControlFromPoint(point[0], point[1])
                    for _depth in range(40):
                        if control is None:
                            break
                        runtime_id = runtime_id_from_control(control)
                        if (
                            target.snapshot.runtime_id
                            and runtime_id == target.snapshot.runtime_id
                        ):
                            target.click_point = point
                            return True
                        try:
                            control_type = str(control.ControlTypeName or "")
                            name = str(control.Name or "").strip()
                            rect = rect_from_control(control)
                        except Exception:
                            control_type = ""
                            name = ""
                            rect = Rect(0, 0, 0, 0)
                        if (
                            not target.snapshot.runtime_id
                            and control_type == target.snapshot.control_type
                            and rect == live_rect
                            and (not target.snapshot.name or name == target.snapshot.name)
                        ):
                            target.click_point = point
                            return True
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

        def _sync_window_geometry(self) -> bool:
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
                    self._invalidate_navigation("页面内容已经滚动或重新排版")
                    return False
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
                self.window_rect = current_window_rect
                self.invalid_targets.clear()
                self.events.put(
                    (
                        "geometry_synced",
                        {"delta_x": delta_x, "delta_y": delta_y},
                    )
                )
                self._emit_selection()
                return True

            previous = (
                self.targets[self.selected].snapshot
                if 0 <= self.selected < len(self.targets)
                else None
            )
            self._enumerate(self.hwnd)
            self._apply_targets(restore=previous)
            self._clear_hierarchy()
            self.events.put(("geometry_rescanned", None))
            self._emit_selection()
            return True

        def _enumerate(self, hwnd: int, activate_context: bool = True) -> None:
            (
                self.all_targets,
                self.node_types,
                self.window_rect,
                self.window_name,
                self.visited,
            ) = enumerate_targets(hwnd)
            self.hwnd = hwnd
            self.invalid_targets.clear()
            self.context_valid = activate_context
            self.cache_timestamp = time.perf_counter()

        def _cache_is_reusable(self, hwnd: int) -> bool:
            if (
                hwnd != self.hwnd
                or not self.all_targets
                or time.perf_counter() - self.cache_timestamp
                > self._CACHE_TTL_SECONDS
            ):
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

        def _prewarm(self, hwnd: int) -> None:
            if hwnd <= 0 or self._cache_is_reusable(hwnd):
                return
            started = time.perf_counter()
            self._enumerate(hwnd, activate_context=False)
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

        def _move(self, direction: Direction) -> None:
            if not self._sync_window_geometry():
                return
            for next_index in self.navigation_graph.candidates(
                self.selected, direction
            ):
                target = self.targets[next_index]
                token = self._identity_token(target.snapshot)
                if token in self.invalid_targets:
                    continue
                previous_rect = target.snapshot.rect
                if not self._target_is_navigable(target):
                    self.invalid_targets.add(token)
                    self.events.put(("target_skipped", target.snapshot))
                    continue
                if target.snapshot.rect != previous_rect:
                    self._invalidate_navigation("页面内容已经滚动或重新排版")
                    return
                self.selected = next_index
                self._clear_hierarchy()
                self._emit_selection()
                return

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

        def _scan(self, hwnd: int) -> None:
            started = time.perf_counter()
            point = cursor_point()
            used_cache = self._cache_is_reusable(hwnd)
            if used_cache:
                self.context_valid = True
                self.invalid_targets.clear()
            else:
                self._enumerate(hwnd)
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
                    },
                )
            )

        def _refresh_branch(
            self,
            target: RuntimeTarget,
        ) -> bool:
            if target.control is None or not target.snapshot.path:
                return False
            try:
                parent = target.control.GetParentControl()
            except Exception:
                return False
            if parent is None:
                return False

            branch_path = target.snapshot.path[:-1]
            branch_targets, branch_node_types, visited = collect_targets(
                parent,
                self.window_rect,
                branch_path,
                len(branch_path),
                max(args.max_depth, 16),
            )
            if not branch_targets:
                return False

            self.all_targets = [
                existing
                for existing in self.all_targets
                if not path_is_in_branch(existing.snapshot.path, branch_path)
            ]
            self.all_targets = normalize_runtime_targets(
                [*self.all_targets, *branch_targets]
            )
            self.node_types = {
                path: control_type
                for path, control_type in self.node_types.items()
                if not path_is_in_branch(path, branch_path)
            }
            self.node_types.update(branch_node_types)
            self.visited = visited
            self.invalid_targets.clear()
            return True

        def _refresh_cached_geometry(self) -> bool:
            refreshed: list[RuntimeTarget] = []
            for target in self.all_targets:
                if target.control is None:
                    if target.snapshot.rect.intersects(self.window_rect):
                        refreshed.append(target)
                    continue
                try:
                    if not bool(target.control.IsEnabled) or bool(
                        target.control.IsOffscreen
                    ):
                        continue
                    live_rect = rect_from_control(target.control)
                except Exception:
                    continue
                if (
                    live_rect.width < 16
                    or live_rect.height < 16
                    or not live_rect.intersects(self.window_rect)
                ):
                    continue
                if live_rect != target.snapshot.rect:
                    target.snapshot = replace(target.snapshot, rect=live_rect)
                target.click_point = None
                refreshed.append(target)
            self.all_targets = normalize_runtime_targets(refreshed)
            return bool(self.all_targets)

        def _refresh_after_expand(
            self,
            target: RuntimeTarget,
            previous: TargetSnapshot,
            expanding: bool,
        ) -> str:
            branch_path = target.snapshot.path[:-1]
            previous_count = sum(
                path_is_in_branch(existing.snapshot.path, branch_path)
                for existing in self.all_targets
            )
            refresh_method = "branch"
            for delay in (0.04, 0.05, 0.07):
                time.sleep(delay)
                if not self._refresh_branch(target):
                    self._enumerate(self.hwnd)
                    refresh_method = "full"
                    break
                current_count = sum(
                    path_is_in_branch(existing.snapshot.path, branch_path)
                    for existing in self.all_targets
                )
                changed_as_expected = (
                    current_count > previous_count
                    if expanding
                    else current_count < previous_count
                )
                if changed_as_expected:
                    break
            if refresh_method == "branch" and not self._refresh_cached_geometry():
                self._enumerate(self.hwnd)
                refresh_method = "full"
            self._apply_targets(restore=previous)
            if not self.targets or self.selected < 0:
                self._emit_selection()
                return refresh_method
            self._clear_hierarchy()
            self._emit_selection()
            return refresh_method

        def _refresh_invalid_target(self, target: RuntimeTarget) -> None:
            self.invalid_targets.add(self._identity_token(target.snapshot))
            self.events.put(("target_skipped", target.snapshot))
            self._enumerate(self.hwnd)
            self._apply_targets(restore=target.snapshot)
            self._clear_hierarchy()
            self._emit_selection()

        def _activate(self) -> None:
            if not self.targets or self.selected < 0:
                return
            started = time.perf_counter()
            if not self._sync_window_geometry():
                return
            target = self.targets[self.selected]
            if not self._update_live_target(target):
                self._refresh_invalid_target(target)
                return
            expansion = (
                toggle_expanded(target.control)
                if target.snapshot.supports_expand
                else None
            )
            if expansion is not None:
                method, expanding = expansion
                refresh = self._refresh_after_expand(
                    target, target.snapshot, expanding=expanding
                )
                self.events.put(
                    (
                        "expanded",
                        {
                            "target": target.snapshot,
                            "method": method or "Expand",
                            "refresh": refresh,
                            "elapsed": time.perf_counter() - started,
                        },
                    )
                )
                return

            method = (
                try_semantic_invoke(target)
                if target.snapshot.has_action_pattern
                else None
            )
            if method is None:
                if not self._target_is_exposed(
                    target, allow_semantic_bypass=False
                ):
                    self._refresh_invalid_target(target)
                    return
                method = click_target(target)
            self.events.put(
                (
                    "activated",
                    {
                        "target": target.snapshot,
                        "method": method,
                        "elapsed": time.perf_counter() - started,
                    },
                )
            )

        def _back(self) -> None:
            self.events.put(("exit_requested", None))

        def _run(self) -> None:
            auto.InitializeUIAutomationInCurrentThread()
            try:
                while True:
                    command, value, generation = self.commands.get()
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
                            self._scan(int(value))
                        elif command == "prewarm":
                            self._prewarm(int(value))
                        elif (
                            command == "move"
                            and self.context_valid
                            and self.targets
                            and self.selected >= 0
                        ):
                            self._move(Direction(value))
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
                        elif command == "back":
                            self._back()
                        elif command == "sync_window":
                            self._sync_window_geometry()
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
            self,
            target: TargetSnapshot,
            selected: int,
            count: int,
            hierarchy_index: int = -1,
            hierarchy_count: int = 0,
        ) -> None:
            self._target = target
            self._position = f"{selected + 1}/{count}  {target.name or target.control_type}"
            if hierarchy_count > 1 and hierarchy_index >= 0:
                self._position += (
                    f"  层级 {hierarchy_index + 1}/{hierarchy_count}"
                )
            if target.source == "msaa":
                self._position += "  MSAA"
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
        VK_PAGEUP = 0x21
        VK_PAGEDOWN = 0x22
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
                self.VK_PAGEUP: "parent",
                self.VK_PAGEDOWN: "child",
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
    app.setApplicationName("Remote Mic Element Navigation Prototype")
    prototype_process_id = int(kernel32.GetCurrentProcessId())
    overlay = NavigationOverlay()
    worker = AutomationWorker()
    worker.start()
    keyboard_events: queue.Queue[str] = queue.Queue()
    active = threading.Event()
    scanning = False
    shutting_down = False
    prewarm_hwnd = 0
    prewarm_requested_at = 0.0

    def enqueue_keyboard_action(action: str) -> None:
        keyboard_events.put(action)

    hook = KeyboardHook(enqueue_keyboard_action, active)
    hook.start()

    def leave_navigation() -> None:
        active.clear()
        worker.deactivate()
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
        elif action in {"parent", "child"} and active.is_set():
            worker.post(action)
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
                        selected,
                        len(targets),
                        payload["hierarchy_index"],
                        payload["hierarchy_count"],
                    )
            elif event == "selection":
                if active.is_set():
                    overlay.show_target(
                        payload["target"],
                        payload["selected"],
                        payload["count"],
                        payload["hierarchy_index"],
                        payload["hierarchy_count"],
                    )
            elif event == "expanded":
                target = payload["target"]
                print(
                    f"已切换: {target.name or target.control_type} "
                    f"({payload['method']} / {payload['refresh']} refresh / "
                    f"{payload['elapsed']:.3f}s)"
                )
            elif event == "activated":
                target = payload["target"]
                print(
                    f"已执行: {target.name or target.control_type} "
                    f"({payload['method']} / {payload['elapsed']:.3f}s)"
                )
                leave_navigation()
            elif event == "exit_requested":
                leave_navigation()
            elif event == "geometry_synced":
                print(
                    f"窗口位置已同步: {payload['delta_x']:+d}, {payload['delta_y']:+d}"
                )
            elif event == "geometry_rescanned":
                print("窗口尺寸或缩放变化，已重新扫描。")
            elif event == "navigation_invalidated":
                print(f"导航已暂停: {payload}，请重新按 Ctrl+Alt+N。")
                leave_navigation()
            elif event == "prewarm_done":
                print(
                    f"已预识别 {payload['window']}，耗时 {payload['elapsed']:.2f}s。"
                )
            elif event == "target_skipped":
                print(
                    f"已跳过当前无法命中的元素: "
                    f"{payload.name or payload.control_type}"
                )
            elif event == "error":
                scanning = False
                leave_navigation()
                print(f"操作失败: {payload}", file=sys.stderr)

    timer = QTimer()
    timer.timeout.connect(drain_events)
    timer.start(20)

    def monitor_navigation_context() -> None:
        nonlocal prewarm_hwnd, prewarm_requested_at
        foreground = int(user32.GetForegroundWindow())
        if not active.is_set():
            if scanning or foreground <= 0:
                return
            if window_process_id(foreground) == prototype_process_id:
                return
            now = time.perf_counter()
            if (
                foreground != prewarm_hwnd
                or now - prewarm_requested_at
                >= AutomationWorker._CACHE_TTL_SECONDS
            ):
                prewarm_hwnd = foreground
                prewarm_requested_at = now
                worker.post("prewarm", foreground)
            return
        if (
            worker.hwnd
            and foreground != worker.hwnd
            and window_process_id(foreground) != prototype_process_id
        ):
            print("导航已暂停: 已切换到其它窗口，请重新按 Ctrl+Alt+N。")
            leave_navigation()
            return
        worker.post("sync_window")

    geometry_timer = QTimer()
    geometry_timer.timeout.connect(monitor_navigation_context)
    geometry_timer.start(250)

    def cleanup() -> None:
        hook.stop()
        worker.stop()

    app.aboutToQuit.connect(cleanup)
    print("元素导航键盘原型已启动。")
    print(
        "Ctrl+Alt+N 开始/退出，方向键移动，PageUp/PageDown 切换父子元素，"
        "Enter 展开/收起或执行，"
        "Esc 退出，Ctrl+Alt+Q 关闭。"
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
