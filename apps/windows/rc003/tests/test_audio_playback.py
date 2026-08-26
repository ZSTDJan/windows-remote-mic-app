"""Exercises EndpointPlaybackSink's device-resolution logic with a fake
``sounddevice``-shaped object (it already takes ``sd`` as a parameter, which
makes this possible without the real optional dependency installed) - see
audio_output.py's identical name+host_api disambiguation contract, which
this mirrors for the actual PortAudio device-index lookup used at
``open()`` time (XRBM-014 review RETRY P2 #1).
"""

import threading
import time
import unittest
from unittest import mock

from ovb_rc003 import audio_output, audio_playback
from ovb_rc003.audio_playback import EndpointPlaybackSink


class FakeSoundDevice:
    def __init__(self, devices, host_apis):
        self._devices = devices
        self._host_apis = host_apis
        self.checked_settings = []

    def query_devices(self):
        return self._devices

    def query_hostapis(self):
        return self._host_apis

    def check_output_settings(self, *, device, channels, dtype, samplerate):
        self.checked_settings.append(
            {
                "device": device,
                "channels": channels,
                "dtype": dtype,
                "samplerate": samplerate,
            }
        )


def _device(name, max_output_channels, hostapi_index, default_samplerate=16000.0):
    return {
        "name": name,
        "max_output_channels": max_output_channels,
        "hostapi": hostapi_index,
        "default_samplerate": default_samplerate,
    }


class ResolveDeviceIndexTests(unittest.TestCase):
    def setUp(self):
        self.host_apis = [{"name": "Windows WASAPI"}, {"name": "MME"}]

    def test_resolves_the_sole_matching_name(self):
        sd = FakeSoundDevice(
            devices=[_device("Speakers", 2, 0), _device("Mic In", 0, 0)],
            host_apis=self.host_apis,
        )
        sink = EndpointPlaybackSink("Speakers")
        self.assertEqual(sink._resolve_device_index(sd), 0)

    def test_ignores_input_only_devices_with_same_name(self):
        sd = FakeSoundDevice(
            devices=[_device("Line", 0, 0), _device("Line", 2, 1)],
            host_apis=self.host_apis,
        )
        sink = EndpointPlaybackSink("Line")
        self.assertEqual(sink._resolve_device_index(sd), 1)

    def test_missing_endpoint_fails_closed(self):
        sd = FakeSoundDevice(devices=[_device("Speakers", 2, 0)], host_apis=self.host_apis)
        sink = EndpointPlaybackSink("Nonexistent")
        with self.assertRaises(audio_output.AudioOutputUnavailableError):
            sink._resolve_device_index(sd)


