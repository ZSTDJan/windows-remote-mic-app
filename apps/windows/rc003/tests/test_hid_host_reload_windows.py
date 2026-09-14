"""All device mutation is mocked; real Windows calls only enumerate this process."""

from contextlib import ExitStack
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest
from unittest import mock

from ovb_rc003 import frida_hid_tap_injector as injector
from ovb_rc003 import frida_hid_tap_runtime as runtime
from ovb_rc003 import hid_host_reload_windows as reload


DLL = Path(r"C:\Program Files\RemoteMic\owner\17.15.3-x64-hash-reload\RemoteMicRC003HidTap.dll")
LEGACY = DLL.parent.with_name(DLL.parent.name.removesuffix("-reload")) / DLL.name
INSTANCE = r"BTHLEDevice\{00001812-0000-1000-8000-00805f9b34fb}_dev_vid&012717_pid&32b8_rev&00a4\test"


class HostReloadTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.mocks = {}
        defaults = {
            "loaded_gadget_paths": [LEGACY], "_exclusive_instance": INSTANCE,
            "_is_present_and_started": True, "_restart_device": 0,
            "_instance_host_pid": 4321,
        }
        for name, value in defaults.items():
            self.mocks[name] = self.stack.enter_context(mock.patch.object(reload, name, return_value=value))
        self.stack.enter_context(mock.patch.object(reload.sys, "getwindowsversion", return_value=SimpleNamespace(build=19041)))
        self.host = self.stack.enter_context(mock.patch.object(runtime, "find_rc003_hidogatt_host_pid", return_value=1234))

    def test_exclusive_legacy_host_restarts_exactly_once_and_waits_for_replacement(self):
        self.assertEqual(reload.ensure_reload_capable_host(1234, DLL), "restarted")
        self.mocks["_restart_device"].assert_called_once_with(INSTANCE)
        self.assertEqual(self.mocks["_exclusive_instance"].call_count, 2)

    def test_loaded_reload_runtime_does_not_restart_or_inject_again(self):
        self.mocks["loaded_gadget_paths"].return_value = [DLL]
        self.assertEqual(reload.ensure_reload_capable_host(1234, DLL), "loaded")
        self.mocks["_restart_device"].assert_not_called()

    def test_fresh_host_needs_no_device_operation(self):
        self.mocks["loaded_gadget_paths"].return_value = []
        self.assertEqual(reload.ensure_reload_capable_host(1234, DLL), "fresh")
        self.mocks["_exclusive_instance"].assert_not_called()

    def test_unrecognized_or_duplicate_runtime_never_restarts_device(self):
        for paths in ([Path(r"C:\unrelated\RemoteMicRC003HidTap.dll")], [LEGACY, DLL]):
            with self.subTest(paths=paths):
                self.mocks["loaded_gadget_paths"].return_value = paths
                self.assertEqual(reload.ensure_reload_capable_host(1234, DLL), "restart_required")
        self.mocks["_restart_device"].assert_not_called()

    def test_shared_ambiguous_absent_or_changed_host_never_restarts(self):
        for name, value in (("_exclusive_instance", None), ("_is_present_and_started", False)):
            with self.subTest(name=name):
                original = self.mocks[name].return_value
                self.mocks[name].return_value = value
                self.assertEqual(reload.ensure_reload_capable_host(1234, DLL), "restart_required")
                self.mocks[name].return_value = original
        self.host.return_value = 999
        self.assertEqual(reload.ensure_reload_capable_host(1234, DLL), "restart_required")
        self.mocks["_restart_device"].assert_not_called()

    def test_target_changes_during_revalidation(self):
        self.mocks["_exclusive_instance"].side_effect = [INSTANCE, None]
        self.assertEqual(reload.ensure_reload_capable_host(1234, DLL), "restart_required")
        self.mocks["_restart_device"].assert_not_called()

    def test_older_windows_retains_explicit_fallback(self):
        with mock.patch.object(reload.sys, "getwindowsversion", return_value=SimpleNamespace(build=17763)):
            self.assertEqual(reload.ensure_reload_capable_host(1234, DLL), "restart_required")
        self.mocks["_restart_device"].assert_not_called()

    def test_restart_failure_and_reboot_required_do_not_claim_success(self):
        for result in (1, 3010, OSError("denied"), subprocess.TimeoutExpired("pnputil", 15)):
            with self.subTest(result=result):
                command = self.mocks["_restart_device"]
                command.side_effect = result if isinstance(result, Exception) else None
                command.return_value = result
                self.assertEqual(reload.ensure_reload_capable_host(1234, DLL), "restart_required")

    def test_success_exit_with_same_host_is_not_success_and_does_not_repeat(self):
        self.mocks["_instance_host_pid"].return_value = 1234
        with mock.patch.object(reload.time, "monotonic", side_effect=[0, 1, 6]), mock.patch.object(reload.time, "sleep"):
            self.assertEqual(reload.ensure_reload_capable_host(1234, DLL), "restart_required")
        self.mocks["_restart_device"].assert_called_once()

    def test_registry_or_module_snapshot_failure_is_closed(self):
        self.mocks["_exclusive_instance"].side_effect = PermissionError("denied")
        self.assertEqual(reload.ensure_reload_capable_host(1234, DLL), "restart_required")
        self.mocks["_restart_device"].assert_not_called()
        self.mocks["loaded_gadget_paths"].side_effect = OSError("incomplete")
        with self.assertRaises(OSError):
            reload.ensure_reload_capable_host(1234, DLL)


