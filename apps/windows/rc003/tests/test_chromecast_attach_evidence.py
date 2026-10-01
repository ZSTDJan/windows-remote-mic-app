"""Failure evidence must survive real collection, IPC and the exported ZIP."""
import importlib.util
import ctypes
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import zipfile

from ovb_rc003 import chromecast_attach_diagnostics_windows as native
from ovb_rc003 import chromecast_hid_tap_windows as tap
from ovb_rc003 import chromecast_diagnostics_windows as environment
from ovb_rc003.chromecast_observation import Observation, valid_record
from ovb_rc003.chromecast_channel import Channel, SessionIdentity
from ovb_rc003 import log_export


class AttachEvidenceTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == 'win32', 'Windows last-error semantics')
    def test_helper_snapshot_failure_and_unreadable_exit_are_incomplete(self):
        monitor = native.HelperLifecycle(lambda *_args, **_kw: None)
        monitor.k = mock.Mock()
        monitor.k.CreateToolhelp32Snapshot.return_value = 123
        monitor.k.Process32FirstW.return_value = True
        def failed_next(*_args):
            ctypes.set_last_error(5)
            return False
        monitor.k.Process32NextW.side_effect = failed_next
        with self.assertRaises(OSError):
            monitor._sample()
        monitor.k.CloseHandle.assert_called_once_with(123)

        rows = []
        monitor = native.HelperLifecycle(lambda step, state, **kw:
            rows.append(tap.startup_record(step, state, **kw)))
        fake_kernel = mock.Mock()
        fake_kernel.WaitForSingleObject.return_value = 0xffffffff
        fake_kernel.GetExitCodeProcess.return_value = False
        def one_sample():
            monitor.samples += 1
            monitor.candidates[10] = dict(role='manager', pid=10, path='private/one',
                                          created=1, handle=123, sync=True)
        with mock.patch.object(native, 'api', return_value=(fake_kernel, None)), \
             mock.patch.object(monitor, '_sample', side_effect=one_sample):
            monitor.start()
            monitor.finish()
        self.assertEqual(rows[0]['details']['query_complete'], 0)
        self.assertEqual(rows[1]['details']['alive'], -1)
        self.assertTrue(all(row['state'] == 'failed' and valid_record(row) for row in rows))
        fake_kernel.CloseHandle.assert_called_once_with(123)

    @unittest.skipUnless(sys.platform == 'win32', 'Windows process snapshot layout')
    def test_helper_scan_finds_both_standalone_and_windows_service_children(self):
        monitor = native.HelperLifecycle(lambda *_args, **_kw: None)
        monitor.candidates[10] = dict(role='manager', pid=10, path='private/one',
                                      created=100, handle=123, sync=True)
        monitor.k = mock.Mock()
        monitor.k.CreateToolhelp32Snapshot.return_value = 111
        entries = [(4, 0, 'services.exe'), (11, 10, 'frida-helper-x86_64.exe'),
                   (12, 4, 'frida-helper-x86_64.exe'),
                   (13, 4, 'frida-helper-x86_64.exe')]
        cursor = iter(entries)
        def next_entry(_snapshot, pointer):
            try:
                pid, parent, name = next(cursor)
            except StopIteration:
                ctypes.set_last_error(18)
                return False
            entry = ctypes.cast(pointer, ctypes.POINTER(native._ProcessEntry)).contents
            entry.pid, entry.parent, entry.name = pid, parent, name
            return True
        monitor.k.Process32FirstW.side_effect = next_entry
        monitor.k.Process32NextW.side_effect = next_entry
        monitor.k.OpenProcess.side_effect = [211, 212, 213]
        def image_name(_handle, _flags, buffer, _size):
            buffer.value = 'private/other' if _handle == 213 else 'private/one'
            return True
        def process_times(_handle, created, *_rest):
            value = ctypes.cast(created, ctypes.POINTER(native.W.FILETIME)).contents
            value.dwLowDateTime, value.dwHighDateTime = 200, 0
            return True
        monitor.k.QueryFullProcessImageNameW.side_effect = image_name
        monitor.k.GetProcessTimes.side_effect = process_times
        monitor._sample()
        self.assertEqual({pid: item['role'] for pid, item in monitor.candidates.items()},
                         {10: 'manager', 11: 'service', 12: 'service'})
        self.assertEqual(monitor.samples, 1)
        self.assertFalse(monitor.limited)
        self.assertEqual(monitor.query_errors, 0)
        self.assertEqual(monitor.k.CloseHandle.call_count, 2)  # snapshot and unrelated image

    def test_helper_observer_busy_does_not_start_another_thread(self):
        rows = []
        monitor = native.HelperLifecycle(lambda step, state, **kw:
            rows.append(tap.startup_record(step, state, **kw)))
        with mock.patch.object(native, '_HELPER_SLOT') as slot:
            slot.acquire.return_value = False
            monitor.start()
            monitor.finish()
            slot.release.assert_not_called()
        self.assertIsNone(monitor.thread)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['details']['query_complete'], 0)
        self.assertEqual(rows[0]['details']['scan_limited'], 1)
        self.assertTrue(valid_record(rows[0]))

    def test_blocked_helper_probe_keeps_single_observer_slot(self):
        entered, release = threading.Event(), threading.Event()
        first = native.HelperLifecycle(lambda *_args, **_kw: None)
        rows = []
        second = native.HelperLifecycle(lambda step, state, **kw:
            rows.append(tap.startup_record(step, state, **kw)))
        def blocked_sample():
            entered.set()
            self.assertTrue(release.wait(2))
        try:
            with mock.patch.object(native, 'api', return_value=(mock.Mock(), None)), \
                 mock.patch.object(first, '_sample', side_effect=blocked_sample):
                first.start()
                self.assertTrue(entered.wait(1))
                second.start()
                second.finish()
                self.assertIsNone(second.thread)
                self.assertEqual(rows[0]['details']['query_complete'], 0)
                first.finish()
                self.assertTrue(first.thread.is_alive())
        finally:
            release.set()
            if first.thread is not None:
                first.thread.join(2)
        self.assertFalse(first.thread.is_alive())

    def test_helper_summary_ignores_unrelated_service_path_and_marks_incomplete(self):
        rows = []
        monitor = native.HelperLifecycle(lambda step, state, **kw:
            rows.append(tap.startup_record(step, state, **kw)))
        monitor.thread = mock.Mock()
        monitor.thread.is_alive.return_value = False
        monitor.samples = 3
        monitor.candidates = {
            10: dict(role='manager', pid=10, path='private/one', created=0x100000002,
                     alive=0, exit_code=5),
            11: dict(role='service', pid=11, path='private/one', created=0x300000004,
                     alive=0, exit_code=6),
            12: dict(role='service', pid=12, path='private/other', created=0x500000006,
                     alive=1, exit_code=-1),
        }
        monitor.finish()
        self.assertTrue(all(valid_record(row) for row in rows))
        self.assertEqual([row['details']['helper_count'] for row in rows[1:]], [1, 1])
        self.assertEqual([row['details']['exit_code'] for row in rows[1:]], [5, 6])
        self.assertEqual([row['details']['created_high'] for row in rows[1:]], [1, 3])
        self.assertNotIn('private', json.dumps(rows))
        rows.clear()
        monitor.limited = True
        monitor.finish()
        self.assertEqual(rows[0]['details']['query_complete'], 0)
        self.assertTrue(all(row['state'] == 'failed' for row in rows))

    @unittest.skipUnless(sys.platform == 'win32', 'Windows process lifecycle')
    def test_helper_lifecycle_observes_only_owned_process_and_redacts_path(self):
        rows = []
        with tempfile.TemporaryDirectory() as root:
            helper = Path(root) / 'frida-helper-x86_64.exe'
            shutil.copy2(Path(os.environ['SystemRoot']) / 'System32' / 'ping.exe', helper)
            child = subprocess.Popen([str(helper), '-n', '30', '127.0.0.1'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW)
            monitor = native.HelperLifecycle(lambda step, state, **kw:
                rows.append(tap.startup_record(step, state, **kw)))
            try:
                monitor.start()
                time.sleep(.12)
                child.terminate()
                child.wait(timeout=5)
                monitor.finish()
            finally:
                if child.poll() is None:
                    child.terminate()
                    child.wait(timeout=5)
                if monitor.thread is not None and monitor.thread.is_alive():
                    monitor.stop_requested.set()
                    monitor.thread.join(2)
        self.assertTrue(all(valid_record(row) for row in rows))
        self.assertEqual([row['step'] for row in rows],
                         ['attach_helper_scan', 'attach_helper_manager', 'attach_helper_service'])
        self.assertEqual(rows[0]['details']['query_complete'], 1)
        self.assertEqual(rows[1]['details']['helper_count'], 1)
        self.assertEqual(rows[1]['details']['helper_pid'], child.pid)
        self.assertEqual(rows[1]['details']['alive'], 0)
        self.assertEqual(rows[2]['details']['helper_count'], 0)
        identity = SessionIdentity.create('a' * 64)
        sender, receiver = Channel(identity, commands=False), Channel(identity, commands=False)
        self.assertEqual([receiver.decode(sender.encode('evidence', record=row))['record'] for row in rows], rows)
        self.assertNotIn(root, json.dumps(rows))

    @unittest.skipUnless(sys.platform == 'win32', 'Windows file attributes')
    def test_agent_scan_covers_normal_and_elevated_helper_layouts(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            normal = root / 'temp' / 'frida-owned' / 'x86_64' / 'frida-agent.dll'
            elevated = root / 'programs' / 'Frida' / ('a' * 40) / 'frida-agent.dll'
            unrelated = root / 'programs' / 'Frida' / 'unrelated' / 'frida-agent.dll'
            for path in (normal, elevated, unrelated):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'fixture')
            with mock.patch.dict(os.environ, TEMP=str(root / 'temp'), TMP=str(root / 'temp'),
                                 TMPDIR=str(root / 'temp'), ProgramFiles=str(root / 'programs')), \
                 mock.patch.object(native.tempfile, 'gettempdir', return_value=str(root / 'temp')):
                found, summary = native.agent_candidates()
                rows = []
                evidence = native.AttachEvidence(42, lambda step, state, **kw:
                    rows.append(tap.startup_record(step, state, **kw)))
                evidence._query('attach_assets_after', evidence._assets_after)
            self.assertEqual(set(found), {normal, elevated})
            self.assertEqual(summary, {'scan_errors': 0, 'scan_limited': 0, 'candidates': 2})
            self.assertTrue(all(valid_record(row) for row in rows))
            files = [r for r in rows if r['step'] == 'attach_asset_file']
            self.assertEqual([r['details']['asset_location'] for r in files], ['elevated_helper', 'temporary'])
            self.assertNotIn(str(root), json.dumps(rows))

    def test_query_results_distinguish_unavailable_partial_and_captured(self):
        rows = []
        evidence = native.AttachEvidence(42, lambda step, state, **kw:
            rows.append(tap.startup_record(step, state, **kw)))
        for details in ({'process_handle': 0}, {'token_available': 0, 'read_execute': -1},
                        {'signature_policy': -1, 'signature_policy_error': 5},
                        {'scan_errors': 1}, {'hash_complete': 0},
                        {'process_changed': 1}, {'read_execute': 0, 'token_available': 1}):
            evidence._query('attach_asset_access', lambda details=details: details)
        self.assertEqual([r['details']['query_outcome'] for r in rows],
                         ['unavailable', 'unavailable', 'partial', 'partial', 'partial', 'unavailable', 'captured'])
        self.assertEqual([r['state'] for r in rows], ['failed'] * 6 + ['success'])
        self.assertTrue(evidence.incomplete)
        self.assertTrue(all(valid_record(r) for r in rows))

    @unittest.skipUnless(sys.platform == 'win32', 'Windows process access evidence')
    def test_native_denied_query_is_incomplete_and_survives_export(self):
        # Deny access only to our own child. No Frida attach, elevation, device
        # access or machine-wide security changes are involved in this fixture.
        import ctypes as C
        from ctypes import wintypes as W
        from ovb_rc003.chromecast_pipe_windows import api, _bind
        k, a = api()
        _bind(a, 'GetKernelObjectSecurity', W.BOOL, W.HANDLE, W.DWORD, W.LPVOID, W.DWORD, C.POINTER(W.DWORD))
        _bind(a, 'SetKernelObjectSecurity', W.BOOL, W.HANDLE, W.DWORD, W.LPVOID)
        _bind(a, 'ConvertStringSecurityDescriptorToSecurityDescriptorW', W.BOOL,
              W.LPCWSTR, W.DWORD, C.POINTER(W.LPVOID), W.LPVOID)
        def checked(value):
            if not value:
                raise C.WinError(C.get_last_error())
        child = subprocess.Popen([os.environ['SystemRoot'] + r'\System32\ping.exe', '-n', '30', '127.0.0.1'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
        descriptor, saved = W.LPVOID(), None
        rows = []
        def emit(step, state, **kw):
            rows.append(tap.startup_record(step, state, host_pid=child.pid, **kw))
        try:
            needed = W.DWORD()
            a.GetKernelObjectSecurity(int(child._handle), 4, None, 0, C.byref(needed))
            saved = C.create_string_buffer(needed.value)
            checked(a.GetKernelObjectSecurity(int(child._handle), 4, saved, len(saved), C.byref(needed)))
            checked(a.ConvertStringSecurityDescriptorToSecurityDescriptorW('D:(A;;GA;;;SY)', 1, C.byref(descriptor), None))
            checked(a.SetKernelObjectSecurity(int(child._handle), 4, descriptor))
            evidence = native.BoundedAttachEvidence(child.pid, emit)
            with mock.patch.object(native, 'agent_candidates', return_value=({}, {'candidates': 0})):
                evidence.begin()
                evidence.finish(RuntimeError('the connection is closed'))
                self.assertTrue(evidence.done.wait(2))
            denied = [r for r in rows if r['step'] == 'attach_process_before']
            self.assertEqual(len(denied), 1)
            if denied[0]['state'] != 'failed':
                self.skipTest('caller already has process DACL bypass privilege')
            self.assertEqual(denied[0]['native_code'], 5)
            unavailable = [r for r in rows if r['details'].get('process_handle') == 0]
            self.assertTrue(unavailable)
            self.assertTrue(all(r['state'] == 'failed' and r['details']['query_outcome'] == 'unavailable'
                                for r in unavailable))
            phases = [r for r in rows if r['step'] == 'attach_evidence']
            self.assertEqual([r['details']['sample_phase'] for r in phases], ['before', 'after'])
            self.assertTrue(all(r['state'] == 'failed' and r['details']['query_complete'] == 0 for r in phases))
            # Restoring our child's original DACL proves queries recover without
            # changing a real device or granting the test runner new privileges.
            checked(a.SetKernelObjectSecurity(int(child._handle), 4, saved))
            recovered = native.AttachEvidence(child.pid, emit)
            try:
                recovered._query('attach_process_before', recovered._open)
                self.assertFalse(recovered.incomplete)
            finally:
                recovered.close()
        finally:
            if saved is not None:
                a.SetKernelObjectSecurity(int(child._handle), 4, saved)
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=5)
            if descriptor:
                k.LocalFree(descriptor)
        identity = SessionIdentity.create('a' * 64)
        send, receive = Channel(identity, commands=False), Channel(identity, commands=False)
        records = [receive.decode(send.encode('evidence', record=r))['record'] for r in rows]
        with tempfile.TemporaryDirectory() as root:
            logs = Path(root) / 'logs'; logs.mkdir()
            (logs / 'app.log').write_text('\n'.join(json.dumps(r) for r in records), encoding='utf-8')
            archive = Path(root) / 'query-evidence.zip'
            self.assertEqual(log_export.export_logs(archive, root=Path(root)).outcome, 'exported')
            with zipfile.ZipFile(archive) as z:
                exported = [json.loads(line) for line in z.read('app.log').decode('utf-8').splitlines()]
        self.assertEqual(records, exported)
        self.assertTrue(any(r['details'].get('error_family') == 'connection_closed' for r in exported))

    def test_blocked_before_query_preserves_attach_error_and_bounds_retries(self):
        entered, release = threading.Event(), threading.Event()
        samplers, closed_by = [], []
        original = RuntimeError('LoadLibraryW failed: error=126 PRIVATE')
        caller = threading.get_ident()
        factory = native.BoundedAttachEvidence
        def create(*args):
            sampler = factory(*args); samplers.append(sampler); return sampler
        def blocked():
            entered.set(); release.wait(5); return {'query_complete': 1}
        instance = tap.HidTap('a'*64)
        with mock.patch.object(native, 'BoundedAttachEvidence', side_effect=create), \
             mock.patch.object(native.AttachEvidence, '_controller', side_effect=blocked) as query, \
             mock.patch.object(native.AttachEvidence, '_open') as later, \
             mock.patch.object(native.AttachEvidence, 'close', side_effect=lambda: closed_by.append(threading.get_ident())):
            try:
                frida = mock.Mock(); frida.attach.side_effect = original
                started = time.monotonic()
                with self.assertRaises(RuntimeError) as caught:
                    instance._attach(frida, 42)
                self.assertLess(time.monotonic()-started, 2)
                self.assertTrue(entered.is_set())
                self.assertIs(caught.exception, original)
                self.assertFalse(closed_by)  # Caller must not close handles still in use.
                rows = instance.poll_startup()
                self.assertTrue(any(r['details'].get('sample_outcome') == 'timeout' and
                                    r['details']['sample_phase'] == 'before' for r in rows))
                self.assertTrue(any(r['step'] == 'attach_exception' and
                                    r['details'].get('reported_code') == 126 for r in rows))
                again = tap.HidTap('a'*64)
                frida.attach.side_effect = None
                self.assertIs(again._attach(frida, 42), frida.attach.return_value)
                self.assertTrue(any(r['details'].get('sample_outcome') == 'busy' for r in again.poll_startup()))
                self.assertEqual(query.call_count, 1)
            finally:
                release.set()
                self.assertTrue(samplers[0].done.wait(2))
            later.assert_not_called()
            self.assertEqual(len(closed_by), 1)
            self.assertNotEqual(closed_by[0], caller)
            self.assertFalse(instance.poll_startup())  # No late rows masquerading as pre-attach evidence.
        self.assertTrue(all(valid_record(row) for row in rows))

    def test_blocked_after_query_returns_session_and_timeout_survives_ipc_export(self):
        entered, release = threading.Event(), threading.Event()
        samplers = []
        factory = native.BoundedAttachEvidence
        def create(*args):
            sampler = factory(*args); samplers.append(sampler); return sampler
        def blocked():
            entered.set(); release.wait(5); return {'alive': 1}
        instance = tap.HidTap('a'*64)
        with mock.patch.object(native, 'BoundedAttachEvidence', side_effect=create), \
             mock.patch.object(native.AttachEvidence, 'begin'), \
             mock.patch.object(native.AttachEvidence, '_state', side_effect=blocked), \
             mock.patch.object(native.AttachEvidence, '_modules') as later, \
             mock.patch.object(native.AttachEvidence, 'close') as close:
            try:
                frida = mock.Mock()
                started = time.monotonic()
                self.assertIs(instance._attach(frida, 42), frida.attach.return_value)
                self.assertLess(time.monotonic()-started, 2)
                self.assertTrue(entered.is_set())
                close.assert_not_called()
                rows = instance.poll_startup()
            finally:
                release.set()
                self.assertTrue(samplers[0].done.wait(2))
            close.assert_called_once()
            later.assert_not_called()
            self.assertFalse(instance.poll_startup())
        identity = SessionIdentity.create('a'*64)
        send, receive = Channel(identity, commands=False), Channel(identity, commands=False)
        saved = []
        obs = Observation(lambda r: saved.append(receive.decode(send.encode('evidence', record=r))['record']))
        for row in rows:
            obs.hid_startup(row)
        self.assertEqual(obs.counts['metadata_rejected'], 0)
        with tempfile.TemporaryDirectory() as root:
            logs = Path(root)/'logs'; logs.mkdir()
            (logs/'app.log').write_text('\n'.join(json.dumps(row) for row in saved), encoding='utf-8')
            archive = Path(root)/'timeout.zip'
            self.assertEqual(log_export.export_logs(archive, root=Path(root)).outcome, 'exported')
            with zipfile.ZipFile(archive) as z:
                exported = [json.loads(s) for s in z.read('app.log').decode('utf-8').splitlines()]
        self.assertEqual(exported, saved)
        self.assertTrue(any(r['details'].get('sample_outcome') == 'timeout' and
                            r['details']['sample_phase'] == 'after' and
                            r['details']['query_complete'] == 0 for r in exported))

    def test_sampler_thread_start_failure_releases_slot_and_attach_still_runs(self):
        instance = tap.HidTap('a'*64)
        with mock.patch.object(native.threading.Thread, 'start', side_effect=RuntimeError('start failed')):
            frida = mock.Mock()
            self.assertIs(instance._attach(frida, 42), frida.attach.return_value)
        self.assertTrue(native._SAMPLER_SLOT.acquire(blocking=False))
        native._SAMPLER_SLOT.release()
        self.assertTrue(any(r['step'] == 'attach_evidence' and r['state'] == 'failed'
                            for r in instance.poll_startup()))

    def test_error_classes_context_and_unknown_error_identity_are_preserved(self):
        for message, family in (
            ('process refused to load frida-agent, or terminated during injection', 'agent_load_or_process_exit'),
            ('Access is denied: PRIVATE_PATH', 'access_denied'),
            ('the connection is closed', 'connection_closed'),
            ('request timed out PRIVATE', 'timeout'),
            ('unknown PRIVATE_ERROR', 'unclassified'),
        ):
            details = native.exception_details(RuntimeError(message))
            self.assertEqual(details['error_family'], family)
            self.assertNotIn('PRIVATE', json.dumps(details))
            self.assertTrue(valid_record(tap.startup_record('attach_exception', 'failed', details=details)))
        details = native.exception_details(RuntimeError('LoadLibraryW failed: error=126 PRIVATE'))
        self.assertEqual(details['reported_code'], 126)
        self.assertEqual(details['native_api'], 'LoadLibraryW')
        self.assertNotIn('reported_code', native.exception_details(RuntimeError('process 126 PRIVATE')))
        error = RuntimeError('wrapper')
        error.__context__ = OSError(5, 'PRIVATE')
        self.assertEqual(tap.startup_record('attach', 'failed', error=error)['cause_code'], 5)

    def test_rejected_rows_are_counted_and_original_attach_error_is_unchanged(self):
        instance = tap.HidTap('a'*64)
        instance._startup_record('attach', 'success', details={'not_allowed': 'PRIVATE'})
        rows = instance.poll_startup()
        self.assertEqual(rows[0]['reason'], 'records_rejected')
        self.assertEqual(rows[0]['details']['dropped'], 1)
        observation = Observation(lambda row: None)
        observation.hid_startup(None)
        self.assertEqual(observation.counts['metadata_rejected'], 1)
        original = RuntimeError('original attach failure')
        with mock.patch.object(native.AttachEvidence, 'begin', side_effect=ValueError('query failed')), \
             mock.patch.object(native.AttachEvidence, 'finish', side_effect=ValueError('query failed')), \
             mock.patch.object(native.AttachEvidence, 'close') as close:
            frida = mock.Mock(); frida.attach.side_effect = original
            with self.assertRaises(RuntimeError) as caught:
                instance._attach(frida, 42)
        self.assertIs(caught.exception, original)
        close.assert_called_once()

    def test_independent_system_sources_finish_when_pnp_times_out(self):
        def collect(query, **kwargs):
            if 'Get-PnpDevice)' in query:
                raise subprocess.TimeoutExpired('PRIVATE', kwargs['timeout'])
            return {'query_stage': 'complete'}
        with mock.patch.object(environment, '_raw_snapshot', return_value={}), \
             mock.patch.object(environment, '_hid_driver_snapshot', return_value={}), \
             mock.patch('ovb_rc003.chromecast_etw_windows.capture_settings_snapshot', return_value={}), \
             mock.patch.object(environment, '_environment', side_effect=collect) as query, \
             self.assertLogs('ovb_rc003', level='INFO') as logs:
            environment._collect('a'*64, 'hid_capture_failed', 123)
        rows = [json.loads(line.split('Chromecast system evidence: ')[1]) for line in logs.output
                if 'Chromecast system evidence: ' in line]
        self.assertEqual([r['part'] for r in rows], ['registry', 'events', 'drivers', 'pnp'])
        self.assertEqual(rows[-1]['environment']['error'], 'query_timeout')
        self.assertTrue(all('error' not in r['environment'] for r in rows[:-1]))
        self.assertEqual([c.kwargs['timeout'] for c in query.call_args_list], [3,5,5,8])

    @unittest.skipUnless(sys.platform == 'win32', 'Windows child deadline')
    def test_real_slow_query_is_reaped_and_next_source_still_reaches_export(self):
        children = []
        real_popen = subprocess.Popen
        def launch(*args, **kwargs):
            child = real_popen(*args, **kwargs); children.append(child); return child
        with tempfile.TemporaryDirectory() as root:
            logs = Path(root)/'logs'; logs.mkdir()
            handler = logging.FileHandler(logs/'app.log', encoding='utf-8')
            logger = logging.getLogger('ovb_rc003'); before = logger.level
            logger.setLevel(logging.INFO); logger.addHandler(handler)
            try:
                with mock.patch.object(environment, '_QUERY_PARTS', (
                    ('blocked', "Snapshot 'before'; Start-Sleep -Seconds 10", .5),
                    ('independent', "Snapshot 'complete'", 5))), \
                     mock.patch.object(environment, '_raw_snapshot', return_value={}), \
                     mock.patch.object(environment, '_hid_driver_snapshot', return_value={}), \
                     mock.patch('ovb_rc003.chromecast_etw_windows.capture_settings_snapshot', return_value={}), \
                     mock.patch.object(environment.subprocess, 'Popen', side_effect=launch):
                    environment._collect('a'*64, 'hid_capture_failed', 123)
                handler.flush()
                archive = Path(root)/'query.zip'
                self.assertEqual(log_export.export_logs(archive, root=Path(root)).outcome, 'exported')
                with zipfile.ZipFile(archive) as z:
                    content = z.read('app.log').decode('utf-8')
                rows = [json.loads(s.split('Chromecast system evidence: ')[1]) for s in content.splitlines()
                        if 'Chromecast system evidence: ' in s]
                self.assertEqual(rows[0]['environment']['error'], 'query_timeout')
                self.assertEqual(rows[1]['environment']['query_stage'], 'complete')
                self.assertTrue(all(c.poll() is not None for c in children))
                self.assertEqual(len(children), 2)
            finally:
                logger.removeHandler(handler); handler.close(); logger.setLevel(before)

    def test_buttons_voice_and_missing_evidence_remain_distinct_in_export(self):
        identity = SessionIdentity.create('a'*64)
        send, receive = Channel(identity, commands=False), Channel(identity, commands=False)
        rows=[]
        obs=Observation(lambda r:rows.append(receive.decode(send.encode('evidence', record=r))['record']))
        obs.stage('hid_primary_ready'); obs.count('hid_edges', 2)
        for outcome, code in (('timeout', -1), ('os_error', 0x80070005), ('cancelled', -1)):
            obs.voice_query(dict(step='services', cache='live', duration_ms=10000,
                connected_before=0, connected_after=0, outcome=outcome, status=-1,
                protocol_error=-1, hresult=code, item_count=0, items=[]), 1)
        obs.voice_link(dict(kind='voice_link', step='characteristics', outcome='status_failed', duration_ms=3,
            hresult=-1, connected=1, device_access=1, service_access=2, sharing=1,
            service_handle=0x50, session_status=1, maintain=1, can_maintain=1), 1)
        obs.voice_notification('audio', b'PRIVATE_AUDIO')
        obs.hid_startup(None)
        obs.flush(final=True)
        with tempfile.TemporaryDirectory() as root:
            logs=Path(root)/'logs'; logs.mkdir()
            (logs/'app.log').write_text('\n'.join(json.dumps(r) for r in rows), encoding='utf-8')
            archive=Path(root)/'chain.zip'
            self.assertEqual(log_export.export_logs(archive, root=Path(root)).outcome, 'exported')
            with zipfile.ZipFile(archive) as z:
                content=z.read('app.log').decode('utf-8')
            saved=[json.loads(s) for s in content.splitlines()]
        self.assertEqual(saved, rows)
        self.assertTrue(all(valid_record(r) for r in saved))
        self.assertEqual([r['outcome'] for r in saved if r['kind']=='voice_query'], ['timeout','os_error','cancelled'])
        self.assertEqual(saved[-1]['counts']['hid_edges'], 2)
        self.assertEqual(saved[-1]['counts']['audio_packets'], 1)
        self.assertEqual(saved[-1]['counts']['metadata_rejected'], 1)
        self.assertNotIn('PRIVATE_AUDIO', content)

    @unittest.skipUnless(sys.platform == 'win32', 'Windows native permission regression')
    def test_restricted_child_denial_recovery_and_exit_survive_export(self):
        with tempfile.TemporaryDirectory(prefix='remote-mic-attach-test-') as root:
            env = dict(os.environ, TEMP=root, TMP=root, TMPDIR=root, PYTHONUTF8='1')
            fixture = Path(__file__).with_name('native_attach_fixture.py')
            process = subprocess.run([sys.executable, '-B', str(fixture), root], env=env,
                capture_output=True, timeout=35, creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(process.returncode, 0, process.stdout.decode(errors='replace') + process.stderr.decode(errors='replace'))
            with zipfile.ZipFile(Path(root)/'native-evidence.zip') as archive:
                content = archive.read('app.log').decode('utf-8')
            rows = [json.loads(line.split(' record=')[1]) for line in content.splitlines()]
            self.assertTrue(all(valid_record(r) for r in rows))
            denied = [r for r in rows if r.get('step') == 'attach_asset_access' and r['details'].get('read_execute') == 0]
            allowed = [r for r in rows if r.get('step') == 'attach_asset_access' and r['details'].get('read_execute') == 1]
            self.assertTrue(denied, content)
            self.assertTrue(allowed, content)
            after = [r['details'] for r in rows if r.get('step') == 'attach_process_after']
            self.assertEqual([r.get('alive') for r in after], [1,1,1,0])
            files = [r['details'] for r in rows if r.get('step') == 'attach_asset_file'
                     and r['details'].get('asset_location') == 'temporary']
            self.assertEqual([r['pe_valid'] for r in files], [1,0,1,1])
            self.assertTrue(all(r['hash_complete'] == 1 and r['file_unchanged'] == 1 for r in files))
            self.assertEqual(files[0]['agent_sha256'], files[2]['agent_sha256'])
            errors = [r for r in rows if r.get('step') == 'attach_exception']
            self.assertTrue(any(r['error_type'] == 'ProcessNotRespondingError' and
                r['details']['error_family'] == 'agent_load_or_process_exit' for r in errors), content)
            self.assertTrue(any(r.get('step') == 'attach_helper_scan' and
                r['details']['sample_count'] > 0 for r in rows))
            self.assertFalse(any(r.get('step') == 'evidence_overflow' for r in rows))
            self.assertEqual(rows[-1]['counts']['send_failed'], 0)
            self.assertEqual(rows[-1]['counts']['metadata_rejected'], 0)
            self.assertNotIn(root, content)


class BundleAuditTests(unittest.TestCase):
    def test_inventory_reads_rotations_and_does_not_misattribute_old_failures(self):
        file = Path(__file__).parents[1]/'scripts'/'audit_log_bundle.py'
        spec = importlib.util.spec_from_file_location('audit_log_bundle', file)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/'logs.zip'
            old = '2026-09-27 10:00:00,000 INFO x: startup: app identity: version=1.0.75\n'
            old += '2026-09-27 10:00:01,000 INFO x: Chromecast evidence run=aaaaaaaaaaaa seq=1 record={"kind":"stage","stage":"voice_unavailable"}\n'
            current = '2026-09-28 10:00:00,000 INFO x: startup: app identity: version=1.0.80\n'
            current += '2026-09-28 10:00:01,000 INFO x: Chromecast evidence run=bbbbbbbbbbbb seq=1 record={"kind":"hid_startup","step":"attach","state":"failed"}\n'
            with zipfile.ZipFile(path, 'w') as z:
                z.writestr('app.log', current); z.writestr('app.log.1', old)
                z.writestr('diagnostic-trace.jsonl', '{"event":"device_context"}\nBROKEN\n')
                z.writestr('diagnostic-report.json', '{"incidents":[{"dropped_after":7}]}')
                z.writestr('export-info.json', '{"app_version":"1.0.80","files":[]}')
            result = module.audit(path)
            self.assertEqual(result['runs']['aaaaaaaaaaaa']['version'], '1.0.75')
            self.assertEqual(result['runs']['bbbbbbbbbbbb']['version'], '1.0.80')
            self.assertIn('invalid_trace:diagnostic-trace.jsonl', result['limitations'])
            self.assertIn('incident_window_limited_read_application_log', result['limitations'])
            self.assertTrue(result['all_members_accounted'])
            self.assertEqual(len(result['inventory']), 5)
