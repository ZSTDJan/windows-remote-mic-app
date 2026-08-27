import importlib.util
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

    def test_keeps_current_target_when_no_candidate_exists(self):
        targets = [self.target(100, 100, 160, 140, "only")]
        self.assertEqual(
            prototype.next_target_index(targets, 0, prototype.Direction.LEFT),
            0,
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

    def test_drops_secondary_action_nested_inside_primary_button(self):
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

    def test_folder_scope_hides_children_until_entered(self):
        folder = self.target(
            20,
            20,
            220,
            60,
            "folder",
            path=(0, 0),
            supports_expand=True,
            has_action_pattern=True,
        )
        first = self.target(
            20, 70, 220, 110, "first", path=(0, 1, 0), has_action_pattern=True
        )
        second = self.target(
            20, 115, 220, 155, "second", path=(0, 1, 1), has_action_pattern=True
        )
        outside = self.target(
            300, 20, 380, 60, "outside", path=(1,), has_action_pattern=True
        )
        targets = [folder, first, second, outside]
        groups = prototype.discover_group_scopes(
            targets,
            {
                (0, 0): "ButtonControl",
                (0, 1): "ListControl",
                (1,): "ButtonControl",
            },
        )
        self.assertEqual(groups, {(0,): 0})
        self.assertEqual(prototype.scope_target_indices(targets, groups), [0, 3])
        self.assertEqual(
            prototype.scope_target_indices(targets, groups, (0,)), [1, 2]
        )

    def test_small_options_button_does_not_claim_neighboring_list(self):
        options = self.target(
            220,
            20,
            255,
            55,
            "options",
            path=(0, 0),
            supports_expand=True,
            has_action_pattern=True,
        )
        row = self.target(
            20, 70, 260, 110, "conversation", path=(0, 1, 0), has_action_pattern=True
        )
        self.assertEqual(
            prototype.discover_group_scopes(
                [options, row],
                {(0, 0): "ButtonControl", (0, 1): "ListControl"},
            ),
            {},
        )

    def test_restore_target_uses_name_and_type_after_layout_moves(self):
        previous = self.target(20, 20, 120, 60, "folder")
        targets = [
            self.target(20, 200, 120, 240, "other"),
            self.target(20, 80, 120, 120, "folder"),
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
