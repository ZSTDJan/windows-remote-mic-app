import asyncio
from collections import deque
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from ovb_rc003 import chromecast_client, chromecast_hid_tap_windows, chromecast_hid_worker
from ovb_rc003.chromecast_channel import Channel, ChannelError, SessionIdentity
from ovb_rc003.chromecast_runtime import ChromecastRuntime, RuntimeServices
from ovb_rc003.receiver_startup_errors import WORK_DIRECTORY_FAILED


ENTITY = "a" * 64


class WorkDirectoryContractTests(unittest.TestCase):
    def test_only_frida_directory_creation_is_classified(self):
        from frida import PermissionDeniedError

        tap = chromecast_hid_tap_windows.HidTap(ENTITY)
        errors = [
            (PermissionDeniedError(r"error creating directory C:\private\frida: Permission denied"),
             chromecast_hid_tap_windows.TapError),
            (PermissionDeniedError("access to process denied"), PermissionDeniedError),
            (RuntimeError("error creating directory unavailable"), RuntimeError),
        ]
        for native, expected in errors:
            with self.subTest(error_type=type(native).__name__), mock.patch(
                "ovb_rc003.chromecast_attach_diagnostics_windows.BoundedAttachEvidence"
            ):
                frida = SimpleNamespace(PermissionDeniedError=PermissionDeniedError,
                                        attach=mock.Mock(side_effect=native))
                with self.assertRaises(expected) as raised:
                    tap._attach(frida, 123)
                if expected is chromecast_hid_tap_windows.TapError:
                    self.assertEqual(str(raised.exception), WORK_DIRECTORY_FAILED)
                    self.assertIs(raised.exception.__cause__, native)
                else:
                    self.assertIs(raised.exception, native)

    def test_pipe_only_accepts_a_fixed_option_for_normal_service(self):
        identity = SessionIdentity.create(ENTITY)
        start = Channel(identity, commands=True).encode("start", mode="run", private_work_directory=True)
        self.assertIs(Channel(identity, commands=True).decode(start)["private_work_directory"], True)
        for mode, value in (("detect", True), ("setup", True), ("run", r"C:\untrusted"), ("run", False)):
            with self.subTest(mode=mode, value=value), self.assertRaises(ChannelError):
                Channel(identity, commands=True).encode("start", mode=mode, private_work_directory=value)

    def test_worker_changes_only_its_temporary_environment_after_preparing_fixed_directory(self):
        worker = chromecast_hid_worker
        security = worker.hid_elevation_windows
        owner = Path(r"C:\Program Files\RemoteMic\RC003\HidRuntimeUsersV1\owner")
        directory = owner / "receiver-temp"
        with (mock.patch.dict(os.environ, TEMP="previous", TMP="previous", TMPDIR="previous"),
              mock.patch.object(security, "current_user_sid", return_value="S-1-5-21-1-2-3-4"),
              mock.patch.object(security, "protected_runtime_owner_root", return_value=owner),
              mock.patch.object(security, "ensure_protected_directory", return_value=directory) as prepare):
            worker._use_private_work_directory()
            prepare.assert_called_once_with(directory, user_sid="S-1-5-21-1-2-3-4",
                                            read_execute_sids=(security.LOCAL_SERVICE_SID,))
            self.assertEqual([os.environ[name] for name in ("TEMP", "TMP", "TMPDIR")], [str(directory)] * 3)

    def test_retry_requires_authenticated_failure_and_confirmed_clean_exit(self):
        client = chromecast_client.Client(ENTITY, mode="run", on_edge=mock.Mock())
        client.reason = WORK_DIRECTORY_FAILED
        client._terminal_type = "error"
        client.finished.set()
        self.assertFalse(client.can_retry_work_directory())
        client.cleanup_confirmed = True
        self.assertFalse(client.can_retry_work_directory())
        client.exit_succeeded = True
        self.assertTrue(client.can_retry_work_directory())
        client._terminal_type = "none"
        self.assertFalse(client.can_retry_work_directory())


class WorkDirectoryServiceTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, *, failure=WORK_DIRECTORY_FAILED, confirmed=True, change_device=False,
                       replacement_fails=False, stop_during_cleanup=False):
        trace = []
        clients = []
        selected = [ENTITY]
        host_ready = asyncio.Event()
        loop = asyncio.get_running_loop()

        def create_host():
            host = mock.Mock()

            def stop():
                trace.append("host_stopped")
                if stop_during_cleanup and len(clients) == 1:
                    owner.stopping.set()

            host.stop.side_effect = stop
            return host

        services = RuntimeServices(
            selected_key=lambda: selected[0], logger=mock.Mock(), create_voice_host=create_host,
            set_input_state=mock.Mock(), publish_status=mock.Mock(), start_input=mock.Mock(),
            stop_input=mock.Mock(), disable_input=mock.Mock(), cancel_mappings=mock.Mock(),
            release_inputs=lambda: trace.append("inputs_released") or True, route_edge=mock.Mock(),
        )
        owner = ChromecastRuntime(services)
        actual_client = chromecast_client.Client

        def create_client(entity, **options):
            client = actual_client(entity, **options)
            clients.append(client)
            number = len(clients)

            def start(*, cancel_event):
                trace.append(("start", number, options.get("private_work_directory", False)))
                if (number == 1 and failure) or (number == 2 and replacement_fails):
                    client.reason = failure
                    client._terminal_type = "error"
                    client.cleanup_confirmed = client.exit_succeeded = confirmed
                    client.finished.set()
                    if change_device:
                        selected[0] = "b" * 64
                    if number == 2 or not confirmed or change_device or failure != WORK_DIRECTORY_FAILED:
                        loop.call_soon_threadsafe(host_ready.set)
                    raise RuntimeError("start failed")
                client.on_ready()
                loop.call_soon_threadsafe(host_ready.set)

            client.start = start
            client.stop = mock.Mock(side_effect=client.finished.set)
            return client

        with mock.patch.object(chromecast_client, "Client", side_effect=create_client):
            task = asyncio.create_task(owner.run())
            try:
                if not stop_during_cleanup:
                    await asyncio.wait_for(host_ready.wait(), timeout=3)
            finally:
                # The simulated UI closes while the owner still holds its receiver.
                if not stop_during_cleanup:
                    owner.stopping.set()
                await asyncio.wait_for(task, timeout=3)
        return clients, trace, services

    async def test_new_receiver_only_after_cleanup_and_only_once(self):
        clients, trace, services = await self.exercise()
        self.assertEqual(trace[:4], [("start", 1, False), "host_stopped", "inputs_released",
                                     ("start", 2, True)])
        self.assertNotEqual(clients[0].identity.generation, clients[1].identity.generation)
        self.assertTrue(clients[1].stop.called)
        self.assertIn(mock.call(raw_input_state="chromecast_ready", hid_tap_state="ready"),
                      services.set_input_state.call_args_list)
        clients, _, _ = await self.exercise(replacement_fails=True)
        self.assertEqual(len(clients), 2)

    async def test_normal_start_and_other_failures_never_switch_directory(self):
        for options in (dict(failure=None), dict(failure="hid_capture_failed"),
                        dict(confirmed=False), dict(change_device=True), dict(stop_during_cleanup=True)):
            with self.subTest(options=options):
                clients, trace, _ = await self.exercise(**options)
                self.assertEqual(len(clients), 1)
                self.assertEqual(trace[0], ("start", 1, False))


class WorkDirectoryWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_reports_specific_failure_and_starts_fresh_receiver_on_retry(self):
        worker = chromecast_hid_worker
        for use_private_directory in (False, True):
            identity = SessionIdentity.create(ENTITY)
            commands = Channel(identity, commands=True)
            events = Channel(identity, commands=False)
            extra = {"private_work_directory": True} if use_private_directory else {}
            pending = deque([commands.encode("start", mode="run", **extra)])
            received = []
            called = []
            target = SimpleNamespace(changed=threading.Event(), open=mock.AsyncMock(),
                                     close_voice=mock.AsyncMock(), close=mock.Mock())

            def prepare():
                called.append("prepare")

            def start():
                called.append("capture")
                if not use_private_directory:
                    raise chromecast_hid_tap_windows.TapError(WORK_DIRECTORY_FAILED)

            def write(raw):
                event = events.decode(raw)
                received.append(event)
                if event["type"] == "ready":
                    pending.append(commands.encode("stop"))

            tap = SimpleNamespace(start=start, source_alive=lambda: True, abort_start=mock.Mock(),
                                  poll=lambda: [("hook_ready", "", time.monotonic())],
                                  close=mock.Mock(return_value=True))
            pipe = SimpleNamespace(read=lambda: pending.popleft() if pending else None, write=write)
            with mock.patch.multiple(worker, _selected=lambda _: True, _use_private_work_directory=prepare,
                                     inspect_peer=lambda _: "parent", verify_peer=lambda *_: None,
                                     SelectedDevice=lambda _: target, HidTap=lambda _: tap):
                result = await asyncio.wait_for(worker.receive(identity, SimpleNamespace(pid=1), pipe), 3)
            self.assertEqual(result, 0)
            tap.close.assert_called_once()
            target.close.assert_called_once()
            terminal = received[-1]
            if use_private_directory:
                self.assertEqual(called, ["prepare", "capture"])
                self.assertTrue(any(event["type"] == "ready" for event in received))
                self.assertEqual((terminal["type"], terminal["reason"]), ("stopped", "stopped"))
            else:
                self.assertEqual(called, ["capture"])
                self.assertEqual((terminal["type"], terminal["reason"]), ("error", WORK_DIRECTORY_FAILED))
