import ctypes as C
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from ovb_rc003 import chromecast_etw_windows as etw
from ovb_rc003 import chromecast_observation as observation
from ovb_rc003 import chromecast_wetype_finish as finish
from ovb_rc003.chromecast_pipe_windows import Peer, PipeError
from tests import test_chromecast_wetype_toggle as fixtures
from tests.test_chromecast_host_activity import session


class EtwRecoveryTests(unittest.TestCase):
    def start_capture(self, *, query_guid=True, stop_result=0):
        capture = etw.Capture('random-generation')
        api = mock.Mock()
        rows = []
        capture.observe = rows.append
        def claim():
            capture.name = 'RemoteMic-Chromecast-Capture-' + 'a' * 32
            capture.owner_ref = 'a' * 32
            capture._guid = b'1' * 16
            capture._guard = mock.MagicMock()
        def start(pointer, name, storage):
            if api.StartTraceW.call_count == 1:
                return 183
            C.cast(pointer, C.POINTER(C.c_uint64)).contents.value = 19
            return 0
        def control(handle, name, storage, action):
            if action == 0:
                etw.Properties.from_buffer(storage).wnode.guid[:] = b'1' * 16 if query_guid else b'2' * 16
                return 0
            return stop_result
        api.StartTraceW.side_effect = start
        api.ControlTraceW.side_effect = control
        api.EnableTraceEx2.return_value = 0
        api.OpenTraceW.return_value = 22
        api.CloseTrace.return_value = 0
        with mock.patch.object(etw.C, 'WinDLL', return_value=api), \
                mock.patch.object(capture, '_claim_owner', side_effect=claim), \
                mock.patch.object(etw.threading, 'Thread') as thread:
            thread.return_value.is_alive.return_value = False
            try:
                capture.start()
                error = None
            except PipeError as exc:
                error = str(exc)
        return capture, api, rows, error

    def test_exact_owned_orphan_recovered_then_started(self):
        capture, api, rows, error = self.start_capture()
        self.assertIsNone(error)
        self.assertEqual(api.StartTraceW.call_count, 2)
        self.assertEqual([r['operation'] for r in rows], ['start', 'recover_query', 'recover_stop', 'restart'])
        self.assertTrue(all(observation.valid_record(row) for row in rows))
        guard = capture._guard
        capture.stop()
        guard.__exit__.assert_called_once()

    def test_foreign_guid_and_failed_recovery_never_start_second_session(self):
        for kwargs, expected in ((dict(query_guid=False), 'capture_recovery_unconfirmed'),
                                 (dict(stop_result=5), 'capture_cleanup_failed')):
            capture, api, rows, error = self.start_capture(**kwargs)
            self.assertEqual(error, expected)
            self.assertEqual(api.StartTraceW.call_count, 1)
            self.assertIsNone(capture._guard)
            if not kwargs.get('query_guid', True):
                self.assertEqual(api.ControlTraceW.call_count, 1)

    def test_failed_stop_still_closes_consumer_and_joins_then_retries(self):
        capture = etw.Capture('g')
        capture.session, capture.consumer, capture.storage = 11, 22, b'fake'
        capture._guard = mock.MagicMock()
        capture.worker = mock.Mock()
        capture.worker.is_alive.return_value = False
        capture.adv = mock.Mock()
        capture.adv.ControlTraceW.side_effect = [5, 5, 0]
        capture.adv.CloseTrace.return_value = 0
        with self.assertRaises(PipeError):
            capture.stop()
        capture.adv.CloseTrace.assert_called_once_with(22)
        capture.worker.join.assert_called_once_with(2)
        self.assertIsNotNone(capture._guard)
        capture.stop()
        self.assertIsNone(capture._guard)
        self.assertEqual(capture.session, 0)

    def test_live_owner_prevents_start_or_recovery(self):
        peer = Peer(1, 2, 'test-sid', 1, 'image')
        with mock.patch('ovb_rc003.chromecast_pipe_windows.inspect_peer', return_value=peer), \
                mock.patch('ovb_rc003.single_instance.BridgeInstanceGuard') as guard:
            guard.return_value.__enter__.side_effect = RuntimeError('busy')
            capture = etw.Capture('g')
            with self.assertRaisesRegex(PipeError, 'capture_owner_busy'):
                capture._claim_owner()
            self.assertIsNone(capture._guard)


