"""Cross-integrity restore signaling; never starts the application or hardware."""

import ctypes
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from ctypes import wintypes
from unittest import mock

from ovb_rc003 import single_instance as si


# Lower only this disposable child process's token; never change Windows UAC
# policy or the account. All imports happen before lowering integrity.
_SIGNAL_AT_INTEGRITY = r'''
import ctypes
import sys
from ctypes import wintypes
from ovb_rc003 import single_instance as si
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
class SidAndAttributes(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]
kernel32.GetCurrentProcess.argtypes = ()
kernel32.GetCurrentProcess.restype = wintypes.HANDLE
kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
kernel32.LocalFree.restype = ctypes.c_void_p
advapi32.OpenProcessToken.argtypes = (wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE))
advapi32.OpenProcessToken.restype = wintypes.BOOL
advapi32.ConvertStringSidToSidW.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p))
advapi32.ConvertStringSidToSidW.restype = wintypes.BOOL
advapi32.GetLengthSid.argtypes = (ctypes.c_void_p,)
advapi32.GetLengthSid.restype = wintypes.DWORD
advapi32.SetTokenInformation.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
advapi32.SetTokenInformation.restype = wintypes.BOOL
token = wintypes.HANDLE()
sid = ctypes.c_void_p()
if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0080 | 0x0008, ctypes.byref(token)):
    raise ctypes.WinError(ctypes.get_last_error())
try:
    if not advapi32.ConvertStringSidToSidW("S-1-16-" + sys.argv[1], ctypes.byref(sid)):
        raise ctypes.WinError(ctypes.get_last_error())
    label = SidAndAttributes(sid, 0x20)
    if not advapi32.SetTokenInformation(token, 25, ctypes.byref(label), ctypes.sizeof(label) + advapi32.GetLengthSid(sid)):
        raise ctypes.WinError(ctypes.get_last_error())
finally:
    kernel32.LocalFree(sid)
    si._real_close_handle(token.value)
sent = si._real_signal_restore_event(int(sys.argv[2]), int(sys.argv[3]))
sys.exit(0 if sent == (sys.argv[4] == "allow") else 2)
'''


