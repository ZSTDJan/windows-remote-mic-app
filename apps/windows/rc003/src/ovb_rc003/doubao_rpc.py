"""Small adapters for Doubao's native keyboard paths.

Doubao ignores the ``LLKHF_INJECTED`` flag on ordinary Win32 synthetic
keyboard events.  The native RPC functions are retained as a narrow diagnostic
adapter, while the production voice path uses an optional Frida callback hook
to clear that flag inside Doubao's own low-level keyboard callback.  It is
deliberately lazy: importing the bridge must still work when Doubao is not
installed or when tests run off Windows.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import sys
import threading
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Optional, Sequence, Tuple

from . import diagnostic_trace, voice_key_physicalizer_windows, voice_program_manager


DEFAULT_PIPE = r"\\.\pipe\ObricIme\oime-server"
_RPC_DLL_NAME = "rpc.dll"
_IME_SERVICE_NAME = "ImeService.exe"
_TSF_CORE_NAME = "tsf-oime-core.dll"
VOICE_PRESS_STOP_MESSAGE = 0x3E9
_VERIFIED_IME_SERVICE_BUILDS = {
    "94b17bdca571ac3cd2dafa687b4cb8e3a789cda9d044276483003b0a9e8f77a6": (
        0x7426C0, 0x7427F1, 0x7427F7, 0x7431CD, 0x38
    ),
}
_VERIFIED_VOICE_STOP_BUILDS = {
    "a3ead1a55850257bac01a878c899f42291a1f41bd2b834e0caa5c5b66a674e02": (
        "94b17bdca571ac3cd2dafa687b4cb8e3a789cda9d044276483003b0a9e8f77a6",
        "77d58bfc5bbc9016ee58135967603fbab19a30e79a1c338bce6a2df62ce60a83",
    ),
}
_PHYSICALIZER_READY_TIMEOUT_SECONDS = 2.0
_MARKER_CONFIRM_TIMEOUT_SECONDS = 0.75
_diagnostic_trace: Optional[diagnostic_trace.DiagnosticTrace] = None
_MODIFIER_VKS = frozenset(
    {
        0x10,  # VK_SHIFT
        0x11,  # VK_CONTROL
        0x12,  # VK_MENU
        0x5B,  # VK_LWIN
        0x5C,  # VK_RWIN
        0xA0,  # VK_LSHIFT
        0xA1,  # VK_RSHIFT
        0xA2,  # VK_LCONTROL
        0xA3,  # VK_RCONTROL
        0xA4,  # VK_LMENU
        0xA5,  # VK_RMENU
    }
)


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


def _module_sha256(path: str) -> str:
    """Hash the current module contents at every verification boundary."""

    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _shortcut_vks_to_suppress(vk_codes: Sequence[int]) -> tuple[int, ...]:
    """Return only the final printable/function key that may leak to the app."""

    if len(vk_codes) <= 1:
        return ()
    candidate = int(vk_codes[-1])
    if candidate in _MODIFIER_VKS:
        return ()
    return (candidate,)


def _physicalizer_source(
    vk_codes: Sequence[int],
    callback_rva: int,
    early_forward_rva: int,
    early_continue_rva: int,
    decision_gate_rva: int,
    consumed_stack_offset: int,
) -> str:
    allowed_vks = ", ".join(f"0x{int(vk):02X}" for vk in vk_codes)
    # The final non-modifier edge is the only part that can leak printable
    # text into the foreground application. Doubao's verified callback sees it
    # first, then one of the verified forward gates marks it consumed.
    suppressed_vks = ", ".join(
        f"0x{int(vk):02X}"
        for vk in _shortcut_vks_to_suppress(vk_codes)
    )
    marker_prefix = voice_key_physicalizer_windows.VOICE_EVENT_MARKER_PREFIX
    marker_mask = voice_key_physicalizer_windows.VOICE_EVENT_MARKER_PREFIX_MASK
    sequence_mask = voice_key_physicalizer_windows.VOICE_EVENT_MARKER_SEQUENCE_MASK
    return f"""
