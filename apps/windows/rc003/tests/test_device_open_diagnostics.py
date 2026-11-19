"""Device-open evidence keeps the real operation and never owns a connection."""
import asyncio
from collections import deque
import json
import logging
from pathlib import Path, PureWindowsPath
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import uuid
import zipfile

from ovb_rc003 import chromecast_device_windows as devices
from ovb_rc003 import chromecast_hid_worker as worker
from ovb_rc003 import log_export, logging_setup
from ovb_rc003 import receiver_startup_errors as errors
from ovb_rc003.chromecast_channel import Channel, SessionIdentity
from ovb_rc003.chromecast_client import Client
from ovb_rc003.chromecast_observation import valid_record

ENTITY = 'a' * 64


def discovery():
    callbacks, calls = {}, []
    watcher = SimpleNamespace(stop=mock.Mock())
    for name in ('added', 'removed', 'stopped', 'enumeration_completed'):
        def add(callback, name=name):
            calls.append('add_' + name)
            callbacks[name] = callback
            return len(callbacks)
        setattr(watcher, 'add_' + name, mock.Mock(side_effect=add))
        setattr(watcher, 'remove_' + name, mock.Mock())

    def start():
        calls.append('watcher_start')
        callbacks['added'](None, SimpleNamespace(id='radio-id'))
        callbacks['enumeration_completed'](None, None)

    watcher.start = mock.Mock(side_effect=start)
    opened = SimpleNamespace(device_id='BluetoothLE#BluetoothLEAA:BB:CC:DD:EE:FF-AA:BB:CC:DD:EE:FF', close=mock.Mock())
    info = SimpleNamespace(id=opened.device_id, name='Chromecast Remote')
    adapter = SimpleNamespace(get_device_selector=mock.Mock(return_value='radio-selector'),
        from_id_async=mock.AsyncMock(return_value=SimpleNamespace(device_id='radio-id', bluetooth_address=1)))
    ble = SimpleNamespace(get_device_selector_from_pairing_state=mock.Mock(return_value='paired-selector'),
        from_id_async=mock.AsyncMock(return_value=opened))
    enum = SimpleNamespace(create_watcher_aqs_filter=mock.Mock(return_value=watcher),
        find_all_async_aqs_filter_and_additional_properties=mock.AsyncMock(return_value=[info]))
    modules = {'winrt.windows.devices.bluetooth': SimpleNamespace(BluetoothAdapter=adapter, BluetoothLEDevice=ble),
        'winrt.windows.devices.enumeration': SimpleNamespace(DeviceInformation=enum)}
    return SimpleNamespace(modules=modules, watcher=watcher, adapter=adapter, ble=ble,
        enum=enum, info=info, opened=opened, calls=calls, callbacks=callbacks)


def native_error():
    error = OSError('native operation rejected its parameter')
    error.winerror = -2147024809
    return error


class DeviceOpenEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def open_case(self, d, observer):
        target = devices.SelectedDevice(ENTITY)
        target.open_observer = observer
        with mock.patch.dict(sys.modules, d.modules), mock.patch.object(
                devices.remote_selection, 'candidate_key', return_value=ENTITY):
            await target.open(require_input=False)
        return target

    async def test_normal_open_and_close_preserve_calls_and_values(self):
        outcomes = []
        for enabled in (False, True):
            d, rows = discovery(), []
            target = await self.open_case(d, rows.append if enabled else None)
            self.assertIs(target.device, d.opened)
            d.ble.from_id_async.assert_awaited_once_with(d.info.id)
            d.enum.find_all_async_aqs_filter_and_additional_properties.assert_awaited_once_with(
                'paired-selector', ['System.Devices.Aep.ContainerId'])
            target.close()
            self.assertIsNone(target.device)
            self.assertIsNone(target.watcher)
            d.opened.close.assert_called_once()
            outcomes.append(d.calls)
            if enabled:
                self.assertTrue(all(valid_record(row) for row in rows))
                self.assertEqual(rows[-1]['step'], 'device_open_summary')
                self.assertEqual(rows[-1]['details']['open_outcome'], 'success')
                self.assertNotIn('AA:BB:CC:DD:EE:FF', json.dumps(rows))
        self.assertEqual(*outcomes)

    async def test_each_failing_native_call_keeps_its_own_name_and_exception(self):
        for call in ('adapter_selector', 'watcher_create', 'watcher_add_added', 'watcher_add_removed',
                     'watcher_add_stopped', 'watcher_add_enumeration_completed', 'watcher_start',
                     'paired_selector', 'paired_enumerate', 'ble_from_id'):
            with self.subTest(call=call):
                d, rows, error = discovery(), [], native_error()
                operation = {'adapter_selector': d.adapter.get_device_selector,
                    'watcher_create': d.enum.create_watcher_aqs_filter,
                    'watcher_start': d.watcher.start,
                    'paired_selector': d.ble.get_device_selector_from_pairing_state,
                    'paired_enumerate': d.enum.find_all_async_aqs_filter_and_additional_properties,
                    'ble_from_id': d.ble.from_id_async}.get(call)
                if call.startswith('watcher_add_'):
                    operation = getattr(d.watcher, call.removeprefix('watcher_'))
                operation.side_effect = error
                with self.assertRaises(OSError) as raised:
                    await self.open_case(d, rows.append)
                self.assertIs(raised.exception, error)
                precise = [r for r in rows if r['step'] == 'device_open_error' and r['details']['open_call'] == call]
                self.assertEqual(len(precise), 1)
                self.assertEqual(precise[0]['native_code'], 0x80070057)
                self.assertIn('rejected its parameter', precise[0]['details']['error_message'])
                self.assertTrue(all(valid_record(row) for row in rows))
                if call != 'ble_from_id':
                    d.ble.from_id_async.assert_not_awaited()

    async def test_cancel_and_timeout_keep_original_cleanup_ownership(self):
        for kind in ('cancel', 'timeout'):
            d, rows, entered = discovery(), [], asyncio.Event()
            async def pending(*_):
                entered.set()
                if kind == 'timeout':
                    raise TimeoutError('native timeout')
                await asyncio.Event().wait()
            d.ble.from_id_async.side_effect = pending
            target = devices.SelectedDevice(ENTITY)
            target.open_observer = rows.append
            with mock.patch.dict(sys.modules, d.modules), mock.patch.object(
                    devices.remote_selection, 'candidate_key', return_value=ENTITY):
                task = asyncio.create_task(target.open(require_input=False))
                await entered.wait()
                if kind == 'cancel':
                    task.cancel()
                with self.assertRaises(asyncio.CancelledError if kind == 'cancel' else TimeoutError):
                    await task
            self.assertIsNone(target.device)
            self.assertIs(target.watcher, d.watcher)
            target.close()
            d.watcher.stop.assert_called_once()
            outcome = 'cancelled' if kind == 'cancel' else 'timeout'
            self.assertTrue(any(r['details'].get('open_call') == 'ble_from_id'
                and r['details'].get('open_outcome') == outcome for r in rows))

    async def test_broken_observer_does_not_change_open_or_close(self):
        d = discovery()
        target = await self.open_case(d, mock.Mock(side_effect=RuntimeError('journal unavailable')))
        self.assertIs(target.device, d.opened)
        target.close()
        d.opened.close.assert_called_once()

    @unittest.skipUnless(sys.platform == 'win32', 'requires native WinRT GUID unboxing')
    async def test_native_identity_unboxing_keeps_source_location(self):
        from winrt.system import box_string
        d, rows = discovery(), []
        d.info.properties = {'System.Devices.Aep.ContainerId': box_string(str(uuid.UUID(int=1)))}
        target = devices.SelectedDevice(ENTITY)
        target.open_observer = rows.append
        with mock.patch.dict(sys.modules, d.modules):
            with self.assertRaises(OSError):
                await target.open(require_input=False)
        precise = next(r for r in rows if r['step'] == 'device_open_error'
            and r['details']['open_call'] == 'candidate_identity')
        self.assertEqual(precise['details']['error_origin'], 'remote_selection')
        self.assertGreater(precise['details']['source_line'], 0)
        self.assertEqual(precise['native_code'], 0x80004002)
        d.ble.from_id_async.assert_not_awaited()
        target.close()

    def test_bounded_rows_keep_late_failure_and_summary(self):
        rows = []
        journal = errors.DeviceOpenLog(rows.append)
        with self.assertRaises(OSError):
            with journal.operation('open', summary=True):
                for _ in range(500):
                    journal.call('candidate_identity', lambda: None)
                journal.call('ble_from_id', mock.Mock(side_effect=native_error()))
        self.assertLess(len(rows), 75)
        self.assertTrue(any(r['details'].get('rows_omitted', 0) > 0 for r in rows))
        self.assertTrue(any(r['step'] == 'device_open_error' and r['details']['open_call'] == 'ble_from_id' for r in rows))

    def test_skipped_identities_are_bounded_without_becoming_terminal_failures(self):
        rows = []
        journal = errors.DeviceOpenLog(rows.append)
        with journal.operation('open', summary=True):
            for _ in range(500):
                try:
                    journal.call('candidate_identity', mock.Mock(side_effect=devices.remote_selection.SelectionError('missing')),
                        expected=(devices.remote_selection.SelectionError,))
                except devices.remote_selection.SelectionError:
                    pass
        self.assertLess(len(rows), 70)
        self.assertEqual(rows[-1]['details']['open_outcome'], 'success')
        self.assertGreater(rows[-1]['details']['rows_omitted'], 0)

    def test_wrapped_native_cause_is_retained_without_changing_exception(self):
        rows, native = [], native_error()
        def select_endpoint():
            try:
                raise native
            except OSError as error:
                raise devices.PipeError('source_unconfirmed') from error
        with self.assertRaises(devices.PipeError) as raised:
            errors.DeviceOpenLog(rows.append).call('paired_endpoint_select', select_endpoint)
        self.assertIs(raised.exception.__cause__, native)
        cause = next(r for r in rows if r['step'] == 'device_open_error' and r['details']['chain_depth'] == 1)
        self.assertEqual(cause['native_code'], 0x80070057)
        self.assertIn('rejected its parameter', cause['details']['error_message'])

    def test_private_identifiers_removed_and_unicode_fits_actual_pipe(self):
        for text in (rf'failed {PureWindowsPath("C:/Users")}\Private Person\secret: invalid parameter',
                     'failed BluetoothLE#BluetoothLEAA:BB:CC:DD:EE:FF-AA:BB:CC:DD:EE:FF: invalid parameter',
                     '错误' * 500):
            rows = []
            error = OSError(text)
            error.winerror = 0x80070057
            with self.assertRaises(OSError):
                errors.DeviceOpenLog(rows.append).call('ble_from_id', mock.Mock(side_effect=error))
            identity = SessionIdentity.create(ENTITY)
            outgoing, incoming = Channel(identity, commands=False), Channel(identity, commands=False)
            for row in rows:
                self.assertTrue(valid_record(row))
                raw = outgoing.encode('evidence', record=row)
                self.assertLessEqual(len(raw), 2048)
                self.assertEqual(incoming.decode(raw)['record'], row)
                self.assertNotIn('Private Person', json.dumps(row))
                self.assertNotIn('AA:BB:CC:DD:EE:FF', json.dumps(row))

    def test_open_contract_rejects_unredacted_address_and_unknown_operation(self):
        from ovb_rc003.chromecast_hid_tap_windows import startup_record
        self.assertIsNone(startup_record('device_open_error', 'failed', details={
            'open_call': 'ble_from_id', 'error_message': 'failed AA:BB:CC:DD:EE:FF'}))
        self.assertIsNone(startup_record('device_open_call', 'failed', details={
            'open_call': 'private-device-name'}))


class NativeOpenFailureExportTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(sys.platform == 'win32', 'requires native Windows WinRT errors')
    async def test_two_real_native_errors_distinguished_in_exported_zip(self):
        from winrt.windows.devices.bluetooth import BluetoothLEDevice
        from winrt.windows.devices.enumeration import DeviceInformation
        native = {}
        for call, invoke in (
            ('paired_enumerate', lambda: DeviceInformation.find_all_async_aqs_filter_and_additional_properties(
                'RemoteMic_Invalid_AQS_Parameter', ['System.Devices.Aep.ContainerId'])),
            ('ble_from_id', lambda: BluetoothLEDevice.from_id_async('RemoteMic_Invalid_Device_ID')),
        ):
            try:
                await asyncio.wait_for(invoke(), 2)
            except OSError as error:
                self.assertEqual(error.winerror & 0xffffffff, 0x80070057)
                native[call] = error
        self.assertEqual(len(native), 2)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            handler = logging_setup.EvidenceFileHandler(root / 'logs/app.log', asynchronous=True)
            handler.addFilter(logging_setup.PrivacySafeExceptionFilter())
            handler.setFormatter(logging.Formatter(logging_setup.LOG_FORMAT))
            logger = logging.getLogger('ovb_rc003')
            old_handlers, old_level, old_propagate = logger.handlers[:], logger.level, logger.propagate
            logger.handlers, logger.level, logger.propagate = [handler], logging.INFO, False
            try:
                for call, error in native.items():
                    identity = SessionIdentity.create(ENTITY)
                    client = Client(ENTITY, mode='run', on_edge=lambda *_: None)
                    client.identity = identity
                    incoming, outgoing, decoded = Channel(identity, commands=True), Channel(identity, commands=False), []
                    inbox = deque([incoming.encode('start', mode='run', voice=True)])
                    d = discovery()
                    if call == 'paired_enumerate':
                        d.enum.find_all_async_aqs_filter_and_additional_properties.side_effect = error
                    else:
                        d.ble.from_id_async.side_effect = error
                    def write(raw):
                        event = outgoing.decode(raw)
                        decoded.append(event)
                        if event['type'] == 'evidence':
                            client._observe_evidence(event)
                    with mock.patch.dict(sys.modules, d.modules), mock.patch.object(
                            devices.remote_selection, 'candidate_key', return_value=ENTITY), mock.patch.object(
                            worker, '_selected', return_value=True), mock.patch.object(worker, 'inspect_peer'), mock.patch.object(
                            worker, 'verify_peer'), mock.patch.object(worker, 'HidTap') as tap:
                        result = await asyncio.wait_for(worker.receive(identity, SimpleNamespace(pid=1),
                            SimpleNamespace(read=lambda: inbox.popleft() if inbox else None, write=write)), 3)
                    self.assertEqual(result, 0)
                    tap.assert_not_called()
                    self.assertTrue(any(e.get('record', {}).get('step') == 'device_open_error'
                        and e['record']['details']['open_call'] == call for e in decoded))
                    d.watcher.stop.assert_called_once()
                self.assertTrue(handler.flush_pending())
                destination = root / 'device-open-evidence.zip'
                result = log_export.export_logs(destination, root=root)
                self.assertEqual(result.outcome, 'exported')
                with zipfile.ZipFile(destination) as archive:
                    self.assertIsNone(archive.testzip())
                    body = archive.read('app.log').decode('utf-8')
                    manifest = json.loads(archive.read('export-info.json'))
                for call in native:
                    self.assertIn('"open_call":"' + call + '"', body)
                self.assertIn('2147942487', body)
                self.assertIn('error_message', body)
                self.assertNotIn('AA:BB:CC:DD:EE:FF', body)
                self.assertFalse(manifest['incomplete'])
            finally:
                handler.close()
                logger.handlers, logger.level, logger.propagate = old_handlers, old_level, old_propagate


if __name__ == '__main__':
    unittest.main()
