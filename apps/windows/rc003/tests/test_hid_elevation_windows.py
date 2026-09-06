import ctypes
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from ovb_rc003 import hid_elevation_windows


SID = "S-1-5-21-111-222-333-1001"


def _with_empty_triggers(xml_text: str) -> str:
    return xml_text.replace(
        "  <Principals>",
        "  <Triggers />\n  <Principals>",
        1,
    )


class _CallableWin32Function:
    def __init__(self, callback):
        self._callback = callback
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self._callback(*args)


class _Collection:
    def __init__(self, items):
        self._items = list(items)
        self.Count = len(self._items)

    def Item(self, index):
        return self._items[index - 1]


class ProcessElevationStateTests(unittest.TestCase):
    def test_strict_query_returns_the_verified_token_state(self):
        for raw_state, expected in ((0, False), (1, True)):
            elevation = hid_elevation_windows._TOKEN_ELEVATION(raw_state)
            with self.subTest(raw_state=raw_state), mock.patch.object(
                hid_elevation_windows, "_is_windows", return_value=True
            ), mock.patch.object(
                hid_elevation_windows,
                "_token_information",
                return_value=bytes(elevation),
            ):
                self.assertIs(
                    hid_elevation_windows.query_process_elevated(),
                    expected,
                )

    def test_strict_query_preserves_a_known_token_error(self):
        with mock.patch.object(
            hid_elevation_windows, "_is_windows", return_value=True
        ), mock.patch.object(
            hid_elevation_windows,
            "_token_information",
            side_effect=hid_elevation_windows.HidElevationError("token_failed"),
        ):
            with self.assertRaises(hid_elevation_windows.HidElevationError) as ctx:
                hid_elevation_windows.query_process_elevated()

        self.assertEqual(str(ctx.exception), "token_failed")

    def test_strict_query_normalizes_unexpected_token_read_failures(self):
        for failure in (OSError("read failed"), b""):
            with self.subTest(failure=failure), mock.patch.object(
                hid_elevation_windows, "_is_windows", return_value=True
            ), mock.patch.object(
                hid_elevation_windows,
                "_token_information",
                side_effect=failure if isinstance(failure, BaseException) else None,
                return_value=failure if isinstance(failure, bytes) else None,
            ):
                with self.assertRaises(
                    hid_elevation_windows.HidElevationError
                ) as ctx:
                    hid_elevation_windows.query_process_elevated()

            self.assertEqual(
                str(ctx.exception),
                "current_user_elevation_status_unavailable",
            )

    def test_legacy_boolean_query_still_falls_back_to_not_elevated(self):
        with mock.patch.object(
            hid_elevation_windows,
            "query_process_elevated",
            side_effect=hid_elevation_windows.HidElevationError("token_failed"),
        ):
            self.assertFalse(hid_elevation_windows.is_process_elevated())

    def test_self_elevation_is_available_for_elevated_or_split_admin_tokens(self):
        with mock.patch.object(
            hid_elevation_windows, "_is_windows", return_value=True
        ), mock.patch.object(
            hid_elevation_windows, "is_process_elevated", return_value=True
        ), mock.patch.object(
            hid_elevation_windows, "token_elevation_type"
        ) as token_type:
            self.assertTrue(hid_elevation_windows.can_current_user_self_elevate())
        token_type.assert_not_called()

        with mock.patch.object(
            hid_elevation_windows, "_is_windows", return_value=True
        ), mock.patch.object(
            hid_elevation_windows, "is_process_elevated", return_value=False
        ), mock.patch.object(
            hid_elevation_windows,
            "token_elevation_type",
            return_value=hid_elevation_windows._TOKEN_ELEVATION_TYPE_LIMITED,
        ):
            self.assertTrue(hid_elevation_windows.can_current_user_self_elevate())

    def test_self_elevation_is_unavailable_for_standard_or_unknown_tokens(self):
        for elevation_type in (1, 2):
            with self.subTest(elevation_type=elevation_type), mock.patch.object(
                hid_elevation_windows, "_is_windows", return_value=True
            ), mock.patch.object(
                hid_elevation_windows, "is_process_elevated", return_value=False
            ), mock.patch.object(
                hid_elevation_windows,
                "token_elevation_type",
                return_value=elevation_type,
            ):
                self.assertFalse(
                    hid_elevation_windows.can_current_user_self_elevate()
                )

        with mock.patch.object(
            hid_elevation_windows, "_is_windows", return_value=True
        ), mock.patch.object(
            hid_elevation_windows, "is_process_elevated", return_value=False
        ), mock.patch.object(
            hid_elevation_windows,
            "token_elevation_type",
            side_effect=hid_elevation_windows.HidElevationError("unavailable"),
        ):
            self.assertFalse(hid_elevation_windows.can_current_user_self_elevate())

        with mock.patch.object(
            hid_elevation_windows, "_is_windows", return_value=False
        ), mock.patch.object(
            hid_elevation_windows, "is_process_elevated"
        ) as elevated:
            self.assertFalse(hid_elevation_windows.can_current_user_self_elevate())
        elevated.assert_not_called()


class TaskDefinitionTests(unittest.TestCase):
    def setUp(self):
        self.helper = Path(r"C:\Program Files\RemoteMic\RC003\protected") / hid_elevation_windows.HELPER_EXE_NAME
        self.task_name = hid_elevation_windows.task_name_for_sid(SID)
        self.xml = hid_elevation_windows.task_definition_xml(
            self.helper, SID, task_name=self.task_name
        )

    def test_task_is_on_demand_highest_and_has_one_fixed_action(self):
        self.assertTrue(
            hid_elevation_windows.validate_registered_task_xml(
                self.xml,
                helper_path=self.helper,
                user_sid=SID,
                task_name=self.task_name,
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
        self.assertIn(f"<URI>{self.task_name}</URI>", self.xml)

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
                dynamic,
                helper_path=self.helper,
                user_sid=SID,
                task_name=self.task_name,
            )
        )
        self.assertFalse(
            hid_elevation_windows.validate_registered_task_xml(
                triggered,
                helper_path=self.helper,
                user_sid=SID,
                task_name=self.task_name,
            )
        )

    def test_task_validation_accepts_only_one_empty_trigger_container(self):
        variants = (
            _with_empty_triggers(self.xml),
            _with_empty_triggers(self.xml).replace(
                "<Triggers />", "<Triggers>  </Triggers>"
            ),
        )
        for xml_text in variants:
            with self.subTest(xml_text=xml_text):
                self.assertTrue(
                    hid_elevation_windows.validate_registered_task_xml(
                        xml_text,
                        helper_path=self.helper,
                        user_sid=SID,
                        task_name=self.task_name,
                    )
                )

    def test_task_validation_rejects_nonempty_or_misplaced_trigger_containers(self):
        empty = _with_empty_triggers(self.xml)
        invalid_variants = (
            empty.replace("<Triggers />", '<Triggers Enabled="false" />'),
            empty.replace("<Triggers />", "<Triggers>unexpected</Triggers>"),
            empty.replace("<Triggers />", "<Triggers><BootTrigger /></Triggers>"),
            empty.replace("<Triggers />", "<Triggers><EventTrigger /></Triggers>"),
            empty.replace("<Triggers />", "<Triggers />\n  <Triggers />"),
            self.xml.replace(
                "    <Exec>",
                "    <Triggers />\n    <Exec>",
                1,
            ),
        )
        for xml_text in invalid_variants:
            with self.subTest(xml_text=xml_text):
                self.assertFalse(
                    hid_elevation_windows.validate_registered_task_xml(
                        xml_text,
                        helper_path=self.helper,
                        user_sid=SID,
                        task_name=self.task_name,
                    )
                )

    @unittest.skipUnless(sys.platform == "win32", "Windows Task Scheduler only")
    def test_task_validation_accepts_windows_in_memory_normalization(self):
        import comtypes.client

        sid = hid_elevation_windows.current_user_sid()
        helper = Path(
            r"C:\Program Files\RemoteMic\RC003\probe\RemoteMicRC003HidHelper.exe"
        )
        task_name = hid_elevation_windows.task_name_for_sid(sid)
        service = comtypes.client.CreateObject("Schedule.Service", dynamic=True)
        definition = service.NewTask(0)
        definition.XmlText = hid_elevation_windows.task_definition_xml(
            helper,
            sid,
            task_name=task_name,
        )
        normalized = str(definition.XmlText)

        self.assertIn("<Triggers />", normalized)
        self.assertTrue(
            hid_elevation_windows.validate_registered_task_xml(
                normalized,
                helper_path=helper,
                user_sid=sid,
                task_name=task_name,
            )
        )

    def test_task_validation_accepts_real_windows_registered_normalization(self):
        normalized_settings = """  <Settings>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT30S</ExecutionTimeLimit>
    <Hidden>true</Hidden>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <IdleSettings>
      <StopOnIdleEnd>true</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <UseUnifiedSchedulingEngine>true</UseUnifiedSchedulingEngine>
  </Settings>"""
        start = self.xml.index("  <Settings>")
        end = self.xml.index("  </Settings>", start) + len("  </Settings>")
        registered = self.xml[:start] + normalized_settings + self.xml[end:]
        registered = _with_empty_triggers(registered)
        registered = registered.replace(
            "  <RegistrationInfo>",
            "  <RegistrationInfo>\n"
            f"    <SecurityDescriptor>{hid_elevation_windows.task_security_sddl(SID)}</SecurityDescriptor>",
            1,
        )

        self.assertTrue(
            hid_elevation_windows.validate_registered_task_xml(
                registered,
                helper_path=self.helper,
                user_sid=SID,
                task_name=self.task_name,
            )
        )

    def test_task_validation_rejects_missing_nondefault_registered_settings(self):
        required = (
            "MultipleInstancesPolicy",
            "DisallowStartIfOnBatteries",
            "StopIfGoingOnBatteries",
            "Hidden",
            "ExecutionTimeLimit",
        )
        for name in required:
            with self.subTest(setting=name):
                start = self.xml.index(f"    <{name}>")
                end = self.xml.index(f"</{name}>", start) + len(f"</{name}>")
                without_setting = self.xml[:start] + self.xml[end:]
                self.assertFalse(
                    hid_elevation_windows.validate_registered_task_xml(
                        without_setting,
                        helper_path=self.helper,
                        user_sid=SID,
                        task_name=self.task_name,
                    )
                )

    def test_task_validation_rejects_extra_actions_and_restart_policy(self):
        extra_exec = self.xml.replace(
            "  </Actions>",
            "    <Exec><Command>C:\\bad.exe</Command></Exec>\n  </Actions>",
        )
        com_handler = self.xml.replace(
            "  </Actions>",
            "    <ComHandler><ClassId>{00000000-0000-0000-0000-000000000000}</ClassId></ComHandler>\n  </Actions>",
        )
        second_actions = self.xml.replace(
            "</Task>",
            "<Actions Context=\"Author\"><Exec><Command>C:\\bad.exe</Command></Exec></Actions></Task>",
        )
        restart = self.xml.replace(
            "    <Priority>7</Priority>",
            "    <Priority>7</Priority><RestartOnFailure><Interval>PT1M</Interval><Count>3</Count></RestartOnFailure>",
        )
        maintenance = self.xml.replace(
            "  </Settings>",
            "    <MaintenanceSettings>\n"
            "      <Period>P1D</Period>\n"
            "      <Deadline>P7D</Deadline>\n"
            "      <Exclusive>false</Exclusive>\n"
            "    </MaintenanceSettings>\n"
            "  </Settings>",
            1,
        )
        for invalid in (
            extra_exec,
            com_handler,
            second_actions,
            restart,
            maintenance,
        ):
            self.assertFalse(
                hid_elevation_windows.validate_registered_task_xml(
                    invalid,
                    helper_path=self.helper,
                    user_sid=SID,
                    task_name=self.task_name,
                )
            )

    def test_task_validation_rejects_each_fixed_setting_when_changed(self):
        replacements = {
            "MultipleInstancesPolicy": "Parallel",
            "DisallowStartIfOnBatteries": "true",
            "StopIfGoingOnBatteries": "true",
            "AllowHardTerminate": "false",
            "StartWhenAvailable": "true",
            "RunOnlyIfNetworkAvailable": "true",
            "AllowStartOnDemand": "false",
            "Enabled": "false",
            "Hidden": "false",
            "RunOnlyIfIdle": "true",
            "WakeToRun": "true",
            "ExecutionTimeLimit": "PT1H",
            "Priority": "1",
        }
        for name, changed in replacements.items():
            with self.subTest(setting=name):
                marker = f"<{name}>"
                start = self.xml.index(marker) + len(marker)
                end = self.xml.index(f"</{name}>", start)
                invalid = self.xml[:start] + changed + self.xml[end:]
                self.assertFalse(
                    hid_elevation_windows.validate_registered_task_xml(
                        invalid,
                        helper_path=self.helper,
                        user_sid=SID,
                        task_name=self.task_name,
                    )
                )

    def test_task_and_helper_names_are_isolated_by_sid(self):
        other_sid = "S-1-5-21-111-222-333-1002"
        self.assertNotEqual(
            hid_elevation_windows.task_name_for_sid(SID),
            hid_elevation_windows.task_name_for_sid(other_sid),
        )
        self.assertNotEqual(
            hid_elevation_windows.protected_owner_root(
                SID, program_files_root=Path(r"C:\Program Files")
            ),
            hid_elevation_windows.protected_owner_root(
                other_sid, program_files_root=Path(r"C:\Program Files")
            ),
        )
        self.assertNotEqual(self.task_name, hid_elevation_windows.LEGACY_TASK_NAME)

    def test_task_sddl_grants_only_admin_control_and_owner_run(self):
        sddl = hid_elevation_windows.task_security_sddl(SID)
        self.assertTrue(
            hid_elevation_windows.validate_task_security_sddl(
                sddl, user_sid=SID
            )
        )
        self.assertFalse(
            hid_elevation_windows.validate_task_security_sddl(
                sddl.replace(f"(A;;FRFX;;;{SID})", f"(A;;FA;;;{SID})"),
                user_sid=SID,
            )
        )
        self.assertFalse(
            hid_elevation_windows.validate_task_security_sddl(
                sddl + "(A;;FR;;;WD)", user_sid=SID
            )
        )
        self.assertFalse(
            hid_elevation_windows.validate_task_security_sddl(
                sddl.replace(f"(A;;FRFX;;;{SID})", f"(A;;FR;;;{SID})"),
                user_sid=SID,
            )
        )
        self.assertTrue(
            hid_elevation_windows.validate_task_security_sddl(
                sddl.replace(
                    f"(A;;FRFX;;;{SID})", f"(A;;0x1200a9;;;{SID})"
                ),
                user_sid=SID,
            )
        )

    def test_task_sddl_accepts_windows_inherited_admin_equivalent(self):
        normalized = (
            f"O:BAG:{SID}D:"
            "(A;ID;0x1f019f;;;BA)"
            "(A;ID;0x1f019f;;;SY)"
            "(A;ID;FA;;;BA)"
            f"(A;;FRFX;;;{SID})"
        )

        self.assertTrue(
            hid_elevation_windows.validate_task_security_sddl(
                normalized, user_sid=SID
            )
        )

    def test_task_sddl_rejects_unsafe_windows_normalized_variants(self):
        normalized = (
            f"O:BAG:{SID}D:"
            "(A;ID;0x1f019f;;;BA)"
            "(A;ID;0x1f019f;;;SY)"
            "(A;ID;FA;;;BA)"
            f"(A;;FRFX;;;{SID})"
        )
        invalid_variants = (
            normalized + "(A;ID;FR;;;AU)",
            normalized.replace(f"(A;;FRFX;;;{SID})", f"(A;;FA;;;{SID})"),
            normalized.replace(f"(A;;FRFX;;;{SID})", f"(A;;FR;;;{SID})"),
            normalized.replace("(A;ID;0x1f019f;;;SY)", ""),
            normalized.replace(f"(A;;FRFX;;;{SID})", f"(A;OI;FRFX;;;{SID})"),
        )

        for candidate in invalid_variants:
            with self.subTest(candidate=candidate):
                self.assertFalse(
                    hid_elevation_windows.validate_task_security_sddl(
                        candidate, user_sid=SID
                    )
                )


