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
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ovb_rc003 import element_navigation_runtime as runtime, log_export
from ovb_rc003.diagnostic_trace import DiagnosticTrace


prototype = runtime._load_prototype()
host = prototype._load_element_navigation_windows_host()
TREE = ast.parse(Path(host.__file__).read_text(encoding="utf-8"))
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

    def test_mapped_local_action_reaches_real_worker_queue_and_moves_selection(self):
        self.scan()
        self.worker.selected = 0
        self.host_actions()
        self.ns["active"].set()
        self.worker.post = lambda action, value: self.worker._move(host.Direction(value)) if action == "move" else None
        local = host._LocalNavigationInput(lambda vk: True,
            lambda action: self.ns["keyboard_events"].put((action, 0)), self.diagnostics)
        embedded = host.EmbeddedElementNavigationRuntime(mock.Mock(), mock.Mock(), mock.Mock(), local)
        self.assertTrue(embedded.route_mapped_key(host.VK_RIGHT))
        self.ns["drain_events"]()
        self.assertEqual(self.worker.selected, 1)

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


if __name__ == "__main__":
    unittest.main()