def window_api(rows):
    """Win32 metadata fixture: hwnd -> (pid, class, mark, visible)."""
    api = mock.Mock()
    def enumerate_windows(visit, _):
        for hwnd in rows:
            if not visit(hwnd, 0):
                return False
        return True
    def process_id(hwnd, pointer):
        pointer._obj.value = rows[hwnd][0]
        return 1
    def class_name(hwnd, buffer, capacity):
        name = rows[hwnd][1]
        if name is None:
            return 0
        buffer.value = name
        return len(name)
    api.EnumWindows.side_effect = enumerate_windows
    api.GetWindowThreadProcessId.side_effect = process_id
    api.GetClassNameW.side_effect = class_name
    api.GetPropW.side_effect = lambda hwnd, _: rows[hwnd][2]
    api.IsWindowVisible.side_effect = lambda hwnd: rows[hwnd][3]
    return api, lambda visit: visit


class WindowSelectionTests(unittest.TestCase):
    def test_voice_selected_with_statusbar_menu_and_ordinary_settings_visible(self):
        rows = {
            1: (42, 'wetype.flutter.setting', 1, True),
            2: (42, 'wetype.statusbar.window', 1, True),
            3: (42, 'ConfigMenuWindow', 1, True),
            4: (42, 'wetype.flutter.setting', 0, True),
            5: (43, 'wetype.flutter.setting', 1, True),
            6: (42, 'wetype.flutter.setting', 1, False),
        }
        with mock.patch.object(finish, '_api', return_value=window_api(rows)):
            self.assertEqual(finish._windows(42), (1,))
            rows.pop(1)
            self.assertEqual(finish._windows(42), ())

    def test_two_real_voice_windows_remain_ambiguous(self):
        rows = {1: (42, 'wetype.flutter.setting', 1, True),
                2: (42, 'wetype.flutter.setting', 1, True)}
        with mock.patch.object(finish, '_api', return_value=window_api(rows)):
            self.assertEqual(finish._windows(42), (1, 2))

    def test_unreadable_marked_window_does_not_leave_a_partial_unique_result(self):
        rows = {1: (42, 'wetype.flutter.setting', 1, True),
                2: (42, None, 1, True)}
        with mock.patch.object(finish, '_api', return_value=window_api(rows)):
            with self.assertRaises(OSError):
                finish._windows(42)


class FinishTests(unittest.TestCase):
    def setUp(self):
        self.peer = Peer(42, 123, 'sid', 1, str(Path('wetype_update.exe').absolute()))
        self.target = finish.Target(('endpoint', 'session', 42), self.peer, 99, (10, 20))
        for name, value in [('inspect_peer', self.peer), ('_stamp', (10, 20)),
                            ('_windows', (99,)), ('_active', {self.target.identity}),
                            ('_send', (True, 0))]:
            patcher = mock.patch.object(finish, name, return_value=value)
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)

    def run_finish(self, **kwargs):
        return finish.finish(self.target, deadline=kwargs.get('deadline', time.monotonic() + 1),
                             cancelled=kwargs.get('cancelled', lambda: False))

    def test_single_normal_finish_and_observed_end(self):
        self._active.side_effect = [{self.target.identity}, set()]
        self.assertEqual(self.run_finish(), ('requested', 'capture_ended', 0))
        self._send.assert_called_once_with(99)

    def test_no_message_for_manual_replacement_process_window_or_expired(self):
        for name, value in [('_active', {('endpoint', 'manual', 42)}),
                            ('_windows', (100,)), ('_stamp', (11, 20)),
                            ('inspect_peer', Peer(42, 456, 'sid', 1, self.peer.image))]:
            with self.subTest(name=name), mock.patch.object(finish, name, return_value=value):
                self.run_finish()
                self._send.assert_not_called()
        self.run_finish(deadline=0)
        self.run_finish(cancelled=lambda: True)
        self._send.assert_not_called()

    def test_uncertain_send_never_retried_or_claimed_complete(self):
        self._send.return_value = (False, 1460)
        self.assertEqual(self.run_finish(), ('send_unconfirmed', 'unobserved', 1460))
        self._send.assert_called_once()

    def test_binding_refuses_unknown_binary_and_accepts_only_verified_unique_target(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, 'wetype_update.exe')
            path.write_bytes(b'test fixture, not executable')
            peer = Peer(42, 123, 'sid', 1, str(path))
            self.inspect_peer.return_value = peer
            with mock.patch.object(finish.hashlib, 'file_digest') as digest:
                digest.return_value.hexdigest.return_value = '0' * 64
                self.assertEqual(finish.bind(self.target.identity), (None, 'version_unverified'))
                digest.return_value.hexdigest.return_value = next(iter(finish._VERIFIED))
                target, result = finish.bind(self.target.identity)
                self.assertEqual(result, 'bound')
                self.assertEqual(target.hwnd, 99)
                for windows, captures, reason in (
                    ((), {self.target.identity}, 'voice_window_missing'),
                    ((99, 100), {self.target.identity}, 'voice_window_ambiguous'),
                    ((99,), set(), 'capture_mismatch'),
                    ((99,), {('endpoint', 'another-session', 42)}, 'capture_mismatch'),
                    ((99,), {self.target.identity, ('endpoint', 'another-session', 42)}, 'capture_mismatch'),
                ):
                    with self.subTest(reason=reason, windows=windows, captures=captures):
                        self._windows.return_value = windows
                        self._active.return_value = captures
                        self._active.reset_mock()
                        self.assertEqual(finish.bind(self.target.identity), (None, reason))
                        if len(windows) != 1:
                            self._active.assert_called_once()
            self._send.assert_not_called()


