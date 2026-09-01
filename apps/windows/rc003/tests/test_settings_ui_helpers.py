"""Tests the pure display<->action/save-model helper functions in
settings_ui.py without constructing any real Tk widget or window - see
XRBM-014 review RETRY P1 #7/#8 boundary ("不启动 Tk 或任何可见窗口"). Every
function under test here takes and returns plain data (strings, dicts,
dataclasses); none of it touches ``tkinter.Tk``/``Toplevel``/mainloop.
"""

import unittest
import tempfile
from pathlib import Path

from ovb_rc003 import audio_output, bridge_launcher, config, hotkey, key_mapping, logging_setup, settings_ui, single_instance
from ovb_rc003.settings_ui import (
    LAUNCH_ALREADY_RUNNING_TEXT,
    LAUNCH_NOT_STARTED_TEXT,
    LAUNCH_STATUS_UNKNOWN_TEXT,
    SettingsValidationError,
    _REMOVED_VOICE_DISPLAY,
    _VOICE_DISPLAY,
    _VOICE_HOLD_DISPLAY,
    _action_to_display,
    _display_to_action,
    _endpoint_display,
    _parse_endpoint_display,
    build_save_model,
    default_display_state,
    describe_launch_result,
    describe_log_open_result,
    voice_hotkey_for_trigger_mode,
)


