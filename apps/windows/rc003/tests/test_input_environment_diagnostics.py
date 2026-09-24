import ctypes
from ctypes import wintypes
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

from ovb_rc003 import input_environment_diagnostics_windows as env
from ovb_rc003 import voice_interaction_diagnostics_windows as focus
from ovb_rc003 import voice_program_manager as programs
from ovb_rc003 import diagnostic_trace, shortcut_observation_windows as receiver


class TokenMetadataTests(unittest.TestCase):
    def apis(self, *, deny_token=False, missing_ui_access=False):
        kernel, advapi = mock.Mock(), mock.Mock()
        kernel.OpenProcess.return_value = 77
        def name(handle, flags, buffer, size):
            buffer.value = r'C:\PrivateFolder\Input.exe'
            size._obj.value = len(buffer.value)
            return 1
        kernel.QueryFullProcessImageNameW.side_effect = name
        def times(handle, created, *others):
            created._obj.dwLowDateTime = 123
            return 1
        kernel.GetProcessTimes.side_effect = times
        def token(handle, access, output):
            if deny_token:
                ctypes.set_last_error(5)
                return 0
            output._obj.value = 88
            return 1
        advapi.OpenProcessToken.side_effect = token
        def information(token, kind, buffer, size, returned):
            if kind == 25:
                returned._obj.value = 28
                if buffer is None:
                    return 0
                base = ctypes.addressof(buffer)
                ctypes.c_void_p.from_buffer(buffer).value = base + 16
                sid = bytes([1, 1, 0, 0, 0, 0, 0, 16]) + (0x3000).to_bytes(4, 'little')
                ctypes.memmove(base + 16, sid, len(sid))
                return 1
            if kind == 26 and missing_ui_access:
                ctypes.set_last_error(5)
                return 0
            returned._obj.value = ctypes.sizeof(wintypes.DWORD)
            buffer._obj.value = {20: 1, 26: 0, 12: 2}[kind]
            return 1
        advapi.GetTokenInformation.side_effect = information
        return kernel, advapi

    def test_permission_values_and_handle_lifetimes_without_paths(self):
        for deny, partial in ((False, False), (True, False), (False, True)):
            with self.subTest(deny=deny, partial=partial):
                kernel, advapi = self.apis(deny_token=deny, missing_ui_access=partial)
                with mock.patch.object(env.ctypes, 'WinDLL', side_effect=lambda name, **_: kernel if name == 'kernel32' else advapi):
                    result = env.process_security(99)
                self.assertEqual(result['status'], 'token_open_failed' if deny else 'partial' if partial else 'captured')
                kernel.OpenProcess.assert_called_once_with(0x1000, False, 99)
                self.assertEqual(advapi.OpenProcessToken.call_args.args[1], 8)
                self.assertEqual(kernel.CloseHandle.call_count, 1 if deny else 2)
                self.assertNotIn('PrivateFolder', json.dumps(result))
                if not deny:
                    self.assertEqual(result['integrity_rid'], 0x3000)
                    self.assertIs(result['elevated'], True)
                    self.assertIs(result['ui_access'], None if partial else False)
                    self.assertEqual(result['session_id'], 2)
                    self.assertEqual(result['created_filetime'], 123)

    def test_invalid_label_pointers_and_lengths_are_rejected(self):
        buffer = ctypes.create_string_buffer(32)
        for address, returned in ((0, 32), (ctypes.addressof(buffer) - 1, 32),
                                  (ctypes.addressof(buffer) + 30, 32),
                                  (ctypes.addressof(buffer) + 16, 9000)):
            ctypes.c_void_p.from_buffer(buffer).value = address
            self.assertIsNone(env._integrity_rid(buffer, returned))

    @unittest.skipUnless(sys.platform == 'win32', 'read-only native token query')
    def test_native_own_process(self):
        result = env.process_security(os.getpid())
        self.assertEqual(result['status'], 'captured', result)
        self.assertEqual(result['executable'].lower(), Path(sys.executable).name.lower())
        self.assertIsInstance(result['integrity_rid'], int)
        self.assertIsInstance(result['session_id'], int)

    def test_clipboard_metadata_without_opening_or_reading_content(self):
        sequence = mock.Mock(return_value=123)
        user = types.SimpleNamespace(GetClipboardSequenceNumber=sequence,
            GetClipboardOwner=mock.Mock(return_value=0),
            GetWindowThreadProcessId=mock.Mock(), IsClipboardFormatAvailable=mock.Mock(return_value=1))
        with mock.patch.object(env.ctypes, 'WinDLL', return_value=user):
            result = env.clipboard_metadata()
            self.assertEqual(result['sequence'], 123)
            self.assertEqual(result['status'], 'captured')
            self.assertIsNone(result['owner_pid'])
            self.assertTrue(result['unicode_text_available'])
            self.assertEqual(result['submission_confirmation'], 'unknown')
            sequence.return_value = 0
            self.assertEqual(env.clipboard_metadata(), dict(status='unavailable', sequence=None))


