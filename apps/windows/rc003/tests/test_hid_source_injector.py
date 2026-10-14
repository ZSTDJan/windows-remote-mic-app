import json
import os
from pathlib import Path
import sys
import threading
import unittest
from unittest import mock

from ovb_rc003 import frida_compat as compat
from ovb_rc003 import hid_source_injector as source
from ovb_rc003 import hid_injection_diagnostics as diagnostics
from ovb_rc003 import frida_hid_tap_injector as injector


class SourceInjectorTests(unittest.TestCase):
    def setUp(self):
        self.identity = source.Identity('a' * 32, 'b' * 64)
        self.parent = source.transport.Peer(100, 200, 'sid', 1, 'pythonw.exe')
        self.worker = source.transport.Peer(101, 201, 'sid', 1, 'pythonw.exe')

    def test_source_routes_to_consent_and_never_registered_task_or_direct_child(self):
        registered = mock.Mock()
        cancel = threading.Event()
        with mock.patch.object(source, 'run', return_value=(0, {})) as elevated, \
                mock.patch.object(compat, '_run_direct_injector_subprocess') as direct:
            compat.run_injector_subprocess(321, frozen=False, cancel=cancel,
                _is_elevated=lambda: False, _registered_injector=registered)
        elevated.assert_called_once_with(321, timeout=30.0, cancel=cancel)
        direct.assert_not_called()
        registered.assert_not_called()

    def test_consent_failure_preserves_reason_without_fallback(self):
        error = source.SourceInjectionError('source_authorization_cancelled',
            dict(stage='child_launch', winerror=1223))
        with mock.patch.object(source, 'run', side_effect=error), \
                mock.patch.object(compat, '_run_direct_injector_subprocess') as direct:
            with self.assertRaises(compat.HidTapInjectionError) as caught:
                compat.run_injector_subprocess(321, frozen=False,
                    _is_elevated=lambda: False, _registered_injector=mock.Mock())
        self.assertEqual(str(caught.exception), 'source_authorization_cancelled')
        self.assertEqual(caught.exception.injection_diagnostic['winerror'], 1223)
        self.assertEqual(caught.exception.injection_diagnostic['execution_route'], 'source_elevated')
        direct.assert_not_called()

    def test_elevated_source_keeps_direct_path(self):
        with mock.patch.object(source, 'run') as elevated, \
                mock.patch.object(compat, '_run_direct_injector_subprocess') as direct:
            compat.run_injector_subprocess(321, frozen=False,
                _is_elevated=lambda: True, _registered_injector=mock.Mock())
        elevated.assert_not_called()
        direct.assert_called_once()

    def test_source_child_privilege_failure_is_actionable(self):
        with mock.patch.object(source, 'run', return_value=(4, dict(stage='debug_privilege', winerror=1300))):
            with self.assertRaisesRegex(compat.HidTapInjectionError, 'source_authorization_failed'):
                compat.run_injector_subprocess(321, frozen=False,
                    _is_elevated=lambda: False, _registered_injector=mock.Mock())

    def test_fixed_source_command_uses_real_parent_image_not_venv_redirector(self):
        executable, args = source.build_command(self.parent, self.identity, 321)
        self.assertEqual(executable, self.parent.image)
        self.assertEqual(Path(args[0]), Path(source.__file__).resolve().parents[1] / 'launcher.py')
        self.assertEqual(args[1:7], [source.FLAG, '100', '200', 'a' * 32, 'b' * 64, '321'])

    def test_frames_reject_other_sessions_and_commands(self):
        for payload in (b'{}', source._frame(self.identity, 'received'),
                        source._frame(source.Identity('c' * 32, 'b' * 64), 'start')):
            with self.assertRaises(source.SourceInjectionError):
                source._verify_frame(payload, self.identity, 'start')

    def test_cancelled_request_does_not_launch(self):
        cancel = threading.Event()
        cancel.set()
        with mock.patch.object(source, '_PendingLaunch') as launch:
            with self.assertRaisesRegex(source.SourceInjectionError, 'source_injector_cancelled'):
                source.run(321, cancel=cancel)
        launch.assert_not_called()

    def test_late_uac_response_disposes_only_its_child(self):
        entered, release = threading.Event(), threading.Event()
        def launch(*args):
            entered.set()
            self.assertTrue(release.wait(3))
            return 123
        with mock.patch.object(source, '_launch', side_effect=launch), \
                mock.patch.object(source, '_dispose_process') as dispose:
            pending = source._PendingLaunch(self.parent, self.identity, 321)
            self.assertTrue(entered.wait(3))
            pending.close()
            release.set()
            self.assertTrue(pending.done.wait(3))
        dispose.assert_called_once_with(123)
        self.assertIsNone(pending.handle)

    def _parent_exchange(self, *, peer=None, payload=None, close_error=None):
        pipe = mock.Mock()
        pipe.close.side_effect = close_error
        pipe.k.GetProcessId.return_value = self.worker.pid
        pipe.peer_pid.return_value = self.worker.pid
        pipe.k.WaitForSingleObject.return_value = 0
        def exit_code(handle, pointer):
            pointer._obj.value = 0
            return True
        pipe.k.GetExitCodeProcess.side_effect = exit_code
        record = diagnostics.result_record(success=True, target_pid=321, exit_code=0)
        record['worker_pid'] = self.worker.pid
        pipe.read.return_value = payload or diagnostics.encode(record)
        pending = mock.Mock(handle=123, error=None)
        pending.done.is_set.return_value = True
        with mock.patch.object(source.transport, 'inspect_peer', side_effect=[self.parent, self.worker, peer or self.worker]), \
                mock.patch.object(source, 'ResultPipe', return_value=pipe), \
                mock.patch.object(source, '_PendingLaunch', return_value=pending):
            try:
                return source.run(321), pipe, pending
            finally:
                pipe.close.assert_called_once()
                pending.close.assert_called_once()

    def test_parent_authenticates_result_and_exit(self):
        (code, record), pipe, pending = self._parent_exchange()
        self.assertEqual(code, 0)
        self.assertEqual(record['worker_pid'], self.worker.pid)
        self.assertEqual(pipe.write.call_count, 2)

    def test_parent_rejects_wrong_peer(self):
        with self.assertRaises(source.transport.PipeError):
            self._parent_exchange(peer=self.parent)

    def test_parent_rejects_forged_result(self):
        with self.assertRaises(source.SourceInjectionError):
            self._parent_exchange(payload=b'{"exit_code":0}')

    def test_missing_target_cannot_be_accepted_as_a_successful_result(self):
        record = diagnostics.result_record(success=True, target_pid=321, exit_code=0)
        record['worker_pid'] = self.worker.pid
        record.pop('target_pid')
        with self.assertRaisesRegex(source.SourceInjectionError, 'source_injector_result_invalid'):
            self._parent_exchange(payload=diagnostics.encode(record))

    def test_pipe_close_failure_still_disposes_child(self):
        with self.assertRaisesRegex(RuntimeError, 'close failed'):
            self._parent_exchange(close_error=RuntimeError('close failed'))

    def test_authorization_wait_is_bounded_and_cancel_closes_pending_launch(self):
        for cancelled in (False, True):
            cancel = threading.Event()
            pending = mock.Mock(handle=None, error=None)
            pending.done.is_set.return_value = False
            def wait(_timeout):
                if cancelled:
                    cancel.set()
            pending.done.wait.side_effect = wait
            with self.subTest(cancelled=cancelled), \
                    mock.patch.object(source.transport, 'inspect_peer', return_value=self.parent), \
                    mock.patch.object(source, 'ResultPipe') as pipe, \
                    mock.patch.object(source, '_PendingLaunch', return_value=pending), \
                    mock.patch.object(source, 'AUTHORIZATION_TIMEOUT', .02):
                expected = 'source_injector_cancelled' if cancelled else 'source_authorization_timeout'
                with self.assertRaisesRegex(source.SourceInjectionError, expected):
                    source.run(321, cancel=cancel)
            pending.close.assert_called_once()
            pipe.return_value.close.assert_called_once()
            pipe.return_value.write.assert_not_called()

    def test_worker_rejects_reused_parent_pid_before_injection(self):
        with mock.patch.object(source.transport, 'inspect_peer', return_value=self.parent), \
                mock.patch.object(injector, 'injection_result') as inject, \
                mock.patch.object(source, 'ResultPipe') as pipe:
            code = source.main(['100', '999', self.identity.generation, self.identity.token, '321'])
        self.assertEqual(code, 5)
        inject.assert_not_called()
        pipe.assert_not_called()

    def test_tap_preserves_cancelled_authorization_state(self):
        tap = compat.RC003HidReportTap(mock.Mock(), enabled=False)
        def fail(pid):
            tap.stop_event.set()
            raise compat.HidTapInjectionError('source_authorization_cancelled')
        tap.injector = fail
        with mock.patch.object(compat.frida_hid_tap_runtime, 'find_rc003_hidogatt_host_pid', return_value=321), \
                mock.patch.object(compat.socket, 'socket'):
            tap._run()
        self.assertEqual(tap.status, 'authorization_cancelled')
        tap.report_handler.assert_not_called()

    def test_authorization_failure_never_retries_automatically_after_host_change(self):
        for reason in ('source_authorization_cancelled', 'source_authorization_failed',
                       'source_authorization_timeout'):
            tap = compat.RC003HidReportTap(mock.Mock(), enabled=False)
            tap.injector = mock.Mock(side_effect=compat.HidTapInjectionError(reason))
            waits = []
            def wait(_timeout):
                waits.append(1)
                if len(waits) == 4:
                    tap.stop_event.set()
            with mock.patch.object(compat.frida_hid_tap_runtime, 'find_rc003_hidogatt_host_pid',
                                   side_effect=[321, None, 999]) as lookup, \
                    mock.patch.object(tap.stop_event, 'wait', side_effect=wait), \
                    mock.patch.object(compat.socket, 'socket'):
                tap._run()
            tap.injector.assert_called_once_with(321)
            lookup.assert_called_once()


if __name__ == '__main__':
    unittest.main()
