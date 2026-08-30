"""Platform-independent spatial navigation geometry and traversal core.

The executable Windows/UIA prototype remains in
``element_navigation_prototype.py``. This module contains only the data
models and deterministic navigation algorithms shared by that host.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, Sequence

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
HORIZONTAL_LANE_MIN_CENTER_TOLERANCE = 12.0
HORIZONTAL_LANE_MAX_CENTER_TOLERANCE = 48.0
HORIZONTAL_LANE_SIZE_MULTIPLIER = 0.75
VERTICAL_LANE_MIN_CENTER_TOLERANCE = 16.0
VERTICAL_LANE_MAX_CENTER_TOLERANCE = 96.0
VERTICAL_LANE_SIZE_MULTIPLIER = 0.35
GRID_SAFE_CELL_MAX_CHILDREN = 24
RECTANGULAR_GRID_MIN_SHARED_EDGE_UNITS = 0.32
RECTANGULAR_GRID_SHARED_EDGE_RATIO = 0.18
RECTANGULAR_GRID_BALANCE_WEIGHT = 0.08
RECTANGULAR_GRID_DISTANT_GAP_MIN = 2400
RECTANGULAR_GRID_DISTANT_GAP_RATIO = 3.0
RECTANGULAR_GRID_DISTANT_EXTENT_RATIO = 4.0
SKELETON_LANE_MIN_UNITS = 0.75
SKELETON_LANE_MAX_UNITS = 1.75
SKELETON_LANE_SPAN_MULTIPLIER = 1.5
SKELETON_PROMOTION_MIN_UNITS = 1.5
SKELETON_ROUNDING_REFERENCE_UNIT = 60.0
SKELETON_SUPPORT_MAX_GAP_UNITS = 32.0


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


def _axis_gap(a_start: int, a_end: int, b_start: int, b_end: int) -> int:
    if b_start > a_end:
        return b_start - a_end
    if a_start > b_end:
        return a_start - b_end
    return 0


def _axis_overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> int:
    return max(0, min(a_end, b_end) - max(a_start, b_start))


def target_has_interaction_evidence(target: TargetSnapshot) -> bool:
    """Return whether the element itself represents an operable action."""

    return bool(
        target.has_action_pattern
        or target.supports_expand
        or target.control_type in PRIMARY_ACTION_CONTROL_TYPES
    )


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


def _union_rect(rects: Sequence[Rect]) -> Rect:
    return Rect(
        min(rect.left for rect in rects),
        min(rect.top for rect in rects),
        max(rect.right for rect in rects),
        max(rect.bottom for rect in rects),
    )


def _rectangular_grid_prefers_rows(
    indices: Sequence[int],
    anchor_rects: Sequence[Rect],
    bounds: Rect,
) -> bool:
    if len(indices) < 3:
        return False
    heights = sorted(max(1, anchor_rects[index].height) for index in indices)
    median_height = heights[len(heights) // 2]
    tolerance = max(8.0, median_height * 0.75)
    row_centers: list[list[float]] = []
    for center in sorted(anchor_rects[index].center_y for index in indices):
        if not row_centers or center - row_centers[-1][-1] > tolerance:
            row_centers.append([center])
        else:
            row_centers[-1].append(center)
    repeated_rows = sum(1 for row in row_centers if len(row) >= 2)
    widths = sorted(max(1, anchor_rects[index].width) for index in indices)
    median_width = widths[len(widths) // 2]
    column_tolerance = max(8.0, median_width * 0.75)
    column_centers: list[list[float]] = []
    for center in sorted(anchor_rects[index].center_x for index in indices):
        if (
            not column_centers
            or center - column_centers[-1][-1] > column_tolerance
        ):
            column_centers.append([center])
        else:
            column_centers[-1].append(center)
    repeated_columns = sum(
        1 for column in column_centers if len(column) >= 2
    )
    has_wide_row = any(
        anchor_rects[index].width >= bounds.width * 0.6
        for index in indices
    )
    if repeated_columns >= 2 and repeated_rows < 2 and not has_wide_row:
        return False
    return (
        len(row_centers) >= 2
        and (
            repeated_rows >= 2
            or has_wide_row
            or (
                len(column_centers) == 1
                and bounds.height >= bounds.width * 1.1
            )
        )
    )


def _rectangular_partition(
    indices: Sequence[int],
    bounds: Rect,
    anchor_rects: Sequence[Rect],
    stable_keys: Sequence[tuple[Any, ...]],
    output: list[Optional[Rect]],
    *,
    preferred_axis: Optional[str] = None,
) -> None:
    if not indices or bounds.width <= 0 or bounds.height <= 0:
        return
    if len(indices) == 1:
        output[indices[0]] = bounds
        return

    def center(index: int, axis: str) -> float:
        rect = anchor_rects[index]
        return rect.center_x if axis == "x" else rect.center_y

    candidates: list[
        tuple[int, float, float, str, int, int, list[int]]
    ] = []
    for axis in ("x", "y"):
        start = bounds.left if axis == "x" else bounds.top
        end = bounds.right if axis == "x" else bounds.bottom
        if end - start < 2:
            continue

        def edge_start(index: int) -> int:
            rect = anchor_rects[index]
            return rect.left if axis == "x" else rect.top

        def edge_end(index: int) -> int:
            rect = anchor_rects[index]
            return rect.right if axis == "x" else rect.bottom

        # A cut through real controls creates thin, unstable bands. Prefer
        # empty range gaps before considering center-only separation.
        ordered = sorted(
            indices,
            key=lambda index: (center(index, axis), stable_keys[index]),
        )
        prefix_end: list[int] = []
        maximum_end = edge_end(ordered[0])
        for index in ordered:
            maximum_end = max(maximum_end, edge_end(index))
            prefix_end.append(maximum_end)
        suffix_start = [0] * len(ordered)
        minimum_start = edge_start(ordered[-1])
        for offset in range(len(ordered) - 1, -1, -1):
            minimum_start = min(minimum_start, edge_start(ordered[offset]))
            suffix_start[offset] = minimum_start

        for offset in range(1, len(ordered)):
            before = center(ordered[offset - 1], axis)
            after = center(ordered[offset], axis)
            if after <= before:
                continue
            first_end = prefix_end[offset - 1]
            second_start = suffix_start[offset]
            if first_end <= second_start:
                split = round((first_end + second_start) / 2)
                clearance = second_start - first_end
                overlap = 0
            else:
                split = round((before + after) / 2)
                clearance = 0
                overlap = first_end - second_start
            split = max(start + 1, min(end - 1, split))
            crossings = sum(
                edge_start(index) < split < edge_end(index)
                for index in indices
            )
            balance = 1.0 - abs(offset - (len(ordered) - offset)) / len(ordered)
            gap = (after - before) / max(1, end - start)
            priority = (
                clearance / max(1, end - start)
                + gap
                + balance * RECTANGULAR_GRID_BALANCE_WEIGHT
                + (0.015 if axis == preferred_axis else 0.0)
            )
            candidates.append(
                (
                    crossings,
                    overlap / max(1, end - start),
                    -priority,
                    axis,
                    split,
                    offset,
                    ordered,
                )
            )

    if candidates:
        _crossings, _overlap, _priority, axis, split, offset, ordered = min(
            candidates,
            key=lambda item: (
                item[0],
                item[1],
                item[2],
                item[3] != "y",
                item[4],
            ),
        )
    else:
        axis = "x" if bounds.width >= bounds.height else "y"
        start = bounds.left if axis == "x" else bounds.top
        end = bounds.right if axis == "x" else bounds.bottom
        if end - start < 2:
            axis = "y" if axis == "x" else "x"
            start = bounds.left if axis == "x" else bounds.top
            end = bounds.right if axis == "x" else bounds.bottom
        ordered = sorted(
            indices,
            key=lambda index: (center(index, axis), stable_keys[index]),
        )
        offset = max(1, min(len(ordered) - 1, len(ordered) // 2))
        shared_center = center(ordered[0], axis)
        split = max(start + 1, min(end - 1, round(shared_center)))

    first_bounds = bounds
    second_bounds = bounds
    if axis == "x":
        first_bounds = Rect(bounds.left, bounds.top, split, bounds.bottom)
        second_bounds = Rect(split, bounds.top, bounds.right, bounds.bottom)
    else:
        first_bounds = Rect(bounds.left, bounds.top, bounds.right, split)
        second_bounds = Rect(bounds.left, split, bounds.right, bounds.bottom)
    _rectangular_partition(
        ordered[:offset],
        first_bounds,
        anchor_rects,
        stable_keys,
        output,
        preferred_axis=preferred_axis,
    )
    _rectangular_partition(
        ordered[offset:],
        second_bounds,
        anchor_rects,
        stable_keys,
        output,
        preferred_axis=preferred_axis,
    )


def _range_occupancy_stable_key(
    target: TargetSnapshot, anchor_rect: Rect
) -> tuple[Any, ...]:
    return (
        anchor_rect.top,
        anchor_rect.left,
        anchor_rect.bottom,
        anchor_rect.right,
        target.control_type,
        target.name,
        target.automation_id,
        target.runtime_id,
        target.path,
    )


def _distant_cluster_split(
    indices: Sequence[int],
    anchor_rects: Sequence[Rect],
    stable_keys: Sequence[tuple[Any, ...]],
) -> Optional[tuple[str, int, int, list[int]]]:
    choices: list[tuple[float, str, int, int, list[int]]] = []
    for axis in ("x", "y"):
        ordered = sorted(
            indices,
            key=lambda index: (
                anchor_rects[index].center_x
                if axis == "x"
                else anchor_rects[index].center_y,
                stable_keys[index],
            ),
        )
        centers = [
            anchor_rects[index].center_x
            if axis == "x"
            else anchor_rects[index].center_y
            for index in ordered
        ]
        gaps = [
            (centers[offset] - centers[offset - 1], offset)
            for offset in range(1, len(centers))
            if centers[offset] > centers[offset - 1]
        ]
        if not gaps:
            continue
        best_gap, offset = max(gaps)
        other_gaps = sorted(
            (gap for gap, other_offset in gaps if other_offset != offset),
            reverse=True,
        )
        comparison_gap = other_gaps[0] if other_gaps else 0.0
        extents = sorted(
            max(
                1,
                anchor_rects[index].width
                if axis == "x"
                else anchor_rects[index].height,
            )
            for index in indices
        )
        median_extent = extents[len(extents) // 2]
        required_gap = max(
            RECTANGULAR_GRID_DISTANT_GAP_MIN,
            comparison_gap * RECTANGULAR_GRID_DISTANT_GAP_RATIO,
            median_extent * RECTANGULAR_GRID_DISTANT_EXTENT_RATIO,
        )
        if best_gap < required_gap:
            continue
        split = round((centers[offset - 1] + centers[offset]) / 2)
        choices.append((best_gap / required_gap, axis, split, offset, ordered))
    if not choices:
        return None
    _strength, axis, split, offset, ordered = max(
        choices,
        key=lambda item: (item[0], item[1] == "y", -item[2]),
    )
    return axis, split, offset, ordered


def _expand_partition_to_bounds(
    indices: Sequence[int],
    local_bounds: Rect,
    allocated_bounds: Rect,
    output: list[Optional[Rect]],
) -> None:
    if local_bounds == allocated_bounds:
        return
    for index in indices:
        rect = output[index]
        if rect is None:
            continue
        output[index] = Rect(
            allocated_bounds.left if rect.left == local_bounds.left else rect.left,
            allocated_bounds.top if rect.top == local_bounds.top else rect.top,
            allocated_bounds.right if rect.right == local_bounds.right else rect.right,
            allocated_bounds.bottom if rect.bottom == local_bounds.bottom else rect.bottom,
        )


def _partition_distant_clusters(
    indices: Sequence[int],
    allocated_bounds: Rect,
    anchor_rects: Sequence[Rect],
    stable_keys: Sequence[tuple[Any, ...]],
    output: list[Optional[Rect]],
) -> None:
    split = _distant_cluster_split(indices, anchor_rects, stable_keys)
    if split is None:
        local_bounds = _union_rect([anchor_rects[index] for index in indices])
        preferred_axis = (
            "y"
            if _rectangular_grid_prefers_rows(
                indices, anchor_rects, local_bounds
            )
            else None
        )
        _rectangular_partition(
            indices,
            local_bounds,
            anchor_rects,
            stable_keys,
            output,
            preferred_axis=preferred_axis,
        )
        _expand_partition_to_bounds(
            indices, local_bounds, allocated_bounds, output
        )
        return

    axis, split_at, offset, ordered = split
    if axis == "x":
        split_at = max(
            allocated_bounds.left + 1,
            min(allocated_bounds.right - 1, split_at),
        )
        first_bounds = Rect(
            allocated_bounds.left,
            allocated_bounds.top,
            split_at,
            allocated_bounds.bottom,
        )
        second_bounds = Rect(
            split_at,
            allocated_bounds.top,
            allocated_bounds.right,
            allocated_bounds.bottom,
        )
    else:
        split_at = max(
            allocated_bounds.top + 1,
            min(allocated_bounds.bottom - 1, split_at),
        )
        first_bounds = Rect(
            allocated_bounds.left,
            allocated_bounds.top,
            allocated_bounds.right,
            split_at,
        )
        second_bounds = Rect(
            allocated_bounds.left,
            split_at,
            allocated_bounds.right,
            allocated_bounds.bottom,
        )
    _partition_distant_clusters(
        ordered[:offset],
        first_bounds,
        anchor_rects,
        stable_keys,
        output,
    )
    _partition_distant_clusters(
        ordered[offset:],
        second_bounds,
        anchor_rects,
        stable_keys,
        output,
    )


def range_occupancy_grid_rects(
    targets: Sequence[TargetSnapshot],
    anchor_rects: Optional[Sequence[Rect]] = None,
) -> tuple[Rect, ...]:
    """Build one gapless orthogonal territory map from visible screen geometry.

    UIA ancestry remains useful for recognizing actions, but it cannot move an
    element away from its visible position. Every split therefore operates on
    the single flat screen plane and keeps the target center inside its own
    territory.
    """

    if not targets:
        return ()
    if anchor_rects is None:
        anchor_rects = navigation_grid_rects(targets)
    anchors = tuple(anchor_rects)
    output: list[Optional[Rect]] = [None] * len(targets)
    indices = list(range(len(targets)))
    bounds = _union_rect(anchors)
    stable_keys = tuple(
        _range_occupancy_stable_key(target, anchors[index])
        for index, target in enumerate(targets)
    )
    _partition_distant_clusters(
        indices,
        bounds,
        anchors,
        stable_keys,
        output,
    )
    return tuple(
        rect if rect is not None else anchors[index]
        for index, rect in enumerate(output)
    )


def territory_contains_anchor_center(territory: Rect, anchor: Rect) -> bool:
    return bool(
        territory.left <= anchor.center_x <= territory.right
        and territory.top <= anchor.center_y <= territory.bottom
    )


@dataclass(frozen=True)
class NavigationContact:
    target_index: int
    start: int
    end: int

    @property
    def length(self) -> int:
        return max(0, self.end - self.start)


def _minimum_navigation_contact(
    first_anchor: Rect,
    second_anchor: Rect,
    direction: Direction,
    scale_unit: float,
) -> int:
    smaller = (
        min(first_anchor.height, second_anchor.height)
        if direction in {Direction.LEFT, Direction.RIGHT}
        else min(first_anchor.width, second_anchor.width)
    )
    scale_floor = min(
        max(1, round(scale_unit * RECTANGULAR_GRID_MIN_SHARED_EDGE_UNITS)),
        max(1, round(smaller * 0.5)),
    )
    return min(
        max(1, smaller),
        max(
            scale_floor,
            round(smaller * RECTANGULAR_GRID_SHARED_EDGE_RATIO),
        ),
    )


def range_occupancy_navigation_contacts(
    territories: Sequence[Rect],
    anchor_rects: Sequence[Rect],
    scale_unit: Optional[float] = None,
) -> dict[tuple[int, Direction], tuple[NavigationContact, ...]]:
    if scale_unit is None:
        scale_unit = navigation_scale_unit(anchor_rects)
    contacts: dict[tuple[int, Direction], list[NavigationContact]] = defaultdict(list)
    for first_index, first in enumerate(territories):
        for second_index in range(first_index + 1, len(territories)):
            second = territories[second_index]
            if first.right == second.left or second.right == first.left:
                start = max(first.top, second.top)
                end = min(first.bottom, second.bottom)
                if end > start:
                    if first.right == second.left:
                        first_direction = Direction.RIGHT
                        second_direction = Direction.LEFT
                    else:
                        first_direction = Direction.LEFT
                        second_direction = Direction.RIGHT
                    if (
                        direction_score(
                            anchor_rects[first_index],
                            anchor_rects[second_index],
                            first_direction,
                        )
                        is not None
                        and end - start >= _minimum_navigation_contact(
                            anchor_rects[first_index],
                            anchor_rects[second_index],
                            first_direction,
                            scale_unit,
                        )
                    ):
                        contacts[(first_index, first_direction)].append(
                            NavigationContact(second_index, start, end)
                        )
                        contacts[(second_index, second_direction)].append(
                            NavigationContact(first_index, start, end)
                        )
            if first.bottom == second.top or second.bottom == first.top:
                start = max(first.left, second.left)
                end = min(first.right, second.right)
                if end > start:
                    if first.bottom == second.top:
                        first_direction = Direction.DOWN
                        second_direction = Direction.UP
                    else:
                        first_direction = Direction.UP
                        second_direction = Direction.DOWN
                    if (
                        direction_score(
                            anchor_rects[first_index],
                            anchor_rects[second_index],
                            first_direction,
                        )
                        is not None
                        and end - start >= _minimum_navigation_contact(
                            anchor_rects[first_index],
                            anchor_rects[second_index],
                            first_direction,
                            scale_unit,
                        )
                    ):
                        contacts[(first_index, first_direction)].append(
                            NavigationContact(second_index, start, end)
                        )
                        contacts[(second_index, second_direction)].append(
                            NavigationContact(first_index, start, end)
                        )
    return {key: tuple(value) for key, value in contacts.items()}


def _navigation_contact_rank(
    active_rect: Rect,
    target_rect: Rect,
    direction: Direction,
    contact: NavigationContact,
    crosses_parallel_section_boundary: bool = False,
) -> tuple[float, ...]:
    if direction in {Direction.LEFT, Direction.RIGHT}:
        active_start, active_end = active_rect.top, active_rect.bottom
        target_start, target_end = target_rect.top, target_rect.bottom
        active_center = active_rect.center_y
        target_center = target_rect.center_y
    else:
        active_start, active_end = active_rect.left, active_rect.right
        target_start, target_end = target_rect.left, target_rect.right
        active_center = active_rect.center_x
        target_center = target_rect.center_x
    target_lane_gap = _axis_gap(
        active_start,
        active_end,
        target_start,
        target_end,
    )
    lane_gap = _axis_gap(active_start, active_end, contact.start, contact.end)
    if contact.start <= active_center <= contact.end:
        center_gap = 0.0
    else:
        center_gap = min(
            abs(active_center - contact.start),
            abs(active_center - contact.end),
        )
    return (
        float(target_lane_gap > 0),
        float(crosses_parallel_section_boundary),
        float(target_lane_gap),
        float(lane_gap > 0),
        float(lane_gap),
        center_gap,
        abs(target_center - active_center),
        float(target_rect.top),
        float(target_rect.left),
        float(contact.target_index),
    )


def _navigation_lane_gap(
    current: Rect,
    target: Rect,
    direction: Direction,
) -> float:
    if direction in {Direction.LEFT, Direction.RIGHT}:
        return _axis_gap(current.top, current.bottom, target.top, target.bottom)
    return _axis_gap(current.left, current.right, target.left, target.right)


def _navigation_lane_tolerance(
    current: Rect,
    target: Rect,
    direction: Direction,
    scale_unit: float,
) -> float:
    if direction in {Direction.LEFT, Direction.RIGHT}:
        current_span = current.height
        target_span = target.height
    else:
        current_span = current.width
        target_span = target.width
    minimum = max(1.0, scale_unit * 0.75)
    maximum = max(minimum, scale_unit * 5.0)
    return max(minimum, min(maximum, (current_span + target_span) * 0.75))


def _navigation_perpendicular_center_offset(
    current: Rect,
    target: Rect,
    direction: Direction,
) -> float:
    if direction in {Direction.LEFT, Direction.RIGHT}:
        return abs(target.center_y - current.center_y)
    return abs(target.center_x - current.center_x)


def _navigation_forward_center_distance(
    current: Rect,
    target: Rect,
    direction: Direction,
) -> float:
    if direction == Direction.LEFT:
        return current.center_x - target.center_x
    if direction == Direction.RIGHT:
        return target.center_x - current.center_x
    if direction == Direction.UP:
        return current.center_y - target.center_y
    return target.center_y - current.center_y


def _skeleton_lane_tolerance(
    current: Rect,
    target: Rect,
    direction: Direction,
) -> float:
    if direction in {Direction.LEFT, Direction.RIGHT}:
        smaller_span = min(current.height, target.height)
    else:
        smaller_span = min(current.width, target.width)
    local_unit = navigation_scale_unit((current, target))
    minimum = max(1.0, local_unit * SKELETON_LANE_MIN_UNITS)
    maximum = max(minimum, local_unit * SKELETON_LANE_MAX_UNITS)
    return max(
        minimum,
        min(maximum, max(1, smaller_span) * SKELETON_LANE_SPAN_MULTIPLIER),
    )


def _skeleton_rounding_epsilon(current: Rect, target: Rect) -> float:
    return max(
        1.0,
        navigation_scale_unit((current, target))
        / SKELETON_ROUNDING_REFERENCE_UNIT,
    )


def _skeleton_lane_matches(
    current: Rect,
    target: Rect,
    direction: Direction,
) -> bool:
    tolerance = _skeleton_lane_tolerance(
        current,
        target,
        direction,
    )
    center_offset = _navigation_perpendicular_center_offset(
        current,
        target,
        direction,
    )
    rounding_epsilon = _skeleton_rounding_epsilon(current, target)
    return bool(
        _navigation_lane_gap(current, target, direction)
        <= tolerance + rounding_epsilon
        and center_offset <= tolerance + rounding_epsilon
        and _navigation_forward_center_distance(
            current,
            target,
            direction,
        )
        + rounding_epsilon
        >= center_offset
        and (
            center_offset <= rounding_epsilon
            or _navigation_forward_gap(current, target, direction)
            + rounding_epsilon
            >= center_offset * 0.75
        )
    )


def _skeleton_support_lane_matches(
    current: Rect,
    target: Rect,
    direction: Direction,
) -> bool:
    tolerance = _skeleton_lane_tolerance(current, target, direction)
    center_offset = _navigation_perpendicular_center_offset(
        current,
        target,
        direction,
    )
    if direction in {Direction.LEFT, Direction.RIGHT}:
        centers_are_separate = bool(
            not target.left <= current.center_x <= target.right
            and not current.left <= target.center_x <= current.right
        )
    else:
        centers_are_separate = bool(
            not target.top <= current.center_y <= target.bottom
            and not current.top <= target.center_y <= current.bottom
        )
    rounding_epsilon = _skeleton_rounding_epsilon(current, target)
    return bool(
        centers_are_separate
        and _navigation_lane_gap(current, target, direction)
        <= tolerance + rounding_epsilon
        and center_offset <= tolerance + rounding_epsilon
        and _navigation_forward_center_distance(
            current,
            target,
            direction,
        )
        + rounding_epsilon
        >= center_offset
    )


def _navigation_forward_gap(
    current: Rect,
    target: Rect,
    direction: Direction,
) -> float:
    score = direction_score(current, target, direction)
    return float("inf") if score is None else score[1]


def _skeleton_support_is_local(
    current: Rect,
    target: Rect,
    direction: Direction,
) -> bool:
    if direction in {Direction.LEFT, Direction.RIGHT}:
        local_extent = max(current.width, target.width)
    else:
        local_extent = max(current.height, target.height)
    local_unit = navigation_scale_unit((current, target))
    distance_limit = max(
        local_extent * RECTANGULAR_GRID_DISTANT_EXTENT_RATIO,
        local_unit * SKELETON_SUPPORT_MAX_GAP_UNITS,
    )
    return _navigation_forward_gap(current, target, direction) < distance_limit


def _navigation_forward_far_edge_distance(
    current: Rect,
    target: Rect,
    direction: Direction,
) -> float:
    if direction == Direction.LEFT:
        return max(0.0, float(current.left - target.left))
    if direction == Direction.RIGHT:
        return max(0.0, float(target.right - current.right))
    if direction == Direction.UP:
        return max(0.0, float(current.top - target.top))
    return max(0.0, float(target.bottom - current.bottom))


def navigation_scale_unit(rects: Sequence[Rect]) -> float:
    """Return a layout-relative unit for scale-stable navigation thresholds."""

    extents = sorted(
        min(rect.width, rect.height)
        for rect in rects
        if rect.width > 0 and rect.height > 0
    )
    if not extents:
        return 1.0
    middle = len(extents) // 2
    if len(extents) % 2:
        return float(extents[middle])
    return (float(extents[middle - 1]) + float(extents[middle])) / 2


def navigation_crosses_parallel_section_boundary(
    current: TargetSnapshot,
    target: TargetSnapshot,
    direction: Direction,
) -> bool:
    """Prefer the same visual band only when moving parallel to its divider."""

    current_section = current.section_rect
    target_section = target.section_rect
    if current_section is None or target_section is None:
        return False
    if direction in {Direction.UP, Direction.DOWN}:
        current_start, current_end = current_section.left, current_section.right
        target_start, target_end = target_section.left, target_section.right
    else:
        current_start, current_end = current_section.top, current_section.bottom
        target_start, target_end = target_section.top, target_section.bottom
    smaller_span = min(current_end - current_start, target_end - target_start)
    if smaller_span <= 0:
        return False
    overlap = _axis_overlap(
        current_start,
        current_end,
        target_start,
        target_end,
    )
    return overlap < smaller_span * 0.5


def _orthogonal_direction_toward(
    current: Rect,
    target: Rect,
    direction: Direction,
) -> Optional[Direction]:
    if direction in {Direction.UP, Direction.DOWN}:
        if target.center_x < current.center_x:
            return Direction.LEFT
        if target.center_x > current.center_x:
            return Direction.RIGHT
    else:
        if target.center_y < current.center_y:
            return Direction.UP
        if target.center_y > current.center_y:
            return Direction.DOWN
    return None


def direction_score(
    current: Rect, candidate: Rect, direction: Direction
) -> Optional[tuple[int, float, float, float, float, int, int]]:
    """Return a stable spatial-navigation score, or None for wrong direction.

    This is an independent prototype heuristic based on the same general
    geometry used by TV and CSS spatial navigation: stay in the requested
    half-plane, use center lines to identify the current row/column, then rank
    by forward distance and remaining perpendicular geometry.
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
        center_tolerance = max(
            HORIZONTAL_LANE_MIN_CENTER_TOLERANCE,
            min(
                HORIZONTAL_LANE_MAX_CENTER_TOLERANCE,
                smaller_height * HORIZONTAL_LANE_SIZE_MULTIPLIER,
            ),
        )
        # Rectangle overlap alone cannot create a row: a tall target must not
        # claim every horizontal track that crosses its bounds.
        beam_rank = 0 if center_offset <= center_tolerance else 1
    else:
        smaller_width = max(1, min(current.width, candidate.width))
        center_tolerance = max(
            VERTICAL_LANE_MIN_CENTER_TOLERANCE,
            min(
                VERTICAL_LANE_MAX_CENTER_TOLERANCE,
                smaller_width * VERTICAL_LANE_SIZE_MULTIPLIER,
            ),
        )
        beam_rank = 0 if center_offset <= center_tolerance else 1
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


