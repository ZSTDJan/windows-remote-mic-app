"""Control WeType voice input through a configured held shortcut on Windows.

WeType's voice shortcut works only while its TSF profile is active.
Profile activation therefore runs on a dedicated STA thread. A cold switch
gets a short rebinding delay, then the shortcut is delivered through spaced
SendInput edges. Window visibility is diagnostic only and never gates audio.
"""

from __future__ import annotations

import ctypes
import logging
import os
import queue
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Optional, Sequence, TypeVar, cast

from . import diagnostic_trace, voice_playback_session_windows, win32_input

_STA_RESULT_TIMEOUT_SECONDS = 0.5
_SESSION_REBIND_SETTLE_SECONDS = 0.05
_WETYPE_CONFIRM_DELAY_SECONDS = 0.7
_WETYPE_RETRY_SETTLE_SECONDS = (2.0, 3.0, 5.0)

_COINIT_APARTMENTTHREADED = 0x2
_CLSCTX_INPROC_SERVER = 0x1
_TF_PROFILETYPE_INPUTPROCESSOR = 0x1
_TF_IPP_FLAG_ENABLED = 0x2
_TF_IPPMF_DONTCARECURRENTINPUTLANGUAGE = 0x4
_TF_IPPMF_FORSESSION = 0x20000000
_PROFILE_ACTIVATION_FLAGS = (
    _TF_IPPMF_FORSESSION | _TF_IPPMF_DONTCARECURRENTINPUTLANGUAGE
)


class _GUID(ctypes.Structure):
    _fields_ = [
        ("data1", ctypes.c_uint32),
        ("data2", ctypes.c_uint16),
        ("data3", ctypes.c_uint16),
        ("data4", ctypes.c_ubyte * 8),
    ]


class _TF_INPUTPROCESSORPROFILE(ctypes.Structure):
    _fields_ = [
        ("dwProfileType", ctypes.c_uint32),
        ("langid", ctypes.c_uint16),
        ("clsid", _GUID),
        ("guidProfile", _GUID),
        ("catid", _GUID),
        ("hklSubstitute", ctypes.c_void_p),
        ("dwCaps", ctypes.c_uint32),
        ("hkl", ctypes.c_void_p),
        ("dwFlags", ctypes.c_uint32),
    ]


def _guid_bytes(value: str) -> bytes:
    return uuid.UUID(value).bytes_le


def _native_guid(value: bytes) -> _GUID:
    return _GUID.from_buffer_copy(value)


@dataclass(frozen=True)
class _InputProfileSnapshot:
    profile_type: int
    language_id: int
    clsid: bytes
    profile_guid: bytes
    hkl: int = 0
    flags: int = 0


_PROFILE_MANAGER_CLSID = _guid_bytes("33C53A50-F456-4884-B049-85FD643ECFED")
_PROFILE_MANAGER_IID = _guid_bytes("71C6E74C-0F28-11D8-A82A-00065B84435C")
_KEYBOARD_CATEGORY_GUID = _guid_bytes("34745C63-B2F0-4784-8B67-5E12C8701A31")
_LANGUAGE_ID_ZH_CN = 0x0804
_PROFILE_MATCHERS = {
    "wetype": ("wetype", "微信输入法"),
    "doubao": ("doubao", "豆包输入法"),
}
_STA_OPERATION_LOCK = threading.Lock()
_STA_REQUEST_LOCAL = threading.local()
_diagnostic_trace: Optional[diagnostic_trace.DiagnosticTrace] = None


def set_diagnostic_trace(trace: Optional[diagnostic_trace.DiagnosticTrace]) -> None:
    global _diagnostic_trace
    _diagnostic_trace = trace


def _trace(event: str, **fields: object) -> None:
    trace = _diagnostic_trace
    if trace is None:
        return
    try:
        payload = dict(trace.current_context())
        payload.update(fields)
        trace.emit(event, **payload)
    except BaseException:
        pass


def _ole32():
    win32_input._require_windows()
    ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    ole32.CoInitializeEx.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
    ole32.CoInitializeEx.restype = ctypes.c_long
    ole32.CoUninitialize.argtypes = ()
    ole32.CoUninitialize.restype = None
    ole32.CoCreateInstance.argtypes = (
        ctypes.POINTER(_GUID),
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(_GUID),
        ctypes.POINTER(ctypes.c_void_p),
    )
    ole32.CoCreateInstance.restype = ctypes.c_long
    return ole32


def _raise_for_hresult(operation: str, result: int) -> None:
    _trace(
        "input_profile_com_result",
        operation=str(operation),
        hresult=f"0x{result & 0xFFFFFFFF:08X}",
        success=result >= 0,
    )
    if result < 0:
        raise OSError(f"{operation} failed: HRESULT=0x{result & 0xFFFFFFFF:08X}")


