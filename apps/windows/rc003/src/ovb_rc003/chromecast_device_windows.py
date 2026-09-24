"""Resolve the selected entity/radio and its narrowly scoped voice commands."""
from __future__ import annotations
import asyncio
import hashlib
import queue
import re
import threading
import time
import uuid

from . import raw_input_windows, remote_selection
from .chromecast_pipe_windows import PipeError
from .chromecast_voice import GET_CAPABILITIES

# Observed model/revision contract, NOT a default for arbitrary Chromecast
# firmware. Both PnP identity and the live nonce proof must succeed each run.
_VERIFIED_INPUT_HANDLES = {(0x18D1, 0x9450, 0x0110): 0x29}
# The same verified firmware exposes declaration handles through WinRT, while
# ATT notifications carry VALUE handles. These are not interchangeable. Never
# guess "+1" for an unknown layout; open() has already enforced the PnP revision.
_VERIFIED_VOICE_DECLARATIONS = (0x39, 0x3B, 0x3E)  # TX, audio, control
_VERIFIED_VOICE_VALUES = {0x3C: "audio", 0x3F: "control"}
_PNP = re.compile(r"(?:dev_vid&[0-9a-f]{2}|vid_)([0-9a-f]{4})(?:_pid&|&pid_)([0-9a-f]{4})(?:_rev&|&rev_)([0-9a-f]{4})", re.I)


def supported_path(path):
    match = _PNP.search(path)
    return bool(match and tuple(int(part, 16) for part in match.groups()[:2]) == (0x18D1, 0x9450))


def input_handle(entity, *, enumerate_paths=None, resolve=None):
    enumerate_paths = enumerate_paths or raw_input_windows.enumerate_matching_device_paths
    resolve = resolve or remote_selection.raw_path_key
    matches = set()
    paths = enumerate_paths(matches=supported_path)
    if not paths:
        raise PipeError("input_interface_unavailable")
    for path in paths:
        try:
            selected = resolve(path) == entity
        except (remote_selection.SelectionError, OSError):
            continue
        if selected:
            match = _PNP.search(path)
            if match:
                matches.add(tuple(int(part, 16) for part in match.groups()))
    if not matches:
        raise PipeError("input_identity_unconfirmed")
    if len(matches) != 1 or not matches <= _VERIFIED_INPUT_HANDLES.keys():
        raise PipeError("unsupported_layout")
    return _VERIFIED_INPUT_HANDLES[matches.pop()]


