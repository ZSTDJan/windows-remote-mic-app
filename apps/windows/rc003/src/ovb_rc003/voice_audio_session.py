"""Own one shared voice output stream and its bounded writer.

Remote policies choose when to flush/release. Failed close retains the actual
resource; this module never starts a device reconnect or changes a shortcut.
"""
from __future__ import annotations
from . import audio_output, audio_playback, audio_playback_worker
from .atvv_session import PcmStats


class VoiceAudioSession:
    def __init__(self, *, config, logger, request_cleanup, disable_forwarding, on_first_frame):
        self.config = config
        self.logger = logger
        self.request_cleanup = request_cleanup
        self.disable_forwarding = disable_forwarding
        self.on_first_frame = on_first_frame
        self.sink = None
        self.writer = None
        self.stats = PcmStats()

    def stop_writer(self):
        if self.writer is not None:
            if not self.writer.stop():
                return False
            self.writer = None
        return True

    def close_sink(self):
        # Call only after the writer has confirmed stop; callers preserve their
        # original sequencing and error reporting around this operation.
        if self.sink is not None:
            self.sink.close()
            self.sink = None

    def adopt(self, sink):
        # Transfer a prepared private endpoint under the caller's session lock.
        self.sink = sink

    def detach(self, sink):
        # Startup rollback returns ownership to its private attempt.
        if self.sink is sink:
            self.sink = None

    def open(self) -> bool:
        if self.sink is not None:
            if getattr(self.sink, "ready", True):
                return self.ensure_writer(self.sink)
            self.logger.warning(
                "voice playback cannot reopen while a failed stream remains owned; "
                "requesting cleanup"
            )
            self.request_cleanup()
            return False
        endpoint_name = self.config().get("output_endpoint_name") or ""
        endpoint_host_api = self.config().get("output_endpoint_host_api") or ""
        sink = None
        try:
            endpoints = audio_output.enumerate_output_endpoints()
            audio_output.resolve_selected_endpoint(endpoints, endpoint_name, endpoint_host_api)
            sink = audio_playback.EndpointPlaybackSink(endpoint_name, endpoint_host_api)
            self.sink = sink
            sink.open()
            if not self.ensure_writer(sink):
                raise audio_output.AudioOutputUnavailableError(
                    "audio playback writer could not start"
                )
            timing_snapshot = getattr(sink, "timing_snapshot", None)
            timing = timing_snapshot() if callable(timing_snapshot) else None
            if timing is None:
                self.logger.info(
                    "voice playback opened: endpoint=%s host_api=%s "
                    "sample_rate=%s channels=%s",
                    endpoint_name or "unspecified",
                    endpoint_host_api or "unspecified",
                    sink.output_sample_rate_hz,
                    sink.output_channels,
                )
            else:
                self.logger.info(
                    "voice playback opened: endpoint=%s host_api=%s "
                    "sample_rate=%s channels=%s open_ms=%.2f",
                    endpoint_name or "unspecified",
                    endpoint_host_api or "unspecified",
                    sink.output_sample_rate_hz,
                    sink.output_channels,
                    timing.open_elapsed_ms,
                )
            return True
        except audio_output.AudioOutputUnavailableError as exc:
            self.logger.info("voice audio unavailable, failing closed: %s", exc)
            if sink is None or not sink.owns_stream:
                self.sink = None
            else:
                self.logger.warning(
                    "voice audio open cleanup incomplete; playback owner retained"
                )
                self.request_cleanup()
            return False
        except Exception:
            self.logger.exception("voice audio failed to open, failing closed")
            if sink is None or not sink.owns_stream:
                self.sink = None
            else:
                self.logger.warning(
                    "voice audio open cleanup incomplete; playback owner retained"
                )
                self.request_cleanup()
            return False


    def ensure_writer(self, sink) -> bool:
        writer = self.writer
        if writer is not None:
            if writer.is_alive and writer.failure is None:
                return True
            self.logger.warning(
                "voice playback writer is unavailable; requesting cleanup"
            )
            self.request_cleanup()
            return False
        writer = audio_playback_worker.PlaybackWriteWorker(
            lambda samples: self.write_frame(sink, samples),
            self.on_worker_error,
        )
        try:
            writer.start()
        except Exception:
            self.logger.exception("voice playback writer failed to start")
            return False
        self.writer = writer
        return True


    def on_worker_error(self, error: BaseException) -> None:
        self.disable_forwarding()
        self.logger.error("audio playback worker failed; failing closed: %s", error)
        self.request_cleanup()


    def flush(
        self,
        reason: str,
    ) -> audio_playback_worker.PlaybackFlushResult:
        writer = self.writer
        if writer is None:
            return audio_playback_worker.PlaybackFlushResult(True)
        result = writer.flush()
        if not result.completed:
            self.disable_forwarding()
            self.logger.error(
                "audio playback flush failed during %s: %s",
                reason,
                result.error or "unknown error",
            )
            self.request_cleanup()
        return result


    def write_frame(self, sink, samples) -> None:
        if sink is not self.sink:
            raise RuntimeError("audio playback sink changed while a write was queued")
        self.stats.add(samples)
        if self.stats.frames == 1:
            self.on_first_frame()
        sink.write(samples)
        if (
            self.stats.frames in (1, 10)
            or self.stats.frames % 200 == 0
        ):
            stats = self.stats.summary()
            timing_snapshot = getattr(sink, "timing_snapshot", None)
            timing = timing_snapshot() if callable(timing_snapshot) else None
            if timing is None:
                self.logger.info(
                    "voice PCM progress: frames=%s samples=%s peak=%s rms=%.1f "
                    "mean_abs=%.1f clipped=%.3f%%",
                    stats["frames"],
                    stats["samples"],
                    stats["peak"],
                    stats["rms"],
                    stats["mean_abs"],
                    stats["clipped_pct"],
                )
            else:
                self.logger.info(
                    "voice PCM progress: frames=%s samples=%s peak=%s rms=%.1f "
                    "mean_abs=%.1f clipped=%.3f%% write_ms=%.2f "
                    "max_write_ms=%.2f underflows=%s",
                    stats["frames"],
                    stats["samples"],
                    stats["peak"],
                    stats["rms"],
                    stats["mean_abs"],
                    stats["clipped_pct"],
                    timing.last_write_elapsed_ms,
                    timing.max_write_elapsed_ms,
                    timing.underflow_count,
                )
