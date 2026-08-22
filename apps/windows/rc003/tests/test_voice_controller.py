import unittest

from ovb_rc003.key_mapping import VoiceTriggerMode
from ovb_rc003.voice_controller import VoiceController, VoiceHostAction


class ToggleModeTests(unittest.TestCase):
    """Toggle follows user presses, not the device's short-press release."""

    def test_press_issues_a_tap(self):
        controller = VoiceController(VoiceTriggerMode.TOGGLE)
        self.assertEqual(controller.on_mic_button_pressed(), VoiceHostAction.TAP)

    def test_toggle_mode_never_reports_holding(self):
        controller = VoiceController(VoiceTriggerMode.TOGGLE)
        controller.on_mic_button_pressed()
        self.assertFalse(controller.holding)

    def test_press_marks_the_session_active(self):
        controller = VoiceController(VoiceTriggerMode.TOGGLE)
        controller.on_mic_button_pressed()
        self.assertTrue(controller.active)

    def test_repeated_presses_alternate_active_state_and_each_issue_a_tap(self):
        controller = VoiceController(VoiceTriggerMode.TOGGLE)
        self.assertEqual(controller.on_mic_button_pressed(), VoiceHostAction.TAP)
        self.assertTrue(controller.active)
        self.assertEqual(controller.on_mic_button_pressed(), VoiceHostAction.TAP)
        self.assertFalse(controller.active)
        self.assertEqual(controller.on_mic_button_pressed(), VoiceHostAction.TAP)
        self.assertTrue(controller.active)

    def test_audio_stopped_after_a_press_keeps_toggle_active(self):
        controller = VoiceController(VoiceTriggerMode.TOGGLE)
        controller.on_mic_button_pressed()
        self.assertIsNone(controller.on_audio_stopped())
        self.assertTrue(controller.active)

    def test_audio_stopped_without_a_prior_press_does_nothing(self):
        controller = VoiceController(VoiceTriggerMode.TOGGLE)
        self.assertIsNone(controller.on_audio_stopped())

    def test_repeated_audio_stopped_events_do_not_change_toggle_state(self):
        controller = VoiceController(VoiceTriggerMode.TOGGLE)
        controller.on_mic_button_pressed()
        self.assertIsNone(controller.on_audio_stopped())
        self.assertIsNone(controller.on_audio_stopped())
        self.assertTrue(controller.active)

    def test_reset_after_a_press_issues_the_closing_tap(self):
        controller = VoiceController(VoiceTriggerMode.TOGGLE)
        controller.on_mic_button_pressed()
        self.assertEqual(controller.reset(), VoiceHostAction.TAP)
        self.assertFalse(controller.active)

    def test_reset_after_audio_stopped_still_closes_the_active_toggle(self):
        controller = VoiceController(VoiceTriggerMode.TOGGLE)
        controller.on_mic_button_pressed()
        controller.on_audio_stopped()
        self.assertEqual(controller.reset(), VoiceHostAction.TAP)
        self.assertFalse(controller.active)

    def test_cancel_pending_clears_active_without_an_action(self):
        controller = VoiceController(VoiceTriggerMode.TOGGLE)
        controller.on_mic_button_pressed()
        controller.cancel_pending()
        self.assertFalse(controller.active)
        self.assertIsNone(controller.reset())


class HoldModeTests(unittest.TestCase):
    def test_press_holds_key_down(self):
        controller = VoiceController(VoiceTriggerMode.HOLD)
        self.assertEqual(controller.on_mic_button_pressed(), VoiceHostAction.KEY_DOWN)
        self.assertTrue(controller.holding)

    def test_audio_stopped_releases_key(self):
        controller = VoiceController(VoiceTriggerMode.HOLD)
        controller.on_mic_button_pressed()
        self.assertEqual(controller.on_audio_stopped(), VoiceHostAction.KEY_UP)
        self.assertFalse(controller.holding)

    def test_physical_release_releases_key_before_audio_stop(self):
        controller = VoiceController(VoiceTriggerMode.HOLD)
        controller.on_mic_button_pressed()

        self.assertEqual(
            controller.on_mic_button_released(), VoiceHostAction.KEY_UP
        )
        self.assertFalse(controller.active)
        self.assertIsNone(controller.on_audio_stopped())

    def test_audio_stopped_without_a_prior_press_does_nothing(self):
        controller = VoiceController(VoiceTriggerMode.HOLD)
        self.assertIsNone(controller.on_audio_stopped())

    def test_repeated_press_without_stop_stays_down(self):
        controller = VoiceController(VoiceTriggerMode.HOLD)
        controller.on_mic_button_pressed()
        self.assertEqual(controller.on_mic_button_pressed(), VoiceHostAction.KEY_DOWN)
        self.assertTrue(controller.holding)

    def test_press_marks_the_session_active(self):
        controller = VoiceController(VoiceTriggerMode.HOLD)
        controller.on_mic_button_pressed()
        self.assertTrue(controller.active)

    def test_cancel_pending_clears_active_without_an_action(self):
        controller = VoiceController(VoiceTriggerMode.HOLD)
        controller.on_mic_button_pressed()
        controller.cancel_pending()
        self.assertFalse(controller.active)
        self.assertFalse(controller.holding)
        self.assertIsNone(controller.reset())


class ResetTests(unittest.TestCase):
    def test_reset_releases_a_held_key_provably(self):
        controller = VoiceController(VoiceTriggerMode.HOLD)
        controller.on_mic_button_pressed()
        self.assertEqual(controller.reset(), VoiceHostAction.KEY_UP)
        self.assertFalse(controller.holding)

    def test_reset_is_a_no_op_when_not_held(self):
        controller = VoiceController(VoiceTriggerMode.HOLD)
        self.assertIsNone(controller.reset())


class RestorePendingTests(unittest.TestCase):
    """XRBM-019 review round 1 P1 #4: reset()/on_audio_stopped() clear
    outstanding state BEFORE the caller has confirmed the closing action
    actually delivered. restore_pending() lets a caller undo that eager
    clear when delivery is later found to have failed - see app.py's
    _cleanup_once/_on_control_event, which now check
    _apply_voice_action()'s return value.
    """

    def test_restores_holding_after_a_failed_hold_mode_key_up(self):
        controller = VoiceController(VoiceTriggerMode.HOLD)
        controller.on_mic_button_pressed()
        action = controller.reset()
        self.assertEqual(action, VoiceHostAction.KEY_UP)
        self.assertFalse(controller.holding)  # reset() already cleared it

        controller.restore_pending(action)

        self.assertTrue(controller.holding)
        self.assertTrue(controller.active)

    def test_restores_toggle_active_after_a_failed_closing_tap(self):
        controller = VoiceController(VoiceTriggerMode.TOGGLE)
        controller.on_mic_button_pressed()
        action = controller.on_mic_button_pressed()
        self.assertEqual(action, VoiceHostAction.TAP)
        self.assertFalse(controller.active)  # second press eagerly closed it

        controller.restore_pending(action)

        self.assertTrue(controller.active)
        self.assertFalse(controller.holding)  # still toggle, never "holding"

    def test_a_successful_delivery_never_needs_restoring(self):
        controller = VoiceController(VoiceTriggerMode.HOLD)
        controller.on_mic_button_pressed()
        controller.reset()
        # No restore_pending() call here - simulating the success path -
        # active must stay False.
        self.assertFalse(controller.active)


if __name__ == "__main__":
    unittest.main()