class TaskSchedulerApiTests(unittest.TestCase):
    def test_missing_task_is_reported_by_collection_enumeration(self):
        tasks = mock.Mock()
        tasks.Count = 0
        root = mock.Mock()
        root.GetTasks.return_value = tasks

        self.assertIsNone(
            hid_elevation_windows._read_registered_task(
                r"\RemoteMicRC003-HidTap-missing",
                _root=root,
            )
        )
        root.GetTask.assert_not_called()

    def test_register_uses_dont_add_principal_ace(self):
        root = mock.Mock()
        registered = mock.Mock()
        registered.Xml = "registered-xml"
        registered.GetSecurityDescriptor.return_value = "registered-sddl"
        root.RegisterTask.return_value = registered

        snapshot = hid_elevation_windows._register_task(
            "task", "xml", "sddl", _root=root
        )

        flags = root.RegisterTask.call_args.args[2]
        self.assertEqual(
            flags,
            hid_elevation_windows.TASK_CREATE_OR_UPDATE
            | hid_elevation_windows.TASK_DONT_ADD_PRINCIPAL_ACE,
        )
        self.assertEqual(
            snapshot,
            hid_elevation_windows._RegisteredTaskSnapshot(
                "registered-xml", "registered-sddl"
            ),
        )

    def test_register_result_read_failure_falls_back_to_a_named_query(self):
        class UnreadableRegisteredTask:
            @property
            def Xml(self):
                raise OSError("registered task is not readable yet")

        root = mock.Mock()
        root.RegisterTask.return_value = UnreadableRegisteredTask()

        snapshot = hid_elevation_windows._register_task(
            "task", "xml", "sddl", _root=root
        )

        self.assertIsNone(snapshot)

    def test_post_registration_query_stops_after_the_bounded_wait(self):
        reads = mock.Mock(return_value=None)
        sleeps = []

        snapshot = hid_elevation_windows._read_registered_task_after_registration(
            "task",
            timeout_seconds=0.2,
            poll_seconds=0.1,
            _read_task=reads,
            _sleep=sleeps.append,
        )

        self.assertIsNone(snapshot)
        self.assertEqual(reads.call_count, 3)
        self.assertEqual(sleeps, [0.1, 0.1])

    def test_com_initialization_is_balanced_on_the_background_thread(self):
        import comtypes

        calls = []
        root = object()

        def record(name):
            calls.append((name, threading.get_ident()))

        def worker():
            with hid_elevation_windows._task_service_session() as yielded:
                self.assertIs(yielded, root)

        with mock.patch.object(hid_elevation_windows, "_is_windows", return_value=True), mock.patch.object(
            hid_elevation_windows,
            "_task_service_root",
            return_value=root,
        ), mock.patch.object(
            comtypes,
            "CoInitialize",
            side_effect=lambda: record("init"),
        ), mock.patch.object(
            comtypes,
            "CoUninitialize",
            side_effect=lambda: record("uninit"),
        ):
            thread = threading.Thread(target=worker)
            thread.start()
            thread.join(1.0)

        self.assertFalse(thread.is_alive())
        self.assertEqual([name for name, _thread_id in calls], ["init", "uninit"])
        self.assertEqual(calls[0][1], calls[1][1])
        self.assertNotEqual(calls[0][1], threading.get_ident())

    @staticmethod
    def _task_root(*, states, result):
        class Running:
            def __init__(self, values):
                self._values = list(values)

            @property
            def State(self):
                if len(self._values) > 1:
                    return self._values.pop(0)
                return self._values[0]

        task = mock.Mock()
        task.Name = "task"
        task.Run.return_value = Running(states)
        task.LastTaskResult = result
        root = mock.Mock()
        root.GetTasks.return_value = _Collection([task])
        return root, task

    def test_registered_task_waits_for_completion_and_accepts_zero_result(self):
        root, task = self._task_root(states=[4, 3], result=0)

        with mock.patch.object(hid_elevation_windows.time, "sleep"):
            hid_elevation_windows._run_task(r"\task", _root=root)

        task.Run.assert_called_once_with("")

    def test_registered_task_busy_exit_is_reported_for_retry(self):
        root, _task = self._task_root(
            states=[3],
            result=hid_elevation_windows.HELPER_EXIT_OPERATION_BUSY,
        )

        with self.assertRaises(hid_elevation_windows.HidElevationError) as ctx:
            hid_elevation_windows._run_task(r"\task", _root=root)

        self.assertEqual(str(ctx.exception), "hid_helper_operation_busy")

    def test_registered_task_maps_runtime_failure_exit_codes(self):
        for exit_code, detail in (
            hid_elevation_windows._HELPER_RUNTIME_EXIT_ERROR_DETAILS.items()
        ):
            with self.subTest(exit_code=exit_code, detail=detail):
                root, _task = self._task_root(states=[3], result=exit_code)

                with self.assertRaises(
                    hid_elevation_windows.HidElevationError
                ) as ctx:
                    hid_elevation_windows._run_task(r"\task", _root=root)

                self.assertEqual(str(ctx.exception), detail)

    def test_registered_task_refreshes_stale_com_state_before_reading_result(self):
        class StaleRunningTask:
            def __init__(self):
                self.State = 4
                self.refresh_calls = 0

            def Refresh(self):
                self.refresh_calls += 1
                if self.refresh_calls >= 2:
                    self.State = 0
                    raise OSError("the completed running instance was retired")

        running = StaleRunningTask()
        task = mock.Mock()
        task.Name = "task"
        task.Run.return_value = running
        task.LastTaskResult = hid_elevation_windows.HELPER_EXIT_VALIDATION_FAILED
        root = mock.Mock()
        root.GetTasks.return_value = _Collection([task])

        with mock.patch.object(hid_elevation_windows.time, "sleep"):
            with self.assertRaises(hid_elevation_windows.HidElevationError) as ctx:
                hid_elevation_windows._run_task(r"\task", _root=root)

        self.assertEqual(
            str(ctx.exception),
            f"hid_helper_task_exit_{hid_elevation_windows.HELPER_EXIT_VALIDATION_FAILED}",
        )
        self.assertEqual(running.refresh_calls, 2)

    def test_registered_task_timeout_is_not_reported_as_success(self):
        root, _task = self._task_root(states=[4], result=0)

        with mock.patch.object(
            hid_elevation_windows.time,
            "monotonic",
            side_effect=[0.0, 31.0],
        ), mock.patch.object(hid_elevation_windows.time, "sleep"):
            with self.assertRaises(hid_elevation_windows.HidElevationError) as ctx:
                hid_elevation_windows._run_task(r"\task", _root=root)

        self.assertEqual(str(ctx.exception), "hid_helper_task_timeout")

    def test_registered_task_without_running_state_is_rejected(self):
        task = mock.Mock()
        task.Name = "task"
        task.Run.return_value = object()
        root = mock.Mock()
        root.GetTasks.return_value = _Collection([task])

        with self.assertRaises(hid_elevation_windows.HidElevationError) as ctx:
            hid_elevation_windows._run_task(r"\task", _root=root)

        self.assertEqual(
            str(ctx.exception),
            "hid_helper_task_result_unavailable",
        )

    def test_registered_task_start_exception_is_sanitized(self):
        task = mock.Mock()
        task.Name = "task"
        task.Run.side_effect = RuntimeError("COM failure")
        root = mock.Mock()
        root.GetTasks.return_value = _Collection([task])

        with self.assertRaises(hid_elevation_windows.HidElevationError) as ctx:
            hid_elevation_windows._run_task(r"\task", _root=root)

        self.assertEqual(str(ctx.exception), "hid_helper_task_start_failed")


class ElevatedProcessTests(unittest.TestCase):
    def test_uac_helper_timeout_terminates_and_waits_for_final_exit(self):
        wait_calls = []
        terminate_calls = []
        close_calls = []

        class Shell32:
            pass

        shell32 = Shell32()

        def launch(info_ptr):
            info_ptr._obj.hProcess = 123
            return True

        shell32.ShellExecuteExW = _CallableWin32Function(launch)

        class Kernel32:
            pass

        kernel32 = Kernel32()

        def wait(handle, timeout):
            wait_calls.append((handle, timeout))
            return 0x00000102 if len(wait_calls) == 1 else 0

        kernel32.WaitForSingleObject = _CallableWin32Function(wait)
        kernel32.GetExitCodeProcess = _CallableWin32Function(
            lambda _handle, _exit_code: True
        )
        kernel32.TerminateProcess = _CallableWin32Function(
            lambda handle, exit_code: terminate_calls.append(
                (handle, exit_code)
            )
            or True
        )
        kernel32.CloseHandle = _CallableWin32Function(
            lambda handle: close_calls.append(handle) or True
        )

        with mock.patch.object(
            hid_elevation_windows.ctypes,
            "WinDLL",
            side_effect=[shell32, kernel32],
        ):
            with self.assertRaises(hid_elevation_windows.HidElevationError) as ctx:
                hid_elevation_windows._run_elevated_and_wait(
                    Path(r"C:\helper.exe"),
                    "--install-task",
                    1.0,
                )

        self.assertEqual(str(ctx.exception), "hid_helper_setup_timeout")
        self.assertEqual(
            wait_calls,
            [
                (123, 1000),
                (123, hid_elevation_windows._ELEVATED_PROCESS_TERMINATION_WAIT_MS),
            ],
        )
        self.assertEqual(
            terminate_calls,
            [(123, hid_elevation_windows.HELPER_EXIT_UNEXPECTED_FAILURE)],
        )
        self.assertEqual(close_calls, [123])


