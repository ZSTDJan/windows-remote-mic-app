import hashlib
import lzma
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ovb_rc003 import frida_hid_tap_runtime, hid_elevation_windows


SID = "S-1-5-21-111-222-333-1001"


class ProtectedRuntimePathTests(unittest.TestCase):
    def test_runtime_is_under_the_sid_isolated_program_files_root(self):
        program_files = Path(r"C:\Program Files")
        first = frida_hid_tap_runtime.secure_runtime_directory(
            user_sid=SID,
            program_files_root=program_files,
        )
        second = frida_hid_tap_runtime.secure_runtime_directory(
            user_sid="S-1-5-21-111-222-333-1002",
            program_files_root=program_files,
        )

        self.assertNotEqual(first, second)
        self.assertEqual(first.parents[4], program_files)
        self.assertNotIn("ProgramData", str(first))
        self.assertIn(hid_elevation_windows.PROTECTED_RUNTIME_NAMESPACE, first.parts)
        self.assertNotIn(hid_elevation_windows.PROTECTED_NAMESPACE, first.parts)

    def test_prepare_runtime_uses_local_service_read_execute_acl(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            program_files = root / "Program Files"
            program_files.mkdir()
            payload = b"verified gadget payload"
            archive = root / "gadget.dll.xz"
            with lzma.open(archive, "wb") as stream:
                stream.write(payload)
            archive_hash = hashlib.sha256(archive.read_bytes()).hexdigest()
            payload_hash = hashlib.sha256(payload).hexdigest()
            ensure = mock.Mock()
            apply_security = mock.Mock()
            validate_security = mock.Mock(return_value=True)

            def ensure_directory(path, **_kwargs):
                path.mkdir(parents=True, exist_ok=True)
                return path

            ensure.side_effect = ensure_directory
            with mock.patch.object(
                frida_hid_tap_runtime, "GADGET_ARCHIVE_SHA256", archive_hash
            ), mock.patch.object(
                frida_hid_tap_runtime, "GADGET_DLL_SHA256", payload_hash
            ), mock.patch.object(
                frida_hid_tap_runtime, "gadget_archive_path", return_value=archive
            ), mock.patch.object(
                hid_elevation_windows, "current_user_sid", return_value=SID
            ), mock.patch.object(
                hid_elevation_windows,
                "_program_files_root",
                return_value=program_files,
            ), mock.patch.object(
                hid_elevation_windows,
                "ensure_protected_directory",
                ensure,
            ), mock.patch.object(
                hid_elevation_windows, "assert_no_reparse_points"
            ), mock.patch.object(
                hid_elevation_windows,
                "_apply_path_security",
                apply_security,
            ), mock.patch.object(
                hid_elevation_windows,
                "_read_path_security_sddl",
                return_value="verified",
            ), mock.patch.object(
                hid_elevation_windows,
                "validate_path_security_sddl",
                validate_security,
            ):
                dll_path = frida_hid_tap_runtime.prepare_secure_runtime()

                readers = (hid_elevation_windows.LOCAL_SERVICE_SID,)
                runtime_root = hid_elevation_windows.protected_runtime_owner_root(
                    SID, program_files_root=program_files
                )
                ensure.assert_called_once_with(
                    dll_path.parent,
                    user_sid=SID,
                    trusted_root=program_files,
                    security_root=runtime_root,
                    read_execute_sids=readers,
                )
                self.assertEqual(apply_security.call_count, 4)
                for call in apply_security.call_args_list:
                    self.assertFalse(call.kwargs["directory"])
                    self.assertEqual(call.kwargs["read_execute_sids"], readers)
                self.assertEqual(validate_security.call_count, 5)
                self.assertEqual(
                    [
                        call.kwargs["directory"]
                        for call in validate_security.call_args_list
                    ],
                    [True, True, False, False, False],
                )
                for call in validate_security.call_args_list:
                    self.assertEqual(call.kwargs["read_execute_sids"], readers)

                ensure.reset_mock()
                apply_security.reset_mock()
                validate_security.reset_mock()
                self.assertEqual(
                    frida_hid_tap_runtime.prepare_secure_runtime(), dll_path
                )

                ensure.assert_called_once()
                self.assertEqual(apply_security.call_count, 3)
                self.assertEqual(
                    {call.args[0].name for call in apply_security.call_args_list},
                    {
                        frida_hid_tap_runtime.GADGET_DLL_NAME,
                        frida_hid_tap_runtime.GADGET_CONFIG_NAME,
                        frida_hid_tap_runtime.GADGET_SCRIPT_NAME,
                    },
                )
                for call in apply_security.call_args_list:
                    self.assertFalse(call.kwargs["directory"])
                    self.assertEqual(call.kwargs["read_execute_sids"], readers)
                self.assertEqual(validate_security.call_count, 5)


if __name__ == "__main__":
    unittest.main()
