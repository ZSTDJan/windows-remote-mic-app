"""Bounded RC003 On-request voice probe.

This diagnostic is deliberately separate from the production bridge. It
does not start HID capture, inject a host shortcut, open PortAudio, or write
decoded voice content. A narrow low-level guard swallows the RC003 microphone
button's legacy F5 leak while the probe is running. The probe negotiates ATVV
On-request only, waits for the physical remote to send START_SEARCH, replies
with MIC_OPEN, and records privacy-safe control/PCM counts in the normal log
directory.
"""

from __future__ import annotations

import asyncio
import ctypes
from ctypes import wintypes
from dataclasses import dataclass, field
from enum import Enum
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Callable, Optional

from . import atvv_protocol as proto
from . import atvv_session
from . import ble_transport_winrt
from . import identity
from . import legacy_key_suppressor_windows
from . import logging_setup
from . import single_instance

ON_REQUEST_PROBE_FLAG = "--on-request-probe"
PROBE_RESULT_FILENAME = "on-request-probe-result.json"
PROBE_SCHEMA_VERSION = 1
CAPABILITIES_TIMEOUT_SECONDS = 10.0
PROBE_TIMEOUT_SECONDS = 18.0
SECOND_PRESS_MIN_SECONDS = 0.75
SUSTAINED_PCM_THRESHOLD_SECONDS = 1.0
FINAL_DRAIN_SECONDS = 1.0
STARTUP_HARD_TIMEOUT_SECONDS = 32.0
POST_READY_HARD_TIMEOUT_SECONDS = 25.0

PROBE_COMPLETED_EXIT_CODE = 0
PROBE_BLOCKED_EXIT_CODE = 21
PROBE_FAILED_EXIT_CODE = 22

_TITLE = "Remote Mic RC003 开关型语音诊断"


class ProbeAction(Enum):
    NONE = "none"
    SEND_OPEN = "send_open"
    SEND_CLOSE = "send_close"


ClockFn = Callable[[], float]
NoticeFn = Callable[[str, str], None]
ProbeRunner = Callable[[NoticeFn], dict]


class ProbeStartupTimeoutError(TimeoutError):
    """The probe did not reach the ready notice before the hard deadline."""


class ProbeCompletionTimeoutError(TimeoutError):
    """The probe did not finish after its ready notice was dismissed."""


