import importlib.util
import random
import sys
import unittest
from pathlib import Path


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "element_navigation_prototype.py"
)
SPEC = importlib.util.spec_from_file_location("element_navigation_prototype", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
prototype = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = prototype
SPEC.loader.exec_module(prototype)


class SpatialNavigationTests(unittest.TestCase):
    def target(self, left, top, right, bottom, name="", **kwargs):
        return prototype.TargetSnapshot(
            prototype.Rect(left, top, right, bottom),
            name,
            "ButtonControl",
            **kwargs,
        )

    def test_ignores_elements_in_the_opposite_direction(self):
        current = prototype.Rect(100, 100, 160, 140)
        left = prototype.Rect(20, 100, 80, 140)
        self.assertIsNone(
            prototype.direction_score(current, left, prototype.Direction.RIGHT)
        )

    def test_maps_physical_uia_rect_to_scaled_qt_screen(self):
        logical_screen = prototype.Rect(0, 0, 2560, 1440)
        physical_screen = prototype.physical_screen_rect(logical_screen, 1.5)
        self.assertEqual(physical_screen, prototype.Rect(0, 0, 3840, 2160))
        self.assertEqual(
            prototype.physical_to_screen_logical_rect(
                prototype.Rect(1500, 450, 1800, 600), physical_screen, 1.5
            ),
            prototype.Rect(1000, 300, 1200, 400),
        )

    def test_keeps_physical_origin_for_unscaled_secondary_screen(self):
        logical_screen = prototype.Rect(3840, 0, 5760, 1200)
        physical_screen = prototype.physical_screen_rect(logical_screen, 1.0)
        self.assertEqual(physical_screen, logical_screen)
        self.assertEqual(
            prototype.physical_to_screen_logical_rect(
                prototype.Rect(4000, 100, 4200, 200), physical_screen, 1.0
            ),
            prototype.Rect(160, 100, 360, 200),
        )

    def test_prefers_same_row_over_closer_diagonal_target(self):
        targets = [
            self.target(100, 100, 160, 140, "current"),
            self.target(175, 165, 235, 205, "diagonal"),
            self.target(210, 100, 270, 140, "same row"),
        ]
        self.assertEqual(
            prototype.next_target_index(targets, 0, prototype.Direction.RIGHT),
            2,
        )
        self.assertEqual(
            prototype.ranked_target_indices(
                targets, 0, prototype.Direction.RIGHT
            )[:2],
            [2, 1],
        )

    def test_right_wraps_from_row_end_to_next_row_start(self):
        targets = [
            self.target(900, 100, 960, 140, "p1", path=(0, 2, 0)),
            self.target(500, 180, 560, 220, "p2", path=(0, 2, 1)),
            self.target(700, 180, 760, 220, "next row second", path=(0, 2, 2)),
        ]
        self.assertEqual(
            prototype.next_target_index(targets, 0, prototype.Direction.RIGHT),
            1,
        )

    def test_left_wraps_from_row_start_to_previous_row_end(self):
        targets = [
            self.target(900, 100, 960, 140, "p1", path=(0, 2, 0)),
            self.target(500, 180, 560, 220, "p2", path=(0, 2, 1)),
        ]
        self.assertEqual(
            prototype.next_target_index(targets, 1, prototype.Direction.LEFT),
            0,
        )

    def test_horizontal_wrap_beats_closer_diagonal_sidebar_target(self):
        targets = [
            self.target(900, 100, 960, 140, "p1", path=(0, 2, 0)),
            self.target(500, 180, 560, 220, "p2", path=(0, 2, 1)),
            self.target(980, 155, 1040, 195, "sidebar", path=(0, 1, 0)),
        ]
        self.assertEqual(
            prototype.ranked_target_indices(
                targets, 0, prototype.Direction.RIGHT
            )[:2],
            [1, 2],
        )

    def test_forward_same_branch_target_beats_wrap_to_sidebar(self):
        targets = [
            self.target(
                760,
                350,
                800,
                390,
                "current message action",
                path=(0, 2, 4, 8),
            ),
            self.target(
                1640,
                405,
                1730,
                460,
                "continue",
                path=(0, 2, 5, 0),
            ),
            self.target(
                40,
                425,
                390,
                470,
                "sidebar conversation",
                path=(0, 1, 7),
            ),
        ]
        self.assertEqual(
            prototype.ranked_target_indices(
                targets, 0, prototype.Direction.RIGHT
            )[:2],
            [1, 2],
        )

    def test_visual_distance_beats_deeper_uia_branch(self):
        targets = [
            self.target(500, 100, 600, 150, "current", path=(0, 2, 0)),
            self.target(900, 100, 1000, 150, "same branch", path=(0, 2, 5)),
            self.target(620, 100, 720, 150, "near branch", path=(0, 1, 0)),
        ]
        self.assertEqual(
            prototype.next_target_index(targets, 0, prototype.Direction.RIGHT),
            2,
        )

    def test_horizontal_wrap_does_not_cross_a_distant_blank_region(self):
        targets = [
            self.target(900, 100, 960, 140, "current", path=(0, 2, 0)),
            self.target(500, 900, 560, 940, "far next row", path=(0, 2, 1)),
            self.target(980, 210, 1040, 250, "near diagonal", path=(0, 1, 0)),
        ]
        self.assertEqual(
            prototype.next_target_index(targets, 0, prototype.Direction.RIGHT),
            2,
        )

    def test_horizontal_wrap_rejects_overlapping_rows(self):
        targets = [
            self.target(300, 100, 500, 200, "upper right", path=(0, 1, 0)),
            self.target(200, 180, 400, 240, "lower left", path=(0, 1, 1)),
        ]
        self.assertEqual(
            prototype.next_target_index(targets, 0, prototype.Direction.RIGHT),
            0,
        )

    def test_repeated_direction_never_cycles_in_irregular_layouts(self):
        generator = random.Random(827)
        for _case in range(40):
            targets = []
            for index in range(generator.randint(2, 35)):
                left = generator.randint(0, 1600)
                top = generator.randint(0, 900)
                width = generator.randint(24, 260)
                height = generator.randint(20, 100)
                targets.append(
                    self.target(
                        left,
                        top,
                        left + width,
                        top + height,
                        str(index),
                        path=(0, generator.randint(0, 4), index),
                    )
                )
            graph = prototype.NavigationGraph(targets)
            for start in (0, len(targets) // 2, len(targets) - 1):
                for direction in prototype.Direction:
                    traversal = prototype.NavigationTraversal()
                    seen = {start}
                    current = start
                    for _step in range(len(targets) + 1):
                        candidates = traversal.available(
                            current,
                            direction,
                            graph.candidates(current, direction),
                        )
                        if not candidates:
                            break
                        current = candidates[0]
                        self.assertNotIn(current, seen)
                        seen.add(current)
                        traversal.commit(current)

    def test_changing_direction_allows_returning_to_previous_target(self):
        targets = [
            self.target(20, 20, 80, 60, "left"),
            self.target(120, 20, 180, 60, "right"),
        ]
        graph = prototype.NavigationGraph(targets)
        traversal = prototype.NavigationTraversal()
        right = traversal.available(
            0,
            prototype.Direction.RIGHT,
            graph.candidates(0, prototype.Direction.RIGHT),
        )[0]
        traversal.commit(right)
        left = traversal.available(
            right,
            prototype.Direction.LEFT,
            graph.candidates(right, prototype.Direction.LEFT),
        )[0]
        self.assertEqual((right, left), (1, 0))

    def test_immediate_opposite_direction_prioritizes_the_previous_target(self):
        traversal = prototype.NavigationTraversal()
        right = traversal.available(
            0, prototype.Direction.RIGHT, (1, 2)
        )[0]
        traversal.commit(right)
        left_candidates = traversal.available(
            right, prototype.Direction.LEFT, (2, 0)
        )
        self.assertEqual(left_candidates, (0, 2))

    def test_repeated_right_does_not_loop_from_folder_back_to_header_actions(self):
        targets = [
            self.target(300, 100, 340, 140, "options", path=(0, 1, 0)),
            self.target(360, 100, 400, 140, "add", path=(0, 1, 1)),
            self.target(20, 150, 280, 195, "folder", path=(0, 1, 2)),
        ]
        first = prototype.next_target_index(
            targets, 0, prototype.Direction.RIGHT
        )
        second = prototype.next_target_index(
            targets, first, prototype.Direction.RIGHT
        )
        third = prototype.next_target_index(
            targets, second, prototype.Direction.RIGHT
        )
        self.assertEqual((first, second, third), (1, 2, 2))

    def test_repeated_left_does_not_loop_from_header_back_to_lower_folder(self):
        targets = [
            self.target(300, 100, 340, 140, "options", path=(0, 1, 0)),
            self.target(360, 100, 400, 140, "add", path=(0, 1, 1)),
            self.target(20, 150, 280, 195, "folder", path=(0, 1, 2)),
        ]
        self.assertEqual(
            prototype.next_target_index(targets, 0, prototype.Direction.LEFT),
            0,
        )

    def test_right_does_not_treat_slightly_indented_sidebar_row_as_wrap(self):
        targets = [
            self.target(40, 100, 340, 150, "sidebar current", path=(0, 1, 0)),
            self.target(35, 165, 335, 215, "sidebar below", path=(0, 1, 1)),
            self.target(500, 260, 1000, 360, "main content", path=(0, 2, 0)),
        ]
        self.assertEqual(
            prototype.next_target_index(targets, 0, prototype.Direction.RIGHT),
            2,
        )

    def test_left_reaches_sidebar_before_wrapping_to_upper_right(self):
        targets = [
            self.target(40, 420, 340, 470, "sidebar", path=(0, 1, 8)),
            self.target(720, 420, 1760, 475, "file row", path=(0, 2, 4)),
            self.target(1670, 350, 1760, 395, "review", path=(0, 2, 3)),
        ]
        self.assertEqual(
            prototype.next_target_index(targets, 1, prototype.Direction.LEFT),
            0,
        )

    def test_prefers_nearest_target_in_a_vertical_column(self):
        targets = [
            self.target(100, 100, 160, 140, "current"),
            self.target(100, 250, 160, 290, "far"),
            self.target(100, 165, 160, 205, "near"),
        ]
        self.assertEqual(
            prototype.next_target_index(targets, 0, prototype.Direction.DOWN),
            2,
        )

    def test_vertical_navigation_stays_in_main_content_lane(self):
        targets = [
            self.target(440, 50, 780, 100, "title"),
            self.target(50, 130, 400, 175, "near sidebar"),
            self.target(530, 300, 800, 340, "far main content"),
        ]
        self.assertEqual(
            prototype.next_target_index(targets, 0, prototype.Direction.DOWN),
            2,
        )
        self.assertEqual(
            prototype.next_target_index(targets, 2, prototype.Direction.UP),
            0,
        )

    def test_vertical_navigation_prefers_same_content_branch_over_sidebar(self):
        targets = [
            self.target(400, 80, 620, 130, "main tab", path=(0, 2, 0)),
            self.target(40, 350, 340, 400, "sidebar", path=(0, 1, 5)),
            self.target(660, 460, 1200, 560, "main content", path=(0, 2, 3)),
        ]
        self.assertEqual(
            prototype.next_target_index(targets, 0, prototype.Direction.DOWN),
            2,
        )

    def test_vertical_navigation_reaches_near_message_action_before_next_block(self):
        targets = [
            self.target(500, 100, 1200, 240, "message"),
            self.target(495, 250, 535, 290, "复制"),
            self.target(500, 330, 1200, 430, "next message"),
        ]
        self.assertEqual(
            prototype.next_target_index(targets, 0, prototype.Direction.DOWN),
            1,
        )

    def test_keeps_current_target_when_no_candidate_exists(self):
        targets = [self.target(100, 100, 160, 140, "only")]
        self.assertEqual(
            prototype.next_target_index(targets, 0, prototype.Direction.LEFT),
            0,
        )

    def test_navigation_diagnostic_explains_ranking_and_rejections(self):
        targets = [
            self.target(100, 100, 160, 140, "current", path=(0, 2, 0)),
            self.target(190, 100, 250, 140, "same lane", path=(0, 2, 1)),
            self.target(180, 180, 240, 220, "diagonal", path=(0, 2, 2)),
            self.target(20, 100, 80, 140, "wrong way", path=(0, 1, 0)),
        ]
        ranked = prototype.ranked_target_indices(
            targets, 0, prototype.Direction.RIGHT
        )
        diagnostic = prototype.build_navigation_diagnostic(
            targets,
            0,
            prototype.Direction.RIGHT,
            ranked_indices=ranked,
            available_indices=ranked,
            selected_index=1,
            outcome="selected",
        )

        self.assertIsNotNone(diagnostic)
        assert diagnostic is not None
        self.assertEqual([item.index for item in diagnostic.candidates], [1, 2])
        self.assertEqual([item.route for item in diagnostic.candidates], ["lane", "diagonal"])
        self.assertEqual(dict(diagnostic.rejected_counts)["wrong_direction"], 1)
        rendered = prototype.format_navigation_diagnostic(diagnostic)
        self.assertIn("最终选中", rendered)
        self.assertIn("同一通道", rendered)
        self.assertIn("不在请求方向 1 个", rendered)

    def test_navigation_diagnostic_marks_horizontal_wrap(self):
        targets = [
            self.target(900, 100, 960, 140, "row end", path=(0, 2, 0)),
            self.target(500, 180, 560, 220, "next row", path=(0, 2, 1)),
        ]
        diagnostic = prototype.build_navigation_diagnostic(
            targets,
            0,
            prototype.Direction.RIGHT,
            available_indices=(1,),
            selected_index=1,
            outcome="selected",
        )

        self.assertIsNotNone(diagnostic)
        assert diagnostic is not None
        self.assertEqual(diagnostic.candidates[0].route, "wrap")
        self.assertIn("跨行补充", prototype.format_navigation_diagnostic(diagnostic))

    def test_navigation_diagnostic_marks_cached_reverse_return(self):
        targets = [
            self.target(100, 100, 300, 300, "parent"),
            self.target(120, 200, 180, 240, "child"),
        ]
        graph = prototype.NavigationGraph(targets)
        self.assertEqual(graph.candidates(0, prototype.Direction.DOWN)[0], 1)
        reverse = graph.candidates(1, prototype.Direction.UP)
        diagnostic = prototype.build_navigation_diagnostic(
            targets,
            1,
            prototype.Direction.UP,
            ranked_indices=reverse,
            available_indices=reverse,
            selected_index=0,
            outcome="selected",
        )

        self.assertIsNotNone(diagnostic)
        assert diagnostic is not None
        self.assertEqual(diagnostic.candidates[0].route, "reverse")
        self.assertIn("反向返回", prototype.format_navigation_diagnostic(diagnostic))

    def test_navigation_diagnostics_are_opt_in(self):
        self.assertFalse(prototype._parse_args([]).diagnostics)
        self.assertTrue(prototype._parse_args(["--diagnostics"]).diagnostics)

    def test_navigation_graph_caches_natural_reverse_edge(self):
        targets = [
            self.target(20, 20, 80, 60, "left"),
            self.target(120, 20, 180, 60, "right"),
        ]
        graph = prototype.NavigationGraph(targets)
        self.assertEqual(
            graph.candidates(0, prototype.Direction.RIGHT)[0], 1
        )
        self.assertEqual(
            graph.candidates(1, prototype.Direction.LEFT)[0], 0
        )

    def test_regular_grid_is_reachable_and_reversible_in_all_directions(self):
        targets = [
            self.target(
                column * 100,
                row * 80,
                column * 100 + 70,
                row * 80 + 50,
                f"{row},{column}",
                path=(0, row, column),
            )
            for row in range(3)
            for column in range(3)
        ]
        graph = prototype.NavigationGraph(targets)
        visited = {4}
        pending = [4]
        while pending:
            current = pending.pop()
            for direction in prototype.Direction:
                candidates = graph.candidates(current, direction)
                if not candidates:
                    continue
                neighbor = candidates[0]
                reverse = graph.candidates(
                    neighbor, prototype.OPPOSITE_DIRECTION[direction]
                )
                self.assertEqual(reverse[0], current)
                if neighbor not in visited:
                    visited.add(neighbor)
                    pending.append(neighbor)
        self.assertEqual(visited, set(range(9)))

    def test_geometry_anchors_cover_selection_and_layout_extremes(self):
        self.assertEqual(prototype.geometry_anchor_indices(8, 3), [3, 0, 4, 7])
        self.assertEqual(prototype.geometry_anchor_indices(1, 0), [0])

    def test_shifts_cached_snapshot_without_losing_identity(self):
        target = self.target(
            100,
            100,
            160,
            140,
            "button",
            runtime_id=(7, 8, 9),
            source="uia-point",
        )
        shifted = prototype.shifted_snapshot(target, 30, -20)
        self.assertEqual(shifted.rect, prototype.Rect(130, 80, 190, 120))
        self.assertEqual(shifted.runtime_id, (7, 8, 9))
        self.assertEqual(shifted.source, "uia-point")

    def test_repeated_names_at_different_positions_are_not_the_same_target(self):
        first = self.target(100, 100, 180, 140, "复制")
        second = self.target(300, 100, 380, 140, "复制")
        self.assertFalse(prototype.same_target_identity(first, second))
        self.assertTrue(prototype.same_target_identity(first, first))

    def test_runtime_id_remains_the_strongest_target_identity(self):
        first = self.target(
            100, 100, 180, 140, "old", runtime_id=(1, 2, 3)
        )
        moved = self.target(
            500, 400, 580, 440, "new", runtime_id=(1, 2, 3)
        )
        self.assertTrue(prototype.same_target_identity(first, moved))

    def test_native_handle_treats_missing_foreground_as_zero(self):
        self.assertEqual(prototype.native_handle_value(None), 0)
        self.assertEqual(prototype.native_handle_value(1234), 1234)

    def test_keyboard_navigation_maps_mouse_like_remote_actions(self):
        expected = {
            prototype.VK_RETURN: "activate",
            prototype.VK_APPS: "context",
            prototype.VK_VOLUME_UP: "scroll_up",
            prototype.VK_VOLUME_DOWN: "scroll_down",
            prototype.VK_ESCAPE: "cancel",
        }
        self.assertEqual(
            {
                vk: prototype.keyboard_navigation_action(vk)
                for vk in expected
            },
            expected,
        )
        self.assertIsNone(prototype.keyboard_navigation_action(0x70))

    def test_global_hotkey_maps_the_diagnostics_toggle(self):
        self.assertEqual(
            prototype.global_hotkey_action(prototype.VK_D),
            "toggle_diagnostics",
        )
        self.assertEqual(prototype.global_hotkey_action(prototype.VK_N), "toggle")
        self.assertEqual(prototype.global_hotkey_action(prototype.VK_Q), "quit")
        self.assertIsNone(prototype.global_hotkey_action(0x70))

    def test_native_menu_temporarily_receives_navigation_keys(self):
        for vk in (
            prototype.VK_UP,
            prototype.VK_DOWN,
            prototype.VK_LEFT,
            prototype.VK_RIGHT,
            prototype.VK_RETURN,
            prototype.VK_ESCAPE,
        ):
            self.assertTrue(prototype.should_pass_through_native_menu(vk, True))
            self.assertFalse(prototype.should_pass_through_native_menu(vk, False))
        self.assertFalse(
            prototype.should_pass_through_native_menu(
                prototype.VK_VOLUME_UP, True
            )
        )

    def test_content_refresh_only_follows_context_or_double_click(self):
        self.assertEqual(prototype.content_refresh_delay_ms("contexted"), 120)
        self.assertEqual(
            prototype.content_refresh_delay_ms("activated", True), 180
        )
        self.assertEqual(
            prototype.content_refresh_delay_ms("activated", False), 0
        )

    def test_negative_wheel_delta_is_encoded_as_a_windows_dword(self):
        self.assertEqual(prototype.mouse_wheel_data(120), 120)
        self.assertEqual(prototype.mouse_wheel_data(-120), 0xFFFFFF88)

    def test_pointer_point_requires_a_verified_uia_hit(self):
        target = self.target(100, 200, 180, 240, "button")
        self.assertEqual(
            prototype.target_pointer_point(target, (112, 220), False),
            (112, 220),
        )
        self.assertIsNone(prototype.target_pointer_point(target, None, False))
        self.assertEqual(
            prototype.target_pointer_point(target, None, True),
            (140, 220),
        )

    def test_owner_chain_follows_a_nested_popup_without_looping(self):
        owners = {30: 20, 20: 10, 10: 0, 40: 40}
        owner_of = lambda hwnd: owners.get(hwnd, 0)
        self.assertTrue(prototype.owner_chain_contains(30, 10, owner_of))
        self.assertFalse(prototype.owner_chain_contains(10, 30, owner_of))
        self.assertFalse(prototype.owner_chain_contains(40, 10, owner_of))

    def test_foreground_context_follows_same_process_popup(self):
        process_ids = {10: 100, 20: 100, 90: 900}
        action = prototype.navigation_foreground_action(
            20,
            10,
            10,
            100,
            900,
            process_ids.get,
            lambda _hwnd: 0,
        )
        self.assertEqual(action, "follow")

    def test_foreground_context_follows_owned_cross_process_dialog(self):
        process_ids = {10: 100, 20: 200, 90: 900}
        owners = {20: 10}
        action = prototype.navigation_foreground_action(
            20,
            10,
            10,
            100,
            900,
            process_ids.get,
            lambda hwnd: owners.get(hwnd, 0),
        )
        self.assertEqual(action, "follow")

    def test_foreground_context_returns_from_owned_dialog_to_root(self):
        process_ids = {10: 100, 20: 200, 90: 900}
        owners = {20: 10}
        action = prototype.navigation_foreground_action(
            10,
            20,
            10,
            100,
            900,
            process_ids.get,
            lambda hwnd: owners.get(hwnd, 0),
        )
        self.assertEqual(action, "follow")

    def test_foreground_context_ignores_overlay_and_leaves_unrelated_app(self):
        process_ids = {10: 100, 30: 300, 90: 900}
        common = (10, 10, 100, 900, process_ids.get, lambda _hwnd: 0)
        self.assertEqual(
            prototype.navigation_foreground_action(90, *common),
            "ignore",
        )
        self.assertEqual(
            prototype.navigation_foreground_action(30, *common),
            "leave",
        )

    def test_prewarm_runs_once_after_the_foreground_is_stable(self):
        self.assertFalse(
            prototype.prewarm_request_due(7, 7, 10.0, 0, 10.5)
        )
        self.assertTrue(
            prototype.prewarm_request_due(7, 7, 10.0, 0, 10.8)
        )
        self.assertFalse(
            prototype.prewarm_request_due(7, 7, 10.0, 7, 30.0)
        )

    def test_scan_budget_can_be_cancelled_or_expire(self):
        self.assertTrue(prototype.scan_should_stop(None, lambda: True, now=1.0))
        self.assertTrue(prototype.scan_should_stop(1.0, None, now=1.0))
        self.assertFalse(prototype.scan_should_stop(2.0, None, now=1.0))

    def test_target_probe_points_cover_sparse_left_content(self):
        rect = prototype.Rect(40, 100, 540, 150)
        points = prototype.target_probe_points(rect)
        self.assertEqual(points[0], (290, 125))
        self.assertIn((64, 125), points)
        self.assertTrue(
            all(rect.contains_point(point) for point in points)
        )

    def test_initial_target_uses_focused_element(self):
        targets = [
            self.target(20, 20, 80, 60, "first"),
            self.target(200, 200, 280, 250, "focused"),
        ]
        self.assertEqual(
            prototype.initial_target_index(
                targets,
                prototype.Rect(210, 210, 260, 240),
                prototype.Rect(0, 0, 600, 400),
            ),
            1,
        )

    def test_initial_target_falls_back_to_reading_order(self):
        targets = [
            self.target(200, 120, 260, 160, "second row"),
            self.target(300, 30, 360, 70, "top right"),
            self.target(50, 30, 110, 70, "top left"),
        ]
        self.assertEqual(
            prototype.initial_target_index(
                targets,
                None,
                prototype.Rect(0, 0, 600, 400),
            ),
            2,
        )

    def test_initial_target_prefers_smallest_element_under_mouse(self):
        targets = [
            self.target(20, 20, 300, 200, "group"),
            self.target(100, 80, 160, 120, "button"),
        ]
        self.assertEqual(
            prototype.initial_target_index(
                targets,
                None,
                prototype.Rect(0, 0, 600, 400),
                (120, 100),
            ),
            1,
        )

    def test_initial_target_uses_nearest_element_to_mouse_in_blank_area(self):
        targets = [
            self.target(20, 20, 80, 60, "left"),
            self.target(300, 200, 360, 240, "right"),
        ]
        self.assertEqual(
            prototype.initial_target_index(
                targets,
                None,
                prototype.Rect(0, 0, 600, 400),
                (280, 180),
            ),
            1,
        )

    def test_point_hit_prefers_exact_runtime_id(self):
        targets = [
            self.target(
                20,
                20,
                120,
                60,
                "same",
                runtime_id=(1, 2, 3),
            ),
            self.target(
                20,
                20,
                120,
                60,
                "same",
                runtime_id=(4, 5, 6),
            ),
        ]
        self.assertEqual(
            prototype.hit_target_match_index(
                targets, prototype.Rect(20, 20, 120, 60), (4, 5, 6)
            ),
            1,
        )

    def test_point_hit_accepts_small_cross_api_rectangle_difference(self):
        targets = [self.target(100, 100, 200, 150, "button")]
        self.assertEqual(
            prototype.hit_target_match_index(
                targets, prototype.Rect(102, 99, 202, 151)
            ),
            0,
        )

    def test_point_hit_rejects_unrelated_overlapping_container(self):
        targets = [self.target(0, 0, 600, 400, "container")]
        self.assertEqual(
            prototype.hit_target_match_index(
                targets, prototype.Rect(100, 100, 140, 140)
            ),
            -1,
        )

    def test_drops_large_structural_wrapper_around_real_button(self):
        targets = [
            prototype.TargetSnapshot(
                prototype.Rect(0, 0, 600, 400), "", "GroupControl"
            ),
            self.target(40, 40, 140, 90, "button"),
        ]
        self.assertEqual(prototype.nested_container_keep_indices(targets), [1])

    def test_keeps_standalone_structural_action(self):
        targets = [
            prototype.TargetSnapshot(
                prototype.Rect(40, 40, 140, 90), "canvas action", "CustomControl"
            )
        ]
        self.assertEqual(prototype.nested_container_keep_indices(targets), [0])

    def test_drops_same_row_hover_action_nested_inside_primary_button(self):
        targets = [
            self.target(
                40,
                40,
                240,
                90,
                "conversation",
                path=(0, 1),
                has_action_pattern=True,
            ),
            self.target(
                200,
                50,
                230,
                80,
                "archive",
                path=(0, 1, 0, 2),
                has_action_pattern=True,
            ),
        ]
        self.assertEqual(prototype.nested_container_keep_indices(targets), [0])

    def test_nested_show_more_remains_the_next_down_target(self):
        folder = self.target(
            20,
            140,
            360,
            245,
            "folder",
            path=(0, 1),
            has_action_pattern=True,
        )
        show_more = self.target(
            45,
            200,
            175,
            238,
            "展开显示",
            path=(0, 1, 0),
            has_action_pattern=True,
        )
        next_folder = self.target(
            20,
            265,
            360,
            310,
            "next folder",
            path=(0, 2),
            has_action_pattern=True,
        )
        targets = [folder, show_more, next_folder]
        kept = prototype.nested_container_keep_indices(targets)
        visible = [targets[index] for index in kept]
        self.assertEqual(kept, [0, 1, 2])
        self.assertEqual(
            prototype.next_target_index(
                visible, 0, prototype.Direction.DOWN
            ),
            1,
        )

    def test_keeps_message_actions_nested_inside_message_button(self):
        targets = [
            self.target(
                40,
                40,
                640,
                180,
                "message",
                path=(0, 1),
                has_action_pattern=True,
            ),
            self.target(
                50,
                130,
                90,
                170,
                "复制消息",
                path=(0, 1, 0, 2),
                has_action_pattern=True,
            ),
            self.target(
                95,
                130,
                135,
                170,
                "从这里创建聊天分支",
                path=(0, 1, 0, 3),
                has_action_pattern=True,
            ),
        ]
        self.assertEqual(
            prototype.nested_container_keep_indices(targets), [0, 1, 2]
        )

    def test_drops_project_list_wrapper_but_keeps_folder_and_rows(self):
        wrapper = prototype.TargetSnapshot(
            prototype.Rect(20, 20, 260, 300),
            "project contents",
            "ListItemControl",
            path=(0,),
            has_action_pattern=True,
        )
        folder = self.target(
            20,
            20,
            260,
            60,
            "project",
            path=(0, 0),
            supports_expand=True,
            has_action_pattern=True,
        )
        row = self.target(
            20, 70, 260, 110, "conversation", path=(0, 1, 0), has_action_pattern=True
        )
        self.assertEqual(
            prototype.nested_container_keep_indices([wrapper, folder, row]), [1, 2]
        )

    def test_filters_chat_message_jump_helpers(self):
        self.assertTrue(prototype.is_navigation_noise("跳转到用户消息 12"))
        self.assertTrue(prototype.is_navigation_noise("Jump to user message 12"))
        self.assertFalse(prototype.is_navigation_noise("发送"))

    def test_rejects_unnamed_group_even_with_automation_id(self):
        self.assertFalse(
            prototype.structural_action_has_identity(
                "GroupControl", "", "radix-_r_2rhf_"
            )
        )

    def test_trusts_named_semantic_button_when_hover_hit_is_transparent(self):
        target = self.target(
            100,
            100,
            140,
            140,
            "复制消息",
            has_action_pattern=True,
        )
        self.assertTrue(prototype.semantic_action_can_bypass_point_hit(target))
        self.assertFalse(
            prototype.semantic_action_can_bypass_point_hit(
                prototype.TargetSnapshot(
                    prototype.Rect(100, 100, 240, 140),
                    "",
                    "GroupControl",
                    automation_id="radix-_r_2rhf_",
                    has_action_pattern=True,
                )
            )
        )
        self.assertTrue(
            prototype.structural_action_has_identity(
                "CustomControl", "", "canvas-action"
            )
        )

    def test_large_semantic_row_requires_a_real_point_hit(self):
        ghost_row = self.target(
            100,
            100,
            430,
            145,
            "hidden embedded row",
            has_action_pattern=True,
        )
        compact_text_action = self.target(
            100,
            160,
            220,
            200,
            "展开显示",
            has_action_pattern=True,
        )
        self.assertFalse(
            prototype.semantic_action_can_bypass_point_hit(ghost_row)
        )
        self.assertTrue(
            prototype.semantic_action_can_bypass_point_hit(compact_text_action)
        )

    def test_same_rectangle_prefers_deeper_real_action(self):
        wrapper = prototype.TargetSnapshot(
            prototype.Rect(40, 40, 240, 90),
            "conversation",
            "ListItemControl",
            path=(0, 1),
            depth=2,
        )
        button = self.target(
            40,
            40,
            240,
            90,
            "conversation",
            path=(0, 1, 0, 0),
            depth=4,
            keyboard_focusable=True,
            has_action_pattern=True,
        )
        self.assertGreater(
            prototype.target_quality_rank(button),
            prototype.target_quality_rank(wrapper),
        )

    def test_expanded_folder_and_children_share_flat_navigation(self):
        folder = self.target(
            20,
            20,
            220,
            60,
            "folder",
            path=(0, 0, 0),
            supports_expand=True,
            has_action_pattern=True,
        )
        first = self.target(
            20,
            70,
            220,
            110,
            "first",
            path=(0, 0, 1, 0),
            has_action_pattern=True,
        )
        second = self.target(
            20,
            115,
            220,
            155,
            "second",
            path=(0, 0, 1, 1),
            has_action_pattern=True,
        )
        outside = self.target(
            300, 20, 380, 60, "outside", path=(1,), has_action_pattern=True
        )
        targets = [folder, first, second, outside]
        self.assertEqual(
            prototype.flat_target_indices(targets), [0, 1, 2, 3]
        )
        self.assertEqual(
            prototype.next_target_index(targets, 0, prototype.Direction.DOWN),
            1,
        )

    def test_section_header_does_not_hide_the_whole_project_list(self):
        section = self.target(
            20,
            20,
            220,
            60,
            "projects",
            path=(0, 0),
            supports_expand=True,
            has_action_pattern=True,
        )
        folder = self.target(
            20, 70, 220, 110, "folder", path=(0, 1, 0), has_action_pattern=True
        )
        self.assertEqual(
            prototype.flat_target_indices([section, folder]), [0, 1]
        )

    def test_restore_target_uses_name_and_type_after_layout_moves(self):
        previous = self.target(20, 20, 120, 60, "folder")
        targets = [
            self.target(20, 200, 120, 240, "other"),
            self.target(20, 80, 120, 120, "folder"),
        ]
        self.assertEqual(prototype.restore_target_index(targets, previous), 1)

    def test_restore_target_uses_runtime_id_before_repeated_name(self):
        previous = self.target(
            20, 20, 120, 60, "copy", runtime_id=(7, 8, 9)
        )
        targets = [
            self.target(20, 80, 120, 120, "copy", runtime_id=(1, 2, 3)),
            self.target(200, 80, 300, 120, "copy", runtime_id=(7, 8, 9)),
        ]
        self.assertEqual(prototype.restore_target_index(targets, previous), 1)

    def test_chromium_renderer_raises_scan_depth(self):
        self.assertEqual(prototype.effective_scan_depth(16, True), 32)
        self.assertEqual(prototype.effective_scan_depth(30, True), 32)
        self.assertEqual(prototype.effective_scan_depth(36, True), 36)

    def test_desktop_scan_keeps_configured_depth(self):
        self.assertEqual(prototype.effective_scan_depth(16, False), 16)


if __name__ == "__main__":
    unittest.main()
