import ctypes
import json
import logging
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock
import zipfile

from ovb_rc003 import hotkey, hotkey_capture_windows, win32_keys


class KeyboardTokenTests(unittest.TestCase):
    def test_keypad_enter_cannot_silently_turn_into_main_enter(self):
        self.assertEqual(hotkey_capture_windows.token_for_keyboard_event(0x0D, 0x1C, 0), "enter")
        token = hotkey_capture_windows.token_for_keyboard_event(0x0D, 0x1C, 1)
        self.assertEqual(token, "numpad_enter")
        with self.assertRaisesRegex(hotkey.HotkeyParseError, "小键盘 Enter"):
            hotkey.HotkeySpec.from_user_text(token, mapping=True)

    def test_directional_modifiers_keep_their_physical_side(self):
        self.assertEqual(
            hotkey_capture_windows.token_for_keyboard_event(0xA2, 0x1D, 0),
            "lctrl",
        )
        self.assertEqual(
            hotkey_capture_windows.token_for_keyboard_event(
                0x11, 0x1D, hotkey_capture_windows.LLKHF_EXTENDED
            ),
            "rctrl",
        )
        self.assertEqual(
            hotkey_capture_windows.token_for_keyboard_event(0xA5, 0x38, 0x21),
            "ralt",
        )
        self.assertEqual(
            hotkey_capture_windows.token_for_keyboard_event(0x5B, 0x5B, 0x01),
            "lwin",
        )

    def test_unknown_virtual_keys_round_trip_through_dynamic_token(self):
        token = hotkey_capture_windows.token_for_keyboard_event(0xE2, 0x56, 0)
        self.assertEqual(token, "vk_e2")
        self.assertEqual(win32_keys.resolve_vk_codes((token,)), [0xE2])

    def test_numlock_off_keypad_navigation_cannot_turn_into_main_keys(self):
        cases = (
            (0x0C, 0x4C, "clear"), (0x21, 0x49, "page_up"),
            (0x22, 0x51, "page_down"), (0x23, 0x4F, "end"),
            (0x24, 0x47, "home"), (0x25, 0x4B, "left"),
            (0x26, 0x48, "up"), (0x27, 0x4D, "right"),
            (0x28, 0x50, "down"), (0x2D, 0x52, "insert"),
            (0x2E, 0x53, "delete"),
        )
        for vk, scan, name in cases:
            with self.subTest(name=name):
                token = hotkey_capture_windows.token_for_keyboard_event(vk, scan, 0)
                self.assertEqual(token, "numpad_" + name)
                for text in (token, win32_keys.key_label(token)):
                    with self.assertRaisesRegex(hotkey.HotkeyParseError, "NumLock"):
                        hotkey.HotkeySpec.from_user_text(text, mapping=True)
                if name != "clear":
                    self.assertEqual(hotkey_capture_windows.token_for_keyboard_event(vk, scan, 1), name)
                    self.assertEqual(hotkey_capture_windows.token_for_keyboard_event(vk, 0, 0), name)

    def test_out_of_range_vk_is_not_truncated_into_a_different_key(self):
        for vk in (-1, 0x141, 0x1A2):
            with self.subTest(vk=vk):
                self.assertEqual(hotkey_capture_windows.token_for_keyboard_event(vk, 0, 0), "unsupported_key")


