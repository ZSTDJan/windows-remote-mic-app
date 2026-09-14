import ctypes
import os
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest import mock

from ovb_rc003 import voice_playback_session_windows as subject


def state(*, muted=False, instance="session-1"):
    result = mock.MagicMock()
    result.__enter__.return_value = result
    result.endpoint_id = "device-1"
    result.instance_id = instance
    result.muted = muted
    return result


class PlaybackMuteGuardTests(unittest.TestCase):
    def test_capture_is_read_only_and_restore_uses_stable_identity(self):
        before, after = state(), state(muted=True)
        with mock.patch.object(subject, "_Session", side_effect=[before, after]) as factory:
            guard = subject.prepare_playback_mute_guard("selected endpoint")
            before.unmute.assert_not_called()
            self.assertEqual(guard.restore_if_muted(), "restored")
        self.assertEqual(factory.call_args_list, [
            mock.call(name="selected endpoint"), mock.call(endpoint_id="device-1")
        ])
        after.unmute.assert_called_once_with()

    def test_preexisting_mute_or_missing_session_is_not_overridden(self):
        for muted in (True, None):
            with self.subTest(muted=muted):
                session = state(muted=muted)
                with mock.patch.object(subject, "_Session", return_value=session):
                    self.assertIsNone(subject.prepare_playback_mute_guard("selected"))
                session.unmute.assert_not_called()

    def test_empty_endpoint_never_falls_back_to_default(self):
        with mock.patch.object(subject, "_Session") as factory:
            self.assertIsNone(subject.prepare_playback_mute_guard(""))
        factory.assert_not_called()

    def test_replaced_or_disappeared_session_is_not_changed(self):
        for session in (state(muted=True, instance="replacement"), state(muted=None)):
            with mock.patch.object(subject, "_Session", return_value=session):
                guard = subject.PlaybackMuteGuard("device-1", "session-1")
                self.assertEqual(guard.restore_if_muted(), "session_changed")
            session.unmute.assert_not_called()

    def test_no_mute_requires_no_setter(self):
        session = state()
        with mock.patch.object(subject, "_Session", return_value=session):
            guard = subject.PlaybackMuteGuard("device-1", "session-1")
            self.assertEqual(guard.restore_if_muted(), "waiting")
        session.unmute.assert_not_called()

    def test_native_failure_releases_context_and_propagates(self):
        session = state(muted=True)
        session.unmute.side_effect = OSError("disconnected")
        with mock.patch.object(subject, "_Session", return_value=session):
            with self.assertRaises(OSError):
                subject.PlaybackMuteGuard("device-1", "session-1").restore_if_muted()
        session.__exit__.assert_called_once()


class FakeCoreAudio:
    def __init__(self, devices):
        self.devices = devices
        self.objects = {}
        self.writes = []
        self.releases = []

    def output(self, argument, value):
        argument._obj.value = value

    def pointer(self, argument, value):
        identity = len(self.objects) + 1
        self.objects[identity] = value
        self.output(argument, identity)

    def create(self, *_args):
        self.pointer(_args[-1], ("enumerator", None))
        return 0

    def call(self, pointer, index, _types, *args):
        kind, data = self.objects[pointer.value]
        if kind == "enumerator" and index == 3:
            assert args[:2] == (0, 1)  # Only active render endpoints.
            self.pointer(args[-1], ("devices", self.devices))
        elif kind in ("devices", "sessions") and index == 3:
            self.output(args[-1], len(data))
        elif kind == "devices" and index == 4:
            self.pointer(args[-1], ("device", data[args[0]]))
        elif kind == "device" and index == 3:
            self.pointer(args[-1], ("manager", data))
        elif kind == "manager" and index == 5:
            self.pointer(args[-1], ("sessions", data["sessions"]))
        elif kind == "sessions" and index == 4:
            self.pointer(args[-1], ("control", data[args[0]]))
        elif kind == "control" and index == 0:
            iid = bytes(args[0]._obj)
            is_volume = iid == bytes(subject._guid("87CE5498-68D6-44E5-9215-6DA47EF883D8"))
            self.pointer(args[-1], ("volume" if is_volume else "control", data))
        elif kind == "control" and index in (3, 14):
            self.output(args[-1], data["state" if index == 3 else "pid"])
        elif kind == "volume" and index == 6:
            self.output(args[-1], int(data["muted"]))
        elif kind == "volume" and index == 5:
            assert args[0] == 0
            self.writes.append(data["instance"])
            data["muted"] = False
        else:
            raise AssertionError((kind, index))

    def string(self, pointer, index):
        kind, data = self.objects[pointer.value]
        return data["id"] if kind == "device" and index == 5 else data["instance"]


