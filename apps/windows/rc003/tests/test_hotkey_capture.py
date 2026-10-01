import threading
import unittest

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


if __name__ == "__main__":
    unittest.main()
