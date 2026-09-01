"""Tests bridge_launcher.py's command construction and launch-outcome
detection (XRBM-029) purely via dependency injection - no real subprocess is
ever spawned and no real ``time.sleep`` ever runs, matching this package's
established "never touch a real OS resource in a test" convention (see e.g.
tests/test_single_instance.py's injected ``_create_mutex``/etc.).
"""

import unittest

from ovb_rc003 import bridge_launcher, single_instance


class BuildLaunchCommandTests(unittest.TestCase):
    def test_frozen_uses_the_current_executable_with_bridge_flag(self):
        command = bridge_launcher.build_launch_command(
            frozen=True, executable=r"C:\Apps\RemoteMicRC003.exe"
        )
        self.assertEqual(
            command,
            [
                r"C:\Apps\RemoteMicRC003.exe",
                "--bridge",
                bridge_launcher.SETTINGS_LAUNCH_FLAG,
            ],
        )

    def test_frozen_command_never_recurses_into_settings(self):
        command = bridge_launcher.build_launch_command(
            frozen=True, executable=r"C:\Apps\RemoteMicRC003.exe"
        )
        self.assertNotIn("--settings", command)

    def test_frozen_settings_mode_still_launches_the_same_executable_with_bridge(self):
        # The single packaged exe handles both modes: no arguments / --settings
        # open the settings window, --bridge starts the bridge. There is no
        # separate settings sibling anymore.
        command = bridge_launcher.build_launch_command(
            frozen=True, executable=r"C:\Apps\RemoteMicRC003.exe"
        )
        self.assertEqual(
            command,
            [
                r"C:\Apps\RemoteMicRC003.exe",
                "--bridge",
                bridge_launcher.SETTINGS_LAUNCH_FLAG,
            ],
        )

    def test_source_uses_the_current_interpreter_with_module_flag_and_bridge(self):
        command = bridge_launcher.build_launch_command(
            frozen=False, executable=r"C:\Python312\python.exe"
        )
        self.assertEqual(
            command,
            [
                r"C:\Python312\python.exe",
                "-m",
                "ovb_rc003",
                "--bridge",
                bridge_launcher.SETTINGS_LAUNCH_FLAG,
            ],
        )

    def test_settings_launch_marker_is_always_after_the_bridge_flag(self):
        command = bridge_launcher.build_launch_command(
            frozen=True, executable=r"C:\Apps\RemoteMicRC003.exe"
        )
        self.assertEqual(command[-2:], ["--bridge", bridge_launcher.SETTINGS_LAUNCH_FLAG])

    def test_source_command_never_recurses_into_settings(self):
        command = bridge_launcher.build_launch_command(
            frozen=False, executable=r"C:\Python312\python.exe"
        )
        self.assertNotIn("--settings", command)

    def test_frozen_settings_command_uses_the_same_executable(self):
        command = bridge_launcher.build_settings_command(
            frozen=True, executable=r"C:\Apps\RemoteMicRC003.exe"
        )
        self.assertEqual(
            command,
            [r"C:\Apps\RemoteMicRC003.exe", "--settings"],
        )

    def test_source_settings_command_uses_module_entrypoint(self):
        command = bridge_launcher.build_settings_command(
            frozen=False, executable=r"C:\Python312\python.exe"
        )
        self.assertEqual(
            command,
            [r"C:\Python312\python.exe", "-m", "ovb_rc003", "--settings"],
        )

    def test_empty_executable_fails_closed(self):
        with self.assertRaises(bridge_launcher.BridgeLaunchConfigurationError):
            bridge_launcher.build_launch_command(frozen=False, executable="")

    def test_defaults_read_the_real_sys_module_without_raising(self):
        # Exercises the frozen=None/executable=None default-resolution
        # branch directly (whatever this test process' own sys.executable/
        # sys.frozen happen to be) - just proves it never raises and always
        # returns a non-empty list, since the exact values are host-specific.
        command = bridge_launcher.build_launch_command()
        self.assertTrue(command)


class _FakeProcess:
    """Scripted stand-in for subprocess.Popen: ``poll_results`` is consumed
    one value at a time by each ``.poll()`` call, then the last value
    repeats - so a test can express "alive for N checks, then exit code X"
    as a short list.
    """

    def __init__(self, poll_results, *, pid=4242):
        self.pid = pid
        self._poll_results = list(poll_results)
        self._index = 0

    def poll(self):
        if self._index < len(self._poll_results):
            value = self._poll_results[self._index]
            self._index += 1
            return value
        return self._poll_results[-1] if self._poll_results else None