class DelayedBindingTests(unittest.TestCase):
    def setUp(self):
        FinishTests.setUp(self)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name, 'wetype_update.exe')
        self.path.write_bytes(b'non-executable fixture')
        self.peer = Peer(42, 123, 'sid', 1, str(self.path))
        self.inspect_peer.return_value = self.peer
        patcher = mock.patch.object(finish.hashlib, 'file_digest')
        self.digest = patcher.start()
        self.addCleanup(patcher.stop)
        self.digest.return_value.hexdigest.return_value = next(iter(finish._VERIFIED))
        self.binding = finish.TargetBinding()

    def poll(self, now, *, state='tracking', identity=None):
        return self.binding.poll(identity or self.target.identity, state, now)

    def test_delayed_window_is_bound_without_busy_polling_or_rebinding(self):
        self._windows.return_value = ()
        self.assertEqual(self.poll(10), 'voice_window_missing')
        self._windows.return_value = (99,)
        self.assertIsNone(self.poll(10.1))
        self._windows.assert_called_once()
        self.assertEqual(self.poll(10.25), 'bound')
        self.assertEqual(self.binding.target.hwnd, 99)
        self.assertIsNone(self.poll(200))
        self.assertEqual(self._windows.call_count, 2)

    def test_window_wait_expires_and_never_adopts_a_late_window(self):
        self._windows.return_value = ()
        for now in (10, 10.25, 10.5, 10.75):
            self.assertEqual(self.poll(now), 'voice_window_missing')
        self._windows.return_value = (99,)
        self.assertEqual(self.poll(11), 'window_wait_expired')
        self.assertIsNone(self.poll(12))
        self.assertIsNone(self.binding.target)
        self.assertEqual(self._windows.call_count, 4)

    def test_real_ambiguity_and_unknown_version_are_not_retried(self):
        for windows, digest, reason in (
            ((99, 100), next(iter(finish._VERIFIED)), 'voice_window_ambiguous'),
            ((99,), '0' * 64, 'version_unverified'),
        ):
            with self.subTest(reason=reason):
                self.binding = finish.TargetBinding()
                self._windows.return_value = windows
                self.digest.return_value.hexdigest.return_value = digest
                self.assertEqual(self.poll(10), reason)
                self._windows.return_value = (99,)
                self.digest.return_value.hexdigest.return_value = next(iter(finish._VERIFIED))
                self.assertIsNone(self.poll(10.25))
                self.assertIsNone(self.binding.target)

    def test_capture_gap_or_replacement_cancels_pending_and_bound_targets(self):
        for initial_windows in ((), (99,)):
            for state, identity in (('unknown', self.target.identity),
                                    ('ending', self.target.identity),
                                    ('tracking', ('capture', 'replacement', 42))):
                with self.subTest(initial_windows=initial_windows, state=state):
                    self.binding = finish.TargetBinding()
                    self._windows.return_value = initial_windows
                    self.poll(10)
                    self.assertEqual(self.poll(10.1, state=state, identity=identity), 'capture_changed')
                    self._windows.return_value = (99,)
                    self.assertIsNone(self.poll(10.25))
                    self.assertIsNone(self.binding.target)

    def test_reused_pid_or_replaced_binary_cancels_retry(self):
        for name, value in (('inspect_peer', Peer(42, 456, 'sid', 1, str(self.path))),
                            ('_stamp', (11, 20))):
            with self.subTest(name=name):
                self.binding = finish.TargetBinding()
                self._windows.return_value = ()
                self.poll(10)
                self._windows.return_value = (99,)
                with mock.patch.object(finish, name, return_value=value):
                    self.assertEqual(self.poll(10.25), 'process_changed')
                self.assertIsNone(self.poll(10.5))
                self.assertIsNone(self.binding.target)

    def test_capture_loss_during_missing_window_is_terminal(self):
        self._windows.return_value = ()
        self._active.return_value = set()
        self.assertEqual(self.poll(10), 'capture_mismatch')
        self._active.return_value = {self.target.identity}
        self._windows.return_value = (99,)
        self.assertIsNone(self.poll(10.25))
        self.assertIsNone(self.binding.target)

    def test_stopping_cancels_pending_window_discovery(self):
        self._windows.return_value = ()
        self.poll(10)
        self.assertIsNone(self.binding.take_target())
        self._windows.return_value = (99,)
        self.assertIsNone(self.poll(10.25))
        self._send.assert_not_called()


