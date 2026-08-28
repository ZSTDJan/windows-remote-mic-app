"""Experimental keyboard-driven UI Automation spatial navigator.

This developer-only prototype is intentionally outside ``ovb_rc003``. It is
not imported by Remote Mic, included in candidate builds, or connected to the
RC003 input/configuration path. Its job is to answer one question cheaply:
does spatial element navigation feel useful in the user's fixed Windows apps?

Controls:
    Ctrl+Alt+N  scan the foreground window and enter/leave navigation
    Ctrl+Alt+D  enable/disable navigation diagnostics
    Arrow keys  move the highlighted target
    PageUp/Down move to the parent/child element at the same location
    Enter       left-click the highlighted target; press twice to double-click
    Menu        right-click the highlighted target
    Volume +/-  scroll up/down at the highlighted target
    Esc         leave navigation
    Ctrl+Alt+Q  quit the prototype
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import queue
import sys
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field, replace
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
LEGACY_ONLY_WEAK_CONTROL_TYPES = frozenset(
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
SEMANTIC_BYPASS_MAX_WIDTH = 180
SEMANTIC_BYPASS_MAX_HEIGHT = 96
PREWARM_STABILITY_SECONDS = 0.75
DYNAMIC_REFRESH_FALLBACK_SECONDS = 5.0
DYNAMIC_REFRESH_MAX_CACHE_SECONDS = 30.0
DYNAMIC_REFRESH_SETTLE_SECONDS = 0.15
FOLLOW_WINDOW_SCAN_BUDGET_SECONDS = 0.2
FOLLOW_WINDOW_EMPTY_REFRESH_RETRIES = 2
NAVIGATION_STRUCTURE_EVENTS = frozenset(
    {
        0x8000,  # EVENT_OBJECT_CREATE
        0x8001,  # EVENT_OBJECT_DESTROY
        0x8002,  # EVENT_OBJECT_SHOW
        0x8003,  # EVENT_OBJECT_HIDE
        0x8004,  # EVENT_OBJECT_REORDER
        0x800A,  # EVENT_OBJECT_STATECHANGE
    }
)
SECTION_MAX_WINDOW_WIDTH_RATIO = 0.88
SECTION_MIN_WINDOW_WIDTH_RATIO = 0.15
SECTION_BODY_MIN_WINDOW_HEIGHT_RATIO = 0.40
SECTION_BODY_MAX_WINDOW_HEIGHT_RATIO = 1.25
SECTION_HEADER_MIN_WINDOW_WIDTH_RATIO = 0.65
SECTION_HEADER_MAX_WINDOW_HEIGHT_RATIO = 0.15
SECTION_HEADER_MAX_TOP_OFFSET_RATIO = 0.15
SECTION_MIN_HEIGHT = 32
HORIZONTAL_LANE_MIN_OVERLAP_RATIO = 0.50
HORIZONTAL_LANE_MIN_CENTER_TOLERANCE = 12.0
HORIZONTAL_LANE_MAX_CENTER_TOLERANCE = 48.0
HORIZONTAL_LANE_SIZE_MULTIPLIER = 0.75
VERTICAL_LANE_MIN_OVERLAP_RATIO = 0.35
VERTICAL_LANE_MIN_CENTER_TOLERANCE = 16.0
VERTICAL_LANE_MAX_CENTER_TOLERANCE = 96.0
VERTICAL_LANE_SIZE_MULTIPLIER = 0.35
GRID_SAFE_CELL_MAX_CHILDREN = 24
VK_RETURN = 0x0D
VK_ESCAPE = 0x1B
VK_PAGEUP = 0x21
VK_PAGEDOWN = 0x22
VK_LEFT = 0x25
VK_UP = 0x26
VK_RIGHT = 0x27
VK_DOWN = 0x28
VK_D = 0x44
VK_N = 0x4E
VK_Q = 0x51
VK_APPS = 0x5D
VK_VOLUME_DOWN = 0xAE
VK_VOLUME_UP = 0xAF
NAVIGATION_KEY_ACTIONS = {
    VK_UP: "up",
    VK_DOWN: "down",
    VK_LEFT: "left",
    VK_RIGHT: "right",
    VK_PAGEUP: "parent",
    VK_PAGEDOWN: "child",
    VK_RETURN: "activate",
    VK_APPS: "context",
    VK_VOLUME_DOWN: "scroll_down",
    VK_VOLUME_UP: "scroll_up",
    VK_ESCAPE: "cancel",
}
NATIVE_MENU_NAVIGATION_KEYS = frozenset(
    {VK_UP, VK_DOWN, VK_LEFT, VK_RIGHT, VK_RETURN, VK_ESCAPE}
)
GLOBAL_HOTKEY_ACTIONS = {
    VK_D: "toggle_diagnostics",
    VK_N: "toggle",
    VK_Q: "quit",
}
NAVIGATION_SECTION_CONTROL_TYPES = frozenset(
    {
        "DataGridControl",
        "ListControl",
        "MenuControl",
        "TabControl",
        "TableControl",
        "ToolBarControl",
        "TreeControl",
    }
)
REPEATED_CONTENT_PARENT_TYPES = frozenset(
    {
        "ApplicationControl",
        "CustomControl",
        "DataGridControl",
        "GroupControl",
        "ListControl",
        "PaneControl",
        "TableControl",
    }
)
REPEATED_CONTENT_ITEM_TYPES = frozenset(
    {"DataItemControl", "GroupControl", "ListItemControl"}
)
VISUAL_SURFACE_CONTROL_TYPES = frozenset({"CustomControl", "PaneControl"})
VISUAL_SURFACE_MIN_WIDTH = 220
VISUAL_SURFACE_MIN_HEIGHT = 120
VISUAL_SURFACE_MIN_WINDOW_AREA_RATIO = 0.06
OVERLAY_MAX_ROOT_AREA_RATIO = 0.35
OVERLAY_MIN_INTERSECTION_RATIO = 0.65
OVERLAY_ROOT_TARGET_MAX_SIZE = 200
QUICKER_FLOAT_WINDOW_TITLES = frozenset(
    {"FloatButtonWindow", "FloatPanelWindow", "TextFloatPanelWindow"}
)
QUICKER_STATE_FILE_ENV = "REMOTE_MIC_QUICKER_STATE_FILE"
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
NON_NAVIGATION_OVERLAY_CLASSES = frozenset(
    {
        "Progman",
        "Shell_SecondaryTrayWnd",
        "Shell_TrayWnd",
        "WorkerW",
    }
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
    section_path: tuple[int, ...] = ()
    section_rect: Optional[Rect] = None


@dataclass(frozen=True)
class ElementSnapshot:
    rect: Rect
    name: str
    control_type: str
    automation_id: str
    path: tuple[int, ...]
    enabled: bool = True
    offscreen: bool = False
    keyboard_focusable: bool = False
    has_legacy_pattern: bool = False
    has_scroll_pattern: bool = False


@dataclass(frozen=True)
class SyntheticTargetSpec:
    snapshot: TargetSnapshot
    click_point: tuple[int, int]


@dataclass(frozen=True)
class QuickerOverlayAssociation:
    hwnd: int
    bind_process_name: str


def normalized_process_name(value: str) -> str:
    name = os.path.basename(str(value or "").strip()).casefold()
    return name[:-4] if name.endswith(".exe") else name


def default_quicker_state_file() -> str:
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if not local_app_data:
        return ""
    return os.path.join(
        local_app_data,
        "RemoteMic",
        "RC003",
        "quicker-navigation.json",
    )


def load_quicker_overlay_associations(
    path: str,
) -> dict[int, QuickerOverlayAssociation]:
    """Read an optional snapshot produced by a Quicker-side bridge action."""

    if not path:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as stream:
            payload = json.load(stream)
    except (OSError, ValueError, TypeError):
        return {}
    items = payload.get("items", ()) if isinstance(payload, dict) else ()
    associations: dict[int, QuickerOverlayAssociation] = {}
    for item in items if isinstance(items, list) else ():
        if not isinstance(item, dict):
            continue
        try:
            hwnd = int(item.get("hwnd", 0))
        except (TypeError, ValueError):
            continue
        bind_process_name = normalized_process_name(
            str(item.get("bindProcessName", item.get("bindProcess", "")))
        )
        if (
            hwnd > 0
            and bind_process_name
            and bool(item.get("isBound", item.get("isBindProcess", False)))
            and bool(item.get("visible", True))
        ):
            associations[hwnd] = QuickerOverlayAssociation(
                hwnd,
                bind_process_name,
            )
    return associations


def quicker_overlay_matches_process(
    association: Optional[QuickerOverlayAssociation],
    process_name: str,
) -> bool:
    return bool(
        association is not None
        and association.bind_process_name == normalized_process_name(process_name)
    )


@dataclass(frozen=True)
class OpaqueVisualSurface:
    rect: Rect
    path: tuple[int, ...]
    name: str = ""


@dataclass(frozen=True)
class CandidateDiagnostic:
    index: int
    target: TargetSnapshot
    rank: int
    route: str
    common_path_prefix: int
    beam_rank: Optional[int]
    primary_gap: Optional[float]
    perpendicular_gap: Optional[float]
    score: Optional[float]
    center_offset: Optional[float]


@dataclass(frozen=True)
class NavigationDiagnostic:
    current_index: int
    current: TargetSnapshot
    direction: Direction
    candidates: tuple[CandidateDiagnostic, ...]
    available_indices: tuple[int, ...]
    invalid_cached_indices: tuple[int, ...]
    unhittable_indices: tuple[int, ...]
    selected_index: Optional[int]
    outcome: str
    rejected_counts: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class DirtyWindowState:
    generation: int
    changed_at: float


class DirtyWindowTracker:
    """Track changes for one watched window without scanning in callbacks."""

    def __init__(self, clock: Callable[[], float] = time.perf_counter) -> None:
        self._lock = threading.Lock()
        self._clock = clock
        self._window_id = 0
        self._process_id = 0
        self._generation = 0
        self._consumed_generation = 0
        self._changed_at = 0.0

    def watch(self, window_id: int, process_id: int) -> bool:
        with self._lock:
            if window_id == self._window_id and process_id == self._process_id:
                return False
            self._window_id = window_id
            self._process_id = process_id
            self._generation = 0
            self._consumed_generation = 0
            self._changed_at = 0.0
            return True

    def mark(self, window_id: int, process_id: int) -> bool:
        if window_id <= 0 or process_id <= 0:
            return False
        with self._lock:
            if window_id != self._window_id or process_id != self._process_id:
                return False
            self._generation += 1
            self._changed_at = self._clock()
            return True

    def state(self, window_id: int, process_id: int) -> Optional[DirtyWindowState]:
        with self._lock:
            if (
                window_id != self._window_id
                or process_id != self._process_id
                or self._generation <= self._consumed_generation
            ):
                return None
            return DirtyWindowState(self._generation, self._changed_at)

    def consume(
        self,
        window_id: int,
        process_id: int,
        through_generation: Optional[int] = None,
    ) -> bool:
        with self._lock:
            if window_id != self._window_id or process_id != self._process_id:
                return False
            generation = (
                self._generation
                if through_generation is None
                else min(self._generation, through_generation)
            )
            if generation <= self._consumed_generation:
                return False
            self._consumed_generation = generation
            return True


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


def is_navigation_structure_event(event_id: int) -> bool:
    return event_id in NAVIGATION_STRUCTURE_EVENTS


def target_has_interaction_evidence(target: TargetSnapshot) -> bool:
    """Return whether the element itself represents an operable action."""

    return bool(
        target.has_action_pattern
        or target.supports_expand
        or target.control_type in PRIMARY_ACTION_CONTROL_TYPES
    )


def standard_control_has_actionable_semantics(
    control_type: str,
    keyboard_focusable: bool,
    has_action_pattern: bool,
    has_direct_action_pattern: bool,
) -> bool:
    """Treat Legacy-only list/data items as content rather than actions."""

    if control_type in LEGACY_ONLY_WEAK_CONTROL_TYPES:
        return keyboard_focusable or has_direct_action_pattern
    return keyboard_focusable or has_action_pattern


def target_is_finer_descendant(
    target: TargetSnapshot, candidate: TargetSnapshot
) -> bool:
    if target.rect == candidate.rect:
        return False
    target_area = max(1, target.rect.width * target.rect.height)
    candidate_area = max(1, candidate.rect.width * candidate.rect.height)
    if target_area <= candidate_area * 1.25:
        return False
    if not target.rect.contains(candidate.rect):
        return False
    if target.path and candidate.path:
        return bool(
            len(candidate.path) > len(target.path)
            and candidate.path[: len(target.path)] == target.path
        )
    return True


def target_is_action_descendant(
    target: TargetSnapshot, candidate: TargetSnapshot
) -> bool:
    """Match a retained UIA action child without geometry-size heuristics."""

    if not target_has_interaction_evidence(candidate):
        return False
    if target.path and candidate.path:
        return bool(
            len(candidate.path) > len(target.path)
            and candidate.path[: len(target.path)] == target.path
        )
    return target_is_finer_descendant(target, candidate)


def _path_is_descendant(path: tuple[int, ...], parent: tuple[int, ...]) -> bool:
    return bool(
        len(path) > len(parent)
        and path[: len(parent)] == parent
    )


def _median_number(values: Sequence[float]) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[middle])
    return (float(ordered[middle - 1]) + float(ordered[middle])) / 2


def _rect_intersection_area(first: Rect, second: Rect) -> int:
    width = max(0, min(first.right, second.right) - max(first.left, second.left))
    height = max(0, min(first.bottom, second.bottom) - max(first.top, second.top))
    return width * height


def repeated_content_target_specs(
    elements: Sequence[ElementSnapshot],
    window_rect: Rect,
) -> list[SyntheticTargetSpec]:
    """Promote repeated Chromium/Electron content rows to coordinate targets."""

    by_path = {element.path: element for element in elements}
    children: dict[tuple[int, ...], list[tuple[int, ...]]] = defaultdict(list)
    for element in elements:
        if element.path:
            children[element.path[:-1]].append(element.path)

    content_by_path: dict[tuple[int, ...], list[ElementSnapshot]] = {}
    for path in sorted(by_path, key=len, reverse=True):
        element = by_path[path]
        content: list[ElementSnapshot] = []
        if (
            element.name
            and element.control_type in {"ImageControl", "TextControl"}
            and element.rect.width >= 4
            and element.rect.height >= 4
        ):
            content.append(element)
        for child_path in children.get(path, ()):
            content.extend(content_by_path.get(child_path, ()))
            if len(content) >= 16:
                break
        content_by_path[path] = content[:16]

    specs: list[SyntheticTargetSpec] = []
    for parent_path, child_paths in children.items():
        parent = by_path.get(parent_path)
        if (
            parent is None
            or parent.control_type not in REPEATED_CONTENT_PARENT_TYPES
            or parent.rect.width < 80
            or parent.rect.height < 80
        ):
            continue

        repeated: list[tuple[ElementSnapshot, list[ElementSnapshot]]] = []
        for child_path in child_paths:
            child = by_path.get(child_path)
            if (
                child is None
                or child.control_type not in REPEATED_CONTENT_ITEM_TYPES
                or not child.enabled
                or child.rect.width < 24
                or child.rect.height < 24
            ):
                continue
            content = content_by_path.get(child_path, [])
            if not content:
                continue
            child_area = max(1, child.rect.width * child.rect.height)
            parent_area = max(1, parent.rect.width * parent.rect.height)
            if child_area > parent_area * 0.72:
                continue
            repeated.append((child, content))

        if len(repeated) < 3:
            continue
        median_width = _median_number([item.rect.width for item, _content in repeated])
        if median_width <= 0:
            continue

        for item, content in repeated:
            if (
                item.offscreen
                or not item.rect.intersects(window_rect)
                or item.rect.width < median_width * 0.55
                or item.rect.width > median_width * 1.8
                or item.rect.height > max(480, parent.rect.height * 0.55)
            ):
                continue
            visible_content = [
                leaf
                for leaf in content
                if not leaf.offscreen
                and leaf.rect.intersects(window_rect)
                and item.rect.intersects(leaf.rect)
            ]
            if not visible_content:
                continue
            ordered_content = sorted(
                visible_content,
                key=lambda leaf: (leaf.rect.top, leaf.rect.left, leaf.name),
            )
            names = list(dict.fromkeys(leaf.name for leaf in ordered_content if leaf.name))
            label = " / ".join(names[:3])[:120]
            click_leaf = max(
                ordered_content,
                key=lambda leaf: (
                    int(leaf.control_type == "TextControl"),
                    leaf.rect.width * leaf.rect.height,
                ),
            )
            click_point = (
                round(click_leaf.rect.center_x),
                round(click_leaf.rect.center_y),
            )
            specs.append(
                SyntheticTargetSpec(
                    TargetSnapshot(
                        rect=item.rect,
                        name=label or item.name or "内容项",
                        control_type="ContentItemControl",
                        automation_id=item.automation_id,
                        path=item.path,
                        depth=len(item.path),
                        has_action_pattern=True,
                        source="uia-content",
                        section_path=parent.path,
                        section_rect=parent.rect,
                    ),
                    click_point,
                )
            )
    return specs


def opaque_visual_surfaces(
    elements: Sequence[ElementSnapshot],
    targets: Sequence[TargetSnapshot],
    window_rect: Rect,
) -> list[OpaqueVisualSurface]:
    """Find legacy focusable panes whose repeated items are not exposed by UIA."""

    window_area = max(1, window_rect.width * window_rect.height)
    candidates: list[OpaqueVisualSurface] = []
    for element in elements:
        rect = element.rect
        if (
            element.control_type not in VISUAL_SURFACE_CONTROL_TYPES
            or not element.enabled
            or element.offscreen
            or not element.keyboard_focusable
            or not element.has_legacy_pattern
            or rect.width < VISUAL_SURFACE_MIN_WIDTH
            or rect.height < VISUAL_SURFACE_MIN_HEIGHT
            or rect.width * rect.height
            < window_area * VISUAL_SURFACE_MIN_WINDOW_AREA_RATIO
            or not rect.intersects(window_rect)
        ):
            continue

        descendants = [
            candidate
            for candidate in elements
            if _path_is_descendant(candidate.path, element.path)
            and candidate.rect.intersects(rect)
        ]
        header_bottoms = [
            candidate.rect.bottom
            for candidate in descendants
            if candidate.control_type == "HeaderControl"
            and candidate.rect.width >= rect.width * 0.5
        ]
        if not element.has_scroll_pattern and not header_bottoms:
            continue
        scrollbar_tops = [
            candidate.rect.top
            for candidate in descendants
            if candidate.control_type == "ScrollBarControl"
            and candidate.rect.width >= rect.width * 0.5
            and candidate.rect.top > rect.top + rect.height * 0.5
        ]
        content_rect = Rect(
            rect.left,
            max([rect.top, *header_bottoms]),
            rect.right,
            min([rect.bottom, *scrollbar_tops]),
        )
        if content_rect.width < VISUAL_SURFACE_MIN_WIDTH or content_rect.height < 80:
            continue

        target_descendants = [
            target
            for target in targets
            if _path_is_descendant(target.path, element.path)
            and target.rect.intersects(content_rect)
        ]
        if len(target_descendants) > 24:
            continue
        covered_area = sum(
            _rect_intersection_area(target.rect, content_rect)
            for target in target_descendants
        )
        if covered_area > content_rect.width * content_rect.height * 0.35:
            continue
        candidates.append(
            OpaqueVisualSurface(content_rect, element.path, element.name)
        )

    selected: list[OpaqueVisualSurface] = []
    for candidate in sorted(
        candidates, key=lambda item: item.rect.width * item.rect.height
    ):
        if any(candidate.rect.contains(existing.rect) for existing in selected):
            continue
        selected.append(candidate)
    return selected


def _filled_activity_runs(
    activity: Sequence[bool],
    max_gap: int = 1,
    min_length: int = 2,
) -> list[tuple[int, int]]:
    filled = list(activity)
    index = 0
    while index < len(filled):
        if filled[index]:
            index += 1
            continue
        gap_end = index
        while gap_end < len(filled) and not filled[gap_end]:
            gap_end += 1
        if index > 0 and gap_end < len(filled) and gap_end - index <= max_gap:
            filled[index:gap_end] = [True] * (gap_end - index)
        index = gap_end

    runs: list[tuple[int, int]] = []
    index = 0
    while index < len(filled):
        if not filled[index]:
            index += 1
            continue
        run_end = index + 1
        while run_end < len(filled) and filled[run_end]:
            run_end += 1
        if run_end - index >= min_length:
            runs.append((index, run_end))
        index = run_end
    return runs


def _substantial_regular_run_clusters(
    runs: Sequence[tuple[int, int]],
    median_gap: float,
) -> list[list[tuple[int, int]]]:
    if len(runs) < 3 or median_gap <= 0:
        return [list(runs)]
    maximum_gap = max(18.0, median_gap * 1.9)
    clusters: list[list[tuple[int, int]]] = [[runs[0]]]
    previous_center = (runs[0][0] + runs[0][1]) / 2
    for run in runs[1:]:
        center = (run[0] + run[1]) / 2
        if center - previous_center > maximum_gap:
            clusters.append([])
        clusters[-1].append(run)
        previous_center = center
    largest_length = max(len(cluster) for cluster in clusters)
    minimum_length = min(
        largest_length,
        max(3, (largest_length + 2) // 3),
    )
    return [cluster for cluster in clusters if len(cluster) >= minimum_length]


def _screen_to_image_rect(
    rect: Rect,
    window_rect: Rect,
    image_width: int,
    image_height: int,
) -> Rect:
    if window_rect.width <= 0 or window_rect.height <= 0:
        return Rect(0, 0, 0, 0)
    return Rect(
        max(
            0,
            min(
                image_width,
                round((rect.left - window_rect.left) * image_width / window_rect.width),
            ),
        ),
        max(
            0,
            min(
                image_height,
                round((rect.top - window_rect.top) * image_height / window_rect.height),
            ),
        ),
        max(
            0,
            min(
                image_width,
                round((rect.right - window_rect.left) * image_width / window_rect.width),
            ),
        ),
        max(
            0,
            min(
                image_height,
                round((rect.bottom - window_rect.top) * image_height / window_rect.height),
            ),
        ),
    )


def _image_to_screen_rect(
    rect: Rect,
    window_rect: Rect,
    image_width: int,
    image_height: int,
) -> Rect:
    return Rect(
        window_rect.left + round(rect.left * window_rect.width / image_width),
        window_rect.top + round(rect.top * window_rect.height / image_height),
        window_rect.left + round(rect.right * window_rect.width / image_width),
        window_rect.top + round(rect.bottom * window_rect.height / image_height),
    )


def visual_grid_target_specs(
    rgb: bytes,
    image_width: int,
    image_height: int,
    bytes_per_line: int,
    window_rect: Rect,
    surfaces: Sequence[OpaqueVisualSurface],
) -> list[SyntheticTargetSpec]:
    """Detect regular text rows or thumbnail cells inside opaque legacy panes."""

    if image_width <= 0 or image_height <= 0 or bytes_per_line < image_width * 3:
        return []

    def is_ink(x: int, y: int) -> bool:
        offset = y * bytes_per_line + x * 3
        red, green, blue = rgb[offset : offset + 3]
        high = max(red, green, blue)
        low = min(red, green, blue)
        return bool(high < 178 or (high - low > 72 and low < 92))

    specs: list[SyntheticTargetSpec] = []
    for surface_index, surface in enumerate(surfaces):
        pixel_rect = _screen_to_image_rect(
            surface.rect, window_rect, image_width, image_height
        )
        if pixel_rect.width < 48 or pixel_rect.height < 32:
            continue
        x_start = min(pixel_rect.right - 1, pixel_rect.left + 2)
        x_end = max(x_start + 1, pixel_rect.right - 2)
        row_counts: list[int] = []
        for y in range(pixel_rect.top, pixel_rect.bottom):
            row_counts.append(
                sum(1 for x in range(x_start, x_end) if is_ink(x, y))
            )
        row_threshold = max(3, pixel_rect.width // 110)
        raw_runs = _filled_activity_runs(
            [count >= row_threshold for count in row_counts],
            max_gap=1,
            min_length=2,
        )
        if len(raw_runs) < 2:
            continue
        runs = [
            (pixel_rect.top + start, pixel_rect.top + end)
            for start, end in raw_runs
        ]
        merged_runs: list[tuple[int, int]] = []
        for run in runs:
            if (
                merged_runs
                and run[0] - merged_runs[-1][1] <= 8
                and (
                    merged_runs[-1][1] - merged_runs[-1][0] >= 14
                    or run[1] - run[0] >= 14
                )
            ):
                merged_runs[-1] = (merged_runs[-1][0], run[1])
            else:
                merged_runs.append(run)
        runs = merged_runs
        centers = [(top + bottom) / 2 for top, bottom in runs]
        gaps = [centers[index + 1] - centers[index] for index in range(len(centers) - 1)]
        median_gap = _median_number(gaps)
        detail_mode = bool(
            len(runs) >= 3
            and median_gap <= max(18.0, pixel_rect.height * 0.08)
            and sum(
                median_gap * 0.45 <= gap <= median_gap * 1.9 for gap in gaps
            )
            >= max(1, round(len(gaps) * 0.6))
        )

        surface_specs: list[SyntheticTargetSpec] = []
        if detail_mode:
            row_index = 0
            for cluster in _substantial_regular_run_clusters(runs, median_gap):
                cluster_centers = [
                    (top + bottom) / 2 for top, bottom in cluster
                ]
                cluster_gaps = [
                    cluster_centers[index + 1] - cluster_centers[index]
                    for index in range(len(cluster_centers) - 1)
                ]
                cluster_gap = _median_number(cluster_gaps) or median_gap
                for cluster_index, center in enumerate(cluster_centers):
                    previous_center = (
                        cluster_centers[cluster_index - 1]
                        if cluster_index
                        else center - cluster_gap
                    )
                    next_center = (
                        cluster_centers[cluster_index + 1]
                        if cluster_index + 1 < len(cluster_centers)
                        else center + cluster_gap
                    )
                    top = max(
                        pixel_rect.top,
                        round((previous_center + center) / 2),
                    )
                    bottom = min(
                        pixel_rect.bottom,
                        round((center + next_center) / 2),
                    )
                    if bottom - top < 3:
                        continue
                    image_cell = Rect(
                        pixel_rect.left, top, pixel_rect.right, bottom
                    )
                    screen_cell = _image_to_screen_rect(
                        image_cell, window_rect, image_width, image_height
                    )
                    click_x = pixel_rect.left + max(
                        8, min(pixel_rect.width // 7, 48)
                    )
                    click_point_rect = _image_to_screen_rect(
                        Rect(
                            click_x,
                            round(center),
                            click_x + 1,
                            round(center) + 1,
                        ),
                        window_rect,
                        image_width,
                        image_height,
                    )
                    surface_specs.append(
                        SyntheticTargetSpec(
                            TargetSnapshot(
                                rect=screen_cell,
                                name=f"视觉行 {row_index + 1}",
                                control_type="VisualItemControl",
                                path=surface.path + (1_000_000 + row_index,),
                                depth=len(surface.path) + 1,
                                has_action_pattern=True,
                                source="visual-grid",
                                section_path=surface.path,
                                section_rect=surface.rect,
                            ),
                            (click_point_rect.left, click_point_rect.top),
                        )
                    )
                    row_index += 1
        else:
            cell_index = 0
            for run_top, run_bottom in runs:
                run_height = run_bottom - run_top
                column_counts = [
                    sum(1 for y in range(run_top, run_bottom) if is_ink(x, y))
                    for x in range(pixel_rect.left, pixel_rect.right)
                ]
                column_threshold = max(2, run_height // 10)
                column_runs = _filled_activity_runs(
                    [count >= column_threshold for count in column_counts],
                    max_gap=3,
                    min_length=max(5, pixel_rect.width // 70),
                )
                cells = [
                    (
                        pixel_rect.left + left,
                        pixel_rect.left + right,
                    )
                    for left, right in column_runs
                    if right - left >= max(6, pixel_rect.width // 50)
                ]
                if len(cells) < 2:
                    cells = [(pixel_rect.left, pixel_rect.right)]
                for left, right in cells:
                    image_cell = Rect(left, run_top, right, run_bottom)
                    screen_cell = _image_to_screen_rect(
                        image_cell, window_rect, image_width, image_height
                    )
                    surface_specs.append(
                        SyntheticTargetSpec(
                            TargetSnapshot(
                                rect=screen_cell,
                                name=f"视觉项 {cell_index + 1}",
                                control_type="VisualItemControl",
                                path=surface.path + (1_000_000 + cell_index,),
                                depth=len(surface.path) + 1,
                                has_action_pattern=True,
                                source="visual-grid",
                                section_path=surface.path,
                                section_rect=surface.rect,
                            ),
                            (
                                round(screen_cell.center_x),
                                round(screen_cell.center_y),
                            ),
                        )
                    )
                    cell_index += 1
        if len(surface_specs) >= 2:
            specs.extend(surface_specs)
    return specs


def finer_descendant_index_map(
    targets: Sequence[TargetSnapshot],
) -> tuple[tuple[int, ...], ...]:
    return tuple(
        tuple(
            candidate_index
            for candidate_index, candidate in enumerate(targets)
            if candidate_index != target_index
            and target_is_finer_descendant(target, candidate)
        )
        for target_index, target in enumerate(targets)
    )


def target_has_finer_descendant(
    targets: Sequence[TargetSnapshot], target_index: int
) -> bool:
    """Return whether a broad target contains a more specific action target."""

    if not 0 <= target_index < len(targets):
        return False
    return any(
        index != target_index
        and target_is_finer_descendant(targets[target_index], candidate)
        for index, candidate in enumerate(targets)
    )


def _clip_rect(rect: Rect, bounds: Rect) -> Optional[Rect]:
    clipped = Rect(
        max(rect.left, bounds.left),
        max(rect.top, bounds.top),
        min(rect.right, bounds.right),
        min(rect.bottom, bounds.bottom),
    )
    return clipped if clipped.width > 0 and clipped.height > 0 else None


def _merged_intervals(
    intervals: Sequence[tuple[int, int]], start: int, end: int
) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for interval_start, interval_end in sorted(intervals):
        interval_start = max(start, interval_start)
        interval_end = min(end, interval_end)
        if interval_end <= interval_start:
            continue
        if merged and interval_start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], interval_end))
        else:
            merged.append((interval_start, interval_end))
    return merged


def _interval_gaps(
    occupied: Sequence[tuple[int, int]], start: int, end: int
) -> list[tuple[int, int]]:
    gaps: list[tuple[int, int]] = []
    cursor = start
    for interval_start, interval_end in occupied:
        if interval_start > cursor:
            gaps.append((cursor, interval_start))
        cursor = max(cursor, interval_end)
    if cursor < end:
        gaps.append((cursor, end))
    return gaps


def target_grid_rect(
    targets: Sequence[TargetSnapshot],
    target_index: int,
    descendants_by_target: Sequence[Sequence[int]],
) -> Rect:
    """Return the target's own clickable cell, excluding retained child actions."""

    target = targets[target_index]
    child_rects = [
        clipped
        for child_index in descendants_by_target[target_index]
        if target_is_action_descendant(target, targets[child_index])
        and (clipped := _clip_rect(targets[child_index].rect, target.rect))
        is not None
    ]
    if not child_rects:
        return target.rect

    candidates: list[Rect] = []
    occupied_x = _merged_intervals(
        [(rect.left, rect.right) for rect in child_rects],
        target.rect.left,
        target.rect.right,
    )
    occupied_y = _merged_intervals(
        [(rect.top, rect.bottom) for rect in child_rects],
        target.rect.top,
        target.rect.bottom,
    )
    for left, right in _interval_gaps(
        occupied_x, target.rect.left, target.rect.right
    ):
        candidates.append(Rect(left, target.rect.top, right, target.rect.bottom))
    for top, bottom in _interval_gaps(
        occupied_y, target.rect.top, target.rect.bottom
    ):
        candidates.append(Rect(target.rect.left, top, target.rect.right, bottom))

    # Sparse embedded controls can leave a useful cell between their X/Y bands.
    # Adjacent partition cells find that space without an expensive rectangle search.
    if len(child_rects) <= GRID_SAFE_CELL_MAX_CHILDREN:
        x_edges = sorted(
            {
                target.rect.left,
                target.rect.right,
                *(edge for rect in child_rects for edge in (rect.left, rect.right)),
            }
        )
        y_edges = sorted(
            {
                target.rect.top,
                target.rect.bottom,
                *(edge for rect in child_rects for edge in (rect.top, rect.bottom)),
            }
        )
        for left, right in zip(x_edges, x_edges[1:]):
            for top, bottom in zip(y_edges, y_edges[1:]):
                cell = Rect(left, top, right, bottom)
                if (
                    cell.width > 0
                    and cell.height > 0
                    and not any(cell.intersects(child) for child in child_rects)
                ):
                    candidates.append(cell)

    usable = [
        rect
        for rect in candidates
        if rect.width > 0
        and rect.height > 0
        and not any(rect.intersects(child) for child in child_rects)
    ]
    if not usable:
        return target.rect
    return max(
        usable,
        key=lambda rect: (
            rect.width * rect.height,
            -abs(rect.center_y - target.rect.center_y),
            -abs(rect.center_x - target.rect.center_x),
            -rect.top,
            -rect.left,
        ),
    )