class DisplayRoundTripTests(unittest.TestCase):
    def test_disabled_action_round_trips(self):
        action = key_mapping.ButtonAction(key_mapping.ActionKind.DISABLED)
        self.assertEqual(_action_to_display(action), "禁用")
        self.assertEqual(_display_to_action("禁用").kind, key_mapping.ActionKind.DISABLED)

    def test_key_combo_round_trips(self):
        action = key_mapping.ButtonAction(
            key_mapping.ActionKind.KEY_COMBO, ("ctrl", "shift", "p")
        )
        display = _action_to_display(action)
        restored = _display_to_action(display)
        self.assertEqual(restored.kind, key_mapping.ActionKind.KEY_COMBO)
        self.assertEqual(restored.keys, ("ctrl", "shift", "p"))

    def test_reference_action_labels_round_trip_to_windows_chords(self):
        expected = {
            "Escape": key_mapping.ActionKind.ESCAPE,
            "回车": key_mapping.ActionKind.RETURN,
            "退格": key_mapping.ActionKind.DELETE_BACKWARD,
            "方向上": key_mapping.ActionKind.ARROW_UP,
            "方向下": key_mapping.ActionKind.ARROW_DOWN,
            "方向左": key_mapping.ActionKind.ARROW_LEFT,
            "方向右": key_mapping.ActionKind.ARROW_RIGHT,
            "显示桌面": key_mapping.ActionKind.SHOW_DESKTOP,
            "右键菜单": key_mapping.ActionKind.CONTEXT_MENU,
            "应用切换": key_mapping.ActionKind.APP_SWITCHER,
            "元素导航开关": key_mapping.ActionKind.ELEMENT_NAVIGATION_TOGGLE,
        }
        for label, action_kind in expected.items():
            restored = _display_to_action(label)
            self.assertEqual(restored.kind, action_kind, label)
            self.assertEqual(restored.keys, (), label)
            self.assertEqual(_action_to_display(restored), label)

    def test_legacy_reference_labels_remain_accepted_but_render_current_names(self):
        expected = {
            "Return": (key_mapping.ActionKind.RETURN, "回车"),
            "Delete（退格）": (key_mapping.ActionKind.DELETE_BACKWARD, "退格"),
            "上下文菜单": (key_mapping.ActionKind.CONTEXT_MENU, "右键菜单"),
        }
        for legacy_label, (action_kind, current_label) in expected.items():
            restored = _display_to_action(legacy_label)
            self.assertEqual(restored.kind, action_kind, legacy_label)
            self.assertEqual(_action_to_display(restored), current_label)

    def test_action_groups_flatten_to_real_options_without_separator_items(self):
        flattened = tuple(
            option
            for _group_title, group_options in settings_ui.ACTION_OPTION_GROUPS
            for option in group_options
        )
        self.assertEqual(flattened, settings_ui._PRESET_KEY_COMBOS)
        self.assertEqual(
            settings_ui.ACTION_OPTION_GROUP_STARTS,
            frozenset(group_options[0] for _, group_options in settings_ui.ACTION_OPTION_GROUPS),
        )
        self.assertNotIn("lctrl+win", flattened)
        self.assertNotIn("ralt", flattened)
        self.assertNotIn("ralt+space", flattened)

    def test_legacy_alt_escape_app_switch_is_displayed_as_reference_action(self):
        action = key_mapping.ButtonAction(
            key_mapping.ActionKind.KEY_COMBO, ("alt", "esc")
        )
        self.assertEqual(_action_to_display(action), "应用切换")

    def test_reference_open_app_labels_round_trip_to_semantic_actions(self):
        expected = {
            "打开无线麦": key_mapping.ActionKind.OPEN_REMOTE_MIC,
            "打开 Codex": key_mapping.ActionKind.OPEN_CODEX,
            "打开 Claude": key_mapping.ActionKind.OPEN_CLAUDE,
            "打开 cmux": key_mapping.ActionKind.OPEN_CMUX,
            "打开 Chrome": key_mapping.ActionKind.OPEN_CHROME,
        }
        for label, action_kind in expected.items():
            restored = _display_to_action(label)
            self.assertEqual(restored.kind, action_kind, label)
            self.assertEqual(_action_to_display(restored), label)

    def test_modifier_only_combo_round_trips_through_button_mapping(self):
        restored = _display_to_action("ctrl+shift")
        self.assertEqual(restored.kind, key_mapping.ActionKind.KEY_COMBO)
        self.assertEqual(restored.keys, ("ctrl", "shift"))

    def test_volume_up_round_trips(self):
        action = key_mapping.ButtonAction(key_mapping.ActionKind.SYSTEM_VOLUME_UP)
        restored = _display_to_action(_action_to_display(action))
        self.assertEqual(restored.kind, key_mapping.ActionKind.SYSTEM_VOLUME_UP)

    def test_volume_down_round_trips(self):
        action = key_mapping.ButtonAction(key_mapping.ActionKind.SYSTEM_VOLUME_DOWN)
        restored = _display_to_action(_action_to_display(action))
        self.assertEqual(restored.kind, key_mapping.ActionKind.SYSTEM_VOLUME_DOWN)

    def test_legacy_voice_action_displays_an_explicit_disabled_notice(self):
        action = key_mapping.ButtonAction(key_mapping.ActionKind.VOICE)
        display = _action_to_display(action)
        self.assertEqual(display, _REMOVED_VOICE_DISPLAY)

    def test_legacy_voice_display_is_not_a_selectable_action(self):
        action = key_mapping.ButtonAction(key_mapping.ActionKind.VOICE)
        display = _action_to_display(action)
        self.assertEqual(display, _REMOVED_VOICE_DISPLAY)
        with self.assertRaises(hotkey.HotkeyParseError):
            _display_to_action(display)

    def test_hold_voice_round_trips_and_toggle_displays_as_removed(self):
        action = _display_to_action(_VOICE_HOLD_DISPLAY)
        self.assertEqual(action.kind, key_mapping.ActionKind.VOICE_HOLD)
        self.assertEqual(_action_to_display(action), _VOICE_HOLD_DISPLAY)
        legacy_toggle = key_mapping.ButtonAction(key_mapping.ActionKind.VOICE_TOGGLE)
        self.assertEqual(_action_to_display(legacy_toggle), _REMOVED_VOICE_DISPLAY)

    def test_removed_toggle_label_is_not_accepted_as_a_new_action(self):
        with self.assertRaises(hotkey.HotkeyParseError):
            _display_to_action("开关型语音")

    def test_unknown_key_is_rejected_before_it_can_break_runtime_input(self):
        with self.assertRaises(hotkey.HotkeyParseError):
            _display_to_action("ctrl+not_a_real_key")


class VoiceTriggerPresetTests(unittest.TestCase):
    def test_trigger_modes_have_matching_physical_shortcuts(self):
        self.assertEqual(
            voice_hotkey_for_trigger_mode(key_mapping.VoiceTriggerMode.TOGGLE),
            "ralt+space",
        )
        self.assertEqual(
            voice_hotkey_for_trigger_mode(key_mapping.VoiceTriggerMode.HOLD),
            "ralt",
        )