class RestoreEventTests(unittest.TestCase):
    def setUp(self):
        self.registry = mock.patch.dict(si._settings_window_restore_events, {}, clear=True)
        self.registry.start()
        self.addCleanup(self.registry.stop)

    def test_register_publishes_only_after_create_and_releases_once(self):
        calls = []
        with mock.patch.object(si, "_real_create_restore_event", return_value=99) as create, \
             mock.patch.object(si, "_real_set_window_property_value", return_value=True) as publish, \
             mock.patch.object(si, "_real_remove_window_property", side_effect=lambda *args: calls.append("remove")), \
             mock.patch.object(si, "_real_close_handle", side_effect=lambda *args: calls.append("close")):
            self.assertTrue(si.register_settings_window_restore_event(123))
            self.assertTrue(si.register_settings_window_restore_event(123))
            create.assert_called_once()
            token = create.call_args.args[1]
            self.assertGreater(token, 0)
            publish.assert_called_once_with(123, si._SETTINGS_WINDOW_RESTORE_EVENT_PROPERTY, token)
            si.release_settings_window_restore_event(123)
            si.release_settings_window_restore_event(123)
        self.assertEqual(calls, ["remove", "close"])
        self.assertEqual(si._settings_window_restore_events, {})

    def test_create_failure_publishes_no_capability(self):
        for result in (0, OSError("unavailable")):
            with self.subTest(result=result), \
                 mock.patch.object(si, "_real_create_restore_event", side_effect=result if isinstance(result, Exception) else None, return_value=0), \
                 mock.patch.object(si, "_real_set_window_property_value") as publish:
                self.assertFalse(si.register_settings_window_restore_event(123))
                publish.assert_not_called()

    def test_publication_failure_closes_unpublished_handle(self):
        with mock.patch.object(si, "_real_create_restore_event", return_value=99), \
             mock.patch.object(si, "_real_set_window_property_value", return_value=False), \
             mock.patch.object(si, "_real_close_handle") as close:
            self.assertFalse(si.register_settings_window_restore_event(123))
        close.assert_called_once_with(99)
        self.assertEqual(si._settings_window_restore_events, {})

    def test_remove_failure_still_closes_handle(self):
        si._settings_window_restore_events[123] = 99
        with mock.patch.object(si, "_real_remove_window_property", side_effect=OSError()), \
             mock.patch.object(si, "_real_close_handle") as close:
            with self.assertRaises(OSError):
                si.release_settings_window_restore_event(123)
        close.assert_called_once_with(99)
        self.assertEqual(si._settings_window_restore_events, {})

    def test_consume_drains_both_new_and_legacy_requests_once(self):
        si._settings_window_restore_events[123] = 99
        api = mock.Mock()
        api.WaitForSingleObject.side_effect = [0, 258]
        remove = mock.Mock(side_effect=[1, 0])
        with mock.patch.object(si, "_restore_event_api", return_value=api):
            self.assertTrue(si.consume_settings_window_restore_request(123, _remove_property=remove))
            self.assertFalse(si.consume_settings_window_restore_request(123, _remove_property=remove))
        self.assertEqual(remove.call_count, 2)
        api.WaitForSingleObject.assert_called_with(99, 0)

    def test_legacy_read_failure_does_not_discard_event(self):
        si._settings_window_restore_events[123] = 99
        api = mock.Mock()
        api.WaitForSingleObject.return_value = 0
        with mock.patch.object(si, "_restore_event_api", return_value=api):
            self.assertTrue(si.consume_settings_window_restore_request(
                123, _remove_property=mock.Mock(side_effect=OSError())
            ))

    def test_failed_wait_is_not_a_restore(self):
        si._settings_window_restore_events[123] = 99
        api = mock.Mock()
        api.WaitForSingleObject.return_value = 0xFFFFFFFF
        with mock.patch.object(si, "_restore_event_api", return_value=api):
            self.assertFalse(si.consume_settings_window_restore_request(123, _remove_property=lambda *args: 0))

    def test_sender_requests_only_signal_access_and_closes_handle(self):
        for result in (True, False, OSError("failed")):
            api = mock.Mock()
            api.OpenEventW.return_value = 99
            api.SetEvent.side_effect = result if isinstance(result, Exception) else None
            api.SetEvent.return_value = result
            with self.subTest(result=result), \
                 mock.patch.object(si, "_restore_event_api", return_value=api), \
                 mock.patch.object(si, "_real_close_handle") as close:
                if isinstance(result, Exception):
                    with self.assertRaises(OSError):
                        si._real_signal_restore_event(123, 456)
                else:
                    self.assertEqual(si._real_signal_restore_event(123, 456), result)
                api.OpenEventW.assert_called_once_with(2, False, si._restore_event_name(123, 456))
                close.assert_called_once_with(99)

    def test_open_failure_does_not_signal_or_claim_success(self):
        api = mock.Mock()
        api.OpenEventW.return_value = 0
        with mock.patch.object(si, "_restore_event_api", return_value=api):
            self.assertFalse(si._real_signal_restore_event(123, 456))
        api.SetEvent.assert_not_called()

    def test_real_activation_uses_event_when_setprop_is_blocked_by_uipi(self):
        user32 = mock.Mock()
        user32.EnumWindows.side_effect = lambda callback, _: callback(123, 0)
        user32.GetPropW.side_effect = lambda hwnd, name: 456 if name == si._SETTINGS_WINDOW_RESTORE_EVENT_PROPERTY else 1
        user32.SetPropW.return_value = False  # The former path cannot succeed.
        with mock.patch.object(si, "_require_windows"), \
             mock.patch.object(ctypes, "WinDLL", return_value=user32, create=True), \
             mock.patch.object(ctypes, "WINFUNCTYPE", side_effect=lambda *args: lambda fn: fn, create=True), \
             mock.patch.object(si, "_real_signal_restore_event", return_value=True) as signal:
            self.assertTrue(si.activate_current_runtime_settings_window())
            signal.assert_called_once_with(123, 456)
            signal.return_value = False
            self.assertFalse(si.activate_current_runtime_settings_window())
        user32.SetPropW.assert_not_called()
        user32.ShowWindow.assert_not_called()

    def test_older_window_keeps_qt_property_restore_protocol(self):
        user32 = mock.Mock()
        user32.EnumWindows.side_effect = lambda callback, _: callback(123, 0)
        user32.GetPropW.side_effect = lambda hwnd, name: 0 if name == si._SETTINGS_WINDOW_RESTORE_EVENT_PROPERTY else 1
        user32.SetPropW.return_value = True
        with mock.patch.object(si, "_require_windows"), \
             mock.patch.object(ctypes, "WinDLL", return_value=user32, create=True), \
             mock.patch.object(ctypes, "WINFUNCTYPE", side_effect=lambda *args: lambda fn: fn, create=True):
            self.assertTrue(si.activate_current_runtime_settings_window())
        user32.SetPropW.assert_called_once()
        user32.ShowWindow.assert_not_called()


