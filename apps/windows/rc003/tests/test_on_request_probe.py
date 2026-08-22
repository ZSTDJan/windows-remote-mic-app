import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from ovb_rc003 import atvv_protocol as proto
from ovb_rc003 import atvv_session
from ovb_rc003 import on_request_probe
from ovb_rc003 import single_instance


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def _caps(interaction=0):
    return proto.ATVVCapabilities(
        version=0x0100,
        codecs=0x02,
        interaction=interaction,
        frame_size=120,
        selected_codec=0x02,
        sample_rate=16000.0,
    )


class OnRequestProbeStateTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.state = on_request_probe.OnRequestProbeState(clock=self.clock)
        self.state.note_capabilities(_caps())

    def test_first_start_search_opens_and_later_press_closes(self):
        self.assertEqual(
            self.state.handle_control(atvv_session.MicButtonPressed()),
            on_request_probe.ProbeAction.NONE,
        )
        self.state.arm()
        self.assertEqual(
            self.state.handle_control(atvv_session.MicButtonPressed()),
            on_request_probe.ProbeAction.SEND_OPEN,
        )
        self.clock.now = 0.2
        self.assertEqual(
            self.state.handle_control(atvv_session.MicButtonPressed()),
            on_request_probe.ProbeAction.NONE,
        )
        self.clock.now = 2.0
        self.assertEqual(
            self.state.handle_control(atvv_session.MicButtonPressed()),
            on_request_probe.ProbeAction.SEND_CLOSE,
        )
        snapshot = self.state.snapshot()
        self.assertEqual(snapshot["events"]["start_search_count"], 3)
        self.assertTrue(snapshot["events"]["open_sent"])
        self.assertTrue(snapshot["events"]["close_sent"])

    def test_audio_reason_codec_stream_and_stop_reason_are_recorded(self):
        self.state.arm()
        self.state.handle_control(atvv_session.MicButtonPressed())
        self.clock.now = 0.1
        self.state.handle_control(
            atvv_session.AudioStarted(
                session_id=0,
                reason=0,
                codec=2,
            )
        )
        self.clock.now = 0.4
        action = self.state.handle_control(atvv_session.AudioStopped(reason=2))

        self.assertEqual(action, on_request_probe.ProbeAction.NONE)
        events = self.state.snapshot()["events"]
        self.assertEqual(events["first_audio_start_reason"], 0)
        self.assertEqual(events["first_audio_start_codec"], 2)
        self.assertEqual(events["first_audio_stream_id"], 0)
        self.assertEqual(events["last_audio_stop_reason"], 2)

    def test_pcm_after_one_second_is_classified_as_sustained(self):
        self.state.arm()
        self.state.handle_control(atvv_session.MicButtonPressed())
        self.state.handle_control(
            atvv_session.AudioStarted(session_id=0, reason=0, codec=2)
        )
        self.clock.now = 0.2
        self.state.handle_pcm([1, 2])
        self.clock.now = 1.2
        self.state.handle_pcm([3, 4, 5])

        snapshot = self.state.snapshot()
        self.assertEqual(snapshot["outcome"], "pcm_continued_past_1000ms")
        self.assertEqual(snapshot["pcm"]["mic_open_batches_after_1000ms"], 1)
        self.assertEqual(snapshot["pcm"]["mic_open_samples_after_1000ms"], 3)
        self.assertEqual(snapshot["pcm"]["last_pcm_ms_after_start_search"], 1200)

    def test_long_htt_stream_cannot_false_positive_as_on_request(self):
        self.state.arm()
        self.state.handle_control(atvv_session.MicButtonPressed())
        self.state.handle_control(
            atvv_session.AudioStarted(session_id=1, reason=3, codec=2)
        )
        self.clock.now = 1.5
        self.state.handle_pcm([1, 2, 3])

        snapshot = self.state.snapshot()
        self.assertEqual(snapshot["outcome"], "no_mic_open_audio_start")
        self.assertEqual(snapshot["pcm"]["samples"], 3)
        self.assertEqual(snapshot["pcm"]["mic_open_samples"], 0)

    def test_pcm_after_mic_open_stream_stops_is_not_sustained_evidence(self):
        self.state.arm()
        self.state.handle_control(atvv_session.MicButtonPressed())
        self.state.handle_control(
            atvv_session.AudioStarted(session_id=0, reason=0, codec=2)
        )
        self.clock.now = 0.2
        self.state.handle_pcm([1, 2])
        self.state.handle_control(atvv_session.AudioStopped(reason=2))
        self.clock.now = 1.5
        self.state.handle_pcm([3, 4, 5])

        snapshot = self.state.snapshot()
        self.assertEqual(
            snapshot["outcome"],
            "pcm_did_not_continue_past_1000ms",
        )
        self.assertEqual(snapshot["pcm"]["samples"], 5)
        self.assertEqual(snapshot["pcm"]["mic_open_samples"], 2)
        self.assertEqual(snapshot["pcm"]["mic_open_samples_after_1000ms"], 0)

    def test_control_only_start_is_not_mistaken_for_audio(self):
        self.state.arm()
        self.state.handle_control(atvv_session.MicButtonPressed())
        self.state.handle_control(
            atvv_session.AudioStarted(session_id=0, reason=0, codec=2)
        )

        self.assertEqual(self.state.snapshot()["outcome"], "control_only_no_pcm")