class _InputProfileManager:
    """Minimal ITfInputProcessorProfileMgr wrapper kept local to WeType."""

    def __init__(self) -> None:
        self._pointer = ctypes.c_void_p()
        self._ole32 = None
        self._uninitialize = False

    def __enter__(self):
        ole32 = _ole32()
        result = int(ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED))
        _raise_for_hresult("CoInitializeEx", result)
        _trace("input_profile_com_apartment", apartment="STA", initialized=True)
        self._uninitialize = True
        self._ole32 = ole32

        class_id = _native_guid(_PROFILE_MANAGER_CLSID)
        interface_id = _native_guid(_PROFILE_MANAGER_IID)
        result = int(
            ole32.CoCreateInstance(
                ctypes.byref(class_id),
                None,
                _CLSCTX_INPROC_SERVER,
                ctypes.byref(interface_id),
                ctypes.byref(self._pointer),
            )
        )
        if result < 0:
            ole32.CoUninitialize()
            self._uninitialize = False
            _raise_for_hresult("CoCreateInstance", result)
        return self

    def _method(self, index: int, result_type, *argument_types):
        vtable = ctypes.cast(
            self._pointer,
            ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)),
        ).contents
        prototype = ctypes.WINFUNCTYPE(
            result_type,
            ctypes.c_void_p,
            *argument_types,
        )
        return prototype(vtable[index])

    def get_active_profile(self) -> _InputProfileSnapshot:
        profile = _TF_INPUTPROCESSORPROFILE()
        category = _native_guid(_KEYBOARD_CATEGORY_GUID)
        method = self._method(
            10,
            ctypes.c_long,
            ctypes.POINTER(_GUID),
            ctypes.POINTER(_TF_INPUTPROCESSORPROFILE),
        )
        result = int(
            method(self._pointer, ctypes.byref(category), ctypes.byref(profile))
        )
        _raise_for_hresult("GetActiveProfile", result)
        return _InputProfileSnapshot(
            profile_type=int(profile.dwProfileType),
            language_id=int(profile.langid),
            clsid=bytes(profile.clsid),
            profile_guid=bytes(profile.guidProfile),
            hkl=int(profile.hkl or 0),
            flags=int(profile.dwFlags),
        )

    def enum_profiles(self, language_id: int) -> tuple[_InputProfileSnapshot, ...]:
        enumerator = ctypes.c_void_p()
        method = self._method(
            6,
            ctypes.c_long,
            ctypes.c_uint16,
            ctypes.POINTER(ctypes.c_void_p),
        )
        result = int(method(self._pointer, int(language_id), ctypes.byref(enumerator)))
        _raise_for_hresult("EnumProfiles", result)
        if not enumerator.value:
            return ()
        profiles: list[_InputProfileSnapshot] = []
        try:
            vtable = ctypes.cast(
                enumerator,
                ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)),
            ).contents
            next_profile = ctypes.WINFUNCTYPE(
                ctypes.c_long,
                ctypes.c_void_p,
                ctypes.c_ulong,
                ctypes.POINTER(_TF_INPUTPROCESSORPROFILE),
                ctypes.POINTER(ctypes.c_ulong),
            )(vtable[4])
            while True:
                profile = _TF_INPUTPROCESSORPROFILE()
                fetched = ctypes.c_ulong(0)
                result = int(
                    next_profile(
                        enumerator,
                        1,
                        ctypes.byref(profile),
                        ctypes.byref(fetched),
                    )
                )
                if result < 0:
                    _raise_for_hresult("IEnumTfInputProcessorProfiles.Next", result)
                if fetched.value == 0:
                    break
                profiles.append(
                    _InputProfileSnapshot(
                        profile_type=int(profile.dwProfileType),
                        language_id=int(profile.langid),
                        clsid=bytes(profile.clsid),
                        profile_guid=bytes(profile.guidProfile),
                        hkl=int(profile.hkl or 0),
                        flags=int(profile.dwFlags),
                    )
                )
                if result == 1:
                    break
        finally:
            vtable = ctypes.cast(
                enumerator,
                ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)),
            ).contents
            release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[2])
            release(enumerator)
        return tuple(profiles)

    def activate_profile(self, profile: _InputProfileSnapshot) -> None:
        class_id = _native_guid(profile.clsid)
        profile_id = _native_guid(profile.profile_guid)
        method = self._method(
            3,
            ctypes.c_long,
            ctypes.c_uint32,
            ctypes.c_uint16,
            ctypes.POINTER(_GUID),
            ctypes.POINTER(_GUID),
            ctypes.c_void_p,
            ctypes.c_uint32,
        )
        result = int(
            method(
                self._pointer,
                profile.profile_type,
                profile.language_id,
                ctypes.byref(class_id),
                ctypes.byref(profile_id),
                ctypes.c_void_p(profile.hkl),
                _PROFILE_ACTIVATION_FLAGS,
            )
        )
        _raise_for_hresult("ActivateProfile", result)

    def __exit__(self, _exc_type, _exc_value, _traceback) -> None:
        try:
            if self._pointer.value:
                release = self._method(2, ctypes.c_ulong)
                release(self._pointer)
        finally:
            self._pointer = ctypes.c_void_p()
            if self._uninitialize and self._ole32 is not None:
                self._ole32.CoUninitialize()
                _trace("input_profile_com_apartment", apartment="STA", initialized=False)
                self._uninitialize = False


