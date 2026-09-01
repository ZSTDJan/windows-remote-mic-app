import unittest

from ovb_rc003.button_combo import ButtonComboRecognizer, ComboCommand


class ButtonComboRecognizerTests(unittest.TestCase):
    def setUp(self):
        self.recognizer = ButtonComboRecognizer()
        self.configured = frozenset({"up", "ok"})

    def press(self, button_id):
        return self.recognizer.press(
            button_id,
            modifier="tv",
            configured_buttons=self.configured,
        )

    def test_modifier_click_is_forwarded_on_release_when_no_combo_matches(self):
        self.assertEqual(self.press("tv"), [])
        self.assertEqual(
            self.recognizer.release("tv"),
            [
                ComboCommand.forward_press("tv"),
                ComboCommand.forward_release("tv"),
            ],
        )

    def test_matching_second_key_triggers_once_and_consumes_both_single_keys(self):
        self.press("tv")
        self.assertEqual(self.press("up"), [ComboCommand.trigger("up")])
        self.assertEqual(self.press("up"), [])
        self.assertEqual(self.recognizer.release("up"), [])
        self.assertEqual(self.recognizer.release("tv"), [])

    def test_one_modifier_hold_can_trigger_multiple_configured_buttons(self):
        self.press("tv")
        self.assertEqual(self.press("up"), [ComboCommand.trigger("up")])
        self.recognizer.release("up")
        self.assertEqual(self.press("ok"), [ComboCommand.trigger("ok")])
        self.recognizer.release("ok")
        self.assertEqual(self.recognizer.release("tv"), [])

    def test_unconfigured_second_key_keeps_its_ordinary_edges(self):
        self.press("tv")
        self.assertEqual(self.press("back"), [ComboCommand.forward_press("back")])
        self.assertEqual(
            self.recognizer.release("back"),
            [ComboCommand.forward_release("back")],
        )
        self.assertEqual(
            self.recognizer.release("tv"),
            [
                ComboCommand.forward_press("tv"),
                ComboCommand.forward_release("tv"),
            ],
        )

    def test_no_configured_combo_changes_no_button_edges(self):
        self.assertEqual(
            self.recognizer.press(
                "tv", modifier=None, configured_buttons=frozenset()
            ),
            [ComboCommand.forward_press("tv")],
        )
        self.assertEqual(
            self.recognizer.release("tv"),
            [ComboCommand.forward_release("tv")],
        )

    def test_reset_drops_a_pending_modifier_without_emitting_an_action(self):
        self.press("tv")
        self.recognizer.reset()
        self.assertEqual(
            self.recognizer.release("tv"),
            [ComboCommand.forward_release("tv")],
        )


if __name__ == "__main__":
    unittest.main()
