"""Bounded startup evidence using synthetic packets, never real Bluetooth."""
import asyncio
import ctypes as C
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import zipfile
from collections import deque
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ovb_rc003 import chromecast_etw_windows as etw, chromecast_worker as worker
from ovb_rc003.chromecast_channel import Channel, SessionIdentity
from ovb_rc003.chromecast_buttons import ButtonReceiver
from ovb_rc003.chromecast_observation import Observation, valid_record
from tests.test_chromecast_buttons import (
    ENTITY, GENERATION, RADIO, CAPTURE_DETAILS, acl, incomplete_notification,
)


class SourceEvidenceTests(unittest.TestCase):
    def make(self):
        rows = []
        obs = Observation(rows.append, clock=lambda: 10)
        receiver = ButtonReceiver(ENTITY, GENERATION, RADIO, 10, observation=obs)
        return receiver, rows

    def test_timeout_preserves_completed_api_but_missing_wire_proof(self):
        receiver, rows = self.make()
        receiver.api_result(ENTITY, GENERATION, success=True, services=0, input_attribute=0x29, now=10)
        receiver.advance(26)
        row = rows[-1]
        self.assertEqual(row['reason'], 'source_timeout')
        self.assertTrue(row['api_ok'])
        self.assertFalse(row['request_seen'])
        self.assertFalse(row['response_seen'])
        self.assertEqual(row['input_attribute'], 0x29)
        self.assertFalse(receiver.api_ok)  # Snapshot survived the existing clear.
        self.assertTrue(valid_record(row))
        receiver.stop(27)
        self.assertEqual(len(rows), 1)

    def test_short_packet_attribution_stays_unknown_until_proof(self):
        for request_seen, packet_handle, expected in ((False, 0x31, 'unknown'),
                (True, 0x31, 'same_handle'), (True, 0x32, 'other_handle')):
            with self.subTest(expected=expected):
                receiver, rows = self.make()
                if request_seen:
                    receiver.feed(RADIO, GENERATION, 4, acl(receiver._request), 10.01)
                receiver.feed(RADIO, GENERATION, 3, incomplete_notification(0x29, packet_handle),
                              10.02, capture_details=CAPTURE_DETAILS)
                self.assertEqual(receiver.reason, 'invalid_acl_length')
                self.assertEqual(rows[-1]['packet_relation'], expected)
                self.assertEqual(rows[-1]['request_seen'], request_seen)
                self.assertFalse(rows[-1]['ready'])
                self.assertTrue(valid_record(rows[-1]))

    def test_broken_diagnostic_sink_does_not_prevent_cancel_and_clear(self):
        receiver, _ = self.make()
        receiver.observation.send = Mock(side_effect=OSError('PRIVATE'))
        receiver.button = 'up'
        edges = receiver.stop(11, 'source_timeout')
        self.assertEqual(edges[0].action, 'cancel')
        self.assertTrue(receiver.stopped)
        self.assertIsNone(receiver.button)


class ProbeEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_completed_api_keeps_actual_status_and_result(self):
        from ovb_rc003.chromecast_device_windows import SelectedDevice
        from winrt.windows.devices.bluetooth import BluetoothCacheMode
        for status in (0, 1, 2, 3):
            with self.subTest(status=status):
                rows = []
                obs = Observation(rows.append)
                target = SelectedDevice(ENTITY)
                query = Mock()
                async def result(*args):
                    query(*args)
                    return SimpleNamespace(status=status, services=[])
                target.device = SimpleNamespace(get_gatt_services_for_uuid_with_cache_mode_async=result)
                marker = object()
                value = await worker._probe_source(target, marker, obs)
                self.assertEqual(value, (status == 0, 0))
                query.assert_called_once_with(marker, BluetoothCacheMode.UNCACHED)
                self.assertEqual([r['state'] for r in rows], ['begin', 'completed'])
                self.assertEqual(rows[-1]['status'], status)
                self.assertTrue(all(valid_record(r) for r in rows))

    async def test_api_exception_records_code_without_exception_text(self):
        rows = []
        error = OSError('PRIVATE DEVICE ADDRESS')
        error.hresult = -2147024891
        async def fail(_):
            raise error
        with self.assertRaises(OSError):
            await worker._probe_source(SimpleNamespace(probe=fail), None, Observation(rows.append))
        self.assertEqual(rows[-1]['state'], 'failed')
        self.assertEqual(rows[-1]['hresult'], 0x80070005)
        self.assertNotIn('PRIVATE', str(rows))
        self.assertTrue(all(valid_record(r) for r in rows))

    async def test_pending_probe_short_packet_worker_exports_reason_and_cancellation(self):
        identity = SessionIdentity.create(ENTITY)
        commands, reader = Channel(identity, commands=True), Channel(identity, commands=False)
        queue = deque([commands.encode('start', mode='run')])
        rows, packets = [], []
        async def opened():
            return None
        async def pending(_):
            packets.append((3, incomplete_notification(0x29), time.monotonic(), CAPTURE_DETAILS))
            await asyncio.Future()
        target = SimpleNamespace(open=opened, probe=pending, close=Mock(), radio=RADIO,
                                 attribute=0x29, changed=threading.Event())
        def poll():
            result = packets[:]
            packets.clear()
            return result
        capture = SimpleNamespace(start=Mock(), stop=Mock(), poll=poll, report_flow=Mock())
        pipe = SimpleNamespace(read=lambda: queue.popleft() if queue else None,
                               write=lambda raw: rows.append(reader.decode(raw)))
        with patch.multiple(worker, _selected=lambda _: True, sensitive_logging_enabled=lambda: True,
                            inspect_peer=lambda _: None, verify_peer=lambda *_: None,
                            SelectedDevice=lambda _: target, Capture=lambda _: capture):
            result = await asyncio.wait_for(worker.receive(identity, SimpleNamespace(pid=1), pipe), 2)
        self.assertEqual(result, 0)
        self.assertEqual(rows[-1]['reason'], 'source_unconfirmed')
        records = [r['record'] for r in rows if r['type'] == 'evidence']
        self.assertIn('source_probe_scheduled', [r['stage'] for r in records if r['kind'] == 'stage'])
        self.assertEqual([r['state'] for r in records if r['kind'] == 'source_probe'], ['begin', 'cancelled'])
        state = next(r for r in records if r['kind'] == 'source_state')
        self.assertEqual(state['reason'], 'invalid_acl_length')
        self.assertFalse(state['ready'])
        self.assertEqual(state['packet_relation'], 'unknown')
        capture.stop.assert_called_once()
        capture.report_flow.assert_called_once()
        target.close.assert_called_once()


