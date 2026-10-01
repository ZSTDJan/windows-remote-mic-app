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
# Raw ATT capture is restricted to the verified A layout. Direct WinRT callbacks
# are bound to the discovered characteristic object, so their routing keys can
# use its actual declaration handle without guessing an ATT value handle.
_VERIFIED_VOICE_DECLARATIONS = (0x39, 0x3B, 0x3E)  # TX, audio, control
_VERIFIED_VOICE_VALUES = {0x3C: "audio", 0x3F: "control"}
_VOICE_SERVICE_ID = uuid.UUID("ab5e0001-5a21-4f05-bc7d-af01f617b664")
# Windows may spend seven seconds establishing an unreachable GATT connection.
# The worker keeps processing input/stop while this bounded query is pending.
_VOICE_SERVICE_TIMEOUT = 10
_VOICE_SESSION_TIMEOUT = 5
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
        self.voice_session = None
        self._voice_services = []
        self._voice_cleanup_failed = False
        self.voice_attributes = {}
        self.input_pending = False
        self.probe_status = -1
        self._voice_events = queue.Queue(maxsize=512)
        self._voice_overflow = threading.Event()
        self._voice_tokens = []
        self._voice_subscribed = []
        self._voice_open = False
        self.voice_queries = []
        self.voice_retryable = False
        self._voice_cache_checked = False

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

    def _connection_status(self):
        try:
            return int(self.device.connection_status)
        except Exception:
            return -1

    def _voice_link(self, step, *, outcome="success", started=None, error=None):
        # Read-only status snapshots. Never request permissions or log device IDs.
        def read(call, maximum):
            try:
                value = int(call())
                return value if 0 <= value <= maximum else -1
            except Exception:
                return -1
        code = getattr(error, "winerror", None)
        row = dict(kind="voice_link", step=step, outcome=outcome,
            duration_ms=0 if started is None else max(0, min(2147483647,
                round((time.monotonic() - started) * 1000))),
            hresult=(code & 0xffffffff) if type(code) is int else -1,
            connected=self._connection_status(),
            device_access=read(lambda: self.device.device_access_information.current_status, 3),
            service_access=read(lambda: self.voice_service.device_access_information.current_status, 3),
            sharing=read(lambda: self.voice_service.sharing_mode, 1),
            service_handle=read(lambda: self.voice_service.attribute_handle, 65535),
            session_status=read(lambda: self.voice_session.session_status, 1),
            maintain=read(lambda: self.voice_session.maintain_connection, 1),
            can_maintain=read(lambda: self.voice_session.can_maintain_connection, 1))
        if len(self.voice_queries) < 16:
            self.voice_queries.append(row)

    async def _start_voice_session(self):
        from winrt.windows.devices.bluetooth.genericattributeprofile import GattSession
        started, outcome, error = time.monotonic(), "success", None
        try:
            self.voice_session = await asyncio.wait_for(
                GattSession.from_device_id_async(self.device.bluetooth_device_id), _VOICE_SESSION_TIMEOUT)
            if self.voice_session is None or not self.voice_session.can_maintain_connection:
                raise PipeError("unsupported_layout")
            self.voice_session.maintain_connection = True
        except BaseException as exc:
            error = exc
            self.voice_retryable = isinstance(exc, TimeoutError)
            outcome = "cancelled" if isinstance(exc, asyncio.CancelledError) else (
                "timeout" if isinstance(exc, TimeoutError) else "exception")
            raise
        finally:
            self._voice_link("session_create", outcome=outcome, started=started, error=error)

    async def _wait_voice_session(self):
        from winrt.windows.devices.bluetooth.genericattributeprofile import GattSessionStatus
        started, outcome, error = time.monotonic(), "success", None
        try:
            async with asyncio.timeout(_VOICE_SESSION_TIMEOUT):
                while self.voice_session.session_status != GattSessionStatus.ACTIVE:
                    if self.changed.is_set():
                        raise PipeError("radio_ambiguous")
                    await asyncio.sleep(.05)
        except BaseException as exc:
            error = exc
            self.voice_retryable = isinstance(exc, TimeoutError)
            outcome = "cancelled" if isinstance(exc, asyncio.CancelledError) else (
                "timeout" if isinstance(exc, TimeoutError) else "exception")
            raise
        finally:
            self._voice_link("session_active", outcome=outcome, started=started, error=error)

    async def _voice_query(self, step, call, *, cached=False, timeout=5):
        """Keep only bounded interface metadata, never values or exception text."""
        started = time.monotonic()
        row = dict(step=step, cache="cached" if cached else "live", duration_ms=0,
                   connected_before=self._connection_status(), connected_after=-1,
                   outcome="exception", status=-1, protocol_error=-1, hresult=-1, item_count=0, items=[])
        result = None
        try:
            result = await asyncio.wait_for(call(), timeout)
            row["status"] = int(result if isinstance(result, int) else result.status)
            try:
                protocol_error = getattr(result, "protocol_error", None)
                if type(protocol_error) is int and 0 <= protocol_error <= 255:
                    row["protocol_error"] = protocol_error
            except Exception:
                pass
            row["outcome"] = "success" if row["status"] == 0 else "status_failed"
            return result
        except asyncio.CancelledError:
            row["outcome"] = "cancelled"
            raise
        except Exception as error:
            row["outcome"] = "timeout" if isinstance(error, TimeoutError) else (
                "os_error" if isinstance(error, OSError) else "exception")
            code = getattr(error, "winerror", None)
            if type(code) is int:
                row["hresult"] = code & 0xffffffff
            raise
        finally:
            row["connected_after"] = self._connection_status()
            row["duration_ms"] = max(0, min(2147483647, round((time.monotonic() - started) * 1000)))
            try:
                items = list(getattr(result, step, ())) if step in {"services", "characteristics"} else []
                row["item_count"] = min(len(items), 65535)
                for item in items[:32]:
                    row["items"].append(dict(uuid=str(uuid.UUID(str(item.uuid))),
                        handle=int(item.attribute_handle),
                        properties=int(item.characteristic_properties) if step == "characteristics" else -1))
            except Exception:
                pass  # Metadata access cannot change a successful query.
            if len(self.voice_queries) < 16:
                self.voice_queries.append(row)
            if step == "characteristics" and not cached:
                self._voice_link("characteristics", outcome=row["outcome"])

    async def _cached_voice_inventory(self):
        """A once-per-run diagnostic fallback; cached data never enables voice."""
        if self._voice_cache_checked:
            return
        self._voice_cache_checked = True
        from winrt.windows.devices.bluetooth import BluetoothCacheMode
        services = []
        try:
            result = await self._voice_query("services", lambda:
                self.device.get_gatt_services_with_cache_mode_async(BluetoothCacheMode.CACHED),
                cached=True, timeout=3)
            services = list(result.services)
            if int(result.status) == 0:
                selected = [s for s in services if s.uuid == _VOICE_SERVICE_ID]
                if len(selected) == 1:
                    await self._voice_query("characteristics", lambda:
                        selected[0].get_characteristics_with_cache_mode_async(BluetoothCacheMode.CACHED),
                        cached=True, timeout=3)
        except Exception:
            pass  # Its failure is already recorded; preserve the live failure.
        finally:
            for service in services:
                try:
                    service.close()
                except Exception:
                    pass  # Cached diagnostic cleanup must not mask the live error.

    async def open_voice(self, *, direct=False):
        self.voice_queries = []
        self.voice_retryable = False
        self.voice_evidence = dict(step="service", status=-1, tx=-1, audio=-1, control=-1,
            tx_handle=-1, audio_handle=-1, control_handle=-1, service_count=-1, characteristic_count=-1, mtu=-1)
        try:
            if self._voice_cleanup_failed or self.voice_session is not None or self._voice_services:
                raise PipeError("cleanup_failed")
            if direct:
                await self._start_voice_session()
            await self._discover_voice(direct=direct)
            self._voice_link("ready")
        except BaseException as error:
            self._voice_link("failed", outcome="cancelled" if isinstance(error, asyncio.CancelledError)
                             else "exception", error=error)
            try:
                await self.close_voice()
            except Exception:
                self.voice_retryable = False
            raise

    async def _discover_voice(self, *, direct):
        from winrt.windows.devices.bluetooth import BluetoothCacheMode
        from winrt.windows.devices.bluetooth.genericattributeprofile import GattCharacteristicProperties, GattWriteOption
        try:
            result = await self._voice_query("services", lambda:
                self.device.get_gatt_services_with_cache_mode_async(BluetoothCacheMode.UNCACHED),
                timeout=_VOICE_SERVICE_TIMEOUT)
        except (TimeoutError, OSError) as error:
            self.voice_retryable = isinstance(error, TimeoutError)
            await self._cached_voice_inventory()
            raise
        services = self._voice_services = list(result.services)
        selected = [service for service in services if service.uuid == _VOICE_SERVICE_ID]
        self.voice_evidence["status"] = int(result.status)
        self.voice_evidence["service_count"] = len(selected)
        if int(result.status) != 0 or len(selected) != 1:
            self.voice_retryable = int(result.status) == 1  # DeviceUnreachable only.
            if self.voice_retryable:
                await self._cached_voice_inventory()
            raise PipeError("unsupported_layout")
        self.voice_service = selected[0]
        if direct:
            await self._wait_voice_session()
        try:
            session = self.voice_session if direct else self.voice_service.session
            self.voice_evidence["mtu"] = int(session.max_pdu_size)
        except Exception:
            pass  # Read-only diagnostic; never create or configure a session.
        self.voice_evidence.update(step="characteristics", status=-1)
        result = await self._voice_query("characteristics", lambda:
            self.voice_service.get_characteristics_with_cache_mode_async(BluetoothCacheMode.UNCACHED))
        discovered = list(result.characteristics)
        characteristics = {str(c.uuid).lower(): c for c in discovered}
        self.voice_evidence["status"] = int(result.status)
        self.voice_evidence["characteristic_count"] = len(characteristics)
        keys = [f"ab5e000{part}-5a21-4f05-bc7d-af01f617b664" for part in (2, 3, 4)]
        if (int(result.status) != 0 or len(characteristics) != len(discovered)
                or not all(key in characteristics for key in keys)):
            raise PipeError("unsupported_layout")
        tx, audio, control = (characteristics[key] for key in keys)
        self.voice_evidence.update(tx=int(tx.characteristic_properties),
                                   audio=int(audio.characteristic_properties), control=int(control.characteristic_properties),
                                   tx_handle=int(tx.attribute_handle), audio_handle=int(audio.attribute_handle),
                                   control_handle=int(control.attribute_handle))
        handles = tuple(int(c.attribute_handle) for c in (tx, audio, control))
        if len(set(handles)) != 3 or not all(0 < h <= 65535 for h in handles):
            raise PipeError("unsupported_layout")
        if direct and not all(h > int(self.voice_service.attribute_handle) for h in handles):
            raise PipeError("unsupported_layout")
        if not direct and handles != _VERIFIED_VOICE_DECLARATIONS:
            raise PipeError("unsupported_layout")
        if direct and any(not (c.characteristic_properties &
                (GattCharacteristicProperties.NOTIFY | GattCharacteristicProperties.INDICATE))
                for c in (audio, control)):
            raise PipeError("unsupported_layout")
        self.voice_tx = tx
        properties = tx.characteristic_properties
        if properties & GattCharacteristicProperties.WRITE:
            self.voice_write_option = GattWriteOption.WRITE_WITH_RESPONSE
        elif properties & GattCharacteristicProperties.WRITE_WITHOUT_RESPONSE:
            self.voice_write_option = GattWriteOption.WRITE_WITHOUT_RESPONSE
        else:
            raise PipeError("unsupported_layout")
        self.voice_attributes = ({handles[1]: "audio", handles[2]: "control"} if direct
                                 else dict(_VERIFIED_VOICE_VALUES))
        if direct:
            from winrt.windows.devices.bluetooth.genericattributeprofile import (
                GattClientCharacteristicConfigurationDescriptorValue as CccdValue,
            )
            self._voice_open = True
            for characteristic, attribute, kind in ((audio, handles[1], "audio"), (control, handles[2], "control")):
                self.voice_evidence["step"] = kind + "_subscription"
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
                # A timed-out write may already have enabled the remote CCCD.
                # Include every attempted subscription in bounded cleanup.
                self._voice_subscribed.append(characteristic)
                status = await self._voice_query(self.voice_evidence["step"], lambda:
                    characteristic.write_client_characteristic_configuration_descriptor_async(mode))
                self.voice_evidence["status"] = int(status)
                if int(status) != 0:
                    raise PipeError("voice_subscribe_failed")
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
        try:
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
        except asyncio.CancelledError:
            failed = True
            raise
        finally:
            failed = not self._release_voice_resources() or failed
            self._voice_cleanup_failed = failed or self._voice_cleanup_failed
            self._voice_link("closed", outcome="exception" if self._voice_cleanup_failed else "success")
        if self._voice_cleanup_failed:
            raise PipeError("cleanup_failed")

    def _release_voice_resources(self):
        self._voice_open = False
        self.voice_tx = None
        self.voice_attributes = {}
        failed_services = []
        session = self.voice_session
        ok = True
        for service in self._voice_services:
            try:
                service.close()
            except Exception:
                ok = False
                failed_services.append(service)
        # Failed closes remain owned so final worker cleanup can try again.
        self._voice_services = failed_services
        if not any(service is self.voice_service for service in failed_services):
            self.voice_service = None
        if session is not None:
            try:
                session.maintain_connection = False
            except Exception:
                ok = False
            try:
                session.close()
            except Exception:
                ok = False
            else:
                self.voice_session = None
        while not self._voice_events.empty():
            try:
                self._voice_events.get_nowait()
            except queue.Empty:
                break
        self._voice_overflow.clear()
        return ok

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
        clean = self._release_voice_resources()
        if self.watcher:
            watcher, self.watcher = self.watcher, None
            for name, token in self.tokens:
                getattr(watcher, "remove_" + name)(token)
            self.tokens.clear()
            watcher.stop()
        if self.device:
            device, self.device = self.device, None
            device.close()
        if not clean or self._voice_cleanup_failed:
            raise PipeError("cleanup_failed")
