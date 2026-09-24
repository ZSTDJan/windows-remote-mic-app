import unittest
from unittest import mock

from ovb_rc003 import voice_interaction_diagnostics_windows as diagnostics


class VoiceInteractionDiagnosticsTests(unittest.TestCase):
    def test_native_input_metadata_and_unknown_controls(self):
        info = diagnostics._GuiThreadInfo()
        info.hwndFocus = 20
        info.hwndCaret = 20
        user32 = mock.Mock()
        for name, style, editable, read_only, status in (
            ("Edit", 0, True, False, "native_edit_style"),
            ("RichEditD2DPT", 0x0800, False, True, "native_edit_style"),
            ("RICHEDIT50W", 0x08000000, False, False, "native_edit_style"),
            ("Chrome_WidgetWin_1", 0, None, None, "unsupported_control"),
            ("Button", 0, None, None, "unsupported_control"),
        ):
            with self.subTest(name=name):
                user32.GetWindowLongW.return_value = style
                result = diagnostics._focus_input_state(user32, 20, name, info, True, True)
                self.assertEqual(result["focus_editable"], editable)
                self.assertEqual(result["focus_read_only"], read_only)
                self.assertEqual(result["focus_input_status"], status)
                self.assertTrue(result["focus_native_caret_detected"])
        info.hwndCaret = 0
        result = diagnostics._focus_input_state(user32, 20, "Chrome_WidgetWin_1", info, True, True)
        self.assertFalse(result["focus_native_caret_detected"])
        self.assertIsNone(result["focus_editable"])

    def test_missing_or_unstable_focus_never_becomes_user_error(self):
        info = diagnostics._GuiThreadInfo()
        info.hwndFocus = 20
        user32 = mock.Mock()
        for gui_ok, stable, focus in ((False, True, 20), (True, False, 20), (True, True, 21)):
            result = diagnostics._focus_input_state(user32, focus, "Edit", info, gui_ok, stable)
            self.assertIsNone(result["focus_editable"])
            self.assertIsNone(result["focus_read_only"])
            self.assertIsNone(result["focus_native_caret_detected"])
        user32.GetWindowLongW.assert_not_called()

    def test_style_failure_preserves_independent_caret_observation(self):
        info = diagnostics._GuiThreadInfo()
        info.hwndFocus = info.hwndCaret = 20
        user32 = mock.Mock()
        user32.GetWindowLongW.side_effect = OSError("denied")
        result = diagnostics._focus_input_state(user32, 20, "Edit", info, True, True)
        self.assertIsNone(result["focus_editable"])
        self.assertEqual(result["focus_input_status"], "style_unavailable")
        self.assertTrue(result["focus_native_caret_detected"])

    def test_context_includes_input_metadata_without_text(self):
        snapshot = diagnostics.FocusSnapshot(True, focus_editable=True, focus_read_only=False,
            focus_native_caret_detected=True, focus_input_status="native_edit_style", text_length=9)
        fields = diagnostics.context_fields(snapshot)
        self.assertTrue(fields["focus_editable"])
        self.assertFalse(fields["focus_read_only"])
        self.assertTrue(fields["focus_native_caret_detected"])
        self.assertEqual(fields["focus_input_status"], "native_edit_style")
        self.assertNotIn("text_length", fields)
        self.assertIsNone(diagnostics.context_fields(diagnostics.FocusSnapshot(False))["focus_editable"])

    def test_off_windows_is_explicitly_unsupported(self):
        snapshot = diagnostics.capture_focus_snapshot(platform="linux")

        self.assertFalse(snapshot.supported)
        self.assertEqual(snapshot.error, "unsupported_platform")

    def test_capture_failure_does_not_escape_the_voice_path(self):
        with mock.patch.object(
            diagnostics,
            "_capture_windows_focus",
            side_effect=OSError("simulated"),
        ):
            snapshot = diagnostics.capture_focus_snapshot(platform="win32")

        self.assertTrue(snapshot.supported)
        self.assertEqual(snapshot.error, "capture_failed")

    def test_compare_reports_focus_and_text_length_without_text_content(self):
        before = diagnostics.FocusSnapshot(
            True,
            foreground_pid=10,
            foreground_class="WindowClass",
            focus_handle=20,
            focus_class="Edit",
            text_length=4,
        )
        after = diagnostics.FocusSnapshot(
            True,
            foreground_pid=10,
            foreground_class="WindowClass",
            focus_handle=20,
            focus_class="Edit",
            text_length=9,
        )

        observation = diagnostics.compare_submission(before, after)

        self.assertEqual(observation.focus_state, "same")
        self.assertEqual(observation.text_state, "grew")
        self.assertEqual(observation.text_delta, 5)

    def test_compare_keeps_focus_result_when_text_length_is_unavailable(self):
        before = diagnostics.FocusSnapshot(True, foreground_pid=10, focus_handle=20)
        after = diagnostics.FocusSnapshot(True, foreground_pid=10, focus_handle=20)

        observation = diagnostics.compare_submission(before, after)

        self.assertEqual(observation.focus_state, "same")
        self.assertEqual(observation.text_state, "unavailable")
        self.assertIsNone(observation.text_delta)


if __name__ == "__main__":
    unittest.main()