class LaunchBridgeTests(unittest.TestCase):
    def setUp(self):
        self._sleep_calls = []

    def _fake_sleep(self, seconds):
        self._sleep_calls.append(seconds)

    def test_process_still_alive_after_grace_period_is_started(self):
        process = _FakeProcess([None] * 20, pid=111)
        popen_calls = []

        def fake_popen(command):
            popen_calls.append(command)
            return process

        result = bridge_launcher.launch_bridge(
            ["exe"],
            grace_checks=3,
            poll_interval_seconds=0.01,
            _popen=fake_popen,
            _sleep=self._fake_sleep,
        )

        self.assertEqual(result.outcome, bridge_launcher.LaunchOutcome.STARTED)
        self.assertEqual(result.pid, 111)
        self.assertIsNone(result.exit_code)
        self.assertEqual(popen_calls, [["exe"]])
        # Grace period exhausted (3 checks), never a real sleep.
        self.assertEqual(len(self._sleep_calls), 3)

    def test_exact_duplicate_instance_exit_code_is_already_running(self):
        process = _FakeProcess([single_instance.DUPLICATE_INSTANCE_EXIT_CODE])

        result = bridge_launcher.launch_bridge(
            ["exe"],
            _popen=lambda command: process,
            _sleep=self._fake_sleep,
        )

        self.assertEqual(result.outcome, bridge_launcher.LaunchOutcome.ALREADY_RUNNING)
        self.assertEqual(result.exit_code, single_instance.DUPLICATE_INSTANCE_EXIT_CODE)
        # Detected on the very first poll - no grace-period sleeping needed.
        self.assertEqual(self._sleep_calls, [])

    def test_other_nonzero_exit_code_is_a_quick_exit_with_the_real_code_preserved(self):
        process = _FakeProcess([7])

        result = bridge_launcher.launch_bridge(
            ["exe"], _popen=lambda command: process, _sleep=self._fake_sleep
        )

        self.assertEqual(result.outcome, bridge_launcher.LaunchOutcome.QUICK_EXIT)
        self.assertEqual(result.exit_code, 7)

    def test_clean_zero_exit_within_grace_period_is_still_a_quick_exit(self):
        # A bridge that exits 0 almost immediately did not stay up to run
        # the bridge - "0" must not be silently treated as "started" just
        # because it isn't an error code.
        process = _FakeProcess([0])

        result = bridge_launcher.launch_bridge(
            ["exe"], _popen=lambda command: process, _sleep=self._fake_sleep
        )

        self.assertEqual(result.outcome, bridge_launcher.LaunchOutcome.QUICK_EXIT)
        self.assertEqual(result.exit_code, 0)

    def test_guard_unavailable_exit_code_is_a_distinct_quick_exit_not_already_running(self):
        process = _FakeProcess([single_instance.GUARD_UNAVAILABLE_EXIT_CODE])

        result = bridge_launcher.launch_bridge(
            ["exe"], _popen=lambda command: process, _sleep=self._fake_sleep
        )

        self.assertEqual(result.outcome, bridge_launcher.LaunchOutcome.QUICK_EXIT)
        self.assertEqual(result.exit_code, single_instance.GUARD_UNAVAILABLE_EXIT_CODE)

    def test_popen_oserror_is_reported_as_launch_failed_not_raised(self):
        def raising_popen(command):
            raise OSError("[WinError 2] The system cannot find the file specified")

        result = bridge_launcher.launch_bridge(
            ["missing.exe"], _popen=raising_popen, _sleep=self._fake_sleep
        )

        self.assertEqual(result.outcome, bridge_launcher.LaunchOutcome.LAUNCH_FAILED)
        self.assertIsNone(result.exit_code)
        self.assertEqual(result.error, "OSError")
        self.assertEqual(self._sleep_calls, [])

    def test_initial_poll_oserror_retains_created_process_as_status_unknown(self):
        class PollFailureProcess:
            pid = 9876

            def poll(self):
                raise OSError("simulated process status failure")

        result = bridge_launcher.launch_bridge(
            ["exe"],
            _popen=lambda _command: PollFailureProcess(),
            _sleep=self._fake_sleep,
        )

        self.assertEqual(result.outcome, bridge_launcher.LaunchOutcome.STATUS_UNKNOWN)
        self.assertEqual(result.pid, 9876)
        self.assertEqual(result.error, "OSError")
        self.assertEqual(self._sleep_calls, [])

    def test_later_poll_oserror_retains_created_process_as_status_unknown(self):
        class LaterPollFailureProcess:
            pid = 9877

            def __init__(self):
                self.calls = 0

            def poll(self):
                self.calls += 1
                if self.calls == 1:
                    return None
                raise OSError("simulated process status failure")

        result = bridge_launcher.launch_bridge(
            ["exe"],
            _popen=lambda _command: LaterPollFailureProcess(),
            _sleep=self._fake_sleep,
        )

        self.assertEqual(result.outcome, bridge_launcher.LaunchOutcome.STATUS_UNKNOWN)
        self.assertEqual(result.pid, 9877)
        self.assertEqual(result.error, "OSError")
        self.assertEqual(len(self._sleep_calls), 1)

    def test_process_exits_partway_through_the_grace_period(self):
        # Alive for the first two checks, then exits - proves the polling
        # loop keeps checking rather than only ever looking once. Uses code
        # 9, deliberately distinct from single_instance.
        # DUPLICATE_INSTANCE_EXIT_CODE (3), to stay in the QUICK_EXIT branch.
        process = _FakeProcess([None, None, 9])

        result = bridge_launcher.launch_bridge(
            ["exe"],
            grace_checks=5,
            _popen=lambda command: process,
            _sleep=self._fake_sleep,
        )

        self.assertEqual(result.outcome, bridge_launcher.LaunchOutcome.QUICK_EXIT)
        self.assertEqual(result.exit_code, 9)
        self.assertEqual(len(self._sleep_calls), 2)

    def test_no_command_argument_falls_back_to_build_launch_command(self):
        popen_calls = []

        def fake_popen(command):
            popen_calls.append(command)
            return _FakeProcess([None] * 5)

        bridge_launcher.launch_bridge(
            grace_checks=1,
            _popen=fake_popen,
            _sleep=self._fake_sleep,
        )

        self.assertEqual(len(popen_calls), 1)
        self.assertTrue(popen_calls[0])  # non-empty, host-dependent contents