def _profile_guid_text(value: bytes) -> str:
    return "{" + str(uuid.UUID(bytes_le=value)).upper() + "}"


def _profile_registry_text(profile: _InputProfileSnapshot) -> str:
    if os.name != "nt":
        return ""
    try:
        import winreg
    except ImportError:
        return ""
    relative = (
        "Software\\Microsoft\\CTF\\TIP\\"
        f"{_profile_guid_text(profile.clsid)}\\LanguageProfile\\"
        f"0x{profile.language_id:08X}\\{_profile_guid_text(profile.profile_guid)}"
    )
    values: list[str] = []
    views = tuple(
        dict.fromkeys(
            (
                0,
                getattr(winreg, "KEY_WOW64_64KEY", 0),
                getattr(winreg, "KEY_WOW64_32KEY", 0),
            )
        )
    )
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in views:
            try:
                with winreg.OpenKey(
                    hive,
                    relative,
                    0,
                    winreg.KEY_READ | view,
                ) as key:
                    for name in ("Description", "Display Description", "IconFile"):
                        try:
                            value, _kind = winreg.QueryValueEx(key, name)
                        except OSError:
                            continue
                        values.append(str(value))
            except OSError:
                continue
    return "\n".join(values).casefold()


def _same_profile(
    left: _InputProfileSnapshot,
    right: _InputProfileSnapshot,
) -> bool:
    """Compare the stable TSF profile identity, excluding mutable flags."""

    return (
        left.profile_type == right.profile_type
        and left.language_id == right.language_id
        and left.clsid == right.clsid
        and left.profile_guid == right.profile_guid
        and left.hkl == right.hkl
    )


def _profile_trace_fields(
    prefix: str, profile: _InputProfileSnapshot
) -> dict[str, object]:
    return {
        f"{prefix}_type": int(profile.profile_type),
        f"{prefix}_language_id": int(profile.language_id),
        f"{prefix}_clsid": _profile_guid_text(profile.clsid),
        f"{prefix}_guid": _profile_guid_text(profile.profile_guid),
        f"{prefix}_hkl": int(profile.hkl),
        f"{prefix}_flags": int(profile.flags),
    }


def _discover_input_profile(
    manager: _InputProfileManager,
    provider_id: str,
) -> _InputProfileSnapshot:
    matchers = _PROFILE_MATCHERS[provider_id]
    for profile in manager.enum_profiles(_LANGUAGE_ID_ZH_CN):
        if profile.profile_type != _TF_PROFILETYPE_INPUTPROCESSOR:
            continue
        if profile.flags and not (profile.flags & _TF_IPP_FLAG_ENABLED):
            continue
        text = _profile_registry_text(profile)
        if any(matcher in text for matcher in matchers):
            return profile
    raise OSError(f"{provider_id} input profile is not installed or enabled")


def _sta_cancelled() -> bool:
    event = getattr(_STA_REQUEST_LOCAL, "cancel_event", None)
    return bool(event is not None and event.is_set())


