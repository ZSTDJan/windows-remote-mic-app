import json
import ctypes
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


RC003_ROOT = Path(__file__).resolve().parents[1]
WINDOWS_ROOT = RC003_ROOT.parent
TEMPLATE_ROOT = WINDOWS_ROOT / "orthofocus"
EXPORT_SCRIPT = TEMPLATE_ROOT / "tools" / "export-source.ps1"


class ElementNavigationExportTests(unittest.TestCase):
    def run_export(self, script, destination, *, force=False):
        command = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                   "-File", str(script), "-Destination", str(destination)]
        if force:
            command.append("-Force")
        return subprocess.run(command, capture_output=True, text=True, timeout=30)

    def isolated_repository(self, root):
        repository = root / "Source repository"
        template = repository / "apps" / "windows" / "orthofocus"
        (template / "tools").mkdir(parents=True)
        for directory in ("scripts", "tests"):
            (repository / "apps" / "windows" / "rc003" / directory).mkdir(parents=True)
        script = template / "tools" / EXPORT_SCRIPT.name
        shutil.copyfile(EXPORT_SCRIPT, script)
        sentinel = repository / "keep-source.txt"
        sentinel.write_text("source must remain", encoding="utf-8")
        return repository, script, sentinel

    def test_force_rejects_repository_ancestors_and_descendants_before_deleting(self):
        for case in ("repository", "ancestor", "existing_child", "missing_child", "root"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                repository, script, sentinel = self.isolated_repository(root)
                destinations = {
                    "repository": repository,
                    "ancestor": root,
                    "existing_child": repository / "apps",
                    "missing_child": repository / "new" / "export",
                    "root": Path(root.anchor),
                }
                destination = destinations[case]
                # A missing regression guard must never be allowed to test a
                # real drive root. Root rejection is exercised without Force.
                result = self.run_export(script, destination, force=case != "root")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("outside the repository", result.stderr)
                self.assertEqual(sentinel.read_text(encoding="utf-8"), "source must remain")
                self.assertTrue(script.exists())
                if case == "missing_child":
                    self.assertFalse(destination.exists())

    def test_force_rejects_junction_destination_parent_and_nested_link(self):
        for case in ("destination", "parent", "nested"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                repository, script, sentinel = self.isolated_repository(root)
                export = root / "export"
                if case == "nested":
                    export.mkdir()
                    link = export / "source-link"
                    destination = export
                else:
                    link = root / "source-link"
                    destination = link if case == "destination" else link / "new-export"
                subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(repository)],
                               check=True, capture_output=True)
                try:
                    result = self.run_export(script, destination, force=True)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("links", result.stderr)
                    self.assertEqual(sentinel.read_text(encoding="utf-8"), "source must remain")
                finally:
                    if link.exists():
                        os.rmdir(link)  # Remove only the junction, never its target.

    def test_force_rejects_short_name_alias_of_the_repository(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository, script, sentinel = self.isolated_repository(Path(temporary))
            get_short_path = ctypes.WinDLL("kernel32", use_last_error=True).GetShortPathNameW
            get_short_path.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
            get_short_path.restype = ctypes.c_uint32
            buffer = ctypes.create_unicode_buffer(32768)
            self.assertGreater(get_short_path(str(repository), buffer, len(buffer)), 0)
            if buffer.value.casefold() == str(repository).casefold():
                self.skipTest("This volume does not generate DOS short names")
            result = self.run_export(script, buffer.value, force=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("outside the repository", result.stderr)
            self.assertTrue(sentinel.exists())

    def test_force_preserves_a_source_directory_reached_through_a_junction(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository, script, sentinel = self.isolated_repository(root)
            external = root / "External source"
            external.mkdir()
            external_sentinel = external / "keep-source.txt"
            external_sentinel.write_text("external source", encoding="utf-8")
            source_link = repository / "apps" / "windows" / "rc003" / "scripts"
            os.rmdir(source_link)  # Fixture creates this as an empty directory.
            subprocess.run(["cmd", "/c", "mklink", "/J", str(source_link), str(external)],
                           check=True, capture_output=True)
            try:
                result = self.run_export(script, external, force=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("outside the repository", result.stderr)
                self.assertEqual(external_sentinel.read_text(encoding="utf-8"), "external source")
                self.assertTrue(sentinel.exists())
            finally:
                if source_link.exists():
                    os.rmdir(source_link)

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
            marker = destination / "obsolete.txt"
            marker.write_text("old export", encoding="utf-8")
            refused = self.run_export(EXPORT_SCRIPT, destination)
            self.assertNotEqual(refused.returncode, 0)
            self.assertTrue(marker.exists())
            replaced = self.run_export(EXPORT_SCRIPT, destination, force=True)
            self.assertEqual(replaced.returncode, 0, replaced.stderr)
            self.assertFalse(marker.exists())
            self.assertTrue((destination / "SOURCE-SNAPSHOT.json").is_file())


if __name__ == "__main__":
    unittest.main()
