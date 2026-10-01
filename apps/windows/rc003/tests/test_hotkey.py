import unittest

from ovb_rc003 import hotkey, key_mapping


class HotkeySpecTests(unittest.TestCase):
    def test_visible_names_resolve_to_the_same_physical_keys(self):
        from ovb_rc003 import win32_keys
        for token in win32_keys.VK_CODES:
            if token in {"ctrl", "alt", "shift", "win"}:
                text = f"{token}+a"
            else:
                text = token
            with self.subTest(token=token):
                spec = hotkey.HotkeySpec.parse(text)
                visible = hotkey.format_hotkey_text(text)
                parsed = hotkey.HotkeySpec.from_user_text(visible, mapping=True)
                self.assertEqual(
                    win32_keys.resolve_vk_codes((*parsed.modifiers, parsed.key)),
                    win32_keys.resolve_vk_codes((*spec.modifiers, spec.key)),
                )

    def test_manual_punctuation_and_common_names_accept_harmless_variations(self):
        cases = {
            " 左 Ctrl ＋ [ ": "lctrl+left_bracket",
            "CTRL+CapsLock": "ctrl+caps_lock",
            "Ctrl + Del": "ctrl+delete",
            "Win + L": "win+l",
            "Ctrl+小键盘加号": "ctrl+numpad_add",
            "ctrl + ctrl + A": "ctrl+a",
            "Ctrl+PrintScreen": "ctrl+print_screen",
            "Ctrl+vk_41": "ctrl+a",
            "vk_a2 + 左 Ctrl + [": "lctrl+left_bracket",
            "Ctrl+键码 0xE2": "ctrl+vk_e2",
            "左 Ctrl + Shift + 左 Ctrl": "shift+lctrl",
            "Ctrl+Shift+Ctrl": "shift+ctrl",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(hotkey.HotkeySpec.from_user_text(text, mapping=True).serialize(), expected)

    def test_ambiguous_incomplete_or_unsupported_keys_are_never_dropped(self):
        cases = {
            "Ctrl+": "补全",
            "Ctrl++": "小键盘加号",
            "Ctrl+{": "Shift",
            "Ctrl+小键盘 Enter": "暂不支持",
            "Ctrl+unsupported_key": "无法识别",
            "Ctrl+A+B": "一个普通键",
            "Ctrl+A+A": "一个普通键",
            "Ctrl+A+vk_41": "一个普通键",
            "Ctrl+[+left_bracket": "一个普通键",
            "Ctrl+Del+Delete": "一个普通键",
            "Ctrl+vk_e2+键码 0xE2": "一个普通键",
            "Ctrl+小键盘 ←": "NumLock",
            "Ctrl+vk_00": "无效",
            "Ctrl+不存在": "不存在",
        }
        for text, message in cases.items():
            with self.subTest(text=text):
                with self.assertRaisesRegex(hotkey.HotkeyParseError, message):
                    hotkey.HotkeySpec.from_user_text(text, mapping=True)

    def test_dynamic_key_display_can_be_edited_without_changing_its_vk(self):
        from ovb_rc003 import win32_keys
        for token in ("vk_e2", "vk_a8", "vk_41", "vk_a5"):
            with self.subTest(token=token):
                visible = hotkey.format_hotkey_text(f"ctrl+{token}")
                self.assertNotIn("vk_", visible)
                parsed = hotkey.HotkeySpec.from_user_text(visible, mapping=True)
                self.assertEqual(
                    win32_keys.resolve_vk_codes((*parsed.modifiers, parsed.key)),
                    win32_keys.resolve_vk_codes(("ctrl", token)),
                )

    def test_legacy_raw_vk_chords_keep_their_vk_and_press_order(self):
        from ovb_rc003 import win32_keys
        modifiers = ("ctrl", "shift", "alt", "win", "lctrl", "rctrl",
                     "lshift", "rshift", "lalt", "ralt", "lwin", "rwin")
        for vk in range(256):
            token = f"vk_{vk:02x}"
            cases = [token]
            for modifier in modifiers:
                cases.extend((f"{token}+{modifier}", f"{modifier}+{token}"))
            for text in cases:
                expected = hotkey.HotkeySpec.parse(text)
                for value in (text, hotkey.format_hotkey_text(text)):
                    with self.subTest(value=value):
                        if vk in (0x00, 0xFF, 0x01, 0x02, 0x04, 0x05, 0x06, 0xE5, 0xE7):
                            with self.assertRaises(hotkey.HotkeyParseError):
                                hotkey.HotkeySpec.from_user_text(value)
                            continue
                        actual = hotkey.HotkeySpec.from_user_text(value)
                        if vk in (0x10, 0x11, 0x12, 0x5B, 0x5C, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5):
                            self.assertEqual(actual, expected)
                        self.assertEqual(
                            win32_keys.resolve_vk_codes((*actual.modifiers, actual.key)),
                            win32_keys.resolve_vk_codes((*expected.modifiers, expected.key)),
                        )

    def test_default_voice_hotkey_uses_the_hold_to_talk_key(self):
        self.assertEqual(hotkey.DEFAULT_VOICE_HOTKEY.serialize(), "ralt")

    def test_voice_hotkey_presets_are_owned_by_the_trigger_mode_model(self):
        self.assertEqual(
            key_mapping.voice_hotkey_for_trigger_mode(key_mapping.VoiceTriggerMode.TOGGLE),
            "ralt+space",
        )
        self.assertEqual(
            key_mapping.voice_hotkey_for_trigger_mode(key_mapping.VoiceTriggerMode.HOLD),
            "ralt",
        )

    def test_three_key_chord_still_round_trips_as_a_custom_shortcut(self):
        spec = hotkey.HotkeySpec.parse("ctrl+alt+f8")
        self.assertEqual(spec.modifiers, ("ctrl", "alt"))
        self.assertEqual(spec.key, "f8")
        self.assertEqual(spec.serialize(), "ctrl+alt+f8")

    def test_right_alt_can_be_used_as_a_hold_trigger(self):
        spec = hotkey.HotkeySpec.parse("right_alt")
        self.assertEqual(spec.modifiers, ())
        self.assertEqual(spec.key, "ralt")
        self.assertEqual(spec.serialize(), "ralt")

    def test_right_alt_space_round_trips_for_toggle_trigger(self):
        spec = hotkey.HotkeySpec.parse("right_alt+space")
        self.assertEqual(spec.modifiers, ("ralt",))
        self.assertEqual(spec.key, "space")
        self.assertEqual(spec.serialize(), "ralt+space")

    def test_left_ctrl_win_round_trips_as_a_modifier_only_chord(self):
        spec = hotkey.HotkeySpec.parse("left_ctrl+win")
        self.assertEqual(spec.modifiers, ("lctrl",))
        self.assertEqual(spec.key, "win")
        self.assertEqual(spec.serialize(), "lctrl+win")

    def test_duplicate_modifiers_are_normalized(self):
        self.assertEqual(hotkey.HotkeySpec.parse("ctrl+ctrl+shift+p").serialize(), "ctrl+shift+p")

    def test_parse_and_serialize_round_trip(self):
        spec = hotkey.HotkeySpec.parse("win+h")
        self.assertEqual(spec.modifiers, ("win",))
        self.assertEqual(spec.key, "h")
        self.assertEqual(spec.serialize(), "win+h")

    def test_parse_orders_modifiers_canonically_on_serialize(self):
        spec = hotkey.HotkeySpec.parse("alt+ctrl+shift+v")
        self.assertEqual(spec.serialize(), "ctrl+shift+alt+v")

    def test_parse_rejects_empty_string(self):
        with self.assertRaises(hotkey.HotkeyParseError):
            hotkey.HotkeySpec.parse("")

    def test_modifier_only_chord_is_accepted(self):
        spec = hotkey.HotkeySpec.parse("ctrl+shift")
        self.assertEqual(spec.modifiers, ("ctrl",))
        self.assertEqual(spec.key, "shift")
        self.assertEqual(spec.serialize(), "ctrl+shift")

    def test_parse_rejects_a_single_generic_modifier(self):
        with self.assertRaises(hotkey.HotkeyParseError):
            hotkey.HotkeySpec.parse("ctrl")

    def test_parse_rejects_two_non_modifier_keys(self):
        with self.assertRaises(hotkey.HotkeyParseError):
            hotkey.HotkeySpec.parse("a+b")

    def test_construct_rejects_unknown_modifier(self):
        with self.assertRaises(hotkey.HotkeyParseError):
            hotkey.HotkeySpec(modifiers=("meta",), key="a")

    def test_construct_rejects_empty_key(self):
        with self.assertRaises(hotkey.HotkeyParseError):
            hotkey.HotkeySpec(modifiers=(), key="")


if __name__ == "__main__":
    unittest.main()
