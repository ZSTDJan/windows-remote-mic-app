"""Platform-neutral element targeting and refresh support.

This module turns discovered UI data into stable navigation targets. It
does not import UI Automation, Qt, Win32 APIs, or the executable entry.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from dataclasses import dataclass, replace
from typing import Callable, Optional, Sequence

from spatial_navigation_core import (
    Direction,
    PRIMARY_ACTION_CONTROL_TYPES,
    Rect,
    TargetSnapshot,
    _axis_overlap,
    direction_score,
    target_has_interaction_evidence,
    target_is_action_descendant,
)
STRUCTURAL_CONTROL_TYPES = frozenset(
    {"CustomControl", "PaneControl", "GroupControl", "ImageControl"}
)

SPLIT_ACTION_ANCHOR_CONTROL_TYPES = frozenset(
    {"ButtonControl", "SplitButtonControl", "ComboBoxControl"}
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

CHROMIUM_MIN_SCAN_DEPTH = 32

SEMANTIC_BYPASS_MAX_WIDTH = 180

SEMANTIC_BYPASS_MAX_HEIGHT = 96

SPLIT_COMPANION_MAX_WIDTH = 64

SPLIT_COMPANION_MAX_HEIGHT = 96

SPLIT_COMPANION_MAX_GAP = 4

PREWARM_STABILITY_SECONDS = 0.75

DYNAMIC_REFRESH_FALLBACK_SECONDS = 5.0

DYNAMIC_REFRESH_MAX_CACHE_SECONDS = 30.0

DYNAMIC_REFRESH_SETTLE_SECONDS = 0.15

FOLLOW_WINDOW_SCAN_BUDGET_SECONDS = 0.2

FOLLOW_WINDOW_EMPTY_REFRESH_RETRIES = 2

EVENT_OBJECT_LOCATIONCHANGE = 0x800B

OBJID_CARET = -8

OBJID_CURSOR = -9

NAVIGATION_STRUCTURE_EVENTS = frozenset(
    {
        0x8000,  # EVENT_OBJECT_CREATE
        0x8001,  # EVENT_OBJECT_DESTROY
        0x8002,  # EVENT_OBJECT_SHOW
        0x8003,  # EVENT_OBJECT_HIDE
        0x8004,  # EVENT_OBJECT_REORDER
        0x800A,  # EVENT_OBJECT_STATECHANGE
        EVENT_OBJECT_LOCATIONCHANGE,
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
    has_direct_action_pattern: bool = False
    has_legacy_pattern: bool = False
    has_scroll_pattern: bool = False

@dataclass(frozen=True)
class SyntheticTargetSpec:
    snapshot: TargetSnapshot
    click_point: tuple[int, int]

@dataclass(frozen=True)
class OpaqueVisualSurface:
    rect: Rect
    path: tuple[int, ...]
    name: str = ""

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

def is_navigation_structure_event(event_id: int) -> bool:
    return event_id in NAVIGATION_STRUCTURE_EVENTS

def navigation_structure_event_affects_targets(
    event_id: int, object_id: int
) -> bool:
    if not is_navigation_structure_event(event_id):
        return False
    return not (
        event_id == EVENT_OBJECT_LOCATIONCHANGE
        and object_id in {OBJID_CARET, OBJID_CURSOR}
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
    direct_action_descendants: dict[
        tuple[int, ...], list[ElementSnapshot]
    ] = defaultdict(list)
    for element in elements:
        if not element.has_direct_action_pattern:
            continue
        for depth in range(1, len(element.path) + 1):
            direct_action_descendants[element.path[:depth]].append(element)

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
            action_evidence = child.has_direct_action_pattern or any(
                _rect_intersection_area(child.rect, action.rect)
                >= min(
                    child.rect.width * child.rect.height,
                    action.rect.width * action.rect.height,
                )
                * 0.85
                for action in direct_action_descendants.get(child.path, ())
            )
            if not action_evidence:
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

def split_button_companion_target_specs(
    elements: Sequence[ElementSnapshot],
    window_rect: Rect,
) -> list[SyntheticTargetSpec]:
    """Expose a compact menu half next to a separately wrapped main button."""

    children: dict[tuple[int, ...], list[ElementSnapshot]] = defaultdict(list)
    action_descendants: dict[tuple[int, ...], list[ElementSnapshot]] = defaultdict(list)
    for element in elements:
        if element.path:
            children[element.path[:-1]].append(element)
        if (
            element.control_type in SPLIT_ACTION_ANCHOR_CONTROL_TYPES
            and (element.name or element.automation_id)
        ):
            for depth in range(1, len(element.path)):
                action_descendants[element.path[:depth]].append(element)

    specs: list[SyntheticTargetSpec] = []
    for candidate in elements:
        rect = candidate.rect
        if (
            not candidate.path
            or candidate.control_type != "GroupControl"
            or candidate.name
            or candidate.automation_id
            or not candidate.enabled
            or candidate.offscreen
            or not candidate.has_direct_action_pattern
            or not 16 <= rect.width <= SPLIT_COMPANION_MAX_WIDTH
            or not 24 <= rect.height <= SPLIT_COMPANION_MAX_HEIGHT
            or not rect.intersects(window_rect)
            or action_descendants.get(candidate.path)
        ):
            continue

        matched_anchor: Optional[ElementSnapshot] = None
        for peer in children.get(candidate.path[:-1], ()):
            peer_rect = peer.rect
            gap = rect.left - peer_rect.right
            vertical_overlap = _axis_overlap(
                rect.top,
                rect.bottom,
                peer_rect.top,
                peer_rect.bottom,
            )
            if (
                peer.path == candidate.path
                or peer.control_type != "GroupControl"
                or not peer.enabled
                or peer.offscreen
                or not peer.has_direct_action_pattern
                or peer_rect.center_x >= rect.center_x
                or gap < -2
                or gap > SPLIT_COMPANION_MAX_GAP
                or peer_rect.width < rect.width
                or peer_rect.width > 240
                or vertical_overlap
                < min(rect.height, peer_rect.height) * 0.75
            ):
                continue

            anchors = [
                action
                for action in action_descendants.get(peer.path, ())
                if not action.offscreen
                and action.rect.width >= 16
                and action.rect.height >= 16
                and _rect_intersection_area(peer_rect, action.rect)
                >= action.rect.width * action.rect.height * 0.75
            ]
            if anchors:
                matched_anchor = min(
                    anchors,
                    key=lambda action: (
                        len(action.path),
                        -action.rect.width * action.rect.height,
                    ),
                )
                break

        if matched_anchor is None:
            continue
        label = (
            f"{matched_anchor.name}的更多选项"
            if matched_anchor.name
            else "更多选项"
        )
        specs.append(
            SyntheticTargetSpec(
                TargetSnapshot(
                    rect=rect,
                    name=label,
                    control_type="GroupControl",
                    automation_id=candidate.automation_id,
                    path=candidate.path,
                    depth=len(candidate.path),
                    has_action_pattern=True,
                    source="uia-split-action",
                ),
                (round(rect.center_x), round(rect.center_y)),
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

def content_refresh_delay_ms(event: str, repeated_activation: bool = False) -> int:
    if event == "contexted":
        return 120
    if event == "activated" and repeated_activation:
        return 180
    if event == "scrolled":
        return 180
    return 0

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

def msaa_wrapper_should_be_ignored(
    msaa_rect: Rect,
    window_rect: Rect,
    existing_targets: Sequence[TargetSnapshot],
) -> bool:
    """Ignore a window-sized legacy shell when finer UIA actions exist."""

    if not existing_targets:
        return False
    window_area = max(1, window_rect.width * window_rect.height)
    coverage = _rect_intersection_area(msaa_rect, window_rect) / window_area
    if coverage < 0.85:
        return False
    contained_targets = sum(
        target.rect != msaa_rect
        and msaa_rect.contains(target.rect)
        for target in existing_targets
    )
    return contained_targets >= 2

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

def structural_target_has_actionable_semantics(
    control_type: str,
    name: str,
    automation_id: str,
    rect: Rect,
    *,
    keyboard_focusable: bool,
    has_direct_action_pattern: bool,
    has_text_edit_pattern: bool,
) -> bool:
    if control_type not in STRUCTURAL_CONTROL_TYPES:
        return False
    if keyboard_focusable and has_text_edit_pattern:
        return True
    if not structural_action_has_identity(control_type, name, automation_id):
        return False
    return bool(
        has_direct_action_pattern
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


__all__ = (
    'STRUCTURAL_CONTROL_TYPES',
    'SPLIT_ACTION_ANCHOR_CONTROL_TYPES',
    'WRAPPER_CONTROL_TYPES',
    'LEGACY_ONLY_WEAK_CONTROL_TYPES',
    'NOISE_NAME_PREFIXES',
    'PRESERVED_NESTED_ACTION_NAMES',
    'CHROMIUM_MIN_SCAN_DEPTH',
    'SEMANTIC_BYPASS_MAX_WIDTH',
    'SEMANTIC_BYPASS_MAX_HEIGHT',
    'SPLIT_COMPANION_MAX_WIDTH',
    'SPLIT_COMPANION_MAX_HEIGHT',
    'SPLIT_COMPANION_MAX_GAP',
    'PREWARM_STABILITY_SECONDS',
    'DYNAMIC_REFRESH_FALLBACK_SECONDS',
    'DYNAMIC_REFRESH_MAX_CACHE_SECONDS',
    'DYNAMIC_REFRESH_SETTLE_SECONDS',
    'FOLLOW_WINDOW_SCAN_BUDGET_SECONDS',
    'FOLLOW_WINDOW_EMPTY_REFRESH_RETRIES',
    'EVENT_OBJECT_LOCATIONCHANGE',
    'OBJID_CARET',
    'OBJID_CURSOR',
    'NAVIGATION_STRUCTURE_EVENTS',
    'SECTION_MAX_WINDOW_WIDTH_RATIO',
    'SECTION_MIN_WINDOW_WIDTH_RATIO',
    'SECTION_BODY_MIN_WINDOW_HEIGHT_RATIO',
    'SECTION_BODY_MAX_WINDOW_HEIGHT_RATIO',
    'SECTION_HEADER_MIN_WINDOW_WIDTH_RATIO',
    'SECTION_HEADER_MAX_WINDOW_HEIGHT_RATIO',
    'SECTION_HEADER_MAX_TOP_OFFSET_RATIO',
    'SECTION_MIN_HEIGHT',
    'NAVIGATION_SECTION_CONTROL_TYPES',
    'REPEATED_CONTENT_PARENT_TYPES',
    'REPEATED_CONTENT_ITEM_TYPES',
    'VISUAL_SURFACE_CONTROL_TYPES',
    'VISUAL_SURFACE_MIN_WIDTH',
    'VISUAL_SURFACE_MIN_HEIGHT',
    'VISUAL_SURFACE_MIN_WINDOW_AREA_RATIO',
    'ElementSnapshot',
    'SyntheticTargetSpec',
    'OpaqueVisualSurface',
    'DirtyWindowState',
    'DirtyWindowTracker',
    'is_navigation_structure_event',
    'navigation_structure_event_affects_targets',
    'standard_control_has_actionable_semantics',
    '_path_is_descendant',
    '_median_number',
    '_rect_intersection_area',
    'repeated_content_target_specs',
    'split_button_companion_target_specs',
    'opaque_visual_surfaces',
    '_filled_activity_runs',
    '_substantial_regular_run_clusters',
    '_screen_to_image_rect',
    '_image_to_screen_rect',
    'visual_grid_target_specs',
    'move_should_refresh_dynamic_targets',
    'background_refresh_due',
    'dynamic_refresh_fallback_due',
    'infer_navigation_section_path',
    'geometry_anchor_indices',
    'shifted_snapshot',
    'shifted_point',
    'same_target_identity',
    'content_refresh_delay_ms',
    'prewarm_request_due',
    'scan_should_stop',
    'bounded_scan_timeout_ms',
    'scan_commit_decision',
    'empty_follow_refresh_should_retry',
    'target_probe_points',
    'available_target_probe_points',
    'initial_target_index',
    'hit_target_match_index',
    'msaa_wrapper_should_be_ignored',
    'nested_container_keep_indices',
    'target_quality_rank',
    'flat_target_indices',
    'restore_target_index',
    'is_navigation_noise',
    'structural_action_has_identity',
    'focus_only_structural_target_is_specific',
    'structural_target_has_actionable_semantics',
    'semantic_action_can_bypass_point_hit',
    'effective_scan_depth',
)