class NonBlockingLaunchTests(unittest.TestCase):
    def test_start_returns_a_pending_launch_without_waiting(self):
        process = _FakeProcess([None, None], pid=2468)

        attempt = bridge_launcher.start_bridge_launch(
            ["exe"],
            grace_checks=2,
            _popen=lambda command: process,
        )

        self.assertIsInstance(attempt, bridge_launcher.PendingBridgeLaunch)
        self.assertEqual(attempt.pid, 2468)
        self.assertEqual(attempt.checks_remaining, 2)

    def test_poll_returns_none_until_the_grace_checks_finish(self):
        pending = bridge_launcher.PendingBridgeLaunch(
            command=("exe",),
            process=_FakeProcess([None, None]),
            pid=2468,
            checks_remaining=2,
        )

        self.assertIsNone(bridge_launcher.poll_bridge_launch(pending))
        result = bridge_launcher.poll_bridge_launch(pending)

        self.assertEqual(result.outcome, bridge_launcher.LaunchOutcome.STARTED)
        self.assertEqual(result.pid, 2468)

    def test_poll_reports_a_quick_exit_before_grace_finishes(self):
        pending = bridge_launcher.PendingBridgeLaunch(
            command=("exe",),
            process=_FakeProcess([7]),
            pid=2468,
            checks_remaining=5,
        )

        result = bridge_launcher.poll_bridge_launch(pending)

        self.assertEqual(result.outcome, bridge_launcher.LaunchOutcome.QUICK_EXIT)
        self.assertEqual(result.exit_code, 7)


class LaunchSettingsTests(unittest.TestCase):
    def test_success_reports_the_created_process_pid(self):
        process = _FakeProcess([None], pid=6543)
        popen_calls = []

        result = bridge_launcher.launch_settings(
            ["exe", "--settings"],
            _popen=lambda command: popen_calls.append(command) or process,
        )

        self.assertTrue(result.started)
        self.assertEqual(result.pid, 6543)
        self.assertEqual(popen_calls, [["exe", "--settings"]])

    def test_launch_error_is_returned_without_raising(self):
        def fail(_command):
            raise OSError("missing settings executable")

        result = bridge_launcher.launch_settings(
            ["missing.exe", "--settings"],
            _popen=fail,
        )

        self.assertFalse(result.started)
        self.assertEqual(result.error, "OSError")


if __name__ == "__main__":
    unittest.main()
