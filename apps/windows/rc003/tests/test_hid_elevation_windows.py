import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ovb_rc003 import hid_elevation_windows


SID = "S-1-5-21-111-222-333-1001"


class TaskDefinitionTests(unittest.TestCase):
    def setUp(self):
        self.helper = Path(r"C:\Program Files\RemoteMic\RC003\protected") / hid_elevation_windows.HELPER_EXE_NAME
        self.task_name = hid_elevation_windows.task_name_for_sid(SID)
        self.xml = hid_elevation_windows.task_definition_xml(
            self.helper, SID, task_name=self.task_name
        )

    def test_task_is_on_demand_highest_and_has_one_fixed_action(self):
        self.assertTrue(
            hid_elevation_windows.validate_registered_task_xml(
                self.xml,
                helper_path=self.helper,
                user_sid=SID,
                task_name=self.task_name,
            )
        )
        self.assertNotIn("<Triggers", self.xml)
        self.assertEqual(self.xml.count("<Exec>"), 1)
        self.assertIn("<LogonType>InteractiveToken</LogonType>", self.xml)
        self.assertIn("<RunLevel>HighestAvailable</RunLevel>", self.xml)
        self.assertIn("<AllowStartOnDemand>true</AllowStartOnDemand>", self.xml)
        self.assertIn("<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>", self.xml)
        self.assertIn(f"<Arguments>{hid_elevation_windows.INJECT_FLAG}</Arguments>", self.xml)
        self.assertNotIn("--pid", self.xml)
        self.assertIn(f"<URI>{self.task_name}</URI>", self.xml)

    def test_task_validation_rejects_dynamic_arguments_or_a_trigger(self):
        dynamic = self.xml.replace(
            f"<Arguments>{hid_elevation_windows.INJECT_FLAG}</Arguments>",
            "<Arguments>--inject --pid 2468</Arguments>",
        )
        triggered = self.xml.replace(
            "  <Principals>",
            "  <Triggers><LogonTrigger /></Triggers>\n  <Principals>",
        )
        self.assertFalse(
            hid_elevation_windows.validate_registered_task_xml(
                dynamic,
                helper_path=self.helper,
                user_sid=SID,
                task_name=self.task_name,
            )
        )
        self.assertFalse(
            hid_elevation_windows.validate_registered_task_xml(
                triggered,
                helper_path=self.helper,
                user_sid=SID,
                task_name=self.task_name,
            )
        )

    def test_task_validation_rejects_extra_actions_and_restart_policy(self):
        extra_exec = self.xml.replace(
            "  </Actions>",
            "    <Exec><Command>C:\\bad.exe</Command></Exec>\n  </Actions>",
        )
        com_handler = self.xml.replace(
            "  </Actions>",
            "    <ComHandler><ClassId>{00000000-0000-0000-0000-000000000000}</ClassId></ComHandler>\n  </Actions>",
        )
        second_actions = self.xml.replace(
            "</Task>",
            "<Actions Context=\"Author\"><Exec><Command>C:\\bad.exe</Command></Exec></Actions></Task>",
        )
        restart = self.xml.replace(
            "    <Priority>7</Priority>",
            "    <Priority>7</Priority><RestartOnFailure><Interval>PT1M</Interval><Count>3</Count></RestartOnFailure>",
        )
        for invalid in (extra_exec, com_handler, second_actions, restart):
            self.assertFalse(
                hid_elevation_windows.validate_registered_task_xml(
                    invalid,
                    helper_path=self.helper,
                    user_sid=SID,
                    task_name=self.task_name,
                )
            )

    def test_task_and_helper_names_are_isolated_by_sid(self):
        other_sid = "S-1-5-21-111-222-333-1002"
        self.assertNotEqual(
            hid_elevation_windows.task_name_for_sid(SID),
            hid_elevation_windows.task_name_for_sid(other_sid),
        )
        self.assertNotEqual(
            hid_elevation_windows.protected_owner_root(
                SID, program_files_root=Path(r"C:\Program Files")
            ),
            hid_elevation_windows.protected_owner_root(
                other_sid, program_files_root=Path(r"C:\Program Files")
            ),
        )
        self.assertNotEqual(self.task_name, hid_elevation_windows.LEGACY_TASK_NAME)

    def test_task_sddl_grants_only_admin_control_and_owner_run(self):
        sddl = hid_elevation_windows.task_security_sddl(SID)
        self.assertTrue(
            hid_elevation_windows.validate_task_security_sddl(
                sddl, user_sid=SID
            )
        )
        self.assertFalse(
            hid_elevation_windows.validate_task_security_sddl(
                sddl.replace(f"(A;;FRFX;;;{SID})", f"(A;;FA;;;{SID})"),
                user_sid=SID,
            )
        )
        self.assertFalse(
            hid_elevation_windows.validate_task_security_sddl(
                sddl + "(A;;FR;;;WD)", user_sid=SID
            )
        )
        self.assertFalse(
            hid_elevation_windows.validate_task_security_sddl(
                sddl.replace(f"(A;;FRFX;;;{SID})", f"(A;;FR;;;{SID})"),
                user_sid=SID,
            )
        )


class TaskSchedulerApiTests(unittest.TestCase):
    def test_missing_task_is_reported_by_collection_enumeration(self):
        tasks = mock.Mock()
        tasks.Count = 0
        root = mock.Mock()
        root.GetTasks.return_value = tasks

        self.assertIsNone(
            hid_elevation_windows._read_registered_task(
                r"\RemoteMicRC003-HidTap-missing",
                _root=root,
            )
        )
        root.GetTask.assert_not_called()

    def test_register_uses_dont_add_principal_ace(self):
        root = mock.Mock()
        hid_elevation_windows._register_task(
            "task", "xml", "sddl", _root=root
        )

        flags = root.RegisterTask.call_args.args[2]
        self.assertEqual(
            flags,
            hid_elevation_windows.TASK_CREATE_OR_UPDATE
            | hid_elevation_windows.TASK_DONT_ADD_PRINCIPAL_ACE,
        )