@dataclass
class OnRequestProbeState:
    """Thread-safe, transport-independent evidence collector."""

    clock: ClockFn = time.monotonic
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    armed: bool = False
    capabilities: Optional[proto.ATVVCapabilities] = None
    terminal_outcome: Optional[str] = None
    start_search_count: int = 0
    audio_start_count: int = 0
    audio_stop_count: int = 0
    pcm_batches: int = 0
    pcm_samples: int = 0
    mic_open_audio_start_count: int = 0
    mic_open_pcm_batches: int = 0
    mic_open_pcm_samples: int = 0
    mic_open_pcm_batches_after_threshold: int = 0
    mic_open_pcm_samples_after_threshold: int = 0
    first_start_search_at: Optional[float] = None
    first_audio_start_at: Optional[float] = None
    first_mic_open_audio_start_at: Optional[float] = None
    first_pcm_at: Optional[float] = None
    last_pcm_at: Optional[float] = None
    open_sent: bool = False
    close_sent: bool = False
    first_audio_start_reason: Optional[int] = None
    first_audio_start_codec: Optional[int] = None
    first_audio_stream_id: Optional[int] = None
    last_audio_stop_reason: Optional[int] = None
    _mic_open_stream_active: bool = False

    def note_capabilities(self, capabilities: proto.ATVVCapabilities) -> None:
        with self._lock:
            self.capabilities = capabilities

    def arm(self) -> None:
        with self._lock:
            self.armed = True
            self.terminal_outcome = None
            self.start_search_count = 0
            self.audio_start_count = 0
            self.audio_stop_count = 0
            self.pcm_batches = 0
            self.pcm_samples = 0
            self.mic_open_audio_start_count = 0
            self.mic_open_pcm_batches = 0
            self.mic_open_pcm_samples = 0
            self.mic_open_pcm_batches_after_threshold = 0
            self.mic_open_pcm_samples_after_threshold = 0
            self.first_start_search_at = None
            self.first_audio_start_at = None
            self.first_mic_open_audio_start_at = None
            self.first_pcm_at = None
            self.last_pcm_at = None
            self.open_sent = False
            self.close_sent = False
            self.first_audio_start_reason = None
            self.first_audio_start_codec = None
            self.first_audio_stream_id = None
            self.last_audio_stop_reason = None
            self._mic_open_stream_active = False

    def mark_terminal(self, outcome: str) -> None:
        with self._lock:
            self.terminal_outcome = outcome

    def handle_control(self, event: object) -> ProbeAction:
        if isinstance(event, atvv_session.CapsReceived):
            self.note_capabilities(event.capabilities)
            return ProbeAction.NONE

        with self._lock:
            if not self.armed:
                return ProbeAction.NONE
            now = self.clock()
            if isinstance(event, atvv_session.MicButtonPressed):
                self.start_search_count += 1
                if not self.open_sent:
                    self.first_start_search_at = now
                    self.open_sent = True
                    return ProbeAction.SEND_OPEN
                if (
                    not self.close_sent
                    and self.first_start_search_at is not None
                    and now - self.first_start_search_at >= SECOND_PRESS_MIN_SECONDS
                ):
                    self.close_sent = True
                    return ProbeAction.SEND_CLOSE
                return ProbeAction.NONE

            if isinstance(event, atvv_session.AudioStarted):
                self.audio_start_count += 1
                if self.first_audio_start_at is None:
                    self.first_audio_start_at = now
                    self.first_audio_start_reason = event.reason
                    self.first_audio_start_codec = event.codec
                    self.first_audio_stream_id = event.session_id
                self._mic_open_stream_active = (
                    event.reason == 0x00 and event.session_id == 0x00
                )
                if self._mic_open_stream_active:
                    self.mic_open_audio_start_count += 1
                    if self.first_mic_open_audio_start_at is None:
                        self.first_mic_open_audio_start_at = now
                return ProbeAction.NONE

            if isinstance(event, atvv_session.AudioStopped):
                self.audio_stop_count += 1
                self.last_audio_stop_reason = event.reason
                self._mic_open_stream_active = False
            return ProbeAction.NONE

    def handle_pcm(self, samples: list[int]) -> None:
        if not samples:
            return
        with self._lock:
            if not self.armed:
                return
            now = self.clock()
            self.pcm_batches += 1
            self.pcm_samples += len(samples)
            if self.first_pcm_at is None:
                self.first_pcm_at = now
            self.last_pcm_at = now
            if self._mic_open_stream_active:
                self.mic_open_pcm_batches += 1
                self.mic_open_pcm_samples += len(samples)
                if (
                    self.first_mic_open_audio_start_at is not None
                    and now - self.first_mic_open_audio_start_at
                    >= SUSTAINED_PCM_THRESHOLD_SECONDS
                ):
                    self.mic_open_pcm_batches_after_threshold += 1
                    self.mic_open_pcm_samples_after_threshold += len(samples)

    def snapshot(self) -> dict:
        with self._lock:
            if self.terminal_outcome is not None:
                outcome = self.terminal_outcome
            elif self.start_search_count == 0:
                outcome = "no_start_search"
            elif self.audio_start_count == 0:
                outcome = "no_audio_start"
            elif self.mic_open_audio_start_count == 0:
                outcome = "no_mic_open_audio_start"
            elif self.mic_open_pcm_batches == 0:
                outcome = "control_only_no_pcm"
            elif self.mic_open_pcm_batches_after_threshold == 0:
                outcome = "pcm_did_not_continue_past_1000ms"
            else:
                outcome = "pcm_continued_past_1000ms"

            capabilities = self.capabilities
            return {
                "schema": PROBE_SCHEMA_VERSION,
                "probe": "rc003_on_request",
                "outcome": outcome,
                "capabilities": None
                if capabilities is None
                else {
                    "version": capabilities.version,
                    "interaction": capabilities.interaction,
                    "frame_size": capabilities.frame_size,
                    "selected_codec": capabilities.selected_codec,
                    "sample_rate": capabilities.sample_rate,
                },
                "events": {
                    "start_search_count": self.start_search_count,
                    "audio_start_count": self.audio_start_count,
                    "audio_stop_count": self.audio_stop_count,
                    "mic_open_audio_start_count": self.mic_open_audio_start_count,
                    "open_sent": self.open_sent,
                    "close_sent": self.close_sent,
                    "first_audio_start_reason": self.first_audio_start_reason,
                    "first_audio_start_codec": self.first_audio_start_codec,
                    "first_audio_stream_id": self.first_audio_stream_id,
                    "last_audio_stop_reason": self.last_audio_stop_reason,
                },
                "pcm": {
                    "callback_batches": self.pcm_batches,
                    "samples": self.pcm_samples,
                    "mic_open_callback_batches": self.mic_open_pcm_batches,
                    "mic_open_samples": self.mic_open_pcm_samples,
                    "mic_open_batches_after_1000ms": (
                        self.mic_open_pcm_batches_after_threshold
                    ),
                    "mic_open_samples_after_1000ms": (
                        self.mic_open_pcm_samples_after_threshold
                    ),
                    "first_pcm_ms_after_start_search": self._offset_ms(
                        self.first_pcm_at
                    ),
                    "last_pcm_ms_after_start_search": self._offset_ms(
                        self.last_pcm_at
                    ),
                },
            }

    def _offset_ms(self, timestamp: Optional[float]) -> Optional[int]:
        if timestamp is None or self.first_start_search_at is None:
            return None
        return max(
            0,
            int(round((timestamp - self.first_start_search_at) * 1000.0)),
        )


