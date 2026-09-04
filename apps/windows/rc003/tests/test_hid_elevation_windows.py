import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ovb_rc003 import hid_elevation_windows


SID = "S-1-5-21-111-222-333-1001"


class TaskDefinitionTests(unittest.TestCase):
    def setUp(self):
        self.helper = Path(r"C:\Program Files\RemoteMic\RC003\HidHelper") / (
            hid_elevation_windows.HELPER_EXE_NAME
        )
        self.xml = hid_elevation_windows.task_definition_xml(self.helper, SID)

    def test_task_is_on_demand_highest_and_has_one_fixed_action(self):
        self.assertTrue(
            hid_elevation_windows.validate_registered_task_xml(
                self.xml,
                helper_path=self.helper,
                user_sid=SID,
            )
        )
        self.assertNotIn("<Triggers", self.xml)
        self.assertEqual(self.xml.count("<Exec>"), 1)
        self.assertIn("<LogonType>InteractiveToken</LogonType>", self.xml)
        self.assertIn("<RunLevel>HighestAvailable</RunLevel>", self.xml)
        self.assertIn("<AllowStartOnDemand>true</AllowStartOnDemand>", self.xml)
        self.assertIn("<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>", self.xml)
        self.assertIn(f"<Arguments>{hid_elevation_windows.INJECT_FLAG}</Arguments>", self.xml)
        self.assertNotIn("--pid", self.xml)

    def test_task_validation_rejects_dynamic_arguments_or_a_trigger(self):
        dynamic = self.xml.replace(
            f"<Arguments>{hid_elevation_windows.INJECT_FLAG}</Arguments>",
            "<Arguments>--inject --pid 2468</Arguments>",
        )
        triggered = self.xml.replace(
            "  <Principals>",
            "  <Triggers><LogonTrigger /></Triggers>\n  <Principals>",
        )
        self.assertFalse(
            hid_elevation_windows.validate_registered_task_xml(
                dynamic, helper_path=self.helper, user_sid=SID
            )
        )
        self.assertFalse(
            hid_elevation_windows.validate_registered_task_xml(
                triggered, helper_path=self.helper, user_sid=SID
            )
        )

    def test_utf16_task_query_output_is_decoded_without_localized_text(self):
        completed = subprocess.CompletedProcess(
            ["schtasks.exe"], 0, stdout=self.xml.encode("utf-16")
        )
        with mock.patch.object(
            hid_elevation_windows,
            "_run_schtasks",
            return_value=completed,
        ):
            self.assertEqual(hid_elevation_windows._query_task_xml(), self.xml)


