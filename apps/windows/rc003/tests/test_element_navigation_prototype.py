import ast
import importlib.util
import json
import random
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "element_navigation_prototype.py"
)
TARGETING_CORE_PATH = SCRIPT_PATH.with_name("element_targeting_core.py")
SUPPORT_PATH = SCRIPT_PATH.with_name("element_navigation_support.py")
WINDOWS_HOST_PATH = SCRIPT_PATH.with_name("element_navigation_windows_host.py")
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

    def element(
        self,
        left,
        top,
        right,
        bottom,
        name="",
        control_type="TextControl",
        path=(),
        **kwargs,
    ):
        return prototype.ElementSnapshot(
            prototype.Rect(left, top, right, bottom),
            name,
            control_type,
            "",
            path,
            **kwargs,
        )

    def rgb_image(self, width, height, ink_rects):
        pixels = bytearray([255] * (width * height * 3))
        for left, top, right, bottom in ink_rects:
            for y in range(top, bottom):
                for x in range(left, right):
                    offset = (y * width + x) * 3
                    pixels[offset : offset + 3] = b"\x00\x00\x00"
        return bytes(pixels)

    def test_legacy_entry_reexports_the_spatial_core_objects(self):
        core = prototype._spatial_navigation_core

        self.assertEqual(
            Path(core.__file__).resolve(),
            SCRIPT_PATH.with_name("spatial_navigation_core.py"),
        )
        self.assertIs(prototype.Direction, core.Direction)
        self.assertIs(prototype.Rect, core.Rect)
        self.assertIs(prototype.TargetSnapshot, core.TargetSnapshot)
        self.assertIs(prototype.NavigationGraph, core.NavigationGraph)
        self.assertIs(prototype.NavigationTraversal, core.NavigationTraversal)

    def test_legacy_entry_reexports_every_spatial_core_symbol(self):
        core = prototype._spatial_navigation_core

        for name in core.__all__:
            with self.subTest(name=name):
                self.assertIs(getattr(prototype, name), getattr(core, name))

    def test_spatial_core_imports_only_declared_standard_library_modules(self):
        core_path = SCRIPT_PATH.with_name("spatial_navigation_core.py")
        tree = ast.parse(core_path.read_text(encoding="utf-8"))
        imported_modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.update(
                    alias.name.split(".", 1)[0] for alias in node.names
                )
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.add(node.module.split(".", 1)[0])

        self.assertEqual(
            imported_modules,
            {"__future__", "collections", "dataclasses", "enum", "typing"},
        )

    def test_legacy_entry_reexports_every_element_targeting_symbol(self):
        targeting = prototype._element_targeting_core

        self.assertEqual(Path(targeting.__file__).resolve(), TARGETING_CORE_PATH)
        self.assertTrue(
            set(targeting.__all__).isdisjoint(
                prototype._spatial_navigation_core.__all__
            )
        )
        for name in targeting.__all__:
            with self.subTest(name=name):
                self.assertIs(getattr(prototype, name), getattr(targeting, name))

    def test_element_targeting_core_has_no_platform_or_entry_dependencies(self):
        tree = ast.parse(TARGETING_CORE_PATH.read_text(encoding="utf-8"))
        imported_modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.update(
                    alias.name.split(".", 1)[0] for alias in node.names
                )
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.add(node.module.split(".", 1)[0])

        self.assertEqual(
            imported_modules,
            {
                "__future__",
                "collections",
                "dataclasses",
                "spatial_navigation_core",
                "threading",
                "time",
                "typing",
            },
        )

    def test_legacy_entry_reexports_every_navigation_support_symbol(self):
        support = prototype._element_navigation_support
        existing_exports = set(prototype._spatial_navigation_core.__all__) | set(
            prototype._element_targeting_core.__all__
        )

        self.assertEqual(Path(support.__file__).resolve(), SUPPORT_PATH)
        self.assertTrue(set(support.__all__).isdisjoint(existing_exports))
        for name in support.__all__:
            with self.subTest(name=name):
                self.assertIs(getattr(prototype, name), getattr(support, name))

    def test_navigation_support_has_no_platform_or_entry_dependencies(self):
        tree = ast.parse(SUPPORT_PATH.read_text(encoding="utf-8"))
        imported_modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.update(
                    alias.name.split(".", 1)[0] for alias in node.names
                )
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.add(node.module.split(".", 1)[0])

        self.assertEqual(
            imported_modules,
            {
                "__future__",
                "dataclasses",
                "element_targeting_core",
                "json",
                "os",
                "spatial_navigation_core",
                "threading",
                "typing",
            },
        )

    def test_windows_host_keeps_platform_imports_lazy_and_dpi_ordered(self):
        tree = ast.parse(WINDOWS_HOST_PATH.read_text(encoding="utf-8"))
        top_level_imports = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                top_level_imports.update(
                    alias.name.split(".", 1)[0] for alias in node.names
                )
            elif isinstance(node, ast.ImportFrom) and node.module:
                top_level_imports.add(node.module.split(".", 1)[0])

        self.assertEqual(
            top_level_imports,
            {
                "__future__",
                "argparse",
                "collections",
                "ctypes",
                "dataclasses",
                "element_navigation_command_windows",
                "element_navigation_support",
                "element_targeting_core",
                "queue",
                "spatial_navigation_core",
                "sys",
                "threading",
                "time",
                "typing",
            },
        )

        run_windows = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_run_windows"
        )
        dpi_call_line = min(
            node.lineno
            for node in ast.walk(run_windows)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "SetProcessDpiAwarenessContext"
        )
        platform_import_line = min(
            node.lineno
            for node in ast.walk(run_windows)
            if (
                isinstance(node, ast.Import)
                and any(alias.name == "uiautomation" for alias in node.names)
            )
            or (
                isinstance(node, ast.ImportFrom)
                and node.module
                and node.module.startswith("PySide6")
            )
        )
        self.assertLess(dpi_call_line, platform_import_line)

    def test_windows_host_loads_without_importing_uia_or_qt(self):
        code = f"""
import importlib.util
import sys

path = {str(SCRIPT_PATH)!r}
spec = importlib.util.spec_from_file_location("isolated_element_navigation", path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
host = module._load_element_navigation_windows_host()
assert host.__file__ == {str(WINDOWS_HOST_PATH)!r}
assert "uiautomation" not in sys.modules
assert not any(name == "PySide6" or name.startswith("PySide6.") for name in sys.modules)
"""
        with tempfile.TemporaryDirectory() as temp_dir:
            completed = subprocess.run(
                [sys.executable, "-c", code],
                cwd=temp_dir,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_legacy_entry_help_works_from_an_arbitrary_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            completed = subprocess.run(
                [sys.executable, str(SCRIPT_PATH), "--help"],
                cwd=temp_dir,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("--scan-only", completed.stdout)

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

    def test_long_overlapping_cell_claims_its_contacted_range(self):
        targets = [
            self.target(1100, 500, 1168, 568, "current action"),
            self.target(100, 400, 1200, 440, "long passive-looking row"),
            self.target(1100, 300, 1168, 368, "upper action"),
        ]

        self.assertEqual(
            prototype.ranked_target_indices(
                targets, 0, prototype.Direction.UP
            )[:2],
            [1, 2],
        )

    def test_horizontal_route_uses_an_intermediate_cell_only_in_the_same_row(self):
        targets = [
            self.target(100, 100, 160, 140, "current"),
            self.target(180, 105, 240, 145, "near-row intermediate"),
            self.target(300, 100, 360, 140, "far row target"),
        ]

        self.assertEqual(
            prototype.ranked_target_indices(
                targets, 0, prototype.Direction.RIGHT
            )[:2],
            [1, 2],
        )

    def test_horizontal_route_skips_an_off_row_intermediate_in_both_directions(self):
        targets = [
            self.target(100, 100, 160, 140, "left row target"),
            self.target(180, 165, 240, 205, "off-row intermediate"),
            self.target(300, 100, 360, 140, "right row target"),
        ]

        self.assertEqual(
            prototype.next_target_index(
                targets, 0, prototype.Direction.RIGHT
            ),
            2,
        )
        self.assertEqual(
            prototype.next_target_index(
                targets, 2, prototype.Direction.LEFT
            ),
            0,
        )
        graph = prototype.NavigationGraph(targets)
        self.assertEqual(graph.candidates(0, prototype.Direction.RIGHT)[0], 2)
        self.assertEqual(graph.candidates(2, prototype.Direction.LEFT)[0], 0)

    def test_horizontal_route_treats_one_pixel_overlap_as_contact(self):
        targets = [
            self.target(100, 100, 160, 140, "left row target"),
            self.target(180, 139, 240, 179, "one-pixel overlap"),
            self.target(300, 100, 360, 140, "right row target"),
        ]

        self.assertEqual(
            prototype.next_target_index(
                targets, 0, prototype.Direction.RIGHT
            ),
            1,
        )
        self.assertEqual(
            prototype.next_target_index(
                targets, 2, prototype.Direction.LEFT
            ),
            1,
        )

    def test_parent_body_and_inline_action_are_separate_grid_cells(self):
        targets = [
            self.target(
                100,
                100,
                500,
                150,
                "conversation",
                path=(0, 1),
                has_action_pattern=True,
            ),
            self.target(
                450,
                110,
                490,
                140,
                "archive",
                path=(0, 1, 0),
                has_action_pattern=True,
            ),
        ]
        graph = prototype.NavigationGraph(targets)

        body, action = graph.grid_rects
        self.assertEqual(body.left, 100)
        self.assertEqual(action.right, 490)
        self.assertEqual(body.right, action.left)
        self.assertEqual((body.top, body.bottom), (action.top, action.bottom))
        self.assertEqual(graph.candidates(0, prototype.Direction.RIGHT)[0], 1)
        self.assertEqual(graph.candidates(1, prototype.Direction.LEFT)[0], 0)

    def test_vertical_navigation_preserves_body_and_inline_action_columns(self):
        targets = [
            self.target(
                100,
                100,
                500,
                150,
                "row 1",
                path=(0, 1),
                has_action_pattern=True,
            ),
            self.target(
                450,
                110,
                490,
                140,
                "button 1",
                path=(0, 1, 0),
                has_action_pattern=True,
            ),
            self.target(
                100,
                180,
                500,
                230,
                "row 2",
                path=(0, 2),
                has_action_pattern=True,
            ),
            self.target(
                450,
                190,
                490,
                220,
                "button 2",
                path=(0, 2, 0),
                has_action_pattern=True,
            ),
        ]
        graph = prototype.NavigationGraph(targets)

        self.assertEqual(graph.candidates(0, prototype.Direction.DOWN)[0], 2)
        self.assertEqual(graph.candidates(2, prototype.Direction.UP)[0], 0)
        self.assertEqual(graph.candidates(1, prototype.Direction.DOWN)[0], 3)
        self.assertEqual(graph.candidates(3, prototype.Direction.UP)[0], 1)

    def test_multiple_inline_action_columns_keep_their_grid_tracks(self):
        targets = []
        for row, top in enumerate((100, 180, 260), 1):
            parent_path = (0, row)
            targets.append(
                self.target(
                    100,
                    top,
                    600,
                    top + 50,
                    f"row {row}",
                    path=parent_path,
                    has_action_pattern=True,
                )
            )
            targets.append(
                self.target(
                    500,
                    top + 10,
                    530,
                    top + 40,
                    f"first action {row}",
                    path=parent_path + (0,),
                    has_action_pattern=True,
                )
            )
            if row != 2:
                targets.append(
                    self.target(
                        550,
                        top + 10,
                        580,
                        top + 40,
                        f"second action {row}",
                        path=parent_path + (1,),
                        has_action_pattern=True,
                    )
                )
        graph = prototype.NavigationGraph(targets)

        self.assertEqual(graph.candidates(0, prototype.Direction.RIGHT)[0], 1)
        self.assertEqual(graph.candidates(1, prototype.Direction.RIGHT)[0], 2)
        self.assertEqual(graph.candidates(0, prototype.Direction.DOWN)[0], 3)
        self.assertEqual(graph.candidates(1, prototype.Direction.DOWN)[0], 4)
        traversal = prototype.NavigationTraversal()
        current_cell = graph.anchor_rects[2]
        middle = graph.candidates(
            2, prototype.Direction.DOWN, current_cell
        )[0]
        self.assertEqual(middle, 4)
        current_cell = prototype.navigation_contact_cell(
            current_cell,
            graph.grid_rects[middle],
            prototype.Direction.DOWN,
        )
        traversal.commit(middle, current_cell)
        self.assertEqual(
            traversal.available(
                middle,
                prototype.Direction.DOWN,
                graph.candidates(
                    middle, prototype.Direction.DOWN, current_cell
                ),
            )[0],
            7,
        )

    def test_missing_grid_cell_skips_within_the_same_column(self):
        targets = [
            self.target(100, 100, 300, 140, "A1"),
            self.target(400, 100, 460, 140, "A2"),
            self.target(100, 180, 300, 220, "B1"),
            self.target(100, 260, 300, 300, "C1"),
            self.target(400, 260, 460, 300, "C2"),
        ]

        self.assertEqual(
            prototype.next_target_index(targets, 1, prototype.Direction.DOWN),
            4,
        )

    def test_triangle_grid_keeps_horizontal_row_and_reverse_return(self):
        targets = [
            self.target(0, 100, 60, 140, "A"),
            self.target(300, 100, 360, 140, "B"),
            self.target(150, 0, 210, 40, "C"),
        ]
        graph = prototype.NavigationGraph(targets)
        traversal = prototype.NavigationTraversal()

        self.assertEqual(graph.candidates(0, prototype.Direction.RIGHT)[0], 1)
        self.assertEqual(graph.candidates(1, prototype.Direction.LEFT)[0], 0)
        upper = traversal.available(
            0,
            prototype.Direction.UP,
            graph.candidates(0, prototype.Direction.UP),
        )[0]
        traversal.commit(upper)
        self.assertEqual(upper, 2)
        self.assertEqual(
            traversal.available(
                upper,
                prototype.Direction.DOWN,
                graph.candidates(upper, prototype.Direction.DOWN),
            )[0],
            0,
        )

    def test_irregular_cell_does_not_rewire_the_nearest_column(self):
        targets = [
            self.target(0, 0, 40, 40, "A"),
            self.target(200, 0, 240, 40, "B"),
            self.target(150, 100, 190, 140, "X"),
            self.target(0, 200, 40, 240, "C"),
            self.target(200, 200, 240, 240, "D"),
        ]
        graph = prototype.NavigationGraph(targets)

        traversal = prototype.NavigationTraversal()
        current_cell = graph.anchor_rects[1]
        middle = graph.candidates(
            1, prototype.Direction.DOWN, current_cell
        )[0]
        self.assertEqual(middle, 2)
        current_cell = prototype.navigation_contact_cell(
            current_cell,
            graph.grid_rects[middle],
            prototype.Direction.DOWN,
        )
        traversal.commit(middle, current_cell)
        self.assertEqual(
            traversal.available(
                middle,
                prototype.Direction.DOWN,
                graph.candidates(
                    middle, prototype.Direction.DOWN, current_cell
                ),
            )[0],
            4,
        )

    def test_overlapping_candidates_use_forward_center_distance(self):
        targets = [
            self.target(100, 100, 200, 140, "current"),
            self.target(80, 100, 160, 140, "near overlapping left"),
            self.target(0, 100, 180, 140, "far overlapping left"),
        ]

        self.assertEqual(
            prototype.ranked_target_indices(
                targets, 0, prototype.Direction.LEFT
            )[:2],
            [1, 2],
        )

    def test_right_stops_at_the_end_of_a_grid_row(self):
        targets = [
            self.target(900, 100, 960, 140, "p1", path=(0, 2, 0)),
            self.target(500, 180, 560, 220, "p2", path=(0, 2, 1)),
            self.target(700, 180, 760, 220, "next row second", path=(0, 2, 2)),
        ]
        self.assertEqual(
            prototype.next_target_index(targets, 0, prototype.Direction.RIGHT),
            0,
        )

    def test_left_stops_at_the_start_of_a_grid_row(self):
        targets = [
            self.target(900, 100, 960, 140, "p1", path=(0, 2, 0)),
            self.target(500, 180, 560, 220, "p2", path=(0, 2, 1)),
        ]
        self.assertEqual(
            prototype.next_target_index(targets, 1, prototype.Direction.LEFT),
            1,
        )

    def test_true_direction_does_not_append_reading_order_wrap(self):
        targets = [
            self.target(900, 100, 960, 140, "p1", path=(0, 2, 0)),
            self.target(500, 180, 560, 220, "p2", path=(0, 2, 1)),
            self.target(980, 155, 1040, 195, "sidebar", path=(0, 1, 0)),
        ]
        self.assertEqual(
            prototype.ranked_target_indices(
                targets, 0, prototype.Direction.RIGHT
            ),
            [2],
        )

    def test_forward_target_does_not_append_opposite_sidebar(self):
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
            ),
            [1],
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

    def test_specific_child_beats_a_broad_action_parent(self):
        targets = [
            self.target(20, 100, 80, 140, "current", path=(0, 0)),
            self.target(
                120,
                60,
                420,
                220,
                "broad action",
                path=(0, 1),
                has_action_pattern=True,
            ),
            self.target(
                150,
                100,
                190,
                140,
                "specific button",
                path=(0, 1, 0),
                has_action_pattern=True,
            ),
        ]
        self.assertTrue(prototype.target_has_finer_descendant(targets, 1))
        self.assertFalse(prototype.target_has_finer_descendant(targets, 2))
        self.assertEqual(
            prototype.next_target_index(targets, 0, prototype.Direction.RIGHT),
            2,
        )

    def test_broad_target_keeps_priority_over_an_off_lane_child(self):
        targets = [
            self.target(20, 100, 80, 140, "current", path=(0, 0)),
            self.target(
                120,
                90,
                420,
                240,
                "broad action",
                path=(0, 1),
                has_action_pattern=True,
            ),
            self.target(
                150,
                190,
                190,
                230,
                "off lane child",
                path=(0, 1, 0),
                has_action_pattern=True,
            ),
        ]
        self.assertEqual(
            prototype.next_target_index(targets, 0, prototype.Direction.RIGHT),
            1,
        )

    def test_broad_target_keeps_priority_over_a_far_child_in_the_same_lane(self):
        targets = [
            self.target(0, 100, 40, 140, "current", path=(0, 0)),
            self.target(
                60,
                80,
                1000,
                180,
                "near broad action",
                path=(0, 1),
                has_action_pattern=True,
            ),
            self.target(
                900,
                100,
                940,
                140,
                "far child action",
                path=(0, 1, 0),
                has_action_pattern=True,
            ),
        ]
        self.assertEqual(
            prototype.ranked_target_indices(
                targets, 0, prototype.Direction.RIGHT
            )[:2],
            [1, 2],
        )

    def test_broad_target_keeps_priority_over_a_far_perpendicular_child(self):
        targets = [
            self.target(0, 0, 40, 1000, "current", path=(0, 0)),
            self.target(
                60,
                0,
                500,
                1000,
                "near broad action",
                path=(0, 1),
                has_action_pattern=True,
            ),
            self.target(
                80,
                900,
                120,
                940,
                "far lower child",
                path=(0, 1, 0),
                has_action_pattern=True,
            ),
        ]
        self.assertEqual(
            prototype.ranked_target_indices(
                targets, 0, prototype.Direction.RIGHT
            )[:2],
            [1, 2],
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
                    current_cell = graph.grid_rects[current]
                    for _step in range(len(targets) + 1):
                        candidates = traversal.available(
                            current,
                            direction,
                            graph.candidates(
                                current, direction, current_cell
                            ),
                        )
                        if not candidates:
                            break
                        next_index = candidates[0]
                        next_cell = prototype.navigation_contact_cell(
                            current_cell,
                            graph.grid_rects[next_index],
                            direction,
                        )
                        self.assertTrue(
                            graph.grid_rects[next_index].contains(next_cell)
                        )
                        self.assertNotIn(next_index, seen)
                        seen.add(next_index)
                        traversal.commit(next_index, next_cell)
                        current = next_index
                        current_cell = next_cell

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

    def test_immediate_opposite_direction_keeps_static_route_order(self):
        traversal = prototype.NavigationTraversal()
        right = traversal.available(
            0, prototype.Direction.RIGHT, (1, 2)
        )[0]
        traversal.commit(right)
        left_candidates = traversal.available(
            right, prototype.Direction.LEFT, (2, 0)
        )
        self.assertEqual(left_candidates, (2, 0))

    def test_immediate_opposite_direction_returns_when_no_static_route_exists(self):
        traversal = prototype.NavigationTraversal()
        right = traversal.available(0, prototype.Direction.RIGHT, (1,))[0]
        traversal.commit(right)

        self.assertEqual(
            traversal.available(right, prototype.Direction.LEFT, ()),
            (0,),
        )

    def test_right_stops_instead_of_wrapping_to_a_lower_folder(self):
        targets = [
            self.target(300, 100, 340, 140, "options", path=(0, 1, 0)),
            self.target(360, 100, 400, 140, "add", path=(0, 1, 1)),
            self.target(20, 150, 280, 195, "folder", path=(0, 1, 2)),
        ]
        graph = prototype.NavigationGraph(targets)
        traversal = prototype.NavigationTraversal()
        current = traversal.available(
            0,
            prototype.Direction.RIGHT,
            graph.candidates(0, prototype.Direction.RIGHT),
        )[0]
        traversal.commit(current)
        self.assertEqual(current, 1)
        self.assertEqual(
            traversal.available(
                current,
                prototype.Direction.RIGHT,
                graph.candidates(current, prototype.Direction.RIGHT),
            ),
            (),
        )

    def test_left_can_reach_a_lower_target_in_the_left_half_plane(self):
        targets = [
            self.target(300, 100, 340, 140, "options", path=(0, 1, 0)),
            self.target(360, 100, 400, 140, "add", path=(0, 1, 1)),
            self.target(20, 150, 280, 195, "folder", path=(0, 1, 2)),
        ]
        self.assertEqual(
            prototype.next_target_index(targets, 0, prototype.Direction.LEFT),
            2,
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

    def test_infers_first_large_split_region_as_navigation_section(self):
        window = prototype.Rect(496, 188, 2388, 1758)
        shared = (1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 4)
        full_width_content = shared + (0,)
        sidebar = full_width_content + (0,)
        main = full_width_content + (1,)
        sidebar_target_path = sidebar + (3, 3, 1)
        target_path = main + (2, 0, 0, 0, 0, 0, 1, 0, 0, 0)
        rects = {
            shared: prototype.Rect(497, 242, 2390, 1759),
            full_width_content: prototype.Rect(497, 400, 2390, 1759),
            sidebar: prototype.Rect(497, 242, 934, 1759),
            main: prototype.Rect(934, 242, 2390, 1759),
        }

        self.assertEqual(
            prototype.infer_navigation_section_path(
                sidebar_target_path, rects, window
            ),
            sidebar,
        )
        self.assertEqual(
            prototype.infer_navigation_section_path(
                target_path, rects, window
            ),
            main,
        )

    def test_distinguishes_main_header_from_scrollable_content_region(self):
        window = prototype.Rect(216, 183, 2108, 1753)
        main = (1, 4, 1)
        header = main + (1,)
        body = main + (2, 0, 0)
        sidebar = (1, 4, 0)
        project_list = sidebar + (3, 3)
        rects = {
            main: prototype.Rect(652, 237, 2109, 1754),
            header: prototype.Rect(652, 237, 2109, 306),
            body: prototype.Rect(653, 307, 2109, 1754),
            sidebar: prototype.Rect(216, 237, 653, 1754),
            project_list: prototype.Rect(228, 535, 618, 906),
        }
        types = {project_list: "ListControl"}

        self.assertEqual(
            prototype.infer_navigation_section_path(
                header + (1, 0), rects, window
            ),
            header,
        )
        self.assertEqual(
            prototype.infer_navigation_section_path(
                body + (0, 0, 1), rects, window
            ),
            body,
        )
        self.assertEqual(
            prototype.infer_navigation_section_path(
                project_list + (0, 0), rects, window, types
            ),
            project_list,
        )

    def test_adjacent_section_exit_treats_above_and_below_equally(self):
        body_section = (0, 2)
        sidebar_section = (0, 1)
        body_rect = prototype.Rect(300, 0, 1000, 800)
        sidebar_rect = prototype.Rect(0, 0, 280, 800)
        targets = [
            self.target(
                300,
                200,
                340,
                240,
                "current",
                section_path=body_section,
                section_rect=body_rect,
            ),
            self.target(
                100,
                100,
                140,
                140,
                "upper left",
                section_path=sidebar_section,
                section_rect=sidebar_rect,
            ),
            self.target(
                100,
                250,
                140,
                290,
                "lower left",
                section_path=sidebar_section,
                section_rect=sidebar_rect,
            ),
        ]

        self.assertEqual(
            prototype.next_target_index(
                targets, 0, prototype.Direction.LEFT
            ),
            2,
        )

    def test_adjacent_pane_includes_its_indented_inner_list(self):
        body_section = (0, 2)
        sidebar_section = (0, 1)
        inner_list_section = sidebar_section + (3,)
        targets = [
            self.target(
                947,
                1334,
                2052,
                1389,
                "current file action",
                section_path=body_section,
                section_rect=prototype.Rect(773, 376, 2226, 1823),
            ),
            self.target(
                345,
                486,
                738,
                531,
                "outer sidebar row",
                section_path=sidebar_section,
                section_rect=prototype.Rect(333, 421, 773, 1754),
            ),
            self.target(
                345,
                879,
                738,
                924,
                "aligned inner list row",
                section_path=inner_list_section,
                section_rect=prototype.Rect(345, 693, 738, 1017),
            ),
        ]

        self.assertEqual(
            prototype.ranked_target_indices(
                targets, 0, prototype.Direction.LEFT
            )[:2],
            [2, 1],
        )
        self.assertEqual(
            prototype.next_target_index(
                targets, 2, prototype.Direction.RIGHT
            ),
            0,
        )

    def test_same_section_left_compares_upper_and_lower_targets_equally(self):
        section = (0, 2)
        section_rect = prototype.Rect(0, 0, 1000, 800)
        targets = [
            self.target(
                300,
                200,
                340,
                240,
                "current",
                section_path=section,
                section_rect=section_rect,
            ),
            self.target(
                100,
                20,
                140,
                60,
                "far upper left",
                section_path=section,
                section_rect=section_rect,
            ),
            self.target(
                100,
                250,
                140,
                290,
                "near lower left",
                section_path=section,
                section_rect=section_rect,
            ),
        ]

        self.assertEqual(
            prototype.next_target_index(
                targets, 0, prototype.Direction.LEFT
            ),
            2,
        )

    def test_right_prefers_axis_alignment_over_almost_vertical_target(self):
        section = (0, 2)
        section_rect = prototype.Rect(120, 200, 1260, 700)
        targets = [
            self.target(
                155,
                271,
                197,
                313,
                "current",
                section_path=section,
                section_rect=section_rect,
            ),
            self.target(
                170,
                568,
                220,
                608,
                "almost below",
                section_path=section,
                section_rect=section_rect,
            ),
            self.target(
                1090,
                325,
                1225,
                392,
                "corresponding right",
                section_path=section,
                section_rect=section_rect,
            ),
        ]

        self.assertEqual(
            prototype.next_target_index(
                targets, 0, prototype.Direction.RIGHT
            ),
            2,
        )

    def test_left_exits_content_to_aligned_sidebar_not_upper_header(self):
        body_section = (0, 2, 1)
        header_section = (0, 2, 0)
        sidebar_section = (0, 1)
        body_rect = prototype.Rect(653, 307, 2109, 1754)
        header_rect = prototype.Rect(652, 237, 2109, 306)
        sidebar_rect = prototype.Rect(216, 397, 653, 1722)
        targets = [
            self.target(
                823,
                295,
                862,
                334,
                "复制",
                path=body_section + (0, 39),
                section_path=body_section,
                section_rect=body_rect,
            ),
            self.target(
                706,
                253,
                932,
                290,
                "窗口标题",
                path=header_section + (1, 1),
                section_path=header_section,
                section_rect=header_rect,
            ),
            self.target(
                228,
                397,
                618,
                443,
                "对应的左侧行",
                path=sidebar_section + (3, 0),
                section_path=sidebar_section,
                section_rect=sidebar_rect,
            ),
            self.target(
                228,
                700,
                618,
                746,
                "更远的左侧行",
                path=sidebar_section + (8, 0),
                section_path=sidebar_section,
                section_rect=sidebar_rect,
            ),
        ]

        ranked = prototype.ranked_target_indices(
            targets, 0, prototype.Direction.LEFT
        )
        self.assertEqual(ranked[:3], [2, 3, 1])
        graph = prototype.NavigationGraph(targets)
        self.assertEqual(graph.candidates(0, prototype.Direction.LEFT)[0], 2)
        self.assertEqual(graph.candidates(2, prototype.Direction.RIGHT)[0], 0)
        diagnostic = prototype.build_navigation_diagnostic(
            targets,
            0,
            prototype.Direction.LEFT,
            ranked_indices=ranked,
            available_indices=ranked,
            selected_index=2,
            outcome="selected",
        )
        self.assertIsNotNone(diagnostic)
        assert diagnostic is not None
        self.assertEqual(diagnostic.candidates[0].route, "diagonal")
        self.assertIn(
            "斜向候选", prototype.format_navigation_diagnostic(diagnostic)
        )

    def test_range_occupancy_grid_ignores_hierarchy_and_uses_spatial_order(self):
        main_section = (0, 2)
        main_rect = prototype.Rect(934, 242, 2390, 1759)
        sidebar_rect = prototype.Rect(497, 242, 934, 1759)
        targets = [
            self.target(
                1104,
                688,
                1143,
                727,
                "复制",
                path=main_section + (0, 0, 52),
                section_path=main_section,
                section_rect=main_rect,
            ),
            self.target(
                2170,
                875,
                2209,
                914,
                "复制消息",
                path=main_section + (0, 0, 58),
                section_path=main_section,
                section_rect=main_rect,
            ),
            self.target(
                509,
                891,
                899,
                937,
                "窗口对话列表",
                path=(0, 1, 3, 3, 1),
                section_path=(0, 1),
                section_rect=sidebar_rect,
            ),
        ]

        self.assertEqual(
            prototype.ranked_target_indices(
                targets, 1, prototype.Direction.LEFT
            )[:2],
            [2, 0],
        )
        graph = prototype.NavigationGraph(targets)
        self.assertEqual(graph.candidates(1, prototype.Direction.LEFT)[0], 2)
        self.assertEqual(graph.candidates(2, prototype.Direction.RIGHT)[0], 1)

        ranked = graph.candidates(1, prototype.Direction.LEFT)
        diagnostic = prototype.build_navigation_diagnostic(
            targets,
            1,
            prototype.Direction.LEFT,
            current_rect=graph.anchor_rects[1],
            grid_rects=graph.grid_rects,
            ranked_indices=ranked,
            available_indices=ranked,
            selected_index=2,
            outcome="selected",
        )
        self.assertIsNotNone(diagnostic)
        assert diagnostic is not None
        self.assertEqual(diagnostic.candidates[0].index, 2)

    def test_same_region_does_not_lock_out_a_much_nearer_other_pane(self):
        main_rect = prototype.Rect(480, 0, 1000, 800)
        adjacent_rect = prototype.Rect(0, 0, 480, 800)
        targets = [
            self.target(
                500,
                400,
                540,
                440,
                "current",
                path=(0, 2, 0),
                section_path=(0, 2),
                section_rect=main_rect,
            ),
            self.target(
                100,
                100,
                140,
                140,
                "far same region",
                path=(0, 2, 1),
                section_path=(0, 2),
                section_rect=main_rect,
            ),
            self.target(
                420,
                400,
                470,
                440,
                "near other pane",
                path=(0, 1, 0),
                section_path=(0, 1),
                section_rect=adjacent_rect,
            ),
        ]

        self.assertEqual(
            prototype.next_target_index(
                targets, 0, prototype.Direction.LEFT
            ),
            2,
        )

    def test_steep_same_region_diagonal_does_not_beat_an_aligned_adjacent_pane(self):
        main_section = (0, 2)
        sidebar_section = (0, 1)
        targets = [
            self.target(
                700,
                400,
                740,
                440,
                "current",
                section_path=main_section,
                section_rect=prototype.Rect(480, 0, 1000, 800),
            ),
            self.target(
                500,
                625,
                540,
                665,
                "steep same-region diagonal",
                section_path=main_section,
                section_rect=prototype.Rect(480, 0, 1000, 800),
            ),
            self.target(
                200,
                400,
                440,
                440,
                "aligned adjacent pane",
                section_path=sidebar_section,
                section_rect=prototype.Rect(0, 0, 480, 800),
            ),
        ]

        self.assertEqual(
            prototype.next_target_index(
                targets, 0, prototype.Direction.LEFT
            ),
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

    def test_vertical_navigation_uses_the_long_message_range(self):
        targets = [
            self.target(500, 100, 1200, 240, "message"),
            self.target(495, 250, 535, 290, "复制"),
            self.target(500, 330, 1200, 430, "next message"),
        ]
        self.assertEqual(
            prototype.next_target_index(targets, 0, prototype.Direction.DOWN),
            1,
        )
        self.assertEqual(
            prototype.next_target_index(targets, 0, prototype.Direction.LEFT),
            1,
        )

    def test_bottom_controls_enter_the_wide_input_before_distant_headers(self):
        targets = [
            self.target(530, 952, 1599, 1018, "input"),
            self.target(524, 1024, 567, 1066, "add"),
            self.target(573, 1024, 708, 1066, "permission"),
            self.target(1431, 1024, 1563, 1066, "model"),
            self.target(1562, 1024, 1605, 1066, "stop"),
            self.target(494, 71, 597, 108, "left header"),
            self.target(1271, 7, 1477, 49, "right header"),
        ]
        graph = prototype.NavigationGraph(targets)

        for current in range(1, 5):
            with self.subTest(target=targets[current].name):
                self.assertEqual(
                    graph.candidates(current, prototype.Direction.UP)[0],
                    0,
                )

    def test_wide_input_keeps_the_column_where_navigation_entered(self):
        targets = [
            self.target(524, 1024, 567, 1066, "add"),
            self.target(530, 952, 1599, 1018, "input"),
            self.target(506, 851, 545, 890, "copy"),
            self.target(547, 851, 586, 890, "branch"),
            self.target(1040, 847, 1089, 895, "scroll"),
        ]
        graph = prototype.NavigationGraph(targets)
        current_cell = graph.anchor_rects[0]

        input_index = graph.candidates(
            0, prototype.Direction.UP, current_cell
        )[0]
        self.assertEqual(input_index, 1)
        input_cell = prototype.navigation_contact_cell(
            current_cell,
            graph.grid_rects[input_index],
            prototype.Direction.UP,
        )
        self.assertEqual(
            (input_cell.left, input_cell.right),
            (current_cell.left, current_cell.right),
        )
        self.assertEqual(
            graph.candidates(
                input_index, prototype.Direction.UP, input_cell
            )[0],
            2,
        )

    def test_direction_change_recenters_the_virtual_contact_lane(self):
        traversal = prototype.NavigationTraversal()
        anchor = prototype.Rect(320, 35, 360, 75)
        virtual_lane = prototype.Rect(80, 20, 120, 100)

        traversal.available(0, prototype.Direction.RIGHT, (1,))
        traversal.commit(1, virtual_lane)

        self.assertEqual(
            traversal.current_cell(1, anchor, prototype.Direction.RIGHT),
            virtual_lane,
        )
        self.assertEqual(
            traversal.current_cell(1, anchor, prototype.Direction.UP),
            anchor,
        )

    def test_failed_direction_change_preserves_the_previous_contact_lane(self):
        traversal = prototype.NavigationTraversal()
        anchor = prototype.Rect(50, 260, 1150, 360)
        vertical_lane = prototype.Rect(280, 260, 380, 360)

        traversal.available(0, prototype.Direction.UP, (1,))
        traversal.commit(1, vertical_lane)

        self.assertEqual(
            traversal.current_cell(1, anchor, prototype.Direction.LEFT),
            anchor,
        )
        self.assertEqual(
            traversal.available(1, prototype.Direction.LEFT, ()),
            (),
        )
        self.assertEqual(
            traversal.current_cell(1, anchor, prototype.Direction.UP),
            vertical_lane,
        )

    def test_far_vertical_candidate_requires_the_available_horizontal_step(self):
        coordinates = [
            (43, 336, 112, 378),
            (572, 590, 661, 680),
            (405, 459, 454, 544),
            (45, 414, 124, 481),
            (313, 375, 446, 438),
            (892, 589, 952, 656),
            (678, 589, 747, 649),
            (497, 161, 538, 205),
            (384, 585, 456, 633),
        ]
        targets = [
            self.target(left, top, right, bottom, str(index))
            for index, (left, top, right, bottom) in enumerate(coordinates)
        ]
        graph = prototype.NavigationGraph(targets)

        upper = graph.candidates(
            5,
            prototype.Direction.UP,
            graph.anchor_rects[5],
        )[0]
        self.assertEqual(upper, 7)
        self.assertTrue(
            graph.requires_orthogonal_grid_step(
                5,
                prototype.Direction.UP,
                graph.anchor_rects[5],
                upper,
            )
        )
        self.assertEqual(
            graph.candidates(
                5,
                prototype.Direction.LEFT,
                graph.anchor_rects[5],
            )[0],
            6,
        )
        self.assertEqual(
            graph.candidates(
                1,
                prototype.Direction.UP,
                graph.anchor_rects[1],
            )[0],
            7,
        )
        self.assertFalse(
            graph.requires_orthogonal_grid_step(
                1,
                prototype.Direction.UP,
                graph.anchor_rects[1],
                7,
            )
        )

    def test_far_diagonal_remains_available_without_an_orthogonal_step(self):
        targets = [
            self.target(400, 400, 440, 440, "current"),
            self.target(100, 100, 140, 140, "upper left"),
        ]
        graph = prototype.NavigationGraph(targets)

        candidate = graph.candidates(
            0,
            prototype.Direction.UP,
            graph.anchor_rects[0],
        )[0]
        self.assertEqual(candidate, 1)
        self.assertFalse(
            graph.requires_orthogonal_grid_step(
                0,
                prototype.Direction.UP,
                graph.anchor_rects[0],
                candidate,
            )
        )

    def test_orthogonal_step_requires_a_real_turn_to_the_candidate(self):
        targets = [
            self.target(400, 400, 440, 440, "current"),
            self.target(100, 100, 140, 140, "upper left"),
            self.target(300, 400, 340, 440, "left dead end"),
        ]
        graph = prototype.NavigationGraph(targets)
        graph._contacts = {
            (0, prototype.Direction.UP): (
                prototype.NavigationContact(1, 0, 40),
            ),
            (0, prototype.Direction.LEFT): (
                prototype.NavigationContact(2, 0, 40),
            ),
            (2, prototype.Direction.RIGHT): (
                prototype.NavigationContact(0, 0, 40),
            ),
        }

        self.assertFalse(
            graph.requires_orthogonal_grid_step(
                0,
                prototype.Direction.UP,
                graph.anchor_rects[0],
                1,
            )
        )

    def test_navigation_thresholds_scale_with_the_layout(self):
        base_current = prototype.Rect(400, 400, 440, 440)
        base_target = prototype.Rect(300, 300, 340, 340)
        base_rects = [base_current, base_target, prototype.Rect(0, 0, 40, 40)]
        base_unit = prototype.navigation_scale_unit(base_rects)
        base_tolerance = prototype._navigation_lane_tolerance(
            base_current,
            base_target,
            prototype.Direction.UP,
            base_unit,
        )

        for factor in (0.5, 1.5, 2.0):
            def scaled(rect):
                return prototype.Rect(
                    round(rect.left * factor),
                    round(rect.top * factor),
                    round(rect.right * factor),
                    round(rect.bottom * factor),
                )

            scaled_rects = [scaled(rect) for rect in base_rects]
            scaled_unit = prototype.navigation_scale_unit(scaled_rects)
            scaled_tolerance = prototype._navigation_lane_tolerance(
                scaled_rects[0],
                scaled_rects[1],
                prototype.Direction.UP,
                scaled_unit,
            )
            self.assertAlmostEqual(scaled_unit, base_unit * factor)
            self.assertAlmostEqual(scaled_tolerance, base_tolerance * factor)

    def test_section_boundary_only_assists_parallel_movement(self):
        left = self.target(
            100,
            300,
            140,
            340,
            "left",
            section_rect=prototype.Rect(0, 0, 300, 800),
        )
        right = self.target(
            500,
            100,
            540,
            140,
            "right",
            section_rect=prototype.Rect(300, 0, 800, 800),
        )
        top = self.target(
            100,
            100,
            140,
            140,
            "top",
            section_rect=prototype.Rect(0, 0, 800, 300),
        )
        bottom = self.target(
            500,
            500,
            540,
            540,
            "bottom",
            section_rect=prototype.Rect(0, 300, 800, 800),
        )

        self.assertTrue(
            prototype.navigation_crosses_parallel_section_boundary(
                left, right, prototype.Direction.UP
            )
        )
        self.assertFalse(
            prototype.navigation_crosses_parallel_section_boundary(
                left, right, prototype.Direction.RIGHT
            )
        )
        self.assertTrue(
            prototype.navigation_crosses_parallel_section_boundary(
                top, bottom, prototype.Direction.LEFT
            )
        )
        self.assertFalse(
            prototype.navigation_crosses_parallel_section_boundary(
                top, bottom, prototype.Direction.DOWN
            )
        )

    def test_parallel_section_bonus_prefers_the_same_column(self):
        left_section = prototype.Rect(0, 0, 300, 800)
        right_section = prototype.Rect(300, 0, 800, 800)
        targets = [
            self.target(
                280,
                400,
                320,
                440,
                "current",
                section_rect=left_section,
            ),
            self.target(
                260,
                100,
                300,
                140,
                "same column",
                section_rect=left_section,
            ),
            self.target(
                300,
                200,
                340,
                240,
                "near other column",
                section_rect=right_section,
            ),
        ]
        graph = prototype.NavigationGraph(targets)
        graph._contacts[(0, prototype.Direction.UP)] = (
            prototype.NavigationContact(2, 0, 40),
            prototype.NavigationContact(1, 0, 40),
        )
        graph._natural.clear()

        self.assertEqual(graph.candidates(0, prototype.Direction.UP)[0], 1)

        graph._contacts[(0, prototype.Direction.UP)] = (
            prototype.NavigationContact(2, 0, 40),
        )
        graph._natural.clear()
        candidates = graph.candidates(0, prototype.Direction.UP)
        self.assertEqual(candidates[0], 2)
        self.assertIn(1, candidates)

    def test_codex_bottom_controls_cross_full_width_rows_before_branching(self):
        targets = [
            self.target(996, 786, 1099, 823, "button mouse"),
            self.target(1049, 849, 1088, 888, "upper branch"),
            self.target(1998, 901, 2119, 1022, "attachment"),
            self.target(2074, 1137, 2113, 1176, "message copy"),
            self.target(1014, 1199, 1183, 1234, "elapsed"),
            self.target(1049, 1566, 1088, 1605, "lower branch"),
            self.target(1032, 1667, 2101, 1733, "input"),
            self.target(1026, 1739, 1069, 1781, "add"),
            self.target(1075, 1739, 1210, 1781, "permission"),
            self.target(1933, 1739, 2065, 1781, "model"),
            self.target(2064, 1739, 2107, 1781, "stop"),
        ]
        graph = prototype.NavigationGraph(targets)

        def move_up_twice(start: int) -> tuple[int, int]:
            traversal = prototype.NavigationTraversal()
            current = start
            current_cell = graph.anchor_rects[current]
            path = []
            for _step in range(2):
                candidates = traversal.available(
                    current,
                    prototype.Direction.UP,
                    graph.candidates(
                        current, prototype.Direction.UP, current_cell
                    ),
                )
                next_index = candidates[0]
                current_cell = prototype.navigation_contact_cell(
                    current_cell,
                    graph.grid_rects[next_index],
                    prototype.Direction.UP,
                )
                traversal.commit(next_index, current_cell)
                current = next_index
                path.append(current)
            return tuple(path)

        self.assertEqual(move_up_twice(7), (6, 5))
        self.assertEqual(move_up_twice(8), (6, 5))
        self.assertEqual(move_up_twice(9), (6, 5))
        self.assertEqual(move_up_twice(10), (6, 5))

    def test_codex_navigation_routes_are_stable_under_uniform_scale(self):
        coordinates = [
            (996, 786, 1099, 823, "button mouse"),
            (1049, 849, 1088, 888, "upper branch"),
            (1998, 901, 2119, 1022, "attachment"),
            (2074, 1137, 2113, 1176, "message copy"),
            (1014, 1199, 1183, 1234, "elapsed"),
            (1049, 1566, 1088, 1605, "lower branch"),
            (1032, 1667, 2101, 1733, "input"),
            (1026, 1739, 1069, 1781, "add"),
            (1075, 1739, 1210, 1781, "permission"),
            (1933, 1739, 2065, 1781, "model"),
            (2064, 1739, 2107, 1781, "stop"),
        ]

        def routes(factor):
            targets = [
                self.target(
                    round(left * factor),
                    round(top * factor),
                    round(right * factor),
                    round(bottom * factor),
                    name,
                )
                for left, top, right, bottom, name in coordinates
            ]
            graph = prototype.NavigationGraph(targets)
            result = []
            for index in range(len(targets)):
                for direction in prototype.Direction:
                    candidates = graph.candidates(
                        index,
                        direction,
                        graph.anchor_rects[index],
                    )
                    first = candidates[0] if candidates else None
                    result.append(
                        (
                            first,
                            bool(
                                first is not None
                                and graph.requires_orthogonal_grid_step(
                                    index,
                                    direction,
                                    graph.anchor_rects[index],
                                    first,
                                )
                            ),
                        )
                    )
            return tuple(result)

        self.assertEqual(routes(0.75), routes(1.0))
        self.assertEqual(routes(1.25), routes(1.0))

    def test_contact_cell_direction_change_uses_the_recentered_static_route(self):
        generator = random.Random(830)
        for _case in range(80):
            targets = []
            for index in range(generator.randint(2, 35)):
                left = generator.randint(0, 1600)
                top = generator.randint(0, 900)
                width = generator.randint(24, 360)
                height = generator.randint(20, 120)
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
                    current_cell = graph.grid_rects[start]
                    candidates = traversal.available(
                        start,
                        direction,
                        graph.candidates(start, direction, current_cell),
                    )
                    if not candidates:
                        continue
                    next_index = candidates[0]
                    next_cell = prototype.navigation_contact_cell(
                        current_cell,
                        graph.grid_rects[next_index],
                        direction,
                    )
                    traversal.commit(next_index, next_cell)
                    reverse = prototype.OPPOSITE_DIRECTION[direction]
                    reverse_cell = traversal.current_cell(
                        next_index,
                        graph.anchor_rects[next_index],
                        reverse,
                    )
                    static_reverse = graph.candidates(
                        next_index,
                        reverse,
                        reverse_cell,
                    )
                    reverse_candidates = traversal.available(
                        next_index,
                        reverse,
                        static_reverse,
                    )
                    self.assertEqual(
                        reverse_candidates[0],
                        static_reverse[0] if static_reverse else start,
                    )

    def test_navigation_diagnostic_uses_the_active_contact_cell(self):
        targets = [
            self.target(530, 952, 1599, 1018, "input"),
            self.target(506, 851, 545, 890, "left action"),
            self.target(1500, 851, 1539, 890, "right action"),
        ]
        active_cell = prototype.Rect(1490, 952, 1590, 1018)
        diagnostic = prototype.build_navigation_diagnostic(
            targets,
            0,
            prototype.Direction.UP,
            current_rect=active_cell,
        )

        self.assertIsNotNone(diagnostic)
        assert diagnostic is not None
        self.assertEqual(diagnostic.candidates[0].index, 2)

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

    def test_navigation_diagnostic_reports_no_candidate_at_grid_edge(self):
        targets = [
            self.target(900, 100, 960, 140, "row end", path=(0, 2, 0)),
            self.target(500, 180, 560, 220, "next row", path=(0, 2, 1)),
        ]
        diagnostic = prototype.build_navigation_diagnostic(
            targets,
            0,
            prototype.Direction.RIGHT,
            available_indices=(),
            outcome="no_candidate",
        )

        self.assertIsNotNone(diagnostic)
        assert diagnostic is not None
        self.assertEqual(diagnostic.candidates, ())
        self.assertIn("保持原位", prototype.format_navigation_diagnostic(diagnostic))

    def test_navigation_diagnostic_explains_required_grid_alignment(self):
        diagnostic = prototype.build_navigation_diagnostic(
            [
                self.target(400, 400, 440, 440, "current"),
                self.target(100, 100, 140, 140, "upper left"),
            ],
            0,
            prototype.Direction.UP,
            ranked_indices=(1,),
            available_indices=(),
            outcome="orthogonal_step",
        )

        self.assertIsNotNone(diagnostic)
        assert diagnostic is not None
        self.assertIn(
            "请先横向或纵向对齐",
            prototype.format_navigation_diagnostic(diagnostic),
        )

    def test_navigation_diagnostic_marks_parent_grid_cell(self):
        targets = [
            self.target(100, 100, 300, 300, "parent"),
            self.target(120, 200, 180, 240, "child"),
        ]
        graph = prototype.NavigationGraph(targets)
        self.assertEqual(graph.candidates(0, prototype.Direction.LEFT)[0], 1)
        reverse = graph.candidates(1, prototype.Direction.RIGHT)
        diagnostic = prototype.build_navigation_diagnostic(
            targets,
            1,
            prototype.Direction.RIGHT,
            grid_rects=graph.grid_rects,
            ranked_indices=reverse,
            available_indices=reverse,
            selected_index=0,
            outcome="selected",
        )

        self.assertIsNotNone(diagnostic)
        assert diagnostic is not None
        self.assertEqual(diagnostic.candidates[0].route, "parent_cell")
        self.assertIn("父级单元格", prototype.format_navigation_diagnostic(diagnostic))

    def test_navigation_diagnostics_are_opt_in(self):
        self.assertFalse(prototype._parse_args([]).diagnostics)
        self.assertTrue(prototype._parse_args(["--diagnostics"]).diagnostics)

    def test_scan_can_target_a_specific_native_window(self):
        self.assertEqual(prototype._parse_args([]).window_handle, 0)
        self.assertEqual(
            prototype._parse_args(["--window-handle", "0x1234"]).window_handle,
            0x1234,
        )

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

    def test_navigation_graph_is_independent_of_query_order(self):
        targets = [
            self.target(100, 100, 300, 300, "parent"),
            self.target(120, 200, 180, 240, "child"),
        ]
        child_first = prototype.NavigationGraph(targets)
        self.assertEqual(
            child_first.candidates(1, prototype.Direction.RIGHT), (0,)
        )
        self.assertEqual(
            child_first.candidates(0, prototype.Direction.LEFT)[0], 1
        )
        self.assertEqual(
            child_first.candidates(1, prototype.Direction.RIGHT), (0,)
        )

        parent_first = prototype.NavigationGraph(targets)
        self.assertEqual(
            parent_first.candidates(0, prototype.Direction.LEFT)[0], 1
        )
        self.assertEqual(
            parent_first.candidates(1, prototype.Direction.RIGHT), (0,)
        )

    def test_hybrid_plan_keeps_an_existing_territory_route(self):
        targets = [
            self.target(20, 20, 80, 60, "left"),
            self.target(120, 20, 180, 60, "right"),
        ]
        graph = prototype.NavigationGraph(targets)

        plan = prototype.navigation_candidate_plan(
            graph,
            0,
            prototype.Direction.RIGHT,
            graph.anchor_rects[0],
        )

        self.assertEqual(plan.natural, (1,))
        self.assertEqual(plan.ranked, plan.natural)
        self.assertFalse(plan.orthogonal_step_required)
        self.assertFalse(plan.uses_xy_fallback)

    def test_xy_fallback_prefers_the_same_section_before_global_space(self):
        targets = [
            self.target(
                100,
                100,
                150,
                150,
                "current",
                section_path=(1,),
            ),
            self.target(
                500,
                100,
                550,
                150,
                "same section",
                section_path=(1,),
            ),
            self.target(
                220,
                100,
                270,
                150,
                "other section",
                section_path=(2,),
            ),
        ]
        graph = prototype.NavigationGraph(targets)

        self.assertEqual(
            graph.xy_focus_candidates(0, prototype.Direction.RIGHT),
            (1, 2),
        )

    def test_xy_fallback_fills_a_direction_without_a_territory_route(self):
        targets = [
            self.target(60, 220, 140, 260, "current"),
            self.target(420, 240, 440, 280, "lower right"),
        ]
        graph = prototype.NavigationGraph(targets)

        plan = prototype.navigation_candidate_plan(
            graph,
            0,
            prototype.Direction.DOWN,
            graph.anchor_rects[0],
        )

        self.assertEqual(plan.natural, ())
        self.assertEqual(plan.ranked, (1,))
        self.assertFalse(plan.orthogonal_step_required)
        self.assertTrue(plan.uses_xy_fallback)

    def test_xy_fallback_immediately_returns_in_the_opposite_direction(self):
        targets = [
            self.target(60, 220, 140, 260, "current"),
            self.target(420, 240, 440, 280, "lower right"),
        ]
        graph = prototype.NavigationGraph(targets)
        traversal = prototype.NavigationTraversal()
        plan = prototype.navigation_candidate_plan(
            graph,
            0,
            prototype.Direction.DOWN,
            graph.anchor_rects[0],
        )
        selected = traversal.available(
            0, prototype.Direction.DOWN, plan.ranked
        )[0]
        traversal.commit(selected, graph.anchor_rects[selected])
        reverse_plan = prototype.navigation_candidate_plan(
            graph,
            selected,
            prototype.Direction.UP,
            graph.anchor_rects[selected],
        )

        self.assertEqual(
            traversal.available(
                selected,
                prototype.Direction.UP,
                reverse_plan.ranked,
            )[0],
            0,
        )

    def test_xy_fallback_replaces_only_an_orthogonally_blocked_route(self):
        coordinates = (
            (460, 20, 560, 60),
            (300, 160, 340, 180),
            (60, 260, 80, 320),
            (360, 60, 400, 100),
            (480, 20, 520, 80),
            (400, 320, 440, 360),
            (0, 200, 120, 240),
        )
        targets = [
            self.target(left, top, right, bottom, str(index))
            for index, (left, top, right, bottom) in enumerate(coordinates)
        ]
        graph = prototype.NavigationGraph(targets)

        plan = prototype.navigation_candidate_plan(
            graph,
            3,
            prototype.Direction.LEFT,
            graph.anchor_rects[3],
        )

        self.assertEqual(plan.natural[0], 6)
        self.assertEqual(plan.ranked[0], 1)
        self.assertNotIn(plan.natural[0], plan.ranked)
        self.assertTrue(plan.orthogonal_step_required)
        self.assertTrue(plan.uses_xy_fallback)

    def test_xy_fallback_does_not_restore_the_blocked_natural_route(self):
        targets = [
            self.target(1107, 855, 1278, 909, "current"),
            self.target(426, 500, 603, 589, "blocked"),
            self.target(1063, 694, 1303, 787, "bridge"),
        ]
        graph = prototype.NavigationGraph(targets)

        plan = prototype.navigation_candidate_plan(
            graph,
            0,
            prototype.Direction.LEFT,
            graph.anchor_rects[0],
        )

        self.assertEqual(
            graph.xy_focus_candidates(0, prototype.Direction.LEFT),
            (1,),
        )
        self.assertEqual(plan.natural, (1,))
        self.assertEqual(plan.ranked, ())
        self.assertTrue(plan.orthogonal_step_required)
        self.assertFalse(plan.uses_xy_fallback)

    def test_reverse_history_does_not_restore_an_orthogonally_blocked_route(self):
        targets = [
            self.target(357, 554, 461, 592, "start"),
            self.target(593, 252, 782, 313, "upper"),
            self.target(482, 386, 549, 438, "bridge"),
            self.target(743, 591, 786, 649, "destination"),
        ]
        graph = prototype.NavigationGraph(targets)
        traversal = prototype.NavigationTraversal()
        current_cell = traversal.current_cell(
            0,
            graph.anchor_rects[0],
            prototype.Direction.DOWN,
        )
        forward_plan = prototype.navigation_candidate_plan(
            graph,
            0,
            prototype.Direction.DOWN,
            current_cell,
        )
        selected = traversal.available(
            0,
            prototype.Direction.DOWN,
            forward_plan.ranked,
            allow_previous_fallback=not forward_plan.orthogonal_step_required,
        )[0]
        self.assertEqual(selected, 3)
        traversal.commit(
            selected,
            prototype.navigation_contact_cell(
                current_cell,
                graph.grid_rects[selected],
                prototype.Direction.DOWN,
            ),
        )
        reverse_cell = traversal.current_cell(
            selected,
            graph.anchor_rects[selected],
            prototype.Direction.UP,
        )
        plan = prototype.navigation_candidate_plan(
            graph,
            selected,
            prototype.Direction.UP,
            reverse_cell,
        )

        self.assertEqual(plan.natural, (2, 1))
        self.assertTrue(plan.orthogonal_step_required)
        self.assertEqual(
            traversal.available(
                selected,
                prototype.Direction.UP,
                plan.ranked,
                allow_previous_fallback=not plan.orthogonal_step_required,
            ),
            (),
        )

    def test_random_hybrid_plans_never_rewire_a_usable_territory_route(self):
        generator = random.Random(914)
        for _case in range(120):
            targets = []
            for index in range(generator.randint(2, 25)):
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
                        path=(index,),
                        section_path=(generator.randint(0, 5),),
                    )
                )
            graph = prototype.NavigationGraph(targets)
            for current in range(len(targets)):
                for direction in prototype.Direction:
                    plan = prototype.navigation_candidate_plan(
                        graph,
                        current,
                        direction,
                        graph.anchor_rects[current],
                    )
                    if plan.natural and not plan.orthogonal_step_required:
                        self.assertEqual(plan.ranked, plan.natural)
                        self.assertFalse(plan.uses_xy_fallback)
                        continue
                    if not plan.ranked:
                        self.assertFalse(plan.uses_xy_fallback)
                        continue
                    self.assertTrue(plan.uses_xy_fallback)
                    self.assertTrue(
                        prototype._xy_focus_is_candidate(
                            graph.anchor_rects[current],
                            graph.anchor_rects[plan.ranked[0]],
                            direction,
                        )
                    )

    def test_projection_candidate_precedes_a_closer_territory_contact(self):
        targets = [
            self.target(657, 89, 770, 164, "directly above"),
            self.target(700, 266, 869, 290, "current"),
            self.target(539, 138, 673, 158, "upper left"),
        ]
        graph = prototype.NavigationGraph(targets)

        self.assertNotIn(
            0,
            [
                contact.target_index
                for contact in graph._contacts.get(
                    (1, prototype.Direction.UP), ()
                )
            ],
        )
        self.assertEqual(
            graph.candidates(
                1,
                prototype.Direction.UP,
                graph.anchor_rects[1],
            )[0],
            0,
        )

    def test_projection_priority_applies_in_all_four_directions(self):
        cases = (
            (
                prototype.Direction.UP,
                self.target(100, 220, 180, 260, "current"),
                self.target(90, 120, 190, 160, "projected"),
                self.target(210, 170, 260, 210, "diagonal"),
            ),
            (
                prototype.Direction.DOWN,
                self.target(100, 120, 180, 160, "current"),
                self.target(90, 220, 190, 260, "projected"),
                self.target(210, 170, 260, 210, "diagonal"),
            ),
            (
                prototype.Direction.LEFT,
                self.target(220, 100, 260, 180, "current"),
                self.target(120, 90, 160, 190, "projected"),
                self.target(170, 210, 210, 260, "diagonal"),
            ),
            (
                prototype.Direction.RIGHT,
                self.target(120, 100, 160, 180, "current"),
                self.target(220, 90, 260, 190, "projected"),
                self.target(170, 210, 210, 260, "diagonal"),
            ),
        )

        for direction, current, projected, diagonal in cases:
            with self.subTest(direction=direction):
                graph = prototype.NavigationGraph(
                    [current, projected, diagonal]
                )
                self.assertEqual(
                    graph.candidates(
                        0,
                        direction,
                        graph.anchor_rects[0],
                    )[0],
                    1,
                )

    def test_skeleton_promotes_a_nearer_offset_track_stably(self):
        base_rects = (
            (500, 100, 540, 140, "current"),
            (270, 155, 310, 195, "near"),
            (200, 100, 240, 140, "far"),
            (500, 240, 540, 280, "current support"),
            (270, 275, 310, 315, "near support"),
            (200, 240, 240, 280, "far support"),
        )

        for transpose in (False, True):
            direction = (
                prototype.Direction.UP
                if transpose
                else prototype.Direction.LEFT
            )
            for scale in (0.75, 1.0, 1.5):
                for reverse_order in (False, True):
                    coordinates = []
                    for left, top, right, bottom, name in base_rects:
                        if transpose:
                            left, top, right, bottom = top, left, bottom, right
                        coordinates.append(
                            (
                                round(left * scale),
                                round(top * scale),
                                round(right * scale),
                                round(bottom * scale),
                                name,
                            )
                        )
                    if reverse_order:
                        coordinates.reverse()
                    targets = [
                        self.target(left, top, right, bottom, name)
                        for left, top, right, bottom, name in coordinates
                    ]
                    graph = prototype.NavigationGraph(targets)
                    index_by_name = {
                        target.name: index for index, target in enumerate(targets)
                    }
                    current_index = index_by_name["current"]
                    current_cell = graph.anchor_rects[current_index]
                    traversal = prototype.NavigationTraversal()
                    plan = prototype.navigation_candidate_plan(
                        graph,
                        current_index,
                        direction,
                        current_cell,
                    )

                    with self.subTest(
                        transpose=transpose,
                        scale=scale,
                        reverse_order=reverse_order,
                    ):
                        selected = traversal.available(
                            current_index,
                            direction,
                            plan.ranked,
                        )[0]
                        self.assertEqual(targets[selected].name, "near")
                        next_cell = prototype.navigation_contact_cell(
                            current_cell,
                            graph.grid_rects[selected],
                            direction,
                        )
                        traversal.commit(selected, next_cell)
                        reverse = prototype.OPPOSITE_DIRECTION[direction]
                        reverse_plan = prototype.navigation_candidate_plan(
                            graph,
                            selected,
                            reverse,
                            next_cell,
                        )
                        returned = traversal.available(
                            selected,
                            reverse,
                            reverse_plan.ranked,
                        )[0]
                        self.assertEqual(targets[returned].name, "current")

    def test_skeleton_does_not_replace_a_reasonable_same_row_target(self):
        targets = [
            self.target(13, 67, 141, 115, "current"),
            self.target(231, 10, 305, 46, "near upper track"),
            self.target(344, 73, 381, 109, "same row"),
            self.target(13, 170, 141, 210, "current support"),
            self.target(231, 170, 305, 210, "upper track support"),
            self.target(344, 170, 381, 210, "same row support"),
        ]
        graph = prototype.NavigationGraph(targets)

        plan = prototype.navigation_candidate_plan(
            graph,
            0,
            prototype.Direction.RIGHT,
            graph.anchor_rects[0],
        )

        self.assertEqual(plan.ranked[0], 2)

    def test_skeleton_falls_back_to_an_existing_same_row_target(self):
        coordinates = (
            (402, 101, 443, 152),
            (148, 206, 210, 251),
            (253, 4, 308, 36),
            (203, -4, 241, 23),
            (101, 303, 152, 326),
            (301, -3, 368, 35),
            (103, 157, 153, 198),
            (105, 258, 137, 304),
            (354, 154, 406, 179),
            (402, 206, 469, 232),
            (194, 155, 258, 206),
            (155, 153, 201, 207),
        )
        targets = [
            self.target(
                left,
                top,
                right,
                bottom,
                str(index),
                path=(index,),
            )
            for index, (left, top, right, bottom) in enumerate(coordinates)
        ]
        graph = prototype.NavigationGraph(targets)

        plan = prototype.navigation_candidate_plan(
            graph,
            1,
            prototype.Direction.RIGHT,
            graph.anchor_rects[1],
        )

        self.assertEqual(plan.natural[:2], (9, 10))
        self.assertEqual(plan.ranked, plan.natural)
        self.assertFalse(plan.orthogonal_step_required)
        self.assertFalse(plan.uses_xy_fallback)

    def test_skeleton_uses_an_existing_same_lane_target_before_stopping(self):
        coordinates = (
            (1002, 672, 1210, 759),
            (552, 583, 605, 627),
            (1043, 282, 1225, 347),
            (433, 558, 555, 657),
            (441, 640, 484, 721),
            (521, 624, 613, 724),
        )
        names = ("3", "6", "10", "12", "13", "15")
        targets = [
            self.target(left, top, right, bottom, name, path=(index,))
            for index, (name, (left, top, right, bottom)) in enumerate(
                zip(names, coordinates)
            )
        ]
        graph = prototype.NavigationGraph(targets)

        plan = prototype.navigation_candidate_plan(
            graph,
            5,
            prototype.Direction.LEFT,
            graph.anchor_rects[5],
        )

        self.assertEqual(targets[plan.ranked[0]].name, "13")
        self.assertFalse(plan.orthogonal_step_required)
        self.assertFalse(plan.uses_xy_fallback)

    def test_skeleton_keeps_contained_row_actions_stable_under_scale(self):
        base_rects = (
            (13, 565, 406, 610, "current"),
            (258, 572, 289, 603, "inline action"),
            (13, 518, 406, 564, "current support"),
            (258, 526, 289, 556, "inline support"),
        )

        for scale in (0.75, 1.0, 1.25, 1.5):
            targets = [
                self.target(
                    round(left * scale),
                    round(top * scale),
                    round(right * scale),
                    round(bottom * scale),
                    name,
                )
                for left, top, right, bottom, name in base_rects
            ]
            graph = prototype.NavigationGraph(targets)

            with self.subTest(scale=scale):
                self.assertEqual(
                    graph._skeleton_candidates(
                        0,
                        prototype.Direction.RIGHT,
                        graph.anchor_rects[0],
                    ),
                    (1,),
                )

    def test_distant_element_cannot_create_local_skeleton_support(self):
        base_rects = (
            (100, 100, 140, 140, "current"),
            (103, 300, 143, 340, "candidate"),
            (160, 100, 200, 140, "current support"),
            (2100, 300, 2140, 340, "distant support"),
        )

        for scale in (0.75, 1.0, 1.5):
            for reverse_order in (False, True):
                coordinates = [
                    (
                        round(left * scale),
                        round(top * scale),
                        round(right * scale),
                        round(bottom * scale),
                        name,
                    )
                    for left, top, right, bottom, name in base_rects
                ]
                if reverse_order:
                    coordinates.reverse()
                targets = [
                    self.target(left, top, right, bottom, name)
                    for left, top, right, bottom, name in coordinates
                ]
                graph = prototype.NavigationGraph(targets)
                index_by_name = {
                    target.name: index for index, target in enumerate(targets)
                }
                current = index_by_name["current"]
                candidate = index_by_name["candidate"]

                with self.subTest(scale=scale, reverse_order=reverse_order):
                    self.assertTrue(
                        graph._has_skeleton_track_support(
                            current,
                            prototype.Direction.DOWN,
                        )
                    )
                    self.assertFalse(
                        graph._has_skeleton_track_support(
                            candidate,
                            prototype.Direction.DOWN,
                        )
                    )
                    self.assertNotIn(
                        candidate,
                        graph._skeleton_candidates(
                            current,
                            prototype.Direction.DOWN,
                            graph.anchor_rects[current],
                        ),
                    )

    def test_skeleton_support_overlap_is_stable_under_scale(self):
        for scale in (0.75, 1.0, 1.25, 1.5):
            targets = [
                self.target(
                    round(left * scale),
                    round(top * scale),
                    round(right * scale),
                    round(bottom * scale),
                    name,
                )
                for left, top, right, bottom, name in (
                    (100, 100, 140, 140, "current"),
                    (116, 160, 156, 200, "support"),
                )
            ]
            graph = prototype.NavigationGraph(targets)

            with self.subTest(scale=scale):
                self.assertTrue(
                    graph._has_skeleton_track_support(
                        0,
                        prototype.Direction.RIGHT,
                    )
                )

        tall_targets = [
            self.target(100, 100, 140, 140, "current"),
            self.target(120, -500, 160, 500, "tall unrelated target"),
        ]
        tall_graph = prototype.NavigationGraph(tall_targets)
        self.assertFalse(
            tall_graph._has_skeleton_track_support(
                0,
                prototype.Direction.RIGHT,
            )
        )

    def test_skeleton_sparse_exit_is_stable_under_scale(self):
        base_rects = (
            (280, 54, 344, 102),
            (708, 62, 812, 118),
            (944, 38, 1088, 94),
            (1172, 42, 1324, 74),
            (76, 166, 212, 238),
            (264, 174, 424, 206),
            (732, 170, 844, 210),
            (928, 166, 976, 246),
            (1148, 162, 1284, 202),
            (276, 290, 380, 330),
            (508, 282, 652, 314),
            (712, 294, 864, 350),
            (956, 302, 1092, 358),
            (1160, 290, 1272, 338),
            (284, 414, 332, 470),
            (728, 406, 872, 462),
            (956, 402, 1100, 466),
            (1144, 402, 1240, 442),
            (52, 538, 124, 570),
            (292, 518, 364, 550),
            (504, 538, 624, 594),
            (724, 522, 868, 562),
            (928, 538, 984, 602),
            (60, 650, 172, 714),
            (272, 654, 320, 718),
            (488, 662, 648, 710),
            (716, 638, 764, 686),
            (952, 662, 1064, 726),
            (1144, 646, 1224, 694),
            (60, 770, 140, 834),
            (268, 770, 308, 802),
            (708, 766, 812, 846),
            (928, 782, 976, 830),
            (1156, 766, 1276, 822),
        )

        for scale in (0.75, 1.0, 1.25, 1.5):
            targets = [
                self.target(
                    round(left * scale),
                    round(top * scale),
                    round(right * scale),
                    round(bottom * scale),
                    f"n{index}",
                )
                for index, (left, top, right, bottom) in enumerate(base_rects)
            ]
            graph = prototype.NavigationGraph(targets)
            plan = prototype.navigation_candidate_plan(
                graph,
                30,
                prototype.Direction.DOWN,
                graph.anchor_rects[30],
            )

            with self.subTest(scale=scale):
                self.assertEqual(targets[plan.ranked[0]].name, "n32")
                self.assertFalse(plan.orthogonal_step_required)
                self.assertTrue(plan.uses_xy_fallback)

    def test_skeleton_orthogonal_path_is_stable_under_rounding_scale(self):
        base_rects = (
            (504, 158, 560, 230),
            (276, 414, 404, 494),
            (504, 406, 568, 470),
            (68, 538, 164, 586),
            (268, 538, 332, 594),
            (500, 542, 604, 622),
            (56, 654, 176, 694),
            (296, 646, 440, 678),
        )
        names = ("n5", "n9", "n10", "n11", "n12", "n13", "n14", "n15")

        for scale in (0.75, 1.0, 1.25, 1.5):
            targets = [
                self.target(
                    round(left * scale),
                    round(top * scale),
                    round(right * scale),
                    round(bottom * scale),
                    name,
                )
                for name, (left, top, right, bottom) in zip(names, base_rects)
            ]
            graph = prototype.NavigationGraph(targets)
            plan = prototype.navigation_candidate_plan(
                graph,
                7,
                prototype.Direction.RIGHT,
                graph.anchor_rects[7],
            )

            with self.subTest(scale=scale):
                self.assertEqual(plan.ranked, ())
                self.assertTrue(plan.orthogonal_step_required)
                self.assertFalse(plan.uses_xy_fallback)

    def test_skeleton_orthogonal_path_uses_anchor_geometry_across_scale(self):
        base_rects = (
            (516, 162, 644, 242, "n6"),
            (76, 294, 204, 374, "n9"),
            (512, 286, 616, 358, "n10"),
            (76, 406, 116, 446, "n13"),
            (280, 410, 328, 474, "n14"),
            (60, 530, 100, 562, "n17"),
            (940, 526, 1100, 582, "n21"),
        )

        for scale in (0.75, 1.0, 1.25, 1.5):
            targets = [
                self.target(
                    round(left * scale),
                    round(top * scale),
                    round(right * scale),
                    round(bottom * scale),
                    name,
                )
                for left, top, right, bottom, name in base_rects
            ]
            graph = prototype.NavigationGraph(targets)
            current = next(
                index
                for index, target in enumerate(targets)
                if target.name == "n9"
            )
            plan = prototype.navigation_candidate_plan(
                graph,
                current,
                prototype.Direction.RIGHT,
                graph.anchor_rects[current],
            )

            with self.subTest(scale=scale):
                self.assertTrue(plan.ranked)
                self.assertEqual(targets[plan.ranked[0]].name, "n10")

    def test_skeleton_rounding_margin_scales_with_local_geometry(self):
        base_rects = (
            (956, 278, 1092, 342, "n15"),
            (288, 530, 344, 610, "n21"),
            (1172, 522, 1316, 594, "n25"),
            (56, 650, 152, 730, "n26"),
            (276, 662, 436, 742, "n27"),
            (1164, 782, 1252, 814, "n33"),
        )

        for scale in (0.75, 1.0, 1.25, 1.5):
            targets = [
                self.target(
                    round(left * scale),
                    round(top * scale),
                    round(right * scale),
                    round(bottom * scale),
                    name,
                )
                for left, top, right, bottom, name in base_rects
            ]
            graph = prototype.NavigationGraph(targets)
            plan = prototype.navigation_candidate_plan(
                graph,
                1,
                prototype.Direction.LEFT,
                graph.anchor_rects[1],
            )

            with self.subTest(scale=scale):
                self.assertTrue(plan.ranked)
                self.assertEqual(targets[plan.ranked[0]].name, "n26")
                self.assertFalse(plan.orthogonal_step_required)
                self.assertFalse(plan.uses_xy_fallback)

    def test_skeleton_blocks_xy_only_when_a_grid_step_exists(self):
        targets = [
            self.target(80, 100, 110, 130, "top left"),
            self.target(120, 100, 150, 130, "top right"),
            self.target(80, 140, 110, 170, "bottom left"),
            self.target(120, 140, 150, 170, "bottom right"),
            self.target(151, 150, 231, 190, "sparse target"),
        ]
        graph = prototype.NavigationGraph(targets)
        top_right = graph.anchor_rects[1]

        self.assertEqual(
            graph.candidates(1, prototype.Direction.RIGHT, top_right),
            (),
        )
        self.assertEqual(
            graph.xy_focus_candidates(1, prototype.Direction.RIGHT),
            (4,),
        )
        blocked = prototype.navigation_candidate_plan(
            graph,
            1,
            prototype.Direction.RIGHT,
            top_right,
        )
        self.assertEqual(blocked.ranked, ())
        self.assertTrue(blocked.orthogonal_step_required)
        self.assertFalse(blocked.uses_xy_fallback)

        down = prototype.navigation_candidate_plan(
            graph,
            1,
            prototype.Direction.DOWN,
            top_right,
        )
        self.assertEqual(down.ranked[0], 3)

        sparse_exit = prototype.navigation_candidate_plan(
            graph,
            3,
            prototype.Direction.DOWN,
            graph.anchor_rects[3],
        )
        self.assertEqual(sparse_exit.natural, ())
        self.assertEqual(sparse_exit.ranked, (4,))
        self.assertTrue(sparse_exit.uses_xy_fallback)

    def test_range_occupancy_grid_is_one_gapless_non_overlapping_cell_per_target(self):
        targets = [
            self.target(40, 30, 120, 70, "A"),
            self.target(240, 20, 300, 80, "B"),
            self.target(130, 120, 190, 160, "C"),
            self.target(20, 220, 100, 280, "D"),
            self.target(250, 210, 330, 270, "E"),
        ]
        graph = prototype.NavigationGraph(targets)
        territories = graph.grid_rects
        bounds = prototype.Rect(
            min(rect.left for rect in territories),
            min(rect.top for rect in territories),
            max(rect.right for rect in territories),
            max(rect.bottom for rect in territories),
        )

        self.assertEqual(len(territories), len(targets))
        self.assertTrue(all(rect.width > 0 and rect.height > 0 for rect in territories))
        self.assertTrue(
            all(
                prototype.territory_contains_anchor_center(territory, anchor)
                for territory, anchor in zip(territories, graph.anchor_rects)
            )
        )
        self.assertEqual(
            sum(rect.width * rect.height for rect in territories),
            bounds.width * bounds.height,
        )
        for first_index, first in enumerate(territories):
            for second in territories[first_index + 1 :]:
                overlap_width = min(first.right, second.right) - max(
                    first.left, second.left
                )
                overlap_height = min(first.bottom, second.bottom) - max(
                    first.top, second.top
                )
                self.assertFalse(overlap_width > 0 and overlap_height > 0)

    def test_deep_uia_sections_cannot_move_targets_away_from_their_screen_cells(self):
        coordinates = [
            (554, 218, 628, 254, "file"),
            (852, 276, 895, 319, "project"),
            (2345, 210, 2415, 263, "close"),
            (413, 1002, 658, 1048, "above"),
            (413, 1049, 658, 1094, "current"),
            (413, 1095, 658, 1141, "below"),
            (413, 1712, 806, 1757, "eagle"),
            (425, 1781, 713, 1819, "recent"),
        ]
        window = prototype.Rect(400, 200, 2420, 1820)
        targets = [
            self.target(
                left,
                top,
                right,
                bottom,
                name,
                section_path=(1, index, 0, 0, index),
                section_rect=window,
            )
            for index, (left, top, right, bottom, name) in enumerate(
                coordinates
            )
        ]
        graph = prototype.NavigationGraph(targets)

        self.assertTrue(
            all(
                prototype.territory_contains_anchor_center(territory, anchor)
                for territory, anchor in zip(
                    graph.grid_rects, graph.anchor_rects
                )
            )
        )
        expectations = [
            (0, prototype.Direction.DOWN),
            (1, prototype.Direction.LEFT),
            (4, prototype.Direction.UP),
            (4, prototype.Direction.DOWN),
            (6, prototype.Direction.UP),
            (6, prototype.Direction.DOWN),
        ]
        for current, direction in expectations:
            with self.subTest(target=targets[current].name, direction=direction):
                next_index = graph.candidates(
                    current, direction, graph.anchor_rects[current]
                )[0]
                self.assertIsNotNone(
                    prototype.direction_score(
                        graph.anchor_rects[current],
                        graph.anchor_rects[next_index],
                        direction,
                    )
                )

    def test_range_occupancy_routes_are_independent_of_target_scan_order(self):
        generator = random.Random(832)
        targets = [
            self.target(
                column * 140 + generator.randint(-8, 8),
                row * 90 + generator.randint(-6, 6),
                column * 140 + 90 + generator.randint(-8, 8),
                row * 90 + 54 + generator.randint(-6, 6),
                f"target-{row}-{column}",
                path=(0, row, column),
            )
            for row in range(4)
            for column in range(5)
        ]

        def first_neighbors(items):
            graph = prototype.NavigationGraph(items)
            result = {}
            for index, target in enumerate(items):
                result[target.name] = {
                    direction: (
                        items[candidates[0]].name if candidates else None
                    )
                    for direction in prototype.Direction
                    for candidates in [
                        graph.candidates(
                            index, direction, graph.anchor_rects[index]
                        )
                    ]
                }
            return result

        expected = first_neighbors(targets)
        for _case in range(20):
            shuffled = list(targets)
            generator.shuffle(shuffled)
            self.assertEqual(first_neighbors(shuffled), expected)

    def test_every_navigation_edge_stays_in_the_requested_real_direction(self):
        generator = random.Random(833)
        for _case in range(100):
            targets = []
            for index in range(generator.randint(2, 35)):
                left = generator.randint(0, 1800)
                top = generator.randint(0, 1000)
                width = generator.randint(24, 280)
                height = generator.randint(20, 120)
                targets.append(
                    self.target(
                        left,
                        top,
                        left + width,
                        top + height,
                        str(index),
                        section_path=(0, generator.randint(0, 8), index),
                    )
                )
            graph = prototype.NavigationGraph(targets)
            for current in range(len(targets)):
                for direction in prototype.Direction:
                    for candidate in graph.candidates(
                        current, direction, graph.anchor_rects[current]
                    ):
                        self.assertIsNotNone(
                            prototype.direction_score(
                                graph.anchor_rects[current],
                                graph.anchor_rects[candidate],
                                direction,
                            )
                        )

    def test_distant_target_does_not_rewire_existing_local_routes(self):
        targets = [
            self.target(
                column * 120,
                row * 80,
                column * 120 + 80,
                row * 80 + 50,
                f"{row},{column}",
            )
            for row in range(4)
            for column in range(5)
        ]
        original = prototype.NavigationGraph(targets)
        expanded_targets = [
            *targets,
            self.target(4000, 300, 4060, 360, "distant"),
        ]
        expanded = prototype.NavigationGraph(expanded_targets)

        for current in range(len(targets)):
            for direction in prototype.Direction:
                expected = original.candidates(
                    current, direction, original.anchor_rects[current]
                )[:1]
                actual = tuple(
                    index
                    for index in expanded.candidates(
                        current, direction, expanded.anchor_rects[current]
                    )
                    if index < len(targets)
                )[:1]
                self.assertEqual(actual, expected)

    def test_random_distant_target_does_not_rewire_local_routes(self):
        generator = random.Random(834)
        for _case in range(80):
            targets = []
            for index in range(generator.randint(8, 40)):
                left = generator.randint(0, 1600)
                top = generator.randint(0, 900)
                width = generator.randint(25, 240)
                height = generator.randint(20, 100)
                targets.append(
                    self.target(
                        left,
                        top,
                        left + width,
                        top + height,
                        str(index),
                        path=(index,),
                    )
                )
            original = prototype.NavigationGraph(targets)
            far_left = 6000 + generator.randint(0, 300)
            expanded = prototype.NavigationGraph(
                [
                    *targets,
                    self.target(
                        far_left,
                        generator.randint(0, 900),
                        far_left + generator.randint(30, 300),
                        generator.randint(901, 1100),
                        "distant",
                        path=(9999,),
                    ),
                ]
            )

            for current in range(len(targets)):
                for direction in prototype.Direction:
                    expected = original.candidates(
                        current,
                        direction,
                        original.anchor_rects[current],
                    )[:1]
                    actual = tuple(
                        index
                        for index in expanded.candidates(
                            current,
                            direction,
                            expanded.anchor_rects[current],
                        )
                        if index < len(targets)
                    )[:1]
                    self.assertEqual(actual, expected)

    def test_two_targets_with_the_same_center_share_the_split_boundary(self):
        targets = [
            self.target(100, 80, 300, 160, "wide"),
            self.target(150, 100, 250, 140, "nested"),
        ]
        graph = prototype.NavigationGraph(targets)

        self.assertTrue(
            all(
                prototype.territory_contains_anchor_center(territory, anchor)
                for territory, anchor in zip(
                    graph.grid_rects, graph.anchor_rects
                )
            )
        )
        self.assertFalse(graph.grid_rects[0].intersects(graph.grid_rects[1]))

    def test_same_visual_row_is_not_squeezed_into_thin_horizontal_bands(self):
        coordinates = [
            (2823, 369, 2892, 414, "minimize"),
            (2892, 369, 2961, 414, "maximize"),
            (2961, 369, 3030, 414, "close"),
            (2907, 424, 2952, 469, "filter"),
            (2964, 424, 3009, 469, "layout"),
            (894, 426, 1218, 468, "search"),
            (896, 482, 954, 515, "year"),
            (954, 482, 982, 515, "month"),
            (1151, 482, 1184, 515, "previous"),
            (2922, 490, 2967, 535, "more"),
            (2973, 490, 3018, 535, "close-detail"),
            (894, 494, 940, 541, "day-1"),
            (940, 494, 986, 541, "day-2"),
            (987, 494, 1033, 541, "day-3"),
            (1033, 494, 1079, 541, "day-4"),
            (1079, 494, 1125, 541, "day-5"),
            (1125, 494, 1171, 541, "day-6"),
            (1172, 494, 1218, 541, "day-7"),
            (2382, 494, 2514, 533, "adjust-date"),
            (820, 495, 868, 543, "sidebar"),
            (1258, 501, 2348, 615, "input"),
        ]
        graph = prototype.NavigationGraph(
            [
                self.target(left, top, right, bottom, name)
                for left, top, right, bottom, name in coordinates
            ]
        )

        self.assertGreaterEqual(
            min(
                min(territory.width, territory.height)
                for territory in graph.grid_rects
            ),
            20,
        )
        for current, expected in zip(range(11, 17), range(12, 18)):
            self.assertEqual(
                graph.candidates(
                    current,
                    prototype.Direction.RIGHT,
                    graph.anchor_rects[current],
                )[0],
                expected,
            )

    def test_outer_territory_growth_uses_real_centers_for_local_ranking(self):
        coordinates = [
            (-118, 124, -84, 201),
            (1390, -280, 1542, -215),
            (-852, 636, -675, 697),
            (-953, 673, -846, 747),
            (1477, 1177, 1594, 1222),
            (-894, 121, -799, 220),
            (-663, 528, -584, 635),
            (1715, 36, 1758, 133),
            (1060, 52, 1218, 124),
            (1175, 1173, 1447, 1210),
            (1420, 1173, 1671, 1273),
            (-788, 460, -622, 532),
            (647, 284, 812, 391),
            (-1124, 884, -919, 961),
            (284, -4, 562, 19),
            (899, 305, 1146, 400),
            (1238, 900, 1385, 927),
            (-826, 620, -740, 663),
            (-398, 727, -211, 757),
            (46, 933, 289, 1013),
            (641, -101, 842, -79),
            (362, 844, 500, 954),
        ]
        targets = [
            self.target(left, top, right, bottom, str(index))
            for index, (left, top, right, bottom) in enumerate(coordinates)
        ]
        original = prototype.NavigationGraph(targets)
        expanded = prototype.NavigationGraph(
            [
                *targets,
                self.target(9210, -207, 9256, 1447, "distant"),
            ]
        )

        expected = original.candidates(
            1, prototype.Direction.DOWN, original.anchor_rects[1]
        )[:1]
        self.assertTrue(expected)
        self.assertEqual(
            expanded.candidates(
                1, prototype.Direction.DOWN, expanded.anchor_rects[1]
            )[:1],
            expected,
        )

    def test_corner_and_short_edge_contacts_are_not_navigation_neighbors(self):
        anchors = (
            prototype.Rect(10, 10, 30, 30),
            prototype.Rect(60, 60, 80, 80),
        )
        corner_contacts = prototype.range_occupancy_navigation_contacts(
            (
                prototype.Rect(0, 0, 50, 50),
                prototype.Rect(50, 50, 100, 100),
            ),
            anchors,
        )
        short_edge_contacts = prototype.range_occupancy_navigation_contacts(
            (
                prototype.Rect(0, 0, 50, 50),
                prototype.Rect(50, 45, 100, 95),
            ),
            anchors,
        )

        self.assertEqual(corner_contacts, {})
        self.assertEqual(short_edge_contacts, {})

    def test_shared_edge_floor_does_not_consume_a_small_target(self):
        contacts = prototype.range_occupancy_navigation_contacts(
            (
                prototype.Rect(0, 0, 100, 100),
                prototype.Rect(100, 92, 200, 160),
            ),
            (
                prototype.Rect(20, 60, 60, 100),
                prototype.Rect(120, 92, 160, 108),
            ),
            scale_unit=40,
        )

        self.assertEqual(
            contacts[(0, prototype.Direction.RIGHT)][0].target_index,
            1,
        )

    def test_range_occupancy_grid_build_time_stays_bounded(self):
        generator = random.Random(831)

        def build_targets(count):
            targets = []
            for index in range(count):
                left = generator.randint(0, 3000)
                top = generator.randint(0, 1800)
                width = generator.randint(24, 280)
                height = generator.randint(20, 110)
                targets.append(
                    self.target(
                        left,
                        top,
                        left + width,
                        top + height,
                        str(index),
                    )
                )
            return targets

        started = time.perf_counter()
        prototype.NavigationGraph(build_targets(99))
        small_elapsed = time.perf_counter() - started
        started = time.perf_counter()
        prototype.NavigationGraph(build_targets(300))
        large_elapsed = time.perf_counter() - started

        self.assertLess(small_elapsed, 0.75)
        self.assertLess(large_elapsed, 2.5)

    def test_xy_fallback_lookup_is_cached_and_stays_bounded(self):
        generator = random.Random(913)
        targets = []
        for index in range(300):
            left = generator.randint(0, 3000)
            top = generator.randint(0, 1800)
            width = generator.randint(24, 280)
            height = generator.randint(20, 110)
            targets.append(
                self.target(
                    left,
                    top,
                    left + width,
                    top + height,
                    str(index),
                    path=(index,),
                    section_path=(generator.randint(0, 8),),
                )
            )
        graph = prototype.NavigationGraph(targets)

        started = time.perf_counter()
        for current in range(len(targets)):
            for direction in prototype.Direction:
                graph.xy_focus_candidates(current, direction)
        cold_elapsed = time.perf_counter() - started
        started = time.perf_counter()
        for current in range(len(targets)):
            for direction in prototype.Direction:
                graph.xy_focus_candidates(current, direction)
        cached_elapsed = time.perf_counter() - started

        self.assertLess(cold_elapsed, 1.5)
        self.assertLess(cached_elapsed, 0.05)

    def test_skeleton_lookup_for_300_targets_stays_bounded(self):
        targets = []
        for row in range(15):
            for column in range(20):
                left = column * 70 + (row % 3) * 2
                top = row * 55 + (column % 3) * 2
                targets.append(
                    self.target(
                        left,
                        top,
                        left + 40,
                        top + 30,
                        f"{row}:{column}",
                    )
                )
        graph = prototype.NavigationGraph(targets)

        started = time.perf_counter()
        for current in range(len(targets)):
            for direction in prototype.Direction:
                prototype.navigation_candidate_plan(
                    graph,
                    current,
                    direction,
                    graph.anchor_rects[current],
                )
        cold_elapsed = time.perf_counter() - started
        started = time.perf_counter()
        for current in range(len(targets)):
            for direction in prototype.Direction:
                prototype.navigation_candidate_plan(
                    graph,
                    current,
                    direction,
                    graph.anchor_rects[current],
                )
        cached_elapsed = time.perf_counter() - started

        self.assertLess(cold_elapsed, 4.0)
        self.assertLess(cached_elapsed, 0.1)

    def test_irregular_layout_keeps_every_target_in_directional_rankings(self):
        targets = [
            self.target(750, 0, 780, 30, "0"),
            self.target(650, 200, 680, 230, "1"),
            self.target(350, 50, 380, 80, "2"),
            self.target(750, 350, 780, 380, "3"),
            self.target(150, 450, 180, 480, "4"),
        ]
        graph = prototype.NavigationGraph(targets)
        reachable = set()
        for current in range(len(targets)):
            for direction in prototype.Direction:
                reachable.update(graph.candidates(current, direction))
        self.assertEqual(reachable, set(range(len(targets))))

    def test_random_grids_keep_every_cell_in_directional_rankings(self):
        generator = random.Random(828)
        for _case in range(120):
            targets = []
            for index in range(generator.randint(2, 25)):
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
                        path=(0, generator.randint(0, 5), index),
                    )
                )
            graph = prototype.NavigationGraph(targets)
            reachable = set()
            for current in range(len(targets)):
                for direction in prototype.Direction:
                    reachable.update(graph.candidates(current, direction))
            self.assertEqual(reachable, set(range(len(targets))))

    def test_fast_primary_grid_neighbor_matches_full_ranking(self):
        generator = random.Random(829)
        for _case in range(40):
            targets = []
            for index in range(generator.randint(2, 25)):
                left = generator.randint(0, 1200)
                top = generator.randint(0, 700)
                width = generator.randint(24, 240)
                height = generator.randint(20, 90)
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
            descendants = prototype.finer_descendant_index_map(targets)
            grid_rects = prototype.navigation_grid_rects(targets, descendants)
            for current in range(len(targets)):
                for direction in prototype.Direction:
                    ranked = prototype.ranked_target_indices(
                        targets,
                        current,
                        direction,
                        descendants,
                        grid_rects,
                    )
                    expected = ranked[0] if ranked else None
                    self.assertEqual(
                        prototype.best_grid_target_index(
                            targets,
                            current,
                            direction,
                            grid_rects,
                        ),
                        expected,
                    )

    def test_adjacent_message_actions_remain_directly_reachable(self):
        targets = [
            self.target(1300, 500, 1339, 539, "复制消息"),
            self.target(1341, 500, 1380, 539, "编辑消息"),
        ]
        graph = prototype.NavigationGraph(targets)
        self.assertEqual(
            graph.candidates(0, prototype.Direction.RIGHT)[0], 1
        )
        self.assertEqual(
            graph.candidates(1, prototype.Direction.LEFT)[0], 0
        )

    def test_window_edge_action_reaches_nearby_quicker_float_before_upper_copy(self):
        main_section = (1,)
        quicker_section = (2_000_000,)
        targets = [
            self.target(
                2106, 1746, 2149, 1788, "加入队列", section_path=main_section
            ),
            self.target(
                2186,
                1695,
                2254,
                1763,
                "Quicker 底部",
                section_path=quicker_section,
            ),
            self.target(
                2116, 982, 2155, 1021, "复制消息", section_path=main_section
            ),
            self.target(
                2116, 525, 2155, 564, "复制消息", section_path=main_section
            ),
            self.target(
                2186,
                1625,
                2254,
                1693,
                "Quicker 中部",
                section_path=quicker_section,
            ),
        ]
        graph = prototype.NavigationGraph(targets)

        self.assertEqual(
            graph.candidates(
                0, prototype.Direction.RIGHT, graph.anchor_rects[0]
            )[0],
            1,
        )
        self.assertEqual(
            graph.candidates(
                1, prototype.Direction.LEFT, graph.anchor_rects[1]
            )[0],
            0,
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
            section_rect=prototype.Rect(80, 60, 240, 300),
        )
        shifted = prototype.shifted_snapshot(target, 30, -20)
        self.assertEqual(shifted.rect, prototype.Rect(130, 80, 190, 120))
        self.assertEqual(
            shifted.section_rect, prototype.Rect(110, 40, 270, 280)
        )
        self.assertEqual(shifted.runtime_id, (7, 8, 9))
        self.assertEqual(shifted.source, "uia-point")
        self.assertEqual(prototype.shifted_point((112, 220), 30, -20), (142, 200))

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

    def test_raw_remote_direction_owned_by_downstream_is_not_navigation_input(self):
        for vk in (
            prototype.VK_UP,
            prototype.VK_DOWN,
            prototype.VK_LEFT,
            prototype.VK_RIGHT,
        ):
            with self.subTest(vk=vk):
                ownership = prototype.DirectionInputOwnership()
                call_next = mock.Mock(return_value=1)
                self.assertEqual(
                    ownership.route(
                        vk,
                        is_down=True,
                        is_up=False,
                        injected=False,
                        call_next=call_next,
                    ),
                    (True, 1),
                )
                call_next.assert_called_once_with()

    def test_raw_keyboard_direction_remains_navigation_input(self):
        for vk in (
            prototype.VK_UP,
            prototype.VK_DOWN,
            prototype.VK_LEFT,
            prototype.VK_RIGHT,
        ):
            with self.subTest(vk=vk):
                ownership = prototype.DirectionInputOwnership()
                call_next = mock.Mock(return_value=0)
                self.assertEqual(
                    ownership.route(
                        vk,
                        is_down=True,
                        is_up=False,
                        injected=False,
                        call_next=call_next,
                    ),
                    (False, 0),
                )
                call_next.assert_called_once_with()

    def test_injected_direction_stays_available_to_navigation(self):
        for vk in (
            prototype.VK_UP,
            prototype.VK_DOWN,
            prototype.VK_LEFT,
            prototype.VK_RIGHT,
        ):
            with self.subTest(vk=vk):
                ownership = prototype.DirectionInputOwnership()
                call_next = mock.Mock(return_value=1)
                self.assertEqual(
                    ownership.route(
                        vk,
                        is_down=True,
                        is_up=False,
                        injected=True,
                        call_next=call_next,
                    ),
                    (False, 0),
                )
                call_next.assert_not_called()

    def test_forwarded_direction_release_reaches_downstream_and_clears_state(self):
        for vk in (
            prototype.VK_UP,
            prototype.VK_DOWN,
            prototype.VK_LEFT,
            prototype.VK_RIGHT,
        ):
            with self.subTest(vk=vk):
                ownership = prototype.DirectionInputOwnership()
                call_next = mock.Mock(side_effect=(1, 0, 0))
                ownership.route(
                    vk,
                    is_down=True,
                    is_up=False,
                    injected=False,
                    call_next=call_next,
                )
                self.assertEqual(
                    ownership.route(
                        vk,
                        is_down=True,
                        is_up=False,
                        injected=False,
                        call_next=call_next,
                    ),
                    (True, 0),
                )
                self.assertEqual(
                    ownership.route(
                        vk,
                        is_down=False,
                        is_up=True,
                        injected=False,
                        call_next=call_next,
                    ),
                    (True, 0),
                )
                self.assertEqual(call_next.call_count, 3)
                self.assertFalse(ownership.has_forwarded_down(vk))
                self.assertEqual(
                    ownership.route(
                        vk,
                        is_down=False,
                        is_up=True,
                        injected=False,
                        call_next=call_next,
                    ),
                    (False, 0),
                )

    def test_tracks_only_relevant_structure_changes(self):
        self.assertTrue(prototype.is_navigation_structure_event(0x8000))
        self.assertTrue(prototype.is_navigation_structure_event(0x800A))
        self.assertTrue(
            prototype.is_navigation_structure_event(
                prototype.EVENT_OBJECT_LOCATIONCHANGE
            )
        )
        self.assertFalse(prototype.is_navigation_structure_event(0x8005))

        event = prototype.EVENT_OBJECT_LOCATIONCHANGE
        self.assertTrue(
            prototype.navigation_structure_event_affects_targets(event, -4)
        )
        self.assertFalse(
            prototype.navigation_structure_event_affects_targets(
                event, prototype.OBJID_CARET
            )
        )
        self.assertFalse(
            prototype.navigation_structure_event_affects_targets(
                event, prototype.OBJID_CURSOR
            )
        )

        now = [10.0]
        tracker = prototype.DirtyWindowTracker(lambda: now[0])
        self.assertTrue(tracker.watch(100, 42))
        self.assertFalse(tracker.watch(100, 42))
        self.assertFalse(tracker.mark(200, 42))
        self.assertFalse(tracker.mark(100, 43))
        self.assertTrue(tracker.mark(100, 42))
        first = tracker.state(100, 42)
        self.assertEqual(first, prototype.DirtyWindowState(1, 10.0))

        now[0] = 11.0
        self.assertTrue(tracker.mark(100, 42))
        self.assertTrue(
            tracker.consume(
                100,
                42,
                through_generation=first.generation if first is not None else 0,
            )
        )
        self.assertEqual(
            tracker.state(100, 42), prototype.DirtyWindowState(2, 11.0)
        )
        self.assertTrue(tracker.consume(100, 42))
        self.assertIsNone(tracker.state(100, 42))

        tracker.watch(300, 99)
        self.assertIsNone(tracker.state(100, 42))

    def test_suspicious_moves_only_request_a_later_refresh(self):
        window = prototype.Rect(0, 0, 1600, 900)
        current = prototype.Rect(600, 400, 660, 440)
        self.assertFalse(
            prototype.move_should_refresh_dynamic_targets(
                current,
                prototype.Rect(700, 400, 760, 440),
                prototype.Direction.RIGHT,
                window,
            )
        )
        self.assertTrue(
            prototype.move_should_refresh_dynamic_targets(
                current,
                prototype.Rect(1200, 100, 1260, 140),
                prototype.Direction.RIGHT,
                window,
            )
        )
        self.assertTrue(
            prototype.move_should_refresh_dynamic_targets(
                current,
                prototype.Rect(1100, 400, 1160, 440),
                prototype.Direction.RIGHT,
                window,
            )
        )
        self.assertTrue(
            prototype.move_should_refresh_dynamic_targets(
                current,
                prototype.Rect(300, 450, 360, 490),
                prototype.Direction.RIGHT,
                window,
            )
        )

        dirty = prototype.DirtyWindowState(1, 20.0)
        self.assertFalse(
            prototype.background_refresh_due(
                dirty,
                20.1,
                1.0,
            )
        )
        self.assertTrue(
            prototype.background_refresh_due(
                dirty,
                20.2,
                1.0,
            )
        )
        self.assertTrue(
            prototype.background_refresh_due(
                None,
                20.2,
                1.0,
                requested=True,
                input_idle_for=0.15,
            )
        )
        self.assertFalse(
            prototype.background_refresh_due(
                None,
                20.2,
                31.0,
                requested=True,
                input_idle_for=0.149,
            )
        )
        newly_changed = prototype.DirtyWindowState(2, 20.19)
        self.assertFalse(
            prototype.background_refresh_due(
                newly_changed,
                20.2,
                31.0,
                requested=True,
            )
        )
        self.assertFalse(prototype.background_refresh_due(None, 20.2, 29.9))
        self.assertTrue(prototype.background_refresh_due(None, 20.2, 30.0))

    def test_dynamic_refresh_fallback_has_a_maximum_cache_age(self):
        self.assertFalse(prototype.dynamic_refresh_fallback_due(4.9, True))
        self.assertTrue(prototype.dynamic_refresh_fallback_due(5.0, True))
        self.assertFalse(prototype.dynamic_refresh_fallback_due(29.9, False))
        self.assertTrue(prototype.dynamic_refresh_fallback_due(30.0, False))

    def test_follow_window_scan_uses_a_short_cooperative_budget(self):
        self.assertGreater(prototype.FOLLOW_WINDOW_SCAN_BUDGET_SECONDS, 0.0)
        self.assertLessEqual(prototype.FOLLOW_WINDOW_SCAN_BUDGET_SECONDS, 0.2)
        self.assertEqual(
            prototype.bounded_scan_timeout_ms(None, 100, now=10.0), 100
        )
        self.assertEqual(
            prototype.bounded_scan_timeout_ms(9.0, 100, now=10.0), 0
        )
        self.assertEqual(
            prototype.bounded_scan_timeout_ms(10.001, 100, now=10.0), 1
        )
        self.assertEqual(
            prototype.bounded_scan_timeout_ms(11.0, 100, now=10.0), 100
        )

    def test_interrupted_follow_scan_can_commit_partial_targets(self):
        self.assertEqual(
            prototype.scan_commit_decision(3, 3, True, False, True),
            (True, True),
        )
        self.assertEqual(
            prototype.scan_commit_decision(3, 3, False, True, True),
            (True, True),
        )
        self.assertEqual(
            prototype.scan_commit_decision(3, 3, True, False, False),
            (False, False),
        )

    def test_empty_follow_refresh_has_a_bounded_retry_window(self):
        self.assertEqual(prototype.FOLLOW_WINDOW_EMPTY_REFRESH_RETRIES, 2)
        self.assertTrue(
            prototype.empty_follow_refresh_should_retry(200, 200, 0)
        )
        self.assertTrue(
            prototype.empty_follow_refresh_should_retry(200, 200, 1)
        )
        self.assertFalse(
            prototype.empty_follow_refresh_should_retry(200, 200, 2)
        )
        self.assertFalse(
            prototype.empty_follow_refresh_should_retry(100, 200, 0)
        )

    def test_stale_scan_result_never_reactivates_navigation(self):
        self.assertEqual(
            prototype.scan_commit_decision(3, 4, False, False, True),
            (False, False),
        )
        self.assertEqual(
            prototype.scan_commit_decision(3, 4, True, True, True),
            (False, False),
        )
        self.assertEqual(
            prototype.scan_commit_decision(3, 3, False, False, False),
            (True, False),
        )
        self.assertTrue(prototype.scan_event_is_current(4, 4, True))
        self.assertFalse(prototype.scan_event_is_current(3, 4, True))
        self.assertFalse(prototype.scan_event_is_current(4, 4, False))
        self.assertFalse(prototype.scan_event_is_current(0, 0, True))

    def test_managed_companion_intercepts_scan_keys_and_hides_dev_hotkeys(self):
        source = WINDOWS_HOST_PATH.read_text(encoding="utf-8")
        self.assertIn("intercepting.set()", source)
        self.assertIn("intercepting.clear()", source)
        self.assertIn(
            "if self._intercepting.is_set() and action is not None:",
            source,
        )

        tree = ast.parse(source)
        functions = {
            node.name: node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
        }
        for function_name in (
            "prepare_navigation_action",
            "monitor_navigation_context",
        ):
            force_refreshes = [
                call
                for call in ast.walk(functions[function_name])
                if isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "refresh_navigation_overlay_signature"
                and any(
                    keyword.arg == "force"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is True
                    for keyword in call.keywords
                )
            ]
            self.assertEqual(len(force_refreshes), 1, function_name)
        self.assertIn(
            "include_developer_hotkeys = not managed_companion or diagnostics_enabled",
            source,
        )
        self.assertIn("def owner_process_is_alive(process_id: int)", source)
        self.assertIn("owner_timer.start(500)", source)
        self.assertIn("QTimer.singleShot(0, monitor_owner_process)", source)
        self.assertIn("if vk in self._passthrough:", source)
        self.assertIn("if is_down and action is not None:", source)
        self.assertIn("self._passthrough.add(vk)", source)

    def test_overlay_signature_checks_are_rate_limited(self):
        self.assertTrue(prototype.periodic_check_due(10.0, 0.0, 1.0))
        self.assertFalse(prototype.periodic_check_due(10.5, 10.0, 1.0))
        self.assertTrue(prototype.periodic_check_due(11.0, 10.0, 1.0))

    def test_worker_scan_contract_is_budgeted_and_generation_guarded(self):
        tree = ast.parse(WINDOWS_HOST_PATH.read_text(encoding="utf-8"))
        functions = {
            node.name: node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
        }
        automation_worker = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef)
            and node.name == "AutomationWorker"
        )
        worker_functions = {
            node.name: node
            for node in automation_worker.body
            if isinstance(node, ast.FunctionDef)
        }
        scan_function = worker_functions["_scan"]
        used_cache_branch = next(
            node
            for node in scan_function.body
            if isinstance(node, ast.If)
            and isinstance(node.test, ast.Name)
            and node.test.id == "used_cache"
        )
        self.assertIsInstance(used_cache_branch.body[0], ast.With)
        cache_lock = used_cache_branch.body[0]
        cache_lock_attributes = {
            node.attr
            for node in ast.walk(cache_lock)
            if isinstance(node, ast.Attribute)
            and isinstance(node.ctx, ast.Store)
        }
        self.assertIn("context_valid", cache_lock_attributes)
        self.assertTrue(
            any(
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "clear"
                and isinstance(call.func.value, ast.Attribute)
                and call.func.value.attr == "invalid_targets"
                for call in ast.walk(cache_lock)
            )
        )
        enumerate_function = worker_functions["_enumerate"]
        enumerate_args = {
            argument.arg for argument in enumerate_function.args.args
        }
        self.assertIn("allow_partial", enumerate_args)
        self.assertIn("expected_generation", enumerate_args)

        guarded_commit = False
        for with_node in (
            node
            for node in ast.walk(enumerate_function)
            if isinstance(node, ast.With)
        ):
            calls = {
                call.func.id
                for call in ast.walk(with_node)
                if isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
            }
            assigned_attributes = {
                target.attr
                for assignment in ast.walk(with_node)
                if isinstance(assignment, ast.Assign)
                for target in assignment.targets
                if isinstance(target, ast.Attribute)
            }
            if (
                "scan_commit_decision" in calls
                and "context_valid" in assigned_attributes
            ):
                guarded_commit = True
                break
        self.assertTrue(guarded_commit)

        follow_function = worker_functions["_follow_window"]
        enumerate_calls = [
            call
            for call in ast.walk(follow_function)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "_enumerate"
        ]
        self.assertEqual(len(enumerate_calls), 1)
        keywords = {
            keyword.arg: keyword.value
            for keyword in enumerate_calls[0].keywords
            if keyword.arg is not None
        }
        self.assertIn("deadline", keywords)
        self.assertIn("expected_generation", keywords)
        self.assertIsInstance(keywords.get("allow_partial"), ast.Constant)
        self.assertTrue(keywords["allow_partial"].value)
        self.assertTrue(
            any(
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "_request_background_refresh"
                for call in ast.walk(follow_function)
            )
        )

        refresh_function = worker_functions["_refresh_targets"]
        refresh_enumerations = [
            call
            for call in ast.walk(refresh_function)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "_enumerate"
        ]
        self.assertEqual(len(refresh_enumerations), 1)
        self.assertIn(
            "commit_empty",
            {
                keyword.arg
                for keyword in refresh_enumerations[0].keywords
                if keyword.arg is not None
            },
        )

        handle_function = functions["handle_keyboard_action"]
        self.assertTrue(
            any(
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "prepare_navigation_action"
                for call in ast.walk(handle_function)
            )
        )
        run_function = worker_functions["_run"]
        self.assertTrue(
            any(
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "_defer_move"
                for call in ast.walk(run_function)
            )
        )

        chromium_activation = functions[
            "activate_embedded_chromium_accessibility"
        ]
        self.assertTrue(
            any(
                isinstance(attribute, ast.Attribute)
                and isinstance(attribute.value, ast.Name)
                and attribute.value.id == "result"
                and attribute.attr == "value"
                for attribute in ast.walk(chromium_activation)
            )
        )

    def test_global_hotkey_maps_the_diagnostics_toggle(self):
        self.assertEqual(
            prototype.global_hotkey_action(prototype.VK_D),
            "toggle_diagnostics",
        )
        self.assertEqual(prototype.global_hotkey_action(prototype.VK_N), "toggle")
        self.assertEqual(prototype.global_hotkey_action(prototype.VK_Q), "quit")
        self.assertIsNone(prototype.global_hotkey_action(0x70))
        self.assertEqual(
            prototype.global_hotkey_action(
                prototype.VK_N,
                include_developer_actions=False,
            ),
            "toggle",
        )
        self.assertIsNone(
            prototype.global_hotkey_action(
                prototype.VK_D,
                include_developer_actions=False,
            )
        )
        self.assertIsNone(
            prototype.global_hotkey_action(
                prototype.VK_Q,
                include_developer_actions=False,
            )
        )

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

    def test_content_refresh_follows_context_double_click_or_scroll(self):
        self.assertEqual(prototype.content_refresh_delay_ms("contexted"), 120)
        self.assertEqual(
            prototype.content_refresh_delay_ms("activated", True), 180
        )
        self.assertEqual(
            prototype.content_refresh_delay_ms("activated", False), 0
        )
        self.assertEqual(prototype.content_refresh_delay_ms("scrolled"), 180)

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

    def test_navigation_overlay_label_omits_position_counter(self):
        target = self.target(100, 100, 160, 140, "编辑消息")

        self.assertEqual(
            prototype.navigation_overlay_label(target),
            "编辑消息",
        )

    def test_navigation_overlay_label_keeps_meaningful_state(self):
        target = self.target(
            100,
            100,
            160,
            140,
            "动作",
            source="msaa",
        )

        self.assertEqual(
            prototype.navigation_overlay_label(target, 1, 3),
            "动作  层级 2/3  MSAA",
        )

    def test_owner_chain_follows_a_nested_popup_without_looping(self):
        owners = {30: 20, 20: 10, 10: 0, 40: 40}
        owner_of = lambda hwnd: owners.get(hwnd, 0)
        self.assertTrue(prototype.owner_chain_contains(30, 10, owner_of))
        self.assertFalse(prototype.owner_chain_contains(10, 30, owner_of))
        self.assertFalse(prototype.owner_chain_contains(40, 10, owner_of))

    def test_promotes_repeated_named_content_rows_without_promoting_layout_groups(self):
        window = prototype.Rect(0, 0, 1000, 800)
        parent = self.element(
            20,
            20,
            360,
            500,
            "会话列表",
            "PaneControl",
            (0,),
        )
        rows = []
        for index, name in enumerate(("张三", "李四", "项目群")):
            top = 40 + index * 72
            rows.extend(
                [
                    self.element(
                        30,
                        top,
                        350,
                        top + 64,
                        "",
                        "GroupControl",
                        (0, index),
                        has_direct_action_pattern=True,
                    ),
                    self.element(
                        92,
                        top + 12,
                        210,
                        top + 36,
                        name,
                        "TextControl",
                        (0, index, 0),
                    ),
                ]
            )

        specs = prototype.repeated_content_target_specs(
            [parent, *rows], window
        )
        self.assertEqual(len(specs), 3)
        self.assertEqual(
            [spec.snapshot.control_type for spec in specs],
            ["ContentItemControl"] * 3,
        )
        self.assertEqual(
            [spec.snapshot.name for spec in specs],
            ["张三", "李四", "项目群"],
        )
        self.assertEqual(specs[0].click_point, (151, 64))

        passive_rows = [
            self.element(
                30,
                40 + index * 52,
                950,
                80 + index * 52,
                "",
                "GroupControl",
                (2, index),
                keyboard_focusable=(index == 1),
                has_legacy_pattern=True,
            )
            for index in range(3)
        ]
        passive_text = [
            self.element(
                40,
                48 + index * 52,
                940,
                72 + index * 52,
                f"状态文字 {index}",
                path=(2, index, 0),
            )
            for index in range(3)
        ]
        passive_parent = self.element(
            20,
            20,
            970,
            240,
            "状态区",
            "PaneControl",
            (2,),
        )
        self.assertEqual(
            prototype.repeated_content_target_specs(
                [passive_parent, *passive_rows, *passive_text], window
            ),
            [],
        )

        layout = self.element(
            400,
            20,
            900,
            500,
            "普通布局",
            "GroupControl",
            (1,),
        )
        two_cards = [
            self.element(420, 50, 640, 180, "", "GroupControl", (1, 0)),
            self.element(660, 50, 880, 180, "", "GroupControl", (1, 1)),
            self.element(440, 80, 560, 110, "卡片一", path=(1, 0, 0)),
            self.element(680, 80, 800, 110, "卡片二", path=(1, 1, 0)),
        ]
        self.assertEqual(
            prototype.repeated_content_target_specs(
                [layout, *two_cards], window
            ),
            [],
        )

    def test_keeps_the_menu_half_of_a_wrapped_split_button(self):
        window = prototype.Rect(0, 0, 1000, 800)
        parent = self.element(
            700, 650, 825, 698, "", "GroupControl", (0,)
        )
        main_half = self.element(
            700,
            650,
            790,
            698,
            "",
            "GroupControl",
            (0, 0),
            has_direct_action_pattern=True,
        )
        disabled_send = self.element(
            724,
            653,
            766,
            695,
            "发送",
            "ButtonControl",
            (0, 0, 0),
            enabled=False,
        )
        menu_half = self.element(
            791,
            650,
            825,
            698,
            "",
            "GroupControl",
            (0, 1),
            has_direct_action_pattern=True,
        )
        menu_icon = self.element(
            803, 660, 825, 682, "", "ImageControl", (0, 1, 0)
        )

        specs = prototype.split_button_companion_target_specs(
            [parent, main_half, disabled_send, menu_half, menu_icon],
            window,
        )

        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0].snapshot.rect, menu_half.rect)
        self.assertEqual(specs[0].snapshot.name, "发送的更多选项")
        self.assertEqual(specs[0].snapshot.source, "uia-split-action")
        self.assertEqual(specs[0].click_point, (808, 674))

    def test_split_button_companion_also_survives_when_main_action_is_enabled(self):
        window = prototype.Rect(0, 0, 1000, 800)
        elements = [
            self.element(700, 650, 825, 698, "", "GroupControl", (0,)),
            self.element(
                700,
                650,
                790,
                698,
                "",
                "GroupControl",
                (0, 0),
                has_direct_action_pattern=True,
            ),
            self.element(
                724, 653, 766, 695, "发送", "ButtonControl", (0, 0, 0)
            ),
            self.element(
                791,
                650,
                825,
                698,
                "",
                "GroupControl",
                (0, 1),
                has_direct_action_pattern=True,
            ),
        ]

        self.assertEqual(
            len(prototype.split_button_companion_target_specs(elements, window)),
            1,
        )

    def test_does_not_promote_anonymous_action_groups_without_a_split_button(self):
        window = prototype.Rect(0, 0, 1000, 800)
        layout_groups = [
            self.element(600, 200, 690, 248, "", "GroupControl", (0, 0)),
            self.element(
                691,
                200,
                725,
                248,
                "",
                "GroupControl",
                (0, 1),
                has_direct_action_pattern=True,
            ),
            self.element(612, 212, 660, 236, "状态", path=(0, 0, 0)),
        ]

        self.assertEqual(
            prototype.split_button_companion_target_specs(layout_groups, window),
            [],
        )

    def test_opaque_visual_surface_requires_a_legacy_list_like_pane(self):
        window = prototype.Rect(0, 0, 1200, 800)
        pane = self.element(
            100,
            80,
            600,
            700,
            "文件列表",
            "PaneControl",
            (0,),
            keyboard_focusable=True,
            has_legacy_pattern=True,
        )
        header = self.element(
            100,
            80,
            600,
            116,
            "名称",
            "HeaderControl",
            (0, 0),
        )
        surfaces = prototype.opaque_visual_surfaces(
            [pane, header], [], window
        )
        self.assertEqual(
            surfaces,
            [
                prototype.OpaqueVisualSurface(
                    prototype.Rect(100, 116, 600, 700), (0,), "文件列表"
                )
            ],
        )

        ordinary_pane = self.element(
            650,
            80,
            1150,
            700,
            "普通内容",
            "PaneControl",
            (1,),
            keyboard_focusable=True,
            has_legacy_pattern=True,
        )
        self.assertEqual(
            prototype.opaque_visual_surfaces([ordinary_pane], [], window),
            [],
        )

        covered_target = prototype.TargetSnapshot(
            prototype.Rect(100, 116, 600, 500),
            "真实文件列表",
            "ListControl",
            path=(0, 1),
            has_action_pattern=True,
        )
        self.assertEqual(
            prototype.opaque_visual_surfaces(
                [pane, header], [covered_target], window
            ),
            [],
        )

    def test_visual_fallback_detects_regular_detail_rows(self):
        width, height = 200, 220
        rgb = self.rgb_image(
            width,
            height,
            [
                (20, 10, 100, 14),
                (20, 26, 100, 30),
                (20, 42, 100, 46),
                (20, 86, 100, 90),
                (20, 102, 100, 106),
                (20, 118, 100, 122),
                (20, 134, 100, 138),
                (20, 182, 60, 186),
                (20, 192, 60, 196),
            ],
        )
        specs = prototype.visual_grid_target_specs(
            rgb,
            width,
            height,
            width * 3,
            prototype.Rect(0, 0, width, height),
            [prototype.OpaqueVisualSurface(prototype.Rect(0, 0, width, height), (0,))],
        )
        self.assertEqual(len(specs), 7)
        self.assertEqual(
            [spec.snapshot.control_type for spec in specs],
            ["VisualItemControl"] * 7,
        )
        self.assertEqual(
            [point.click_point[1] for point in specs],
            [12, 28, 44, 88, 104, 120, 136],
        )

    def test_visual_fallback_detects_a_thumbnail_grid_by_cells(self):
        width, height = 200, 100
        rgb = self.rgb_image(
            width,
            height,
            [
                (10, 10, 30, 20),
                (80, 10, 100, 20),
                (10, 60, 30, 70),
                (80, 60, 100, 70),
            ],
        )
        specs = prototype.visual_grid_target_specs(
            rgb,
            width,
            height,
            width * 3,
            prototype.Rect(0, 0, width, height),
            [prototype.OpaqueVisualSurface(prototype.Rect(0, 0, width, height), (0,))],
        )
        self.assertEqual(len(specs), 4)
        self.assertEqual(len({spec.click_point for spec in specs}), 4)
        self.assertTrue(
            all(spec.snapshot.source == "visual-grid" for spec in specs)
        )

    def test_overlay_requires_a_real_window_relationship(self):
        self.assertTrue(
            prototype.overlay_window_is_related(
                100,
                100,
                candidate_owned_by_root=False,
                root_owned_by_candidate=False,
                extended_style=0,
            )
        )
        self.assertTrue(
            prototype.overlay_window_is_related(
                200,
                100,
                candidate_owned_by_root=True,
                root_owned_by_candidate=False,
                extended_style=0,
            )
        )
        for style in (
            prototype.WS_EX_TOPMOST,
            prototype.WS_EX_TOOLWINDOW,
            prototype.WS_EX_NOACTIVATE,
        ):
            self.assertTrue(
                prototype.overlay_window_is_related(
                    200,
                    100,
                    candidate_owned_by_root=False,
                    root_owned_by_candidate=False,
                    extended_style=style,
                )
            )
        self.assertFalse(
            prototype.overlay_window_is_related(
                200,
                100,
                candidate_owned_by_root=False,
                root_owned_by_candidate=False,
                extended_style=0,
            )
        )

    def test_overlay_must_be_small_visible_and_mostly_over_the_root(self):
        root = prototype.Rect(100, 100, 1100, 900)
        accepted = dict(
            visible=True,
            minimized=False,
            cloaked=False,
            related=True,
        )
        self.assertTrue(
            prototype.overlay_window_is_candidate(
                root, prototype.Rect(700, 180, 1000, 380), **accepted
            )
        )
        self.assertFalse(
            prototype.overlay_window_is_candidate(
                root, prototype.Rect(850, 180, 1250, 380), **accepted
            )
        )
        self.assertFalse(
            prototype.overlay_window_is_candidate(
                root, prototype.Rect(150, 150, 1000, 700), **accepted
            )
        )
        self.assertFalse(
            prototype.overlay_window_is_candidate(
                root,
                prototype.Rect(700, 180, 1000, 380),
                **{**accepted, "related": False},
            )
        )

    def test_only_associated_quicker_overlay_can_sit_far_outside_root(self):
        root = prototype.Rect(100, 100, 1100, 900)
        outside = prototype.Rect(1200, 200, 1268, 268)
        nearby = prototype.Rect(1120, 200, 1188, 268)
        common = dict(
            visible=True,
            minimized=False,
            cloaked=False,
            related=True,
        )
        self.assertTrue(
            prototype.overlay_window_is_candidate(
                root,
                outside,
                explicitly_associated=True,
                **common,
            )
        )
        self.assertFalse(
            prototype.overlay_window_is_candidate(
                root,
                outside,
                trusted_small_overlay=True,
                **common,
            )
        )
        self.assertTrue(
            prototype.overlay_window_is_candidate(
                root,
                nearby,
                trusted_small_overlay=True,
                **common,
            )
        )
        self.assertFalse(
            prototype.overlay_window_is_candidate(root, outside, **common)
        )

        codex_root = prototype.Rect(320, 278, 1998, 1850)
        linked_stack = prototype.Rect(1847, 1367, 1915, 1435)
        unrelated_desktop_action = prototype.Rect(3317, 175, 3437, 295)
        self.assertTrue(
            prototype.overlay_window_is_candidate(
                codex_root,
                linked_stack,
                trusted_small_overlay=True,
                **common,
            )
        )
        self.assertFalse(
            prototype.overlay_window_is_candidate(
                codex_root,
                unrelated_desktop_action,
                trusted_small_overlay=True,
                **common,
            )
        )

    def test_reads_quicker_process_association_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "quicker-navigation.json"
            path.write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "hwnd": 1234,
                                "isBound": True,
                                "bindProcessName": "C:/Apps/Codex.exe",
                                "visible": True,
                            },
                            {
                                "hwnd": 5678,
                                "isBound": False,
                                "bindProcessName": "QQ.exe",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            associations = prototype.load_quicker_overlay_associations(str(path))

        self.assertEqual(set(associations), {1234})
        self.assertTrue(
            prototype.quicker_overlay_matches_process(
                associations[1234], "CODEX.EXE"
            )
        )
        self.assertFalse(
            prototype.quicker_overlay_matches_process(
                associations[1234], "QQ.exe"
            )
        )
        self.assertEqual(
            prototype.load_quicker_overlay_associations("missing.json"), {}
        )

    def test_quicker_process_association_snapshot_is_cached_by_file_signature(self):
        support = prototype._element_navigation_support
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "quicker-navigation.json"
            path.write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "hwnd": 1234,
                                "isBound": True,
                                "bindProcessName": "Codex.exe",
                                "visible": True,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            support._QUICKER_ASSOCIATION_CACHE.clear()
            with mock.patch("builtins.open", wraps=open) as patched_open:
                first = prototype.load_quicker_overlay_associations(str(path))
                second = prototype.load_quicker_overlay_associations(str(path))
            support._QUICKER_ASSOCIATION_CACHE.clear()

        self.assertEqual(set(first), {1234})
        self.assertEqual(first, second)
        self.assertIsNot(first, second)
        self.assertEqual(patched_open.call_count, 1)

    def test_root_only_small_overlay_becomes_one_clickable_cell(self):
        spec = prototype.root_only_overlay_target_spec(
            prototype.Rect(1457, 1037, 1502, 1082),
            "CustomWindowAutomationPeer",
        )
        self.assertIsNotNone(spec)
        assert spec is not None
        self.assertEqual(spec.snapshot.control_type, "OverlayWindowControl")
        self.assertEqual(spec.snapshot.name, "悬浮操作")
        self.assertEqual(spec.click_point, (1480, 1060))
        self.assertIsNone(
            prototype.root_only_overlay_target_spec(
                prototype.Rect(100, 100, 500, 500),
                "large popup",
            )
        )

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

    def test_foreground_context_keeps_an_associated_cross_process_overlay(self):
        process_ids = {10: 100, 20: 200, 90: 900}
        action = prototype.navigation_foreground_action(
            20,
            10,
            10,
            100,
            900,
            process_ids.get,
            lambda _hwnd: 0,
            associated_hwnds=(20,),
        )
        self.assertEqual(action, "sync")

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
        self.assertIn((516, 112), points)
        self.assertIn((516, 138), points)
        self.assertTrue(
            all(rect.contains_point(point) for point in points)
        )

    def test_parent_probe_points_avoid_retained_child_actions(self):
        parent = self.target(
            100,
            100,
            500,
            260,
            "card",
            path=(0, 1),
            has_action_pattern=True,
        )
        child = self.target(
            276,
            160,
            324,
            208,
            "delete",
            path=(0, 1, 0),
            has_action_pattern=True,
        )
        points = prototype.available_target_probe_points(parent, [parent, child])
        self.assertNotIn((300, 180), points)
        self.assertTrue(points)
        self.assertTrue(
            all(not child.rect.contains_point(point) for point in points)
        )

    def test_action_descendant_ignores_area_ratio_and_one_pixel_overflow(self):
        parent = self.target(
            100,
            100,
            500,
            260,
            "card",
            path=(0,),
            has_action_pattern=True,
        )
        child = self.target(
            276,
            160,
            324,
            261,
            "delete",
            path=(0, 1),
            has_action_pattern=True,
        )
        self.assertFalse(prototype.target_is_finer_descendant(parent, child))
        self.assertTrue(prototype.target_is_action_descendant(parent, child))
        self.assertNotIn(
            (300, 180),
            prototype.available_target_probe_points(parent, [parent, child]),
        )

        near_equal_child = self.target(
            102,
            101,
            498,
            259,
            "full child",
            path=(0, 2),
            has_action_pattern=True,
        )
        self.assertFalse(
            prototype.target_is_finer_descendant(parent, near_equal_child)
        )
        self.assertTrue(
            prototype.target_is_action_descendant(parent, near_equal_child)
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
                prototype.Rect(0, 0, 600, 400),
                "wrapper",
                "GroupControl",
                path=(0,),
            ),
            self.target(40, 40, 140, 90, "button", path=(0, 0)),
        ]
        self.assertEqual(prototype.nested_container_keep_indices(targets), [1])

    def test_keeps_standalone_structural_action(self):
        targets = [
            prototype.TargetSnapshot(
                prototype.Rect(40, 40, 140, 90),
                "canvas action",
                "CustomControl",
                has_action_pattern=True,
            )
        ]
        self.assertEqual(prototype.nested_container_keep_indices(targets), [0])

    def test_keeps_same_row_interactive_child_inside_primary_button(self):
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
        self.assertEqual(prototype.nested_container_keep_indices(targets), [0, 1])

    def test_keeps_anonymous_interactive_child_inside_primary_button(self):
        targets = [
            self.target(
                0,
                0,
                500,
                80,
                "row",
                path=(0, 1),
                has_action_pattern=True,
            ),
            self.target(
                440,
                20,
                472,
                52,
                "",
                path=(0, 1, 0),
                has_action_pattern=True,
            ),
        ]
        self.assertEqual(prototype.nested_container_keep_indices(targets), [0, 1])

    def test_keeps_interactive_custom_card_and_child_button(self):
        card = prototype.TargetSnapshot(
            prototype.Rect(100, 100, 600, 300),
            "card",
            "CustomControl",
            path=(0, 1),
            has_action_pattern=True,
        )
        more = self.target(
            540,
            120,
            580,
            160,
            "more",
            path=(0, 1, 0),
            has_action_pattern=True,
        )
        self.assertEqual(
            prototype.nested_container_keep_indices([card, more]), [0, 1]
        )

    def test_overlapping_different_branches_do_not_remove_weak_parent(self):
        wrapper = prototype.TargetSnapshot(
            prototype.Rect(100, 100, 600, 300),
            "wrapper",
            "GroupControl",
            path=(0, 1),
        )
        unrelated = self.target(
            540,
            120,
            580,
            160,
            "other branch",
            path=(0, 2, 0),
            has_action_pattern=True,
        )
        self.assertEqual(
            prototype.nested_container_keep_indices([wrapper, unrelated]),
            [0, 1],
        )

    def test_nested_show_more_occupies_the_folder_range(self):
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
        self.assertEqual(
            prototype.next_target_index(
                visible, 0, prototype.Direction.LEFT
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
            keyboard_focusable=True,
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

    def test_legacy_only_list_and_data_items_are_not_actionable(self):
        for control_type in ("ListItemControl", "DataItemControl"):
            self.assertFalse(
                prototype.standard_control_has_actionable_semantics(
                    control_type,
                    keyboard_focusable=False,
                    has_action_pattern=True,
                    has_direct_action_pattern=False,
                )
            )

    def test_native_list_items_keep_real_focus_or_direct_actions(self):
        self.assertTrue(
            prototype.standard_control_has_actionable_semantics(
                "ListItemControl",
                keyboard_focusable=True,
                has_action_pattern=True,
                has_direct_action_pattern=False,
            )
        )
        self.assertTrue(
            prototype.standard_control_has_actionable_semantics(
                "DataItemControl",
                keyboard_focusable=False,
                has_action_pattern=False,
                has_direct_action_pattern=True,
            )
        )

    def test_legacy_primary_button_remains_actionable(self):
        self.assertTrue(
            prototype.standard_control_has_actionable_semantics(
                "ButtonControl",
                keyboard_focusable=False,
                has_action_pattern=True,
                has_direct_action_pattern=False,
            )
        )

    def test_rejects_unnamed_group_even_with_automation_id(self):
        self.assertFalse(
            prototype.structural_action_has_identity(
                "GroupControl", "", "radix-_r_2rhf_"
            )
        )

    def test_focus_only_structural_target_must_be_compact(self):
        self.assertFalse(
            prototype.focus_only_structural_target_is_specific(
                "PaneControl",
                "blank content",
                "",
                prototype.Rect(0, 0, 1200, 800),
            )
        )
        self.assertTrue(
            prototype.focus_only_structural_target_is_specific(
                "CustomControl",
                "",
                "toolbar-action",
                prototype.Rect(100, 100, 140, 140),
            )
        )

    def test_text_edit_pattern_keeps_a_large_structural_editor(self):
        editor = prototype.Rect(100, 200, 900, 460)

        self.assertTrue(
            prototype.structural_target_has_actionable_semantics(
                "GroupControl",
                "",
                "",
                editor,
                keyboard_focusable=True,
                has_direct_action_pattern=False,
                has_text_edit_pattern=True,
            )
        )
        self.assertFalse(
            prototype.structural_target_has_actionable_semantics(
                "GroupControl",
                "",
                "",
                editor,
                keyboard_focusable=True,
                has_direct_action_pattern=False,
                has_text_edit_pattern=False,
            )
        )
        self.assertFalse(
            prototype.structural_target_has_actionable_semantics(
                "DocumentControl",
                "document root",
                "RootWebArea",
                editor,
                keyboard_focusable=True,
                has_direct_action_pattern=False,
                has_text_edit_pattern=True,
            )
        )

    def test_msaa_window_wrapper_is_dropped_only_with_finer_targets(self):
        window = prototype.Rect(0, 0, 1000, 800)
        wrapper = prototype.Rect(0, 0, 1000, 800)
        children = [
            self.target(40, 40, 140, 90, "first"),
            self.target(200, 120, 320, 180, "second"),
        ]

        self.assertTrue(
            prototype.msaa_wrapper_should_be_ignored(
                wrapper,
                window,
                children,
            )
        )
        self.assertFalse(
            prototype.msaa_wrapper_should_be_ignored(
                wrapper,
                window,
                children[:1],
            )
        )
        self.assertFalse(
            prototype.msaa_wrapper_should_be_ignored(
                wrapper,
                window,
                [],
            )
        )
        self.assertFalse(
            prototype.msaa_wrapper_should_be_ignored(
                prototype.Rect(100, 100, 500, 400),
                window,
                children,
            )
        )

    def test_runtime_scan_wires_editor_and_msaa_wrapper_rules(self):
        tree = ast.parse(WINDOWS_HOST_PATH.read_text(encoding="utf-8"))
        runtime_target = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == "runtime_target_from_control"
        )
        point_hierarchy = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == "point_hierarchy_targets"
        )

        self.assertTrue(
            any(
                isinstance(attribute, ast.Attribute)
                and attribute.attr == "TextEditPattern"
                for attribute in ast.walk(runtime_target)
            )
        )
        self.assertTrue(
            any(
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "msaa_wrapper_should_be_ignored"
                for call in ast.walk(point_hierarchy)
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