class SelectOutputSampleRateTests(unittest.TestCase):
    def setUp(self):
        self.host_apis = [{"name": "Windows WASAPI"}, {"name": "MME"}]

    def test_prefers_the_endpoint_default_sample_rate_when_supported(self):
        sd = FakeSoundDevice(
            devices=[_device("CABLE Input", 2, 0, default_samplerate=48000.0)],
            host_apis=[{"name": "Windows WASAPI"}],
        )
        sink = EndpointPlaybackSink("CABLE Input", host_api="Windows WASAPI")

        self.assertEqual(sink._select_output_sample_rate(sd, 0), 48000)
        self.assertEqual(sd.checked_settings[0]["samplerate"], 48000)

    def test_falls_back_to_16k_when_default_sample_rate_is_rejected(self):
        class RejectDefaultSoundDevice(FakeSoundDevice):
            def check_output_settings(self, *, device, channels, dtype, samplerate):
                super().check_output_settings(
                    device=device, channels=channels, dtype=dtype, samplerate=samplerate
                )
                if samplerate == 48000:
                    raise RuntimeError("unsupported")

        sd = RejectDefaultSoundDevice(
            devices=[_device("CABLE Input", 2, 0, default_samplerate=48000.0)],
            host_apis=[{"name": "Windows WASAPI"}],
        )
        sink = EndpointPlaybackSink("CABLE Input", host_api="Windows WASAPI")

        self.assertEqual(sink._select_output_sample_rate(sd, 0), 16000)
        self.assertEqual([c["samplerate"] for c in sd.checked_settings], [48000, 16000])

    def test_resamples_16k_pcm_to_selected_output_rate_before_writing(self):
        class RecordingStream:
            def __init__(self):
                self.writes = []

            def write(self, array):
                self.writes.append(array)

        stream = RecordingStream()
        sink = EndpointPlaybackSink("CABLE Input", host_api="Windows WASAPI")
        sink._stream = stream
        sink._output_sample_rate_hz = 48000
        sink._output_channels = 2

        sink.write([0, 16000, -16000])

        self.assertEqual(stream.writes[0].shape, (9, 2))
        self.assertEqual(stream.writes[0][:, 0].tolist(), stream.writes[0][:, 1].tolist())

    def test_selects_stereo_when_the_endpoint_supports_two_channels(self):
        sd = FakeSoundDevice(
            devices=[_device("CABLE Input", 2, 0, default_samplerate=48000.0)],
            host_apis=[{"name": "Windows WASAPI"}],
        )
        sink = EndpointPlaybackSink("CABLE Input", host_api="Windows WASAPI")

        self.assertEqual(sink._select_output_channels(sd, 0), 2)

    def test_falls_back_to_mono_for_a_mono_only_endpoint(self):
        sd = FakeSoundDevice(
            devices=[_device("Speaker", 1, 0)],
            host_apis=[{"name": "Windows WASAPI"}],
        )
        sink = EndpointPlaybackSink("Speaker", host_api="Windows WASAPI")

        self.assertEqual(sink._select_output_channels(sd, 0), 1)

    def test_48k_resampler_keeps_interpolation_continuous_between_chunks(self):
        class RecordingStream:
            def __init__(self):
                self.writes = []

            def write(self, array):
                self.writes.append(array[:, 0].tolist())

        stream = RecordingStream()
        sink = EndpointPlaybackSink("CABLE Input", host_api="Windows WASAPI")
        sink._stream = stream
        sink._output_sample_rate_hz = 48000

        sink.write([0, 300])
        sink.write([600])

        self.assertEqual(stream.writes[0], [0, 0, 0, 100, 200, 300])
        self.assertEqual(stream.writes[1], [400, 500, 600])

    def test_name_without_host_api_prefers_wasapi(self):
        sd = FakeSoundDevice(
            devices=[_device("Speakers", 2, 0), _device("Speakers", 2, 1)],
            host_apis=self.host_apis,
        )
        sink = EndpointPlaybackSink("Speakers")
        self.assertEqual(sink._resolve_device_index(sd), 0)

    def test_ambiguous_name_with_host_api_resolves_the_right_index(self):
        sd = FakeSoundDevice(
            devices=[_device("Speakers", 2, 0), _device("Speakers", 2, 1)],
            host_apis=self.host_apis,
        )
        sink = EndpointPlaybackSink("Speakers", host_api="MME")
        self.assertEqual(sink._resolve_device_index(sd), 1)

    def test_saved_host_api_no_longer_present_fails_closed(self):
        sd = FakeSoundDevice(devices=[_device("Speakers", 2, 0)], host_apis=self.host_apis)
        sink = EndpointPlaybackSink("Speakers", host_api="MME")
        with self.assertRaises(audio_output.AudioOutputUnavailableError):
            sink._resolve_device_index(sd)

    def test_explicit_wdm_ks_endpoint_fails_before_stream_open(self):
        sd = FakeSoundDevice(
            devices=[_device("Output (VB-Audio Point)", 2, 0)],
            host_apis=[{"name": "Windows WDM-KS"}],
        )
        sink = EndpointPlaybackSink(
            "Output (VB-Audio Point)", host_api="Windows WDM-KS"
        )
        with self.assertRaises(audio_output.AudioOutputUnavailableError):
            sink._resolve_device_index(sd)