def navigation_grid_rects(
    targets: Sequence[TargetSnapshot],
    descendants_by_target: Optional[Sequence[Sequence[int]]] = None,
) -> tuple[Rect, ...]:
    if descendants_by_target is None:
        descendants_by_target = finer_descendant_index_map(targets)
    return tuple(
        target_grid_rect(targets, index, descendants_by_target)
        for index in range(len(targets))
    )


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
    if direction in {Direction.LEFT, Direction.RIGHT}:
        smaller_height = max(1, min(current.height, candidate.height))
        overlap_ratio = overlap / smaller_height
        center_tolerance = max(
            HORIZONTAL_LANE_MIN_CENTER_TOLERANCE,
            min(
                HORIZONTAL_LANE_MAX_CENTER_TOLERANCE,
                smaller_height * HORIZONTAL_LANE_SIZE_MULTIPLIER,
            ),
        )
        # A one-pixel edge touch is not a row. Keep an intermediate target in
        # the horizontal route only when its Y projection or centers are close.
        beam_rank = 0 if (
            overlap_ratio >= HORIZONTAL_LANE_MIN_OVERLAP_RATIO
            or center_offset <= center_tolerance
        ) else 1
    else:
        smaller_width = max(1, min(current.width, candidate.width))
        overlap_ratio = overlap / smaller_width
        center_tolerance = max(
            VERTICAL_LANE_MIN_CENTER_TOLERANCE,
            min(
                VERTICAL_LANE_MAX_CENTER_TOLERANCE,
                smaller_width * VERTICAL_LANE_SIZE_MULTIPLIER,
            ),
        )
        beam_rank = 0 if (
            overlap_ratio >= VERTICAL_LANE_MIN_OVERLAP_RATIO
            or center_offset <= center_tolerance
        ) else 1
    return (
        beam_rank,
        float(primary_gap),
        float(perpendicular_gap),
        score,
        center_offset,
        candidate.top,
        candidate.left,
    )


