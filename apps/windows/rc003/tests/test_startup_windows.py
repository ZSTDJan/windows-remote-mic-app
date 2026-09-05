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

    def __init__(self, value=None, *, value_type=None, set_error=None):
        self.value = value
        self.value_type = self.REG_SZ if value_type is None else value_type
        self.set_error = set_error
        self.set_calls = 0

    def OpenKey(self, root, path, reserved, access):
        if self.value is None:
            raise FileNotFoundError(path)
        return _Key()

    def CreateKeyEx(self, root, path, reserved, access):
        return _Key()

    def QueryValueEx(self, key, name):
        if self.value is None:
            raise FileNotFoundError(name)
        return self.value, self.value_type

    def SetValueEx(self, key, name, reserved, value_type, value):
        self.set_calls += 1
        if self.set_error is not None:
            raise self.set_error
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

    def test_rebind_owned_frozen_startup_moves_old_portable_command_to_current_path(self):
        old_command = startup_windows.command_line(
            [r"D:\旧版 无线麦\RemoteMicRC003.exe", "--background"]
        )
        current_command = startup_windows.command_line(
            [r"E:\新版 无线麦\RemoteMicRC003.exe", "--background"]
        )
        registry = _FakeWinreg(old_command)

        state = startup_windows.rebind_owned_frozen_startup(
            platform="win32",
            frozen=True,
            executable=r"E:\新版 无线麦\RemoteMicRC003.exe",
            current_command=current_command,
            winreg_module=registry,
            command_parser=lambda _value: [
                r"D:\旧版 无线麦\RemoteMicRC003.exe",
                "--background",
            ],
        )

        self.assertTrue(state.enabled)
        self.assertEqual(state.error, "")
        self.assertEqual(registry.value, current_command)
        self.assertEqual(registry.set_calls, 1)

    def test_rebind_owned_frozen_startup_works_when_old_exe_is_missing(self):
        old_command = startup_windows.command_line(
            [r"D:\已删除\RemoteMicRC003.exe", "--background"]
        )
        registry = _FakeWinreg(old_command)

        state = startup_windows.rebind_owned_frozen_startup(
            platform="win32",
            frozen=True,
            executable=r"E:\当前\RemoteMicRC003.exe",
            winreg_module=registry,
            command_parser=lambda _value: [
                r"D:\已删除\RemoteMicRC003.exe",
                "--background",
            ],
        )

        self.assertTrue(state.enabled)
        self.assertEqual(
            registry.value,
            startup_windows.command_line(
                [r"E:\当前\RemoteMicRC003.exe", "--background"]
            ),
        )

    def test_rebind_owned_frozen_startup_same_command_does_not_write(self):
        current_command = startup_windows.command_line(
            [r"E:\当前\RemoteMicRC003.exe", "--background"]
        )
        registry = _FakeWinreg(current_command)

        state = startup_windows.rebind_owned_frozen_startup(
            platform="win32",
            frozen=True,
            executable=r"E:\当前\RemoteMicRC003.exe",
            current_command=current_command,
            winreg_module=registry,
            command_parser=lambda _value: self.fail("parser should not run"),
        )

        self.assertTrue(state.enabled)
        self.assertEqual(registry.set_calls, 0)

    def test_rebind_owned_frozen_startup_rejects_unknown_or_extra_arguments(self):
        current_executable = r"E:\当前\RemoteMicRC003.exe"
        cases = (
            ([r"D:\旧版\RemoteMicRC003.exe", "--settings"],),
            ([r"D:\旧版\RemoteMicRC003.exe", "--background", "--bridge"],),
            ([r"D:\旧版\Other.exe", "--background"],),
            ([r"python.exe", r"D:\launcher.py", "--background"],),
        )
        for (arguments,) in cases:
            with self.subTest(arguments=arguments):
                old_command = startup_windows.command_line(arguments)
                registry = _FakeWinreg(old_command)
                state = startup_windows.rebind_owned_frozen_startup(
                    platform="win32",
                    frozen=True,
                    executable=current_executable,
                    winreg_module=registry,
                    command_parser=lambda _value, arguments=arguments: arguments,
                )
                self.assertFalse(state.enabled)
                self.assertEqual(state.error, "")
                self.assertEqual(registry.value, old_command)
                self.assertEqual(registry.set_calls, 0)

    def test_rebind_owned_frozen_startup_rejects_non_string_registry_value(self):
        registry = _FakeWinreg(
            [r"D:\旧版\RemoteMicRC003.exe", "--background"]
        )

        state = startup_windows.rebind_owned_frozen_startup(
            platform="win32",
            frozen=True,
            executable=r"E:\当前\RemoteMicRC003.exe",
            winreg_module=registry,
            command_parser=lambda _value: self.fail("parser should not run"),
        )

        self.assertFalse(state.enabled)
        self.assertEqual(registry.set_calls, 0)

    def test_rebind_owned_frozen_startup_write_failure_preserves_old_value(self):
        old_command = startup_windows.command_line(
            [r"D:\旧版\RemoteMicRC003.exe", "--background"]
        )
        registry = _FakeWinreg(old_command, set_error=PermissionError("denied"))

        state = startup_windows.rebind_owned_frozen_startup(
            platform="win32",
            frozen=True,
            executable=r"E:\当前\RemoteMicRC003.exe",
            winreg_module=registry,
            command_parser=lambda _value: [
                r"D:\旧版\RemoteMicRC003.exe",
                "--background",
            ],
        )

        self.assertFalse(state.enabled)
        self.assertEqual(state.error, "PermissionError")
        self.assertEqual(registry.value, old_command)
        self.assertEqual(registry.set_calls, 1)

    def test_rebind_owned_frozen_startup_ignores_non_string_registry_type(self):
        old_command = startup_windows.command_line(
            [r"D:\旧版\RemoteMicRC003.exe", "--background"]
        )
        registry = _FakeWinreg(old_command, value_type=7)

        state = startup_windows.rebind_owned_frozen_startup(
            platform="win32",
            frozen=True,
            executable=r"E:\当前\RemoteMicRC003.exe",
            winreg_module=registry,
            command_parser=lambda _value: self.fail("parser should not run"),
        )

        self.assertFalse(state.enabled)
        self.assertEqual(registry.value, old_command)
        self.assertEqual(registry.set_calls, 0)


if __name__ == "__main__":
    unittest.main()