class PlaybackPreflightTests(unittest.TestCase):
    def tearDown(self):
        retained = audio_playback._retained_preflight_sink
        if retained is not None:
            stream = retained._stream
            if stream is not None and hasattr(stream, "fail_close"):
                stream.fail_close = False
            audio_playback.cleanup_retained_preflight_sink()
        retained_loopback = audio_playback._retained_loopback_stream
        if retained_loopback is not None:
            if hasattr(retained_loopback, "fail_close"):
                retained_loopback.fail_close = False
            audio_playback.cleanup_retained_loopback_stream()

    def _fake_module(self, stream_type):
        import types

        module = types.ModuleType("sounddevice")
        module.query_hostapis = lambda: [{"name": "Windows WASAPI"}]
        module.query_devices = lambda: [_device("CABLE Input", 2, 0, 48000.0)]
        module.check_output_settings = lambda **_kwargs: None
        module.OutputStream = stream_type
        return module

    def test_preflight_opens_starts_stops_and_closes(self):
        import sys

        events = []

        class Stream:
            def __init__(self, **_kwargs):
                events.append("open")

            def start(self):
                events.append("start")

            def stop(self):
                events.append("stop")

            def close(self):
                events.append("close")

        with mock.patch.dict(sys.modules, {"sounddevice": self._fake_module(Stream)}):
            from ovb_rc003.audio_playback import preflight_output_endpoint

            preflight_output_endpoint("CABLE Input", "Windows WASAPI")
        self.assertEqual(events, ["open", "start", "stop", "close"])

    def test_start_failure_is_sanitized_and_closes_stream(self):
        import sys

        events = []

        class Stream:
            def __init__(self, **_kwargs):
                events.append("open")

            def start(self):
                raise RuntimeError("private endpoint detail")

            def close(self):
                events.append("close")

        with mock.patch.dict(sys.modules, {"sounddevice": self._fake_module(Stream)}):
            from ovb_rc003.audio_playback import preflight_output_endpoint

            with self.assertRaises(audio_output.AudioOutputUnavailableError) as ctx:
                preflight_output_endpoint("CABLE Input", "Windows WASAPI")
        self.assertEqual(events, ["open", "close"])
        self.assertNotIn("private endpoint detail", str(ctx.exception))

    def test_start_and_close_failure_retains_stream_for_cleanup_retry(self):
        import sys

        class Stream:
            def __init__(self, **_kwargs):
                self.close_calls = 0

            def start(self):
                raise RuntimeError("start failed")

            def stop(self):
                pass

            def close(self):
                self.close_calls += 1
                raise RuntimeError("close failed")

        sink = EndpointPlaybackSink("CABLE Input", "Windows WASAPI")
        with mock.patch.dict(sys.modules, {"sounddevice": self._fake_module(Stream)}):
            with self.assertRaises(audio_output.AudioOutputUnavailableError):
                sink.open()

        self.assertTrue(sink.owns_stream)
        self.assertFalse(sink.ready)
        self.assertEqual(sink._stream.close_calls, 1)

    def test_preflight_close_failure_retains_owner_until_a_retry_succeeds(self):
        import sys

        instances = []

        class Stream:
            def __init__(self, **_kwargs):
                self.fail_close = True
                self.close_calls = 0
                instances.append(self)

            def start(self):
                pass

            def stop(self):
                pass

            def close(self):
                self.close_calls += 1
                if self.fail_close:
                    raise RuntimeError("close failed")

        with mock.patch.dict(sys.modules, {"sounddevice": self._fake_module(Stream)}):
            with self.assertRaises(audio_playback.PreflightCleanupIncompleteError):
                audio_playback.preflight_output_endpoint(
                    "CABLE Input", "Windows WASAPI"
                )

        self.assertIsNotNone(audio_playback._retained_preflight_sink)
        self.assertTrue(audio_playback._retained_preflight_sink.owns_stream)
        instances[0].fail_close = False
        audio_playback.cleanup_retained_preflight_sink()
        self.assertIsNone(audio_playback._retained_preflight_sink)

    def test_preflight_cleans_a_retained_loopback_stream_before_opening(self):
        import sys

        class RetainedLoopback:
            def __init__(self):
                self.abort_calls = 0
                self.close_calls = 0

            def abort(self):
                self.abort_calls += 1

            def close(self):
                self.close_calls += 1

        class Stream:
            def __init__(self, **_kwargs):
                pass

            def start(self):
                pass

            def stop(self):
                pass

            def close(self):
                pass

        retained = RetainedLoopback()
        audio_playback._retained_loopback_stream = retained

        with mock.patch.dict(sys.modules, {"sounddevice": self._fake_module(Stream)}):
            audio_playback.preflight_output_endpoint(
                "CABLE Input", "Windows WASAPI"
            )

        self.assertEqual(retained.abort_calls, 1)
        self.assertEqual(retained.close_calls, 1)
        self.assertIsNone(audio_playback._retained_loopback_stream)


