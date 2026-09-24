"""Own one bounded, realtime-only BTHPORT ETW session. No ETL files.

x64 EVENT_RECORD/EVENT_TRACE_LOGFILEW ABI follows evntcons.h/evntrace.h.
Packet 402's actual installed manifest exposes BIP_Type/DataLen/Data only,
not a radio identity: the caller must maintain a single-adapter guard.
"""
from __future__ import annotations
import ctypes as C
from ctypes import wintypes as W
from collections import deque
import threading
import time
import uuid
import hashlib
import os

from .chromecast_pipe_windows import _bind, PipeError
from .chromecast_observation import CAPTURE_COUNTS, MAX_COUNT

PROVIDER = uuid.UUID("8a1f9517-3a8c-4a9e-a018-4f17a200f277").bytes_le


class Wnode(C.Structure):
    _fields_ = [("size", W.ULONG), ("provider", W.ULONG), ("context", C.c_uint64),
               ("stamp", C.c_int64), ("guid", C.c_ubyte * 16), ("clock", W.ULONG), ("flags", W.ULONG)]


class Properties(C.Structure):
    _fields_ = [("wnode", Wnode)] + [(name, W.ULONG) for name in (
        "buffer_kb", "minimum", "maximum", "max_file", "mode", "flush_seconds", "enable_flags", "age",
        "buffers", "free", "events_lost", "written", "log_lost", "realtime_lost")] + [
        ("thread", W.HANDLE), ("file_offset", W.ULONG), ("name_offset", W.ULONG)]


class Header(C.Structure):
    _fields_ = [("size", W.WORD), ("header_type", W.WORD), ("flags", W.WORD), ("property", W.WORD),
               ("tid", W.ULONG), ("pid", W.ULONG), ("stamp", C.c_int64), ("provider", C.c_ubyte * 16),
               ("id", W.WORD), ("version", C.c_ubyte), ("channel", C.c_ubyte), ("level", C.c_ubyte),
               ("opcode", C.c_ubyte), ("task", W.WORD), ("keywords", C.c_uint64),
               ("processor_time", C.c_uint64), ("activity", C.c_ubyte * 16)]


class Record(C.Structure):
    _fields_ = [("header", Header), ("buffer_context", W.ULONG), ("extended_count", W.WORD),
               ("data_size", W.WORD), ("extended", C.c_void_p), ("data", C.c_void_p), ("context", C.c_void_p)]


class Logfile(C.Structure):
    _fields_ = [("file", C.c_void_p), ("name", C.c_void_p), ("current_time", C.c_int64),
               ("buffers_read", W.ULONG), ("mode", W.ULONG), ("event_and_header", C.c_ubyte * 368),
               ("buffer_callback", C.c_void_p), ("buffer_size", W.ULONG), ("filled", W.ULONG),
               ("lost", W.ULONG), ("callback", C.c_void_p), ("kernel", W.ULONG), ("context", C.c_void_p)]


class PropertyDescriptor(C.Structure):
    _fields_ = [("name", C.c_uint64), ("index", W.ULONG), ("reserved", W.ULONG)]


_PARAMETERS_KEY = r"SYSTEM\CurrentControlSet\Services\BthPort\Parameters"
_SENSITIVE_VALUE = "EtwLogSensitiveData"
_CAPTURE_SETTINGS = {_SENSITIVE_VALUE: 1, "MaxEtwBytes": 0x400, "EtwDropLargeEvents": 0}


def _setting_ready(name, value, kind, dword):
    return kind == dword and (value >= 0x400 if name == "MaxEtwBytes"
                              else value == _CAPTURE_SETTINGS[name])


def capture_settings_snapshot():
    """Read only fixed capture values; configured does not prove kernel effect."""
    import winreg
    result = {}
    for name in _CAPTURE_SETTINGS:
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _PARAMETERS_KEY, 0, winreg.KEY_READ) as key:
                value, kind = winreg.QueryValueEx(key, name)
            result[name] = {"state": "read", "dword": kind == winreg.REG_DWORD,
                            "value": value if kind == winreg.REG_DWORD else None,
                            "ready": _setting_ready(name, value, kind, winreg.REG_DWORD)}
        except FileNotFoundError:
            result[name] = {"state": "missing", "ready": False}
        except OSError:
            result[name] = {"state": "unavailable", "ready": False}
    return result


