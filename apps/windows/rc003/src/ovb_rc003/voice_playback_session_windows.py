"""Recover one newly muted, process-owned playback session during WeType hold.

Only metadata crosses threads. Native COM interfaces are acquired and released
on the calling thread; no endpoint volume, default device or other PID is changed.
"""

from __future__ import annotations

import ctypes
import os
import sys
import uuid
from contextlib import ExitStack
from dataclasses import dataclass
from typing import Optional

_P = ctypes.c_void_p
_U = ctypes.c_uint32
_I = ctypes.c_int32
_GUID = ctypes.c_ubyte * 16


class _PropertyKey(ctypes.Structure):
    _fields_ = [("fmtid", _GUID), ("pid", _U)]


class _VariantData(ctypes.Union):
    _fields_ = [
        ("text", ctypes.c_wchar_p),
        ("padding", ctypes.c_ubyte * (2 * ctypes.sizeof(_P))),
    ]


class _PropVariant(ctypes.Structure):
    _fields_ = [
        ("vt", ctypes.c_uint16),
        ("reserved", ctypes.c_uint16 * 3),
        ("data", _VariantData),
    ]


def _guid(value: str):
    return _GUID.from_buffer_copy(uuid.UUID(value).bytes_le)


def _check(result: int) -> None:
    if result < 0:
        raise OSError(f"Core Audio HRESULT 0x{result & 0xffffffff:08x}")


def _call(pointer, index: int, types: tuple, *args) -> None:
    if not pointer:
        raise OSError("Core Audio returned a null interface")
    table = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(_P))).contents
    method = ctypes.WINFUNCTYPE(_I, _P, *types)(table[index])
    _check(int(method(pointer, *args)))


def _release(pointer) -> None:
    if pointer:
        table = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(_P))).contents
        ctypes.WINFUNCTYPE(_U, _P)(table[2])(pointer)