class PlaybackCloseOwnershipTests(unittest.TestCase):
    def test_stop_failure_does_not_hide_a_successful_close(self):
        class Stream:
            def __init__(self):
                self.close_calls = 0

            def stop(self):
                raise RuntimeError("stop failed")

            def close(self):
                self.close_calls += 1

        stream = Stream()
        sink = EndpointPlaybackSink("CABLE Input")
        sink._stream = stream

        sink.close()

        self.assertEqual(stream.close_calls, 1)
        self.assertIsNone(sink._stream)

    def test_close_failure_retains_stream_for_a_later_retry(self):
        class Stream:
            def __init__(self):
                self.close_calls = 0
                self.fail_close = True

            def stop(self):
                pass

            def close(self):
                self.close_calls += 1
                if self.fail_close:
                    raise RuntimeError("close failed")

        stream = Stream()
        sink = EndpointPlaybackSink("CABLE Input")
        sink._stream = stream

        with self.assertRaises(RuntimeError):
            sink.close()
        self.assertIs(sink._stream, stream)

        stream.fail_close = False
        sink.close()

        self.assertEqual(stream.close_calls, 2)
        self.assertIsNone(sink._stream)


class PlaybackTimingTests(unittest.TestCase):
    def test_write_timing_and_underflow_are_recorded(self):
        class Stream:
            def __init__(self):
                self.results = iter((False, True))

            def write(self, _array):
                return next(self.results)

        sink = EndpointPlaybackSink("CABLE Input")
        sink._stream = Stream()
        with mock.patch.object(
            audio_playback.time,
            "perf_counter",
            side_effect=(1.0, 1.010, 2.0, 2.030),
        ):
            sink.write([1, 2])
            sink.write([3, 4])

        timing = sink.timing_snapshot()
        self.assertEqual(timing.write_count, 2)
        self.assertEqual(timing.underflow_count, 1)
        self.assertAlmostEqual(timing.last_write_elapsed_ms, 30.0)
        self.assertAlmostEqual(timing.max_write_elapsed_ms, 30.0)


class LoopbackSignalAnalysisTests(unittest.TestCase):
    def test_delayed_scaled_probe_is_detected_and_latency_is_measured(self):
        import numpy as np

        output, probe, lead_samples = audio_playback._build_loopback_probe(np, 48000)
        captured = np.zeros_like(output)
        delay_samples = 480
        start = lead_samples + delay_samples
        captured[start : start + len(probe)] = np.rint(
            probe.astype(np.float64) * 0.5
        ).astype("int16")

        correlation, baseline_rms, signal_rms, latency_ms = (
            audio_playback._analyze_loopback_capture(
                np, captured, probe, lead_samples, 48000
            )
        )

        self.assertGreater(correlation, 0.99)
        self.assertEqual(baseline_rms, 0.0)
        self.assertGreater(signal_rms, audio_playback.LOOPBACK_PROBE_MIN_SIGNAL_RMS)
        self.assertAlmostEqual(latency_ms, 10.0, places=1)

    def test_unrelated_single_tone_does_not_match_the_sweep(self):
        import numpy as np

        output, probe, lead_samples = audio_playback._build_loopback_probe(np, 48000)
        times = np.arange(len(output), dtype=np.float64) / 48000.0
        captured = np.rint(0.2 * np.sin(2.0 * np.pi * 1000.0 * times) * 32767.0).astype(
            "int16"
        )

        correlation, _baseline_rms, _signal_rms, _latency_ms = (
            audio_playback._analyze_loopback_capture(
                np, captured, probe, lead_samples, 48000
            )
        )

        self.assertLess(
            correlation, audio_playback.LOOPBACK_PROBE_CORRELATION_THRESHOLD
        )


class _FakeCallbackStop(Exception):
    pass


class _FakeCallbackAbort(Exception):
    pass


class _FakeStreamStatus:
    input_overflow = False
    output_underflow = False


