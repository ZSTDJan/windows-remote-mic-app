import unittest
from unittest import mock

from ovb_rc003 import config, device_catalog, key_mapping, remote_layout, resources, settings_ui
from tests import test_remote_settings as persistence_fixture, test_remote_selection as selection_fixture
from tests.test_remote_settings import A, B


class ChromecastLayoutTests(unittest.TestCase):
    def test_exact_physical_buttons_and_defaults(self):
        profile = device_catalog.CHROMECAST_ID
        order = remote_layout.button_order(profile)
        self.assertEqual(len(order), 15)
        self.assertEqual(set(order), set(remote_layout.CHROMECAST_REPORT_CODES) | {"mic"})
        self.assertNotIn("menu", order)
        self.assertNotIn("tv", order)
        defaults = remote_layout.default_actions(profile)
        for key in ("youtube", "netflix", "input_source"):
            self.assertEqual(defaults[key].kind, key_mapping.ActionKind.DISABLED)
        self.assertEqual(defaults["volume_mute"].kind, key_mapping.ActionKind.SYSTEM_VOLUME_MUTE)
        for button in order:
            hotspot = remote_layout.hotspot_for(button, profile)
            self.assertTrue(0 < hotspot.x <= 1 and 0 < hotspot.y <= 1)
            self.assertTrue(hotspot.width > 0 and hotspot.height > 0)
        self.assertEqual(len(remote_layout.button_order()), 13)

    def test_photo_source_is_a_separate_asset(self):
        chrome = resources.find_remote_photo(device_catalog.CHROMECAST_ID)
        xiaomi = resources.find_remote_photo()
        self.assertIsNotNone(chrome)
        self.assertNotEqual(chrome, xiaomi)
        import hashlib
        self.assertEqual(hashlib.sha256(chrome.read_bytes()).hexdigest(),
                         "72f0350e9cafd91f2d13c7aa8618fbd5e6c89f27effdb46d74ededdb093f9712")
        from PySide6.QtGui import QImage
        image = QImage(str(chrome))
        self.assertEqual((image.width(), image.height()), (453, 1443))
        self.assertTrue(image.hasAlphaChannel())
        self.assertEqual(image.pixelColor(0, 0).alpha(), 0)


class ChromecastMappingPersistenceTests(unittest.TestCase):
    setUp = persistence_fixture.EntitySettingsTests.setUp
    switch = persistence_fixture.EntitySettingsTests.switch

    def test_new_button_mapping_notes_survive_save_switch_and_reload(self):
        profile = device_catalog.CHROMECAST_ID
        settings, bindings = self.switch(B)
        view = settings_ui.default_display_state(profile)
        view.button_display_map["youtube"] = "ctrl+f8"
        view.secondary_display_map["input_source"]["double_click"] = "ctrl+f9"
        new_settings, new_bindings = settings_ui.build_save_model(
            button_display_map=view.button_display_map,
            secondary_display_map=view.secondary_display_map,
            display_note_map={"youtube": {"single_click": "我的操作"}},
            hotkey_text="ralt", trigger_mode=key_mapping.VoiceTriggerMode.HOLD,
            endpoint_display_text="", base_config=settings, base_bindings=bindings,
            selected_device_profile=profile, validate_voice_hotkeys=False)
        config.save_settings_pair(self.settings, new_settings, self.bindings, new_bindings)
        _, xiaomi = self.switch(A)
        self.assertNotIn("youtube", xiaomi["bindings"])
        _, chrome = self.switch(B)
        self.assertEqual(chrome["bindings"]["youtube"]["keys"], ["ctrl", "f8"])
        self.assertEqual(chrome["secondary_bindings"]["input_source"]["double_click"]["keys"], ["ctrl", "f9"])
        self.assertEqual(chrome["display_notes"]["youtube"]["single_click"], "我的操作")
        self.assertNotIn("menu", chrome["bindings"])


class ChromecastMappingControllerTests(unittest.TestCase):
    setUp = selection_fixture.SelectionControllerTests.setUp
    tearDown = selection_fixture.SelectionControllerTests.tearDown
    _make_controller = selection_fixture.SelectionControllerTests._make_controller
    seed_chromecast = selection_fixture.SelectionControllerTests.seed_chromecast

    def test_current_entity_only_auto_save_restore_and_model_reset(self):
        controller = self.seed_chromecast()
        model = controller._model
        with mock.patch.object(controller, "_refresh_bridge_status", return_value=False):
            controller.useRemoteDevice(selection_fixture.B)
            self.assertEqual(model.rowCount(), 15)
            self.assertEqual(model.index_of("menu"), -1)
            model.setActionTextAt(model.index_of("youtube"), "ctrl+f8")
            self.assertTrue(controller._save_mapping(), controller.errorMessage)
            controller.useRemoteDevice(selection_fixture.A)
            self.assertEqual(model.rowCount(), 13)
            self.assertEqual(model.index_of("youtube"), -1)
            controller.restoreMappingDefaults()
            self.assertTrue(controller._save_mapping(), controller.errorMessage)
            controller.useRemoteDevice(selection_fixture.B)
            self.assertEqual(model.to_display_map()["youtube"], "ctrl+f8")
            self.assertIn("Chromecast-remote-photo.png", controller.photoSource)
            controller.restoreMappingDefaults()
            self.assertTrue(controller._save_mapping(), controller.errorMessage)
            self.assertEqual(model.to_display_map()["youtube"], "禁用")
