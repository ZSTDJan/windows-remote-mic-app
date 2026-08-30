import json
import subprocess
import tempfile
import unittest
from pathlib import Path


RC003_ROOT = Path(__file__).resolve().parents[1]
WINDOWS_ROOT = RC003_ROOT.parent
TEMPLATE_ROOT = WINDOWS_ROOT / "orthofocus"
EXPORT_SCRIPT = TEMPLATE_ROOT / "tools" / "export-source.ps1"


class ElementNavigationExportTests(unittest.TestCase):
    def test_template_has_an_independent_entry_and_exact_runtime_dependencies(self):
        pyproject = (TEMPLATE_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        requirements = (TEMPLATE_ROOT / "requirements.txt").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            'orthofocus = "element_navigation_prototype:main"',
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
        self.assertIn('license = {text = "GPL-3.0-only"}', pyproject)
        self.assertIn("https://github.com/ZSTDJan/orthofocus", pyproject)

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
            destination = Path(temporary) / "OrthoFocus"
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
            self.assertTrue((destination / "LICENSE").is_file())
            self.assertIn(
                "GNU GENERAL PUBLIC LICENSE",
                (destination / "LICENSE").read_text(encoding="utf-8"),
            )
            self.assertTrue((destination / "COPYRIGHT.md").is_file())
            self.assertTrue(
                (destination / "docs" / "screenshots" / "directional-navigation.png").is_file()
            )
            self.assertTrue(
                (destination / "docs" / "screenshots" / "orthogonal-territory-grid.png").is_file()
            )


if __name__ == "__main__":
    unittest.main()
