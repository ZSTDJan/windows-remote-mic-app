"""Verified Microsoft driver samples must resolve offline without widening trust.

The fixture records native DLL/PDB verification evidence; it is not runtime data.
The application only consumes chromecast_hid_tap_windows._KNOWN_LAYOUTS.
"""
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

from ovb_rc003 import chromecast_hid_tap_windows as tap


EVIDENCE = Path(__file__).with_name("fixtures") / "chromecast_hid_layouts.json"
LEGACY = {
    "8a18571ddfa447bd590354ccdf7800b815a098806359ab195d395158f9d07cc5": (0x12FCC, 24),
    "de2b7ff2a61d50473cfe1e00ec012e99bda9361d563249ef93dce59f576e9936": (0x1580C, 8),
    "372c3628e3366c18152199eb8fb554d27bb4f02d1356b1f514f711328df4a8d6": (0x157FC, 8),
}


class BuiltinLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.samples = json.loads(EVIDENCE.read_text(encoding="utf-8"))["samples"]

    def image(self, sample):
        return SimpleNamespace(
            FILE_HEADER=SimpleNamespace(Machine=sample["machine"]),
            sections=[SimpleNamespace(
                VirtualAddress=s["virtual_address"], Misc_VirtualSize=s["virtual_size"],
                SizeOfRawData=s["raw_size"], Characteristics=s["characteristics"],
            ) for s in sample["sections"]],
            get_data=mock.Mock(side_effect=AssertionError("known layout must not read a constructor")),
            close=mock.Mock(),
        )

    def test_catalog_preserves_legacy_and_only_adds_verified_samples(self):
        expected = dict(LEGACY)
        digests = [row["sha256"] for row in self.samples]
        self.assertEqual(len(digests), len(set(digests)))
        for row in self.samples:
            self.assertEqual(row["machine"], 0x8664)
            self.assertTrue(row["dll_url"].startswith("https://msdl.microsoft.com/download/symbols/"))
            self.assertTrue(row["pdb_url"].startswith("https://msdl.microsoft.com/download/symbols/"))
            self.assertRegex(row["sha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(row["pdb_sha256"], r"^[0-9a-f]{64}$")
            expected[row["sha256"]] = (row["callback_rva"], row["address_offset"])
        self.assertEqual(tap._KNOWN_LAYOUTS, expected)

    def test_every_verified_sample_uses_its_exact_layout_without_symbols(self):
        with tempfile.TemporaryDirectory() as directory:
            module = Path(directory) / "driver.dll"
            module.write_bytes(b"fixture driver; digest supplied by native evidence")
            for row in self.samples:
                image = self.image(row)
                with self.subTest(version=row["version"], digest=row["sha256"]), \
                        mock.patch.object(tap.hashlib, "sha256") as sha, \
                        mock.patch("pefile.PE", return_value=image), \
                        mock.patch.object(tap, "_resolve_pdb_symbols",
                                          side_effect=AssertionError("unexpected symbol access")) as resolver:
                    sha.return_value.hexdigest.return_value = row["sha256"]
                    self.assertEqual(tap._driver_layout(module),
                                     (row["callback_rva"], row["address_offset"]))
                    resolver.assert_not_called()
                    image.close.assert_called_once()

    def test_known_hash_still_requires_x64_and_backed_executable_callback(self):
        sample = self.samples[0]
        with tempfile.TemporaryDirectory() as directory:
            module = Path(directory) / "driver.dll"
            module.write_bytes(b"fixture driver")
            for invalid in ("architecture", "nonexecutable", "unbacked", "overlap"):
                image = self.image(sample)
                if invalid == "architecture":
                    image.FILE_HEADER.Machine = 0xAA64
                elif invalid == "nonexecutable":
                    for section in image.sections:
                        section.Characteristics = 0
                elif invalid == "unbacked":
                    for section in image.sections:
                        section.SizeOfRawData = 0
                else:
                    image.sections += list(image.sections)
                with self.subTest(invalid=invalid), \
                        mock.patch.object(tap.hashlib, "sha256") as sha, \
                        mock.patch("pefile.PE", return_value=image), \
                        mock.patch.object(tap, "_resolve_pdb_symbols",
                                          side_effect=AssertionError("unexpected symbol access")):
                    sha.return_value.hexdigest.return_value = sample["sha256"]
                    with self.assertRaises(tap.TapError):
                        tap._driver_layout(module)
                    image.close.assert_called_once()

    def test_unlisted_file_still_requires_symbol_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            module = Path(directory) / "driver.dll"
            module.write_bytes(b"unlisted driver contents")
            with mock.patch.object(tap, "_resolve_pdb_symbols",
                                   side_effect=tap.TapError("symbol probe required")) as resolver:
                with self.assertRaisesRegex(tap.TapError, "symbol probe required"):
                    tap._driver_layout(module)
                resolver.assert_called_once_with(module)


if __name__ == "__main__":
    unittest.main()
