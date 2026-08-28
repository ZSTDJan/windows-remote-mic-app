"""Developer source-session marker tests without starting real processes."""

import os
import unittest
from unittest.mock import patch

from ovb_rc003 import bridge_launcher, dev_session


class DevSessionTests(unittest.TestCase):
    def test_marker_is_consumed_and_enables_child_inheritance(self):
        with patch.dict(os.environ, {}, clear=True):
            arguments = dev_session.consume_marker(
                ["--settings", dev_session.DEV_SESSION_FLAG]
            )

            self.assertEqual(arguments, ["--settings"])
            self.assertTrue(dev_session.is_active())
            self.assertEqual(os.environ[dev_session.DEV_SESSION_ENV], "1")

    def test_inactive_commands_are_unchanged(self):
        with patch.dict(os.environ, {}, clear=True):
            command = ["python.exe", "-m", "ovb_rc003", "--settings"]
            self.assertEqual(dev_session.mark_command(command), command)

    def test_source_bridge_and_settings_children_keep_the_marker(self):
        with patch.dict(
            os.environ,
            {dev_session.DEV_SESSION_ENV: "1"},
            clear=True,
        ):
            bridge_command = bridge_launcher.build_launch_command(
                frozen=False,
                executable=r"C:\Python312\pythonw.exe",
            )
            settings_command = bridge_launcher.build_settings_command(
                frozen=False,
                executable=r"C:\Python312\pythonw.exe",
            )

            self.assertEqual(bridge_command[-1], dev_session.DEV_SESSION_FLAG)
            self.assertEqual(settings_command[-1], dev_session.DEV_SESSION_FLAG)


if __name__ == "__main__":
    unittest.main()
