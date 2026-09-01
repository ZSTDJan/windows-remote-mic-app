import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ovb_rc003 import config, key_mapping


class ConfigRootTests(unittest.TestCase):
    def test_uses_localappdata_when_set(self):
        with mock.patch.dict("os.environ", {"LOCALAPPDATA": "/tmp/fake-appdata"}):
            root = config.config_root()
        self.assertEqual(root, Path("/tmp/fake-appdata") / "RemoteMic" / "RC003")

    def test_falls_back_to_home_without_localappdata(self):
        with mock.patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("LOCALAPPDATA", None)
            root = config.config_root()
        self.assertEqual(root, Path.home() / "RemoteMic" / "RC003")


class DefaultConfigPrivacyTests(unittest.TestCase):
    def test_default_config_uses_rc003_and_the_right_alt_hold_shortcut(self):
        defaults = config.default_config()
        self.assertEqual(defaults["selected_device_profile"], "xiaomi-rc003")
        self.assertEqual(defaults["voice_hotkey"], "ralt")
        self.assertEqual(defaults["voice_trigger_mode"], "hold")
        self.assertEqual(defaults["voice_hotkeys"]["hold"], "ralt")
        self.assertNotIn("toggle", defaults["voice_hotkeys"])
        self.assertNotIn("voice_release_finish_tap_enabled", defaults)
        self.assertEqual(
            defaults["voice_hotkeys_by_provider"]["sogou"]["hold"],
            "rctrl",
        )
        self.assertEqual(
            defaults["voice_hotkeys_by_provider"]["wetype"]["hold"],
            "lctrl+lwin",
        )
        self.assertEqual(
            defaults["voice_hotkeys_by_provider"]["windows_dictation"]["hold"],
            "win+h",
        )
        self.assertEqual(defaults["schema_version"], config.SCHEMA_VERSION)
        self.assertEqual(
            defaults["voice_program"]["launch_elevated_by_provider"],
            {"sogou": True, "custom": False},
        )
        self.assertEqual(defaults["gain_db"], 10.0)
        self.assertFalse(defaults["launch_bridge_on_app_start"])
        self.assertEqual(
            defaults["close_behavior"], config.CLOSE_BEHAVIOR_HIDE_TO_TRAY
        )

    def test_default_config_contains_no_forbidden_identity_fields(self):
        defaults = config.default_config()
        self.assertFalse(config.FORBIDDEN_KEYS.intersection(defaults.keys()))

    def test_default_key_bindings_contains_no_forbidden_identity_fields(self):
        defaults = config.default_key_bindings()
        self.assertFalse(config.FORBIDDEN_KEYS.intersection(defaults.keys()))
        self.assertEqual(defaults["display_notes"], {})
        self.assertEqual(
            defaults["combo_bindings"],
            {"modifier": "tv", "bindings": {}, "display_notes": {}},
        )

    def test_output_endpoint_defaults_to_empty_so_voice_fails_closed(self):
        self.assertEqual(config.default_config()["output_endpoint_name"], "")

    def test_missing_config_file_uses_the_right_alt_hold_shortcut(self):
        with tempfile.TemporaryDirectory() as tmp:
            loaded = config.load_config(Path(tmp) / "config.json")
        self.assertEqual(loaded["voice_hotkey"], "ralt")
        self.assertEqual(loaded["voice_hotkeys"], {"hold": "ralt"})

    def test_invalid_desktop_behavior_falls_back_safely(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "launch_bridge_on_app_start": 1,
                        "close_behavior": "unexpected",
                    }
                ),
                encoding="utf-8",
            )
            loaded = config.load_config(path)
        self.assertTrue(loaded["launch_bridge_on_app_start"])
        self.assertEqual(
            loaded["close_behavior"], config.CLOSE_BEHAVIOR_HIDE_TO_TRAY
        )

    def test_load_preserves_an_existing_right_alt_hold_shortcut(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "voice_trigger_mode": "hold",
                        "voice_hotkey": "ralt",
                    }
                ),
                encoding="utf-8",
            )
            loaded = config.load_config(path)
        self.assertEqual(loaded["voice_hotkey"], "ralt")
        self.assertEqual(loaded["voice_hotkeys"], {"hold": "ralt"})

    def test_load_preserves_an_existing_custom_hold_shortcut(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "voice_trigger_mode": "hold",
                        "voice_hotkey": "ctrl+l",
                    }
                ),
                encoding="utf-8",
            )
            loaded = config.load_config(path)
        self.assertEqual(loaded["voice_hotkey"], "ctrl+l")
        self.assertEqual(loaded["voice_hotkeys"], {"hold": "ctrl+l"})

    def test_legacy_global_shortcut_is_assigned_to_the_selected_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 6,
                        "voice_program": {"provider": "wetype"},
                        "voice_hotkey": "lctrl+lshift+f9",
                        "voice_hotkeys": {"hold": "lctrl+lshift+f9"},
                    }
                ),
                encoding="utf-8",
            )

            loaded = config.load_config(path)

        self.assertEqual(loaded["voice_hotkey"], "lctrl+lshift+f9")
        self.assertEqual(
            loaded["voice_hotkeys_by_provider"]["wetype"]["hold"],
            "lctrl+lshift+f9",
        )
        self.assertEqual(
            loaded["voice_hotkeys_by_provider"]["sogou"]["hold"],
            "rctrl",
        )

    def test_switching_provider_uses_its_own_remembered_shortcut(self):
        data = config.default_config()
        data["voice_program"] = {"provider": "wetype"}
        config.set_voice_hotkey_for_provider(
            data, "wetype", "lctrl+lshift+f9"
        )
        config.set_voice_hotkey_for_provider(
            data, "sogou", "lctrl+lshift+f7"
        )
        data["voice_program"] = {"provider": "sogou"}
        config._normalize_voice_program(data)
        config._normalize_voice_hotkey(data)

        self.assertEqual(data["voice_hotkey"], "lctrl+lshift+f7")
        self.assertEqual(data["voice_hotkeys"], {"hold": "lctrl+lshift+f7"})

    def test_provider_scoped_wetype_native_ctrl_win_is_not_legacy_repaired(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 7,
                        "voice_program": {"provider": "wetype"},
                        "voice_hotkeys_by_provider": {
                            "wetype": {"hold": "lctrl+lwin"}
                        },
                    }
                ),
                encoding="utf-8",
            )

            loaded = config.load_config(path)

        self.assertEqual(loaded["voice_hotkey"], "lctrl+lwin")

    def test_schema_7_false_elevation_choice_is_not_replaced_by_sogou_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 7,
                        "voice_program": {
                            "provider": "sogou",
                            "launch_on_bridge_start": True,
                            "launch_elevated": False,
                        },
                    }
                ),
                encoding="utf-8",
            )

            loaded = config.load_config(path)

        self.assertFalse(loaded["voice_program"]["launch_elevated"])
        self.assertEqual(
            loaded["voice_program"]["launch_elevated_by_provider"],
            {"sogou": False, "custom": False},
        )

    def test_schema_7_true_elevation_choice_is_preserved_for_both_programs(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 7,
                        "voice_program": {
                            "provider": "custom",
                            "launch_on_bridge_start": True,
                            "launch_elevated": True,
                        },
                    }
                ),
                encoding="utf-8",
            )

            loaded = config.load_config(path)

        self.assertTrue(loaded["voice_program"]["launch_elevated"])
        self.assertEqual(
            loaded["voice_program"]["launch_elevated_by_provider"],
            {"sogou": True, "custom": True},
        )

    def test_load_preserves_nested_hold_when_old_file_has_no_top_level_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "voice_trigger_mode": "hold",
                        "voice_hotkeys": {"hold": "ctrl+l"},
                    }
                ),
                encoding="utf-8",
            )
            loaded = config.load_config(path)
        self.assertEqual(loaded["voice_hotkey"], "ctrl+l")
        self.assertEqual(loaded["voice_hotkeys"], {"hold": "ctrl+l"})

    def test_load_disables_legacy_toggle_without_reinterpreting_its_shortcut(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                json.dumps({"voice_trigger_mode": "toggle", "voice_hotkey": "ralt"}),
                encoding="utf-8",
            )
            loaded = config.load_config(path)
        self.assertEqual(loaded["voice_hotkey"], "ralt")
        self.assertEqual(loaded["voice_trigger_mode"], "hold")
        self.assertEqual(
            loaded[config.RUNTIME_LEGACY_VOICE_MODE_KEY],
            "toggle",
        )

    def test_load_removes_the_retired_release_finish_setting(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "voice_trigger_mode": "hold",
                        "voice_hotkey": "ralt",
                        "voice_release_finish_tap_enabled": True,
                    }
                ),
                encoding="utf-8",
            )

            loaded = config.load_config(path)

        self.assertEqual(loaded["schema_version"], config.SCHEMA_VERSION)
        self.assertNotIn("voice_release_finish_tap_enabled", loaded)

    def test_save_preserves_hold_with_right_alt_space(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            data = config.default_config()
            data.update({"voice_trigger_mode": "hold", "voice_hotkey": "ralt+space"})
            config.save_config(path, data)
            loaded = config.load_config(path)
        self.assertEqual(loaded["voice_hotkey"], "ralt+space")
        self.assertEqual(loaded["voice_trigger_mode"], "hold")

    def test_load_does_not_reinterpret_a_custom_toggle_shortcut_as_hold(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                json.dumps({"voice_trigger_mode": "toggle", "voice_hotkey": "win+h"}),
                encoding="utf-8",
            )
            loaded = config.load_config(path)
        self.assertEqual(loaded["voice_hotkey"], "ralt")
        self.assertEqual(loaded["voice_hotkeys"]["hold"], "ralt")
        self.assertNotIn("toggle", loaded["voice_hotkeys"])

    def test_load_preserves_the_separately_saved_hold_shortcut(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "voice_trigger_mode": "toggle",
                        "voice_hotkey": "lalt+space",
                        "voice_hotkeys": {
                            "toggle": "lalt+space",
                            "hold": "ctrl+l",
                        },
                    }
                ),
                encoding="utf-8",
            )
            loaded = config.load_config(path)
        self.assertEqual(loaded["voice_hotkey"], "ctrl+l")
        self.assertEqual(loaded["voice_hotkeys"]["hold"], "ctrl+l")
        self.assertNotIn("toggle", loaded["voice_hotkeys"])

    def test_load_repairs_legacy_ctrl_win_values_to_historical_hold_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            for legacy_hotkey in ("lctrl+win", "lctrl+lwin"):
                with self.subTest(legacy_hotkey=legacy_hotkey):
                    path.write_text(
                        json.dumps(
                            {
                                "voice_trigger_mode": "hold",
                                "voice_hotkey": legacy_hotkey,
                            }
                        ),
                        encoding="utf-8",
                    )
                    loaded = config.load_config(path)
                    self.assertEqual(loaded["voice_trigger_mode"], "hold")
                    self.assertEqual(loaded["voice_hotkey"], "ralt")

    def test_load_repairs_recorded_left_alt_to_right_alt_in_hold_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                json.dumps({"voice_trigger_mode": "hold", "voice_hotkey": "lalt"}),
                encoding="utf-8",
            )
            loaded = config.load_config(path)
        self.assertEqual(loaded["voice_trigger_mode"], "hold")
        self.assertEqual(loaded["voice_hotkey"], "ralt")