def _range_frontier_rank_key(
    current: Rect,
    candidate: Rect,
    direction: Direction,
    score: tuple[int, float, float, float, float, int, int],
    common_prefix: int,
) -> tuple[float, ...]:
    """Prefer the first full-range contact before center-line alignment."""

    if direction in {Direction.LEFT, Direction.RIGHT}:
        overlap = _axis_overlap(
            current.top, current.bottom, candidate.top, candidate.bottom
        )
        forward_center_distance = abs(candidate.center_x - current.center_x)
    else:
        overlap = _axis_overlap(
            current.left, current.right, candidate.left, candidate.right
        )
        forward_center_distance = abs(candidate.center_y - current.center_y)
    if overlap > 0:
        return (
            0.0,
            score[1],
            float(-overlap),
            score[4],
            forward_center_distance,
            float(-common_prefix),
            float(score[5]),
            float(score[6]),
        )
    return (
        1.0,
        *_direction_rank_key(
            current,
            candidate,
            direction,
            score,
            common_prefix,
        ),
    )


def ranked_target_indices(
    targets: Sequence[TargetSnapshot],
    current_index: int,
    direction: Direction,
    descendants_by_target: Optional[Sequence[Sequence[int]]] = None,
    grid_rects: Optional[Sequence[Rect]] = None,
    current_rect: Optional[Rect] = None,
) -> list[int]:
    if not targets or not 0 <= current_index < len(targets):
        return []
    if descendants_by_target is None:
        descendants_by_target = finer_descendant_index_map(targets)
    if grid_rects is None:
        grid_rects = navigation_grid_rects(targets, descendants_by_target)
    current = grid_rects[current_index] if current_rect is None else current_rect
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
        return _range_frontier_rank_key(
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

OPPOSITE_DIRECTION = {
    Direction.UP: Direction.DOWN,
    Direction.DOWN: Direction.UP,
    Direction.LEFT: Direction.RIGHT,
    Direction.RIGHT: Direction.LEFT,
}


def _xy_focus_is_candidate(
    source: Rect, candidate: Rect, direction: Direction
) -> bool:
    if direction == Direction.LEFT:
        return (
            (source.right > candidate.right or source.left >= candidate.right)
            and source.left > candidate.left
        )
    if direction == Direction.RIGHT:
        return (
            (source.left < candidate.left or source.right <= candidate.left)
            and source.right < candidate.right
        )
    if direction == Direction.UP:
        return (
            (source.bottom > candidate.bottom or source.top >= candidate.bottom)
            and source.top > candidate.top
        )
    return (
        (source.top < candidate.top or source.bottom <= candidate.top)
        and source.bottom < candidate.bottom
    )


def _xy_focus_beams_overlap(
    source: Rect, candidate: Rect, direction: Direction
) -> bool:
    if direction in {Direction.LEFT, Direction.RIGHT}:
        return candidate.bottom >= source.top and candidate.top <= source.bottom
    return candidate.right >= source.left and candidate.left <= source.right


def _xy_focus_is_strictly_in_direction(
    source: Rect, candidate: Rect, direction: Direction
) -> bool:
    if direction == Direction.LEFT:
        return source.left >= candidate.right
    if direction == Direction.RIGHT:
        return source.right <= candidate.left
    if direction == Direction.UP:
        return source.top >= candidate.bottom
    return source.bottom <= candidate.top


def _xy_focus_major_axis_distance(
    source: Rect, candidate: Rect, direction: Direction
) -> float:
    if direction == Direction.LEFT:
        return max(0, source.left - candidate.right)
    if direction == Direction.RIGHT:
        return max(0, candidate.left - source.right)
    if direction == Direction.UP:
        return max(0, source.top - candidate.bottom)
    return max(0, candidate.top - source.bottom)


def _xy_focus_major_axis_far_edge_distance(
    source: Rect, candidate: Rect, direction: Direction
) -> float:
    if direction == Direction.LEFT:
        return max(1, source.left - candidate.left)
    if direction == Direction.RIGHT:
        return max(1, candidate.right - source.right)
    if direction == Direction.UP:
        return max(1, source.top - candidate.top)
    return max(1, candidate.bottom - source.bottom)


def _xy_focus_minor_axis_distance(
    source: Rect, candidate: Rect, direction: Direction
) -> float:
    if direction in {Direction.LEFT, Direction.RIGHT}:
        return abs(source.center_y - candidate.center_y)
    return abs(source.center_x - candidate.center_x)


def _xy_focus_beam_beats(
    source: Rect, first: Rect, second: Rect, direction: Direction
) -> bool:
    first_in_beam = _xy_focus_beams_overlap(source, first, direction)
    second_in_beam = _xy_focus_beams_overlap(source, second, direction)
    if second_in_beam or not first_in_beam:
        return False
    if not _xy_focus_is_strictly_in_direction(source, second, direction):
        return True
    if direction in {Direction.LEFT, Direction.RIGHT}:
        return True
    return _xy_focus_major_axis_distance(
        source, first, direction
    ) < _xy_focus_major_axis_far_edge_distance(source, second, direction)


def _xy_focus_weighted_distance(
    source: Rect, candidate: Rect, direction: Direction
) -> float:
    major = _xy_focus_major_axis_distance(source, candidate, direction)
    minor = _xy_focus_minor_axis_distance(source, candidate, direction)
    return 13 * major * major + minor * minor


def _xy_focus_better(
    source: Rect,
    candidate: Rect,
    incumbent: Optional[Rect],
    direction: Direction,
) -> bool:
    if not _xy_focus_is_candidate(source, candidate, direction):
        return False
    if incumbent is None or not _xy_focus_is_candidate(
        source, incumbent, direction
    ):
        return True
    if _xy_focus_beam_beats(source, candidate, incumbent, direction):
        return True
    if _xy_focus_beam_beats(source, incumbent, candidate, direction):
        return False
    return _xy_focus_weighted_distance(
        source, candidate, direction
    ) < _xy_focus_weighted_distance(source, incumbent, direction)


def xy_focus_target_index(
    targets: Sequence[TargetSnapshot],
    rects: Sequence[Rect],
    current_index: int,
    direction: Direction,
    allowed_indices: Optional[set[int]] = None,
) -> Optional[int]:
    """Select one Android-style XY target with deterministic tie-breaking."""

    if not 0 <= current_index < len(rects):
        return None
    source = rects[current_index]
    best_index: Optional[int] = None
    best_rect: Optional[Rect] = None
    for index, candidate in enumerate(rects):
        if index == current_index:
            continue
        if allowed_indices is not None and index not in allowed_indices:
            continue
        candidate_is_better = _xy_focus_better(
            source, candidate, best_rect, direction
        )
        if not candidate_is_better and best_index is not None:
            candidate_is_better = bool(
                not _xy_focus_better(source, best_rect, candidate, direction)
                and _range_occupancy_stable_key(targets[index], candidate)
                < _range_occupancy_stable_key(targets[best_index], best_rect)
            )
        if candidate_is_better:
            best_index = index
            best_rect = candidate
    return best_index


def navigation_contact_cell(
    current: Rect, target: Rect, direction: Direction
) -> Rect:
    """Keep the contacted strip when entering a target that spans many cells."""

    if direction in {Direction.UP, Direction.DOWN}:
        left = max(current.left, target.left)
        right = min(current.right, target.right)
        if right <= left:
            width = max(1, min(current.width, target.width))
            center = min(max(current.center_x, target.left), target.right)
            left = max(target.left, round(center - width / 2))
            right = min(target.right, left + width)
            left = max(target.left, right - width)
        return Rect(left, target.top, right, target.bottom)

    top = max(current.top, target.top)
    bottom = min(current.bottom, target.bottom)
    if bottom <= top:
        height = max(1, min(current.height, target.height))
        center = min(max(current.center_y, target.top), target.bottom)
        top = max(target.top, round(center - height / 2))
        bottom = min(target.bottom, top + height)
        top = max(target.top, bottom - height)
    return Rect(target.left, top, target.right, bottom)


class NavigationGraph:
    """Cache projection-aware routes over one rectangular territory map."""

    def __init__(self, targets: Sequence[TargetSnapshot]) -> None:
        self.targets = tuple(targets)
        self._descendants_by_target = finer_descendant_index_map(self.targets)
        self.anchor_rects = navigation_grid_rects(
            self.targets, self._descendants_by_target
        )
        self.scale_unit = navigation_scale_unit(self.anchor_rects)
        self.grid_rects = range_occupancy_grid_rects(
            self.targets, self.anchor_rects
        )
        self._contacts = range_occupancy_navigation_contacts(
            self.grid_rects,
            self.anchor_rects,
            self.scale_unit,
        )
        self._natural: dict[tuple[int, Direction], tuple[int, ...]] = {}
        self._skeleton: dict[tuple[int, Direction], tuple[int, ...]] = {}
        self._skeleton_support: dict[tuple[int, bool], bool] = {}
        self._xy_fallback: dict[tuple[int, Direction], tuple[int, ...]] = {}

    def _has_skeleton_track_support(
        self,
        target_index: int,
        direction: Direction,
    ) -> bool:
        horizontal = direction in {Direction.LEFT, Direction.RIGHT}
        key = (target_index, horizontal)
        cached = self._skeleton_support.get(key)
        if cached is not None:
            return cached
        support_directions = (
            (Direction.UP, Direction.DOWN)
            if horizontal
            else (Direction.LEFT, Direction.RIGHT)
        )
        current = self.anchor_rects[target_index]
        supported = False
        for index, candidate in enumerate(self.anchor_rects):
            if index == target_index:
                continue
            for support_direction in support_directions:
                score = direction_score(current, candidate, support_direction)
                if (
                    score is not None
                    and _skeleton_support_lane_matches(
                        current,
                        candidate,
                        support_direction,
                    )
                    and _skeleton_support_is_local(
                        current,
                        candidate,
                        support_direction,
                    )
                ):
                    supported = True
                    break
            if supported:
                break
        self._skeleton_support[key] = supported
        return supported

    def _skeleton_candidates(
        self,
        current_index: int,
        direction: Direction,
        active_rect: Rect,
    ) -> tuple[int, ...]:
        use_cache = active_rect == self.anchor_rects[current_index]
        key = (current_index, direction)
        if use_cache:
            cached = self._skeleton.get(key)
            if cached is not None:
                return cached
        candidates = []
        current_supported = self._has_skeleton_track_support(
            current_index,
            direction,
        )
        for index, candidate in enumerate(self.anchor_rects):
            if index == current_index:
                continue
            score = direction_score(active_rect, candidate, direction)
            if score is None or not _skeleton_lane_matches(
                active_rect,
                candidate,
                direction,
            ):
                continue
            if not current_supported or not self._has_skeleton_track_support(
                index, direction
            ):
                continue
            candidates.append((index, score))
        candidates.sort(
            key=lambda item: (
                self._crosses_parallel_section_boundary(
                    current_index,
                    item[0],
                    direction,
                ),
                _navigation_forward_gap(
                    active_rect,
                    self.anchor_rects[item[0]],
                    direction,
                ),
                _navigation_perpendicular_center_offset(
                    active_rect,
                    self.anchor_rects[item[0]],
                    direction,
                ),
                _navigation_lane_gap(
                    active_rect,
                    self.anchor_rects[item[0]],
                    direction,
                ),
                item[1][3],
                _range_occupancy_stable_key(
                    self.targets[item[0]],
                    self.anchor_rects[item[0]],
                ),
            )
        )
        result = tuple(index for index, _score in candidates)
        if use_cache:
            self._skeleton[key] = result
        return result

    def _skeleton_axis_is_stable(
        self,
        current_index: int,
        direction: Direction,
        active_rect: Rect,
    ) -> bool:
        return bool(
            self._skeleton_candidates(current_index, direction, active_rect)
            or self._skeleton_candidates(
                current_index,
                OPPOSITE_DIRECTION[direction],
                active_rect,
            )
        )

    def _skeleton_accepts(
        self,
        active_rect: Rect,
        candidate_index: int,
        direction: Direction,
    ) -> bool:
        return bool(
            0 <= candidate_index < len(self.anchor_rects)
            and _skeleton_lane_matches(
                active_rect,
                self.anchor_rects[candidate_index],
                direction,
            )
        )

    def _skeleton_requires_orthogonal_step(
        self,
        current_index: int,
        direction: Direction,
        active_rect: Rect,
        candidate_index: int,
    ) -> bool:
        if (
            not 0 <= candidate_index < len(self.anchor_rects)
            or not self._skeleton_axis_is_stable(
                current_index,
                direction,
                active_rect,
            )
            or self._skeleton_accepts(
                active_rect,
                candidate_index,
                direction,
            )
        ):
            return False
        side_direction = _orthogonal_direction_toward(
            active_rect,
            self.anchor_rects[candidate_index],
            direction,
        )
        if side_direction is None:
            return False
        return self._orthogonal_route_reaches_candidate(
            current_index,
            side_direction,
            direction,
            candidate_index,
            active_rect,
            primary_only=True,
        )

    def _promote_skeleton_candidate(
        self,
        current_index: int,
        direction: Direction,
        active_rect: Rect,
        natural: Sequence[int],
    ) -> tuple[int, ...]:
        skeleton = self._skeleton_candidates(
            current_index,
            direction,
            active_rect,
        )
        if not skeleton:
            return tuple(natural)
        if not natural:
            return skeleton
        best = skeleton[0]
        incumbent = natural[0]
        if best == incumbent:
            return tuple(natural)
        best_gap = _navigation_forward_gap(
            active_rect,
            self.anchor_rects[best],
            direction,
        )
        incumbent_gap = _navigation_forward_gap(
            active_rect,
            self.anchor_rects[incumbent],
            direction,
        )
        local_unit = navigation_scale_unit(
            (
                self.anchor_rects[current_index],
                self.anchor_rects[best],
                self.anchor_rects[incumbent],
            )
        )
        material_margin = max(
            local_unit * SKELETON_PROMOTION_MIN_UNITS,
            incumbent_gap * 0.2,
        )
        if best_gap + material_margin >= incumbent_gap:
            if incumbent_gap > 0 or not self._skeleton_requires_orthogonal_step(
                current_index,
                direction,
                active_rect,
                incumbent,
            ):
                return tuple(natural)
        return tuple(dict.fromkeys((*skeleton, *natural)))

    def _projected_candidates(
        self,
        current_index: int,
        direction: Direction,
        active_rect: Rect,
    ) -> tuple[int, ...]:
        """Return real controls intersected by the requested direction beam."""

        ranked = ranked_target_indices(
            self.targets,
            current_index,
            direction,
            self._descendants_by_target,
            self.anchor_rects,
            active_rect,
        )
        projected = [
            index
            for index in ranked
            if _navigation_lane_gap(
                active_rect,
                self.anchor_rects[index],
                direction,
            )
            <= 0
        ]
        rank_by_index = {
            index: rank for rank, index in enumerate(ranked)
        }
        projected.sort(
            key=lambda index: (
                self._crosses_parallel_section_boundary(
                    current_index,
                    index,
                    direction,
                ),
                rank_by_index[index],
            )
        )
        return tuple(projected)

    def _merge_projected_and_adjacent(
        self,
        current_index: int,
        active_rect: Rect,
        direction: Direction,
        projected: Sequence[int],
        adjacent: Sequence[int],
    ) -> tuple[int, ...]:
        """Merge direct beam hits without skipping a nearer visual row."""

        pending = list(dict.fromkeys(projected))
        merged: list[int] = []
        for adjacent_index in adjacent:
            frontier = _navigation_forward_far_edge_distance(
                active_rect,
                self.anchor_rects[adjacent_index],
                direction,
            )
            before_frontier = [
                index
                for index in pending
                if _navigation_forward_gap(
                    active_rect,
                    self.anchor_rects[index],
                    direction,
                )
                <= frontier
            ]
            frontier_group = list(
                dict.fromkeys([*before_frontier, adjacent_index])
            )

            def frontier_rank(index: int) -> tuple[float, ...]:
                score = direction_score(
                    active_rect,
                    self.anchor_rects[index],
                    direction,
                )
                if score is None:
                    return (1.0, float("inf"), float(index))
                return (
                    float(
                        self._crosses_parallel_section_boundary(
                            current_index,
                            index,
                            direction,
                        )
                    ),
                    score[3],
                    score[1],
                    score[2],
                    score[4],
                    float(index),
                )

            frontier_group.sort(key=frontier_rank)
            merged.extend(
                index for index in frontier_group if index not in merged
            )
            pending = [
                index for index in pending if index not in before_frontier
            ]
        merged.extend(index for index in pending if index not in merged)
        return tuple(merged)

    def _unpromoted_candidates(
        self,
        current_index: int,
        direction: Direction,
        active_rect: Rect,
    ) -> tuple[int, ...]:
        projected = self._projected_candidates(
            current_index,
            direction,
            active_rect,
        )
        contacts = self._contacts.get((current_index, direction), ())
        adjacent = tuple(
            contact.target_index
            for contact in sorted(
                contacts,
                key=lambda contact: _navigation_contact_rank(
                    active_rect,
                    self.anchor_rects[contact.target_index],
                    direction,
                    contact,
                    self._crosses_parallel_section_boundary(
                        current_index,
                        contact.target_index,
                        direction,
                    ),
                ),
            )
        )
        return self._merge_projected_and_adjacent(
            current_index,
            active_rect,
            direction,
            projected,
            adjacent,
        )

    def _anchor_primary_candidate(
        self,
        current_index: int,
        direction: Direction,
        active_rect: Rect,
    ) -> Optional[int]:
        candidates: list[tuple[int, tuple[float, ...]]] = []
        for index, candidate in enumerate(self.anchor_rects):
            if index == current_index:
                continue
            score = direction_score(active_rect, candidate, direction)
            if score is None:
                continue
            tolerance = _skeleton_lane_tolerance(
                active_rect,
                candidate,
                direction,
            )
            rounding_epsilon = _skeleton_rounding_epsilon(
                active_rect,
                candidate,
            )
            if (
                _navigation_lane_gap(active_rect, candidate, direction)
                > tolerance + rounding_epsilon
                or _navigation_perpendicular_center_offset(
                    active_rect,
                    candidate,
                    direction,
                )
                > tolerance + rounding_epsilon
            ):
                continue
            candidates.append((index, score))
        candidates.sort(
            key=lambda item: (
                self._crosses_parallel_section_boundary(
                    current_index,
                    item[0],
                    direction,
                ),
                _navigation_forward_gap(
                    active_rect,
                    self.anchor_rects[item[0]],
                    direction,
                ),
                _navigation_perpendicular_center_offset(
                    active_rect,
                    self.anchor_rects[item[0]],
                    direction,
                ),
                _navigation_lane_gap(
                    active_rect,
                    self.anchor_rects[item[0]],
                    direction,
                ),
                item[1][3],
                _range_occupancy_stable_key(
                    self.targets[item[0]],
                    self.anchor_rects[item[0]],
                ),
            )
        )
        return candidates[0][0] if candidates else None

    def requires_orthogonal_grid_step(
        self,
        current_index: int,
        direction: Direction,
        active_rect: Rect,
        candidate_index: int,
    ) -> bool:
        if not 0 <= candidate_index < len(self.anchor_rects):
            return False
        candidate = self.anchor_rects[candidate_index]
        if _navigation_lane_gap(
            active_rect,
            candidate,
            direction,
        ) <= _navigation_lane_tolerance(
            active_rect,
            candidate,
            direction,
            self.scale_unit,
        ):
            return False
        side_direction = _orthogonal_direction_toward(
            active_rect,
            candidate,
            direction,
        )
        if side_direction is None:
            return False
        return self._orthogonal_route_reaches_candidate(
            current_index,
            side_direction,
            direction,
            candidate_index,
            active_rect,
        )

    def _orthogonal_route_reaches_candidate(
        self,
        current_index: int,
        side_direction: Direction,
        requested_direction: Direction,
        candidate_index: int,
        active_rect: Rect,
        *,
        primary_only: bool = False,
    ) -> bool:
        candidate = self.anchor_rects[candidate_index]
        if primary_only:
            node_index = current_index
            node_rect = active_rect
            visited = {current_index}
            while True:
                requested_index = self._anchor_primary_candidate(
                    node_index,
                    requested_direction,
                    node_rect,
                )
                if (
                    node_index != current_index
                    and requested_index == candidate_index
                ):
                    return True

                side_index = self._anchor_primary_candidate(
                    node_index,
                    side_direction,
                    node_rect,
                )
                if (
                    side_index is None
                    or side_index == candidate_index
                    or side_index in visited
                ):
                    return False
                side_target = self.anchor_rects[side_index]
                node_distance = (
                    abs(candidate.center_x - node_rect.center_x)
                    if side_direction in {Direction.LEFT, Direction.RIGHT}
                    else abs(candidate.center_y - node_rect.center_y)
                )
                side_distance = (
                    abs(candidate.center_x - side_target.center_x)
                    if side_direction in {Direction.LEFT, Direction.RIGHT}
                    else abs(candidate.center_y - side_target.center_y)
                )
                if side_distance >= node_distance:
                    return False
                visited.add(side_index)
                node_rect = navigation_contact_cell(
                    node_rect,
                    self.anchor_rects[side_index],
                    side_direction,
                )
                node_index = side_index

        pending = deque([(current_index, active_rect)])
        visited = {current_index}
        while pending:
            node_index, node_rect = pending.popleft()
            for requested_contact in self._contacts.get(
                (node_index, requested_direction), ()
            ):
                if requested_contact.target_index != candidate_index:
                    continue
                if _navigation_lane_gap(
                    node_rect,
                    candidate,
                    requested_direction,
                ) <= _navigation_lane_tolerance(
                    node_rect,
                    candidate,
                    requested_direction,
                    self.scale_unit,
                ):
                    return node_index != current_index

            node_distance = (
                abs(candidate.center_x - node_rect.center_x)
                if side_direction in {Direction.LEFT, Direction.RIGHT}
                else abs(candidate.center_y - node_rect.center_y)
            )
            for contact in self._contacts.get((node_index, side_direction), ()):
                side_index = contact.target_index
                if side_index in visited:
                    continue
                side_target = self.anchor_rects[side_index]
                if _navigation_lane_gap(
                    node_rect,
                    side_target,
                    side_direction,
                ) > _navigation_lane_tolerance(
                    node_rect,
                    side_target,
                    side_direction,
                    self.scale_unit,
                ):
                    continue
                side_distance = (
                    abs(candidate.center_x - side_target.center_x)
                    if side_direction in {Direction.LEFT, Direction.RIGHT}
                    else abs(candidate.center_y - side_target.center_y)
                )
                if side_distance >= node_distance:
                    continue
                visited.add(side_index)
                pending.append((side_index, side_target))
        return False

    def _crosses_parallel_section_boundary(
        self,
        current_index: int,
        target_index: int,
        direction: Direction,
    ) -> bool:
        return navigation_crosses_parallel_section_boundary(
            self.targets[current_index],
            self.targets[target_index],
            direction,
        )

    def natural_candidates(
        self,
        current_index: int,
        direction: Direction,
        current_rect: Optional[Rect] = None,
    ) -> tuple[int, ...]:
        if not 0 <= current_index < len(self.targets):
            return ()
        active_rect = (
            self.anchor_rects[current_index]
            if current_rect is None
            else current_rect
        )
        use_cache = active_rect == self.anchor_rects[current_index]
        key = (current_index, direction)
        if use_cache:
            natural = self._natural.get(key)
            if natural is not None:
                return natural
        natural = self._unpromoted_candidates(
            current_index,
            direction,
            active_rect,
        )
        if not use_cache:
            return self._promote_skeleton_candidate(
                current_index,
                direction,
                active_rect,
                natural,
            )
        # Territory adjacency keeps gaps and sparse layouts traversable,
        # but it must not hide a real control hit by the requested beam.
        natural = self._promote_skeleton_candidate(
            current_index,
            direction,
            active_rect,
            natural,
        )
        self._natural[key] = natural
        return natural

    def candidates(
        self,
        current_index: int,
        direction: Direction,
        current_rect: Optional[Rect] = None,
    ) -> tuple[int, ...]:
        return self.natural_candidates(current_index, direction, current_rect)

    def xy_focus_candidates(
        self, current_index: int, direction: Direction
    ) -> tuple[int, ...]:
        """Return a same-section-first XY fallback without changing local routes."""

        if not 0 <= current_index < len(self.targets):
            return ()
        key = (current_index, direction)
        cached = self._xy_fallback.get(key)
        if cached is not None:
            return cached
        section_path = self.targets[current_index].section_path
        same_section = {
            index
            for index, target in enumerate(self.targets)
            if target.section_path == section_path
        }
        contained = xy_focus_target_index(
            self.targets,
            self.anchor_rects,
            current_index,
            direction,
            same_section,
        )
        global_target = xy_focus_target_index(
            self.targets,
            self.anchor_rects,
            current_index,
            direction,
        )
        fallback = tuple(
            dict.fromkeys(
                index
                for index in (contained, global_target)
                if index is not None
            )
        )
        self._xy_fallback[key] = fallback
        return fallback


@dataclass(frozen=True)
class NavigationCandidatePlan:
    natural: tuple[int, ...]
    ranked: tuple[int, ...]
    orthogonal_step_required: bool
    uses_xy_fallback: bool


def navigation_candidate_plan(
    graph: NavigationGraph,
    current_index: int,
    direction: Direction,
    current_rect: Optional[Rect] = None,
) -> NavigationCandidatePlan:
    natural = graph.candidates(current_index, direction, current_rect)
    active_rect = (
        graph.anchor_rects[current_index]
        if current_rect is None
        else current_rect
    )
    orthogonal_step_required = bool(
        natural
        and graph.requires_orthogonal_grid_step(
            current_index,
            direction,
            active_rect,
            natural[0],
        )
    )
    blocked_natural_index = (
        natural[0] if natural and orthogonal_step_required else None
    )
    fallback: tuple[int, ...] = ()
    if not natural:
        fallback = graph.xy_focus_candidates(current_index, direction)
    candidate_index = natural[0] if natural else (fallback[0] if fallback else None)
    skeleton_step_required = bool(
        candidate_index is not None
        and not orthogonal_step_required
        and graph._skeleton_requires_orthogonal_step(
            current_index,
            direction,
            active_rect,
            candidate_index,
        )
    )
    if skeleton_step_required:
        ranked_source = natural if natural else fallback
        lane_candidates = tuple(
            dict.fromkeys(
                (
                    *ranked_source,
                    *graph._skeleton_candidates(
                        current_index,
                        direction,
                        active_rect,
                    ),
                )
            )
        )
        same_lane = next(
            (
                index
                for index in lane_candidates
                if index != candidate_index
                if graph._skeleton_accepts(active_rect, index, direction)
            ),
            None,
        )
        if same_lane is not None:
            promoted = tuple(dict.fromkeys((same_lane, *ranked_source)))
            if natural:
                natural = promoted
            else:
                fallback = promoted
            skeleton_step_required = False
    orthogonal_step_required = bool(
        orthogonal_step_required or skeleton_step_required
    )
    if natural and not orthogonal_step_required:
        return NavigationCandidatePlan(
            natural=natural,
            ranked=natural,
            orthogonal_step_required=False,
            uses_xy_fallback=False,
        )
    if skeleton_step_required:
        return NavigationCandidatePlan(
            natural=natural,
            ranked=(),
            orthogonal_step_required=True,
            uses_xy_fallback=False,
        )
    if not fallback:
        fallback = graph.xy_focus_candidates(current_index, direction)
    if blocked_natural_index is not None:
        fallback = tuple(
            index for index in fallback if index != blocked_natural_index
        )
    return NavigationCandidatePlan(
        natural=natural,
        ranked=fallback,
        orthogonal_step_required=orthogonal_step_required,
        uses_xy_fallback=bool(fallback),
    )


@dataclass
class NavigationTraversal:
    direction: Optional[Direction] = None
    visited: set[int] = field(default_factory=set)
    last_from: Optional[int] = None
    last_to: Optional[int] = None
    last_direction: Optional[Direction] = None
    pending_from: Optional[int] = None
    pending_direction: Optional[Direction] = None
    active_index: Optional[int] = None
    active_rect: Optional[Rect] = None

    def reset(self) -> None:
        self.direction = None
        self.visited.clear()
        self.last_from = None
        self.last_to = None
        self.last_direction = None
        self.pending_from = None
        self.pending_direction = None
        self.active_index = None
        self.active_rect = None

    def current_cell(
        self,
        current_index: int,
        default_rect: Rect,
        direction: Optional[Direction] = None,
    ) -> Rect:
        if self.active_index != current_index or self.active_rect is None:
            self.active_index = current_index
            self.active_rect = default_rect
        if (
            direction is not None
            and self.last_direction is not None
            and direction != self.last_direction
        ):
            # A failed turn is only a probe; keep the last committed lane.
            return default_rect
        return self.active_rect

    def available(
        self,
        current_index: int,
        direction: Direction,
        candidates: Sequence[int],
        *,
        allow_previous_fallback: bool = True,
    ) -> tuple[int, ...]:
        if direction != self.direction:
            self.direction = direction
            self.visited = {current_index}
        else:
            self.visited.add(current_index)
        ranked = tuple(candidates)
        if (
            not ranked
            and allow_previous_fallback
            and self.last_direction is not None
            and direction == OPPOSITE_DIRECTION[self.last_direction]
            and current_index == self.last_to
            and self.last_from is not None
        ):
            ranked = (self.last_from,)
        self.pending_from = current_index
        self.pending_direction = direction
        return tuple(index for index in ranked if index not in self.visited)

    def commit(
        self, selected_index: int, active_rect: Optional[Rect] = None
    ) -> None:
        self.last_from = self.pending_from
        self.last_to = selected_index
        self.last_direction = self.pending_direction
        self.visited.add(selected_index)
        self.active_index = selected_index
        self.active_rect = active_rect


__all__ = (
    'PRIMARY_ACTION_CONTROL_TYPES',
    'HORIZONTAL_LANE_MIN_CENTER_TOLERANCE',
    'HORIZONTAL_LANE_MAX_CENTER_TOLERANCE',
    'HORIZONTAL_LANE_SIZE_MULTIPLIER',
    'VERTICAL_LANE_MIN_CENTER_TOLERANCE',
    'VERTICAL_LANE_MAX_CENTER_TOLERANCE',
    'VERTICAL_LANE_SIZE_MULTIPLIER',
    'GRID_SAFE_CELL_MAX_CHILDREN',
    'RECTANGULAR_GRID_MIN_SHARED_EDGE_UNITS',
    'RECTANGULAR_GRID_SHARED_EDGE_RATIO',
    'RECTANGULAR_GRID_BALANCE_WEIGHT',
    'RECTANGULAR_GRID_DISTANT_GAP_MIN',
    'RECTANGULAR_GRID_DISTANT_GAP_RATIO',
    'RECTANGULAR_GRID_DISTANT_EXTENT_RATIO',
    'SKELETON_LANE_MIN_UNITS',
    'SKELETON_LANE_MAX_UNITS',
    'SKELETON_LANE_SPAN_MULTIPLIER',
    'SKELETON_PROMOTION_MIN_UNITS',
    'SKELETON_ROUNDING_REFERENCE_UNIT',
    'SKELETON_SUPPORT_MAX_GAP_UNITS',
    'Direction',
    'Rect',
    'TargetSnapshot',
    '_axis_gap',
    '_axis_overlap',
    'target_has_interaction_evidence',
    'target_is_finer_descendant',
    'target_is_action_descendant',
    'finer_descendant_index_map',
    'target_has_finer_descendant',
    '_clip_rect',
    '_merged_intervals',
    '_interval_gaps',
    'target_grid_rect',
    'navigation_grid_rects',
    '_union_rect',
    '_rectangular_grid_prefers_rows',
    '_rectangular_partition',
    '_range_occupancy_stable_key',
    '_distant_cluster_split',
    '_expand_partition_to_bounds',
    '_partition_distant_clusters',
    'range_occupancy_grid_rects',
    'territory_contains_anchor_center',
    'NavigationContact',
    '_minimum_navigation_contact',
    'range_occupancy_navigation_contacts',
    '_navigation_contact_rank',
    '_navigation_lane_gap',
    '_navigation_lane_tolerance',
    '_navigation_perpendicular_center_offset',
    '_navigation_forward_center_distance',
    '_skeleton_lane_tolerance',
    '_skeleton_rounding_epsilon',
    '_skeleton_lane_matches',
    '_skeleton_support_lane_matches',
    '_navigation_forward_gap',
    '_skeleton_support_is_local',
    '_navigation_forward_far_edge_distance',
    'navigation_scale_unit',
    'navigation_crosses_parallel_section_boundary',
    '_orthogonal_direction_toward',
    'direction_score',
    'next_target_index',
    '_common_path_prefix_length',
    '_direction_rank_key',
    '_range_frontier_rank_key',
    'ranked_target_indices',
    'best_grid_target_index',
    'OPPOSITE_DIRECTION',
    '_xy_focus_is_candidate',
    '_xy_focus_beams_overlap',
    '_xy_focus_is_strictly_in_direction',
    '_xy_focus_major_axis_distance',
    '_xy_focus_major_axis_far_edge_distance',
    '_xy_focus_minor_axis_distance',
    '_xy_focus_beam_beats',
    '_xy_focus_weighted_distance',
    '_xy_focus_better',
    'xy_focus_target_index',
    'navigation_contact_cell',
    'NavigationGraph',
    'NavigationCandidatePlan',
    'navigation_candidate_plan',
    'NavigationTraversal',
)