def probe_result_path(root: Optional[Path] = None) -> Path:
    return logging_setup.log_dir(root) / PROBE_RESULT_FILENAME


def write_probe_result(path: Path, result: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def _show_notice(title: str, message: str) -> None:
    if os.name != "nt":
        print(f"{title}: {message}")
        return
    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    user32.MessageBoxW.argtypes = (
        wintypes.HWND,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.UINT,
    )
    user32.MessageBoxW.restype = ctypes.c_int
    mb_ok = 0x00000000
    mb_icon_information = 0x00000040
    mb_system_modal = 0x00001000
    user32.MessageBoxW(
        None,
        message,
        title,
        mb_ok | mb_icon_information | mb_system_modal,
    )


def _open_result_directory(path: Path) -> None:
    startfile = getattr(os, "startfile", None)
    if startfile is not None:
        startfile(str(path.parent))


def _run_probe_sync(show_notice: NoticeFn) -> dict:
    return asyncio.run(run_probe(show_notice=show_notice))


def _run_probe_with_hard_deadlines(
    probe_runner: ProbeRunner,
    show_notice: NoticeFn,
    *,
    startup_timeout: float,
    post_ready_timeout: float,
) -> dict:
    """Run the probe without letting an uncooperative WinRT call own main."""

    logger = logging_setup.get_logger().getChild("on_request_probe")
    finished = threading.Event()
    ready_notice_started = threading.Event()
    ready_notice_returned = threading.Event()
    expired = threading.Event()
    outcome: dict[str, object] = {}

    def supervised_notice(title: str, message: str) -> None:
        if expired.is_set():
            return
        ready_notice_started.set()
        if expired.is_set():
            return
        try:
            show_notice(title, message)
        finally:
            ready_notice_returned.set()

    def worker() -> None:
        try:
            outcome["result"] = probe_runner(supervised_notice)
        except BaseException as exc:  # noqa: BLE001 - re-raised on main thread
            outcome["error"] = exc
        finally:
            finished.set()

    thread = threading.Thread(
        target=worker,
        name="rc003-on-request-probe",
        daemon=True,
    )
    thread.start()

    startup_deadline = time.monotonic() + max(0.0, startup_timeout)
    while not finished.is_set() and not ready_notice_started.is_set():
        remaining = startup_deadline - time.monotonic()
        if remaining <= 0:
            expired.set()
            logger.info(
                "on-request probe hard timeout: phase=startup timeout=%.3fs",
                startup_timeout,
            )
            raise ProbeStartupTimeoutError("probe startup hard timeout")
        finished.wait(min(remaining, 0.05))

    if finished.is_set():
        error = outcome.get("error")
        if isinstance(error, BaseException):
            raise error
        return outcome["result"]  # type: ignore[return-value]

    # The ready notice is intentionally user-controlled. The completion
    # deadline starts only after the user dismisses that notice.
    while not finished.is_set() and not ready_notice_returned.is_set():
        finished.wait(0.05)

    if finished.is_set():
        error = outcome.get("error")
        if isinstance(error, BaseException):
            raise error
        return outcome["result"]  # type: ignore[return-value]

    completion_deadline = time.monotonic() + max(0.0, post_ready_timeout)
    remaining = completion_deadline - time.monotonic()
    if remaining > 0:
        finished.wait(remaining)
    if not finished.is_set():
        expired.set()
        logger.info(
            "on-request probe hard timeout: phase=completion timeout=%.3fs",
            post_ready_timeout,
        )
        raise ProbeCompletionTimeoutError("probe completion hard timeout")

    error = outcome.get("error")
    if isinstance(error, BaseException):
        raise error
    return outcome["result"]  # type: ignore[return-value]


def _create_f5_suppressor() -> legacy_key_suppressor_windows.LegacyKeySuppressor:
    # The probe has no HID report tap, so the remote's microphone key would
    # otherwise reach the foreground app as F5 (Notepad inserts the current
    # date/time). The guard is intentionally active only for this short-lived
    # diagnostic and never replaces F5 with another host shortcut.
    return legacy_key_suppressor_windows.LegacyKeySuppressor({0x74})


async def run_probe(
    *,
    show_notice: Callable[[str, str], None] = _show_notice,
    discover_candidates=ble_transport_winrt.discover_candidates,
    select_candidate=ble_transport_winrt.select_connectable_candidate,
    session_factory=ble_transport_winrt.RC003BleSession,
    clock: ClockFn = time.monotonic,
    capabilities_timeout: float = CAPABILITIES_TIMEOUT_SECONDS,
    probe_timeout: float = PROBE_TIMEOUT_SECONDS,
    final_drain_seconds: float = FINAL_DRAIN_SECONDS,
) -> dict:
    logger = logging_setup.get_logger().getChild("on_request_probe")
    loop = asyncio.get_running_loop()
    state = OnRequestProbeState(clock=clock)
    capabilities_ready = asyncio.Event()
    probe_finished = asyncio.Event()
    finish_scheduled = False
    session_holder: dict[str, ble_transport_winrt.RC003BleSession] = {}

    def schedule_finish(delay: float) -> None:
        def schedule_on_loop() -> None:
            nonlocal finish_scheduled
            if finish_scheduled or probe_finished.is_set():
                return
            finish_scheduled = True
            loop.call_later(delay, probe_finished.set)

        loop.call_soon_threadsafe(schedule_on_loop)

    def on_control_event(event: object) -> None:
        action = state.handle_control(event)
        if isinstance(event, atvv_session.CapsReceived):
            caps = event.capabilities
            logger.info(
                "on-request probe capabilities: version=0x%04x interaction=%d "
                "frame_size=%d sample_rate=%.0f",
                caps.version,
                caps.interaction,
                caps.frame_size,
                caps.sample_rate,
            )
            loop.call_soon_threadsafe(capabilities_ready.set)
        elif isinstance(event, atvv_session.MicButtonPressed):
            logger.info(
                "on-request probe START_SEARCH: count=%d action=%s",
                state.snapshot()["events"]["start_search_count"],
                action.value,
            )
        elif isinstance(event, atvv_session.AudioStarted):
            logger.info(
                "on-request probe AUDIO_START: reason=%s codec=%s stream_id=%s",
                event.reason,
                event.codec,
                event.session_id,
            )
        elif isinstance(event, atvv_session.AudioStopped):
            logger.info(
                "on-request probe AUDIO_STOP: reason=%s",
                event.reason,
            )

        session = session_holder.get("session")
        if session is None:
            return
        if action is ProbeAction.SEND_OPEN:
            logger.info("on-request probe MIC_OPEN scheduled after START_SEARCH")
            session.send_mic_open_threadsafe()
        elif action is ProbeAction.SEND_CLOSE:
            logger.info(
                "on-request probe MIC_CLOSE scheduled after second START_SEARCH"
            )
            session.send_mic_close_threadsafe()
            schedule_finish(final_drain_seconds)

    def on_pcm_frame(samples: list[int]) -> None:
        state.handle_pcm(samples)

    def on_error(exc: BaseException) -> None:
        logger.info(
            "on-request probe transport failed: error_type=%s",
            type(exc).__name__,
        )
        state.mark_terminal("transport_error")
        loop.call_soon_threadsafe(probe_finished.set)

    def on_disconnected() -> None:
        logger.info("on-request probe disconnected")
        state.mark_terminal("disconnected")
        loop.call_soon_threadsafe(probe_finished.set)

    session = session_factory(
        on_pcm_frame=on_pcm_frame,
        on_control_event=on_control_event,
        on_error=on_error,
        on_disconnected=on_disconnected,
        get_capabilities_command=proto.GET_CAPABILITIES_ON_REQUEST_V10,
        loop=loop,
    )
    session_holder["session"] = session

    logger.info("on-request probe startup: discovering RC003")
    connected = False
    primary_error: Optional[BaseException] = None
    try:
        candidates = await discover_candidates()
        candidate = await select_candidate(candidates)
        await session.connect(candidate)
        connected = True
        try:
            await asyncio.wait_for(
                capabilities_ready.wait(),
                timeout=capabilities_timeout,
            )
        except TimeoutError:
            state.mark_terminal("capabilities_timeout")
            return state.snapshot()

        snapshot = state.snapshot()
        capabilities = snapshot["capabilities"]
        if capabilities is None or capabilities["interaction"] != 0:
            state.mark_terminal("on_request_not_selected")
            return state.snapshot()

        show_notice(
            _TITLE,
            "连接与开关型语音协商已经完成。\n\n"
            "点击“确定”后，请严格按下面步骤操作：\n"
            "1. 短按一次话筒键并立即松开，不要长按；\n"
            "2. 松开后马上连续说话约 3 秒；\n"
            "3. 再短按一次话筒键结束。\n\n"
            "诊断最多等待 18 秒，不会播放、保存或转写语音内容。",
        )
        state.arm()
        logger.info("on-request probe armed: waiting for physical START_SEARCH")
        try:
            await asyncio.wait_for(
                probe_finished.wait(),
                timeout=probe_timeout,
            )
        except TimeoutError:
            logger.info("on-request probe observation window ended")
        return state.snapshot()
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        try:
            final_snapshot = state.snapshot()
            if connected and final_snapshot["events"]["open_sent"]:
                if not final_snapshot["events"]["close_sent"]:
                    session.send_mic_close_threadsafe()
                    await asyncio.sleep(0.2)
            await session.close()
        except Exception as cleanup_exc:
            logger.info(
                "on-request probe cleanup failed: error_type=%s",
                type(cleanup_exc).__name__,
            )
            if primary_error is None:
                raise


def _failure_result(outcome: str, error_type: Optional[str] = None) -> dict:
    result = {
        "schema": PROBE_SCHEMA_VERSION,
        "probe": "rc003_on_request",
        "outcome": outcome,
        "capabilities": None,
        "events": {},
        "pcm": {},
    }
    if error_type is not None:
        result["error_type"] = error_type
    return result


def _result_message(result: dict) -> str:
    outcome = result.get("outcome")
    if outcome == "pcm_continued_past_1000ms":
        conclusion = "检测到短按后超过 1 秒仍有真实语音数据，这种方式值得继续开发。"
    elif outcome in {
        "control_only_no_pcm",
        "pcm_did_not_continue_past_1000ms",
        "no_audio_start",
        "no_mic_open_audio_start",
    }:
        conclusion = "没有检测到短按后持续语音，结果支持 RC003 只正式保留按住说话。"
    elif outcome == "no_start_search":
        conclusion = "本次没有收到话筒启动信号，请确认操作的是话筒键并重新测试。"
    elif outcome == "bridge_already_running":
        conclusion = (
            "后台桥接或上一次专项检测仍在运行。请先从通知区域退出桥接；"
            "如果刚运行过专项检测，请等它结束后再重试。"
        )
    elif outcome == "f5_suppressor_unavailable":
        conclusion = (
            "无法安全拦截遥控器话筒按钮误发的日期按键，"
            "本次诊断已停止，没有执行能力判断。"
        )
    elif outcome == "startup_timeout":
        conclusion = (
            f"连接遥控器超过 {STARTUP_HARD_TIMEOUT_SECONDS:g} 秒仍未完成。"
            "本次诊断已自动结束，"
            "按键拦截和后台占用已经释放。"
        )
    elif outcome == "completion_timeout":
        conclusion = (
            "按键测试结束后程序未能正常收尾。本次诊断已强制结束，"
            "按键拦截和后台占用已经释放。"
        )
    else:
        conclusion = "诊断未完整结束，请把结果文件和 app.log 一起回传。"
    return (
        f"{conclusion}\n\n"
        "日志文件夹将自动打开，请回传 on-request-probe-result.json 和 app.log。"
    )


def main(
    *,
    show_notice: NoticeFn = _show_notice,
    open_result_directory: Callable[[Path], None] = _open_result_directory,
    guard_factory: Callable[[], object] = single_instance.BridgeInstanceGuard,
    f5_suppressor_factory: Callable[[], object] = _create_f5_suppressor,
    probe_runner: ProbeRunner = _run_probe_sync,
    startup_hard_timeout: float = STARTUP_HARD_TIMEOUT_SECONDS,
    post_ready_hard_timeout: float = POST_READY_HARD_TIMEOUT_SECONDS,
) -> int:
    logger = logging_setup.get_logger().getChild("on_request_probe")
    result: Optional[dict] = None
    exit_code = PROBE_COMPLETED_EXIT_CODE
    try:
        with guard_factory():
            f5_suppressor = f5_suppressor_factory()
            f5_suppressor.start()
            logger.info("on-request probe legacy F5 guard enabled")
            try:
                show_notice(
                    _TITLE,
                    "这是 RC003 最后一条开关型语音能力诊断。\n\n"
                    "请先从通知区域退出正在运行的后台桥接。"
                    "\n诊断期间会拦截遥控器话筒按钮误发的日期按键，不会向输入框写入日期。"
                    "\n诊断不会触发输入法快捷键，也不会使用虚拟声卡。\n\n"
                    "点击“确定”后程序会连接遥控器。"
                    f"连接最长等待 {STARTUP_HARD_TIMEOUT_SECONDS:g} 秒，"
                    "超时会自动结束并释放后台。",
                )
                result = _run_probe_with_hard_deadlines(
                    probe_runner,
                    show_notice,
                    startup_timeout=startup_hard_timeout,
                    post_ready_timeout=post_ready_hard_timeout,
                )
            finally:
                f5_suppressor.stop()
                logger.info("on-request probe legacy F5 guard stopped")
    except single_instance.DuplicateInstanceError:
        result = _failure_result("bridge_already_running")
        exit_code = PROBE_BLOCKED_EXIT_CODE
    except legacy_key_suppressor_windows.LegacyKeySuppressorUnavailableError as exc:
        result = _failure_result("f5_suppressor_unavailable", type(exc).__name__)
        exit_code = PROBE_FAILED_EXIT_CODE
    except single_instance.SingleInstanceUnavailableError as exc:
        result = _failure_result(
            "single_instance_unavailable",
            type(exc).__name__,
        )
        exit_code = PROBE_BLOCKED_EXIT_CODE
    except single_instance.MutexCleanupError as exc:
        result = _failure_result(
            "single_instance_cleanup_failed",
            type(exc).__name__,
        )
        exit_code = PROBE_FAILED_EXIT_CODE
    except identity.NoCandidateFoundError as exc:
        result = _failure_result("no_candidate", type(exc).__name__)
        exit_code = PROBE_FAILED_EXIT_CODE
    except identity.AmbiguousCandidateError as exc:
        result = _failure_result("ambiguous_candidate", type(exc).__name__)
        exit_code = PROBE_FAILED_EXIT_CODE
    except ble_transport_winrt.NoReachableCandidateError as exc:
        result = _failure_result(
            "no_reachable_candidate",
            type(exc).__name__,
        )
        exit_code = PROBE_FAILED_EXIT_CODE
    except ProbeStartupTimeoutError:
        result = _failure_result("startup_timeout")
        exit_code = PROBE_FAILED_EXIT_CODE
    except ProbeCompletionTimeoutError:
        result = _failure_result("completion_timeout")
        exit_code = PROBE_FAILED_EXIT_CODE
    except Exception as exc:  # noqa: BLE001 - visible, sanitized failure
        logger.info(
            "on-request probe failed: error_type=%s",
            type(exc).__name__,
        )
        result = _failure_result("probe_failed", type(exc).__name__)
        exit_code = PROBE_FAILED_EXIT_CODE

    path = probe_result_path()
    try:
        write_probe_result(path, result)
    except Exception as exc:  # noqa: BLE001 - no raw exception in UI/log
        logger.info(
            "on-request probe result write failed: error_type=%s",
            type(exc).__name__,
        )
        show_notice(
            _TITLE,
            "诊断结束，但结果文件写入失败。请只回传 app.log。",
        )
        return PROBE_FAILED_EXIT_CODE

    logger.info(
        "on-request probe result: outcome=%s start_search=%s audio_start=%s "
        "audio_stop=%s pcm_batches=%s pcm_samples=%s pcm_after_1000ms=%s",
        result.get("outcome"),
        result.get("events", {}).get("start_search_count"),
        result.get("events", {}).get("audio_start_count"),
        result.get("events", {}).get("audio_stop_count"),
        result.get("pcm", {}).get("callback_batches"),
        result.get("pcm", {}).get("samples"),
        result.get("pcm", {}).get("mic_open_batches_after_1000ms"),
    )
    show_notice(_TITLE, _result_message(result))
    try:
        open_result_directory(path)
    except Exception as exc:  # noqa: BLE001 - result itself is already safe
        logger.info(
            "on-request probe result directory open failed: error_type=%s",
            type(exc).__name__,
        )
    return exit_code
