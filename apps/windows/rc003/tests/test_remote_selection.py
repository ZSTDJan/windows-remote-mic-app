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
        exclude_argument = next(item.value for item in helper.keywords if item.arg == "excludes")
        self.assertIsInstance(exclude_argument, ast.Call)
        self.assertIsInstance(exclude_argument.func, ast.Name)
        self.assertEqual(exclude_argument.func.id, "list")
        self.assertEqual(len(exclude_argument.args), 1)
        self.assertIsInstance(exclude_argument.args[0], ast.Name)
        self.assertEqual(exclude_argument.args[0].id, "HELPER_EXCLUDES")
        excluded = ast.literal_eval(next(
            node.value for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "HELPER_EXCLUDES"
                    for target in node.targets)
        ))
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

    def test_shared_host_accepts_first_mic_report_before_bind_ack(self):
        messages = self._source_messages() + [
            {"kind": "copy_candidate", "protocol": 4, "source_binding_revision": 1,
             "source_key": A, "source_kind": "device", "handle": "0x123", "epoch": 1},
            {"kind": "gatt_read", "protocol": 4, "source_binding_revision": 1,
             "source_key": A, "intercepted": True, "raw": "0100003e0000000000"},
            {"kind": "gatt_read", "protocol": 4, "source_binding_revision": 1,
             "source_key": A, "intercepted": True, "raw": "010000000000000000"},
        ]
        tap, injector, client, reports, states = self._run_shared_tap(messages)
        injector.assert_called_once_with(100)
        self.assertEqual(tap.status, "ready")
        self.assertEqual(
            reports,
            [(1, bytes.fromhex("3e0000000000")), (1, b"\x00" * 6)],
        )
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


class PairedChoiceTests(unittest.TestCase):
    def test_confirmation_registers_only_chosen_entity_and_counts_all_xiaomi(self):
        rows = saved_selection()["devices"]
        updated, legacy = selection.select_paired_device(selection.empty_selection(), A, rows)
        self.assertEqual(updated["active"], A)
        self.assertEqual(updated["devices"], [rows[0]])
        self.assertFalse(legacy)  # The second Xiaomi was never manually added.
        _, legacy = selection.select_paired_device(selection.empty_selection(), A, rows[:1])
        self.assertTrue(legacy)

    def test_rejects_missing_unsupported_and_changed_profile(self):
        for rows in ([], [{"key": A, "profile": ""}],
                     [{"key": A, "profile": selection.CHROMECAST_PROFILE}]):
            with self.subTest(rows=rows), self.assertRaises(selection.SelectionError):
                selection.select_paired_device(saved_selection(), A, rows)

    def test_old_known_xiaomi_is_not_forgotten_when_scan_only_sees_one(self):
        old = saved_selection("")
        updated, legacy = selection.select_paired_device(old, A, old["devices"][:1])
        self.assertFalse(legacy)
        self.assertEqual(len(updated["devices"]), 2)


