"""Probe safety and event-format checks; never send live keyboard input."""

import contextlib
import io
import json
import tempfile
from pathlib import Path
import unittest
from unittest import mock

from scripts import voice_hotkey_probe as probe


class VoiceHotkeyProbeTests(unittest.TestCase):
    def test_scan_modifier_format_matches_reference(self):
        for vk, scan, flags in ((0xA2, 0x1D, 8), (0xA0, 0x2A, 8), (0xA5, 0x38, 9)):
            with self.subTest(vk=vk):
                event = probe.planned_edge("sendinput-scan", vk, False)
                self.assertEqual((event["vk"], event["scan_code"], event["flags"]),
                                 (0, scan, flags))
                self.assertEqual(event["extra_info"], 0)
        event = probe.planned_edge("sendinput-scan", 0x78, False)
        self.assertEqual((event["vk"], event["scan_code"]), (0x78, 0))

    def test_virtual_key_format_preserves_vk(self):
        event = probe.planned_edge("sendinput-vk", 0xA5, True)
        self.assertEqual((event["vk"], event["scan_code"], event["flags"]),
                         (0xA5, 0, 3))

    def test_hold_releases_in_reverse_with_equal_edge_gaps(self):
        sender, sleep = mock.Mock(return_value=1), mock.Mock()
        report = {"edges": []}
        probe.run_hold([0xA2, 0xA0, 0x78], "sendinput-vk", 1.2, report,
                       mock.Mock(), sender=sender, sleep=sleep)
        self.assertEqual([(c.args[1], c.args[2]) for c in sender.call_args_list],
                         [(0xA2, False), (0xA0, False), (0x78, False),
                          (0x78, True), (0xA0, True), (0xA2, True)])
        self.assertEqual([c.args[0] for c in sleep.call_args_list],
                         [0.08, 0.08, 1.2, 0.08, 0.08])

    def test_ambiguous_down_releases_attempted_keys_only(self):
        sender = mock.Mock(side_effect=[1, OSError("unknown delivery"), 1, 1])
        report = {"edges": []}
        with self.assertRaises(OSError):
            probe.run_hold([0xA2, 0xA0, 0x78], "sendinput-vk", 1.2, report,
                           mock.Mock(), sender=sender, sleep=mock.Mock())
        self.assertEqual([(c.args[1], c.args[2]) for c in sender.call_args_list],
                         [(0xA2, False), (0xA0, False), (0xA0, True), (0xA2, True)])

    def test_release_failure_does_not_skip_other_releases(self):
        sender = mock.Mock(side_effect=[1, 1, 0, 1])
        report = {"edges": []}
        with self.assertRaisesRegex(OSError, "Release"):
            probe.run_hold([0xA2, 0xA0], "sendinput-vk", 1.2, report,
                           mock.Mock(), sender=sender, sleep=mock.Mock())
        self.assertEqual(sender.call_args.args[1:], (0xA2, True))
        self.assertEqual(len(report["release_errors"]), 1)

    def test_focus_loss_releases_previous_key(self):
        sender = mock.Mock(return_value=1)
        with self.assertRaisesRegex(RuntimeError, "focus"):
            probe.run_hold([0xA2, 0x78], "sendinput-vk", 1.2, {"edges": []},
                           mock.Mock(side_effect=[None, RuntimeError("focus")]),
                           sender=sender, sleep=mock.Mock())
        self.assertEqual([(c.args[1], c.args[2]) for c in sender.call_args_list],
                         [(0xA2, False), (0xA2, True)])

    def test_interrupt_during_hold_still_releases(self):
        sender = mock.Mock(return_value=None)
        report = {"edges": []}
        with self.assertRaises(KeyboardInterrupt):
            probe.run_hold([0xA5], "keybd-event", 1.2, report, mock.Mock(),
                           sender=sender, sleep=mock.Mock(side_effect=KeyboardInterrupt))
        self.assertEqual(sender.call_args.args[1:], (0xA5, True))
        self.assertIsNone(report["edges"][0]["returned"])

    def test_dry_run_does_not_send_or_read_host(self):
        with mock.patch.object(probe, "run_hold") as hold, \
                mock.patch.object(probe, "environment") as env, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(probe.main([
                "--provider", "doubao", "--hotkey", "ralt",
                "--method", "sendinput-scan",
            ]), 0)
        self.assertEqual(json.loads(output.getvalue())["mode"], "plan_only")
        hold.assert_not_called()
        env.assert_not_called()

    def test_backends_are_raw_and_legacy_has_no_marker(self):
        with mock.patch.object(probe.win32_input, "_real_send_input_batch",
                               return_value=1) as scan, \
                mock.patch.object(probe.win32_input, "_real_send_virtual_key_input_batch",
                                  return_value=1) as vk, \
                mock.patch.object(probe.win32_input, "_real_keybd_event") as legacy:
            self.assertEqual(probe.send_edge("sendinput-scan", 0xA5, False), 1)
            self.assertEqual(probe.send_edge("sendinput-vk", 0xA5, True), 1)
            self.assertIsNone(probe.send_edge("keybd-event", 0xA5, False))
        scan.assert_called_once_with([(0xA5, False)])
        vk.assert_called_once_with([(0xA5, True)])
        legacy.assert_called_once_with(0xA5, False, _extra_info=0)

    def test_running_app_blocks_probe(self):
        with mock.patch.object(probe.single_instance, "application_instance_running",
                               return_value=True):
            with self.assertRaisesRegex(RuntimeError, "Exit Remote Mic"):
                probe.require_app_stopped()

    def test_compatibility_dry_run_never_attaches(self):
        with mock.patch.object(probe, "doubao_compatibility") as compat, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(probe.main([
                "--provider", "doubao", "--hotkey", "ralt", "--method", "doubao-compat",
            ]), 0)
        compat.assert_not_called()
        self.assertIn("ticket", json.loads(output.getvalue())["plan"][0]["extra_info"])

    def test_compatibility_is_restricted_to_right_alt(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            probe.main([
                "--provider", "doubao", "--hotkey", "lalt", "--method", "doubao-compat",
            ])

    def test_compatibility_cleans_both_components_on_failure(self):
        for failure in ("startup", "sending", "cleanup"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                with mock.patch.object(probe.win32_input, "_require_live_input_allowed"), \
                        mock.patch.object(probe, "require_app_stopped"), \
                        mock.patch.object(probe.voice_key_physicalizer_windows,
                                          "VoiceKeyPhysicalizer") as local_type, \
                        mock.patch.object(probe.doubao_rpc, "DoubaoPhysicalizer") as target_type, \
                        mock.patch.object(probe.diagnostic_trace, "DiagnosticTrace") as trace_type:
                    local, target = local_type.return_value, target_type.return_value
                    target.start.return_value = failure != "startup"
                    if failure == "cleanup":
                        target.stop.side_effect = RuntimeError("detach failed")
                    report = {}
                    with self.assertRaises(RuntimeError):
                        with probe.doubao_compatibility(report, Path(directory) / "trace"):
                            raise RuntimeError("send failed")
                    target.stop.assert_called_once()
                    local.stop.assert_called_once()
                    trace_type.return_value.close.assert_called_once()
                    self.assertEqual(report["compatibility"]["target_stopped"],
                                     failure != "cleanup")

    def test_compatibility_marks_and_releases_using_existing_backend(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(probe.win32_input, "_require_live_input_allowed"), \
                mock.patch.object(probe.win32_input, "_real_voice_event") as voice, \
                mock.patch.object(probe, "require_app_stopped"), \
                mock.patch.object(probe.voice_key_physicalizer_windows,
                                  "VoiceKeyPhysicalizer") as local_type, \
                mock.patch.object(probe.doubao_rpc, "DoubaoPhysicalizer") as target_type, \
                mock.patch.object(probe.diagnostic_trace, "DiagnosticTrace"):
            target_type.return_value.start.return_value = True
            with probe.doubao_compatibility({}, Path(directory) / "trace") as sender:
                sender("doubao-compat", 0xA5, False)
                sender("doubao-compat", 0xA5, True)
            self.assertEqual(voice.call_args_list, [mock.call(0xA5, False), mock.call(0xA5, True)])
            target_type.return_value.start.assert_called_once_with([0xA5])
            self.assertEqual(target_type.return_value.expect_markers.call_args_list,
                             [mock.call("down", 1), mock.call("up", 1)])
            local_type.return_value.stop.assert_called_once()
            target_type.return_value.stop.assert_called_once()

    def test_diagnostic_uses_production_start_without_a_discovery_override(self):
        target = mock.Mock()
        target.start.return_value = True
        probe.start_doubao_target(target)
        self.assertEqual(target.mock_calls, [mock.call.start([0xA5])])

    def test_diagnostic_never_bypasses_production_failure(self):
        for status, error in (
            ("unavailable", "ImeService.exe is not running"),
            ("unsupported_version", "ImeService.exe version is not verified"),
            ("cleanup_required", "retained resources"),
        ):
            with self.subTest(status=status):
                target = mock.Mock(status=status, error=error)
                target.start.return_value = False
                with self.assertRaisesRegex(RuntimeError, status):
                    probe.start_doubao_target(target)
                self.assertEqual(target.mock_calls, [mock.call.start([0xA5])])

    def test_live_input_gate_blocks_before_delay_and_send(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "result.json"
            with mock.patch.dict("os.environ", {"RC003_DISABLE_LIVE_INPUT": "1"}), \
                    mock.patch.object(probe.sys, "platform", "win32"), \
                    mock.patch.object(probe, "run_hold") as hold, \
                    mock.patch.object(probe.time, "sleep") as sleep, \
                    contextlib.redirect_stdout(io.StringIO()):
                result = probe.main([
                    "--provider", "doubao", "--hotkey", "ralt", "--send",
                    "--method", "sendinput-scan", "--target-pid", "123",
                    "--output", str(output),
                ])
            self.assertEqual(result, 1)
            self.assertIn("disabled", json.loads(output.read_text())["error"])
            hold.assert_not_called()
            sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