class InstalledHelperTests(unittest.TestCase):
    def test_distribution_detection_requires_frozen_executable_and_uninstaller(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            executable = root / "RemoteMicRC003.exe"
            executable.write_bytes(b"app")

            self.assertFalse(
                hid_elevation_windows.is_installed_distribution(
                    frozen=False,
                    executable=str(executable),
                )
            )
            self.assertFalse(
                hid_elevation_windows.is_installed_distribution(
                    frozen=True,
                    executable=str(executable),
                )
            )

            (root / "unins000.exe").write_bytes(b"uninstaller")

            self.assertTrue(
                hid_elevation_windows.is_installed_distribution(
                    frozen=True,
                    executable=str(executable),
                )
            )

    def test_matching_helper_and_task_are_available(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            app = root / "app"
            protected = root / "protected" / hid_elevation_windows.HELPER_EXE_NAME
            app.mkdir()
            protected.parent.mkdir()
            bundled = app / hid_elevation_windows.HELPER_BUNDLE_RELATIVE_PATH
            bundled.parent.mkdir()
            bundled.write_bytes(b"same-helper")
            protected.write_bytes(b"same-helper")
            xml = hid_elevation_windows.task_definition_xml(protected, SID)

            def fake_run(command, **_kwargs):
                self.assertEqual(command[1:3], ["/Query", "/TN"])
                return subprocess.CompletedProcess(command, 0, stdout=xml.encode("utf-16"))

            state = hid_elevation_windows.inspect_installed_helper(
                frozen=True,
                executable=str(app / "RemoteMicRC003.exe"),
                _run=fake_run,
                user_sid=SID,
                protected_path=protected,
            )

        self.assertEqual(state, hid_elevation_windows.HidHelperState(True))

    def test_hash_mismatch_fails_before_task_execution(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            app = root / "app"
            protected = root / "protected" / hid_elevation_windows.HELPER_EXE_NAME
            app.mkdir()
            protected.parent.mkdir()
            bundled = app / hid_elevation_windows.HELPER_BUNDLE_RELATIVE_PATH
            bundled.parent.mkdir()
            bundled.write_bytes(b"new")
            protected.write_bytes(b"old")
            runner = mock.Mock()

            state = hid_elevation_windows.inspect_installed_helper(
                frozen=True,
                executable=str(app / "RemoteMicRC003.exe"),
                _run=runner,
                user_sid=SID,
                protected_path=protected,
            )

        self.assertEqual(state.detail, "protected_helper_version_mismatch")
        runner.assert_not_called()

    def test_source_runtime_never_offers_task_installation(self):
        state = hid_elevation_windows.inspect_installed_helper(frozen=False)
        self.assertEqual(state, hid_elevation_windows.HidHelperState(False, "source_runtime"))

    def test_frozen_distribution_keeps_bundled_helper_under_internal(self):
        with tempfile.TemporaryDirectory() as raw:
            executable = Path(raw) / "RemoteMicRC003.exe"
            self.assertEqual(
                hid_elevation_windows.bundled_helper_path(
                    frozen=True,
                    executable=str(executable),
                ),
                Path(raw) / "_internal" / hid_elevation_windows.HELPER_EXE_NAME,
            )


class TaskLifecycleTests(unittest.TestCase):
    def test_install_copies_helper_registers_and_rechecks_exact_xml(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "bundle" / hid_elevation_windows.HELPER_EXE_NAME
            target = root / "protected" / hid_elevation_windows.HELPER_EXE_NAME
            source.parent.mkdir()
            source.write_bytes(b"helper-binary")
            registered_xml = ""

            def fake_run(command, **_kwargs):
                nonlocal registered_xml
                if "/Create" in command:
                    xml_path = Path(command[command.index("/XML") + 1])
                    registered_xml = xml_path.read_text(encoding="utf-16")
                    return subprocess.CompletedProcess(command, 0, stdout=b"")
                return subprocess.CompletedProcess(
                    command, 0, stdout=registered_xml.encode("utf-16")
                )

            with mock.patch.object(
                hid_elevation_windows, "is_process_elevated", return_value=True
            ):
                hid_elevation_windows.install_task(
                    source_executable=source,
                    target_path=target,
                    user_sid=SID,
                    _run=fake_run,
                )

            self.assertEqual(target.read_bytes(), source.read_bytes())
            self.assertTrue(
                hid_elevation_windows.validate_registered_task_xml(
                    registered_xml, helper_path=target, user_sid=SID
                )
            )
            self.assertEqual(list(target.parent.glob(".hid-task-*.xml")), [])

    def test_uninstall_is_idempotent_when_task_and_helper_are_absent(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / "protected" / hid_elevation_windows.HELPER_EXE_NAME
            task_file = root / "Tasks" / "HidTapInjector"
            runner = mock.Mock(
                return_value=subprocess.CompletedProcess(["schtasks.exe"], 1, stdout=b"")
            )
            with mock.patch.object(
                hid_elevation_windows, "is_process_elevated", return_value=True
            ), mock.patch.object(
                hid_elevation_windows, "task_file_path", return_value=task_file
            ):
                hid_elevation_windows.uninstall_task(
                    target_path=target,
                    _run=runner,
                )

    def test_uninstall_refuses_to_delete_helper_when_task_removal_fails(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / "protected" / hid_elevation_windows.HELPER_EXE_NAME
            task_file = root / "Tasks" / "HidTapInjector"
            target.parent.mkdir()
            task_file.parent.mkdir()
            target.write_bytes(b"helper")
            task_file.write_bytes(b"task")
            runner = mock.Mock(
                return_value=subprocess.CompletedProcess(["schtasks.exe"], 1, stdout=b"")
            )
            with mock.patch.object(
                hid_elevation_windows, "is_process_elevated", return_value=True
            ), mock.patch.object(
                hid_elevation_windows, "task_file_path", return_value=task_file
            ):
                with self.assertRaises(hid_elevation_windows.HidElevationError):
                    hid_elevation_windows.uninstall_task(
                        target_path=target,
                        _run=runner,
                    )

            self.assertTrue(target.is_file())

    def test_registered_injector_rejects_changed_host_before_running_task(self):
        runner = mock.Mock()
        with self.assertRaises(hid_elevation_windows.HidElevationError) as ctx:
            hid_elevation_windows.run_registered_injector(
                2468,
                _run=runner,
                _host_pid=lambda: 9999,
            )
        self.assertEqual(str(ctx.exception), "hid_helper_host_changed")
        runner.assert_not_called()

    def test_registered_injector_runs_only_the_fixed_task(self):
        runner = mock.Mock(
            return_value=subprocess.CompletedProcess(["schtasks.exe"], 0, stdout=b"")
        )
        with mock.patch.object(
            hid_elevation_windows,
            "inspect_installed_helper",
            return_value=hid_elevation_windows.HidHelperState(True),
        ):
            hid_elevation_windows.run_registered_injector(
                2468,
                _run=runner,
                _host_pid=lambda: 2468,
            )
        command = runner.call_args.args[0]
        self.assertEqual(
            command,
            ["schtasks.exe", "/Run", "/TN", hid_elevation_windows.TASK_NAME],
        )
        self.assertNotIn("2468", command)


class ElevationRequestTests(unittest.TestCase):
    def test_uac_cancellation_is_reported_without_claiming_success(self):
        with tempfile.TemporaryDirectory() as raw:
            helper = Path(raw) / hid_elevation_windows.HELPER_EXE_NAME
            helper.write_bytes(b"helper")

            def cancel(_path, _args, _timeout):
                raise hid_elevation_windows.HidElevationError("uac_cancelled")

            state = hid_elevation_windows.request_install_elevation(
                helper_path=helper,
                _launch=cancel,
            )

        self.assertEqual(state, hid_elevation_windows.HidHelperState(False, "uac_cancelled"))

    def test_success_is_rechecked_after_the_elevated_process_exits(self):
        with tempfile.TemporaryDirectory() as raw:
            helper = Path(raw) / hid_elevation_windows.HELPER_EXE_NAME
            helper.write_bytes(b"helper")
            inspected = hid_elevation_windows.HidHelperState(True)
            launch = mock.Mock(return_value=hid_elevation_windows.HELPER_EXIT_OK)
            inspect = mock.Mock(return_value=inspected)

            state = hid_elevation_windows.request_install_elevation(
                helper_path=helper,
                timeout_seconds=12.0,
                _launch=launch,
                _inspect=inspect,
            )

        self.assertEqual(state, inspected)
        launch.assert_called_once_with(
            helper, hid_elevation_windows.INSTALL_FLAG, 12.0
        )
        inspect.assert_called_once_with()


class HelperEntryPointTests(unittest.TestCase):
    def test_inject_mode_resolves_target_internally(self):
        with mock.patch.object(hid_elevation_windows, "_inject_once") as inject:
            self.assertEqual(
                hid_elevation_windows.helper_main([hid_elevation_windows.INJECT_FLAG]),
                hid_elevation_windows.HELPER_EXIT_OK,
            )
        inject.assert_called_once_with()

    def test_inject_mode_rejects_pid_or_executable_arguments(self):
        with self.assertRaises(SystemExit):
            hid_elevation_windows.helper_main(
                [hid_elevation_windows.INJECT_FLAG, "--pid", "2468"]
            )


if __name__ == "__main__":
    unittest.main()