@unittest.skipUnless(sys.platform == "win32", "requires native Windows events")
class NativeRestoreEventTests(unittest.TestCase):
    def test_real_qml_can_restore_hide_and_restore_again(self):
        # Offscreen Qt has no Win32 HWND. Only property publication is faked;
        # the event, production registration/polling, QML and cleanup are real.
        script = r'''
import sys
from PySide6.QtCore import QTimer
from ovb_rc003 import qt_settings_app as m
si = m.single_instance
si.bridge_instance_running = lambda: False
published = {}
def publish(hwnd, name, token):
    if name == si._SETTINGS_WINDOW_RESTORE_EVENT_PROPERTY:
        published[hwnd] = token
    return True
si._real_set_window_property_value = publish
original_connect = m._connect_application_exit
def connect(app, controller):
    original_connect(app, controller)
    def check(stage):
        try:
            window = next(w for w in app.topLevelWindows() if w.title() == controller.applicationPresentationLabel)
            hwnd = controller._settings_window_hwnd
            if stage in (0, 2):
                assert not window.isVisible(), "window should be hidden"
                assert si._real_signal_restore_event(hwnd, published[hwnd]), "event signal failed"
            elif stage == 1:
                assert window.isVisible(), "first restore failed"
                window.close()
            else:
                assert window.isVisible(), "second restore failed"
                controller.requestApplicationExit()
        except Exception as exc:
            print(repr(exc), file=sys.stderr)
            app.exit(2)
    for stage, delay in enumerate((50, 600, 850, 1400)):
        QTimer.singleShot(delay, lambda stage=stage: check(stage))
    QTimer.singleShot(5000, lambda: app.exit(3))
m._connect_application_exit = connect
result = m.run_settings_window(start_hidden=True)
assert not si._settings_window_restore_events, "event leaked after shutdown"
sys.exit(result)
'''
        with tempfile.TemporaryDirectory() as root:
            env = dict(os.environ, QT_QPA_PLATFORM="offscreen", LOCALAPPDATA=root, RC003_DISABLE_LIVE_INPUT="1")
            result = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_medium_token_can_signal_but_low_token_cannot(self):
        token = uuid.uuid4().int & 0x7FFFFFFF or 1
        handle = si._real_create_restore_event(123, token)
        self.assertTrue(handle)
        try:
            for integrity, expected in ((8192, "allow"), (4096, "deny")):
                with self.subTest(integrity=integrity):
                    result = subprocess.run(
                        [sys.executable, "-c", _SIGNAL_AT_INTEGRITY,
                         str(integrity), "123", str(token), expected],
                        capture_output=True, text=True, timeout=15,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(si._restore_event_api().WaitForSingleObject(handle, 0), 0 if expected == "allow" else 258)
        finally:
            si._real_close_handle(handle)

    def test_hidden_window_restored_through_real_cross_process_channel(self):
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.CreateWindowExW.argtypes = (
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p,
        )
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.DestroyWindow.argtypes = (wintypes.HWND,)
        user32.DestroyWindow.restype = wintypes.BOOL
        hwnd = user32.CreateWindowExW(0, "STATIC", "restore-test", 0, 0, 0, 1, 1, None, None, None, None)
        self.assertTrue(hwnd)
        marker = "RemoteMicRC003.TestRestore." + uuid.uuid4().hex
        try:
            self.assertTrue(si.register_settings_window_restore_event(hwnd))
            self.assertTrue(si._real_set_window_property(hwnd, marker))
            code = "from ovb_rc003 import single_instance as s; import sys; sys.exit(0 if s._real_activate_marked_window(sys.argv[1], _require_restore_request=True) else 1)"
            result = subprocess.run([sys.executable, "-c", code, marker], capture_output=True, text=True, timeout=15, env=os.environ.copy())
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(si.consume_settings_window_restore_request(hwnd))
            self.assertFalse(si.consume_settings_window_restore_request(hwnd))
            token = uuid.uuid4().int & 0x7FFFFFFF or 1
            self.assertFalse(si._real_signal_restore_event(hwnd, token))
        finally:
            si.release_settings_window_restore_event(hwnd)
            si._real_remove_window_property(hwnd, marker)
            user32.DestroyWindow(hwnd)

    def test_preexisting_event_is_rejected_and_last_close_destroys_it(self):
        token = uuid.uuid4().int & 0x7FFFFFFF or 1
        handle = si._real_create_restore_event(123, token)
        self.assertTrue(handle)
        try:
            self.assertEqual(si._real_create_restore_event(123, token), 0)
            self.assertTrue(si._real_signal_restore_event(123, token))
            self.assertEqual(si._restore_event_api().WaitForSingleObject(handle, 0), 0)
            self.assertEqual(si._restore_event_api().WaitForSingleObject(handle, 0), 258)
        finally:
            si._real_close_handle(handle)
        self.assertFalse(si._real_signal_restore_event(123, token))


if __name__ == "__main__":
    unittest.main()