class EnvironmentSamplerTests(unittest.TestCase):
    def test_process_cache_foreground_change_and_clipboard_unknown(self):
        tick = [1.0]
        security = mock.Mock(side_effect=lambda pid: dict(pid=pid, status='captured', executable='Input.exe', integrity_rid=8192))
        processes = mock.Mock(return_value=[('wetype', 88, 'Input.exe')])
        clip = mock.Mock(side_effect=[{'sequence': 10}, {'sequence': 11}, {'sequence': None}, {'sequence': 11}, {'sequence': 11}])
        sampler = env.EnvironmentSampler(processes=processes, security=security, clipboard=clip, clock=lambda: tick[0])
        context = {'foreground_pid': 55, 'foreground_executable': 'Input.exe'}
        first = sampler.sample(context)
        self.assertEqual([r['role'] for r in first['process_security']], ['observer', 'foreground', 'wetype'])
        self.assertIsNone(first['clipboard']['changed_since_previous_sample'])
        second = sampler.sample(context)
        self.assertTrue(second['clipboard']['changed_since_previous_sample'])
        self.assertEqual(processes.call_count, 1)
        self.assertIsNone(sampler.sample(context)['clipboard']['changed_since_previous_sample'])
        self.assertIsNone(sampler.sample(context)['clipboard']['changed_since_previous_sample'])
        tick[0] = 7
        security.side_effect = lambda pid: dict(pid=pid, status='captured', executable='ReusedPid.exe', integrity_rid=12288)
        final = sampler.sample(context)
        self.assertEqual(processes.call_count, 2)
        self.assertEqual(final['process_security'][-1], dict(role='wetype', pid=88, status='identity_unverified', query_status='captured'))
        self.assertEqual(final['permission_failure_cause'], 'not_inferred')
        self.assertFalse(final['clipboard']['content_observed'])

    def test_inventory_is_bounded_and_failures_stay_unknown(self):
        sampler = env.EnvironmentSampler(processes=lambda: [('sogou', p, '') for p in range(100)],
                                         security=lambda pid: {'pid': pid, 'status': 'token_open_failed'},
                                         clipboard=lambda: {'sequence': 1})
        result = sampler.sample({})
        self.assertTrue(result['voice_inventory_truncated'])
        self.assertEqual(len(result['process_security']), 33)
        sampler = env.EnvironmentSampler(processes=mock.Mock(side_effect=OSError('private')),
                                         security=mock.Mock(side_effect=OSError('private')),
                                         clipboard=mock.Mock(side_effect=OSError('private')))
        result = sampler.sample({})
        self.assertEqual(result['voice_inventory_status'], 'query_failed')
        self.assertEqual(result['clipboard']['status'], 'query_failed')
        self.assertNotIn('private', json.dumps(result))

    def test_filtered_inventory_uses_existing_names_without_querying_unrelated_processes(self):
        kernel, advapi = mock.Mock(), mock.Mock()
        kernel.CreateToolhelp32Snapshot.return_value = 55
        entries = iter([(7, 'Chrome.exe'), (8, 'ImeService.exe'), (9, 'wetype_server.exe')])
        def next_process(handle, output):
            try:
                pid, name = next(entries)
            except StopIteration:
                ctypes.set_last_error(18)
                return 0
            output._obj.th32ProcessID = pid
            output._obj.szExeFile = name
            return 1
        kernel.Process32FirstW.side_effect = next_process
        kernel.Process32NextW.side_effect = next_process
        with mock.patch.object(programs.ctypes, 'WinDLL', side_effect=lambda name, **_: kernel if name == 'kernel32' else advapi):
            candidates = programs.diagnostic_voice_processes()
        self.assertEqual(candidates, (('doubao_ime', 8, 'ImeService.exe'), ('wetype', 9, 'wetype_server.exe')))
        kernel.OpenProcess.assert_not_called()
        kernel.CloseHandle.assert_called_once_with(55)


