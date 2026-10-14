import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ovb_rc003 import config, device_catalog, mapping_presets as presets, key_mapping
from tests import test_qt_settings_app as qt_tests


class PresetStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "data"
        self.store = presets.PresetStore(self.root)
        self.profile = device_catalog.RC003_ID
        self.bindings = config.default_key_bindings(self.profile)
        self.bindings["physical_bindings"] = {"power": {"vk": 123}}
        self.mapping = presets.snapshot(self.bindings, self.profile)

    def create(self, name="工作"):
        return self.store.create(name, self.profile, self.mapping)

    def test_full_roundtrip_is_independent_and_contains_no_identity(self):
        identity = self.create()
        before = copy.deepcopy(self.bindings)
        export = Path(self.tmp.name) / "backup.json"
        self.store.export_file(identity, export)
        value = json.loads(export.read_text(encoding="utf-8"))
        self.assertEqual(set(value["mapping"]), set(presets.MAPPING_FIELDS))
        self.assertNotIn("id", value)
        imported = self.store.import_file(export, self.profile)
        self.assertNotEqual(imported, identity)
        self.assertEqual(self.store.get(imported)["name"], "工作 (2)")
        self.assertEqual(self.bindings, before)
        applied = presets.apply_to(before, self.store.get(imported), self.profile)
        self.assertEqual(applied["physical_bindings"], before["physical_bindings"])
        self.assertEqual(applied["bindings"], before["bindings"])

    def test_rename_uses_id_and_delete_all_does_not_write_active_files(self):
        first, second = self.create("A"), self.create("B")
        self.store.rename(first, "A改名")
        self.assertEqual(self.store.get(second)["name"], "B")
        for identity in (first, second):
            self.store.delete(identity)
        self.assertEqual(self.store.load(), [])
        self.assertFalse(config.config_path(self.root).exists())
        self.assertFalse(config.key_bindings_path(self.root).exists())

    def test_duplicate_and_stale_ids_do_not_modify_library(self):
        identity = self.create()
        before = self.store.path.read_bytes()
        for operation in (lambda: self.create(), lambda: self.store.rename("missing", "x"),
                          lambda: self.store.delete("missing")):
            with self.assertRaises(ValueError):
                operation()
            self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(self.store.get(identity)["name"], "工作")

    def test_atomic_failure_preserves_previous_bytes_and_removes_temp_file(self):
        identity = self.create()
        before = self.store.path.read_bytes()
        with mock.patch.object(config.os, "replace", side_effect=PermissionError("locked")):
            with self.assertRaises(PermissionError):
                self.store.rename(identity, "新名称")
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(list(self.root.iterdir()), [self.store.path])

    def test_corrupted_library_is_not_silently_replaced(self):
        self.root.mkdir()
        self.store.path.write_text("{bad", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.create()
        self.assertEqual(self.store.path.read_text(), "{bad")

    def test_export_cannot_overwrite_active_data_or_library(self):
        identity = self.create()
        before = self.store.path.read_bytes()
        for name in (presets.FILENAME, "config.json", "key_bindings.json"):
            with self.assertRaises(ValueError):
                self.store.export_file(identity, self.root / name)
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_import_validation_rejects_wrong_profile_unknown_version_and_settings(self):
        identity = self.create()
        export = Path(self.tmp.name) / "backup.json"
        self.store.export_file(identity, export)
        original = json.loads(export.read_text())
        invalid = []
        for key, value in (("schema_version", 2), ("schema_version", True), ("profile", "other"),
                           ("voice_hotkey", "f2")):
            document = copy.deepcopy(original)
            document[key] = value
            invalid.append(document)
        document = copy.deepcopy(original)
        document["mapping"]["physical_bindings"] = {}
        invalid.append(document)
        for document in invalid:
            export.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaises(ValueError):
                self.store.import_file(export, self.profile)
        self.assertEqual(len(self.store.load()), 1)

    def test_all_four_holds_roundtrip_only_in_long_slot(self):
        for kind in key_mapping.MOUSE_HOLD_BUTTONS:
            value = copy.deepcopy(self.mapping)
            value["secondary_bindings"] = {"ok": {"long_press": {"kind": kind.value}}}
            presets.validate_mapping(value, self.profile)
            value["secondary_bindings"]["ok"] = {"double_click": {"kind": kind.value}}
            with self.assertRaises(ValueError):
                presets.validate_mapping(value, self.profile)
            value["secondary_bindings"] = {}
            value["bindings"]["ok"] = {"kind": kind.value}
            with self.assertRaises(ValueError):
                presets.validate_mapping(value, self.profile)

    def test_unsafe_keys_voice_conflicts_and_unknown_buttons_are_rejected(self):
        for raw in ({"kind": "key_combo", "keys": ["unknown-key"]},
                    {"kind": "voice_hold"}, {"kind": "unknown"}):
            value = copy.deepcopy(self.mapping)
            value["bindings"]["ok"] = raw
            with self.assertRaises(ValueError):
                presets.validate_mapping(value, self.profile)
        value = copy.deepcopy(self.mapping)
        value["secondary_bindings"]["mic"] = {"long_press": {"kind": "mouse_left_hold"}}
        with self.assertRaises(ValueError):
            presets.validate_mapping(value, self.profile)

    def test_both_supported_profiles_and_device_compatibility(self):
        for profile in presets.PROFILES:
            mapping = presets.snapshot(config.default_key_bindings(profile), profile)
            identity = self.store.create("默认", profile, mapping)
            presets.apply_to(config.default_key_bindings(profile), self.store.get(identity), profile)
        other = self.store.load()[1]
        with self.assertRaises(ValueError):
            presets.apply_to(self.bindings, other, self.profile)

    def test_automatic_row_tracks_current_settings_without_creating_an_extra_mapping_file(self):
        row = self.store.view(self.bindings, self.profile)[0]
        self.assertEqual(row["name"], "26-1012 自动保存")
        self.assertTrue(row["automatic"] and row["applied"])
        self.bindings["bindings"]["ok"] = {"kind": "escape", "keys": []}
        latest = presets.PresetStore(self.root).view(self.bindings, self.profile)[0]
        self.assertEqual(latest["name"], row["name"])
        self.assertEqual(latest["mapping"]["bindings"]["ok"]["kind"], "escape")
        self.assertFalse(self.store.path.exists())
        self.assertFalse(config.key_bindings_path(self.root).exists())

    def test_applied_marker_survives_reopen_but_never_follows_browsing_or_changed_values(self):
        first, second = self.create("A"), self.create("B")
        def applied(bindings=None):
            return [row["id"] for row in presets.PresetStore(self.root).view(
                self.bindings if bindings is None else bindings, self.profile) if row["applied"]]
        self.assertEqual(applied(), [presets.AUTO_ID])
        self.store.mark_applied(self.profile, second)
        self.store.rename(second, "B改名")
        self.assertEqual(applied(), [second])
        changed = copy.deepcopy(self.bindings)
        changed["bindings"]["ok"] = {"kind": "escape", "keys": []}
        self.assertEqual(applied(changed), [presets.AUTO_ID])
        other_profile = device_catalog.CHROMECAST_ID
        self.store.view(config.default_key_bindings(other_profile), other_profile)
        self.assertEqual(applied(), [second])
        self.store.delete(second)
        self.assertEqual(applied(), [presets.AUTO_ID])
        self.assertEqual(self.store.get(first)["name"], "A")

    def test_v1_upgrade_preserves_snapshots_and_failed_upgrade_keeps_original_file(self):
        identity = self.create()
        old = {"schema_version": 1, "presets": self.store.load()}
        self.store.path.write_text(json.dumps(old), encoding="utf-8")
        before = self.store.path.read_bytes()
        self.assertEqual(self.store.load(), old["presets"])
        rows = self.store.view(self.bindings, self.profile)
        self.assertEqual(rows[1]["id"], identity)
        self.assertEqual(self.store.path.read_bytes(), before)
        with mock.patch.object(config.os, "replace", side_effect=PermissionError("locked")):
            with self.assertRaises(PermissionError):
                self.store.mark_applied(self.profile, identity)
        self.assertEqual(self.store.path.read_bytes(), before)
        self.store.mark_applied(self.profile, identity)
        self.assertEqual(json.loads(self.store.path.read_text())["presets"], old["presets"])
        self.assertEqual(json.loads(self.store.path.read_text())["schema_version"], 2)

    def test_automatic_export_roundtrip_and_reserved_name(self):
        row = self.store.view(self.bindings, self.profile)[0]
        export = Path(self.tmp.name) / "automatic.json"
        self.store.export_entry(row, export)
        imported = self.store.import_file(export, self.profile)
        self.assertEqual(self.store.get(imported)["mapping"], row["mapping"])
        self.assertEqual(self.store.get(imported)["name"], row["name"] + " (2)")
        with self.assertRaises(ValueError):
            self.create(row["name"])
        with self.assertRaises(ValueError):
            self.store.rename(imported, row["name"])

    def test_invalid_current_mapping_does_not_hide_valid_presets_or_rewrite_data(self):
        identity = self.create()
        self.store.mark_applied(self.profile, identity)
        before = self.store.path.read_bytes()
        for raw in ({"kind": "voice_toggle", "keys": []}, {"kind": "unknown"}, {"keys": []}, None):
            with self.subTest(raw=raw):
                damaged = copy.deepcopy(self.bindings)
                damaged["bindings"]["mic"] = raw
                rows = self.store.view(damaged, self.profile)
                self.assertEqual([row["id"] for row in rows], [presets.AUTO_ID, identity])
                self.assertTrue(rows[0]["automatic"] and rows[0]["applied"])
                self.assertIsNone(rows[0]["mapping"])
                self.assertEqual(rows[1]["mapping"], self.mapping)
                with self.assertRaises(ValueError):
                    self.store.export_entry(rows[0], Path(self.tmp.name) / "invalid.json")
                self.assertEqual(self.store.path.read_bytes(), before)
        with self.assertRaises(ValueError):
            self.store.view(self.bindings, "unsupported")


@unittest.skipUnless(qt_tests._HAS_PYSIDE6, qt_tests._SKIP_REASON)
class PresetControllerTests(unittest.TestCase):
    setUp = qt_tests.SettingsControllerTests.setUp
    tearDown = qt_tests.SettingsControllerTests.tearDown
    _make_controller = qt_tests.SettingsControllerTests._make_controller

    def test_copy_rename_apply_export_import_delete_without_other_settings_changes(self):
        from PySide6.QtCore import QUrl
        controller, model = self._make_controller(selected_remote=True)
        run = controller.mappingPresetAction
        controller._bindings["bindings"]["ok"] = {"kind": "mouse_right_click", "keys": []}
        controller._load_bindings_into_model()
        self.assertTrue(controller._save_mapping())
        original_config = copy.deepcopy(controller._config)
        original_physical = copy.deepcopy(controller._bindings.get("physical_bindings"))
        saved = run("copy", presets.AUTO_ID, "配置 A")
        self.assertTrue(saved["ok"], saved)
        duplicate = run("copy", saved["id"], "配置 A 副本")
        self.assertTrue(duplicate["ok"], duplicate)
        self.assertTrue(run("rename", saved["id"], "A改名")["ok"])
        self.assertEqual(controller.mappingPresets[2]["name"], "配置 A 副本")
        controller._bindings["bindings"]["ok"] = {"kind": "escape", "keys": []}
        controller._load_bindings_into_model()
        self.assertTrue(controller._save_mapping())
        result = run("apply", saved["id"], "")
        self.assertTrue(result["ok"], result)
        self.assertEqual(controller._bindings["bindings"]["ok"]["kind"], "mouse_right_click")
        self.assertEqual(controller._config, original_config)
        self.assertEqual(controller._bindings.get("physical_bindings"), original_physical)
        export = QUrl.fromLocalFile(str(Path(self._tmpdir.name) / "export.json")).toString()
        self.assertTrue(run("export", saved["id"], export)["ok"])
        before = copy.deepcopy(controller._bindings)
        result = run("import", "", export)
        self.assertTrue(result["ok"], result)
        self.assertEqual(controller._bindings, before)
        for row in list(controller.mappingPresets):
            if not row["automatic"]:
                self.assertTrue(run("delete", row["id"], "")["ok"])
        self.assertEqual(len(controller.mappingPresets), 1)
        self.assertTrue(controller.mappingPresets[0]["automatic"])
        self.assertEqual(controller._bindings, before)

    def test_failed_apply_keeps_disk_and_model_and_drafts_block_apply(self):
        controller, model = self._make_controller()
        self.assertTrue(controller._save_mapping())
        run = controller.mappingPresetAction
        saved = run("copy", presets.AUTO_ID, "备份")
        self.assertTrue(saved["ok"], saved)
        controller._bindings["bindings"]["ok"] = {"kind": "escape", "keys": []}
        controller._load_bindings_into_model()
        controller._save_mapping()
        before = copy.deepcopy(controller._bindings)
        display = model.to_display_map()
        with mock.patch.object(config, "save_settings_pair", side_effect=OSError("disk full")):
            self.assertFalse(run("apply", saved["id"], "")["ok"])
        self.assertEqual(controller._bindings, before)
        self.assertEqual(model.to_display_map(), display)
        controller._mapping_dirty = True
        self.assertFalse(run("apply", saved["id"], "")["ok"])
        self.assertFalse(run("copy", presets.AUTO_ID, "草稿")["ok"])

    def test_long_options_do_not_leak_to_single_or_double(self):
        controller, model = self._make_controller()
        from ovb_rc003 import settings_ui
        for label in settings_ui.LONG_PRESS_ACTION_LABELS.values():
            self.assertIn(label, controller.longPressActionOptionsFor("ok"))
            self.assertNotIn(label, controller.primaryActionOptionsFor("ok"))
            self.assertNotIn(label, controller.secondaryActionOptionsFor("ok"))

    def test_automatic_entry_and_marker_survive_reopen_and_failed_marker_write_is_reported(self):
        controller, _ = self._make_controller()
        run = controller.mappingPresetAction
        opened = run("refresh", "", "")
        self.assertTrue(opened["ok"], opened)
        self.assertEqual(opened["id"], presets.AUTO_ID)
        self.assertTrue(controller.mappingPresets[0]["applied"])
        for operation in ("rename", "delete"):
            self.assertFalse(run(operation, presets.AUTO_ID, "不可操作")["ok"])
        saved = run("copy", presets.AUTO_ID, "保留副本")
        self.assertTrue(saved["ok"], saved)
        self.assertTrue(controller.mappingPresets[0]["applied"])
        with mock.patch.object(presets.PresetStore, "mark_applied", side_effect=OSError("disk full")):
            result = run("apply", saved["id"], "")
        self.assertTrue(result["ok"], result)
        self.assertIn("应用标记未能保存", result["message"])
        self.assertTrue(controller.mappingPresets[1]["applied"])
        self.assertTrue(run("apply", saved["id"], "")["ok"])
        controller._mapping_applied_overrides.clear()
        result = run("refresh", "", "")
        self.assertEqual(result["id"], saved["id"])

    def test_applied_marker_uses_effective_values_after_loader_normalization(self):
        controller, _ = self._make_controller()
        profile = device_catalog.RC003_ID
        mapping = presets.snapshot(controller._bindings, profile)
        mapping["bindings"]["ok"] = {"kind": "key_combo", "keys": ["enter"]}
        mapping["secondary_bindings"]["ok"] = {"double_click": {"kind": "disabled"}}
        mapping["display_notes"]["ok"] = {"single_click": "  确定  ", "long_press": ""}
        store = presets.PresetStore(controller._config_root)
        identity = store.create("可兼容配置", profile, mapping)
        result = controller.mappingPresetAction("apply", identity, "")
        self.assertTrue(result["ok"], result)
        self.assertEqual([row["id"] for row in controller.mappingPresets if row["applied"]], [identity])
        self.assertEqual(store.get(identity)["mapping"], mapping)
        controller._bindings["display_notes"]["ok"]["single_click"] = "已修改"
        result = controller.mappingPresetAction("refresh", "", "")
        self.assertEqual(result["id"], presets.AUTO_ID)

    def test_import_error_cannot_expand_the_modal_with_unbounded_file_content(self):
        from PySide6.QtCore import QUrl
        controller, _ = self._make_controller()
        value = {"format": presets.FORMAT, "schema_version": 1, "name": "bad",
                 "profile": device_catalog.RC003_ID,
                 "mapping": presets.snapshot(controller._bindings, device_catalog.RC003_ID)}
        value["mapping"]["bindings"]["ok"] = {"kind": "x" * 10000}
        source = Path(self._tmpdir.name) / "bad.json"
        source.write_text(json.dumps(value), encoding="utf-8")
        result = controller.mappingPresetAction("import", "", QUrl.fromLocalFile(str(source)).toString())
        self.assertFalse(result["ok"])
        self.assertLessEqual(len(result["message"]), 241)
        self.assertEqual(controller.mappingPresets, [])

    def test_real_legacy_load_keeps_list_and_successful_import_available(self):
        from PySide6.QtCore import QUrl
        controller, _ = self._make_controller()
        root = controller._config_root
        store = presets.PresetStore(root)
        identity = store.create("有效副本", device_catalog.RC003_ID,
                                presets.snapshot(controller._bindings, device_catalog.RC003_ID))
        export = Path(self._tmpdir.name) / "valid.json"
        store.export_file(identity, export)
        legacy = copy.deepcopy(controller._bindings)
        legacy["bindings"]["mic"] = {"kind": "voice_toggle", "keys": []}
        config.save_settings_pair(config.config_path(root), controller._config,
                                  config.key_bindings_path(root), legacy)
        reloaded, _ = self._make_controller()
        before = config.key_bindings_path(root).read_bytes()
        self.assertIn("mic", reloaded._removed_voice_bindings)
        run = reloaded.mappingPresetAction
        opened = run("refresh", "", "")
        self.assertTrue(opened["ok"], opened)
        self.assertEqual(len(reloaded.mappingPresets), 2)
        imported = run("import", "", QUrl.fromLocalFile(str(export)).toString())
        self.assertTrue(imported["ok"], imported)
        self.assertIn("配置已导入", imported["message"])
        self.assertEqual(len(store.load()), 2)
        self.assertEqual(len(reloaded.mappingPresets), 3)
        self.assertTrue(run("copy", identity, "命名副本")["ok"])
        self.assertTrue(run("rename", imported["id"], "导入后改名")["ok"])
        self.assertTrue(run("export", imported["id"], QUrl.fromLocalFile(str(export)).toString())["ok"])
        for operation in ("copy", "export"):
            self.assertFalse(run(operation, presets.AUTO_ID, "非法副本")["ok"])
        self.assertTrue(run("delete", identity, "")["ok"])
        reopened, _ = self._make_controller()
        self.assertTrue(reopened.mappingPresetAction("refresh", "", "")["ok"])
        self.assertEqual(len(reopened.mappingPresets), 3)
        self.assertEqual(config.key_bindings_path(root).read_bytes(), before)

    def test_committed_import_is_successful_when_only_the_list_refresh_fails(self):
        from PySide6.QtCore import QUrl
        controller, _ = self._make_controller()
        store = presets.PresetStore(controller._config_root)
        identity = store.create("备份", device_catalog.RC003_ID,
                                presets.snapshot(controller._bindings, device_catalog.RC003_ID))
        export = Path(self._tmpdir.name) / "valid.json"
        store.export_file(identity, export)
        with mock.patch.object(type(controller), "_refresh_mapping_presets", side_effect=OSError("locked")):
            result = controller.mappingPresetAction("import", "", QUrl.fromLocalFile(str(export)).toString())
            failed_refresh = controller.mappingPresetAction("refresh", "", "")
        self.assertTrue(result["ok"], result)
        self.assertIn("配置已导入", result["message"])
        self.assertIn("列表未能刷新", result["message"])
        self.assertFalse(failed_refresh["ok"])
        self.assertEqual(len(store.load()), 2)
        self.assertEqual(store.get(result["id"])["name"], "备份 (2)")
        self.assertTrue(controller.mappingPresetAction("refresh", "", "")["ok"])
        self.assertEqual(len(controller.mappingPresets), 3)

    def test_failed_import_write_preserves_library_and_reports_failure(self):
        from PySide6.QtCore import QUrl
        controller, _ = self._make_controller()
        store = presets.PresetStore(controller._config_root)
        identity = store.create("备份", device_catalog.RC003_ID,
                                presets.snapshot(controller._bindings, device_catalog.RC003_ID))
        export = Path(self._tmpdir.name) / "valid.json"
        store.export_file(identity, export)
        controller.mappingPresetAction("refresh", "", "")
        before, rows = store.path.read_bytes(), copy.deepcopy(controller.mappingPresets)
        with mock.patch.object(config.os, "replace", side_effect=PermissionError("locked")):
            result = controller.mappingPresetAction("import", "", QUrl.fromLocalFile(str(export)).toString())
        self.assertFalse(result["ok"])
        self.assertEqual(store.path.read_bytes(), before)
        self.assertEqual(controller.mappingPresets, rows)