class ProtectedPathTests(unittest.TestCase):
    def test_path_acl_rejects_user_write_or_extra_everyone_access(self):
        valid = hid_elevation_windows._path_security_sddl_text(
            SID, directory=True
        )
        self.assertTrue(
            hid_elevation_windows.validate_path_security_sddl(
                valid, user_sid=SID, directory=True
            )
        )
        self.assertFalse(
            hid_elevation_windows.validate_path_security_sddl(
                valid.replace(f"(A;OICI;GRGX;;;{SID})", f"(A;OICI;FA;;;{SID})"),
                user_sid=SID,
                directory=True,
            )
        )
        self.assertFalse(
            hid_elevation_windows.validate_path_security_sddl(
                valid + "(A;OICI;GR;;;WD)", user_sid=SID, directory=True
            )
        )

    def test_runtime_acl_grants_local_service_read_execute_only(self):
        valid = hid_elevation_windows._path_security_sddl_text(
            SID,
            directory=True,
            read_execute_sids=(hid_elevation_windows.LOCAL_SERVICE_SID,),
        )

        self.assertTrue(
            hid_elevation_windows.validate_path_security_sddl(
                valid,
                user_sid=SID,
                directory=True,
                read_execute_sids=(hid_elevation_windows.LOCAL_SERVICE_SID,),
            )
        )
        self.assertFalse(
            hid_elevation_windows.validate_path_security_sddl(
                valid, user_sid=SID, directory=True
            )
        )
        self.assertFalse(
            hid_elevation_windows.validate_path_security_sddl(
                valid.replace(
                    f"(A;OICI;GRGX;;;{hid_elevation_windows.LOCAL_SERVICE_SID})",
                    f"(A;OICI;FA;;;{hid_elevation_windows.LOCAL_SERVICE_SID})",
                ),
                user_sid=SID,
                directory=True,
                read_execute_sids=(hid_elevation_windows.LOCAL_SERVICE_SID,),
            )
        )

    def test_runtime_acl_accepts_windows_local_service_alias(self):
        valid = hid_elevation_windows._path_security_sddl_text(
            SID,
            directory=False,
            read_execute_sids=(hid_elevation_windows.LOCAL_SERVICE_SID,),
        ).replace(hid_elevation_windows.LOCAL_SERVICE_SID, "LS")

        self.assertTrue(
            hid_elevation_windows.validate_path_security_sddl(
                valid,
                user_sid=SID,
                directory=False,
                read_execute_sids=(hid_elevation_windows.LOCAL_SERVICE_SID,),
            )
        )

    def test_reparse_component_is_rejected_before_directory_creation(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            existing = root / "RemoteMic"
            existing.mkdir()
            target = existing / "RC003" / "protected"
            with mock.patch.object(
                hid_elevation_windows,
                "_is_reparse_point",
                side_effect=lambda path: path == existing,
            ), mock.patch.object(
                hid_elevation_windows, "_apply_path_security"
            ) as secure:
                with self.assertRaises(hid_elevation_windows.HidElevationError):
                    hid_elevation_windows.ensure_protected_directory(
                        target,
                        user_sid=SID,
                        trusted_root=root,
                        security_root=target,
                    )

            self.assertFalse((existing / "RC003").exists())
            secure.assert_not_called()

    def test_dangling_reparse_component_is_still_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            dangling = root / "dangling"
            real_lexists = hid_elevation_windows.os.path.lexists

            def lexists(path):
                return Path(path) == dangling or real_lexists(path)

            with mock.patch.object(
                hid_elevation_windows.os.path,
                "lexists",
                side_effect=lexists,
            ), mock.patch.object(
                hid_elevation_windows,
                "_is_reparse_point",
                side_effect=lambda path: Path(path) == dangling,
            ):
                with self.assertRaises(
                    hid_elevation_windows.HidElevationError
                ) as ctx:
                    hid_elevation_windows.assert_no_reparse_points(
                        dangling / "child",
                        trusted_root=root,
                    )

            self.assertEqual(str(ctx.exception), "protected_path_reparse_point")


class InstalledHelperTests(unittest.TestCase):
    @staticmethod
    def _acl(path: Path) -> str:
        return hid_elevation_windows._path_security_sddl_text(
            SID, directory=path.is_dir()
        )

    def _fixture(self, raw: str, *, helper_bytes: bytes = b"helper"):
        root = Path(raw)
        app = root / "app"
        app.mkdir()
        bundled = app / hid_elevation_windows.HELPER_BUNDLE_RELATIVE_PATH
        bundled.parent.mkdir()
        bundled.write_bytes(helper_bytes)
        owner_root = root / "protected"
        owner_root.mkdir()
        digest = hid_elevation_windows._sha256(bundled)
        manifest = hid_elevation_windows._build_manifest(SID, digest)
        target = owner_root / Path(manifest.helper_relative_path)
        target.parent.mkdir(parents=True)
        target.write_bytes(helper_bytes)
        manifest_path = owner_root / hid_elevation_windows.MANIFEST_FILENAME
        manifest_path.write_text(
            __import__("json").dumps(manifest.as_dict()), encoding="utf-8"
        )
        xml = hid_elevation_windows.task_definition_xml(
            target, SID, task_name=manifest.task_name
        )
        task = hid_elevation_windows._RegisteredTaskSnapshot(
            xml,
            hid_elevation_windows.task_security_sddl(SID),
        )
        return root, app, owner_root, manifest_path, target, manifest, task

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
            root, app, owner_root, _manifest_path, _target, manifest, task = (
                self._fixture(raw)
            )

            state = hid_elevation_windows.inspect_installed_helper(
                frozen=True,
                executable=str(app / "RemoteMicRC003.exe"),
                user_sid=SID,
                owner_root=owner_root,
                program_files_root=root,
                _read_task=lambda name: (
                    task if name == manifest.task_name else None
                ),
                _read_acl=self._acl,
            )

        self.assertEqual(state, hid_elevation_windows.HidHelperState(True))

    def test_hash_mismatch_fails_before_task_execution(self):
        with tempfile.TemporaryDirectory() as raw:
            root, app, owner_root, _manifest_path, target, _manifest, _task = (
                self._fixture(raw)
            )
            target.write_bytes(b"corrupt")
            task_reader = mock.Mock()

            state = hid_elevation_windows.inspect_installed_helper(
                frozen=True,
                executable=str(app / "RemoteMicRC003.exe"),
                user_sid=SID,
                owner_root=owner_root,
                program_files_root=root,
                _read_task=task_reader,
                _read_acl=self._acl,
            )

        self.assertEqual(state.detail, "protected_helper_hash_mismatch")
        task_reader.assert_not_called()

    def test_same_generation_from_an_older_build_is_reused(self):
        with tempfile.TemporaryDirectory() as raw:
            root, app, owner_root, _manifest_path, _target, manifest, task = (
                self._fixture(raw, helper_bytes=b"older-helper")
            )
            bundled = app / hid_elevation_windows.HELPER_BUNDLE_RELATIVE_PATH
            bundled.write_bytes(b"current-helper")

            state = hid_elevation_windows.inspect_installed_helper(
                frozen=True,
                executable=str(app / "RemoteMicRC003.exe"),
                user_sid=SID,
                owner_root=owner_root,
                program_files_root=root,
                _read_task=lambda name: (
                    task if name == manifest.task_name else None
                ),
                _read_acl=self._acl,
            )

        self.assertEqual(state, hid_elevation_windows.HidHelperState(True))

    def test_invalid_newer_generation_is_reported_without_repair(self):
        with tempfile.TemporaryDirectory() as raw:
            root, app, owner_root, manifest_path, target, manifest, _task = (
                self._fixture(raw)
            )
            newer_generation = hid_elevation_windows.HELPER_GENERATION + 1
            newer_relative = hid_elevation_windows.helper_relative_path(
                manifest.helper_sha256, generation=newer_generation
            )
            newer_target = owner_root / newer_relative
            newer_target.parent.mkdir(parents=True)
            target.replace(newer_target)
            data = manifest.as_dict()
            data["helper_generation"] = newer_generation
            data["helper_relative_path"] = newer_relative.as_posix()
            manifest_path.write_text(json.dumps(data), encoding="utf-8")

            state = hid_elevation_windows.inspect_installed_helper(
                frozen=True,
                executable=str(app / "RemoteMicRC003.exe"),
                user_sid=SID,
                owner_root=owner_root,
                program_files_root=root,
                _read_task=lambda _name: None,
                _read_acl=self._acl,
            )

        self.assertEqual(
            state,
            hid_elevation_windows.HidHelperState(False, "newer_helper_invalid"),
        )

    def test_same_protocol_newer_generation_is_reused(self):
        with tempfile.TemporaryDirectory() as raw:
            root, app, owner_root, manifest_path, target, manifest, _task = (
                self._fixture(raw)
            )
            newer_generation = hid_elevation_windows.HELPER_GENERATION + 1
            newer_relative = hid_elevation_windows.helper_relative_path(
                manifest.helper_sha256, generation=newer_generation
            )
            newer_target = owner_root / newer_relative
            newer_target.parent.mkdir(parents=True)
            target.replace(newer_target)
            data = manifest.as_dict()
            data["helper_generation"] = newer_generation
            data["helper_relative_path"] = newer_relative.as_posix()
            manifest_path.write_text(__import__("json").dumps(data), encoding="utf-8")
            xml = hid_elevation_windows.task_definition_xml(
                newer_target, SID, task_name=manifest.task_name
            )
            task = hid_elevation_windows._RegisteredTaskSnapshot(
                xml, hid_elevation_windows.task_security_sddl(SID)
            )

            state = hid_elevation_windows.inspect_installed_helper(
                frozen=True,
                executable=str(app / "RemoteMicRC003.exe"),
                user_sid=SID,
                owner_root=owner_root,
                program_files_root=root,
                _read_task=lambda _name: task,
                _read_acl=self._acl,
            )

        self.assertTrue(state.available)

    def test_older_task_contract_requires_one_repair(self):
        with tempfile.TemporaryDirectory() as raw:
            root, app, owner_root, manifest_path, _target, manifest, task = (
                self._fixture(raw)
            )
            data = manifest.as_dict()
            data["task_contract_version"] = (
                hid_elevation_windows.TASK_CONTRACT_VERSION - 1
            )
            manifest_path.write_text(__import__("json").dumps(data), encoding="utf-8")

            state = hid_elevation_windows.inspect_installed_helper(
                frozen=True,
                executable=str(app / "RemoteMicRC003.exe"),
                user_sid=SID,
                owner_root=owner_root,
                program_files_root=root,
                _read_task=lambda _name: task,
                _read_acl=self._acl,
            )

        self.assertEqual(state.detail, "protected_helper_outdated")

    def test_manifest_rejects_absolute_and_parent_relative_helper_paths(self):
        base = hid_elevation_windows._build_manifest(
            SID, "a" * 64
        ).as_dict()
        for invalid in ("C:/Windows/bad.exe", "../bad.exe", "generations/../bad.exe"):
            candidate = dict(base)
            candidate["helper_relative_path"] = invalid
            with self.assertRaises(hid_elevation_windows.HidElevationError):
                hid_elevation_windows._manifest_from_mapping(candidate)

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

    def test_offer_id_is_stable_for_different_binaries_in_the_same_contract(self):
        with tempfile.TemporaryDirectory() as raw:
            executable = Path(raw) / "RemoteMicRC003.exe"
            helper = Path(raw) / hid_elevation_windows.HELPER_BUNDLE_RELATIVE_PATH
            helper.parent.mkdir()
            helper.write_bytes(b"first-helper")
            first = hid_elevation_windows.bundled_helper_offer_id(
                frozen=True, executable=str(executable)
            )
            helper.write_bytes(b"second-helper")
            second = hid_elevation_windows.bundled_helper_offer_id(
                frozen=True, executable=str(executable)
            )

        self.assertEqual(first, second)
        self.assertEqual(
            first,
            (
                f"{hid_elevation_windows.MANIFEST_SCHEMA_VERSION}:"
                f"{hid_elevation_windows.HELPER_PROTOCOL_VERSION}:"
                f"{hid_elevation_windows.HELPER_GENERATION}:"
                f"{hid_elevation_windows.TASK_CONTRACT_VERSION}"
            ),
        )


class TaskLifecycleTests(unittest.TestCase):
    @staticmethod
    def _acl(path: Path) -> str:
        return hid_elevation_windows._path_security_sddl_text(
            SID, directory=path.is_dir()
        )

    @staticmethod
    def _task_snapshot(target: Path):
        task_name = hid_elevation_windows.task_name_for_sid(SID)
        return hid_elevation_windows._RegisteredTaskSnapshot(
            _with_empty_triggers(
                hid_elevation_windows.task_definition_xml(
                    target, SID, task_name=task_name
                )
            ),
            hid_elevation_windows.task_security_sddl(SID),
        )

    def test_install_copies_helper_registers_and_commits_manifest_last(self):
        with tempfile.TemporaryDirectory() as raw:
            program_files = Path(raw)
            source = program_files / "bundle" / hid_elevation_windows.HELPER_EXE_NAME
            source.parent.mkdir()
            source.write_bytes(b"helper-binary")
            owner_root = program_files / "protected"
            registered = None

            def register(name, xml, sddl):
                nonlocal registered
                registered = hid_elevation_windows._RegisteredTaskSnapshot(
                    _with_empty_triggers(xml),
                    sddl,
                )

            def read_task(name):
                if name == hid_elevation_windows.LEGACY_TASK_NAME:
                    return None
                return registered

            with mock.patch.object(
                hid_elevation_windows, "is_process_elevated", return_value=True
            ), mock.patch.object(
                hid_elevation_windows, "current_user_sid", return_value=SID
            ), mock.patch.object(
                hid_elevation_windows, "_apply_path_security"
            ), mock.patch.object(
                hid_elevation_windows,
                "_read_path_security_sddl",
                side_effect=self._acl,
            ), mock.patch.object(
                hid_elevation_windows, "_register_task", side_effect=register
            ), mock.patch.object(
                hid_elevation_windows, "_read_registered_task", side_effect=read_task
            ):
                hid_elevation_windows.install_task(
                    source_executable=source,
                    request_sid=SID,
                    owner_root=owner_root,
                    program_files_root=program_files,
                    _read_acl=self._acl,
                )

            manifest = hid_elevation_windows._load_manifest(
                owner_root / hid_elevation_windows.MANIFEST_FILENAME
            )
            target = owner_root / Path(manifest.helper_relative_path)
            self.assertEqual(target.read_bytes(), source.read_bytes())
            self.assertIsNotNone(registered)
            self.assertTrue(
                hid_elevation_windows.validate_registered_task_xml(
                    registered.xml_text,
                    helper_path=target,
                    user_sid=SID,
                    task_name=manifest.task_name,
                )
            )

    def test_copy_failure_is_reported_without_exposing_the_os_error(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source.exe"
            destination = root / "protected" / "helper.exe"
            source.write_bytes(b"helper")
            destination.parent.mkdir()

            with mock.patch.object(
                hid_elevation_windows.shutil,
                "copy2",
                side_effect=OSError("machine-local path detail"),
            ):
                with self.assertRaises(
                    hid_elevation_windows.HidElevationError
                ) as ctx:
                    hid_elevation_windows._copy_verified_helper(
                        source,
                        destination,
                        user_sid=SID,
                    )

            self.assertEqual(str(ctx.exception), "helper_copy_failed")
            self.assertFalse(destination.exists())

    def test_post_copy_acl_query_failure_removes_the_uncommitted_helper(self):
        with tempfile.TemporaryDirectory() as raw:
            program_files = Path(raw)
            source = program_files / "bundle" / hid_elevation_windows.HELPER_EXE_NAME
            source.parent.mkdir()
            source.write_bytes(b"helper-binary")
            owner_root = program_files / "protected"
            target = owner_root / hid_elevation_windows.helper_relative_path(
                hid_elevation_windows._sha256(source)
            )
            register = mock.Mock()

            def read_acl(path):
                if Path(path) == target:
                    raise OSError("simulated post-copy ACL query failure")
                return self._acl(Path(path))

            with mock.patch.object(
                hid_elevation_windows, "is_process_elevated", return_value=True
            ), mock.patch.object(
                hid_elevation_windows, "current_user_sid", return_value=SID
            ), mock.patch.object(
                hid_elevation_windows, "_apply_path_security"
            ), mock.patch.object(
                hid_elevation_windows, "_read_registered_task", return_value=None
            ), mock.patch.object(
                hid_elevation_windows, "_register_task", register
            ):
                with self.assertRaises(
                    hid_elevation_windows.HidElevationError
                ) as ctx:
                    hid_elevation_windows.install_task(
                        source_executable=source,
                        request_sid=SID,
                        owner_root=owner_root,
                        program_files_root=program_files,
                        _read_acl=read_acl,
                    )

            self.assertEqual(str(ctx.exception), "protected_path_acl_query_failed")
            self.assertFalse(target.exists())
            self.assertFalse(
                (owner_root / hid_elevation_windows.MANIFEST_FILENAME).exists()
            )
            register.assert_not_called()

    def test_install_uses_register_result_when_query_is_not_yet_visible(self):
        with tempfile.TemporaryDirectory() as raw:
            program_files = Path(raw)
            source = program_files / "bundle" / hid_elevation_windows.HELPER_EXE_NAME
            source.parent.mkdir()
            source.write_bytes(b"helper-binary")
            owner_root = program_files / "protected"

            def register(_name, xml, sddl):
                return hid_elevation_windows._RegisteredTaskSnapshot(
                    _with_empty_triggers(xml), sddl
                )

            with mock.patch.object(
                hid_elevation_windows, "is_process_elevated", return_value=True
            ), mock.patch.object(
                hid_elevation_windows, "current_user_sid", return_value=SID
            ), mock.patch.object(
                hid_elevation_windows, "_apply_path_security"
            ), mock.patch.object(
                hid_elevation_windows,
                "_read_path_security_sddl",
                side_effect=self._acl,
            ), mock.patch.object(
                hid_elevation_windows, "_register_task", side_effect=register
            ), mock.patch.object(
                hid_elevation_windows, "_read_registered_task", return_value=None
            ):
                hid_elevation_windows.install_task(
                    source_executable=source,
                    request_sid=SID,
                    owner_root=owner_root,
                    program_files_root=program_files,
                    _read_acl=self._acl,
                )

            manifest = hid_elevation_windows._load_manifest(
                owner_root / hid_elevation_windows.MANIFEST_FILENAME
            )
            self.assertEqual(manifest.helper_sha256, hid_elevation_windows._sha256(source))

    def test_install_retries_until_the_registered_task_becomes_visible(self):
        with tempfile.TemporaryDirectory() as raw:
            program_files = Path(raw)
            source = program_files / "bundle" / hid_elevation_windows.HELPER_EXE_NAME
            source.parent.mkdir()
            source.write_bytes(b"helper-binary")
            owner_root = program_files / "protected"
            registered = None
            post_register_reads = 0
            sleeps = []

            def register(_name, xml, sddl):
                nonlocal registered
                registered = hid_elevation_windows._RegisteredTaskSnapshot(
                    _with_empty_triggers(xml), sddl
                )

            def read_task(name):
                nonlocal post_register_reads
                if name == hid_elevation_windows.LEGACY_TASK_NAME:
                    return None
                if registered is None:
                    return None
                post_register_reads += 1
                if post_register_reads == 1:
                    raise hid_elevation_windows.HidElevationError(
                        "hid_helper_task_query_failed"
                    )
                if post_register_reads == 2:
                    return None
                return registered

            with mock.patch.object(
                hid_elevation_windows, "is_process_elevated", return_value=True
            ), mock.patch.object(
                hid_elevation_windows, "current_user_sid", return_value=SID
            ), mock.patch.object(
                hid_elevation_windows, "_apply_path_security"
            ), mock.patch.object(
                hid_elevation_windows,
                "_read_path_security_sddl",
                side_effect=self._acl,
            ), mock.patch.object(
                hid_elevation_windows, "_register_task", side_effect=register
            ), mock.patch.object(
                hid_elevation_windows,
                "_read_registered_task",
                side_effect=read_task,
            ), mock.patch.object(
                hid_elevation_windows.time, "sleep", side_effect=sleeps.append
            ):
                hid_elevation_windows.install_task(
                    source_executable=source,
                    request_sid=SID,
                    owner_root=owner_root,
                    program_files_root=program_files,
                    _read_acl=self._acl,
                )

            manifest = hid_elevation_windows._load_manifest(
                owner_root / hid_elevation_windows.MANIFEST_FILENAME
            )
            self.assertEqual(manifest.helper_sha256, hid_elevation_windows._sha256(source))
            self.assertEqual(post_register_reads, 3)
            self.assertEqual(
                sleeps,
                [
                    hid_elevation_windows._TASK_REGISTRATION_SETTLE_POLL_SECONDS,
                    hid_elevation_windows._TASK_REGISTRATION_SETTLE_POLL_SECONDS,
                ],
            )

    def test_install_removes_the_current_users_valid_legacy_task(self):
        with tempfile.TemporaryDirectory() as raw:
            program_files = Path(raw)
            source = program_files / "bundle" / hid_elevation_windows.HELPER_EXE_NAME
            source.parent.mkdir()
            source.write_bytes(b"helper-binary")
            owner_root = program_files / "protected"
            legacy_target = hid_elevation_windows.legacy_protected_helper_path(
                program_files_root=program_files
            )
            legacy_target.parent.mkdir(parents=True)
            legacy_target.write_bytes(b"old-helper")
            legacy_task = hid_elevation_windows._RegisteredTaskSnapshot(
                hid_elevation_windows.task_definition_xml(
                    legacy_target,
                    SID,
                    task_name=hid_elevation_windows.LEGACY_TASK_NAME,
                ),
                "legacy-sddl",
            )
            tasks = {hid_elevation_windows.LEGACY_TASK_NAME: legacy_task}

            def read_task(name):
                return tasks.get(name)

            def delete_task(name, *, missing_ok):
                self.assertFalse(missing_ok)
                tasks.pop(name, None)

            def register_task(name, xml, sddl):
                tasks[name] = hid_elevation_windows._RegisteredTaskSnapshot(
                    xml, sddl
                )

            with mock.patch.object(
                hid_elevation_windows, "is_process_elevated", return_value=True
            ), mock.patch.object(
                hid_elevation_windows, "current_user_sid", return_value=SID
            ), mock.patch.object(
                hid_elevation_windows, "_apply_path_security"
            ), mock.patch.object(
                hid_elevation_windows,
                "_read_path_security_sddl",
                side_effect=self._acl,
            ), mock.patch.object(
                hid_elevation_windows, "_read_registered_task", side_effect=read_task
            ), mock.patch.object(
                hid_elevation_windows, "_delete_task", side_effect=delete_task
            ), mock.patch.object(
                hid_elevation_windows, "_register_task", side_effect=register_task
            ):
                hid_elevation_windows.install_task(
                    source_executable=source,
                    request_sid=SID,
                    owner_root=owner_root,
                    program_files_root=program_files,
                    _read_acl=self._acl,
                )

            self.assertNotIn(hid_elevation_windows.LEGACY_TASK_NAME, tasks)
            self.assertFalse(legacy_target.exists())

    def test_install_removes_current_users_legacy_task_with_damaged_settings(self):
        with tempfile.TemporaryDirectory() as raw:
            program_files = Path(raw)
            legacy_target = hid_elevation_windows.legacy_protected_helper_path(
                program_files_root=program_files
            )
            legacy_target.parent.mkdir(parents=True)
            legacy_target.write_bytes(b"old-helper")
            damaged_xml = hid_elevation_windows.task_definition_xml(
                legacy_target,
                SID,
                task_name=hid_elevation_windows.LEGACY_TASK_NAME,
            ).replace(
                "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>",
                "<MultipleInstancesPolicy>Parallel</MultipleInstancesPolicy>",
            )
            tasks = {
                hid_elevation_windows.LEGACY_TASK_NAME: (
                    hid_elevation_windows._RegisteredTaskSnapshot(
                        damaged_xml,
                        "legacy-sddl",
                    )
                )
            }

            def delete_task(name, *, missing_ok):
                self.assertFalse(missing_ok)
                tasks.pop(name, None)

            with mock.patch.object(
                hid_elevation_windows,
                "_read_registered_task",
                side_effect=lambda name: tasks.get(name),
            ), mock.patch.object(
                hid_elevation_windows,
                "_delete_task",
                side_effect=delete_task,
            ):
                removed = hid_elevation_windows._cleanup_legacy_contract_for_user(
                    SID,
                    trusted_root=program_files,
                )

            self.assertTrue(removed)
            self.assertEqual(tasks, {})
            self.assertFalse(legacy_target.exists())

    def test_install_preserves_another_users_legacy_task(self):
        with tempfile.TemporaryDirectory() as raw:
            program_files = Path(raw)
            legacy_target = hid_elevation_windows.legacy_protected_helper_path(
                program_files_root=program_files
            )
            legacy_target.parent.mkdir(parents=True)
            legacy_target.write_bytes(b"old-helper")
            other_sid = "S-1-5-21-111-222-333-1002"
            legacy_task = hid_elevation_windows._RegisteredTaskSnapshot(
                hid_elevation_windows.task_definition_xml(
                    legacy_target,
                    other_sid,
                    task_name=hid_elevation_windows.LEGACY_TASK_NAME,
                ),
                "legacy-sddl",
            )
            delete = mock.Mock()
            with mock.patch.object(
                hid_elevation_windows,
                "_read_registered_task",
                return_value=legacy_task,
            ), mock.patch.object(
                hid_elevation_windows, "_delete_task", delete
            ):
                removed = hid_elevation_windows._cleanup_legacy_contract_for_user(
                    SID,
                    trusted_root=program_files,
                )

            self.assertFalse(removed)
            self.assertTrue(legacy_target.exists())
            delete.assert_not_called()

    def test_other_users_legacy_task_is_ignored_before_its_damaged_path(self):
        other_sid = "S-1-5-21-111-222-333-1002"
        with tempfile.TemporaryDirectory() as raw:
            program_files = Path(raw)
            legacy_target = hid_elevation_windows.legacy_protected_helper_path(
                program_files_root=program_files
            )
            legacy_target.mkdir(parents=True)
            legacy_task = hid_elevation_windows._RegisteredTaskSnapshot(
                hid_elevation_windows.task_definition_xml(
                    legacy_target,
                    other_sid,
                    task_name=hid_elevation_windows.LEGACY_TASK_NAME,
                ),
                "legacy-sddl",
            )
            delete = mock.Mock()

            with mock.patch.object(
                hid_elevation_windows,
                "_read_registered_task",
                return_value=legacy_task,
            ), mock.patch.object(
                hid_elevation_windows,
                "_delete_task",
                delete,
            ):
                removed = hid_elevation_windows._cleanup_legacy_contract_for_user(
                    SID,
                    trusted_root=program_files,
                )

            self.assertFalse(removed)
            self.assertTrue(legacy_target.is_dir())
            delete.assert_not_called()

    def test_unreadable_legacy_owner_remains_visible_as_cleanup_pending(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            app = root / "app"
            app.mkdir()
            bundled = app / hid_elevation_windows.HELPER_BUNDLE_RELATIVE_PATH
            bundled.parent.mkdir()
            bundled.write_bytes(b"helper")
            owner_root = root / "protected"
            owner_root.mkdir()
            manifest = hid_elevation_windows._build_manifest(
                SID,
                hid_elevation_windows._sha256(bundled),
            )
            target = owner_root / Path(manifest.helper_relative_path)
            target.parent.mkdir(parents=True)
            target.write_bytes(b"helper")
            (owner_root / hid_elevation_windows.MANIFEST_FILENAME).write_text(
                json.dumps(manifest.as_dict()),
                encoding="utf-8",
            )
            task = self._task_snapshot(target)
            unreadable_legacy = hid_elevation_windows._RegisteredTaskSnapshot(
                "<Task broken",
                "legacy-sddl",
            )

            def read_task(name):
                if name == manifest.task_name:
                    return task
                if name == hid_elevation_windows.LEGACY_TASK_NAME:
                    return unreadable_legacy
                return None

            states = [
                hid_elevation_windows.inspect_installed_helper(
                    frozen=True,
                    executable=str(app / "RemoteMicRC003.exe"),
                    user_sid=SID,
                    owner_root=owner_root,
                    program_files_root=root,
                    _read_task=read_task,
                    _read_acl=self._acl,
                )
                for _ in range(2)
            ]

        self.assertEqual(
            states,
            [
                hid_elevation_windows.HidHelperState(
                    True, "helper_cleanup_pending"
                ),
                hid_elevation_windows.HidHelperState(
                    True, "helper_cleanup_pending"
                ),
            ],
        )

    def test_sid_mismatch_fails_before_any_write(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / hid_elevation_windows.HELPER_EXE_NAME
            source.write_bytes(b"helper")
            with mock.patch.object(
                hid_elevation_windows, "is_process_elevated", return_value=True
            ), mock.patch.object(
                hid_elevation_windows,
                "current_user_sid",
                return_value="S-1-5-21-111-222-333-1002",
            ), mock.patch.object(
                hid_elevation_windows, "ensure_protected_directory"
            ) as mkdir, mock.patch.object(
                hid_elevation_windows, "_register_task"
            ) as register:
                with self.assertRaises(hid_elevation_windows.HidElevationError) as ctx:
                    hid_elevation_windows.install_task(
                        source_executable=source,
                        request_sid=SID,
                        owner_root=root / "protected",
                        program_files_root=root,
                    )

            self.assertEqual(str(ctx.exception), "request_sid_mismatch")
            mkdir.assert_not_called()
            register.assert_not_called()

    def test_failed_task_verification_restores_previous_task_and_manifest(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "bundle" / hid_elevation_windows.HELPER_EXE_NAME
            source.parent.mkdir()
            source.write_bytes(b"new-helper")
            owner_root = root / "protected"
            owner_root.mkdir()
            old_bytes = b"old-helper"
            old_source = root / "old-helper.exe"
            old_source.write_bytes(old_bytes)
            old_digest = hid_elevation_windows._sha256(old_source)
            old_manifest = hid_elevation_windows._build_manifest(SID, old_digest)
            old_relative = hid_elevation_windows.helper_relative_path(
                old_digest, generation=hid_elevation_windows.HELPER_GENERATION - 1 or 1
            )
            old_target = owner_root / old_relative
            old_target.parent.mkdir(parents=True)
            old_target.write_bytes(old_bytes)
            old_data = old_manifest.as_dict()
            old_data["helper_generation"] = max(1, hid_elevation_windows.HELPER_GENERATION - 1)
            old_data["helper_relative_path"] = old_relative.as_posix()
            (owner_root / hid_elevation_windows.MANIFEST_FILENAME).write_text(
                __import__("json").dumps(old_data), encoding="utf-8"
            )
            previous = hid_elevation_windows._RegisteredTaskSnapshot(
                hid_elevation_windows.task_definition_xml(
                    old_target,
                    SID,
                    task_name=old_manifest.task_name,
                ),
                hid_elevation_windows.task_security_sddl(SID),
            )
            current = previous
            register_calls = []

            def register(_name, xml, sddl):
                nonlocal current
                register_calls.append((xml, sddl))
                current = hid_elevation_windows._RegisteredTaskSnapshot(xml, sddl)

            reads = 0

            def read_task(name):
                nonlocal reads
                if name == hid_elevation_windows.LEGACY_TASK_NAME:
                    return None
                reads += 1
                if reads == 1:
                    return previous
                if len(register_calls) == 1:
                    return hid_elevation_windows._RegisteredTaskSnapshot(
                        register_calls[0][0].replace("</Actions>", "<ComHandler /></Actions>"),
                        register_calls[0][1],
                    )
                return current

            with mock.patch.object(
                hid_elevation_windows, "is_process_elevated", return_value=True
            ), mock.patch.object(
                hid_elevation_windows, "current_user_sid", return_value=SID
            ), mock.patch.object(
                hid_elevation_windows, "_apply_path_security"
            ), mock.patch.object(
                hid_elevation_windows,
                "_read_path_security_sddl",
                side_effect=self._acl,
            ), mock.patch.object(
                hid_elevation_windows, "_register_task", side_effect=register
            ), mock.patch.object(
                hid_elevation_windows, "_read_registered_task", side_effect=read_task
            ):
                with self.assertRaises(hid_elevation_windows.HidElevationError):
                    hid_elevation_windows.install_task(
                        source_executable=source,
                        request_sid=SID,
                        owner_root=owner_root,
                        program_files_root=root,
                        _read_acl=self._acl,
                    )

            self.assertGreaterEqual(len(register_calls), 2)
            self.assertEqual(register_calls[-1], (previous.xml_text, previous.security_sddl))
            saved = hid_elevation_windows._load_manifest(
                owner_root / hid_elevation_windows.MANIFEST_FILENAME
            )
            self.assertEqual(saved.helper_sha256, old_digest)

    def test_install_repairs_a_corrupt_manifest_and_task_owned_helper(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "bundle" / hid_elevation_windows.HELPER_EXE_NAME
            source.parent.mkdir()
            source.write_bytes(b"verified-current-helper")
            digest = hid_elevation_windows._sha256(source)
            owner_root = root / "protected"
            target = owner_root / hid_elevation_windows.helper_relative_path(digest)
            target.parent.mkdir(parents=True)
            target.write_bytes(b"corrupted-helper")
            manifest_path = owner_root / hid_elevation_windows.MANIFEST_FILENAME
            manifest_path.write_text("{not-json", encoding="utf-8")
            task_name = hid_elevation_windows.task_name_for_sid(SID)
            tasks = {task_name: self._task_snapshot(target)}

            def read_task(name):
                if name == hid_elevation_windows.LEGACY_TASK_NAME:
                    return None
                return tasks.get(name)

            def register_task(name, xml, sddl):
                tasks[name] = hid_elevation_windows._RegisteredTaskSnapshot(
                    xml, sddl
                )

            with mock.patch.object(
                hid_elevation_windows, "is_process_elevated", return_value=True
            ), mock.patch.object(
                hid_elevation_windows, "current_user_sid", return_value=SID
            ), mock.patch.object(
                hid_elevation_windows, "_apply_path_security"
            ), mock.patch.object(
                hid_elevation_windows, "_read_registered_task", side_effect=read_task
            ), mock.patch.object(
                hid_elevation_windows, "_register_task", side_effect=register_task
            ):
                hid_elevation_windows.install_task(
                    source_executable=source,
                    request_sid=SID,
                    owner_root=owner_root,
                    program_files_root=root,
                    _read_acl=self._acl,
                )

            saved = hid_elevation_windows._load_manifest(manifest_path)
            self.assertEqual(saved.helper_sha256, digest)
            self.assertEqual(target.read_bytes(), source.read_bytes())
            self.assertTrue(
                hid_elevation_windows.validate_registered_task_xml(
                    tasks[task_name].xml_text,
                    helper_path=target,
                    user_sid=SID,
                    task_name=task_name,
                )
            )

    def test_install_replaces_damaged_fixed_task_variants(self):
        for label in ("malformed_xml", "invalid_acl", "outside_command"):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                source = root / "bundle" / hid_elevation_windows.HELPER_EXE_NAME
                source.parent.mkdir()
                source.write_bytes(b"current-helper")
                owner_root = root / "protected"
                task_name = hid_elevation_windows.task_name_for_sid(SID)
                old_target = owner_root / hid_elevation_windows.helper_relative_path(
                    "a" * 64
                )
                valid_xml = hid_elevation_windows.task_definition_xml(
                    old_target, SID, task_name=task_name
                )
                if label == "malformed_xml":
                    previous = hid_elevation_windows._RegisteredTaskSnapshot(
                        "<Task>", hid_elevation_windows.task_security_sddl(SID)
                    )
                elif label == "invalid_acl":
                    previous = hid_elevation_windows._RegisteredTaskSnapshot(
                        valid_xml, "D:(A;;FA;;;WD)"
                    )
                else:
                    previous = hid_elevation_windows._RegisteredTaskSnapshot(
                        hid_elevation_windows.task_definition_xml(
                            root / "outside.exe", SID, task_name=task_name
                        ),
                        hid_elevation_windows.task_security_sddl(SID),
                    )
                tasks = {task_name: previous}

                def read_task(name):
                    if name == hid_elevation_windows.LEGACY_TASK_NAME:
                        return None
                    return tasks.get(name)

                def register_task(name, xml, sddl):
                    tasks[name] = hid_elevation_windows._RegisteredTaskSnapshot(
                        xml, sddl
                    )

                with mock.patch.object(
                    hid_elevation_windows, "is_process_elevated", return_value=True
                ), mock.patch.object(
                    hid_elevation_windows, "current_user_sid", return_value=SID
                ), mock.patch.object(
                    hid_elevation_windows, "_apply_path_security"
                ), mock.patch.object(
                    hid_elevation_windows,
                    "_read_registered_task",
                    side_effect=read_task,
                ), mock.patch.object(
                    hid_elevation_windows,
                    "_register_task",
                    side_effect=register_task,
                ):
                    hid_elevation_windows.install_task(
                        source_executable=source,
                        request_sid=SID,
                        owner_root=owner_root,
                        program_files_root=root,
                        _read_acl=self._acl,
                    )

                manifest = hid_elevation_windows._load_manifest(
                    owner_root / hid_elevation_windows.MANIFEST_FILENAME
                )
                target = owner_root / Path(manifest.helper_relative_path)
                self.assertEqual(target.read_bytes(), source.read_bytes())
                self.assertTrue(
                    hid_elevation_windows.validate_registered_task_xml(
                        tasks[task_name].xml_text,
                        helper_path=target,
                        user_sid=SID,
                        task_name=task_name,
                    )
                )

    def test_install_cleanup_reuses_a_valid_same_generation_helper(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "bundle" / hid_elevation_windows.HELPER_EXE_NAME
            source.parent.mkdir()
            source.write_bytes(b"newer-build-same-contract")
            owner_root = root / "protected"
            installed_source = root / "installed-helper.exe"
            installed_source.write_bytes(b"already-approved-helper")
            installed_digest = hid_elevation_windows._sha256(installed_source)
            manifest = hid_elevation_windows._build_manifest(SID, installed_digest)
            target = owner_root / Path(manifest.helper_relative_path)
            target.parent.mkdir(parents=True)
            target.write_bytes(installed_source.read_bytes())
            manifest_path = owner_root / hid_elevation_windows.MANIFEST_FILENAME
            manifest_path.write_text(json.dumps(manifest.as_dict()), encoding="utf-8")

            orphan_source = root / "old-helper.exe"
            orphan_source.write_bytes(b"old-generation")
            orphan_digest = hid_elevation_windows._sha256(orphan_source)
            orphan_target = owner_root / hid_elevation_windows.helper_relative_path(
                orphan_digest,
                generation=max(1, hid_elevation_windows.HELPER_GENERATION - 1),
            )
            orphan_target.parent.mkdir(parents=True)
            orphan_target.write_bytes(orphan_source.read_bytes())
            tasks = {manifest.task_name: self._task_snapshot(target)}

            def read_task(name):
                if name == hid_elevation_windows.LEGACY_TASK_NAME:
                    return None
                return tasks.get(name)

            def register_task(name, xml, sddl):
                tasks[name] = hid_elevation_windows._RegisteredTaskSnapshot(xml, sddl)

            with mock.patch.object(
                hid_elevation_windows, "is_process_elevated", return_value=True
            ), mock.patch.object(
                hid_elevation_windows, "current_user_sid", return_value=SID
            ), mock.patch.object(
                hid_elevation_windows, "_apply_path_security"
            ), mock.patch.object(
                hid_elevation_windows,
                "_read_registered_task",
                side_effect=read_task,
            ), mock.patch.object(
                hid_elevation_windows,
                "_register_task",
                side_effect=register_task,
            ):
                hid_elevation_windows.install_task(
                    source_executable=source,
                    request_sid=SID,
                    owner_root=owner_root,
                    program_files_root=root,
                    _read_acl=self._acl,
                )

            saved = hid_elevation_windows._load_manifest(manifest_path)
            bundled_target = owner_root / hid_elevation_windows.helper_relative_path(
                hid_elevation_windows._sha256(source)
            )
            self.assertEqual(saved.helper_sha256, installed_digest)
            self.assertEqual(target.read_bytes(), installed_source.read_bytes())
            self.assertFalse(bundled_target.exists())
            self.assertFalse(orphan_target.exists())

    def test_install_recovers_a_missing_manifest_and_cleans_the_old_helper(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "bundle" / hid_elevation_windows.HELPER_EXE_NAME
            source.parent.mkdir()
            source.write_bytes(b"new-helper")
            owner_root = root / "protected"
            old_source = root / "old-helper.exe"
            old_source.write_bytes(b"old-helper")
            old_digest = hid_elevation_windows._sha256(old_source)
            old_target = owner_root / hid_elevation_windows.helper_relative_path(
                old_digest,
                generation=max(1, hid_elevation_windows.HELPER_GENERATION - 1),
            )
            old_target.parent.mkdir(parents=True)
            old_target.write_bytes(old_source.read_bytes())
            task_name = hid_elevation_windows.task_name_for_sid(SID)
            tasks = {task_name: self._task_snapshot(old_target)}

            def read_task(name):
                if name == hid_elevation_windows.LEGACY_TASK_NAME:
                    return None
                return tasks.get(name)

            def register_task(name, xml, sddl):
                tasks[name] = hid_elevation_windows._RegisteredTaskSnapshot(
                    xml, sddl
                )

            with mock.patch.object(
                hid_elevation_windows, "is_process_elevated", return_value=True
            ), mock.patch.object(
                hid_elevation_windows, "current_user_sid", return_value=SID
            ), mock.patch.object(
                hid_elevation_windows, "_apply_path_security"
            ), mock.patch.object(
                hid_elevation_windows, "_read_registered_task", side_effect=read_task
            ), mock.patch.object(
                hid_elevation_windows, "_register_task", side_effect=register_task
            ):
                hid_elevation_windows.install_task(
                    source_executable=source,
                    request_sid=SID,
                    owner_root=owner_root,
                    program_files_root=root,
                    _read_acl=self._acl,
                )

            manifest = hid_elevation_windows._load_manifest(
                owner_root / hid_elevation_windows.MANIFEST_FILENAME
            )
            new_target = owner_root / Path(manifest.helper_relative_path)
            self.assertTrue(new_target.is_file())
            self.assertFalse(old_target.exists())

    def test_install_preserves_a_future_task_before_any_write(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "bundle" / hid_elevation_windows.HELPER_EXE_NAME
            source.parent.mkdir()
            source.write_bytes(b"current-helper")
            future_source = root / "future-helper.exe"
            future_source.write_bytes(b"future-helper")
            future_digest = hid_elevation_windows._sha256(future_source)
            owner_root = root / "protected"
            future_target = owner_root / hid_elevation_windows.helper_relative_path(
                future_digest,
                generation=hid_elevation_windows.HELPER_GENERATION + 1,
            )
            future_target.parent.mkdir(parents=True)
            future_target.write_bytes(future_source.read_bytes())
            task_name = hid_elevation_windows.task_name_for_sid(SID)
            task = self._task_snapshot(future_target)
            ensure_directory = mock.Mock()
            register_task = mock.Mock()

            def read_task(name):
                if name == hid_elevation_windows.LEGACY_TASK_NAME:
                    return None
                return task if name == task_name else None

            with mock.patch.object(
                hid_elevation_windows, "is_process_elevated", return_value=True
            ), mock.patch.object(
                hid_elevation_windows, "current_user_sid", return_value=SID
            ), mock.patch.object(
                hid_elevation_windows,
                "ensure_protected_directory",
                ensure_directory,
            ), mock.patch.object(
                hid_elevation_windows, "_register_task", register_task
            ), mock.patch.object(
                hid_elevation_windows, "_read_registered_task", side_effect=read_task
            ):
                with self.assertRaises(
                    hid_elevation_windows.HidElevationError
                ) as ctx:
                    hid_elevation_windows.install_task(
                        source_executable=source,
                        request_sid=SID,
                        owner_root=owner_root,
                        program_files_root=root,
                        _read_acl=self._acl,
                    )

            self.assertEqual(str(ctx.exception), "newer_helper_preserved")
            ensure_directory.assert_not_called()
            register_task.assert_not_called()
            self.assertTrue(future_target.is_file())

    def test_manifest_write_failure_restores_the_previous_contract(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "bundle" / hid_elevation_windows.HELPER_EXE_NAME
            source.parent.mkdir()
            source.write_bytes(b"new-helper")
            owner_root = root / "protected"
            owner_root.mkdir()
            old_source = root / "old-helper.exe"
            old_source.write_bytes(b"old-helper")
            old_digest = hid_elevation_windows._sha256(old_source)
            old_generation = max(1, hid_elevation_windows.HELPER_GENERATION - 1)
            old_relative = hid_elevation_windows.helper_relative_path(
                old_digest, generation=old_generation
            )
            old_target = owner_root / old_relative
            old_target.parent.mkdir(parents=True)
            old_target.write_bytes(old_source.read_bytes())
            old_manifest = hid_elevation_windows._build_manifest(SID, old_digest)
            old_data = old_manifest.as_dict()
            old_data["helper_generation"] = old_generation
            old_data["helper_relative_path"] = old_relative.as_posix()
            manifest_path = owner_root / hid_elevation_windows.MANIFEST_FILENAME
            manifest_path.write_text(
                json.dumps(old_data, sort_keys=True), encoding="utf-8"
            )
            old_manifest_bytes = manifest_path.read_bytes()
            task_name = hid_elevation_windows.task_name_for_sid(SID)
            previous_task = self._task_snapshot(old_target)
            tasks = {task_name: previous_task}

            def read_task(name):
                if name == hid_elevation_windows.LEGACY_TASK_NAME:
                    return None
                return tasks.get(name)

            def register_task(name, xml, sddl):
                tasks[name] = hid_elevation_windows._RegisteredTaskSnapshot(
                    xml, sddl
                )

            def fail_after_replace(path, manifest):
                path.write_text(json.dumps(manifest.as_dict()), encoding="utf-8")
                raise OSError("simulated manifest commit failure")

            with mock.patch.object(
                hid_elevation_windows, "is_process_elevated", return_value=True
            ), mock.patch.object(
                hid_elevation_windows, "current_user_sid", return_value=SID
            ), mock.patch.object(
                hid_elevation_windows, "_apply_path_security"
            ), mock.patch.object(
                hid_elevation_windows, "_read_registered_task", side_effect=read_task
            ), mock.patch.object(
                hid_elevation_windows, "_register_task", side_effect=register_task
            ), mock.patch.object(
                hid_elevation_windows,
                "_write_manifest_atomic",
                side_effect=fail_after_replace,
            ):
                with self.assertRaises(hid_elevation_windows.HidElevationError):
                    hid_elevation_windows.install_task(
                        source_executable=source,
                        request_sid=SID,
                        owner_root=owner_root,
                        program_files_root=root,
                        _read_acl=self._acl,
                    )

            new_digest = hid_elevation_windows._sha256(source)
            new_target = owner_root / hid_elevation_windows.helper_relative_path(
                new_digest
            )
            self.assertEqual(manifest_path.read_bytes(), old_manifest_bytes)
            self.assertEqual(tasks.get(task_name), previous_task)
            self.assertTrue(old_target.is_file())
            self.assertFalse(new_target.exists())

    def test_uninstall_is_idempotent_when_manifest_is_absent(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            with mock.patch.object(
                hid_elevation_windows, "is_process_elevated", return_value=True
            ), mock.patch.object(
                hid_elevation_windows, "current_user_sid", return_value=SID
            ), mock.patch.object(
                hid_elevation_windows, "_read_registered_task", return_value=None
            ), mock.patch.object(hid_elevation_windows, "_delete_task") as delete:
                hid_elevation_windows.uninstall_task(
                    request_sid=SID,
                    owner_root=root / "protected",
                    program_files_root=root,
                )
            delete.assert_not_called()

    def test_uninstall_removes_a_verified_orphaned_helper_without_manifest(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            owner_root = root / "protected"
            helper_bytes = b"orphaned-helper"
            source = root / "source-helper.exe"
            source.write_bytes(helper_bytes)
            digest = hid_elevation_windows._sha256(source)
            target = owner_root / hid_elevation_windows.helper_relative_path(digest)
            target.parent.mkdir(parents=True)
            target.write_bytes(helper_bytes)

            with mock.patch.object(
                hid_elevation_windows, "is_process_elevated", return_value=True
            ), mock.patch.object(
                hid_elevation_windows, "current_user_sid", return_value=SID
            ), mock.patch.object(
                hid_elevation_windows, "_read_registered_task", return_value=None
            ):
                hid_elevation_windows.uninstall_task(
                    request_sid=SID,
                    owner_root=owner_root,
                    program_files_root=root,
                    _read_acl=self._acl,
                )

            self.assertFalse(target.exists())

    def test_uninstall_preserves_an_orphan_whose_hash_does_not_match_its_path(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            owner_root = root / "protected"
            target = owner_root / hid_elevation_windows.helper_relative_path(
                "a" * 64
            )
            target.parent.mkdir(parents=True)
            target.write_bytes(b"not-the-declared-helper")

            with mock.patch.object(
                hid_elevation_windows, "is_process_elevated", return_value=True
            ), mock.patch.object(
                hid_elevation_windows, "current_user_sid", return_value=SID
            ), mock.patch.object(
                hid_elevation_windows, "_read_registered_task", return_value=None
            ):
                with self.assertRaises(
                    hid_elevation_windows.HidElevationError
                ) as ctx:
                    hid_elevation_windows.uninstall_task(
                        request_sid=SID,
                        owner_root=owner_root,
                        program_files_root=root,
                        _read_acl=self._acl,
                    )

            self.assertEqual(str(ctx.exception), "protected_helper_hash_mismatch")
            self.assertTrue(target.exists())

    def test_uninstall_preserves_an_orphan_from_a_newer_generation(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            owner_root = root / "protected"
            source = root / "source-helper.exe"
            source.write_bytes(b"newer-helper")
            digest = hid_elevation_windows._sha256(source)
            target = owner_root / hid_elevation_windows.helper_relative_path(
                digest,
                generation=hid_elevation_windows.HELPER_GENERATION + 1,
            )
            target.parent.mkdir(parents=True)
            target.write_bytes(source.read_bytes())

            with mock.patch.object(
                hid_elevation_windows, "is_process_elevated", return_value=True
            ), mock.patch.object(
                hid_elevation_windows, "current_user_sid", return_value=SID
            ), mock.patch.object(
                hid_elevation_windows, "_read_registered_task", return_value=None
            ):
                with self.assertRaises(
                    hid_elevation_windows.HidElevationError
                ) as ctx:
                    hid_elevation_windows.uninstall_task(
                        request_sid=SID,
                        owner_root=owner_root,
                        program_files_root=root,
                        _read_acl=self._acl,
                    )

            self.assertEqual(str(ctx.exception), "newer_helper_preserved")
            self.assertTrue(target.exists())

    def test_uninstall_recovers_a_valid_task_when_its_manifest_is_missing(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            owner_root = root / "protected"
            source = root / "source-helper.exe"
            source.write_bytes(b"helper-without-manifest")
            digest = hid_elevation_windows._sha256(source)
            target = owner_root / hid_elevation_windows.helper_relative_path(digest)
            target.parent.mkdir(parents=True)
            target.write_bytes(source.read_bytes())
            task_name = hid_elevation_windows.task_name_for_sid(SID)
            task = hid_elevation_windows._RegisteredTaskSnapshot(
                hid_elevation_windows.task_definition_xml(
                    target, SID, task_name=task_name
                ),
                hid_elevation_windows.task_security_sddl(SID),
            )
            tasks = {task_name: task}

            def read_task(name):
                if name == hid_elevation_windows.LEGACY_TASK_NAME:
                    return None
                return tasks.get(name)

            def delete_task(name, *, missing_ok):
                self.assertFalse(missing_ok)
                tasks.pop(name, None)

            with mock.patch.object(
                hid_elevation_windows, "is_process_elevated", return_value=True
            ), mock.patch.object(
                hid_elevation_windows, "current_user_sid", return_value=SID
            ), mock.patch.object(
                hid_elevation_windows, "_apply_path_security"
            ), mock.patch.object(
                hid_elevation_windows,
                "_read_registered_task",
                side_effect=read_task,
            ), mock.patch.object(
                hid_elevation_windows, "_delete_task", side_effect=delete_task
            ):
                hid_elevation_windows.uninstall_task(
                    request_sid=SID,
                    owner_root=owner_root,
                    program_files_root=root,
                    _read_acl=self._acl,
                )

            self.assertNotIn(task_name, tasks)
            self.assertFalse(target.exists())

    def test_uninstall_recovers_invalid_manifest_variants_from_the_task(self):
        variants = {
            "bad-json": b"{not-json",
            "missing-field": json.dumps(
                {
                    key: value
                    for key, value in hid_elevation_windows._build_manifest(
                        SID, "a" * 64
                    ).as_dict().items()
                    if key != "task_name"
                }
            ).encode("utf-8"),
            "wrong-sid": json.dumps(
                {
                    **hid_elevation_windows._build_manifest(
                        SID, "a" * 64
                    ).as_dict(),
                    "owner_sid": "S-1-5-21-111-222-333-1002",
                }
            ).encode("utf-8"),
        }
        for label, manifest_bytes in variants.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                owner_root = root / "protected"
                source = root / "source-helper.exe"
                source.write_bytes(b"helper")
                digest = hid_elevation_windows._sha256(source)
                target = owner_root / hid_elevation_windows.helper_relative_path(
                    digest
                )
                target.parent.mkdir(parents=True)
                target.write_bytes(source.read_bytes())
                manifest_path = owner_root / hid_elevation_windows.MANIFEST_FILENAME
                manifest_path.write_bytes(manifest_bytes)
                task_name = hid_elevation_windows.task_name_for_sid(SID)
                tasks = {task_name: self._task_snapshot(target)}

                def read_task(name):
                    if name == hid_elevation_windows.LEGACY_TASK_NAME:
                        return None
                    return tasks.get(name)

                def delete_task(name, *, missing_ok):
                    self.assertFalse(missing_ok)
                    tasks.pop(name, None)

                with mock.patch.object(
                    hid_elevation_windows, "is_process_elevated", return_value=True
                ), mock.patch.object(
                    hid_elevation_windows, "current_user_sid", return_value=SID
                ), mock.patch.object(
                    hid_elevation_windows,
                    "_read_registered_task",
                    side_effect=read_task,
                ), mock.patch.object(
                    hid_elevation_windows, "_delete_task", side_effect=delete_task
                ):
                    hid_elevation_windows.uninstall_task(
                        request_sid=SID,
                        owner_root=owner_root,
                        program_files_root=root,
                        _read_acl=self._acl,
                    )

                self.assertNotIn(task_name, tasks)
                self.assertFalse(target.exists())
                self.assertFalse(manifest_path.exists())

    def test_uninstall_deletes_damaged_fixed_task_variants(self):
        for label in ("malformed_xml", "invalid_acl", "outside_command"):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                owner_root = root / "protected"
                source = root / "source-helper.exe"
                source.write_bytes(b"helper")
                digest = hid_elevation_windows._sha256(source)
                manifest = hid_elevation_windows._build_manifest(SID, digest)
                target = owner_root / Path(manifest.helper_relative_path)
                target.parent.mkdir(parents=True)
                target.write_bytes(source.read_bytes())
                manifest_path = owner_root / hid_elevation_windows.MANIFEST_FILENAME
                manifest_path.write_text(
                    json.dumps(manifest.as_dict()), encoding="utf-8"
                )
                valid_xml = hid_elevation_windows.task_definition_xml(
                    target, SID, task_name=manifest.task_name
                )
                if label == "malformed_xml":
                    previous = hid_elevation_windows._RegisteredTaskSnapshot(
                        "<Task>", hid_elevation_windows.task_security_sddl(SID)
                    )
                elif label == "invalid_acl":
                    previous = hid_elevation_windows._RegisteredTaskSnapshot(
                        valid_xml, "D:(A;;FA;;;WD)"
                    )
                else:
                    previous = hid_elevation_windows._RegisteredTaskSnapshot(
                        hid_elevation_windows.task_definition_xml(
                            root / "outside.exe",
                            SID,
                            task_name=manifest.task_name,
                        ),
                        hid_elevation_windows.task_security_sddl(SID),
                    )
                tasks = {manifest.task_name: previous}

                def read_task(name):
                    if name == hid_elevation_windows.LEGACY_TASK_NAME:
                        return None
                    return tasks.get(name)

                def delete_task(name, *, missing_ok):
                    self.assertFalse(missing_ok)
                    tasks.pop(name, None)

                with mock.patch.object(
                    hid_elevation_windows, "is_process_elevated", return_value=True
                ), mock.patch.object(
                    hid_elevation_windows, "current_user_sid", return_value=SID
                ), mock.patch.object(
                    hid_elevation_windows,
                    "_read_registered_task",
                    side_effect=read_task,
                ), mock.patch.object(
                    hid_elevation_windows,
                    "_delete_task",
                    side_effect=delete_task,
                ):
                    hid_elevation_windows.uninstall_task(
                        request_sid=SID,
                        owner_root=owner_root,
                        program_files_root=root,
                        _read_acl=self._acl,
                    )

                self.assertNotIn(manifest.task_name, tasks)
                self.assertFalse(target.exists())
                self.assertFalse(manifest_path.exists())

    def test_uninstall_removes_all_generations_with_the_active_helper_last(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            owner_root = root / "protected"
            old_source = root / "old-helper.exe"
            old_source.write_bytes(b"old-helper")
            old_digest = hid_elevation_windows._sha256(old_source)
            old_target = owner_root / hid_elevation_windows.helper_relative_path(
                old_digest,
                generation=max(1, hid_elevation_windows.HELPER_GENERATION - 1),
            )
            old_target.parent.mkdir(parents=True)
            old_target.write_bytes(old_source.read_bytes())
            active_source = root / "active-helper.exe"
            active_source.write_bytes(b"active-helper")
            active_digest = hid_elevation_windows._sha256(active_source)
            active_target = owner_root / hid_elevation_windows.helper_relative_path(
                active_digest
            )
            active_target.parent.mkdir(parents=True)
            active_target.write_bytes(active_source.read_bytes())
            manifest_path = owner_root / hid_elevation_windows.MANIFEST_FILENAME
            manifest_path.write_text("{broken", encoding="utf-8")
            task_name = hid_elevation_windows.task_name_for_sid(SID)
            tasks = {task_name: self._task_snapshot(active_target)}
            removal_order = []
            real_remove = hid_elevation_windows._remove_helper_generation

            def read_task(name):
                if name == hid_elevation_windows.LEGACY_TASK_NAME:
                    return None
                return tasks.get(name)

            def delete_task(name, *, missing_ok):
                self.assertFalse(missing_ok)
                tasks.pop(name, None)

            def remove_target(path, *, owner_root):
                removal_order.append(Path(path))
                real_remove(Path(path), owner_root=owner_root)

            with mock.patch.object(
                hid_elevation_windows, "is_process_elevated", return_value=True
            ), mock.patch.object(
                hid_elevation_windows, "current_user_sid", return_value=SID
            ), mock.patch.object(
                hid_elevation_windows, "_read_registered_task", side_effect=read_task
            ), mock.patch.object(
                hid_elevation_windows, "_delete_task", side_effect=delete_task
            ), mock.patch.object(
                hid_elevation_windows,
                "_remove_helper_generation",
                side_effect=remove_target,
            ):
                hid_elevation_windows.uninstall_task(
                    request_sid=SID,
                    owner_root=owner_root,
                    program_files_root=root,
                    _read_acl=self._acl,
                )

            self.assertEqual(removal_order, [old_target, active_target])
            self.assertNotIn(task_name, tasks)
            self.assertFalse(manifest_path.exists())

    def test_task_query_failure_after_delete_restores_task_and_manifest(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            owner_root = root / "protected"
            source = root / "source-helper.exe"
            source.write_bytes(b"helper")
            digest = hid_elevation_windows._sha256(source)
            manifest = hid_elevation_windows._build_manifest(SID, digest)
            target = owner_root / Path(manifest.helper_relative_path)
            target.parent.mkdir(parents=True)
            target.write_bytes(source.read_bytes())
            manifest_path = owner_root / hid_elevation_windows.MANIFEST_FILENAME
            manifest_path.write_text(json.dumps(manifest.as_dict()), encoding="utf-8")
            manifest_bytes = manifest_path.read_bytes()
            task_name = manifest.task_name
            task = self._task_snapshot(target)
            tasks = {task_name: task}
            task_reads = 0
            remove_target = mock.Mock()

            def read_task(name):
                nonlocal task_reads
                if name == hid_elevation_windows.LEGACY_TASK_NAME:
                    return None
                task_reads += 1
                if task_reads == 2:
                    raise OSError("simulated task query failure")
                return tasks.get(name)

            def delete_task(name, *, missing_ok):
                self.assertFalse(missing_ok)
                tasks.pop(name, None)

            def register_task(name, xml, sddl):
                tasks[name] = hid_elevation_windows._RegisteredTaskSnapshot(
                    xml, sddl
                )

            with mock.patch.object(
                hid_elevation_windows, "is_process_elevated", return_value=True
            ), mock.patch.object(
                hid_elevation_windows, "current_user_sid", return_value=SID
            ), mock.patch.object(
                hid_elevation_windows, "_apply_path_security"
            ), mock.patch.object(
                hid_elevation_windows, "_read_registered_task", side_effect=read_task
            ), mock.patch.object(
                hid_elevation_windows, "_delete_task", side_effect=delete_task
            ), mock.patch.object(
                hid_elevation_windows, "_register_task", side_effect=register_task
            ), mock.patch.object(
                hid_elevation_windows,
                "_remove_helper_generation",
                remove_target,
            ):
                with self.assertRaises(hid_elevation_windows.HidElevationError):
                    hid_elevation_windows.uninstall_task(
                        request_sid=SID,
                        owner_root=owner_root,
                        program_files_root=root,
                        _read_acl=self._acl,
                    )

            self.assertEqual(tasks.get(task_name), task)
            self.assertEqual(manifest_path.read_bytes(), manifest_bytes)
            self.assertTrue(target.is_file())
            remove_target.assert_not_called()

    def test_uninstall_keeps_helper_when_task_removal_fails(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            owner_root = root / "protected"
            owner_root.mkdir()
            digest = hid_elevation_windows._sha256(Path(__file__))
            manifest = hid_elevation_windows._build_manifest(SID, digest)
            target = owner_root / Path(manifest.helper_relative_path)
            target.parent.mkdir(parents=True)
            target.write_bytes(Path(__file__).read_bytes())
            manifest_path = owner_root / hid_elevation_windows.MANIFEST_FILENAME
            manifest_path.write_text(
                __import__("json").dumps(manifest.as_dict()), encoding="utf-8"
            )
            task = hid_elevation_windows._RegisteredTaskSnapshot(
                hid_elevation_windows.task_definition_xml(
                    target, SID, task_name=manifest.task_name
                ),
                hid_elevation_windows.task_security_sddl(SID),
            )
            with mock.patch.object(
                hid_elevation_windows, "is_process_elevated", return_value=True
            ), mock.patch.object(
                hid_elevation_windows, "current_user_sid", return_value=SID
            ), mock.patch.object(
                hid_elevation_windows, "_apply_path_security"
            ), mock.patch.object(
                hid_elevation_windows,
                "_read_registered_task",
                side_effect=lambda name: (
                    None
                    if name == hid_elevation_windows.LEGACY_TASK_NAME
                    else task
                ),
            ), mock.patch.object(
                hid_elevation_windows,
                "_delete_task",
                side_effect=hid_elevation_windows.HidElevationError(
                    "hid_helper_task_removal_failed"
                ),
            ):
                with self.assertRaises(hid_elevation_windows.HidElevationError):
                    hid_elevation_windows.uninstall_task(
                        request_sid=SID,
                        owner_root=owner_root,
                        program_files_root=root,
                        _read_acl=self._acl,
                    )

            self.assertTrue(target.is_file())
            self.assertTrue(manifest_path.is_file())

    def test_uninstall_keeps_permission_disabled_when_helper_cleanup_fails(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            owner_root = root / "protected"
            owner_root.mkdir()
            source = root / "source-helper.exe"
            source.write_bytes(b"helper")
            digest = hid_elevation_windows._sha256(source)
            manifest = hid_elevation_windows._build_manifest(SID, digest)
            target = owner_root / Path(manifest.helper_relative_path)
            target.parent.mkdir(parents=True)
            target.write_bytes(source.read_bytes())
            manifest_path = owner_root / hid_elevation_windows.MANIFEST_FILENAME
            manifest_path.write_text(
                __import__("json").dumps(manifest.as_dict()), encoding="utf-8"
            )
            task = hid_elevation_windows._RegisteredTaskSnapshot(
                hid_elevation_windows.task_definition_xml(
                    target, SID, task_name=manifest.task_name
                ),
                hid_elevation_windows.task_security_sddl(SID),
            )
            tasks = {manifest.task_name: task}

            def read_task(name):
                if name == hid_elevation_windows.LEGACY_TASK_NAME:
                    return None
                return tasks.get(name)

            def delete_task(name, *, missing_ok):
                self.assertFalse(missing_ok)
                tasks.pop(name, None)

            def register_task(name, xml, sddl):
                tasks[name] = hid_elevation_windows._RegisteredTaskSnapshot(
                    xml, sddl
                )

            with mock.patch.object(
                hid_elevation_windows, "is_process_elevated", return_value=True
            ), mock.patch.object(
                hid_elevation_windows, "current_user_sid", return_value=SID
            ), mock.patch.object(
                hid_elevation_windows, "_apply_path_security"
            ), mock.patch.object(
                hid_elevation_windows,
                "_read_registered_task",
                side_effect=read_task,
            ), mock.patch.object(
                hid_elevation_windows, "_delete_task", side_effect=delete_task
            ), mock.patch.object(
                hid_elevation_windows, "_register_task", side_effect=register_task
            ), mock.patch.object(
                hid_elevation_windows,
                "_remove_helper_generation",
                side_effect=hid_elevation_windows.HidElevationError(
                    "protected_helper_removal_failed"
                ),
            ):
                with self.assertRaises(
                    hid_elevation_windows.HidElevationError
                ) as ctx:
                    hid_elevation_windows.uninstall_task(
                        request_sid=SID,
                        owner_root=owner_root,
                        program_files_root=root,
                        _read_acl=self._acl,
                    )

            self.assertEqual(str(ctx.exception), "protected_helper_cleanup_pending")
            self.assertTrue(target.exists())
            self.assertFalse(manifest_path.exists())
            self.assertNotIn(manifest.task_name, tasks)

    def test_uninstall_succeeds_when_an_injected_runtime_cache_remains(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            owner_root = root / "protected"
            owner_root.mkdir()
            digest = hid_elevation_windows._sha256(Path(__file__))
            manifest = hid_elevation_windows._build_manifest(SID, digest)
            target = owner_root / Path(manifest.helper_relative_path)
            target.parent.mkdir(parents=True)
            target.write_bytes(Path(__file__).read_bytes())
            manifest_path = owner_root / hid_elevation_windows.MANIFEST_FILENAME
            manifest_path.write_text(
                __import__("json").dumps(manifest.as_dict()), encoding="utf-8"
            )
            runtime_cache = owner_root / "runtime" / "loaded-gadget"
            runtime_cache.mkdir(parents=True)
            cached_dll = runtime_cache / "remote-mic-rc003-gadget.dll"
            cached_dll.write_bytes(b"still loaded by WUDFHost")
            task = hid_elevation_windows._RegisteredTaskSnapshot(
                hid_elevation_windows.task_definition_xml(
                    target, SID, task_name=manifest.task_name
                ),
                hid_elevation_windows.task_security_sddl(SID),
            )
            tasks = {manifest.task_name: task}

            def read_task(name):
                if name == hid_elevation_windows.LEGACY_TASK_NAME:
                    return None
                return tasks.get(name)

            def delete_task(name, *, missing_ok):
                self.assertFalse(missing_ok)
                tasks.pop(name, None)

            with mock.patch.object(
                hid_elevation_windows, "is_process_elevated", return_value=True
            ), mock.patch.object(
                hid_elevation_windows, "current_user_sid", return_value=SID
            ), mock.patch.object(
                hid_elevation_windows, "_read_registered_task", side_effect=read_task
            ), mock.patch.object(
                hid_elevation_windows, "_delete_task", side_effect=delete_task
            ):
                hid_elevation_windows.uninstall_task(
                    request_sid=SID,
                    owner_root=owner_root,
                    program_files_root=root,
                    _read_acl=self._acl,
                )

            self.assertNotIn(manifest.task_name, tasks)
            self.assertFalse(manifest_path.exists())
            self.assertFalse(target.exists())
            self.assertTrue(cached_dll.exists())

    def test_registered_injector_rejects_changed_host_before_running_task(self):
        runner = mock.Mock()
        with self.assertRaises(hid_elevation_windows.HidElevationError) as ctx:
            hid_elevation_windows.run_registered_injector(
                2468,
                _run_registered_task=runner,
                _host_pid=lambda: 9999,
            )
        self.assertEqual(str(ctx.exception), "hid_helper_host_changed")
        runner.assert_not_called()

    def test_registered_injector_runs_only_the_sid_derived_task(self):
        runner = mock.Mock()
        with mock.patch.object(
            hid_elevation_windows,
            "inspect_installed_helper",
            return_value=hid_elevation_windows.HidHelperState(True),
        ), mock.patch.object(
            hid_elevation_windows, "current_user_sid", return_value=SID
        ):
            hid_elevation_windows.run_registered_injector(
                2468,
                _run_registered_task=runner,
                _host_pid=lambda: 2468,
            )
        runner.assert_called_once_with(hid_elevation_windows.task_name_for_sid(SID))


class RemovalInspectionTests(unittest.TestCase):
    def test_runtime_cache_alone_does_not_request_an_unnecessary_uac(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            owner_root = hid_elevation_windows.protected_owner_root(
                SID, program_files_root=root
            )
            runtime_cache = owner_root / "runtime" / "loaded-gadget"
            runtime_cache.mkdir(parents=True)
            (runtime_cache / "remote-mic-rc003-gadget.dll").write_bytes(
                b"still loaded"
            )

            requires_removal = (
                hid_elevation_windows._installation_requires_elevated_removal(
                    SID,
                    program_files_root=root,
                    _read_task=lambda _name: None,
                )
            )

        self.assertFalse(requires_removal)

    def test_orphaned_generation_helper_still_requests_elevated_cleanup(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            owner_root = hid_elevation_windows.protected_owner_root(
                SID, program_files_root=root
            )
            target = owner_root / hid_elevation_windows.helper_relative_path(
                "a" * 64
            )
            target.parent.mkdir(parents=True)
            target.write_bytes(b"orphan")

            requires_removal = (
                hid_elevation_windows._installation_requires_elevated_removal(
                    SID,
                    program_files_root=root,
                    _read_task=lambda _name: None,
                )
            )

        self.assertTrue(requires_removal)

    def test_legacy_helper_without_its_task_still_requests_cleanup(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = hid_elevation_windows.legacy_protected_helper_path(
                program_files_root=root
            )
            target.parent.mkdir(parents=True)
            target.write_bytes(b"legacy-helper")

            requires_removal = (
                hid_elevation_windows._installation_requires_elevated_removal(
                    SID,
                    program_files_root=root,
                    _read_task=lambda _name: None,
                )
            )

        self.assertTrue(requires_removal)

    def test_another_users_legacy_task_is_not_this_users_residual(self):
        other_sid = "S-1-5-21-111-222-333-1002"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = hid_elevation_windows.legacy_protected_helper_path(
                program_files_root=root
            )
            target.parent.mkdir(parents=True)
            target.write_bytes(b"legacy-helper")
            snapshot = hid_elevation_windows._RegisteredTaskSnapshot(
                xml_text=hid_elevation_windows.task_definition_xml(
                    target,
                    other_sid,
                    task_name=hid_elevation_windows.LEGACY_TASK_NAME,
                ),
                security_sddl="",
            )

            def read_task(name):
                if name == hid_elevation_windows.LEGACY_TASK_NAME:
                    return snapshot
                return None

            requires_removal = (
                hid_elevation_windows._installation_requires_elevated_removal(
                    SID,
                    program_files_root=root,
                    _read_task=read_task,
                )
            )

        self.assertFalse(requires_removal)

    def test_inert_manifest_alone_does_not_request_uac(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            owner_root = hid_elevation_windows.protected_owner_root(
                SID, program_files_root=root
            )
            owner_root.mkdir(parents=True)
            (owner_root / hid_elevation_windows.MANIFEST_FILENAME).write_text(
                "{stale", encoding="utf-8"
            )

            requires_removal = (
                hid_elevation_windows._installation_requires_elevated_removal(
                    SID,
                    program_files_root=root,
                    _read_task=lambda _name: None,
                )
            )

        self.assertFalse(requires_removal)


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
                _inspect=lambda: hid_elevation_windows.HidHelperState(
                    False, "helper_manifest_missing"
                ),
                _can_self_elevate=lambda: True,
                _current_sid=lambda: SID,
            )

        self.assertEqual(state, hid_elevation_windows.HidHelperState(False, "uac_cancelled"))

    def test_success_is_rechecked_after_the_elevated_process_exits(self):
        with tempfile.TemporaryDirectory() as raw:
            helper = Path(raw) / hid_elevation_windows.HELPER_EXE_NAME
            helper.write_bytes(b"helper")
            inspected = hid_elevation_windows.HidHelperState(True)
            launch = mock.Mock(return_value=hid_elevation_windows.HELPER_EXIT_OK)
            inspect = mock.Mock(
                side_effect=[
                    hid_elevation_windows.HidHelperState(
                        False, "helper_manifest_missing"
                    ),
                    inspected,
                ]
            )

            state = hid_elevation_windows.request_install_elevation(
                helper_path=helper,
                timeout_seconds=12.0,
                _launch=launch,
                _inspect=inspect,
                _can_self_elevate=lambda: True,
                _current_sid=lambda: SID,
            )

        self.assertEqual(state, inspected)
        launch.assert_called_once_with(
            helper,
            f"{hid_elevation_windows.INSTALL_FLAG} {hid_elevation_windows.REQUEST_SID_FLAG} {SID}",
            12.0,
        )
        self.assertEqual(inspect.call_count, 2)

    def test_success_waits_for_task_scheduler_visibility(self):
        with tempfile.TemporaryDirectory() as raw:
            helper = Path(raw) / hid_elevation_windows.HELPER_EXE_NAME
            helper.write_bytes(b"helper")
            ready = hid_elevation_windows.HidHelperState(True)
            inspect = mock.Mock(
                side_effect=[
                    hid_elevation_windows.HidHelperState(
                        False, "helper_manifest_missing"
                    ),
                    hid_elevation_windows.HidHelperState(
                        False, "hid_helper_task_missing"
                    ),
                    hid_elevation_windows.HidHelperState(
                        False, "hid_helper_task_query_failed"
                    ),
                    ready,
                ]
            )
            sleeps = []

            state = hid_elevation_windows.request_install_elevation(
                helper_path=helper,
                _launch=lambda _path, _args, _timeout: (
                    hid_elevation_windows.HELPER_EXIT_OK
                ),
                _inspect=inspect,
                _can_self_elevate=lambda: True,
                _current_sid=lambda: SID,
                _sleep=sleeps.append,
            )

        self.assertEqual(state, ready)
        self.assertEqual(inspect.call_count, 4)
        self.assertEqual(
            sleeps,
            [
                hid_elevation_windows._TASK_REGISTRATION_SETTLE_POLL_SECONDS,
                hid_elevation_windows._TASK_REGISTRATION_SETTLE_POLL_SECONDS,
            ],
        )

    def test_success_does_not_retry_a_nontransient_invalid_task(self):
        with tempfile.TemporaryDirectory() as raw:
            helper = Path(raw) / hid_elevation_windows.HELPER_EXE_NAME
            helper.write_bytes(b"helper")
            invalid = hid_elevation_windows.HidHelperState(
                False, "hid_helper_task_invalid"
            )
            inspect = mock.Mock(
                side_effect=[
                    hid_elevation_windows.HidHelperState(
                        False, "helper_manifest_missing"
                    ),
                    invalid,
                ]
            )
            sleeps = []

            state = hid_elevation_windows.request_install_elevation(
                helper_path=helper,
                _launch=lambda _path, _args, _timeout: (
                    hid_elevation_windows.HELPER_EXIT_OK
                ),
                _inspect=inspect,
                _can_self_elevate=lambda: True,
                _current_sid=lambda: SID,
                _sleep=sleeps.append,
            )

        self.assertEqual(state, invalid)
        self.assertEqual(inspect.call_count, 2)
        self.assertEqual(sleeps, [])

    def test_install_rechecks_final_state_even_when_the_launcher_reports_failure(self):
        with tempfile.TemporaryDirectory() as raw:
            helper = Path(raw) / hid_elevation_windows.HELPER_EXE_NAME
            helper.write_bytes(b"helper")
            inspect = mock.Mock(
                side_effect=[
                    hid_elevation_windows.HidHelperState(
                        False, "helper_manifest_missing"
                    ),
                    hid_elevation_windows.HidHelperState(True),
                ]
            )

            state = hid_elevation_windows.request_install_elevation(
                helper_path=helper,
                _launch=mock.Mock(
                    side_effect=hid_elevation_windows.HidElevationError(
                        "hid_helper_setup_wait_failed"
                    )
                ),
                _inspect=inspect,
                _can_self_elevate=lambda: True,
                _current_sid=lambda: SID,
            )

        self.assertEqual(state, hid_elevation_windows.HidHelperState(True))
        self.assertEqual(inspect.call_count, 2)

    def test_cleanup_failure_keeps_the_verified_helper_available(self):
        with tempfile.TemporaryDirectory() as raw:
            helper = Path(raw) / hid_elevation_windows.HELPER_EXE_NAME
            helper.write_bytes(b"helper")
            inspect = mock.Mock(
                side_effect=[
                    hid_elevation_windows.HidHelperState(
                        False, "protected_helper_outdated"
                    ),
                    hid_elevation_windows.HidHelperState(True),
                ]
            )

            state = hid_elevation_windows.request_install_elevation(
                helper_path=helper,
                _launch=lambda _path, _args, _timeout: (
                    hid_elevation_windows.HELPER_EXIT_VALIDATION_FAILED
                ),
                _inspect=inspect,
                _can_self_elevate=lambda: True,
                _current_sid=lambda: SID,
            )

        self.assertEqual(
            state,
            hid_elevation_windows.HidHelperState(
                True, "helper_cleanup_pending"
            ),
        )

    def test_install_reports_the_sanitized_failure_stage(self):
        with tempfile.TemporaryDirectory() as raw:
            helper = Path(raw) / hid_elevation_windows.HELPER_EXE_NAME
            helper.write_bytes(b"helper")

            state = hid_elevation_windows.request_install_elevation(
                helper_path=helper,
                _launch=lambda _path, _args, _timeout: (
                    hid_elevation_windows.HELPER_EXIT_TASK_ACL_INVALID
                ),
                _inspect=lambda: hid_elevation_windows.HidHelperState(
                    False, "helper_manifest_missing"
                ),
                _can_self_elevate=lambda: True,
                _current_sid=lambda: SID,
            )

        self.assertEqual(
            state,
            hid_elevation_windows.HidHelperState(
                False, "hid_helper_task_acl_invalid"
            ),
        )

    def test_install_decodes_every_sanitized_helper_exit(self):
        with tempfile.TemporaryDirectory() as raw:
            helper = Path(raw) / hid_elevation_windows.HELPER_EXE_NAME
            helper.write_bytes(b"helper")

            for exit_code, detail in hid_elevation_windows._HELPER_EXIT_ERROR_DETAILS.items():
                with self.subTest(exit_code=exit_code, detail=detail):
                    state = hid_elevation_windows.request_install_elevation(
                        helper_path=helper,
                        _launch=lambda _path, _args, _timeout, code=exit_code: code,
                        _inspect=lambda: hid_elevation_windows.HidHelperState(
                            False, "helper_manifest_missing"
                        ),
                        _can_self_elevate=lambda: True,
                        _current_sid=lambda: SID,
                    )

                    self.assertEqual(
                        state,
                        hid_elevation_windows.HidHelperState(False, detail),
                    )

    def test_install_reports_a_preserved_future_helper(self):
        with tempfile.TemporaryDirectory() as raw:
            helper = Path(raw) / hid_elevation_windows.HELPER_EXE_NAME
            helper.write_bytes(b"helper")

            state = hid_elevation_windows.request_install_elevation(
                helper_path=helper,
                _launch=lambda _path, _args, _timeout: (
                    hid_elevation_windows.HELPER_EXIT_NEWER_PRESERVED
                ),
                _inspect=lambda: hid_elevation_windows.HidHelperState(
                    False, "helper_manifest_invalid"
                ),
                _can_self_elevate=lambda: True,
                _current_sid=lambda: SID,
            )

        self.assertEqual(
            state,
            hid_elevation_windows.HidHelperState(
                False, "newer_helper_preserved"
            ),
        )

    def test_matching_helper_returns_without_uac(self):
        with tempfile.TemporaryDirectory() as raw:
            helper = Path(raw) / hid_elevation_windows.HELPER_EXE_NAME
            helper.write_bytes(b"helper")
            ready = hid_elevation_windows.HidHelperState(True)
            launch = mock.Mock()

            state = hid_elevation_windows.request_install_elevation(
                helper_path=helper,
                _launch=launch,
                _inspect=lambda: ready,
                _can_self_elevate=lambda: True,
                _current_sid=lambda: SID,
            )

        self.assertEqual(state, ready)
        launch.assert_not_called()

    def test_invalid_newer_helper_returns_without_uac(self):
        with tempfile.TemporaryDirectory() as raw:
            helper = Path(raw) / hid_elevation_windows.HELPER_EXE_NAME
            helper.write_bytes(b"helper")
            newer = hid_elevation_windows.HidHelperState(
                False, "newer_helper_invalid"
            )
            launch = mock.Mock()

            state = hid_elevation_windows.request_install_elevation(
                helper_path=helper,
                _launch=launch,
                _inspect=lambda: newer,
                _can_self_elevate=lambda: True,
                _current_sid=lambda: SID,
            )

        self.assertEqual(state, newer)
        launch.assert_not_called()

    def test_standard_account_is_rejected_before_uac(self):
        with tempfile.TemporaryDirectory() as raw:
            helper = Path(raw) / hid_elevation_windows.HELPER_EXE_NAME
            helper.write_bytes(b"helper")
            launch = mock.Mock()

            state = hid_elevation_windows.request_install_elevation(
                helper_path=helper,
                _launch=launch,
                _inspect=lambda: hid_elevation_windows.HidHelperState(
                    False, "helper_manifest_missing"
                ),
                _can_self_elevate=lambda: False,
                _current_sid=lambda: SID,
            )

        self.assertEqual(
            state.detail, "current_account_cannot_self_elevate"
        )
        launch.assert_not_called()

    def test_uninstall_uses_the_bundled_helper_and_fixed_flag(self):
        with tempfile.TemporaryDirectory() as raw:
            helper = Path(raw) / hid_elevation_windows.HELPER_EXE_NAME
            helper.write_bytes(b"helper")
            launch = mock.Mock(return_value=hid_elevation_windows.HELPER_EXIT_OK)
            requires_removal = mock.Mock(side_effect=[True, False])

            state = hid_elevation_windows.request_uninstall_elevation(
                helper_path=helper,
                timeout_seconds=9.0,
                _launch=launch,
                _can_self_elevate=lambda: True,
                _current_sid=lambda: SID,
                _requires_removal=requires_removal,
            )

        self.assertEqual(state, hid_elevation_windows.HidHelperState(True))
        launch.assert_called_once_with(
            helper,
            f"{hid_elevation_windows.UNINSTALL_FLAG} {hid_elevation_windows.REQUEST_SID_FLAG} {SID}",
            9.0,
        )

    def test_uninstall_uac_cancellation_is_reported_without_claiming_success(self):
        with tempfile.TemporaryDirectory() as raw:
            helper = Path(raw) / hid_elevation_windows.HELPER_EXE_NAME
            helper.write_bytes(b"helper")

            def cancel(_path, _args, _timeout):
                raise hid_elevation_windows.HidElevationError("uac_cancelled")

            state = hid_elevation_windows.request_uninstall_elevation(
                helper_path=helper,
                _launch=cancel,
                _can_self_elevate=lambda: True,
                _current_sid=lambda: SID,
                _requires_removal=lambda _sid: True,
            )

        self.assertEqual(
            state,
            hid_elevation_windows.HidHelperState(False, "uac_cancelled"),
        )

    def test_uninstall_decodes_every_sanitized_helper_exit(self):
        with tempfile.TemporaryDirectory() as raw:
            helper = Path(raw) / hid_elevation_windows.HELPER_EXE_NAME
            helper.write_bytes(b"helper")

            for exit_code, detail in hid_elevation_windows._HELPER_EXIT_ERROR_DETAILS.items():
                with self.subTest(exit_code=exit_code, detail=detail):
                    state = hid_elevation_windows.request_uninstall_elevation(
                        helper_path=helper,
                        _launch=lambda _path, _args, _timeout, code=exit_code: code,
                        _can_self_elevate=lambda: True,
                        _current_sid=lambda: SID,
                        _requires_removal=lambda _sid: True,
                    )

                    self.assertEqual(
                        state,
                        hid_elevation_windows.HidHelperState(False, detail),
                    )

    def test_uninstall_preserves_a_known_future_helper_without_uac(self):
        with tempfile.TemporaryDirectory() as raw:
            helper = Path(raw) / hid_elevation_windows.HELPER_EXE_NAME
            helper.write_bytes(b"helper")
            launch = mock.Mock()

            state = hid_elevation_windows.request_uninstall_elevation(
                helper_path=helper,
                _launch=launch,
                _current_sid=lambda: SID,
                _inspect=lambda: hid_elevation_windows.HidHelperState(
                    True, "helper_available_newer_generation"
                ),
                _requires_removal=lambda _sid: True,
            )

        self.assertEqual(
            state,
            hid_elevation_windows.HidHelperState(
                True, "helper_preserved_newer_contract"
            ),
        )
        launch.assert_not_called()

    def test_uninstall_maps_the_future_preserved_exit_to_success(self):
        with tempfile.TemporaryDirectory() as raw:
            helper = Path(raw) / hid_elevation_windows.HELPER_EXE_NAME
            helper.write_bytes(b"helper")

            state = hid_elevation_windows.request_uninstall_elevation(
                helper_path=helper,
                _launch=lambda _path, _args, _timeout: (
                    hid_elevation_windows.HELPER_EXIT_NEWER_PRESERVED
                ),
                _can_self_elevate=lambda: True,
                _current_sid=lambda: SID,
                _inspect=lambda: hid_elevation_windows.HidHelperState(
                    False, "helper_manifest_invalid"
                ),
                _requires_removal=lambda _sid: True,
            )

        self.assertEqual(
            state,
            hid_elevation_windows.HidHelperState(
                True, "helper_preserved_newer_contract"
            ),
        )

    def test_standard_account_without_a_helper_uninstalls_without_uac(self):
        with tempfile.TemporaryDirectory() as raw:
            helper = Path(raw) / hid_elevation_windows.HELPER_EXE_NAME
            helper.write_bytes(b"helper")
            launch = mock.Mock()

            state = hid_elevation_windows.request_uninstall_elevation(
                helper_path=helper,
                _launch=launch,
                _can_self_elevate=lambda: False,
                _current_sid=lambda: SID,
                _requires_removal=lambda _sid: False,
            )

        self.assertEqual(state, hid_elevation_windows.HidHelperState(True))
        launch.assert_not_called()

    def test_uninstall_rechecks_after_an_initial_inspection_error(self):
        with tempfile.TemporaryDirectory() as raw:
            helper = Path(raw) / hid_elevation_windows.HELPER_EXE_NAME
            helper.write_bytes(b"helper")
            launch = mock.Mock()
            requires_removal = mock.Mock(
                side_effect=[
                    hid_elevation_windows.HidElevationError(
                        "hid_helper_task_query_failed"
                    ),
                    False,
                ]
            )

            state = hid_elevation_windows.request_uninstall_elevation(
                helper_path=helper,
                _launch=launch,
                _current_sid=lambda: SID,
                _requires_removal=requires_removal,
            )

        self.assertEqual(state, hid_elevation_windows.HidHelperState(True))
        self.assertEqual(requires_removal.call_count, 2)
        launch.assert_not_called()

    def test_uninstall_invalid_sid_returns_a_stable_error(self):
        state = hid_elevation_windows.request_uninstall_elevation(
            _current_sid=lambda: "not-a-sid",
            _requires_removal=mock.Mock(),
        )

        self.assertEqual(state.detail, "current_user_sid_invalid")

    def test_standard_account_with_a_helper_is_rejected_before_uac(self):
        with tempfile.TemporaryDirectory() as raw:
            helper = Path(raw) / hid_elevation_windows.HELPER_EXE_NAME
            helper.write_bytes(b"helper")
            launch = mock.Mock()

            state = hid_elevation_windows.request_uninstall_elevation(
                helper_path=helper,
                _launch=launch,
                _can_self_elevate=lambda: False,
                _current_sid=lambda: SID,
                _requires_removal=lambda _sid: True,
            )

        self.assertEqual(state.detail, "current_account_cannot_self_elevate")
        launch.assert_not_called()


class HelperMainTests(unittest.TestCase):
    def test_fixed_frozen_helper_uses_a_new_generation(self):
        self.assertGreaterEqual(hid_elevation_windows.HELPER_GENERATION, 8)
        self.assertGreaterEqual(hid_elevation_windows.TASK_CONTRACT_VERSION, 5)

    def test_inject_once_reports_a_stable_failure_stage(self):
        from ovb_rc003 import frida_hid_tap_injector, frida_hid_tap_runtime

        cases = (
            (
                "identity",
                mock.patch.object(
                    hid_elevation_windows,
                    "_validate_helper_execution_identity",
                    side_effect=hid_elevation_windows.HidElevationError(
                        "installed_helper_hash_mismatch"
                    ),
                ),
                None,
                None,
                "hid_helper_execution_identity_invalid",
            ),
            (
                "host",
                mock.patch.object(
                    hid_elevation_windows,
                    "_validate_helper_execution_identity",
                ),
                mock.patch.object(
                    frida_hid_tap_runtime,
                    "find_rc003_hidogatt_host_pid",
                    return_value=None,
                ),
                None,
                "hid_helper_host_unavailable",
            ),
            *(
                (
                    detail,
                    mock.patch.object(
                        hid_elevation_windows,
                        "_validate_helper_execution_identity",
                    ),
                    mock.patch.object(
                        frida_hid_tap_runtime,
                        "find_rc003_hidogatt_host_pid",
                        return_value=4321,
                    ),
                    mock.patch.object(
                        frida_hid_tap_injector,
                        "inject_current_process",
                        side_effect=frida_hid_tap_injector.HidInjectionStageError(
                            detail
                        ),
                    ),
                    detail,
                )
                for detail in hid_elevation_windows._HELPER_RUNTIME_ERROR_EXIT_CODES
                if detail not in {
                    "hid_helper_execution_identity_invalid",
                    "hid_helper_host_unavailable",
                }
            ),
            (
                "unknown_injection",
                mock.patch.object(
                    hid_elevation_windows,
                    "_validate_helper_execution_identity",
                ),
                mock.patch.object(
                    frida_hid_tap_runtime,
                    "find_rc003_hidogatt_host_pid",
                    return_value=4321,
                ),
                mock.patch.object(
                    frida_hid_tap_injector,
                    "inject_current_process",
                    side_effect=OSError("failed"),
                ),
                "hid_helper_injection_failed",
            ),
        )

        for name, identity_patch, host_patch, injector_patch, detail in cases:
            with self.subTest(name=name), identity_patch:
                if host_patch is None:
                    with self.assertRaises(
                        hid_elevation_windows.HidElevationError
                    ) as ctx:
                        hid_elevation_windows._inject_once()
                else:
                    with host_patch:
                        if injector_patch is None:
                            with self.assertRaises(
                                hid_elevation_windows.HidElevationError
                            ) as ctx:
                                hid_elevation_windows._inject_once()
                        else:
                            with injector_patch, self.assertRaises(
                                hid_elevation_windows.HidElevationError
                            ) as ctx:
                                hid_elevation_windows._inject_once()
                self.assertEqual(str(ctx.exception), detail)

    def test_runtime_failure_stage_survives_the_task_boundary(self):
        for detail, expected_exit in (
            hid_elevation_windows._HELPER_RUNTIME_ERROR_EXIT_CODES.items()
        ):
            with self.subTest(detail=detail), mock.patch.object(
                hid_elevation_windows,
                "_inject_once",
                side_effect=hid_elevation_windows.HidElevationError(detail),
            ):
                result = hid_elevation_windows.helper_main(
                    [hid_elevation_windows.INJECT_FLAG]
                )

            self.assertEqual(result, expected_exit)
            self.assertEqual(
                hid_elevation_windows.helper_runtime_detail_from_exit_code(result),
                detail,
            )

    def test_self_check_explicitly_imports_the_operation_lock_dependency(self):
        source = __import__("inspect").getsource(hid_elevation_windows._self_check)
        self.assertIn("single_instance", source)

    def test_self_check_has_no_privileged_or_hid_side_effects(self):
        with mock.patch.object(hid_elevation_windows, "install_task") as install, mock.patch.object(
            hid_elevation_windows, "uninstall_task"
        ) as uninstall, mock.patch.object(
            hid_elevation_windows, "_inject_once"
        ) as inject, mock.patch.object(
            hid_elevation_windows, "_self_check"
        ) as self_check:
            result = hid_elevation_windows.helper_main(
                [hid_elevation_windows.SELF_CHECK_FLAG]
            )

        self.assertEqual(result, hid_elevation_windows.HELPER_EXIT_OK)
        install.assert_not_called()
        uninstall.assert_not_called()
        inject.assert_not_called()
        self_check.assert_called_once_with()

    def test_install_passes_the_pre_elevation_sid_to_the_helper(self):
        with mock.patch.object(hid_elevation_windows, "install_task") as install:
            result = hid_elevation_windows.helper_main(
                [
                    hid_elevation_windows.INSTALL_FLAG,
                    hid_elevation_windows.REQUEST_SID_FLAG,
                    SID,
                ]
            )

        self.assertEqual(result, hid_elevation_windows.HELPER_EXIT_OK)
        install.assert_called_once_with(request_sid=SID)

    def test_future_helper_preservation_has_a_distinct_exit_code(self):
        for flag, operation_name in (
            (hid_elevation_windows.INSTALL_FLAG, "install_task"),
            (hid_elevation_windows.UNINSTALL_FLAG, "uninstall_task"),
        ):
            with self.subTest(flag=flag), mock.patch.object(
                hid_elevation_windows,
                operation_name,
                side_effect=hid_elevation_windows.HidElevationError(
                    "newer_helper_preserved"
                ),
            ):
                result = hid_elevation_windows.helper_main(
                    [flag, hid_elevation_windows.REQUEST_SID_FLAG, SID]
                )

            self.assertEqual(
                result, hid_elevation_windows.HELPER_EXIT_NEWER_PRESERVED
            )

    def test_install_failure_stage_survives_the_elevated_process_boundary(self):
        for expected_exit, detail in hid_elevation_windows._HELPER_EXIT_ERROR_DETAILS.items():
            with self.subTest(detail=detail), mock.patch.object(
                hid_elevation_windows,
                "install_task",
                side_effect=hid_elevation_windows.HidElevationError(detail),
            ):
                result = hid_elevation_windows.helper_main(
                    [
                        hid_elevation_windows.INSTALL_FLAG,
                        hid_elevation_windows.REQUEST_SID_FLAG,
                        SID,
                    ]
                )

            self.assertEqual(result, expected_exit)
            self.assertEqual(
                hid_elevation_windows.helper_setup_detail_from_exit_code(result),
                detail,
            )

    def test_failure_aliases_map_to_their_sanitized_stage(self):
        for detail, expected_exit in hid_elevation_windows._HELPER_ERROR_EXIT_CODES.items():
            with self.subTest(detail=detail), mock.patch.object(
                hid_elevation_windows,
                "install_task",
                side_effect=hid_elevation_windows.HidElevationError(detail),
            ):
                result = hid_elevation_windows.helper_main(
                    [
                        hid_elevation_windows.INSTALL_FLAG,
                        hid_elevation_windows.REQUEST_SID_FLAG,
                        SID,
                    ]
                )

            self.assertEqual(result, expected_exit)

    def test_unknown_install_failure_uses_the_install_stage(self):
        with mock.patch.object(
            hid_elevation_windows,
            "install_task",
            side_effect=hid_elevation_windows.HidElevationError("unknown_failure"),
        ):
            result = hid_elevation_windows.helper_main(
                [
                    hid_elevation_windows.INSTALL_FLAG,
                    hid_elevation_windows.REQUEST_SID_FLAG,
                    SID,
                ]
            )

        self.assertEqual(result, hid_elevation_windows.HELPER_EXIT_INSTALL_FAILED)

    def test_missing_elevation_has_the_dedicated_exit_code(self):
        with mock.patch.object(
            hid_elevation_windows,
            "install_task",
            side_effect=hid_elevation_windows.HidElevationError(
                "administrator_elevation_required"
            ),
        ):
            result = hid_elevation_windows.helper_main(
                [
                    hid_elevation_windows.INSTALL_FLAG,
                    hid_elevation_windows.REQUEST_SID_FLAG,
                    SID,
                ]
            )

        self.assertEqual(result, hid_elevation_windows.HELPER_EXIT_REQUIRES_ADMIN)

    def test_install_permission_error_is_not_misreported_as_missing_elevation(self):
        with mock.patch.object(
            hid_elevation_windows,
            "install_task",
            side_effect=PermissionError("policy denied the protected path"),
        ):
            result = hid_elevation_windows.helper_main(
                [
                    hid_elevation_windows.INSTALL_FLAG,
                    hid_elevation_windows.REQUEST_SID_FLAG,
                    SID,
                ]
            )

        self.assertEqual(result, hid_elevation_windows.HELPER_EXIT_INSTALL_FAILED)

    def test_unknown_uninstall_failure_uses_the_uninstall_stage(self):
        with mock.patch.object(
            hid_elevation_windows,
            "uninstall_task",
            side_effect=hid_elevation_windows.HidElevationError("unknown_failure"),
        ):
            result = hid_elevation_windows.helper_main(
                [
                    hid_elevation_windows.UNINSTALL_FLAG,
                    hid_elevation_windows.REQUEST_SID_FLAG,
                    SID,
                ]
            )

        self.assertEqual(result, hid_elevation_windows.HELPER_EXIT_UNINSTALL_FAILED)

    def test_unknown_self_check_failure_keeps_the_generic_validation_exit(self):
        with mock.patch.object(
            hid_elevation_windows,
            "_self_check",
            side_effect=hid_elevation_windows.HidElevationError("unknown_failure"),
        ):
            result = hid_elevation_windows.helper_main(
                [hid_elevation_windows.SELF_CHECK_FLAG]
            )

        self.assertEqual(result, hid_elevation_windows.HELPER_EXIT_VALIDATION_FAILED)

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