class ProtectedPathTests(unittest.TestCase):
    def test_path_acl_rejects_user_write_or_extra_everyone_access(self):
        valid = hid_elevation_windows._path_security_sddl_text(
            SID, directory=True
        )
        self.assertTrue(
            hid_elevation_windows.validate_path_security_sddl(
                valid, user_sid=SID, directory=True
            )
        )
        self.assertFalse(
            hid_elevation_windows.validate_path_security_sddl(
                valid.replace(f"(A;OICI;GRGX;;;{SID})", f"(A;OICI;FA;;;{SID})"),
                user_sid=SID,
                directory=True,
            )
        )
        self.assertFalse(
            hid_elevation_windows.validate_path_security_sddl(
                valid + "(A;OICI;GR;;;WD)", user_sid=SID, directory=True
            )
        )

    def test_reparse_component_is_rejected_before_directory_creation(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            existing = root / "RemoteMic"
            existing.mkdir()
            target = existing / "RC003" / "protected"
            with mock.patch.object(
                hid_elevation_windows,
                "_is_reparse_point",
                side_effect=lambda path: path == existing,
            ), mock.patch.object(
                hid_elevation_windows, "_apply_path_security"
            ) as secure:
                with self.assertRaises(hid_elevation_windows.HidElevationError):
                    hid_elevation_windows.ensure_protected_directory(
                        target,
                        user_sid=SID,
                        trusted_root=root,
                        security_root=target,
                    )

            self.assertFalse((existing / "RC003").exists())
            secure.assert_not_called()

    def test_dangling_reparse_component_is_still_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            dangling = root / "dangling"
            real_lexists = hid_elevation_windows.os.path.lexists

            def lexists(path):
                return Path(path) == dangling or real_lexists(path)

            with mock.patch.object(
                hid_elevation_windows.os.path,
                "lexists",
                side_effect=lexists,
            ), mock.patch.object(
                hid_elevation_windows,
                "_is_reparse_point",
                side_effect=lambda path: Path(path) == dangling,
            ):
                with self.assertRaises(
                    hid_elevation_windows.HidElevationError
                ) as ctx:
                    hid_elevation_windows.assert_no_reparse_points(
                        dangling / "child",
                        trusted_root=root,
                    )

            self.assertEqual(str(ctx.exception), "protected_path_reparse_point")


class InstalledHelperTests(unittest.TestCase):
    @staticmethod
    def _acl(path: Path) -> str:
        return hid_elevation_windows._path_security_sddl_text(
            SID, directory=path.is_dir()
        )

    def _fixture(self, raw: str, *, helper_bytes: bytes = b"helper"):
        root = Path(raw)
        app = root / "app"
        app.mkdir()
        bundled = app / hid_elevation_windows.HELPER_BUNDLE_RELATIVE_PATH
        bundled.parent.mkdir()
        bundled.write_bytes(helper_bytes)
        owner_root = root / "protected"
        owner_root.mkdir()
        digest = hid_elevation_windows._sha256(bundled)
        manifest = hid_elevation_windows._build_manifest(SID, digest)
        target = owner_root / Path(manifest.helper_relative_path)
        target.parent.mkdir(parents=True)
        target.write_bytes(helper_bytes)
        manifest_path = owner_root / hid_elevation_windows.MANIFEST_FILENAME
        manifest_path.write_text(
            __import__("json").dumps(manifest.as_dict()), encoding="utf-8"
        )
        xml = hid_elevation_windows.task_definition_xml(
            target, SID, task_name=manifest.task_name
        )
        task = hid_elevation_windows._RegisteredTaskSnapshot(
            xml,
            hid_elevation_windows.task_security_sddl(SID),
        )
        return root, app, owner_root, manifest_path, target, manifest, task

    def test_distribution_detection_requires_frozen_executable_and_uninstaller(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            executable = root / "RemoteMicRC003.exe"
            executable.write_bytes(b"app")

            self.assertFalse(
                hid_elevation_windows.is_installed_distribution(
                    frozen=False,
                    executable=str(executable),
                )
            )
            self.assertFalse(
                hid_elevation_windows.is_installed_distribution(
                    frozen=True,
                    executable=str(executable),
                )
            )

            (root / "unins000.exe").write_bytes(b"uninstaller")

            self.assertTrue(
                hid_elevation_windows.is_installed_distribution(
                    frozen=True,
                    executable=str(executable),
                )
            )

    def test_matching_helper_and_task_are_available(self):
        with tempfile.TemporaryDirectory() as raw:
            root, app, owner_root, _manifest_path, _target, _manifest, task = (
                self._fixture(raw)
            )

            state = hid_elevation_windows.inspect_installed_helper(
                frozen=True,
                executable=str(app / "RemoteMicRC003.exe"),
                user_sid=SID,
                owner_root=owner_root,
                program_files_root=root,
                _read_task=lambda _name: task,
                _read_acl=self._acl,
            )

        self.assertEqual(state, hid_elevation_windows.HidHelperState(True))

    def test_hash_mismatch_fails_before_task_execution(self):
        with tempfile.TemporaryDirectory() as raw:
            root, app, owner_root, _manifest_path, target, _manifest, _task = (
                self._fixture(raw)
            )
            target.write_bytes(b"corrupt")
            task_reader = mock.Mock()

            state = hid_elevation_windows.inspect_installed_helper(
                frozen=True,
                executable=str(app / "RemoteMicRC003.exe"),
                user_sid=SID,
                owner_root=owner_root,
                program_files_root=root,
                _read_task=task_reader,
                _read_acl=self._acl,
            )

        self.assertEqual(state.detail, "protected_helper_hash_mismatch")
        task_reader.assert_not_called()

    def test_same_generation_from_an_older_build_is_reused(self):
        with tempfile.TemporaryDirectory() as raw:
            root, app, owner_root, _manifest_path, _target, _manifest, task = (
                self._fixture(raw, helper_bytes=b"older-helper")
            )
            bundled = app / hid_elevation_windows.HELPER_BUNDLE_RELATIVE_PATH
            bundled.write_bytes(b"current-helper")

            state = hid_elevation_windows.inspect_installed_helper(
                frozen=True,
                executable=str(app / "RemoteMicRC003.exe"),
                user_sid=SID,
                owner_root=owner_root,
                program_files_root=root,
                _read_task=lambda _name: task,
                _read_acl=self._acl,
            )

        self.assertEqual(state, hid_elevation_windows.HidHelperState(True))

    def test_invalid_newer_generation_is_reported_without_repair(self):
        with tempfile.TemporaryDirectory() as raw:
            root, app, owner_root, manifest_path, target, manifest, _task = (
                self._fixture(raw)
            )
            newer_generation = hid_elevation_windows.HELPER_GENERATION + 1
            newer_relative = hid_elevation_windows.helper_relative_path(
                manifest.helper_sha256, generation=newer_generation
            )
            newer_target = owner_root / newer_relative
            newer_target.parent.mkdir(parents=True)
            target.replace(newer_target)
            data = manifest.as_dict()
            data["helper_generation"] = newer_generation
            data["helper_relative_path"] = newer_relative.as_posix()
            manifest_path.write_text(json.dumps(data), encoding="utf-8")

            state = hid_elevation_windows.inspect_installed_helper(
                frozen=True,
                executable=str(app / "RemoteMicRC003.exe"),
                user_sid=SID,
                owner_root=owner_root,
                program_files_root=root,
                _read_task=lambda _name: None,
                _read_acl=self._acl,
            )

        self.assertEqual(
            state,
            hid_elevation_windows.HidHelperState(False, "newer_helper_invalid"),
        )

    def test_same_protocol_newer_generation_is_reused(self):
        with tempfile.TemporaryDirectory() as raw:
            root, app, owner_root, manifest_path, target, manifest, _task = (
                self._fixture(raw)
            )
            newer_generation = hid_elevation_windows.HELPER_GENERATION + 1
            newer_relative = hid_elevation_windows.helper_relative_path(
                manifest.helper_sha256, generation=newer_generation
            )
            newer_target = owner_root / newer_relative
            newer_target.parent.mkdir(parents=True)
            target.replace(newer_target)
            data = manifest.as_dict()
            data["helper_generation"] = newer_generation
            data["helper_relative_path"] = newer_relative.as_posix()
            manifest_path.write_text(__import__("json").dumps(data), encoding="utf-8")
            xml = hid_elevation_windows.task_definition_xml(
                newer_target, SID, task_name=manifest.task_name
            )
            task = hid_elevation_windows._RegisteredTaskSnapshot(
                xml, hid_elevation_windows.task_security_sddl(SID)
            )

            state = hid_elevation_windows.inspect_installed_helper(
                frozen=True,
                executable=str(app / "RemoteMicRC003.exe"),
                user_sid=SID,
                owner_root=owner_root,
                program_files_root=root,
                _read_task=lambda _name: task,
                _read_acl=self._acl,
            )

        self.assertTrue(state.available)

    def test_older_task_contract_requires_one_repair(self):
        with tempfile.TemporaryDirectory() as raw:
            root, app, owner_root, manifest_path, _target, manifest, task = (
                self._fixture(raw)
            )
            data = manifest.as_dict()
            data["task_contract_version"] = (
                hid_elevation_windows.TASK_CONTRACT_VERSION - 1
            )
            manifest_path.write_text(__import__("json").dumps(data), encoding="utf-8")

            state = hid_elevation_windows.inspect_installed_helper(
                frozen=True,
                executable=str(app / "RemoteMicRC003.exe"),
                user_sid=SID,
                owner_root=owner_root,
                program_files_root=root,
                _read_task=lambda _name: task,
                _read_acl=self._acl,
            )

        self.assertEqual(state.detail, "protected_helper_outdated")

    def test_manifest_rejects_absolute_and_parent_relative_helper_paths(self):
        base = hid_elevation_windows._build_manifest(
            SID, "a" * 64
        ).as_dict()
        for invalid in ("C:/Windows/bad.exe", "../bad.exe", "generations/../bad.exe"):
            candidate = dict(base)
            candidate["helper_relative_path"] = invalid
            with self.assertRaises(hid_elevation_windows.HidElevationError):
                hid_elevation_windows._manifest_from_mapping(candidate)

    def test_source_runtime_never_offers_task_installation(self):
        state = hid_elevation_windows.inspect_installed_helper(frozen=False)
        self.assertEqual(state, hid_elevation_windows.HidHelperState(False, "source_runtime"))

    def test_frozen_distribution_keeps_bundled_helper_under_internal(self):
        with tempfile.TemporaryDirectory() as raw:
            executable = Path(raw) / "RemoteMicRC003.exe"
            self.assertEqual(
                hid_elevation_windows.bundled_helper_path(
                    frozen=True,
                    executable=str(executable),
                ),
                Path(raw) / "_internal" / hid_elevation_windows.HELPER_EXE_NAME,
            )

    def test_offer_id_is_stable_for_different_binaries_in_the_same_contract(self):
        with tempfile.TemporaryDirectory() as raw:
            executable = Path(raw) / "RemoteMicRC003.exe"
            helper = Path(raw) / hid_elevation_windows.HELPER_BUNDLE_RELATIVE_PATH
            helper.parent.mkdir()
            helper.write_bytes(b"first-helper")
            first = hid_elevation_windows.bundled_helper_offer_id(
                frozen=True, executable=str(executable)
            )
            helper.write_bytes(b"second-helper")
            second = hid_elevation_windows.bundled_helper_offer_id(
                frozen=True, executable=str(executable)
            )

        self.assertEqual(first, second)
        self.assertEqual(
            first,
            (
                f"{hid_elevation_windows.MANIFEST_SCHEMA_VERSION}:"
                f"{hid_elevation_windows.HELPER_PROTOCOL_VERSION}:"
                f"{hid_elevation_windows.HELPER_GENERATION}:"
                f"{hid_elevation_windows.TASK_CONTRACT_VERSION}"
            ),
        )


class TaskLifecycleTests(unittest.TestCase):
    @staticmethod
    def _acl(path: Path) -> str:
        return hid_elevation_windows._path_security_sddl_text(
            SID, directory=path.is_dir()
        )

    @staticmethod
    def _task_snapshot(target: Path):
        task_name = hid_elevation_windows.task_name_for_sid(SID)
        return hid_elevation_windows._RegisteredTaskSnapshot(
            hid_elevation_windows.task_definition_xml(
                target, SID, task_name=task_name
            ),
            hid_elevation_windows.task_security_sddl(SID),
        )

    def test_install_copies_helper_registers_and_commits_manifest_last(self):
        with tempfile.TemporaryDirectory() as raw:
            program_files = Path(raw)
            source = program_files / "bundle" / hid_elevation_windows.HELPER_EXE_NAME
            source.parent.mkdir()
            source.write_bytes(b"helper-binary")
            owner_root = program_files / "protected"
            registered = None

            def register(name, xml, sddl):
                nonlocal registered
                registered = hid_elevation_windows._RegisteredTaskSnapshot(xml, sddl)

            def read_task(name):
                if name == hid_elevation_windows.LEGACY_TASK_NAME:
                    return None
                return registered

            with mock.patch.object(
                hid_elevation_windows, "is_process_elevated", return_value=True
            ), mock.patch.object(
                hid_elevation_windows, "current_user_sid", return_value=SID
            ), mock.patch.object(
                hid_elevation_windows, "_apply_path_security"
            ), mock.patch.object(
                hid_elevation_windows,
                "_read_path_security_sddl",
                side_effect=self._acl,
            ), mock.patch.object(
                hid_elevation_windows, "_register_task", side_effect=register
            ), mock.patch.object(
                hid_elevation_windows, "_read_registered_task", side_effect=read_task
            ):
                hid_elevation_windows.install_task(
                    source_executable=source,
                    request_sid=SID,
                    owner_root=owner_root,
                    program_files_root=program_files,
                    _read_acl=self._acl,
                )

            manifest = hid_elevation_windows._load_manifest(
                owner_root / hid_elevation_windows.MANIFEST_FILENAME
            )
            target = owner_root / Path(manifest.helper_relative_path)
            self.assertEqual(target.read_bytes(), source.read_bytes())
            self.assertIsNotNone(registered)
            self.assertTrue(
                hid_elevation_windows.validate_registered_task_xml(
                    registered.xml_text,
                    helper_path=target,
                    user_sid=SID,
                    task_name=manifest.task_name,
                )
            )

    def test_install_removes_the_current_users_valid_legacy_task(self):
        with tempfile.TemporaryDirectory() as raw:
            program_files = Path(raw)
            source = program_files / "bundle" / hid_elevation_windows.HELPER_EXE_NAME
            source.parent.mkdir()
            source.write_bytes(b"helper-binary")
            owner_root = program_files / "protected"
            legacy_target = hid_elevation_windows.legacy_protected_helper_path(
                program_files_root=program_files
            )
            legacy_target.parent.mkdir(parents=True)
            legacy_target.write_bytes(b"old-helper")
            legacy_task = hid_elevation_windows._RegisteredTaskSnapshot(
                hid_elevation_windows.task_definition_xml(
                    legacy_target,
                    SID,
                    task_name=hid_elevation_windows.LEGACY_TASK_NAME,
                ),
                "legacy-sddl",
            )
            tasks = {hid_elevation_windows.LEGACY_TASK_NAME: legacy_task}

            def read_task(name):
                return tasks.get(name)

            def delete_task(name, *, missing_ok):
                self.assertFalse(missing_ok)
                tasks.pop(name, None)

            def register_task(name, xml, sddl):
                tasks[name] = hid_elevation_windows._RegisteredTaskSnapshot(
                    xml, sddl
                )

            with mock.patch.object(
                hid_elevation_windows, "is_process_elevated", return_value=True
            ), mock.patch.object(
                hid_elevation_windows, "current_user_sid", return_value=SID
            ), mock.patch.object(
                hid_elevation_windows, "_apply_path_security"
            ), mock.patch.object(
                hid_elevation_windows,
                "_read_path_security_sddl",
                side_effect=self._acl,
            ), mock.patch.object(
                hid_elevation_windows, "_read_registered_task", side_effect=read_task
            ), mock.patch.object(
                hid_elevation_windows, "_delete_task", side_effect=delete_task
            ), mock.patch.object(
                hid_elevation_windows, "_register_task", side_effect=register_task
            ):
                hid_elevation_windows.install_task(
                    source_executable=source,
                    request_sid=SID,
                    owner_root=owner_root,
                    program_files_root=program_files,
                    _read_acl=self._acl,
                )

            self.assertNotIn(hid_elevation_windows.LEGACY_TASK_NAME, tasks)
            self.assertFalse(legacy_target.exists())

    def test_install_preserves_another_users_legacy_task(self):
        with tempfile.TemporaryDirectory() as raw:
            program_files = Path(raw)
            legacy_target = hid_elevation_windows.legacy_protected_helper_path(
                program_files_root=program_files
            )
            legacy_target.parent.mkdir(parents=True)
            legacy_target.write_bytes(b"old-helper")
            other_sid = "S-1-5-21-111-222-333-1002"
            legacy_task = hid_elevation_windows._RegisteredTaskSnapshot(
                hid_elevation_windows.task_definition_xml(
                    legacy_target,
                    other_sid,
                    task_name=hid_elevation_windows.LEGACY_TASK_NAME,
                ),
                "legacy-sddl",
            )
            delete = mock.Mock()
            with mock.patch.object(
                hid_elevation_windows,
                "_read_registered_task",
                return_value=legacy_task,
            ), mock.patch.object(
                hid_elevation_windows, "_delete_task", delete
            ):
                removed = hid_elevation_windows._cleanup_legacy_contract_for_user(
                    SID,
                    trusted_root=program_files,
                )

            self.assertFalse(removed)
            self.assertTrue(legacy_target.exists())
            delete.assert_not_called()

    def test_sid_mismatch_fails_before_any_write(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / hid_elevation_windows.HELPER_EXE_NAME
            source.write_bytes(b"helper")
            with mock.patch.object(
                hid_elevation_windows, "is_process_elevated", return_value=True
            ), mock.patch.object(
                hid_elevation_windows,
                "current_user_sid",
                return_value="S-1-5-21-111-222-333-1002",
            ), mock.patch.object(
                hid_elevation_windows, "ensure_protected_directory"
            ) as mkdir, mock.patch.object(
                hid_elevation_windows, "_register_task"
            ) as register:
                with self.assertRaises(hid_elevation_windows.HidElevationError) as ctx:
                    hid_elevation_windows.install_task(
                        source_executable=source,
                        request_sid=SID,
                        owner_root=root / "protected",
                        program_files_root=root,
                    )

            self.assertEqual(str(ctx.exception), "request_sid_mismatch")
            mkdir.assert_not_called()
            register.assert_not_called()

    def test_failed_task_verification_restores_previous_task_and_manifest(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "bundle" / hid_elevation_windows.HELPER_EXE_NAME
            source.parent.mkdir()
            source.write_bytes(b"new-helper")
            owner_root = root / "protected"
            owner_root.mkdir()
            old_bytes = b"old-helper"
            old_source = root / "old-helper.exe"
            old_source.write_bytes(old_bytes)
            old_digest = hid_elevation_windows._sha256(old_source)
            old_manifest = hid_elevation_windows._build_manifest(SID, old_digest)
            old_relative = hid_elevation_windows.helper_relative_path(
                old_digest, generation=hid_elevation_windows.HELPER_GENERATION - 1 or 1
            )
            old_target = owner_root / old_relative
            old_target.parent.mkdir(parents=True)
            old_target.write_bytes(old_bytes)
            old_data = old_manifest.as_dict()
            old_data["helper_generation"] = max(1, hid_elevation_windows.HELPER_GENERATION - 1)
            old_data["helper_relative_path"] = old_relative.as_posix()
            (owner_root / hid_elevation_windows.MANIFEST_FILENAME).write_text(
                __import__("json").dumps(old_data), encoding="utf-8"
            )
            previous = hid_elevation_windows._RegisteredTaskSnapshot(
                hid_elevation_windows.task_definition_xml(
                    old_target,
                    SID,
                    task_name=old_manifest.task_name,
                ),
                hid_elevation_windows.task_security_sddl(SID),
            )
            current = previous
            register_calls = []

            def register(_name, xml, sddl):
                nonlocal current
                register_calls.append((xml, sddl))
                current = hid_elevation_windows._RegisteredTaskSnapshot(xml, sddl)

            reads = 0

            def read_task(name):
                nonlocal reads
                if name == hid_elevation_windows.LEGACY_TASK_NAME:
                    return None
                reads += 1
                if reads == 1:
                    return previous
                if len(register_calls) == 1:
                    return hid_elevation_windows._RegisteredTaskSnapshot(
                        register_calls[0][0].replace("</Actions>", "<ComHandler /></Actions>"),
                        register_calls[0][1],
                    )
                return current

            with mock.patch.object(
                hid_elevation_windows, "is_process_elevated", return_value=True
            ), mock.patch.object(
                hid_elevation_windows, "current_user_sid", return_value=SID
            ), mock.patch.object(
                hid_elevation_windows, "_apply_path_security"
            ), mock.patch.object(
                hid_elevation_windows,
                "_read_path_security_sddl",
                side_effect=self._acl,
            ), mock.patch.object(
                hid_elevation_windows, "_register_task", side_effect=register
            ), mock.patch.object(
                hid_elevation_windows, "_read_registered_task", side_effect=read_task
            ):
                with self.assertRaises(hid_elevation_windows.HidElevationError):
                    hid_elevation_windows.install_task(
                        source_executable=source,
                        request_sid=SID,
                        owner_root=owner_root,
                        program_files_root=root,
                        _read_acl=self._acl,
                    )

            self.assertGreaterEqual(len(register_calls), 2)
            self.assertEqual(register_calls[-1], (previous.xml_text, previous.security_sddl))
            saved = hid_elevation_windows._load_manifest(
                owner_root / hid_elevation_windows.MANIFEST_FILENAME
            )
            self.assertEqual(saved.helper_sha256, old_digest)

    def test_install_repairs_a_corrupt_manifest_and_task_owned_helper(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "bundle" / hid_elevation_windows.HELPER_EXE_NAME
            source.parent.mkdir()
            source.write_bytes(b"verified-current-helper")
            digest = hid_elevation_windows._sha256(source)
            owner_root = root / "protected"
            target = owner_root / hid_elevation_windows.helper_relative_path(digest)
            target.parent.mkdir(parents=True)
            target.write_bytes(b"corrupted-helper")
            manifest_path = owner_root / hid_elevation_windows.MANIFEST_FILENAME
            manifest_path.write_text("{not-json", encoding="utf-8")
            task_name = hid_elevation_windows.task_name_for_sid(SID)
            tasks = {task_name: self._task_snapshot(target)}

            def read_task(name):
                if name == hid_elevation_windows.LEGACY_TASK_NAME:
                    return None
                return tasks.get(name)

            def register_task(name, xml, sddl):
                tasks[name] = hid_elevation_windows._RegisteredTaskSnapshot(
                    xml, sddl
                )

            with mock.patch.object(
                hid_elevation_windows, "is_process_elevated", return_value=True
            ), mock.patch.object(
                hid_elevation_windows, "current_user_sid", return_value=SID
            ), mock.patch.object(
                hid_elevation_windows, "_apply_path_security"
            ), mock.patch.object(
                hid_elevation_windows, "_read_registered_task", side_effect=read_task
            ), mock.patch.object(
                hid_elevation_windows, "_register_task", side_effect=register_task
            ):
                hid_elevation_windows.install_task(
                    source_executable=source,
                    request_sid=SID,
                    owner_root=owner_root,
                    program_files_root=root,
                    _read_acl=self._acl,
                )

            saved = hid_elevation_windows._load_manifest(manifest_path)
            self.assertEqual(saved.helper_sha256, digest)
            self.assertEqual(target.read_bytes(), source.read_bytes())
            self.assertTrue(
                hid_elevation_windows.validate_registered_task_xml(
                    tasks[task_name].xml_text,
                    helper_path=target,
                    user_sid=SID,
                    task_name=task_name,
                )
            )

    def test_install_replaces_damaged_fixed_task_variants(self):
        for label in ("malformed_xml", "invalid_acl", "outside_command"):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                source = root / "bundle" / hid_elevation_windows.HELPER_EXE_NAME
                source.parent.mkdir()
                source.write_bytes(b"current-helper")
                owner_root = root / "protected"
                task_name = hid_elevation_windows.task_name_for_sid(SID)
                old_target = owner_root / hid_elevation_windows.helper_relative_path(
                    "a" * 64
                )
                valid_xml = hid_elevation_windows.task_definition_xml(
                    old_target, SID, task_name=task_name
                )
                if label == "malformed_xml":
                    previous = hid_elevation_windows._RegisteredTaskSnapshot(
                        "<Task>", hid_elevation_windows.task_security_sddl(SID)
                    )
                elif label == "invalid_acl":
                    previous = hid_elevation_windows._RegisteredTaskSnapshot(
                        valid_xml, "D:(A;;FA;;;WD)"
                    )
                else:
                    previous = hid_elevation_windows._RegisteredTaskSnapshot(
                        hid_elevation_windows.task_definition_xml(
                            root / "outside.exe", SID, task_name=task_name
                        ),
                        hid_elevation_windows.task_security_sddl(SID),
                    )
                tasks = {task_name: previous}

                def read_task(name):
                    if name == hid_elevation_windows.LEGACY_TASK_NAME:
                        return None
                    return tasks.get(name)

                def register_task(name, xml, sddl):
                    tasks[name] = hid_elevation_windows._RegisteredTaskSnapshot(
                        xml, sddl
                    )

                with mock.patch.object(
                    hid_elevation_windows, "is_process_elevated", return_value=True
                ), mock.patch.object(
                    hid_elevation_windows, "current_user_sid", return_value=SID
                ), mock.patch.object(
                    hid_elevation_windows, "_apply_path_security"
                ), mock.patch.object(
                    hid_elevation_windows,
                    "_read_registered_task",
                    side_effect=read_task,
                ), mock.patch.object(
                    hid_elevation_windows,
                    "_register_task",
                    side_effect=register_task,
                ):
                    hid_elevation_windows.install_task(
                        source_executable=source,
                        request_sid=SID,
                        owner_root=owner_root,
                        program_files_root=root,
                        _read_acl=self._acl,
                    )

                manifest = hid_elevation_windows._load_manifest(
                    owner_root / hid_elevation_windows.MANIFEST_FILENAME
                )
                target = owner_root / Path(manifest.helper_relative_path)
                self.assertEqual(target.read_bytes(), source.read_bytes())
                self.assertTrue(
                    hid_elevation_windows.validate_registered_task_xml(
                        tasks[task_name].xml_text,
                        helper_path=target,
                        user_sid=SID,
                        task_name=task_name,
                    )
                )

    def test_install_cleanup_reuses_a_valid_same_generation_helper(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "bundle" / hid_elevation_windows.HELPER_EXE_NAME
            source.parent.mkdir()
            source.write_bytes(b"newer-build-same-contract")
            owner_root = root / "protected"
            installed_source = root / "installed-helper.exe"
            installed_source.write_bytes(b"already-approved-helper")
            installed_digest = hid_elevation_windows._sha256(installed_source)
            manifest = hid_elevation_windows._build_manifest(SID, installed_digest)
            target = owner_root / Path(manifest.helper_relative_path)
            target.parent.mkdir(parents=True)
            target.write_bytes(installed_source.read_bytes())
            manifest_path = owner_root / hid_elevation_windows.MANIFEST_FILENAME
            manifest_path.write_text(json.dumps(manifest.as_dict()), encoding="utf-8")

            orphan_source = root / "old-helper.exe"
            orphan_source.write_bytes(b"old-generation")
            orphan_digest = hid_elevation_windows._sha256(orphan_source)
            orphan_target = owner_root / hid_elevation_windows.helper_relative_path(
                orphan_digest,
                generation=max(1, hid_elevation_windows.HELPER_GENERATION - 1),
            )
            orphan_target.parent.mkdir(parents=True)
            orphan_target.write_bytes(orphan_source.read_bytes())
            tasks = {manifest.task_name: self._task_snapshot(target)}

            def read_task(name):
                if name == hid_elevation_windows.LEGACY_TASK_NAME:
                    return None
                return tasks.get(name)

            def register_task(name, xml, sddl):
                tasks[name] = hid_elevation_windows._RegisteredTaskSnapshot(xml, sddl)

            with mock.patch.object(
                hid_elevation_windows, "is_process_elevated", return_value=True
            ), mock.patch.object(
                hid_elevation_windows, "current_user_sid", return_value=SID
            ), mock.patch.object(
                hid_elevation_windows, "_apply_path_security"
            ), mock.patch.object(
                hid_elevation_windows,
                "_read_registered_task",
                side_effect=read_task,
            ), mock.patch.object(
                hid_elevation_windows,
                "_register_task",
                side_effect=register_task,
            ):
                hid_elevation_windows.install_task(
                    source_executable=source,
                    request_sid=SID,
                    owner_root=owner_root,
                    program_files_root=root,
                    _read_acl=self._acl,
                )

            saved = hid_elevation_windows._load_manifest(manifest_path)
            bundled_target = owner_root / hid_elevation_windows.helper_relative_path(
                hid_elevation_windows._sha256(source)
            )
            self.assertEqual(saved.helper_sha256, installed_digest)
            self.assertEqual(target.read_bytes(), installed_source.read_bytes())
            self.assertFalse(bundled_target.exists())
            self.assertFalse(orphan_target.exists())

    def test_install_recovers_a_missing_manifest_and_cleans_the_old_helper(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "bundle" / hid_elevation_windows.HELPER_EXE_NAME
            source.parent.mkdir()
            source.write_bytes(b"new-helper")
            owner_root = root / "protected"
            old_source = root / "old-helper.exe"
            old_source.write_bytes(b"old-helper")
            old_digest = hid_elevation_windows._sha256(old_source)
            old_target = owner_root / hid_elevation_windows.helper_relative_path(
                old_digest,
                generation=max(1, hid_elevation_windows.HELPER_GENERATION - 1),
            )
            old_target.parent.mkdir(parents=True)
            old_target.write_bytes(old_source.read_bytes())
            task_name = hid_elevation_windows.task_name_for_sid(SID)
            tasks = {task_name: self._task_snapshot(old_target)}

            def read_task(name):
                if name == hid_elevation_windows.LEGACY_TASK_NAME:
                    return None
                return tasks.get(name)

            def register_task(name, xml, sddl):
                tasks[name] = hid_elevation_windows._RegisteredTaskSnapshot(
                    xml, sddl
                )

            with mock.patch.object(
                hid_elevation_windows, "is_process_elevated", return_value=True
            ), mock.patch.object(
                hid_elevation_windows, "current_user_sid", return_value=SID
            ), mock.patch.object(
                hid_elevation_windows, "_apply_path_security"
            ), mock.patch.object(
                hid_elevation_windows, "_read_registered_task", side_effect=read_task
            ), mock.patch.object(
                hid_elevation_windows, "_register_task", side_effect=register_task
            ):
                hid_elevation_windows.install_task(
                    source_executable=source,
                    request_sid=SID,
                    owner_root=owner_root,
                    program_files_root=root,
                    _read_acl=self._acl,
                )

            manifest = hid_elevation_windows._load_manifest(
                owner_root / hid_elevation_windows.MANIFEST_FILENAME
            )
            new_target = owner_root / Path(manifest.helper_relative_path)
            self.assertTrue(new_target.is_file())
            self.assertFalse(old_target.exists())

    def test_install_preserves_a_future_task_before_any_write(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "bundle" / hid_elevation_windows.HELPER_EXE_NAME
            source.parent.mkdir()
            source.write_bytes(b"current-helper")
            future_source = root / "future-helper.exe"
            future_source.write_bytes(b"future-helper")
            future_digest = hid_elevation_windows._sha256(future_source)
            owner_root = root / "protected"
            future_target = owner_root / hid_elevation_windows.helper_relative_path(
                future_digest,
                generation=hid_elevation_windows.HELPER_GENERATION + 1,
            )
            future_target.parent.mkdir(parents=True)
            future_target.write_bytes(future_source.read_bytes())
            task_name = hid_elevation_windows.task_name_for_sid(SID)
            task = self._task_snapshot(future_target)
            ensure_directory = mock.Mock()
            register_task = mock.Mock()

            def read_task(name):
                if name == hid_elevation_windows.LEGACY_TASK_NAME:
                    return None
                return task if name == task_name else None

            with mock.patch.object(
                hid_elevation_windows, "is_process_elevated", return_value=True
            ), mock.patch.object(
                hid_elevation_windows, "current_user_sid", return_value=SID
            ), mock.patch.object(
                hid_elevation_windows,
                "ensure_protected_directory",
                ensure_directory,
            ), mock.patch.object(
                hid_elevation_windows, "_register_task", register_task
            ), mock.patch.object(
                hid_elevation_windows, "_read_registered_task", side_effect=read_task
            ):
                with self.assertRaises(
                    hid_elevation_windows.HidElevationError
                ) as ctx:
                    hid_elevation_windows.install_task(
                        source_executable=source,
                        request_sid=SID,
                        owner_root=owner_root,
                        program_files_root=root,
                        _read_acl=self._acl,
                    )

            self.assertEqual(str(ctx.exception), "newer_helper_preserved")
            ensure_directory.assert_not_called()
            register_task.assert_not_called()
            self.assertTrue(future_target.is_file())

    def test_manifest_write_failure_restores_the_previous_contract(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "bundle" / hid_elevation_windows.HELPER_EXE_NAME
            source.parent.mkdir()
            source.write_bytes(b"new-helper")
            owner_root = root / "protected"
            owner_root.mkdir()
            old_source = root / "old-helper.exe"
            old_source.write_bytes(b"old-helper")
            old_digest = hid_elevation_windows._sha256(old_source)
            old_generation = max(1, hid_elevation_windows.HELPER_GENERATION - 1)
            old_relative = hid_elevation_windows.helper_relative_path(
                old_digest, generation=old_generation
            )
            old_target = owner_root / old_relative
            old_target.parent.mkdir(parents=True)
            old_target.write_bytes(old_source.read_bytes())
            old_manifest = hid_elevation_windows._build_manifest(SID, old_digest)
            old_data = old_manifest.as_dict()
            old_data["helper_generation"] = old_generation
            old_data["helper_relative_path"] = old_relative.as_posix()
            manifest_path = owner_root / hid_elevation_windows.MANIFEST_FILENAME
            manifest_path.write_text(
                json.dumps(old_data, sort_keys=True), encoding="utf-8"
            )
            old_manifest_bytes = manifest_path.read_bytes()
            task_name = hid_elevation_windows.task_name_for_sid(SID)
            previous_task = self._task_snapshot(old_target)
            tasks = {task_name: previous_task}

            def read_task(name):
                if name == hid_elevation_windows.LEGACY_TASK_NAME:
                    return None
                return tasks.get(name)

            def register_task(name, xml, sddl):
                tasks[name] = hid_elevation_windows._RegisteredTaskSnapshot(
                    xml, sddl
                )

            def fail_after_replace(path, manifest):
                path.write_text(json.dumps(manifest.as_dict()), encoding="utf-8")
                raise OSError("simulated manifest commit failure")

            with mock.patch.object(
                hid_elevation_windows, "is_process_elevated", return_value=True
            ), mock.patch.object(
                hid_elevation_windows, "current_user_sid", return_value=SID
            ), mock.patch.object(
                hid_elevation_windows, "_apply_path_security"
            ), mock.patch.object(
                hid_elevation_windows, "_read_registered_task", side_effect=read_task
            ), mock.patch.object(
                hid_elevation_windows, "_register_task", side_effect=register_task
            ), mock.patch.object(
                hid_elevation_windows,
                "_write_manifest_atomic",
                side_effect=fail_after_replace,
            ):
                with self.assertRaises(hid_elevation_windows.HidElevationError):
                    hid_elevation_windows.install_task(
                        source_executable=source,
                        request_sid=SID,
                        owner_root=owner_root,
                        program_files_root=root,
                        _read_acl=self._acl,
                    )

            new_digest = hid_elevation_windows._sha256(source)
            new_target = owner_root / hid_elevation_windows.helper_relative_path(
                new_digest
            )
            self.assertEqual(manifest_path.read_bytes(), old_manifest_bytes)
            self.assertEqual(tasks.get(task_name), previous_task)
            self.assertTrue(old_target.is_file())
            self.assertFalse(new_target.exists())

    def test_uninstall_is_idempotent_when_manifest_is_absent(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            with mock.patch.object(
                hid_elevation_windows, "is_process_elevated", return_value=True
            ), mock.patch.object(
                hid_elevation_windows, "current_user_sid", return_value=SID
            ), mock.patch.object(
                hid_elevation_windows, "_read_registered_task", return_value=None
            ), mock.patch.object(hid_elevation_windows, "_delete_task") as delete:
                hid_elevation_windows.uninstall_task(
                    request_sid=SID,
                    owner_root=root / "protected",
                    program_files_root=root,
                )
            delete.assert_not_called()

    def test_uninstall_removes_a_verified_orphaned_helper_without_manifest(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            owner_root = root / "protected"
            helper_bytes = b"orphaned-helper"
            source = root / "source-helper.exe"
            source.write_bytes(helper_bytes)
            digest = hid_elevation_windows._sha256(source)
            target = owner_root / hid_elevation_windows.helper_relative_path(digest)
            target.parent.mkdir(parents=True)
            target.write_bytes(helper_bytes)

            with mock.patch.object(
                hid_elevation_windows, "is_process_elevated", return_value=True
            ), mock.patch.object(
                hid_elevation_windows, "current_user_sid", return_value=SID
            ), mock.patch.object(
                hid_elevation_windows, "_read_registered_task", return_value=None
            ):
                hid_elevation_windows.uninstall_task(
                    request_sid=SID,
                    owner_root=owner_root,
                    program_files_root=root,
                    _read_acl=self._acl,
                )

            self.assertFalse(target.exists())

    def test_uninstall_preserves_an_orphan_whose_hash_does_not_match_its_path(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            owner_root = root / "protected"
            target = owner_root / hid_elevation_windows.helper_relative_path(
                "a" * 64
            )
            target.parent.mkdir(parents=True)
            target.write_bytes(b"not-the-declared-helper")

            with mock.patch.object(
                hid_elevation_windows, "is_process_elevated", return_value=True
            ), mock.patch.object(
                hid_elevation_windows, "current_user_sid", return_value=SID
            ), mock.patch.object(
                hid_elevation_windows, "_read_registered_task", return_value=None
            ):
                with self.assertRaises(
                    hid_elevation_windows.HidElevationError
                ) as ctx:
                    hid_elevation_windows.uninstall_task(
                        request_sid=SID,
                        owner_root=owner_root,
                        program_files_root=root,
                        _read_acl=self._acl,
                    )

            self.assertEqual(str(ctx.exception), "protected_helper_hash_mismatch")
            self.assertTrue(target.exists())

    def test_uninstall_preserves_an_orphan_from_a_newer_generation(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            owner_root = root / "protected"
            source = root / "source-helper.exe"
            source.write_bytes(b"newer-helper")
            digest = hid_elevation_windows._sha256(source)
            target = owner_root / hid_elevation_windows.helper_relative_path(
                digest,
                generation=hid_elevation_windows.HELPER_GENERATION + 1,
            )
            target.parent.mkdir(parents=True)
            target.write_bytes(source.read_bytes())

            with mock.patch.object(
                hid_elevation_windows, "is_process_elevated", return_value=True
            ), mock.patch.object(
                hid_elevation_windows, "current_user_sid", return_value=SID
            ), mock.patch.object(
                hid_elevation_windows, "_read_registered_task", return_value=None
            ):
                with self.assertRaises(
                    hid_elevation_windows.HidElevationError
                ) as ctx:
                    hid_elevation_windows.uninstall_task(
                        request_sid=SID,
                        owner_root=owner_root,
                        program_files_root=root,
                        _read_acl=self._acl,
                    )

            self.assertEqual(str(ctx.exception), "newer_helper_preserved")
            self.assertTrue(target.exists())

    def test_uninstall_recovers_a_valid_task_when_its_manifest_is_missing(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            owner_root = root / "protected"
            source = root / "source-helper.exe"
            source.write_bytes(b"helper-without-manifest")
            digest = hid_elevation_windows._sha256(source)
            target = owner_root / hid_elevation_windows.helper_relative_path(digest)
            target.parent.mkdir(parents=True)
            target.write_bytes(source.read_bytes())
            task_name = hid_elevation_windows.task_name_for_sid(SID)
            task = hid_elevation_windows._RegisteredTaskSnapshot(
                hid_elevation_windows.task_definition_xml(
                    target, SID, task_name=task_name
                ),
                hid_elevation_windows.task_security_sddl(SID),
            )
            tasks = {task_name: task}

            def read_task(name):
                if name == hid_elevation_windows.LEGACY_TASK_NAME:
                    return None
                return tasks.get(name)

            def delete_task(name, *, missing_ok):
                self.assertFalse(missing_ok)
                tasks.pop(name, None)

            with mock.patch.object(
                hid_elevation_windows, "is_process_elevated", return_value=True
            ), mock.patch.object(
                hid_elevation_windows, "current_user_sid", return_value=SID
            ), mock.patch.object(
                hid_elevation_windows, "_apply_path_security"
            ), mock.patch.object(
                hid_elevation_windows,
                "_read_registered_task",
                side_effect=read_task,
            ), mock.patch.object(
                hid_elevation_windows, "_delete_task", side_effect=delete_task
            ):
                hid_elevation_windows.uninstall_task(
                    request_sid=SID,
                    owner_root=owner_root,
                    program_files_root=root,
                    _read_acl=self._acl,
                )

            self.assertNotIn(task_name, tasks)
            self.assertFalse(target.exists())

    def test_uninstall_recovers_invalid_manifest_variants_from_the_task(self):
        variants = {
            "bad-json": b"{not-json",
            "missing-field": json.dumps(
                {
                    key: value
                    for key, value in hid_elevation_windows._build_manifest(
                        SID, "a" * 64
                    ).as_dict().items()
                    if key != "task_name"
                }
            ).encode("utf-8"),
            "wrong-sid": json.dumps(
                {
                    **hid_elevation_windows._build_manifest(
                        SID, "a" * 64
                    ).as_dict(),
                    "owner_sid": "S-1-5-21-111-222-333-1002",
                }
            ).encode("utf-8"),
        }
        for label, manifest_bytes in variants.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                owner_root = root / "protected"
                source = root / "source-helper.exe"
                source.write_bytes(b"helper")
                digest = hid_elevation_windows._sha256(source)
                target = owner_root / hid_elevation_windows.helper_relative_path(
                    digest
                )
                target.parent.mkdir(parents=True)
                target.write_bytes(source.read_bytes())
                manifest_path = owner_root / hid_elevation_windows.MANIFEST_FILENAME
                manifest_path.write_bytes(manifest_bytes)
                task_name = hid_elevation_windows.task_name_for_sid(SID)
                tasks = {task_name: self._task_snapshot(target)}

                def read_task(name):
                    if name == hid_elevation_windows.LEGACY_TASK_NAME:
                        return None
                    return tasks.get(name)

                def delete_task(name, *, missing_ok):
                    self.assertFalse(missing_ok)
                    tasks.pop(name, None)

                with mock.patch.object(
                    hid_elevation_windows, "is_process_elevated", return_value=True
                ), mock.patch.object(
                    hid_elevation_windows, "current_user_sid", return_value=SID
                ), mock.patch.object(
                    hid_elevation_windows,
                    "_read_registered_task",
                    side_effect=read_task,
                ), mock.patch.object(
                    hid_elevation_windows, "_delete_task", side_effect=delete_task
                ):
                    hid_elevation_windows.uninstall_task(
                        request_sid=SID,
                        owner_root=owner_root,
                        program_files_root=root,
                        _read_acl=self._acl,
                    )

                self.assertNotIn(task_name, tasks)
                self.assertFalse(target.exists())
                self.assertFalse(manifest_path.exists())

    def test_uninstall_deletes_damaged_fixed_task_variants(self):
        for label in ("malformed_xml", "invalid_acl", "outside_command"):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                owner_root = root / "protected"
                source = root / "source-helper.exe"
                source.write_bytes(b"helper")
                digest = hid_elevation_windows._sha256(source)
                manifest = hid_elevation_windows._build_manifest(SID, digest)
                target = owner_root / Path(manifest.helper_relative_path)
                target.parent.mkdir(parents=True)
                target.write_bytes(source.read_bytes())
                manifest_path = owner_root / hid_elevation_windows.MANIFEST_FILENAME
                manifest_path.write_text(
                    json.dumps(manifest.as_dict()), encoding="utf-8"
                )
                valid_xml = hid_elevation_windows.task_definition_xml(
                    target, SID, task_name=manifest.task_name
                )
                if label == "malformed_xml":
                    previous = hid_elevation_windows._RegisteredTaskSnapshot(
                        "<Task>", hid_elevation_windows.task_security_sddl(SID)
                    )
                elif label == "invalid_acl":
                    previous = hid_elevation_windows._RegisteredTaskSnapshot(
                        valid_xml, "D:(A;;FA;;;WD)"
                    )
                else:
                    previous = hid_elevation_windows._RegisteredTaskSnapshot(
                        hid_elevation_windows.task_definition_xml(
                            root / "outside.exe",
                            SID,
                            task_name=manifest.task_name,
                        ),
                        hid_elevation_windows.task_security_sddl(SID),
                    )
                tasks = {manifest.task_name: previous}

                def read_task(name):
                    if name == hid_elevation_windows.LEGACY_TASK_NAME:
                        return None
                    return tasks.get(name)

                def delete_task(name, *, missing_ok):
                    self.assertFalse(missing_ok)
                    tasks.pop(name, None)

                with mock.patch.object(
                    hid_elevation_windows, "is_process_elevated", return_value=True
                ), mock.patch.object(
                    hid_elevation_windows, "current_user_sid", return_value=SID
                ), mock.patch.object(
                    hid_elevation_windows,
                    "_read_registered_task",
                    side_effect=read_task,
                ), mock.patch.object(
                    hid_elevation_windows,
                    "_delete_task",
                    side_effect=delete_task,
                ):
                    hid_elevation_windows.uninstall_task(
                        request_sid=SID,
                        owner_root=owner_root,
                        program_files_root=root,
                        _read_acl=self._acl,
                    )

                self.assertNotIn(manifest.task_name, tasks)
                self.assertFalse(target.exists())
                self.assertFalse(manifest_path.exists())

    def test_uninstall_removes_all_generations_with_the_active_helper_last(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            owner_root = root / "protected"
            old_source = root / "old-helper.exe"
            old_source.write_bytes(b"old-helper")
            old_digest = hid_elevation_windows._sha256(old_source)
            old_target = owner_root / hid_elevation_windows.helper_relative_path(
                old_digest,
                generation=max(1, hid_elevation_windows.HELPER_GENERATION - 1),
            )
            old_target.parent.mkdir(parents=True)
            old_target.write_bytes(old_source.read_bytes())
            active_source = root / "active-helper.exe"
            active_source.write_bytes(b"active-helper")
            active_digest = hid_elevation_windows._sha256(active_source)
            active_target = owner_root / hid_elevation_windows.helper_relative_path(
                active_digest
            )
            active_target.parent.mkdir(parents=True)
            active_target.write_bytes(active_source.read_bytes())
            manifest_path = owner_root / hid_elevation_windows.MANIFEST_FILENAME
            manifest_path.write_text("{broken", encoding="utf-8")
            task_name = hid_elevation_windows.task_name_for_sid(SID)
            tasks = {task_name: self._task_snapshot(active_target)}
            removal_order = []
            real_remove = hid_elevation_windows._remove_helper_generation

            def read_task(name):
                if name == hid_elevation_windows.LEGACY_TASK_NAME:
                    return None
                return tasks.get(name)

            def delete_task(name, *, missing_ok):
                self.assertFalse(missing_ok)
                tasks.pop(name, None)

            def remove_target(path, *, owner_root):
                removal_order.append(Path(path))
                real_remove(Path(path), owner_root=owner_root)

            with mock.patch.object(
                hid_elevation_windows, "is_process_elevated", return_value=True
            ), mock.patch.object(
                hid_elevation_windows, "current_user_sid", return_value=SID
            ), mock.patch.object(
                hid_elevation_windows, "_read_registered_task", side_effect=read_task
            ), mock.patch.object(
                hid_elevation_windows, "_delete_task", side_effect=delete_task
            ), mock.patch.object(
                hid_elevation_windows,
                "_remove_helper_generation",
                side_effect=remove_target,
            ):
                hid_elevation_windows.uninstall_task(
                    request_sid=SID,
                    owner_root=owner_root,
                    program_files_root=root,
                    _read_acl=self._acl,
                )

            self.assertEqual(removal_order, [old_target, active_target])
            self.assertNotIn(task_name, tasks)
            self.assertFalse(manifest_path.exists())

    def test_task_query_failure_after_delete_restores_task_and_manifest(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            owner_root = root / "protected"
            source = root / "source-helper.exe"
            source.write_bytes(b"helper")
            digest = hid_elevation_windows._sha256(source)
            manifest = hid_elevation_windows._build_manifest(SID, digest)
            target = owner_root / Path(manifest.helper_relative_path)
            target.parent.mkdir(parents=True)
            target.write_bytes(source.read_bytes())
            manifest_path = owner_root / hid_elevation_windows.MANIFEST_FILENAME
            manifest_path.write_text(json.dumps(manifest.as_dict()), encoding="utf-8")
            manifest_bytes = manifest_path.read_bytes()
            task_name = manifest.task_name
            task = self._task_snapshot(target)
            tasks = {task_name: task}
            task_reads = 0
            remove_target = mock.Mock()

            def read_task(name):
                nonlocal task_reads
                if name == hid_elevation_windows.LEGACY_TASK_NAME:
                    return None
                task_reads += 1
                if task_reads == 2:
                    raise OSError("simulated task query failure")
                return tasks.get(name)

            def delete_task(name, *, missing_ok):
                self.assertFalse(missing_ok)
                tasks.pop(name, None)

            def register_task(name, xml, sddl):
                tasks[name] = hid_elevation_windows._RegisteredTaskSnapshot(
                    xml, sddl
                )

            with mock.patch.object(
                hid_elevation_windows, "is_process_elevated", return_value=True
            ), mock.patch.object(
                hid_elevation_windows, "current_user_sid", return_value=SID
            ), mock.patch.object(
                hid_elevation_windows, "_apply_path_security"
            ), mock.patch.object(
                hid_elevation_windows, "_read_registered_task", side_effect=read_task
            ), mock.patch.object(
                hid_elevation_windows, "_delete_task", side_effect=delete_task
            ), mock.patch.object(
                hid_elevation_windows, "_register_task", side_effect=register_task
            ), mock.patch.object(
                hid_elevation_windows,
                "_remove_helper_generation",
                remove_target,
            ):
                with self.assertRaises(hid_elevation_windows.HidElevationError):
                    hid_elevation_windows.uninstall_task(
                        request_sid=SID,
                        owner_root=owner_root,
                        program_files_root=root,
                        _read_acl=self._acl,
                    )

            self.assertEqual(tasks.get(task_name), task)
            self.assertEqual(manifest_path.read_bytes(), manifest_bytes)
            self.assertTrue(target.is_file())
            remove_target.assert_not_called()

    def test_uninstall_keeps_helper_when_task_removal_fails(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            owner_root = root / "protected"
            owner_root.mkdir()
            digest = hid_elevation_windows._sha256(Path(__file__))
            manifest = hid_elevation_windows._build_manifest(SID, digest)
            target = owner_root / Path(manifest.helper_relative_path)
            target.parent.mkdir(parents=True)
            target.write_bytes(Path(__file__).read_bytes())
            manifest_path = owner_root / hid_elevation_windows.MANIFEST_FILENAME
            manifest_path.write_text(
                __import__("json").dumps(manifest.as_dict()), encoding="utf-8"
            )
            task = hid_elevation_windows._RegisteredTaskSnapshot(
                hid_elevation_windows.task_definition_xml(
                    target, SID, task_name=manifest.task_name
                ),
                hid_elevation_windows.task_security_sddl(SID),
            )
            with mock.patch.object(
                hid_elevation_windows, "is_process_elevated", return_value=True
            ), mock.patch.object(
                hid_elevation_windows, "current_user_sid", return_value=SID
            ), mock.patch.object(
                hid_elevation_windows, "_apply_path_security"
            ), mock.patch.object(
                hid_elevation_windows,
                "_read_registered_task",
                side_effect=lambda name: (
                    None
                    if name == hid_elevation_windows.LEGACY_TASK_NAME
                    else task
                ),
            ), mock.patch.object(
                hid_elevation_windows,
                "_delete_task",
                side_effect=hid_elevation_windows.HidElevationError(
                    "hid_helper_task_removal_failed"
                ),
            ):
                with self.assertRaises(hid_elevation_windows.HidElevationError):
                    hid_elevation_windows.uninstall_task(
                        request_sid=SID,
                        owner_root=owner_root,
                        program_files_root=root,
                        _read_acl=self._acl,
                    )

            self.assertTrue(target.is_file())
            self.assertTrue(manifest_path.is_file())

    def test_uninstall_keeps_permission_disabled_when_helper_cleanup_fails(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            owner_root = root / "protected"
            owner_root.mkdir()
            source = root / "source-helper.exe"
            source.write_bytes(b"helper")
            digest = hid_elevation_windows._sha256(source)
            manifest = hid_elevation_windows._build_manifest(SID, digest)
            target = owner_root / Path(manifest.helper_relative_path)
            target.parent.mkdir(parents=True)
            target.write_bytes(source.read_bytes())
            manifest_path = owner_root / hid_elevation_windows.MANIFEST_FILENAME
            manifest_path.write_text(
                __import__("json").dumps(manifest.as_dict()), encoding="utf-8"
            )
            task = hid_elevation_windows._RegisteredTaskSnapshot(
                hid_elevation_windows.task_definition_xml(
                    target, SID, task_name=manifest.task_name
                ),
                hid_elevation_windows.task_security_sddl(SID),
            )
            tasks = {manifest.task_name: task}

            def read_task(name):
                if name == hid_elevation_windows.LEGACY_TASK_NAME:
                    return None
                return tasks.get(name)

            def delete_task(name, *, missing_ok):
                self.assertFalse(missing_ok)
                tasks.pop(name, None)

            def register_task(name, xml, sddl):
                tasks[name] = hid_elevation_windows._RegisteredTaskSnapshot(
                    xml, sddl
                )

            with mock.patch.object(
                hid_elevation_windows, "is_process_elevated", return_value=True
            ), mock.patch.object(
                hid_elevation_windows, "current_user_sid", return_value=SID
            ), mock.patch.object(
                hid_elevation_windows, "_apply_path_security"
            ), mock.patch.object(
                hid_elevation_windows,
                "_read_registered_task",
                side_effect=read_task,
            ), mock.patch.object(
                hid_elevation_windows, "_delete_task", side_effect=delete_task
            ), mock.patch.object(
                hid_elevation_windows, "_register_task", side_effect=register_task
            ), mock.patch.object(
                hid_elevation_windows,
                "_remove_helper_generation",
                side_effect=hid_elevation_windows.HidElevationError(
                    "protected_helper_removal_failed"
                ),
            ):
                with self.assertRaises(
                    hid_elevation_windows.HidElevationError
                ) as ctx:
                    hid_elevation_windows.uninstall_task(
                        request_sid=SID,
                        owner_root=owner_root,
                        program_files_root=root,
                        _read_acl=self._acl,
                    )

            self.assertEqual(str(ctx.exception), "protected_helper_cleanup_pending")
            self.assertTrue(target.exists())
            self.assertFalse(manifest_path.exists())
            self.assertNotIn(manifest.task_name, tasks)

    def test_uninstall_succeeds_when_an_injected_runtime_cache_remains(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            owner_root = root / "protected"
            owner_root.mkdir()
            digest = hid_elevation_windows._sha256(Path(__file__))
            manifest = hid_elevation_windows._build_manifest(SID, digest)
            target = owner_root / Path(manifest.helper_relative_path)
            target.parent.mkdir(parents=True)
            target.write_bytes(Path(__file__).read_bytes())
            manifest_path = owner_root / hid_elevation_windows.MANIFEST_FILENAME
            manifest_path.write_text(
                __import__("json").dumps(manifest.as_dict()), encoding="utf-8"
            )
            runtime_cache = owner_root / "runtime" / "loaded-gadget"
            runtime_cache.mkdir(parents=True)
            cached_dll = runtime_cache / "remote-mic-rc003-gadget.dll"
            cached_dll.write_bytes(b"still loaded by WUDFHost")
            task = hid_elevation_windows._RegisteredTaskSnapshot(
                hid_elevation_windows.task_definition_xml(
                    target, SID, task_name=manifest.task_name
                ),
                hid_elevation_windows.task_security_sddl(SID),
            )
            tasks = {manifest.task_name: task}

            def read_task(name):
                if name == hid_elevation_windows.LEGACY_TASK_NAME:
                    return None
                return tasks.get(name)

            def delete_task(name, *, missing_ok):
                self.assertFalse(missing_ok)
                tasks.pop(name, None)

            with mock.patch.object(
                hid_elevation_windows, "is_process_elevated", return_value=True
            ), mock.patch.object(
                hid_elevation_windows, "current_user_sid", return_value=SID
            ), mock.patch.object(
                hid_elevation_windows, "_read_registered_task", side_effect=read_task
            ), mock.patch.object(
                hid_elevation_windows, "_delete_task", side_effect=delete_task
            ):
                hid_elevation_windows.uninstall_task(
                    request_sid=SID,
                    owner_root=owner_root,
                    program_files_root=root,
                    _read_acl=self._acl,
                )

            self.assertNotIn(manifest.task_name, tasks)
            self.assertFalse(manifest_path.exists())
            self.assertFalse(target.exists())
            self.assertTrue(cached_dll.exists())

    def test_registered_injector_rejects_changed_host_before_running_task(self):
        runner = mock.Mock()
        with self.assertRaises(hid_elevation_windows.HidElevationError) as ctx:
            hid_elevation_windows.run_registered_injector(
                2468,
                _run_registered_task=runner,
                _host_pid=lambda: 9999,
            )
        self.assertEqual(str(ctx.exception), "hid_helper_host_changed")
        runner.assert_not_called()

    def test_registered_injector_runs_only_the_sid_derived_task(self):
        runner = mock.Mock()
        with mock.patch.object(
            hid_elevation_windows,
            "inspect_installed_helper",
            return_value=hid_elevation_windows.HidHelperState(True),
        ), mock.patch.object(
            hid_elevation_windows, "current_user_sid", return_value=SID
        ):
            hid_elevation_windows.run_registered_injector(
                2468,
                _run_registered_task=runner,
                _host_pid=lambda: 2468,
            )
        runner.assert_called_once_with(hid_elevation_windows.task_name_for_sid(SID))


class RemovalInspectionTests(unittest.TestCase):
    def test_runtime_cache_alone_does_not_request_an_unnecessary_uac(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            owner_root = hid_elevation_windows.protected_owner_root(
                SID, program_files_root=root
            )
            runtime_cache = owner_root / "runtime" / "loaded-gadget"
            runtime_cache.mkdir(parents=True)
            (runtime_cache / "remote-mic-rc003-gadget.dll").write_bytes(
                b"still loaded"
            )

            requires_removal = (
                hid_elevation_windows._installation_requires_elevated_removal(
                    SID,
                    program_files_root=root,
                    _read_task=lambda _name: None,
                )
            )

        self.assertFalse(requires_removal)

    def test_orphaned_generation_helper_still_requests_elevated_cleanup(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            owner_root = hid_elevation_windows.protected_owner_root(
                SID, program_files_root=root
            )
            target = owner_root / hid_elevation_windows.helper_relative_path(
                "a" * 64
            )
            target.parent.mkdir(parents=True)
            target.write_bytes(b"orphan")

            requires_removal = (
                hid_elevation_windows._installation_requires_elevated_removal(
                    SID,
                    program_files_root=root,
                    _read_task=lambda _name: None,
                )
            )

        self.assertTrue(requires_removal)

    def test_legacy_helper_without_its_task_still_requests_cleanup(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = hid_elevation_windows.legacy_protected_helper_path(
                program_files_root=root
            )
            target.parent.mkdir(parents=True)
            target.write_bytes(b"legacy-helper")

            requires_removal = (
                hid_elevation_windows._installation_requires_elevated_removal(
                    SID,
                    program_files_root=root,
                    _read_task=lambda _name: None,
                )
            )

        self.assertTrue(requires_removal)

    def test_inert_manifest_alone_does_not_request_uac(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            owner_root = hid_elevation_windows.protected_owner_root(
                SID, program_files_root=root
            )
            owner_root.mkdir(parents=True)
            (owner_root / hid_elevation_windows.MANIFEST_FILENAME).write_text(
                "{stale", encoding="utf-8"
            )

            requires_removal = (
                hid_elevation_windows._installation_requires_elevated_removal(
                    SID,
                    program_files_root=root,
                    _read_task=lambda _name: None,
                )
            )

        self.assertFalse(requires_removal)


class ElevationRequestTests(unittest.TestCase):
    def test_uac_cancellation_is_reported_without_claiming_success(self):
        with tempfile.TemporaryDirectory() as raw:
            helper = Path(raw) / hid_elevation_windows.HELPER_EXE_NAME
            helper.write_bytes(b"helper")

            def cancel(_path, _args, _timeout):
                raise hid_elevation_windows.HidElevationError("uac_cancelled")

            state = hid_elevation_windows.request_install_elevation(
                helper_path=helper,
                _launch=cancel,
                _inspect=lambda: hid_elevation_windows.HidHelperState(
                    False, "helper_manifest_missing"
                ),
                _can_self_elevate=lambda: True,
                _current_sid=lambda: SID,
            )

        self.assertEqual(state, hid_elevation_windows.HidHelperState(False, "uac_cancelled"))

    def test_success_is_rechecked_after_the_elevated_process_exits(self):
        with tempfile.TemporaryDirectory() as raw:
            helper = Path(raw) / hid_elevation_windows.HELPER_EXE_NAME
            helper.write_bytes(b"helper")
            inspected = hid_elevation_windows.HidHelperState(True)
            launch = mock.Mock(return_value=hid_elevation_windows.HELPER_EXIT_OK)
            inspect = mock.Mock(
                side_effect=[
                    hid_elevation_windows.HidHelperState(
                        False, "helper_manifest_missing"
                    ),
                    inspected,
                ]
            )

            state = hid_elevation_windows.request_install_elevation(
                helper_path=helper,
                timeout_seconds=12.0,
                _launch=launch,
                _inspect=inspect,
                _can_self_elevate=lambda: True,
                _current_sid=lambda: SID,
            )

        self.assertEqual(state, inspected)
        launch.assert_called_once_with(
            helper,
            f"{hid_elevation_windows.INSTALL_FLAG} {hid_elevation_windows.REQUEST_SID_FLAG} {SID}",
            12.0,
        )
        self.assertEqual(inspect.call_count, 2)

    def test_cleanup_failure_keeps_the_verified_helper_available(self):
        with tempfile.TemporaryDirectory() as raw:
            helper = Path(raw) / hid_elevation_windows.HELPER_EXE_NAME
            helper.write_bytes(b"helper")
            inspect = mock.Mock(
                side_effect=[
                    hid_elevation_windows.HidHelperState(
                        False, "protected_helper_outdated"
                    ),
                    hid_elevation_windows.HidHelperState(True),
                ]
            )

            state = hid_elevation_windows.request_install_elevation(
                helper_path=helper,
                _launch=lambda _path, _args, _timeout: (
                    hid_elevation_windows.HELPER_EXIT_VALIDATION_FAILED
                ),
                _inspect=inspect,
                _can_self_elevate=lambda: True,
                _current_sid=lambda: SID,
            )

        self.assertEqual(
            state,
            hid_elevation_windows.HidHelperState(
                True, "helper_cleanup_pending"
            ),
        )

    def test_install_reports_a_preserved_future_helper(self):
        with tempfile.TemporaryDirectory() as raw:
            helper = Path(raw) / hid_elevation_windows.HELPER_EXE_NAME
            helper.write_bytes(b"helper")

            state = hid_elevation_windows.request_install_elevation(
                helper_path=helper,
                _launch=lambda _path, _args, _timeout: (
                    hid_elevation_windows.HELPER_EXIT_NEWER_PRESERVED
                ),
                _inspect=lambda: hid_elevation_windows.HidHelperState(
                    False, "helper_manifest_invalid"
                ),
                _can_self_elevate=lambda: True,
                _current_sid=lambda: SID,
            )

        self.assertEqual(
            state,
            hid_elevation_windows.HidHelperState(
                False, "newer_helper_preserved"
            ),
        )

    def test_matching_helper_returns_without_uac(self):
        with tempfile.TemporaryDirectory() as raw:
            helper = Path(raw) / hid_elevation_windows.HELPER_EXE_NAME
            helper.write_bytes(b"helper")
            ready = hid_elevation_windows.HidHelperState(True)
            launch = mock.Mock()

            state = hid_elevation_windows.request_install_elevation(
                helper_path=helper,
                _launch=launch,
                _inspect=lambda: ready,
                _can_self_elevate=lambda: True,
                _current_sid=lambda: SID,
            )

        self.assertEqual(state, ready)
        launch.assert_not_called()

    def test_invalid_newer_helper_returns_without_uac(self):
        with tempfile.TemporaryDirectory() as raw:
            helper = Path(raw) / hid_elevation_windows.HELPER_EXE_NAME
            helper.write_bytes(b"helper")
            newer = hid_elevation_windows.HidHelperState(
                False, "newer_helper_invalid"
            )
            launch = mock.Mock()

            state = hid_elevation_windows.request_install_elevation(
                helper_path=helper,
                _launch=launch,
                _inspect=lambda: newer,
                _can_self_elevate=lambda: True,
                _current_sid=lambda: SID,
            )

        self.assertEqual(state, newer)
        launch.assert_not_called()

    def test_standard_account_is_rejected_before_uac(self):
        with tempfile.TemporaryDirectory() as raw:
            helper = Path(raw) / hid_elevation_windows.HELPER_EXE_NAME
            helper.write_bytes(b"helper")
            launch = mock.Mock()

            state = hid_elevation_windows.request_install_elevation(
                helper_path=helper,
                _launch=launch,
                _inspect=lambda: hid_elevation_windows.HidHelperState(
                    False, "helper_manifest_missing"
                ),
                _can_self_elevate=lambda: False,
                _current_sid=lambda: SID,
            )

        self.assertEqual(
            state.detail, "current_account_cannot_self_elevate"
        )
        launch.assert_not_called()

    def test_uninstall_uses_the_bundled_helper_and_fixed_flag(self):
        with tempfile.TemporaryDirectory() as raw:
            helper = Path(raw) / hid_elevation_windows.HELPER_EXE_NAME
            helper.write_bytes(b"helper")
            launch = mock.Mock(return_value=hid_elevation_windows.HELPER_EXIT_OK)
            requires_removal = mock.Mock(side_effect=[True, False])

            state = hid_elevation_windows.request_uninstall_elevation(
                helper_path=helper,
                timeout_seconds=9.0,
                _launch=launch,
                _can_self_elevate=lambda: True,
                _current_sid=lambda: SID,
                _requires_removal=requires_removal,
            )

        self.assertEqual(state, hid_elevation_windows.HidHelperState(True))
        launch.assert_called_once_with(
            helper,
            f"{hid_elevation_windows.UNINSTALL_FLAG} {hid_elevation_windows.REQUEST_SID_FLAG} {SID}",
            9.0,
        )

    def test_uninstall_uac_cancellation_is_reported_without_claiming_success(self):
        with tempfile.TemporaryDirectory() as raw:
            helper = Path(raw) / hid_elevation_windows.HELPER_EXE_NAME
            helper.write_bytes(b"helper")

            def cancel(_path, _args, _timeout):
                raise hid_elevation_windows.HidElevationError("uac_cancelled")

            state = hid_elevation_windows.request_uninstall_elevation(
                helper_path=helper,
                _launch=cancel,
                _can_self_elevate=lambda: True,
                _current_sid=lambda: SID,
                _requires_removal=lambda _sid: True,
            )

        self.assertEqual(
            state,
            hid_elevation_windows.HidHelperState(False, "uac_cancelled"),
        )

    def test_uninstall_preserves_a_known_future_helper_without_uac(self):
        with tempfile.TemporaryDirectory() as raw:
            helper = Path(raw) / hid_elevation_windows.HELPER_EXE_NAME
            helper.write_bytes(b"helper")
            launch = mock.Mock()

            state = hid_elevation_windows.request_uninstall_elevation(
                helper_path=helper,
                _launch=launch,
                _current_sid=lambda: SID,
                _inspect=lambda: hid_elevation_windows.HidHelperState(
                    True, "helper_available_newer_generation"
                ),
                _requires_removal=lambda _sid: True,
            )

        self.assertEqual(
            state,
            hid_elevation_windows.HidHelperState(
                True, "helper_preserved_newer_contract"
            ),
        )
        launch.assert_not_called()

    def test_uninstall_maps_the_future_preserved_exit_to_success(self):
        with tempfile.TemporaryDirectory() as raw:
            helper = Path(raw) / hid_elevation_windows.HELPER_EXE_NAME
            helper.write_bytes(b"helper")

            state = hid_elevation_windows.request_uninstall_elevation(
                helper_path=helper,
                _launch=lambda _path, _args, _timeout: (
                    hid_elevation_windows.HELPER_EXIT_NEWER_PRESERVED
                ),
                _can_self_elevate=lambda: True,
                _current_sid=lambda: SID,
                _inspect=lambda: hid_elevation_windows.HidHelperState(
                    False, "helper_manifest_invalid"
                ),
                _requires_removal=lambda _sid: True,
            )

        self.assertEqual(
            state,
            hid_elevation_windows.HidHelperState(
                True, "helper_preserved_newer_contract"
            ),
        )

    def test_standard_account_without_a_helper_uninstalls_without_uac(self):
        with tempfile.TemporaryDirectory() as raw:
            helper = Path(raw) / hid_elevation_windows.HELPER_EXE_NAME
            helper.write_bytes(b"helper")
            launch = mock.Mock()

            state = hid_elevation_windows.request_uninstall_elevation(
                helper_path=helper,
                _launch=launch,
                _can_self_elevate=lambda: False,
                _current_sid=lambda: SID,
                _requires_removal=lambda _sid: False,
            )

        self.assertEqual(state, hid_elevation_windows.HidHelperState(True))
        launch.assert_not_called()

    def test_standard_account_with_a_helper_is_rejected_before_uac(self):
        with tempfile.TemporaryDirectory() as raw:
            helper = Path(raw) / hid_elevation_windows.HELPER_EXE_NAME
            helper.write_bytes(b"helper")
            launch = mock.Mock()

            state = hid_elevation_windows.request_uninstall_elevation(
                helper_path=helper,
                _launch=launch,
                _can_self_elevate=lambda: False,
                _current_sid=lambda: SID,
                _requires_removal=lambda _sid: True,
            )

        self.assertEqual(state.detail, "current_account_cannot_self_elevate")
        launch.assert_not_called()


class HelperMainTests(unittest.TestCase):
    def test_fixed_frozen_helper_uses_a_new_generation(self):
        self.assertGreaterEqual(hid_elevation_windows.HELPER_GENERATION, 4)

    def test_self_check_explicitly_imports_the_operation_lock_dependency(self):
        source = __import__("inspect").getsource(hid_elevation_windows._self_check)
        self.assertIn("single_instance", source)

    def test_self_check_has_no_privileged_or_hid_side_effects(self):
        with mock.patch.object(hid_elevation_windows, "install_task") as install, mock.patch.object(
            hid_elevation_windows, "uninstall_task"
        ) as uninstall, mock.patch.object(
            hid_elevation_windows, "_inject_once"
        ) as inject, mock.patch.object(
            hid_elevation_windows, "_self_check"
        ) as self_check:
            result = hid_elevation_windows.helper_main(
                [hid_elevation_windows.SELF_CHECK_FLAG]
            )

        self.assertEqual(result, hid_elevation_windows.HELPER_EXIT_OK)
        install.assert_not_called()
        uninstall.assert_not_called()
        inject.assert_not_called()
        self_check.assert_called_once_with()

    def test_install_passes_the_pre_elevation_sid_to_the_helper(self):
        with mock.patch.object(hid_elevation_windows, "install_task") as install:
            result = hid_elevation_windows.helper_main(
                [
                    hid_elevation_windows.INSTALL_FLAG,
                    hid_elevation_windows.REQUEST_SID_FLAG,
                    SID,
                ]
            )

        self.assertEqual(result, hid_elevation_windows.HELPER_EXIT_OK)
        install.assert_called_once_with(request_sid=SID)

    def test_future_helper_preservation_has_a_distinct_exit_code(self):
        for flag, operation_name in (
            (hid_elevation_windows.INSTALL_FLAG, "install_task"),
            (hid_elevation_windows.UNINSTALL_FLAG, "uninstall_task"),
        ):
            with self.subTest(flag=flag), mock.patch.object(
                hid_elevation_windows,
                operation_name,
                side_effect=hid_elevation_windows.HidElevationError(
                    "newer_helper_preserved"
                ),
            ):
                result = hid_elevation_windows.helper_main(
                    [flag, hid_elevation_windows.REQUEST_SID_FLAG, SID]
                )

            self.assertEqual(
                result, hid_elevation_windows.HELPER_EXIT_NEWER_PRESERVED
            )


class HelperEntryPointTests(unittest.TestCase):
    def test_inject_mode_resolves_target_internally(self):
        with mock.patch.object(hid_elevation_windows, "_inject_once") as inject:
            self.assertEqual(
                hid_elevation_windows.helper_main([hid_elevation_windows.INJECT_FLAG]),
                hid_elevation_windows.HELPER_EXIT_OK,
            )
        inject.assert_called_once_with()

    def test_inject_mode_rejects_pid_or_executable_arguments(self):
        with self.assertRaises(SystemExit):
            hid_elevation_windows.helper_main(
                [hid_elevation_windows.INJECT_FLAG, "--pid", "2468"]
            )


if __name__ == "__main__":
    unittest.main()