class SaveConfigPrivacyGuardTests(unittest.TestCase):
    def test_save_config_rejects_forbidden_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            bad_config = config.default_config()
            bad_config["address"] = "AA:BB:CC:DD:EE:FF"
            with self.assertRaises(config.ConfigPrivacyError):
                config.save_config(path, bad_config)
            self.assertFalse(path.exists())

    def test_save_key_bindings_rejects_device_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "key_bindings.json"
            bad_bindings = config.default_key_bindings()
            bad_bindings["device_token"] = "aabbccddeeff"
            with self.assertRaises(config.ConfigPrivacyError):
                config.save_key_bindings(path, bad_bindings)

    def test_save_rejects_device_paths_and_platform_device_ids(self):
        for forbidden_key in (
            "device_path",
            "hid_device_path",
            "raw_device_path",
            "device_id",
            "ble_device_id",
        ):
            with self.subTest(forbidden_key=forbidden_key), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "config.json"
                bad_config = config.default_config()
                bad_config["runtime"] = {forbidden_key: "private-machine-identity"}

                with self.assertRaises(config.ConfigPrivacyError):
                    config.save_config(path, bad_config)

                self.assertFalse(path.exists())

    def test_load_config_rejects_a_forbidden_key_found_on_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"interface_id": "abc"}), encoding="utf-8")
            with self.assertRaises(config.ConfigPrivacyError):
                config.load_config(path)

    # -- recursive guard (XRBM-014 review RETRY P1 #6): a forbidden key must
    #    be refused no matter how deeply it is nested inside dicts and
    #    dicts-inside-lists, not just at the top level. --------------------

    def test_rejects_forbidden_key_nested_two_levels_deep(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "key_bindings.json"
            bad_bindings = config.default_key_bindings()
            bad_bindings["bindings"]["menu"] = {
                "kind": "key_combo",
                "keys": ["a"],
                "metadata": {"address": "AA:BB:CC:DD:EE:FF"},
            }
            with self.assertRaises(config.ConfigPrivacyError) as ctx:
                config.save_key_bindings(path, bad_bindings)
            self.assertIn("bindings.menu.metadata.address", str(ctx.exception))
            self.assertFalse(path.exists())

    def test_rejects_forbidden_key_regardless_of_letter_case(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            bad_config = config.default_config()
            bad_config["metadata"] = {"Bluetooth_Address": "private"}

            with self.assertRaises(config.ConfigPrivacyError) as ctx:
                config.save_config(path, bad_config)

            self.assertIn("metadata.Bluetooth_Address", str(ctx.exception))
            self.assertFalse(path.exists())

    def test_rejects_forbidden_key_nested_inside_a_list_of_dicts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            bad_config = config.default_config()
            bad_config["history"] = [
                {"note": "fine"},
                {"device_token": "aabbccddeeff"},
            ]
            with self.assertRaises(config.ConfigPrivacyError) as ctx:
                config.save_config(path, bad_config)
            self.assertIn("history[1].device_token", str(ctx.exception))

    def test_rejects_forbidden_key_nested_three_levels_deep_in_mixed_structure(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            bad_config = config.default_config()
            bad_config["profiles"] = [
                {"devices": [{"bt_address": "AA:BB:CC:DD:EE:FF"}]},
            ]
            with self.assertRaises(config.ConfigPrivacyError) as ctx:
                config.save_config(path, bad_config)
            self.assertIn("profiles[0].devices[0].bt_address", str(ctx.exception))

    def test_deeply_nested_forbidden_key_found_on_load_from_disk_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                json.dumps({"a": {"b": [{"mac_address": "AA:BB:CC:DD:EE:FF"}]}}),
                encoding="utf-8",
            )
            with self.assertRaises(config.ConfigPrivacyError):
                config.load_config(path)

    def test_deeply_nested_structure_without_a_forbidden_key_saves_fine(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            good_config = config.default_config()
            good_config["profiles"] = [{"devices": [{"friendly_name": "Speakers"}]}]
            config.save_config(path, good_config)  # must not raise
            self.assertTrue(path.exists())


class RoundTripTests(unittest.TestCase):
    def test_load_config_rejects_a_non_object_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text("[]", encoding="utf-8")

            with self.assertRaises(config.ConfigFormatError):
                config.load_config(path)

    def test_load_key_bindings_rejects_a_non_object_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "key_bindings.json"
            path.write_text("[]", encoding="utf-8")

            with self.assertRaises(config.ConfigFormatError):
                config.load_key_bindings(path)

    def test_save_config_replaces_an_existing_file_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text('{"old": true}\n', encoding="utf-8")
            updated = config.default_config()
            updated["gain_db"] = 4.0

            config.save_config(path, updated)

            self.assertEqual(config.load_config(path)["gain_db"], 4.0)
            self.assertEqual(list(path.parent.glob(".config.json.*.tmp")), [])

    def test_failed_atomic_replace_does_not_leave_a_temporary_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text('{"old": true}\n', encoding="utf-8")
            with mock.patch.object(config.os, "replace", side_effect=OSError("locked")):
                with self.assertRaisesRegex(OSError, "locked"):
                    config.save_config(path, config.default_config())

            self.assertEqual(path.read_text(encoding="utf-8"), '{"old": true}\n')
            self.assertEqual(list(path.parent.glob(".config.json.*.tmp")), [])

    def test_save_and_load_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            original = config.default_config()
            original["gain_db"] = 3.5
            config.save_config(path, original)
            loaded = config.load_config(path)
            self.assertEqual(loaded["gain_db"], 3.5)

    def test_save_removes_the_retired_release_finish_setting(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            original = config.default_config()
            original["voice_release_finish_tap_enabled"] = True

            config.save_config(path, original)

            persisted = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["schema_version"], config.SCHEMA_VERSION)
        self.assertNotIn("voice_release_finish_tap_enabled", persisted)

    def test_load_missing_file_returns_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "does-not-exist.json"
            loaded = config.load_config(path)
            self.assertEqual(loaded, config.default_config())

    def test_key_bindings_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "key_bindings.json"
            original = config.default_key_bindings()
            original["display_notes"] = {
                "power": {"single_click": "  关机  "},
            }
            config.save_key_bindings(path, original)
            loaded = config.load_key_bindings(path)
        self.assertEqual(loaded["bindings"], original["bindings"])
        self.assertEqual(
            loaded["display_notes"],
            {"power": {"single_click": "关机"}},
        )

    def test_combo_bindings_round_trip_with_a_quicker_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "key_bindings.json"
            original = config.default_key_bindings()
            original["combo_bindings"] = {
                "modifier": "tv",
                "bindings": {
                    "up": {
                        "kind": "quicker_uri",
                        "keys": [],
                        "uri": "quicker:runaction:test-action",
                    }
                },
                "display_notes": {"up": "置顶窗口"},
            }

            config.save_key_bindings(path, original)
            loaded = config.load_key_bindings(path)

        self.assertEqual(loaded["combo_bindings"], original["combo_bindings"])

    def test_combo_bindings_fail_closed_for_forbidden_buttons_and_actions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "key_bindings.json"
            path.write_text(
                json.dumps(
                    {
                        "combo_bindings": {
                            "modifier": "mic",
                            "bindings": {
                                "mic": {"kind": "escape", "keys": []},
                                "power": {"kind": "escape", "keys": []},
                                "up": {"kind": "voice_hold", "keys": []},
                                "ok": {"kind": "return", "keys": []},
                            },
                            "display_notes": {
                                "mic": "忽略",
                                "ok": "确认",
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )

            loaded = config.load_key_bindings(path)

        self.assertEqual(loaded["combo_bindings"]["modifier"], "tv")
        self.assertEqual(
            loaded["combo_bindings"]["bindings"],
            {"ok": {"kind": "return", "keys": []}},
        )
        self.assertEqual(loaded["combo_bindings"]["display_notes"], {"ok": "确认"})

    def test_combo_bindings_fail_closed_when_modifier_has_delayed_gestures(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "key_bindings.json"
            path.write_text(
                json.dumps(
                    {
                        "secondary_bindings": {
                            "tv": {
                                "double_click": {"kind": "escape", "keys": []}
                            }
                        },
                        "combo_bindings": {
                            "modifier": "tv",
                            "bindings": {
                                "up": {"kind": "return", "keys": []}
                            },
                            "display_notes": {"up": "确认"},
                        },
                    }
                ),
                encoding="utf-8",
            )

            loaded = config.load_key_bindings(path)

        self.assertIn("tv", loaded["secondary_bindings"])
        self.assertEqual(loaded["combo_bindings"]["bindings"], {})
        self.assertEqual(loaded["combo_bindings"]["display_notes"], {})

    def test_load_key_bindings_cleans_unknown_or_empty_display_notes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "key_bindings.json"
            path.write_text(
                json.dumps(
                    {
                        "display_notes": {
                            "power": {
                                "single_click": "  关机  ",
                                "double_click": "   ",
                                "long_press": "未命名",
                                "unknown": "忽略",
                            },
                            "unknown_button": {"single_click": "忽略"},
                            "up": "错误类型",
                        }
                    }
                ),
                encoding="utf-8",
            )

            loaded = config.load_key_bindings(path)

        self.assertEqual(
            loaded["display_notes"],
            {"power": {"single_click": "关机"}},
        )

    def test_paired_save_rolls_back_config_when_bindings_save_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            bindings_path = root / "key_bindings.json"
            config_path.write_bytes(b'{"old_config": true}\n')
            bindings_path.write_bytes(b'{"old_bindings": true}\n')

            with mock.patch.object(
                config,
                "save_key_bindings",
                side_effect=OSError("bindings locked"),
            ):
                with self.assertRaisesRegex(OSError, "bindings locked"):
                    config.save_settings_pair(
                        config_path,
                        config.default_config(),
                        bindings_path,
                        config.default_key_bindings(),
                    )

            self.assertEqual(config_path.read_bytes(), b'{"old_config": true}\n')
            self.assertEqual(bindings_path.read_bytes(), b'{"old_bindings": true}\n')

    def test_paired_save_removes_new_config_when_second_save_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            bindings_path = root / "key_bindings.json"

            with mock.patch.object(
                config,
                "save_key_bindings",
                side_effect=OSError("bindings locked"),
            ):
                with self.assertRaisesRegex(OSError, "bindings locked"):
                    config.save_settings_pair(
                        config_path,
                        config.default_config(),
                        bindings_path,
                        config.default_key_bindings(),
                    )

            self.assertFalse(config_path.exists())
            self.assertFalse(bindings_path.exists())

    def test_paired_save_validates_both_documents_before_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            bindings_path = root / "key_bindings.json"
            bad_bindings = config.default_key_bindings()
            bad_bindings["device_path"] = "private"

            with self.assertRaises(config.ConfigPrivacyError):
                config.save_settings_pair(
                    config_path,
                    config.default_config(),
                    bindings_path,
                    bad_bindings,
                )

            self.assertFalse(config_path.exists())
            self.assertFalse(bindings_path.exists())

    def test_paired_save_reports_an_incomplete_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            bindings_path = root / "key_bindings.json"
            config_path.write_text('{"old": true}\n', encoding="utf-8")

            with mock.patch.object(
                config,
                "save_key_bindings",
                side_effect=OSError("bindings locked"),
            ), mock.patch.object(
                config,
                "_restore_file_snapshot",
                side_effect=OSError("rollback locked"),
            ):
                with self.assertRaises(config.ConfigTransactionError):
                    config.save_settings_pair(
                        config_path,
                        config.default_config(),
                        bindings_path,
                        config.default_key_bindings(),
                    )

    def test_single_config_save_restores_previous_file_when_readback_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            previous = b'{"old": true}\n'
            path.write_bytes(previous)

            with mock.patch.object(
                config,
                "load_config",
                side_effect=config.ConfigFormatError("readback failed"),
            ):
                with self.assertRaisesRegex(
                    config.ConfigFormatError, "readback failed"
                ):
                    config.save_config_and_load(path, config.default_config())

            self.assertEqual(path.read_bytes(), previous)

    def test_single_config_save_reports_an_incomplete_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text('{"old": true}\n', encoding="utf-8")

            with mock.patch.object(
                config,
                "load_config",
                side_effect=config.ConfigFormatError("readback failed"),
            ), mock.patch.object(
                config,
                "_restore_file_snapshot",
                side_effect=OSError("rollback locked"),
            ):
                with self.assertRaises(config.ConfigTransactionError):
                    config.save_config_and_load(path, config.default_config())

    def test_legacy_reference_chords_are_migrated_to_semantic_actions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "key_bindings.json"
            path.write_text(
                json.dumps(
                    {
                        "bindings": {
                            "up": {"kind": "key_combo", "keys": ["up"]},
                            "home": {
                                "kind": "key_combo",
                                "keys": ["win", "d"],
                            },
                            "tv": {
                                "kind": "key_combo",
                                "keys": ["alt", "esc"],
                            },
                            "power": {
                                "kind": "key_combo",
                                "keys": ["ctrl", "shift", "p"],
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )

            loaded = config.load_key_bindings(path)

        self.assertEqual(
            loaded["bindings"]["up"]["kind"],
            key_mapping.ActionKind.ARROW_UP.value,
        )
        self.assertEqual(
            loaded["bindings"]["home"]["kind"],
            key_mapping.ActionKind.SHOW_DESKTOP.value,
        )
        self.assertEqual(
            loaded["bindings"]["tv"]["kind"],
            key_mapping.ActionKind.APP_SWITCHER.value,
        )
        self.assertEqual(
            loaded["bindings"]["power"],
            {"kind": "key_combo", "keys": ["ctrl", "shift", "p"]},
        )


class EditableMicBindingTests(unittest.TestCase):
    def test_an_ordinary_mic_binding_on_disk_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "key_bindings.json"
            stale = config.default_key_bindings()
            stale["bindings"]["mic"] = {"kind": "key_combo", "keys": ["a"]}
            path.write_text(json.dumps(stale), encoding="utf-8")

            loaded = config.load_key_bindings(path)

            self.assertEqual(
                loaded["bindings"]["mic"],
                {"kind": "key_combo", "keys": ["a"]},
            )

    def test_a_missing_mic_binding_uses_the_hold_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "key_bindings.json"
            stale = config.default_key_bindings()
            del stale["bindings"]["mic"]
            path.write_text(json.dumps(stale), encoding="utf-8")

            loaded = config.load_key_bindings(path)

            self.assertEqual(
                loaded["bindings"]["mic"],
                {"kind": "voice_hold", "keys": []},
            )

    def test_default_key_bindings_mic_is_explicit_hold_voice(self):
        self.assertEqual(
            config.default_key_bindings()["bindings"]["mic"],
            {"kind": "voice_hold", "keys": []},
        )

    def test_legacy_toggle_binding_is_failed_closed_without_overwriting_source(self):
        stored = config.default_key_bindings()
        stored["bindings"]["mic"] = {"kind": "voice_toggle", "keys": []}
        current_config = config.default_config()

        removed = config.normalize_voice_product_boundary(current_config, stored)

        self.assertEqual(removed, {"mic": "voice_toggle"})
        self.assertEqual(
            stored["bindings"]["mic"],
            {"kind": "voice_toggle", "keys": []},
        )

    def test_non_mic_voice_binding_fails_closed_for_the_whole_button(self):
        stored = config.default_key_bindings()
        stored["bindings"]["up"] = {"kind": "voice_hold", "keys": []}
        stored["secondary_bindings"]["up"] = {
            "double_click": {"kind": "escape", "keys": []}
        }

        removed = config.normalize_voice_product_boundary(
            config.default_config(),
            stored,
        )

        self.assertEqual(removed, {"up": "voice_hold"})

    def test_legacy_generic_voice_binding_is_preserved_for_cross_file_migration(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "key_bindings.json"
            stored = config.default_key_bindings()
            stored["bindings"]["mic"] = {"kind": "voice", "keys": []}
            path.write_text(json.dumps(stored), encoding="utf-8")

            loaded = config.load_key_bindings(path)

        self.assertEqual(loaded["bindings"]["mic"], {"kind": "voice", "keys": []})

    def test_mic_secondary_mapping_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "key_bindings.json"
            stored = config.default_key_bindings()
            stored["secondary_bindings"] = {
                "mic": {
                    "double_click": {"kind": "escape", "keys": []}
                }
            }
            path.write_text(json.dumps(stored), encoding="utf-8")

            loaded = config.load_key_bindings(path)

        self.assertEqual(
            loaded["secondary_bindings"]["mic"]["double_click"]["kind"],
            "escape",
        )


if __name__ == "__main__":
    unittest.main()
