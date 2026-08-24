import threading
import unittest

from ovb_rc003 import hotkey, hotkey_capture_windows, win32_keys


class KeyboardTokenTests(unittest.TestCase):
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
        token = hotkey_capture_windows.token_for_keyboard_event(0xE7, 0, 0)
        self.assertEqual(token, "vk_e7")
        self.assertEqual(win32_keys.resolve_vk_codes((token,)), [0xE7])


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


if __name__ == "__main__":
    unittest.main()