class FakeLoopbackSoundDevice:
    CallbackStop = _FakeCallbackStop
    CallbackAbort = _FakeCallbackAbort

    def __init__(
        self,
        *,
        gain=0.7,
        delay_samples=480,
        fail_close=False,
        fail_start_formats=(),
    ):
        self.gain = gain
        self.delay_samples = delay_samples
        self.fail_close = fail_close
        self.fail_start_formats = set(fail_start_formats)
        self.streams = []
        self.stream_formats = []
        self.last_stream_kwargs = None
        self._devices = [
            {
                "name": "CABLE Input",
                "max_input_channels": 0,
                "max_output_channels": 2,
                "hostapi": 0,
                "default_samplerate": 48000.0,
            },
            {
                "name": "CABLE Output",
                "max_input_channels": 2,
                "max_output_channels": 0,
                "hostapi": 0,
                "default_samplerate": 48000.0,
            },
        ]

    def query_devices(self):
        return self._devices

    def query_hostapis(self):
        return [{"name": "Windows WASAPI"}]

    def check_input_settings(self, **_kwargs):
        return None

    def check_output_settings(self, **_kwargs):
        return None

    def Stream(self, **kwargs):
        import numpy as np

        owner = self
        owner.last_stream_kwargs = kwargs
        owner.stream_formats.append(
            (kwargs["samplerate"], kwargs["channels"][1])
        )

        class Stream:
            def __init__(self):
                self.abort_calls = 0
                self.stop_calls = 0
                self.close_calls = 0
                self.fail_close = owner.fail_close
                self._route_buffer = np.zeros(owner.delay_samples, dtype="int16")

            def start(self):
                if (
                    kwargs["samplerate"],
                    kwargs["channels"][1],
                ) in owner.fail_start_formats:
                    raise RuntimeError("simulated duplex format failure")
                callback = kwargs["callback"]
                output_channels = kwargs["channels"][1]
                block_frames = 256
                for _ in range(1000):
                    available = self._route_buffer[:block_frames]
                    self._route_buffer = self._route_buffer[block_frames:]
                    if len(available) < block_frames:
                        available = np.pad(
                            available, (0, block_frames - len(available))
                        )
                    routed = np.rint(available.astype(np.float64) * owner.gain)
                    routed = np.clip(routed, -32768, 32767).astype("int16")
                    indata = routed.reshape(-1, 1)
                    outdata = np.zeros(
                        (block_frames, output_channels), dtype="int16"
                    )
                    try:
                        callback(
                            indata,
                            outdata,
                            block_frames,
                            None,
                            _FakeStreamStatus(),
                        )
                    except _FakeCallbackStop:
                        self._route_buffer = np.concatenate(
                            (self._route_buffer, outdata[:, 0])
                        )
                        kwargs["finished_callback"]()
                        return
                    self._route_buffer = np.concatenate(
                        (self._route_buffer, outdata[:, 0])
                    )
                raise AssertionError("loopback callback did not stop")

            def abort(self):
                self.abort_calls += 1

            def stop(self):
                self.stop_calls += 1

            def close(self):
                self.close_calls += 1
                if self.fail_close:
                    raise RuntimeError("private device close failure")

        stream = Stream()
        self.streams.append(stream)
        return stream


