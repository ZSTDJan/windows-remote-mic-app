"""Optional RC003 telemetry with its own WinRT resources and daemon loop.

A stuck Windows GATT call must not hold the ATVV loop or its teardown. Each
device has at most one worker, retained until all its resources are released;
reconnect skips telemetry while an earlier worker still owns that device.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid

BATTERY_SERVICE_UUID = "0000180f-0000-1000-8000-00805f9b34fb"
BATTERY_LEVEL_UUID = "00002a19-0000-1000-8000-00805f9b34fb"
_logger = logging.getLogger(__name__)
_workers: dict[str, "BatteryMonitor"] = {}
_workers_lock = threading.Lock()


class BatteryMonitor:
    def __init__(self, device_id, loop, callback, modules_factory, timeout=3.0):
        self._device_id = str(device_id)
        self._loop = loop
        self._callback = callback
        self._modules_factory = modules_factory
        self._deadline = time.monotonic() + timeout
        self._stopped = threading.Event()
        self._ready = threading.Event()
        self._timer = None
        self._thread = threading.Thread(
            target=self._run, name="rc003-battery", daemon=True
        )

    def start(self) -> bool:
        with _workers_lock:
            if self._device_id in _workers:
                return False
            _workers[self._device_id] = self
            try:
                self._timer = self._loop.call_later(
                    max(0.0, self._deadline - time.monotonic()), self._expire_setup
                )
                self._thread.start()
            except BaseException:
                _workers.pop(self._device_id, None)
                self.stop()
                raise
        return True

    def stop(self) -> None:
        """Only revoke delivery; the worker releases its own resources."""
        self._stopped.set()
        self._callback = None
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _expire_setup(self) -> None:
        self._timer = None
        if not self._ready.is_set() and not self._stopped.is_set():
            self.stop()
            _logger.warning("optional RC003 battery monitor setup timed out")

    def _active(self) -> bool:
        return not self._stopped.is_set() and (
            self._ready.is_set() or time.monotonic() < self._deadline
        )

    def _publish(self, level) -> None:
        # Runs on the ATVV/app loop; queued results are checked again after
        # stop/timeout, not merely when the native callback arrived.
        if self._active() and self._ready.is_set() and self._callback is not None:
            self._callback(level)

    def _post(self, level) -> None:
        if level is not None and self._active():
            try:
                self._loop.call_soon_threadsafe(self._publish, level)
            except RuntimeError:
                pass

    @staticmethod
    def _decode(buffer, winrt):
        reader = winrt.data_reader_factory(buffer)
        try:
            if int(reader.unconsumed_buffer_length) != 1:
                return None
            level = int(reader.read_byte())
            return level if 0 <= level <= 100 else None
        finally:
            reader.close()

    def _run(self) -> None:
        try:
            asyncio.run(self._monitor())
        except Exception as exc:
            _logger.warning(
                "optional RC003 battery monitor unavailable: error_type=%s",
                type(exc).__name__,
            )
        finally:
            self._stopped.set()
            with _workers_lock:
                if _workers.get(self._device_id) is self:
                    _workers.pop(self._device_id, None)

    async def _monitor(self) -> None:
        winrt = self._modules_factory()
        device = None
        services = []
        characteristic = None
        token = None
        subscription_attempted = False
        try:
            # Open on this thread. Never share the ATVV device/service object.
            if not self._active():
                return
            device = await winrt.bluetooth_le_device.from_id_async(self._device_id)
            if device is None or not self._active():
                return
            result = await device.get_gatt_services_for_uuid_with_cache_mode_async(
                uuid.UUID(BATTERY_SERVICE_UUID), winrt.bluetooth_cache_mode.UNCACHED
            )
            services = list(result.services)
            if (not self._active() or not services
                    or result.status != winrt.gatt_communication_status.SUCCESS):
                return
            result = await services[0].get_characteristics_with_cache_mode_async(
                winrt.bluetooth_cache_mode.UNCACHED
            )
            if (not self._active()
                    or result.status != winrt.gatt_communication_status.SUCCESS):
                return
            characteristic = next(
                (item for item in result.characteristics
                 if str(item.uuid).casefold() == BATTERY_LEVEL_UUID), None
            )
            if characteristic is None:
                return

            def notify(_sender, args):
                if not self._active() or not self._ready.is_set():
                    return
                try:
                    self._post(self._decode(args.characteristic_value, winrt))
                except Exception as exc:
                    _logger.warning(
                        "RC003 battery notification ignored: error_type=%s",
                        type(exc).__name__,
                    )

            token = characteristic.add_value_changed(notify)
            subscription_attempted = True
            status = await characteristic.write_client_characteristic_configuration_descriptor_async(
                winrt.cccd_value.NOTIFY
            )
            if not self._active() or status != winrt.gatt_communication_status.SUCCESS:
                return
            result = await characteristic.read_value_with_cache_mode_async(
                winrt.bluetooth_cache_mode.UNCACHED
            )
            if (not self._active()
                    or result.status != winrt.gatt_communication_status.SUCCESS):
                return
            level = self._decode(result.value, winrt)
            if level is None or not self._active():
                return
            self._ready.set()
            self._post(level)
            while self._active():
                await asyncio.sleep(0.05)
        finally:
            # Detach delivery before any potentially stuck CCCD/close call.
            self._stopped.set()
            try:
                if characteristic is not None and token is not None:
                    try:
                        characteristic.remove_value_changed(token)
                    except Exception:
                        _logger.warning("optional battery notification detach failed")
                if characteristic is not None and subscription_attempted:
                    try:
                        await characteristic.write_client_characteristic_configuration_descriptor_async(
                            winrt.cccd_value.NONE
                        )
                    except Exception:
                        _logger.warning("optional battery unsubscribe failed")
            finally:
                for service in services:
                    try:
                        service.close()
                    except Exception:
                        _logger.warning("optional battery service close failed")
                if device is not None:
                    try:
                        device.close()
                    except Exception:
                        _logger.warning("optional battery device close failed")