class SelectionControllerTests(unittest.TestCase):
    from tests.test_qt_settings_app import SettingsControllerTests as _Fixture
    setUp = _Fixture.setUp
    tearDown = _Fixture.tearDown
    _make_controller = _Fixture._make_controller

    def seed(self):
        settings = config.default_config()
        settings[selection.KEY] = saved_selection()
        config.save_config(config.config_path(config.config_root()), settings)
        controller = self._make_controller()[0]
        controller._on_remote_devices_ready(([
            dict(row, label=selection.label(row["key"], row["profile"]))
            for row in settings[selection.KEY]["devices"]], ""))
        return controller

    def test_initial_start_requests_selection_without_launching(self):
        controller, _model = self._make_controller()
        with mock.patch.object(controller, "_start_bridge_process") as start:
            controller.startBridge()
        start.assert_not_called()
        self.assertIn("选择设备", controller.errorMessage)

    def test_first_confirm_adds_and_activates_without_separate_save(self):
        controller, _ = self._make_controller()
        controller._on_remote_devices_ready(([{"key": A, "profile": selection.RC003_PROFILE}], ""))
        self.assertFalse(controller.activeRemoteKey)
        self.assertFalse(controller._remote_selection()["devices"])
        with mock.patch.object(controller, "_refresh_bridge_status", return_value=False), \
             mock.patch.object(config, "switch_remote_settings", wraps=config.switch_remote_settings) as save:
            controller.useRemoteDevice(A)
        self.assertEqual(controller.activeRemoteKey, A)
        self.assertEqual(save.call_count, 1)
        self.assertTrue(save.call_args.kwargs["allow_legacy_binding"])

    def test_new_choice_registration_rolls_back_with_failed_save(self):
        controller = self.seed()
        key = "d" * 64
        controller._on_remote_devices_ready(([{"key": key, "profile": selection.CHROMECAST_PROFILE}], ""))
        before = config.config_path(config.config_root()).read_bytes()
        with mock.patch.object(controller, "_refresh_bridge_status", return_value=False), \
             mock.patch.object(config, "switch_remote_settings", side_effect=OSError("disk full")):
            controller.useRemoteDevice(key)
        self.assertEqual(controller.activeRemoteKey, A)
        self.assertNotIn(key, [r["key"] for r in controller._remote_selection()["devices"]])
        self.assertEqual(config.config_path(config.config_root()).read_bytes(), before)

    def test_failed_refresh_keeps_cached_choices_but_disallows_confirmation(self):
        controller = self.seed()
        original_keys = [r["key"] for r in controller.remoteDeviceChoices]
        controller._on_remote_devices_ready((None, "读取失败"))
        self.assertEqual([r["key"] for r in controller.remoteDeviceChoices], original_keys)
        self.assertTrue(all(not r["canUse"] for r in controller.remoteDeviceChoices))
        with mock.patch.object(controller, "_change_active_remote") as change:
            controller.useRemoteDevice(B)
            change.assert_not_called()
        self.assertEqual(controller.activeRemoteKey, A)

    def test_refresh_is_single_flight_read_only_and_does_not_lock_dismissal(self):
        controller = self.seed()
        path = config.config_path(config.config_root())
        before = path.read_bytes()
        with mock.patch.object(controller, "_start_background_task") as start:
            controller.refreshRemoteDevices()
            controller.refreshRemoteDevices()
        self.assertEqual(start.call_count, 1)
        self.assertTrue(controller.remoteDevicesRefreshing)
        self.assertFalse(controller.remoteSelectionBusy)
        self.assertEqual(path.read_bytes(), before)
        controller.useRemoteDevice(B)
        self.assertEqual(controller.activeRemoteKey, A)
        controller._on_remote_devices_ready(([], ""))
        self.assertFalse(controller.remoteDevicesRefreshing)

    def test_scan_start_failure_can_retry_without_replacing_selection(self):
        controller = self.seed()
        with mock.patch.object(controller, "_start_background_task", side_effect=RuntimeError):
            controller.refreshRemoteDevices()
        self.assertFalse(controller.remoteDevicesRefreshing)
        self.assertFalse(controller._remote_scan_valid)
        self.assertEqual(controller.activeRemoteKey, A)

    def test_choices_filter_unsupported_and_preserve_distinct_short_labels(self):
        controller = self.seed()
        key = A[:6] + ("b" if A[6] != "b" else "c") * 58
        controller._on_remote_devices_ready(([
            {"key": A, "profile": selection.RC003_PROFILE},
            {"key": key, "profile": selection.RC003_PROFILE},
            {"key": B, "profile": ""}], ""))
        rows = controller.remoteDeviceChoices
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0]["label"], rows[1]["label"])
        self.assertEqual([r["key"] for r in rows if r["isActive"]], [A])

    def test_multiple_paired_xiaomi_pass_no_legacy_binding_to_transaction(self):
        controller, _ = self._make_controller()
        controller._on_remote_devices_ready((saved_selection()["devices"], ""))
        with mock.patch.object(controller, "_refresh_bridge_status", return_value=False), \
             mock.patch.object(config, "switch_remote_settings", wraps=config.switch_remote_settings) as save:
            controller.useRemoteDevice(A)
        self.assertFalse(save.call_args.kwargs["allow_legacy_binding"])
        self.assertEqual(controller.activeRemoteKey, A)

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
             mock.patch.object(config, "switch_remote_settings", side_effect=OSError("disk full")):
            controller.useRemoteDevice(B)
        self.assertEqual(controller.activeRemoteKey, A)
        self.assertEqual(selection.saved_active_key(), A)

    def test_refresh_missing_current_preserves_selection_and_settings(self):
        controller = self.seed()
        path = config.config_path(config.config_root())
        before = path.read_bytes()
        controller._on_remote_devices_ready(([], ""))
        self.assertEqual(controller.activeRemoteKey, A)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(len(controller.remoteDeviceChoices), 1)
        self.assertIn("本次未找到", controller.remoteDeviceChoices[0]["label"])
        self.assertFalse(controller.remoteDeviceChoices[0]["canUse"])

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
             mock.patch.object(config, "switch_remote_settings", side_effect=config.ConfigTransactionError("rollback failed")), \
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

    def test_new_choice_never_registers_or_saves_unsaved_mapping_drafts(self):
        controller = self.seed()
        controller._on_remote_devices_ready(([{"key": "c" * 64, "profile": "xiaomi-rc003", "label": "device"}], ""))
        path = config.config_path(config.config_root())
        before = path.read_bytes()
        controller._set_mapping_dirty(True)
        controller.useRemoteDevice("c" * 64)
        self.assertEqual(before, path.read_bytes())
        self.assertEqual(controller.activeRemoteKey, A)
        self.assertNotIn("c" * 64, [row["key"] for row in controller._remote_selection()["devices"]])


    def seed_chromecast(self):
        settings = config.default_config()
        settings[selection.KEY] = saved_selection()
        settings[selection.KEY]["devices"][1]["profile"] = selection.CHROMECAST_PROFILE
        config.save_config(config.config_path(config.config_root()), settings)
        controller = self._make_controller()[0]
        controller._on_remote_devices_ready(([
            dict(row, label=selection.label(row["key"], row["profile"]))
            for row in settings[selection.KEY]["devices"]], ""))
        return controller

    def test_chromecast_selection_does_not_enable_etw_or_launch_setup_receiver(self):
        controller = self.seed_chromecast()
        with mock.patch.object(controller, "_refresh_bridge_status", return_value=False), \
             mock.patch("ovb_rc003.chromecast_etw_windows.sensitive_logging_enabled") as check, \
             mock.patch("ovb_rc003.chromecast_etw_windows.enable_sensitive_logging") as enable, \
             mock.patch("ovb_rc003.chromecast_client.Client") as factory, \
             mock.patch.object(controller, "_start_bridge_process") as bridge:
            controller.useRemoteDevice(B)
        check.assert_not_called()
        enable.assert_not_called()
        factory.assert_not_called()
        bridge.assert_not_called()
        self.assertFalse(controller.remoteSelectionBusy)
        self.assertFalse(controller.chromecastSetupError)
        self.assertEqual(controller.activeRemotePairingState, "paired")

    def test_repeated_choice_never_launches_setup(self):
        controller = self.seed_chromecast()
        with mock.patch.object(controller, "_refresh_bridge_status", return_value=False), \
             mock.patch("ovb_rc003.chromecast_client.Client") as factory:
            controller.useRemoteDevice(B)
            controller.useRemoteDevice(B)
        factory.assert_not_called()
        self.assertIn("已使用", controller.remoteSelectionMessage)

    def test_failed_save_does_not_enable_chromecast(self):
        controller = self.seed_chromecast()
        with mock.patch.object(controller, "_refresh_bridge_status", return_value=False), \
             mock.patch.object(config, "switch_remote_settings", side_effect=OSError), \
             mock.patch.object(controller, "_prepare_chromecast_selection") as prepare:
            controller.useRemoteDevice(B)
        prepare.assert_not_called()
        self.assertEqual(controller.activeRemoteKey, A)

    def test_cleanup_failure_retains_owner_and_blocks_switch(self):
        controller = self.seed_chromecast()
        fake = mock.Mock(reason="configured")
        fake.stop.side_effect = RuntimeError("pending")
        with mock.patch.object(controller, "_refresh_bridge_status", return_value=False):
            controller.useRemoteDevice(B)
            controller._chromecast_setup_client = fake
            self.assertIs(controller._chromecast_setup_client, fake)
            controller.useRemoteDevice(A)
            self.assertEqual(controller.activeRemoteKey, B)
            self.assertIn("尚未退出", controller.remoteSelectionMessage)
            fake.stop.side_effect = None
            controller.useRemoteDevice(B)
        self.assertIsNone(controller._chromecast_setup_client)
        self.assertFalse(controller.chromecastSetupError)

    def test_exit_during_pending_setup_does_not_launch_or_restart(self):
        controller = self.seed_chromecast()
        pending = []
        controller._background_task_runner = lambda target, name: pending.append((target, name))
        with mock.patch.object(controller, "_refresh_bridge_status", return_value=False), \
             mock.patch("ovb_rc003.chromecast_client.Client") as factory, \
             mock.patch.object(controller, "_start_bridge_process") as bridge:
            controller.useRemoteDevice(B)
            self.assertTrue(controller.remoteSelectionBusy)
            controller._application_exit_intent.set()
            next(target for target, name in pending if name == "chromecast-setup")()
        factory.assert_not_called()
        bridge.assert_not_called()
        self.assertFalse(controller.remoteSelectionBusy)

    def test_chromecast_choice_drives_model_label_and_has_separate_receiver(self):
        controller = self.seed_chromecast()
        with mock.patch.object(controller, "_refresh_bridge_status", return_value=False), \
             mock.patch.object(controller, "_start_bridge_process") as start:
            controller.useRemoteDevice(B)
            self.assertEqual(controller.activeRemoteProfile, selection.CHROMECAST_PROFILE)
            self.assertEqual(controller.currentRemoteModelName, "谷歌 Chromecast 遥控器")
            self.assertIn("Chromecast", controller.activeRemoteLabel)
            self.assertFalse(controller.isRc003Device)
            self.assertEqual(controller.selectedDeviceIndex, 1)
            controller.selectedDeviceIndex = 0
            self.assertEqual(controller.selectedDeviceIndex, 1)
            self.assertTrue(controller.activeRemoteReady)
            self.assertFalse(controller.keyDetectionActive)
            self.assertFalse(controller.claimPortableHidSetupPrompt())
            start.assert_not_called()
            controller.useRemoteDevice(A)
            self.assertTrue(controller.isRc003Device)
            self.assertEqual(controller.selectedDeviceIndex, 0)

    def test_switch_reloads_saved_shortcut_and_notifies_page(self):
        controller = self.seed_chromecast()
        path = config.config_path(config.config_root())
        config.switch_remote_settings(path, config.key_bindings_path(config.config_root()),
                                      selection.select_device(controller._remote_selection(), B))
        chrome = config.load_config(path)
        config.set_voice_hotkey_for_provider(chrome, chrome["voice_program"]["provider"], "ctrl+f9")
        config.save_config(path, chrome)
        config.switch_remote_settings(path, config.key_bindings_path(config.config_root()), controller._remote_selection())
        notified = []
        controller.holdVoiceHotkeyTextChanged.connect(lambda: notified.append(True))
        with mock.patch.object(controller, "_refresh_bridge_status", return_value=False):
            controller.useRemoteDevice(B)
        self.assertEqual(controller.holdVoiceHotkeyText, "ctrl+f9")
        self.assertTrue(notified)
        self.assertFalse(controller._has_unsaved_non_mapping_settings())

    def test_unsaved_mapping_stays_on_old_entity(self):
        controller = self.seed_chromecast()
        controller._set_mapping_dirty(True)
        controller.useRemoteDevice(B)
        self.assertEqual(controller.activeRemoteKey, A)
        self.assertIn("先保存", controller.remoteSelectionMessage)

    def test_old_endpoint_enumeration_cannot_replace_current_device_selection(self):
        controller = self.seed_chromecast()
        controller._endpoint_options_refresh_running = True
        old_snapshot = dict(controller._config)
        with mock.patch.object(controller, "_refresh_bridge_status", return_value=False):
            controller.useRemoteDevice(B)
        with mock.patch.object(controller, "_apply_endpoint_options_payload") as apply, \
             mock.patch("PySide6.QtCore.QTimer.singleShot") as refresh:
            controller._on_endpoint_options_refresh_ready((old_snapshot, {"old": True}))
        apply.assert_not_called()
        refresh.assert_called_once_with(0, controller._request_endpoint_options_refresh)