class ObservationIntegrationTests(unittest.TestCase):
    def test_late_submission_tail_survives_long_recording_and_other_mapping(self):
        tick = [0.0]
        timeline = focus.ContextTimeline(lambda: focus.FocusSnapshot(True), lambda: tick[0],
                                        environment=mock.Mock(sample=lambda _: {'clipboard': {'sequence': int(tick[0])}}))
        timeline.accept(dict(event='attempt_started', attempt_id='a', provider='sogou'))
        self.assertEqual(timeline.poll()['provider'], 'sogou')
        tick[0] = 31
        self.assertEqual(timeline.poll()['phase'], 'observation_ended')
        timeline.accept(dict(event='mapping_trigger', gesture_id='ordinary'))
        self.assertEqual(timeline.context['attempt_id'], 'a')
        tick[0] = 35
        timeline.accept(dict(event='attempt_finished', attempt_id='a'))
        self.assertEqual(timeline.poll()['observation_stage'], 'after_voice')
        tick[0] = 42
        self.assertEqual(timeline.poll()['clipboard']['sequence'], 42)
        tick[0] = 46
        self.assertEqual(timeline.poll()['phase'], 'observation_ended')
        timeline.accept(dict(event='mapping_trigger', gesture_id='new'))
        self.assertNotIn('attempt_id', timeline.context)

    def test_disabled_trace_never_samples_process_or_clipboard(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(env, 'process_security') as security, \
             mock.patch.object(env, 'clipboard_metadata') as clipboard:
            trace = diagnostic_trace.DiagnosticTrace(Path(tmp), enabled=False)
            trace.emit('attempt_started', attempt_id='a')
            trace.close()
            security.assert_not_called()
            clipboard.assert_not_called()

    def test_enabled_trace_persists_environment_with_attempt_on_writer_thread(self):
        observed = threading.Event()
        sampling_threads = []
        metadata = {'process_security': [{'role': 'foreground', 'pid': 55,
                     'elevated': False, 'integrity_rid': 8192, 'ui_access': False}],
                    'clipboard': {'sequence': 7, 'content_observed': False}}
        def sample(context):
            sampling_threads.append(threading.current_thread().name)
            return metadata
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(focus, 'capture_focus_snapshot', return_value=focus.FocusSnapshot(True)), \
             mock.patch.object(env, 'EnvironmentSampler', return_value=mock.Mock(sample=sample)):
            trace = diagnostic_trace.DiagnosticTrace(Path(tmp), enabled=True)
            original_emit = trace.emit
            def emit(event, **fields):
                result = original_emit(event, **fields)
                if event == 'input_context_observation' and result:
                    observed.set()
                return result
            trace.emit = emit
            try:
                attempt = trace.begin_attempt('gesture', provider='wetype')
                self.assertTrue(observed.wait(2))
            finally:
                trace.close()
            rows = [json.loads(line) for line in trace.path.read_text(encoding='utf-8').splitlines()]
        observation = next(row for row in rows if row['event'] == 'input_context_observation')
        self.assertEqual(observation['attempt_id'], attempt)
        self.assertEqual(observation['provider'], 'wetype')
        for key, value in metadata.items():
            self.assertEqual(observation[key], value)
        self.assertEqual(set(sampling_threads), {'remote-mic-diagnostic-trace'})

    def test_receiver_context_work_runs_on_writer_thread(self):
        sampled = threading.Event()
        thread_ids = []
        def sample(context):
            thread_ids.append(threading.get_ident())
            sampled.set()
            return {'clipboard': {'sequence': 7, 'content_observed': False}}
        stream = io.StringIO()
        with mock.patch.object(receiver, 'foreground_context', return_value={'foreground_pid': 55}), \
             mock.patch.object(receiver, 'EnvironmentSampler', return_value=mock.Mock(sample=sample)):
            writer = receiver._EvidenceWriter(stream)
            writer.emit({'event': '_sample_context'})
            self.assertTrue(sampled.wait(2))
            writer.close()
        rows = [json.loads(line) for line in stream.getvalue().splitlines()]
        self.assertEqual(rows[0]['event'], 'receiver_context')
        self.assertNotEqual(thread_ids[0], threading.get_ident())
        self.assertEqual(rows[0]['clipboard']['sequence'], 7)

    def test_sogou_diagnostic_attempt_uses_actual_provider(self):
        from ovb_rc003.app import RC003App
        app = RC003App.__new__(RC003App)
        app._voice_shortcut = types.SimpleNamespace()
        app._voice_attempt_id = None
        app._config = {'voice_program': {'provider': 'sogou'}}
        app._diagnostic_trace = mock.Mock()
        app._diagnostic_trace.current_context.return_value = {}
        app._voice_shortcut.controller = types.SimpleNamespace(trigger_mode=types.SimpleNamespace(value='hold'))
        app._voice_shortcut.hotkey = types.SimpleNamespace(modifiers=('lctrl', 'lshift'), key='f7')
        with mock.patch.object(diagnostic_trace, 'foreground_context', return_value={}):
            app._ensure_voice_diagnostic_attempt(
                provider_shortcut_mode='toggle',
                effective_hotkey_tokens=('lshift', 'f8'),
                effective_backend='toggle_shortcut',
            )
        self.assertEqual(app._diagnostic_trace.begin_attempt.call_args.kwargs['provider'], 'sogou')
        fields = app._diagnostic_trace.emit.call_args.kwargs
        self.assertEqual(fields['provider_shortcut_mode'], 'toggle')
        self.assertEqual(fields['hotkey_tokens'], ['lshift', 'f8'])
        self.assertEqual(fields['backend'], 'toggle_shortcut')
        self.assertEqual(fields['physicalizer_binding'], 'not_required')

    def test_voice_attempt_only_observes_tracker_before_dispatch_receipt(self):
        from ovb_rc003.app import RC003App
        app = RC003App.__new__(RC003App)
        app._voice_shortcut = types.SimpleNamespace()
        app._voice_attempt_id = None
        app._config = {'voice_program': {'provider': 'custom'}}
        app._diagnostic_trace = mock.Mock()
        app._diagnostic_trace.current_context.return_value = {}
        app._voice_shortcut.controller = types.SimpleNamespace(
            trigger_mode=types.SimpleNamespace(value='hold')
        )
        app._voice_shortcut.hotkey = types.SimpleNamespace(modifiers=(), key='ralt')
        app._voice_key_physicalizer_lifecycle_lock = threading.RLock()
        first = types.SimpleNamespace(
            tracker_generation=17,
            installation_epoch=31,
            accepts_new_down=True,
        )
        app._voice_key_physicalizer = first

        with mock.patch.object(diagnostic_trace, 'foreground_context', return_value={}):
            app._ensure_voice_diagnostic_attempt()
        app._voice_key_physicalizer = types.SimpleNamespace(
            tracker_generation=18,
            installation_epoch=32,
            accepts_new_down=True,
        )

        fields = app._diagnostic_trace.emit.call_args.kwargs
        self.assertEqual(fields['physicalizer_binding'], 'observed')
        self.assertEqual(fields['physicalizer_generation'], -1)
        self.assertEqual(fields['physicalizer_installation_epoch'], -1)
        self.assertEqual(fields['physicalizer_observed_generation'], 17)
        self.assertEqual(fields['physicalizer_observed_installation_epoch'], 31)
