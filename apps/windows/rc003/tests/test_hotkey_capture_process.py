"""Real child/pipe lifetimes with a fake keyboard, never global input hooks."""
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from unittest import mock

from ovb_rc003 import hotkey_capture_process as worker
from ovb_rc003.hotkey_capture_windows import HotkeyCapture, HotkeyCaptureUnavailableError


_PRELUDE = "import sys,json,time\ndef send(**x): print(json.dumps(x),flush=True)\n"


@unittest.skipUnless(sys.platform == "win32", "Windows recorder child")
class ProcessCaptureTests(unittest.TestCase):
    def capture(self, body, **kwargs):
        patch = mock.patch.object(worker, "_command", return_value=[getattr(sys, "_base_executable", sys.executable), "-u", "-c", _PRELUDE + body])
        patch.start()
        self.addCleanup(patch.stop)
        value = worker.ProcessCapture(**kwargs)
        self.addCleanup(value.stop)
        return value

    def test_complete_chord_and_diagnostics_cross_pipe_once(self):
        result = []
        capture = self.capture(
            "send(kind='ready')\nsend(kind='result',chord='rctrl+rshift+0')\n"
            "send(kind='diagnostics',state={'unhook_succeeded':True})\n",
            on_captured=result.append,
        )
        capture.start()
        capture._thread.join(3)
        self.assertEqual(result, ["rctrl+rshift+0"])
        self.assertEqual(capture.diagnostics, {"unhook_succeeded": True, "child_pid": capture._process.pid})
        self.assertFalse(capture.is_running)
        self.assertTrue(capture._process.stdout.closed)
        self.assertTrue(capture._process.stdin.closed)

    def test_cancel_discards_a_late_result_and_allows_restart(self):
        result = []
        capture = self.capture(
            "send(kind='ready')\nsys.stdin.buffer.read(1)\n"
            "send(kind='result',chord='lctrl+lshift+0')\n",
            on_captured=result.append,
        )
        pids = []
        for _ in range(2):
            capture.start()
            pids.append(capture._process.pid)
            capture.stop()
            self.assertFalse(capture.is_running)
        self.assertEqual(result, [])
        self.assertNotEqual(*pids)

    def test_unexpected_exit_after_ready_notifies_owner(self):
        errors = []
        capture = self.capture("send(kind='ready')\ntime.sleep(.1)\n", on_captured=lambda _: None, on_error=errors.append)
        capture.start()
        capture._thread.join(3)
        self.assertEqual(errors, ["快捷键录入已中断，请重新点击录入。"])
        self.assertFalse(capture.is_running)

    def test_exit_before_ready_is_a_start_failure(self):
        capture = self.capture("sys.exit(1)\n", on_captured=lambda _: None)
        with self.assertRaises(HotkeyCaptureUnavailableError):
            capture.start()
        self.assertFalse(capture.is_running)

    def test_start_timeout_reaps_an_unresponsive_child(self):
        capture = self.capture("time.sleep(60)\n", on_captured=lambda _: None)
        before = time.monotonic()
        with self.assertRaisesRegex(HotkeyCaptureUnavailableError, "timed out"):
            capture.start(start_timeout=.1)
        self.assertLess(time.monotonic() - before, 5)
        self.assertFalse(capture.is_running)
        self.assertIsNotNone(capture._process.poll())

    def test_oversized_reply_is_rejected_and_reaped(self):
        capture = self.capture("print('x'*9000,flush=True)\n", on_captured=lambda _: None)
        with self.assertRaises(HotkeyCaptureUnavailableError):
            capture.start()
        self.assertFalse(capture.is_running)

    def test_duplicate_start_cannot_orphan_the_original_child(self):
        capture = self.capture("send(kind='ready')\nsys.stdin.buffer.read(1)\n", on_captured=lambda _: None)
        capture.start()
        process = capture._process
        with self.assertRaisesRegex(HotkeyCaptureUnavailableError, "already running"):
            capture.start()
        self.assertIs(capture._process, process)
        capture.stop()
        self.assertIsNotNone(process.poll())

    def test_launch_failure_is_reported_without_a_live_resource(self):
        capture = worker.ProcessCapture(lambda _: None)
        with mock.patch.object(worker.subprocess, "Popen", side_effect=OSError("test launch failure")):
            with self.assertRaises(HotkeyCaptureUnavailableError):
                capture.start()
        self.assertFalse(capture.is_running)

    def test_injected_opt_in_and_error_receiver_are_forwarded(self):
        callback, error = mock.Mock(), mock.Mock()
        child = mock.Mock()
        child.is_running = True
        with mock.patch.object(worker, "ProcessCapture", return_value=child) as factory:
            capture = HotkeyCapture(callback, accept_injected=True, isolated=True, on_error=error)
            capture.start(start_timeout=2)
            self.assertTrue(capture.is_running)
            capture.stop()
        factory.assert_called_once_with(callback, accept_injected=True, on_error=error)
        child.start.assert_called_once_with(start_timeout=2)
        child.stop.assert_called_once_with()

    def test_frozen_child_uses_the_same_executable_without_python_flags(self):
        with mock.patch.object(worker.sys, "frozen", True, create=True):
            self.assertEqual(worker._command(True), [sys.executable, worker.FLAG, "--accept-injected"])

    def test_source_worker_owns_the_real_interpreter_not_a_venv_redirector(self):
        with mock.patch.object(worker.sys, "_base_executable", "base-python.exe", create=True), \
                mock.patch.object(worker.sys, "executable", "venv-python.exe"), \
                mock.patch.object(worker.sys, "frozen", False, create=True):
            command = worker._command(False)
        self.assertEqual(command[:2], ["base-python.exe", "-c"])
        self.assertIn("child_main(sys.argv[1:])", command[2])

    def test_child_diagnostics_and_failures_survive_the_final_exported_zip(self):
        from ovb_rc003 import logging_setup, log_export
        logger = logging.getLogger("ovb_rc003")
        previous_level = logger.level
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            handler = logging_setup.EvidenceFileHandler(
                root / "logs" / "app.log", asynchronous=True, encoding="utf-8",
                maxBytes=logging_setup.LOG_MAX_BYTES, backupCount=1)
            handler.addFilter(logging_setup.PrivacySafeExceptionFilter())
            logger.addHandler(handler)
            logger.setLevel(logging.INFO)
            try:
                cases = (
                    ("send(kind='ready')\nsend(kind='result',chord='rctrl+rshift+0')\n"
                     "send(kind='diagnostics',state={'result_deliveries':1,'unhook_succeeded':True})\n", False, None),
                    ("send(kind='ready')\nsys.stdin.buffer.read(1)\n"
                     "send(kind='diagnostics',state={'result_deliveries':0,'unhook_succeeded':True})\n", True, None),
                    ("send(kind='ready')\ntime.sleep(.1)\n", False, None),
                    ("sys.exit(1)\n", False, 3),
                    ("time.sleep(60)\n", False, .1),
                )
                for body, cancel, failed_timeout in cases:
                    command = [getattr(sys, "_base_executable", sys.executable), "-u", "-c", _PRELUDE + body]
                    capture = worker.ProcessCapture(lambda _: None)
                    with mock.patch.object(worker, "_command", return_value=command):
                        if failed_timeout is not None:
                            with self.assertRaises(HotkeyCaptureUnavailableError):
                                capture.start(start_timeout=failed_timeout)
                        else:
                            capture.start()
                            if cancel:
                                capture.stop()
                            capture._thread.join(3)
                        capture.stop()
                        self.assertFalse(capture.is_running)
                result = log_export.export_logs(root / "evidence.zip", root=root)
                self.assertEqual(result.outcome, "exported")
                self.assertFalse(result.incomplete)
                with zipfile.ZipFile(result.path) as archive:
                    self.assertIsNone(archive.testzip())
                    log = archive.read("app.log").decode("utf-8")
                    states = [json.loads(line.split(" state=", 1)[1]) for line in log.splitlines()
                              if "diagnostics reason=child_exit" in line]
                    self.assertEqual([s["result_deliveries"] for s in states], [1, 0])
                    self.assertTrue(all(s["unhook_succeeded"] and s["child_pid"] > 0 for s in states))
                    for reason in ("exit_during_recording", "exit_before_ready", "start_timeout"):
                        self.assertIn("reason=" + reason, log)
                    self.assertNotIn("rctrl+rshift+0", log)
                    self.assertIn("hotkey_capture_diagnostics", archive.read("diagnostic-report.json").decode("utf-8"))
                    self.assertTrue(json.loads(archive.read("export-info.json"))["application_log_flushed"])
            finally:
                logger.removeHandler(handler)
                logger.setLevel(previous_level)
                handler.close()


