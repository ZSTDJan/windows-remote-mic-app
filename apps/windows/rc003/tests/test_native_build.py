"""Build-only inventory and fail-closed receipt contracts; no compiler required."""
import hashlib
import importlib.machinery
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

BUILD = Path(__file__).resolve().parents[1] / "build"
with mock.patch.object(sys, "path", [str(BUILD), *sys.path]):
    import native_inventory as inventory
    import check_native


class NativeInventoryTests(unittest.TestCase):
    def test_real_inventory_covers_all_business_sources_and_navigation(self):
        root = BUILD.parent
        sources = inventory.module_sources(root)
        self.assertEqual(set(sources.values()),
                         set((root / "src/ovb_rc003").glob("*.py"))
                         - {root / "src/ovb_rc003/__init__.py"}
                         | {root / "scripts" / f"{n}.py" for n in inventory.NAVIGATION_MODULES})
        self.assertEqual(sources["ovb_rc003._native_main"].name, "__main__.py")
        self.assertTrue(set(inventory.LEGACY_MODULES) <= set(sources))

    def test_helper_closure_excludes_desktop_and_preserves_its_own_dependencies(self):
        names = inventory.import_closure(
            BUILD.parent, ["ovb_rc003.hid_elevation_windows"],
            excludes=("ovb_rc003.app", "ovb_rc003.qt_settings_app", "PySide6"))
        self.assertIn("ovb_rc003.frida_hid_tap_injector", names)
        self.assertIn("ctypes", names)
        self.assertNotIn("ovb_rc003.qt_settings_app", names)


class NativeReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.source = root / "source.py"
        self.source.write_text("VALUE = 1\n", encoding="utf-8")
        self.stage = root / "stage"
        package = self.stage / "src/ovb_rc003"
        package.mkdir(parents=True)
        (package / "__main__.py").write_text(inventory.MAIN_BOOTSTRAP, encoding="utf-8")
        self.binary = package / ("unit" + importlib.machinery.EXTENSION_SUFFIXES[0])
        self.binary.write_bytes(b"test extension placeholder")
        self.receipt = {
            "scope": "full", "python": sys.version,
            "modules": {"ovb_rc003.unit": {
                "source_sha256": hashlib.sha256(self.source.read_bytes()).hexdigest(),
                "output": self.binary.relative_to(self.stage).as_posix(),
                "sha256": hashlib.sha256(self.binary.read_bytes()).hexdigest(),
            }},
        }
        self.patch = mock.patch.object(check_native, "module_sources",
                                      return_value={"ovb_rc003.unit": self.source})
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def verify(self):
        (self.stage / "native-build.json").write_text(json.dumps(self.receipt), encoding="utf-8")
        return check_native.verify_stage(self.stage)

    def test_unchanged_stage_is_accepted(self):
        self.assertEqual(self.verify(), self.receipt)

    def test_changed_source_is_rejected(self):
        self.source.write_text("VALUE = 2\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "source changed"):
            self.verify()

    def test_changed_binary_is_rejected(self):
        self.binary.write_bytes(b"changed")
        with self.assertRaisesRegex(RuntimeError, "binary changed"):
            self.verify()

    def test_missing_module_is_rejected(self):
        self.receipt["modules"].clear()
        with self.assertRaisesRegex(RuntimeError, "inventory"):
            self.verify()

    def test_legacy_stage_cannot_be_used_for_production(self):
        self.receipt["scope"] = "legacy"
        with self.assertRaisesRegex(RuntimeError, "inventory"):
            self.verify()

    def test_retained_python_business_module_is_rejected(self):
        self.binary.with_name("unit.py").write_text("VALUE = 1\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "retains Python"):
            self.verify()

    def test_changed_bootstrap_is_rejected(self):
        (self.stage / "src/ovb_rc003/__main__.py").write_text("pass\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "bootstrap"):
            self.verify()

    def test_path_outside_stage_is_rejected(self):
        self.receipt["modules"]["ovb_rc003.unit"]["output"] = "../source.py"
        with self.assertRaisesRegex(RuntimeError, "output path"):
            self.verify()