class _Session:
    """Fail closed unless one active endpoint and one own session match."""

    def __init__(self, *, name: str = "", endpoint_id: str = "") -> None:
        self._name = name
        self._wanted_id = endpoint_id
        self._resources = ExitStack()
        self._volume = None
        self.endpoint_id = ""
        self.instance_id = ""
        self.muted: Optional[bool] = None

    def _pointer(self):
        pointer = _P()
        self._resources.callback(_release, pointer)
        return pointer

    def _string(self, pointer, index: int) -> str:
        value = _P()
        _call(pointer, index, (_P,), ctypes.byref(value))
        try:
            return ctypes.wstring_at(value) if value else ""
        finally:
            self._ole.CoTaskMemFree(value)

    def _device_name(self, device) -> str:
        store = self._pointer()
        _call(device, 4, (_U, _P), 0, ctypes.byref(store))
        key = _PropertyKey(_guid("A45C254E-DF1C-4EFD-8020-67D146A850E0"), 14)
        value = _PropVariant()
        try:
            _call(store, 5, (_P, _P), ctypes.byref(key), ctypes.byref(value))
            return (value.data.text or "") if value.vt == 31 else ""
        finally:
            self._ole.PropVariantClear(ctypes.byref(value))

    def __enter__(self):
        if sys.platform != "win32":
            raise OSError("Core Audio playback sessions require Windows")
        try:
            self._ole = ctypes.WinDLL("ole32")
            self._ole.CoInitializeEx.argtypes = (_P, _U)
            self._ole.CoInitializeEx.restype = _I
            self._ole.CoUninitialize.argtypes = ()
            self._ole.CoUninitialize.restype = None
            self._ole.CoCreateInstance.argtypes = (_P, _P, _U, _P, _P)
            self._ole.CoCreateInstance.restype = _I
            self._ole.CoTaskMemFree.argtypes = (_P,)
            self._ole.CoTaskMemFree.restype = None
            self._ole.PropVariantClear.argtypes = (_P,)
            self._ole.PropVariantClear.restype = _I
            result = int(self._ole.CoInitializeEx(None, 0))
            if result >= 0:
                self._resources.callback(self._ole.CoUninitialize)
            elif result & 0xffffffff != 0x80010106:  # Already STA is also valid.
                _check(result)
            self._find()
            return self
        except Exception:
            self._resources.close()
            raise

    def __exit__(self, *_args) -> None:
        self._resources.close()

    def _find(self) -> None:
        enumerator = self._pointer()
        clsid = _guid("BCDE0395-E52F-467C-8E3D-C4579291692E")
        iid = _guid("A95664D2-9614-4F35-A746-DE8DB63617E6")
        _check(int(self._ole.CoCreateInstance(
            ctypes.byref(clsid), None, 1, ctypes.byref(iid), ctypes.byref(enumerator)
        )))
        devices = self._pointer()
        _call(enumerator, 3, (_I, _U, _P), 0, 1, ctypes.byref(devices))
        count = _U()
        _call(devices, 3, (_P,), ctypes.byref(count))
        matches = []
        for index in range(count.value):
            device = self._pointer()
            _call(devices, 4, (_U, _P), index, ctypes.byref(device))
            identity = self._string(device, 5)
            if self._wanted_id:
                matched = identity == self._wanted_id
            else:
                matched = bool(self._name) and self._device_name(device) == self._name
            if matched:
                matches.append((device, identity))
        if len(matches) != 1:
            return
        device, self.endpoint_id = matches[0]
        manager = self._pointer()
        iid = _guid("77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F")
        _call(device, 3, (_P, _U, _P, _P), ctypes.byref(iid), 23, None, ctypes.byref(manager))
        sessions = self._pointer()
        _call(manager, 5, (_P,), ctypes.byref(sessions))
        session_count = _I()
        _call(sessions, 3, (_P,), ctypes.byref(session_count))
        owned = []
        for index in range(session_count.value):
            control = self._pointer()
            control2 = self._pointer()
            _call(sessions, 4, (_I, _P), index, ctypes.byref(control))
            iid = _guid("BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D")
            _call(control, 0, (_P, _P), ctypes.byref(iid), ctypes.byref(control2))
            pid = _U()
            state = _I()
            _call(control2, 14, (_P,), ctypes.byref(pid))
            _call(control2, 3, (_P,), ctypes.byref(state))
            if pid.value == os.getpid() and state.value == 1:
                owned.append(control2)
        if len(owned) != 1:
            return
        self.instance_id = self._string(owned[0], 13)
        self._volume = self._pointer()
        iid = _guid("87CE5498-68D6-44E5-9215-6DA47EF883D8")
        _call(owned[0], 0, (_P, _P), ctypes.byref(iid), ctypes.byref(self._volume))
        self.muted = self._read_mute()

    def _read_mute(self) -> bool:
        muted = _I()
        _call(self._volume, 6, (_P,), ctypes.byref(muted))
        return bool(muted.value)

    def unmute(self) -> None:
        context = _guid("AD39D42D-6B86-4BF7-90B1-C18E25035170")
        _call(self._volume, 5, (_I, _P), 0, ctypes.byref(context))
        if self._read_mute():
            raise OSError("Own playback session remained muted")


@dataclass(frozen=True)
class PlaybackMuteGuard:
    endpoint_id: str
    instance_id: str

    def restore_if_muted(self) -> str:
        with _Session(endpoint_id=self.endpoint_id) as session:
            if session.instance_id != self.instance_id or session.muted is None:
                return "session_changed"
            if not session.muted:
                return "waiting"
            session.unmute()
            return "restored"


def prepare_playback_mute_guard(endpoint_name: str) -> Optional[PlaybackMuteGuard]:
    if not endpoint_name:
        return None
    with _Session(name=endpoint_name) as session:
        if session.muted is not False or not session.instance_id:
            return None
        return PlaybackMuteGuard(session.endpoint_id, session.instance_id)