_QML_PROBE = r'''
import json, os, sys, time
from pathlib import Path
from unittest import mock
from ovb_rc003 import qt_settings_app as m, remote_selection as selection
from ovb_rc003 import chromecast_etw_windows
chromecast_etw_windows.sensitive_logging_enabled = lambda: True
if os.environ.get('TEST_ISOLATED_ROOT'):
    from ovb_rc003 import dev_session
    dev_session.isolated_root = lambda: Path(os.environ['TEST_ISOLATED_ROOT'])
    dev_session.consume_marker([dev_session.ISOLATED_FLAG])
    initial = m.config.default_config()
    initial['launch_bridge_on_app_start'] = True
    m.config.save_config(m.config.config_path(), initial)
from tests.test_remote_selection import A, B
from PySide6.QtCore import QObject, QMetaObject, QUrl, QPointF
classes = m._load_qt_classes()
classes['QQuickStyle'].setStyle(os.environ.get('REMOTE_SELECTION_STYLE', 'Basic'))
app = classes['QGuiApplication']([])
from PySide6.QtGui import QFontDatabase
font_path = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts' / 'msyh.ttc'
if font_path.is_file():
    QFontDatabase.addApplicationFont(str(font_path))
for icon_name in ('SegoeIcons.ttf', 'segmdl2.ttf'):
    icon_path = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts' / icon_name
    if icon_path.is_file():
        QFontDatabase.addApplicationFont(str(icon_path))
m.single_instance.bridge_instance_running = lambda: False
m.startup_windows.rebind_owned_frozen_startup = lambda: m.startup_windows.StartupState(False)
m.startup_windows.read_startup_state = lambda: m.startup_windows.StartupState(False)
m.hid_elevation_windows.bundled_helper_offer_id = lambda: 'test'
m.voice_hotkey_sync_windows.read_provider_hotkey = lambda provider_id, **kw: m.voice_hotkey_sync_windows.VoiceHotkeySyncResult(provider_id, False, 'local_only')
paired_rows = [{'key': key, 'profile': 'xiaomi-rc003', 'label': selection.label(key)} for key in (A, B)]
profile_b = os.environ.get('REMOTE_SELECTION_PROFILE_B', 'xiaomi-rc003')
paired_rows[1].update(profile=profile_b, label=selection.label(B, profile_b))
label_b = paired_rows[1]['label']
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
if os.environ.get('TEST_ISOLATED_ROOT'):
    assert '隔离测试' in window.title()
    assert not controller._launch_bridge_on_app_start
    controller.setLaunchBridgeOnAppStart(True)
    assert not controller._launch_bridge_on_app_start
    assert controller._config_root == Path(os.environ['TEST_ISOLATED_ROOT'])
window.setWidth(int(os.environ.get('REMOTE_SELECTION_WIDTH', '720')))
window.setHeight(int(os.environ.get('REMOTE_SELECTION_HEIGHT', '560')))
window.show()
app.processEvents()
def item(name):
    result = window.findChild(QObject, name)
    if result is None:
        pending, visited = [window], set()
        while pending:
            node = pending.pop()
            if node in visited:
                continue
            visited.add(node)
            if node.objectName() == name:
                result = node
                break
            pending.extend(node.children())
            if hasattr(node, 'childItems'):
                pending.extend(node.childItems())
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
    if screenshot_dir and name == 'remoteDeviceCombo':
        assert window.grabWindow().save(str(Path(screenshot_dir) / 'paired-device-options.png'))
    assert not engine.evaluate('probeCombo.popup.close()').isError()
    app.processEvents()
    return labels

click('selectRemoteButton')
from PySide6.QtTest import QTest
for _ in range(50):
    app.processEvents()
    if item('remoteDeviceCombo').property('count') == 2 and not controller.remoteDevicesRefreshing:
        break
    QTest.qWait(10)
assert item('remoteDeviceDialog').property('visible')
assert controller.remoteSelectionMessage == ''
assert not item('remoteSelectionMessage').property('visible')
assert item('remoteDeviceHelpNote').property('lineCount') == 1
assert item('remoteDeviceHelpNote').property('text') == '切换会结束录音，各设备设置分别保留。'
assert not controller.activeRemoteKey
assert not item('useRemoteButton').property('enabled')
assert '请选择' in item('remoteDeviceCombo').property('displayText')
available_labels = popup_labels('remoteDeviceCombo')
assert available_labels == [selection.label(A), label_b], available_labels
assert window.findChild(QObject, 'addRemoteButton') is None
assert window.findChild(QObject, 'removeRemoteButton') is None
assert engine.evaluate('probeCombo.contentItem.textFormat').toInt() == 0

def choose(key):
    combo = item('remoteDeviceCombo')
    index = next(i for i, row in enumerate(controller.remoteDeviceChoices) if row['key'] == key)
    engine.globalObject().setProperty('probeChoice', engine.newQObject(combo))
    result = engine.evaluate('probeChoice.currentIndex = %d; probeChoice.activated(%d)' % (index, index))
    assert not result.isError(), result.toString()
    app.processEvents()

choose(A)
assert not controller.activeRemoteKey and not controller._remote_selection()['devices']
click('useRemoteButton')
assert controller.activeRemoteKey == A
choose(B)
assert controller.activeRemoteKey == A
assert popup_labels('remoteDeviceCombo') == [selection.label(A), label_b]
weights = engine.evaluate('[0, 1].map(i => probeCombo.popup.contentItem.itemAtIndex(i).contentItem.font.weight)').toVariant()
assert weights[0] > weights[1], weights
assert item('remoteDeviceCombo_effectiveMark_0').property('glyph') == '\uE73E'
# Browsing B does not move A's effective (bold/check) marker. Refresh preserves
# the draft by identity even if the operating system changes enumeration order.
assert item('remoteDeviceCombo').property('effectiveIndex') == 0
controller._on_remote_devices_ready(([paired_rows[1], paired_rows[0], paired_rows[2]], ''))
app.processEvents()
assert item('remoteDeviceCombo').property('currentIndex') == 0
assert item('remoteDeviceCombo').property('effectiveIndex') == 1
assert item('remoteDeviceDialog').property('pendingRemoteKey') == B
controller._on_remote_devices_ready((None, '读取失败'))
app.processEvents()
assert controller.activeRemoteKey == A
assert item('remoteDeviceDialog').property('pendingRemoteKey') == B
assert not item('useRemoteButton').property('enabled')
controller._on_remote_devices_ready(([paired_rows[0]], ''))
app.processEvents()
assert item('remoteDeviceCombo').property('currentIndex') == -1
assert not item('useRemoteButton').property('enabled')
controller._on_remote_devices_ready((paired_rows, ''))
app.processEvents()
assert item('remoteDeviceDialog').property('pendingRemoteKey') == B
click('closeRemoteDialogButton')
assert controller.activeRemoteKey == A
click('selectRemoteButton')
for _ in range(20):
    app.processEvents()
    QTest.qWait(5)
assert item('remoteDeviceDialog').property('pendingRemoteKey') == A
choose(B)
click('useRemoteButton')
assert controller.activeRemoteKey == B
assert controller.activeRemoteProfile == profile_b
if profile_b == 'xiaomi-rc003':
    click('closeRemoteDialogButton')
    assert QMetaObject.invokeMethod(item('mappingTabButton'), 'pressed')
    for _ in range(12):
        app.processEvents()
        window.grabWindow()
    assert model.rowCount() == 13
    assert 'RC003-remote-photo.png' in controller.photoSource
    assert item('rc003DeviceButton').property('highlighted')
    assert not item('futureDeviceButton').property('highlighted')
    assert item('editMapping_menu').property('enabled')
    assert item('editMapping_tv').property('enabled')
    assert item('photoSidebar').property('width') == 114
    assert item('photoImage').property('height') == 270
    assert item('mappingLines').property('z') == 0
    if os.environ.get('REMOTE_SELECTION_SCREENSHOT_DIR'):
        assert window.grabWindow().save(str(Path(os.environ['REMOTE_SELECTION_SCREENSHOT_DIR']) / 'xiaomi-buttons.png'))
    assert QMetaObject.invokeMethod(item('deviceTabButton'), 'pressed')
    app.processEvents()
    click('selectRemoteButton')
if profile_b == 'chromecast-remote':
    assert item('currentDeviceRow').property('stateText') == '已配对'
    assert '蓝牙详细事件' not in item('remoteDeviceHelpNote').property('text')
    from ovb_rc003 import bridge_runtime_status as runtime_status
    controller._set_bridge_running(True)
    controller._set_bridge_input_states(runtime_status.BridgeRuntimeStatus(
        3, runtime_status.BridgeConnectionState.CONNECTING, os.getpid(), time.time(), raw_input_state='starting'))
    app.processEvents()
    assert item('currentDeviceRow').property('stateText') == '已配对'
    controller._set_bridge_input_states(runtime_status.BridgeRuntimeStatus(
        3, runtime_status.BridgeConnectionState.WAITING_FOR_DEVICE, os.getpid(), time.time(),
        raw_input_state='chromecast_peer_identity_failed'))
    app.processEvents()
    assert item('currentDeviceRow').property('stateText') == '已配对'
    controller._set_bridge_input_states(runtime_status.BridgeRuntimeStatus(
        3, runtime_status.BridgeConnectionState.WAITING_FOR_DEVICE, os.getpid(), time.time(),
        raw_input_state='chromecast_input_payload_unavailable'))
    app.processEvents()
    page = item('deviceScroll').parent()
    assert item('buttonReceiverRow').property('stateText') == '按键数据不完整'
    assert item('buttonReceiverRow').property('descriptionText') == 'Windows 没有提供完整按键数据；可尝试重启电脑一次，仍失败请导出日志'
    assert item('buttonReceiverRow').property('descriptionNeverElide')
    assert page.buttonReceiverStateCode() == 'input_incomplete'
    assert not page.bridgeNeedsRestartAction()
    controller._set_bridge_input_states(runtime_status.BridgeRuntimeStatus(
        3, runtime_status.BridgeConnectionState.WAITING_FOR_DEVICE, os.getpid(), time.time(),
        raw_input_state='chromecast_sensitive_logging_enable_failed'))
    app.processEvents()
    assert item('buttonReceiverRow').property('stateText') == '启用未完成'
    assert not page.bridgeNeedsRestartAction()
    assert page.bridgeStateText() == '请先完成启用'
    controller._set_bridge_running(False)
    controller._set_bridge_input_states(None)
    # The actual pages are lazy-loaded; instantiate their production loaders.
    item('buttonsPageLoader').setProperty('active', True)
    item('voicePageLoader').setProperty('active', True)
    app.processEvents()
    assert item('rc003DeviceButton').property('text') == '小米遥控器2 Pro'
    assert item('futureDeviceButton').property('text') == '谷歌 Chromecast 遥控器'
    assert not item('rc003DeviceButton').property('highlighted')
    assert item('futureDeviceButton').property('highlighted')
    assert '谷歌 Chromecast 遥控器' in item('audioPrerequisiteSectionTitle').property('text')
    assert controller.remoteRecordingModeIndex == 0
    assert controller.remoteRecordingLimitIndex == 1
    assert item('remoteRecordingModeCombo').property('count') == 2
    controller.setRemoteRecordingPreferences(1, 1)
    app.processEvents()
    assert controller.remoteRecordingModeText == '开关型'
    assert item('remoteRecordingModeCombo').property('currentIndex') == 1
    stored_voice = m.config.load_config(m.config.config_path(controller._config_root))
    assert stored_voice['remote_recording_mode'] == 'toggle'
    assert stored_voice['remote_recording_limit_seconds'] == 120
    controller.setRemoteRecordingPreferences(0, 1)
    app.processEvents()
    assert controller.activeRemoteReady
    assert model.rowCount() == 15
    assert 'Chromecast-remote-photo.png' in controller.photoSource
    click('closeRemoteDialogButton')
    assert QMetaObject.invokeMethod(item('mappingTabButton'), 'pressed')
    for _ in range(12):
        app.processEvents()
        window.grabWindow()
    if os.environ.get('REMOTE_SELECTION_SCREENSHOT_DIR'):
        assert window.grabWindow().save(str(Path(os.environ['REMOTE_SELECTION_SCREENSHOT_DIR']) / 'mapping-before-assert.png'))
    assert not item('editMapping_mic').property('enabled')
    # Ordinary connectors pass behind the photo; only the selected connector
    # overlays it. The narrower frame preserves the center-column gutter.
    photo = item('photoImage')
    sidebar = item('photoSidebar')
    assert photo.property('height') == 250
    assert sidebar.property('width') - photo.property('paintedWidth') >= 30
    assert item('mappingLines').property('z') < sidebar.property('z')
    assert item('activeMappingLine').property('z') > sidebar.property('z')
    if os.environ.get('REMOTE_SELECTION_SCREENSHOT_DIR'):
        for button in ('power', 'input_source', 'up', 'ok', 'volume_down'):
            controller.selectButton(button)
            for _ in range(12):
                app.processEvents()
                window.grabWindow()
            assert window.grabWindow().save(str(Path(os.environ['REMOTE_SELECTION_SCREENSHOT_DIR']) / ('connector-' + button + '.png')))
    board = item('mappingList')
    engine.globalObject().setProperty('probePage', engine.newQObject(item('buttonsPageLoader').property('item')))
    engine.globalObject().setProperty('probeCanvas', engine.newQObject(item('mappingLines')))
    image_left = photo.mapToItem(item('mappingLines'), QPointF(
        (photo.property('width') - photo.property('paintedWidth')) / 2, 0)).x()
    image_right = image_left + photo.property('paintedWidth')
    for button in ('up', 'left', 'down', 'back', 'home', 'youtube', 'power',
                   'right', 'ok', 'volume_up', 'volume_down', 'mic', 'volume_mute', 'netflix', 'input_source'):
        engine.globalObject().setProperty('probeCard', engine.newQObject(item('editMapping_' + button)))
        engine.globalObject().setProperty('probeHotspot', engine.newQObject(item('photoHotspot_' + button)))
        route_value = engine.evaluate('probePage.connectorRoute(probeCard, probeHotspot, probeCanvas)')
        assert not route_value.isError(), route_value.toString()
        route = route_value.toVariant()
        assert 'joinX' not in route and 'joinY' not in route, (button, route)
        assert route['control1Y'] == route['startY'] and route['control2Y'] == route['endY']
        span = abs(route['endX'] - route['startX'])
        radius = min(max(12, min(72, span * .56)), span * .48)
        assert abs(abs(route['control1X'] - route['startX']) - radius) < 1e-6, (button, route)
        assert abs(abs(route['control2X'] - route['endX']) - radius) < 1e-6, (button, route)
        assert image_left <= route['endX'] <= image_right, (button, route)
    def card_point(button):
        return item('editMapping_' + button).mapToItem(board, QPointF(0, 0))
    for left, right in [('back', 'mic'), ('home', 'volume_mute'),
                        ('youtube', 'netflix'), ('power', 'input_source')]:
        a, b = card_point(left), card_point(right)
        assert a.x() < b.x(), (left, right, a, b)
    # Each column stays compact: there is no blank row to force pair alignment.
    for column_buttons in [('up', 'left', 'down', 'back', 'home', 'youtube', 'power'),
                           ('ok', 'right', 'volume_up', 'volume_down', 'mic', 'volume_mute', 'netflix', 'input_source')]:
        for previous, following in zip(column_buttons, column_buttons[1:]):
            previous_bottom = card_point(previous).y() + item('editMapping_' + previous).property('height')
            assert abs(card_point(following).y() - previous_bottom - 2) < 1, (previous, following)
    assert card_point('up').y() < card_point('left').y() < card_point('down').y() < card_point('back').y()
    assert card_point('volume_up').x() == card_point('volume_down').x() == card_point('mic').x()
    assert card_point('ok').y() < card_point('right').y() < card_point('volume_up').y() < card_point('volume_down').y() < card_point('mic').y()
    for side in ('leftMappingCards', 'rightMappingCards'):
        column = item(side)
        top = column.mapToItem(board, QPointF(0, 0)).y()
        assert top >= -1 and top + column.property('height') <= board.property('contentHeight') + 1, (side, top, column.property('height'), board.property('contentHeight'))
    if board.property('contentHeight') > board.property('height'):
        board.setProperty('contentY', board.property('contentHeight') - board.property('height'))
        app.processEvents()
        for button in ('power', 'input_source'):
            bottom_card = item('editMapping_' + button)
            bottom = bottom_card.mapToItem(board, QPointF(0, bottom_card.property('height'))).y()
            assert bottom <= board.property('height') + 1
        board.setProperty('contentY', 0)
    assert item('detectRealKeyButton').property('enabled')
    assert window.findChild(QObject, 'editMapping_menu') is None
    assert window.findChild(QObject, 'editMapping_tv') is None
    click('editMapping_input_source')
    editor = item('actionEditorDialog')
    assert editor.property('visible')
    editor.setProperty('primaryText', 'ctrl+f9')
    editor.setProperty('primaryNote', '我的输入源操作')
    app.processEvents()
    click('actionEditorSaveButton')
    from PySide6.QtTest import QTest
    for _ in range(100):
        app.processEvents()
        if not controller.mappingDirty:
            break
        QTest.qWait(10)
    assert not controller.mappingDirty, controller.errorMessage
    stored = m.config.load_key_bindings(m.config.key_bindings_path(controller._config_root))
    assert stored['bindings']['input_source']['keys'] == ['ctrl', 'f9']
    assert stored['display_notes']['input_source']['single_click'] == '我的输入源操作'
    assert QMetaObject.invokeMethod(item('deviceTabButton'), 'pressed')
    app.processEvents()
    click('selectRemoteButton')
    if os.environ.get('REMOTE_SELECTION_SCREENSHOT_DIR'):
        click('closeRemoteDialogButton')
        for tab, filename in [('deviceTabButton', 'device'), ('mappingTabButton', 'buttons'), ('voiceTabButton', 'voice')]:
            assert QMetaObject.invokeMethod(item(tab), 'pressed')
            for _ in range(12):
                app.processEvents()
                window.grabWindow()
            assert window.grabWindow().save(str(Path(os.environ['REMOTE_SELECTION_SCREENSHOT_DIR']) / (filename + '.png')))
            if filename == 'voice':
                controller.setRemoteRecordingPreferences(1, 1)
                engine.globalObject().setProperty('probeVoiceScroll', engine.newQObject(item('voiceScroll')))
                result = engine.evaluate('probeVoiceScroll.contentItem.contentY = Math.max(0, probeVoiceScroll.contentHeight - probeVoiceScroll.height)')
                assert not result.isError(), result.toString()
                for _ in range(12):
                    app.processEvents()
                    window.grabWindow()
                assert window.grabWindow().save(str(Path(os.environ['REMOTE_SELECTION_SCREENSHOT_DIR']) / 'voice-toggle.png'))
                controller.setRemoteRecordingPreferences(0, 1)
        assert QMetaObject.invokeMethod(item('deviceTabButton'), 'pressed')
        app.processEvents()
        click('selectRemoteButton')
if os.environ.get('REMOTE_SELECTION_SCREENSHOT'):
    assert window.grabWindow().save(os.environ['REMOTE_SELECTION_SCREENSHOT'])
choose(A)
assert controller.activeRemoteKey == B
with mock.patch.object(controller, '_start_background_task'):
    click('refreshRemotesButton')
assert controller.remoteDevicesRefreshing
assert item('remoteDeviceDialog').property('dismissalEnabled')
click('closeRemoteDialogButton')
# A scan finishing after cancellation may refresh the cache, never the choice.
controller._on_remote_devices_ready((paired_rows, ''))
app.processEvents()
assert controller.activeRemoteKey == B
for _ in range(50):
    app.processEvents()
    if not item('remoteDeviceDialog').property('visible'):
        break
    QTest.qWait(10)
assert not item('remoteDeviceDialog').property('visible')
assert item('currentDeviceRow').property('height') == 42
controller.shutdownBackgroundTasks()
m._shutdown_diagnostics_workers()
assert not warnings, warnings
print(json.dumps({'journey': 'choose-confirm-refresh-switch-cancel', 'warnings': warnings}))
'''