const module = Process.getModuleByName('ImeService.exe');
const callback = module.base.add(0x{int(callback_rva):X});
const earlyForward = module.base.add(0x{int(early_forward_rva):X});
const earlyContinue = module.base.add(0x{int(early_continue_rva):X});
const decisionGate = module.base.add(0x{int(decision_gate_rva):X});
const consumedStackOffset = 0x{int(consumed_stack_offset):X};
const allowedVks = new Set([{allowed_vks}]);
const suppressVks = new Set([{suppressed_vks}]);
const pendingByThread = new Map();
const markerPrefix = uint64('0x{marker_prefix:X}');
const markerMask = uint64('0x{marker_mask:X}');
const sequenceMask = uint64('0x{sequence_mask:X}');
function hasBytes(address, expected) {{
  const actual = new Uint8Array(address.readByteArray(expected.length));
  if (actual.length !== expected.length) return false;
  for (let index = 0; index < expected.length; index++) {{
    if (actual[index] !== expected[index]) return false;
  }}
  return true;
}}
if (!hasBytes(earlyForward, [0xFF, 0x15, 0x59, 0x9B, 0x8A, 0x00,
                             0xE9, 0xFB, 0x09, 0x00, 0x00]) ||
    !hasBytes(decisionGate, [0x80, 0x7C, 0x24, 0x38, 0x00, 0x75, 0x14])) {{
  throw new Error('verified Doubao hook layout changed');
}}
function currentFrame() {{
  const stack = pendingByThread.get(Process.getCurrentThreadId());
  if (stack === undefined || stack.length === 0) return null;
  return stack[stack.length - 1];
}}
Interceptor.attach(callback, {{
  onEnter(args) {{
    const threadId = Process.getCurrentThreadId();
    let stack = pendingByThread.get(threadId);
    if (stack === undefined) {{
      stack = [];
      pendingByThread.set(threadId, stack);
    }}
    const frame = {{ payload: null }};
    stack.push(frame);
    this.remoteMicFrame = frame;
    this.remoteMicThread = threadId;
    if (args[0].toInt32() < 0) return;
    const event = args[2];
    const vk = event.readU32();
    const flags = event.add(8).readU32();
    if (!allowedVks.has(vk) || (flags & 0x10) === 0) return;
    const marker = event.add(16).readU64();
    if (marker.and(markerMask).compare(markerPrefix) !== 0) return;
    if (marker.and(sequenceMask).compare(uint64(0)) === 0) return;
    event.add(8).writeU32(flags & ~0x12);
    event.add(16).writeU64(0);
    const payload = {{
      type: 'marker_processed',
      marker: marker.toString(),
      vk: vk,
      message: args[1].toInt32(),
      key_up: (flags & 0x80) !== 0,
      flags_before: flags,
      flags_after: event.add(8).readU32(),
      extra_before_nonzero: true,
      extra_after_zero: event.add(16).readU64().compare(uint64(0)) === 0,
      callback_suppressed: false,
      callback_path: 'native'
    }};
    frame.payload = payload;
  }},
  onLeave(retval) {{
    const frame = this.remoteMicFrame;
    if (frame === undefined) return;
    const stack = pendingByThread.get(this.remoteMicThread);
    if (stack !== undefined) {{
      const index = stack.lastIndexOf(frame);
      if (index >= 0) stack.splice(index, 1);
      if (stack.length === 0) pendingByThread.delete(this.remoteMicThread);
    }}
    if (frame.payload !== null) send(frame.payload);
  }}
}});
Interceptor.attach(earlyForward, {{
  onEnter(args) {{
    const frame = currentFrame();
    if (frame === null || frame.payload === null ||
        !suppressVks.has(frame.payload.vk)) return;
    this.context.rax = ptr(1);
    this.context.pc = earlyContinue;
    frame.payload.callback_suppressed = true;
    frame.payload.callback_path = 'early_bypass';
  }}
}});
Interceptor.attach(decisionGate, {{
  onEnter(args) {{
    const frame = currentFrame();
    if (frame === null || frame.payload === null ||
        !suppressVks.has(frame.payload.vk)) return;
    const consumed = this.context.rsp.add(consumedStackOffset);
    frame.payload.original_consumed = consumed.readU8() !== 0;
    consumed.writeU8(1);
    frame.payload.callback_suppressed = true;
    frame.payload.callback_path = 'normal_decision';
  }}
}});
send({{type: 'ready', callback: callback.toString(),
      early_forward: earlyForward.toString(), decision_gate: decisionGate.toString()}});
