"""Contract tests for ble_transport_winrt.py against an in-memory fake of
the locked winrt-Windows.*==3.2.1 projection surface (see
tests/fakes/fake_winrt.py), fixed after XRBM-014 review RETRY P1 #1 and
again after review round 2 P1 #2:

- discovery uses BluetoothLEDevice.get_device_selector_from_pairing_state(True)
  passed to DeviceInformation.find_all_async_aqs_filter(selector) - not
  GattDeviceService.get_device_selector_from_uuid(), which enumerates a
  different (GATT service-instance) WinRT ID domain than
  BluetoothLEDevice.from_id_async() requires;
- from_id_async() rejects a GATT service-instance-domain ID outright (see
  RejectsWrongIdDomainTests below);
- every GATT UUID is a real uuid.UUID by the time it reaches the fake;
- write_value_with_result_async's returned object's .status is compared,
  not the object itself;
- add_value_changed/add_connection_status_changed tokens are exactly what
  gets passed back to remove_value_changed/remove_connection_status_changed
  (the fakes themselves assert this - a wrong token raises AssertionError);
- CCCD is written back to NONE and the service/device are closed on cleanup;
- a real BluetoothConnectionStatus.DISCONNECTED callback is observed.

This is not a substitute for running against a live WinRT runtime and real
hardware (still 待核验 - see this package's top-level README.md "Known gaps"
section for the full list of what remains unverified on real hardware), but it
does prove the module's own call shape is internally consistent and matches
the documented 3.2.1 signatures as closely as this project can verify
without one.
"""

import asyncio
import queue
import threading
import time
import unittest
import uuid
from unittest import mock

from ovb_rc003 import atvv_protocol as proto
from ovb_rc003 import atvv_session
from ovb_rc003 import identity
from ovb_rc003.ble_transport_winrt import (
    BATTERY_SERVICE_UUID,
    NoReachableCandidateError,
    RC003BleSession,
    _candidate_has_voice_service,
    _candidate_has_voice_service_with_hard_timeout,
    discover_candidates,
    select_connectable_candidate,
)

from .fakes.fake_winrt import (
    FakeDeviceInformation,
    FakeWinRTEnvironment,
    gatt_service_instance_id,
)


def _run(coro):
    # Explicitly closing the loop (XRBM-014 review round 2 evidence: a
    # ResourceWarning for an unclosed test event loop) rather than letting
    # it be garbage-collected.
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _wait_until(predicate, timeout=2.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


async def _async_wait_until(predicate, timeout=2.0, interval=0.01):
    """Like _wait_until, but yields control back to the event loop between
    checks (via a real ``asyncio.sleep``) instead of blocking the whole
    thread - required for anything that depends on the event loop itself
    processing a ``call_soon_threadsafe``-scheduled callback (as
    send_mic_open_threadsafe() does), which a synchronous busy-wait would
    starve.
    """

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


def _caps_payload(codec_bit=0x02, frame_size=4):
    return bytes((0x0B, 0x01, 0x00, codec_bit, 0x03)) + frame_size.to_bytes(2, "big")


class DiscoverCandidatesTests(unittest.TestCase):
    def test_uses_the_paired_ble_device_selector_and_returns_a_candidate(self):
        env = FakeWinRTEnvironment(name="MI RC")
        winrt = env.build_winrt_modules()

        candidates = _run(discover_candidates(winrt=winrt))

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].name, "MI RC")
        self.assertIs(candidates[0].handle, env.discovered_info)
        # The candidate's handle.id must be the BLE-device-domain ID this
        # environment's paired-device factory would accept - not a GATT
        # service-instance ID (see RejectsWrongIdDomainTests below).
        self.assertEqual(candidates[0].handle.id, env.device_id)

    def test_candidate_is_selectable_via_identity(self):
        env = FakeWinRTEnvironment(name="Xiaomi Bluetooth Remote 2 Pro")
        winrt = env.build_winrt_modules()

        candidates = _run(discover_candidates(winrt=winrt))
        chosen = identity.select_single_candidate(candidates)
        self.assertEqual(chosen.name, "Xiaomi Bluetooth Remote 2 Pro")

    def test_logs_duplicate_device_ids_without_logging_the_id(self):
        env = FakeWinRTEnvironment(device_id="private-device-id", name="MI RC")
        env.discovered_infos.append(
            FakeDeviceInformation("PRIVATE-DEVICE-ID", "MI RC")
        )

        with self.assertLogs("ovb_rc003.ble_transport_winrt", level="INFO") as logs:
            candidates = _run(discover_candidates(winrt=env.build_winrt_modules()))

        self.assertEqual(len(candidates), 2)
        combined = "\n".join(logs.output)
        self.assertIn("total=2", combined)
        self.assertIn("rc003_name_matches=2", combined)
        self.assertIn("unique_device_ids=1", combined)
        self.assertIn("duplicate_device_id_entries=1", combined)
        self.assertNotIn("private-device-id", combined.lower())

    def test_logs_distinct_device_ids_without_logging_either_id(self):
        env = FakeWinRTEnvironment(device_id="private-device-id-a", name="MI RC")
        env.discovered_infos.append(
            FakeDeviceInformation("private-device-id-b", "MI RC")
        )

        with self.assertLogs("ovb_rc003.ble_transport_winrt", level="INFO") as logs:
            candidates = _run(discover_candidates(winrt=env.build_winrt_modules()))

        self.assertEqual(len(candidates), 2)
        combined = "\n".join(logs.output)
        self.assertIn("total=2", combined)
        self.assertIn("unique_device_ids=2", combined)
        self.assertIn("duplicate_device_id_entries=0", combined)
        self.assertNotIn("private-device-id-a", combined)
        self.assertNotIn("private-device-id-b", combined)


