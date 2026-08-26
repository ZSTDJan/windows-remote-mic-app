"""Writes decoded ATVV PCM to the one user-selected Windows output endpoint.

Windows-only (``sounddevice``/PortAudio). Never touches the system default
device: it always opens the specific endpoint the user picked by name, and
raises immediately if that endpoint can't be opened - callers must treat
that as "voice fails closed, buttons keep working" (see audio_output.py).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import List, Optional

from . import audio_output

SOURCE_SAMPLE_RATE_HZ = 16000
DEFAULT_CHANNELS = 1

LOOPBACK_PROBE_SAMPLE_RATE_HZ = 48000
LOOPBACK_PROBE_LEAD_SECONDS = 0.20
LOOPBACK_PROBE_SIGNAL_SECONDS = 0.35
LOOPBACK_PROBE_TAIL_SECONDS = 0.65
LOOPBACK_PROBE_TIMEOUT_SECONDS = 3.0
LOOPBACK_PROBE_CORRELATION_THRESHOLD = 0.60
LOOPBACK_PROBE_MIN_SIGNAL_RMS = 0.005
LOOPBACK_PROBE_BASELINE_MULTIPLIER = 4.0


class PlaybackUnavailableError(audio_output.AudioOutputUnavailableError):
    pass


class PreflightCleanupIncompleteError(audio_output.AudioOutputUnavailableError):
    """A settings/diagnostics preflight still owns a PortAudio stream."""


class LoopbackProbeUnavailableError(audio_output.AudioOutputUnavailableError):
    """The explicit VB-CABLE route probe could not produce a trustworthy result."""


class LoopbackProbeCancelledError(LoopbackProbeUnavailableError):
    """The explicit VB-CABLE route probe was cancelled during shutdown."""


@dataclass(frozen=True)
class PlaybackTimingSnapshot:
    open_elapsed_ms: float
    last_write_elapsed_ms: float
    max_write_elapsed_ms: float
    write_count: int
    underflow_count: int


@dataclass(frozen=True)
class CableLoopbackProbeResult:
    detected: bool
    correlation: float
    baseline_rms: float
    signal_rms: float
    roundtrip_latency_ms: Optional[float]
    input_overflowed: bool
    output_underflowed: bool
    sample_rate_hz: int


# Serializes these in-process helpers and their retained cleanup. Production
# runs the explicit loopback helper inside a disposable diagnostics child;
# direct callers/tests still share this ownership gate with preflight.
_portaudio_test_lock = threading.RLock()
_retained_preflight_sink: Optional["EndpointPlaybackSink"] = None
_retained_loopback_stream = None


class EndpointPlaybackSink:
    """Opens one output stream bound to a specific, already-resolved endpoint
    and accepts decoded int16 PCM sample batches to play.

    Endpoint identity is (name, host_api) - matching audio_output.py's
    disambiguation contract - since a bare display name is not always unique
    across PortAudio host APIs (e.g. the same physical device can appear
    once under WASAPI and once under MME).
    """

    def __init__(self, endpoint_name: str, host_api: str = "") -> None:
        self._endpoint_name = endpoint_name
        self._host_api = host_api
        self._stream = None
        self._ready = False
        self._output_sample_rate_hz = SOURCE_SAMPLE_RATE_HZ
        self._output_channels = DEFAULT_CHANNELS
        self._previous_sample = 0
        self._have_previous_sample = False
        self._timing_lock = threading.Lock()
        self._open_elapsed_ms = 0.0
        self._last_write_elapsed_ms = 0.0
        self._max_write_elapsed_ms = 0.0
        self._write_count = 0
        self._underflow_count = 0

    def open(self) -> None:
        if self._stream is not None:
            raise PlaybackUnavailableError(
                "playback sink still owns a stream; close it before reopening"
            )
        open_started_at = time.perf_counter()
        with self._timing_lock:
            self._open_elapsed_ms = 0.0
            self._last_write_elapsed_ms = 0.0
            self._max_write_elapsed_ms = 0.0
            self._write_count = 0
            self._underflow_count = 0
        try:
            import sounddevice as sd  # type: ignore
        except ImportError as exc:  # pragma: no cover - exercised only on Windows
            raise PlaybackUnavailableError(
                "the 'sounddevice' package is not installed"
            ) from exc

        device_index = self._resolve_device_index(sd)
        self._output_channels = self._select_output_channels(sd, device_index)
        self._output_sample_rate_hz = self._select_output_sample_rate(sd, device_index)

        stream = None
        try:
            stream = sd.OutputStream(
                device=device_index,
                channels=self._output_channels,
                dtype="int16",
                samplerate=self._output_sample_rate_hz,
                latency="low",
            )
            # Retain ownership before start(): PortAudio may allocate native
            # resources during construction even when start() later fails.
            self._stream = stream
            stream.start()
            self._ready = True
            with self._timing_lock:
                self._open_elapsed_ms = (
                    time.perf_counter() - open_started_at
                ) * 1000.0
        except Exception as exc:  # noqa: BLE001 - normalize PortAudio backend failures
            if self._stream is not None:
                try:
                    self.close()
                except Exception:
                    pass
            raise audio_output.AudioOutputUnavailableError(
                "selected output endpoint could not be opened for blocking playback"
            ) from exc
        self._previous_sample = 0
        self._have_previous_sample = False

    @property
    def owns_stream(self) -> bool:
        return self._stream is not None

    @property
    def ready(self) -> bool:
        return self._ready and self._stream is not None

    @property
    def output_sample_rate_hz(self) -> int:
        return self._output_sample_rate_hz

    @property
    def output_channels(self) -> int:
        return self._output_channels

    def timing_snapshot(self) -> PlaybackTimingSnapshot:
        with self._timing_lock:
            return PlaybackTimingSnapshot(
                open_elapsed_ms=self._open_elapsed_ms,
                last_write_elapsed_ms=self._last_write_elapsed_ms,
                max_write_elapsed_ms=self._max_write_elapsed_ms,
                write_count=self._write_count,
                underflow_count=self._underflow_count,
            )

    def _select_output_channels(self, sd, device_index: int) -> int:
        """Use stereo when the endpoint supports it so virtual cables receive both channels."""
        device = sd.query_devices()[device_index]
        return 2 if int(device.get("max_output_channels") or 0) >= 2 else DEFAULT_CHANNELS

    def _select_output_sample_rate(self, sd, device_index: int) -> int:
        device = sd.query_devices()[device_index]
        preferred = int(device.get("default_samplerate") or 0)
        candidates = []
        if preferred > 0:
            candidates.append(preferred)
        candidates.extend([SOURCE_SAMPLE_RATE_HZ, 48000, 44100])

        seen = set()
        errors = []
        for sample_rate in candidates:
            if sample_rate in seen:
                continue
            seen.add(sample_rate)
            try:
                sd.check_output_settings(
                    device=device_index,
                    channels=self._output_channels,
                    dtype="int16",
                    samplerate=sample_rate,
                )
                return sample_rate
            except Exception as exc:  # pragma: no cover - exercised only on Windows
                errors.append(f"{sample_rate} Hz: {exc}")

        detail = "; ".join(errors) if errors else "no candidate sample rates available"
        raise audio_output.AudioOutputUnavailableError(
            "selected output endpoint cannot play mono int16 PCM at any supported "
            f"sample rate ({detail})"
        )

    def _resolve_device_index(self, sd) -> int:
        host_apis = sd.query_hostapis()
        candidates = []
        for index, device in enumerate(sd.query_devices()):
            if device.get("max_output_channels", 0) <= 0:
                continue
            if device["name"] != self._endpoint_name:
                continue
            host_api_name = host_apis[device["hostapi"]]["name"] if host_apis else ""
            candidates.append((index, host_api_name))

        if not candidates:
            raise audio_output.AudioOutputUnavailableError(
                f"selected output endpoint is not currently present: {self._endpoint_name!r}"
            )

        resolved = audio_output.resolve_selected_endpoint(
            [
                audio_output.AudioEndpoint(name=self._endpoint_name, host_api=host_api_name)
                for _index, host_api_name in candidates
            ],
            self._endpoint_name,
            self._host_api,
        )
        matching_indices = [
            index for index, host_api_name in candidates if host_api_name == resolved.host_api
        ]
        if len(matching_indices) != 1:
            raise audio_output.AudioOutputUnavailableError(
                f"{len(matching_indices)} output endpoints share the resolved name and "
                "host API; select a unique device explicitly"
            )
        return matching_indices[0]

    def write(self, samples: List[int]) -> None:
        if self._stream is None:
            raise PlaybackUnavailableError("open() must be called before write()")
        import numpy as np  # type: ignore

        array = np.asarray(samples, dtype="int16").reshape(-1, 1)
        if self._output_sample_rate_hz == 48000 and len(array) > 0:
            # Match the upstream RC003 path: continuous 16 kHz -> 48 kHz
            # interpolation keeps the boundary between BLE notifications smooth.
            values = array[:, 0].astype("int32").tolist()
            previous = self._previous_sample if self._have_previous_sample else values[0]
            output = []
            for current in values:
                delta = current - previous
                output.extend(
                    (
                        previous + round(delta / 3.0),
                        previous + round(delta * (2.0 / 3.0)),
                        current,
                    )
                )
                previous = current
            self._previous_sample = values[-1]
            self._have_previous_sample = True
            array = np.asarray(output, dtype="int16").reshape(-1, 1)
        elif self._output_sample_rate_hz != SOURCE_SAMPLE_RATE_HZ and len(array) > 1:
            ratio = self._output_sample_rate_hz / SOURCE_SAMPLE_RATE_HZ
            output_length = max(1, int(round(len(array) * ratio)))
            source_positions = np.arange(len(array), dtype=np.float64)
            target_positions = np.linspace(0, len(array) - 1, output_length)
            resampled = np.interp(target_positions, source_positions, array[:, 0])
            array = np.rint(resampled).clip(-32768, 32767).astype("int16").reshape(-1, 1)
        if self._output_channels > 1:
            array = np.repeat(array, self._output_channels, axis=1)
        write_started_at = time.perf_counter()
        underflowed = False
        try:
            underflowed = bool(self._stream.write(array))
        finally:
            elapsed_ms = (time.perf_counter() - write_started_at) * 1000.0
            with self._timing_lock:
                self._last_write_elapsed_ms = elapsed_ms
                self._max_write_elapsed_ms = max(
                    self._max_write_elapsed_ms, elapsed_ms
                )
                self._write_count += 1
                if underflowed:
                    self._underflow_count += 1

    def close(self) -> None:
        if self._stream is not None:
            stream = self._stream
            self._ready = False
            try:
                stream.stop()
            except Exception:
                # A successful close is sufficient proof that PortAudio no
                # longer owns the endpoint even if stop() itself reported an
                # error. Only a close failure requires retaining the stream
                # handle so cleanup can retry it.
                pass
            try:
                stream.close()
            except Exception:
                self._stream = stream
                raise
            self._stream = None


def preflight_output_endpoint(endpoint_name: str, host_api: str = "") -> None:
    """Prove the selected endpoint can open now without sending any PCM."""

    global _retained_preflight_sink
    with _portaudio_test_lock:
        cleanup_retained_portaudio_test_resources()
        sink = EndpointPlaybackSink(endpoint_name, host_api)
        try:
            sink.open()
        except BaseException as open_exc:
            try:
                sink.close()
            except Exception as close_exc:
                if sink.owns_stream:
                    _retained_preflight_sink = sink
                raise PreflightCleanupIncompleteError(
                    "output preflight failed and its PortAudio stream did not close"
                ) from open_exc
            raise
        try:
            sink.close()
        except Exception as close_exc:
            if sink.owns_stream:
                _retained_preflight_sink = sink
            raise PreflightCleanupIncompleteError(
                "output preflight stream did not close"
            ) from close_exc


def cleanup_retained_preflight_sink() -> None:
    """Retry cleanup of a preflight stream whose prior close failed."""

    global _retained_preflight_sink
    with _portaudio_test_lock:
        sink = _retained_preflight_sink
        if sink is None:
            return
        sink.close()
        if not sink.owns_stream:
            _retained_preflight_sink = None


def _resolve_loopback_device_index(sd, endpoint, channel_count_key: str) -> int:
    try:
        host_apis = sd.query_hostapis()
        matches = []
        for index, device in enumerate(sd.query_devices()):
            if int(device.get(channel_count_key) or 0) <= 0:
                continue
            if device.get("name") != endpoint.name:
                continue
            host_api_index = int(device["hostapi"])
            host_api_name = host_apis[host_api_index]["name"] if host_apis else ""
            if host_api_name == endpoint.host_api:
                matches.append(index)
    except Exception as exc:  # noqa: BLE001 - never expose device details
        raise LoopbackProbeUnavailableError(
            "无法解析 VB-CABLE 音频端点"
        ) from exc
    if len(matches) != 1:
        raise LoopbackProbeUnavailableError(
            "VB-CABLE 音频端点无法按名称和音频接口唯一确定"
        )
    return matches[0]


def _loopback_stream_format_candidates(sd, input_index: int, output_index: int):
    try:
        devices = sd.query_devices()
        input_device = devices[input_index]
        output_device = devices[output_index]
        max_output_channels = int(output_device.get("max_output_channels") or 0)
        preferred_output_channels = 2 if max_output_channels >= 2 else 1
        candidates = [
            LOOPBACK_PROBE_SAMPLE_RATE_HZ,
            int(output_device.get("default_samplerate") or 0),
            int(input_device.get("default_samplerate") or 0),
            44100,
            SOURCE_SAMPLE_RATE_HZ,
        ]
    except Exception as exc:  # noqa: BLE001 - never expose device details
        raise LoopbackProbeUnavailableError(
            "无法读取 VB-CABLE 音频格式"
        ) from exc

    seen = set()
    formats = []
    for sample_rate_hz in candidates:
        if sample_rate_hz <= 0 or sample_rate_hz in seen:
            continue
        seen.add(sample_rate_hz)
        output_channel_candidates = (
            (preferred_output_channels, 1)
            if preferred_output_channels > 1
            else (1,)
        )
        for output_channels in output_channel_candidates:
            try:
                sd.check_input_settings(
                    device=input_index,
                    channels=1,
                    dtype="int16",
                    samplerate=sample_rate_hz,
                )
                sd.check_output_settings(
                    device=output_index,
                    channels=output_channels,
                    dtype="int16",
                    samplerate=sample_rate_hz,
                )
            except Exception:
                continue
            formats.append((sample_rate_hz, output_channels))
    if formats:
        return formats
    raise LoopbackProbeUnavailableError(
        "CABLE Input 与 CABLE Output 没有可共同使用的音频格式"
    )


def _build_loopback_probe(np, sample_rate_hz: int):
    lead_samples = int(round(LOOPBACK_PROBE_LEAD_SECONDS * sample_rate_hz))
    probe_samples = int(round(LOOPBACK_PROBE_SIGNAL_SECONDS * sample_rate_hz))
    tail_samples = int(round(LOOPBACK_PROBE_TAIL_SECONDS * sample_rate_hz))
    times = np.arange(probe_samples, dtype=np.float64) / sample_rate_hz
    start_hz = 650.0
    end_hz = 1900.0
    sweep_rate = (end_hz - start_hz) / LOOPBACK_PROBE_SIGNAL_SECONDS
    phase = 2.0 * np.pi * (
        start_hz * times + 0.5 * sweep_rate * times * times
    )
    envelope = np.hanning(probe_samples)
    probe_float = 0.20 * np.sin(phase) * envelope
    probe = np.rint(probe_float * 32767.0).astype("int16")
    output = np.zeros(lead_samples + probe_samples + tail_samples, dtype="int16")
    output[lead_samples : lead_samples + probe_samples] = probe
    return output, probe, lead_samples


def _analyze_loopback_capture(np, captured, probe, lead_samples, sample_rate_hz):
    captured_float = np.asarray(captured, dtype=np.float64).reshape(-1) / 32768.0
    probe_float = np.asarray(probe, dtype=np.float64).reshape(-1) / 32768.0
    if len(captured_float) < len(probe_float) or len(probe_float) == 0:
        return 0.0, 0.0, 0.0, None

    baseline = captured_float[:lead_samples]
    baseline_rms = (
        float(np.sqrt(np.mean(np.square(baseline)))) if len(baseline) else 0.0
    )
    captured_centered = captured_float - float(np.mean(captured_float))
    probe_centered = probe_float - float(np.mean(probe_float))
    probe_energy = float(np.sum(np.square(probe_centered)))
    if probe_energy <= 0.0:
        return 0.0, baseline_rms, 0.0, None

    full_length = len(captured_centered) + len(probe_centered) - 1
    fft_length = 1 << (full_length - 1).bit_length()
    correlation_full = np.fft.irfft(
        np.fft.rfft(captured_centered, fft_length)
        * np.fft.rfft(probe_centered[::-1], fft_length),
        fft_length,
    )[:full_length]
    valid_dot_products = correlation_full[
        len(probe_centered) - 1 : len(captured_centered)
    ]
    cumulative_energy = np.concatenate(
        ([0.0], np.cumsum(np.square(captured_centered)))
    )
    window_energy = (
        cumulative_energy[len(probe_centered) :]
        - cumulative_energy[: -len(probe_centered)]
    )
    denominators = np.sqrt(np.maximum(window_energy * probe_energy, 0.0))
    normalized = np.zeros_like(valid_dot_products)
    usable = denominators > 1e-12
    normalized[usable] = valid_dot_products[usable] / denominators[usable]
    if len(normalized) == 0:
        return 0.0, baseline_rms, 0.0, None

    best_index = int(np.argmax(np.abs(normalized)))
    correlation = float(abs(normalized[best_index]))
    best_window = captured_float[best_index : best_index + len(probe_float)]
    signal_rms = float(np.sqrt(np.mean(np.square(best_window))))
    latency_ms = max(0.0, (best_index - lead_samples) * 1000.0 / sample_rate_hz)
    return correlation, baseline_rms, signal_rms, latency_ms


def probe_virtual_cable_loopback(
    output_endpoint: audio_output.AudioEndpoint,
    input_endpoint: audio_output.AudioEndpoint,
    *,
    cancel_event: Optional[threading.Event] = None,
    sd_module=None,
) -> CableLoopbackProbeResult:
    """Actively prove CABLE Input reaches CABLE Output without using defaults.

    The generated sweep and captured PCM live only in memory for this call.
    Only scalar timing/energy evidence is returned to callers.
    """

    global _retained_loopback_stream
    with _portaudio_test_lock:
        cleanup_retained_portaudio_test_resources()
        if cancel_event is not None and cancel_event.is_set():
            raise LoopbackProbeCancelledError("VB-CABLE 通道测试已取消")
        if not audio_output.is_cable_input_endpoint(output_endpoint.name):
            raise LoopbackProbeUnavailableError("播放端点不是 CABLE Input")
        if not audio_output.is_cable_output_endpoint(input_endpoint.name):
            raise LoopbackProbeUnavailableError("录音端点不是 CABLE Output")
        if not output_endpoint.host_api or (
            output_endpoint.host_api != input_endpoint.host_api
        ):
            raise LoopbackProbeUnavailableError(
                "CABLE Input 与 CABLE Output 必须使用同一音频接口"
            )
        audio_output.ensure_supported_output_endpoint(output_endpoint)

        if sd_module is None:
            try:
                import sounddevice as sd  # type: ignore
            except ImportError as exc:  # pragma: no cover - Windows packaging path
                raise LoopbackProbeUnavailableError(
                    "缺少 sounddevice，无法测试 VB-CABLE 通道"
                ) from exc
        else:
            sd = sd_module
        try:
            import numpy as np  # type: ignore
        except ImportError as exc:  # pragma: no cover - frozen build includes NumPy
            raise LoopbackProbeUnavailableError(
                "缺少 NumPy，无法分析 VB-CABLE 测试信号"
            ) from exc

        output_index = _resolve_loopback_device_index(
            sd, output_endpoint, "max_output_channels"
        )
        input_index = _resolve_loopback_device_index(
            sd, input_endpoint, "max_input_channels"
        )
        format_candidates = _loopback_stream_format_candidates(
            sd, input_index, output_index
        )
        stream = None
        stream_started = False
        last_open_error = None

        for sample_rate_hz, output_channels in format_candidates:
            if cancel_event is not None and cancel_event.is_set():
                raise LoopbackProbeCancelledError("VB-CABLE 通道测试已取消")
            output_signal, probe_signal, lead_samples = _build_loopback_probe(
                np, sample_rate_hz
            )
            captured = np.zeros(len(output_signal), dtype="int16")
            cursor = 0
            finished = threading.Event()
            callback_errors = []
            input_overflowed = False
            output_underflowed = False

            def _callback(indata, outdata, frames, _time_info, status) -> None:
                nonlocal cursor, input_overflowed, output_underflowed
                try:
                    input_overflowed = input_overflowed or bool(
                        getattr(status, "input_overflow", False)
                    )
                    output_underflowed = output_underflowed or bool(
                        getattr(status, "output_underflow", False)
                    )
                    outdata.fill(0)
                    remaining = len(output_signal) - cursor
                    take = min(frames, max(0, remaining))
                    if take > 0:
                        chunk = output_signal[cursor : cursor + take]
                        outdata[:take, :] = chunk.reshape(-1, 1)
                        captured[cursor : cursor + take] = indata[:take, 0]
                        cursor += take
                    if cursor >= len(output_signal):
                        raise sd.CallbackStop
                except sd.CallbackStop:
                    raise
                except Exception as exc:  # pragma: no cover - defensive callback gate
                    callback_errors.append(exc)
                    finished.set()
                    raise sd.CallbackAbort

            candidate_stream = None
            try:
                candidate_stream = sd.Stream(
                    device=(input_index, output_index),
                    samplerate=sample_rate_hz,
                    blocksize=0,
                    channels=(1, output_channels),
                    dtype=("int16", "int16"),
                    latency="low",
                    callback=_callback,
                    finished_callback=finished.set,
                )
                candidate_stream.start()
            except Exception as exc:  # noqa: BLE001 - try the next real duplex format
                last_open_error = exc
                if candidate_stream is not None:
                    try:
                        candidate_stream.abort()
                    except Exception:
                        pass
                    try:
                        candidate_stream.close()
                    except Exception as close_exc:
                        _retained_loopback_stream = candidate_stream
                        raise LoopbackProbeUnavailableError(
                            "VB-CABLE 通道测试切换音频格式时未能释放临时音频流"
                        ) from close_exc
                continue
            stream = candidate_stream
            stream_started = True
            break

        if stream is None:
            raise LoopbackProbeUnavailableError(
                "无法用任何共同格式打开 VB-CABLE 双工音频流"
            ) from last_open_error

        operation_error = None
        timed_out_or_cancelled = False
        deadline = time.perf_counter() + LOOPBACK_PROBE_TIMEOUT_SECONDS
        try:
            while not finished.wait(0.02):
                if cancel_event is not None and cancel_event.is_set():
                    timed_out_or_cancelled = True
                    raise LoopbackProbeCancelledError("VB-CABLE 通道测试已取消")
                if time.perf_counter() >= deadline:
                    timed_out_or_cancelled = True
                    raise LoopbackProbeUnavailableError(
                        "VB-CABLE 通道测试超时，未得到完整结果"
                    )
            if callback_errors:
                raise LoopbackProbeUnavailableError(
                    "VB-CABLE 通道测试的音频回调失败"
                ) from callback_errors[0]
        except LoopbackProbeUnavailableError as exc:
            operation_error = exc
        except Exception as exc:  # noqa: BLE001 - never expose device details
            operation_error = LoopbackProbeUnavailableError(
                "无法打开或运行 VB-CABLE 临时音频流"
            )
            operation_error.__cause__ = exc

        cleanup_errors = []
        if stream is not None:
            if stream_started:
                try:
                    if timed_out_or_cancelled:
                        stream.abort()
                    else:
                        stream.stop()
                except Exception as exc:  # noqa: BLE001 - close still must run
                    cleanup_errors.append(exc)
            try:
                stream.close()
            except Exception as exc:  # noqa: BLE001 - retain ownership for retry
                cleanup_errors.append(exc)
                _retained_loopback_stream = stream
            else:
                if _retained_loopback_stream is stream:
                    _retained_loopback_stream = None

        if cleanup_errors:
            raise LoopbackProbeUnavailableError(
                "VB-CABLE 通道测试结束时未能完整释放临时音频流"
            ) from cleanup_errors[0]
        if operation_error is not None:
            raise operation_error

        correlation, baseline_rms, signal_rms, latency_ms = (
            _analyze_loopback_capture(
                np, captured, probe_signal, lead_samples, sample_rate_hz
            )
        )
        detected = (
            correlation >= LOOPBACK_PROBE_CORRELATION_THRESHOLD
            and signal_rms
            >= max(
                LOOPBACK_PROBE_MIN_SIGNAL_RMS,
                baseline_rms * LOOPBACK_PROBE_BASELINE_MULTIPLIER,
            )
            and not input_overflowed
            and not output_underflowed
        )
        return CableLoopbackProbeResult(
            detected=detected,
            correlation=correlation,
            baseline_rms=baseline_rms,
            signal_rms=signal_rms,
            roundtrip_latency_ms=latency_ms if detected else None,
            input_overflowed=input_overflowed,
            output_underflowed=output_underflowed,
            sample_rate_hz=sample_rate_hz,
        )


def cleanup_retained_loopback_stream() -> None:
    """Retry closing a loopback stream whose earlier close failed."""

    global _retained_loopback_stream
    with _portaudio_test_lock:
        stream = _retained_loopback_stream
        if stream is None:
            return
        try:
            stream.abort()
        except Exception:
            pass
        stream.close()
        _retained_loopback_stream = None


def cleanup_retained_portaudio_test_resources(*, blocking: bool = True) -> bool:
    """Clean retained settings-test streams without defeating bounded shutdown.

    Normal callers wait for the shared PortAudio test lock. Window shutdown
    passes ``blocking=False``: if an active native call still owns the lock
    after the diagnostics worker's bounded join, shutdown must continue
    instead of waiting on that same call forever.
    """

    acquired = _portaudio_test_lock.acquire(blocking=blocking)
    if not acquired:
        return False
    try:
        try:
            cleanup_retained_loopback_stream()
        finally:
            cleanup_retained_preflight_sink()
    finally:
        _portaudio_test_lock.release()
    return True