class CaptureFlowTests(unittest.TestCase):
    def make(self):
        capture = etw.Capture(GENERATION)
        record = etw.Record()
        record.header.provider[:] = etw.PROVIDER
        record.header.id = 402
        record.header.stamp = int(time.time() * 1e7) + 116444736000000000
        values = {'BIP_Type': b'\x03', 'BIP_Data': b'PRIVATE', 'BIP_DataLen': (7).to_bytes(4, 'little')}
        capture._property = lambda _, name, maximum: values[name]
        return capture, record, values

    def test_filter_queue_and_parse_counts_have_no_callback_ipc(self):
        capture, record, values = self.make()
        rows = []
        capture.observe = rows.append
        for kind in (2, 3, 4, 9):
            values['BIP_Type'] = bytes([kind])
            capture._record(C.byref(record))
        record.header.id, record.header.version = 403, 7
        capture._record(C.byref(record))
        record.header.provider[:] = bytes(16)
        capture._record(C.byref(record))
        record.header.provider[:] = etw.PROVIDER
        record.header.id = 402
        values['BIP_Type'] = b'\x03'
        capture.bytes = 512 * 1024
        capture._record(C.byref(record))
        values['BIP_DataLen'] = bytes(4)
        capture._record(C.byref(record))
        self.assertEqual(rows, [])
        capture.report_flow()
        row = rows[0]
        self.assertEqual(row['counts'], dict(received=8, other_provider=1, other_event=1,
            other_kind=1, hci=1, rx=3, tx=1, queued=3, queue_overflow=1, parse_failed=1))
        self.assertEqual((row['first_other_event'], row['first_other_version'], row['first_other_kind']), (403, 7, 9))
        self.assertEqual(row['schema_step'], 'length')
        self.assertNotIn('PRIVATE', json.dumps(row))
        identity = SessionIdentity.create(ENTITY)
        sender, reader = Channel(identity, commands=False), Channel(identity, commands=False)
        self.assertEqual(reader.decode(sender.encode('evidence', record=row))['record'], row)
        self.assertFalse(valid_record(dict(row, payload='SECRET')))

    def test_native_property_failure_keeps_first_step_and_numeric_status(self):
        capture, record, _ = self.make()
        del capture._property
        capture.tdh = SimpleNamespace(TdhGetPropertySize=Mock(return_value=1168))
        capture._record(C.byref(record))
        capture._schema_failure('read', 'BIP_Data', 5)
        rows = []
        capture.observe = rows.append
        capture.report_flow()
        self.assertEqual(capture.failure, 'capture_failed')
        self.assertEqual(rows[0]['schema_property'], 'BIP_Type')
        self.assertEqual(rows[0]['schema_step'], 'size')
        self.assertEqual(rows[0]['schema_code'], 1168)
        self.assertTrue(valid_record(rows[0]))

    def test_new_evidence_survives_existing_zip_export_without_detailed_trace(self):
        from ovb_rc003 import log_export
        capture, record, _ = self.make()
        rows = []
        obs = Observation(rows.append)
        obs.source_probe('begin', time.monotonic())
        obs.source_probe('completed', time.monotonic(), status=0, services=0)
        receiver = ButtonReceiver(ENTITY, GENERATION, RADIO, 10, observation=obs)
        receiver.advance(26)
        capture.observe = rows.append
        capture._record(C.byref(record))
        capture.report_flow()
        identity = SessionIdentity.create(ENTITY)
        sender, reader = Channel(identity, commands=False), Channel(identity, commands=False)
        lines = []
        for row in rows:
            event = reader.decode(sender.encode('evidence', record=row))
            lines.append('Chromecast evidence run=%s seq=%s record=%s' % (
                identity.generation[:12], event['seq'], json.dumps(event['record'], separators=(',', ':'))))
        content = ('\n'.join(lines) + '\n').encode()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'logs').mkdir()
            (root / 'logs' / 'app.log').write_bytes(content)
            destination = root / 'evidence.zip'
            result = log_export.export_logs(destination, root=root)
            self.assertEqual(result.outcome, 'exported')
            self.assertFalse(result.incomplete)
            with zipfile.ZipFile(destination) as archive:
                self.assertEqual(archive.read('app.log'), content)
                self.assertNotIn('diagnostic-trace.jsonl', archive.namelist())

    def test_high_volume_has_fixed_memory_and_one_summary_not_per_packet_logs(self):
        capture, record, _ = self.make()
        capture.observe = Mock()
        for _ in range(10000):
            capture._record(C.byref(record))
            capture.poll()
        capture.observe.assert_not_called()
        capture.report_flow()
        capture.observe.assert_called_once()
        row = capture.observe.call_args.args[0]
        self.assertEqual(row['counts']['queued'], 10000)
        self.assertEqual(len(row['counts']), 10)
        self.assertLess(len(json.dumps(row)), 1024)
        capture.observe = Mock(side_effect=OSError())
        capture.report_flow()  # A broken sink cannot change capture state.
        self.assertFalse(capture.failure)


if __name__ == '__main__':
    unittest.main()
