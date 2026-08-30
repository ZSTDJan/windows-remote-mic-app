import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from ovb_rc003 import qt_settings_app


_RC003_ROOT = Path(__file__).resolve().parents[1]
_ICON_DIR = _RC003_ROOT / "src" / "ovb_rc003" / "assets" / "icons"
_GENERATOR = _RC003_ROOT / "build" / "generate-app-icon.py"


class ApplicationIconTests(unittest.TestCase):
    def test_qt_icon_is_applied_to_the_application_and_native_window(self):
        calls = []

        class FakeIcon:
            def __init__(self, path):
                self.path = path

        class FakeApplication:
            def setWindowIcon(self, icon):
                calls.append(("application", icon.path))

        class FakeWindow:
            def setIcon(self, icon):
                calls.append(("window", icon.path))

        icon_path = _ICON_DIR / "remote-mic-connected.svg"
        qt_settings_app._apply_application_icon(
            FakeApplication(), FakeWindow(), FakeIcon, icon_path
        )

        self.assertEqual(
            calls,
            [
                ("application", str(icon_path)),
                ("window", str(icon_path)),
            ],
        )

    def test_windows_ico_is_generated_from_the_shared_connected_svg(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            generated = Path(tmpdir) / "remote-mic.ico"
            result = subprocess.run(
                [
                    sys.executable,
                    str(_GENERATOR),
                    "--source",
                    str(_ICON_DIR / "remote-mic-connected.svg"),
                    "--output",
                    str(generated),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(
                generated.read_bytes(),
                (_ICON_DIR / "remote-mic.ico").read_bytes(),
            )

            content = generated.read_bytes()
            _reserved, image_type, image_count = struct.unpack_from("<HHH", content)
            self.assertEqual(image_type, 1)
            self.assertEqual(image_count, 9)

    def test_builds_regenerate_the_shared_windows_icon(self):
        local_build = (_RC003_ROOT / "build" / "build-candidate.ps1").read_text(
            encoding="utf-8"
        )
        ci = (
            _RC003_ROOT.parents[2]
            / ".github"
            / "workflows"
            / "windows-rc003-ci.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("generate-app-icon.py", local_build)
        self.assertIn("python build/generate-app-icon.py", ci)

    def test_development_shortcut_uses_the_shared_ico_directly(self):
        script = (_RC003_ROOT / "build" / "install-dev-shortcut.ps1").read_text(
            encoding="utf-8"
        )

        self.assertIn("src\\ovb_rc003\\assets\\icons\\remote-mic.ico", script)
        self.assertNotIn("dist\\RemoteMicRC003\\RemoteMicRC003.exe", script)


if __name__ == "__main__":
    unittest.main()
