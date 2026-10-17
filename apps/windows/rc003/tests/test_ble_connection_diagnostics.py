"""Connection evidence must distinguish failure branches without changing GATT."""
import asyncio
import io
import json
import logging
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock
import zipfile

from ovb_rc003 import ble_connection_diagnostics as evidence
from ovb_rc003 import ble_candidate_probe_process as probe
from ovb_rc003 import ble_transport_winrt as transport
from ovb_rc003 import identity, log_export, logging_setup
from tests.fakes.fake_winrt import FakeWinRTEnvironment, FakeGattWriteResult


class Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.rows = []

    def emit(self, record):
        if record.msg == evidence.MESSAGE:
            self.rows.append(json.loads(record.args[0]))


class ConnectionEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.capture = Capture()
        self.logger = evidence.LOGGER
        self.level = self.logger.level
        self.logger.setLevel(logging.INFO)
        self.logger.addHandler(self.capture)
        self.addCleanup(self.logger.setLevel, self.level)
        self.addCleanup(self.logger.removeHandler, self.capture)

    def session(self, env, **kwargs):
        return transport.RC003BleSession(on_pcm_frame=lambda samples: None,
                    winrt=env.build_winrt_modules(), **kwargs)

    def candidate(self, env):
        return identity.RC003Candidate(env.name, False, env.discovered_info, "a" * 64)

    def row(self, stage, state):
        return next(row for row in self.capture.rows if row["stage"] == stage and row["state"] == state)

    def test_native_result_codes_and_zero_protocol_error_survive(self):
        for status, name in ((1, "unreachable"), (2, "protocol_error"), (3, "access_denied")):
            with self.subTest(status=status):
                self.capture.rows.clear()
                env = FakeWinRTEnvironment()
                async def services(*args):
                    return SimpleNamespace(status=status, protocol_error=0, services=[])
                env.device.get_gatt_services_for_uuid_with_cache_mode_async = services
                async def run():
                    session = self.session(env)
                    try:
                        with self.assertRaises(ConnectionError):
                            await session.connect(self.candidate(env))
                    finally:
                        await session.close()
                asyncio.run(run())
                row = self.row("services", "failed")
                self.assertEqual((row["status"], row["status_name"], row["protocol_error"], row["item_count"]),
                                 (status, name, 0, 0))
                self.assertEqual(row["reason"], "service_query_failed")
                self.assertTrue(env.device.closed)

    def test_empty_successful_service_query_is_different_from_native_failure(self):
        env = FakeWinRTEnvironment()
        async def services(*args):
            return SimpleNamespace(status=0, protocol_error=None, services=[])
        env.device.get_gatt_services_for_uuid_with_cache_mode_async = services
        async def run():
            session = self.session(env)
            try:
                with self.assertRaises(ConnectionError):
                    await session.connect(self.candidate(env))
            finally:
                await session.close()
        asyncio.run(run())
        self.assertEqual(self.row("services", "failed")["reason"], "service_not_found")

    def test_characteristic_query_missing_uuid_notify_and_write_are_distinct(self):
        cases = ("query", "missing", "audio", "control", "write")
        for case in cases:
            with self.subTest(case=case):
                self.capture.rows.clear()
                env = FakeWinRTEnvironment()
                if case == "query":
                    async def failed(*args):
                        return SimpleNamespace(status=3, protocol_error=5, characteristics=[])
                    env.service.get_characteristics_with_cache_mode_async = failed
                    stage, reason = "characteristics_tx", "characteristic_query_failed"
                elif case == "missing":
                    env.service._characteristics.pop(env.tx_characteristic.uuid)
                    stage, reason = "characteristics_tx", "characteristic_not_found"
                elif case in {"audio", "control"}:
                    async def failed(value):
                        return 3 if value == 1 else 0
                    target = env.audio_characteristic if case == "audio" else env.control_characteristic
                    target.write_client_characteristic_configuration_descriptor_async = failed
                    stage, reason = "subscribe_" + case, "subscription_failed"
                else:
                    async def failed(*args):
                        return SimpleNamespace(status=2, protocol_error=3)
                    env.tx_characteristic.write_value_with_result_async = failed
                    stage, reason = "get_capabilities", "tx_write_failed"
                async def run():
                    session = self.session(env)
                    try:
                        with self.assertRaises(ConnectionError):
                            await session.connect(self.candidate(env))
                    finally:
                        await session.close()
                asyncio.run(run())
                self.assertEqual(self.row(stage, "failed")["reason"], reason)
                self.assertTrue(env.service.closed)
                self.assertTrue(env.device.closed)
                self.assertEqual(self.row("close", "success")["attempt"], self.row(stage, "failed")["attempt"])

    def test_timeout_and_cancellation_keep_the_original_exception_and_cleanup(self):
        for kind in ("timeout", "cancelled"):
            with self.subTest(kind=kind):
                self.capture.rows.clear()
                env = FakeWinRTEnvironment()
                original = TimeoutError("private detail") if kind == "timeout" else asyncio.CancelledError()
                async def failed(*args):
                    raise original
                env.device.get_gatt_services_for_uuid_with_cache_mode_async = failed
                async def run():
                    session = self.session(env)
                    try:
                        try:
                            await session.connect(self.candidate(env))
                        except BaseException as actual:
                            self.assertIs(actual, original)
                    finally:
                        await session.close()
                asyncio.run(run())
                self.row("services", kind)
                self.row("close", "success")

    def test_logger_failure_and_failed_metadata_reads_leave_normal_connection_unchanged(self):
        env = FakeWinRTEnvironment()
        class DeniedMetadata:
            status = 0
            services = [env.service]
            @property
            def protocol_error(self):
                raise PermissionError("private detail")
        async def services(*args):
            return DeniedMetadata()
        env.device.get_gatt_services_for_uuid_with_cache_mode_async = services
        async def run():
            session = self.session(env)
            try:
                await session.connect(self.candidate(env))
            finally:
                await session.close()
        asyncio.run(run())
        self.assertTrue(self.row("services", "success")["evidence_partial"])
        self.assertEqual(len(env.tx_characteristic.write_history), 1)
        self.capture.rows.clear()
        env.device.closed = False
        env.service.closed = False
        with mock.patch.object(evidence.LOGGER, "info", side_effect=RuntimeError("log failed")):
            asyncio.run(run())
        self.assertTrue(env.device.closed)

    def test_disconnect_read_error_and_real_disconnect_are_different(self):
        env = FakeWinRTEnvironment()
        disconnected = []
        async def run():
            session = self.session(env, on_disconnected=lambda: disconnected.append(True))
            class Unreadable:
                @property
                def connection_status(self):
                    raise OSError(5, "private detail")
            session._handle_connection_status_changed(SimpleNamespace(connection_status=0), None)
            session._handle_connection_status_changed(Unreadable(), None)
            class NotDisconnected:
                def __eq__(self, value):
                    return False
                def __int__(self):
                    raise RuntimeError("metadata unavailable")
            session._handle_connection_status_changed(SimpleNamespace(connection_status=NotDisconnected()), None)
            await session.close()
        asyncio.run(run())
        self.assertEqual(len(disconnected), 2)
        self.assertEqual(self.row("disconnect_status", "exception")["reason"], "native_status_read_failed")
        self.assertEqual(self.row("disconnect_status", "success")["reason"], "reported_disconnected")

    def test_exception_codes_are_real_and_no_exception_text_is_formatted(self):
        class PrivateError(OSError):
            def __str__(self):
                raise AssertionError("exception text must never be formatted")
        error = PrivateError(5, "private contents")
        error.winerror = -2147024891
        error.hresult = -2147024891
        diagnostic = evidence.ConnectionDiagnostics()
        diagnostic.emit("open_device", "exception", error=error)
        row = self.row("open_device", "exception")
        self.assertEqual(row["errors"][0]["hresult"], -2147024891)
        self.assertEqual(row["errors"][0]["winerror"], -2147024891)
        self.assertNotIn("private", json.dumps(row))

    def test_new_connection_has_new_identity_and_fixed_short_selection_only(self):
        async def run():
            for _ in range(2):
                env = FakeWinRTEnvironment()
                session = self.session(env)
                await session.connect(self.candidate(env))
                await session.close()
        asyncio.run(run())
        begins = [r for r in self.capture.rows if r["stage"] == "connect" and r["state"] == "begin"]
        self.assertEqual(len({r["attempt"] for r in begins}), 2)
        self.assertEqual({r["selected_ref"] for r in begins}, {"a" * 12})
        self.assertTrue(all(evidence.valid_record(row) for row in self.capture.rows))

    def test_evidence_survives_actual_async_writer_fault_report_and_zip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            logs = root / "logs"
            logs.mkdir()
            handler = logging_setup.EvidenceFileHandler(logs / "app.log", asynchronous=True, encoding="utf-8")
            handler.addFilter(logging_setup.PrivacySafeExceptionFilter())
            self.logger.addHandler(handler)
            try:
                diagnostic = evidence.ConnectionDiagnostics()
                with self.assertRaises(OSError):
                    with diagnostic.step("open_device"):
                        raise OSError(5, "private native detail")
                destination = root / "connection.zip"
                result = log_export.export_logs(destination, root=root)
                self.assertEqual(result.outcome, "exported")
                self.assertFalse(result.incomplete)
                with zipfile.ZipFile(destination) as archive:
                    app = archive.read("app.log").decode("utf-8")
                    report = archive.read("diagnostic-report.json").decode("utf-8")
                    self.assertIn('"stage":"open_device"', app)
                    self.assertIn("open_device", report)
                    self.assertNotIn("private native detail", app + report)
            finally:
                self.logger.removeHandler(handler)
                handler.close()


class ProbeEvidenceTests(unittest.TestCase):
    def test_structured_probe_result_replays_with_native_codes_and_rejects_extra_fields(self):
        row = dict(event="ble_connection_step", attempt="a" * 32, scope="probe", stage="services",
                   state="failed", pid=1, elapsed_ms=1, status=3, protocol_error=5, item_count=0)
        with mock.patch.object(evidence, "replay") as replay:
            process = SimpleNamespace(stdout=io.BytesIO(json.dumps([["evidence", row]]).encode()))
            self.assertTrue(probe._read_diagnostics(process))
            replay.assert_called_once_with(row)
            row["private_value"] = "must not pass"
            process = SimpleNamespace(stdout=io.BytesIO(json.dumps([["evidence", row]]).encode()))
            self.assertFalse(probe._read_diagnostics(process))
