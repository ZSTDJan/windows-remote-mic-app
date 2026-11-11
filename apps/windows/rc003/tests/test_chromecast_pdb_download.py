"""Bounded retries and actual curl cancellation against a local HTTP fixture."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

from ovb_rc003 import chromecast_hid_tap_windows as tap
from ovb_rc003.chromecast_observation import valid_record


DATA = b'Microsoft C/C++ MSF 7.00' + bytes(100_000)


class PdbDownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.module = self.root / 'driver.dll'
        self.module.write_bytes(b'unlisted driver')
        self.cancel = threading.Event()
        token = tap._startup_cancel.set(self.cancel)
        self.addCleanup(tap._startup_cancel.reset, token)
        for patcher in (
            mock.patch.object(tap, '_pdb_identity', return_value=('Driver.pdb', 'A' * 32 + '1')),
            mock.patch.object(tap, '_symbol_cache', return_value=self.root / 'cache'),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.target = self.root / 'cache' / 'Driver.pdb' / ('A' * 32 + '1') / 'Driver.pdb'

    def assert_clean(self):
        self.assertFalse(self.target.exists())
        self.assertEqual(list(self.root.rglob('*.tmp')), [])

    def test_transient_failures_retry_but_not_beyond_five(self):
        for success in (True, False):
            calls = []
            def download(args, deadline):
                self.assertFalse(Path(args[args.index('--output') + 1]).exists())
                calls.append(deadline)
                Path(args[args.index('--output') + 1]).write_bytes(DATA)
                code = 0 if success and len(calls) == 5 else 28
                return subprocess.CompletedProcess(args, code, b'200', b'')
            observer = tap.HidTap('a' * 64)
            token = tap._startup_sink.set(observer._startup_record)
            try:
                with self.subTest(success=success), mock.patch.object(tap, '_download_pdb', side_effect=download):
                    if success:
                        self.assertEqual(tap._ensure_pdb(self.module).read_bytes(), DATA)
                        self.target.unlink()
                    else:
                        with self.assertRaises(tap.TapError):
                            tap._ensure_pdb(self.module)
                    self.assertEqual(len(calls), 5)
                    self.assertEqual(len(set(calls)), 1)
                    rows = observer.poll_startup()
                    self.assertTrue(all(valid_record(row) for row in rows))
                    done = [r for r in rows if r['step'] == 'pdb_download' and r['state'] != 'begin']
                    self.assertEqual([r['details']['download_attempt'] for r in done], list(range(1, 6)))
                    self.assertEqual([r['state'] for r in done], ['failed'] * 4 + ['success' if success else 'failed'])
                    self.assert_clean()
            finally:
                tap._startup_sink.reset(token)

    def test_permanent_http_tls_and_oversize_failures_do_not_retry(self):
        for code, status in ((22, b'404'), (22, b'403'), (60, b'000'), (63, b'200')):
            with self.subTest(code=code, status=status), mock.patch.object(tap, '_download_pdb',
                    return_value=subprocess.CompletedProcess([], code, status)) as download:
                with self.assertRaises(tap.TapError):
                    tap._ensure_pdb(self.module)
                self.assertEqual(download.call_count, 1)
                self.assert_clean()

    def test_cancel_before_spawn_and_during_retry_wait(self):
        self.cancel.set()
        with mock.patch.object(tap, '_download_pdb') as download:
            with self.assertRaises(tap.TapError) as error:
                tap._ensure_pdb(self.module)
            self.assertEqual(error.exception.detail, 'startup_cancelled')
            download.assert_not_called()
        self.cancel.clear()
        def failure(args, deadline):
            self.cancel.set()
            return subprocess.CompletedProcess(args, 6, b'000')
        with mock.patch.object(tap, '_download_pdb', side_effect=failure) as download:
            with self.assertRaises(tap.TapError) as error:
                tap._ensure_pdb(self.module)
            self.assertEqual(error.exception.detail, 'startup_cancelled')
            self.assertEqual(download.call_count, 1)
            self.assert_clean()

    def test_abort_context_is_scoped_to_this_start_and_prevents_layout_work(self):
        outer = tap._startup_cancel.get()
        instance = tap.HidTap('a' * 64)
        instance.abort_start()
        with mock.patch.object(instance, '_start') as start:
            with self.assertRaises(tap.TapError):
                instance.start()
            start.assert_not_called()
        self.assertIs(tap._startup_cancel.get(), outer)

    @contextmanager
    def server(self, mode):
        owner = self
        self.requests = 0
        self.requested = threading.Event()
        released = threading.Event()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                owner.requests += 1
                if mode == 'retry' and owner.requests < 3:
                    self.send_response(503)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header('Content-Length', str(len(DATA)))
                self.end_headers()
                try:
                    self.wfile.write(DATA[:32] if mode == 'stall' else DATA)
                    self.wfile.flush()
                    owner.requested.set()
                    if mode == 'stall':
                        released.wait(5)
                except (ConnectionError, OSError):
                    pass
        httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=httpd.serve_forever, kwargs={'poll_interval': .02})
        thread.start()
        original = tap._download_pdb
        def local(args, deadline):
            # Exercise production curl arguments and cleanup; only the URL differs.
            return original(args[:-1] + ['--noproxy', '*', f'http://127.0.0.1:{httpd.server_port}/symbols'], deadline)
        try:
            with mock.patch.object(tap, '_download_pdb', side_effect=local):
                yield
        finally:
            released.set()
            httpd.shutdown()
            thread.join(2)
            httpd.server_close()

    def test_actual_curl_retries_http_and_caches_valid_result(self):
        with self.server('retry'):
            self.assertEqual(tap._ensure_pdb(self.module).read_bytes(), DATA)
            self.assertEqual(self.requests, 3)
            self.assertEqual(list(self.root.rglob('*.tmp')), [])

    def test_actual_curl_is_reaped_on_cancel_and_total_timeout(self):
        for cancel in (True, False):
            self.cancel.clear()
            processes = []
            popen = subprocess.Popen
            def track(*args, **kwargs):
                process = popen(*args, **kwargs)
                processes.append(process)
                return process
            def stop():
                if self.requested.wait(3) and cancel:
                    self.cancel.set()
            with self.subTest(cancel=cancel), self.server('stall'), \
                    mock.patch.object(tap.subprocess, 'Popen', side_effect=track), \
                    mock.patch.object(tap, '_PDB_DOWNLOAD_SECONDS', 1):
                stopper = threading.Thread(target=stop)
                stopper.start()
                started = time.monotonic()
                with self.assertRaises(tap.TapError) as error:
                    tap._ensure_pdb(self.module)
                elapsed = time.monotonic() - started
                stopper.join(3)
                self.assertLess(elapsed, 3)
                if cancel:
                    self.assertEqual(error.exception.detail, 'startup_cancelled')
                else:
                    self.assertIsInstance(error.exception.__cause__, subprocess.TimeoutExpired)
                self.assertEqual(len(processes), 1)
                self.assertIsNotNone(processes[0].poll())
                self.assertTrue(processes[0].stdout.closed)
                self.assertTrue(processes[0].stderr.closed)
                self.assert_clean()


if __name__ == '__main__':
    unittest.main()