class InjectorLifecycleTests(unittest.TestCase):
    def test_lifecycle_outcomes_never_load_another_gadget_over_legacy(self):
        for state, expected in (("fresh", None), ("loaded", None), ("restarted", "hid_helper_host_restarted"),
                                ("restart_required", "hid_helper_legacy_runtime_restart_required")):
            with self.subTest(state=state), ExitStack() as stack:
                for name, value in (("find_rc003_hidogatt_host_pid", 1234), ("enable_debug_privilege", None),
                                    ("_target_process_name", "wudfhost.exe"), ("prepare_secure_runtime", DLL),
                                    ("sha256_file", runtime.GADGET_DLL_SHA256), ("ensure_reload_capable_host", state)):
                    stack.enter_context(mock.patch.object(injector, name, return_value=value))
                load = stack.enter_context(mock.patch.object(injector, "inject_library"))
                if expected:
                    with self.assertRaisesRegex(injector.HidInjectionStageError, expected):
                        injector.inject_current_process(1234)
                else:
                    injector.inject_current_process(1234)
                self.assertEqual(load.call_count, int(state == "fresh"))

    def test_direct_child_preserves_migration_outcomes(self):
        for detail, code in (("hid_helper_host_restarted", 6), ("hid_helper_legacy_runtime_restart_required", 7)):
            with mock.patch.object(injector, "inject_current_process", side_effect=injector.HidInjectionStageError(detail)):
                self.assertEqual(injector.main(["--pid", "1234"]), code)


@unittest.skipUnless(os.name == "nt", "Windows APIs")
class WindowsApiTests(unittest.TestCase):
    def test_enumerates_only_the_test_process_without_mutating_it(self):
        self.assertEqual(reload.loaded_gadget_paths(os.getpid()), [])

    def test_nonexistent_node_is_not_present(self):
        self.assertFalse(reload._is_present_and_started(INSTANCE))

    def test_command_uses_system_binary_and_one_literal_instance_without_reboot(self):
        with mock.patch.object(reload.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
            self.assertEqual(reload._restart_device(INSTANCE), 0)
        args, kwargs = run.call_args
        self.assertEqual(args[0][1:], ["/restart-device", INSTANCE])
        self.assertTrue(Path(args[0][0]).is_absolute())
        self.assertEqual(Path(args[0][0]).name, "pnputil.exe")
        self.assertNotIn("shell", kwargs)
        self.assertEqual(kwargs["creationflags"], subprocess.CREATE_NO_WINDOW)
        self.assertEqual(kwargs["timeout"], 15)


if __name__ == "__main__":
    unittest.main()