def sensitive_logging_enabled():
    # Historical entry name retained for setup, run and detection consumers.
    return all(row["ready"] for row in capture_settings_snapshot().values())


def enable_sensitive_logging():
    """Fixed administrator operation. Keep enabled after receiver exit."""
    import winreg
    before = capture_settings_snapshot()
    if all(row["ready"] for row in before.values()):
        return
    with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, _PARAMETERS_KEY,
                           0, winreg.KEY_SET_VALUE) as key:
        for name, value in _CAPTURE_SETTINGS.items():
            if not before[name]["ready"]:
                winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, value)
    if not sensitive_logging_enabled():
        raise OSError("Bluetooth detailed events could not be enabled")


class Capture:
    def __init__(self, generation):
        self.name = "RemoteMic-Chromecast-" + generation
        self.session, self.consumer = 0, None
        self.queue, self.bytes = deque(), 0
        self.lock = threading.Lock()
        self.failure = ""
        self.worker = None
        self.offset = time.monotonic() - time.time()
        self._guard = None
        self._guid = None
        self.owner_ref = ""
        self.observe = lambda _row: None
        self._flow = dict.fromkeys(CAPTURE_COUNTS, 0)
        self._first_other_event = self._first_other_version = self._first_other_kind = -1
        self._schema_step, self._schema_property, self._schema_code = "none", "none", -1

    def report_flow(self):
        # Only the worker emits this one final snapshot. The ETW callback does
        # bounded in-memory accounting, never formatting, IPC or disk writes.
        with self.lock:
            row = dict(kind="capture_flow", counts=dict(self._flow),
                       first_other_event=self._first_other_event,
                       first_other_version=self._first_other_version,
                       first_other_kind=self._first_other_kind, schema_step=self._schema_step,
                       schema_property=self._schema_property, schema_code=self._schema_code)
        try:
            self.observe(row)
        except Exception:
            pass

    def _count_flow(self, name):
        self._flow[name] = min(MAX_COUNT, self._flow[name] + 1)

    def _schema_failure(self, step, name, code=-1):
        with self.lock:
            if self._schema_step == "none":
                self._schema_step, self._schema_property, self._schema_code = step, name, int(code)

    def _report(self, operation, result, attempt=1):
        try:
            self.observe(dict(kind="capture", operation=operation, result=int(result),
                              attempt=attempt, owner_ref=self.owner_ref))
        except Exception:
            pass

    def _claim_owner(self):
        # The mutex lives as long as the receiving process, including failed
        # cleanup. IPC generations remain random; the OS resource name does not.
        from .chromecast_pipe_windows import inspect_peer
        from .single_instance import BridgeInstanceGuard
        peer = inspect_peer(os.getpid())
        scope = f"RemoteMic-Chromecast-Capture:{peer.sid}:{peer.session}"
        self.owner_ref = hashlib.sha256(scope.encode()).hexdigest()[:32]
        self.name = "RemoteMic-Chromecast-Capture-" + self.owner_ref
        self._guid = uuid.uuid5(uuid.NAMESPACE_OID, scope).bytes_le
        self._guard = BridgeInstanceGuard(name="Local\\" + self.name)
        try:
            self._guard.__enter__()
        except Exception:
            self._guard = None
            self._report("owner_busy", 1)
            raise PipeError("capture_owner_busy") from None

    def _release_owner(self):
        if self._guard is not None:
            self._guard.__exit__(None, None, None)
            self._guard = None

    def _stop_session(self, handle, *, operation):
        result = 0
        for attempt in (1, 2):
            result = self.adv.ControlTraceW(handle, self.name, self.storage, 1)
            self._report(operation, result, attempt)
            if result in (0, 234, 4201):
                return True
            if attempt == 1:
                time.sleep(.05)
        return False

    def _property(self, pointer, name, maximum):
        text = C.create_unicode_buffer(name)
        descriptor = PropertyDescriptor(C.addressof(text), 0xFFFFFFFF, 0)
        size = W.ULONG()
        result = self.tdh.TdhGetPropertySize(pointer, 0, None, 1, C.byref(descriptor), C.byref(size))
        if result or size.value > maximum:
            self._schema_failure("size" if result else "limit", name, result)
            raise PipeError("capture_schema_failed")
        buffer = C.create_string_buffer(size.value)
        result = self.tdh.TdhGetProperty(pointer, 0, None, 1, C.byref(descriptor), size, buffer)
        if result:
            self._schema_failure("read", name, result)
            raise PipeError("capture_schema_failed")
        return buffer.raw

    def _record(self, pointer):
        try:
            record = C.cast(pointer, C.POINTER(Record)).contents
            with self.lock:
                self._count_flow("received")
                if bytes(record.header.provider) != PROVIDER:
                    self._count_flow("other_provider")
                    return
                if record.header.id != 402:
                    self._count_flow("other_event")
                    if self._first_other_event == -1:
                        self._first_other_event = int(record.header.id)
                        self._first_other_version = int(record.header.version)
                    return
            kind = self._property(pointer, "BIP_Type", 1)
            if len(kind) != 1 or kind[0] not in (2, 3, 4):
                with self.lock:
                    self._count_flow("other_kind")
                    if self._first_other_kind == -1 and len(kind) == 1:
                        self._first_other_kind = kind[0]
                return
            with self.lock:
                self._count_flow({2: "hci", 3: "rx", 4: "tx"}[kind[0]])
            packet = self._property(pointer, "BIP_Data", 65536)
            length = self._property(pointer, "BIP_DataLen", 4)
            declared_length = int.from_bytes(length, "little") if len(length) == 4 else -1
            if len(length) != 4 or declared_length != len(packet):
                self._schema_failure("length", "BIP_DataLen")
                raise PipeError("capture_schema_failed")
            details = {
                "capture_metadata_available": True,
                "capture_layout": "bthport_402_tdh",
                "capture_event_id": int(record.header.id),
                "capture_event_version": int(record.header.version),
                "capture_user_data_length": int(record.data_size),
                "capture_property_length": declared_length,
                "capture_extracted_length": len(packet),
                "capture_length_match": True,
            }
            stamp = (record.header.stamp - 116444736000000000) / 10_000_000 + self.offset
            with self.lock:
                if len(self.queue) >= 1024 or self.bytes + len(packet) > 512 * 1024:
                    self._count_flow("queue_overflow")
                    self.failure = "capture_lost"
                    return
                self.queue.append((kind[0], packet, stamp, details))
                self.bytes += len(packet)
                self._count_flow("queued")
        except Exception:
            with self.lock:
                self._count_flow("parse_failed")
            self.failure = "capture_failed"

    def start(self):
        if C.sizeof(C.c_void_p) != 8 or C.sizeof(Header) != 80 or C.sizeof(Logfile) != 448 or Logfile.callback.offset != 424:
            raise PipeError("capture_abi_unsupported")
        self.adv = C.WinDLL("advapi32", use_last_error=True)
        self.tdh = C.WinDLL("tdh", use_last_error=True)
        _bind(self.adv, "StartTraceW", W.ULONG, C.POINTER(C.c_uint64), W.LPCWSTR, C.c_void_p)
        _bind(self.adv, "ControlTraceW", W.ULONG, C.c_uint64, W.LPCWSTR, C.c_void_p, W.ULONG)
        _bind(self.adv, "EnableTraceEx2", W.ULONG, C.c_uint64, C.c_void_p, W.ULONG, C.c_ubyte,
              C.c_uint64, C.c_uint64, W.ULONG, C.c_void_p)
        _bind(self.adv, "OpenTraceW", C.c_uint64, C.POINTER(Logfile))
        _bind(self.adv, "ProcessTrace", W.ULONG, C.POINTER(C.c_uint64), W.ULONG, C.c_void_p, C.c_void_p)
        _bind(self.adv, "CloseTrace", W.ULONG, C.c_uint64)
        for name, last in (("TdhGetPropertySize", (C.POINTER(W.ULONG),)), ("TdhGetProperty", (W.ULONG, C.c_void_p))):
            _bind(self.tdh, name, W.ULONG, C.c_void_p, W.ULONG, C.c_void_p, W.ULONG, C.POINTER(PropertyDescriptor), *last)
        self.storage = C.create_string_buffer(C.sizeof(Properties) + 1024)
        self.properties = Properties.from_buffer(self.storage)
        p = self.properties
        p.wnode.size, p.wnode.flags, p.wnode.clock = len(self.storage), 0x20000, 2
        p.buffer_kb, p.minimum, p.maximum, p.mode, p.flush_seconds = 64, 4, 16, 0x100, 1
        p.name_offset = C.sizeof(Properties)
        self._claim_owner()
        p.wnode.guid[:] = self._guid
        name = (self.name + "\0").encode("utf-16-le")
        C.memmove(C.addressof(self.storage) + p.name_offset, name, len(name))
        session = C.c_uint64()
        result = self.adv.StartTraceW(C.byref(session), self.name, self.storage)
        self._report("start", result)
        if result == 183:  # Existing exact-name resource, while no live owner holds our mutex.
            result = self.adv.ControlTraceW(0, self.name, self.storage, 0)
            self._report("recover_query", result)
            if result != 0 or bytes(p.wnode.guid) != self._guid:
                self._release_owner()
                raise PipeError("capture_recovery_unconfirmed")
            if not self._stop_session(0, operation="recover_stop"):
                self._release_owner()
                raise PipeError("capture_cleanup_failed")
            # QUERY/STOP output is not a valid new-session configuration.
            C.memset(C.addressof(self.storage), 0, len(self.storage))
            p.wnode.size, p.wnode.flags, p.wnode.clock = len(self.storage), 0x20000, 2
            p.wnode.guid[:] = self._guid
            p.buffer_kb, p.minimum, p.maximum, p.mode, p.flush_seconds = 64, 4, 16, 0x100, 1
            p.name_offset = C.sizeof(Properties)
            C.memmove(C.addressof(self.storage) + p.name_offset, name, len(name))
            result = self.adv.StartTraceW(C.byref(session), self.name, self.storage)
            self._report("restart", result)
        if result:
            self._release_owner()
            raise PipeError("capture_start_failed")
        self.session = session.value
        try:
            provider = (C.c_ubyte * 16).from_buffer_copy(PROVIDER)
            if self.adv.EnableTraceEx2(self.session, provider, 1, 5, 0x8000000000000000, 0, 0, None):
                raise PipeError("capture_enable_failed")
            self.callback = C.WINFUNCTYPE(None, C.c_void_p)(self._record)
            self.name_buffer = C.create_unicode_buffer(self.name)
            logfile = Logfile()
            logfile.name = C.addressof(self.name_buffer)
            logfile.mode = 0x10000000 | 0x100
            logfile.callback = C.cast(self.callback, C.c_void_p).value
            self.consumer = self.adv.OpenTraceW(C.byref(logfile))
            if self.consumer == 0xFFFFFFFFFFFFFFFF:
                self.consumer = None
                raise PipeError("capture_open_failed")
            consumer = C.c_uint64(self.consumer)
            def consume():
                result = self.adv.ProcessTrace(C.byref(consumer), 1, None, None)
                if result not in (0, 1223):
                    self.failure = "capture_failed"
            self.worker = threading.Thread(target=consume, name="chromecast-etw", daemon=True)
            self.worker.start()
        except BaseException:
            self.stop()
            raise

    def poll(self):
        if self.failure:
            raise PipeError(self.failure)
        if self.worker is not None and not self.worker.is_alive():
            raise PipeError("capture_ended")
        if self.session:
            if self.adv.ControlTraceW(self.session, self.name, self.storage, 3):
                raise PipeError("capture_flush_failed")
            p = self.properties
            if p.events_lost or p.log_lost or p.realtime_lost:
                raise PipeError("capture_lost")
        with self.lock:
            packets = list(self.queue)
            self.queue.clear()
            self.bytes = 0
        return packets

    def stop(self):
        failed = False
        if self.session:
            if self._stop_session(self.session, operation="stop"):
                self.session = 0
            else:
                failed = True
        if self.consumer is not None:
            result = self.adv.CloseTrace(self.consumer)
            self._report("close_consumer", result)
            if result not in (0, 7007):  # ERROR_CTX_CLOSE_PENDING: processing thread is leaving.
                failed = True
            else:
                self.consumer = None
        if self.worker is not None:
            self.worker.join(2)
            if self.worker.is_alive():
                failed = True
        if failed:
            # Keep the guard until retry or process exit. A successor can then
            # recover this exact stable-name/GUID session, never an arbitrary prefix.
            raise PipeError("capture_cleanup_failed")
        self._release_owner()
        with self.lock:
            self.queue.clear()
            self.bytes = 0
