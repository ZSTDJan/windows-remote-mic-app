import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

from ovb_rc003 import diagnostic_trace, win32_input
from ovb_rc003 import voice_interaction_diagnostics_windows as focus
from ovb_rc003 import shortcut_observation_windows as receiver


class ProgramContextTests(unittest.TestCase):
    def test_generic_context_and_changes_are_bounded(self):
        clock = [0.0]
        first = focus.FocusSnapshot(True, foreground_pid=10, foreground_executable="Browser.exe",
                                    foreground_handle=20, focus_handle=21, keyboard_layout=1033,
                                    foreground_stable=True)
        second = focus.FocusSnapshot(True, foreground_pid=11, foreground_executable="Remote.exe",
                                     foreground_handle=30, focus_handle=31, keyboard_layout=2052,
                                     foreground_stable=True)
        capture = mock.Mock(side_effect=[first, first, second])
        timeline = focus.ContextTimeline(capture, lambda: clock[0], environment=mock.Mock(sample=lambda _: {}))
        self.assertIsNone(timeline.poll())
        timeline.accept(dict(event="attempt_started", attempt_id="a"))
        self.assertEqual(timeline.poll()["foreground_executable"], "Browser.exe")
        self.assertIsNone(timeline.poll())
        clock[0] = 0.25
        self.assertIsNone(timeline.poll())
        clock[0] = 0.5
        changed = timeline.poll()
        self.assertEqual(changed["foreground_executable"], "Remote.exe")
        self.assertEqual(changed["phase"], "changed")
        self.assertEqual(changed["keyboard_layout"], 2052)
        self.assertEqual(changed["target_response"], "unknown")
        timeline.accept(dict(event="mapping_trigger", gesture_id="ordinary"))
        self.assertEqual(timeline.context, {"attempt_id": "a"})
        timeline.accept(dict(event="attempt_finished", attempt_id="a"))
        clock[0] = 11
        self.assertEqual(timeline.poll()["phase"], "observation_ended")
        self.assertIsNone(timeline.poll())
        self.assertEqual(capture.call_count, 3)

    def test_capture_errors_and_stale_foreground_are_explicit(self):
        timeline = focus.ContextTimeline(mock.Mock(side_effect=OSError("private path")), lambda: 1,
                                         environment=mock.Mock(sample=lambda _: {}))
        timeline.accept(dict(event="mapping_trigger", gesture_id="g"))
        observed = timeline.poll()
        self.assertEqual(observed["observation_status"], "capture_failed")
        self.assertNotIn("private", json.dumps(observed))
        self.assertFalse(focus.context_fields(focus.FocusSnapshot(True))["foreground_stable"])

    @unittest.skipUnless(sys.platform == "win32", "Windows native query")
    def test_native_process_query_exposes_basename_only(self):
        name, status = focus._process_basename(os.getpid())
        self.assertEqual(status, "captured")
        self.assertEqual(name.lower(), Path(sys.executable).name.lower())
        self.assertNotIn("\\", name)
        self.assertNotIn(":", name)

    def test_no_text_query_for_timeline_context(self):
        with mock.patch.object(focus, "_capture_windows_focus", return_value=focus.FocusSnapshot(True)) as capture:
            focus.capture_focus_snapshot(platform="win32", include_text_length=False)
        capture.assert_called_once_with(include_text_length=False)


class NativeSendEvidenceTests(unittest.TestCase):
    def test_each_key_and_error_logged_without_changing_send(self):
        trace = mock.Mock(enabled=True)
        trace.current_context.return_value = {"attempt_id": "a"}
        user = mock.Mock()
        user.MapVirtualKeyW.return_value = 23
        with mock.patch.object(win32_input.ctypes, "windll", types.SimpleNamespace(user32=user)), \
             mock.patch.object(win32_input, "_diagnostic_trace", trace), \
             mock.patch.object(win32_input, "_require_live_input_allowed"), \
             mock.patch.object(win32_input, "_require_windows"), \
             mock.patch.object(diagnostic_trace, "foreground_context", return_value={"foreground_executable":"AnyApp.exe"}):
            win32_input._real_keybd_event(73, False, _extra_info=123)
            user.keybd_event.assert_called_once_with(73, 23, 0, 123)
            self.assertEqual([c.kwargs["phase"] for c in trace.emit.call_args_list], ["requested", "native_call_returned"])
            self.assertEqual(trace.emit.call_args.kwargs["target_response"], "unknown")
            user.keybd_event.side_effect = OSError("private path")
            with self.assertRaises(OSError):
                win32_input._real_keybd_event(73, True)
            self.assertEqual(trace.emit.call_args.kwargs["phase"], "native_call_failed")
            self.assertNotIn("private", str(trace.emit.call_args))
            user.keybd_event.side_effect = None
            trace.emit.side_effect = RuntimeError("broken sink")
            win32_input._real_keybd_event(73, True)