class SelectionQmlTests(unittest.TestCase):
    def test_isolated_source_data_and_visible_title(self):
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ, QT_QPA_PLATFORM="offscreen", TEST_ISOLATED_ROOT=directory,
                       RC003_DISABLE_LIVE_INPUT="1", PYTHONUTF8="1", PYTHONIOENCODING="utf-8",
                       REMOTE_SELECTION_PROFILE_B="chromecast-remote")
            result = subprocess.run([sys.executable, "-c", _QML_PROBE], env=env,
                                    capture_output=True, text=True, encoding="utf-8", timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue((Path(directory) / 'config.json').is_file())

    def test_real_dialog_add_use_switch_remove(self):
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ, LOCALAPPDATA=directory, QT_QPA_PLATFORM="offscreen",
                       RC003_DISABLE_LIVE_INPUT="1", PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
            result = subprocess.run([sys.executable, "-c", _QML_PROBE], env=env,
                                    capture_output=True, text=True, encoding="utf-8", timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('"warnings": []', result.stdout)


    def test_chromecast_dialog_switch_updates_all_three_pages(self):
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ, LOCALAPPDATA=directory, QT_QPA_PLATFORM="offscreen",
                       RC003_DISABLE_LIVE_INPUT="1", PYTHONUTF8="1", PYTHONIOENCODING="utf-8",
                       REMOTE_SELECTION_PROFILE_B="chromecast-remote")
            result = subprocess.run([sys.executable, "-c", _QML_PROBE], env=env,
                                    capture_output=True, text=True, encoding="utf-8", timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('"warnings": []', result.stdout)


    def test_chromecast_minimum_window_and_scaled_style(self):
        for style, scale in (("Basic", "1"), ("FluentWinUI3", "1.5")):
            with self.subTest(style=style), tempfile.TemporaryDirectory() as directory:
                env = dict(os.environ, LOCALAPPDATA=directory, QT_QPA_PLATFORM="offscreen",
                           RC003_DISABLE_LIVE_INPUT="1", PYTHONUTF8="1", PYTHONIOENCODING="utf-8",
                           REMOTE_SELECTION_PROFILE_B="chromecast-remote", REMOTE_SELECTION_STYLE=style,
                           REMOTE_SELECTION_WIDTH="640", REMOTE_SELECTION_HEIGHT="480", QT_SCALE_FACTOR=scale)
                result = subprocess.run([sys.executable, "-c", _QML_PROBE], env=env,
                                        capture_output=True, text=True, encoding="utf-8", timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
