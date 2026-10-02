"""Run the actual host branches with simulated UIA/input; never install a hook."""
from __future__ import annotations

import ast
import ctypes
import json
import queue
import tempfile
import threading
import time
import unittest
import zipfile
from ctypes import wintypes
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ovb_rc003 import element_navigation_runtime as runtime, log_export, win32_input
from ovb_rc003.diagnostic_trace import DiagnosticTrace


prototype = runtime._load_prototype()
host = prototype._load_element_navigation_windows_host()
TREE = ast.parse((Path(__file__).resolve().parents[1] / "scripts" /
                  "element_navigation_windows_host.py").read_text(encoding="utf-8"))
RUN = next(n for n in TREE.body if isinstance(n, ast.FunctionDef) and n.name == "_run_windows")


def load_host_nodes(namespace, *names):
    class Globals(ast.NodeTransformer):
        def visit_Nonlocal(self, node):
            return ast.copy_location(ast.Global(node.names), node)

    # Reparse so the production AST isn't mutated between tests.
    nodes = [Globals().visit(ast.parse(ast.unparse(n)).body[0])
             for n in RUN.body if isinstance(n, (ast.ClassDef, ast.FunctionDef)) and n.name in names]
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])),
                 host.__file__, "exec"), namespace)


class NavigationDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.records = []
        self.diagnostics = host._NavigationDiagnostics(
            lambda event, **fields: self.records.append(dict(event=event, **fields)))
        self.ns = dict(vars(host), diagnostics=self.diagnostics,
                       user32=SimpleNamespace(GetDoubleClickTime=lambda: 500,
                                              GetForegroundWindow=lambda: 42),
                       cursor_point=lambda: None, focused_rect=lambda: None,
                       window_process_id=lambda hwnd: 123,
                       dirty_windows=mock.Mock(watch=mock.Mock(return_value=False),
                                               state=mock.Mock(return_value=None)))
        load_host_nodes(self.ns, "AutomationWorker")
        self.worker = self.ns["AutomationWorker"]()
        self.worker._reset_hierarchy_for_selected = lambda: None
        self.worker._sync_window_geometry = lambda: True
        self.worker._target_is_navigable = lambda target: True
        self.worker._cache_is_reusable = lambda hwnd: False

    def test_local_input_owns_qt_repeats_and_release_without_system_injection(self):
        allowed = [True]
        actions = []
        local = host._LocalNavigationInput(lambda vk: allowed[0], actions.append, self.diagnostics)
        self.assertTrue(local.edge(host.VK_UP, True, False, False))
        self.assertTrue(local.edge(host.VK_UP, False, True, False))
        self.assertTrue(local.edge(host.VK_UP, True, True, False))
        allowed[0] = False
        self.assertTrue(local.edge(host.VK_UP, False, False, False))
        self.assertFalse(local.edge(host.VK_UP, False, False, False))
        self.assertEqual(actions, ["up", "up"])
        self.assertFalse(local.tap(host.VK_UP))

    def test_local_input_passes_existing_hold_modifiers_and_non_navigation_keys(self):
        allowed = [False]
        actions = []
        local = host._LocalNavigationInput(lambda vk: allowed[0], actions.append, self.diagnostics)
        self.assertFalse(local.edge(host.VK_UP, True, False, False))
        allowed[0] = True
        self.assertFalse(local.edge(host.VK_UP, True, True, False))
        self.assertFalse(local.edge(host.VK_UP, False, False, False))
        self.assertFalse(local.edge(host.VK_LEFT, True, False, True))
        self.assertFalse(local.edge(0x41, True, False, False))
        self.assertEqual(actions, [])
        self.assertTrue(local.edge(host.VK_UP, True, False, False))
        self.assertEqual(actions, ["up"])

    def test_local_input_return_repeat_and_lost_release_do_not_stick(self):
        actions = []
        local = host._LocalNavigationInput(lambda vk: True, actions.append, self.diagnostics)
        self.assertTrue(local.edge(host.VK_RETURN, True, False, False))
        self.assertTrue(local.edge(host.VK_RETURN, True, True, False))
        self.assertEqual(actions, ["activate"])
        self.assertTrue(local.edge(host.VK_RETURN, True, False, False))
        self.assertEqual(actions, ["activate", "activate"])

    def test_local_claim_is_limited_to_active_exact_own_window_and_native_menu_policy(self):
        self.host_actions()
        self.ns.update(shutting_down=False, navigation_root_hwnd=42,
                       navigation_process_id=1, native_menu_mode_active=lambda: False)
        load_host_nodes(self.ns, "can_claim_local_key")
        claim = self.ns["can_claim_local_key"]
        self.assertFalse(claim(host.VK_UP))
        self.ns["intercepting"].set()
        self.assertTrue(claim(host.VK_UP))
        self.ns["navigation_process_id"] = 123
        self.assertFalse(claim(host.VK_UP))
        self.ns["navigation_process_id"] = 1
        self.ns["user32"].GetForegroundWindow = lambda: 99
        self.assertFalse(claim(host.VK_UP))
        self.ns["user32"].GetForegroundWindow = lambda: 42
        self.ns["native_menu_mode_active"] = lambda: True
        self.assertFalse(claim(host.VK_UP))
        self.ns["native_menu_mode_active"] = lambda: False
        self.ns["shutting_down"] = True
        self.assertFalse(claim(host.VK_UP))

    def test_mapped_remote_action_reaches_worker_without_owning_keyboard(self):
        self.scan()
        self.worker.selected = 0
        self.host_actions()
        self.ns["active"].set()
        self.ns["intercepting"].set()
        self.ns.update(shutting_down=False, navigation_root_hwnd=42,
                       navigation_process_id=123,
                       navigation_action_for_foreground=lambda hwnd: "sync" if hwnd == 42 else "leave",
                       native_menu_mode_active=lambda: False,
                       enqueue_keyboard_action=lambda action: self.ns["keyboard_events"].put((action, 0)))
        load_host_nodes(self.ns, "route_mapped_key")
        self.worker.post = lambda action, value: self.worker._move(host.Direction(value)) if action == "move" else None
        embedded = host.EmbeddedElementNavigationRuntime(
            mock.Mock(), mock.Mock(), mock.Mock(),
            mapped_input=self.ns["route_mapped_key"],
        )
        self.assertFalse(embedded.route_local_key(host.VK_RIGHT, True, False, False))
        self.assertTrue(embedded.route_mapped_key(host.VK_RIGHT))
        self.ns["drain_events"]()
        self.assertEqual(self.worker.selected, 1)
        self.ns["user32"].GetForegroundWindow = lambda: 99
        self.assertFalse(embedded.route_mapped_key(host.VK_LEFT))
        self.ns["user32"].GetForegroundWindow = lambda: 42
        self.ns["native_menu_mode_active"] = lambda: True
        self.assertFalse(embedded.route_mapped_key(host.VK_RETURN))
        self.ns["intercepting"].clear()
        self.assertFalse(embedded.route_mapped_key(host.VK_RIGHT))

    def scan(self, count=2, token=7):
        def enumerate_window(*args, **kwargs):
            w = self.worker
            w.window_rect = host.Rect(0, 0, 500, 400)
            w.hwnd, w.window_name, w.visited = 42, "PRIVATE WINDOW", count + 1
            w.all_targets = [SimpleNamespace(snapshot=host.TargetSnapshot(
                host.Rect(20 + i * 100, 20, 80 + i * 100, 70),
                "PRIVATE TEXT", "ButtonControl", path=(i,))) for i in range(count)]
            w.context_valid = True
            w.cache_timestamp = time.perf_counter()
            return True, False, count == 0
        self.worker._enumerate = enumerate_window
        self.worker._scan(42, 0, token)

    def last(self, event):
        return next(r for r in reversed(self.records) if r["event"] == "element_navigation_" + event)

    def mouse_fixture(self, *, semantic=False):
        rect = host.Rect(100, 100, 240, 180)
        control = SimpleNamespace(runtime_id=(1,), rect=rect,
                                  ControlTypeName="ButtonControl", Name="PRIVATE TEXT",
                                  GetParentControl=mock.Mock(return_value=None))
        target = SimpleNamespace(snapshot=host.TargetSnapshot(
            rect, "PRIVATE TEXT", "ButtonControl", path=(0,), runtime_id=(1,),
            has_action_pattern=semantic), control=control, click_point=None)
        self.ns.update(auto=SimpleNamespace(ControlFromPoint=mock.Mock(return_value=control)),
                       runtime_id_from_control=lambda item: item.runtime_id,
                       rect_from_control=lambda item: item.rect,
                       click_point=mock.Mock(), scroll_point=mock.Mock(),
                       send_mouse_events=mock.Mock(return_value=1),
                       try_semantic_invoke=mock.Mock(return_value="InvokePattern"))
        self.worker.targets = self.worker.all_targets = [target]
        self.worker.selected = 0
        self.worker.context_valid = True
        self.worker._update_live_target = mock.Mock(return_value=True)
        self.worker._cached_pointer_point = lambda item: None
        self.worker._cached_scroll_point = lambda item: None
        self.worker._request_background_refresh = mock.Mock()
        self.worker._emit_selection = mock.Mock()
        return target, control

    def test_hit_test_exception_retries_and_clicks_only_the_later_verified_point(self):
        for operation in ("_activate", "_context_click", "_scroll"):
            with self.subTest(operation=operation):
                target, control = self.mouse_fixture()
                self.ns["auto"].ControlFromPoint.side_effect = [OSError("PRIVATE"), control]
                if operation == "_scroll":
                    self.worker._scroll(1)
                else:
                    getattr(self.worker, operation)()
                click = self.ns["scroll_point"] if operation == "_scroll" else self.ns["click_point"]
                self.assertEqual(click.call_count, 1)
                probe = self.last("point_probe")
                self.assertEqual((probe["outcome"], probe["count"]), ("verified", 2))
                self.assertEqual(self.last("point_probe_error")["reason"], "hit_test_failed")
                expected = host.available_target_probe_points(target.snapshot, [target.snapshot])[1]
                self.assertEqual(target.click_point, expected)
                self.assertEqual(click.call_args.args[0], expected)
                self.assertEqual(self.last("mouse_action")["outcome"], "submitted")

    def test_parent_query_exception_does_not_cancel_remaining_probe_points(self):
        target, control = self.mouse_fixture()
        broken_child = SimpleNamespace(runtime_id=(2,), rect=control.rect,
                                       ControlTypeName="TextControl", Name="PRIVATE",
                                       GetParentControl=mock.Mock(side_effect=OSError("PRIVATE")))
        self.ns["auto"].ControlFromPoint.side_effect = [broken_child, control]
        self.assertTrue(self.worker._target_is_exposed(target, allow_semantic_bypass=False))
        self.assertEqual(self.last("point_probe")["count"], 2)
        self.assertEqual(self.last("point_probe_error")["reason"], "parent_query_failed")

    def test_probe_misses_and_exceptions_do_not_create_an_unverified_center_click(self):
        for failure in (None, OSError("PRIVATE")):
            for operation in ("_activate", "_context_click"):
                with self.subTest(failure=type(failure).__name__, operation=operation):
                    target, control = self.mouse_fixture()
                    probe = self.ns["auto"].ControlFromPoint
                    if failure is None:
                        probe.return_value = None
                    else:
                        probe.side_effect = failure
                    getattr(self.worker, operation)()
                    self.assertIsNone(target.click_point)
                    self.assertEqual(probe.call_count, len(host.target_probe_points(target.snapshot.rect)))
                    self.ns["click_point"].assert_not_called()
                    self.assertEqual(self.last("point_probe")["outcome"], "missed")
                    self.assertEqual(self.last("target_skipped")["reason"], "target_unavailable")

    def test_ordinary_descendant_is_clickable_but_retained_child_action_is_not(self):
        target, control = self.mouse_fixture()
        child_rect = host.Rect(140, 125, 190, 160)
        child = SimpleNamespace(runtime_id=(2,), rect=child_rect,
                                ControlTypeName="TextControl", Name="PRIVATE CHILD",
                                GetParentControl=mock.Mock(return_value=control))
        self.ns["auto"].ControlFromPoint.return_value = child
        self.assertTrue(self.worker._target_is_exposed(target, allow_semantic_bypass=False))
        target.click_point = None
        self.worker.targets.append(SimpleNamespace(snapshot=host.TargetSnapshot(
            child_rect, child.Name, child.ControlTypeName, path=(0, 1), runtime_id=(2,),
            has_action_pattern=True), control=child, click_point=None))
        self.assertFalse(self.worker._target_is_exposed(target, allow_semantic_bypass=False))
        self.assertIsNone(target.click_point)

    def test_child_actions_covering_all_probes_never_fall_back_to_the_parent_center(self):
        target, control = self.mouse_fixture()
        self.worker.targets.append(SimpleNamespace(snapshot=host.TargetSnapshot(
            target.snapshot.rect, "PRIVATE CHILD", "ButtonControl", path=(0, 1),
            runtime_id=(2,), has_action_pattern=True), control=control, click_point=None))
        self.worker._context_click()
        self.ns["auto"].ControlFromPoint.assert_not_called()
        self.ns["click_point"].assert_not_called()
        self.assertEqual(self.last("point_probe")["reason"], "no_available_point")

    def test_semantic_fallback_remains_left_click_only_and_does_not_send_mouse_input(self):
        target, control = self.mouse_fixture(semantic=True)
        self.ns["auto"].ControlFromPoint.return_value = None
        self.worker._activate()
        self.ns["try_semantic_invoke"].assert_called_once_with(target)
        self.ns["click_point"].assert_not_called()
        self.assertEqual(self.last("semantic_action")["outcome"], "returned")
        self.ns["try_semantic_invoke"].reset_mock()
        self.worker._context_click()
        self.ns["try_semantic_invoke"].assert_not_called()
        self.ns["click_point"].assert_not_called()

    def test_mouse_guard_failures_and_original_cause_are_logged_without_error_text(self):
        target, control = self.mouse_fixture()
        failures = [(host.MouseInputBusyError("PRIVATE"), "busy"),
                    (host.MouseInputDeliveryError("PRIVATE"), "failed"),
                    (host.MouseInputCleanupIncompleteError("left", "PRIVATE"), "cleanup_pending")]
        failures[-1][0].__cause__ = ctypes.ArgumentError("PRIVATE INPUT")
        for error, outcome in failures:
            with self.subTest(outcome=outcome):
                self.worker._mouse_input_safety = host._MouseInputSafetyState()
                self.assertFalse(self.worker._try_mouse_action(
                    target, "左击", mock.Mock(side_effect=error)))
                row = self.last("mouse_action")
                self.assertEqual((row["outcome"], row["error_type"]), (outcome, type(error).__name__))
        self.assertEqual(self.last("mouse_action")["cause_type"], "ArgumentError")
        self.assertNotIn("PRIVATE", json.dumps(self.records))

    def test_click_queue_suppression_and_invalid_context_are_visible(self):
        self.ns.update(auto=mock.Mock(), send_mouse_events=mock.Mock(return_value=1))
        self.worker.post("activate")
        self.worker.post("activate")
        self.worker.post("activate")
        self.assertEqual(self.last("command_discarded")["reason"], "pending_limit")
        self.worker._generation = 1
        self.worker.context_valid = False
        self.worker.commands.put(("context", None, 1))
        self.worker.commands.put(("stop", None, 1))
        self.worker._run()
        rows = [r for r in self.records if r["event"] == "element_navigation_command_discarded"]
        self.assertIn("generation_changed", {r["reason"] for r in rows})
        self.assertEqual(self.last("command_started")["outcome"], "context_invalid")

    def test_pending_mouse_release_still_blocks_until_a_confirmed_release(self):
        target, control = self.mouse_fixture()
        self.worker._mouse_input_safety.pending_button = "left"
        self.ns["send_mouse_events"].side_effect = [ctypes.ArgumentError("PRIVATE"), 1]
        self.assertFalse(self.worker._try_mouse_action(target, "右击", self.ns["click_point"]))
        self.ns["click_point"].assert_not_called()
        self.assertEqual(self.worker._mouse_input_safety.pending_button, "left")
        self.assertTrue(self.worker._try_mouse_action(target, "右击", self.ns["click_point"]))
        self.assertIsNone(self.worker._mouse_input_safety.pending_button)
        self.ns["click_point"].assert_called_once()

    def test_exit_release_failure_preserves_the_cause_and_remains_retryable(self):
        self.worker._mouse_input_safety.pending_button = "left"
        self.ns.update(auto=mock.Mock(), send_mouse_events=mock.Mock(
            side_effect=[ctypes.ArgumentError("PRIVATE"), 1]))
        self.worker.commands.put(("stop", None, 0))
        self.worker.commands.put(("stop", None, 0))
        self.worker._run()
        row = self.last("worker_error")
        self.assertEqual((row["command"], row["cause_type"]), ("stop", "ArgumentError"))
        self.assertIsNone(self.worker._mouse_input_safety.pending_button)
        self.assertNotIn("PRIVATE", json.dumps(self.records))

    @unittest.skipUnless(hasattr(ctypes, "WinDLL"), "Windows native ctypes binding")
    def test_native_keyboard_and_two_navigators_keep_independent_input_bindings(self):
        callbacks, submissions = [], []

        def callback_for(label):
            @ctypes.WINFUNCTYPE(wintypes.UINT, wintypes.UINT, ctypes.c_void_p, ctypes.c_int)
            def send(count, address, size):
                submissions.append((label, int(count), int(size)))
                return count
            callbacks.append(send)  # Keep the native callback alive.
            return send

        shared = SimpleNamespace(SendInput=callback_for("ordinary"))
        binding_nodes = [n for n in RUN.body if isinstance(n, ast.Assign)
                         and any(ast.unparse(t) in {"user32", "user32.SendInput.argtypes",
                                                   "user32.SendInput.restype"} for t in n.targets)]
        senders = []
        with mock.patch.object(ctypes, "windll", SimpleNamespace(user32=shared)), \
                mock.patch.object(win32_input, "_require_live_input_allowed"):
            for label in ("first", "second"):
                ns = dict(vars(host), wintypes=wintypes, diagnostics=self.diagnostics)
                load_host_nodes(ns, "MouseInput", "InputUnion", "Input", "send_mouse_events")
                exec(compile(ast.fix_missing_locations(ast.Module(body=binding_nodes, type_ignores=[])),
                             host.__file__, "exec"), ns)
                # Load the actual WinDLL and production declarations, then
                # replace only its function address; no OS input is submitted.
                send = callback_for(label)
                send.argtypes = ns["user32"].SendInput.argtypes
                ns["user32"].SendInput = send
                senders.append(ns["send_mouse_events"])
            self.assertEqual(senders[0]([(2, 0), (4, 0)]), 2)
            for ordinary, events in ((win32_input._real_send_input_batch, [(75, False), (75, True)]),
                                     (win32_input._real_send_virtual_key_input_batch, [(162, False)]),
                                     (win32_input._real_send_mouse_input_batch, [(2, 0), (4, 0)])):
                self.assertEqual(ordinary(events), len(events))
                for sender in senders:
                    state = host._MouseInputSafetyState()
                    moves = []
                    for button in ("left", "right"):
                        state.run(lambda: host._move_and_click_safely(
                            (100, 100), button, is_button_down=lambda _: False,
                            move_pointer=lambda point: moves.append(point) or True,
                            send_events=sender), send_events=sender)
                    state.run(lambda: host._move_and_wheel_safely(
                        (100, 100), 1, pointer_move_is_blocked=lambda: False,
                        move_pointer=lambda point: moves.append(point) or True,
                        send_events=sender), send_events=sender)
                    state.release_pending(send_events=sender)
                    self.assertIsNone(state.pending_button)
                    self.assertEqual(len(moves), 3)
        self.assertEqual({label for label, _, _ in submissions}, {"ordinary", "first", "second"})
        self.assertEqual({size for _, _, size in submissions}, {40})

    @unittest.skipUnless(hasattr(ctypes, "WinDLL"), "Windows native ctypes binding")
    def test_native_submission_failures_recovery_and_probe_results_survive_zip_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trace = DiagnosticTrace(root, enabled=True)
            self.addCleanup(trace.close)
            self.addCleanup(runtime.clear_diagnostic_trace, trace)
            runtime.set_diagnostic_trace(trace)
            self.diagnostics._sink = runtime._emit_diagnostic
            self.diagnostics._enabled = runtime._diagnostic_enabled
            target, control = self.mouse_fixture()
            native_calls, moves, delivery = [], [], [None]

            @ctypes.WINFUNCTYPE(wintypes.UINT, wintypes.UINT, ctypes.c_void_p, ctypes.c_int)
            def native_callback(count, pointer, size):
                native_calls.append(int(count))
                return count if delivery[0] is None else min(count, delivery[0])

            self.ns.update(wintypes=wintypes, user32=SimpleNamespace(SendInput=native_callback))
            load_host_nodes(self.ns, "MouseInput", "InputUnion", "Input", "send_mouse_events")
            sender = self.ns["send_mouse_events"]
            self.ns["click_point"] = lambda point, button="left": host._move_and_click_safely(
                point, button, is_button_down=lambda _: False,
                move_pointer=lambda item: moves.append(item) or True, send_events=sender)
            # Genuine ctypes argument conversion fails before this native callback.
            native_callback.argtypes = (wintypes.UINT, ctypes.POINTER(win32_input.INPUT), ctypes.c_int)
            self.worker._activate()
            self.worker._context_click()
            self.assertEqual((len(moves), native_calls), (1, []))
            self.assertEqual(self.worker._mouse_input_safety.pending_button, "left")
            native_callback.argtypes = (wintypes.UINT, ctypes.POINTER(self.ns["Input"]), ctypes.c_int)
            self.worker._context_click()
            self.assertIsNone(self.worker._mouse_input_safety.pending_button)
            # Partial submission is distinct from a pre-call conversion exception.
            delivery[0] = 1
            self.worker._activate()
            delivery[0] = None
            self.ns["auto"].ControlFromPoint.side_effect = [TimeoutError("PRIVATE"), control]
            self.worker._context_click()
            self.ns["auto"].ControlFromPoint.side_effect = None
            self.worker._generation = 1
            self.worker.commands.put(("activate", None, 0))
            self.worker.commands.put(("stop", None, 1))
            self.ns["auto"].InitializeUIAutomationInCurrentThread = mock.Mock()
            self.ns["auto"].UninitializeUIAutomationInCurrentThread = mock.Mock()
            self.worker._run()
            # Diagnostic disable and a replacement writer must keep the same
            # live navigator attached, without producing disabled mouse records.
            trace.set_enabled(False)
            self.worker._activate()
            trace.set_enabled(True)
            self.worker._activate()
            trace.close()
            destination = root / "mouse-evidence.zip"
            self.assertEqual(log_export.export_logs(destination, root=root).outcome, "exported")
            with zipfile.ZipFile(destination) as archive:
                data = archive.read("diagnostic-trace.jsonl").decode("utf-8")
                manifest = json.loads(archive.read("export-info.json"))
            rows = [json.loads(line) for line in data.splitlines()]
            actions = [r for r in rows if r["event"] == "element_navigation_mouse_action"]
            self.assertEqual({r["outcome"] for r in actions}, {"submitted", "failed", "cleanup_pending"})
            self.assertTrue(any(r.get("cause_type") == "ArgumentError" for r in actions))
            submissions = [r for r in rows if r["event"] == "element_navigation_mouse_submission"]
            self.assertEqual({r["outcome"] for r in submissions}, {"submitted", "incomplete", "exception"})
            self.assertTrue(any(r.get("requested") == 2 and r.get("returned") == 1 for r in submissions))
            self.assertTrue(any(r["event"] == "element_navigation_point_probe_error"
                                and r["error_type"] == "TimeoutError" for r in rows))
            self.assertTrue(any(r["event"] == "element_navigation_command_discarded"
                                and r["reason"] == "generation_changed" for r in rows))
            sessions = [r for r in rows if r["event"] == "session_finished"]
            self.assertEqual((len(sessions), {r["dropped"] for r in sessions}), (2, {0}))
            self.assertFalse(manifest["incomplete"])
            self.assertNotIn("PRIVATE", data)

    def test_scan_and_actual_movement_share_token_without_text(self):
        self.scan()
        self.worker.selected = 0
        self.worker._move(host.Direction.RIGHT)
        self.assertEqual(self.worker.selected, 1)
        self.assertEqual(self.last("scan_result")["count"], 2)
        movement = self.last("move")
        self.assertEqual((movement["scan_token"], movement["outcome"]), (7, "selected"))
        self.assertEqual(movement["candidate_count"], 1)
        self.assertNotIn("PRIVATE", json.dumps(self.records))

    def test_single_empty_cancelled_and_unhittable_are_distinct(self):
        self.scan(0)
        self.assertEqual(self.last("scan_result")["outcome"], "empty")
        self.scan(1)
        self.worker._move(host.Direction.RIGHT)
        self.assertEqual(self.last("scan_result")["outcome"], "single")
        self.assertEqual(self.last("move")["candidate_count"], 0)
        self.scan(2)
        self.worker.selected = 0
        self.worker._target_is_navigable = lambda target: False
        self.worker._move(host.Direction.RIGHT)
        self.assertEqual(self.last("move")["unhittable_count"], 1)
        self.assertEqual(self.worker.selected, 0)
        previous = len(self.records)
        self.worker._scan(42, 99, 8)
        self.assertEqual([r["event"] for r in self.records[previous:]],
                         ["element_navigation_scan_started"])
        self.assertIn(("scan_cancelled", {"scan_token": 8}), list(self.worker.events.queue))

    def test_sink_failure_does_not_change_scan_or_move(self):
        self.diagnostics._sink = mock.Mock(side_effect=OSError("PRIVATE ERROR"))
        self.scan()
        self.worker.selected = 0
        self.worker._move(host.Direction.RIGHT)
        self.assertEqual(self.worker.selected, 1)

    def test_collection_reports_provider_errors_without_error_text(self):
        load_host_nodes(self.ns, "collect_targets")
        control = mock.Mock()
        type(control).ControlTypeName = property(lambda _: (_ for _ in ()).throw(OSError("PRIVATE")))
        control.GetChildren.side_effect = PermissionError("PRIVATE")
        self.ns.update(args=SimpleNamespace(max_nodes=100, max_elements=50),
                       normalize_runtime_targets=lambda targets, rect: targets)
        result = self.ns["collect_targets"](control, host.Rect(0, 0, 500, 400), (), 0, 5)
        self.assertEqual(result[3], 1)
        self.assertEqual(self.last("collection")["property_errors"], 1)
        self.assertEqual(self.last("collection")["children_errors"], 1)
        self.assertEqual(self.last("collection_error")["error_type"], "OSError")
        self.assertNotIn("PRIVATE", json.dumps(self.records))

    def test_worker_initialization_and_command_failure_are_logged(self):
        auto = mock.Mock()
        self.ns.update(auto=auto, send_mouse_events=mock.Mock())
        auto.InitializeUIAutomationInCurrentThread.side_effect = OSError("PRIVATE")
        with self.assertRaises(OSError):
            self.worker._run()
        self.assertEqual(self.last("worker_error")["command"], "initialize")
        auto.InitializeUIAutomationInCurrentThread.side_effect = None
        self.worker._scan = mock.Mock(side_effect=PermissionError("PRIVATE"))
        self.worker.commands.put(("scan", (42, 17), 0))
        self.worker.commands.put(("stop", None, 0))
        self.worker._run()
        self.assertEqual(self.last("worker_error")["scan_token"], 17)
        self.assertEqual(self.last("worker_error")["error_type"], "PermissionError")
        auto.UninitializeUIAutomationInCurrentThread.assert_called_once()

    def host_actions(self):
        self.ns.update(active=threading.Event(), intercepting=threading.Event(),
                       scanning=False, current_scan_token=0, scan_token_counter=0,
                       navigation_root_hwnd=0, navigation_process_id=0,
                       diagnostics_enabled=False, worker=self.worker, overlay=mock.Mock(),
                       keyboard_events=queue.Queue(), prototype_process_id=1,
                       associated_overlay_window_signature=lambda *a, **k: (),
                       prepare_navigation_action=lambda: True,
                       leave_navigation=mock.Mock(), request_quit=mock.Mock())
        load_host_nodes(self.ns, "handle_keyboard_action", "drain_events")

    def test_toggle_scanning_ignore_and_stale_results_keep_state(self):
        self.host_actions()
        action = self.ns["handle_keyboard_action"]
        action("toggle", 42)
        self.assertEqual(self.last("scan_requested")["target_hwnd"], 42)
        action("right")
        self.assertEqual(self.last("action_ignored")["reason"], "scanning")
        self.worker.events.put(("scan_done", {"scan_token": 99}))
        self.ns["drain_events"]()
        self.assertEqual(self.last("scan_applied")["outcome"], "stale")
        self.assertTrue(self.ns["scanning"])
        self.scan(token=1)
        self.ns["drain_events"]()
        self.assertTrue(self.ns["active"].is_set())
        self.ns["overlay"].show_target.assert_called_once()

    def make_hook(self):
        load_host_nodes(self.ns, "KeyboardHook")
        hook = self.ns["KeyboardHook"].__new__(self.ns["KeyboardHook"])
        class Data(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint) for n in ("vkCode", "scanCode", "flags")]
        hook._struct, hook._hook = Data, None
        hook._active = threading.Event()
        hook._active.set()
        hook._intercepting = hook._active
        hook._down, hook._swallowed, hook._passthrough = set(), set(), set()
        hook._pressed, hook._include_developer_hotkeys = lambda _: False, False
        hook._include_toggle_hotkey = True
        hook._on_action = mock.Mock()
        hook._direction_input_ownership = mock.Mock(
            route=mock.Mock(return_value=(False, None)),
            has_forwarded_down=mock.Mock(return_value=False))
        self.ns["user32"].CallNextHookEx = mock.Mock(return_value=77)
        self.ns["native_menu_mode_active"] = lambda: False
        return hook, Data

    def test_hook_observes_navigation_routes_and_ignores_typing(self):
        hook, Data = self.make_hook()
        def down(vk):
            data = Data(vk, 0, 0x10)
            return hook._handle(0, hook.WM_KEYDOWN, ctypes.addressof(data))
        self.assertEqual(down(0x41), 77)
        self.assertEqual(self.records, [])
        self.assertEqual(down(0x27), 1)
        self.assertEqual(self.last("key_route")["outcome"], "queued")
        hook._on_action.assert_called_once_with("right")
        hook._direction_input_ownership.route.return_value = (True, 88)
        self.assertEqual(down(0x25), 88)
        self.assertEqual(self.last("key_route")["outcome"], "device_edge_owned")
        self.ns["native_menu_mode_active"] = lambda: True
        self.assertEqual(down(0x26), 77)
        self.assertEqual(self.last("key_route")["outcome"], "native_menu_passthrough")

    def test_key_entry_and_return_identify_own_and_external_window_without_typing(self):
        hook, Data = self.make_hook()
        self.diagnostics.keyboard_context(7, 42, 123)
        for foreground in (42, 900):
            self.ns["user32"].GetForegroundWindow = lambda: foreground
            data = Data(0x27, 77, 0x11)
            self.assertEqual(hook._handle(0, hook.WM_KEYDOWN, ctypes.addressof(data)), 1)
            entry, result = self.last("key_received"), self.last("key_finished")
            self.assertEqual(entry["foreground_hwnd"], foreground)
            self.assertEqual((entry["scan_token"], entry["target_hwnd"], entry["target_pid"]), (7, 42, 123))
            self.assertEqual(entry["key_seq"], result["key_seq"])
            self.assertEqual(result["callback_result"], 1)
        before = len(self.records)
        for message in (hook.WM_KEYDOWN, hook.WM_KEYUP):
            data = Data(0x41, 30, 0)
            self.assertEqual(hook._handle(0, message, ctypes.addressof(data)), 77)
        self.assertEqual(len(self.records), before)
        self.diagnostics.emit("worker_event")
        self.assertNotIn("key_seq", self.last("worker_event"))

    def test_all_early_key_returns_keep_their_original_ownership(self):
        hook, Data = self.make_hook()
        hook._direction_input_ownership.has_forwarded_down.return_value = False

        def send(vk=0x27, message=hook.WM_KEYDOWN, flags=0):
            data = Data(vk, 77, flags)
            return hook._handle(0, message, ctypes.addressof(data))

        hook._passthrough.add(0x27)
        self.assertEqual(send(), 77)
        self.assertEqual(self.last("key_route")["outcome"], "held_before_navigation")
        self.assertEqual(send(message=hook.WM_KEYUP), 77)
        self.assertNotIn(0x27, hook._passthrough)
        self.assertEqual(send(message=hook.WM_KEYUP), 77)
        self.assertEqual(self.last("key_route")["outcome"], "unowned_release")
        self.assertEqual(send(flags=0x10), 1)
        self.assertEqual(send(message=hook.WM_KEYUP, flags=0x10), 1)
        self.assertEqual(self.last("key_route")["outcome"], "owned_release")
        hook._direction_input_ownership.has_forwarded_down.return_value = True
        hook._direction_input_ownership.route.return_value = (True, 88)
        self.assertEqual(send(message=hook.WM_KEYUP), 88)
        self.assertEqual(self.last("key_route")["outcome"], "forwarded_release")
        hook._direction_input_ownership.route.return_value = (False, None)
        self.assertEqual(send(vk=host.VK_RETURN, flags=0x10), 1)
        self.assertEqual(send(vk=host.VK_RETURN, flags=0x10), 1)
        self.assertEqual(self.last("key_route")["outcome"], "action_repeat_suppressed")
        hook._active.clear()
        self.assertEqual(send(vk=0x25, flags=0x10), 77)
        self.assertEqual(self.last("key_route")["outcome"], "navigation_inactive")

    def test_callback_exception_is_recorded_and_still_propagates(self):
        hook, Data = self.make_hook()
        error = OSError("PRIVATE ERROR")
        hook._pressed = mock.Mock(side_effect=error)
        data = Data(0x27, 77, 0x10)
        with self.assertRaises(OSError) as raised:
            hook._handle(0, hook.WM_KEYDOWN, ctypes.addressof(data))
        self.assertIs(raised.exception, error)
        self.assertEqual(self.last("key_error")["key_seq"], self.last("key_received")["key_seq"])
        self.assertNotIn("PRIVATE", json.dumps(self.records))

    def test_muted_or_broken_sink_does_not_change_hook_or_query_foreground(self):
        hook, Data = self.make_hook()
        self.ns["user32"].GetForegroundWindow = mock.Mock(side_effect=RuntimeError("PRIVATE"))
        self.diagnostics._enabled = lambda: False
        data = Data(0x27, 77, 0x10)
        self.assertEqual(hook._handle(0, hook.WM_KEYDOWN, ctypes.addressof(data)), 1)
        self.ns["user32"].GetForegroundWindow.assert_not_called()
        self.assertEqual(self.records, [])
        self.diagnostics._enabled = lambda: True
        self.diagnostics._sink = mock.Mock(side_effect=OSError("PRIVATE"))
        self.assertEqual(hook._handle(0, hook.WM_KEYDOWN, ctypes.addressof(data)), 1)

    def collection_fixture(self):
        load_host_nodes(self.ns, "RuntimeTarget", "runtime_target_from_control", "collect_targets")
        self.ns.update(
            interactive_types={"ButtonControl"},
            action_pattern_support=lambda control: (True, True, False),
            runtime_id_from_control=lambda control: (),
            rect_from_control=lambda control: control.rect,
            is_navigation_noise=lambda name: name == "PRIVATE noise",
            normalize_runtime_targets=lambda targets, rect: targets,
            args=SimpleNamespace(max_nodes=1000, max_elements=500),
        )

        def control(kind="ButtonControl", children=(), **overrides):
            fields = dict(ControlTypeName=kind, Name="PRIVATE TEXT", AutomationId="PRIVATE ID",
                          IsEnabled=True, IsOffscreen=False, IsKeyboardFocusable=True,
                          rect=host.Rect(20, 20, 80, 80), GetChildren=mock.Mock(return_value=children))
            fields.update(overrides)
            return SimpleNamespace(**fields)
        return control

    def test_real_candidate_filters_report_exact_reasons_without_extra_reads(self):
        control = self.collection_fixture()
        cases = [
            (dict(ControlTypeName="TextControl"), "unsupported_type"),
            (dict(IsEnabled=False), "disabled"),
            (dict(IsOffscreen=True), "offscreen"),
            (dict(rect=host.Rect(1, 1, 4, 4)), "size_outside_limits"),
            (dict(Name="PRIVATE noise"), "navigation_noise"),
            (dict(rect=host.Rect(900, 900, 950, 950)), "outside_window"),
            (dict(), "candidate"),
        ]
        for overrides, reason in cases:
            with self.subTest(reason=reason):
                child = control(**overrides)
                root = control("WindowControl", children=[child])
                self.records.clear()
                result = self.ns["collect_targets"](root, host.Rect(0, 0, 500, 400), (), 0, 5,
                                                    diagnostic_hwnd=42)
                rows = [r for r in self.records if r["event"] == "element_navigation_collection_filter"]
                self.assertIn(reason, {r["reason"] for r in rows})
                self.assertEqual(len(result[0]), int(reason == "candidate"))
                self.assertEqual({r["target_hwnd"] for r in rows}, {42})
                root.GetChildren.assert_called_once_with()
                child.GetChildren.assert_called_once_with()
                self.assertNotIn("PRIVATE", json.dumps(self.records))

    def test_semantic_rejection_and_swallowed_target_error_are_visible(self):
        control = self.collection_fixture()
        collection = self.diagnostics.collection(42)
        self.ns["action_pattern_support"] = lambda control: (False, False, False)
        result = self.ns["runtime_target_from_control"](
            control(IsKeyboardFocusable=False), host.Rect(0, 0, 500, 400),
            diagnostic_collection=collection)
        self.assertIsNone(result)
        self.ns["action_pattern_support"] = mock.Mock(side_effect=OSError("PRIVATE"))
        self.assertIsNone(self.ns["runtime_target_from_control"](
            control(), host.Rect(0, 0, 500, 400), diagnostic_collection=collection))
        collection.emit()
        self.assertEqual(collection.counts[("ButtonControl", "no_actionable_semantics")], 1)
        self.assertEqual(collection.counts[("ButtonControl", "target_property_error")], 1)
        self.assertEqual(self.last("target_read_error")["error_type"], "OSError")
        self.assertNotIn("PRIVATE", json.dumps(self.records))

    def test_collection_summary_is_bounded_and_redacts_unknown_types(self):
        collection = self.diagnostics.collection(42)
        for _ in range(1000):
            collection.record("unsupported_type", "PRIVATEControl")
        collection.emit()
        self.assertEqual(len(self.records), 1)
        self.assertEqual(self.records[0]["count"], 1000)
        self.assertEqual(self.records[0]["control_type"], "UnknownControl")
        self.assertNotIn("PRIVATE", json.dumps(self.records))
        for index in range(300):
            collection.record(str(index), "ButtonControl")
        self.assertLessEqual(len(collection.counts), 128)
        self.diagnostics._enabled = lambda: False
        muted = self.diagnostics.collection(42)
        muted.record("candidate", "ButtonControl")
        muted.emit()
        self.assertEqual(muted.counts, {})

    def test_real_trace_switch_restart_and_zip_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trace = DiagnosticTrace(root, enabled=False)
            self.addCleanup(trace.close)
            self.addCleanup(runtime.clear_diagnostic_trace, trace)
            runtime.set_diagnostic_trace(trace)
            self.diagnostics._sink = runtime._emit_diagnostic
            self.diagnostics._enabled = runtime._diagnostic_enabled
            self.scan()
            self.assertFalse((root / "logs" / "diagnostic-trace.jsonl").exists())
            trace.set_enabled(True)
            self.scan()
            self.worker.selected = 0
            self.worker._move(host.Direction.RIGHT)
            hook, Data = self.make_hook()
            data = Data(0x27, 77, 0x10)
            hook._handle(0, hook.WM_KEYDOWN, ctypes.addressof(data))
            collection = self.diagnostics.collection(42)
            collection.record("unsupported_type", "TextControl")
            collection.emit()
            self.diagnostics.emit("ignored_fields", text="PRIVATE", target=object(),
                                  count={"text": "PRIVATE"}, window="PRIVATE")
            # An old bridge finishing must not detach the current writer.
            runtime.clear_diagnostic_trace(object())
            self.diagnostics.emit("still_attached")
            runtime.clear_diagnostic_trace(trace)
            self.diagnostics.emit("after_detach")
            trace.close()
            destination = root / "export.zip"
            self.assertEqual(log_export.export_logs(destination, root=root).outcome, "exported")
            with zipfile.ZipFile(destination) as archive:
                data = archive.read("diagnostic-trace.jsonl").decode("utf-8")
            rows = [json.loads(line) for line in data.splitlines()]
            self.assertIn("element_navigation_move", {r["event"] for r in rows})
            self.assertIn("element_navigation_key_received", {r["event"] for r in rows})
            self.assertIn("element_navigation_key_finished", {r["event"] for r in rows})
            self.assertIn("element_navigation_collection_filter", {r["event"] for r in rows})
            self.assertIn("element_navigation_still_attached", {r["event"] for r in rows})
            self.assertNotIn("element_navigation_after_detach", {r["event"] for r in rows})
            self.assertNotIn("PRIVATE", data)

    def test_embedded_callback_survives_writer_replacement_and_standalone_is_optional(self):
        with mock.patch.object(runtime, "_load_prototype") as load:
            runtime.start_embedded_element_navigation(object())
            callback = load.return_value.start_embedded.call_args.kwargs["diagnostic_sink"]
        first, second = mock.Mock(), mock.Mock()
        try:
            runtime.set_diagnostic_trace(first)
            callback("element_navigation_ready")
            runtime.set_diagnostic_trace(second)
            runtime.clear_diagnostic_trace(first)
            callback("element_navigation_ready")
            first.emit.assert_called_once()
            second.emit.assert_called_once()
            host._NavigationDiagnostics().emit("ready")
        finally:
            runtime.clear_diagnostic_trace(second)

    def test_startup_failure_is_available_before_bridge_trace_exists(self):
        with mock.patch.object(runtime, "_diagnostic_trace", None), \
                self.assertLogs("ovb_rc003", level="INFO") as logs:
            host._NavigationDiagnostics(runtime._emit_diagnostic, runtime._diagnostic_enabled).error(
                "worker_error", PermissionError("PRIVATE"), command="initialize")
        self.assertIn("PermissionError", logs.output[0])
        self.assertNotIn("PRIVATE", logs.output[0])

    def test_exit_failure_keeps_native_cause_in_application_log_without_a_trace(self):
        error = host.MouseInputCleanupIncompleteError("left", "PRIVATE")
        error.__cause__ = ctypes.ArgumentError("PRIVATE INPUT")
        with mock.patch.object(runtime, "_diagnostic_trace", None), \
                self.assertLogs("ovb_rc003", level="INFO") as logs:
            host._NavigationDiagnostics(runtime._emit_diagnostic, runtime._diagnostic_enabled).error(
                "worker_error", error, command="stop")
        self.assertIn("cause_type=ArgumentError", logs.output[0])
        self.assertNotIn("PRIVATE", logs.output[0])


if __name__ == "__main__":
    unittest.main()
