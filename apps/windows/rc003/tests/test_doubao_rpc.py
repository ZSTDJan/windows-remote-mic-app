import ctypes
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from ovb_rc003 import doubao_rpc, voice_program_manager


class _FakeFunction:
    def __init__(self, result=0):
        self.result = result
        self.calls = []
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        self.calls.append(args)
        return self.result


class _FakeLibrary:
    def __init__(self, result=0):
        self.RpcPipe_KeyDown = _FakeFunction(result)
        self.RpcPipe_KeyUp = _FakeFunction(result)
        self.RpcPipe_SimpleMessageEx = _FakeFunction(result)


def _ready_script():
    script = mock.Mock()
    callbacks = {}
    script.on.side_effect = lambda name, callback: callbacks.__setitem__(name, callback)
    script.load.side_effect = lambda: callbacks["message"](
        {"type": "send", "payload": {"type": "ready"}},
        None,
    )
    return script


class DoubaoRpcTests(unittest.TestCase):
    def tearDown(self):
        doubao_rpc.clear_cached_api()

    def test_same_path_hash_reads_current_contents_without_cache_clear(self):

        with tempfile.TemporaryDirectory() as directory:
            module = Path(directory) / "ImeService.exe"
            module.write_bytes(b"first")
            first = doubao_rpc._module_sha256(str(module))

            module.write_bytes(b"other")
            refreshed = doubao_rpc._module_sha256(str(module))
            self.assertNotEqual(refreshed, first)

    def test_key_down_configures_native_abi_and_passes_endpoint(self):
        library = _FakeLibrary()
        with mock.patch.object(
            doubao_rpc.sys, "platform", "win32"
        ), mock.patch.object(
            doubao_rpc.os.path, "isfile", return_value=True
        ), mock.patch.object(
            doubao_rpc.ctypes, "WinDLL", return_value=library
        ):
            doubao_rpc.send_key_edge(0xA5, False)

        self.assertEqual(
            library.RpcPipe_KeyDown.calls,
            [(b"\\\\.\\pipe\\ObricIme\\oime-server", 0xA5, 0, None)],
        )
        self.assertEqual(
            library.RpcPipe_KeyDown.argtypes,
            (ctypes.c_char_p, ctypes.c_uint32, ctypes.c_uint64, ctypes.c_char_p),
        )

    def test_key_up_uses_reverse_edge_without_context_arguments(self):
        library = _FakeLibrary()
        with mock.patch.object(
            doubao_rpc,
            "_load_api",
            return_value=(library.RpcPipe_KeyDown, library.RpcPipe_KeyUp),
        ):
            doubao_rpc.send_key_edge(0xA5, True)

        self.assertEqual(
            library.RpcPipe_KeyUp.calls,
            [(b"\\\\.\\pipe\\ObricIme\\oime-server", 0xA5)],
        )

    def test_nonzero_status_is_a_call_error(self):
        library = _FakeLibrary(result=5)
        with mock.patch.object(
            doubao_rpc,
            "_load_api",
            return_value=(library.RpcPipe_KeyDown, library.RpcPipe_KeyUp),
        ):
            with self.assertRaises(doubao_rpc.DoubaoRpcCallError):
                doubao_rpc.send_key_edge(0xA5, False)

    def test_missing_rpc_is_distinguishable_from_call_failure(self):
        with mock.patch.object(
            doubao_rpc,
            "_load_api",
            side_effect=doubao_rpc.DoubaoRpcUnavailableError("not installed"),
        ):
            with self.assertRaises(doubao_rpc.DoubaoRpcUnavailableError):
                doubao_rpc.send_key_edge(0xA5, False)

    def test_voice_press_stop_uses_verified_builtin_message_once(self):
        library = _FakeLibrary()
        with mock.patch.object(
            doubao_rpc,
            "_load_voice_stop_api",
            return_value=library.RpcPipe_SimpleMessageEx,
        ):
            doubao_rpc.send_voice_press_stop()

        self.assertEqual(
            library.RpcPipe_SimpleMessageEx.calls,
            [
                (
                    b"\\\\.\\pipe\\ObricIme\\oime-server",
                    doubao_rpc.VOICE_PRESS_STOP_MESSAGE,
                    0,
                    0,
                    None,
                )
            ],
        )

    def test_voice_stop_loader_rejects_unverified_build_before_loading(self):
        loader = mock.Mock()
        with mock.patch.object(
            doubao_rpc.sys, "platform", "win32"
        ), mock.patch.object(
            doubao_rpc, "_resolve_rpc_dll_path", return_value=r"C:\DoubaoIME\rpc.dll"
        ), mock.patch.object(
            doubao_rpc, "_verified_voice_stop_build", return_value=False
        ), mock.patch.object(
            doubao_rpc.ctypes, "WinDLL", loader
        ):
            with self.assertRaises(doubao_rpc.DoubaoRpcUnavailableError):
                doubao_rpc._load_voice_stop_api()

        loader.assert_not_called()

    def test_voice_stop_loader_configures_the_verified_native_abi(self):
        library = _FakeLibrary()
        dll_path = r"C:\Program Files\DoubaoIME\versions\v0.9.0.0\rpc.dll"
        with mock.patch.object(
            doubao_rpc.sys, "platform", "win32"
        ), mock.patch.object(
            doubao_rpc, "_resolve_rpc_dll_path", return_value=dll_path
        ), mock.patch.object(
            doubao_rpc, "_verified_voice_stop_build", return_value=True
        ), mock.patch.object(
            doubao_rpc.ctypes, "WinDLL", return_value=library
        ) as loader:
            function = doubao_rpc._load_voice_stop_api()

        self.assertIs(function, library.RpcPipe_SimpleMessageEx)
        loader.assert_called_once_with(dll_path)
        self.assertEqual(
            function.argtypes,
            (
                ctypes.c_char_p,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.c_char_p,
            ),
        )
        self.assertIs(function.restype, ctypes.c_int32)

    def test_voice_stop_loader_reverifies_the_current_build_on_each_binding(self):
        library = _FakeLibrary()
        dll_path = r"C:\Program Files\DoubaoIME\versions\v0.9.0.0\rpc.dll"
        with mock.patch.object(
            doubao_rpc.sys, "platform", "win32"
        ), mock.patch.object(
            doubao_rpc, "_resolve_rpc_dll_path", return_value=dll_path
        ), mock.patch.object(
            doubao_rpc,
            "_verified_voice_stop_build",
            side_effect=(True, False),
        ) as verified, mock.patch.object(
            doubao_rpc.ctypes, "WinDLL", return_value=library
        ) as loader:
            doubao_rpc._load_voice_stop_api()
            with self.assertRaises(doubao_rpc.DoubaoRpcUnavailableError):
                doubao_rpc._load_voice_stop_api()

        self.assertEqual(verified.call_count, 2)
        loader.assert_called_once_with(dll_path)

    def test_voice_stop_nonzero_status_is_a_call_error(self):
        function = _FakeFunction(result=7)
        with mock.patch.object(
            doubao_rpc,
            "_load_voice_stop_api",
            return_value=function,
        ):
            with self.assertRaises(doubao_rpc.DoubaoRpcCallError):
                doubao_rpc.send_voice_press_stop()


