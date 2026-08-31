"""Platform-neutral support for the element navigation entry and host.

This module contains diagnostics, overlay association policy, input maps,
and other glue that does not call UI Automation, Qt, or Win32 directly.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence

from spatial_navigation_core import (
    Direction,
    Rect,
    TargetSnapshot,
    _axis_gap,
    _common_path_prefix_length,
    direction_score,
    finer_descendant_index_map,
    navigation_grid_rects,
    ranked_target_indices,
)
from element_targeting_core import SyntheticTargetSpec, _rect_intersection_area


CHROMIUM_RENDERER_CLASS = "Chrome_RenderWidgetHostHWND"

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

DIRECTION_NAVIGATION_KEYS = frozenset({VK_UP, VK_DOWN, VK_LEFT, VK_RIGHT})


class DirectionInputOwnership:
    """Let a downstream device hook claim raw direction-key edges."""

    def __init__(self) -> None:
        self._forwarded_down: set[int] = set()
        self._downstream_owned: set[int] = set()

    def has_forwarded_down(self, vk: int) -> bool:
        return vk in self._forwarded_down

    def route(
        self,
        vk: int,
        *,
        is_down: bool,
        is_up: bool,
        injected: bool,
        call_next: Callable[[], int],
    ) -> tuple[bool, int]:
        """Return ``(downstream_owned, downstream_result)``.

        Raw direction downs are offered to the rest of the hook chain first.
        A non-zero result means the selected RC003 suppressor claimed the
        physical edge. Its repeats and matching release remain downstream-owned.
        Injected mapping events bypass this path and stay available to navigation.
        """

        if injected or vk not in DIRECTION_NAVIGATION_KEYS:
            return False, 0

        was_forwarded = self.has_forwarded_down(vk)
        was_owned = vk in self._downstream_owned
        if not is_down and not (is_up and was_forwarded):
            return False, 0

        downstream_result = int(call_next())
        downstream_owned = was_owned or downstream_result != 0
        if is_down:
            self._forwarded_down.add(vk)
            if downstream_result != 0:
                self._downstream_owned.add(vk)
        if is_up:
            self._forwarded_down.discard(vk)
            self._downstream_owned.discard(vk)

        return downstream_owned, downstream_result

GLOBAL_HOTKEY_ACTIONS = {
    VK_D: "toggle_diagnostics",
    VK_N: "toggle",
    VK_Q: "quit",
}

OVERLAY_MAX_ROOT_AREA_RATIO = 0.35

OVERLAY_MIN_INTERSECTION_RATIO = 0.65

OVERLAY_ROOT_TARGET_MAX_SIZE = 200

OVERLAY_UNASSOCIATED_MAX_GAP = 48

QUICKER_FLOAT_WINDOW_TITLES = frozenset(
    {"FloatButtonWindow", "FloatPanelWindow", "TextFloatPanelWindow"}
)

QUICKER_STATE_FILE_ENV = "ELEMENT_NAVIGATION_QUICKER_STATE_FILE"

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

@dataclass(frozen=True)
class QuickerOverlayAssociation:
    hwnd: int
    bind_process_name: str


_QUICKER_ASSOCIATION_CACHE_LOCK = threading.Lock()
_QUICKER_ASSOCIATION_CACHE: dict[
    str,
    tuple[tuple[int, int, int], dict[int, QuickerOverlayAssociation]],
] = {}


def normalized_process_name(value: str) -> str:
    name = os.path.basename(str(value or "").strip()).casefold()
    return name[:-4] if name.endswith(".exe") else name

def default_quicker_state_file() -> str:
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if not local_app_data:
        return ""
    return os.path.join(
        local_app_data,
        "ElementNavigation",
        "quicker-navigation.json",
    )

def load_quicker_overlay_associations(
    path: str,
) -> dict[int, QuickerOverlayAssociation]:
    """Read and cache an optional Quicker-side association snapshot."""

    if not path:
        return {}
    normalized_path = os.path.abspath(path)
    try:
        stat = os.stat(normalized_path)
        signature = (int(stat.st_mtime_ns), int(stat.st_ctime_ns), int(stat.st_size))
    except OSError:
        return {}
    with _QUICKER_ASSOCIATION_CACHE_LOCK:
        cached = _QUICKER_ASSOCIATION_CACHE.get(normalized_path)
        if cached is not None and cached[0] == signature:
            return dict(cached[1])
    try:
        with open(normalized_path, "r", encoding="utf-8") as stream:
            payload = json.load(stream)
    except (OSError, ValueError, TypeError):
        with _QUICKER_ASSOCIATION_CACHE_LOCK:
            _QUICKER_ASSOCIATION_CACHE[normalized_path] = (signature, {})
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
    with _QUICKER_ASSOCIATION_CACHE_LOCK:
        _QUICKER_ASSOCIATION_CACHE[normalized_path] = (
            signature,
            dict(associations),
        )
    return dict(associations)

def quicker_overlay_matches_process(
    association: Optional[QuickerOverlayAssociation],
    process_name: str,
) -> bool:
    return bool(
        association is not None
        and association.bind_process_name == normalized_process_name(process_name)
    )

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
    "selected_xy_fallback": "已移动（XY 兜底）",
    "no_candidate": "没有可用候选，保持原位",
    "orthogonal_step": "目标偏离当前通道，请先横向或纵向对齐",
    "geometry_changed": "候选位置变化，已重新扫描",
}

def build_navigation_diagnostic(
    targets: Sequence[TargetSnapshot],
    current_index: int,
    direction: Direction,
    *,
    current_rect: Optional[Rect] = None,
    grid_rects: Optional[Sequence[Rect]] = None,
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
    if grid_rects is None:
        grid_rects = navigation_grid_rects(targets, descendants_by_target)
    else:
        grid_rects = tuple(grid_rects)
    active_rect = (
        grid_rects[current_index] if current_rect is None else current_rect
    )
    ranked = tuple(
        ranked_target_indices(
            targets,
            current_index,
            direction,
            descendants_by_target,
            grid_rects,
            active_rect,
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
            active_rect,
        )
    )
    scored_by_index = {
        index: direction_score(active_rect, grid_rects[index], direction)
        for index in ranked
        if 0 <= index < len(targets) and index != current_index
    }
    candidates: list[CandidateDiagnostic] = []
    for rank, index in enumerate(ranked, 1):
        if not 0 <= index < len(targets) or index == current_index:
            continue
        target = targets[index]
        score = direction_score(active_rect, grid_rects[index], direction)
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
        if target_rect == active_rect:
            reason = "same_rectangle"
        elif direction_score(active_rect, target_rect, direction) is None:
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

def native_handle_value(handle: Any) -> int:
    return int(handle or 0)

def keyboard_navigation_action(vk: int) -> Optional[str]:
    return NAVIGATION_KEY_ACTIONS.get(vk)

def global_hotkey_action(
    vk: int,
    *,
    include_developer_actions: bool = True,
) -> Optional[str]:
    action = GLOBAL_HOTKEY_ACTIONS.get(vk)
    if action in {"toggle_diagnostics", "quit"} and not include_developer_actions:
        return None
    return action


def scan_event_is_current(
    event_token: int,
    current_token: int,
    scanning: bool,
) -> bool:
    return bool(scanning and event_token > 0 and event_token == current_token)


def periodic_check_due(
    now: float,
    last_checked_at: float,
    interval_seconds: float,
) -> bool:
    return bool(last_checked_at <= 0.0 or now - last_checked_at >= interval_seconds)

def should_pass_through_native_menu(vk: int, menu_mode_active: bool) -> bool:
    return menu_mode_active and vk in NATIVE_MENU_NAVIGATION_KEYS

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
    intersection = _rect_intersection_area(root_rect, candidate_rect)
    if intersection >= candidate_area * OVERLAY_MIN_INTERSECTION_RATIO:
        return True
    if not (
        trusted_small_overlay
        and candidate_rect.width <= OVERLAY_ROOT_TARGET_MAX_SIZE
        and candidate_rect.height <= OVERLAY_ROOT_TARGET_MAX_SIZE
    ):
        return False
    horizontal_gap = _axis_gap(
        root_rect.left,
        root_rect.right,
        candidate_rect.left,
        candidate_rect.right,
    )
    vertical_gap = _axis_gap(
        root_rect.top,
        root_rect.bottom,
        candidate_rect.top,
        candidate_rect.bottom,
    )
    return (
        horizontal_gap <= OVERLAY_UNASSOCIATED_MAX_GAP
        and vertical_gap <= OVERLAY_UNASSOCIATED_MAX_GAP
    )

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

def navigation_overlay_label(
    target: TargetSnapshot,
    hierarchy_index: int = -1,
    hierarchy_count: int = 0,
) -> str:
    label = target.name or target.control_type
    if hierarchy_count > 1 and hierarchy_index >= 0:
        label += f"  层级 {hierarchy_index + 1}/{hierarchy_count}"
    if target.source == "msaa":
        label += "  MSAA"
    return label


__all__ = (
    'CHROMIUM_RENDERER_CLASS',
    'VK_RETURN',
    'VK_ESCAPE',
    'VK_PAGEUP',
    'VK_PAGEDOWN',
    'VK_LEFT',
    'VK_UP',
    'VK_RIGHT',
    'VK_DOWN',
    'VK_D',
    'VK_N',
    'VK_Q',
    'VK_APPS',
    'VK_VOLUME_DOWN',
    'VK_VOLUME_UP',
    'NAVIGATION_KEY_ACTIONS',
    'NATIVE_MENU_NAVIGATION_KEYS',
    'DIRECTION_NAVIGATION_KEYS',
    'DirectionInputOwnership',
    'GLOBAL_HOTKEY_ACTIONS',
    'OVERLAY_MAX_ROOT_AREA_RATIO',
    'OVERLAY_MIN_INTERSECTION_RATIO',
    'OVERLAY_ROOT_TARGET_MAX_SIZE',
    'OVERLAY_UNASSOCIATED_MAX_GAP',
    'QUICKER_FLOAT_WINDOW_TITLES',
    'QUICKER_STATE_FILE_ENV',
    'WS_EX_TOPMOST',
    'WS_EX_TOOLWINDOW',
    'WS_EX_NOACTIVATE',
    'NON_NAVIGATION_OVERLAY_CLASSES',
    'QuickerOverlayAssociation',
    'normalized_process_name',
    'default_quicker_state_file',
    'load_quicker_overlay_associations',
    'quicker_overlay_matches_process',
    'CandidateDiagnostic',
    'NavigationDiagnostic',
    'physical_screen_rect',
    'physical_to_screen_logical_rect',
    'DIAGNOSTIC_REJECTION_ORDER',
    'DIAGNOSTIC_REJECTION_LABELS',
    'DIAGNOSTIC_ROUTE_LABELS',
    'DIAGNOSTIC_DIRECTION_LABELS',
    'DIAGNOSTIC_OUTCOME_LABELS',
    'build_navigation_diagnostic',
    '_diagnostic_target_label',
    'format_navigation_diagnostic',
    'native_handle_value',
    'keyboard_navigation_action',
    'global_hotkey_action',
    'scan_event_is_current',
    'periodic_check_due',
    'should_pass_through_native_menu',
    'mouse_wheel_data',
    'owner_chain_contains',
    'overlay_window_is_related',
    'overlay_window_is_candidate',
    'root_only_overlay_target_spec',
    'navigation_foreground_action',
    'target_pointer_point',
    'navigation_overlay_label',
)
