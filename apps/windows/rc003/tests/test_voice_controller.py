import unittest

from ovb_rc003.key_mapping import VoiceTriggerMode
from ovb_rc003.voice_controller import VoiceController, VoiceHostAction


class HoldModeTests(unittest.TestCase):
    def test_default_controller_is_hold_to_talk(self):
        controller = VoiceController()
        self.assertEqual(controller.trigger_mode, VoiceTriggerMode.HOLD)

    def test_toggle_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            VoiceController(VoiceTriggerMode.TOGGLE)

    def test_press_holds_key_down(self):
        controller = VoiceController()
        self.assertEqual(controller.on_mic_button_pressed(), VoiceHostAction.KEY_DOWN)
        self.assertTrue(controller.holding)
        self.assertTrue(controller.active)

    def test_physical_release_releases_before_audio_stop(self):
        controller = VoiceController()
        controller.on_mic_button_pressed()
        self.assertEqual(
            controller.on_mic_button_released(),
            VoiceHostAction.KEY_UP,
        )
        self.assertFalse(controller.active)
        self.assertIsNone(controller.on_audio_stopped())

    def test_audio_stop_is_a_release_fallback(self):
        controller = VoiceController()
        controller.on_mic_button_pressed()
        self.assertEqual(controller.on_audio_stopped(), VoiceHostAction.KEY_UP)
        self.assertFalse(controller.active)

    def test_duplicate_release_is_harmless(self):
        controller = VoiceController()
        controller.on_mic_button_pressed()
        controller.on_mic_button_released()
        self.assertIsNone(controller.on_mic_button_released())
        self.assertIsNone(controller.on_audio_stopped())

    def test_cancel_pending_clears_an_undelivered_press(self):
        controller = VoiceController()
        controller.on_mic_button_pressed()
        controller.cancel_pending()
        self.assertFalse(controller.active)
        self.assertIsNone(controller.reset())

    def test_reset_releases_a_held_key(self):
        controller = VoiceController()
        controller.on_mic_button_pressed()
        self.assertEqual(controller.reset(), VoiceHostAction.KEY_UP)
        self.assertFalse(controller.active)

    def test_failed_key_up_can_restore_pending_state(self):
        controller = VoiceController()
        controller.on_mic_button_pressed()
        action = controller.reset()
        controller.restore_pending(action)
        self.assertTrue(controller.active)
        self.assertTrue(controller.holding)

    def test_tap_does_not_create_hold_state(self):
        controller = VoiceController()
        controller.restore_pending(VoiceHostAction.TAP)
        self.assertFalse(controller.active)


if __name__ == "__main__":
    unittest.main()
