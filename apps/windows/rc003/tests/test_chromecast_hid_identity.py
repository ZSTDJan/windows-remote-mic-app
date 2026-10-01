"""The HID tap accepts only the two named Chromecast PnP identities."""
from contextlib import nullcontext
from types import SimpleNamespace
import sys
import unittest
from unittest import mock

from ovb_rc003 import chromecast_hid_tap_windows as tap


ENTITY = "a" * 64
OTHER = "b" * 64
OLD = "Dev_VID&0118d1_PID&9450_REV&0110"
NEW = "Dev_VID&0218d1_PID&9450_REV&011b"


def resolve(services):
    root = r"SYSTEM\CurrentControlSet\Enum\BTHLEDevice"
    children = {root: []}
    values = {}
    for number, (hardware, address, pid, container) in enumerate(services):
        name = f"{tap._HID_SERVICE}#Dev_{number}#{hardware}_{address}"
        service = f"{root}\\{name}"
        instance = f"{service}\\instance"
        info = f"{instance}\\Device Parameters\\WUDFDiagnosticInfo"
        children[root].append(name)
        children[service] = ["instance"]
        children[instance] = []
        children[info] = []
        values[(instance, "ContainerID")] = (container, 0)
        values[(info, "HostPid")] = (pid, 0)

    def open_key(parent, name):
        path = name if parent == "HKLM" else f"{parent}\\{name}"
        if path not in children:
            raise FileNotFoundError(path)
        return nullcontext(path)

    registry = SimpleNamespace(
        HKEY_LOCAL_MACHINE="HKLM",
        OpenKey=open_key,
        QueryInfoKey=lambda key: (len(children[key]), 0, 0),
        EnumKey=lambda key, index: children[key][index],
        QueryValueEx=lambda key, name: values[(key, name)],
    )
    with mock.patch.dict(sys.modules, winreg=registry), mock.patch.object(
        tap.remote_selection, "container_key", side_effect=lambda value: value
    ):
        return tap.selected_host_and_address(ENTITY)


class SelectedHidIdentityTests(unittest.TestCase):
    def test_existing_and_b_revision_resolve_selected_host(self):
        for hardware in (OLD, NEW):
            with self.subTest(hardware=hardware):
                self.assertEqual(resolve([(hardware, "aabbccddeeff", 1234, ENTITY)]),
                                 (1234, "aabbccddeeff"))

    def test_other_revision_and_vendor_source_remain_rejected(self):
        for hardware in (
            "Dev_VID&0118d1_PID&9450_REV&011b",
            "Dev_VID&0218d1_PID&9450_REV&0110",
            "Dev_VID&0218d1_PID&9450_REV&011c",
        ):
            with self.subTest(hardware=hardware), self.assertRaisesRegex(tap.TapError, "hid_source_unconfirmed"):
                resolve([(hardware, "aabbccddeeff", 1234, ENTITY)])

    def test_other_device_cannot_supply_selected_host(self):
        with self.assertRaisesRegex(tap.TapError, "hid_source_unconfirmed"):
            resolve([(NEW, "aabbccddeeff", 1234, OTHER)])

    def test_two_matching_selected_hosts_are_rejected(self):
        with self.assertRaisesRegex(tap.TapError, "hid_source_unconfirmed"):
            resolve([(OLD, "aabbccddeeff", 1234, ENTITY),
                     (NEW, "112233445566", 5678, ENTITY)])


if __name__ == "__main__":
    unittest.main()