"""


class DoubaoRpcError(OSError):
    """Base class for failures talking to Doubao's native RPC client."""


class DoubaoRpcUnavailableError(DoubaoRpcError):
    """Doubao is not installed or its RPC exports are unavailable."""


class DoubaoRpcCallError(DoubaoRpcError):
    """The RPC call was made but Doubao rejected it."""


RpcFunction = Callable[..., int]


class DoubaoPhysicalizer:
    """Clear the injected marker only inside the verified Doubao callback."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._session = None
        self._script = None
        self._frida = None
        self._frida_manager = None
        self._target_pid: Optional[int] = None
        self._vk_codes: tuple[int, ...] = ()
        self._status = "not_started"
        self._error: Optional[str] = None
        self._generation = 0
        self._expectation_sequence = 0
        self._marker_expectations: dict[int, dict[str, Any]] = {}

    @property
    def status(self) -> str:
        with self._lock:
            self._refresh_detached_locked()
            return self._status

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    @property
    def target_pid(self) -> Optional[int]:
        with self._lock:
            self._refresh_detached_locked()
            return self._target_pid

    @property
    def error(self) -> Optional[str]:
        with self._lock:
            return self._error

    def _refresh_detached_locked(self) -> None:
        if self._status != "active" or self._session is None:
            return
        detached = getattr(self._session, "is_detached", False)
        if not (isinstance(detached, bool) and detached):
            return
        # Frida releases every script with the detached session.  Forget only
        # the current generation; a stale session can never invalidate a later
        # successful attach.
        self._session = None
        self._script = None
        self._frida = None
        manager, self._frida_manager = self._frida_manager, None
        if manager is not None:
            try:
                manager._impl.close()
            except Exception:
                self._frida_manager = manager
                self._status = "cleanup_required"
                self._error = "Frida manager close failed"
                return
        self._target_pid = None
        self._vk_codes = ()
        self._status = "detached"
        self._error = "session_detached"
        _trace(
            "doubao_physicalizer_status",
            status="detached",
            success=False,
            error_type="session_detached",
            generation=int(self._generation),
        )

    def is_active_generation(self, generation: int) -> bool:
        with self._lock:
            self._refresh_detached_locked()
            return (
                self._status == "active"
                and self._session is not None
                and self._script is not None
                and self._generation == int(generation)
            )

    def _set_failure(self, status: str, error: BaseException | str) -> bool:
        self._status = status
        # Frida/native exception messages may embed a process path, PID, or
        # address. Persistent logs consume this field, so only fixed strings
        # or an exception type may cross this boundary.
        self._error = error if isinstance(error, str) else type(error).__name__
        _trace(
            "doubao_physicalizer_status",
            status=str(status),
            success=False,
            error_type=(error if isinstance(error, str) else type(error).__name__),
        )
        return False

    @staticmethod
    def _module_probe_source() -> str:
        return (
            "const module = Process.getModuleByName('ImeService.exe');"
            "send({type: 'module', path: module.path, size: module.size});"
        )

    @staticmethod
    def _verified_callback_rva(path: str) -> Optional[int]:
        layout = DoubaoPhysicalizer._verified_hook_layout(path)
        return int(layout[0]) if layout is not None else None

    @staticmethod
    def _verified_hook_layout(path: str) -> Optional[tuple[int, int, int, int, int]]:
        try:
            module_path = Path(path)
            if module_path.name.casefold() != _IME_SERVICE_NAME.casefold():
                return None
            if "doubaoime" not in str(module_path.parent).casefold():
                return None
            digest = _module_sha256(str(module_path))
        except OSError:
            return None
        layout = _VERIFIED_IME_SERVICE_BUILDS.get(digest)
        _trace(
            "doubao_module_verification",
            module_name=module_path.name,
            sha256=digest,
            verified=layout is not None,
            callback_rva=int(layout[0]) if layout is not None else -1,
            early_forward_rva=int(layout[1]) if layout is not None else -1,
            decision_gate_rva=int(layout[3]) if layout is not None else -1,
        )
        return layout

    @staticmethod
    def _verify_module(path: str) -> bool:
        return DoubaoPhysicalizer._verified_callback_rva(path) is not None

    def _probe_module(self, session: Any) -> Optional[str]:
        info: dict[str, Any] = {}
        ready = threading.Event()

        def on_message(message: dict[str, Any], _data: Any) -> None:
            if message.get("type") == "send":
                payload = message.get("payload") or {}
                if payload.get("type") == "module":
                    info.update(payload)
                    ready.set()
            elif message.get("type") == "error":
                ready.set()

        probe = session.create_script(self._module_probe_source())
        probe.on("message", on_message)
        unloaded = False
        try:
            probe.load()
            if not ready.wait(2.0):
                _trace(
                    "doubao_module_probe",
                    success=False,
                    reason="timeout",
                )
                return None
            path = info.get("path")
            _trace(
                "doubao_module_probe",
                success=bool(path),
                module_name=_IME_SERVICE_NAME,
                module_size=int(info.get("size") or 0),
            )
            return str(path) if path else None
        finally:
            try:
                probe.unload()
                unloaded = True
            finally:
                _trace("doubao_probe_cleanup", script_unloaded=unloaded)

    def _finish_marker_expectation(self, token: int, result: str) -> None:
        with self._lock:
            expectation = self._marker_expectations.pop(int(token), None)
            if expectation is None:
                return
            timer = expectation.get("timer")
            if result != "timeout" and timer is not None:
                timer.cancel()
        _trace(
            "doubao_marker_expectation",
            phase=str(expectation["phase"]),
            expected=int(expectation["expected"]),
            received=int(expectation["received"]),
            result=str(result),
            **dict(expectation.get("context") or {}),
        )

    def expect_markers(self, phase: str, count: int) -> Optional[int]:
        """Observe target callback delivery without delaying the hotkey path."""

        expected = 0
        try:
            expected = max(0, int(count))
            if expected == 0:
                return None
            with self._lock:
                if self._status != "active" or self._script is None:
                    _trace(
                        "doubao_marker_expectation",
                        phase=str(phase),
                        expected=expected,
                        received=0,
                        result="unavailable",
                    )
                    return None
                self._expectation_sequence += 1
                token = self._expectation_sequence
                context = (
                    dict(_diagnostic_trace.current_context())
                    if _diagnostic_trace is not None
                    else {}
                )
                timer = threading.Timer(
                    _MARKER_CONFIRM_TIMEOUT_SECONDS,
                    self._finish_marker_expectation,
                    args=(token, "timeout"),
                )
                timer.daemon = True
                self._marker_expectations[token] = {
                    "phase": str(phase),
                    "expected": expected,
                    "received": 0,
                    "context": context,
                    "timer": timer,
                }
            _trace(
                "doubao_marker_expectation",
                phase=str(phase),
                expected=expected,
                received=0,
                result="started",
                **context,
            )
            timer.start()
            return token
        except BaseException as exc:
            with self._lock:
                if "token" in locals():
                    self._marker_expectations.pop(token, None)
            _trace(
                "doubao_marker_expectation",
                phase=str(phase),
                expected=expected,
                received=0,
                result="observer_failed",
                error_type=type(exc).__name__,
            )
            return None

    def _on_marker_processed(self, payload: dict[str, Any]) -> None:
        key_up = bool(payload.get("key_up", False))
        phase = "up" if key_up else "down"
        completed: list[int] = []
        marker_context: dict[str, object] = {}
        with self._lock:
            for token, expectation in self._marker_expectations.items():
                if expectation["phase"] != phase:
                    continue
                marker_context.update(expectation.get("context") or {})
                expectation["received"] = int(expectation["received"]) + 1
                if int(expectation["received"]) >= int(expectation["expected"]):
                    completed.append(token)
                break
        _trace(
            "doubao_marker_processed",
            marker=str(payload.get("marker") or ""),
            vk=int(payload.get("vk") or 0),
            message=int(payload.get("message") or 0),
            key_up=key_up,
            flags_before=int(payload.get("flags_before") or 0),
            flags_after=int(payload.get("flags_after") or 0),
            extra_before_nonzero=bool(payload.get("extra_before_nonzero", False)),
            extra_after_zero=bool(payload.get("extra_after_zero", False)),
            callback_suppressed=bool(payload.get("callback_suppressed", False)),
            callback_path=str(payload.get("callback_path") or "unknown"),
            original_consumed=bool(payload.get("original_consumed", False)),
            **marker_context,
        )
        for token in completed:
            self._finish_marker_expectation(token, "confirmed")

    @staticmethod
    def _normalize_vk_codes(vk_codes: Sequence[int]) -> tuple[int, ...]:
        return tuple(dict.fromkeys(int(vk) for vk in vk_codes if 0 < int(vk) <= 0xFF))

    def start(self, vk_codes: Sequence[int]) -> bool:
        with self._lock:
            normalized_vks = self._normalize_vk_codes(vk_codes)
            _trace(
                "doubao_physicalizer_start",
                phase="requested",
                vk_codes=list(normalized_vks),
            )
            if not normalized_vks:
                return self._set_failure("invalid_hotkey", "Doubao hotkey has no valid keys")
            if (
                self._status == "active"
                and self._script is not None
                and self._session is not None
            ):
                detached = getattr(self._session, "is_detached", False)
                if not (isinstance(detached, bool) and detached):
                    if self._vk_codes == normalized_vks:
                        return True
                    try:
                        self._stop_locked()
                    except RuntimeError:
                        return False
                else:
                    self._session = None
                    self._script = None
                    self._frida = None
                    manager, self._frida_manager = self._frida_manager, None
                    if manager is not None:
                        try:
                            manager._impl.close()
                        except Exception:
                            self._frida_manager = manager
                            return self._set_failure(
                                "cleanup_required", "Frida manager close failed"
                            )
                    self._target_pid = None
                    self._vk_codes = ()
                    self._status = "starting"
            if (self._script is not None or self._session is not None
                    or self._frida_manager is not None):
                return self._set_failure(
                    "cleanup_required",
                    "retained Frida resources require stop() before restart",
                )
            self._status = "starting"
            self._error = None

            if sys.platform != "win32":
                return self._set_failure("unavailable", "Doubao physicalizer is Windows-only")
            try:
                import frida  # type: ignore[import-not-found]
            except ImportError as exc:
                return self._set_failure("unavailable", "Python frida package is not installed")

            try:
                # Frida's process list can omit a running ImeService.exe. Reuse
                # Windows discovery; every PID still passes the loaded-module
                # path and build-hash checks below before a hook is installed.
                processes = voice_program_manager._iter_windows_processes()
                candidates = [
                    process
                    for process in processes
                    if str(process.name).casefold() == _IME_SERVICE_NAME.casefold()
                ]
                _trace(
                    "doubao_process_candidates",
                    discovery="windows_toolhelp32",
                    count=len(candidates),
                    pids=[int(process.pid) for process in candidates],
                )
                if not candidates:
                    return self._set_failure("unavailable", "ImeService.exe is not running")
                last_error: Optional[Exception] = None
                for process in candidates:
                    session = None
                    script = None
                    fresh_manager = None
                    try:
                        _trace(
                            "doubao_attach",
                            phase="started",
                            pid=int(process.pid),
                        )
                        try:
                            session = frida.attach(process.pid)
                        except Exception as attach_error:
                            if type(attach_error).__name__ not in {
                                "TransportError", "InvalidArgumentError"
                            }:
                                raise
                            # A failed attach can leave Frida's process-wide
                            # device manager unusable after a profile switch.
                            # A separate manager is owned until this session
                            # has been detached; never retry another failure.
                            _trace(
                                "doubao_attach_recovery",
                                phase="started",
                                error_type=type(attach_error).__name__,
                            )
                            fresh_manager = frida.core.DeviceManager(
                                frida._frida.DeviceManager()
                            )
                            session = fresh_manager.get_local_device().attach(
                                process.pid
                            )
                        _trace(
                            "doubao_attach",
                            phase="finished",
                            pid=int(process.pid),
                            success=True,
                        )
                        module_path = self._probe_module(session)
                        hook_layout = (
                            self._verified_hook_layout(module_path)
                            if module_path
                            else None
                        )
                        if hook_layout is None:
                            raise DoubaoRpcUnavailableError(
                                "ImeService.exe version is not verified"
                            )
                        (
                            callback_rva,
                            early_forward_rva,
                            early_continue_rva,
                            decision_gate_rva,
                            consumed_stack_offset,
                        ) = hook_layout
                        ready = threading.Event()
                        script_error = []

                        def on_message(message: dict[str, Any], _data: Any) -> None:
                            if message.get("type") == "send":
                                payload = message.get("payload") or {}
                                if payload.get("type") == "ready":
                                    ready.set()
                                elif payload.get("type") == "marker_processed":
                                    self._on_marker_processed(payload)
                            elif message.get("type") == "error":
                                script_error.append("script_error")
                                _trace(
                                    "doubao_hook_message",
                                    message_type="error",
                                    error_type="script_error",
                                )
                                ready.set()

                        script = session.create_script(
                            _physicalizer_source(
                                normalized_vks,
                                callback_rva,
                                early_forward_rva,
                                early_continue_rva,
                                decision_gate_rva,
                                consumed_stack_offset,
                            )
                        )
                        script.on("message", on_message)
                        script.load()
                        _trace(
                            "doubao_hook_load",
                            phase="loaded",
                            pid=int(process.pid),
                        )
                        if not ready.wait(_PHYSICALIZER_READY_TIMEOUT_SECONDS):
                            _trace(
                                "doubao_hook_ready",
                                success=False,
                                reason="timeout",
                                timeout_ms=int(
                                    _PHYSICALIZER_READY_TIMEOUT_SECONDS * 1000
                                ),
                            )
                            raise TimeoutError("Doubao callback hook did not report ready")
                        if script_error:
                            raise RuntimeError("Doubao callback hook failed to load")
                        self._frida = frida
                        self._frida_manager = fresh_manager
                        self._session = session
                        self._script = script
                        self._target_pid = int(process.pid)
                        self._vk_codes = normalized_vks
                        self._generation += 1
                        self._status = "active"
                        _trace(
                            "doubao_hook_ready",
                            success=True,
                            pid=int(process.pid),
                            callback_rva=int(callback_rva),
                            early_forward_rva=int(early_forward_rva),
                            decision_gate_rva=int(decision_gate_rva),
                        )
                        return True
                    except BaseException as exc:  # noqa: BLE001 - optional integration
                        _trace(
                            "doubao_attach",
                            phase="failed",
                            pid=int(process.pid),
                            success=False,
                            error_type=type(exc).__name__,
                        )
                        script_unload_failed = False
                        if script is not None:
                            try:
                                script.unload()
                            except Exception:
                                script_unload_failed = True
                        session_detach_failed = False
                        if session is not None:
                            try:
                                session.detach()
                            except Exception:
                                self._session = session
                                session_detach_failed = True
                            else:
                                # A detached Frida session no longer owns any
                                # of its scripts, even if a prior explicit
                                # script.unload() call reported an error.
                                script_unload_failed = False
                        if script_unload_failed:
                            self._script = script
                        cleanup_failed = script_unload_failed or session_detach_failed
                        if fresh_manager is not None:
                            if cleanup_failed:
                                self._frida_manager = fresh_manager
                            else:
                                try:
                                    fresh_manager._impl.close()
                                except Exception:
                                    self._frida_manager = fresh_manager
                                    cleanup_failed = True
                        _trace(
                            "doubao_start_cleanup",
                            pid=int(process.pid),
                            script_unloaded=not script_unload_failed,
                            session_detached=not session_detach_failed,
                            success=not cleanup_failed,
                        )
                        if cleanup_failed:
                            self._frida = frida
                            if not isinstance(exc, Exception):
                                raise
                            return self._set_failure(
                                "cleanup_required",
                                "failed Frida startup resources remain owned",
                            )
                        if not isinstance(exc, Exception):
                            raise
                        last_error = exc
                status = (
                    "unsupported_version"
                    if isinstance(last_error, DoubaoRpcUnavailableError)
                    else "unavailable"
                )
                return self._set_failure(
                    status,
                    last_error or "could not attach to ImeService.exe",
                )
            except Exception as exc:  # noqa: BLE001 - optional integration
                return self._set_failure("unavailable", exc)

    def _stop_locked(self) -> None:
        for expectation in self._marker_expectations.values():
            timer = expectation.get("timer")
            if timer is not None:
                timer.cancel()
        cancelled_expectations = list(self._marker_expectations)
        self._marker_expectations.clear()
        script, session = self._script, self._session
        failures = []
        script_unload_failed = False
        if script is not None:
            try:
                script.unload()
            except Exception:
                script_unload_failed = True
            else:
                self._script = None
        if session is not None:
            try:
                session.detach()
            except Exception:
                failures.append("session detach failed")
            else:
                self._session = None
                self._script = None
                script_unload_failed = False
        if script_unload_failed:
            failures.append("script unload failed")
        if self._session is None and self._frida_manager is not None:
            try:
                self._frida_manager._impl.close()
            except Exception:
                failures.append("Frida manager close failed")
            else:
                self._frida_manager = None
        if failures:
            self._status = "cleanup_required"
            self._error = "; ".join(failures)
            _trace(
                "doubao_physicalizer_stop",
                success=False,
                script_unloaded=not script_unload_failed,
                session_detached=self._session is None,
                cancelled_expectations=len(cancelled_expectations),
            )
            raise RuntimeError("Doubao physicalizer cleanup incomplete")
        self._frida = None
        self._vk_codes = ()
        self._target_pid = None
        self._status = "stopped"
        self._error = None
        _trace(
            "doubao_physicalizer_stop",
            success=True,
            script_unloaded=True,
            session_detached=True,
            cancelled_expectations=len(cancelled_expectations),
        )

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()


_physicalizer = DoubaoPhysicalizer()


def start_physicalizer(vk_codes: Sequence[int]) -> bool:
    """Install the verified in-process Doubao callback filter if possible."""

    return _physicalizer.start(vk_codes)


def stop_physicalizer() -> None:
    """Remove the optional Doubao callback filter."""

    _physicalizer.stop()


def physicalizer_status() -> str:
    return _physicalizer.status


def physicalizer_error() -> Optional[str]:
    return _physicalizer.error


def _candidate_dll_paths() -> Tuple[str, ...]:
    roots = []
    for variable in ("ProgramFiles", "ProgramW6432"):
        root = os.environ.get(variable)
        if root:
            roots.append(Path(root) / "DoubaoIME")
    roots.append(Path(r"C:\Program Files") / "DoubaoIME")

    paths = []
    for root in dict.fromkeys(roots):
        paths.append(str(root / _RPC_DLL_NAME))
        versions = root / "versions"
        try:
            version_directories = [item for item in versions.iterdir() if item.is_dir()]
        except OSError:
            continue

        def version_key(path: Path) -> tuple[int, ...]:
            try:
                return tuple(
                    int(part)
                    for part in path.name.removeprefix("v").split(".")
                )
            except ValueError:
                return ()

        for version in sorted(version_directories, key=version_key, reverse=True):
            paths.append(str(version / _RPC_DLL_NAME))
    return tuple(dict.fromkeys(paths))


def _resolve_rpc_dll_path() -> str:
    dll_path = next(
        (path for path in _candidate_dll_paths() if os.path.isfile(path)),
        None,
    )
    if dll_path is None:
        raise DoubaoRpcUnavailableError("Doubao rpc.dll was not found")
    return dll_path


def _verified_voice_stop_build(dll_path: str) -> bool:
    try:
        path = Path(dll_path)
        if (
            path.name.casefold() != _RPC_DLL_NAME.casefold()
            or "doubaoime" not in str(path.parent).casefold()
        ):
            return False
        expected = _VERIFIED_VOICE_STOP_BUILDS.get(_module_sha256(str(path)))
        if expected is None:
            return False
        service_hash, tsf_hash = expected
        return (
            _module_sha256(str(path.with_name(_IME_SERVICE_NAME))) == service_hash
            and _module_sha256(str(path.with_name(_TSF_CORE_NAME))) == tsf_hash
        )
    except OSError:
        return False


def _configure_function(
    library: Any,
    name: str,
    argtypes: Tuple[Any, ...],
) -> RpcFunction:
    try:
        function = getattr(library, name)
    except AttributeError as exc:
        raise DoubaoRpcUnavailableError(f"Doubao rpc.dll is missing {name}") from exc
    function.argtypes = argtypes
    function.restype = ctypes.c_int32
    return function


@lru_cache(maxsize=1)
def _load_api() -> Tuple[RpcFunction, RpcFunction]:
    if sys.platform != "win32":
        raise DoubaoRpcUnavailableError("Doubao RPC is Windows-only")

    loader = getattr(ctypes, "WinDLL", None)
    if loader is None:
        raise DoubaoRpcUnavailableError("ctypes.WinDLL is unavailable")

    dll_path = _resolve_rpc_dll_path()
    try:
        library = loader(dll_path)
    except OSError as exc:
        raise DoubaoRpcUnavailableError(f"could not load Doubao rpc.dll: {exc}") from exc

    key_down = _configure_function(
        library,
        "RpcPipe_KeyDown",
        (ctypes.c_char_p, ctypes.c_uint32, ctypes.c_uint64, ctypes.c_char_p),
    )
    key_up = _configure_function(
        library,
        "RpcPipe_KeyUp",
        (ctypes.c_char_p, ctypes.c_uint32),
    )
    return key_down, key_up


def _load_voice_stop_api() -> RpcFunction:
    if sys.platform != "win32":
        raise DoubaoRpcUnavailableError("Doubao RPC is Windows-only")

    loader = getattr(ctypes, "WinDLL", None)
    if loader is None:
        raise DoubaoRpcUnavailableError("ctypes.WinDLL is unavailable")

    dll_path = _resolve_rpc_dll_path()
    if not _verified_voice_stop_build(dll_path):
        raise DoubaoRpcUnavailableError(
            "Doubao voice-stop RPC build is not verified"
        )
    try:
        library = loader(dll_path)
    except OSError as exc:
        raise DoubaoRpcUnavailableError(
            f"could not load Doubao rpc.dll: {exc}"
        ) from exc
    return _configure_function(
        library,
        "RpcPipe_SimpleMessageEx",
        (
            ctypes.c_char_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_char_p,
        ),
    )


def clear_cached_api() -> None:
    """Clear the lazy DLL handle, primarily for tests and app restarts."""

    _load_api.cache_clear()


def send_voice_press_stop(
    *,
    endpoint: str = DEFAULT_PIPE,
    context: Optional[bytes] = None,
) -> None:
    """Ask the verified Doubao service to finalize active voice input.

    ``VOICE_PRESS_STOP`` is Doubao's own normal-stop message.  The input
    method's registered TSF notification sink remains responsible for
    committing the finalized text into the focused editor.
    """

    stop = _load_voice_stop_api()
    try:
        result = stop(
            endpoint.encode("ascii"),
            VOICE_PRESS_STOP_MESSAGE,
            0,
            0,
            context,
        )
    except (OSError, ValueError) as exc:
        raise DoubaoRpcCallError(
            f"Doubao voice-stop RPC failed: {exc}"
        ) from exc
    if int(result) != 0:
        raise DoubaoRpcCallError(
            f"Doubao voice-stop RPC returned nonzero status {int(result)}"
        )


def send_key_edge(
    vk_code: int,
    key_up: bool,
    *,
    endpoint: str = DEFAULT_PIPE,
    event_value: int = 0,
    context: Optional[bytes] = None,
) -> None:
    """Send one virtual-key edge through Doubao's own RPC input path.

    ``RpcPipe_KeyDown`` and ``RpcPipe_KeyUp`` return zero on the successful
    path.  A nonzero result is treated as a hard failure so callers do not
    silently mix a partially delivered RPC hold with a Win32 fallback.
    """

    key_down, key_up_function = _load_api()
    endpoint_bytes = endpoint.encode("ascii")
    try:
        if key_up:
            result = key_up_function(endpoint_bytes, int(vk_code))
        else:
            result = key_down(
                endpoint_bytes,
                int(vk_code),
                int(event_value),
                context,
            )
    except (OSError, ValueError) as exc:
        raise DoubaoRpcCallError(f"Doubao RPC key edge failed: {exc}") from exc

    if int(result) != 0:
        operation = "up" if key_up else "down"
        raise DoubaoRpcCallError(
            f"Doubao RPC key-{operation} returned nonzero status {int(result)}"
        )
