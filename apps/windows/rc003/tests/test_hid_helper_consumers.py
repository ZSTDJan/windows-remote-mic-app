import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ovb_rc003 import hid_elevation_windows, hid_helper_consumers


class HidHelperConsumerTests(unittest.TestCase):
    def _distribution(self, root: Path, name: str, *, installed: bool) -> Path:
        directory = root / name
        directory.mkdir()
        app = directory / "RemoteMicRC003.exe"
        app.write_bytes(b"app")
        helper = directory / hid_elevation_windows.HELPER_BUNDLE_RELATIVE_PATH
        helper.parent.mkdir()
        helper.write_bytes((name + "-helper").encode("ascii"))
        if installed:
            (directory / "unins000.exe").write_bytes(b"uninstaller")
        return app

    def test_portable_consumer_is_registered_and_detected(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config_root = root / "config"
            portable = self._distribution(root, "portable", installed=False)

            marker = hid_helper_consumers.register_current_consumer(
                config_root,
                frozen=True,
                executable=str(portable),
            )

            self.assertIsNotNone(marker)
            self.assertTrue(
                hid_helper_consumers.has_valid_portable_consumer(config_root)
            )

    def test_installed_consumer_does_not_block_helper_removal(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config_root = root / "config"
            installed = self._distribution(root, "installed", installed=True)
            hid_helper_consumers.register_current_consumer(
                config_root,
                frozen=True,
                executable=str(installed),
            )

            self.assertFalse(
                hid_helper_consumers.has_valid_portable_consumer(config_root)
            )

    def test_stale_or_tampered_marker_is_ignored(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config_root = root / "config"
            portable = self._distribution(root, "portable", installed=False)
            marker = hid_helper_consumers.register_current_consumer(
                config_root,
                frozen=True,
                executable=str(portable),
            )
            assert marker is not None
            helper = portable.parent / hid_elevation_windows.HELPER_BUNDLE_RELATIVE_PATH
            helper.write_bytes(b"tampered")

            self.assertFalse(
                hid_helper_consumers.has_valid_portable_consumer(config_root)
            )

    def test_unregister_removes_only_the_current_copy(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config_root = root / "config"
            first = self._distribution(root, "first", installed=False)
            second = self._distribution(root, "second", installed=False)
            first_marker = hid_helper_consumers.register_current_consumer(
                config_root, frozen=True, executable=str(first)
            )
            second_marker = hid_helper_consumers.register_current_consumer(
                config_root, frozen=True, executable=str(second)
            )

            hid_helper_consumers.unregister_current_consumer(
                config_root, executable=str(first)
            )

            self.assertFalse(first_marker.is_file())
            self.assertTrue(second_marker.is_file())

    def test_install_preserves_the_specific_helper_failure_detail(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config_root = root / "config"
            portable = self._distribution(root, "portable", installed=False)

            state = hid_helper_consumers.install_for_current_consumer(
                config_root,
                lambda: hid_elevation_windows.HidHelperState(
                    False, "hid_helper_task_acl_invalid"
                ),
                frozen=True,
                executable=str(portable),
            )

            self.assertEqual(
                state,
                hid_elevation_windows.HidHelperState(
                    False, "hid_helper_task_acl_invalid"
                ),
            )

    def test_portable_remove_keeps_its_marker_when_newer_helper_is_preserved(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config_root = root / "config"
            portable = self._distribution(root, "portable", installed=False)
            marker = hid_helper_consumers.register_current_consumer(
                config_root, frozen=True, executable=str(portable)
            )
            assert marker is not None

            state = hid_helper_consumers.remove_for_portable_consumer(
                config_root,
                lambda: hid_elevation_windows.HidHelperState(
                    True, "helper_preserved_newer_contract"
                ),
                frozen=True,
                executable=str(portable),
            )

            self.assertEqual(
                state,
                hid_elevation_windows.HidHelperState(
                    False, "helper_preserved_newer_contract"
                ),
            )
            self.assertTrue(marker.is_file())

    def test_distribution_uninstall_can_leave_a_newer_shared_helper(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config_root = root / "config"
            installed = self._distribution(root, "installed", installed=True)
            marker = hid_helper_consumers.register_current_consumer(
                config_root, frozen=True, executable=str(installed)
            )
            assert marker is not None

            state = hid_helper_consumers.uninstall_current_distribution(
                config_root,
                lambda: hid_elevation_windows.HidHelperState(
                    True, "helper_preserved_newer_contract"
                ),
                frozen=True,
                executable=str(installed),
            )

            self.assertEqual(
                state,
                hid_elevation_windows.HidHelperState(
                    True, "helper_preserved_newer_contract"
                ),
            )
            self.assertFalse(marker.exists())

    def test_distribution_uninstall_drops_its_marker_and_keeps_an_unknown_consumer(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config_root = root / "config"
            installed = self._distribution(root, "installed", installed=True)
            marker = hid_helper_consumers.register_current_consumer(
                config_root, frozen=True, executable=str(installed)
            )
            assert marker is not None
            remove_helper = mock.Mock(
                return_value=hid_elevation_windows.HidHelperState(True)
            )

            with mock.patch.object(
                hid_helper_consumers,
                "_inspect_other_consumers_unlocked",
                return_value=hid_helper_consumers.OtherConsumerPresence.UNKNOWN,
            ):
                state = hid_helper_consumers.uninstall_current_distribution(
                    config_root,
                    remove_helper,
                    frozen=True,
                    executable=str(installed),
                )

            self.assertEqual(
                state,
                hid_elevation_windows.HidHelperState(
                    True, "helper_kept_for_unknown_consumer"
                ),
            )
            self.assertFalse(marker.is_file())
            remove_helper.assert_not_called()

    def test_distribution_uninstall_keeps_helper_for_real_damaged_consumers(self):
        for damage in ("invalid_json", "missing_helper", "hash_mismatch"):
            with self.subTest(damage=damage), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                config_root = root / "config"
                installed = self._distribution(root, "installed", installed=True)
                portable = self._distribution(root, "portable", installed=False)
                installed_marker = hid_helper_consumers.register_current_consumer(
                    config_root,
                    frozen=True,
                    executable=str(installed),
                )
                portable_marker = hid_helper_consumers.register_current_consumer(
                    config_root,
                    frozen=True,
                    executable=str(portable),
                )
                assert installed_marker is not None
                assert portable_marker is not None
                portable_helper = (
                    portable.parent
                    / hid_elevation_windows.HELPER_BUNDLE_RELATIVE_PATH
                )
                if damage == "invalid_json":
                    portable_marker.write_text("{broken", encoding="utf-8")
                elif damage == "missing_helper":
                    portable_helper.unlink()
                else:
                    portable_helper.write_bytes(b"tampered")
                remove_helper = mock.Mock(
                    return_value=hid_elevation_windows.HidHelperState(True)
                )

                state = hid_helper_consumers.uninstall_current_distribution(
                    config_root,
                    remove_helper,
                    frozen=True,
                    executable=str(installed),
                )

                self.assertEqual(
                    state,
                    hid_elevation_windows.HidHelperState(
                        True, "helper_kept_for_unknown_consumer"
                    ),
                )
                self.assertFalse(installed_marker.exists())
                self.assertTrue(portable_marker.exists())
                remove_helper.assert_not_called()

    def test_distribution_uninstall_restores_marker_when_helper_removal_fails(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config_root = root / "config"
            installed = self._distribution(root, "installed", installed=True)
            marker = hid_helper_consumers.register_current_consumer(
                config_root, frozen=True, executable=str(installed)
            )
            assert marker is not None

            state = hid_helper_consumers.uninstall_current_distribution(
                config_root,
                lambda: hid_elevation_windows.HidHelperState(
                    False, "uac_cancelled"
                ),
                frozen=True,
                executable=str(installed),
            )

            self.assertEqual(
                state,
                hid_elevation_windows.HidHelperState(False, "uac_cancelled"),
            )
            self.assertTrue(marker.is_file())


if __name__ == "__main__":
    unittest.main()