class EndpointDisplayTests(unittest.TestCase):
    def test_endpoint_with_host_api_round_trips(self):
        endpoint = audio_output.AudioEndpoint(name="Speakers", host_api="Windows WASAPI")
        display = _endpoint_display(endpoint)
        name, host_api = _parse_endpoint_display(display)
        self.assertEqual(name, "Speakers")
        self.assertEqual(host_api, "Windows WASAPI")

    def test_endpoint_without_host_api_round_trips_to_empty_host_api(self):
        endpoint = audio_output.AudioEndpoint(name="Speakers", host_api="")
        display = _endpoint_display(endpoint)
        name, host_api = _parse_endpoint_display(display)
        self.assertEqual(name, "Speakers")
        self.assertEqual(host_api, "")

    def test_bare_name_with_no_separator_parses_to_empty_host_api(self):
        name, host_api = _parse_endpoint_display("Just A Name")
        self.assertEqual(name, "Just A Name")
        self.assertEqual(host_api, "")

    def test_empty_string_parses_to_empty_name_and_host_api(self):
        name, host_api = _parse_endpoint_display("")
        self.assertEqual(name, "")
        self.assertEqual(host_api, "")


class BuildSaveModelTests(unittest.TestCase):
    def setUp(self):
        self.base_config = {"voice_hotkey": "ralt", "voice_trigger_mode": "hold"}
        self.base_bindings = {"schema_version": 1, "bindings": {}}

    def test_default_mic_mapping_saves_without_raising(self):
        # Direct regression test for the P1 #7 bug via the actual save path
        # a user hits when they change nothing (or click "restore defaults").
        new_config, new_bindings = build_save_model(
            button_display_map={"mic": _VOICE_HOLD_DISPLAY, "power": "escape"},
            hotkey_text="ralt",
            trigger_mode=key_mapping.VoiceTriggerMode.HOLD,
            endpoint_display_text="",
            base_config=self.base_config,
            base_bindings=self.base_bindings,
        )
        self.assertEqual(new_bindings["bindings"]["mic"]["kind"], "voice_hold")
        self.assertEqual(new_bindings["bindings"]["power"]["kind"], "key_combo")

    def test_save_model_removes_the_retired_release_finish_setting(self):
        self.base_config["voice_release_finish_tap_enabled"] = True

        new_config, _ = build_save_model(
            button_display_map={"mic": _VOICE_HOLD_DISPLAY},
            hotkey_text="ralt",
            trigger_mode=key_mapping.VoiceTriggerMode.HOLD,
            endpoint_display_text="",
            base_config=self.base_config,
            base_bindings=self.base_bindings,
        )

        self.assertNotIn("voice_release_finish_tap_enabled", new_config)

    def test_mic_can_be_saved_as_an_ordinary_action(self):
        new_config, new_bindings = build_save_model(
            button_display_map={"mic": "escape", "power": "escape"},
            hotkey_text="win+h",
            trigger_mode=key_mapping.VoiceTriggerMode.HOLD,
            endpoint_display_text="",
            base_config=self.base_config,
            base_bindings=self.base_bindings,
        )
        self.assertEqual(
            new_bindings["bindings"]["mic"],
            {"kind": "key_combo", "keys": ["escape"]},
        )

    def test_absent_mic_mapping_is_not_synthesized(self):
        new_config, new_bindings = build_save_model(
            button_display_map={"power": "escape"},
            hotkey_text="win+h",
            trigger_mode=key_mapping.VoiceTriggerMode.TOGGLE,
            endpoint_display_text="",
            base_config=self.base_config,
            base_bindings=self.base_bindings,
        )
        self.assertNotIn("mic", new_bindings["bindings"])

    def test_restore_defaults_state_saves_without_raising(self):
        defaults = default_display_state()
        new_config, new_bindings = build_save_model(
            button_display_map=defaults.button_display_map,
            hotkey_text=defaults.hotkey_text,
            trigger_mode=key_mapping.VoiceTriggerMode.TOGGLE,
            endpoint_display_text="",
            base_config=self.base_config,
            base_bindings=self.base_bindings,
        )
        self.assertEqual(new_bindings["bindings"]["mic"]["kind"], "voice_hold")
        self.assertEqual(new_config["voice_hotkey"], "ralt")

    def test_invalid_hotkey_raises_with_no_button_id(self):
        with self.assertRaises(SettingsValidationError) as ctx:
            build_save_model(
                button_display_map={},
                hotkey_text="ctrl",  # a single generic modifier is invalid
                trigger_mode=key_mapping.VoiceTriggerMode.TOGGLE,
                endpoint_display_text="",
                base_config=self.base_config,
                base_bindings=self.base_bindings,
            )
        self.assertIsNone(ctx.exception.button_id)

    def test_invalid_button_mapping_raises_with_the_button_id(self):
        with self.assertRaises(SettingsValidationError) as ctx:
            build_save_model(
                button_display_map={"menu": "a+b"},  # two non-modifier keys
                hotkey_text="win+h",
                trigger_mode=key_mapping.VoiceTriggerMode.TOGGLE,
                endpoint_display_text="",
                base_config=self.base_config,
                base_bindings=self.base_bindings,
            )
        self.assertEqual(ctx.exception.button_id, "menu")

    def test_blank_button_mapping_is_left_unbound(self):
        new_config, new_bindings = build_save_model(
            button_display_map={"volume_mute": ""},
            hotkey_text="win+h",
            trigger_mode=key_mapping.VoiceTriggerMode.TOGGLE,
            endpoint_display_text="",
            base_config=self.base_config,
            base_bindings=self.base_bindings,
        )
        self.assertNotIn("volume_mute", new_bindings["bindings"])

    def test_endpoint_display_text_splits_into_name_and_host_api(self):
        new_config, _ = build_save_model(
            button_display_map={},
            hotkey_text="win+h",
            trigger_mode=key_mapping.VoiceTriggerMode.HOLD,
            endpoint_display_text="Speakers — Windows WASAPI",
            base_config=self.base_config,
            base_bindings=self.base_bindings,
        )
        self.assertEqual(new_config["output_endpoint_name"], "Speakers")
        self.assertEqual(new_config["output_endpoint_host_api"], "Windows WASAPI")
        self.assertEqual(new_config["voice_trigger_mode"], "hold")

    def test_only_the_hold_shortcut_is_saved(self):
        new_config, _ = build_save_model(
            button_display_map={"mic": _VOICE_HOLD_DISPLAY},
            hotkey_text="ignored-active-alias",
            trigger_mode=key_mapping.VoiceTriggerMode.HOLD,
            endpoint_display_text="",
            base_config=self.base_config,
            base_bindings=self.base_bindings,
            voice_hotkeys={"toggle": "lalt+space", "hold": "ctrl+l"},
        )
        self.assertEqual(new_config["voice_hotkeys"], {"hold": "ctrl+l"})
        self.assertEqual(new_config["voice_hotkey"], "ctrl+l")

    def test_blank_inactive_voice_shortcut_is_allowed(self):
        new_config, _ = build_save_model(
            button_display_map={},
            hotkey_text="lalt+space",
            trigger_mode=key_mapping.VoiceTriggerMode.TOGGLE,
            endpoint_display_text="",
            base_config=self.base_config,
            base_bindings=self.base_bindings,
            voice_hotkeys={"toggle": "lalt+space", "hold": ""},
        )
        self.assertEqual(new_config["voice_hotkeys"]["hold"], "")

    def test_blank_active_voice_shortcut_is_rejected(self):
        with self.assertRaises(SettingsValidationError) as ctx:
            build_save_model(
                button_display_map={"mic": _VOICE_HOLD_DISPLAY},
                hotkey_text="",
                trigger_mode=key_mapping.VoiceTriggerMode.HOLD,
                endpoint_display_text="",
                base_config=self.base_config,
                base_bindings=self.base_bindings,
                voice_hotkeys={"toggle": "lalt+space", "hold": ""},
            )
        self.assertEqual(ctx.exception.button_id, "mic")
        self.assertIn("按住说话", ctx.exception.message)

    def test_mic_hold_mapping_selects_the_hold_shortcut(self):
        new_config, _ = build_save_model(
            button_display_map={"mic": _VOICE_HOLD_DISPLAY, "up": "Escape"},
            hotkey_text="ignored-active-alias",
            trigger_mode=key_mapping.VoiceTriggerMode.TOGGLE,
            endpoint_display_text="",
            base_config=self.base_config,
            base_bindings=self.base_bindings,
            voice_hotkeys={"toggle": "lalt+space", "hold": "ctrl+l"},
        )
        self.assertEqual(new_config["voice_trigger_mode"], "hold")
        self.assertEqual(new_config["voice_hotkey"], "ctrl+l")

    def test_non_mic_primary_voice_is_rejected(self):
        with self.assertRaises(SettingsValidationError) as ctx:
            build_save_model(
                button_display_map={
                    "mic": _VOICE_HOLD_DISPLAY,
                    "up": _VOICE_HOLD_DISPLAY,
                },
                hotkey_text="ralt+space",
                trigger_mode=key_mapping.VoiceTriggerMode.TOGGLE,
                endpoint_display_text="",
                base_config=self.base_config,
                base_bindings=self.base_bindings,
            )
        self.assertEqual(ctx.exception.button_id, "up")
        self.assertIn("只有实体话筒键", ctx.exception.message)

    def test_secondary_voice_action_is_rejected(self):
        with self.assertRaises(SettingsValidationError) as ctx:
            build_save_model(
                button_display_map={"mic": "Escape"},
                secondary_display_map={
                    "up": {"double_click": _VOICE_HOLD_DISPLAY}
                },
                hotkey_text="ralt+space",
                trigger_mode=key_mapping.VoiceTriggerMode.TOGGLE,
                endpoint_display_text="",
                base_config=self.base_config,
                base_bindings=self.base_bindings,
            )
        self.assertEqual(ctx.exception.button_id, "up")
        self.assertIn("只能用于主映射", ctx.exception.message)

    def test_mic_voice_primary_preserves_inactive_explicit_secondary_action(self):
        _, new_bindings = build_save_model(
            button_display_map={"mic": _VOICE_HOLD_DISPLAY},
            secondary_display_map={
                "mic": {"double_click": "Escape", "long_press": ""}
            },
            hotkey_text="ralt+space",
            trigger_mode=key_mapping.VoiceTriggerMode.TOGGLE,
            endpoint_display_text="",
            base_config=self.base_config,
            base_bindings=self.base_bindings,
        )
        self.assertEqual(
            new_bindings["secondary_bindings"]["mic"]["double_click"],
            {"kind": "escape", "keys": []},
        )

    def test_mic_voice_primary_preserves_inactive_raw_secondary_action(self):
        base_bindings = {
            "schema_version": 1,
            "bindings": {},
            "secondary_bindings": {
                "mic": {
                    "long_press": {"kind": "escape", "keys": []},
                }
            },
        }
        _, new_bindings = build_save_model(
            button_display_map={"mic": _VOICE_HOLD_DISPLAY},
            hotkey_text="ralt",
            trigger_mode=key_mapping.VoiceTriggerMode.HOLD,
            endpoint_display_text="",
            base_config=self.base_config,
            base_bindings=base_bindings,
        )
        self.assertEqual(
            new_bindings["secondary_bindings"]["mic"]["long_press"],
            {"kind": "escape", "keys": []},
        )

    def test_mic_voice_primary_can_preserve_a_disabled_raw_secondary_action(self):
        base_bindings = {
            "schema_version": 1,
            "bindings": {},
            "secondary_bindings": {
                "mic": {
                    "long_press": {"kind": "disabled", "keys": []},
                }
            },
        }
        _, new_bindings = build_save_model(
            button_display_map={"mic": _VOICE_HOLD_DISPLAY},
            hotkey_text="ralt",
            trigger_mode=key_mapping.VoiceTriggerMode.HOLD,
            endpoint_display_text="",
            base_config=self.base_config,
            base_bindings=base_bindings,
        )
        self.assertEqual(
            new_bindings["secondary_bindings"]["mic"]["long_press"],
            {"kind": "disabled", "keys": []},
        )

    def test_ordinary_mic_primary_can_keep_ordinary_secondary_actions(self):
        _, new_bindings = build_save_model(
            button_display_map={"mic": "Escape"},
            secondary_display_map={
                "mic": {"double_click": "Return", "long_press": ""}
            },
            hotkey_text="ralt+space",
            trigger_mode=key_mapping.VoiceTriggerMode.TOGGLE,
            endpoint_display_text="",
            base_config=self.base_config,
            base_bindings=self.base_bindings,
        )
        self.assertEqual(
            new_bindings["secondary_bindings"]["mic"]["double_click"],
            {"kind": "return", "keys": []},
        )

    def test_zero_voice_buttons_are_allowed(self):
        new_config, new_bindings = build_save_model(
            button_display_map={"mic": "Escape", "up": "方向上"},
            hotkey_text="",
            trigger_mode=key_mapping.VoiceTriggerMode.TOGGLE,
            endpoint_display_text="",
            base_config=self.base_config,
            base_bindings=self.base_bindings,
            voice_hotkeys={"toggle": "", "hold": ""},
        )
        self.assertEqual(new_bindings["bindings"]["mic"]["kind"], "escape")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            config.save_config(path, new_config)
            reloaded = config.load_config(path)
        self.assertEqual(reloaded["voice_hotkey"], "ralt")
        self.assertEqual(reloaded["voice_hotkeys"], {"hold": "ralt"})

    def test_does_not_mutate_base_dicts(self):
        base_config_copy = dict(self.base_config)
        base_bindings_copy = {"schema_version": 1, "bindings": dict(self.base_bindings["bindings"])}
        build_save_model(
            button_display_map={"power": "escape"},
            hotkey_text="win+h",
            trigger_mode=key_mapping.VoiceTriggerMode.TOGGLE,
            endpoint_display_text="",
            base_config=self.base_config,
            base_bindings=self.base_bindings,
        )
        self.assertEqual(self.base_config, base_config_copy)
        self.assertEqual(self.base_bindings, base_bindings_copy)

    def test_selected_dji_profile_is_persisted_without_overwriting_rc003_bindings(self):
        new_config, new_bindings = build_save_model(
            button_display_map={"power": "escape"},
            hotkey_text="win+h",
            trigger_mode=key_mapping.VoiceTriggerMode.TOGGLE,
            endpoint_display_text="",
            base_config=self.base_config,
            base_bindings=self.base_bindings,
            selected_device_profile="dji-mic-2",
        )
        self.assertEqual(new_config["selected_device_profile"], "dji-mic-2")
        self.assertEqual(new_bindings["bindings"]["power"]["keys"], ["escape"])

    def test_unknown_device_profile_falls_back_to_rc003(self):
        new_config, _ = build_save_model(
            button_display_map={},
            hotkey_text="win+h",
            trigger_mode=key_mapping.VoiceTriggerMode.TOGGLE,
            endpoint_display_text="",
            base_config=self.base_config,
            base_bindings=self.base_bindings,
            selected_device_profile="invented-device",
        )
        self.assertEqual(new_config["selected_device_profile"], "xiaomi-rc003")

    def test_secondary_display_map_round_trips_double_and_long_actions(self):
        _, new_bindings = build_save_model(
            button_display_map={"power": "escape"},
            secondary_display_map={
                "power": {
                    "double_click": "f5",
                    "long_press": "系统音量 +",
                }
            },
            hotkey_text="win+h",
            trigger_mode=key_mapping.VoiceTriggerMode.TOGGLE,
            endpoint_display_text="",
            base_config=self.base_config,
            base_bindings=self.base_bindings,
        )
        self.assertEqual(
            new_bindings["secondary_bindings"]["power"]["double_click"],
            {"kind": "key_combo", "keys": ["f5"]},
        )
        self.assertEqual(
            new_bindings["secondary_bindings"]["power"]["long_press"]["kind"],
            "system_volume_up",
        )

    def test_blank_secondary_action_is_not_persisted(self):
        _, new_bindings = build_save_model(
            button_display_map={"power": "escape"},
            secondary_display_map={
                "power": {"double_click": "", "long_press": "禁用"}
            },
            hotkey_text="win+h",
            trigger_mode=key_mapping.VoiceTriggerMode.TOGGLE,
            endpoint_display_text="",
            base_config=self.base_config,
            base_bindings=self.base_bindings,
        )
        self.assertEqual(new_bindings["secondary_bindings"], {})

    def test_combo_actions_and_notes_are_saved_under_one_modifier(self):
        _, new_bindings = build_save_model(
            button_display_map={"power": "escape"},
            secondary_display_map={},
            combo_modifier="menu",
            combo_display_map={
                "up": "quicker:runaction:pin-window?mode=toggle",
                "ok": "ctrl+enter",
            },
            combo_note_map={"up": "  置顶窗口  ", "ok": "提交"},
            hotkey_text="ralt",
            trigger_mode=key_mapping.VoiceTriggerMode.HOLD,
            endpoint_display_text="",
            base_config=self.base_config,
            base_bindings=self.base_bindings,
        )

        combo = new_bindings["combo_bindings"]
        self.assertEqual(combo["modifier"], "menu")
        self.assertEqual(combo["bindings"]["up"]["kind"], "quicker_uri")
        self.assertEqual(combo["bindings"]["ok"]["keys"], ["ctrl", "enter"])
        self.assertEqual(combo["display_notes"], {"up": "置顶窗口", "ok": "提交"})

    def test_combo_modifier_cannot_keep_double_or_long_press(self):
        with self.assertRaises(SettingsValidationError) as ctx:
            build_save_model(
                button_display_map={"tv": "escape"},
                secondary_display_map={
                    "tv": {"double_click": "Return", "long_press": ""}
                },
                combo_modifier="tv",
                combo_display_map={"up": "escape"},
                hotkey_text="ralt",
                trigger_mode=key_mapping.VoiceTriggerMode.HOLD,
                endpoint_display_text="",
                base_config=self.base_config,
                base_bindings=self.base_bindings,
            )

        self.assertEqual(ctx.exception.button_id, "tv")
        self.assertIn("组合主键", ctx.exception.message)

    def test_display_notes_are_trimmed_and_kept_separate_from_actions(self):
        _, new_bindings = build_save_model(
            button_display_map={"power": "ctrl+c"},
            display_note_map={
                "power": {
                    "single_click": "  复制  ",
                    "double_click": "   ",
                    "long_press": "未命名",
                    "unknown": "忽略",
                },
                "up": {"single_click": True},
                "unknown_button": {"single_click": "忽略"},
            },
            hotkey_text="ralt",
            trigger_mode=key_mapping.VoiceTriggerMode.HOLD,
            endpoint_display_text="",
            base_config=self.base_config,
            base_bindings=self.base_bindings,
        )

        self.assertEqual(
            new_bindings["display_notes"],
            {"power": {"single_click": "复制"}},
        )
        self.assertEqual(
            new_bindings["bindings"]["power"],
            {"kind": "key_combo", "keys": ["ctrl", "c"]},
        )

    def test_omitted_display_note_map_preserves_existing_notes(self):
        base_bindings = {
            "schema_version": 4,
            "bindings": {},
            "display_notes": {"power": {"single_click": "关机"}},
        }

        _, new_bindings = build_save_model(
            button_display_map={},
            hotkey_text="ralt",
            trigger_mode=key_mapping.VoiceTriggerMode.HOLD,
            endpoint_display_text="",
            base_config=self.base_config,
            base_bindings=base_bindings,
        )

        self.assertEqual(new_bindings["display_notes"], base_bindings["display_notes"])


