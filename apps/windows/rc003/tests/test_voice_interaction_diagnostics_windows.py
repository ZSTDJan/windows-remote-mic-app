import unittest
from unittest import mock

from ovb_rc003 import voice_interaction_diagnostics_windows as diagnostics


class VoiceInteractionDiagnosticsTests(unittest.TestCase):
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