class ReceiverTests(unittest.TestCase):
    def test_filters_keys_and_keeps_origin_unknown(self):
        event = receiver._KeyboardEvent(73, 23, 0x12, 1234, 987)
        result = receiver.observed_edge(0, 0x104, event, {73})
        self.assertTrue(result["injected"])
        self.assertEqual(result["origin"], "unknown")
        self.assertNotIn("extra", result)
        self.assertIsNone(receiver.observed_edge(-1, 0x104, event, {73}))
        self.assertIsNone(receiver.observed_edge(0, 0x104, event, {165}))

    def test_rejects_bad_arguments_and_preserves_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "capture.jsonl"
            output.write_text("keep")
            self.assertEqual(receiver.main(["--keys", "ralt+i", "--output", str(output), "--dry-run"]), 2)
            self.assertEqual(output.read_text(), "keep")
            self.assertEqual(receiver.main(["--keys", "ralt+i", "--output", str(output), "--seconds", "121"]), 2)
            self.assertEqual(receiver.main(["--keys", "invalid", "--output", str(output)]), 2)

    def test_real_entry_dry_run_has_no_desktop_or_hook(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "capture.jsonl"
            result = subprocess.run([sys.executable, "-m", "ovb_rc003", receiver.FLAG,
                                     "--keys", "ralt+i", "--output", str(output), "--dry-run"],
                                    capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            rows = [json.loads(x) for x in output.read_text().splitlines()]
            self.assertEqual([r["event"] for r in rows], ["observation_started", "observation_finished"])
            self.assertEqual(rows[-1]["result"], "dry_run")
            self.assertIn(73, rows[0]["keys"])
            self.assertIn(165, rows[0]["keys"])

    @unittest.skipUnless(sys.platform == "win32", "Windows callback type")
    def test_mock_hook_always_passes_and_unhooks_without_live_input(self):
        user, kernel, writer = mock.Mock(), mock.Mock(), mock.Mock()
        writer.failed = False
        callback = []
        user.SetWindowsHookExW.side_effect = lambda kind, fn, module, thread: callback.append(fn) or 77
        user.CallNextHookEx.return_value = 987
        user.UnhookWindowsHookEx.return_value = 1
        payload = receiver._KeyboardEvent(73, 23, 0x10, 123, 0)
        def peek(*args):
            self.assertEqual(callback[0](0, 0x100, ctypes.addressof(payload)), 987)
            self.assertEqual(callback[0](-1, 0x100, 0), 987)
            return False
        user.PeekMessageW.side_effect = peek
        ticks = iter([0, .1, .2, .3, 2])
        with mock.patch.object(ctypes, "WinDLL", side_effect=lambda name, **kw: user if name == "user32" else kernel), \
             mock.patch.object(receiver.time, "monotonic", side_effect=lambda: next(ticks)), \
             mock.patch.object(receiver.time, "sleep"), \
             mock.patch.object(receiver, "foreground_context", return_value={"foreground_pid":1}), \
             mock.patch.object(receiver, "EnvironmentSampler", return_value=mock.Mock(sample=lambda _: {})):
            result = receiver.observe({73}, 1, writer)
        self.assertEqual(result, "completed")
        user.UnhookWindowsHookEx.assert_called_once_with(77)
        self.assertEqual(user.CallNextHookEx.call_count, 2)
        self.assertEqual(writer.emit.call_args_list[0].args[0]["vk"], 73)
