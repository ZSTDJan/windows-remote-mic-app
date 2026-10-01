"""Selected-device voice discovery evidence and a bounded wake retry."""
import asyncio
from collections import deque
import copy
import threading
import time
import unittest
import uuid
from types import SimpleNamespace
from unittest import mock

from winrt.windows.devices.bluetooth import BluetoothCacheMode
from winrt.windows.devices.bluetooth.genericattributeprofile import GattCharacteristicProperties
from ovb_rc003 import chromecast_device_windows as device
from ovb_rc003 import chromecast_hid_worker as worker
from ovb_rc003 import chromecast_voice_receiver as adapter
from ovb_rc003.chromecast_channel import Channel, ChannelError, SessionIdentity
from ovb_rc003.chromecast_observation import Observation, valid_record
from ovb_rc003.chromecast_pipe_windows import PipeError
from ovb_rc003.chromecast_voice import GET_CAPABILITIES


ENTITY = "a" * 64
CAPS = bytes((11, 1, 0, 2, 3, 0, 160, 0, 0))


def service(*, voice=True, handle=0x37, handles=(0x39, 0x3b, 0x3e)):
    chars = [SimpleNamespace(uuid=uuid.UUID(f"ab5e000{part}-5a21-4f05-bc7d-af01f617b664"),
        attribute_handle=h, characteristic_properties=prop, add_value_changed=mock.Mock(),
        remove_value_changed=mock.Mock(),
        write_client_characteristic_configuration_descriptor_async=mock.AsyncMock(return_value=0))
        for part, h, prop in ((2, handles[0], GattCharacteristicProperties.WRITE),
                             (3, handles[1], GattCharacteristicProperties.NOTIFY),
                             (4, handles[2], GattCharacteristicProperties.NOTIFY))]
    return SimpleNamespace(uuid=device._VOICE_SERVICE_ID if voice else uuid.UUID(int=handle),
        attribute_handle=handle, close=mock.Mock(), chars=chars,
        device_access_information=SimpleNamespace(current_status=1), sharing_mode=1,
        get_characteristics_with_cache_mode_async=mock.AsyncMock(
            return_value=SimpleNamespace(status=0, characteristics=chars)))


def session():
    return SimpleNamespace(can_maintain_connection=True, maintain_connection=False,
        session_status=1, max_pdu_size=247, close=mock.Mock())


class DiscoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.sessions = []
        def create(_):
            self.sessions.append(session())
            return self.sessions[-1]
        self.session_factory = mock.AsyncMock(side_effect=create)
        patcher = mock.patch('winrt.windows.devices.bluetooth.genericattributeprofile.GattSession',
                             SimpleNamespace(from_device_id_async=self.session_factory))
        patcher.start()
        self.addCleanup(patcher.stop)

    def target(self, query):
        target = device.SelectedDevice(ENTITY)
        target.device = SimpleNamespace(connection_status=1, bluetooth_device_id=object(), close=mock.Mock(),
            device_access_information=SimpleNamespace(current_status=1),
            get_gatt_services_with_cache_mode_async=mock.AsyncMock(side_effect=query))
        return target

    def check_rows(self, target, *, attempt=1):
        rows = []
        obs = Observation(rows.append)
        for row in target.voice_queries:
            if row.get('kind') == 'voice_link':
                obs.voice_link(row, attempt)
            else:
                obs.voice_query(row, attempt)
        identity = SessionIdentity.create(ENTITY)
        sender, reader = Channel(identity, commands=False), Channel(identity, commands=False)
        for row in rows:
            self.assertTrue(valid_record(row), row)
            self.assertEqual(reader.decode(sender.encode("evidence", record=row))["record"], row)
        self.assertEqual(sum(r['kind'] == 'voice_link' for r in rows),
                         sum(r.get('kind') == 'voice_link' for r in target.voice_queries))
        return [r for r in rows if r['kind'] == 'voice_query']

    async def test_live_interfaces_connection_and_subscription_results_are_recorded(self):
        voice, other = service(), service(voice=False)
        target = self.target(lambda _: SimpleNamespace(status=0, services=[other, voice]))
        await target.open_voice(direct=True)
        rows = self.check_rows(target)
        self.assertEqual([r["step"] for r in rows],
            ["services", "characteristics", "audio_subscription", "control_subscription"])
        self.assertEqual(rows[0]["items"][1]["uuid"], str(device._VOICE_SERVICE_ID))
        self.assertEqual([r["connected_before"] for r in rows], [1] * 4)
        self.assertEqual([r["outcome"] for r in rows], ["success"] * 4)
        self.assertFalse(target.voice_retryable)
        other.close.assert_not_called()
        self.assertTrue(self.sessions[-1].maintain_connection)
        await target.close_voice()
        other.close.assert_called_once()
        voice.close.assert_called_once()
        self.assertFalse(self.sessions[-1].maintain_connection)
        self.sessions[-1].close.assert_called_once()

    async def test_timeout_keeps_cached_inventory_distinct_and_never_enables_voice(self):
        cached = service()
        target = self.target(None)
        async def query(mode):
            if mode == BluetoothCacheMode.UNCACHED:
                target.device.connection_status = 0
                await asyncio.Event().wait()
            return SimpleNamespace(status=0, services=[cached])
        target.device.get_gatt_services_with_cache_mode_async.side_effect = query
        with mock.patch.object(device, "_VOICE_SERVICE_TIMEOUT", .01):
            with self.assertRaises(TimeoutError):
                await target.open_voice(direct=True)
        rows = self.check_rows(target)
        self.assertEqual([(r["cache"], r["step"], r["outcome"]) for r in rows],
            [("live", "services", "timeout"), ("cached", "services", "success"),
             ("cached", "characteristics", "success")])
        self.assertEqual((rows[0]["connected_before"], rows[0]["connected_after"]), (1, 0))
        self.assertTrue(target.voice_retryable)
        self.assertIsNone(target.voice_tx)
        self.assertEqual(target._voice_tokens, [])
        cached.close.assert_called_once()
        # No repeated cached enumeration when the one wake retry also fails.
        with mock.patch.object(device, "_VOICE_SERVICE_TIMEOUT", .01):
            with self.assertRaises(TimeoutError):
                await target.open_voice(direct=True)
        self.assertEqual(len(self.check_rows(target)), 1)
        self.check_rows(target, attempt=2)

    async def test_missing_service_access_denied_and_unreachable_are_distinguishable(self):
        for status, retry in ((0, False), (1, True), (2, False), (3, False)):
            with self.subTest(status=status):
                target = self.target(lambda _: SimpleNamespace(status=status, services=[],
                    protocol_error=5 if status == 2 else None))
                with self.assertRaises(PipeError):
                    await target.open_voice()
                self.assertEqual(target.voice_retryable, retry)
                row = self.check_rows(target)[0]
                self.assertEqual(row["status"], status)
                self.assertEqual(row["protocol_error"], 5 if status == 2 else -1)
                self.assertEqual(row["outcome"], "success" if status == 0 else "status_failed")

    async def test_os_error_records_only_numeric_code_and_cached_cleanup_cannot_mask_it(self):
        error = OSError("PRIVATE device path")
        error.winerror = -2147024891
        cached = service()
        cached.close.side_effect = OSError("PRIVATE cleanup")
        target = self.target([error, SimpleNamespace(status=0, services=[cached])])
        with self.assertRaises(OSError) as raised:
            await target.open_voice()
        self.assertIs(raised.exception, error)
        rows = self.check_rows(target)
        self.assertEqual(rows[0]["hresult"], 0x80070005)
        self.assertFalse(target.voice_retryable)
        self.assertNotIn("PRIVATE", str(rows))

    async def test_direct_layout_uses_discovered_objects_and_raw_capture_remains_restricted(self):
        # Synthetic B-like positions, not a claim about its still-unread real characteristics.
        changed = service(handle=0x50, handles=(0x51, 0x53, 0x56))
        target = self.target(lambda _: SimpleNamespace(status=0, services=[changed]))
        await target.open_voice(direct=True)
        self.assertEqual(target.voice_attributes, {0x53: 'audio', 0x56: 'control'})
        for char, value in ((changed.chars[2], b'\x08'), (changed.chars[1], b'\x01\x02')):
            char.add_value_changed.call_args.args[0](None, SimpleNamespace(characteristic_value=value))
        self.assertEqual([(h, v) for h, v, _ in target.poll_voice()], [(0x56, b'\x08'), (0x53, b'\x01\x02')])
        rows = self.check_rows(target)
        self.assertEqual(rows[1]['items'][1]['handle'], 0x53)
        await target.close_voice()
        with self.assertRaisesRegex(PipeError, "unsupported_layout"):
            await target.open_voice(direct=False)
        self.assertFalse(target.voice_retryable)
        self.assertEqual(target._voice_tokens, [])

    async def test_cancel_discovery_records_cancellation_without_starting_another_query(self):
        entered = asyncio.Event()
        async def query(_):
            entered.set()
            await asyncio.Event().wait()
        target = self.target(query)
        task = asyncio.create_task(target.open_voice())
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.check_rows(target)[0]["outcome"], "cancelled")
        target.device.get_gatt_services_with_cache_mode_async.assert_awaited_once()
        self.assertFalse(target.voice_retryable)

    async def test_session_precedes_queries_and_preserves_services_until_close(self):
        voice, other = service(), service(voice=False)
        target = self.target(None)
        async def query(_):
            self.assertTrue(self.sessions[-1].maintain_connection)
            self.session_factory.assert_awaited_once_with(target.device.bluetooth_device_id)
            return SimpleNamespace(status=0, services=[voice, other])
        async def characteristics(_):
            other.close.assert_not_called()
            self.assertEqual(self.sessions[-1].session_status, 1)
            return SimpleNamespace(status=0, characteristics=voice.chars)
        target.device.get_gatt_services_with_cache_mode_async.side_effect = query
        voice.get_characteristics_with_cache_mode_async.side_effect = characteristics
        await target.open_voice(direct=True)
        self.assertEqual(target.voice_evidence['mtu'], 247)
        await target.close_voice()
        target.close()
        self.sessions[-1].close.assert_called_once()
        self.assertIsNone(target.device)
        self.check_rows(target)

    async def test_user_access_denial_is_recorded_without_a_permission_request_or_retry(self):
        voice = service(handle=0x50)
        voice.device_access_information.current_status = 2
        voice.get_characteristics_with_cache_mode_async.return_value = SimpleNamespace(status=3, characteristics=[])
        target = self.target(lambda _: SimpleNamespace(status=0, services=[voice]))
        with self.assertRaisesRegex(PipeError, 'unsupported_layout'):
            await target.open_voice(direct=True)
        row = next(r for r in target.voice_queries if r.get('kind') == 'voice_link' and r['step'] == 'characteristics')
        self.assertEqual((row['device_access'], row['service_access'], row['service_handle']), (1, 2, 0x50))
        self.assertEqual((row['session_status'], row['maintain'], row['sharing']), (1, 1, 1))
        self.assertEqual(row['outcome'], 'status_failed')
        self.assertFalse(target.voice_retryable)
        self.assertIsNone(target.voice_session)
        self.sessions[-1].close.assert_called_once()
        self.assertFalse(self.sessions[-1].maintain_connection)
        self.check_rows(target)
        encoded = dict(row, attempt=1, elapsed_ms=1)
        for bad in (dict(encoded, service_access=4), dict(encoded, maintain=True),
                    dict(encoded, step='PRIVATE'), dict(encoded, address='PRIVATE')):
            self.assertFalse(valid_record(bad))

    async def test_closed_session_wait_is_bounded_and_does_not_query_characteristics(self):
        voice = service()
        held = session()
        held.session_status = 0
        self.session_factory.side_effect = None
        self.session_factory.return_value = held
        target = self.target(lambda _: SimpleNamespace(status=0, services=[voice]))
        with mock.patch.object(device, '_VOICE_SESSION_TIMEOUT', .01):
            with self.assertRaises(TimeoutError):
                await target.open_voice(direct=True)
        voice.get_characteristics_with_cache_mode_async.assert_not_awaited()
        self.assertFalse(held.maintain_connection)
        held.close.assert_called_once()
        self.assertTrue(target.voice_retryable)
        row = next(r for r in target.voice_queries if r.get('kind') == 'voice_link' and r['step'] == 'session_active')
        self.assertEqual((row['outcome'], row['session_status']), ('timeout', 0))
        self.check_rows(target)

    async def test_cancel_pending_session_creation_or_characteristics_releases_owned_resources(self):
        for phase in ('session', 'characteristics'):
            with self.subTest(phase=phase):
                entered = asyncio.Event()
                async def pending(_):
                    entered.set()
                    await asyncio.Event().wait()
                voice = service()
                target = self.target(lambda _: SimpleNamespace(status=0, services=[voice]))
                if phase == 'session':
                    self.session_factory.side_effect = pending
                else:
                    held = session()
                    self.session_factory.side_effect = None
                    self.session_factory.return_value = held
                    voice.get_characteristics_with_cache_mode_async.side_effect = pending
                task = asyncio.create_task(target.open_voice(direct=True))
                await entered.wait()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertIsNone(target.voice_session)
                self.assertEqual(target._voice_services, [])
                self.assertEqual(target._voice_tokens, [])
                if phase == 'characteristics':
                    held.close.assert_called_once()
                    self.assertFalse(held.maintain_connection)
                    voice.close.assert_called_once()
                self.check_rows(target)

    async def test_partial_subscription_failure_unsubscribes_and_drops_late_callbacks(self):
        voice = service()
        voice.chars[2].write_client_characteristic_configuration_descriptor_async.side_effect = [3, 0]
        target = self.target(lambda _: SimpleNamespace(status=0, services=[voice]))
        with self.assertRaisesRegex(PipeError, 'voice_subscribe_failed'):
            await target.open_voice(direct=True)
        for char in voice.chars[1:]:
            self.assertEqual(char.write_client_characteristic_configuration_descriptor_async.await_count, 2)
            char.remove_value_changed.assert_called_once()
            char.add_value_changed.call_args.args[0](None, SimpleNamespace(characteristic_value=b'late'))
        self.assertEqual(target.poll_voice(), [])
        self.assertEqual(target.voice_attributes, {})
        self.assertFalse(target.voice_retryable)
        self.sessions[-1].close.assert_called_once()
        self.check_rows(target)

    async def test_cleanup_failure_still_closes_session_and_prevents_reopen(self):
        for failed_owner in ('service', 'session'):
            with self.subTest(failed_owner=failed_owner):
                voice = service()
                target = self.target(lambda _: SimpleNamespace(status=0, services=[voice]))
                await target.open_voice(direct=True)
                held = self.sessions[-1]
                creation_count = self.session_factory.await_count
                owner = voice if failed_owner == 'service' else held
                owner.close.side_effect = OSError('PRIVATE')
                with self.assertRaisesRegex(PipeError, 'cleanup_failed'):
                    await target.close_voice()
                held.close.assert_called_once()
                self.assertFalse(held.maintain_connection)
                if failed_owner == 'service':
                    self.assertEqual(target._voice_services, [voice])
                    self.assertIs(target.voice_service, voice)
                    self.assertIsNone(target.voice_session)
                else:
                    self.assertEqual(target._voice_services, [])
                    self.assertIs(target.voice_session, held)
                with self.assertRaisesRegex(PipeError, 'cleanup_failed'):
                    await target.open_voice(direct=True)
                self.assertEqual(self.session_factory.await_count, creation_count)
                self.assertNotIn('PRIVATE', str(target.voice_queries))
                owner.close.side_effect = None
                with self.assertRaisesRegex(PipeError, 'cleanup_failed'):
                    target.close()
                self.assertEqual(owner.close.call_count, 3)
                self.assertEqual(target._voice_services, [])
                self.assertIsNone(target.voice_service)
                self.assertIsNone(target.voice_session)
                self.assertIsNone(target.device)

    async def test_ambiguous_or_missing_characteristics_never_subscribe(self):
        for invalid in ('missing', 'duplicate_uuid', 'duplicate_handle', 'invalid_handle', 'not_writable', 'not_notify'):
            with self.subTest(invalid=invalid):
                voice = service()
                if invalid == 'missing': voice.chars.pop()
                elif invalid == 'duplicate_uuid': voice.chars.append(copy.copy(voice.chars[0]))
                elif invalid == 'duplicate_handle': voice.chars[1].attribute_handle = voice.chars[0].attribute_handle
                elif invalid == 'invalid_handle': voice.chars[1].attribute_handle = voice.attribute_handle
                elif invalid == 'not_writable': voice.chars[0].characteristic_properties = GattCharacteristicProperties.READ
                else: voice.chars[1].characteristic_properties = GattCharacteristicProperties.READ
                target = self.target(lambda _: SimpleNamespace(status=0, services=[voice]))
                with self.assertRaisesRegex(PipeError, 'unsupported_layout'):
                    await target.open_voice(direct=True)
                self.assertFalse(target._voice_open)
                self.assertEqual(target._voice_tokens, [])
                self.sessions[-1].close.assert_called_once()

    async def test_dynamic_notifications_reach_voice_gate_and_are_counted_once(self):
        voice = service(handle=0x50, handles=(0x51, 0x53, 0x56))
        target = self.target(lambda _: SimpleNamespace(status=0, services=[voice]))
        rows, sent = [], []
        obs = Observation(rows.append)
        with mock.patch.object(adapter.config, 'load_config', return_value={}):
            receiver = adapter.VoiceReceiver(target, lambda *e: sent.append(e),
                observation=obs, direct_notifications=True)
        async def write_caps(command):
            self.assertEqual(command, GET_CAPABILITIES)
            for part, value in ((2, CAPS), (2, b'\x04\x03\x02\x01'), (1, b'\x01\x02')):
                voice.chars[part].add_value_changed.call_args.args[0](None, SimpleNamespace(characteristic_value=value))
            for handle, value, stamp in target.poll_voice():
                receiver.notification(handle, value, stamp)
        target.write_voice = mock.AsyncMock(side_effect=write_caps)
        await receiver.open()
        self.assertTrue(receiver.available)
        self.assertEqual(sum(e[0] == 'host_start' for e in sent), 1)
        self.assertEqual((obs.counts['controls'], obs.counts['audio_packets'], obs.counts['audio_bytes']), (2, 1, 2))
        self.assertTrue(any(r.get('kind') == 'voice_link' for r in rows))
        self.assertTrue(all(valid_record(r) for r in rows))
        await target.close_voice()

    async def test_negotiation_failure_releases_the_open_connection_and_reports_cleanup(self):
        voice = service()
        target = self.target(lambda _: SimpleNamespace(status=0, services=[voice]))
        target.write_voice = mock.AsyncMock(side_effect=OSError('PRIVATE'))
        rows = []
        with mock.patch.object(adapter.config, 'load_config', return_value={}):
            receiver = adapter.VoiceReceiver(target, lambda *e: None,
                observation=Observation(rows.append), direct_notifications=True)
        await receiver.open()
        self.assertFalse(receiver.available)
        self.assertFalse(receiver.retry_after_activity())
        self.assertIsNone(target.voice_session)
        self.sessions[-1].close.assert_called_once()
        self.assertFalse(self.sessions[-1].maintain_connection)
        self.assertTrue(any(r.get('kind') == 'voice_link' and r['step'] == 'closed' for r in rows))
        self.assertNotIn('PRIVATE', str(rows))

    async def test_cancel_unsubscribe_still_releases_service_and_session(self):
        voice = service()
        target = self.target(lambda _: SimpleNamespace(status=0, services=[voice]))
        await target.open_voice(direct=True)
        entered = asyncio.Event()
        async def pending(_):
            entered.set()
            await asyncio.Event().wait()
        voice.chars[1].write_client_characteristic_configuration_descriptor_async.side_effect = pending
        task = asyncio.create_task(target.close_voice())
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIsNone(target.voice_session)
        self.assertFalse(self.sessions[-1].maintain_connection)
        self.sessions[-1].close.assert_called_once()
        voice.close.assert_called_once()
        self.assertTrue(target._voice_cleanup_failed)

    async def test_large_inventory_is_bounded_and_every_chunk_fits_real_pipe(self):
        services = [service(voice=False, handle=i) for i in range(40)]
        target = self.target(lambda _: SimpleNamespace(status=0, services=services))
        with self.assertRaises(PipeError):
            await target.open_voice()
        rows = self.check_rows(target)
        self.assertEqual([r["item_offset"] for r in rows], [0, 8, 16, 24])
        self.assertEqual(sum(len(r["items"]) for r in rows), 32)
        self.assertTrue(all(r["item_count"] == 40 for r in rows))
        for svc in services:
            svc.close.assert_called_once()
        for changes in ({"items": rows[0]["items"] * 2}, {"hresult": "PRIVATE"},
                        {"attempt": 3}, {"cache": "guess"}, {"error": "PRIVATE"},
                        {"connected_before": True}, {"item_offset": 7}):
            bad = dict(rows[0], **changes)
            self.assertFalse(valid_record(bad))
            with self.assertRaises(ChannelError):
                Channel(SessionIdentity.create(ENTITY), commands=False).encode("evidence", record=bad)
        bad = copy.deepcopy(rows[0])
        bad["items"][0]["uuid"] = "PRIVATE".ljust(36, "0")
        self.assertFalse(valid_record(bad))


class WakeRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_retry_requires_recent_selected_press_transient_failure_and_running_service(self):
        target = SimpleNamespace(open_voice=mock.AsyncMock(side_effect=TimeoutError),
                                 voice_retryable=True, voice_attributes={})
        with mock.patch.object(adapter.config, "load_config", return_value={}):
            receiver = adapter.VoiceReceiver(target, lambda *args: None, direct_notifications=True)
        receiver.note_activity(time.monotonic() - 1)
        await receiver.open()
        self.assertFalse(receiver.retry_after_activity())
        receiver.note_activity(time.monotonic())
        self.assertTrue(receiver.retry_after_activity())
        target.voice_retryable = False
        self.assertFalse(receiver.retry_after_activity())
        target.voice_retryable = True
        receiver.stop()
        self.assertFalse(receiver.retry_after_activity())

    async def test_worker_delivers_buttons_and_only_one_retry_then_stops_or_cancels(self):
        for outcome in ("ready", "failed", "cancel"):
            with self.subTest(outcome=outcome):
                identity = SessionIdentity.create(ENTITY)
                commands, events = Channel(identity, commands=True), Channel(identity, commands=False)
                inbox = deque([commands.encode("start", mode="run", voice=True)])
                reports = deque([("hook_ready", "", time.monotonic())])
                notifications, received = deque(), []
                attempts = 0
                after_second_failure = 0
                cancelled = False

                async def open_voice(**_):
                    nonlocal attempts, cancelled
                    attempts += 1
                    if attempts == 1 or outcome == "failed":
                        raise TimeoutError
                    if outcome == "cancel":
                        inbox.append(commands.encode("stop"))
                        try:
                            await asyncio.Event().wait()
                        finally:
                            cancelled = True

                async def write_voice(command):
                    self.assertEqual(command, GET_CAPABILITIES)
                    notifications.append((0x3f, CAPS, time.monotonic()))

                def poll_voice():
                    result = list(notifications)
                    notifications.clear()
                    return result

                class Tap:
                    def start(self): pass
                    def abort_start(self): pass
                    def source_alive(self): return True
                    def close(self, **_): pass
                    def poll(self):
                        nonlocal after_second_failure
                        if attempts == 2 and outcome == "failed":
                            after_second_failure += 1
                            if after_second_failure == 4:
                                inbox.append(commands.encode("stop"))
                        result = list(reports)
                        reports.clear()
                        return result

                def write(raw):
                    event = events.decode(raw)
                    received.append(event)
                    if event["type"] == "voice" and event["event"] == "available":
                        if event["data"] == "ready":
                            inbox.append(commands.encode("stop"))
                        else:
                            reports.extend(("report", value, time.monotonic()) for value in
                                ("0600000000000000", "0000000000000000"))

                target = SimpleNamespace(changed=threading.Event(), open=mock.AsyncMock(),
                    open_voice=open_voice, voice_retryable=True, voice_queries=[],
                    voice_attributes={0x3f: "control", 0x3c: "audio"},
                    write_voice=write_voice, poll_voice=poll_voice, close_voice=mock.AsyncMock(), close=mock.Mock())
                with mock.patch.object(worker, "_selected", return_value=True), \
                     mock.patch.object(worker, "SelectedDevice", return_value=target), \
                     mock.patch.object(worker, "HidTap", return_value=Tap()), \
                     mock.patch.object(worker, "verify_peer"), mock.patch.object(worker, "inspect_peer"), \
                     mock.patch.object(adapter.config, "load_config", return_value={}):
                    result = await asyncio.wait_for(worker.receive(identity, SimpleNamespace(pid=1),
                        SimpleNamespace(read=lambda: inbox.popleft() if inbox else None, write=write)), 3)
                self.assertEqual(result, 0)
                self.assertEqual(attempts, 2)
                self.assertTrue(any(e["type"] == "edge" and e["action"] == "down" for e in received))
                self.assertEqual(sum(e.get("record", {}).get("stage") == "voice_wake_retry"
                                     for e in received), 1)
                self.assertEqual(cancelled, outcome == "cancel")
                target.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
