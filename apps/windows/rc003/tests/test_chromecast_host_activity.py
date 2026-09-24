import unittest
from types import SimpleNamespace
from unittest import mock
from ovb_rc003 import chromecast_host_activity as subject
from ovb_rc003.voice_playback_session_windows import CaptureSession


def session(name="current", state=1, pid=20):
    return CaptureSession("capture", name, pid, state)


class CaptureWatchTests(unittest.TestCase):
    def watch(self, *readings, fast_start=False):
        reader = mock.Mock(side_effect=readings)
        watch = subject.CaptureWatch(reader, fast_start=fast_start)
        watch.begin()
        return watch, reader

    def test_new_session_active_to_inactive_then_ended(self):
        watch, _ = self.watch((), (session(),), (session(state=0),), ())
        self.assertFalse(watch.poll(1))
        self.assertEqual(watch.status, "tracking")
        self.assertFalse(watch.poll(2))
        self.assertTrue(watch.poll(2.75))

    def test_preexisting_active_not_bound_and_cannot_end_our_attempt(self):
        watch, _ = self.watch((session(),), (session(),), (), ())
        for now in (1, 2, 4):
            self.assertFalse(watch.poll(now))
        self.assertIsNone(watch.identity)

    def test_ambiguous_multiple_new_sessions_not_guessed(self):
        watch, _ = self.watch((), (session(), session("other")), ())
        self.assertFalse(watch.poll(1))
        self.assertEqual(watch.status, "ambiguous")
        self.assertFalse(watch.poll(3))

    def test_unknown_breaks_continuous_end_confirmation(self):
        watch, _ = self.watch((), (session(),), (), OSError("query failed"), (), ())
        for now in (1, 2, 3, 4):
            self.assertFalse(watch.poll(now))
        self.assertTrue(watch.poll(4.75))

    def test_reactivated_same_instance_resets_grace(self):
        watch, _ = self.watch((), (session(),), (), (session(),), (), ())
        for now in (1, 2, 2.25, 5):
            self.assertFalse(watch.poll(now))
        self.assertTrue(watch.poll(5.75))

    def test_silence_is_not_a_session_state_and_long_active_remains_active(self):
        watch, _ = self.watch((), (session(),), (session(),), (session(),))
        for now in (1, 100, 200):
            self.assertFalse(watch.poll(now))

    def test_replacement_instance_not_adopted(self):
        watch, _ = self.watch((), (session(),), (session("replacement", pid=30),), ())
        watch.poll(1)
        watch.poll(2)
        self.assertTrue(watch.poll(3))
        self.assertEqual(watch.identity, session().identity)

    def test_failed_baseline_disables_watch_not_recording(self):
        watch, reader = self.watch(OSError("unavailable"))
        self.assertFalse(watch.poll(100))
        self.assertEqual(watch.status, "unavailable")
        reader.assert_called_once()

    def test_queries_throttled_and_only_metadata_used(self):
        watch, reader = self.watch((), (session(),))
        watch.poll(1)
        watch.poll(1.1)
        self.assertEqual(reader.call_count, 2)

    def test_fast_start_detects_ready_before_ordinary_poll_then_restores_rate(self):
        for fast, expected in ((False, "waiting"), (True, "tracking")):
            with self.subTest(fast_start=fast):
                watch, reader = self.watch((), (), (session(),), (), (),
                                          fast_start=fast)
                watch.poll(1)
                watch.poll(1.03)
                self.assertEqual(reader.call_count, 2)
                watch.poll(1.06)
                self.assertEqual(watch.status, expected)
                if fast:
                    watch.poll(1.12)
                    self.assertEqual(reader.call_count, 3)
                    watch.poll(1.32)
                    self.assertEqual(watch.status, "ending")
                    self.assertFalse(watch.poll(1.58))
                    # The ordinary 750 ms end grace still applies.
                    self.assertFalse(watch.ended)

    def test_fast_start_burst_expires_even_if_host_never_becomes_ready(self):
        reader = mock.Mock(return_value=())
        watch = subject.CaptureWatch(reader, fast_start=True)
        watch.begin()
        for now in (1, 1.06, 1.12, 1.18, 1.24, 1.30, 1.36, 1.42, 1.48, 1.54):
            watch.poll(now)
        self.assertEqual(reader.call_count, 11)
        watch.poll(1.60)
        watch.poll(1.72)
        self.assertEqual(reader.call_count, 11)
        watch.poll(1.80)
        self.assertEqual(reader.call_count, 12)
        self.assertIsNone(watch.identity)

    def test_fast_start_count_limit_survives_an_unreliable_clock(self):
        reader = mock.Mock(return_value=())
        watch = subject.CaptureWatch(reader, fast_start=True)
        watch.begin()
        # A repeated timestamp or external rescheduling cannot replenish budget.
        for _ in range(20):
            watch.next_poll = 0
            watch.poll(1)
        self.assertEqual(watch.next_poll, 1.25)
        self.assertEqual(watch._fast_polls, 10)

    def test_fast_start_keeps_baseline_ambiguity_and_query_failure_guards(self):
        old, new, other = session("old"), session("new"), session("other")
        watch, _ = self.watch((old,), (old,), (old, new, other),
                              OSError("query failed"), (old, new),
                              fast_start=True)
        for now, expected in ((1, "waiting"), (1.06, "ambiguous"), (1.12, "unknown")):
            watch.poll(now)
            self.assertEqual(watch.status, expected)
            self.assertIsNone(watch.identity)
        watch.poll(1.18)
        self.assertEqual(watch.status, "tracking")
        self.assertEqual(watch.identity, new.identity)

    def test_fast_start_failed_baseline_never_queries_or_confirms(self):
        watch, reader = self.watch(OSError("unavailable"), fast_start=True)
        for now in (1, 1.06, 2):
            self.assertFalse(watch.poll(now))
        self.assertIsNone(watch.identity)
        reader.assert_called_once()

    def test_doubao_capture_reader_is_scoped_to_verified_pid(self):
        with mock.patch.object(
            subject.audio,
            "read_capture_sessions",
            return_value=(),
        ) as read:
            subject.read_doubao_capture_for_pid(42)
            read.assert_called_once_with({42})

    def test_process_candidates_are_provider_filtered_and_truncation_rejected(self):
        with mock.patch.object(subject.voice_program_manager, "diagnostic_voice_processes", return_value=(
                ("wetype", 20, "wetype_server.exe"), ("sogou", 30, "other.exe"))), \
             mock.patch.object(subject.audio, "read_capture_sessions", return_value=()) as read:
            subject.read_wetype_capture()
            read.assert_called_once_with({20})
        with mock.patch.object(subject.voice_program_manager, "diagnostic_voice_processes", return_value=(
                ("wetype", 20, "wetype_server.exe"),) * 33):
            with self.assertRaises(OSError):
                subject.wetype_pids()
            with self.assertRaises(OSError):
                subject.wetype_pids(capture=True)

    def test_capture_includes_settings_host_without_widening_input_or_launcher_names(self):
        programs = subject.voice_program_manager
        processes = [SimpleNamespace(pid=pid, name=name) for pid, name in (
            (20, 'wetype_server.exe'), (21, 'wetype_service.exe'),
            (22, 'WeType_Update.exe'), (23, 'wetype_renderer.exe'),
            (24, 'not_wetype_update.exe'), (30, 'sogou_voice_assistant.exe'))]
        def enumerate_names(*, names):
            return tuple(p for p in processes if p.name.casefold() in names)
        with mock.patch.object(programs, '_iter_windows_processes', side_effect=enumerate_names):
            self.assertEqual(subject.wetype_pids(), {20, 21})
            self.assertEqual(subject.wetype_pids(capture=True), {20, 21, 22})
            self.assertNotIn(programs._WETYPE_SETTINGS_EXE, programs._WETYPE_PROCESS_NAMES)
            self.assertNotIn(22, {pid for _, pid, _ in programs.diagnostic_voice_processes()})

    def test_real_candidate_path_tracks_update_capture_and_detects_end(self):
        programs = subject.voice_program_manager
        process = SimpleNamespace(pid=22, name='wetype_update.exe')
        state = [0]
        def enumerate_names(*, names):
            return (process,) if process.name in names else ()
        def capture(pids):
            return (session('update', state[0], 22),) if 22 in pids else ()
        with (mock.patch.object(programs, '_iter_windows_processes', side_effect=enumerate_names),
                mock.patch.object(subject.audio, 'read_capture_sessions', side_effect=capture)):
            watch = subject.CaptureWatch()
            watch.begin()
            state[0] = 1
            self.assertFalse(watch.poll(1))
            self.assertEqual(watch.status, 'tracking')
            self.assertEqual(watch.identity, session('update', pid=22).identity)
            state[0] = 0
            self.assertFalse(watch.poll(2))
            self.assertTrue(watch.poll(2.75))


if __name__ == "__main__":
    unittest.main()
