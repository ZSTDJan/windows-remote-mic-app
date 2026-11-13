"""Resident-process wiring tests (no Qt, no real tray, no real bridge).

``resident_shell.main`` itself needs a Qt event loop and is covered by the
frozen/manual checkpoints; everything that decides bridge ownership, request
handling and shutdown ordering is a plain object and is exercised here.
"""

import unittest
from unittest import mock

from ovb_rc003 import element_navigation_control_windows, resident_shell


def _make_shell(*, tray=None, navigation=None):
    application = mock.Mock()
    logger = mock.Mock()
    root = mock.Mock()
    shell = resident_shell._ResidentShell(application, logger, root)
    shell._tray = tray
    shell._navigation = navigation
    return shell


class StartBridgeTests(unittest.TestCase):
    def test_start_bridge_uses_the_in_process_worker(self):
        shell = _make_shell()
        started = resident_shell.bridge_launcher.LaunchResult(
            outcome=resident_shell.bridge_launcher.LaunchOutcome.STARTED,
            command=("<in-process-bridge>",),
            pid=123,
        )
        with mock.patch.object(
            resident_shell.bridge_launcher,
            "in_process_bridge_running",
            return_value=False,
        ), mock.patch.object(
            resident_shell.bridge_launcher,
            "start_in_process_bridge",
            return_value=started,
        ) as start:
            self.assertTrue(shell.start_bridge(reason="resident_start"))

        self.assertTrue(shell._bridge_started)
        start.assert_called_once_with(grace_checks=0, launch_voice_program_on_start=True)

    def test_start_bridge_is_idempotent_while_the_worker_runs(self):
        shell = _make_shell()
        with mock.patch.object(
            resident_shell.bridge_launcher,
            "in_process_bridge_running",
            return_value=True,
        ), mock.patch.object(
            resident_shell.bridge_launcher, "start_in_process_bridge"
        ) as start:
            self.assertTrue(shell.start_bridge(reason="settings_request"))

        start.assert_not_called()
        self.assertTrue(shell._bridge_started)

    def test_start_bridge_failure_keeps_the_tray_alive(self):
        shell = _make_shell()
        failed = resident_shell.bridge_launcher.LaunchResult(
            outcome=resident_shell.bridge_launcher.LaunchOutcome.LAUNCH_FAILED,
            command=(),
            error="OSError",
        )
        with mock.patch.object(
            resident_shell.bridge_launcher,
            "in_process_bridge_running",
            return_value=False,
        ), mock.patch.object(
            resident_shell.bridge_launcher,
            "start_in_process_bridge",
            return_value=failed,
        ):
            self.assertFalse(shell.start_bridge(reason="resident_start"))

        self.assertFalse(shell._bridge_started)
        self.assertFalse(shell._shutdown_complete)


class PollTests(unittest.TestCase):
    def test_application_exit_request_ends_the_process(self):
        shell = _make_shell()
        with mock.patch.object(
            resident_shell.single_instance,
            "consume_and_acknowledge_application_exit_request",
            return_value=object(),
        ):
            self.assertTrue(shell.poll())

    def test_bridge_start_request_starts_the_worker(self):
        shell = _make_shell()
        with mock.patch.object(
            resident_shell.single_instance,
            "consume_and_acknowledge_application_exit_request",
            return_value=None,
        ), mock.patch.object(
            resident_shell.single_instance,
            "consume_bridge_start_request",
            return_value=True,
        ), mock.patch.object(
            shell, "start_bridge", return_value=True
        ) as start:
            self.assertFalse(shell.poll())

        start.assert_called_once_with(reason="settings_request")

    def test_running_worker_treats_a_start_request_as_immediate_reconnect(self):
        shell = _make_shell()
        shell._bridge_started = True
        with mock.patch.object(
            resident_shell.single_instance,
            "consume_and_acknowledge_application_exit_request",
            return_value=None,
        ), mock.patch.object(
            resident_shell.single_instance,
            "consume_bridge_start_request",
            return_value=True,
        ), mock.patch.object(
            resident_shell.bridge_launcher,
            "in_process_bridge_running",
            return_value=True,
        ), mock.patch.object(
            resident_shell.bridge_launcher,
            "reconnect_in_process_bridge_now",
            return_value=True,
        ) as reconnect, mock.patch.object(shell, "start_bridge") as start:
            self.assertFalse(shell.poll())

        reconnect.assert_called_once_with()
        start.assert_not_called()

    def test_application_exit_also_closes_the_settings_window(self):
        shell = _make_shell()
        with mock.patch.object(
            resident_shell.single_instance,
            "consume_and_acknowledge_application_exit_request",
            return_value=object(),
        ), mock.patch.object(
            resident_shell.single_instance,
            "request_settings_window_close",
            return_value=True,
        ) as close_window:
            self.assertTrue(shell.poll())

        close_window.assert_called_once_with()

    def test_start_request_restarts_the_worker_after_it_died(self):
        shell = _make_shell()
        shell._bridge_started = True
        with mock.patch.object(
            resident_shell.single_instance,
            "consume_and_acknowledge_application_exit_request",
            return_value=None,
        ), mock.patch.object(
            resident_shell.single_instance,
            "consume_bridge_start_request",
            return_value=True,
        ), mock.patch.object(
            resident_shell.bridge_launcher,
            "in_process_bridge_running",
            return_value=False,
        ), mock.patch.object(
            shell, "start_bridge", return_value=True
        ) as start:
            self.assertFalse(shell.poll())

        start.assert_called_once_with(reason="settings_request")

    def test_tray_exit_callback_only_sets_the_flag(self):
        shell = _make_shell()
        shell._request_exit()
        self.assertTrue(shell.poll())


