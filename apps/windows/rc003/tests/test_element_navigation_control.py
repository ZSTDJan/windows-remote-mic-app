import inspect
import importlib.util
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ovb_rc003 import (
    element_navigation_control_windows as control,
    element_navigation_runtime as runtime,
    single_instance,
)


COMMAND_SOURCE_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "element_navigation_command_windows.py"
)
COMMAND_SPEC = importlib.util.spec_from_file_location(
    "standalone_element_navigation_command_windows",
    COMMAND_SOURCE_PATH,
)
assert COMMAND_SPEC is not None and COMMAND_SPEC.loader is not None
standalone_command = importlib.util.module_from_spec(COMMAND_SPEC)
COMMAND_SPEC.loader.exec_module(standalone_command)


class ElementNavigationCommandTests(unittest.TestCase):
    def test_source_and_frozen_commands_use_the_hidden_entrypoint(self):
        source_command = control.build_element_navigation_command(
            frozen=False,
            executable="python.exe",
            owner_pid=4321,
        )
        frozen_command = control.build_element_navigation_command(
            frozen=True,
            executable="RemoteMicRC003.exe",
            owner_pid=4321,
        )
        self.assertEqual(
            source_command[:4],
            ["python.exe", "-m", "ovb_rc003", "--element-navigation"],
        )
        self.assertEqual(
            frozen_command[:2],
            ["RemoteMicRC003.exe", "--element-navigation"],
        )
        self.assertIn("--managed-companion", source_command)
        self.assertIn("--managed-companion", frozen_command)
        for command in (source_command, frozen_command):
            owner_index = command.index("--owner-pid")
            self.assertEqual(command[owner_index + 1], "4321")

    def test_embedded_command_preserves_remote_mic_quicker_state_location(self):
        with mock.patch.dict(
            control.os.environ,
            {"LOCALAPPDATA": r"C:\LocalData"},
        ):
            command = control.build_element_navigation_command(
                frozen=False,
                executable="python.exe",
                owner_pid=4321,
            )

        state_index = command.index("--quicker-state-file")
        self.assertEqual(
            command[state_index + 1],
            r"C:\LocalData\RemoteMic\RC003\quicker-navigation.json",
        )

    def test_send_command_distinguishes_absent_delivered_and_failed(self):
        self.assertEqual(
            control.send_element_navigation_command(
                control.ELEMENT_NAVIGATION_COMMAND_TOGGLE,
                _find_window=lambda: 0,
            ),
            control.CommandSendResult.NOT_RUNNING,
        )
        self.assertEqual(
            control.send_element_navigation_command(
                control.ELEMENT_NAVIGATION_COMMAND_TOGGLE,
                321,
                _find_window=lambda: 99,
                _send_window_command=lambda hwnd, command, target: (
                    hwnd,
                    command,
                    target,
                )
                == (99, control.ELEMENT_NAVIGATION_COMMAND_TOGGLE, 321),
            ),
            control.CommandSendResult.DELIVERED,
        )
        self.assertEqual(
            control.send_element_navigation_command(
                control.ELEMENT_NAVIGATION_COMMAND_TOGGLE,
                _find_window=lambda: 99,
                _send_window_command=lambda _hwnd, _command, _target: False,
            ),
            control.CommandSendResult.FAILED,
        )

    def test_win32_message_loop_declares_pointer_sized_ctypes_prototypes(self):
        source = inspect.getsource(standalone_command.ElementNavigationCommandServer._run)
        for token in (
            "RegisterWindowMessageW.argtypes",
            "RegisterWindowMessageW.restype",
            "DefWindowProcW.argtypes",
            "DefWindowProcW.restype",
            "CreateWindowExW.argtypes",
            "CreateWindowExW.restype",
            "GetMessageW.argtypes",
            "GetMessageW.restype",
            "TranslateMessage.argtypes",
            "DispatchMessageW.argtypes",
        ):
            self.assertIn(token, source)

    def test_embedded_client_uses_the_standalone_command_protocol(self):
        for name in (
            "ELEMENT_NAVIGATION_WINDOW_CLASS",
            "ELEMENT_NAVIGATION_WINDOW_TITLE",
            "ELEMENT_NAVIGATION_MESSAGE_NAME",
            "ELEMENT_NAVIGATION_COMMAND_TOGGLE",
            "ELEMENT_NAVIGATION_COMMAND_QUIT",
        ):
            self.assertEqual(getattr(control, name), getattr(standalone_command, name))


