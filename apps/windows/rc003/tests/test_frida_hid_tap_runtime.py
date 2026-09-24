import hashlib
import ctypes
import lzma
import sys
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest import mock

from ovb_rc003 import frida_hid_tap_runtime, hid_elevation_windows
from ovb_rc003 import frida_hid_tap_injector as injector, hid_injection_diagnostics as diagnostics


SID = "S-1-5-21-111-222-333-1001"


@contextmanager
def isolated_runtime(sid=SID):
    with tempfile.TemporaryDirectory() as raw, ExitStack() as stack:
        root = Path(raw)
        payload = b"isolated gadget payload"
        archive = root / "gadget.xz"
        archive.write_bytes(lzma.compress(payload))
        def read_acl(path):
            return hid_elevation_windows._path_security_sddl_text(
                sid, directory=path.is_dir(),
                read_execute_sids=(hid_elevation_windows.LOCAL_SERVICE_SID,),
            )
        for module, name, value in (
            (frida_hid_tap_runtime, "gadget_archive_path", lambda: archive),
            (frida_hid_tap_runtime, "GADGET_ARCHIVE_SHA256", hashlib.sha256(archive.read_bytes()).hexdigest()),
            (frida_hid_tap_runtime, "GADGET_DLL_SHA256", hashlib.sha256(payload).hexdigest()),
            (hid_elevation_windows, "current_user_sid", lambda: sid),
            (hid_elevation_windows, "_program_files_root", lambda: root),
            (hid_elevation_windows, "ensure_protected_directory", lambda path, **kw: path.mkdir(parents=True, exist_ok=True)),
            (hid_elevation_windows, "assert_no_reparse_points", lambda *a, **kw: None),
            (hid_elevation_windows, "_apply_path_security", lambda *a, **kw: None),
            (hid_elevation_windows, "_read_path_security_sddl", read_acl),
        ):
            stack.enter_context(mock.patch.object(module, name, value))
        yield root, archive, read_acl


class ProtectedRuntimePathTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "Windows injector entrypoint")
    def test_validation_failures_preserve_distinct_reason_through_injector(self):
        for reason in diagnostics.REASONS:
            with self.subTest(reason=reason), isolated_runtime() as (_, archive, read_acl), ExitStack() as stack:
                if reason == "runtime_archive_hash_mismatch":
                    archive.write_bytes(b"corrupt")
                elif reason == "runtime_dll_hash_mismatch":
                    stack.enter_context(mock.patch.object(frida_hid_tap_runtime, "GADGET_DLL_SHA256", "bad"))
                else:
                    fail_directory = reason == "runtime_directory_acl_invalid"
                    stack.enter_context(mock.patch.object(
                        hid_elevation_windows, "_read_path_security_sddl",
                        side_effect=lambda path: "invalid" if path.is_dir() == fail_directory else read_acl(path),
                    ))
                stack.enter_context(mock.patch.object(injector, "find_rc003_hidogatt_host_pid", return_value=2468))
                stack.enter_context(mock.patch.object(injector, "enable_debug_privilege"))
                stack.enter_context(mock.patch.object(injector, "_target_process_name", return_value="wudfhost.exe"))
                inject = stack.enter_context(mock.patch.object(injector, "inject_library"))
                with self.assertRaises(injector.HidInjectionStageError) as caught:
                    injector.inject_current_process(2468)
                fields = diagnostics.failure(caught.exception)
                self.assertEqual(fields["stage"], "runtime_prepare")
                self.assertEqual(fields["reason"], reason)
                self.assertEqual(fields["error_type"], "RuntimeError")
                inject.assert_not_called()

    @unittest.skipUnless(sys.platform == "win32", "Windows SDDL serialization")
    def test_runtime_accepts_windows_serialized_builtin_administrator_acl(self):
        helper = hid_elevation_windows
        admin = helper._canonical_acl_sid("LA")
        self.assertTrue(admin.endswith("-500"))
        api = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        api.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = (
            ctypes.c_wchar_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p)
        api.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = (
            ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p)
        kernel.LocalFree.argtypes = (ctypes.c_void_p,)
        serialized = []
        with isolated_runtime(admin) as (_, _, read_acl):
            def native_read(path):
                # Windows maps generic file rights before reading the descriptor.
                text = read_acl(path).replace("GRGX", "0x1200a9").replace(";GR;", ";0x120089;")
                descriptor, output = ctypes.c_void_p(), ctypes.c_void_p()
                self.assertTrue(api.ConvertStringSecurityDescriptorToSecurityDescriptorW(text, 1, ctypes.byref(descriptor), None))
                try:
                    self.assertTrue(api.ConvertSecurityDescriptorToStringSecurityDescriptorW(descriptor, 1, 7, ctypes.byref(output), None))
                    result = ctypes.wstring_at(output)
                    serialized.append(result)
                    return result
                finally:
                    kernel.LocalFree(output)
                    kernel.LocalFree(descriptor)
            with mock.patch.object(helper, "_read_path_security_sddl", side_effect=native_read):
                self.assertTrue(frida_hid_tap_runtime.prepare_secure_runtime().is_file())
        self.assertEqual(len(serialized), 5)
        self.assertTrue(all(";;;LA)" in text for text in serialized))

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
        self.assertEqual(first.name, f"{frida_hid_tap_runtime.GADGET_VERSION}-x64-{frida_hid_tap_runtime.GADGET_DLL_SHA256[:12]}-reload")

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
