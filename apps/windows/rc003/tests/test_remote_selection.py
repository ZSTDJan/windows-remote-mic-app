import asyncio
import json
from pathlib import Path
import tempfile
import subprocess
import os
import sys
import contextlib
import unittest
from types import SimpleNamespace
from unittest import mock

from ovb_rc003 import ble_transport_winrt, config, hid_identity, identity, remote_selection as selection

A = selection.container_key("11111111-1111-4111-8111-111111111111")
B = selection.container_key("22222222-2222-4222-8222-222222222222")


def saved_selection(active=A):
    return {"schema": 1, "active": active,
            "devices": [{"key": key, "profile": "xiaomi-rc003"} for key in (A, B)]}


class SelectionContractTests(unittest.TestCase):
    def test_scan_names_survive_result_transfer_without_enabling_unsupported_devices(self):
        devices = [SimpleNamespace(name=name, properties={"System.Devices.Aep.ContainerId": value})
                   for name, value in (
                       (next(iter(identity.device_profile.BLUETOOTH_NAMES)), "11111111-1111-4111-8111-111111111111"),
                       ("Google TV Remote", "22222222-2222-4222-8222-222222222222"),
                       (" \x00\n ", "33333333-3333-4333-8333-333333333333"))]
        winrt = SimpleNamespace(
            bluetooth_le_device=SimpleNamespace(get_device_selector_from_pairing_state=mock.Mock(return_value="paired")),
            device_information=SimpleNamespace(find_all_async_aqs_filter_and_additional_properties=mock.AsyncMock(return_value=devices)))
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(ble_transport_winrt, "_import_winrt", return_value=winrt):
            path = Path(directory) / "scan.json"
            self.assertEqual(selection.scan_main([str(path)]), 0)
            rows = {row["key"]: row for row in selection._read_scan_result(str(path), 0)}
            self.assertEqual(rows[A]["label"], selection.label(A))
            self.assertEqual(rows[B]["label"], "Google TV Remote（暂不支持）")
            unnamed = selection.container_key(devices[2].properties["System.Devices.Aep.ContainerId"])
            self.assertEqual(rows[unnamed]["label"], f"未命名蓝牙设备 · {unnamed[:6].upper()}（暂不支持）")
            with self.assertRaises(selection.SelectionError):
                selection.add_device(selection.empty_selection(), rows[B])
            saved = selection.add_device(selection.empty_selection(), rows[A])
            self.assertEqual(saved["devices"], [{"key": A, "profile": "xiaomi-rc003"}])

    def test_old_config_requires_explicit_choice_without_touching_mappings(self):
        self.assertEqual(selection.active_key(config.default_config()), "")
        with self.assertRaises(selection.SelectionError):
            selection.require_active(config.default_config())

    def test_normalized_digest_and_saved_record_contain_no_raw_identity(self):
        value = "11111111-1111-4111-8111-111111111111"
        self.assertEqual(A, selection.container_key("{" + value.upper() + "}"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            settings = config.default_config()
            settings[selection.KEY] = saved_selection()
            settings["gain_db"] = 6
            config.save_config(path, settings)
            self.assertNotIn(value, path.read_text(encoding="utf-8"))
            loaded = config.load_config(path)
            self.assertEqual(selection.active_key(loaded), A)
            self.assertEqual(loaded["gain_db"], 6)

    def test_corrupt_or_unregistered_choice_does_not_silently_select_another(self):
        for value in (dict(saved_selection(), active="f" * 64),
                      dict(saved_selection(), schema=True),
                      dict(saved_selection(), devices=[{"key": A, "profile": "unsupported"}])):
            with self.subTest(value=value), self.assertRaises(selection.SelectionError):
                selection.normalize(value)

    def test_add_does_not_activate_and_remove_current_clears_selection(self):
        result = selection.add_device(selection.empty_selection(), {"key": A, "profile": "xiaomi-rc003"})
        self.assertEqual(result["active"], "")
        result = selection.select_device(result, A)
        result = selection.remove_device(result, A)
        self.assertEqual(result, selection.empty_selection())

    def test_raw_input_uses_only_selected_physical_device(self):
        paths = [r"\\?\hid#vid_2717&pid_32b8#a", r"\\?\hid#vid_2717&pid_32b8#b"]
        resolve = dict(zip(paths, (A, B))).__getitem__
        self.assertEqual(selection.selected_raw_path(paths, B, resolve=resolve), paths[1])
        with self.assertRaises(hid_identity.NoDevicePathFoundError):
            selection.selected_raw_path(paths[:1], B, resolve=resolve)

    def test_unknown_raw_identity_is_never_replaced_with_another_remote(self):
        path = r"\\?\hid#vid_2717&pid_32b8#a"
        with self.assertRaises(hid_identity.NoDevicePathFoundError):
            selection.selected_raw_path([path], A, resolve=mock.Mock(side_effect=selection.SelectionError()))

    def test_ble_selects_the_chosen_remote_without_probing_others(self):
        a = identity.RC003Candidate("", True, object(), A)
        b = identity.RC003Candidate("", True, object(), B)
        probe = mock.AsyncMock()
        self.assertIs(asyncio.run(ble_transport_winrt.select_connectable_candidate(
            [a, b], selected_key=B, probe=probe)), b)
        probe.assert_not_called()
        with self.assertRaises(identity.NoCandidateFoundError):
            asyncio.run(ble_transport_winrt.select_connectable_candidate([a], selected_key=B, probe=probe))
        probe.assert_not_called()

    def test_repeated_ble_entries_do_not_allow_arbitrary_selection(self):
        a = identity.RC003Candidate("", True, object(), A)
        with self.assertRaises(identity.AmbiguousCandidateError):
            asyncio.run(ble_transport_winrt.select_connectable_candidate([a, a], selected_key=A))

    def test_scan_rejects_malformed_output(self):
        def child(_command, *, result_path, result_reader, **_kwargs):
            Path(result_path).write_text(json.dumps([{"key": A, "profile": "xiaomi-rc003", "label": "x", "address": "secret"}]))
            return result_reader(result_path, 0)
        with mock.patch("ovb_rc003.windows_diagnostics._run_ble_diagnostics_subprocess", side_effect=child):
            with self.assertRaises(selection.SelectionError):
                selection.scan_paired()

    def test_failed_scan_process_cannot_reuse_a_valid_result_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.json"
            path.write_text("[]", encoding="utf-8")
            with self.assertRaises(selection.SelectionError):
                selection._read_scan_result(str(path), 1)

    def test_helper_reads_only_selection_without_desktop_dependencies(self):
        import ast
        spec = Path(__file__).resolve().parents[1] / "build" / "RemoteMicRC003.spec"
        tree = ast.parse(spec.read_text(encoding="utf-8"))
        helper = next(node.value for node in tree.body if isinstance(node, ast.Assign)
                      and any(isinstance(target, ast.Name) and target.id == "helper_a"
                              for target in node.targets))
        excluded = ast.literal_eval(next(item.value for item in helper.keywords if item.arg == "excludes"))
        script = """
import json, sys
for name in json.loads(sys.argv[1]):
    sys.modules[name] = None
from ovb_rc003 import remote_selection
assert remote_selection.saved_active_key() == sys.argv[2]
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "RemoteMic" / "RC003" / "config.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({selection.KEY: saved_selection(), "voice_hotkey": {}}), encoding="utf-8")
            env = dict(os.environ, LOCALAPPDATA=directory)
            result = subprocess.run([sys.executable, "-c", script, json.dumps(excluded), A],
                                    env=env, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_host_resolution_selects_the_matching_container_not_first_pid(self):
        from ovb_rc003 import frida_hid_tap_runtime as runtime
        service = runtime.HID_SERVICE_PREFIX + "_" + runtime.RC003_HARDWARE_TOKEN
        prefix = runtime.BTHLE_ENUM_KEY + "\\" + service
        leaves = {
            prefix + r"\a": {"ContainerID": "11111111-1111-4111-8111-111111111111"},
            prefix + r"\b": {"ContainerID": "22222222-2222-4222-8222-222222222222"},
            prefix + "\\a\\" + runtime.WUDF_DIAGNOSTIC_SUFFIX: {"HostPid": 100},
            prefix + "\\b\\" + runtime.WUDF_DIAGNOSTIC_SUFFIX: {"HostPid": 200},
        }
        nodes = dict(leaves)
        for path in list(nodes):
            while "\\" in path:
                path = path.rsplit("\\", 1)[0]
                nodes.setdefault(path, {})
        class Registry:
            HKEY_LOCAL_MACHINE = ""
            @staticmethod
            def OpenKey(parent, name):
                path = (parent + "\\" if parent else "") + name
                if path not in nodes:
                    raise FileNotFoundError()
                return contextlib.nullcontext(path)
            @staticmethod
            def children(path):
                return sorted({key[len(path) + 1:].split("\\")[0] for key in nodes
                               if key.startswith(path + "\\")})
            @staticmethod
            def QueryInfoKey(path):
                return (len(Registry.children(path)), 0, 0)
            @staticmethod
            def EnumKey(path, index):
                children = Registry.children(path)
                if index >= len(children):
                    error = OSError()
                    error.winerror = 259
                    raise error
                return children[index]
            @staticmethod
            def QueryValueEx(path, name):
                if name not in nodes[path]:
                    raise FileNotFoundError()
                return nodes[path][name], 1
        with mock.patch.object(runtime, "winreg", Registry):
            self.assertEqual(runtime.find_rc003_hidogatt_host_pid(selected_key=B), 200)
            self.assertIsNone(runtime.find_rc003_hidogatt_host_pid(selected_key="f" * 64))
            self.assertIsNone(runtime.find_rc003_hidogatt_host_pid(selected_key=""))
            nodes[prefix + "\\b\\" + runtime.WUDF_DIAGNOSTIC_SUFFIX]["HostPid"] = 100
            evidence = {}
            self.assertFalse(runtime.rc003_hidogatt_host_is_exclusive(100, selected_key=B, diagnostic=evidence))
            self.assertEqual(evidence["reason"], "shared_host")

    def _run_shared_tap(self, messages):
        from ovb_rc003 import frida_compat, frida_hid_tap_runtime as runtime
        injector, client, server = mock.Mock(), mock.Mock(), mock.Mock()
        reports, states = [], []
        tap = frida_compat.RC003HidReportTap(
            lambda *args: reports.append(args), selected_key=A, injector=injector,
            client_pid_resolver=lambda _client: 100,
            status_handler=lambda *args: states.append(args),
        )
        client.recv.return_value = ("\n".join(json.dumps(message) for message in messages) + "\n").encode()
        server.accept.return_value = (client, ("127.0.0.1", 1))
        def shared(_pid, *, diagnostic, selected_key):
            diagnostic.update(reason="shared_host", scan_complete=True, host_members=2, rc003_members=1)
            return False
        def wait(_delay):
            tap.stop_event.set()
        with mock.patch.object(runtime, "find_rc003_hidogatt_host_pid", return_value=100), \
             mock.patch.object(runtime, "rc003_hidogatt_host_is_exclusive", side_effect=shared), \
             mock.patch.object(frida_compat.socket, "socket", return_value=server), \
             mock.patch.object(tap.stop_event, "wait", side_effect=wait), \
             mock.patch.object(tap, "_run_lease_renewal"), \
             mock.patch.object(tap, "_record_diagnostic"):
            # Deliver one batch, then stop without manufacturing a connection loss.
            def receive(_size):
                tap.stop_event.set()
                return client.recv.return_value
            client.recv.side_effect = receive
            tap._run()
        return tap, injector, client, reports, states

    @staticmethod
    def _source_messages():
        return [
            {"kind": "ready", "hook_installed": True, "protocol": 4, "source_binding_revision": 1},
            {"kind": "control_ack", "action": "enable", "accepted": True, "state": "enabled", "protocol": 4},
        ]

    def test_shared_host_accepts_selected_device_without_restart(self):
        messages = self._source_messages() + [
            {"kind": "copy_candidate", "protocol": 4, "source_binding_revision": 1,
             "source_key": A, "source_kind": "device", "handle": "0x123", "epoch": 1},
            {"kind": "gatt_read", "protocol": 4, "source_binding_revision": 1,
             "source_key": A, "intercepted": True, "raw": "010000520000000000"},
        ]
        tap, injector, client, reports, states = self._run_shared_tap(messages)
        injector.assert_called_once_with(100)
        self.assertEqual(tap.status, "ready")
        self.assertTrue(reports)
        controls = [json.loads(call.args[0]) for call in client.sendall.call_args_list]
        self.assertEqual([item["action"] for item in controls], ["enable", "bind_copy_handle"])
        self.assertEqual(controls[0]["selected_key"], A)
        self.assertIs(controls[0]["exclusive_source_verified"], False)
        self.assertNotIn("restart_required", [state for state, _detail in states])

    def test_unknown_source_does_not_reconnect_or_get_reset_by_renewal(self):
        messages = self._source_messages() + [
            {"kind": "source_status", "source_binding_revision": 1, "verified": False},
            {"kind": "control_ack", "action": "renew", "accepted": True, "state": "enabled", "protocol": 4},
        ]
        tap, injector, _client, reports, _states = self._run_shared_tap(messages)
        injector.assert_called_once_with(100)
        self.assertEqual(tap.status, "selected_device_shared_host")
        self.assertEqual(reports, [])

    def test_old_source_component_is_not_armed(self):
        messages = self._source_messages()[:1]
        messages[0].pop("source_binding_revision")
        tap, injector, client, reports, _states = self._run_shared_tap(messages)
        injector.assert_called_once_with(100)
        client.sendall.assert_not_called()
        self.assertEqual(tap.status_detail, "gadget_runtime_reload_pending")
        self.assertFalse(reports)

    def test_old_handshake_during_reload_waits_for_new_script_in_same_host(self):
        self._check_reload_race(expire=False)

    def test_reload_wait_has_deadline_and_never_reinjects_same_host(self):
        self._check_reload_race(expire=True)

    def _check_reload_race(self, *, expire):
        from ovb_rc003 import frida_compat, frida_hid_tap_runtime as runtime
        now, states, reports = [0.0], [], []
        injector, client_old, client_new, server = mock.Mock(), mock.Mock(), mock.Mock(), mock.Mock()
        def state_changed(state, detail):
            states.append((state, detail))
            if state in {"ready", "restart_required"}:
                tap.stop_event.set()
        tap = frida_compat.RC003HidReportTap(lambda *args: reports.append(args), selected_key=A,
            injector=injector, client_pid_resolver=lambda _client: 100, status_handler=state_changed)
        client_old.recv.return_value = b'{"kind":"ready","hook_installed":true,"protocol":4}\n'
        messages = self._source_messages() + [
            {"kind": "gatt_read", "protocol": 4, "source_binding_revision": 1,
             "source_key": A, "intercepted": True, "raw": "010000520000000000"},
        ]
        client_new.recv.return_value = ("\n".join(json.dumps(item) for item in messages) + "\n").encode()
        accepted = []
        def accept():
            accepted.append(True)
            if len(accepted) == 1:
                return client_old, ("127.0.0.1", 1)
            if expire:
                now[0] = 20.0
                raise frida_compat.socket.timeout()
            return client_new, ("127.0.0.1", 1)
        server.accept.side_effect = accept
        with mock.patch.object(runtime, "find_rc003_hidogatt_host_pid", return_value=100), \
             mock.patch.object(runtime, "rc003_hidogatt_host_is_exclusive", return_value=False), \
             mock.patch.object(frida_compat.socket, "socket", return_value=server), \
             mock.patch.object(frida_compat.time, "monotonic", side_effect=lambda: now[0]), \
             mock.patch.object(tap.stop_event, "wait", return_value=False), \
             mock.patch.object(tap, "_run_lease_renewal"), \
             mock.patch.object(tap, "_record_diagnostic"):
            tap._run()
        injector.assert_called_once_with(100)
        client_old.sendall.assert_not_called()
        self.assertIn((frida_compat.HidTapState.WAITING_CONNECTION.value, "gadget_runtime_reload_pending"), states)
        if expire:
            self.assertEqual(states[-1], ("restart_required", "gadget_source_binding_revision_mismatch"))
            self.assertFalse(reports)
        else:
            self.assertEqual(tap.status, "ready")
            self.assertTrue(reports)
            self.assertNotIn("restart_required", [state for state, _detail in states])

    def test_report_from_another_selected_key_never_reaches_mapping(self):
        messages = self._source_messages() + [
            {"kind": "gatt_read", "protocol": 4, "source_binding_revision": 1,
             "source_key": B, "intercepted": True, "raw": "010000520000000000"},
        ]
        tap, _injector, _client, reports, _states = self._run_shared_tap(messages)
        self.assertEqual(tap.status_detail, "gadget_report_not_intercepted")
        self.assertFalse(reports)

    def test_lost_bound_source_cancels_hold_without_restart(self):
        messages = self._source_messages() + [
            {"kind": "gatt_read", "protocol": 4, "source_binding_revision": 1,
             "source_key": A, "intercepted": True, "raw": "010000520000000000"},
            {"kind": "source_status", "source_binding_revision": 1, "verified": False},
            {"kind": "control_ack", "action": "renew", "accepted": True, "state": "enabled", "protocol": 4},
        ]
        tap, injector, _client, reports, _states = self._run_shared_tap(messages)
        injector.assert_called_once_with(100)
        self.assertEqual(tap.status, "selected_device_shared_host")
        self.assertEqual(reports, [(1, bytes.fromhex("520000000000")), (1, b"\x00" * 6)])

    def test_shared_host_rejects_other_device_proof_and_stale_exclusive_fallback(self):
        from ovb_rc003 import frida_compat, frida_hid_tap_runtime as runtime
        for key, kind, expected in ((B, "device", None), (A, "exclusive", "reject_copy_handle")):
            with self.subTest(key=key, kind=kind):
                tap = frida_compat.RC003HidReportTap(lambda *_: None, selected_key=A)
                client = mock.Mock()
                with mock.patch.object(runtime, "find_rc003_hidogatt_host_pid", return_value=100), \
                     mock.patch.object(runtime, "rc003_hidogatt_host_is_exclusive", return_value=False), \
                     mock.patch.object(tap, "_record_diagnostic"):
                    accepted = tap._bind_copy_candidate(client, {"protocol": 4, "handle": "0x123", "epoch": 1,
                        "source_binding_revision": 1, "source_key": key, "source_kind": kind}, 100)
                self.assertEqual(accepted, expected is not None)
                if expected:
                    self.assertEqual(json.loads(client.sendall.call_args.args[0])["action"], expected)
                else:
                    client.sendall.assert_not_called()


class SelectionControllerTests(unittest.TestCase):
    from tests.test_qt_settings_app import SettingsControllerTests as _Fixture
    setUp = _Fixture.setUp
    tearDown = _Fixture.tearDown
    _make_controller = _Fixture._make_controller

    def seed(self):
        settings = config.default_config()
        settings[selection.KEY] = saved_selection()
        config.save_config(config.config_path(config.config_root()), settings)
        return self._make_controller()[0]

    def test_initial_start_requests_selection_without_launching(self):
        controller, _model = self._make_controller()
        with mock.patch.object(controller, "_start_bridge_process") as start:
            controller.startBridge()
        start.assert_not_called()
        self.assertIn("选择设备", controller.errorMessage)

    def test_switch_while_stopped_saves_without_starting_service(self):
        controller = self.seed()
        with mock.patch.object(controller, "_refresh_bridge_status", return_value=False), \
             mock.patch.object(controller, "_start_bridge_process") as start:
            controller.useRemoteDevice(B)
        self.assertEqual(controller.activeRemoteKey, B)
        self.assertFalse(controller.remoteSelectionBusy)
        start.assert_not_called()
        self.assertEqual(selection.saved_active_key(), B)

    def test_failed_stop_keeps_selection_and_never_starts_another_device(self):
        controller = self.seed()
        with mock.patch.object(controller, "_refresh_bridge_status", return_value=True), \
             mock.patch("ovb_rc003.bridge_control_windows.request_bridge_exit", return_value=mock.Mock(stopped=False)):
            controller.useRemoteDevice(B)
        self.assertEqual(controller.activeRemoteKey, A)
        self.assertEqual(selection.saved_active_key(), A)
        self.assertFalse(controller.remoteSelectionBusy)

    def test_save_failure_preserves_previous_selection(self):
        controller = self.seed()
        with mock.patch.object(controller, "_refresh_bridge_status", return_value=False), \
             mock.patch.object(config, "save_config_and_load", side_effect=OSError("disk full")):
            controller.useRemoteDevice(B)
        self.assertEqual(controller.activeRemoteKey, A)
        self.assertEqual(selection.saved_active_key(), A)

    def test_removing_selected_remote_does_not_touch_bluetooth_pairing(self):
        controller = self.seed()
        with mock.patch.object(controller, "_refresh_bridge_status", return_value=False):
            controller.removeRemoteDevice(A)
        self.assertEqual(controller.activeRemoteKey, "")
        self.assertEqual([row["key"] for row in controller.registeredRemotes], [B])

    def test_running_switch_saves_only_after_old_service_has_stopped(self):
        controller = self.seed()
        jobs = []
        with mock.patch.object(controller, "_refresh_bridge_status", return_value=True), \
             mock.patch.object(controller, "_start_background_task", side_effect=lambda target, name: jobs.append(target)), \
             mock.patch("PySide6.QtCore.QTimer.singleShot") as restart:
            controller.useRemoteDevice(B)
            self.assertEqual(selection.saved_active_key(), A)
            self.assertTrue(controller.remoteSelectionBusy)
            self.assertEqual(len(jobs), 1)
            controller._on_remote_switch_ready((True, ""))
            self.assertEqual(selection.saved_active_key(), B)
            restart.assert_called_once_with(0, controller._start_bridge_process)

    def test_failed_save_rollback_does_not_restart_from_uncertain_settings(self):
        controller = self.seed()
        with mock.patch.object(controller, "_refresh_bridge_status", return_value=True), \
             mock.patch("ovb_rc003.bridge_control_windows.request_bridge_exit", return_value=mock.Mock(stopped=True)), \
             mock.patch.object(config, "save_config_and_load", side_effect=config.ConfigTransactionError("rollback failed")), \
             mock.patch("PySide6.QtCore.QTimer.singleShot") as restart:
            controller.useRemoteDevice(B)
            restart.assert_not_called()
            self.assertTrue(controller._remote_save_uncertain)
            self.assertIn("未能恢复", controller.remoteSelectionMessage)

    def test_exit_during_switch_does_not_save_or_restart_a_device(self):
        controller = self.seed()
        with mock.patch.object(controller, "_refresh_bridge_status", return_value=True), \
             mock.patch.object(controller, "_start_background_task"), \
             mock.patch("PySide6.QtCore.QTimer.singleShot") as restart:
            controller.useRemoteDevice(B)
            controller._application_exit_intent.set()
            controller._on_remote_switch_ready((True, ""))
            self.assertEqual(selection.saved_active_key(), A)
            restart.assert_not_called()

    def test_adding_device_never_saves_unsaved_mapping_drafts(self):
        controller = self.seed()
        controller._remote_devices = [{"key": "c" * 64, "profile": "xiaomi-rc003", "label": "device"}]
        before = config.key_bindings_path(config.config_root()).read_bytes() if config.key_bindings_path(config.config_root()).exists() else None
        controller.addRemoteDevice("c" * 64)
        after = config.key_bindings_path(config.config_root()).read_bytes() if config.key_bindings_path(config.config_root()).exists() else None
        self.assertEqual(before, after)
        self.assertEqual(controller.activeRemoteKey, A)


_QML_PROBE = r'''
import json, os, sys
from pathlib import Path
from unittest import mock
from ovb_rc003 import qt_settings_app as m, remote_selection as selection
from tests.test_remote_selection import A, B
from PySide6.QtCore import QObject, QMetaObject, QUrl
classes = m._load_qt_classes()
classes['QQuickStyle'].setStyle('Basic')
app = classes['QGuiApplication']([])
from PySide6.QtGui import QFontDatabase
font_path = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts' / 'msyh.ttc'
if font_path.is_file():
    QFontDatabase.addApplicationFont(str(font_path))
m.single_instance.bridge_instance_running = lambda: False
m.startup_windows.rebind_owned_frozen_startup = lambda: m.startup_windows.StartupState(False)
m.startup_windows.read_startup_state = lambda: m.startup_windows.StartupState(False)
m.hid_elevation_windows.bundled_helper_offer_id = lambda: 'test'
m.voice_hotkey_sync_windows.read_provider_hotkey = lambda provider_id, **kw: m.voice_hotkey_sync_windows.VoiceHotkeySyncResult(provider_id, False, 'local_only')
paired_rows = [{'key': key, 'profile': 'xiaomi-rc003', 'label': selection.label(key)} for key in (A, B)]
paired_rows.append({'key': 'c' * 64, 'profile': '', 'label': selection._unsupported_label('Chromecast <b>客厅</b>', 'c' * 64)})
selection.scan_paired = lambda **kw: paired_rows
model = classes['ButtonMappingModel']()
controller = classes['SettingsController'](model, background_task_runner=lambda target, name: target())
controller._refresh_bridge_status = lambda: False
diagnostics = classes['DiagnosticsController'](controller, m.config.config_root(), auto_refresh=False)
diagnostics.refreshDiagnostics = lambda: None
for name, instance in [('SettingsController', controller), ('ButtonMappingModel', model), ('DiagnosticsController', diagnostics)]:
    classes['qmlRegisterSingletonInstance'](classes[name], 'OvbRc003Settings', 1, 0, name, instance)
engine = classes['QQmlApplicationEngine']()
warnings = []
engine.warnings.connect(lambda messages: warnings.extend(str(message) for message in messages))
engine.load(QUrl.fromLocalFile(str(m._qml_directory() / 'main.qml')))
assert engine.rootObjects(), warnings
window = engine.rootObjects()[0]
window.show()
app.processEvents()
def item(name):
    result = window.findChild(QObject, name)
    assert result is not None, name
    return result
def click(name):
    button = item(name)
    assert button.property('enabled'), name
    assert QMetaObject.invokeMethod(button, 'clicked'), name
    app.processEvents()

def popup_labels(name):
    combo = item(name)
    engine.globalObject().setProperty('probeCombo', engine.newQObject(combo))
    assert not engine.evaluate('probeCombo.popup.open()').isError()
    for _ in range(12):
        app.processEvents()
        window.grabWindow()
    result = engine.evaluate("""(function() {
        const labels = [];
        for (let i = 0; i < probeCombo.count; i++) {
            const row = probeCombo.popup.contentItem.itemAtIndex(i);
            if (row && row.contentItem.textFormat !== 0) throw new Error('device label must be plain text');
            labels.push(row ? row.contentItem.text : null);
        }
        return labels;
    })()""")
    assert not result.isError(), result.toString()
    labels = result.toVariant()
    screenshot_dir = os.environ.get('REMOTE_SELECTION_SCREENSHOT_DIR')
    if screenshot_dir and name == 'availableRemoteCombo' and len(labels) == 3:
        assert window.grabWindow().save(str(Path(screenshot_dir) / 'paired-device-options.png'))
    assert not engine.evaluate('probeCombo.popup.close()').isError()
    app.processEvents()
    return labels

click('selectRemoteButton')
assert item('remoteDeviceDialog').property('visible')
assert not controller.activeRemoteKey
assert not item('registeredRemoteCombo').property('enabled')
assert '请先在下方添加设备' in item('registeredRemoteCombo').property('displayText')
assert item('availableRemoteCombo').property('displayText') == selection.label(A)
available_labels = popup_labels('availableRemoteCombo')
assert available_labels == [row['label'] for row in paired_rows], available_labels
assert available_labels[2] == 'Chromecast <b>客厅</b>（暂不支持）'
assert engine.evaluate('probeCombo.contentItem.textFormat').toInt() == 0
item('availableRemoteCombo').setProperty('currentIndex', 2)
app.processEvents()
assert not item('addRemoteButton').property('enabled')
item('availableRemoteCombo').setProperty('currentIndex', 0)
app.processEvents()
click('addRemoteButton')
assert not controller.activeRemoteKey
assert item('registeredRemoteCombo').property('enabled')
assert popup_labels('registeredRemoteCombo') == [selection.label(A)]
assert popup_labels('availableRemoteCombo') == [selection.label(B), paired_rows[2]['label']]
click('useRemoteButton')
assert controller.activeRemoteKey == A
click('addRemoteButton')
assert popup_labels('registeredRemoteCombo') == [selection.label(A), selection.label(B)]
assert popup_labels('availableRemoteCombo') == [paired_rows[2]['label']]
item('registeredRemoteCombo').setProperty('currentIndex', 1)
app.processEvents()
click('useRemoteButton')
assert controller.activeRemoteKey == B
if os.environ.get('REMOTE_SELECTION_SCREENSHOT'):
    assert window.grabWindow().save(os.environ['REMOTE_SELECTION_SCREENSHOT'])
click('removeRemoteButton')
assert not controller.activeRemoteKey
assert [row['key'] for row in controller.registeredRemotes] == [A]
click('closeRemoteDialogButton')
assert not item('remoteDeviceDialog').property('visible')
assert item('currentDeviceRow').property('height') == 42
controller.shutdownBackgroundTasks()
m._shutdown_diagnostics_workers()
assert not warnings, warnings
print(json.dumps({'journey': 'add-use-switch-remove', 'warnings': warnings}))
'''


class SelectionQmlTests(unittest.TestCase):
    def test_real_dialog_add_use_switch_remove(self):
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ, LOCALAPPDATA=directory, QT_QPA_PLATFORM="offscreen",
                       RC003_DISABLE_LIVE_INPUT="1", PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
            result = subprocess.run([sys.executable, "-c", _QML_PROBE], env=env,
                                    capture_output=True, text=True, encoding="utf-8", timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('"warnings": []', result.stdout)


if __name__ == "__main__":
    unittest.main()