def move_should_refresh_dynamic_targets(
    current: Rect,
    candidate: Optional[Rect],
    direction: Direction,
    window_rect: Rect,
) -> bool:
    """Refresh before accepting a wrap or a distant diagonal from old data."""

    if candidate is None:
        return True
    score = direction_score(current, candidate, direction)
    if score is None:
        return True
    if score[0] == 0:
        primary_span = (
            window_rect.width
            if direction in {Direction.LEFT, Direction.RIGHT}
            else window_rect.height
        )
        current_size = (
            current.width
            if direction in {Direction.LEFT, Direction.RIGHT}
            else current.height
        )
        local_gap = max(
            96.0,
            min(240.0, primary_span * 0.08),
            current_size * 2.5,
        )
        return score[1] > local_gap
    perpendicular_span = (
        window_rect.height
        if direction in {Direction.LEFT, Direction.RIGHT}
        else window_rect.width
    )
    return score[2] > max(96.0, perpendicular_span * 0.12)


def background_refresh_due(
    dirty_state: Optional[DirtyWindowState],
    now: float,
    cache_age: float,
    requested: bool = False,
    input_idle_for: Optional[float] = None,
) -> bool:
    """Refresh only after the latest structure event has gone quiet."""

    if (
        input_idle_for is not None
        and input_idle_for < DYNAMIC_REFRESH_SETTLE_SECONDS
    ):
        return False
    if (
        dirty_state is not None
        and now - dirty_state.changed_at < DYNAMIC_REFRESH_SETTLE_SECONDS
    ):
        return False
    if requested or dirty_state is not None:
        return True
    return cache_age >= DYNAMIC_REFRESH_MAX_CACHE_SECONDS