class HotkeyCaptureStateTests(unittest.TestCase):
    def _event(self, vk, scan, flags=0):
        return hotkey_capture_windows.KBDLLHOOKSTRUCT(
            vkCode=vk,
            scanCode=scan,
            flags=flags,
            time=0,
            dwExtraInfo=0,
        )

    def test_real_key_down_order_is_emitted_after_the_last_key_up(self):
        captured = []
        recorder = hotkey_capture_windows.HotkeyCapture(captured.append)

        self.assertTrue(
            recorder._handle_event(
                hotkey_capture_windows.WM_KEYDOWN,
                self._event(0xA2, 0x1D),
            )
        )
        self.assertTrue(
            recorder._handle_event(
                hotkey_capture_windows.WM_KEYDOWN,
                self._event(0x5B, 0x5B, hotkey_capture_windows.LLKHF_EXTENDED),
            )
        )
        self.assertEqual(captured, [])
        recorder._handle_event(
            hotkey_capture_windows.WM_KEYUP,
            self._event(
                0x5B,
                0x5B,
                hotkey_capture_windows.LLKHF_EXTENDED
                | hotkey_capture_windows.LLKHF_UP,
            ),
        )
        self.assertEqual(captured, [])
        recorder._handle_event(
            hotkey_capture_windows.WM_KEYUP,
            self._event(0xA2, 0x1D, hotkey_capture_windows.LLKHF_UP),
        )
        self.assertEqual(captured, ["lctrl+lwin"])

    def test_three_key_custom_shortcut_can_record_physical_modifier_sides(self):
        captured = []
        recorder = hotkey_capture_windows.HotkeyCapture(captured.append)

        for vk, scan in ((0xA2, 0x1D), (0xA4, 0x38), (0x77, 0x42)):
            self.assertTrue(
                recorder._handle_event(
                    hotkey_capture_windows.WM_KEYDOWN,
                    self._event(vk, scan),
                )
            )
        for vk, scan in ((0x77, 0x42), (0xA4, 0x38), (0xA2, 0x1D)):
            recorder._handle_event(
                hotkey_capture_windows.WM_KEYUP,
                self._event(vk, scan, hotkey_capture_windows.LLKHF_UP),
            )

        self.assertEqual(captured, ["lctrl+lalt+f8"])
        spec = hotkey.HotkeySpec.parse(captured[0])
        self.assertEqual(spec.serialize(), "lctrl+lalt+f8")
        self.assertEqual(
            win32_keys.resolve_vk_codes((*spec.modifiers, spec.key)),
            [0xA2, 0xA4, 0x77],
        )

    def test_injected_events_are_not_recorded_or_suppressed(self):
        captured = []
        recorder = hotkey_capture_windows.HotkeyCapture(captured.append)
        self.assertFalse(
            recorder._handle_event(
                hotkey_capture_windows.WM_KEYDOWN,
                self._event(0x41, 0, hotkey_capture_windows.LLKHF_INJECTED),
            )
        )
        self.assertEqual(captured, [])

    def test_explicit_recorder_accepts_remote_injected_shortcut(self):
        captured = []
        recorder = hotkey_capture_windows.HotkeyCapture(
            captured.append,
            accept_injected=True,
        )

        self.assertTrue(
            recorder._handle_event(
                hotkey_capture_windows.WM_KEYDOWN,
                self._event(
                    0x41,
                    0x1E,
                    hotkey_capture_windows.LLKHF_INJECTED,
                ),
            )
        )
        self.assertTrue(
            recorder._handle_event(
                hotkey_capture_windows.WM_KEYUP,
                self._event(
                    0x41,
                    0x1E,
                    hotkey_capture_windows.LLKHF_INJECTED
                    | hotkey_capture_windows.LLKHF_UP,
                ),
            )
        )
        self.assertEqual(captured, ["a"])

    def test_key_up_without_a_captured_down_is_not_suppressed(self):
        captured = []
        recorder = hotkey_capture_windows.HotkeyCapture(captured.append)

        self.assertFalse(
            recorder._handle_event(
                hotkey_capture_windows.WM_KEYUP,
                self._event(0x26, 0x48, hotkey_capture_windows.LLKHF_UP),
            )
        )
        self.assertEqual(captured, [])

    def test_remote_injected_key_up_without_owned_down_is_not_suppressed(self):
        captured = []
        recorder = hotkey_capture_windows.HotkeyCapture(
            captured.append,
            accept_injected=True,
        )

        self.assertFalse(
            recorder._handle_event(
                hotkey_capture_windows.WM_KEYUP,
                self._event(
                    0x41,
                    0x1E,
                    hotkey_capture_windows.LLKHF_INJECTED
                    | hotkey_capture_windows.LLKHF_UP,
                ),
            )
        )
        self.assertEqual(captured, [])

    def test_key_held_before_capture_passes_repeats_and_release_through(self):
        captured = []
        recorder = hotkey_capture_windows.HotkeyCapture(captured.append)
        recorder._passthrough_vks.add(0x26)

        self.assertFalse(
            recorder._handle_event(
                hotkey_capture_windows.WM_KEYDOWN,
                self._event(0x26, 0x48),
            )
        )
        self.assertFalse(
            recorder._handle_event(
                hotkey_capture_windows.WM_KEYUP,
                self._event(0x26, 0x48, hotkey_capture_windows.LLKHF_UP),
            )
        )
        self.assertEqual(recorder._passthrough_vks, set())
        self.assertEqual(captured, [])

        self.assertTrue(
            recorder._handle_event(
                hotkey_capture_windows.WM_KEYDOWN,
                self._event(0x26, 0x48),
            )
        )

    def test_events_are_not_swallowed_after_stop_has_started(self):
        recorder = hotkey_capture_windows.HotkeyCapture(lambda _chord: None)
        recorder._stop_event.set()

        self.assertFalse(
            recorder._handle_event(
                hotkey_capture_windows.WM_KEYDOWN,
                self._event(0x26, 0x48),
            )
        )
        self.assertEqual(recorder._pressed_tokens, set())