def device(identity, name, *sessions):
    return {"id": identity, "name": name, "sessions": list(sessions)}


def native_session(instance="ours", *, pid=None, active=True):
    return {"pid": os.getpid() if pid is None else pid, "state": 1 if active else 2,
            "instance": instance, "muted": True}


class NativeSessionSelectionTests(unittest.TestCase):
    def test_existing_com_apartment_is_preserved_and_owned_init_is_balanced(self):
        for result, uninitializes in ((0, 1), (1, 1), (-2147417850, 0)):
            with self.subTest(result=result):
                ole = mock.Mock()
                ole.CoInitializeEx.return_value = result
                with mock.patch.object(subject.sys, "platform", "win32"), mock.patch.object(
                    subject.ctypes, "WinDLL", return_value=ole, create=True
                ), mock.patch.object(subject._Session, "_find"):
                    with subject._Session(name="Selected"):
                        pass
                self.assertEqual(ole.CoUninitialize.call_count, uninitializes)

    def test_native_setup_failure_still_releases_owned_com_initialization(self):
        ole = mock.Mock()
        ole.CoInitializeEx.return_value = 0
        with mock.patch.object(subject.sys, "platform", "win32"), mock.patch.object(
            subject.ctypes, "WinDLL", return_value=ole, create=True
        ), mock.patch.object(subject._Session, "_find", side_effect=OSError("gone")):
            with self.assertRaises(OSError):
                with subject._Session(name="Selected"):
                    pass
        ole.CoUninitialize.assert_called_once_with()

    def find(self, devices, **selection):
        api = FakeCoreAudio(devices)
        with ExitStack() as resources:
            resources.enter_context(mock.patch.object(subject, "_call", side_effect=api.call))
            resources.enter_context(mock.patch.object(subject, "_release", side_effect=lambda p: api.releases.append(p.value)))
            resources.enter_context(mock.patch.object(subject._Session, "_string", lambda _, p, i: api.string(p, i)))
            resources.enter_context(mock.patch.object(subject._Session, "_device_name", lambda _, p: api.objects[p.value][1]["name"]))
            session = subject._Session(**selection)
            session._ole = SimpleNamespace(CoCreateInstance=api.create)
            try:
                session._find()
                if session.muted is True:
                    session.unmute()
            finally:
                session.__exit__()
        self.assertEqual(len(api.releases), len(api.objects))
        return session, api

    def test_only_selected_device_and_current_pid_can_be_unmuted(self):
        devices = [device("other", "Other", native_session("wrong-device")),
                   device("chosen", "Selected", native_session("other-process", pid=os.getpid()+1), native_session())]
        session, api = self.find(devices, name="Selected")
        self.assertEqual((session.endpoint_id, session.instance_id), ("chosen", "ours"))
        self.assertEqual(api.writes, ["ours"])

    def test_missing_ambiguous_foreign_or_expired_matches_fail_closed(self):
        cases = [[], [device("1", "Selected", native_session()), device("2", "Selected", native_session())],
                 [device("1", "Selected", native_session(), native_session("second"))],
                 [device("1", "Selected", native_session(pid=os.getpid()+1))],
                 [device("1", "Selected", native_session(active=False))]]
        for devices in cases:
            with self.subTest(devices=devices):
                session, api = self.find(devices, name="Selected")
                self.assertIsNone(session.muted)
                self.assertFalse(api.writes)

    def test_stable_endpoint_id_does_not_follow_same_name_replacement(self):
        session, api = self.find([device("replacement", "Selected", native_session())], endpoint_id="old")
        self.assertIsNone(session.muted)
        self.assertFalse(api.writes)

    def test_propvariant_layout_matches_native_pointer_width(self):
        self.assertEqual(ctypes.sizeof(subject._PropVariant), 8 + 2 * ctypes.sizeof(ctypes.c_void_p))


if __name__ == "__main__":
    unittest.main()