def _activate_input_profile(
    provider_id: str,
    provider_name: str,
    *,
    _sleep: Callable[[float], None] = time.sleep,
) -> bool:
    started_ms = time.monotonic_ns() // 1_000_000
    _trace(
        "input_profile_activation_started",
        provider=str(provider_id),
        provider_name=str(provider_name),
    )
    try:
        with _InputProfileManager() as manager:
            target = _discover_input_profile(manager, provider_id)
            previous = manager.get_active_profile()
            _trace(
                "input_profile_snapshot",
                provider=str(provider_id),
                phase="before",
                **_profile_trace_fields("active", previous),
                **_profile_trace_fields("target", target),
            )
            if _same_profile(previous, target):
                _trace(
                    "input_profile_activation_finished",
                    provider=str(provider_id),
                    success=True,
                    switched=False,
                    confirmed=True,
                    elapsed_ms=(time.monotonic_ns() // 1_000_000) - started_ms,
                )
                return False
            if _sta_cancelled():
                raise TimeoutError(f"{provider_name} input profile request was cancelled")
            manager.activate_profile(target)
            if _sta_cancelled():
                manager.activate_profile(previous)
                raise TimeoutError(f"{provider_name} input profile request was cancelled")
            current = manager.get_active_profile()
            confirmed = _same_profile(current, target)
            _trace(
                "input_profile_snapshot",
                provider=str(provider_id),
                phase="after",
                confirmed=confirmed,
                **_profile_trace_fields("active", current),
                **_profile_trace_fields("target", target),
            )
            if not confirmed:
                try:
                    manager.activate_profile(previous)
                except OSError:
                    pass
                raise OSError(
                    f"{provider_name} input profile activation could not be confirmed"
                )
            _sleep(_SESSION_REBIND_SETTLE_SECONDS)
            if _sta_cancelled():
                manager.activate_profile(previous)
                raise TimeoutError(f"{provider_name} input profile request was cancelled")
            _trace(
                "input_profile_activation_finished",
                provider=str(provider_id),
                success=True,
                switched=True,
                confirmed=True,
                elapsed_ms=(time.monotonic_ns() // 1_000_000) - started_ms,
            )
            return True
    except BaseException as exc:
        _trace(
            "input_profile_activation_finished",
            provider=str(provider_id),
            success=False,
            switched=False,
            confirmed=False,
            cancelled=isinstance(exc, TimeoutError),
            error_type=type(exc).__name__,
            elapsed_ms=(time.monotonic_ns() // 1_000_000) - started_ms,
        )
        raise


def _activate_wetype_input_profile() -> bool:
    """Activate WeType for the current Windows session; return whether it switched."""

    return _activate_input_profile("wetype", "WeType")


def _activate_doubao_input_profile() -> bool:
    """Activate Doubao IME for the current Windows session."""

    return _activate_input_profile("doubao", "Doubao")


def _activate_wetype_for_voice_start(
    *, _sleep: Callable[[float], None] = time.sleep
) -> bool:
    return _activate_input_profile("wetype", "WeType", _sleep=_sleep)


def _activate_doubao_for_voice_start(
    *, _sleep: Callable[[float], None] = time.sleep
) -> bool:
    return _activate_input_profile("doubao", "Doubao", _sleep=_sleep)


def _cycle_wetype_input_profile(
    *, _sleep: Callable[[float], None] = time.sleep
) -> bool:
    with _InputProfileManager() as manager:
        target = _discover_input_profile(manager, "wetype")
        previous = manager.get_active_profile()
        alternatives = tuple(
            profile
            for profile in manager.enum_profiles(_LANGUAGE_ID_ZH_CN)
            if profile.profile_type == _TF_PROFILETYPE_INPUTPROCESSOR
            and not _same_profile(profile, target)
            and (not profile.flags or profile.flags & _TF_IPP_FLAG_ENABLED)
        )
        if not alternatives:
            raise OSError("no enabled alternative input profile is available")
        if _sta_cancelled():
            raise TimeoutError("WeType input profile cycle was cancelled")
        manager.activate_profile(alternatives[0])
        if _sta_cancelled():
            manager.activate_profile(previous)
            raise TimeoutError("WeType input profile cycle was cancelled")
        _sleep(_SESSION_REBIND_SETTLE_SECONDS)
        if _sta_cancelled():
            manager.activate_profile(previous)
            raise TimeoutError("WeType input profile cycle was cancelled")
        manager.activate_profile(target)
        if _sta_cancelled():
            manager.activate_profile(previous)
            raise TimeoutError("WeType input profile cycle was cancelled")
        _sleep(_SESSION_REBIND_SETTLE_SECONDS)
        if _sta_cancelled():
            manager.activate_profile(previous)
            raise TimeoutError("WeType input profile cycle was cancelled")
        if not _same_profile(manager.get_active_profile(), target):
            manager.activate_profile(previous)
            raise OSError("WeType input profile cycle could not be confirmed")
        return True


_T = TypeVar("_T")


def _run_on_sta_thread(
    callback: Callable[[], _T],
    timeout_seconds: float = _STA_RESULT_TIMEOUT_SECONDS,
) -> _T:
    """Run one TSF operation on a fresh STA thread with a bounded wait."""

    if not _STA_OPERATION_LOCK.acquire(blocking=False):
        _trace("input_profile_sta", phase="rejected", reason="operation_in_progress")
        raise OSError("an input profile STA operation is still in progress")

    result_queue: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)

    cancel_event = threading.Event()

    def run() -> None:
        _trace("input_profile_sta", phase="started")
        try:
            _STA_REQUEST_LOCAL.cancel_event = cancel_event
            if cancel_event.is_set():
                raise TimeoutError("STA request was cancelled before it started")
            value = callback()
            result_queue.put((True, value))
        except BaseException as exc:
            _trace(
                "input_profile_sta",
                phase="callback_failed",
                error_type=type(exc).__name__,
            )
            result_queue.put((False, exc))
        finally:
            try:
                del _STA_REQUEST_LOCAL.cancel_event
            except AttributeError:
                pass
            _STA_OPERATION_LOCK.release()
            _trace("input_profile_sta", phase="finished")

    thread = threading.Thread(
        target=run,
        name="wetype-input-profile-sta",
        daemon=True,
    )
    try:
        thread.start()
    except RuntimeError as exc:
        _STA_OPERATION_LOCK.release()
        _trace(
            "input_profile_sta",
            phase="thread_start_failed",
            error_type=type(exc).__name__,
        )
        raise OSError(f"WeType STA thread could not start: {exc}") from exc
    try:
        succeeded, value = result_queue.get(timeout=timeout_seconds)
    except queue.Empty as exc:
        cancel_event.set()
        _trace(
            "input_profile_sta",
            phase="timeout",
            timeout_ms=int(timeout_seconds * 1000),
            cancelled=True,
        )
        raise TimeoutError(
            f"WeType input profile activation timed out after {timeout_seconds:.3f}s"
        ) from exc
    if succeeded:
        _trace("input_profile_sta", phase="result", success=True)
        return cast(_T, value)
    if isinstance(value, BaseException):
        raise value
    raise OSError("WeType STA operation failed without an exception")


def _wetype_mic_start_timestamp() -> Optional[int]:
    if os.name != "nt":
        return None
    latest = None
    try:
        import winreg

        relative = (
            "Software\\Microsoft\\Windows\\CurrentVersion\\CapabilityAccessManager\\"
            "ConsentStore\\microphone\\NonPackaged"
        )
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, relative) as root:
            index = 0
            while True:
                try:
                    child = winreg.EnumKey(root, index)
                except OSError:
                    break
                index += 1
                if "wetype" not in child.casefold():
                    continue
                try:
                    with winreg.OpenKey(root, child) as key:
                        value, _kind = winreg.QueryValueEx(
                            key, "LastUsedTimeStart"
                        )
                except OSError:
                    continue
                if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                    continue
                # Windows retains records for old installations; enumeration
                # order must not hide microphone activity from a newer version.
                latest = value if latest is None else max(latest, value)
    except (OSError, TypeError, ValueError):
        return None
    return latest


class WeTypeVoiceControl:
    """Own one configured hold session without inspecting WeType windows."""

    def __init__(
        self,
        *,
        logger: Optional[logging.Logger] = None,
        activate_profile: Callable[[], bool] = _activate_wetype_for_voice_start,
        run_sta: Callable[[Callable[[], bool]], bool] = _run_on_sta_thread,
        press_keys: Callable[[Sequence[str]], None] = (
            win32_input.send_wetype_voice_key_combo_down
        ),
        release_keys: Callable[[Sequence[str]], None] = (
            win32_input.send_wetype_voice_key_combo_up
        ),
        on_completion: Optional[Callable[[int, bool], None]] = None,
        on_confirmation: Optional[Callable[[int, bool], None]] = None,
        mic_start_reader: Optional[Callable[[], Optional[int]]] = (
            _wetype_mic_start_timestamp
        ),
        revive_profile: Optional[Callable[[], bool]] = _cycle_wetype_input_profile,
        sleep: Callable[[float], None] = time.sleep,
        thread_factory: Callable[..., threading.Thread] = threading.Thread,
        provider_name: str = "WeType",
        continue_after_activation_error: bool = True,
        prepare_playback_mute_guard: Optional[
            Callable[[], Optional[voice_playback_session_windows.PlaybackMuteGuard]]
        ] = None,
    ) -> None:
        self._logger = logger or logging.getLogger(__name__)
        self._activate_profile = activate_profile
        self._run_sta = run_sta
        self._press_keys = press_keys
        self._release_keys = release_keys
        self._on_completion = on_completion
        self._on_confirmation = on_confirmation
        self._mic_start_reader = mic_start_reader
        self._revive_profile = revive_profile
        self._sleep = sleep
        self._thread_factory = thread_factory
        self._provider_name = str(provider_name)
        self._continue_after_activation_error = bool(
            continue_after_activation_error
        )
        self._prepare_playback_mute_guard = prepare_playback_mute_guard
        self._state_lock = threading.Lock()
        self._io_lock = threading.Lock()
        self._generation = 0
        self._starting_generation: Optional[int] = None
        self._active_generation: Optional[int] = None
        self._active_keys: Optional[tuple[str, ...]] = None
        self._confirmation_generation: Optional[int] = None

    @property
    def current_generation(self) -> int:
        with self._state_lock:
            return self._generation

    @property
    def completion_pending(self) -> bool:
        return False

    @property
    def confirmation_pending(self) -> bool:
        with self._state_lock:
            return self._confirmation_generation is not None

    @property
    def cleanup_pending(self) -> bool:
        with self._state_lock:
            return (
                self._starting_generation is not None
                or self._active_generation is not None
            )

    def _begin_session(self) -> Optional[int]:
        with self._state_lock:
            if (
                self._starting_generation is not None
                or self._active_generation is not None
            ):
                return None
            self._generation += 1
            self._starting_generation = self._generation
            return self._generation

    def _cancel_starting(self, generation: int) -> None:
        with self._state_lock:
            if self._starting_generation == generation:
                self._starting_generation = None

    def _mark_active(self, generation: int, keys: tuple[str, ...]) -> bool:
        with self._state_lock:
            if self._generation != generation:
                return False
            self._starting_generation = None
            self._active_generation = generation
            self._active_keys = keys
            return True

    def _clear_active(self, generation: int) -> None:
        with self._state_lock:
            if self._active_generation == generation:
                self._active_generation = None
                self._active_keys = None
                self._confirmation_generation = None

    def _session_is_active(self, generation: int) -> bool:
        with self._state_lock:
            return self._active_generation == generation

    def _finish_confirmation(self, generation: int, success: bool) -> None:
        callback = None
        with self._state_lock:
            if self._confirmation_generation != generation:
                return
            self._confirmation_generation = None
            callback = self._on_confirmation
        _trace(
            "voice_host_confirmation",
            provider=self._provider_name.casefold(),
            generation=int(generation),
            success=bool(success),
        )
        if callback is not None:
            callback(generation, bool(success))

    @staticmethod
    def _mic_reacted(baseline: Optional[int], current: Optional[int]) -> bool:
        if current is None:
            return False
        return baseline is None or current > baseline

    def _schedule_confirmation(
        self,
        generation: int,
        keys: tuple[str, ...],
        baseline: Optional[int],
        playback_guard: Optional[voice_playback_session_windows.PlaybackMuteGuard] = None,
    ) -> None:
        if self._mic_start_reader is None:
            return
        with self._state_lock:
            if self._active_generation != generation:
                return
            self._confirmation_generation = generation

        def confirm() -> None:
            try:
                confirmation_baseline = baseline
                for retry_index, retry_delay in enumerate(
                    (None, *_WETYPE_RETRY_SETTLE_SECONDS)
                ):
                    if retry_delay is not None:
                        if self._revive_profile is None:
                            break
                        try:
                            revived = self._run_sta(self._revive_profile)
                        except Exception as exc:
                            _trace(
                                "voice_host_recovery",
                                provider=self._provider_name.casefold(),
                                generation=int(generation),
                                retry_index=int(retry_index),
                                success=False,
                                error_type=type(exc).__name__,
                            )
                            self._logger.exception(
                                "%s voice recovery profile cycle failed; "
                                "retrying the shortcut without aborting the hold",
                                self._provider_name,
                            )
                        else:
                            _trace(
                                "voice_host_recovery",
                                provider=self._provider_name.casefold(),
                                generation=int(generation),
                                retry_index=int(retry_index),
                                success=True,
                                profile_cycled=bool(revived),
                            )
                        self._sleep(retry_delay)
                        if not self._session_is_active(generation):
                            _trace(
                                "voice_host_confirmation_check",
                                provider=self._provider_name.casefold(),
                                generation=int(generation),
                                retry_index=int(retry_index),
                                result="cancelled",
                            )
                            return
                        confirmation_baseline = self._mic_start_reader()
                        with self._io_lock:
                            if not self._session_is_active(generation):
                                return
                            self._release_keys(keys)
                            self._press_keys(keys)
                        _trace(
                            "voice_hotkey_retry",
                            provider=self._provider_name.casefold(),
                            generation=int(generation),
                            retry_index=int(retry_index),
                            keys=list(keys),
                        )

                    self._sleep(_WETYPE_CONFIRM_DELAY_SECONDS)
                    if not self._session_is_active(generation):
                        _trace(
                            "voice_host_confirmation_check",
                            provider=self._provider_name.casefold(),
                            generation=int(generation),
                            retry_index=int(retry_index),
                            result="cancelled",
                        )
                        return
                    current = self._mic_start_reader()
                    reacted = self._mic_reacted(confirmation_baseline, current)
                    _trace(
                        "voice_host_confirmation_check",
                        provider=self._provider_name.casefold(),
                        generation=int(generation),
                        retry_index=int(retry_index),
                        baseline=(
                            int(confirmation_baseline)
                            if confirmation_baseline is not None
                            else -1
                        ),
                        current=int(current) if current is not None else -1,
                        result="confirmed" if reacted else "not_confirmed",
                    )
                    if reacted:
                        self._finish_confirmation(generation, True)
                        self._protect_playback_session(generation, playback_guard)
                        return
                self._finish_confirmation(generation, False)
            except Exception as exc:
                _trace(
                    "voice_host_confirmation_check",
                    provider=self._provider_name.casefold(),
                    generation=int(generation),
                    result="failed",
                    error_type=type(exc).__name__,
                )
                self._logger.exception(
                    "%s voice confirmation/retry failed",
                    self._provider_name,
                )
                self._finish_confirmation(generation, False)

        try:
            thread = self._thread_factory(
                target=confirm,
                name="wetype-voice-confirmation",
                daemon=True,
            )
            thread.start()
        except RuntimeError as exc:
            _trace(
                "voice_host_confirmation_check",
                provider=self._provider_name.casefold(),
                generation=int(generation),
                result="thread_start_failed",
                error_type=type(exc).__name__,
            )
            self._logger.exception(
                "%s voice confirmation thread could not start",
                self._provider_name,
            )
            self._finish_confirmation(generation, False)

    def _protect_playback_session(self, generation: int, guard) -> None:
        if guard is None:
            return
        try:
            # A short startup-only window catches host mute after mic confirmation.
            # Never fight a later user mute, and serialize the setter with key-up.
            result = "waiting"
            for index in range(16):
                if index:
                    self._sleep(0.1)
                with self._io_lock:
                    if not self._session_is_active(generation):
                        result = "cancelled"
                        break
                    result = guard.restore_if_muted()
                if result != "waiting":
                    break
            if result == "waiting":
                result = "not_muted"
            _trace(
                "voice_playback_mute_guard",
                provider="wetype",
                generation=int(generation),
                phase="restore",
                endpoint_id=str(guard.endpoint_id),
                process_id=os.getpid(),
                result=result,
            )
            self._logger.info("WeType own playback mute guard: %s", result)
        except Exception as exc:
            _trace(
                "voice_playback_mute_guard",
                generation=int(generation),
                phase="restore",
                result="failed",
                error_type=type(exc).__name__,
            )
            self._logger.exception("WeType own playback mute recovery failed")

    def start(self, keys: Sequence[str]) -> bool:
        session_keys = tuple(str(key) for key in keys)
        if not session_keys:
            _trace(
                "voice_hotkey_control",
                provider=self._provider_name.casefold(),
                phase="start_rejected",
                reason="empty_shortcut",
            )
            self._logger.error(
                "%s voice shortcut start rejected: empty shortcut",
                self._provider_name,
            )
            return False
        generation = self._begin_session()
        if generation is None:
            _trace(
                "voice_hotkey_control",
                provider=self._provider_name.casefold(),
                phase="start_rejected",
                reason="cleanup_pending",
                keys=list(session_keys),
            )
            self._logger.warning(
                "%s voice shortcut start ignored: cleanup is pending",
                self._provider_name,
            )
            return False
        _trace(
            "voice_hotkey_control",
            provider=self._provider_name.casefold(),
            phase="start_requested",
            generation=int(generation),
            keys=list(session_keys),
        )
        baseline = None
        if self._mic_start_reader is not None:
            try:
                baseline = self._mic_start_reader()
            except Exception as exc:
                _trace(
                    "voice_host_baseline",
                    provider=self._provider_name.casefold(),
                    generation=int(generation),
                    available=False,
                    error_type=type(exc).__name__,
                )
                self._logger.exception(
                    "%s microphone baseline could not be read",
                    self._provider_name,
                )
        playback_guard = None
        if self._prepare_playback_mute_guard is not None:
            try:
                playback_guard = self._prepare_playback_mute_guard()
                _trace(
                    "voice_playback_mute_guard",
                    generation=int(generation),
                    phase="baseline",
                    available=playback_guard is not None,
                )
            except Exception as exc:
                _trace(
                    "voice_playback_mute_guard",
                    generation=int(generation),
                    phase="baseline",
                    available=False,
                    error_type=type(exc).__name__,
                )
                self._logger.exception("WeType playback mute baseline unavailable")
        try:
            switched = self._run_sta(self._activate_profile)
        except Exception as exc:
            _trace(
                "voice_hotkey_control",
                provider=self._provider_name.casefold(),
                phase="profile_activation",
                generation=int(generation),
                success=False,
                continued=bool(self._continue_after_activation_error),
                error_type=type(exc).__name__,
            )
            if not self._continue_after_activation_error:
                self._logger.exception(
                    "%s input profile activation failed on STA thread",
                    self._provider_name,
                )
                self._cancel_starting(generation)
                return False
            self._logger.exception(
                "%s input profile activation failed on STA thread; "
                "trying the configured shortcut anyway",
                self._provider_name,
            )
        else:
            _trace(
                "voice_hotkey_control",
                provider=self._provider_name.casefold(),
                phase="profile_activation",
                generation=int(generation),
                success=True,
                switched=bool(switched),
            )
            self._logger.info(
                "%s input profile ready on STA thread switched=%s",
                self._provider_name,
                switched,
            )
        try:
            with self._io_lock:
                self._press_keys(session_keys)
        except win32_input.InputCleanupIncompleteError as exc:
            self._mark_active(generation, session_keys)
            _trace(
                "voice_hotkey_control",
                provider=self._provider_name.casefold(),
                phase="key_down",
                generation=int(generation),
                success=False,
                cleanup_pending=True,
                error_type="InputCleanupIncompleteError",
                system_error=getattr(exc, "winerror", None),
                **diagnostic_trace.foreground_context(),
            )
            self._logger.exception(
                "%s voice shortcut failed and key-up remains pending",
                self._provider_name,
            )
            return False
        except (win32_input.Win32InputUnavailableError, OSError) as exc:
            self._cancel_starting(generation)
            _trace(
                "voice_hotkey_control",
                provider=self._provider_name.casefold(),
                phase="key_down",
                generation=int(generation),
                success=False,
                cleanup_pending=False,
                error_type=type(exc).__name__,
                system_error=getattr(exc, "winerror", None),
                **diagnostic_trace.foreground_context(),
            )
            self._logger.exception(
                "%s voice shortcut could not be delivered", self._provider_name
            )
            return False
        if not self._mark_active(generation, session_keys):
            try:
                self._release_keys(session_keys)
            except OSError:
                self._logger.exception(
                    "%s superseded shortcut could not be released",
                    self._provider_name,
                )
            return False
        _trace(
            "voice_hotkey_control",
            provider=self._provider_name.casefold(),
            phase="key_down",
            generation=int(generation),
            success=True,
            keys=list(session_keys),
        )
        self._logger.info("%s configured voice shortcut pressed", self._provider_name)
        self._schedule_confirmation(generation, session_keys, baseline, playback_guard)
        return True

    def stop(self) -> bool:
        with self._state_lock:
            generation = self._active_generation
            session_keys = self._active_keys
        if generation is None and session_keys is None:
            _trace(
                "voice_hotkey_control",
                provider=self._provider_name.casefold(),
                phase="key_up",
                success=True,
                already_released=True,
            )
            self._logger.info(
                "%s voice shortcut is already released", self._provider_name
            )
            return True
        if generation is None or session_keys is None:
            _trace(
                "voice_hotkey_control",
                provider=self._provider_name.casefold(),
                phase="key_up",
                success=False,
                reason="inconsistent_state",
            )
            self._logger.error(
                "%s voice shortcut session state is inconsistent",
                self._provider_name,
            )
            return False
        try:
            with self._io_lock:
                self._release_keys(session_keys)
                self._clear_active(generation)
        except (win32_input.Win32InputUnavailableError, OSError) as exc:
            _trace(
                "voice_hotkey_control",
                provider=self._provider_name.casefold(),
                phase="key_up",
                generation=int(generation),
                success=False,
                cleanup_pending=True,
                error_type=type(exc).__name__,
                system_error=getattr(exc, "winerror", None),
                **diagnostic_trace.foreground_context(),
            )
            self._logger.exception(
                "%s voice shortcut release failed; cleanup remains pending",
                self._provider_name,
            )
            return False
        _trace(
            "voice_hotkey_control",
            provider=self._provider_name.casefold(),
            phase="key_up",
            generation=int(generation),
            success=True,
            keys=list(session_keys),
        )
        self._logger.info("%s configured voice shortcut released", self._provider_name)
        return True


class DoubaoVoiceControl(WeTypeVoiceControl):
    """Activate Doubao IME and own one configured hold shortcut session."""

    def __init__(
        self,
        *,
        logger: Optional[logging.Logger] = None,
        activate_profile: Callable[[], bool] = _activate_doubao_for_voice_start,
        run_sta: Callable[[Callable[[], bool]], bool] = _run_on_sta_thread,
        press_keys: Callable[[Sequence[str]], None] = win32_input.send_voice_key_combo_down,
        release_keys: Callable[[Sequence[str]], None] = win32_input.send_voice_key_combo_up,
        on_completion: Optional[Callable[[int, bool], None]] = None,
    ) -> None:
        super().__init__(
            logger=logger,
            activate_profile=activate_profile,
            run_sta=run_sta,
            press_keys=press_keys,
            release_keys=release_keys,
            on_completion=on_completion,
            on_confirmation=None,
            mic_start_reader=None,
            revive_profile=None,
            provider_name="Doubao",
            continue_after_activation_error=False,
        )
