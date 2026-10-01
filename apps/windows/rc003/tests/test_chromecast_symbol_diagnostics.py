"""Failure evidence across the driver lookup, using only synthetic files/APIs."""
from contextlib import contextmanager, nullcontext
import ctypes
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import zipfile

from ovb_rc003 import chromecast_hid_tap_windows as tap
from ovb_rc003 import log_export
from ovb_rc003.chromecast_channel import Channel, SessionIdentity
from ovb_rc003.chromecast_observation import Observation, valid_record
from tests import test_chromecast_hid_driver_layout as layout_tests
from tests.test_chromecast_hid_identity import resolve, NEW, ENTITY, OTHER


PDB_KEY = 'A' * 32 + '1'
PDB_DATA = b'Microsoft C/C++ MSF 7.00' + bytes(100_000)


class SymbolDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.module = self.root / 'driver.dll'
        self.module.write_bytes(b'synthetic driver')

    @contextmanager
    def observe(self):
        instance = tap.HidTap(ENTITY)
        token = tap._startup_sink.set(instance._startup_record)
        try:
            yield instance
        finally:
            tap._startup_sink.reset(token)

    def rows(self, instance):
        rows = instance.poll_startup()
        self.assertTrue(rows)
        self.assertTrue(all(valid_record(row) for row in rows))
        self.assertNotIn('PRIVATE', json.dumps(rows))
        sender = Channel(SessionIdentity.create(ENTITY), commands=False)
        for row in rows:
            self.assertLessEqual(len(sender.encode('evidence', record=row)), 2048)
        return rows

    def failed(self, instance, step, reason):
        rows = self.rows(instance)
        row = next(r for r in rows if r['step'] == step and r['state'] == 'failed')
        self.assertEqual(row['reason'], reason)
        return row

    def test_host_scan_distinguishes_unknown_model_other_device_and_ambiguous_host(self):
        cases = [([], 'no_selected_host', 0, 0),
                 ([(NEW, 'aabbccddeeff', 42, OTHER)], 'no_selected_host', 1, 0),
                 ([(NEW, 'bad', 42, ENTITY)], 'no_selected_host', 1, 0),
                 ([(NEW, 'aabbccddeeff', 0, ENTITY)], 'no_selected_host', 1, 1),
                 ([(NEW, 'aabbccddeeff', 42, ENTITY), (NEW, '112233445566', 43, ENTITY)], 'ambiguous_host', 2, 2)]
        for services, reason, hardware, selected in cases:
            with self.subTest(reason=reason, services=services), self.observe() as instance:
                with self.assertRaises(tap.TapError):
                    resolve(services)
                row = self.failed(instance, 'host_scan', reason)
                self.assertEqual(row['details']['hardware_matches'], hardware)
                self.assertEqual(row['details']['selected_matches'], selected)
                self.assertNotIn('aabbccddeeff', json.dumps(row))

    def test_pdb_identity_validation_reports_why_without_pdb_path(self):
        valid = b'RSDS' + bytes(16) + struct.pack('<I', 1) + b'Driver.pdb\0'
        for content, entries, reason in [(valid, 0, 'debug_entries'), (b'bad', 1, 'debug_signature'),
                                         (valid[:24] + b'C:\\PRIVATE.pdb\0', 1, 'pdb_name')]:
            self.module.write_bytes(content)
            entry = SimpleNamespace(struct=SimpleNamespace(Type=2, PointerToRawData=0, SizeOfData=len(content)))
            image = SimpleNamespace(DIRECTORY_ENTRY_DEBUG=[entry]*entries, close=mock.Mock())
            with self.subTest(reason=reason), self.observe() as instance, mock.patch('pefile.PE', return_value=image):
                with self.assertRaises(tap.TapError):
                    tap._pdb_identity(self.module)
                self.failed(instance, 'pdb_identity', reason)
                image.close.assert_called_once()

    @contextmanager
    def cache(self):
        with mock.patch.object(tap, '_pdb_identity', return_value=('Driver.pdb', PDB_KEY)), \
             mock.patch.object(tap, '_symbol_cache', return_value=self.root / 'cache'):
            yield self.root / 'cache' / 'Driver.pdb' / PDB_KEY / 'Driver.pdb'

    def download(self, content=PDB_DATA, code=0, status=b'200', error=None):
        def run(args, **kwargs):
            if error:
                raise error
            Path(args[args.index('--output')+1]).write_bytes(content)
            return SimpleNamespace(returncode=code, stdout=status, stderr=b'PRIVATE_PROXY_PATH')
        return mock.patch.object(tap.subprocess, 'run', side_effect=run)

    def test_download_and_cache_success_are_distinguishable_and_no_second_download(self):
        with self.cache() as target, self.download() as run, self.observe() as instance:
            self.assertEqual(tap._ensure_pdb(self.module), target)
            rows = self.rows(instance)
            done = {r['step']: r for r in rows if r['state'] == 'success'}
            self.assertEqual(done['pdb_download']['details'], {'return_code': 0, 'http_status': 200})
            self.assertEqual(done['cache_lookup']['details']['cache_present'], 0)
            self.assertEqual(target.read_bytes(), PDB_DATA)
            with self.observe() as cached:
                self.assertEqual(tap._ensure_pdb(self.module), target)
                hits = self.rows(cached)
                self.assertEqual(hits[-1]['details']['cache_valid'], 1)
                self.assertNotIn('pdb_download', [r['step'] for r in hits])
            self.assertEqual(run.call_count, 1)

    def test_download_exit_size_signature_and_timeout_are_separate(self):
        cases = [(b'', 22, b'404', None, 'pdb_download', 'download_exit'),
                 (b'', 6, b'000', None, 'pdb_download', 'download_exit'),
                 (b'bad', 0, b'200', None, 'download_validate', 'download_size'),
                 (b'bad' * 40_000, 0, b'200', None, 'download_validate', 'download_signature'),
                 (b'', 0, b'', subprocess.TimeoutExpired('PRIVATE', 18), 'pdb_download', 'none')]
        for content, code, status, error, step, reason in cases:
            with self.subTest(step=step, code=code, reason=reason), self.cache(), \
                 self.download(content, code, status, error), self.observe() as instance:
                with self.assertRaises(tap.TapError):
                    tap._ensure_pdb(self.module)
                row = self.failed(instance, step, reason)
                if error:
                    self.assertEqual(row['error_type'], 'TimeoutExpired')
                if step == 'pdb_download' and not error:
                    self.assertEqual(row['details']['return_code'], code)
                    self.assertEqual(row['details']['http_status'], int(status))
                self.assertEqual(list((self.root/'cache').rglob('*.tmp')), [])

    def test_cache_write_failure_keeps_original_error_and_download_evidence(self):
        with self.cache(), self.download(), self.observe() as instance, \
             mock.patch.object(tap.os, 'replace', side_effect=PermissionError(13, 'PRIVATE')):
            with self.assertRaises(tap.TapError):
                tap._ensure_pdb(self.module)
            row = self.failed(instance, 'cache_commit', 'none')
            self.assertEqual(row['error_type'], 'PermissionError')
            self.assertEqual(row['native_code'], 13)

    @contextmanager
    def symbols(self, failure=None, *, skip_pdb=True):
        dbg, kernel = mock.Mock(), mock.Mock()
        kernel.GetCurrentProcess.return_value = 1
        dbg.SymInitializeW.return_value = failure != 'symbol_initialize'
        dbg.SymLoadModuleExW.return_value = 0 if failure == 'symbol_load' else 0x100000
        dbg.SymCleanup.return_value = True
        def enumerate_symbols(_process, _base, _pattern, callback, _context):
            names = [(tap._SYMBOL, 0x1800), (tap._CONSTRUCTOR, 0x1100)]
            if failure == 'symbol_match':
                names.pop()
            for name, rva in names:
                name_buffer = ctypes.create_unicode_buffer(name)
                raw = ctypes.create_string_buffer(ctypes.sizeof(tap._SymbolInfo) + ctypes.sizeof(name_buffer))
                pointer = ctypes.cast(raw, ctypes.POINTER(tap._SymbolInfo))
                pointer.contents.NameLen = len(name)
                pointer.contents.Address = 0x100000 + rva
                ctypes.memmove(ctypes.addressof(raw) + tap._SymbolInfo.Name.offset, name_buffer, ctypes.sizeof(name_buffer))
                callback(pointer, 0, None)
            return failure != 'symbol_enumerate'
        dbg.SymEnumSymbolsW.side_effect = enumerate_symbols
        with (mock.patch.object(tap, '_ensure_pdb') if skip_pdb else nullcontext()), \
             mock.patch.object(tap, '_symbol_cache', return_value=self.root), \
             mock.patch.object(tap.ctypes, 'WinDLL', side_effect=[dbg, kernel]), \
             mock.patch.object(tap.ctypes, 'get_last_error', return_value=5):
            yield dbg

    def test_symbol_api_failures_and_missing_constructor_are_distinct(self):
        for step, reason in [('symbol_initialize','symbol_initialize_failed'), ('symbol_load','symbol_load_failed'),
                             ('symbol_enumerate','symbol_enumerate_failed'), ('symbol_match','symbol_missing_or_ambiguous')]:
            with self.subTest(step=step), self.symbols(step), self.observe() as instance:
                with self.assertRaises(tap.TapError):
                    tap._resolve_pdb_symbols(self.module)
                row = self.failed(instance, step, reason)
                if step != 'symbol_match':
                    self.assertEqual(row['native_code'], 5)
                else:
                    self.assertEqual(row['details'], {'callback_matches':1,'constructor_matches':0})

    def test_symbol_success_retains_only_public_relative_locations(self):
        with self.symbols() as dbg, self.observe() as instance:
            self.assertEqual(tap._resolve_pdb_symbols(self.module), (0x1800, 0x1100))
            row = next(r for r in self.rows(instance) if r['step']=='symbol_match' and r['state']=='success')
            self.assertEqual(row['details']['constructor_rva'], 0x1100)
            dbg.SymCleanup.assert_called_once()

    def test_nonfatal_symbol_cleanup_error_retains_code_without_changing_layout(self):
        with self.symbols() as dbg, self.observe() as instance:
            dbg.SymCleanup.return_value = False
            self.assertEqual(tap._resolve_pdb_symbols(self.module), (0x1800, 0x1100))
            row = self.rows(instance)[-1]
            self.assertEqual(row['step'], 'symbol_cleanup')
            self.assertEqual(row['details'], {'native_result': 0, 'native_code': 5})

    def test_layout_failures_keep_driver_fingerprint_and_specific_rejection(self):
        for kind in ('architecture', 'callback', 'constructor', 'instructions'):
            image = layout_tests.DriverLayoutTests().image()
            symbols = (0x1800, 0x1100)
            if kind == 'architecture':
                image.FILE_HEADER.Machine = 0xAA64
            elif kind == 'callback':
                symbols = (0x800, 0x1100)
            elif kind == 'constructor':
                symbols = (0x1800, 0x800)
            else:
                image.get_data.return_value = b'\x90'*64
            expected = {'architecture':('driver_arch','unsupported_architecture'),
                        'callback':('callback_range','nonexecutable_rva'),
                        'constructor':('constructor_range','nonexecutable_rva'),
                        'instructions':('constructor_layout','constructor_unrecognized')}
            with self.subTest(kind=kind), self.observe() as instance, \
                 mock.patch.object(tap, '_resolve_pdb_symbols', return_value=symbols), mock.patch('pefile.PE', return_value=image):
                with self.assertRaises(tap.TapError):
                    tap._driver_layout(self.module)
                rows = self.rows(instance)
                self.assertTrue(any(r['details'].get('driver_sha256') == hashlib.sha256(self.module.read_bytes()).hexdigest() for r in rows))
                self.assertTrue(any((r['step'],r['reason']) == expected[kind] for r in rows))
                image.close.assert_called_once()

    def test_full_unknown_driver_startup_evidence_survives_channel_and_export(self):
        image = layout_tests.DriverLayoutTests().image(layout_tests.NEW_PROLOGUE)
        session, script = mock.Mock(), mock.Mock()
        session.create_script.return_value = script
        def loaded():
            cb = script.on.call_args.args[1]
            cb({'type':'send','payload':{'kind':'hid_setup','step':'runtime_module','state':'success','reason':'none',
                'details':{'module_present':1,'module_path_match':1,'pointer_size':8,'loaded_size':65536}}}, None)
            cb({'type':'send','payload':{'kind':'hook_ready'}}, None)
        script.load.side_effect = loaded
        with self.cache(), self.download(), self.symbols(skip_pdb=False), mock.patch.object(tap, 'MODULE', self.module), \
             mock.patch('pefile.PE', return_value=image), \
             mock.patch('ovb_rc003.chromecast_gadget_windows.loaded_or_pending_for_host', return_value=False), \
             mock.patch.object(tap, 'selected_host_and_address', return_value=(42,'010203040506')), \
             mock.patch.dict('sys.modules', frida=SimpleNamespace(attach=lambda pid:session)):
            # The default module is bound at definition; choose the synthetic fixture explicitly.
            real_layout = tap._driver_layout
            with mock.patch.object(tap, '_driver_layout', side_effect=lambda:real_layout(self.module)):
                instance = tap.HidTap(ENTITY)
                instance.start()
                rows = self.rows(instance)
                self.assertLess(len(rows), tap._STARTUP_LIMIT)
                self.assertIn('runtime_module', [r['step'] for r in rows])
                self.assertIn('symbol_match', [r['step'] for r in rows])
                self.assertIn('pdb_download', [r['step'] for r in rows])
                instance.close()
        identity = SessionIdentity.create(ENTITY)
        sender, receiver = Channel(identity,commands=False), Channel(identity,commands=False)
        decoded=[]
        observation=Observation(lambda row:decoded.append(receiver.decode(sender.encode('evidence',record=row))['record']))
        for row in rows:
            observation.hid_startup(row)
        logs=self.root/'logs'; logs.mkdir()
        content='\n'.join(json.dumps(r) for r in decoded)
        (logs/'app.log').write_bytes(content.encode('utf-8'))
        destination=self.root/'diagnostics.zip'
        self.assertEqual(log_export.export_logs(destination,root=self.root).outcome,'exported')
        with zipfile.ZipFile(destination) as archive:
            self.assertEqual(archive.read('app.log').decode('utf-8'),content)
        self.assertNotIn(str(self.root),content)

    def test_diagnostics_callback_failure_does_not_change_result_or_context(self):
        token=tap._startup_sink.set(mock.Mock(side_effect=RuntimeError('diagnostic unavailable')))
        try:
            self.assertEqual(resolve([(NEW,'aabbccddeeff',42,ENTITY)]),(42,'aabbccddeeff'))
        finally:
            tap._startup_sink.reset(token)
        self.assertIsNone(tap._startup_sink.get())