class HotkeyCaptureLifecycleTests(unittest.TestCase):
    def test_start_timeout_retains_a_thread_that_did_not_stop(self):
        recorder = hotkey_capture_windows.HotkeyCapture(lambda _chord: None)
        release = threading.Event()

        def fake_run():
            release.wait()

        try:
            with self.assertRaises(
                hotkey_capture_windows.HotkeyCaptureUnavailableError
            ):
                recorder.start(start_timeout=0.01, _run_target=fake_run)
            self.assertTrue(recorder.is_running)
        finally:
            release.set()
            recorder.stop()

    def test_start_error_retains_a_thread_until_it_really_exits(self):
        recorder = hotkey_capture_windows.HotkeyCapture(lambda _chord: None)
        release = threading.Event()

        def fake_run():
            recorder._start_error = RuntimeError("simulated startup error")
            recorder._ready_event.set()
            release.wait()

        try:
            with self.assertRaises(
                hotkey_capture_windows.HotkeyCaptureUnavailableError
            ):
                recorder.start(_run_target=fake_run)
            self.assertTrue(recorder.is_running)
        finally:
            release.set()
            recorder.stop()


@unittest.skipUnless(sys.platform == "win32", "requires the Windows ctypes callback ABI")
class HotkeyCaptureDiagnosticsTests(unittest.TestCase):
    """Real ctypes callbacks with isolated Win32 calls: no hook or input is installed."""

    def setUp(self):
        self.logger = logging.Logger("capture-test", logging.INFO)
        self.logger.propagate = False
        self.records = []
        handler = logging.Handler()
        handler.emit = self.records.append
        self.logger.addHandler(handler)
        patcher = mock.patch.object(hotkey_capture_windows, "_LOGGER", self.logger)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.unraisable = []
        patcher = mock.patch.object(sys, "unraisablehook", self.unraisable.append)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_callback(self, recorder, events=(), *, message_result=0,
                     cancel=False, unhook=True, install_error=None, forward_error=None):
        # Every OS input/queue API is replaced; WINFUNCTYPE itself is real.
        user = SimpleNamespace(**{name: mock.Mock(return_value=0) for name in (
            "PeekMessageW", "GetMessageW", "TranslateMessage", "DispatchMessageW",
            "SetWindowsHookExW", "CallNextHookEx", "UnhookWindowsHookEx",
            "GetAsyncKeyState", "PostThreadMessageW",
        )})
        kernel = SimpleNamespace(GetCurrentThreadId=mock.Mock(return_value=42),
                                 GetModuleHandleW=mock.Mock(return_value=1))
        user.SetWindowsHookExW.return_value = 7
        user.SetWindowsHookExW.side_effect = install_error
        user.UnhookWindowsHookEx.return_value = unhook
        user.CallNextHookEx.return_value = 73
        user.CallNextHookEx.side_effect = forward_error
        returned = []

        def dispatch(*_args):
            callback = user.SetWindowsHookExW.call_args.args[1]
            self.assertIsInstance(callback, ctypes._CFuncPtr)
            for code, message, data in events:
                pointer = ctypes.addressof(data) if data is not None else 0
                returned.append(callback(code, message, pointer))
            if cancel:
                recorder._stop_event.set()
            return message_result

        user.GetMessageW.side_effect = dispatch
        with mock.patch.object(ctypes, "windll", SimpleNamespace(user32=user, kernel32=kernel)), \
                mock.patch.object(sys, "stderr", None):
            recorder._run()
        return user, returned

    def event(self, vk=0x30, scan=0x0B, *, up=False, flags=0):
        flags |= hotkey_capture_windows.LLKHF_UP if up else 0
        data = hotkey_capture_windows.KBDLLHOOKSTRUCT(vk, scan, flags, 0, 0)
        return (0, hotkey_capture_windows.WM_KEYUP if up else
                hotkey_capture_windows.WM_KEYDOWN, data)

    def summaries(self):
        return [(record.getMessage().split("reason=", 1)[1].split(" ", 1)[0],
                 json.loads(record.getMessage().split(" state=", 1)[1]))
                for record in self.records if "diagnostics reason=" in record.getMessage()]

    def test_complete_chord_and_both_forwarding_paths_keep_their_results(self):
        captured = []
        recorder = hotkey_capture_windows.HotkeyCapture(captured.append, accept_injected=True)
        events = [(-1, 0, None), self.event(0x1B, 1)]
        events += [self.event(vk, scan, flags=flags) for vk, scan, flags in
                   ((0xA3, 0x1D, 1), (0xA1, 0x36, 0), (0x30, 0x0B, 0))]
        events += [self.event(vk, scan, up=True, flags=flags) for vk, scan, flags in
                   ((0x30, 0x0B, 0), (0xA1, 0x36, 0), (0xA3, 0x1D, 1))]
        user, returned = self.run_callback(recorder, events)
        self.assertEqual(captured, ["rctrl+rshift+0"])
        self.assertEqual(returned, [73, 73, 1, 1, 1, 1, 1, 1])
        self.assertEqual(user.CallNextHookEx.call_count, 2)
        self.assertEqual(self.unraisable, [])
        reason, state = self.summaries()[-1]
        self.assertEqual(reason, "thread_exit")
        self.assertEqual((state["callback_entries"], state["callback_returns"],
                          state["callback_errors"]), (8, 8, 0))
        self.assertEqual(state["result_deliveries"], 1)
        self.assertTrue(state["unhook_succeeded"])
        self.assertNotIn("rctrl", json.dumps(state))

    def test_zero_callbacks_can_be_distinguished_from_cancel_and_message_failure(self):
        for cancel, result in ((False, 0), (True, 0), (False, -1)):
            with self.subTest(cancel=cancel, result=result):
                recorder = hotkey_capture_windows.HotkeyCapture(lambda _: None)
                self.run_callback(recorder, cancel=cancel, message_result=result)
                state = self.summaries()[-1][1]
                self.assertEqual(state["callback_entries"], 0)
                self.assertEqual(state["callback_stage"], "not_entered")
                self.assertEqual(state["stop_requested"], cancel)
                self.assertEqual(state["message_result"], result)
                self.assertEqual(state["message_calls"], 1)

    def test_callback_failures_survive_missing_stderr_with_exact_stage_and_no_text(self):
        cases = ("read_event", "log_event", "decode_token", "forward_event")
        for stage in cases:
            with self.subTest(stage=stage):
                recorder = hotkey_capture_windows.HotkeyCapture(lambda _: None)
                error = RuntimeError("PRIVATE diagnostic payload")
                patcher = (mock.patch.object(self.logger, "info", side_effect=error)
                           if stage == "log_event" else
                           mock.patch.object(hotkey_capture_windows, "token_for_keyboard_event",
                                             side_effect=error)
                           if stage == "decode_token" else mock.patch.object(sys, "stderr", None))
                with patcher:
                    self.run_callback(
                        recorder,
                        [(0, hotkey_capture_windows.WM_KEYDOWN, None)] if stage == "read_event"
                        else [(-1, 0, None)] if stage == "forward_event" else [self.event()],
                        forward_error=error if stage == "forward_event" else None,
                    )
                state = self.summaries()[-1][1]
                self.assertEqual(state["callback_entries"], 1)
                self.assertEqual(state["callback_returns"], 0)
                self.assertEqual(state["callback_errors"], 1)
                self.assertEqual(state["first_error_stage"], stage)
                self.assertEqual(state["first_error_type"],
                                 "ValueError" if stage == "read_event" else "RuntimeError")
                self.assertNotIn("PRIVATE", json.dumps(state))
                self.assertEqual(len(self.unraisable), cases.index(stage) + 1)

    def test_receiver_failure_is_recorded_without_changing_existing_cleanup(self):
        recorder = hotkey_capture_windows.HotkeyCapture(
            mock.Mock(side_effect=RuntimeError("PRIVATE receiver")))
        self.run_callback(recorder, [self.event(), self.event(up=True)])
        state = self.summaries()[-1][1]
        self.assertEqual(state["callback_errors"], 0)
        self.assertEqual(state["callback_returns"], 2)
        self.assertEqual(state["result_delivery_errors"], 1)
        self.assertEqual(state["first_error_stage"], "deliver_result")
        self.assertEqual(state["result_deliveries"], 0)
        self.assertTrue(state["stop_requested"])
        self.assertTrue(state["unhook_succeeded"])

    def test_install_and_unhook_failures_cannot_look_like_empty_success(self):
        recorder = hotkey_capture_windows.HotkeyCapture(lambda _: None)
        self.run_callback(recorder, install_error=OSError("PRIVATE native error"))
        state = self.summaries()[-1][1]
        self.assertEqual(state["worker_error_stage"], "install_hook")
        self.assertEqual(state["worker_error_type"], "OSError")
        self.assertIsNone(state["unhook_succeeded"])
        self.run_callback(hotkey_capture_windows.HotkeyCapture(lambda _: None), unhook=False)
        self.assertFalse(self.summaries()[-1][1]["unhook_succeeded"])

    def test_diagnostic_writer_failure_does_not_replace_capture_state_or_escape(self):
        recorder = hotkey_capture_windows.HotkeyCapture(lambda _: None)
        with mock.patch.object(self.logger, "log", side_effect=OSError("PRIVATE log path")):
            recorder._log_diagnostics("ready")
        recorder._log_diagnostics("thread_exit")
        state = self.summaries()[-1][1]
        self.assertEqual(state["diagnostic_log_errors"], 1)
        self.assertEqual(state["diagnostic_log_error_type"], "OSError")
        self.assertNotIn("PRIVATE", json.dumps(state))

    def test_restart_resets_evidence_and_assigns_a_distinct_capture_id(self):
        recorder = hotkey_capture_windows.HotkeyCapture(lambda _: None)
        self.run_callback(recorder, [(0, hotkey_capture_windows.WM_KEYDOWN, None)])
        previous = recorder._diagnostics["capture_id"]
        with mock.patch.object(recorder, "_post_quit"):
            recorder.start(_run_target=recorder._ready_event.set)
            recorder.stop()
        self.assertNotEqual(recorder._diagnostics["capture_id"], previous)
        self.assertEqual(recorder._diagnostics["callback_errors"], 0)
        self.assertEqual(recorder._diagnostics["first_error_type"], "none")

    def test_stop_timeout_records_the_inflight_stage_without_waiting_for_state_lock(self):
        recorder = hotkey_capture_windows.HotkeyCapture(lambda _: None)
        recorder._thread = mock.Mock()
        recorder._thread.is_alive.return_value = True
        recorder._diagnostics.update(callback_entries=1, callback_stage="update_state")
        recorder._state_lock.acquire()
        try:
            with mock.patch.object(recorder, "_post_quit"), \
                    self.assertRaises(hotkey_capture_windows.HotkeyCaptureUnavailableError):
                recorder.stop()
        finally:
            recorder._state_lock.release()
        reason, state = self.summaries()[-1]
        self.assertEqual(reason, "stop_timeout")
        self.assertEqual(state["callback_stage"], "update_state")
        self.assertEqual(state["callback_entries"], 1)
        self.assertEqual(state["callback_returns"], 0)

    def test_callback_evidence_reaches_the_real_exported_zip(self):
        from ovb_rc003 import logging_setup, log_export
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            handler = logging_setup.EvidenceFileHandler(
                root / "logs" / "app.log", asynchronous=True, encoding="utf-8",
                maxBytes=logging_setup.LOG_MAX_BYTES, backupCount=1)
            handler.addFilter(logging_setup.PrivacySafeExceptionFilter())
            self.logger.addHandler(handler)
            try:
                # Exercise outcomes, then inspect the archive, not just mocked log calls.
                captured = []
                self.run_callback(hotkey_capture_windows.HotkeyCapture(captured.append),
                                  [self.event(), self.event(up=True)])
                self.assertEqual(captured, ["0"])
                self.run_callback(hotkey_capture_windows.HotkeyCapture(lambda _: None))
                self.run_callback(hotkey_capture_windows.HotkeyCapture(lambda _: None), cancel=True)
                with mock.patch.object(sys, "unraisablehook", sys.__unraisablehook__):
                    self.run_callback(hotkey_capture_windows.HotkeyCapture(lambda _: None),
                                      [(0, hotkey_capture_windows.WM_KEYDOWN, None)])
                self.run_callback(hotkey_capture_windows.HotkeyCapture(lambda _: None),
                                  install_error=OSError("PRIVATE install failure"))
                timeout_recorder = hotkey_capture_windows.HotkeyCapture(lambda _: None)
                with self.assertRaises(hotkey_capture_windows.HotkeyCaptureUnavailableError):
                    timeout_recorder.start(start_timeout=0.001, _run_target=lambda: None)
                result = log_export.export_logs(root / "evidence.zip", root=root)
                self.assertEqual(result.outcome, "exported")
                self.assertFalse(result.incomplete)
                with zipfile.ZipFile(result.path) as archive:
                    self.assertIsNone(archive.testzip())
                    lines = archive.read("app.log").decode("utf-8").splitlines()
                    states = [json.loads(line.split(" state=", 1)[1]) for line in lines
                              if "diagnostics reason=" in line]
                    self.assertEqual([s["callback_entries"] for s in states], [2, 0, 0, 1, 0, 0])
                    self.assertEqual([s["callback_errors"] for s in states], [0, 0, 0, 1, 0, 0])
                    self.assertEqual(states[0]["result_deliveries"], 1)
                    self.assertFalse(states[1]["stop_requested"])
                    self.assertTrue(states[2]["stop_requested"])
                    self.assertEqual(states[3]["first_error_stage"], "read_event")
                    self.assertEqual(states[4]["worker_error_stage"], "install_hook")
                    self.assertIn("reason=start_timeout", lines[-1])
                    report = archive.read("diagnostic-report.json").decode("utf-8")
                    self.assertIn("hotkey_capture_diagnostics", report)
                    self.assertIn("read_event", report)
                    manifest = json.loads(archive.read("export-info.json"))
                    self.assertTrue(manifest["application_log_flushed"])
                    self.assertFalse(manifest["includes_configuration"])
            finally:
                self.logger.removeHandler(handler)
                handler.close()


if __name__ == "__main__":
    unittest.main()
