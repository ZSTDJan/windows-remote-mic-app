"""Private driver layout must be proven before selecting a report source."""
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

from ovb_rc003 import chromecast_hid_tap_windows as tap


# Constructor starts from Microsoft's matching x64 DLL/PDBs. Only the vtable
# displacement differs between these old-layout samples (26100.1150..8521).
OLD_PROLOGUE = bytes.fromhex(
    "4c894c2420 48894c2408 53 55 56 57 4154 4156 4157 4883ec50 "
    "4d8bf1 410fb7e8 488bf9 4533e4 4c896108 4c896110 "
    "488d05 289d0100 488901 48895118"
)
NEW_PROLOGUE = bytes.fromhex(
    "4c894c2420 48894c2408 53 55 56 57 4154 4156 4157 4883ec50 "
    "4d8bf1 410fb7e8 488bf9 488d05 c38f0100 488901 48895108"
)


class DriverLayoutTests(unittest.TestCase):
    def test_verified_constructor_layouts(self):
        self.assertEqual(tap._constructor_address_offset(OLD_PROLOGUE), 24)
        self.assertEqual(tap._constructor_address_offset(NEW_PROLOGUE), 8)

    def test_unrecognized_truncated_or_shifted_instructions_fail_closed(self):
        for code in (b"", NEW_PROLOGUE[:-1], b"\x90" + OLD_PROLOGUE,
                     NEW_PROLOGUE.replace(bytes.fromhex("48895108"), bytes.fromhex("48895110")),
                     NEW_PROLOGUE.replace(bytes.fromhex("488bf9"), bytes.fromhex("488bd1"))):
            with self.subTest(code=code.hex()), self.assertRaisesRegex(tap.TapError, "hid_symbol_unavailable"):
                tap._constructor_address_offset(code)

    def image(self, code=NEW_PROLOGUE):
        return SimpleNamespace(
            FILE_HEADER=SimpleNamespace(Machine=0x8664),
            sections=[SimpleNamespace(Characteristics=0x20000000, VirtualAddress=0x1000,
                                      Misc_VirtualSize=0x20000, SizeOfRawData=0x20000)],
            get_data=mock.Mock(return_value=code), close=mock.Mock(),
        )

    def resolve(self, image, digest="unlisted", symbols=(0x1800, 0x1100)):
        with tempfile.TemporaryDirectory() as directory:
            module = Path(directory) / "driver.dll"
            module.write_bytes(b"synthetic module")
            with mock.patch.object(tap.hashlib, "sha256") as sha, \
                    mock.patch.object(tap, "_resolve_pdb_symbols", return_value=symbols) as resolver, \
                    mock.patch("pefile.PE", return_value=image):
                sha.return_value.hexdigest.return_value = digest
                result = tap._driver_layout(module)
                return result, resolver.call_count

    def test_exact_hash_fast_paths_do_not_need_pdb_or_guess_by_version(self):
        expected = {
            "8a18571ddfa447bd590354ccdf7800b815a098806359ab195d395158f9d07cc5": (0x12FCC, 24),
            "de2b7ff2a61d50473cfe1e00ec012e99bda9361d563249ef93dce59f576e9936": (0x1580C, 8),
            "372c3628e3366c18152199eb8fb554d27bb4f02d1356b1f514f711328df4a8d6": (0x157FC, 8),
        }
        for digest, layout in expected.items():
            with self.subTest(digest=digest):
                self.assertEqual(self.resolve(self.image(), digest), (layout, 0))

    def test_unlisted_driver_requires_matching_symbols_and_constructor(self):
        for code, offset in ((OLD_PROLOGUE, 24), (NEW_PROLOGUE, 8)):
            image = self.image(code)
            self.assertEqual(self.resolve(image), ((0x1800, offset), 1))
            image.get_data.assert_called_once_with(0x1100, 64)
            image.close.assert_called_once()

    def test_wrong_architecture_nonexecutable_or_out_of_bounds_rvas_fail(self):
        images = [self.image() for _ in range(4)]
        images[0].FILE_HEADER.Machine = 0xAA64
        images[1].sections[0].Characteristics = 0
        images[2].sections[0].SizeOfRawData = 0x120  # constructor crosses backed bytes
        images[3].sections.append(images[3].sections[0])  # ambiguous overlap
        for image in images:
            with self.subTest(image=image), self.assertRaises(tap.TapError):
                self.resolve(image)
            image.close.assert_called_once()
        with self.assertRaises(tap.TapError):
            self.resolve(self.image(), symbols=(0x800, 0x1100))
        with self.assertRaises(tap.TapError):
            self.resolve(self.image(b"\x90" * 64))

    def test_start_injects_resolved_offset_without_relaxing_selected_address(self):
        session, script = mock.Mock(), mock.Mock()
        session.create_script.return_value = script
        for offset in (8, 24):
            with self.subTest(offset=offset), \
                    mock.patch.dict("sys.modules", frida=SimpleNamespace(attach=lambda pid: session)), \
                    mock.patch.object(tap, "selected_host_and_address", return_value=(123, "060504030201")), \
                    mock.patch.object(tap, "_driver_layout", return_value=(0x157FC, offset)):
                instance = tap.HidTap("a" * 64)
                instance.start()
                source = session.create_script.call_args.args[0]
                self.assertIn(f"adapter.add({offset}).readByteArray(6)", source)
                self.assertIn("const expectedAddress = [1, 2, 3, 4, 5, 6];", source)
                self.assertNotIn("__ADDRESS_OFFSET__", source)
                instance.close()