_FAKE_CHILD = r'''
import os,sys,time
from ovb_rc003 import hotkey_capture_process as w
class FakeCapture:
    def __init__(self, callback, *, accept_injected=False):
        self.callback=callback; self.is_running=False
        self._diagnostics={'accept_injected':accept_injected,'unhook_succeeded':None}
    def start(self):
        self.is_running=True
        if os.environ.get('CHORD'):
            self.callback('lctrl+lshift+0')
    def stop(self):
        self.is_running=False;self._diagnostics['unhook_succeeded']=True
w.HotkeyCapture=FakeCapture
if os.environ.get('NO_CONSOLE'):
    sys.stdin=None;sys.stdout=None
raise SystemExit(w.child_main(['--accept-injected']))
'''


@unittest.skipUnless(sys.platform == "win32", "Windows inherited pipe handles")
class ChildPipeTests(unittest.TestCase):
    def launch(self, **values):
        env = os.environ.copy()
        env.update(values)
        env["PYTHONPATH"] = str(Path(worker.__file__).resolve().parent.parent)
        process = subprocess.Popen([getattr(sys, "_base_executable", sys.executable), "-B", "-W", "error::ResourceWarning", "-c", _FAKE_CHILD],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   env=env, creationflags=subprocess.CREATE_NO_WINDOW)
        def cleanup():
            if process.poll() is None:
                process.kill()
            process.wait(3)
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()
        self.addCleanup(cleanup)
        return process

    def test_pipe_eof_cancels_and_unhooks_without_result(self):
        process = self.launch()
        self.assertEqual(json.loads(process.stdout.readline()), {"kind": "ready"})
        process.stdin.close()
        process.wait(3)
        packets = [json.loads(line) for line in process.stdout]
        self.assertEqual([p["kind"] for p in packets], ["diagnostics"])
        self.assertTrue(packets[0]["state"]["unhook_succeeded"])
        self.assertEqual(process.returncode, 0)
        self.assertEqual(process.stderr.read(), b"")


    def test_windowed_runtime_reopens_inherited_handles_and_delivers_result(self):
        process = self.launch(NO_CONSOLE="1", CHORD="1")
        process.wait(3)
        packets = [json.loads(line) for line in process.stdout]
        self.assertEqual([p["kind"] for p in packets], ["ready", "result", "diagnostics"])
        self.assertEqual(packets[1]["chord"], "lctrl+lshift+0")
        self.assertTrue(packets[2]["state"]["unhook_succeeded"])
        self.assertEqual(process.returncode, 0)
        self.assertEqual(process.stderr.read(), b"")


class EntrypointTests(unittest.TestCase):
    def test_private_mode_routes_before_any_desktop_ownership(self):
        from ovb_rc003 import __main__ as entry, single_instance
        with mock.patch.object(sys, "argv", ["app", worker.FLAG, "--accept-injected"]), \
                mock.patch.object(worker, "child_main", return_value=0) as child, \
                mock.patch.object(single_instance, "ApplicationInstanceGuard") as guard:
            with self.assertRaises(SystemExit) as caught:
                entry.main()
        self.assertEqual(caught.exception.code, 0)
        child.assert_called_once_with(["--accept-injected"])
        guard.assert_not_called()

    def test_misplaced_worker_flag_fails_without_starting_worker(self):
        from ovb_rc003 import __main__ as entry
        with mock.patch.object(sys, "argv", ["app", "--settings", worker.FLAG]), \
                mock.patch.object(worker, "child_main") as child:
            with self.assertRaises(SystemExit) as caught:
                entry.main()
        self.assertEqual(caught.exception.code, 2)
        child.assert_not_called()