class DefaultDisplayStateTests(unittest.TestCase):
    def test_covers_every_user_facing_button(self):
        from ovb_rc003 import device_profile
        from ovb_rc003.settings_ui import _USER_FACING_BUTTON_IDS

        state = default_display_state()
        self.assertEqual(set(state.button_display_map.keys()), _USER_FACING_BUTTON_IDS)
        # volume_mute stays a valid protocol-level id (device_profile keeps
        # it), but the RC003 has no physical mute key, so it must not appear
        # in the settings window's own button set at all (XRBM-019 review
        # round 1 P2).
        self.assertIn("volume_mute", device_profile.ALL_BUTTON_IDS)
        self.assertNotIn("volume_mute", _USER_FACING_BUTTON_IDS)

    def test_volume_mute_is_absent_from_the_default_display_map(self):
        state = default_display_state()
        self.assertNotIn("volume_mute", state.button_display_map)

    def test_hotkey_defaults_to_hold_shortcut(self):
        state = default_display_state()
        self.assertEqual(state.hotkey_text, "ralt")

    def test_trigger_mode_defaults_to_hold_label(self):
        state = default_display_state()
        self.assertEqual(state.trigger_mode_label, "按住说话")

    def test_defaults_keep_only_the_hold_shortcut(self):
        state = default_display_state()
        self.assertEqual(state.voice_hotkeys, {"hold": "ralt"})


