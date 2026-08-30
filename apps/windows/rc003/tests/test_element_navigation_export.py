import json
import subprocess
import tempfile
import unittest
from pathlib import Path


RC003_ROOT = Path(__file__).resolve().parents[1]
WINDOWS_ROOT = RC003_ROOT.parent
TEMPLATE_ROOT = WINDOWS_ROOT / "element-navigation"
EXPORT_SCRIPT = TEMPLATE_ROOT / "tools" / "export-source.ps1"


class ElementNavigationExportTests(unittest.TestCase):
    def test_template_has_an_independent_entry_and_exact_runtime_dependencies(self):
        pyproject = (TEMPLATE_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        requirements = (TEMPLATE_ROOT / "requirements.txt").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            'element-navigation = "element_navigation_prototype:main"',
            pyproject,
        )
        for dependency in (
            "PySide6-Essentials==6.11.1",
            "uiautomation==2.0.29",
            "comtypes==1.4.16",
        ):
            self.assertIn(dependency, pyproject)
            self.assertIn(dependency, requirements)
        self.assertNotIn("../requirements.txt", requirements)

    def test_exported_source_has_no_remote_mic_runtime_import(self):
        source_names = (
            "element_navigation_command_windows.py",
            "element_navigation_prototype.py",
            "element_navigation_support.py",
            "element_navigation_windows_host.py",
            "element_targeting_core.py",
            "spatial_navigation_core.py",
        )
        for name in source_names:
            text = (RC003_ROOT / "scripts" / name).read_text(encoding="utf-8")
            with self.subTest(name=name):
                self.assertNotIn("ovb_rc003", text)
                self.assertNotIn("REMOTE_MIC_", text)

    def test_export_script_produces_a_self_contained_testable_tree(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "ElementNavigation"
            subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(EXPORT_SCRIPT),
                    "-Destination",
                    str(destination),
                ],
                check=True,
                cwd=TEMPLATE_ROOT,
                capture_output=True,
                text=True,
            )

            snapshot = json.loads(
                (destination / "SOURCE-SNAPSHOT.json").read_text(encoding="utf-8-sig")
            )
            self.assertEqual(len(snapshot["files"]), 6)
            self.assertTrue(snapshot["sourceCommit"])
            self.assertTrue((destination / "tests" / "test_element_navigation_prototype.py").is_file())
            self.assertFalse((destination / "LICENSE").exists())
            self.assertFalse((destination / "LICENSE.md").exists())


if __name__ == "__main__":
    unittest.main()