class ElementNavigationClientTests(unittest.TestCase):
    @staticmethod
    def _wait_for_delivery(client, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with client._lock:
                worker = client._delivery_thread
            if worker is None:
                return
            worker.join(timeout=0.02)
        raise AssertionError("element-navigation delivery worker did not finish")

    def test_running_companion_receives_toggle_without_process_launch(self):
        calls = []
        client = control.ElementNavigationClient(
            foreground_window=lambda: 321,
            send_command=lambda command, target: calls.append((command, target))
            or control.CommandSendResult.DELIVERED,
            instance_running=lambda: self.fail("status probe must not run"),
            popen=lambda *_args, **_kwargs: self.fail("process must not launch"),
        )

        result = client.toggle()

        self.assertEqual(result.kind, control.ToggleResultKind.DELIVERED)
        self.assertEqual(
            calls,
            [(control.ELEMENT_NAVIGATION_COMMAND_TOGGLE, 321)],
        )

    def test_cold_start_launches_once_and_delivers_queued_toggles_in_order(self):
        ready = threading.Event()
        sleeping = threading.Event()
        foregrounds = iter((111, 222))
        delivered = []
        launches = []

        def send(command, target):
            if not ready.is_set():
                return control.CommandSendResult.NOT_RUNNING
            delivered.append((command, target))
            return control.CommandSendResult.DELIVERED

        def wait_for_ready(_seconds):
            sleeping.set()
            ready.wait(timeout=2.0)

        client = control.ElementNavigationClient(
            foreground_window=lambda: next(foregrounds),
            send_command=send,
            instance_running=lambda: False,
            build_command=lambda: ["navigator.exe"],
            popen=lambda command, **kwargs: launches.append((command, kwargs))
            or SimpleNamespace(pid=456),
            sleep=wait_for_ready,
        )

        first = client.toggle()
        self.assertTrue(sleeping.wait(timeout=1.0))
        second = client.toggle()
        ready.set()
        self._wait_for_delivery(client)

        self.assertEqual(first.kind, control.ToggleResultKind.STARTED)
        self.assertEqual(first.pid, 456)
        self.assertEqual(second.kind, control.ToggleResultKind.QUEUED)
        self.assertEqual(len(launches), 1)
        self.assertEqual(
            delivered,
            [
                (control.ELEMENT_NAVIGATION_COMMAND_TOGGLE, 111),
                (control.ELEMENT_NAVIGATION_COMMAND_TOGGLE, 222),
            ],
        )

    def test_shutdown_during_owned_startup_terminates_the_child_promptly(self):
        sleeping = threading.Event()
        stopped = []

        def wait_a_moment(_seconds):
            sleeping.set()
            time.sleep(0.01)

        process = SimpleNamespace(
            pid=999,
            terminate=lambda: stopped.append("terminate"),
            wait=lambda timeout: stopped.append(("wait", timeout)),
        )
        client = control.ElementNavigationClient(
            foreground_window=lambda: 777,
            send_command=lambda _command, _target: control.CommandSendResult.NOT_RUNNING,
            instance_running=lambda: False,
            build_command=lambda: ["navigator.exe"],
            popen=lambda _command, **_kwargs: process,
            sleep=wait_a_moment,
        )
        client.toggle()
        self.assertTrue(sleeping.wait(timeout=1.0))

        result = client.shutdown()

        self.assertEqual(result, control.CommandSendResult.NOT_RUNNING)
        self.assertEqual(stopped, ["terminate", ("wait", 1.0)])
        self.assertIsNone(client._delivery_thread)
        self.assertIsNone(client._starting_process)

    def test_failed_owned_child_termination_is_reported_as_shutdown_failure(self):
        sleeping = threading.Event()
        process = SimpleNamespace(
            pid=999,
            poll=lambda: None,
            terminate=lambda: (_ for _ in ()).throw(OSError("denied")),
        )
        client = control.ElementNavigationClient(
            foreground_window=lambda: 777,
            send_command=lambda _command, _target: control.CommandSendResult.NOT_RUNNING,
            instance_running=lambda: False,
            build_command=lambda: ["navigator.exe"],
            popen=lambda _command, **_kwargs: process,
            sleep=lambda _seconds: sleeping.set() or time.sleep(0.01),
        )
        client.toggle()
        self.assertTrue(sleeping.wait(timeout=1.0))

        result = client.shutdown()

        self.assertEqual(result, control.CommandSendResult.FAILED)

    def test_failed_quit_delivery_force_stops_an_owned_companion(self):
        responses = iter(
            (
                control.CommandSendResult.NOT_RUNNING,
                control.CommandSendResult.DELIVERED,
                control.CommandSendResult.FAILED,
            )
        )
        stopped = []
        process = SimpleNamespace(
            pid=999,
            poll=lambda: None,
            terminate=lambda: stopped.append("terminate"),
            wait=lambda timeout: stopped.append(("wait", timeout)),
        )
        client = control.ElementNavigationClient(
            foreground_window=lambda: 777,
            send_command=lambda _command, _target: next(responses),
            instance_running=lambda: False,
            build_command=lambda: ["navigator.exe"],
            popen=lambda _command, **_kwargs: process,
            sleep=lambda _seconds: None,
        )

        client.toggle()
        self._wait_for_delivery(client)
        result = client.shutdown()

        self.assertEqual(result, control.CommandSendResult.FAILED)
        self.assertEqual(stopped, ["terminate", ("wait", 1.0)])

    def test_delivered_quit_force_stops_an_owned_companion_that_hangs(self):
        responses = iter(
            (
                control.CommandSendResult.NOT_RUNNING,
                control.CommandSendResult.DELIVERED,
                control.CommandSendResult.DELIVERED,
            )
        )
        stopped = []

        def wait(timeout):
            stopped.append(("wait", timeout))
            if "terminate" not in stopped:
                raise TimeoutError("still running")

        process = SimpleNamespace(
            pid=999,
            poll=lambda: None,
            terminate=lambda: stopped.append("terminate"),
            wait=wait,
        )
        client = control.ElementNavigationClient(
            foreground_window=lambda: 777,
            send_command=lambda _command, _target: next(responses),
            instance_running=lambda: False,
            build_command=lambda: ["navigator.exe"],
            popen=lambda _command, **_kwargs: process,
            sleep=lambda _seconds: None,
        )

        client.toggle()
        self._wait_for_delivery(client)
        result = client.shutdown()

        self.assertEqual(result, control.CommandSendResult.DELIVERED)
        self.assertEqual(
            stopped,
            [("wait", 1.0), "terminate", ("wait", 1.0)],
        )

    def test_shutdown_never_force_stops_an_external_companion(self):
        responses = iter(
            (
                control.CommandSendResult.DELIVERED,
                control.CommandSendResult.FAILED,
            )
        )
        client = control.ElementNavigationClient(
            foreground_window=lambda: 777,
            send_command=lambda _command, _target: next(responses),
            instance_running=lambda: self.fail("status probe must not run"),
            popen=lambda *_args, **_kwargs: self.fail("process must not launch"),
        )

        self.assertEqual(
            client.toggle().kind,
            control.ToggleResultKind.DELIVERED,
        )
        with mock.patch.object(control, "_terminate_started_process") as terminate:
            result = client.shutdown()

        self.assertEqual(result, control.CommandSendResult.FAILED)
        terminate.assert_not_called()

    def test_toggle_cannot_restart_after_shutdown_begins(self):
        launches = []
        client = control.ElementNavigationClient(
            foreground_window=lambda: 777,
            send_command=lambda _command, _target: control.CommandSendResult.NOT_RUNNING,
            instance_running=lambda: False,
            build_command=lambda: ["navigator.exe"],
            popen=lambda command, **kwargs: launches.append((command, kwargs)),
        )

        self.assertEqual(client.shutdown(), control.CommandSendResult.NOT_RUNNING)
        result = client.toggle()

        self.assertEqual(result.kind, control.ToggleResultKind.FAILED)
        self.assertEqual(result.error, "client_shutdown")
        self.assertEqual(launches, [])

    def test_shutdown_during_external_startup_replaces_pending_toggle_with_quit(self):
        ready = threading.Event()
        sleeping = threading.Event()
        delivered = []

        def send(command, target):
            if not ready.is_set():
                return control.CommandSendResult.NOT_RUNNING
            delivered.append((command, target))
            return control.CommandSendResult.DELIVERED

        def wait_for_ready(_seconds):
            sleeping.set()
            ready.wait(timeout=2.0)

        client = control.ElementNavigationClient(
            foreground_window=lambda: 777,
            send_command=send,
            instance_running=lambda: True,
            popen=lambda *_args, **_kwargs: self.fail("process must not launch"),
            sleep=wait_for_ready,
        )
        client.toggle()
        self.assertTrue(sleeping.wait(timeout=1.0))
        shutdown_result = []
        shutdown_thread = threading.Thread(
            target=lambda: shutdown_result.append(client.shutdown())
        )
        shutdown_thread.start()
        ready.set()
        shutdown_thread.join(timeout=2.0)

        self.assertFalse(shutdown_thread.is_alive())
        self.assertEqual(shutdown_result, [control.CommandSendResult.DELIVERED])
        self.assertEqual(
            delivered,
            [(control.ELEMENT_NAVIGATION_COMMAND_QUIT, 0)],
        )

    def test_fast_child_exit_reports_the_background_failure(self):
        failures = []
        process = SimpleNamespace(pid=123, poll=lambda: 23)
        client = control.ElementNavigationClient(
            foreground_window=lambda: 321,
            send_command=lambda _command, _target: control.CommandSendResult.NOT_RUNNING,
            instance_running=lambda: False,
            build_command=lambda: ["navigator.exe"],
            popen=lambda _command, **_kwargs: process,
            sleep=lambda _seconds: None,
            background_failure=failures.append,
        )

        result = client.toggle()
        self._wait_for_delivery(client)

        self.assertEqual(result.kind, control.ToggleResultKind.STARTED)
        self.assertEqual(failures, ["companion_exited:23"])

    def test_startup_timeout_reports_the_background_failure(self):
        ticks = iter((0.0, 0.0, 7.0))
        failures = []
        client = control.ElementNavigationClient(
            foreground_window=lambda: 321,
            send_command=lambda _command, _target: control.CommandSendResult.NOT_RUNNING,
            instance_running=lambda: True,
            sleep=lambda _seconds: None,
            monotonic=lambda: next(ticks, 7.0),
            background_failure=failures.append,
        )

        result = client.toggle()
        self._wait_for_delivery(client)

        self.assertEqual(result.kind, control.ToggleResultKind.QUEUED)
        self.assertEqual(failures, ["startup_timeout"])

    def test_worker_start_failure_does_not_leave_a_false_starting_state(self):
        class FailingThread:
            def start(self):
                raise RuntimeError("thread unavailable")

        stopped = []
        process = SimpleNamespace(
            pid=1,
            terminate=lambda: stopped.append("terminate"),
            wait=lambda timeout: stopped.append(("wait", timeout)),
        )
        client = control.ElementNavigationClient(
            foreground_window=lambda: 123,
            send_command=lambda _command, _target: control.CommandSendResult.NOT_RUNNING,
            instance_running=lambda: False,
            build_command=lambda: ["navigator.exe"],
            popen=lambda _command, **_kwargs: process,
        )
        with mock.patch.object(control.threading, "Thread", return_value=FailingThread()):
            result = client.toggle()

        self.assertEqual(result.kind, control.ToggleResultKind.FAILED)
        self.assertEqual(result.error, "RuntimeError")
        self.assertIsNone(client._delivery_thread)
        self.assertIsNone(client._starting_process)
        self.assertEqual(list(client._pending), [])
        self.assertEqual(stopped, ["terminate", ("wait", 1.0)])


class ElementNavigationRuntimeTests(unittest.TestCase):
    def test_first_owner_runs_the_single_navigation_source(self):
        received = []

        class Guard:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        prototype = SimpleNamespace(
            main=lambda arguments: received.append(list(arguments)) or 12
        )
        with mock.patch.object(
            runtime.single_instance,
            "ElementNavigationInstanceGuard",
            Guard,
        ), mock.patch.object(runtime, "_load_prototype", return_value=prototype):
            result = runtime.run_element_navigation(["--diagnostics"])

        self.assertEqual(result, 12)
        self.assertEqual(received, [["--diagnostics"]])

    def test_duplicate_activate_is_forwarded_to_the_existing_process(self):
        class DuplicateGuard:
            def __enter__(self):
                raise single_instance.DuplicateInstanceError()

            def __exit__(self, exc_type, exc, tb):
                return False

        calls = []
        with mock.patch.object(
            runtime.single_instance,
            "ElementNavigationInstanceGuard",
            DuplicateGuard,
        ), mock.patch.object(
            runtime.element_navigation_control_windows,
            "send_element_navigation_command",
            side_effect=lambda command, target: calls.append((command, target))
            or control.CommandSendResult.DELIVERED,
        ):
            result = runtime.run_element_navigation(
                ["--activate", "--window-handle", "0x141"]
            )

        self.assertEqual(result, 0)
        self.assertEqual(
            calls,
            [(control.ELEMENT_NAVIGATION_COMMAND_TOGGLE, 321)],
        )

    def test_duplicate_without_activate_exits_without_toggling(self):
        class DuplicateGuard:
            def __enter__(self):
                raise single_instance.DuplicateInstanceError()

            def __exit__(self, exc_type, exc, tb):
                return False

        with mock.patch.object(
            runtime.single_instance,
            "ElementNavigationInstanceGuard",
            DuplicateGuard,
        ), mock.patch.object(
            runtime.element_navigation_control_windows,
            "send_element_navigation_command",
        ) as send:
            result = runtime.run_element_navigation([])

        self.assertEqual(result, single_instance.DUPLICATE_INSTANCE_EXIT_CODE)
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