def dynamic_refresh_fallback_due(
    cache_age: float,
    suspicious_move: bool,
) -> bool:
    """Eventually refresh even when an accessibility provider misses events."""

    return bool(
        cache_age >= DYNAMIC_REFRESH_MAX_CACHE_SECONDS
        or (
            suspicious_move
            and cache_age >= DYNAMIC_REFRESH_FALLBACK_SECONDS
        )
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


def infer_navigation_section_path(
    path: tuple[int, ...],
    ancestor_rects: dict[tuple[int, ...], Rect],
    window_rect: Rect,
    ancestor_types: Optional[dict[tuple[int, ...], str]] = None,
) -> tuple[int, ...]:
    """Return the deepest usable UIA navigation region around a target."""

    if window_rect.width <= 0 or window_rect.height <= 0:
        return ()
    minimum_width = window_rect.width * SECTION_MIN_WINDOW_WIDTH_RATIO
    maximum_width = window_rect.width * SECTION_MAX_WINDOW_WIDTH_RATIO
    body_minimum_height = (
        window_rect.height * SECTION_BODY_MIN_WINDOW_HEIGHT_RATIO
    )
    body_maximum_height = (
        window_rect.height * SECTION_BODY_MAX_WINDOW_HEIGHT_RATIO
    )
    header_minimum_width = (
        window_rect.width * SECTION_HEADER_MIN_WINDOW_WIDTH_RATIO
    )
    header_maximum_height = (
        window_rect.height * SECTION_HEADER_MAX_WINDOW_HEIGHT_RATIO
    )
    header_maximum_top = (
        window_rect.top
        + window_rect.height * SECTION_HEADER_MAX_TOP_OFFSET_RATIO
    )
    section_path: tuple[int, ...] = ()
    for depth in range(1, len(path)):
        prefix = path[:depth]
        rect = ancestor_rects.get(prefix)
        if rect is None or not rect.intersects(window_rect):
            continue
        if (
            rect.width < minimum_width
            or rect.width > maximum_width
            or rect.height < SECTION_MIN_HEIGHT
        ):
            continue
        control_type = (
            "" if ancestor_types is None else ancestor_types.get(prefix, "")
        )
        semantic_container = control_type in NAVIGATION_SECTION_CONTROL_TYPES
        body_region = body_minimum_height <= rect.height <= body_maximum_height
        header_region = (
            rect.width >= header_minimum_width
            and rect.height <= header_maximum_height
            and rect.top <= header_maximum_top
        )
        if semantic_container or body_region or header_region:
            section_path = prefix
    return section_path


def _direction_rank_key(
    current: Rect,
    candidate: Rect,
    direction: Direction,
    score: tuple[int, float, float, float, float, int, int],
    common_prefix: int,
) -> tuple[float, ...]:
    if direction == Direction.RIGHT:
        forward_center_distance = candidate.center_x - current.center_x
    elif direction == Direction.LEFT:
        forward_center_distance = current.center_x - candidate.center_x
    elif direction == Direction.DOWN:
        forward_center_distance = candidate.center_y - current.center_y
    else:
        forward_center_distance = current.center_y - candidate.center_y
    if score[0] == 0:
        axis_distance = score[1]
        secondary_distance = score[4]
        forward_distance = score[2]
    else:
        axis_distance = score[4] / max(1.0, forward_center_distance)
        secondary_distance = score[2]
        forward_distance = forward_center_distance
    return (
        float(score[0]),
        axis_distance,
        secondary_distance,
        forward_distance,
        forward_center_distance,
        score[3],
        float(-common_prefix),
        float(score[5]),
        float(score[6]),
    )


def ranked_target_indices(
    targets: Sequence[TargetSnapshot],
    current_index: int,
    direction: Direction,
    descendants_by_target: Optional[Sequence[Sequence[int]]] = None,
    grid_rects: Optional[Sequence[Rect]] = None,
) -> list[int]:
    if not targets or not 0 <= current_index < len(targets):
        return []
    if descendants_by_target is None:
        descendants_by_target = finer_descendant_index_map(targets)
    if grid_rects is None:
        grid_rects = navigation_grid_rects(targets, descendants_by_target)
    current = grid_rects[current_index]
    scored: list[
        tuple[tuple[int, float, float, float, float, int, int], int, int]
    ] = []
    for index, target in enumerate(targets):
        if index == current_index:
            continue
        score = direction_score(current, grid_rects[index], direction)
        if score is not None:
            common_prefix = _common_path_prefix_length(
                targets[current_index].path, target.path
            )
            scored.append((score, common_prefix, index))

    def rank_key(
        item: tuple[
            tuple[int, float, float, float, float, int, int], int, int
        ],
    ) -> tuple[float, ...]:
        score, common_prefix, index = item
        return _direction_rank_key(
            current,
            grid_rects[index],
            direction,
            score,
            common_prefix,
        )

    scored.sort(key=rank_key)
    return [index for _score, _prefix, index in scored]


def best_grid_target_index(
    targets: Sequence[TargetSnapshot],
    current_index: int,
    direction: Direction,
    grid_rects: Sequence[Rect],
) -> Optional[int]:
    """Return the first neighbor from the same geometry-only ranking."""

    ranked = ranked_target_indices(
        targets,
        current_index,
        direction,
        grid_rects=grid_rects,
    )
    return ranked[0] if ranked else None


DIAGNOSTIC_REJECTION_ORDER = (
    "wrong_direction",
    "same_rectangle",
)
DIAGNOSTIC_REJECTION_LABELS = {
    "wrong_direction": "不在请求方向",
    "same_rectangle": "与当前元素同一矩形",
}
DIAGNOSTIC_ROUTE_LABELS = {
    "lane": "同一通道",
    "diagonal": "斜向候选",
    "reverse": "反向返回",
    "parent_cell": "父级单元格",
}
DIAGNOSTIC_DIRECTION_LABELS = {
    Direction.UP: "上",
    Direction.DOWN: "下",
    Direction.LEFT: "左",
    Direction.RIGHT: "右",
}
DIAGNOSTIC_OUTCOME_LABELS = {
    "selected": "已移动",
    "no_candidate": "没有可用候选，保持原位",
    "geometry_changed": "候选位置变化，已重新扫描",
}


def build_navigation_diagnostic(
    targets: Sequence[TargetSnapshot],
    current_index: int,
    direction: Direction,
    *,
    ranked_indices: Optional[Sequence[int]] = None,
    available_indices: Sequence[int] = (),
    invalid_cached_indices: Sequence[int] = (),
    unhittable_indices: Sequence[int] = (),
    selected_index: Optional[int] = None,
    outcome: str = "no_candidate",
) -> Optional[NavigationDiagnostic]:
    if not targets or not 0 <= current_index < len(targets):
        return None

    current = targets[current_index]
    descendants_by_target = finer_descendant_index_map(targets)
    grid_rects = navigation_grid_rects(targets, descendants_by_target)
    current_rect = grid_rects[current_index]
    ranked = tuple(
        ranked_target_indices(
            targets,
            current_index,
            direction,
            descendants_by_target,
            grid_rects,
        )
        if ranked_indices is None
        else ranked_indices
    )
    natural = set(
        ranked_target_indices(
            targets,
            current_index,
            direction,
            descendants_by_target,
            grid_rects,
        )
    )
    scored_by_index = {
        index: direction_score(current_rect, grid_rects[index], direction)
        for index in ranked
        if 0 <= index < len(targets) and index != current_index
    }
    candidates: list[CandidateDiagnostic] = []
    for rank, index in enumerate(ranked, 1):
        if not 0 <= index < len(targets) or index == current_index:
            continue
        target = targets[index]
        score = direction_score(current_rect, grid_rects[index], direction)
        if target.rect.contains(current.rect) and target.rect != current.rect:
            route = "parent_cell"
        elif index not in natural and target.rect.contains(current.rect):
            route = "reverse"
        elif index not in natural:
            route = "reverse"
        elif score is None:
            route = "reverse"
        elif score is not None and score[0] == 0:
            route = "lane"
        else:
            route = "diagonal"
        candidates.append(
            CandidateDiagnostic(
                index=index,
                target=target,
                rank=rank,
                route=route,
                common_path_prefix=_common_path_prefix_length(
                    current.path, target.path
                ),
                beam_rank=None if score is None else score[0],
                primary_gap=None if score is None else score[1],
                perpendicular_gap=None if score is None else score[2],
                score=None if score is None else score[3],
                center_offset=None if score is None else score[4],
            )
        )

    ranked_set = set(ranked)
    rejected_counts = {reason: 0 for reason in DIAGNOSTIC_REJECTION_ORDER}
    for index, target in enumerate(targets):
        if index == current_index or index in ranked_set:
            continue
        target_rect = grid_rects[index]
        if target_rect == current_rect:
            reason = "same_rectangle"
        elif direction_score(current_rect, target_rect, direction) is None:
            reason = "wrong_direction"
        else:
            continue
        rejected_counts[reason] += 1

    return NavigationDiagnostic(
        current_index=current_index,
        current=current,
        direction=direction,
        candidates=tuple(candidates),
        available_indices=tuple(available_indices),
        invalid_cached_indices=tuple(invalid_cached_indices),
        unhittable_indices=tuple(unhittable_indices),
        selected_index=selected_index,
        outcome=outcome,
        rejected_counts=tuple(
            (reason, rejected_counts[reason])
            for reason in DIAGNOSTIC_REJECTION_ORDER
            if rejected_counts[reason]
        ),
    )


def _diagnostic_target_label(index: int, target: TargetSnapshot) -> str:
    label = " ".join((target.name or target.automation_id or target.control_type).split())
    if len(label) > 60:
        label = label[:57] + "..."
    rect = target.rect
    return (
        f"#{index + 1} {label} "
        f"[{rect.left},{rect.top},{rect.width}x{rect.height}]"
    )


def format_navigation_diagnostic(
    diagnostic: NavigationDiagnostic,
    *,
    candidate_limit: int = 8,
) -> str:
    lines = [
        f"[导航诊断] 方向={DIAGNOSTIC_DIRECTION_LABELS[diagnostic.direction]}",
        "当前: "
        + _diagnostic_target_label(
            diagnostic.current_index, diagnostic.current
        ),
    ]
    if diagnostic.selected_index is None:
        result = DIAGNOSTIC_OUTCOME_LABELS.get(
            diagnostic.outcome, diagnostic.outcome
        )
    else:
        selected = next(
            (
                item.target
                for item in diagnostic.candidates
                if item.index == diagnostic.selected_index
            ),
            None,
        )
        result = DIAGNOSTIC_OUTCOME_LABELS.get(
            diagnostic.outcome, diagnostic.outcome
        )
        if selected is not None:
            result += ": " + _diagnostic_target_label(
                diagnostic.selected_index, selected
            )
    lines.append("结果: " + result)

    displayed = list(diagnostic.candidates[: max(0, candidate_limit)])
    if diagnostic.selected_index is not None and not any(
        item.index == diagnostic.selected_index for item in displayed
    ):
        selected_item = next(
            (
                item
                for item in diagnostic.candidates
                if item.index == diagnostic.selected_index
            ),
            None,
        )
        if selected_item is not None:
            displayed.append(selected_item)

    available = set(diagnostic.available_indices)
    invalid_cached = set(diagnostic.invalid_cached_indices)
    unhittable = set(diagnostic.unhittable_indices)
    if displayed:
        lines.append("候选（距离数值越小越优先）:")
    for item in displayed:
        status = []
        if item.index == diagnostic.selected_index:
            status.append("最终选中")
        elif item.index in invalid_cached:
            status.append("此前已失效")
        elif item.index in unhittable:
            status.append("当前无法命中")
        elif item.index not in available:
            status.append("本轮历史去重")
        else:
            status.append("可用")
        metrics = ""
        if item.score is not None:
            metrics = (
                f"，向前距离={item.primary_gap:.0f}，横向偏离={item.perpendicular_gap:.0f}，"
                f"综合值={item.score:.1f}"
            )
        lines.append(
            f"  {item.rank}. {_diagnostic_target_label(item.index, item.target)}；"
            f"{DIAGNOSTIC_ROUTE_LABELS[item.route]}，共同层级={item.common_path_prefix}"
            f"{metrics}；{'/'.join(status)}"
        )

    if diagnostic.rejected_counts:
        lines.append(
            "未进入候选: "
            + "，".join(
                f"{DIAGNOSTIC_REJECTION_LABELS[reason]} {count} 个"
                for reason, count in diagnostic.rejected_counts
            )
        )
    return "\n".join(lines)


OPPOSITE_DIRECTION = {
    Direction.UP: Direction.DOWN,
    Direction.DOWN: Direction.UP,
    Direction.LEFT: Direction.RIGHT,
    Direction.RIGHT: Direction.LEFT,
}


class NavigationGraph:
    """Cache geometry-only candidates for one flat screen layout."""

    def __init__(self, targets: Sequence[TargetSnapshot]) -> None:
        self.targets = tuple(targets)
        self._descendants_by_target = finer_descendant_index_map(self.targets)
        self.grid_rects = navigation_grid_rects(
            self.targets, self._descendants_by_target
        )
        self._natural: dict[tuple[int, Direction], tuple[int, ...]] = {}

    def natural_candidates(
        self, current_index: int, direction: Direction
    ) -> tuple[int, ...]:
        key = (current_index, direction)
        natural = self._natural.get(key)
        if natural is None:
            natural = tuple(
                ranked_target_indices(
                    self.targets,
                    current_index,
                    direction,
                    self._descendants_by_target,
                    self.grid_rects,
                )
            )
            self._natural[key] = natural
        return natural

    def candidates(self, current_index: int, direction: Direction) -> tuple[int, ...]:
        return self.natural_candidates(current_index, direction)


@dataclass
class NavigationTraversal:
    direction: Optional[Direction] = None
    visited: set[int] = field(default_factory=set)
    last_from: Optional[int] = None
    last_to: Optional[int] = None
    last_direction: Optional[Direction] = None
    pending_from: Optional[int] = None
    pending_direction: Optional[Direction] = None

    def reset(self) -> None:
        self.direction = None
        self.visited.clear()
        self.last_from = None
        self.last_to = None
        self.last_direction = None
        self.pending_from = None
        self.pending_direction = None

    def available(
        self,
        current_index: int,
        direction: Direction,
        candidates: Sequence[int],
    ) -> tuple[int, ...]:
        if direction != self.direction:
            self.direction = direction
            self.visited = {current_index}
        else:
            self.visited.add(current_index)
        ranked = tuple(candidates)
        if (
            self.last_direction is not None
            and direction == OPPOSITE_DIRECTION[self.last_direction]
            and current_index == self.last_to
            and self.last_from is not None
        ):
            ranked = (self.last_from,) + tuple(
                index for index in ranked if index != self.last_from
            )
        self.pending_from = current_index
        self.pending_direction = direction
        return tuple(index for index in ranked if index not in self.visited)

    def commit(self, selected_index: int) -> None:
        self.last_from = self.pending_from
        self.last_to = selected_index
        self.last_direction = self.pending_direction
        self.visited.add(selected_index)


def geometry_anchor_indices(count: int, selected: int) -> list[int]:
    if count <= 0:
        return []
    candidates = [selected, 0, count // 2, count - 1]
    return list(dict.fromkeys(index for index in candidates if 0 <= index < count))


def shifted_snapshot(target: TargetSnapshot, delta_x: int, delta_y: int) -> TargetSnapshot:
    rect = target.rect
    section_rect = target.section_rect
    return replace(
        target,
        rect=Rect(
            rect.left + delta_x,
            rect.top + delta_y,
            rect.right + delta_x,
            rect.bottom + delta_y,
        ),
        section_rect=(
            None
            if section_rect is None
            else Rect(
                section_rect.left + delta_x,
                section_rect.top + delta_y,
                section_rect.right + delta_x,
                section_rect.bottom + delta_y,
            )
        ),
    )


def shifted_point(
    point: tuple[int, int], delta_x: int, delta_y: int
) -> tuple[int, int]:
    return point[0] + delta_x, point[1] + delta_y


def same_target_identity(first: TargetSnapshot, second: TargetSnapshot) -> bool:
    if first.runtime_id and second.runtime_id:
        return first.runtime_id == second.runtime_id
    return first.control_type == second.control_type and first.rect == second.rect


def native_handle_value(handle: Any) -> int:
    return int(handle or 0)


def keyboard_navigation_action(vk: int) -> Optional[str]:
    return NAVIGATION_KEY_ACTIONS.get(vk)


def global_hotkey_action(vk: int) -> Optional[str]:
    return GLOBAL_HOTKEY_ACTIONS.get(vk)


def should_pass_through_native_menu(vk: int, menu_mode_active: bool) -> bool:
    return menu_mode_active and vk in NATIVE_MENU_NAVIGATION_KEYS


def content_refresh_delay_ms(event: str, repeated_activation: bool = False) -> int:
    if event == "contexted":
        return 120
    if event == "activated" and repeated_activation:
        return 180
    return 0


def mouse_wheel_data(delta: int) -> int:
    return delta & 0xFFFFFFFF


def owner_chain_contains(
    start_hwnd: int,
    expected_hwnd: int,
    owner_of: Callable[[int], int],
    max_depth: int = 16,
) -> bool:
    if start_hwnd <= 0 or expected_hwnd <= 0:
        return False
    current = start_hwnd
    seen = {current}
    for _depth in range(max_depth):
        try:
            current = native_handle_value(owner_of(current))
        except Exception:
            return False
        if current <= 0 or current in seen:
            return False
        if current == expected_hwnd:
            return True
        seen.add(current)
    return False


def overlay_window_is_related(
    candidate_process_id: int,
    root_process_id: int,
    *,
    candidate_owned_by_root: bool,
    root_owned_by_candidate: bool,
    extended_style: int,
) -> bool:
    return bool(
        candidate_process_id == root_process_id
        or candidate_owned_by_root
        or root_owned_by_candidate
        or extended_style & (WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
    )


def overlay_window_is_candidate(
    root_rect: Rect,
    candidate_rect: Rect,
    *,
    visible: bool,
    minimized: bool,
    cloaked: bool,
    related: bool,
    explicitly_associated: bool = False,
    trusted_small_overlay: bool = False,
) -> bool:
    if (
        not visible
        or minimized
        or cloaked
        or not related
        or candidate_rect.width < 16
        or candidate_rect.height < 16
    ):
        return False
    candidate_area = candidate_rect.width * candidate_rect.height
    root_area = max(1, root_rect.width * root_rect.height)
    if candidate_area > root_area * OVERLAY_MAX_ROOT_AREA_RATIO:
        return False
    if explicitly_associated:
        return True
    if (
        trusted_small_overlay
        and candidate_rect.width <= OVERLAY_ROOT_TARGET_MAX_SIZE
        and candidate_rect.height <= OVERLAY_ROOT_TARGET_MAX_SIZE
    ):
        return True
    intersection = _rect_intersection_area(root_rect, candidate_rect)
    return intersection >= candidate_area * OVERLAY_MIN_INTERSECTION_RATIO


def root_only_overlay_target_spec(
    rect: Rect,
    name: str = "",
) -> Optional[SyntheticTargetSpec]:
    if (
        rect.width < 16
        or rect.height < 16
        or rect.width > OVERLAY_ROOT_TARGET_MAX_SIZE
        or rect.height > OVERLAY_ROOT_TARGET_MAX_SIZE
    ):
        return None
    label = name.strip()
    if not label or label == "CustomWindowAutomationPeer":
        label = "悬浮操作"
    return SyntheticTargetSpec(
        TargetSnapshot(
            rect=rect,
            name=label,
            control_type="OverlayWindowControl",
            path=(),
            depth=0,
            has_action_pattern=True,
            source="window-root",
            section_rect=rect,
        ),
        (round(rect.center_x), round(rect.center_y)),
    )


def navigation_foreground_action(
    foreground_hwnd: int,
    current_hwnd: int,
    root_hwnd: int,
    root_process_id: int,
    prototype_process_id: int,
    process_id_of: Callable[[int], int],
    owner_of: Callable[[int], int],
    associated_hwnds: Sequence[int] = (),
) -> str:
    """Choose whether to keep, follow, ignore, or leave the active context."""

    if foreground_hwnd <= 0 or foreground_hwnd == current_hwnd:
        return "sync"
    if foreground_hwnd in associated_hwnds:
        return "sync"
    try:
        foreground_process_id = int(process_id_of(foreground_hwnd))
    except Exception:
        return "leave"
    if foreground_process_id == prototype_process_id:
        return "ignore"
    if root_process_id > 0 and foreground_process_id == root_process_id:
        return "follow"
    related_handles = tuple(
        handle for handle in (current_hwnd, root_hwnd) if handle > 0
    )
    if any(
        owner_chain_contains(foreground_hwnd, handle, owner_of)
        or owner_chain_contains(handle, foreground_hwnd, owner_of)
        for handle in related_handles
    ):
        return "follow"
    return "leave"


def target_pointer_point(
    target: TargetSnapshot,
    verified_point: Optional[tuple[int, int]],
    allow_rect_center: bool,
) -> Optional[tuple[int, int]]:
    if verified_point is not None:
        return verified_point
    if allow_rect_center:
        return round(target.rect.center_x), round(target.rect.center_y)
    return None


def configure_standard_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="backslashreplace")
        except (OSError, ValueError):
            continue


def prewarm_request_due(
    foreground_hwnd: int,
    observed_hwnd: int,
    observed_since: float,
    requested_hwnd: int,
    now: float,
    stability_seconds: float = PREWARM_STABILITY_SECONDS,
) -> bool:
    return bool(
        foreground_hwnd > 0
        and foreground_hwnd == observed_hwnd
        and foreground_hwnd != requested_hwnd
        and now - observed_since >= stability_seconds
    )


def scan_should_stop(
    deadline: Optional[float],
    should_cancel: Optional[Callable[[], bool]],
    now: Optional[float] = None,
) -> bool:
    if should_cancel is not None and should_cancel():
        return True
    return bool(
        deadline is not None
        and (time.perf_counter() if now is None else now) >= deadline
    )


def bounded_scan_timeout_ms(
    deadline: Optional[float],
    maximum_ms: int,
    now: Optional[float] = None,
) -> int:
    """Return a per-call timeout that cannot outlive the scan deadline."""

    if deadline is None:
        return max(1, maximum_ms)
    remaining = deadline - (time.perf_counter() if now is None else now)
    if remaining <= 0:
        return 0
    return max(1, min(maximum_ms, int(remaining * 1000)))


def scan_commit_decision(
    expected_generation: int,
    current_generation: int,
    interrupted: bool,
    cancellation_requested: bool,
    allow_partial: bool,
) -> tuple[bool, bool]:
    """Return commit and partial flags for a completed worker scan."""

    if expected_generation != current_generation:
        return False, False
    if interrupted or cancellation_requested:
        return (True, True) if allow_partial else (False, False)
    return True, False


def empty_follow_refresh_should_retry(
    pending_window: int,
    current_window: int,
    attempts: int,
) -> bool:
    """Keep a followed window alive while its async tree is still empty."""

    return bool(
        pending_window > 0
        and pending_window == current_window
        and attempts < FOLLOW_WINDOW_EMPTY_REFRESH_RETRIES
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
        (rect.right - inset_x, rect.top + inset_y),
        (rect.right - inset_x, rect.bottom - inset_y),
    ]
    return list(dict.fromkeys(points))


def available_target_probe_points(
    target: TargetSnapshot,
    targets: Sequence[TargetSnapshot],
) -> list[tuple[int, int]]:
    """Return points that are not occupied by a retained finer action."""

    finer_rects = [
        candidate.rect
        for candidate in targets
        if candidate is not target and target_is_action_descendant(target, candidate)
    ]
    return [
        point
        for point in target_probe_points(target.rect)
        if not any(rect.contains_point(point) for rect in finer_rects)
    ]


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
    """Drop only weak UIA wrappers, preserving real parent and child actions."""

    keep = []
    for index, target in enumerate(targets):
        if (
            target.control_type in WRAPPER_CONTROL_TYPES
            and not target_has_interaction_evidence(target)
            and not target.supports_expand
        ):
            contains_nested_target = any(
                other_index != index
                and target.path
                and other.path
                and len(other.path) > len(target.path)
                and other.path[: len(target.path)] == target.path
                and target.rect != other.rect
                and target.rect.contains(other.rect)
                for other_index, other in enumerate(targets)
            )
            if contains_nested_target:
                continue

        weak_target_under_action = bool(
            not target_has_interaction_evidence(target)
            and any(
                other_index != index
                and target.path
                and other.path
                and len(other.path) < len(target.path)
                and target.path[: len(other.path)] == other.path
                and target_has_interaction_evidence(other)
                and other.rect.contains(target.rect)
                for other_index, other in enumerate(targets)
            )
        )
        if not weak_target_under_action:
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


def focus_only_structural_target_is_specific(
    control_type: str,
    name: str,
    automation_id: str,
    rect: Rect,
) -> bool:
    """Allow compact focus-only custom controls, not broad layout containers."""

    return bool(
        control_type in STRUCTURAL_CONTROL_TYPES
        and structural_action_has_identity(control_type, name, automation_id)
        and rect.width <= SEMANTIC_BYPASS_MAX_WIDTH
        and rect.height <= SEMANTIC_BYPASS_MAX_HEIGHT
    )


def semantic_action_can_bypass_point_hit(target: TargetSnapshot) -> bool:
    return bool(
        target.name
        and target.has_action_pattern
        and target.control_type in PRIMARY_ACTION_CONTROL_TYPES
        and (
            target.name in PRESERVED_NESTED_ACTION_NAMES
            or (
                target.rect.width <= SEMANTIC_BYPASS_MAX_WIDTH
                and target.rect.height <= SEMANTIC_BYPASS_MAX_HEIGHT
            )
        )
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
    parser.add_argument(
        "--window-handle",
        type=lambda value: int(value, 0),
        default=0,
        help="scan this native window handle instead of the foreground window",
    )
    parser.add_argument(
        "--diagnostics",
        action="store_true",
        help="start with navigation candidate diagnostics enabled",
    )
    parser.add_argument(
        "--quicker-state-file",
        default=os.environ.get(
            QUICKER_STATE_FILE_ENV,
            default_quicker_state_file(),
        ),
        help="optional JSON snapshot exported by a Quicker bridge action",
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
            if structural and not structural_action_has_identity(
                control_type, name, automation_id
            ):
                return None
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
                and (
                    direct_action_pattern
                    or (
                        keyboard_focusable
                        and focus_only_structural_target_is_specific(
                            control_type,
                            name,
                            automation_id,
                            rect,
                        )
                    )
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

    def process_name_from_id(process_id: int) -> str:
        if process_id <= 0:
            return ""
        handle = kernel32.OpenProcess(
            process_query_limited_information,
            False,
            process_id,
        )
        if not handle:
            return ""
        try:
            size = wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(size.value)
            if not kernel32.QueryFullProcessImageNameW(
                handle,
                0,
                buffer,
                ctypes.byref(size),
            ):
                return ""
            return normalized_process_name(buffer.value)
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
                list[TargetSnapshot],
                tuple[int, ...],
                tuple[int, ...],
                tuple[int, ...],
            ]:
                current = self.selected
                current_snapshots = [target.snapshot for target in self.targets]
                natural_candidates = self.navigation_graph.natural_candidates(
                    current, direction
                )
                ranked_candidates = self.navigation_graph.candidates(
                    current, direction
                )
                available_candidates = self.traversal.available(
                    current, direction, ranked_candidates
                )
                return (
                    current,
                    current_snapshots,
                    natural_candidates,
                    ranked_candidates,
                    available_candidates,
                )

            current_index, snapshots, natural, ranked, candidates = load_candidates()
            first_index = candidates[0] if candidates else None
            first_rect = (
                self.targets[first_index].snapshot.rect
                if first_index is not None
                else None
            )
            candidate_is_natural = first_index is not None and first_index in natural
            process_id = window_process_id(self.hwnd)
            dirty_state = dirty_windows.state(self.hwnd, process_id)
            now = time.perf_counter()
            suspicious_move = (
                not candidate_is_natural
                or move_should_refresh_dynamic_targets(
                    snapshots[current_index].rect,
                    first_rect,
                    direction,
                    self.window_rect,
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
                self.traversal.commit(next_index)
                self._clear_hierarchy()
                emit_diagnostic("selected", next_index)
                self._emit_selection()
                return
            emit_diagnostic("no_candidate")

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

        def _scan(self, hwnd: int, expected_generation: int) -> None:
            started = time.perf_counter()
            with self._post_lock:
                if expected_generation != self._generation:
                    self.events.put(("scan_cancelled", None))
                    return
            point = cursor_point()
            process_id = window_process_id(hwnd)
            watcher_changed = dirty_windows.watch(hwnd, process_id)
            used_cache = not watcher_changed and self._cache_is_reusable(hwnd)
            if used_cache:
                with self._post_lock:
                    if expected_generation != self._generation:
                        self.events.put(("scan_cancelled", None))
                        return
                    self.context_valid = True
                    self.invalid_targets.clear()
            else:
                committed, _partial, _empty = self._enumerate(
                    hwnd,
                    expected_generation=expected_generation,
                )
                if not committed:
                    previous_process_id = (
                        window_process_id(self.hwnd) if self.hwnd > 0 else 0
                    )
                    dirty_windows.watch(self.hwnd, previous_process_id)
                    self.events.put(("scan_cancelled", None))
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
                            self._scan(int(value), generation)
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
                        self.events.put(
                            (
                                "error",
                                {"command": command, "message": str(exc)},
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

    class StructureChangeWatcher:
        HOOK_RANGES = ((0x8000, 0x8004), (0x800A, 0x800A))
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
            _object_id: int,
            _child_id: int,
            _event_thread: int,
            _event_time: int,
        ) -> None:
            if not hwnd or not is_navigation_structure_event(int(event_id)):
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
        VK_CONTROL = 0x11
        VK_MENU = 0x12

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
            self._passthrough: set[int] = set()
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

            if is_up and vk in self._passthrough:
                self._passthrough.discard(vk)
                return user32.CallNextHookEx(
                    self._hook, code, wparam, lparam
                )

            if is_up and vk in self._swallowed:
                self._swallowed.discard(vk)
                return 1

            ctrl_alt = self._pressed(self.VK_CONTROL) and self._pressed(self.VK_MENU)
            hotkey_action = global_hotkey_action(vk)
            if is_down and ctrl_alt and hotkey_action is not None:
                self._swallowed.add(vk)
                if not was_down:
                    self._on_action(hotkey_action)
                return 1

            action = keyboard_navigation_action(vk)
            if self._active.is_set() and action is not None:
                if should_pass_through_native_menu(
                    vk, native_menu_mode_active()
                ):
                    if is_down:
                        self._passthrough.add(vk)
                    return user32.CallNextHookEx(
                        self._hook, code, wparam, lparam
                    )
                self._swallowed.add(vk)
                if is_down and (vk in self._down):
                    if vk in (VK_RETURN, VK_APPS, VK_ESCAPE) and was_down:
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
    app.setApplicationName("Remote Mic Element Navigation Prototype")
    prototype_process_id = int(kernel32.GetCurrentProcessId())
    overlay = NavigationOverlay()
    worker = AutomationWorker(diagnostics_enabled=bool(args.diagnostics))
    worker.start()
    keyboard_events: queue.Queue[str] = queue.Queue()
    active = threading.Event()
    scanning = False
    shutting_down = False
    prewarm_observed_hwnd = 0
    prewarm_observed_at = 0.0
    prewarm_requested_hwnd = 0
    navigation_root_hwnd = 0
    navigation_process_id = 0
    navigation_overlay_signature: tuple[tuple[int, Rect], ...] = ()
    diagnostics_enabled = bool(args.diagnostics)

    def enqueue_keyboard_action(action: str) -> None:
        keyboard_events.put(action)

    hook = KeyboardHook(enqueue_keyboard_action, active)
    hook.start()
    structure_watcher = StructureChangeWatcher()
    if not structure_watcher.start():
        print(
            "界面变化监听未完整启用，将按缓存时限兜底刷新。",
            file=sys.stderr,
        )

    def leave_navigation() -> None:
        nonlocal navigation_root_hwnd, navigation_process_id
        nonlocal navigation_overlay_signature
        active.clear()
        worker.deactivate()
        overlay.clear_target()
        navigation_root_hwnd = 0
        navigation_process_id = 0
        navigation_overlay_signature = ()

    def request_quit() -> None:
        nonlocal shutting_down
        if shutting_down:
            return
        shutting_down = True
        leave_navigation()
        app.quit()

    def prepare_navigation_action() -> bool:
        foreground = native_handle_value(user32.GetForegroundWindow())
        foreground_action = navigation_foreground_action(
            foreground,
            worker.hwnd,
            navigation_root_hwnd,
            navigation_process_id,
            prototype_process_id,
            window_process_id,
            window_owner,
            tuple(handle for handle, _rect in navigation_overlay_signature),
        )
        if foreground_action == "leave":
            print("导航已暂停: 已切换到其它窗口，请重新按 Ctrl+Alt+N。")
            leave_navigation()
            return False
        if foreground_action == "follow":
            worker.post("follow_window", foreground)
        return True

    def handle_keyboard_action(action: str) -> None:
        nonlocal scanning, navigation_root_hwnd, navigation_process_id
        nonlocal navigation_overlay_signature, diagnostics_enabled
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
            if active.is_set():
                leave_navigation()
            elif not scanning:
                hwnd = native_handle_value(user32.GetForegroundWindow())
                if hwnd <= 0:
                    print("没有可扫描的前台窗口。")
                    return
                navigation_root_hwnd = hwnd
                navigation_process_id = window_process_id(hwnd)
                navigation_overlay_signature = associated_overlay_window_signature(
                    hwnd,
                    excluded_process_id=prototype_process_id,
                )
                scanning = True
                print("正在扫描当前窗口...")
                worker.post("scan", hwnd)
        elif action == "cancel":
            if active.is_set():
                worker.post("back")
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
            elif event == "scan_cancelled":
                scanning = False
            elif event == "selection":
                if active.is_set():
                    overlay.show_target(
                        payload["target"],
                        payload["selected"],
                        payload["count"],
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
                if target.source == "visual-grid" and active.is_set():
                    QTimer.singleShot(180, lambda: worker.post("refresh_content"))
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
                        payload["selected"],
                        payload["count"],
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
                        payload["selected"],
                        payload["count"],
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
                    scanning = False
                leave_navigation()
                print(f"操作失败: {message}", file=sys.stderr)

    timer = QTimer()
    timer.timeout.connect(drain_events)
    timer.start(20)

    def monitor_navigation_context() -> None:
        nonlocal prewarm_observed_hwnd, prewarm_observed_at, prewarm_requested_hwnd
        nonlocal navigation_overlay_signature
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
        current_overlay_signature = associated_overlay_window_signature(
            navigation_root_hwnd,
            excluded_process_id=prototype_process_id,
        )
        if current_overlay_signature != navigation_overlay_signature:
            navigation_overlay_signature = current_overlay_signature
            worker.post("refresh_content")
        foreground_action = navigation_foreground_action(
            foreground,
            worker.hwnd,
            navigation_root_hwnd,
            navigation_process_id,
            prototype_process_id,
            window_process_id,
            window_owner,
            tuple(handle for handle, _rect in navigation_overlay_signature),
        )
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

    cleanup_complete = False

    def cleanup() -> None:
        nonlocal cleanup_complete
        if cleanup_complete:
            return
        cleanup_complete = True
        hook.stop()
        structure_watcher.stop()
        worker.stop()

    app.aboutToQuit.connect(cleanup)
    print("元素导航键盘原型已启动。")
    print(
        "Ctrl+Alt+N 开始/退出，Ctrl+Alt+D 开关导航诊断，"
        "方向键移动，PageUp/PageDown 切换父子元素，"
        "Enter 左击（快速两次为双击），菜单键右击，音量键滚动，"
        "Esc 退出，Ctrl+Alt+Q 关闭。"
    )
    if diagnostics_enabled:
        print("导航诊断已开启。每次方向移动都会解释候选排序。")
    try:
        return int(app.exec())
    finally:
        cleanup()


def main(argv: Optional[Sequence[str]] = None) -> int:
    configure_standard_streams()
    args = _parse_args(argv)
    if sys.platform != "win32":
        print("这个原型只支持 Windows。", file=sys.stderr)
        return 2
    return _run_windows(args)


if __name__ == "__main__":
    raise SystemExit(main())