class SelectConnectableCandidateTests(unittest.TestCase):
    @staticmethod
    def _candidates():
        return [
            identity.RC003Candidate(name="MI RC", hardware_match=False, handle=object()),
            identity.RC003Candidate(
                name="小米蓝牙语音遥控器", hardware_match=False, handle=object()
            ),
        ]

    def test_single_match_uses_fast_path_without_probing(self):
        candidate = identity.RC003Candidate(
            name="MI RC", hardware_match=False, handle=object()
        )

        async def probe(_candidate):
            raise AssertionError("single-candidate fast path must not probe")

        chosen = _run(select_connectable_candidate([candidate], probe=probe))

        self.assertIs(chosen, candidate)

    def test_multiple_matches_select_the_only_reachable_voice_device(self):
        candidates = self._candidates()

        async def probe(candidate):
            return candidate is candidates[0]

        chosen = _run(select_connectable_candidate(candidates, probe=probe))

        self.assertIs(chosen, candidates[0])

    def test_real_probe_timeout_does_not_block_later_reachable_candidate(self):
        candidates = self._candidates()
        with mock.patch(
            "ovb_rc003.ble_transport_winrt."
            "_candidate_has_voice_service_with_hard_timeout",
            side_effect=[None, True],
        ) as hard_probe:
            chosen = _run(select_connectable_candidate(candidates))

        self.assertIs(chosen, candidates[1])
        self.assertEqual(hard_probe.call_count, 2)

    def test_multiple_reachable_voice_devices_remain_ambiguous(self):
        candidates = self._candidates()

        async def probe(_candidate):
            return True

        with self.assertRaises(identity.AmbiguousCandidateError) as ctx:
            _run(select_connectable_candidate(candidates, probe=probe))

        self.assertEqual(ctx.exception.count, 2)

    def test_no_reachable_voice_device_fails_without_guessing(self):
        candidates = self._candidates()

        async def probe(_candidate):
            return False

        with self.assertRaises(NoReachableCandidateError) as ctx:
            _run(select_connectable_candidate(candidates, probe=probe))

        self.assertEqual(ctx.exception.count, 2)

    def test_real_probe_uses_uncached_service_query_and_closes_resources(self):
        env = FakeWinRTEnvironment()
        candidate = identity.RC003Candidate(
            name=env.name, hardware_match=False, handle=env.discovered_info
        )

        reachable = _run(
            _candidate_has_voice_service(candidate, env.build_winrt_modules())
        )

        self.assertTrue(reachable)
        self.assertEqual(
            env.device.service_query_cache_modes,
            [env.build_winrt_modules().bluetooth_cache_mode.UNCACHED],
        )
        self.assertTrue(env.service.closed)
        self.assertTrue(env.device.closed)

    def test_hard_timeout_does_not_wait_for_stuck_platform_coroutine(self):
        release = threading.Event()
        env = FakeWinRTEnvironment()
        candidate = identity.RC003Candidate(
            name=env.name,
            hardware_match=False,
            handle=env.discovered_info,
        )
        modules = env.build_winrt_modules()
        original_from_id = modules.bluetooth_le_device.from_id_async

        async def stuck_from_id(device_id):
            while not release.is_set():
                await asyncio.sleep(0.01)
            return await original_from_id(device_id)

        modules.bluetooth_le_device.from_id_async = stuck_from_id
        started = time.monotonic()
        try:
            result = _run(
                _candidate_has_voice_service_with_hard_timeout(
                    candidate,
                    winrt=modules,
                    timeout=0.03,
                )
            )
        finally:
            release.set()

        self.assertIsNone(result)
        self.assertLess(time.monotonic() - started, 0.5)

    def test_real_probe_treats_unreachable_service_as_unavailable_and_closes(self):
        env = FakeWinRTEnvironment()

        async def unreachable(_service_uuid, cache_mode):
            env.device.service_query_cache_modes.append(cache_mode)
            from .fakes.fake_winrt import (
                FakeGattCommunicationStatus,
                FakeGattServicesResult,
            )

            return FakeGattServicesResult(FakeGattCommunicationStatus.UNREACHABLE, [])

        env.device.get_gatt_services_for_uuid_with_cache_mode_async = unreachable
        candidate = identity.RC003Candidate(
            name=env.name, hardware_match=False, handle=env.discovered_info
        )

        reachable = _run(
            _candidate_has_voice_service(candidate, env.build_winrt_modules())
        )

        self.assertFalse(reachable)
        self.assertTrue(env.device.closed)


class RejectsWrongIdDomainTests(unittest.TestCase):
    """XRBM-018 DoD 2: "the fake no longer accepts a GATT service id as a
    BLE device ID" - proving the two WinRT ID domains XRBM-014 review round
    2 P1 #2 identified are actually distinct in this test double, not just
    in the production code's comments.
    """

    def test_from_id_async_rejects_a_gatt_service_instance_id(self):
        env = FakeWinRTEnvironment()
        winrt = env.build_winrt_modules()
        wrong_domain_id = gatt_service_instance_id(uuid.UUID(proto.VOICE_SERVICE_UUID))
        self.assertNotEqual(wrong_domain_id, env.device_id)

        async def scenario():
            return await winrt.bluetooth_le_device.from_id_async(wrong_domain_id)

        with self.assertRaises(AssertionError):
            _run(scenario())

    def test_from_id_async_accepts_the_real_paired_device_id(self):
        env = FakeWinRTEnvironment()
        winrt = env.build_winrt_modules()

        async def scenario():
            return await winrt.bluetooth_le_device.from_id_async(env.device_id)

        device = _run(scenario())
        self.assertIs(device, env.device)


class ConnectTests(unittest.TestCase):
    def _connect_session(self, env, **kwargs):
        winrt = env.build_winrt_modules()
        session = RC003BleSession(winrt=winrt, loop=asyncio.get_event_loop(), **kwargs)
        candidate = identity.RC003Candidate(
            name=env.name, hardware_match=False, handle=env.discovered_info
        )
        return session, candidate

    def test_connect_subscribes_notify_on_audio_and_control(self):
        env = FakeWinRTEnvironment()

        async def scenario():
            session, candidate = self._connect_session(
                env, on_pcm_frame=lambda samples: None
            )
            await session.connect(candidate)
            return session

        session = _run(scenario())
        try:
            self.assertIn(1, env.audio_characteristic.cccd_history)  # NOTIFY == 1
            self.assertIn(1, env.control_characteristic.cccd_history)
        finally:
            _run(session.close())

    def test_connect_sends_get_capabilities_to_tx(self):
        env = FakeWinRTEnvironment()

        async def scenario():
            session, candidate = self._connect_session(
                env, on_pcm_frame=lambda samples: None
            )
            await session.connect(candidate)
            return session

        session = _run(scenario())
        try:
            self.assertEqual(env.tx_characteristic.write_history[-1], proto.GET_CAPABILITIES_V10)
        finally:
            _run(session.close())

        self.assertTrue(env.data_writers)
        self.assertTrue(all(writer.close_calls == 1 for writer in env.data_writers))

    def test_connect_can_use_the_isolated_on_request_probe_capabilities(self):
        env = FakeWinRTEnvironment()

        async def scenario():
            session, candidate = self._connect_session(
                env,
                on_pcm_frame=lambda samples: None,
                get_capabilities_command=proto.GET_CAPABILITIES_ON_REQUEST_V10,
            )
            await session.connect(candidate)
            return session

        session = _run(scenario())
        try:
            self.assertEqual(
                env.tx_characteristic.write_history[-1],
                proto.GET_CAPABILITIES_ON_REQUEST_V10,
            )
        finally:
            _run(session.close())

    def test_tx_writer_is_closed_even_when_writer_close_itself_fails(self):
        env = FakeWinRTEnvironment()
        close_error = RuntimeError("simulated writer close failure")
        env.data_writer_close_error = close_error

        async def scenario():
            session = RC003BleSession(
                on_pcm_frame=lambda samples: None,
                winrt=env.build_winrt_modules(),
                loop=asyncio.get_event_loop(),
            )
            session._tx_characteristic = env.tx_characteristic
            with self.assertRaises(RuntimeError) as ctx:
                await session._write_tx(b"test")
            self.assertIs(ctx.exception, close_error)

        _run(scenario())
        self.assertEqual(env.tx_characteristic.write_history, [b"test"])
        self.assertEqual(env.data_writers[0].close_calls, 1)

    def test_connect_subscribes_to_connection_status(self):
        env = FakeWinRTEnvironment()

        async def scenario():
            session, candidate = self._connect_session(
                env, on_pcm_frame=lambda samples: None
            )
            await session.connect(candidate)
            return session

        session = _run(scenario())
        try:
            self.assertEqual(len(env.device._connection_status_handlers), 1)
        finally:
            _run(session.close())