class DescribeLaunchResultTests(unittest.TestCase):
    """XRBM-029: settings_ui's status text for each required
    stable bridge-launch states, built directly on the same
    bridge_launcher.LaunchResult values tests/test_bridge_launcher.py
    proves get produced - no Tk, no subprocess.
    """

    def test_not_started_text_is_a_fixed_constant_shown_before_any_launch(self):
        self.assertIn("未运行", LAUNCH_NOT_STARTED_TEXT)
        self.assertIn("按键和语音", LAUNCH_NOT_STARTED_TEXT)

    def test_existing_and_unknown_bridge_states_are_described_honestly(self):
        self.assertIn("已在运行", LAUNCH_ALREADY_RUNNING_TEXT)
        self.assertNotIn("已连接", LAUNCH_ALREADY_RUNNING_TEXT)
        self.assertIn("无法确认", LAUNCH_STATUS_UNKNOWN_TEXT)
        self.assertIn("勿重复启动", LAUNCH_STATUS_UNKNOWN_TEXT)

    def test_started_never_claims_rc003_is_connected(self):
        result = bridge_launcher.LaunchResult(
            outcome=bridge_launcher.LaunchOutcome.STARTED,
            command=("exe",),
            pid=123,
        )
        text = describe_launch_result(result)
        self.assertIn("123", text)
        self.assertIn("约 1 分钟", text)
        self.assertNotIn("小米遥控器2 Pro 已连接", text)
        self.assertNotIn("已连接", text)

    def test_already_running_mentions_the_exit_code_and_is_distinct_from_quick_exit(self):
        result = bridge_launcher.LaunchResult(
            outcome=bridge_launcher.LaunchOutcome.ALREADY_RUNNING,
            command=("exe",),
            exit_code=single_instance.DUPLICATE_INSTANCE_EXIT_CODE,
        )
        already_running_text = describe_launch_result(result)
        self.assertIn(str(single_instance.DUPLICATE_INSTANCE_EXIT_CODE), already_running_text)

        quick_exit_result = bridge_launcher.LaunchResult(
            outcome=bridge_launcher.LaunchOutcome.QUICK_EXIT,
            command=("exe",),
            exit_code=1,
        )
        quick_exit_text = describe_launch_result(quick_exit_result)
        self.assertNotEqual(already_running_text, quick_exit_text)

    def test_quick_exit_preserves_the_real_exit_code_and_points_at_the_log(self):
        result = bridge_launcher.LaunchResult(
            outcome=bridge_launcher.LaunchOutcome.QUICK_EXIT,
            command=("exe",),
            exit_code=9,
        )
        text = describe_launch_result(result)
        self.assertIn("9", text)
        self.assertIn("app.log", text)

    def test_status_unknown_warns_against_restarting_the_created_process(self):
        result = bridge_launcher.LaunchResult(
            outcome=bridge_launcher.LaunchOutcome.STATUS_UNKNOWN,
            command=("exe",),
            pid=456,
            error="OSError",
        )
        text = describe_launch_result(result)
        self.assertIn("456", text)
        self.assertIn("勿重复启动", text)
        self.assertIn("app.log", text)

    def test_launch_failed_surfaces_the_error_and_points_at_the_log(self):
        result = bridge_launcher.LaunchResult(
            outcome=bridge_launcher.LaunchOutcome.LAUNCH_FAILED,
            command=("exe",),
            error="[WinError 2] The system cannot find the file specified",
        )
        text = describe_launch_result(result)
        self.assertIn("WinError 2", text)
        self.assertIn("app.log", text)