class HostFinishTests(unittest.TestCase):
    # Reuse the existing fixture, not its entire test suite.
    def setUp(self):
        fixtures.ToggleHostTests.setUp(self)
        for name, value in (('inspect_peer', Peer(20, 123, 'sid', 1, 'wetype_update.exe')),
                            ('_stamp', (10, 20))):
            patcher = mock.patch.object(finish, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
    _confirm_capture = fixtures.ToggleHostTests._confirm_capture

    def test_device_stop_finishes_only_confirmed_owned_attempt_once(self):
        from ovb_rc003 import chromecast_voice_host as host_module
        with mock.patch.object(host_module.chromecast_wetype_finish, 'bind', return_value=('owned', 'bound')), \
                mock.patch.object(host_module.chromecast_wetype_finish, 'finish', return_value=('requested', 'capture_ended', 0)) as end:
            self.host._start_host(1)
            self._confirm_capture()
            self.host.state = 'recording'
            self.host._handle(dict(event='host_stop', attempt=1, data='device_ended'))
            self.host._handle(dict(event='host_stop', attempt=1, data='device_ended'))
            end.assert_called_once()
            self.assertEqual(end.call_args.args, ('owned',))
            self.tap.assert_called_once()

    def test_unconfirmed_start_and_plain_cleanup_do_not_finish(self):
        from ovb_rc003 import chromecast_voice_host as host_module
        with mock.patch.object(host_module.chromecast_wetype_finish, 'finish') as end:
            self.host._start_host(1)
            self.host._stop_host('device_ended')
            end.assert_not_called()

    def test_capture_gap_drops_ownership_without_adopting_later_speech(self):
        from ovb_rc003 import chromecast_voice_host as host_module
        with mock.patch.object(host_module.chromecast_wetype_finish, 'bind', return_value=('owned', 'bound')) as bind:
            self.host._start_host(1)
            self._confirm_capture()
            watch = self.host.capture_watch
            watch.next_poll = 0
            watch.reader.side_effect = OSError('unknown capture')
            self.host._poll_host()
            self.assertIsNone(self.host._wetype_finish_binding.target)
            watch.next_poll = 0
            watch.reader.side_effect = None
            self.host._poll_host()
            self.assertIsNone(self.host._wetype_finish_binding.target)
            bind.assert_called_once()

    def test_delayed_voice_with_statusbar_and_menu_finishes_once_at_time_limit(self):
        identity = session('wetype-capture').identity
        rows = {2: (20, 'wetype.statusbar.window', 1, True),
                3: (20, 'ConfigMenuWindow', 1, True)}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, 'wetype_update.exe')
            path.write_bytes(b'non-executable fixture')
            peer = Peer(20, 123, 'sid', 1, str(path))
            with (mock.patch.object(finish, 'inspect_peer', return_value=peer),
                  mock.patch.object(finish.hashlib, 'file_digest') as digest,
                  mock.patch.object(finish, '_api', return_value=window_api(rows)),
                  mock.patch.object(finish, '_active', return_value={identity}) as active,
                  mock.patch.object(finish, '_send', return_value=(True, 0)) as send,
                  mock.patch('ovb_rc003.chromecast_voice_host.time.monotonic', return_value=10) as clock):
                digest.return_value.hexdigest.return_value = next(iter(finish._VERIFIED))
                self.host._start_host(1)
                self._confirm_capture()
                self.assertIsNone(self.host._wetype_finish_binding.target)
                rows[1] = (20, 'wetype.flutter.setting', 1, True)
                clock.return_value = 10.25
                self.host._poll_host()
                self.assertEqual(self.host._wetype_finish_binding.target.hwnd, 1)
                self.host.state = 'recording'
                clock.return_value = 130
                active.side_effect = [{identity}, set()]
                self.host._handle(dict(event='host_stop', attempt=1, data='time_limit'))
                self.host._handle(dict(event='host_stop', attempt=1, data='time_limit'))
                send.assert_called_once_with(1)
                self.tap.assert_called_once()
                self.assertTrue(self.host.closed)
