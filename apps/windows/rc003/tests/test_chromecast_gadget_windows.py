"""Narrow Google Gadget fallback and its local privilege boundary."""
from __future__ import annotations

from pathlib import Path
import socket
from types import SimpleNamespace
import unittest
from unittest import mock

from ovb_rc003 import chromecast_gadget_windows as gadget
from ovb_rc003 import chromecast_hid_tap_windows as tap
from ovb_rc003 import frida_hid_tap_runtime as assets
from ovb_rc003 import hid_elevation_windows as security


SID = "S-1-5-21-111-222-333-1001"


class GoogleGadgetSecurityTests(unittest.TestCase):
    def test_token_acl_excludes_ordinary_user_and_rejects_extra_aces(self):
        text = security._path_security_sddl_text(
            SID, directory=False, read_execute_sids=(security.LOCAL_SERVICE_SID,),
            include_user=False,
        )
        self.assertTrue(security.validate_path_security_sddl(
            text, user_sid=SID, directory=False,
            read_execute_sids=(security.LOCAL_SERVICE_SID,), include_user=False,
        ))
        self.assertFalse(security.validate_path_security_sddl(
            text, user_sid=SID, directory=False,
            read_execute_sids=(security.LOCAL_SERVICE_SID,),
        ))
        self.assertNotIn(SID, text)

    def test_runtime_config_rejects_changed_auth_or_listener_policy(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / gadget.CONFIG_NAME
            config = gadget._config(41000, "a" * 64)
            path.write_text(json.dumps(config), encoding="utf-8")
            self.assertEqual(gadget._read_config(path), (41000, "a" * 64))
            config["interaction"]["on_port_conflict"] = "pick-next"
            path.write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaises(gadget.GadgetAttachError):
                gadget._read_config(path)

    def test_token_directory_is_locked_before_config_is_written(self):
        import tempfile
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            owner, root = base / "owner", base / "owner" / "google"
            root.mkdir(parents=True)
            (root / gadget.DLL_NAME).write_bytes(b"test")
            archive = base / "archive.xz"
            archive.write_bytes(b"test")
            locked = False
            strict = security._path_security_sddl_text(
                SID, directory=True, read_execute_sids=(security.LOCAL_SERVICE_SID,),
                include_user=False)
            def apply(path, **kwargs):
                nonlocal locked
                if path == root:
                    self.assertFalse(kwargs["include_user"])
                    locked = True
            def write(path, content, **kwargs):
                self.assertTrue(locked)
                self.assertFalse(kwargs["include_user"])
                path.write_text(content, encoding="utf-8")
            with mock.patch.object(security, "query_process_elevated", return_value=True), \
                 mock.patch.object(security, "current_user_sid", return_value=SID), \
                 mock.patch.object(security, "ensure_protected_directory"), \
                 mock.patch.object(security, "assert_no_reparse_points"), \
                 mock.patch.object(security, "_apply_path_security", side_effect=apply), \
                 mock.patch.object(security, "_read_path_security_sddl", return_value=strict), \
                 mock.patch.object(gadget, "_runtime_paths", return_value=(base, owner, root)), \
                 mock.patch.object(gadget, "existing_endpoint", return_value=None), \
                 mock.patch.object(gadget, "_choose_port", return_value=41000), \
                 mock.patch.object(gadget, "_assert_runtime_security"), \
                 mock.patch.object(assets, "gadget_archive_path", return_value=archive), \
                 mock.patch.object(assets, "sha256_file", side_effect=lambda path:
                                   assets.GADGET_ARCHIVE_SHA256 if path == archive else assets.GADGET_DLL_SHA256), \
                 mock.patch.object(assets, "_write_verified_text", side_effect=write):
                self.assertEqual(gadget.prepare_secure_runtime().port, 41000)
            self.assertTrue(locked)

    def test_shared_host_or_wrong_google_dll_blocks_second_runtime(self):
        endpoint = gadget.Endpoint(Path(r"C:\Program Files\RemoteMic\Google\RemoteMicGoogleHidTap.dll"),
                                   41000, "a" * 64)
        for path in (
            Path(r"C:\Program Files\RemoteMic\RC003\RemoteMicRC003HidTap.dll"),
            Path(r"C:\Temp\frida-agent.dll"),
            Path(r"C:\Other\RemoteMicGoogleHidTap.dll"),
        ):
            with self.subTest(path=path), self.assertRaises(gadget.GadgetAttachError):
                gadget._check_modules(gadget.HostSnapshot(1, (path,)), endpoint)
        self.assertTrue(gadget._check_modules(gadget.HostSnapshot(1, (endpoint.dll,)), endpoint))

    @unittest.skipUnless(__import__("sys").platform == "win32", "Windows TCP owner table")
    def test_wrong_listener_pid_is_rejected_before_frida_connection(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            endpoint = gadget.Endpoint(Path("unused.dll"), listener.getsockname()[1], "a" * 64)
            manager = mock.Mock()
            frida = SimpleNamespace(get_device_manager=lambda: manager,
                                    Cancellable=mock.Mock())
            with self.assertRaises(gadget.GadgetAttachError) as caught:
                gadget._connect(frida, endpoint, 999999, 1)
            self.assertEqual(caught.exception.detail, "gadget_listener_conflict")
            manager.add_remote_device.assert_not_called()

    def test_uncertain_injection_never_retries_same_host(self):
        endpoint = gadget.Endpoint(Path("fixed.dll"), 41000, "a" * 64)
        original = gadget.HostSnapshot(123, ())
        with mock.patch.object(security, "query_process_elevated", return_value=True), \
             mock.patch.object(gadget, "enable_debug_privilege"), \
             mock.patch.object(gadget, "_host_snapshot", return_value=original), \
             mock.patch.object(gadget, "prepare_secure_runtime", return_value=endpoint), \
             mock.patch.object(gadget, "_read_attempt", return_value=(42, 123)), \
             mock.patch.object(security, "_run_serialized_helper_operation",
                               side_effect=lambda operation, **_kw: operation()), \
             mock.patch.object(gadget, "inject_library") as inject:
            with self.assertRaises(gadget.GadgetAttachError) as caught:
                gadget.attach(mock.Mock(), 42, "010203040506", lambda: (42, "010203040506"))
            self.assertEqual(caught.exception.detail, "gadget_inject_uncertain")
            inject.assert_not_called()

    def test_route_does_not_enable_debug_or_query_host_before_direct_attach(self):
        import tempfile
        endpoint = gadget.Endpoint(Path("fixed.dll"), 41000, "a" * 64)
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            with mock.patch.object(gadget, "_runtime_paths", return_value=(base, base, base / "missing")), \
                 mock.patch.object(gadget, "existing_endpoint", return_value=None), \
                 mock.patch.object(gadget, "_host_snapshot") as snapshot:
                self.assertFalse(gadget.loaded_or_pending_for_host(42))
                snapshot.assert_not_called()
            root = base / "existing"
            root.mkdir()
            with mock.patch.object(gadget, "_runtime_paths", return_value=(base, base, root)), \
                 mock.patch.object(gadget, "existing_endpoint", return_value=None):
                self.assertFalse(gadget.loaded_or_pending_for_host(42))
                (root / gadget.ATTEMPT_NAME).write_text("pending", encoding="utf-8")
                with self.assertRaises(gadget.GadgetAttachError) as caught:
                    gadget.loaded_or_pending_for_host(42)
                self.assertEqual(caught.exception.detail, "gadget_config_invalid")
        with mock.patch.object(gadget, "_runtime_paths", return_value=(Path("."), Path("."), Path("."))), \
             mock.patch.object(gadget, "existing_endpoint", return_value=endpoint), \
             mock.patch.object(gadget, "_listener_owner_pid", return_value=None), \
             mock.patch.object(gadget, "_read_attempt", return_value=(41, 123)):
            self.assertFalse(gadget.loaded_or_pending_for_host(42))
        with mock.patch.object(gadget, "_runtime_paths", return_value=(Path("."), Path("."), Path("."))), \
             mock.patch.object(gadget, "existing_endpoint", return_value=endpoint), \
             mock.patch.object(gadget, "_listener_owner_pid", return_value=None), \
             mock.patch.object(gadget, "_read_attempt", return_value=(42, 123)), \
             mock.patch.object(gadget, "_process_birth_if_alive", return_value=123):
            with self.assertRaises(gadget.GadgetAttachError) as caught:
                gadget.loaded_or_pending_for_host(42)
            self.assertEqual(caught.exception.detail, "gadget_inject_uncertain")
        with mock.patch.object(gadget, "_runtime_paths", return_value=(Path("."), Path("."), Path("."))), \
             mock.patch.object(gadget, "existing_endpoint", return_value=endpoint), \
             mock.patch.object(gadget, "_listener_owner_pid", return_value=None), \
             mock.patch.object(gadget, "_read_attempt", return_value=(42, 123)), \
             mock.patch.object(gadget, "_process_birth_if_alive", return_value=124):
            self.assertFalse(gadget.loaded_or_pending_for_host(42))
        with mock.patch.object(gadget, "_runtime_paths", return_value=(Path("."), Path("."), Path("."))), \
             mock.patch.object(gadget, "existing_endpoint", return_value=endpoint), \
             mock.patch.object(gadget, "_listener_owner_pid", return_value=42), \
             mock.patch.object(gadget, "_read_attempt") as marker:
            self.assertTrue(gadget.loaded_or_pending_for_host(42))
            marker.assert_not_called()

    def test_stop_before_native_load_never_injects(self):
        import threading
        endpoint = gadget.Endpoint(Path("fixed.dll"), 41000, "a" * 64)
        stopped = threading.Event()
        def inspect_rc003(_pid):
            stopped.set()
            return False
        with mock.patch.object(security, "query_process_elevated", return_value=True), \
             mock.patch.object(gadget, "enable_debug_privilege"), \
             mock.patch.object(gadget, "_host_snapshot", return_value=gadget.HostSnapshot(123, ())), \
             mock.patch.object(gadget, "prepare_secure_runtime", return_value=endpoint), \
             mock.patch.object(gadget, "_read_attempt", return_value=None), \
             mock.patch.object(gadget, "_rc003_host_member", side_effect=inspect_rc003), \
             mock.patch.object(security, "_run_serialized_helper_operation",
                               side_effect=lambda operation, **_kw: operation()), \
             mock.patch.object(gadget, "inject_library") as inject, \
             mock.patch.object(gadget, "_write_attempt") as marker:
            with self.assertRaises(gadget.GadgetAttachError) as caught:
                gadget.attach(mock.Mock(), 42, "010203040506", lambda: (42, "010203040506"),
                              cancelled=stopped.is_set)
            self.assertEqual(caught.exception.detail, "gadget_cancelled")
            inject.assert_not_called()
            marker.assert_not_called()

    def test_rc003_membership_blocks_first_google_load(self):
        endpoint = gadget.Endpoint(Path("fixed.dll"), 41000, "a" * 64)
        with mock.patch.object(security, "query_process_elevated", return_value=True), \
             mock.patch.object(gadget, "enable_debug_privilege"), \
             mock.patch.object(gadget, "_host_snapshot", return_value=gadget.HostSnapshot(123, ())), \
             mock.patch.object(gadget, "prepare_secure_runtime", return_value=endpoint), \
             mock.patch.object(gadget, "_read_attempt", return_value=None), \
             mock.patch.object(gadget, "_rc003_host_member", return_value=True), \
             mock.patch.object(security, "_run_serialized_helper_operation",
                               side_effect=lambda operation, **_kw: operation()), \
             mock.patch.object(gadget, "inject_library") as inject:
            with self.assertRaises(gadget.GadgetAttachError) as caught:
                gadget.attach(mock.Mock(), 42, "010203040506", lambda: (42, "010203040506"))
            self.assertEqual(caught.exception.detail, "gadget_rc003_host_conflict")
            inject.assert_not_called()

    def test_live_previous_host_cannot_lose_its_injection_marker(self):
        endpoint = gadget.Endpoint(Path("fixed.dll"), 41000, "a" * 64)
        with mock.patch.object(security, "query_process_elevated", return_value=True), \
             mock.patch.object(gadget, "enable_debug_privilege"), \
             mock.patch.object(gadget, "_host_snapshot", return_value=gadget.HostSnapshot(123, ())), \
             mock.patch.object(gadget, "prepare_secure_runtime", return_value=endpoint), \
             mock.patch.object(gadget, "_read_attempt", return_value=(41, 111)), \
             mock.patch.object(gadget, "_process_birth_if_alive", return_value=111), \
             mock.patch.object(security, "_run_serialized_helper_operation",
                               side_effect=lambda operation, **_kw: operation()), \
             mock.patch.object(gadget, "inject_library") as inject, \
             mock.patch.object(gadget, "_write_attempt") as marker:
            with self.assertRaises(gadget.GadgetAttachError) as caught:
                gadget.attach(mock.Mock(), 42, "010203040506", lambda: (42, "010203040506"))
            self.assertEqual(caught.exception.detail, "gadget_listener_conflict")
            inject.assert_not_called()
            marker.assert_not_called()

    @unittest.skipUnless(__import__("sys").platform == "win32", "Windows process times")
    def test_process_birth_is_stable_for_our_own_process(self):
        import os
        born = gadget._process_birth_if_alive(os.getpid())
        self.assertIsInstance(born, int)
        self.assertGreater(born, 0)
        self.assertEqual(gadget._process_birth_if_alive(os.getpid()), born)

    def test_rc003_membership_reads_host_pid_from_service_instance(self):
        class Key:
            def __init__(self, name):
                self.name = name
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                pass
        host = 42
        fake = SimpleNamespace(
            HKEY_LOCAL_MACHINE=object(),
            OpenKey=lambda _parent, name: Key(name),
            QueryInfoKey=lambda key: (1, 0, 0),
            EnumKey=lambda key, _index: (
                assets.HID_SERVICE_PREFIX + assets.RC003_HARDWARE_TOKEN
                if key.name == assets.BTHLE_ENUM_KEY else "instance"),
            QueryValueEx=lambda _key, _name: (host, 4),
        )
        with mock.patch.dict("sys.modules", winreg=fake):
            self.assertTrue(gadget._rc003_host_member(42))
            self.assertFalse(gadget._rc003_host_member(43))


class GoogleGadgetRoutingTests(unittest.TestCase):
    def test_direct_success_stays_direct_and_exact_failure_uses_fallback(self):
        class ProcessNotRespondingError(RuntimeError):
            pass

        session = mock.Mock()
        frida = SimpleNamespace(attach=mock.Mock(return_value=session),
                                ProcessNotRespondingError=ProcessNotRespondingError)
        instance = tap.HidTap("a" * 64)
        instance.address = "010203040506"
        instance.pid = 42
        with mock.patch.object(gadget, "loaded_or_pending_for_host", return_value=False), \
             mock.patch.object(gadget, "attach", return_value=(session, mock.Mock(), "127.0.0.1:41000")) as fallback:
            self.assertIs(instance._attach(frida, 42), session)
            fallback.assert_not_called()
            frida.attach.side_effect = ProcessNotRespondingError("private")
            self.assertIs(instance._attach(frida, 42), session)
            fallback.assert_called_once()
        self.assertEqual(instance._gadget_device[1], "127.0.0.1:41000")

    def test_loaded_gadget_skips_direct_agent(self):
        session = mock.Mock()
        frida = SimpleNamespace(attach=mock.Mock())
        instance = tap.HidTap("a" * 64)
        instance.address = "010203040506"
        instance.pid = 42
        with mock.patch.object(gadget, "loaded_or_pending_for_host", return_value=True), \
             mock.patch.object(gadget, "attach", return_value=(session, mock.Mock(), "127.0.0.1:41000")):
            self.assertIs(instance._attach(frida, 42), session)
        frida.attach.assert_not_called()

    def test_rc003_sees_google_gadget_as_host_conflict(self):
        from ovb_rc003 import hid_host_reload_windows as reload
        google = Path(r"C:\Program Files\RemoteMic\Google") / assets.GOOGLE_GADGET_DLL_NAME
        with mock.patch.object(reload, "loaded_gadget_paths", return_value=[google]):
            self.assertEqual(reload.ensure_reload_capable_host(42, Path(r"C:\Program Files\RemoteMic\RC003\xiaomi.dll")),
                             "restart_required")

    def test_rc003_sees_pending_google_load_even_before_module_appears(self):
        from ovb_rc003 import hid_host_reload_windows as reload
        with mock.patch.object(reload, "loaded_gadget_paths", return_value=[]), \
             mock.patch.object(gadget, "pending_injection_for_host", return_value=True):
            self.assertEqual(reload.ensure_reload_capable_host(42, Path("xiaomi.dll")),
                             "restart_required")


if __name__ == "__main__":
    unittest.main()