class DescribeLogOpenResultTests(unittest.TestCase):
    def test_opened_ready_mentions_the_directory(self):
        directory = Path("/tmp/example/logs")
        location = logging_setup.LogLocation(
            status=logging_setup.LogLocationStatus.READY,
            directory=directory,
            file_path=directory / "app.log",
        )
        result = logging_setup.LogOpenResult(
            outcome=logging_setup.LogOpenOutcome.OPENED, location=location
        )
        text = describe_log_open_result(result)
        self.assertIn(str(directory), text)

    def test_opened_but_file_missing_gives_an_honest_note_not_an_error(self):
        directory = Path("/tmp/example/logs")
        location = logging_setup.LogLocation(
            status=logging_setup.LogLocationStatus.FILE_MISSING,
            directory=directory,
            file_path=directory / "app.log",
        )
        result = logging_setup.LogOpenResult(
            outcome=logging_setup.LogOpenOutcome.OPENED, location=location
        )
        text = describe_log_open_result(result)
        self.assertIn("app.log", text)
        self.assertIn("还没有运行", text)

    def test_directory_missing_does_not_claim_a_log_exists(self):
        directory = Path("/tmp/example/logs")
        location = logging_setup.LogLocation(
            status=logging_setup.LogLocationStatus.DIRECTORY_MISSING,
            directory=directory,
            file_path=directory / "app.log",
        )
        result = logging_setup.LogOpenResult(
            outcome=logging_setup.LogOpenOutcome.DIRECTORY_MISSING, location=location
        )
        text = describe_log_open_result(result)
        self.assertIn(str(directory), text)
        self.assertIn("没有运行", text)

    def test_open_failed_surfaces_the_underlying_error(self):
        directory = Path("/tmp/example/logs")
        location = logging_setup.LogLocation(
            status=logging_setup.LogLocationStatus.READY,
            directory=directory,
            file_path=directory / "app.log",
        )
        result = logging_setup.LogOpenResult(
            outcome=logging_setup.LogOpenOutcome.OPEN_FAILED,
            location=location,
            error="no shell association available",
        )
        text = describe_log_open_result(result)
        self.assertIn("no shell association available", text)


if __name__ == "__main__":
    unittest.main()
