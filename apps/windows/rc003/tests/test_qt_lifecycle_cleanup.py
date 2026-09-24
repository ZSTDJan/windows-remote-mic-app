"""Exercise real Qt class creation and shutdown in a child interpreter.

The complete suite's output warning gate lives in build-candidate.ps1. This
regression exercises the cached PySide classes and teardown ordering without
recursively launching the entire suite a second time.
"""
import os
import subprocess
import sys
import unittest
from pathlib import Path

_RC003_ROOT = Path(__file__).resolve().parents[1]


class NoResourceWarningAtShutdownTests(unittest.TestCase):
    def test_qt_lifecycle_subprocess_has_no_resourcewarning_at_shutdown(self):
        try:
            import PySide6.QtCore
        except ImportError:
            self.skipTest("PySide6-Essentials not installed")
        src_root = (Path(os.environ["RC003_NATIVE_STAGE"]) / "src"
                    if os.environ.get("RC003_NATIVE_STAGE") else _RC003_ROOT / "src")
        env = dict(os.environ, PYTHONPATH=str(src_root),
                   QT_QPA_PLATFORM="offscreen", QML_DISABLE_DISK_CACHE="1",
                   RC003_DISABLE_LIVE_INPUT="1", RC003_ALLOW_LIVE_INPUT_TESTS="0")
        result = subprocess.run(
            [sys.executable, "-W", "default::ResourceWarning", "-m", "unittest",
             "tests.test_qt_settings_app.ButtonMappingModelTests",
             "tests.test_qt_settings_app.DiagnosticsShutdownOrderingTests"],
            cwd=_RC003_ROOT, env=env, capture_output=True, text=True,
            encoding="utf-8", timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("ResourceWarning:", result.stdout)
        self.assertNotIn("ResourceWarning:", result.stderr)


if __name__ == "__main__":
    unittest.main()
