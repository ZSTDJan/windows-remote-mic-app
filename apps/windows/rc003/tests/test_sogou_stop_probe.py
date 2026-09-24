"""Read-only probe bounds and redaction; never touch a live control."""
import subprocess
from types import SimpleNamespace
from unittest import TestCase, mock
from scripts import sogou_stop_probe as probe


class SogouStopProbeTests(TestCase):
    def test_names_do_not_collect_spoken_or_input_text(self):
        self.assertEqual(probe.action_label(' 关闭 '), '关闭')
        self.assertEqual(probe.action_label('我的测试语句'), '<非操作文字已省略>')
        self.assertEqual(probe.action_label(None), '')

    def test_subprocess_is_read_only_bounded_and_hidden(self):
        with mock.patch.object(probe.subprocess, 'run', return_value=SimpleNamespace(
                returncode=0, stdout='{"windows": []}')) as run:
            self.assertEqual(probe.collect_once(), {'windows': []})
        self.assertEqual(run.call_args.args[0][-1], '--snapshot')
        self.assertEqual(run.call_args.kwargs['timeout'], 4.0)
        self.assertEqual(run.call_args.kwargs['creationflags'], subprocess.CREATE_NO_WINDOW)

    def test_timeout_or_bad_result_is_unknown_not_stopped(self):
        for failure in (subprocess.TimeoutExpired('owned-probe', 4), ValueError()):
            with mock.patch.object(probe.subprocess, 'run', side_effect=failure):
                self.assertIn('error', probe.collect_once())
        with mock.patch.object(probe.subprocess, 'run', return_value=SimpleNamespace(returncode=2)):
            self.assertEqual(probe.collect_once()['error'], 'snapshot_failed')

    def test_tree_cap_does_not_call_an_action(self):
        import uiautomation
        control = mock.Mock()
        control.Name = '关闭'
        control.AutomationId = 'close'
        control.ControlTypeName = 'ButtonControl'
        control.BoundingRectangle = SimpleNamespace(left=1, top=2, right=3, bottom=4)
        control.GetChildren.return_value = [control]
        result = probe.controls(control, max_nodes=2)
        self.assertFalse(result['complete'])
        self.assertEqual(len(result['nodes']), 2)
        control.GetPattern.return_value.Invoke.assert_not_called()
        control.Click.assert_not_called()