class ResultFileTests(unittest.TestCase):
    def test_result_is_atomic_json_without_voice_content(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.json"
            result = {
                "schema": 1,
                "outcome": "control_only_no_pcm",
                "events": {"start_search_count": 1},
                "pcm": {"samples": 0},
            }

            on_request_probe.write_probe_result(path, result)

            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8")),
                result,
            )
            self.assertEqual(list(path.parent.glob("*.tmp")), [])


class RunProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_start_search_open_then_second_press_flow(self):
        clock = FakeClock()
        holder = {}

        class FakeSession:
            def __init__(self, **kwargs):
                self.on_pcm_frame = kwargs["on_pcm_frame"]
                self.on_control_event = kwargs["on_control_event"]
                self.on_error = kwargs["on_error"]
                self.on_disconnected = kwargs["on_disconnected"]
                self.capabilities_command = kwargs["get_capabilities_command"]
                self.open_calls = 0
                self.close_calls = 0
                self.closed = False
                holder["session"] = self

            async def connect(self, candidate):
                self.on_control_event(atvv_session.CapsReceived(_caps()))

            def send_mic_open_threadsafe(self):
                self.open_calls += 1

            def send_mic_close_threadsafe(self):
                self.close_calls += 1

            async def close(self):
                self.closed = True

        async def discover_candidates():
            return [object()]

        async def select_candidate(candidates):
            return candidates[0]

        def show_notice(_title, _message):
            loop = asyncio.get_running_loop()

            def run_physical_flow():
                session = holder["session"]
                session.on_control_event(atvv_session.MicButtonPressed())
                session.on_control_event(
                    atvv_session.AudioStarted(session_id=0, reason=0, codec=2)
                )
                clock.now = 0.2
                session.on_pcm_frame([1, 2])
                clock.now = 1.2
                session.on_pcm_frame([3, 4, 5])
                clock.now = 3.0
                session.on_control_event(atvv_session.MicButtonPressed())

            loop.call_soon(run_physical_flow)

        result = await on_request_probe.run_probe(
            show_notice=show_notice,
            discover_candidates=discover_candidates,
            select_candidate=select_candidate,
            session_factory=FakeSession,
            clock=clock,
            capabilities_timeout=0.1,
            probe_timeout=0.1,
            final_drain_seconds=0.0,
        )

        session = holder["session"]
        self.assertEqual(
            session.capabilities_command,
            proto.GET_CAPABILITIES_ON_REQUEST_V10,
        )
        self.assertEqual(session.open_calls, 1)
        self.assertEqual(session.close_calls, 1)
        self.assertTrue(session.closed)
        self.assertEqual(result["outcome"], "pcm_continued_past_1000ms")
        self.assertEqual(result["events"]["start_search_count"], 2)
        self.assertEqual(result["events"]["audio_start_count"], 1)
        self.assertEqual(result["events"]["audio_stop_count"], 0)
        self.assertEqual(result["pcm"]["mic_open_samples_after_1000ms"], 3)

    async def test_cleanup_failure_does_not_mask_primary_discovery_error(self):
        class FakeSession:
            def __init__(self, **_kwargs):
                self.closed = False

            async def close(self):
                self.closed = True
                raise RuntimeError("cleanup failed")

        async def discover_candidates():
            raise LookupError("discovery failed")

        with self.assertRaisesRegex(LookupError, "discovery failed"):
            await on_request_probe.run_probe(
                show_notice=lambda _title, _message: None,
                discover_candidates=discover_candidates,
                session_factory=FakeSession,
            )