class ShutdownTests(unittest.TestCase):
    def test_shutdown_releases_navigation_bridge_and_tray_once(self):
        shell = _make_shell(tray=mock.Mock(), navigation=object())
        shell._bridge_started = True
        with mock.patch.object(
            element_navigation_control_windows,
            "shutdown_element_navigation",
        ) as stop_navigation, mock.patch.object(
            resident_shell.bridge_launcher,
            "stop_in_process_bridge",
            return_value=True,
        ) as stop_bridge, mock.patch.object(
            shell, "_stop_tray"
        ) as stop_tray:
            shell.shutdown()
            shell.shutdown()

        stop_navigation.assert_called_once_with()
        stop_bridge.assert_called_once_with(reason="process_exit")
        stop_tray.assert_called_once_with()

    def test_shutdown_keeps_tray_cleanup_when_navigation_fails(self):
        shell = _make_shell(tray=mock.Mock(), navigation=object())
        with mock.patch.object(
            element_navigation_control_windows,
            "shutdown_element_navigation",
            side_effect=RuntimeError("navigation stuck"),
        ), mock.patch.object(shell, "_stop_tray") as stop_tray:
            shell.shutdown()

        stop_tray.assert_called_once_with()


class ExitCapabilityTests(unittest.TestCase):
    def test_exit_capability_is_published_once_from_a_hidden_window(self):
        shell = _make_shell()
        holder = mock.Mock()
        holder.winId.return_value = 4321
        with mock.patch.object(
            resident_shell, "_create_capability_window", return_value=holder
        ) as create, mock.patch.object(
            resident_shell.single_instance,
            "mark_resident_exit_capability",
            return_value=True,
        ) as mark:
            shell.publish_exit_capability()
            shell.publish_exit_capability()

        create.assert_called_once_with()
        mark.assert_called_once_with(4321)
        self.assertIs(shell._capability_window, holder)
        self.assertEqual(shell._capability_hwnd, 4321)

    def test_exit_capability_failure_keeps_the_resident_running(self):
        shell = _make_shell()
        with mock.patch.object(
            resident_shell,
            "_create_capability_window",
            side_effect=RuntimeError("no qt"),
        ):
            shell.publish_exit_capability()

        self.assertEqual(shell._capability_hwnd, 0)
        self.assertIsNone(shell._capability_window)


class NavigationBindingTests(unittest.TestCase):
    def test_start_navigation_binds_the_embedded_runtime(self):
        shell = _make_shell()
        runtime = object()
        with mock.patch.object(
            resident_shell.element_navigation_runtime,
            "start_embedded_element_navigation",
            return_value=runtime,
        ) as start, mock.patch.object(
            element_navigation_control_windows,
            "bind_embedded_element_navigation",
        ) as bind:
            shell.start_navigation()

        start.assert_called_once_with(shell._application)
        bind.assert_called_once_with(runtime)
        self.assertIs(shell._navigation, runtime)

    def test_start_navigation_failure_is_non_fatal(self):
        shell = _make_shell()
        with mock.patch.object(
            resident_shell.element_navigation_runtime,
            "start_embedded_element_navigation",
            side_effect=RuntimeError("no ui automation"),
        ):
            shell.start_navigation()

        self.assertIsNone(shell._navigation)


if __name__ == "__main__":
    unittest.main()