class BatteryMonitorTests(unittest.TestCase):
    async def _wait(self, predicate):
        deadline = asyncio.get_running_loop().time() + 2.0
        while not predicate():
            if asyncio.get_running_loop().time() >= deadline:
                self.fail("battery worker did not reach expected state")
            await asyncio.sleep(0.005)

    def _session_and_candidate(self, env, levels):
        battery_env = FakeWinRTEnvironment(device_id=env.device_id)
        modules = env.build_winrt_modules()
        owner_thread = threading.get_ident()
        original_open = modules.bluetooth_le_device.from_id_async

        async def open_device(device_id):
            if threading.get_ident() == owner_thread:
                return await original_open(device_id)
            self.assertEqual(device_id, battery_env.device_id)
            return battery_env.device

        modules.bluetooth_le_device.from_id_async = staticmethod(open_device)
        session = RC003BleSession(
            on_pcm_frame=lambda _samples: None,
            on_battery_level=levels.append,
            winrt=modules,
            loop=asyncio.get_running_loop(),
        )
        candidate = identity.RC003Candidate(
            name=env.name, hardware_match=False, handle=env.discovered_info,
        )
        return session, candidate, battery_env

    def test_reads_notifies_and_closes_only_independently_owned_resources(self):
        env = FakeWinRTEnvironment()
        levels = []

        async def scenario():
            session, candidate, battery = self._session_and_candidate(env, levels)
            await session.connect(candidate)
            self.assertEqual(levels, [])
            session.start_battery_monitor()
            monitor = session._battery_monitor
            try:
                await self._wait(lambda: levels == [59])
                self.assertEqual(battery.battery_characteristic.read_cache_modes, [1])
                self.assertIn(1, battery.battery_characteristic.cccd_history)
                battery.battery_characteristic.fire(bytes((42,)))
                battery.battery_characteristic.fire(bytes((101,)))
                battery.battery_characteristic.fire(b"")
                battery.battery_characteristic.fire(bytes((1, 2)))
                await self._wait(lambda: levels == [59, 42])
                # Main loop callback already queued when stop revokes delivery.
                battery.battery_characteristic.fire(bytes((41,)))
                monitor.stop()
                await self._wait(lambda: not monitor._thread.is_alive())
                self.assertEqual(levels, [59, 42])
                self.assertTrue(battery.device.closed)
                self.assertTrue(battery.battery_service.closed)
                self.assertFalse(env.device.closed)
                self.assertFalse(env.service.closed)
                self.assertIn(0, battery.battery_characteristic.cccd_history)
                self.assertEqual(battery.battery_characteristic._handlers, {})
                self.assertTrue(all(r.close_calls == 1 for r in env.data_readers))
            finally:
                await session.close()
                await self._wait(lambda: not monitor._thread.is_alive())
            self.assertTrue(env.device.closed)

        _run(scenario())

    def test_native_block_at_each_stage_cannot_hold_core_commands_close_or_reconnect(self):
        # Blocking Event.wait deliberately stalls the worker's event loop:
        # unlike asyncio.Event it cannot respond to task cancellation.
        for stage in ("open", "service", "characteristics", "subscribe", "read", "unsubscribe"):
            with self.subTest(stage=stage):
                env = FakeWinRTEnvironment(device_id="battery-" + stage)
                levels = []
                entered = threading.Event()
                release = threading.Event()

                async def scenario():
                    session, candidate, battery = self._session_and_candidate(env, levels)
                    await session.connect(candidate)
                    if stage == "open":
                        target = session._winrt.bluetooth_le_device
                        name = "from_id_async"
                    elif stage == "service":
                        target = battery.device
                        name = "get_gatt_services_for_uuid_with_cache_mode_async"
                    elif stage == "characteristics":
                        target = battery.battery_service
                        name = "get_characteristics_with_cache_mode_async"
                    elif stage == "read":
                        target = battery.battery_characteristic
                        name = "read_value_with_cache_mode_async"
                    else:
                        target = battery.battery_characteristic
                        name = "write_client_characteristic_configuration_descriptor_async"
                    original = getattr(target, name)

                    async def blocked(*args):
                        should_block = threading.current_thread().name == "rc003-battery"
                        if stage == "unsubscribe":
                            should_block = should_block and args[0] == 0
                        if should_block:
                            entered.set()
                            if not release.wait(3.0):
                                raise AssertionError("native test gate not released")
                        return await original(*args)

                    setattr(target, name, blocked)
                    monitor = None
                    replacement = None
                    try:
                        session.start_battery_monitor()
                        monitor = session._battery_monitor
                        if stage == "unsubscribe":
                            await self._wait(lambda: levels == [59])
                            monitor.stop()
                        await self._wait(entered.is_set)
                        # ATVV command remains executable while optional GATT is stuck.
                        await asyncio.wait_for(session._write_tx(bytes((0x0C,))), 0.5)
                        await asyncio.wait_for(session.close(), 0.5)
                        self.assertTrue(env.device.closed)
                        self.assertTrue(monitor._thread.is_alive())
                        next_env = FakeWinRTEnvironment(device_id=env.device_id)
                        replacement, next_candidate, _ = self._session_and_candidate(next_env, levels)
                        await asyncio.wait_for(replacement.connect(next_candidate), 0.5)
                        replacement.start_battery_monitor()
                        # Registry keeps the old resource owner; no thread buildup.
                        self.assertIsNone(replacement._battery_monitor)
                        self.assertFalse(next_env.device.closed)
                    finally:
                        release.set()
                        await session.close()
                        if replacement is not None:
                            await replacement.close()
                        if monitor is not None:
                            await self._wait(lambda: not monitor._thread.is_alive())
                    self.assertEqual(levels, [59] if stage == "unsubscribe" else [])
                    self.assertTrue(battery.device.closed)

                _run(scenario())

    def test_setup_deadline_discards_late_value_and_releases_slot_after_cleanup(self):
        env = FakeWinRTEnvironment(device_id="battery-timeout")
        levels = []
        entered = threading.Event()
        release = threading.Event()

        async def scenario():
            session, candidate, battery = self._session_and_candidate(env, levels)
            await session.connect(candidate)
            original = battery.battery_characteristic.read_value_with_cache_mode_async

            async def blocked(*args):
                entered.set()
                release.wait(3.0)
                return await original(*args)

            battery.battery_characteristic.read_value_with_cache_mode_async = blocked
            with mock.patch("ovb_rc003.ble_transport_winrt._BATTERY_SETUP_TIMEOUT_SECONDS", 0.05):
                session.start_battery_monitor()
            monitor = session._battery_monitor
            try:
                await self._wait(entered.is_set)
                await self._wait(monitor._stopped.is_set)
                self.assertTrue(monitor._thread.is_alive())
                self.assertEqual(levels, [])
                self.assertFalse(env.device.closed)
            finally:
                release.set()
                await self._wait(lambda: not monitor._thread.is_alive())
                await session.close()
            self.assertEqual(levels, [])
            self.assertTrue(battery.device.closed)
            # Once cleanup ends, a later connection can acquire telemetry again.
            next_env = FakeWinRTEnvironment(device_id=env.device_id)
            next_session, next_candidate, _ = self._session_and_candidate(next_env, levels)
            await next_session.connect(next_candidate)
            next_session.start_battery_monitor()
            next_monitor = next_session._battery_monitor
            try:
                await self._wait(lambda: levels == [59])
            finally:
                await next_session.close()
                await self._wait(lambda: not next_monitor._thread.is_alive())

        _run(scenario())

    def test_missing_service_and_invalid_read_are_fail_soft(self):
        for payload in (None, b"", bytes((101,)), bytes((1, 2))):
            with self.subTest(payload=payload):
                env = FakeWinRTEnvironment()
                levels = []

                async def scenario():
                    session, candidate, battery = self._session_and_candidate(env, levels)
                    if payload is None:
                        battery.device._services.pop(uuid.UUID(BATTERY_SERVICE_UUID))
                    else:
                        battery.battery_characteristic.read_value = payload
                    await session.connect(candidate)
                    session.start_battery_monitor()
                    monitor = session._battery_monitor
                    try:
                        await self._wait(lambda: not monitor._thread.is_alive())
                        self.assertEqual(levels, [])
                        self.assertFalse(env.device.closed)
                        self.assertFalse(session.session.mic_open)
                        self.assertTrue(battery.device.closed)
                    finally:
                        await session.close()

                _run(scenario())

    def test_permanently_stuck_native_call_does_not_prevent_process_exit(self):
        import subprocess
        import sys
        import textwrap

        result = subprocess.run(
            [sys.executable, "-c", textwrap.dedent('''
                import asyncio
                import threading
                from types import SimpleNamespace
                from ovb_rc003.battery_monitor_winrt import BatteryMonitor

                entered = threading.Event()
                async def open_device(_device_id):
                    entered.set()
                    threading.Event().wait()  # deliberately never released

                async def scenario():
                    modules = SimpleNamespace(bluetooth_le_device=SimpleNamespace(
                        from_id_async=open_device))
                    monitor = BatteryMonitor("exit-test", asyncio.get_running_loop(),
                                             lambda level: None, lambda: modules)
                    assert monitor.start()
                    while not entered.is_set():
                        await asyncio.sleep(0.005)
                    monitor.stop()
                    assert monitor._thread.is_alive()

                asyncio.run(scenario())
                print("main loop exited")
            ''')],
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("main loop exited", result.stdout)


class NotificationProcessingTests(unittest.TestCase):
    def test_concurrent_producers_insert_in_their_assigned_sequence_order(self):
        class BlockingAudioQueue(queue.Queue):
            def __init__(self):
                super().__init__()
                self.put_entered = threading.Event()
                self.release_put = threading.Event()

            def put_nowait(self, item):
                self.put_entered.set()
                if not self.release_put.wait(timeout=2.0):
                    raise AssertionError("test did not release blocked audio enqueue")
                return super().put_nowait(item)

        loop = asyncio.new_event_loop()
        session = RC003BleSession(
            on_pcm_frame=lambda samples: None,
            loop=loop,
        )
        audio_queue = BlockingAudioQueue()
        session._audio_event_queue = audio_queue
        control_inserted = threading.Event()
        original_control_put = session._control_event_queue.put_nowait

        def record_control_put(item):
            original_control_put(item)
            control_inserted.set()

        session._control_event_queue.put_nowait = record_control_put
        audio_thread = threading.Thread(
            target=session._enqueue,
            args=("audio", b"audio"),
        )
        stop_thread = threading.Thread(
            target=session._enqueue,
            args=("control", bytes((proto.OPCODE_AUDIO_STOP, 0x02))),
        )
        try:
            audio_thread.start()
            self.assertTrue(audio_queue.put_entered.wait(timeout=1.0))
            stop_thread.start()

            self.assertFalse(
                control_inserted.wait(timeout=0.1),
                "later control insertion overtook the in-progress audio insertion",
            )

            audio_queue.release_put.set()
            audio_thread.join(timeout=1.0)
            stop_thread.join(timeout=1.0)
            self.assertFalse(audio_thread.is_alive())
            self.assertFalse(stop_thread.is_alive())
            self.assertTrue(control_inserted.is_set())
            self.assertEqual(audio_queue.get_nowait()[1], 0)
            self.assertEqual(session._control_event_queue.get_nowait()[1], 1)
        finally:
            audio_queue.release_put.set()
            audio_thread.join(timeout=1.0)
            stop_thread.join(timeout=1.0)
            loop.close()

    def test_audio_backpressure_never_drops_or_delays_control_behind_audio(self):
        processed = []
        loop = asyncio.new_event_loop()
        session = RC003BleSession(
            on_pcm_frame=lambda samples: None,
            loop=loop,
        )
        session._process_audio = lambda _payload, _sequence=None: processed.append("audio")
        session._process_control = lambda _payload: processed.append("control")
        try:
            for _ in range(200):
                session._enqueue("audio", b"audio")
            session._enqueue("control", b"control")
            for _ in range(200):
                session._enqueue("audio", b"audio")

            session._start_worker(session._generation)
            self.assertTrue(_wait_until(lambda: processed))
            self.assertEqual(processed[0], "control")
            self.assertGreater(session.dropped_event_count, 0)
        finally:
            session._worker_stop.set()
            if session._worker_thread is not None:
                session._worker_thread.join(timeout=2.0)
            loop.close()

    def test_audio_stop_processes_earlier_audio_before_closing_session(self):
        pcm_batches = []
        control_events = []
        loop = asyncio.new_event_loop()
        session = RC003BleSession(
            on_pcm_frame=pcm_batches.append,
            on_control_event=control_events.append,
            loop=loop,
        )
        try:
            session._session.handle_control(_caps_payload(frame_size=2))
            session._enqueue(
                "control",
                bytes((proto.OPCODE_AUDIO_START, 0x03, 0x02, 0x01)),
            )
            for _ in range(10):
                session._enqueue("audio", bytes((0x11, 0x11)))
            session._enqueue("control", bytes((proto.OPCODE_AUDIO_STOP, 0x02)))

            session._start_worker(session._generation)

            self.assertTrue(
                _wait_until(
                    lambda: any(
                        isinstance(event, atvv_session.AudioStopped)
                        for event in control_events
                    )
                )
            )
            self.assertEqual(len(pcm_batches), 10)
            self.assertIsInstance(control_events[0], atvv_session.AudioStarted)
            self.assertIsInstance(control_events[-1], atvv_session.AudioStopped)
        finally:
            session._worker_stop.set()
            session._event_queue_wakeup.set()
            if session._worker_thread is not None:
                session._worker_thread.join(timeout=2.0)
            loop.close()

    def test_audio_stop_does_not_drain_a_later_sessions_audio(self):
        processed = []
        loop = asyncio.new_event_loop()
        session = RC003BleSession(
            on_pcm_frame=lambda samples: None,
            loop=loop,
        )
        session._process_control = lambda payload: processed.append(
            ("control", payload)
        )
        session._process_audio = lambda payload, _sequence=None: processed.append(("audio", payload))
        first_start = bytes((proto.OPCODE_AUDIO_START, 0x03, 0x02, 0x01))
        first_audio = b"first-audio"
        first_stop = bytes((proto.OPCODE_AUDIO_STOP, 0x02))
        second_start = bytes((proto.OPCODE_AUDIO_START, 0x03, 0x02, 0x02))
        second_audio = b"second-audio"
        try:
            session._enqueue("control", first_start)
            session._enqueue("audio", first_audio)
            session._enqueue("control", first_stop)
            session._enqueue("control", second_start)
            session._enqueue("audio", second_audio)

            session._start_worker(session._generation)

            self.assertTrue(_wait_until(lambda: len(processed) == 5))
            self.assertEqual(
                processed,
                [
                    ("control", first_start),
                    ("audio", first_audio),
                    ("control", first_stop),
                    ("control", second_start),
                    ("audio", second_audio),
                ],
            )
        finally:
            session._worker_stop.set()
            session._event_queue_wakeup.set()
            if session._worker_thread is not None:
                session._worker_thread.join(timeout=2.0)
            loop.close()

    def test_caps_then_audio_start_then_audio_notification_yields_pcm(self):
        env = FakeWinRTEnvironment()
        pcm_batches = []
        control_events = []

        async def scenario():
            winrt = env.build_winrt_modules()
            session = RC003BleSession(
                on_pcm_frame=pcm_batches.append,
                on_control_event=control_events.append,
                winrt=winrt,
                loop=asyncio.get_event_loop(),
            )
            candidate = identity.RC003Candidate(
                name=env.name, hardware_match=False, handle=env.discovered_info
            )
            await session.connect(candidate)
            return session

        session = _run(scenario())
        try:
            env.control_characteristic.fire(_caps_payload(frame_size=2))
            env.control_characteristic.fire(bytes((proto.OPCODE_AUDIO_START, 0, 0, 1)))
            env.audio_characteristic.fire(bytes((0x00, 0x00)))  # 2 zero bytes -> 4 samples

            self.assertTrue(_wait_until(lambda: len(pcm_batches) > 0))
            self.assertTrue(
                _wait_until(
                    lambda: any(
                        isinstance(e, atvv_session.AudioStarted) for e in control_events
                    )
                )
            )
        finally:
            _run(session.close())

    def test_pcm_callback_receives_notification_arrival_sequence(self):
        env = FakeWinRTEnvironment()
        sequenced = []

        async def scenario():
            session = RC003BleSession(
                on_pcm_frame=lambda _samples: None,
                on_pcm_frame_with_sequence=(
                    lambda samples, sequence: sequenced.append((samples, sequence))
                ),
                winrt=env.build_winrt_modules(),
                loop=asyncio.get_event_loop(),
            )
            candidate = identity.RC003Candidate(
                name=env.name, hardware_match=False, handle=env.discovered_info
            )
            await session.connect(candidate)
            return session

        session = _run(scenario())
        try:
            env.control_characteristic.fire(_caps_payload(frame_size=2))
            env.control_characteristic.fire(bytes((proto.OPCODE_AUDIO_START, 0, 0, 1)))
            env.audio_characteristic.fire(bytes((0x00, 0x00)))

            self.assertTrue(_wait_until(lambda: bool(sequenced)))
            self.assertEqual(sequenced[0][1], session.audio_arrival_watermark)
        finally:
            _run(session.close())

    def test_fragmented_pcm_callback_keeps_oldest_arrival_sequence(self):
        env = FakeWinRTEnvironment()
        sequenced = []

        async def scenario():
            session = RC003BleSession(
                on_pcm_frame=lambda _samples: None,
                on_pcm_frame_with_sequence=(
                    lambda samples, sequence: sequenced.append((samples, sequence))
                ),
                winrt=env.build_winrt_modules(),
                loop=asyncio.get_event_loop(),
            )
            candidate = identity.RC003Candidate(
                name=env.name,
                hardware_match=False,
                handle=env.discovered_info,
            )
            await session.connect(candidate)
            return session

        session = _run(scenario())
        try:
            session.session.handle_control(_caps_payload(frame_size=2))
            session.session.handle_control(
                bytes((proto.OPCODE_AUDIO_START, 0, 0, 1))
            )
            session._process_audio(bytes((0x00,)), sequence=5)
            self.assertEqual(sequenced, [])
            session._process_audio(bytes((0x00,)), sequence=6)
            self.assertEqual(len(sequenced), 1)
            self.assertEqual(sequenced[0][1], 5)
        finally:
            _run(session.close())

    def test_malformed_control_payload_calls_on_error_not_raise(self):
        env = FakeWinRTEnvironment()
        errors = []

        async def scenario():
            winrt = env.build_winrt_modules()
            session = RC003BleSession(
                on_pcm_frame=lambda samples: None,
                on_error=errors.append,
                winrt=winrt,
                loop=asyncio.get_event_loop(),
            )
            candidate = identity.RC003Candidate(
                name=env.name, hardware_match=False, handle=env.discovered_info
            )
            await session.connect(candidate)
            return session

        session = _run(scenario())
        try:
            # Empty control payload: handle_control() raises ATVVProtocolError.
            env.control_characteristic.fire(b"")
            self.assertTrue(_wait_until(lambda: len(errors) > 0))
            self.assertIsInstance(errors[0], atvv_session.ATVVProtocolError)
        finally:
            _run(session.close())

    def test_control_callback_failure_reports_error_and_stops_worker(self):
        env = FakeWinRTEnvironment()
        errors = []
        boom = RuntimeError("simulated control callback failure")

        async def scenario():
            session = RC003BleSession(
                on_pcm_frame=lambda samples: None,
                on_control_event=lambda _event: (_ for _ in ()).throw(boom),
                on_error=errors.append,
                winrt=env.build_winrt_modules(),
                loop=asyncio.get_event_loop(),
            )
            candidate = identity.RC003Candidate(
                name=env.name, hardware_match=False, handle=env.discovered_info
            )
            await session.connect(candidate)
            env.control_characteristic.fire(_caps_payload(frame_size=2))
            self.assertTrue(_wait_until(lambda: errors))
            self.assertIs(errors[0], boom)
            self.assertTrue(session._worker_stop.is_set())
            await session.close()

        _run(scenario())

    def test_pcm_callback_failure_reports_error_and_stops_worker(self):
        env = FakeWinRTEnvironment()
        errors = []
        boom = RuntimeError("simulated PCM callback failure")

        async def scenario():
            session = RC003BleSession(
                on_pcm_frame=lambda _samples: (_ for _ in ()).throw(boom),
                on_error=errors.append,
                winrt=env.build_winrt_modules(),
                loop=asyncio.get_event_loop(),
            )
            candidate = identity.RC003Candidate(
                name=env.name, hardware_match=False, handle=env.discovered_info
            )
            await session.connect(candidate)
            env.control_characteristic.fire(_caps_payload(frame_size=2))
            env.control_characteristic.fire(
                bytes((proto.OPCODE_AUDIO_START, 0, 0, 1))
            )
            env.audio_characteristic.fire(bytes((0x00, 0x00)))
            self.assertTrue(_wait_until(lambda: errors))
            self.assertIs(errors[0], boom)
            self.assertTrue(session._worker_stop.is_set())
            await session.close()

        _run(scenario())

    def test_disconnect_callback_fires_on_status_change(self):
        env = FakeWinRTEnvironment()
        disconnected = []

        async def scenario():
            winrt = env.build_winrt_modules()
            session = RC003BleSession(
                on_pcm_frame=lambda samples: None,
                on_disconnected=lambda: disconnected.append(True),
                winrt=winrt,
                loop=asyncio.get_event_loop(),
            )
            candidate = identity.RC003Candidate(
                name=env.name, hardware_match=False, handle=env.discovered_info
            )
            await session.connect(candidate)
            return session

        session = _run(scenario())
        try:
            env.device.simulate_disconnect()
            self.assertEqual(disconnected, [True])
        finally:
            _run(session.close())


class SendMicOpenThreadsafeTests(unittest.TestCase):
    """XRBM-018 DoD 4: send_mic_open_threadsafe()'s scheduled write has a
    generation/closing gate, and any write failure that does make it
    through is observed and reported via on_error - not silently dropped as
    an un-awaited fire-and-forget task would be.
    """

    def _connected_session(self, env, **kwargs):
        winrt = env.build_winrt_modules()
        session = RC003BleSession(winrt=winrt, loop=asyncio.get_event_loop(), **kwargs)
        candidate = identity.RC003Candidate(
            name=env.name, hardware_match=False, handle=env.discovered_info
        )
        return session, candidate

    def test_generation_gate_drops_a_write_scheduled_before_a_reconnect(self):
        env = FakeWinRTEnvironment()

        async def scenario():
            session, candidate = self._connected_session(env, on_pcm_frame=lambda samples: None)
            await session.connect(candidate)
            writes_before = len(env.tx_characteristic.write_history)

            session.send_mic_open_threadsafe()  # scheduled with the current generation
            session._generation += 1  # simulate a reconnect bumping generation first
            await asyncio.sleep(0.05)  # give the loop a chance to run the (gated) callback

            self.assertEqual(len(env.tx_characteristic.write_history), writes_before)
            await session.close()

        _run(scenario())

    def test_closing_gate_drops_a_write_scheduled_after_close_started(self):
        env = FakeWinRTEnvironment()

        async def scenario():
            session, candidate = self._connected_session(env, on_pcm_frame=lambda samples: None)
            await session.connect(candidate)
            writes_before = len(env.tx_characteristic.write_history)

            session.send_mic_open_threadsafe()
            session._closing = True  # simulate close() having already started
            await asyncio.sleep(0.05)

            self.assertEqual(len(env.tx_characteristic.write_history), writes_before)
            session._closing = False  # let the real close() below run normally

        _run(scenario())

    def test_a_normal_scheduled_write_lands(self):
        env = FakeWinRTEnvironment()

        async def scenario():
            session, candidate = self._connected_session(env, on_pcm_frame=lambda samples: None)
            await session.connect(candidate)
            writes_before = len(env.tx_characteristic.write_history)

            session.send_mic_open_threadsafe()
            self.assertTrue(
                await _async_wait_until(lambda: len(env.tx_characteristic.write_history) > writes_before)
            )
            self.assertEqual(
                env.tx_characteristic.write_history[-1], session.session.mic_open_command()
            )

            await session.close()

        _run(scenario())

    def test_a_failed_scheduled_write_is_reported_via_on_error(self):
        env = FakeWinRTEnvironment()
        errors = []
        boom = ConnectionError("simulated GATT write failure")

        async def scenario():
            session, candidate = self._connected_session(
                env, on_pcm_frame=lambda samples: None, on_error=errors.append
            )
            await session.connect(candidate)

            def _raise(_payload):
                raise boom

            env.tx_characteristic._on_write = _raise

            session.send_mic_open_threadsafe()
            self.assertTrue(await _async_wait_until(lambda: len(errors) > 0))
            self.assertIs(errors[0], boom)

            env.tx_characteristic._on_write = None
            await session.close()

        _run(scenario())


class SendMicCloseThreadsafeTests(unittest.TestCase):
    def _connected_session(self, env, **kwargs):
        winrt = env.build_winrt_modules()
        session = RC003BleSession(winrt=winrt, loop=asyncio.get_event_loop(), **kwargs)
        candidate = identity.RC003Candidate(
            name=env.name, hardware_match=False, handle=env.discovered_info
        )
        return session, candidate

    def test_a_normal_scheduled_close_write_lands(self):
        env = FakeWinRTEnvironment()

        async def scenario():
            session, candidate = self._connected_session(
                env, on_pcm_frame=lambda samples: None
            )
            await session.connect(candidate)
            writes_before = len(env.tx_characteristic.write_history)

            session.send_mic_close_threadsafe()
            self.assertTrue(
                await _async_wait_until(
                    lambda: len(env.tx_characteristic.write_history) > writes_before
                )
            )
            self.assertEqual(
                env.tx_characteristic.write_history[-1],
                session.session.mic_close_command(),
            )
            await session.close()

        _run(scenario())

    def test_a_failed_scheduled_close_write_is_reported(self):
        env = FakeWinRTEnvironment()
        errors = []
        boom = ConnectionError("simulated MIC_CLOSE write failure")

        async def scenario():
            session, candidate = self._connected_session(
                env, on_pcm_frame=lambda samples: None, on_error=errors.append
            )
            await session.connect(candidate)
            env.tx_characteristic._on_write = lambda _payload: (_ for _ in ()).throw(boom)

            session.send_mic_close_threadsafe()
            self.assertTrue(await _async_wait_until(lambda: len(errors) > 0))
            self.assertIs(errors[0], boom)

            env.tx_characteristic._on_write = None
            await session.close()

        _run(scenario())

    def test_generation_gate_drops_a_stale_close_write(self):
        env = FakeWinRTEnvironment()

        async def scenario():
            session, candidate = self._connected_session(
                env, on_pcm_frame=lambda samples: None
            )
            await session.connect(candidate)
            writes_before = len(env.tx_characteristic.write_history)

            session.send_mic_close_threadsafe()
            session._generation += 1
            await asyncio.sleep(0.05)

            self.assertEqual(len(env.tx_characteristic.write_history), writes_before)
            await session.close()

        _run(scenario())


class CloseCancelsInFlightMicOpenTests(unittest.TestCase):
    """XRBM-018 RETRY 1 P1 #3: close() must cancel/await an ALREADY IN-
    FLIGHT MIC_OPEN write, not merely refuse to schedule a new one. The
    earlier gate-before-scheduling fix (SendMicOpenThreadsafeTests above)
    does nothing once a write has actually started - this proves close()
    now also wins that race, using a controllable blocking write (see
    FakeGattCharacteristic._write_gate) instead of a timing-based sleep.
    """

    def _connected_session(self, env, **kwargs):
        winrt = env.build_winrt_modules()
        session = RC003BleSession(winrt=winrt, loop=asyncio.get_event_loop(), **kwargs)
        candidate = identity.RC003Candidate(
            name=env.name, hardware_match=False, handle=env.discovered_info
        )
        return session, candidate

    def test_close_cancels_and_drains_a_write_already_in_flight(self):
        env = FakeWinRTEnvironment()
        errors = []

        async def scenario():
            session, candidate = self._connected_session(
                env, on_pcm_frame=lambda samples: None, on_error=errors.append
            )
            await session.connect(candidate)
            writes_before = len(env.tx_characteristic.write_history)

            gate = asyncio.Event()  # never set - the write blocks until cancelled
            env.tx_characteristic._write_gate = gate

            session.send_mic_open_threadsafe()
            # Deterministically wait until the write coroutine has actually
            # reached (and is blocked at) the gate - not a fixed sleep.
            self.assertTrue(
                await _async_wait_until(lambda: env.tx_characteristic.write_started_count > 0)
            )
            self.assertEqual(len(session._mic_open_tasks), 1)

            # close() runs while the write is still genuinely in flight.
            await session.close()

            # The write never completed (cancelled before recording it),
            # and close() still fully tore down the GATT resources below -
            # neither raced nor got stuck waiting on it.
            self.assertEqual(len(env.tx_characteristic.write_history), writes_before)
            self.assertEqual(len(session._mic_open_tasks), 0)

        _run(scenario())

        # A routine cancellation during an intentional close() is not a
        # reportable failure - on_error must stay silent for it (it's not
        # actionable: there is nothing left to reconnect).
        self.assertEqual(errors, [])
        self.assertTrue(env.service.closed)
        self.assertTrue(env.device.closed)
        self.assertEqual(env.audio_characteristic.cccd_history[-1], 0)  # NONE == 0
        self.assertEqual(env.control_characteristic.cccd_history[-1], 0)


class CloseWorkerJoinTimeoutTests(unittest.TestCase):
    """XRBM-019 P1 #3: a worker-thread join timeout inside close() must be a
    real close() failure (raised), not merely reported via on_error while
    close() itself returns normally - the previous "report and keep going"
    behavior let app.py's cleanup silently drop the session owner over a
    still-live worker thread anyway (XRBM-018's independent review
    round 2 finding #2). Every other independent GATT cleanup step must
    still be attempted before that raise.
    """

    class _NeverStopsThread:
        """A minimal thread-like stand-in whose join() always returns
        immediately without the thread actually having stopped -
        deterministically exercises the "still alive after join" branch
        without waiting out a real timeout.
        """

        def join(self, timeout=None):
            return None

        def is_alive(self):
            return True

    def test_a_worker_that_never_stops_raises_after_completing_other_cleanup(self):
        env = FakeWinRTEnvironment()
        errors = []

        async def scenario():
            winrt = env.build_winrt_modules()
            session = RC003BleSession(
                on_pcm_frame=lambda samples: None,
                on_error=errors.append,
                winrt=winrt,
                loop=asyncio.get_event_loop(),
            )
            candidate = identity.RC003Candidate(
                name=env.name, hardware_match=False, handle=env.discovered_info
            )
            await session.connect(candidate)

            stuck_thread = self._NeverStopsThread()
            session._worker_thread = stuck_thread

            with self.assertRaises(RuntimeError) as ctx:
                await session.close()
            self.assertIn("did not stop", str(ctx.exception))

            # Not hidden: the still-alive thread reference is left in place
            # rather than cleared to pretend it stopped.
            self.assertIs(session._worker_thread, stuck_thread)
            # This specific failure is now surfaced via the raise itself,
            # not (redundantly/confusingly) via on_error too.
            self.assertEqual(errors, [])

        _run(scenario())

        # Every other independent GATT cleanup step still ran despite the
        # worker not stopping: CCCD written back to NONE, tokens removed,
        # service/device closed.
        self.assertEqual(env.audio_characteristic.cccd_history[-1], 0)  # NONE == 0
        self.assertEqual(env.control_characteristic.cccd_history[-1], 0)
        self.assertTrue(env.service.closed)
        self.assertTrue(env.device.closed)
        self.assertEqual(len(env.audio_characteristic._handlers), 0)
        self.assertEqual(len(env.control_characteristic._handlers), 0)


class CloseTests(unittest.TestCase):
    def test_close_writes_cccd_none_and_closes_service_and_device(self):
        env = FakeWinRTEnvironment()

        async def scenario():
            winrt = env.build_winrt_modules()
            session = RC003BleSession(
                on_pcm_frame=lambda samples: None,
                winrt=winrt,
                loop=asyncio.get_event_loop(),
            )
            candidate = identity.RC003Candidate(
                name=env.name, hardware_match=False, handle=env.discovered_info
            )
            await session.connect(candidate)
            await session.close()

        _run(scenario())

        self.assertEqual(env.audio_characteristic.cccd_history[-1], 0)  # NONE == 0
        self.assertEqual(env.control_characteristic.cccd_history[-1], 0)
        self.assertTrue(env.service.closed)
        self.assertTrue(env.device.closed)
        # Both characteristics' tokens must have been removed (fakes assert
        # the exact token on remove_value_changed - if this didn't raise,
        # the tokens matched).
        self.assertEqual(len(env.audio_characteristic._handlers), 0)
        self.assertEqual(len(env.control_characteristic._handlers), 0)
        self.assertEqual(len(env.device._connection_status_handlers), 0)

    def test_service_close_failure_retains_owner_for_retry(self):
        env = FakeWinRTEnvironment()

        async def scenario():
            session = RC003BleSession(
                on_pcm_frame=lambda samples: None,
                winrt=env.build_winrt_modules(),
                loop=asyncio.get_event_loop(),
            )
            candidate = identity.RC003Candidate(
                name=env.name, hardware_match=False, handle=env.discovered_info
            )
            await session.connect(candidate)
            original_close = env.service.close
            env.service.close = lambda: (_ for _ in ()).throw(
                RuntimeError("simulated service close failure")
            )
            with self.assertRaises(RuntimeError):
                await session.close()
            self.assertIs(session._service, env.service)
            self.assertIs(session._audio_characteristic, env.audio_characteristic)

            env.service.close = original_close
            await session.close()
            self.assertIsNone(session._service)

        _run(scenario())

    def test_device_close_failure_retains_owner_for_retry(self):
        env = FakeWinRTEnvironment()

        async def scenario():
            session = RC003BleSession(
                on_pcm_frame=lambda samples: None,
                winrt=env.build_winrt_modules(),
                loop=asyncio.get_event_loop(),
            )
            candidate = identity.RC003Candidate(
                name=env.name, hardware_match=False, handle=env.discovered_info
            )
            await session.connect(candidate)
            original_close = env.device.close
            env.device.close = lambda: (_ for _ in ()).throw(
                RuntimeError("simulated device close failure")
            )
            with self.assertRaises(RuntimeError):
                await session.close()
            self.assertIs(session._device, env.device)

            env.device.close = original_close
            await session.close()
            self.assertIsNone(session._device)

        _run(scenario())

    def test_close_sends_mic_close_if_a_mic_session_was_open(self):
        env = FakeWinRTEnvironment()

        async def scenario():
            winrt = env.build_winrt_modules()
            session = RC003BleSession(
                on_pcm_frame=lambda samples: None,
                winrt=winrt,
                loop=asyncio.get_event_loop(),
            )
            candidate = identity.RC003Candidate(
                name=env.name, hardware_match=False, handle=env.discovered_info
            )
            await session.connect(candidate)
            env.control_characteristic.fire(_caps_payload())
            env.control_characteristic.fire(bytes((proto.OPCODE_AUDIO_START, 0, 0, 5)))
            self.assertTrue(_wait_until(lambda: session.session.mic_open))
            await session.close()

        _run(scenario())
        self.assertEqual(env.tx_characteristic.write_history[-1][0], 0x0D)  # MIC_CLOSE opcode

    def test_a_mic_close_write_failure_still_lets_the_rest_of_cleanup_run(self):
        """XRBM-018 RETRY 1 item 6: the MIC_CLOSE write's except clause must
        catch more than just ConnectionError - any exception the write
        raises must never abort the unsubscribe/service/device cleanup
        after it.
        """

        env = FakeWinRTEnvironment()

        async def scenario():
            winrt = env.build_winrt_modules()
            session = RC003BleSession(
                on_pcm_frame=lambda samples: None,
                winrt=winrt,
                loop=asyncio.get_event_loop(),
            )
            candidate = identity.RC003Candidate(
                name=env.name, hardware_match=False, handle=env.discovered_info
            )
            await session.connect(candidate)
            env.control_characteristic.fire(_caps_payload())
            env.control_characteristic.fire(bytes((proto.OPCODE_AUDIO_START, 0, 0, 5)))
            self.assertTrue(_wait_until(lambda: session.session.mic_open))

            def _raise(_payload):
                raise RuntimeError("simulated non-ConnectionError MIC_CLOSE failure")

            env.tx_characteristic._on_write = _raise
            await session.close()  # must not raise

        _run(scenario())

        self.assertTrue(env.service.closed)
        self.assertTrue(env.device.closed)
        self.assertEqual(env.audio_characteristic.cccd_history[-1], 0)  # NONE == 0
        self.assertEqual(env.control_characteristic.cccd_history[-1], 0)
        self.assertEqual(len(env.audio_characteristic._handlers), 0)
        self.assertEqual(len(env.control_characteristic._handlers), 0)


if __name__ == "__main__":
    unittest.main()