class EntrypointTests(unittest.TestCase):
    def test_successful_probe_swallows_f5_for_the_full_probe_lifecycle(self):
        lifecycle = []
        notices = []
        opened = []

        class Guard:
            def __enter__(self):
                lifecycle.append("guard_enter")
                return self

            def __exit__(self, exc_type, exc, tb):
                lifecycle.append("guard_exit")
                return False

        class Suppressor:
            def start(self):
                lifecycle.append("f5_start")

            def stop(self):
                lifecycle.append("f5_stop")

        def run_probe(show_notice):
            lifecycle.append("probe")
            show_notice("probe", "armed")
            return on_request_probe._failure_result("no_start_search")

        with tempfile.TemporaryDirectory() as directory:
            original_path = on_request_probe.probe_result_path
            on_request_probe.probe_result_path = (
                lambda: Path(directory) / "result.json"
            )
            try:
                exit_code = on_request_probe.main(
                    show_notice=lambda title, message: notices.append(
                        (title, message)
                    ),
                    open_result_directory=opened.append,
                    guard_factory=Guard,
                    f5_suppressor_factory=Suppressor,
                    probe_runner=run_probe,
                )
            finally:
                on_request_probe.probe_result_path = original_path

        self.assertEqual(exit_code, on_request_probe.PROBE_COMPLETED_EXIT_CODE)
        self.assertEqual(
            lifecycle,
            ["guard_enter", "f5_start", "probe", "f5_stop", "guard_exit"],
        )
        self.assertEqual(len(notices), 3)
        self.assertEqual(len(opened), 1)

    def test_duplicate_bridge_writes_fresh_blocked_result(self):
        notices = []
        opened = []

        class DuplicateGuard:
            def __enter__(self):
                raise single_instance.DuplicateInstanceError("already running")

            def __exit__(self, exc_type, exc, tb):
                return False

        with tempfile.TemporaryDirectory() as directory:
            original_path = on_request_probe.probe_result_path
            on_request_probe.probe_result_path = (
                lambda: Path(directory) / "result.json"
            )
            try:
                exit_code = on_request_probe.main(
                    show_notice=lambda title, message: notices.append(
                        (title, message)
                    ),
                    open_result_directory=opened.append,
                    guard_factory=DuplicateGuard,
                )
            finally:
                on_request_probe.probe_result_path = original_path

            result = json.loads(
                (Path(directory) / "result.json").read_text("utf-8")
            )

        self.assertEqual(
            exit_code,
            on_request_probe.PROBE_BLOCKED_EXIT_CODE,
        )
        self.assertEqual(result["outcome"], "bridge_already_running")
        self.assertEqual(len(notices), 1)
        self.assertIn("上一次专项检测", notices[0][1])
        self.assertEqual(len(opened), 1)

    def test_f5_guard_start_failure_blocks_probe(self):
        notices = []
        opened = []
        probe_calls = []

        class Guard:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        class Suppressor:
            def start(self):
                raise on_request_probe.legacy_key_suppressor_windows.LegacyKeySuppressorUnavailableError(
                    "simulated hook failure"
                )

            def stop(self):
                raise AssertionError("an unstarted guard must not be stopped")

        with tempfile.TemporaryDirectory() as directory:
            original_path = on_request_probe.probe_result_path
            on_request_probe.probe_result_path = (
                lambda: Path(directory) / "result.json"
            )
            try:
                exit_code = on_request_probe.main(
                    show_notice=lambda title, message: notices.append(
                        (title, message)
                    ),
                    open_result_directory=opened.append,
                    guard_factory=Guard,
                    f5_suppressor_factory=Suppressor,
                    probe_runner=lambda _show_notice: probe_calls.append(1),
                )
            finally:
                on_request_probe.probe_result_path = original_path

            result = json.loads(
                (Path(directory) / "result.json").read_text("utf-8")
            )

        self.assertEqual(exit_code, on_request_probe.PROBE_FAILED_EXIT_CODE)
        self.assertEqual(result["outcome"], "f5_suppressor_unavailable")
        self.assertEqual(probe_calls, [])
        self.assertEqual(len(notices), 1)
        self.assertIn("没有执行能力判断", notices[0][1])
        self.assertEqual(len(opened), 1)


if __name__ == "__main__":
    unittest.main()