class SelectedDevice:
    def __init__(self, entity):
        self.entity = entity
        self.changed = threading.Event()
        self.enumerated = threading.Event()
        self.ids = set()
        self.watcher = self.device = None
        self.tokens = []
        self.voice_service = self.voice_tx = None
        self.voice_attributes = {}
        self.input_pending = False
        self.probe_status = -1
        self._voice_events = queue.Queue(maxsize=512)
        self._voice_overflow = threading.Event()
        self._voice_tokens = []
        self._voice_subscribed = []
        self._voice_open = False

    async def _resolve_input(self):
        # The worker owns the existing 20-second startup deadline and continues
        # consuming stop/heartbeat/selection changes while this task waits.
        try:
            while True:
                if self.changed.is_set():
                    raise PipeError("radio_ambiguous")
                try:
                    return input_handle(self.entity)
                except PipeError as error:
                    if str(error) != "input_interface_unavailable":
                        raise
                    self.input_pending = True
                    await asyncio.sleep(.25)
        finally:
            self.input_pending = False

    async def open(self, *, require_input=True):
        from winrt.windows.devices.bluetooth import BluetoothAdapter, BluetoothLEDevice
        from winrt.windows.devices.enumeration import DeviceInformation
        self.watcher = DeviceInformation.create_watcher_aqs_filter(BluetoothAdapter.get_device_selector())
        def added(sender, info):
            if self.enumerated.is_set():
                self.changed.set()
            self.ids.add(info.id)
        self.tokens = [
            ("added", self.watcher.add_added(added)),
            ("removed", self.watcher.add_removed(lambda *_: self.changed.set())),
            ("stopped", self.watcher.add_stopped(lambda *_: self.changed.set())),
            ("enumeration_completed", self.watcher.add_enumeration_completed(lambda *_: self.enumerated.set())),
        ]
        self.watcher.start()
        for _ in range(160):
            if self.enumerated.is_set():
                break
            await asyncio.sleep(.05)
        if not self.enumerated.is_set() or len(self.ids) != 1 or self.changed.is_set():
            raise PipeError("radio_ambiguous")
        self.radio = hashlib.sha256(b"RemoteMic radio v1\0" + next(iter(self.ids)).encode("utf-8")).hexdigest()
        selector = BluetoothLEDevice.get_device_selector_from_pairing_state(True)
        infos = await DeviceInformation.find_all_async_aqs_filter_and_additional_properties(selector, ["System.Devices.Aep.ContainerId"])
        selected = []
        for info in infos:
            try:
                if remote_selection.candidate_key(info) == self.entity:
                    selected.append(info)
            except remote_selection.SelectionError:
                continue
        if len(selected) != 1 or selected[0].name.strip().casefold() != "chromecast remote":
            raise PipeError("source_unconfirmed")
        if require_input:
            self.attribute = await self._resolve_input()
        self.device = await BluetoothLEDevice.from_id_async(selected[0].id)
        if self.device is None or self.device.device_id != selected[0].id:
            raise PipeError("source_unconfirmed")
        if self.changed.is_set():
            raise PipeError("radio_ambiguous")

    async def probe(self, marker):
        from winrt.windows.devices.bluetooth import BluetoothCacheMode
        result = await self.device.get_gatt_services_for_uuid_with_cache_mode_async(marker, BluetoothCacheMode.UNCACHED)
        self.probe_status = int(result.status)
        services = list(result.services)
        for service in services:
            service.close()
        return int(result.status) == 0, len(services)

    async def open_voice(self, *, direct=False):
        self.voice_evidence = dict(step="service", status=-1, tx=-1, audio=-1, control=-1,
            tx_handle=-1, audio_handle=-1, control_handle=-1, service_count=-1, characteristic_count=-1, mtu=-1)
        from winrt.windows.devices.bluetooth import BluetoothCacheMode
        from winrt.windows.devices.bluetooth.genericattributeprofile import GattCharacteristicProperties, GattWriteOption
        service_id = uuid.UUID("ab5e0001-5a21-4f05-bc7d-af01f617b664")
        result = await asyncio.wait_for(self.device.get_gatt_services_for_uuid_with_cache_mode_async(
            service_id, BluetoothCacheMode.UNCACHED), 5)
        services = list(result.services)
        self.voice_evidence["status"] = int(result.status)
        self.voice_evidence["service_count"] = len(services)
        if int(result.status) != 0 or len(services) != 1:
            for service in services:
                service.close()
            raise PipeError("unsupported_layout")
        self.voice_service = services[0]
        try:
            self.voice_evidence["mtu"] = int(self.voice_service.session.max_pdu_size)
        except Exception:
            pass  # Read-only diagnostic; never create or configure a session.
        self.voice_evidence.update(step="characteristics", status=-1)
        result = await asyncio.wait_for(self.voice_service.get_characteristics_with_cache_mode_async(BluetoothCacheMode.UNCACHED), 5)
        characteristics = {str(c.uuid).lower(): c for c in result.characteristics}
        self.voice_evidence["status"] = int(result.status)
        self.voice_evidence["characteristic_count"] = len(characteristics)
        keys = [f"ab5e000{part}-5a21-4f05-bc7d-af01f617b664" for part in (2, 3, 4)]
        if int(result.status) != 0 or not all(key in characteristics for key in keys):
            raise PipeError("unsupported_layout")
        tx, audio, control = (characteristics[key] for key in keys)
        self.voice_evidence.update(tx=int(tx.characteristic_properties),
                                   audio=int(audio.characteristic_properties), control=int(control.characteristic_properties),
                                   tx_handle=int(tx.attribute_handle), audio_handle=int(audio.attribute_handle),
                                   control_handle=int(control.attribute_handle))
        if tuple(int(c.attribute_handle) for c in (tx, audio, control)) != _VERIFIED_VOICE_DECLARATIONS:
            raise PipeError("unsupported_layout")
        self.voice_tx = tx
        properties = tx.characteristic_properties
        if properties & GattCharacteristicProperties.WRITE:
            self.voice_write_option = GattWriteOption.WRITE_WITH_RESPONSE
        elif properties & GattCharacteristicProperties.WRITE_WITHOUT_RESPONSE:
            self.voice_write_option = GattWriteOption.WRITE_WITHOUT_RESPONSE
        else:
            raise PipeError("unsupported_layout")
        self.voice_attributes = dict(_VERIFIED_VOICE_VALUES)
        if direct:
            from winrt.windows.devices.bluetooth.genericattributeprofile import (
                GattClientCharacteristicConfigurationDescriptorValue as CccdValue,
            )
            self._voice_open = True
            for characteristic, attribute in ((audio, 0x3C), (control, 0x3F)):
                self.voice_evidence["step"] = "audio_subscription" if attribute == 0x3C else "control_subscription"
                self.voice_evidence["status"] = -1
                def notify(_sender, args, attribute=attribute):
                    if not self._voice_open:
                        return
                    try:
                        value = bytes(args.characteristic_value)
                        if len(value) > 512:
                            raise ValueError("oversized voice notification")
                        self._voice_events.put_nowait((attribute, value, time.monotonic()))
                    except Exception:
                        self._voice_overflow.set()

                token = characteristic.add_value_changed(notify)
                self._voice_tokens.append((characteristic, token))
                properties = characteristic.characteristic_properties
                if properties & GattCharacteristicProperties.NOTIFY:
                    mode = CccdValue.NOTIFY
                elif properties & GattCharacteristicProperties.INDICATE:
                    mode = CccdValue.INDICATE
                else:
                    raise PipeError("unsupported_layout")
                status = await asyncio.wait_for(
                    characteristic.write_client_characteristic_configuration_descriptor_async(mode), 5)
                self.voice_evidence["status"] = int(status)
                if int(status) != 0:
                    raise PipeError("voice_subscribe_failed")
                self._voice_subscribed.append(characteristic)
        self.voice_evidence["step"] = "ready"

    def poll_voice(self):
        if self._voice_overflow.is_set():
            raise PipeError("capture_lost")
        events = []
        for _ in range(128):
            try:
                events.append(self._voice_events.get_nowait())
            except queue.Empty:
                break
        return events

    async def close_voice(self):
        self._voice_open = False
        tokens, self._voice_tokens = self._voice_tokens, []
        subscribed, self._voice_subscribed = self._voice_subscribed, []
        failed = False
        for characteristic, token in tokens:
            try:
                characteristic.remove_value_changed(token)
            except Exception:
                failed = True
        if subscribed:
            from winrt.windows.devices.bluetooth.genericattributeprofile import (
                GattClientCharacteristicConfigurationDescriptorValue as CccdValue,
            )
            for characteristic in subscribed:
                try:
                    status = await asyncio.wait_for(
                        characteristic.write_client_characteristic_configuration_descriptor_async(CccdValue.NONE), 3)
                    if int(status) != 0:
                        failed = True
                except Exception:
                    failed = True
        if failed:
            raise PipeError("cleanup_failed")

    async def write_voice(self, command):
        from winrt.windows.devices.bluetooth import BluetoothConnectionStatus
        from winrt.windows.storage.streams import DataWriter
        if (self.changed.is_set() or self.voice_tx is None
                or self.device.connection_status != BluetoothConnectionStatus.CONNECTED
                or not isinstance(command, bytes)
                or not (command == GET_CAPABILITIES or (len(command) == 2
                    and command[0] in (12, 13, 14) and command[1] <= 128
                    and (command[0] != 12 or command[1] == 0)))):
            raise PipeError("invalid_voice_command")
        writer = DataWriter()
        try:
            writer.write_bytes(command)
            result = await asyncio.wait_for(self.voice_tx.write_value_with_result_and_option_async(
                writer.detach_buffer(), self.voice_write_option), 3)
            if int(result.status) != 0:
                raise PipeError("voice_write_failed")
        finally:
            writer.close()

    def close(self):
        if self.voice_service:
            self.voice_tx = None
            self.voice_service.close()
            self.voice_service = None
        if self.watcher:
            watcher, self.watcher = self.watcher, None
            for name, token in self.tokens:
                getattr(watcher, "remove_" + name)(token)
            self.tokens.clear()
            watcher.stop()
        if self.device:
            device, self.device = self.device, None
            device.close()
