"""Literal text mappings, persistence and paste failure boundaries."""
import os
import ctypes
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from ovb_rc003 import config, key_mapping as km, mapping_presets, settings_ui, win32_input
from tests import test_app_wiring, test_qt_settings_app

TEXT = " 你好😀\nCtrl+C\nEnter\nquicker:runaction:example\t "


class TextMappingTests(unittest.TestCase):
    def test_literal_text_round_trip_and_no_repeat(self):
        action = km.ButtonAction(km.ActionKind.TEXT, text=TEXT)
        self.assertEqual(km.ButtonAction.from_dict(action.to_dict()), action)
        self.assertNotIn(TEXT, repr(action))
        self.assertFalse(km.action_allows_repeat(action))
        self.assertEqual(settings_ui._action_to_display(action), "输入文字")
        self.assertEqual(settings_ui._display_to_action("Ctrl+C").kind, km.ActionKind.KEY_COMBO)
        for trigger in km.ButtonTrigger:
            parsed = settings_ui._validated_button_action("ok", trigger.value, "输入文字", text_content=TEXT)
            self.assertEqual(parsed.text, TEXT)

    def test_invalid_content_rejected_before_save(self):
        for value in ("", None, 123, "a\0b", "\ud800", "字" * (km.MAX_TEXT_LENGTH + 1)):
            with self.subTest(value_type=type(value).__name__), self.assertRaises(ValueError):
                km.ButtonAction.from_dict({"kind": "text", "text": value})
        for data in ({"kind": "text", "text": TEXT, "keys": ["enter"]},
                     {"kind": "text", "text": TEXT, "uri": "quicker:runaction:x"},
                     {"kind": "key_combo", "keys": ["enter"], "text": TEXT}):
            with self.assertRaises(ValueError):
                km.ButtonAction.from_dict(data)
        self.assertEqual(km.validate_preset_text(" \n\t "), " \n\t ")

    def test_save_reload_and_preset_import_export_for_both_profiles(self):
        for profile in ("xiaomi-rc003", "chromecast-remote"):
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "config"
                root.mkdir()
                settings = config.default_config()
                settings["selected_device_profile"] = profile
                original = config.default_key_bindings(profile)
                mapping = {button: settings_ui._action_to_display(km.ButtonAction.from_dict(raw))
                           for button, raw in original["bindings"].items()}
                mapping["ok"] = "输入文字"
                secondary = {"ok": {"double_click": "输入文字", "long_press": "输入文字"}}
                content = {"ok": {trigger.value: TEXT + trigger.value for trigger in km.ButtonTrigger}}
                new_config, bindings = settings_ui.build_save_model(
                    button_display_map=mapping, secondary_display_map=secondary,
                    text_content_map=content, hotkey_text="ralt", trigger_mode=km.VoiceTriggerMode.HOLD,
                    endpoint_display_text="", base_config=settings, base_bindings=original,
                    selected_device_profile=profile, validate_voice_hotkeys=False)
                config.save_settings_pair(config.config_path(root), new_config, config.key_bindings_path(root), bindings)
                loaded = config.load_key_bindings(config.key_bindings_path(root))
                for trigger in km.ButtonTrigger:
                    self.assertEqual(km.button_action_for(loaded, "ok", trigger).text, content["ok"][trigger.value])
                snapshot = mapping_presets.snapshot(loaded, profile)
                store = mapping_presets.PresetStore(root)
                identity = store.create("文字", profile, snapshot)
                export = Path(directory) / "export.json"
                store.export_file(identity, export)
                imported = store.import_file(export, profile)
                self.assertEqual(store.get(imported)["mapping"], snapshot)
                self.assertEqual(mapping_presets.apply_to(original, store.get(imported), profile)["bindings"]["ok"]["text"],
                                 content["ok"]["single_click"])


class PasteBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.writer = mock.Mock(return_value=42)
        self.sender = mock.Mock(side_effect=lambda events: len(events))
        self.kwargs = dict(_writer=self.writer, _target_query=lambda: (1, 2),
                          _sequence_query=lambda: 42, _key_down_query=lambda vk: False, _sender=self.sender)

    def test_one_paste_no_enter_or_restoration(self):
        win32_input.paste_text(TEXT, **self.kwargs)
        self.writer.assert_called_once_with(TEXT)
        self.assertEqual(self.sender.call_args.args[0], [(0x11, False), (0x56, False), (0x56, True), (0x11, True)])

    def test_busy_or_changed_clipboard_and_changed_focus_do_not_paste(self):
        self.writer.side_effect = OSError("clipboard is busy")
        with self.assertRaises(OSError):
            win32_input.paste_text(TEXT, **self.kwargs)
        self.sender.assert_not_called()
        self.writer.side_effect = None
        with self.assertRaises(OSError):
            win32_input.paste_text(TEXT, **{**self.kwargs, "_sequence_query": lambda: 43})
        self.sender.assert_not_called()
        with self.assertRaises(OSError):
            win32_input.paste_text(TEXT, **{**self.kwargs, "_target_query": mock.Mock(side_effect=[(1, 2), (1, 3)])})
        self.sender.assert_not_called()

    def test_held_modifier_rejected_before_clipboard_write(self):
        with self.assertRaises(win32_input.PhysicalKeyInUseError):
            win32_input.paste_text(TEXT, **{**self.kwargs, "_key_down_query": lambda vk: vk == 0x12})
        self.writer.assert_not_called()
        self.sender.assert_not_called()

    def test_partial_paste_releases_keys_but_never_retypes_text(self):
        self.sender.side_effect = [2, 1, 1]
        with self.assertRaises(OSError):
            win32_input.paste_text(TEXT, **self.kwargs)
        self.writer.assert_called_once()
        self.assertTrue(all(up for call in self.sender.call_args_list[1:] for _, up in call.args[0]))

    def test_simultaneous_writes_and_shortcuts_are_serialized(self):
        entered, release = threading.Event(), threading.Event()
        calls, errors = [], []
        def writer(value):
            calls.append(value)
            if value == "first":
                entered.set()
                if not release.wait(2):
                    raise RuntimeError("test timed out")
            return 42
        def run(value):
            try:
                win32_input.paste_text(value, **{**self.kwargs, "_writer": writer})
            except Exception as exc:
                errors.append(exc)
        first = threading.Thread(target=run, args=("first",))
        second = threading.Thread(target=run, args=("second",))
        first.start()
        try:
            self.assertTrue(entered.wait(2))
            second.start()
            self.assertEqual(calls, ["first"])
        finally:
            release.set()
            first.join(3)
            if second.ident is not None:
                second.join(3)
        self.assertFalse(errors)
        self.assertEqual(calls, ["first", "second"])
        self.assertEqual(self.sender.call_count, 2)

    def test_live_input_guard_precedes_clipboard_access(self):
        with mock.patch.dict(os.environ, RC003_DISABLE_LIVE_INPUT="1"), \
             mock.patch.object(win32_input, "_write_clipboard_text") as writer:
            with self.assertRaises(win32_input.Win32InputUnavailableError):
                win32_input.paste_text(TEXT)
            writer.assert_not_called()

    @unittest.skipUnless(sys.platform == "win32", "native Windows memory and HWND")
    def test_native_owner_and_unicode_memory_without_user_clipboard(self):
        # Native allocator/window ABI, with only the shared clipboard boundary
        # substituted. This never calls real OpenClipboard/EmptyClipboard.
        user = ctypes.WinDLL("user32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        native_dll = ctypes.WinDLL
        allocations, transferred = [], []
        class User:
            def __getattr__(self, name):
                return getattr(user, name)
        proxy = User()
        proxy.OpenClipboard = mock.Mock(return_value=True)
        proxy.EmptyClipboard = mock.Mock(return_value=True)
        proxy.GetClipboardSequenceNumber = mock.Mock(return_value=17)
        def close():
            # Windows synthesizes text formats when closing the clipboard.
            proxy.GetClipboardSequenceNumber.return_value = 20
            return True
        proxy.CloseClipboard = mock.Mock(side_effect=close)
        proxy.GetClipboardOwner = mock.Mock(side_effect=lambda: proxy.OpenClipboard.call_args.args[0])
        def transfer(format_id, handle):
            self.assertEqual(format_id, 13)
            address = kernel.GlobalLock(handle)
            try:
                transferred.append(ctypes.wstring_at(address))
            finally:
                kernel.GlobalUnlock(handle)
            allocations.append(handle)
            return handle
        proxy.SetClipboardData = mock.Mock(side_effect=transfer)
        with mock.patch.object(ctypes, "WinDLL", side_effect=lambda name, **kw:
                               proxy if name == "user32" else kernel if name == "kernel32" else native_dll(name, **kw)):
            try:
                self.assertEqual(win32_input._write_clipboard_text(TEXT), 20)
                self.assertEqual(transferred, [TEXT.replace("\n", "\r\n")])
                self.assertTrue(proxy.OpenClipboard.call_args.args[0])
                proxy.CloseClipboard.assert_called_once()
                proxy.GetClipboardOwner.side_effect = None
                proxy.GetClipboardOwner.return_value = None
                with self.assertRaisesRegex(OSError, "clipboard changed"):
                    win32_input._write_clipboard_text(TEXT)
            finally:
                for handle in allocations:
                    self.assertFalse(kernel.GlobalFree(handle))


class TextActionWiringTests(test_app_wiring._AppWiringTestCase):
    def test_dispatch_and_cleanup_ownership_for_text(self):
        action = km.ButtonAction(km.ActionKind.TEXT, text=TEXT)
        self.app._bindings["bindings"]["ok"] = action.to_dict()
        with mock.patch.object(win32_input, "paste_text") as paste:
            self.app._on_button_trigger("ok", km.ButtonTrigger.SINGLE_CLICK)
            paste.assert_called_once_with(TEXT)
        self.assertFalse(self.app._is_button_repeatable("ok"))
        with mock.patch.object(win32_input, "paste_text", side_effect=win32_input.InputCleanupIncompleteError("test")):
            self.app._apply_button_action(action)
        self.assertEqual(self.app._button_key_release_pending, ("ctrl", "v"))

    def test_stopped_input_does_not_write_clipboard(self):
        self.app._accept_input_events = False
        with mock.patch.object(win32_input, "paste_text") as paste:
            self.app._apply_button_action(km.ButtonAction(km.ActionKind.TEXT, text=TEXT))
        paste.assert_not_called()


@unittest.skipUnless(test_qt_settings_app._HAS_PYSIDE6, test_qt_settings_app._SKIP_REASON)
class TextControllerTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_qt_settings_app.SettingsControllerTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)
        self.controller, self.model = self.fixture._make_controller()
        self.row = self.model.index_of("ok")

    def test_body_only_edit_persists_and_failure_keeps_draft(self):
        model, controller, row = self.model, self.controller, self.row
        model.setTextContentAt(row, "single_click", TEXT)
        model.setActionTextAt(row, "输入文字")
        self.assertTrue(controller._save_mapping())
        controller._load_bindings_into_model()
        self.assertEqual(model.textContentAt(row, "single_click"), TEXT)
        model.setTextContentAt(row, "single_click", "第二段")
        with mock.patch.object(config, "save_settings_pair", side_effect=OSError("test")):
            self.assertFalse(controller._save_mapping())
        self.assertEqual(model.textContentAt(row, "single_click"), "第二段")
        self.assertEqual(controller._bindings["bindings"]["ok"]["text"], TEXT)
        self.assertTrue(controller._save_mapping())
        self.assertEqual(controller._bindings["bindings"]["ok"]["text"], "第二段")
        model.setActionTextAt(row, "Ctrl+C")
        self.assertTrue(controller._save_mapping())
        self.assertNotIn("text", controller._bindings["bindings"]["ok"])
        self.assertEqual(model.textContentAt(row, "single_click"), "")

    def test_empty_text_error_and_voice_restriction(self):
        self.assertIn("填写", self.controller.buttonActionValidationMessage("ok", "single_click", "输入文字", ""))
        self.assertEqual(self.controller.buttonActionValidationMessage("ok", "single_click", "输入文字", "Ctrl+C"), "")
        self.assertIn("输入文字", self.controller.primaryActionOptionsFor("ok"))
        self.assertIn("输入文字", self.controller.secondaryActionOptionsFor("ok"))
        self.assertIn("输入文字", self.controller.longPressActionOptionsFor("ok"))