class DoubaoPhysicalizerTests(unittest.TestCase):
    def setUp(self):
        # Never discover or attach to a real input method during unit tests.
        self.processes = self.enterContext(mock.patch.object(
            voice_program_manager, "_iter_windows_processes", return_value=[]
        ))

    def tearDown(self):
        doubao_rpc.set_diagnostic_trace(None)
        doubao_rpc.clear_cached_api()

    def test_script_only_clears_marked_configured_keys_in_doubao_callback(self):
        source = doubao_rpc._physicalizer_source(
            (0xA5, 0x41), 0x7426C0, 0x7427F1, 0x7427F7, 0x7431CD, 0x38
        )

        self.assertIn("new Set([0xA5, 0x41])", source)
        self.assertIn("const suppressVks = new Set([0x41])", source)
        self.assertIn("flags & 0x10", source)
        self.assertIn("marker.and(markerMask)", source)
        self.assertIn("flags & ~0x12", source)
        self.assertIn("event.add(16).writeU64(0)", source)
        self.assertIn("type: 'marker_processed'", source)
        self.assertIn("flags_before", source)
        self.assertIn("extra_after_zero", source)
        self.assertIn("module.base.add(0x7427F1)", source)
        self.assertIn("module.base.add(0x7427F7)", source)
        self.assertIn("module.base.add(0x7431CD)", source)
        self.assertIn("verified Doubao hook layout changed", source)
        self.assertIn("const frame = { payload: null }", source)
        self.assertLess(
            source.index("stack.push(frame)"),
            source.index("if (args[0].toInt32() < 0) return"),
        )
        self.assertIn("this.context.rax = ptr(1)", source)
        self.assertIn("this.context.pc = earlyContinue", source)
        self.assertIn("const consumed = this.context.rsp.add(consumedStackOffset)", source)
        self.assertIn("consumed.writeU8(1)", source)
        self.assertNotIn("retval.replace", source)
        self.assertIn("callback_suppressed", source)
        self.assertIn("callback_path = 'early_bypass'", source)
        self.assertIn("callback_path = 'normal_decision'", source)

    def test_single_key_shortcut_is_not_suppressed_at_either_forward_gate(self):
        source = doubao_rpc._physicalizer_source(
            (0xA5,), 0x7426C0, 0x7427F1, 0x7427F7, 0x7431CD, 0x38
        )

        self.assertIn("const suppressVks = new Set([])", source)

    def test_modifier_only_shortcuts_never_enter_forward_suppression(self):
        modifier_shortcuts = (
            (0xA3, 0xA5),
            (0xA5, 0xA3),
            (0x10, 0x11, 0x12),
            (0xA0, 0xA2, 0x5B),
            (0xA1, 0xA4, 0x5C),
        )

        for shortcut in modifier_shortcuts:
            with self.subTest(shortcut=shortcut):
                self.assertEqual(doubao_rpc._shortcut_vks_to_suppress(shortcut), ())
                source = doubao_rpc._physicalizer_source(
                    shortcut, 0x7426C0, 0x7427F1, 0x7427F7, 0x7431CD, 0x38
                )
                self.assertIn("const suppressVks = new Set([])", source)

    def test_only_final_non_modifier_remains_suppressed(self):
        self.assertEqual(
            doubao_rpc._shortcut_vks_to_suppress((0xA5, 0x20)),
            (0x20,),
        )
        self.assertEqual(
            doubao_rpc._shortcut_vks_to_suppress((0xA5, 0x49)),
            (0x49,),
        )

    def test_marker_expectation_confirms_without_blocking_the_hotkey_path(self):
        trace = mock.Mock()
        trace.current_context.return_value = {
            "gesture_id": "gesture-1",
            "attempt_id": "attempt-1",
        }
        doubao_rpc.set_diagnostic_trace(trace)
        physicalizer = doubao_rpc.DoubaoPhysicalizer()
        physicalizer._status = "active"
        physicalizer._script = object()

        token = physicalizer.expect_markers("down", 1)
        self.assertIsNotNone(token)
        physicalizer._on_marker_processed(
            {
                "marker": "123",
                "vk": 0xA5,
                "message": 0x0104,
                "key_up": False,
                "flags_before": 0x10,
                "flags_after": 0,
                "extra_before_nonzero": True,
                "extra_after_zero": True,
            }
        )

        results = [
            call.kwargs.get("result")
            for call in trace.emit.call_args_list
            if call.args[0] == "doubao_marker_expectation"
        ]
        self.assertEqual(results, ["started", "confirmed"])
        marker = next(
            call
            for call in trace.emit.call_args_list
            if call.args[0] == "doubao_marker_processed"
        )
        self.assertEqual(marker.kwargs["attempt_id"], "attempt-1")
        self.assertTrue(marker.kwargs["extra_after_zero"])

    def test_marker_expectation_records_timeout_without_raising(self):
        trace = mock.Mock()
        trace.current_context.return_value = {
            "gesture_id": "gesture-2",
            "attempt_id": "attempt-2",
        }
        doubao_rpc.set_diagnostic_trace(trace)
        physicalizer = doubao_rpc.DoubaoPhysicalizer()
        physicalizer._status = "active"
        physicalizer._script = object()

        class ImmediateTimer:
            def __init__(self, _delay, callback, args=()):
                self.callback = callback
                self.args = args
                self.daemon = False

            def start(self):
                self.callback(*self.args)

            def cancel(self):
                pass

        with mock.patch.object(doubao_rpc.threading, "Timer", ImmediateTimer):
            self.assertIsNotNone(physicalizer.expect_markers("down", 2))

        results = [
            call.kwargs.get("result")
            for call in trace.emit.call_args_list
            if call.args[0] == "doubao_marker_expectation"
        ]
        self.assertEqual(results, ["started", "timeout"])
        timeout = trace.emit.call_args_list[-1]
        self.assertEqual(timeout.kwargs["expected"], 2)
        self.assertEqual(timeout.kwargs["received"], 0)

    def test_verified_module_requires_the_installed_doubao_path_and_hash(self):
        with mock.patch.object(
            doubao_rpc.hashlib,
            "sha256",
            return_value=mock.Mock(
                hexdigest=lambda: next(
                    iter(doubao_rpc._VERIFIED_IME_SERVICE_BUILDS)
                )
            ),
        ), mock.patch.object(doubao_rpc.Path, "read_bytes", return_value=b"verified"):
            self.assertTrue(
                doubao_rpc.DoubaoPhysicalizer._verify_module(
                    r"C:\Program Files\DoubaoIME\ImeService.exe"
                )
            )
            self.assertFalse(
                doubao_rpc.DoubaoPhysicalizer._verify_module(
                    r"C:\Program Files\Other\ImeService.exe"
                )
            )

    def test_windows_discovery_finds_service_missing_from_frida_enumeration(self):
        script = _ready_script()
        session = mock.Mock()
        session.create_script.return_value = script
        process = types.SimpleNamespace(pid=46500, name="ImeService.exe")
        self.processes.return_value = [process]
        device = mock.Mock()
        device.enumerate_processes.return_value = []
        fake_frida = types.SimpleNamespace(
            get_local_device=lambda: device,
            attach=mock.Mock(return_value=session),
        )
        physicalizer = doubao_rpc.DoubaoPhysicalizer()
        with mock.patch.object(doubao_rpc.sys, "platform", "win32"), mock.patch.dict(
            sys.modules, {"frida": fake_frida}
        ), mock.patch.object(
            physicalizer,
            "_probe_module",
            return_value=r"C:\Program Files\DoubaoIME\ImeService.exe",
        ), mock.patch.object(
            physicalizer, "_verified_hook_layout",
            return_value=(0x7426C0, 0x7427F1, 0x7427F7, 0x7431CD, 0x38)
        ):
            self.assertTrue(physicalizer.start((0xA5,)))

        self.assertEqual(physicalizer.status, "active")
        device.enumerate_processes.assert_not_called()
        script.load.assert_called_once()
        fake_frida.attach.assert_called_once_with(46500)
        physicalizer.stop()
        script.unload.assert_called_once()
        session.detach.assert_called_once()

    def test_next_start_reattaches_after_the_previous_session_detaches(self):
        old_script = mock.Mock()
        old_session = mock.Mock()
        old_session.is_detached = True
        new_script = _ready_script()
        new_session = mock.Mock()
        new_session.is_detached = False
        new_session.create_script.return_value = new_script
        process = types.SimpleNamespace(pid=46501, name="ImeService.exe")
        self.processes.return_value = [process]
        device = mock.Mock()
        device.enumerate_processes.return_value = [process]
        fake_frida = types.SimpleNamespace(
            get_local_device=lambda: device,
            attach=mock.Mock(return_value=new_session),
        )
        physicalizer = doubao_rpc.DoubaoPhysicalizer()
        physicalizer._session = old_session
        physicalizer._script = old_script
        physicalizer._frida = object()
        physicalizer._vk_codes = (0xA5,)
        physicalizer._status = "active"

        with mock.patch.object(doubao_rpc.sys, "platform", "win32"), mock.patch.dict(
            sys.modules, {"frida": fake_frida}
        ), mock.patch.object(
            physicalizer,
            "_probe_module",
            return_value=r"C:\Program Files\DoubaoIME\ImeService.exe",
        ), mock.patch.object(
            physicalizer, "_verified_hook_layout",
            return_value=(0x7426C0, 0x7427F1, 0x7427F7, 0x7431CD, 0x38)
        ):
            self.assertTrue(physicalizer.start((0xA5,)))

        fake_frida.attach.assert_called_once_with(46501)
        self.assertIs(physicalizer._session, new_session)
        self.assertIs(physicalizer._script, new_script)
        self.assertEqual(physicalizer.status, "active")
        old_script.unload.assert_not_called()
        old_session.detach.assert_not_called()
        physicalizer.stop()

    def test_stale_global_frida_manager_uses_one_owned_recovery_manager(self):
        class TransportError(Exception):
            pass

        script = _ready_script()
        session = mock.Mock()
        session.is_detached = False
        session.create_script.return_value = script
        manager = mock.Mock()
        manager.get_local_device.return_value.attach.return_value = session
        process = types.SimpleNamespace(pid=46500, name="ImeService.exe")
        self.processes.return_value = [process]
        fake_frida = types.SimpleNamespace(
            attach=mock.Mock(side_effect=TransportError("closed")),
            core=types.SimpleNamespace(DeviceManager=mock.Mock(return_value=manager)),
            _frida=types.SimpleNamespace(DeviceManager=mock.Mock()),
        )
        physicalizer = doubao_rpc.DoubaoPhysicalizer()

        with mock.patch.object(doubao_rpc.sys, "platform", "win32"), mock.patch.dict(
            sys.modules, {"frida": fake_frida}
        ), mock.patch.object(
            physicalizer,
            "_probe_module",
            return_value=r"C:\Program Files\DoubaoIME\ImeService.exe",
        ), mock.patch.object(
            physicalizer, "_verified_hook_layout",
            return_value=(0x7426C0, 0x7427F1, 0x7427F7, 0x7431CD, 0x38),
        ):
            self.assertTrue(physicalizer.start((0xA5,)))

        fake_frida.attach.assert_called_once_with(46500)
        manager.get_local_device.return_value.attach.assert_called_once_with(46500)
        self.assertIs(physicalizer._frida_manager, manager)
        physicalizer.stop()
        session.detach.assert_called_once()
        manager._impl.close.assert_called_once()
        self.assertIsNone(physicalizer._frida_manager)

    def test_recovery_attach_failure_closes_manager_and_stays_unavailable(self):
        class TransportError(Exception):
            pass

        class InvalidArgumentError(Exception):
            pass

        manager = mock.Mock()
        manager.get_local_device.return_value.attach.side_effect = InvalidArgumentError()
        self.processes.return_value = [
            types.SimpleNamespace(pid=46500, name="ImeService.exe")
        ]
        fake_frida = types.SimpleNamespace(
            attach=mock.Mock(side_effect=TransportError()),
            core=types.SimpleNamespace(DeviceManager=mock.Mock(return_value=manager)),
            _frida=types.SimpleNamespace(DeviceManager=mock.Mock()),
        )
        physicalizer = doubao_rpc.DoubaoPhysicalizer()

        with mock.patch.object(doubao_rpc.sys, "platform", "win32"), mock.patch.dict(
            sys.modules, {"frida": fake_frida}
        ):
            self.assertFalse(physicalizer.start((0xA5,)))

        self.assertEqual(physicalizer.status, "unavailable")
        self.assertEqual(physicalizer.error, "InvalidArgumentError")
        self.assertIsNone(physicalizer._session)
        self.assertIsNone(physicalizer._frida_manager)
        manager._impl.close.assert_called_once()

    def test_missing_ime_process_is_a_clean_optional_failure(self):
        device = mock.Mock()
        device.enumerate_processes.return_value = []
        fake_frida = types.SimpleNamespace(get_local_device=lambda: device)
        physicalizer = doubao_rpc.DoubaoPhysicalizer()
        with mock.patch.object(doubao_rpc.sys, "platform", "win32"), mock.patch.dict(
            sys.modules, {"frida": fake_frida}
        ):
            self.assertFalse(physicalizer.start((0xA5,)))

        self.assertEqual(physicalizer.status, "unavailable")
        self.assertIn("not running", physicalizer.error or "")

    def test_watchdog_and_similarly_named_processes_are_not_voice_targets(self):
        self.processes.return_value = [
            types.SimpleNamespace(pid=46501, name="ImeWatchdog.exe"),
            types.SimpleNamespace(pid=46502, name="OtherImeService.exe"),
        ]
        fake_frida = types.SimpleNamespace(attach=mock.Mock())
        with mock.patch.object(doubao_rpc.sys, "platform", "win32"), mock.patch.dict(
            sys.modules, {"frida": fake_frida}
        ):
            physicalizer = doubao_rpc.DoubaoPhysicalizer()
            self.assertFalse(physicalizer.start((0xA5,)))

        self.assertEqual(physicalizer.error, "ImeService.exe is not running")
        fake_frida.attach.assert_not_called()

    def test_windows_discovery_failure_does_not_attach(self):
        self.processes.side_effect = OSError("snapshot unavailable")
        fake_frida = types.SimpleNamespace(attach=mock.Mock())
        with mock.patch.object(doubao_rpc.sys, "platform", "win32"), mock.patch.dict(
            sys.modules, {"frida": fake_frida}
        ):
            physicalizer = doubao_rpc.DoubaoPhysicalizer()
            self.assertFalse(physicalizer.start((0xA5,)))

        self.assertEqual(physicalizer.status, "unavailable")
        self.assertEqual(physicalizer.error, "OSError")
        fake_frida.attach.assert_not_called()

    def test_discovered_process_exit_or_access_denied_is_a_clean_failure(self):
        self.processes.return_value = [
            types.SimpleNamespace(pid=46500, name="IMESERVICE.EXE")
        ]
        for error in (ProcessLookupError("exited"), PermissionError("denied")):
            fake_frida = types.SimpleNamespace(attach=mock.Mock(side_effect=error))
            with self.subTest(error=type(error).__name__), mock.patch.object(
                doubao_rpc.sys, "platform", "win32"
            ), mock.patch.dict(sys.modules, {"frida": fake_frida}):
                physicalizer = doubao_rpc.DoubaoPhysicalizer()
                self.assertFalse(physicalizer.start((0xA5,)))
                self.assertEqual(physicalizer.status, "unavailable")
                self.assertEqual(physicalizer.error, type(error).__name__)
                self.assertIsNone(physicalizer._session)
                self.assertIsNone(physicalizer._script)
                fake_frida.attach.assert_called_once_with(46500)

    def test_each_windows_candidate_must_pass_existing_module_verification(self):
        self.processes.return_value = [
            types.SimpleNamespace(pid=46500, name="ImeService.exe"),
            types.SimpleNamespace(pid=46501, name="ImeService.exe"),
        ]
        rejected, accepted = mock.Mock(), mock.Mock()
        script = _ready_script()
        accepted.create_script.return_value = script
        fake_frida = types.SimpleNamespace(
            attach=mock.Mock(side_effect=[rejected, accepted])
        )
        physicalizer = doubao_rpc.DoubaoPhysicalizer()
        with mock.patch.object(doubao_rpc.sys, "platform", "win32"), mock.patch.dict(
            sys.modules, {"frida": fake_frida}
        ), mock.patch.object(physicalizer, "_probe_module", side_effect=[
            r"C:\Other\ImeService.exe", r"C:\Program Files\DoubaoIME\ImeService.exe"
        ]), mock.patch.object(
            physicalizer, "_verified_hook_layout",
            side_effect=[None, (0x7426C0, 0x7427F1, 0x7427F7, 0x7431CD, 0x38)]
        ) as verify:
            self.assertTrue(physicalizer.start((0xA5,)))

        self.assertEqual(verify.call_count, 2)
        rejected.create_script.assert_not_called()
        rejected.detach.assert_called_once()
        self.assertEqual(fake_frida.attach.call_args_list,
                         [mock.call(46500), mock.call(46501)])
        physicalizer.stop()
        script.unload.assert_called_once()
        accepted.detach.assert_called_once()

    def test_unknown_ime_build_is_rejected_without_guessing_an_address(self):
        session = mock.Mock()
        session.create_script.side_effect = AssertionError(
            "unknown builds must not load a callback hook"
        )
        process = types.SimpleNamespace(pid=46500, name="ImeService.exe")
        self.processes.return_value = [process]
        device = mock.Mock()
        device.enumerate_processes.return_value = [process]
        fake_frida = types.SimpleNamespace(
            get_local_device=lambda: device,
            attach=mock.Mock(return_value=session),
        )
        physicalizer = doubao_rpc.DoubaoPhysicalizer()
        with mock.patch.object(doubao_rpc.sys, "platform", "win32"), mock.patch.dict(
            sys.modules, {"frida": fake_frida}
        ), mock.patch.object(
            physicalizer,
            "_probe_module",
            return_value=r"C:\Program Files\DoubaoIME\ImeService.exe",
        ), mock.patch.object(
            physicalizer, "_verified_hook_layout", return_value=None
        ):
            self.assertFalse(physicalizer.start((0xA5,)))

        self.assertEqual(physicalizer.status, "unsupported_version")
        session.create_script.assert_not_called()
        session.detach.assert_called_once()

    def test_native_failure_detail_is_reduced_to_exception_type(self):
        physicalizer = doubao_rpc.DoubaoPhysicalizer()

        physicalizer._set_failure(
            "unavailable",
            RuntimeError("sensitive native device detail"),
        )

        self.assertEqual(physicalizer.error, "RuntimeError")

    def test_process_level_interrupt_is_cleaned_up_and_propagated(self):
        script = mock.Mock()
        script.load.side_effect = KeyboardInterrupt()
        session = mock.Mock()
        session.create_script.return_value = script
        process = types.SimpleNamespace(pid=46500, name="ImeService.exe")
        self.processes.return_value = [process]
        device = mock.Mock()
        device.enumerate_processes.return_value = [process]
        fake_frida = types.SimpleNamespace(
            get_local_device=lambda: device,
            attach=mock.Mock(return_value=session),
        )
        physicalizer = doubao_rpc.DoubaoPhysicalizer()

        with mock.patch.object(doubao_rpc.sys, "platform", "win32"), mock.patch.dict(
            sys.modules, {"frida": fake_frida}
        ), mock.patch.object(
            physicalizer,
            "_probe_module",
            return_value=r"C:\Program Files\DoubaoIME\ImeService.exe",
        ), mock.patch.object(
            physicalizer, "_verified_hook_layout",
            return_value=(0x7426C0, 0x7427F1, 0x7427F7, 0x7431CD, 0x38)
        ), self.assertRaises(KeyboardInterrupt):
            physicalizer.start((0xA5,))

        script.unload.assert_called_once()
        session.detach.assert_called_once()
        self.assertIsNone(physicalizer._script)
        self.assertIsNone(physicalizer._session)

    def test_stop_failure_retains_resources_for_a_later_retry(self):
        script = mock.Mock()
        session = mock.Mock()
        script.unload.side_effect = RuntimeError("unload failed")
        session.detach.side_effect = RuntimeError("detach failed")
        physicalizer = doubao_rpc.DoubaoPhysicalizer()
        physicalizer._script = script
        physicalizer._session = session
        physicalizer._status = "active"

        with self.assertRaises(RuntimeError):
            physicalizer.stop()

        self.assertIs(physicalizer._script, script)
        self.assertIs(physicalizer._session, session)
        self.assertEqual(physicalizer.status, "cleanup_required")

        script.unload.side_effect = None
        session.detach.side_effect = None
        physicalizer.stop()
        self.assertIsNone(physicalizer._script)
        self.assertIsNone(physicalizer._session)

    def test_successful_session_detach_clears_a_script_that_failed_to_unload(self):
        script = mock.Mock()
        session = mock.Mock()
        script.unload.side_effect = RuntimeError("unload failed")
        physicalizer = doubao_rpc.DoubaoPhysicalizer()
        physicalizer._script = script
        physicalizer._session = session
        physicalizer._status = "active"

        physicalizer.stop()

        session.detach.assert_called_once()
        self.assertIsNone(physicalizer._script)
        self.assertIsNone(physicalizer._session)
        self.assertEqual(physicalizer.status, "stopped")

    def test_start_cleanup_accepts_detach_after_script_unload_failure(self):
        script = mock.Mock()
        script.load.side_effect = RuntimeError("load failed")
        script.unload.side_effect = RuntimeError("unload failed")
        session = mock.Mock()
        session.create_script.return_value = script
        process = types.SimpleNamespace(pid=46500, name="ImeService.exe")
        self.processes.return_value = [process]
        device = mock.Mock()
        device.enumerate_processes.return_value = [process]
        fake_frida = types.SimpleNamespace(
            get_local_device=lambda: device,
            attach=mock.Mock(return_value=session),
        )
        physicalizer = doubao_rpc.DoubaoPhysicalizer()

        with mock.patch.object(doubao_rpc.sys, "platform", "win32"), mock.patch.dict(
            sys.modules, {"frida": fake_frida}
        ), mock.patch.object(
            physicalizer,
            "_probe_module",
            return_value=r"C:\Program Files\DoubaoIME\ImeService.exe",
        ), mock.patch.object(
            physicalizer, "_verified_hook_layout",
            return_value=(0x7426C0, 0x7427F1, 0x7427F7, 0x7431CD, 0x38)
        ):
            self.assertFalse(physicalizer.start((0xA5,)))

        session.detach.assert_called_once()
        self.assertIsNone(physicalizer._script)
        self.assertIsNone(physicalizer._session)
        self.assertEqual(physicalizer.status, "unavailable")


if __name__ == "__main__":
    unittest.main()