class CableLoopbackProbeTests(unittest.TestCase):
    def tearDown(self):
        retained = audio_playback._retained_loopback_stream
        if retained is not None:
            retained.fail_close = False
            audio_playback.cleanup_retained_loopback_stream()
        retained_preflight = audio_playback._retained_preflight_sink
        if retained_preflight is not None:
            audio_playback.cleanup_retained_preflight_sink()

    @staticmethod
    def _endpoints():
        return (
            audio_output.AudioEndpoint("CABLE Input", "Windows WASAPI"),
            audio_output.AudioEndpoint("CABLE Output", "Windows WASAPI"),
        )

    def test_probe_uses_explicit_input_output_order_and_detects_route(self):
        sd = FakeLoopbackSoundDevice()
        output_endpoint, input_endpoint = self._endpoints()

        result = audio_playback.probe_virtual_cable_loopback(
            output_endpoint,
            input_endpoint,
            sd_module=sd,
        )

        self.assertTrue(result.detected)
        self.assertGreater(result.correlation, 0.99)
        self.assertAlmostEqual(result.roundtrip_latency_ms, 10.0, delta=6.0)
        self.assertEqual(sd.last_stream_kwargs["device"], (1, 0))
        self.assertEqual(sd.last_stream_kwargs["channels"], (1, 2))
        self.assertEqual(sd.streams[0].stop_calls, 1)
        self.assertEqual(sd.streams[0].close_calls, 1)

    def test_probe_retries_real_duplex_formats_after_start_failure(self):
        sd = FakeLoopbackSoundDevice(
            fail_start_formats={(48000, 2), (48000, 1)}
        )
        output_endpoint, input_endpoint = self._endpoints()

        result = audio_playback.probe_virtual_cable_loopback(
            output_endpoint,
            input_endpoint,
            sd_module=sd,
        )

        self.assertTrue(result.detected)
        self.assertEqual(
            sd.stream_formats[:3],
            [(48000, 2), (48000, 1), (44100, 2)],
        )
        self.assertEqual(sd.streams[0].close_calls, 1)
        self.assertEqual(sd.streams[1].close_calls, 1)
        self.assertEqual(sd.streams[2].stop_calls, 1)
        self.assertEqual(sd.streams[2].close_calls, 1)

    def test_silent_route_returns_an_honest_negative_result(self):
        sd = FakeLoopbackSoundDevice(gain=0.0)
        output_endpoint, input_endpoint = self._endpoints()

        result = audio_playback.probe_virtual_cable_loopback(
            output_endpoint,
            input_endpoint,
            sd_module=sd,
        )

        self.assertFalse(result.detected)
        self.assertEqual(result.signal_rms, 0.0)

    def test_cross_host_api_pair_is_rejected_before_opening_a_stream(self):
        sd = FakeLoopbackSoundDevice()
        output_endpoint = audio_output.AudioEndpoint(
            "CABLE Input", "Windows WASAPI"
        )
        input_endpoint = audio_output.AudioEndpoint("CABLE Output", "MME")

        with self.assertRaises(audio_playback.LoopbackProbeUnavailableError):
            audio_playback.probe_virtual_cable_loopback(
                output_endpoint,
                input_endpoint,
                sd_module=sd,
            )

        self.assertEqual(sd.streams, [])

    def test_close_failure_is_reported_and_owner_is_retained_for_retry(self):
        sd = FakeLoopbackSoundDevice(fail_close=True)
        output_endpoint, input_endpoint = self._endpoints()

        with self.assertRaises(audio_playback.LoopbackProbeUnavailableError) as ctx:
            audio_playback.probe_virtual_cable_loopback(
                output_endpoint,
                input_endpoint,
                sd_module=sd,
            )

        self.assertIn("释放", str(ctx.exception))
        self.assertIs(audio_playback._retained_loopback_stream, sd.streams[0])
        sd.streams[0].fail_close = False
        audio_playback.cleanup_retained_loopback_stream()
        self.assertIsNone(audio_playback._retained_loopback_stream)

    def test_probe_cleans_a_retained_preflight_owner_before_opening(self):
        class RetainedPreflight:
            def __init__(self):
                self.close_calls = 0
                self.owns_stream = True

            def close(self):
                self.close_calls += 1
                self.owns_stream = False

        retained = RetainedPreflight()
        audio_playback._retained_preflight_sink = retained
        sd = FakeLoopbackSoundDevice(gain=0.0)
        output_endpoint, input_endpoint = self._endpoints()

        audio_playback.probe_virtual_cable_loopback(
            output_endpoint,
            input_endpoint,
            sd_module=sd,
        )

        self.assertEqual(retained.close_calls, 1)
        self.assertIsNone(audio_playback._retained_preflight_sink)

    def test_nonblocking_shutdown_cleanup_does_not_wait_for_active_probe_lock(self):
        lock_acquired = threading.Event()
        release_lock = threading.Event()

        def _hold_lock():
            with audio_playback._portaudio_test_lock:
                lock_acquired.set()
                release_lock.wait(timeout=5.0)

        thread = threading.Thread(target=_hold_lock)
        thread.start()
        self.assertTrue(lock_acquired.wait(timeout=1.0))
        started_at = time.monotonic()
        try:
            cleaned = audio_playback.cleanup_retained_portaudio_test_resources(
                blocking=False
            )
        finally:
            release_lock.set()
            thread.join(timeout=1.0)

        self.assertFalse(cleaned)
        self.assertLess(time.monotonic() - started_at, 0.5)


if __name__ == "__main__":
    unittest.main()
