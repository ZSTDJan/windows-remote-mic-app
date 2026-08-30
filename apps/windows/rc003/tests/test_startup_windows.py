import unittest

from ovb_rc003 import startup_windows


class _Key:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeWinreg:
    HKEY_CURRENT_USER = object()
    KEY_QUERY_VALUE = 1
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self, value=None):
        self.value = value

    def OpenKey(self, root, path, reserved, access):
        if self.value is None:
            raise FileNotFoundError(path)
        return _Key()

    def CreateKeyEx(self, root, path, reserved, access):
        return _Key()

    def QueryValueEx(self, key, name):
        if self.value is None:
            raise FileNotFoundError(name)
        return self.value, self.REG_SZ

    def SetValueEx(self, key, name, reserved, value_type, value):
        self.value = value

    def DeleteValue(self, key, name):
        if self.value is None:
            raise FileNotFoundError(name)
        self.value = None


class StartupWindowsTests(unittest.TestCase):
    def test_frozen_command_uses_background_shell(self):
        self.assertEqual(
            startup_windows.build_startup_command(
                frozen=True, executable=r"C:\Apps\Remote Mic\RemoteMicRC003.exe"
            ),
            [r"C:\Apps\Remote Mic\RemoteMicRC003.exe", "--background"],
        )

    def test_source_command_uses_standalone_launcher(self):
        self.assertEqual(
            startup_windows.build_startup_command(
                frozen=False,
                executable=r"C:\Python312\python.exe",
                source_launcher=r"D:\Remote Mic\src\launcher.py",
            ),
            [
                r"C:\Python312\python.exe",
                r"D:\Remote Mic\src\launcher.py",
                "--background",
            ],
        )

    def test_default_source_launcher_exists(self):
        command = startup_windows.build_startup_command(
            frozen=False, executable=r"C:\Python312\python.exe"
        )
        self.assertTrue(startup_windows.Path(command[1]).is_file())

    def test_read_requires_the_exact_owned_command(self):
        registry = _FakeWinreg('"C:\\Apps\\RemoteMicRC003.exe" --settings')
        state = startup_windows.read_startup_state(
            platform="win32",
            expected_command='"C:\\Apps\\RemoteMicRC003.exe" --background',
            winreg_module=registry,
        )
        self.assertFalse(state.enabled)
        self.assertEqual(state.error, "")

    def test_enable_and_disable_round_trip(self):
        registry = _FakeWinreg()
        command = '"C:\\Apps\\RemoteMicRC003.exe" --background'
        enabled = startup_windows.set_startup_enabled(
            True,
            platform="win32",
            startup_command=command,
            winreg_module=registry,
        )
        self.assertTrue(enabled.enabled)
        self.assertEqual(registry.value, command)
        disabled = startup_windows.set_startup_enabled(
            False,
            platform="win32",
            winreg_module=registry,
        )
        self.assertFalse(disabled.enabled)
        self.assertIsNone(registry.value)

    def test_non_windows_fails_without_touching_registry(self):
        state = startup_windows.set_startup_enabled(
            True, platform="linux", winreg_module=_FakeWinreg()
        )
        self.assertFalse(state.enabled)
        self.assertIn("Windows", state.error)


if __name__ == "__main__":
    unittest.main()
