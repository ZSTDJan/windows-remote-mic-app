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
    def target(self, left, top, right, bottom, name=""):
        return prototype.TargetSnapshot(
            prototype.Rect(left, top, right, bottom),
            name,
            "ButtonControl",
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


if __name__ == "__main__":
    unittest.main()
