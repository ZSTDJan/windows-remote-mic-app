import unittest
from pathlib import Path

from ovb_rc003 import frida_hid_tap_runtime


class ProtectedRuntimePathTests(unittest.TestCase):
    def test_runtime_is_under_the_sid_isolated_program_files_root(self):
        program_files = Path(r"C:\Program Files")
        first = frida_hid_tap_runtime.secure_runtime_directory(
            user_sid="S-1-5-21-111-222-333-1001",
            program_files_root=program_files,
        )
        second = frida_hid_tap_runtime.secure_runtime_directory(
            user_sid="S-1-5-21-111-222-333-1002",
            program_files_root=program_files,
        )

        self.assertNotEqual(first, second)
        self.assertEqual(first.parents[5], program_files)
        self.assertNotIn("ProgramData", str(first))


if __name__ == "__main__":
    unittest.main()
