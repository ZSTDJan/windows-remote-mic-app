"""Exercises EndpointPlaybackSink's device-resolution logic with a fake
``sounddevice``-shaped object (it already takes ``sd`` as a parameter, which
makes this possible without the real optional dependency installed) - see
audio_output.py's identical name+host_api disambiguation contract, which
this mirrors for the actual PortAudio device-index lookup used at
``open()`` time (XRBM-014 review RETRY P2 #1).
"""

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


if __name__ == "__main__":
    unittest.main()
