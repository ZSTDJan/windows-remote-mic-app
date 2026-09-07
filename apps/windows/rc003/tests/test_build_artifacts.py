"""Static checks over the build/installer/CI sources themselves - not a
Windows build, just structural/contract validation that runs anywhere.
"""

import ast
import importlib.util
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ovb_rc003 import config, logging_setup

_RC003_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT = _RC003_ROOT.parents[2]
_SPEC_PATH = _RC003_ROOT / "build" / "RemoteMicRC003.spec"
_VERSION_PATH = _RC003_ROOT / "src" / "ovb_rc003" / "VERSION"
_REQUIREMENTS_PATH = _RC003_ROOT / "requirements.txt"
_ISS_PATH = _RC003_ROOT / "installer" / "RemoteMicRC003Setup.iss"
_CI_PATH = _REPO_ROOT / ".github" / "workflows" / "windows-rc003-ci.yml"
_PACKAGE_MAIN_PATH = _RC003_ROOT / "src" / "ovb_rc003" / "__main__.py"
_LAUNCHER_PATH = _RC003_ROOT / "src" / "launcher.py"
_HID_HELPER_LAUNCHER_PATH = _RC003_ROOT / "src" / "hid_helper_launcher.py"
_BUILD_CANDIDATE_PATH = _RC003_ROOT / "build" / "build-candidate.ps1"
_BUILD_PROVENANCE_PATH = _RC003_ROOT / "build" / "build-provenance.ps1"
_PACKAGE_LOCAL_TEST_PATH = _RC003_ROOT / "build" / "package-local-test.ps1"
_RUN_DEV_PATH = _RC003_ROOT / "build" / "run-dev.ps1"
_STOP_DEV_PATH = _RC003_ROOT / "build" / "stop-dev.ps1"
_INSTALL_DEV_SHORTCUT_PATH = _RC003_ROOT / "build" / "install-dev-shortcut.ps1"
_PUBLIC_BOUNDARY_PATH = _RC003_ROOT / "build" / "check-public-boundary.ps1"
_README_PATH = _RC003_ROOT / "README.md"
_INSTALLED_README_PATH = _RC003_ROOT / "installer" / "readme-rc003.txt"
_PORTABLE_README_PATH = (
    _RC003_ROOT / "installer" / "readme-portable-rc003.txt"
)
_ROOT_README_PATH = _REPO_ROOT / "README.md"
_THIRD_PARTY_NOTICES_PATH = _REPO_ROOT / "THIRD_PARTY_NOTICES.md"


def _exec_as_top_level_no_package(path: Path, *, module_name: str) -> None:
    """Reproduces exactly the failure mode PyInstaller's bootloader creates
    for its ``Analysis()`` entry script: the script's own source is
    executed as a top-level module with no ``__package__`` at all,
    regardless of where the file lives on disk (XRBM-021 red baseline, see
    the XRBM-021 task book). A relative import (``from . import X``) inside
    that source then raises ``ImportError: attempted relative import with
    no known parent package`` - independent of what ``__name__`` happens to
    be, since Python's relative-import resolution keys off ``__package__``,
    not ``__name__``.

    ``module_name`` deliberately avoids the literal string ``"__main__"``
    so this stays side-effect-free even against a script that guards its
    own execution with ``if __name__ == "__main__":`` - the import-time
    failure this test targets fires (or doesn't) before any such guard
    could ever run, so a real duplicate of PyInstaller's own ``__name__ ==
    "__main__"`` is not needed to reproduce or verify the fix.
    """

    source = path.read_text(encoding="utf-8")
    code = compile(source, str(path), "exec")
    namespace = {"__name__": module_name, "__package__": None, "__file__": str(path)}
    exec(code, namespace)


def _strip_hash_comments(text: str) -> str:
    """Drop '#'-comment lines (Python/.spec files) before scanning for
    forbidden *directives* - this file's own comments legitimately explain,
    in prose, which things are deliberately excluded, and that explanation
    should not itself trip a "must not mention X" check.
    """

    return "\n".join(
        line for line in text.splitlines() if not line.strip().startswith("#")
    )


def _strip_semicolon_comments(text: str) -> str:
    """Same idea as _strip_hash_comments, for Inno Setup's ';' comments."""

    return "\n".join(
        line for line in text.splitlines() if not line.strip().startswith(";")
    )


def _iss_section(text: str, name: str) -> str:
    """Extracts one Inno Setup ``[Name]`` section's body.

    A naive ``text.split("[Files]")`` is unsafe here: several of this
    script's own prose comments legitimately mention another section by
    its bracketed name (e.g. "...which [UninstallRun] and the Stop
    shortcut..."), which would silently split on that comment occurrence
    instead of the real section header. This only matches an actual
    section header - a line containing nothing but ``[Name]``.
    """

    match = re.search(
        rf"(?m)^\[{re.escape(name)}\][ \t]*\r?$\n(.*?)(?=^\[[A-Za-z]+\][ \t]*\r?$|\Z)",
        text,
        re.DOTALL,
    )
    assert match is not None, f"[{name}] section header not found"
    return match.group(1)


_WINRT_PIN_RE = re.compile(r"^winrt-Windows\.([A-Za-z0-9.]+)==3\.2\.1$")


def _winrt_requirement_modules(text: str) -> set:
    """Maps every exact ``winrt-Windows.<Namespace>==3.2.1`` pin in
    requirements.txt to the ``winrt.windows.<namespace>`` module name it
    installs (e.g. ``winrt-Windows.Foundation.Collections==3.2.1`` ->
    ``winrt.windows.foundation.collections``). Deliberately excludes
    ``winrt-runtime`` (not a ``winrt.windows.*`` namespace import).
    """

    modules = set()
    for line in text.splitlines():
        match = _WINRT_PIN_RE.match(line.strip())
        if match:
            modules.add("winrt.windows." + match.group(1).lower())
    return modules


def _spec_hidden_import_winrt_modules(text: str) -> set:
    tree = ast.parse(text, filename=str(_SPEC_PATH))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "hiddenimports"
            for target in node.targets
        ):
            values = ast.literal_eval(node.value)
            return {value for value in values if value.startswith("winrt.")}
    raise AssertionError("hiddenimports assignment not found in spec")


class WinRTDependencyClosureContractTests(unittest.TestCase):
    """XRBM-024: deterministic, cross-platform proof that every exact
    ``winrt-Windows.*==3.2.1`` pin in requirements.txt has a matching
    PyInstaller hidden import, and vice versa - so the source-test/
    frozen-build dependency closure this task fixed (Foundation and
    Foundation.Collections missing from both) cannot silently drift apart
    again for any winrt-Windows.* projection, present or future.
    """

    def setUp(self):
        self.requirements_text = _REQUIREMENTS_PATH.read_text(encoding="utf-8")
        self.spec_text = _SPEC_PATH.read_text(encoding="utf-8")

    def test_foundation_and_foundation_collections_are_pinned_at_exact_3_2_1(self):
        self.assertIn("winrt-Windows.Foundation==3.2.1", self.requirements_text)
        self.assertIn(
            "winrt-Windows.Foundation.Collections==3.2.1", self.requirements_text
        )

    def test_requirements_never_pulls_the_broad_all_extra(self):
        # XRBM-024 in-scope item 1: exact base-package pins only, never the
        # "[all]" extra, which would pull in every unrelated namespace those
        # packages' own "all" extras reference (ApplicationModel.Background,
        # Security.Credentials, UI, UI.Popups, Storage, System, Networking,
        # Radios, Rfcomm, ...). Checked against effective (non-comment)
        # content, since this file's own comment legitimately explains the
        # "not [all]" rule in prose.
        self.assertNotIn("[all]", _strip_hash_comments(self.requirements_text))

    def test_spec_declares_hidden_imports_for_foundation_and_foundation_collections(self):
        modules = _spec_hidden_import_winrt_modules(self.spec_text)
        self.assertIn("winrt.windows.foundation", modules)
        self.assertIn("winrt.windows.foundation.collections", modules)

    def test_every_pinned_winrt_windows_package_has_a_matching_hidden_import(self):
        pinned_modules = _winrt_requirement_modules(self.requirements_text)
        hidden_import_modules = _spec_hidden_import_winrt_modules(self.spec_text)
        self.assertTrue(
            pinned_modules, "no winrt-Windows.*==3.2.1 pins found in requirements.txt"
        )
        missing_hidden_imports = pinned_modules - hidden_import_modules
        self.assertEqual(
            missing_hidden_imports,
            set(),
            "requirements.txt pins a winrt-Windows.* projection with no "
            "matching PyInstaller hidden import - the frozen build would "
            "pass source tests and then crash at runtime: "
            f"{sorted(missing_hidden_imports)}",
        )

    def test_every_winrt_hidden_import_has_a_matching_requirements_pin(self):
        pinned_modules = _winrt_requirement_modules(self.requirements_text)
        hidden_import_modules = _spec_hidden_import_winrt_modules(self.spec_text)
        missing_pins = hidden_import_modules - pinned_modules
        self.assertEqual(
            missing_pins,
            set(),
            "PyInstaller hidden-imports a winrt.windows.* module with no "
            "matching requirements.txt pin - the frozen build's runtime "
            "closure is undocumented/unpinned: "
            f"{sorted(missing_pins)}",
        )


class PyInstallerSpecTests(unittest.TestCase):
    def test_spec_is_valid_python(self):
        text = _SPEC_PATH.read_text(encoding="utf-8")
        ast.parse(text, filename=str(_SPEC_PATH))  # raises SyntaxError on failure

    def test_spec_requires_and_bundles_the_shared_version_file(self):
        text = _strip_hash_comments(_SPEC_PATH.read_text(encoding="utf-8"))
        self.assertTrue(_VERSION_PATH.is_file())
        self.assertRegex(
            _VERSION_PATH.read_text(encoding="ascii").strip(),
            r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$",
        )
        self.assertIn('VERSION_FILE = SRC_ROOT / "ovb_rc003" / "VERSION"', text)
        self.assertIn('datas.append((str(VERSION_FILE), "ovb_rc003"))', text)

    def test_spec_excludes_other_device_bridges(self):
        text = _SPEC_PATH.read_text(encoding="utf-8")
        self.assertIn("bridges.t1", text)
        self.assertIn("bridges.hanvon", text)

    def test_spec_requires_and_reverifies_the_pinned_frida_archive(self):
        # The archive is never stored in source control, but every frozen
        # build must fail closed unless the explicit fetch step supplied the
        # runtime module's exact pinned filename and hash.
        text = _strip_hash_comments(_SPEC_PATH.read_text(encoding="utf-8")).lower()
        self.assertIn("frida_assets", text)
        self.assertIn("gadget_archive_name", text)
        self.assertIn("gadget_archive_sha256", text)
        self.assertIn("sha256_file", text)
        self.assertIn("raise systemexit", text)
        self.assertNotIn('glob("*.xz")', text)
        self.assertNotIn("frida-gadget-17.15.3-windows-x86_64.dll.xz", text)

    def test_spec_builds_a_self_contained_narrow_hid_helper(self):
        text = _strip_hash_comments(_SPEC_PATH.read_text(encoding="utf-8"))
        helper_analysis = text[
            text.index("helper_a = Analysis(") : text.index("helper_pyz = PYZ(")
        ]
        self.assertTrue(_HID_HELPER_LAUNCHER_PATH.is_file())
        self.assertIn('SRC_ROOT / "hid_helper_launcher.py"', text)
        self.assertIn('HID_HELPER_NAME = "RemoteMicRC003HidHelper"', text)
        self.assertIn(
            'HID_HELPER_RELATIVE_PATH = Path("_internal")',
            text,
        )
        self.assertIn("helper_a = Analysis(", text)
        self.assertIn("helper_pyz = PYZ(", text)
        self.assertIn("helper_exe = EXE(", text)
        self.assertIn("helper_a.binaries", text)
        self.assertIn("helper_a.datas", text)
        self.assertIn('(str(VERSION_FILE), "ovb_rc003")', helper_analysis)
        self.assertIn('"ovb_rc003/frida_assets"', helper_analysis)
        self.assertIn('"ovb_rc003.hid_elevation_windows"', helper_analysis)
        self.assertIn('"ovb_rc003.frida_hid_tap_injector"', helper_analysis)
        self.assertIn('"comtypes"', helper_analysis)
        self.assertIn('"comtypes.client"', helper_analysis)
        self.assertIn('"PySide6"', helper_analysis)
        self.assertIn('"ovb_rc003.qt_settings_app"', helper_analysis)
        self.assertIn(
            '(str(HID_HELPER_RELATIVE_PATH), helper_exe.name, "EXECUTABLE")',
            text,
        )
        self.assertIn("*helper_exe.dependencies", text)
        self.assertNotIn("\n    helper_exe,\n", text)

    def test_main_and_helper_manifests_do_not_require_administrator(self):
        text = _strip_hash_comments(_SPEC_PATH.read_text(encoding="utf-8"))
        self.assertGreaterEqual(text.count("uac_admin=False"), 2)
        self.assertNotIn("uac_admin=True", text)

    def test_spec_requires_and_bundles_the_remote_photo(self):
        text = _strip_hash_comments(_SPEC_PATH.read_text(encoding="utf-8"))
        self.assertIn('REMOTE_PHOTO = REPO_ROOT / "Resources"', text)
        self.assertIn('if not REMOTE_PHOTO.is_file():', text)
        self.assertIn("raise SystemExit", text)
        self.assertIn(
            'datas.append((str(REMOTE_PHOTO), "Resources"))',
            text,
        )
        self.assertNotIn("if REMOTE_PHOTO.is_file():", text)

    def test_spec_bundles_the_verified_vb_cable_zip_as_data_not_a_binary_dependency(self):
        # XRBM-031: unlike Frida, the pinned VB-CABLE base package IS now
        # bundled - but only as opaque `datas` (an ordinary file PyInstaller
        # copies verbatim), never as a `binaries`/hiddenimports entry that
        # would imply this project links against or executes vendor code at
        # build time. build/fetch-vb-cable.ps1 (a required gate in both
        # build-candidate.ps1 and windows-rc003-ci.yml, run BEFORE this spec)
        # is what actually places the verified file on disk; this spec stays
        # defensive (only bundles it if present), matching the existing
        # photo/qml datas entries.
        text = _strip_hash_comments(_SPEC_PATH.read_text(encoding="utf-8"))
        self.assertIn("VBCABLE_Driver_Pack45.zip", text)
        self.assertIn('"vb_cable_bundle"', text)
        self.assertIn("build", text)
        self.assertIn("third_party", text)
        self.assertIn('"ovb_rc003.vb_cable_bundle"', text)
        self.assertIn('"ovb_rc003.windows_diagnostics"', text)

    def test_spec_analyzes_the_standalone_launcher_not_the_package_main(self):
        # XRBM-021: the spec's Analysis() entry script must be
        # src/launcher.py - analyzing src/ovb_rc003/__main__.py directly
        # reproduces the red baseline's relative-import failure the moment
        # the frozen executable runs (see LauncherEntryPointTests below).
        # Checked against effective (non-comment) content, since this
        # file's own comment legitimately explains the "not __main__.py"
        # rule in prose.
        text = _strip_hash_comments(_SPEC_PATH.read_text(encoding="utf-8"))
        self.assertIn('SRC_ROOT / "launcher.py"', text)
        self.assertNotIn('SRC_ROOT / "ovb_rc003" / "__main__.py"', text)

    def test_spec_bundles_the_shared_device_profile_directory_as_data(self):
        text = _SPEC_PATH.read_text(encoding="utf-8")
        self.assertIn('DEVICE_PROFILES_DIR = REPO_ROOT / "device-profiles"', text)
        self.assertIn(
            'datas.append((str(DEVICE_PROFILES_DIR), "device-profiles"))', text
        )

    def test_spec_bundles_the_element_navigation_companion_sources(self):
        text = _SPEC_PATH.read_text(encoding="utf-8")
        for source_name in (
            "element_navigation_prototype.py",
            "element_navigation_command_windows.py",
            "element_navigation_support.py",
            "element_navigation_windows_host.py",
            "element_targeting_core.py",
            "spatial_navigation_core.py",
        ):
            self.assertIn(source_name, text)
        self.assertIn('datas.append((str(source_path), "element_navigation"))', text)
        hiddenimports = _spec_hidden_imports(text)
        for module in (
            "ovb_rc003.element_navigation_control_windows",
            "ovb_rc003.element_navigation_runtime",
            "PySide6.QtWidgets",
        ):
            self.assertIn(module, hiddenimports)


class LauncherEntryPointTests(unittest.TestCase):
    """XRBM-021 In-scope item 1/5: structural regression coverage for the
    red baseline recorded in the XRBM-021 task book - an isolated
    PyInstaller 6.21.0 build of the PRE-FIX spec completed, but running the
    produced executable's `--dry-run` exited 1 with "ImportError: attempted
    relative import with no known parent package" from `__main__.py`. These
    tests reproduce that exact failure mode (and its fix) deterministically
    on any platform, without needing an actual PyInstaller build for every
    test run - the real isolated-environment build/`--dry-run` smoke test
    (see XRBM-021's implementation report) is the platform-level
    confirmation on top of this structural one.
    """

    def test_analyzing_the_package_main_directly_reproduces_the_red_failure(self):
        with self.assertRaises(ImportError) as ctx:
            _exec_as_top_level_no_package(
                _PACKAGE_MAIN_PATH, module_name="frozen_entry_simulation"
            )
        self.assertIn("relative import", str(ctx.exception))

    def test_the_standalone_launcher_exists_outside_the_package(self):
        self.assertTrue(_LAUNCHER_PATH.is_file())
        # No parent package: launcher.py must NOT live inside src/ovb_rc003/.
        self.assertNotEqual(_LAUNCHER_PATH.parent.name, "ovb_rc003")

    def test_the_standalone_launcher_uses_only_an_absolute_import(self):
        text = _LAUNCHER_PATH.read_text(encoding="utf-8")
        self.assertIn("from ovb_rc003.__main__ import main", text)
        # No package-relative import token anywhere in the launcher itself.
        for line in text.splitlines():
            stripped = line.strip()
            self.assertFalse(stripped.startswith("from ."))

    def test_the_standalone_launcher_avoids_the_red_failure(self):
        # Exercises the SAME no-parent-package execution context that broke
        # __main__.py above - the launcher's only import is absolute, so it
        # must succeed here too (ovb_rc003 is importable via this test
        # suite's own PYTHONPATH=src convention).
        _exec_as_top_level_no_package(
            _LAUNCHER_PATH, module_name="frozen_entry_simulation"
        )  # must not raise

    def test_the_standalone_launcher_still_guards_its_own_execution(self):
        # Structural check that launcher.py only calls main() when actually
        # run as a script (mirroring __main__.py's own guard) - it must not
        # call main() merely by being imported/analyzed.
        text = _LAUNCHER_PATH.read_text(encoding="utf-8")
        self.assertIn('if __name__ == "__main__":', text)


class HidHelperLauncherTests(unittest.TestCase):
    def _load_launcher(self):
        spec = importlib.util.spec_from_file_location(
            "test_hid_helper_launcher",
            _HID_HELPER_LAUNCHER_PATH,
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_import_failure_returns_stable_exit_code(self):
        launcher = self._load_launcher()
        with mock.patch.object(
            launcher,
            "_load_helper_main",
            side_effect=ModuleNotFoundError("missing frozen dependency"),
        ):
            self.assertEqual(launcher.main(), 5)

    def test_unexpected_helper_failure_returns_stable_exit_code(self):
        launcher = self._load_launcher()
        with mock.patch.object(
            launcher,
            "_load_helper_main",
            return_value=mock.Mock(side_effect=RuntimeError("boom")),
        ):
            self.assertEqual(launcher.main(), 5)

    def test_helper_specific_nonzero_exit_code_is_preserved(self):
        launcher = self._load_launcher()
        with mock.patch.object(
            launcher,
            "_load_helper_main",
            return_value=mock.Mock(return_value=12),
        ):
            self.assertEqual(launcher.main(), 12)

    def test_elevated_launcher_never_writes_user_controlled_paths(self):
        text = _HID_HELPER_LAUNCHER_PATH.read_text(encoding="utf-8")
        self.assertNotIn("LOCALAPPDATA", text)
        self.assertNotIn("write_parent_hid_helper_event", text)

        helper_source = (
            _RC003_ROOT / "src" / "ovb_rc003" / "hid_elevation_windows.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("write_parent_hid_helper_event", helper_source)


class InnoSetupScriptTests(unittest.TestCase):
    def setUp(self):
        self.text = _ISS_PATH.read_text(encoding="utf-8")
        self.effective_text = _strip_semicolon_comments(self.text)

    def test_privileges_required_is_lowest(self):
        self.assertIn("PrivilegesRequired=lowest", self.text)

    def test_install_and_uninstall_reject_an_elevated_outer_process(self):
        code_section = _iss_section(self.text, "Code")
        setup = code_section.split(
            "function InitializeSetup(): Boolean;", 1
        )[1].split("procedure DeinitializeSetup", 1)[0]
        uninstall = code_section.split(
            "function InitializeUninstall(): Boolean;", 1
        )[1].split("procedure DeinitializeUninstall", 1)[0]

        for branch in (setup, uninstall):
            self.assertIn("if ShellIsUserAnAdmin() then", branch)
            self.assertIn("Result := False", branch)
        self.assertIn("不能以管理员身份运行卸载程序", uninstall)

    def test_installer_maintenance_mutex_is_visible_across_sessions(self):
        code_section = _iss_section(self.text, "Code")
        self.assertIn(
            "InstallerMaintenanceMutexName = "
            "'Global\\RemoteMicRC003_InstallerMaintenance'",
            code_section,
        )
        self.assertIn(
            "CreateMutex(\n    0,\n    False,\n    InstallerMaintenanceMutexName",
            code_section,
        )

    def test_installer_delegates_hid_maintenance_to_the_normal_application(self):
        code_section = _iss_section(self.text, "Code")
        self.assertIn("ShellExec(", code_section)
        self.assertIn("'runas'", code_section)
        self.assertIn("RunApplicationMaintenance", code_section)
        self.assertIn("'--install-hid-helper'", code_section)
        self.assertIn("'--uninstall-hid-helper'", code_section)
        self.assertNotIn("RunHidHelper", code_section)
        self.assertNotIn("'--install-task'", code_section)
        self.assertNotIn("'--uninstall-task'", code_section)
        self.assertIn("StopNeedsElevationExitCode = 10", code_section)
        self.assertIn("StopUnsafeToContinueExitCode = 20", code_section)
        self.assertIn("StopProbeFailedExitCode = 21", code_section)
        self.assertIn("StopUserActionRequiredExitCode = 22", code_section)
        self.assertIn("RunStopApplication(StopScript, True", code_section)
        self.assertIn("-ElevatedRetry", code_section)
        self.assertNotIn("--pid", code_section)
        self.assertNotIn("PrivilegesRequired=admin", self.effective_text)

    def test_installer_hides_the_internal_helper_and_removes_the_old_root_copy(self):
        code_section = _iss_section(self.text, "Code")
        self.assertIn("ExpandConstant('{app}\\{#AppExeName}')", code_section)
        self.assertIn(
            "UpgradeLegacyHelperPath := ExpandConstant('{app}\\{#HidHelperExeName}')",
            code_section,
        )
        self.assertIn("UpgradeHeldLegacyHelperPath", code_section)
        self.assertNotIn("[InstallDelete]", self.text)

    def test_install_failure_keeps_a_concise_in_app_retry_path(self):
        code_section = _iss_section(self.text, "Code")
        self.assertIn("CurStepChanged", code_section)
        self.assertIn("ssPostInstall", code_section)
        self.assertIn("管理员按键组件未启用", code_section)
        self.assertIn("修复权限", code_section)
        self.assertIn("WizardForm.FinishedLabel.Caption", code_section)

    def test_standard_account_gets_one_direct_message_without_a_useless_uac_prompt(self):
        code_section = _iss_section(self.text, "Code")
        self.assertIn("HidHelperAccountUnsupportedExitCode = 25", code_section)
        branch = code_section.split(
            "if ResultCode = HidHelperAccountUnsupportedExitCode then", 1
        )[1].split("else", 1)[0]
        self.assertIn("当前 Windows 账号不是管理员", branch)
        self.assertIn("临时输入另一个管理员账号无效", branch)
        self.assertNotIn("ShellExec", branch)

    def test_uninstall_blocks_before_file_removal_if_helper_cleanup_fails(self):
        code_section = _iss_section(self.text, "Code")
        initialize = code_section.split(
            "function InitializeUninstall(): Boolean", 1
        )[1].split("procedure CurUninstallStepChanged", 1)[0]
        self.assertIn("RunApplicationMaintenance('--uninstall-hid-helper'", initialize)
        self.assertIn("Result := False", initialize)
        self.assertIn("管理员按键组件未能移除", initialize)
        unsupported = initialize.split(
            "if ResultCode = HidHelperAccountUnsupportedExitCode then", 1
        )[1].split("else", 1)[0]
        self.assertIn("请登录原管理员账号后重试", unsupported)
        self.assertIn("临时输入另一个管理员账号无效", unsupported)

    def test_no_autostart_shortcut_or_task(self):
        self.assertNotIn("userstartup", self.effective_text.lower())
        self.assertNotIn("Tasks: \"startup\"", self.effective_text)

    def test_installer_uses_the_shared_application_icon(self):
        self.assertIn(
            r"SetupIconFile=..\src\ovb_rc003\assets\icons\remote-mic.ico",
            self.text,
        )

    def test_installer_bundles_the_current_user_readme(self):
        files_section = _iss_section(self.text, "Files")
        self.assertIn(
            'Source: "readme-rc003.txt"; DestDir: "{app}"; '
            "Flags: isreadme ignoreversion",
            files_section,
        )

    def test_no_vbcable_reference(self):
        # This installer SCRIPT's own directives never mention VB-CABLE at
        # all (checked against the effective, comment-stripped text) - it
        # never installs/configures/removes it, during install or
        # uninstall. The header COMMENT block (not checked by this
        # assertion) legitimately documents, in prose, that the bundled
        # APPLICATION carries it as optional data - see
        # test_header_comment_discloses_bundled_driver_helper_honestly
        # below for that.
        lower = self.effective_text.lower()
        self.assertNotIn("vbcable", lower)
        self.assertNotIn("vb-cable", lower)

    def test_header_comment_discloses_bundled_driver_helper_honestly(self):
        # XRBM-031 RETRY 1 item 5: the header comment previously claimed
        # "No VB-CABLE ... is installed, configured, or referenced" - false,
        # since the frozen application it packages DOES bundle the official
        # VB-CABLE package as data and can launch its setup UI from the
        # diagnostics page. The corrected comment must instead state
        # precisely what stays true (this installer script itself never
        # runs/removes the driver, never elevates) without denying the
        # bundled/launchable reality.
        self.assertIn("vb-cable", self.text.lower())
        self.assertIn("检查与修复", self.text)
        self.assertIn("UAC", self.text)
        self.assertNotIn(
            "no vb-cable or any other driver package is installed, configured, or",
            self.text.lower(),
        )

    def test_install_dir_uses_open_voice_bridge_namespace(self):
        self.assertIn(r"{localappdata}\RemoteMic\{#AppFolder}", self.text)

    def test_app_name_has_no_forbidden_branding(self):
        self.assertNotIn("2655", self.text)
        self.assertNotIn("T1RemoteBridge", self.text)
        self.assertNotIn("V60PenBridge", self.text)

    def test_uninstall_initialization_stops_the_app_before_removing_files(self):
        code_section = _iss_section(self.text, "Code")
        self.assertIn("function InitializeUninstall(): Boolean", code_section)
        self.assertIn("stop-app.ps1", code_section)
        self.assertIn("Result := False", code_section)
        self.assertNotIn("[UninstallRun]", self.text)

    def test_upgrade_aborts_when_the_old_process_cannot_be_confirmed_stopped(self):
        code_section = _iss_section(self.text, "Code")
        stop_helper = code_section.split(
            "function StopApplicationForInstall(const StopScript: String;", 1
        )[1].split("function RunApplicationMaintenance", 1)[0]
        prepare = code_section.split(
            "function PrepareToInstall(var NeedsRestart: Boolean): String;", 1
        )[1].split("procedure CurStepChanged", 1)[0]

        self.assertIn("Started := RunStopApplication(StopScript, False", stop_helper)
        self.assertIn("if not Started then", stop_helper)
        self.assertIn("if ResultCode = 0 then", stop_helper)
        self.assertIn("Result := False", stop_helper)
        self.assertEqual(prepare.count("if not StopApplicationForInstall"), 2)
        self.assertIn("安装没有覆盖任何程序文件", code_section)
        self.assertIn("旧版仍需您确认退出", code_section)
        self.assertIn("请处理旧版窗口并完全退出后重试", code_section)

    def test_upgrade_quarantines_and_recovers_the_complete_old_runtime(self):
        code_section = _iss_section(self.text, "Code")
        prepare = code_section.split(
            "function PrepareToInstall(var NeedsRestart: Boolean): String;", 1
        )[1].split("procedure CurStepChanged", 1)[0]
        post_install = code_section.split(
            "procedure CurStepChanged(CurStep: TSetupStep);", 1
        )[1].split("procedure CurPageChanged", 1)[0]

        self.assertIn("QuarantineExistingApplication", prepare)
        self.assertIn(
            "RenameFile(UpgradeApplicationPath, UpgradeHeldApplicationPath)",
            code_section,
        )
        self.assertIn(
            "RenameFile(UpgradeInternalPath, UpgradeHeldInternalPath)",
            code_section,
        )
        self.assertIn("RestoreHeldRuntime", code_section)
        self.assertIn("RecoverUpgradeRuntimeQuarantine", code_section)
        first_stop = prepare.index("if not StopApplicationForInstall")
        quarantine = prepare.index("if not QuarantineExistingApplication")
        second_stop = prepare.index("if not StopApplicationForInstall", first_stop + 1)
        self.assertLess(
            first_stop,
            second_stop,
        )
        self.assertLess(second_stop, quarantine)
        self.assertNotIn("RecoverUpgradeRuntimeQuarantine", prepare[:quarantine])
        self.assertIn("ValidateInstalledApplication", post_install)
        self.assertIn("CommitUpgradeRuntimeQuarantine", post_install)
        self.assertIn("InstallFilesCompleted := True", post_install)

    def test_committed_upgrade_is_only_cleaned_and_never_rolled_back(self):
        code_section = _iss_section(self.text, "Code")
        normalize = code_section.split(
            "function NormalizePreviousUpgradeRuntime", 1
        )[1].split("function QuarantineExistingApplication", 1)[0]
        committed = normalize.split(
            "if RuntimeState = UpgradeStateCommitted then", 1
        )[1].split(
            "if RuntimeState = UpgradeStateRestoring then",
            1,
        )[0]
        recover = code_section.split(
            "function RecoverUpgradeRuntimeQuarantine", 1
        )[1].split("procedure DeleteObsoleteShortcuts", 1)[0]
        recover_committed = recover.split(
            "if RuntimeState = UpgradeStateCommitted then", 1
        )[1].split("if not DirExists(UpgradeRuntimeHoldPath) then", 1)[0]

        self.assertIn("DeleteDirectoryWithRetries(UpgradeRuntimeHoldPath)", committed)
        self.assertIn("DeleteUpgradeRuntimeStateAfterCleanup", committed)
        self.assertNotIn("RestoreHeldRuntime", committed)
        self.assertIn("FinishUpgradeRuntimeQuarantine", recover_committed)
        self.assertNotIn("RestoreHeldRuntime", recover_committed)

    def test_unknown_upgrade_state_repairs_only_from_verified_runtime_evidence(self):
        code_section = _iss_section(self.text, "Code")
        normalize = code_section.split(
            "function NormalizePreviousUpgradeRuntime", 1
        )[1].split("function QuarantineExistingApplication", 1)[0]
        unknown_normalize = normalize.split(
            "if UpgradeRuntimeQuarantined then", 1
        )[1].split(
            "Result := DeleteDirectoryWithRetries(UpgradeRuntimeHoldPath)", 1
        )[0]
        recover = code_section.split(
            "function RecoverUpgradeRuntimeQuarantine", 1
        )[1].split("procedure DeleteObsoleteShortcuts", 1)[0]
        exact_recovery = recover.split(
            "if RuntimeState = UpgradeStatePreparing then", 1
        )[1]

        current_validation = unknown_normalize.index(
            "ValidateInstalledApplication(ValidationError)"
        )
        committed_state = unknown_normalize.index(
            "WriteUpgradeRuntimeState(UpgradeStateCommitted)"
        )
        held_complete = unknown_normalize.index("if HeldRuntimeIsComplete() then")
        restore = unknown_normalize.index("RestoreHeldRuntime(True)")
        preserve = unknown_normalize.index("Result := False", restore)
        self.assertLess(current_validation, committed_state)
        self.assertLess(committed_state, held_complete)
        self.assertLess(held_complete, restore)
        self.assertLess(restore, preserve)
        self.assertIn("本次未改动文件", unknown_normalize)
        self.assertIn("RuntimeState = UpgradeStateQuarantined", exact_recovery)
        self.assertIn("else\n    Result := False", exact_recovery)

    def test_full_restore_checks_the_backup_before_deleting_the_current_runtime(self):
        code_section = _iss_section(self.text, "Code")
        restore = code_section.split(
            "function RestoreHeldRuntime(RemoveAllCurrent: Boolean): Boolean;", 1
        )[1].split("function ValidateInstalledApplication", 1)[0]

        complete_check = restore.index(
            "if not HeldRuntimeIsComplete() then"
        )
        delete_current = restore.index(
            "if not RemoveCurrentRuntimePayload() then"
        )
        self.assertLess(complete_check, delete_current)
        self.assertIn("current runtime was preserved", restore)

    def test_full_restore_is_resumable_after_only_one_old_component_moves_back(self):
        code_section = _iss_section(self.text, "Code")
        restore = code_section.split(
            "function RestoreHeldRuntime(RemoveAllCurrent: Boolean): Boolean;", 1
        )[1].split("function ValidateInstalledApplication", 1)[0]
        normalize = code_section.split(
            "function NormalizePreviousUpgradeRuntime", 1
        )[1].split("function QuarantineExistingApplication", 1)[0]
        recover = code_section.split(
            "function RecoverUpgradeRuntimeQuarantine", 1
        )[1].split("procedure DeleteObsoleteShortcuts", 1)[0]

        self.assertIn("UpgradeStateRestorePending = 'restore-pending'", code_section)
        self.assertIn("UpgradeStateRestoring = 'restoring'", code_section)
        self.assertIn("RuntimeState <> UpgradeStateRestorePending", restore)
        self.assertIn("RestoringRuntimeIsComplete()", restore)
        self.assertLess(
            restore.index("WriteUpgradeRuntimeState(UpgradeStateRestorePending)"),
            restore.index("RemoveCurrentRuntimePayload()"),
        )
        self.assertLess(
            restore.index("RemoveCurrentRuntimePayload()"),
            restore.index("WriteUpgradeRuntimeState(UpgradeStateRestoring)"),
        )
        self.assertIn("if DirExists(UpgradeHeldInternalPath) then", restore)
        self.assertIn("if FileExists(UpgradeHeldApplicationPath) then", restore)
        self.assertIn("RuntimeState = UpgradeStateRestorePending", normalize)
        self.assertIn("RuntimeState = UpgradeStateRestoring", normalize)
        self.assertIn("RuntimeState = UpgradeStateRestorePending", recover)
        self.assertIn("RuntimeState = UpgradeStateRestoring", recover)

    def test_missing_old_backup_cannot_be_reported_as_a_successful_full_restore(self):
        code_section = _iss_section(self.text, "Code")
        restore = code_section.split(
            "function RestoreHeldRuntime(RemoveAllCurrent: Boolean): Boolean;", 1
        )[1].split("function ValidateInstalledApplication", 1)[0]
        missing_backup = restore.split(
            "if not DirExists(UpgradeRuntimeHoldPath) then", 1
        )[1].split("if RemoveAllCurrent and", 1)[0]

        self.assertIn("if RemoveAllCurrent then", missing_backup)
        self.assertIn("RuntimeState = UpgradeStateRestoring", missing_backup)
        self.assertIn("RuntimeState = UpgradeStatePreparing", missing_backup)
        self.assertIn("CurrentRuntimeIsComplete()", missing_backup)
        self.assertNotIn("Result := True", missing_backup)

    def test_missing_backup_state_is_not_unconditionally_normalized(self):
        code_section = _iss_section(self.text, "Code")
        normalize = code_section.split(
            "function NormalizePreviousUpgradeRuntime", 1
        )[1].split("function QuarantineExistingApplication", 1)[0]
        missing_backup = normalize.split(
            "if not DirExists(UpgradeRuntimeHoldPath) then", 1
        )[1].split("UpgradeRuntimeQuarantined := HeldRuntimeExists()", 1)[0]

        self.assertIn("RuntimeState := ReadUpgradeRuntimeState()", normalize)
        self.assertIn("RuntimeState = UpgradeStateCommitted", missing_backup)
        self.assertIn("RuntimeState = UpgradeStatePreparing", missing_backup)
        self.assertIn("RuntimeState = UpgradeStateRestoring", missing_backup)
        self.assertIn("CurrentRuntimeIsComplete()", missing_backup)
        self.assertIn("Result := False", missing_backup)
        self.assertNotIn("RuntimeState = UpgradeStateQuarantined", missing_backup)

    def test_empty_backup_directory_cannot_hide_an_incomplete_recovery(self):
        code_section = _iss_section(self.text, "Code")
        recover = code_section.split(
            "function RecoverUpgradeRuntimeQuarantine", 1
        )[1].split("procedure DeleteObsoleteShortcuts", 1)[0]
        empty_backup = recover.split(
            "if not UpgradeRuntimeQuarantined then", 1
        )[1].split("if (RuntimeState = UpgradeStateRestorePending)", 1)[0]

        self.assertIn("RuntimeState = UpgradeStatePreparing", empty_backup)
        self.assertIn("RuntimeState = UpgradeStateRestoring", empty_backup)
        self.assertIn("CurrentRuntimeIsComplete()", empty_backup)
        self.assertIn("DeleteDirectoryWithRetries(UpgradeRuntimeHoldPath)", empty_backup)
        self.assertNotIn("RuntimeState = UpgradeStateRestorePending", empty_backup)
        self.assertNotIn("RuntimeState = UpgradeStateQuarantined", empty_backup)

    def test_validation_failure_restores_known_backup_before_any_recovery_reread(self):
        code_section = _iss_section(self.text, "Code")
        failure = code_section.split(
            "procedure RecordInstallValidationFailure", 1
        )[1].split("procedure CurStepChanged", 1)[0]

        previous_branch = failure.split("if InstallHadPreviousRuntime then", 1)[1].split(
            "else", 1
        )[0]
        fresh_install_branch = failure.split("else", 1)[1]
        self.assertIn("RestoreHeldRuntime(True)", previous_branch)
        self.assertNotIn("RemoveCurrentRuntimePayload", previous_branch)
        self.assertNotIn("RecoverUpgradeRuntimeQuarantine", previous_branch)
        self.assertIn("RemoveCurrentRuntimePayload", fresh_install_branch)

    def test_failed_recovery_keeps_runtime_barriers_until_setup_deinitializes(self):
        code_section = _iss_section(self.text, "Code")
        post_install = code_section.split(
            "procedure CurStepChanged(CurStep: TSetupStep);", 1
        )[1].split("procedure CurPageChanged", 1)[0]
        deinitialize = code_section.split(
            "procedure DeinitializeSetup();", 1
        )[1].split("function RunStopApplication", 1)[0]

        self.assertIn(
            "if (not InstallValidationFailed) or InstallRecoverySucceeded then",
            post_install,
        )
        self.assertIn("ReleaseLegacyRuntimeBarriers", deinitialize)
        self.assertIn("ReleaseInstallerMaintenanceMutex", deinitialize)

    def test_setup_deinitialization_only_repairs_files_after_mutation_started(self):
        code_section = _iss_section(self.text, "Code")
        prepare = code_section.split(
            "function PrepareToInstall(var NeedsRestart: Boolean): String;", 1
        )[1].split("procedure RecordInstallValidationFailure", 1)[0]
        deinitialize = code_section.split(
            "procedure DeinitializeSetup();", 1
        )[1].split("function RunStopApplication", 1)[0]

        mutation = prepare.index("UpgradeRuntimeMutationStarted := True")
        quarantine = prepare.index("if not QuarantineExistingApplication")
        second_stop = prepare.index(
            "if not StopApplicationForInstall",
            prepare.index("if not StopApplicationForInstall") + 1,
        )
        self.assertLess(second_stop, mutation)
        self.assertLess(mutation, quarantine)
        self.assertIn("if UpgradeRuntimeMutationStarted then", deinitialize)
        self.assertNotIn("RecoverUpgradeRuntimeQuarantine", deinitialize.split(
            "if UpgradeRuntimeMutationStarted then", 1
        )[0])

    def test_upgrade_state_is_outside_the_backup_and_removed_only_after_cleanup(self):
        code_section = _iss_section(self.text, "Code")
        paths = code_section.split("procedure InitializeUpgradeRuntimePaths", 1)[1].split(
            "function CurrentRuntimeExists", 1
        )[0]
        cleanup = code_section.split(
            "function DeleteUpgradeRuntimeStateAfterCleanup", 1
        )[1].split("function RemoveCurrentRuntimePayload", 1)[0]
        commit = code_section.split(
            "function CommitUpgradeRuntimeQuarantine", 1
        )[1].split("procedure FinishUpgradeRuntimeQuarantine", 1)[0]

        self.assertIn(
            "UpgradeRuntimeHoldPath := ExpandConstant('{app}\\.installing-previous')",
            paths,
        )
        self.assertIn(
            "UpgradeStatePath := ExpandConstant('{app}\\.installing-previous.state')",
            paths,
        )
        self.assertIn("if DirExists(UpgradeRuntimeHoldPath) then", cleanup)
        self.assertLess(
            cleanup.index("if DirExists(UpgradeRuntimeHoldPath) then"),
            cleanup.index("DeleteFileWithRetries(UpgradeStatePath)"),
        )
        self.assertLess(
            commit.index("WriteUpgradeRuntimeState(UpgradeStateCommitted)"),
            commit.index("DeleteDirectoryWithRetries(UpgradeRuntimeHoldPath)"),
        )
        self.assertLess(
            commit.index("DeleteDirectoryWithRetries(UpgradeRuntimeHoldPath)"),
            commit.index("DeleteUpgradeRuntimeStateAfterCleanup"),
        )

    def test_main_executable_is_installed_last_and_verified_before_success(self):
        files_section = _iss_section(self.text, "Files")
        code_section = _iss_section(self.text, "Code")
        post_install = code_section.split(
            "procedure CurStepChanged(CurStep: TSetupStep);", 1
        )[1].split("procedure CurPageChanged", 1)[0]

        wildcard = next(
            line
            for line in files_section.splitlines()
            if 'Source: "{#DistDir}\\*"' in line
        )
        main_executable = next(
            line
            for line in files_section.splitlines()
            if 'Source: "{#DistDir}\\{#AppExeName}"' in line
        )
        self.assertIn('Excludes: "{#AppExeName}"', wildcard)
        self.assertEqual(files_section.strip().splitlines()[-1], main_executable)
        verification = code_section.split(
            "function ValidateInstalledApplication", 1
        )[1].split("function NormalizeLegacyExecutableHold", 1)[0]
        self.assertIn("if not FileExists(UpgradeApplicationPath) then", verification)
        self.assertIn("if not DirExists(UpgradeInternalPath) then", verification)
        self.assertIn("'{#HidHelperExeName}'", verification)
        self.assertIn("'--dry-run'", verification)
        self.assertIn("ResultCode <> 0", verification)
        self.assertLess(
            post_install.index("if not ValidateInstalledApplication"),
            post_install.index("InstallFilesCompleted := True"),
        )
        self.assertIn("Check: ShouldLaunchInstalledApplication", self.text)
        self.assertIn("function GetCustomSetupExitCode", code_section)
        self.assertIn("InstallValidationFailureExitCode", code_section)

    def test_upgrade_blocks_legacy_restarts_until_file_replacement_finishes(self):
        code_section = _iss_section(self.text, "Code")
        prepare = code_section.split(
            "function PrepareToInstall(var NeedsRestart: Boolean): String;", 1
        )[1].split("procedure CurStepChanged", 1)[0]
        post_install = code_section.split(
            "procedure CurStepChanged(CurStep: TSetupStep);", 1
        )[1].split("procedure CurPageChanged", 1)[0]
        deinitialize = code_section.split("procedure DeinitializeSetup();", 1)[1].split(
            "function RunStopApplication", 1
        )[0]

        for name in (
            "Local\\RemoteMicRC003_SettingsInstance",
            "Local\\RemoteMicRC003_BridgeInstance",
            "Local\\RemoteMicRC003_ApplicationHandoff",
        ):
            self.assertIn(name, code_section)
        barriers = code_section.split(
            "function AcquireLegacyRuntimeBarriers", 1
        )[1].split("procedure InitializeUpgradeRuntimePaths", 1)[0]
        self.assertEqual(barriers.count("CreateMutex(0, False"), 3)
        first_stop = prepare.index("if not StopApplicationForInstall")
        barrier = prepare.index("if not AcquireLegacyRuntimeBarriers")
        second_stop = prepare.index("if not StopApplicationForInstall", first_stop + 1)
        quarantine = prepare.index("if not QuarantineExistingApplication")
        self.assertLess(first_stop, barrier)
        self.assertLess(barrier, second_stop)
        self.assertLess(second_stop, quarantine)
        self.assertIn("ReleaseLegacyRuntimeBarriers", post_install)
        self.assertIn("ReleaseLegacyRuntimeBarriers", deinitialize)

    def test_uninstall_stops_then_blocks_legacy_restarts_and_checks_again(self):
        code_section = _iss_section(self.text, "Code")
        uninstall = code_section.split(
            "function InitializeUninstall(): Boolean;", 1
        )[1].split("procedure DeinitializeUninstall", 1)[0]
        deinitialize = code_section.split(
            "procedure DeinitializeUninstall();", 1
        )[1].split("procedure RemoveOwnedLoginStartupValue", 1)[0]

        first_stop = uninstall.index("if not StopApplicationForUninstall")
        barrier = uninstall.index("if not AcquireLegacyRuntimeBarriers")
        second_stop = uninstall.index(
            "if not StopApplicationForUninstall", first_stop + 1
        )
        helper_cleanup = uninstall.index(
            "RunApplicationMaintenance('--uninstall-hid-helper'"
        )
        self.assertLess(first_stop, barrier)
        self.assertLess(barrier, second_stop)
        self.assertLess(second_stop, helper_cleanup)
        self.assertIn("ReleaseLegacyRuntimeBarriers", deinitialize)
        self.assertIn("ReleaseInstallerMaintenanceMutex", deinitialize)

    def test_quarantined_runtime_cleanup_retries_and_uninstall_is_exact(self):
        code_section = _iss_section(self.text, "Code")
        deinitialize = code_section.split("procedure DeinitializeSetup();", 1)[1].split(
            "function RunStopApplication", 1
        )[0]
        uninstall_delete = _strip_semicolon_comments(
            _iss_section(self.text, "UninstallDelete")
        )

        self.assertIn("function DeleteFileWithRetries", code_section)
        self.assertIn("function DeleteDirectoryWithRetries", code_section)
        self.assertIn("FileCleanupAttempts = 5", code_section)
        self.assertIn("FinishUpgradeRuntimeQuarantine", deinitialize)
        self.assertIn(
            'Type: filesandordirs; Name: "{app}\\.installing-previous"',
            uninstall_delete,
        )
        self.assertIn(
            'Type: files; Name: "{app}\\{#AppExeName}.installing-previous"',
            uninstall_delete,
        )
        for user_data in ("config.json", "key_bindings.json", "logs", "captures"):
            self.assertNotIn(user_data, uninstall_delete)

    def test_upgrade_requests_uac_only_for_the_dedicated_elevation_exit_code(self):
        code_section = _iss_section(self.text, "Code")
        stop_helper = code_section.split(
            "function StopApplicationForInstall(const StopScript: String;", 1
        )[1].split("function RunApplicationMaintenance", 1)[0]
        normal_call = stop_helper.index(
            "Started := RunStopApplication(StopScript, False, True, ResultCode);"
        )
        elevation_gate = stop_helper.index(
            "if ResultCode = StopNeedsElevationExitCode then"
        )
        elevated_call = stop_helper.index(
            "Started := RunStopApplication(StopScript, True, True, ResultCode);"
        )
        self.assertLess(normal_call, elevation_gate)
        self.assertLess(elevation_gate, elevated_call)
        self.assertIn("未完成 UAC 确认", stop_helper)
        self.assertIn("需要管理员权限关闭正在以管理员身份运行的旧版", stop_helper)

    def test_stop_script_uses_directory_boundary_and_bounded_exit_confirmation(self):
        script = (_ISS_PATH.parent / "stop-app.ps1").read_text(
            encoding="utf-8-sig"
        )
        self.assertIn("[System.IO.Path]::GetFullPath", script)
        self.assertIn("$maintenanceExitTimeoutSeconds = 50", script)
        self.assertIn("$legacyBridgeExitTimeoutSeconds = 10", script)
        self.assertIn("$currentSessionId", script)
        self.assertIn("$exitOtherSessionRunning = 24", script)
        self.assertIn("CreationDate", script)
        self.assertIn("Get-CurrentTargetProcess", script)
        self.assertIn('$targetExecutableName = "RemoteMicRC003.exe"', script)
        self.assertIn("$targetExecutablePath", script)
        self.assertIn("[switch]$BlockOtherLocations", script)
        self.assertIn("$exitOtherLocationRunning = 23", script)
        self.assertNotIn("Resolve-Path -LiteralPath $AppPath", script)
        self.assertIn("$exitUnsafeToContinue = 20", script)
        self.assertIn("[Console]::Error.WriteLine", script)
        self.assertIn("GetWindowThreadProcessId", script)
        self.assertIn("$postError -eq $errorAccessDenied", script)
        process_list_failure = script.split(
            'Write-StopError "$productName process list could not be read."', 1
        )[1].split("}", 1)[0]
        self.assertIn("exit $script:exitProbeFailed", process_list_failure)
        self.assertNotIn("exit $script:exitNeedsElevation", process_list_failure)
        self.assertNotIn("Write-Error", script)

    def test_stop_script_uses_full_exit_and_never_force_stops_a_legacy_shell(self):
        script = (_ISS_PATH.parent / "stop-app.ps1").read_text(
            encoding="utf-8-sig"
        )
        self.assertIn(
            '$v3ExitCapabilityProperty = "RemoteMicRC003.ApplicationExitRequestV3"',
            script,
        )
        self.assertIn(
            '$windowExitCapabilityProperty = '
            '"RemoteMicRC003.ApplicationExitWindowSignalV1"',
            script,
        )
        self.assertIn(
            '$windowExitRequestProperty = '
            '"RemoteMicRC003.ApplicationExitWindowRequestV1"',
            script,
        )
        self.assertIn("function Invoke-WindowFullExit", script)
        self.assertIn("TrySetProcessWindowProperty", script)
        self.assertIn("TryRemoveProcessWindowPropertyValue", script)
        window_exit = script.split("function Invoke-WindowFullExit", 1)[1].split(
            "function Invoke-LegacyV3FullExit", 1
        )[0]
        self.assertIn("finally", window_exit)
        self.assertIn("$script:windowExitRequestProperty", window_exit)
        self.assertIn("[IntPtr]$requestToken", window_exit)
        self.assertIn("function Invoke-LegacyV3FullExit", script)
        self.assertIn('-ArgumentList "--request-exit"', script)
        self.assertIn("-FilePath $Target.ExecutablePath", script)
        legacy_function = script.split("function Invoke-LegacyV3FullExit", 1)[1]
        elevation_guard = legacy_function.index("if ($script:isElevated)")
        external_launch = legacy_function.index("-FilePath $Target.ExecutablePath")
        self.assertLess(elevation_guard, external_launch)
        marker_gate = script.index("if ($windowSignalTargets.Count -eq 1)")
        legacy_bridge = script.index(
            "[RemoteMicInstaller.LegacyShellMethods]::PostMessage"
        )
        self.assertLess(marker_gate, legacy_bridge)
        self.assertIn("legacy bridge did not finish normal cleanup", script)
        self.assertIn("legacy bridge control window was not found", script)
        self.assertIn("legacy bridge restarted during shutdown confirmation", script)
        self.assertIn("Show-LegacyShellWindow", script)
        self.assertIn("FindWindowForProcessAndTitle", script)
        self.assertIn("exit $exitUserActionRequired", script)
        self.assertEqual(script.count("Stop-Process"), 1)
        self.assertIn("Stop-Process -Id $requestProcess.Id", script)
        self.assertNotIn("Stop-Process -Id $target.ProcessId", script)

    def test_stop_script_only_blocks_the_same_installed_path_in_other_sessions(self):
        script = (_ISS_PATH.parent / "stop-app.ps1").read_text(
            encoding="utf-8-sig"
        )
        other_session = script.split(
            "if ([uint32]$process.SessionId -ne $script:currentSessionId)", 1
        )[1].split("continue", 1)[0]

        self.assertIn("if ($matchesTargetPath)", other_session)
        self.assertNotIn("BlockOtherLocations", other_session)

    def test_install_blocks_a_running_portable_copy_but_uninstall_does_not_touch_it(self):
        code_section = _iss_section(self.text, "Code")
        prepare = code_section.split(
            "function PrepareToInstall(var NeedsRestart: Boolean): String;", 1
        )[1].split("procedure CurStepChanged", 1)[0]
        stop_helper = code_section.split(
            "function StopApplicationForInstall(const StopScript: String;", 1
        )[1].split("function StopApplicationForUninstall", 1)[0]
        uninstall_stop_helper = code_section.split(
            "function StopApplicationForUninstall(const StopScript: String): Boolean;",
            1,
        )[1].split("function RunApplicationMaintenance", 1)[0]
        uninstall = code_section.split("function InitializeUninstall(): Boolean;", 1)[1]

        self.assertIn(
            "RunStopApplication(StopScript, False, True, ResultCode)", stop_helper
        )
        self.assertIn(
            "RunStopApplication(StopScript, True, True, ResultCode)", stop_helper
        )
        self.assertEqual(prepare.count("StopApplicationForInstall"), 2)
        self.assertIn("StopOtherLocationRunningExitCode", stop_helper)
        self.assertIn("旧便携版", stop_helper)
        self.assertIn(
            "RunStopApplication(StopScript, False, False, ResultCode)",
            uninstall_stop_helper,
        )
        self.assertNotIn(
            "RunStopApplication(StopScript, True, False, ResultCode)",
            uninstall_stop_helper,
        )
        self.assertIn("正以管理员身份运行", uninstall_stop_helper)
        self.assertEqual(uninstall.count("StopApplicationForUninstall"), 2)

    def test_uninstall_never_elevates_the_user_writable_stop_script(self):
        code_section = _iss_section(self.text, "Code")
        uninstall_stop_helper = code_section.split(
            "function StopApplicationForUninstall(const StopScript: String): Boolean;",
            1,
        )[1].split("function RunApplicationMaintenance", 1)[0]

        self.assertIn(
            "RunStopApplication(StopScript, False, False, ResultCode)",
            uninstall_stop_helper,
        )
        self.assertNotIn("RunStopApplication(StopScript, True", uninstall_stop_helper)
        self.assertNotIn("ShellExec(", uninstall_stop_helper)

    def test_upgrade_marker_and_runtime_cleanup_do_not_delete_user_data(self):
        files_section = _iss_section(self.text, "Files")
        code_section = _iss_section(self.text, "Code")
        uninstall_delete = _strip_semicolon_comments(
            _iss_section(self.text, "UninstallDelete")
        )
        self.assertIn(
            'Source: "application-exit-contract-v1.json"; DestDir: "{app}"',
            files_section,
        )
        self.assertNotIn("[InstallDelete]", self.text)
        self.assertIn("UpgradeInternalPath", code_section)
        self.assertIn("UpgradeHeldInternalPath", code_section)
        for user_data in ("config.json", "key_bindings.json", "logs", "captures"):
            self.assertNotIn(user_data, uninstall_delete)

    def test_uninstall_aborts_when_the_installed_stop_script_is_missing(self):
        code_section = _iss_section(self.text, "Code")
        uninstall = code_section.split(
            "function InitializeUninstall(): Boolean;", 1
        )[1].split("procedure DeinitializeUninstall", 1)[0]
        self.assertIn("if not FileExists(StopScript) then", uninstall)
        missing_branch = uninstall.split(
            "if not FileExists(StopScript) then", 1
        )[1].split("if not StopApplicationForUninstall", 1)[0]
        self.assertIn("Result := False", missing_branch)
        self.assertIn("ReleaseInstallerMaintenanceMutex", missing_branch)

    def test_stop_app_script_is_both_temp_extractable_and_permanently_installed(self):
        # XRBM-022: the round-1 defect was that stop-app.ps1 only had a
        # "dontcopy" [Files] entry (extractable during PrepareToInstall) and
        # was never actually installed to {app} - so uninstall startup and any
        # Stop shortcut referencing "{app}\stop-app.ps1" pointed at a file
        # that never existed on disk after install. Both entries must exist.
        files_section = _iss_section(self.text, "Files")
        self.assertIn(
            'Source: "stop-app.ps1"; DestDir: "{app}"; Flags: ignoreversion',
            files_section,
        )
        self.assertIn(
            'Source: "stop-app.ps1"; DestDir: "{tmp}"; Flags: dontcopy',
            files_section,
        )

    def test_uninstall_and_a_stop_shortcut_both_target_the_installed_copy(self):
        # At least two independent references to the installed (not
        # temp-extracted) copy: InitializeUninstall and an explicit Stop shortcut.
        self.assertGreaterEqual(self.text.count(r"{app}\stop-app.ps1"), 2)

    def test_primary_start_menu_shortcut_opens_settings_not_bridge(self):
        self.assertIn(
            'Name: "{group}\\{#AppName}"; Filename: "{app}\\{#AppExeName}"',
            self.text,
        )
        primary_line = next(
            line
            for line in _iss_section(self.text, "Icons").splitlines()
            if 'Name: "{group}\\{#AppName}";' in line
        )
        self.assertNotIn("--bridge", primary_line)

    def test_desktop_shortcut_opens_settings_not_bridge(self):
        self.assertIn(
            'Name: "{userdesktop}\\{#AppName}"; Filename: "{app}\\{#AppExeName}"; '
            'Tasks: desktopicon',
            self.text,
        )

    def test_explicit_settings_stop_uninstall_shortcuts_all_exist(self):
        icons_section = _iss_section(self.text, "Icons")
        self.assertIn("设置", icons_section)
        self.assertIn("停止 {#AppName}", icons_section)
        self.assertIn("卸载 {#AppName}", icons_section)

    def test_installer_does_not_create_a_second_direct_bridge_shortcut(self):
        self.assertNotIn("启动 {#AppName}", _iss_section(self.text, "Icons"))

    def test_uninstall_removes_only_the_owned_login_startup_value(self):
        code_section = _iss_section(self.text, "Code")
        self.assertIn("CurUninstallStepChanged", code_section)
        self.assertIn("procedure RemoveOwnedLoginStartupValue", code_section)
        ownership_check = code_section.split(
            "procedure RemoveOwnedLoginStartupValue", 1
        )[1].split("procedure CurUninstallStepChanged", 1)[0]
        self.assertIn("RegQueryStringValue", ownership_check)
        self.assertIn(
            "ExpectedCommand := AppExecutable + ' --background'", ownership_check
        )
        self.assertIn(
            "ExpectedQuotedCommand := '\"' + AppExecutable + '\" --background'",
            ownership_check,
        )
        self.assertIn("CompareText(CurrentCommand, ExpectedCommand)", ownership_check)
        self.assertIn(
            "CompareText(CurrentCommand, ExpectedQuotedCommand)", ownership_check
        )
        self.assertIn("RegDeleteValue", ownership_check)
        self.assertIn("RemoteMicRC003", ownership_check)
        self.assertIn(
            r"Software\Microsoft\Windows\CurrentVersion\Run", ownership_check
        )
        uninstall_hook = code_section.split(
            "procedure CurUninstallStepChanged", 1
        )[1]
        self.assertIn("RemoveOwnedLoginStartupValue", uninstall_hook)
        self.assertNotIn("RegDeleteValue", uninstall_hook)

    def test_postinstall_run_opens_settings_and_never_the_bare_no_arg_bridge(self):
        run_section = _iss_section(self.text, "Run")
        self.assertIn("postinstall", run_section)
        self.assertIn("--settings", run_section)
        self.assertIn("打开 {#AppName} {#AppVersion}", run_section)
        self.assertNotIn("unchecked", run_section)
        # The bare (no-argument) form would start bridge mode - BLE/HID/audio
        # - before the user has configured anything. Only one [Run] entry
        # exists in this file, and it must carry --settings.
        self.assertEqual(run_section.count("Filename:"), 1)

    def test_copyright_is_packaged_alongside_license_and_notices(self):
        self.assertIn('DestName: "COPYRIGHT.txt"', self.text)
        self.assertIn('DestName: "THIRD_PARTY_NOTICES.md"', self.text)
        self.assertIn('DestName: "THIRD_PARTY_SOURCE.md"', self.text)
        self.assertIn('DestName: "ASSET_LICENSES.md"', self.text)
        self.assertIn('DestDir: "{app}\\THIRD_PARTY_LICENSES"', self.text)
        self.assertIn('DestName: "LICENSE.txt"', self.text)


class WindowsCiWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.text = _CI_PATH.read_text(encoding="utf-8")

    def test_targets_windows_runner(self):
        self.assertIn("runs-on: windows-2025", self.text)
        self.assertIn("timeout-minutes: 60", self.text)

    def test_pins_python_patch_version_for_release_inventory(self):
        self.assertIn('python-version: "3.12.10"', self.text)

    def test_scoped_to_rc003_paths(self):
        self.assertIn("apps/windows/rc003/**", self.text)

    def test_runs_public_boundary_scan(self):
        self.assertIn("check-public-boundary.ps1", self.text)

    def test_runs_test_suite(self):
        self.assertIn("unittest discover", self.text)

    def test_attempts_pyinstaller_build(self):
        self.assertIn("PyInstaller", self.text)

    def test_runs_dry_run_smoke_check(self):
        self.assertIn("--dry-run", self.text)

    def test_runs_frozen_qt_runtime_smoke_check(self):
        self.assertIn("--qt-runtime-check", self.text)

    def test_runs_the_frozen_hid_helper_self_check_before_the_main_app(self):
        helper_index = self.text.index("RemoteMicRC003HidHelper.exe")
        self_check_index = self.text.index(
            "Invoke-FrozenExecutableCheck -FilePath $builtHidHelper"
        )
        dry_run_index = self.text.index(
            "Invoke-FrozenExecutableCheck -FilePath $builtExe"
        )
        self.assertLess(helper_index, self_check_index)
        self.assertLess(self_check_index, dry_run_index)
        self.assertIn("Start-Process", self.text)
        self.assertIn("-Wait -PassThru -WindowStyle Hidden", self.text)
        self.assertIn("$process.ExitCode", self.text)
        self.assertNotIn("& $builtHidHelper --self-check", self.text)

    def test_rejects_a_frozen_version_that_differs_from_source(self):
        self.assertIn(
            '$builtVersionFile = "dist/RemoteMicRC003/_internal/ovb_rc003/VERSION"',
            self.text,
        )
        self.assertIn("if ($builtVersion -ne $sourceVersion)", self.text)
        self.assertIn("built VERSION mismatch", self.text)

    def test_binds_the_ci_artifact_to_the_checked_source_and_test_state(self):
        test_index = self.text.index("- name: Run test suite")
        capture_index = self.text.index(
            "$inputState = Get-RC003BuildInputState"
        )
        diff_guard_index = self.text.index(
            "tracked build or test inputs changed after their CI checks ran"
        )
        compare_index = self.text.index(
            "build inputs changed after their CI checks ran"
        )
        pyinstaller_index = self.text.index(
            "python -m PyInstaller build/RemoteMicRC003.spec"
        )
        qt_index = self.text.index(
            'Invoke-FrozenExecutableCheck -FilePath $builtExe -ArgumentList @("--qt-runtime-check")'
        )
        write_index = self.text.index("Write-RC003BuildProvenance")
        assert_index = self.text.index("Assert-RC003BuildProvenance", write_index)
        self.assertLess(capture_index, test_index)
        self.assertLess(test_index, diff_guard_index)
        self.assertLess(diff_guard_index, compare_index)
        self.assertLess(compare_index, pyinstaller_index)
        self.assertLess(pyinstaller_index, qt_index)
        self.assertLess(qt_index, write_index)
        self.assertLess(write_index, assert_index)
        self.assertIn("rc003-build-input.json", self.text)

    def test_compiles_inno_setup_installer(self):
        self.assertIn("ISCC.exe", self.text)

    def test_packages_from_the_shared_runtime_version_file(self):
        self.assertIn(
            "$version = (Get-Content -Raw src\\ovb_rc003\\VERSION).Trim()",
            self.text,
        )
        self.assertNotIn("Select-String.*AppVersion", self.text)

    def test_inno_setup_compile_is_a_required_gate_not_best_effort(self):
        # XRBM-018: promoted from best-effort/continue-on-error to a
        # required gate - a flaky/missing Inno Setup toolchain on the
        # runner must fail the job, not be silently skipped. Checked
        # against the effective (non-comment) YAML: this file's own prose
        # explains the change using that phrase, which is documentation,
        # not a directive.
        self.assertNotIn("continue-on-error", _strip_hash_comments(self.text))

    def test_never_runs_the_compiled_installer(self):
        step_start = self.text.index(
            "- name: Compile Inno Setup installer (required gate, never run/installed)"
        )
        step_end = self.text.index("- name:", step_start + 1)
        installer_step = self.text[step_start:step_end].lower()
        self.assertNotIn("start-process", installer_step)
        self.assertNotIn("/verysilent", installer_step)
        self.assertNotIn("/silent", installer_step)

    def test_uses_pinned_supported_actions(self):
        self.assertIn(
            "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
            self.text,
        )
        self.assertIn(
            "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97",
            self.text,
        )
        self.assertEqual(
            self.text.count(
                "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a"
            ),
            1,
        )
        self.assertNotIn("uses: actions/checkout@v", self.text)
        self.assertNotIn("uses: actions/setup-python@v", self.text)
        self.assertNotIn("uses: actions/upload-artifact@v", self.text)

    def test_exactly_one_upload_step_runs_after_every_required_gate(self):
        # XRBM-022 controller pre-review correction: an earlier round
        # uploaded the raw PyInstaller output right after the dry-run smoke
        # check, BEFORE the Inno Setup compile gate - so a job whose Inno
        # step then failed could still leave a published artifact behind.
        # Exactly one upload-artifact step must exist, and it must appear
        # (textually, which matches step execution order in a linear GitHub
        # Actions job) after the Inno compile and the deterministic
        # packaging step.
        upload_marker = (
            "uses: actions/upload-artifact@"
            "043fb46d1a93c77aae656e7c1c64a875d1fc6a0a"
        )
        self.assertEqual(self.text.count(upload_marker), 1)
        iscc_index = self.text.index("ISCC.exe")
        package_index = self.text.index("Compress-Archive")
        upload_index = self.text.index(upload_marker)
        self.assertLess(iscc_index, upload_index)
        self.assertLess(package_index, upload_index)

    def test_triggers_on_files_the_installer_and_spec_consume_outside_rc003(self):
        # XRBM-022 controller pre-review correction: the installer (.iss)
        # packages root COPYRIGHT/LICENSE/THIRD_PARTY_NOTICES.md, and the
        # device-profiles/xiaomi-rc003.json documents identity/HID facts
        # this Windows adapter mirrors - none of these live under
        # apps/windows/rc003/, so the path-scoped trigger above would
        # otherwise silently skip CI on a change to any of them.
        #
        # Matched as an exact YAML list-item LINE (leading "      - "),
        # not a bare substring: the portable-packaging step below also
        # quotes some of these same filenames as Copy-Item destination
        # names (e.g. `(Join-Path $stagingDir "THIRD_PARTY_NOTICES.md")`),
        # which is unrelated to the push/pull_request trigger list and
        # must not be counted as a trigger occurrence.
        for path_trigger in (
            "COPYRIGHT.md",
            "LICENSE.md",
            "THIRD_PARTY_NOTICES.md",
            "THIRD_PARTY_SOURCE.md",
            "ASSET_LICENSES.md",
            "THIRD_PARTY_LICENSES/**",
            "Resources/RC003-remote-photo.png",
            "device-profiles/xiaomi-rc003.json",
        ):
            trigger_line = f'      - "{path_trigger}"'
            self.assertEqual(
                self.text.count(trigger_line),
                2,
                f'{trigger_line!r} must appear once under push.paths and once under pull_request.paths',
            )

    def test_pip_cache_uses_the_exact_requirements_dev_path(self):
        self.assertIn(
            "cache-dependency-path: apps/windows/rc003/requirements-dev.txt",
            self.text,
        )

    def test_supports_manual_and_release_tag_builds_without_auto_publishing(self):
        self.assertIn("workflow_dispatch:", self.text)
        self.assertIn('      - "v*-windows"', self.text)
        self.assertIn('      - "v*-windows-rc003-candidate.*"', self.text)
        self.assertIn("check-release-readiness.py --enforce", self.text)
        self.assertIn("if: startsWith(github.ref, 'refs/tags/')", self.text)
        self.assertNotIn("gh release create", self.text)

    def test_distribution_artifact_is_uploaded_only_after_a_tag_release_gate(self):
        upload_index = self.text.index(
            "uses: actions/upload-artifact@"
            "043fb46d1a93c77aae656e7c1c64a875d1fc6a0a"
        )
        upload_step_start = self.text.rfind("- name:", 0, upload_index)
        upload_step = self.text[upload_step_start:]
        self.assertIn("if: startsWith(github.ref, 'refs/tags/')", upload_step)

    def test_ci_checks_third_party_inventory_and_pins_inno_setup(self):
        self.assertIn("check-third-party-notices.py", self.text)
        self.assertIn("choco install innosetup --version=6.7.1", self.text)
        self.assertIn("group: windows-rc003-ci-${{ github.workflow }}-${{ github.ref }}", self.text)

    def test_test_suite_is_gated_by_resource_warning(self):
        self.assertIn("-W error::ResourceWarning -m unittest discover", self.text)

    def test_test_suite_step_is_verbose_unbuffered_and_bounded(self):
        # Regression for XRBM-023: both canceled real-Windows-CI runs
        # produced only a single buffered "E.......F<235 dots>" line with no
        # test names at all, and the job had no step-level bound - a future
        # hang would again consume the runner up to the 360-minute job
        # default with no way to identify which test hung. The test step
        # must run unittest verbosely (-v) with unbuffered output (-u /
        # PYTHONUNBUFFERED) so a hung test's name is actually flushed and
        # visible before a step-level timeout-minutes cancels the step.
        run_step_start = self.text.index("- name: Run test suite")
        next_step_start = self.text.index("- name:", run_step_start + 1)
        run_step_text = self.text[run_step_start:next_step_start]

        self.assertIn("timeout-minutes:", run_step_text)
        self.assertIn("PYTHONUNBUFFERED", run_step_text)
        self.assertIn("python -u -W error::ResourceWarning -m unittest discover", run_step_text)
        self.assertIn(
            '-m unittest discover -s tests -t . -p "test_*.py" -v', run_step_text
        )

    def test_test_suite_step_hard_gates_late_resourcewarning_output(self):
        # XRBM-026: real Windows run 29644660267 completed 425 tests with
        # "OK (skipped=3)", then printed an ignored ResourceWarning for one
        # unclosed ProactorEventLoop and two unclosed self-pipe sockets -
        # AFTER unittest's own summary, so -W error::ResourceWarning alone
        # never saw it and the step still exited 0 (a warning-turned-
        # exception raised inside a __del__/finalizer at interpreter
        # shutdown is unraisable and cannot change an already-computed exit
        # code). The step must capture its full output to a file (so the
        # -v/unbuffered live Actions-log stream is unaffected) and then
        # scan that file's CONTENT - independent of $LASTEXITCODE - for
        # every one of these forbidden markers, failing the step if any
        # appear (see tests/test_resourcewarning_gate_replay.py for the
        # pure-Python replay of this exact detection logic).
        run_step_start = self.text.index("- name: Run test suite")
        next_step_start = self.text.index("- name:", run_step_start + 1)
        run_step_text = self.text[run_step_start:next_step_start]

        self.assertIn("Tee-Object -FilePath", run_step_text)
        self.assertIn("Get-Content -Path $logPath -Raw", run_step_text)
        self.assertIn("[regex]::Escape($pattern)", run_step_text)
        for pattern in ("ResourceWarning:", "unclosed event loop", "unclosed <socket.socket"):
            self.assertIn(f'"{pattern}"', run_step_text)

    def test_resourcewarning_pattern_requires_the_colon_to_avoid_self_collision(self):
        # A bare "ResourceWarning" (no colon) would match this very test
        # suite's own class name (tests/test_resourcewarning_gate_replay.py's
        # ResourceWarningGateReplayTests), printed verbatim by unittest's -v
        # output - making the gate fail on its own passing regression tests.
        # Real CPython warning/exception output always renders as
        # "ResourceWarning: <message>", so requiring the colon is both safe
        # and sufficient (see tests/test_resourcewarning_gate_replay.py for
        # the full false-positive proof).
        run_step_start = self.text.index("- name: Run test suite")
        next_step_start = self.text.index("- name:", run_step_start + 1)
        run_step_text = self.text[run_step_start:next_step_start]
        self.assertIn('"ResourceWarning:"', run_step_text)
        self.assertNotIn('"ResourceWarning",', run_step_text)

    def test_packages_deterministic_zip_and_sha256sums(self):
        self.assertIn("Compress-Archive", self.text)
        self.assertIn("Get-FileHash", self.text)
        self.assertIn("SHA256SUMS.txt", self.text)
        self.assertIn("-portable-unsigned.zip", self.text)

    def test_portable_zip_stages_license_source_assets_attribution_and_readme(self):
        # XRBM-022 controller pre-review correction: the portable ZIP
        # previously compressed only the bare PyInstaller output
        # (dist/RemoteMicRC003/*), so a user who only downloaded the
        # portable ZIP - never the installer - got no license, copyright,
        # third-party attribution/provenance, or usage instructions at all.
        # Each of these five files must be staged into the versioned
        # portable folder, matching what the installer itself packages
        # (LICENSE.txt/COPYRIGHT.txt/THIRD_PARTY_NOTICES.md) plus the two
        # extras the installer doesn't need but a bare portable ZIP does
        # (ATTRIBUTION.md's file-by-file provenance record and a dedicated
        # portable README that never tells users to run Setup/Start Menu).
        self.assertIn(
            'Copy-Item -Path "../../../LICENSE.md" -Destination (Join-Path $stagingDir "LICENSE.txt")',
            self.text,
        )
        self.assertIn(
            'Copy-Item -Path "../../../COPYRIGHT.md" -Destination (Join-Path $stagingDir "COPYRIGHT.txt")',
            self.text,
        )
        self.assertIn(
            'Copy-Item -Path "../../../THIRD_PARTY_NOTICES.md" -Destination (Join-Path $stagingDir "THIRD_PARTY_NOTICES.md")',
            self.text,
        )
        self.assertIn(
            'Copy-Item -Path "../../../THIRD_PARTY_SOURCE.md" -Destination (Join-Path $stagingDir "THIRD_PARTY_SOURCE.md")',
            self.text,
        )
        self.assertIn(
            'Copy-Item -Path "../../../ASSET_LICENSES.md" -Destination (Join-Path $stagingDir "ASSET_LICENSES.md")',
            self.text,
        )
        self.assertIn(
            'Copy-Item -Path "../../../THIRD_PARTY_LICENSES" -Destination (Join-Path $stagingDir "THIRD_PARTY_LICENSES") -Recurse -Force',
            self.text,
        )
        self.assertIn(
            'Copy-Item -Path "ATTRIBUTION.md" -Destination (Join-Path $stagingDir "ATTRIBUTION.md")',
            self.text,
        )
        self.assertIn(
            'Copy-Item -Path "installer/readme-portable-rc003.txt" -Destination (Join-Path $stagingDir "README.txt")',
            self.text,
        )

    def test_portable_readme_describes_handoff_without_installer_steps(self):
        text = _PORTABLE_README_PATH.read_text(encoding="utf-8")
        self.assertIn("这是解压即用的便携版，不是安装程序", text)
        self.assertIn("旧版完全退出后，这次双击的当前版本会自动继续打开", text)
        self.assertIn("不会强制结束旧版", text)
        self.assertIn("窗口标题会显示", text)
        self.assertNotIn("运行安装器", text)
        self.assertNotIn("Start Menu 分组", text)

    def test_portable_metadata_files_are_staged_before_compress_archive_runs(self):
        # Staging each file is necessary but not sufficient - it must also
        # happen BEFORE Compress-Archive runs, or the ZIP would still be
        # missing them regardless of the Copy-Item lines existing somewhere
        # in the step. A linear pwsh script's step order is exactly its
        # textual (line) order.
        compress_index = self.text.index("Compress-Archive -Path $stagingDir")
        for destination_marker in (
            'Destination (Join-Path $stagingDir "LICENSE.txt")',
            'Destination (Join-Path $stagingDir "COPYRIGHT.txt")',
            'Destination (Join-Path $stagingDir "THIRD_PARTY_NOTICES.md")',
            'Destination (Join-Path $stagingDir "THIRD_PARTY_SOURCE.md")',
            'Destination (Join-Path $stagingDir "ASSET_LICENSES.md")',
            'Destination (Join-Path $stagingDir "THIRD_PARTY_LICENSES")',
            'Destination (Join-Path $stagingDir "ATTRIBUTION.md")',
            'Destination (Join-Path $stagingDir "README.txt")',
        ):
            self.assertLess(
                self.text.index(destination_marker),
                compress_index,
                f"{destination_marker} must be staged before Compress-Archive runs",
            )

    def test_compress_archive_targets_the_staging_directory_not_the_bare_built_glob(self):
        # The archive source must be the STAGING folder (which contains a
        # copy of the built app plus the metadata/instruction payload above),
        # never the bare "dist/RemoteMicRC003/*" glob
        # directly - compressing that glob again would silently regress to
        # the pre-fix "no license/instructions in the ZIP" bug even if the
        # staging/copy lines above still existed elsewhere in the step.
        self.assertIn(
            "Compress-Archive -Path $stagingDir -DestinationPath $zipPath", self.text
        )
        self.assertNotIn(
            'Compress-Archive -Path "dist/RemoteMicRC003/*"', self.text
        )

    def test_compress_archive_uses_terminating_error_handling_not_lastexitcode(self):
        # XRBM-025 RETRY 1 (controller-accepted self-found blocker):
        # Compress-Archive is a PowerShell CMDLET, not a native command, so
        # it never sets $LASTEXITCODE - a stale/unset (always $null in this
        # step, since nothing native/script-based ran earlier)
        # $LASTEXITCODE made the old `if ($LASTEXITCODE -ne 0)` guard
        # unconditionally true, silently exiting the step with code 0
        # right after the ZIP was built, before the release directory was
        # ever staged or preflighted. The only correct fix is promoting
        # this cmdlet's own errors to terminating ones (-ErrorAction Stop)
        # and handling them with an explicit try/catch that exits nonzero
        # - never a $LASTEXITCODE inspection after a cmdlet.
        self.assertIn(
            "Compress-Archive -Path $stagingDir -DestinationPath $zipPath "
            "-CompressionLevel Optimal -ErrorAction Stop",
            self.text,
        )
        self.assertIn("try {", self.text)
        self.assertIn('Write-Error "Compress-Archive failed:', self.text)

    def test_no_lastexitcode_guard_follows_compress_archive(self):
        # The specific invalid pattern this RETRY removes must never
        # reappear immediately after Compress-Archive: scan the text
        # starting right after the Compress-Archive invocation, up to the
        # next non-blank pwsh statement, and assert it is not a
        # $LASTEXITCODE check.
        compress_index = self.text.index(
            "Compress-Archive -Path $stagingDir -DestinationPath $zipPath"
        )
        after_compress = self.text[compress_index:]
        catch_index = after_compress.index("} catch {")
        try_catch_block = after_compress[:catch_index]
        self.assertNotIn("$LASTEXITCODE", try_catch_block)

    def test_compress_archive_failure_exits_nonzero_inside_catch(self):
        step = self._package_step_text()
        try_index = step.index(
            "try {\n            Compress-Archive -Path $stagingDir"
        )
        catch_index = step.index("} catch {", try_index)
        release_dir_index = step.index('$releaseDir = "$env:GITHUB_WORKSPACE', catch_index)
        catch_block = step[catch_index:release_dir_index]
        self.assertIn("exit 1", catch_block)
        self.assertLess(catch_index, release_dir_index)

    def test_successful_archive_continues_into_release_staging_and_preflight(self):
        # The try/catch's happy path (no explicit "else"/continuation
        # marker needed - normal PowerShell try/catch control flow) must
        # fall straight through into the very next statements: installer
        # discovery, then release-directory staging ($releaseDir), then
        # the hard preflight - all textually after the try/catch block, in
        # the same step, none of them behind any other new guard.
        step = self._package_step_text()
        try_block_end = step.index("} catch {")
        installer_lookup_index = step.index(
            '$expectedInstallerName = "RemoteMicRC003Setup-$version-unsigned.exe"'
        )
        release_dir_index = step.index('$releaseDir = "$env:GITHUB_WORKSPACE')
        preflight_index = step.index("$releaseFiles = @(Get-ChildItem -Path $releaseDir -File)")
        self.assertLess(try_block_end, installer_lookup_index)
        self.assertLess(installer_lookup_index, release_dir_index)
        self.assertLess(release_dir_index, preflight_index)

    def test_portable_zip_contract_is_read_back_before_release_staging(self):
        step = self._package_step_text()
        archive_index = step.index(
            "[System.IO.Compression.ZipFile]::OpenRead((Resolve-Path $zipPath).Path)"
        )
        installer_index = step.index(
            '$expectedInstallerName = "RemoteMicRC003Setup-$version-unsigned.exe"'
        )
        self.assertLess(archive_index, installer_index)
        self.assertIn("portable ZIP must contain exactly one top-level directory", step)
        self.assertIn("portable ZIP root must expose only", step)
        self.assertIn("_internal/RemoteMicRC003HidHelper.exe", step)
        self.assertIn("_internal/ovb_rc003/VERSION", step)
        self.assertIn("_internal/build-provenance.json", step)
        self.assertIn("$archiveFiles.ContainsKey($helperEntryName)", step)
        self.assertIn("$versionEntry = $archiveFiles[$versionEntryName]", step)
        self.assertNotIn("$archive.GetEntry", step)
        self.assertIn("portable ZIP VERSION mismatch", step)
        self.assertIn("portable ZIP file count mismatch", step)
        self.assertIn("portable ZIP is missing staging file", step)
        self.assertIn("portable ZIP file length mismatch", step)
        self.assertIn("portable ZIP file hash mismatch", step)
        self.assertIn("[System.IO.Path]::DirectorySeparatorChar", step)
        staging_enumeration = re.search(
            r"Get-ChildItem -LiteralPath \$stagingRoot[^\r\n]+",
            step,
        )
        self.assertIsNotNone(staging_enumeration)
        self.assertIn("-Force", staging_enumeration.group(0))
        self.assertNotIn(".Replace('\\\\', '/')", step)
        self.assertNotIn("[char[]]@('\\\\', '/')", step)

    def test_release_packaging_rechecks_the_build_before_and_after_copying(self):
        step = self._package_step_text()
        initial_index = step.index("$initialBuildProvenance =")
        copy_index = step.index('Copy-Item -Path "dist/RemoteMicRC003/*"')
        staged_index = step.index("$stagedBuildProvenance =")
        archive_index = step.index("[System.IO.Compression.ZipFile]::OpenRead")
        final_index = step.index("$finalBuildProvenance =")
        installer_index = step.index(
            '$expectedInstallerName = "RemoteMicRC003Setup-$version-unsigned.exe"'
        )
        self.assertLess(initial_index, copy_index)
        self.assertLess(copy_index, staged_index)
        self.assertLess(staged_index, archive_index)
        self.assertLess(archive_index, final_index)
        self.assertLess(final_index, installer_index)
        self.assertGreaterEqual(step.count("Assert-RC003BuildProvenanceMatches"), 2)

    def test_selects_only_the_exact_current_version_installer(self):
        step = self._package_step_text()
        self.assertIn(
            '$expectedInstallerName = "RemoteMicRC003Setup-$version-unsigned.exe"',
            step,
        )
        self.assertIn("$installerFiles.Count -ne 1", step)
        self.assertIn("$installerFiles[0].Name -ne $expectedInstallerName", step)
        self.assertNotIn("Select-Object -First 1", step)

    def test_portable_staging_directory_is_a_single_versioned_top_level_folder(self):
        # Compress-Archive given a bare directory path (not a "/*" content
        # glob) wraps that directory itself as the ZIP's one top-level
        # entry - required so extracting the portable ZIP produces one
        # clearly-versioned folder, not loose files scattered at the
        # archive root.
        self.assertIn('$stagingName = "RemoteMicRC003-$version"', self.text)
        self.assertIn('$stagingDir = "dist/portable/$stagingName"', self.text)

    def _package_step_text(self):
        start = self.text.index(
            "- name: Package deterministic unsigned release directory"
        )
        end = self.text.index(
            "- name: Upload deterministic distribution artifacts", start
        )
        return self.text[start:end]

    def _upload_step_text(self):
        return self.text[
            self.text.index("- name: Upload deterministic distribution artifacts") :
        ]

    def test_release_directory_is_staged_from_the_final_zip_and_installer(self):
        # XRBM-024 RETRY: the release directory is a CLEAN copy target, not
        # the original dist/portable|installer locations - so a stale file
        # left over from a previous run can never leak into an upload.
        step = self._package_step_text()
        self.assertIn(
            '$releaseDir = "$env:GITHUB_WORKSPACE/apps/windows/rc003/dist/release"',
            step,
        )
        self.assertIn(
            "if (Test-Path $releaseDir) { Remove-Item $releaseDir -Recurse -Force }",
            step,
        )
        self.assertIn(
            "Copy-Item -Path $zipPath -Destination $releaseZipPath -Force", step
        )
        self.assertIn(
            "Copy-Item -Path $installerExe.FullName -Destination $releaseInstallerPath -Force",
            step,
        )

    def test_release_directory_is_cleaned_before_the_copies_are_staged(self):
        step = self._package_step_text()
        clean_index = step.index(
            "if (Test-Path $releaseDir) { Remove-Item $releaseDir -Recurse -Force }"
        )
        zip_copy_index = step.index(
            "Copy-Item -Path $zipPath -Destination $releaseZipPath -Force"
        )
        installer_copy_index = step.index(
            "Copy-Item -Path $installerExe.FullName -Destination $releaseInstallerPath -Force"
        )
        self.assertLess(clean_index, zip_copy_index)
        self.assertLess(clean_index, installer_copy_index)

    def test_release_manifest_is_generated_from_the_staged_copies_not_the_originals(self):
        # The manifest must hash the files actually sitting in dist/release
        # - not the originals under dist/portable|installer - so a copy
        # failure (or any divergence between the two locations) is
        # reflected in the manifest the preflight below verifies.
        step = self._package_step_text()
        self.assertIn(
            "Get-FileHash -Algorithm SHA256 -LiteralPath $releaseZipPath", step
        )
        self.assertIn(
            "Get-FileHash -Algorithm SHA256 -LiteralPath $releaseInstallerPath", step
        )
        self.assertIn(
            "Set-Content -Path $releaseManifestPath -Value $lines -Encoding ascii",
            step,
        )
        self.assertNotIn("Set-Content -Path dist/SHA256SUMS.txt", step)

    def test_preflight_hard_checks_exactly_three_files_with_the_expected_names(self):
        step = self._package_step_text()
        self.assertIn("$releaseFiles.Count -ne 3", step)
        self.assertIn(
            '$expectedNames = @($zipName, $installerName, "SHA256SUMS.txt") | Sort-Object',
            step,
        )
        self.assertIn(
            "Compare-Object -ReferenceObject $expectedNames -DifferenceObject $actualNames",
            step,
        )

    def test_preflight_hard_checks_exactly_two_well_formed_manifest_lines(self):
        step = self._package_step_text()
        self.assertIn("$manifestLines.Count -ne 2", step)
        self.assertIn(
            r"'^(?<hash>[0-9a-f]{64})  (?<name>\S.*)$'", step
        )

    def test_preflight_verifies_every_manifest_entry_exists_and_hash_matches(self):
        step = self._package_step_text()
        self.assertIn("if (-not (Test-Path $filePath))", step)
        self.assertIn(
            "$actualHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $filePath).Hash.ToLowerInvariant()",
            step,
        )
        self.assertIn("$actualHash -ne $expectedHash", step)

    def test_preflight_runs_before_the_upload_step(self):
        preflight_index = self.text.index(
            "$releaseFiles = @(Get-ChildItem -Path $releaseDir -File)"
        )
        upload_index = self.text.index(
            "uses: actions/upload-artifact@"
            "043fb46d1a93c77aae656e7c1c64a875d1fc6a0a"
        )
        self.assertLess(preflight_index, upload_index)

    def test_upload_path_is_the_single_release_directory_not_a_multi_pattern_list(self):
        # XRBM-024 RETRY red evidence: a real Windows run (29643494504)
        # reported every step green, but the upload-artifact log itself
        # said "With the provided path, there will be 2 files uploaded" and
        # the downloaded artifact was missing SHA256SUMS.txt entirely -
        # because `if-no-files-found: error` only proves at least one of
        # several independent glob patterns matched something, never that
        # every declared pattern did. The fix uploads a single,
        # already-hard-verified directory instead of a multi-pattern list.
        #
        # XRBM-025 RETRY: the single pattern is now the workspace-absolute
        # wildcard form (see the two tests below for why).
        upload_step = self._upload_step_text()
        self.assertIn(
            "path: ${{ github.workspace }}/apps/windows/rc003/dist/release/*",
            upload_step,
        )
        self.assertNotIn("dist/portable/*.zip", upload_step)
        self.assertNotIn("dist/installer/*.exe", upload_step)
        self.assertNotIn("dist/SHA256SUMS.txt", upload_step)
        # Exactly one "path:" input line, and it is a single scalar pattern
        # - never a YAML block/flow list of multiple patterns.
        self.assertEqual(upload_step.count("path:"), 1)
        self.assertNotIn("path: |", upload_step)
        self.assertNotIn("path:\n", upload_step)

    def test_release_dir_is_workspace_absolute_not_working_directory_relative(self):
        # XRBM-025 red evidence: run 29643870781's upload-artifact@v7 step
        # failed "No files were found with the provided path:
        # apps/windows/rc003/dist/release" - a working-directory-relative
        # producer path ($releaseDir = "dist/release", resolved against
        # the job's `apps/windows/rc003` defaults.run.working-directory)
        # and the upload step's own relative path input carry no
        # guarantee of resolving to the same directory. $releaseDir must
        # now be anchored on $env:GITHUB_WORKSPACE so packaging/preflight
        # always operate on an explicit absolute directory, removing that
        # ambiguity.
        step = self._package_step_text()
        self.assertIn("$env:GITHUB_WORKSPACE", step)
        self.assertNotIn('$releaseDir = "dist/release"', step)

    def test_producer_and_consumer_resolve_the_same_absolute_release_directory(self):
        # Proves equivalence, not just that each string exists in
        # isolation: the pwsh producer ($releaseDir) and the YAML consumer
        # (upload-artifact `path:`) must both be built from the SAME
        # workspace-root variable (`$env:GITHUB_WORKSPACE` and
        # `${{ github.workspace }}` are GitHub Actions' own two spellings
        # of the identical absolute value) joined with the identical
        # repository-relative suffix "apps/windows/rc003/dist/release" -
        # so producer and consumer can never again silently diverge the
        # way the relative forms did in run 29643870781.
        package_step = self._package_step_text()
        upload_step = self._upload_step_text()
        release_suffix = "apps/windows/rc003/dist/release"
        self.assertIn(
            "$env:GITHUB_WORKSPACE/" + release_suffix, package_step
        )
        self.assertIn(
            "${{ github.workspace }}/" + release_suffix + "/*", upload_step
        )

    def test_does_not_falsely_claim_zero_sendinput_or_raw_input_usage(self):
        # XRBM-022: the prior comment falsely claimed the job "does not
        # inject any SendInput/Raw Input event" - the Windows-only tests
        # under tests/windows/ actually do exercise a real, harmless,
        # runner-local SendInput call and Raw Input listener lifecycle.
        lower = self.text.lower()
        self.assertNotIn("does not inject any sendinput", lower)
        self.assertIn("sendinput", lower)
        self.assertIn("raw input", lower)

    def test_disclosure_still_denies_real_hardware_and_installer_execution(self):
        lower = self.text.lower()
        self.assertIn("no rc003", lower)
        self.assertIn("device attached", lower)
        self.assertIn("run the compiled installer", lower)

    def test_fetches_and_verifies_vb_cable_before_pyinstaller_build(self):
        # XRBM-031 In-scope item 8: a required gate, run BEFORE PyInstaller,
        # so the frozen build deterministically bundles the verified ZIP.
        self.assertIn("fetch-vb-cable.ps1", self.text)
        fetch_index = self.text.index("fetch-vb-cable.ps1")
        pyinstaller_index = self.text.index("PyInstaller build (unsigned candidate)")
        self.assertLess(fetch_index, pyinstaller_index)

    def test_fetches_and_verifies_frida_before_pyinstaller_build(self):
        self.assertIn("fetch-frida-gadget.ps1", self.text)
        fetch_index = self.text.index("fetch-frida-gadget.ps1")
        pyinstaller_index = self.text.index("PyInstaller build (unsigned candidate)")
        self.assertLess(fetch_index, pyinstaller_index)

    def test_requires_the_narrow_hid_helper_in_the_built_directory(self):
        self.assertIn(
            'dist/RemoteMicRC003/_internal/RemoteMicRC003HidHelper.exe',
            self.text,
        )
        self.assertIn("expected narrow HID helper not found", self.text)
        self.assertIn("build root must expose only RemoteMicRC003.exe", self.text)

    def test_frida_fetch_step_is_a_required_gate_not_best_effort(self):
        step_start = self.text.index("- name: Fetch and verify Frida Gadget")
        next_step_start = self.text.index("- name:", step_start + 1)
        step_text = self.text[step_start:next_step_start]
        self.assertNotIn("continue-on-error", step_text)
        self.assertIn("$LASTEXITCODE", step_text)

    def test_vb_cable_fetch_step_is_a_required_gate_not_best_effort(self):
        step_start = self.text.index("- name: Fetch and verify VB-CABLE driver pack")
        next_step_start = self.text.index("- name:", step_start + 1)
        step_text = self.text[step_start:next_step_start]
        self.assertNotIn("continue-on-error", step_text)
        self.assertIn("$LASTEXITCODE", step_text)


class BuildProvenanceScriptTests(unittest.TestCase):
    def setUp(self):
        self.text = _BUILD_PROVENANCE_PATH.read_text(encoding="utf-8")

    def test_fingerprint_covers_runtime_tests_and_build_contract(self):
        self.assertIn('$sourceRoot = Join-Path $RC003Root "src"', self.text)
        self.assertIn('$testsRoot = Join-Path $RC003Root "tests"', self.text)
        for required in (
            r"build\build-candidate.ps1",
            r"build\package-local-test.ps1",
            r"build\build-provenance.ps1",
            r"build\check-public-boundary.ps1",
            r"build\check-release-readiness.py",
            r"build\check-third-party-notices.py",
            r"build\fetch-frida-gadget.ps1",
            r"build\fetch-vb-cable.ps1",
            r"build\stop-dev.ps1",
            r"build\RemoteMicRC003.spec",
            r"ATTRIBUTION.md",
            r'Join-Path $RC003Root "README.md"',
            r"ASSET_LICENSES.md",
            r"COPYRIGHT.md",
            r"LICENSE.md",
            r'Join-Path $RepoRoot "README.md"',
            r"THIRD_PARTY_NOTICES.md",
            r"THIRD_PARTY_SOURCE.md",
            r".github\workflows\windows-rc003-ci.yml",
        ):
            self.assertIn(required, self.text)
        self.assertIn('$installerRoot = Join-Path $RC003Root "installer"', self.text)
        self.assertIn(
            '$thirdPartyLicensesRoot = Join-Path $RepoRoot "THIRD_PARTY_LICENSES"',
            self.text,
        )

    def test_hidden_files_are_part_of_source_and_artifact_state(self):
        recursive_enumerations = re.findall(
            r"Get-ChildItem[^\r\n]+-File[^\r\n]+-Recurse[^\r\n]*",
            self.text,
        )
        self.assertGreaterEqual(len(recursive_enumerations), 3)
        for enumeration in recursive_enumerations:
            self.assertIn("-Force", enumeration)

    def test_each_file_hash_and_length_come_from_one_locked_stream(self):
        self.assertIn("[System.IO.File]::Open(", self.text)
        self.assertIn("[System.IO.FileShare]::Read", self.text)
        self.assertIn("$length = $stream.Length", self.text)
        self.assertIn("$sha256.ComputeHash($stream)", self.text)
        self.assertNotIn("$($file.Length)", self.text)

    def test_build_gate_is_path_scoped_nonblocking_and_abandonment_safe(self):
        self.assertIn("function Enter-RC003BuildGate", self.text)
        self.assertIn("Get-RC003BuildGateName -RC003Root $RC003Root", self.text)
        self.assertIn('return "Global\\RemoteMicRC003_BuildGate_', self.text)
        self.assertIn("$mutex.WaitOne(0)", self.text)
        self.assertIn("System.Threading.AbandonedMutexException", self.text)
        self.assertIn("another RC003 build or package operation is already running", self.text)
        self.assertIn("function Exit-RC003BuildGate", self.text)

    def test_manifest_recomputes_source_and_artifact_before_accepting(self):
        self.assertIn("Get-RC003BuildInputState", self.text)
        self.assertIn("Get-RC003ArtifactState", self.text)
        self.assertIn("build inputs changed before the source fingerprint was written", self.text)
        self.assertIn("packaged files changed after the last complete build", self.text)
        self.assertIn("main executable hash changed", self.text)
        self.assertIn("HID helper hash changed", self.text)

    @unittest.skipUnless(
        os.name == "nt" and shutil.which("powershell"),
        "requires Windows PowerShell",
    )
    def test_runtime_rejects_same_version_source_change_and_counts_hidden_file(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            repo_root = Path(temporary_directory) / "repo"
            rc003_root = repo_root / "apps" / "windows" / "rc003"
            source_file = rc003_root / "src" / "ovb_rc003" / "app.py"
            fixture_files = {
                source_file: "alpha",
                rc003_root / "src" / "ovb_rc003" / "VERSION": "0.0.1-test\n",
                rc003_root / "tests" / "test_sample.py": "VALUE = 1\n",
                rc003_root / "build" / "build-candidate.ps1": "fixture\n",
                rc003_root / "build" / "package-local-test.ps1": "fixture\n",
                rc003_root / "build" / "build-provenance.ps1": "fixture\n",
                rc003_root / "build" / "check-public-boundary.ps1": "fixture\n",
                rc003_root / "build" / "check-release-readiness.py": "fixture\n",
                rc003_root / "build" / "check-third-party-notices.py": "fixture\n",
                rc003_root / "build" / "fetch-frida-gadget.ps1": "fixture\n",
                rc003_root / "build" / "fetch-vb-cable.ps1": "fixture\n",
                rc003_root / "build" / "stop-dev.ps1": "fixture\n",
                rc003_root / "build" / "RemoteMicRC003.spec": "fixture\n",
                rc003_root / "build" / "generate-app-icon.py": "fixture\n",
                rc003_root / "build" / "third_party" / "VBCABLE_Driver_Pack45.zip": b"zip",
                rc003_root / "requirements.txt": "fixture\n",
                rc003_root / "requirements-dev.txt": "fixture\n",
                rc003_root / "pyproject.toml": "fixture\n",
                rc003_root / "ATTRIBUTION.md": "fixture\n",
                rc003_root / "README.md": "fixture\n",
                rc003_root / "installer" / "application-exit-contract-v1.json": "{}\n",
                rc003_root / "installer" / "readme-portable-rc003.txt": "fixture\n",
                rc003_root / "installer" / "readme-rc003.txt": "fixture\n",
                rc003_root / "installer" / "RemoteMicRC003Setup.iss": "fixture\n",
                rc003_root / "installer" / "stop-app.ps1": "fixture\n",
                repo_root / ".github" / "workflows" / "windows-rc003-ci.yml": "fixture\n",
                repo_root / "README.md": "fixture\n",
                repo_root / "Resources" / "RC003-remote-photo.png": b"png",
                repo_root / "ASSET_LICENSES.md": "fixture\n",
                repo_root / "COPYRIGHT.md": "fixture\n",
                repo_root / "LICENSE.md": "fixture\n",
                repo_root / "THIRD_PARTY_NOTICES.md": "fixture\n",
                repo_root / "THIRD_PARTY_SOURCE.md": "fixture\n",
                repo_root / "THIRD_PARTY_LICENSES" / "sample.txt": "fixture\n",
                repo_root / "device-profiles" / "rc003.json": "{}\n",
            }
            for script_name in (
                "element_navigation_prototype.py",
                "element_navigation_command_windows.py",
                "element_navigation_support.py",
                "element_navigation_windows_host.py",
                "element_targeting_core.py",
                "spatial_navigation_core.py",
            ):
                fixture_files[rc003_root / "scripts" / script_name] = "fixture\n"
            for path, content in fixture_files.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                if isinstance(content, bytes):
                    path.write_bytes(content)
                else:
                    path.write_text(content, encoding="utf-8")

            build_root = rc003_root / "dist" / "RemoteMicRC003"
            built_files = {
                build_root / "RemoteMicRC003.exe": b"main",
                build_root / "_internal" / "RemoteMicRC003HidHelper.exe": b"helper",
                build_root / "_internal" / "ovb_rc003" / "VERSION": b"0.0.1-test\n",
                build_root / "_internal" / "hidden.dat": b"hidden",
            }
            for path, content in built_files.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)

            environment = os.environ.copy()
            environment.update(
                {
                    "RC003_PROVENANCE_SCRIPT": str(_BUILD_PROVENANCE_PATH),
                    "RC003_FIXTURE_ROOT": str(rc003_root),
                    "RC003_FIXTURE_REPO": str(repo_root),
                    "RC003_FIXTURE_BUILD": str(build_root),
                    "RC003_FIXTURE_SOURCE": str(source_file),
                    "RC003_FIXTURE_HIDDEN": str(build_root / "_internal" / "hidden.dat"),
                    "RC003_FIXTURE_RESULT": str(
                        Path(temporary_directory) / "powershell-result.txt"
                    ),
                }
            )
            command = r"""
trap {
    [System.IO.File]::WriteAllText(
        $env:RC003_FIXTURE_RESULT,
        $_.Exception.ToString()
    )
    exit 1
}
$ErrorActionPreference = 'Stop'
. $env:RC003_PROVENANCE_SCRIPT
[System.IO.File]::SetAttributes(
    $env:RC003_FIXTURE_HIDDEN,
    [System.IO.FileAttributes]::Hidden
)
$inputState = Get-RC003BuildInputState `
    -RC003Root $env:RC003_FIXTURE_ROOT `
    -RepoRoot $env:RC003_FIXTURE_REPO
$artifactState = Get-RC003ArtifactState -BuildRoot $env:RC003_FIXTURE_BUILD
if ($artifactState.FileCount -ne 4) {
    throw "hidden artifact was omitted: $($artifactState.FileCount)"
}
Write-RC003BuildProvenance `
    -RC003Root $env:RC003_FIXTURE_ROOT `
    -RepoRoot $env:RC003_FIXTURE_REPO `
    -BuildRoot $env:RC003_FIXTURE_BUILD `
    -InputState $inputState | Out-Null
Assert-RC003BuildProvenance `
    -RC003Root $env:RC003_FIXTURE_ROOT `
    -RepoRoot $env:RC003_FIXTURE_REPO `
    -BuildRoot $env:RC003_FIXTURE_BUILD | Out-Null
[System.IO.File]::WriteAllText($env:RC003_FIXTURE_SOURCE, 'bravo')
try {
    Assert-RC003BuildProvenance `
        -RC003Root $env:RC003_FIXTURE_ROOT `
        -RepoRoot $env:RC003_FIXTURE_REPO `
        -BuildRoot $env:RC003_FIXTURE_BUILD | Out-Null
    throw 'stale source was accepted'
} catch {
    if ($_.Exception.Message -eq 'stale source was accepted') {
        throw
    }
    if ($_.Exception.Message -notmatch 'build inputs changed') {
        throw
    }
}
[System.IO.File]::WriteAllText($env:RC003_FIXTURE_RESULT, 'ok')
"""
            result = subprocess.run(
                [
                    shutil.which("powershell"),
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    command,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=environment,
                timeout=30,
                check=False,
            )
            self.assertEqual(
                result.returncode,
                0,
                "PowerShell provenance fixture failed:\n"
                + result.stdout
                + "\n"
                + result.stderr
                + "\n"
                + (
                    Path(environment["RC003_FIXTURE_RESULT"]).read_text(
                        encoding="utf-8",
                        errors="replace",
                    )
                    if Path(environment["RC003_FIXTURE_RESULT"]).exists()
                    else "no PowerShell result file"
                ),
            )


class BuildCandidateScriptTests(unittest.TestCase):
    def setUp(self):
        self.text = _BUILD_CANDIDATE_PATH.read_text(encoding="utf-8")

    def test_test_suite_invocation_is_gated_by_resource_warning(self):
        # XRBM-022 controller pre-review correction: build-candidate.ps1
        # must enforce the same -W error::ResourceWarning policy as the CI
        # workflow's test-suite step, not just document it in prose.
        self.assertIn("-W error::ResourceWarning -m unittest discover", self.text)

    def test_test_suite_full_log_is_scanned_for_late_resource_leaks(self):
        self.assertIn("Tee-Object -FilePath", self.text)
        self.assertIn("Get-Content -LiteralPath $testLogPath -Raw", self.text)
        for pattern in (
            "ResourceWarning:",
            "unclosed event loop",
            "unclosed <socket.socket",
        ):
            self.assertIn(f'"{pattern}"', self.text)
        self.assertIn("Remove-Item -LiteralPath $testLogPath", self.text)

    def test_unittest_stderr_progress_does_not_bypass_the_exit_code_gate(self):
        self.assertIn(
            "$previousErrorActionPreference = $ErrorActionPreference",
            self.text,
        )
        self.assertIn('$ErrorActionPreference = "Continue"', self.text)
        self.assertIn(
            "$ErrorActionPreference = $previousErrorActionPreference",
            self.text,
        )
        self.assertIn("$testExitCode = $LASTEXITCODE", self.text)
        self.assertIn("if ($testExitCode -ne 0)", self.text)

    def test_local_build_disables_all_live_keyboard_input(self):
        self.assertIn('$env:RC003_DISABLE_LIVE_INPUT = "1"', self.text)
        self.assertIn('$env:RC003_ALLOW_LIVE_INPUT_TESTS = "0"', self.text)

    def test_local_build_requires_python_312_for_creation_and_existing_venv(self):
        self.assertIn('Get-Command "py.exe"', self.text)
        self.assertIn('PrefixArguments = @("-3.12")', self.text)
        self.assertIn("candidate builds require Python 3.12", self.text)
        self.assertIn("Get-PythonMinorVersion -Command $venvPython", self.text)
        self.assertIn(
            "candidate build virtual environment must use Python 3.12",
            self.text,
        )
        self.assertNotIn('[string]$PythonExecutable = "python"', self.text)

    def test_stops_the_marked_development_session_before_touching_the_venv(self):
        stop_index = self.text.index("stop-dev.ps1")
        pip_index = self.text.index("pip install --upgrade pip")
        self.assertLess(stop_index, pip_index)
        self.assertIn('Assert-LastExitCode "stop-dev.ps1"', self.text)

    def test_fetches_and_verifies_vb_cable_before_pyinstaller_build(self):
        # XRBM-031 In-scope item 8: same ordering requirement as the CI
        # workflow (see WindowsCiWorkflowTests above) for the local build.
        self.assertIn("fetch-vb-cable.ps1", self.text)
        fetch_index = self.text.index("fetch-vb-cable.ps1")
        pyinstaller_index = self.text.index("PyInstaller build (unsigned candidate)")
        self.assertLess(fetch_index, pyinstaller_index)
        assert_index = self.text.index('Assert-LastExitCode "fetch-vb-cable.ps1"')
        self.assertGreater(assert_index, fetch_index)

    def test_fetches_and_verifies_frida_before_pyinstaller_build(self):
        self.assertIn("fetch-frida-gadget.ps1", self.text)
        fetch_index = self.text.index("fetch-frida-gadget.ps1")
        pyinstaller_index = self.text.index("PyInstaller build (unsigned candidate)")
        self.assertLess(fetch_index, pyinstaller_index)
        assert_index = self.text.index(
            'Assert-LastExitCode "fetch-frida-gadget.ps1"'
        )
        self.assertGreater(assert_index, fetch_index)

    def test_checks_the_real_frozen_qt_runtime_after_building(self):
        pyinstaller_index = self.text.index("PyInstaller build (unsigned candidate)")
        qt_check_index = self.text.index('@("--qt-runtime-check")')
        self.assertGreater(qt_check_index, pyinstaller_index)
        self.assertIn("Invoke-FrozenExecutableCheck", self.text)
        self.assertIn("$process.ExitCode", self.text)

    def test_requires_the_narrow_hid_helper_before_smoke_checks(self):
        helper_index = self.text.index("RemoteMicRC003HidHelper.exe")
        dry_run_index = self.text.index('@("--dry-run")')
        self.assertLess(helper_index, dry_run_index)
        self.assertIn("expected narrow HID helper not found", self.text)
        self.assertIn("build root must expose only RemoteMicRC003.exe", self.text)
        self.assertIn('@("--self-check")', self.text)
        self.assertIn("Invoke-FrozenExecutableCheck", self.text)
        self.assertIn("-Wait", self.text)
        self.assertIn("-PassThru", self.text)

    def test_rejects_a_stale_or_missing_frozen_version(self):
        self.assertIn("$sourceVersionFile", self.text)
        self.assertIn("$builtVersionFile", self.text)
        self.assertIn("expected built VERSION file not found", self.text)
        self.assertIn("if ($builtVersion -ne $sourceVersion)", self.text)
        self.assertIn("built VERSION mismatch", self.text)

    def test_invalidates_old_provenance_before_any_new_build_attempt(self):
        remove_index = self.text.index(
            "Remove-Item -LiteralPath $BuildProvenancePath"
        )
        test_index = self.text.index('Write-Host "-- test suite --"')
        self.assertLess(remove_index, test_index)
        self.assertIn('. (Join-Path $PSScriptRoot "build-provenance.ps1")', self.text)

    def test_holds_the_shared_build_gate_before_touching_dist(self):
        gate_index = self.text.index("Enter-RC003BuildGate")
        provenance_remove_index = self.text.index(
            "Remove-Item -LiteralPath $BuildProvenancePath"
        )
        release_index = self.text.rindex("Exit-RC003BuildGate")
        self.assertLess(gate_index, provenance_remove_index)
        self.assertGreater(release_index, provenance_remove_index)

    def test_captures_one_input_state_before_all_validation_and_build_steps(self):
        capture_index = self.text.index("$inputStateBeforeBuild =")
        notice_index = self.text.index(
            'Write-Host "-- third-party notice and license inventory --"'
        )
        boundary_index = self.text.index('Write-Host "-- public boundary scan --"')
        test_index = self.text.index('Write-Host "-- test suite --"')
        pyinstaller_index = self.text.index(
            'Write-Host "-- PyInstaller build (unsigned candidate) --"'
        )
        self.assertLess(capture_index, notice_index)
        self.assertLess(capture_index, boundary_index)
        self.assertLess(capture_index, test_index)
        self.assertLess(capture_index, pyinstaller_index)

    def test_writes_provenance_only_after_all_frozen_checks_pass(self):
        qt_index = self.text.index('@("--qt-runtime-check")')
        write_index = self.text.index("Write-RC003BuildProvenance")
        assert_index = self.text.index("Assert-RC003BuildProvenance")
        self.assertLess(qt_index, write_index)
        self.assertLess(write_index, assert_index)
        self.assertIn(
            "build inputs changed while PyInstaller or frozen checks were running",
            self.text,
        )
        catch_index = self.text.index("} catch {")
        cleanup_index = self.text.index(
            "Remove-Item -LiteralPath $BuildProvenancePath",
            catch_index,
        )
        self.assertGreater(cleanup_index, catch_index)


class LocalTestPackageScriptTests(unittest.TestCase):
    def setUp(self):
        self.text = _PACKAGE_LOCAL_TEST_PATH.read_text(encoding="utf-8")

    def test_refuses_stale_builds_and_existing_output(self):
        self.assertIn("built output is stale", self.text)
        self.assertIn("refusing to overwrite existing test package", self.text)

    def test_holds_the_shared_build_gate_for_the_whole_package_operation(self):
        gate_index = self.text.index("Enter-RC003BuildGate")
        version_index = self.text.index("$sourceVersion = Get-RC003SourceVersion")
        publish_index = self.text.index("Move-Item", version_index)
        release_index = self.text.rindex("Exit-RC003BuildGate")
        self.assertLess(gate_index, version_index)
        self.assertGreater(release_index, publish_index)

    def test_runs_all_frozen_smoke_checks_before_compressing(self):
        helper_index = self.text.index('-ArgumentList @("--self-check")')
        dry_run_index = self.text.index('-ArgumentList @("--dry-run")')
        qt_index = self.text.index('-ArgumentList @("--qt-runtime-check")')
        compress_index = self.text.index("Compress-Archive")
        self.assertLess(helper_index, dry_run_index)
        self.assertLess(dry_run_index, qt_index)
        self.assertLess(qt_index, compress_index)

    def test_frozen_smoke_checks_read_the_process_exit_code(self):
        self.assertIn("Start-Process", self.text)
        self.assertIn("-Wait", self.text)
        self.assertIn("-PassThru", self.text)
        self.assertIn("-WindowStyle Hidden", self.text)
        self.assertIn("$process.ExitCode", self.text)
        self.assertNotIn("$LASTEXITCODE", self.text)

    def test_zip_has_one_user_entry_and_the_internal_helper_version(self):
        self.assertIn("portable ZIP must contain exactly one top-level directory", self.text)
        self.assertIn("portable ZIP root must expose only", self.text)
        self.assertIn("_internal/RemoteMicRC003HidHelper.exe", self.text)
        self.assertIn("_internal/ovb_rc003/VERSION", self.text)
        self.assertIn("$archiveFiles.ContainsKey($helperEntryName)", self.text)
        self.assertIn("$versionEntry = $archiveFiles[$versionEntryName]", self.text)
        self.assertNotIn("$archive.GetEntry", self.text)

    def test_zip_is_compared_file_by_file_with_the_staging_directory(self):
        self.assertIn("portable ZIP file count mismatch", self.text)
        self.assertIn("portable ZIP is missing staging file", self.text)
        self.assertIn("portable ZIP file length mismatch", self.text)
        self.assertIn("portable ZIP file hash mismatch", self.text)
        self.assertIn("Get-StreamSha256", self.text)
        self.assertIn("[System.IO.Path]::DirectorySeparatorChar", self.text)
        self.assertNotIn(".Replace('\\\\', '/')", self.text)
        self.assertNotIn("[char[]]@('\\\\', '/')", self.text)

    def test_outputs_zip_and_main_executable_hashes(self):
        self.assertIn("ZIP SHA-256", self.text)
        self.assertIn("EXE SHA-256", self.text)

    def test_refuses_to_write_the_zip_inside_the_build_output(self):
        refusal_index = self.text.index(
            "OutputPath must be outside the build output directory"
        )
        smoke_index = self.text.index('Write-Host "-- verify frozen HID helper --"')
        self.assertLess(refusal_index, smoke_index)
        self.assertIn("[System.StringComparison]::OrdinalIgnoreCase", self.text)

    def test_checks_provenance_before_smoke_copy_and_after_zip_verification(self):
        initial_index = self.text.index("$initialBuildProvenance =")
        smoke_index = self.text.index('Write-Host "-- verify frozen HID helper --"')
        checked_index = self.text.index("$checkedBuildProvenance =")
        copy_index = self.text.index("Copy-Item -Path (Join-Path $BuildRoot")
        staged_index = self.text.index("$stagedBuildProvenance =")
        compress_index = self.text.index("Compress-Archive")
        final_index = self.text.index("$finalBuildProvenance =")
        move_index = self.text.index("Move-Item", final_index)
        self.assertLess(initial_index, smoke_index)
        self.assertLess(smoke_index, checked_index)
        self.assertLess(checked_index, copy_index)
        self.assertLess(copy_index, staged_index)
        self.assertLess(staged_index, compress_index)
        self.assertLess(compress_index, final_index)
        self.assertLess(final_index, move_index)
        self.assertGreaterEqual(self.text.count("Assert-RC003BuildProvenanceMatches"), 3)
        self.assertIn("_internal/build-provenance.json", self.text)

    def test_zip_is_published_atomically_without_deleting_a_competing_result(self):
        self.assertIn("$temporaryOutputPath =", self.text)
        self.assertIn(".tmp.zip", self.text)
        compress_index = self.text.index("Compress-Archive")
        move_index = self.text.index("Move-Item", compress_index)
        self.assertLess(compress_index, move_index)
        catch_index = self.text.index("} catch {", move_index)
        catch_end = self.text.index("} finally {", catch_index)
        catch_block = self.text[catch_index:catch_end]
        self.assertIn("$temporaryOutputPath", catch_block)
        self.assertNotIn("$outputFullPath", catch_block)

    def test_hidden_staging_files_cannot_escape_zip_comparison(self):
        staging_enumeration = re.search(
            r"Get-ChildItem -LiteralPath \$stagingRoot[^\r\n]+",
            self.text,
        )
        self.assertIsNotNone(staging_enumeration)
        self.assertIn("-Force", staging_enumeration.group(0))


class DeveloperEntryScriptTests(unittest.TestCase):
    def setUp(self):
        self.run_text = _RUN_DEV_PATH.read_text(encoding="utf-8")
        self.stop_text = _STOP_DEV_PATH.read_text(encoding="utf-8")
        self.install_text = _INSTALL_DEV_SHORTCUT_PATH.read_text(encoding="utf-8")

    def test_launcher_uses_the_current_checkout_source_and_private_marker(self):
        self.assertIn(r".venv\Scripts\pythonw.exe", self.run_text)
        self.assertIn("PYTHONPATH", self.run_text)
        self.assertIn("-m", self.run_text)
        self.assertIn("ovb_rc003", self.run_text)
        self.assertIn("--settings", self.run_text)
        self.assertIn("--remote-mic-dev-session", self.run_text)

    def test_stopper_requires_exact_local_interpreter_and_private_marker(self):
        self.assertIn("ExecutablePath", self.stop_text)
        self.assertIn(r".venv\Scripts\python.exe", self.stop_text)
        self.assertIn(r".venv\Scripts\pythonw.exe", self.stop_text)
        self.assertIn("OrdinalIgnoreCase", self.stop_text)
        self.assertIn("--remote-mic-dev-session", self.stop_text)
        self.assertIn("--request-exit", self.stop_text)
        self.assertIn("RemoteMicRC003.ApplicationExitRequestV3", self.stop_text)
        self.assertIn("GetCurrentProcess().SessionId", self.stop_text)
        self.assertIn("Get-ProcessTreeIds", self.stop_text)
        self.assertIn("ParentProcessId", self.stop_text)
        self.assertNotIn("Stop-Process", self.stop_text)

    def test_shortcut_targets_the_source_launcher(self):
        self.assertIn('GetFolderPath("Desktop")', self.install_text)
        self.assertIn("[char]0x5F00", self.install_text)
        self.assertIn("[char]0x53D1", self.install_text)
        self.assertIn("[char]0x7248", self.install_text)
        self.assertIn("run-dev.ps1", self.install_text)
        self.assertIn("-WindowStyle Hidden", self.install_text)

    def test_shortcut_uses_a_stable_powershell_executable(self):
        self.assertIn(
            r"System32\WindowsPowerShell\v1.0\powershell.exe",
            self.install_text,
        )
        self.assertIn("GetCurrentProcess().MainModule.FileName", self.install_text)
        self.assertNotIn('Join-Path $PSHOME "powershell.exe"', self.install_text)


class PublicBoundaryScriptTests(unittest.TestCase):
    def setUp(self):
        self.text = _PUBLIC_BOUNDARY_PATH.read_text(encoding="utf-8")

    def test_local_test_packages_are_excluded_without_utf8_script_literals(self):
        self.assertIn("$localTestPackagePrefix", self.text)
        for codepoint in ("65E0", "7EBF", "9EA6", "4FBF", "643A", "6D4B", "8BD5", "5305"):
            self.assertIn(f"[char]0x{codepoint}", self.text)

    def test_timestamped_generated_directories_match_python_replay(self):
        self.assertIn('$component -like "dist-*"', self.text)
        self.assertIn('$component -like "build-*"', self.text)
        self.assertIn('$component -like "pyinstaller-work-*"', self.text)
        self.assertIn("$ExcludedDirNames -contains $component", self.text)


class VbCablePinConsistencyTests(unittest.TestCase):
    """XRBM-031: the URL/SHA-256 pin must agree, character for character,
    across every place it is duplicated - the build-time fetch script
    (PowerShell) and the runtime verification module (Python) - so a future
    edit to one can never silently drift from the other the way the Frida
    Gadget pin's own two copies (fetch-frida-gadget.ps1 / frida_compat.py)
    already established as this project's precedent for this exact
    duplication pattern.
    """

    def setUp(self):
        self.fetch_script_text = (
            _RC003_ROOT / "build" / "fetch-vb-cable.ps1"
        ).read_text(encoding="utf-8")
        self.module_text = (
            _RC003_ROOT / "src" / "ovb_rc003" / "vb_cable_bundle.py"
        ).read_text(encoding="utf-8")

    def test_pinned_sha256_matches_between_ps1_and_py(self):
        from ovb_rc003 import vb_cable_bundle

        self.assertIn(
            vb_cable_bundle.VB_CABLE_PACK45.sha256.upper(), self.fetch_script_text
        )

    def test_pinned_url_matches_between_ps1_and_py(self):
        from ovb_rc003 import vb_cable_bundle

        self.assertIn(vb_cable_bundle.VB_CABLE_PACK45.url, self.fetch_script_text)
        self.assertIn(vb_cable_bundle.VB_CABLE_PACK45.url, self.module_text)

    def test_fetch_script_writes_to_gitignored_third_party_directory(self):
        self.assertIn("third_party", self.fetch_script_text)

    def test_fetch_script_fails_closed_on_hash_mismatch(self):
        self.assertIn("SHA-256 mismatch", self.fetch_script_text)
        self.assertIn("throw", self.fetch_script_text)


class UserFacingDocumentationContractTests(unittest.TestCase):
    """XRBM-022 controller pre-review correction: structural checks that the
    public README and the installed readme both actually contain the exact
    facts/URLs/commands the task book requires, not just prose that a human
    reviewer has to re-verify by eye every round.
    """

    def setUp(self):
        self.readme_text = _README_PATH.read_text(encoding="utf-8")
        self.installed_readme_text = _INSTALLED_README_PATH.read_text(encoding="utf-8")
        self.portable_readme_text = _PORTABLE_README_PATH.read_text(encoding="utf-8")
        self.both = (self.readme_text, self.installed_readme_text)

    def test_official_vbcable_url_is_present_in_both_docs(self):
        for text in self.both:
            self.assertIn("https://vb-audio.com/Cable/", text)

    def test_checksum_verification_command_is_present_in_both_docs(self):
        for text in self.both:
            self.assertIn("Get-FileHash", text)
            self.assertIn("SHA256", text)
            self.assertIn("SHA256SUMS.txt", text)

    def test_exact_cable_routing_direction_is_preserved_in_both_docs(self):
        for text in self.both:
            self.assertIn("CABLE Input", text)
            self.assertIn("CABLE Output", text)
        # The direction itself, not just the two names in any order:
        # the bridge's own voice-output setting selects CABLE Input, and
        # the recognizer/system microphone input selects CABLE Output.
        self.assertIn("语音输出设备", self.readme_text)
        self.assertIn("CABLE Input", self.readme_text.split("语音输出设备")[1][:80])
        self.assertIn("语音输出设备", self.installed_readme_text)
        self.assertIn(
            "CABLE Input", self.installed_readme_text.split("语音输出设备")[1][:120]
        )

    def test_win_h_prerequisites_are_concrete_in_both_docs(self):
        for text in self.both:
            # Cursor focused in an editable field.
            self.assertIn("可编辑", text)
            # Manual Win+H test in Notepad (or another editable field)
            # BEFORE testing the RC003 itself.
            self.assertIn("记事本", text)
            self.assertIn("手动", text)
            # Windows' own online/networked speech-recognition setting.
            self.assertIn("联机语音识别", text)
            # The system/recognizer microphone input must be CABLE Output.
            self.assertIn("CABLE Output", text)

    def test_win_h_settings_path_is_given_for_both_windows_10_and_11(self):
        # XRBM-022 controller pre-review correction: the docs claim Windows
        # 10 1809+ support, but "设置 → 隐私和安全性 → 语音" is the Windows
        # 11 Settings path only - Windows 10's equivalent page is under
        # "设置 → 隐私 → 语音" instead. Both families must be named
        # explicitly so a Windows 10 user isn't sent looking for a menu
        # that doesn't exist on their system.
        for text in self.both:
            self.assertIn("Windows 11", text)
            self.assertIn("设置 → 隐私和安全性 → 语音", text)
            self.assertIn("Windows 10", text)
            self.assertIn("设置 → 隐私 → 语音", text)

    def test_thirteen_button_no_mute_key_and_back_gap_facts_are_present(self):
        for text in self.both:
            self.assertIn("13", text)
            self.assertIn("没有独立的物理静音键", text)
            self.assertIn("返回", text)

    def test_hid_elevation_and_login_startup_are_explained_consistently(self):
        for text in self.both:
            self.assertIn("管理员按键组件", text)
            self.assertIn("自定义按键映射", text)
            self.assertIn("Windows 原始按键", text)
            self.assertIn("随 Windows 启动", text)
            self.assertIn("不再弹 UAC", text)
            self.assertIn("当前登录", text)
            self.assertIn("管理员组", text)

    def test_hid_failure_disables_every_custom_mapping_in_all_user_guides(self):
        for text in (
            self.readme_text,
            self.installed_readme_text,
            self.portable_readme_text,
        ):
            normalized = _normalize_whitespace(text)
            self.assertIn("全部自定义按键映射停用", normalized)
            self.assertIn("只保留 Windows 原始按键操作", normalized)
            self.assertNotIn("只保留 Windows 原始方向键一次", normalized)

    def test_default_window_close_hides_to_tray_in_all_user_guides(self):
        root_readme = _ROOT_README_PATH.read_text(encoding="utf-8")
        for text in (
            root_readme,
            self.readme_text,
            self.installed_readme_text,
            self.portable_readme_text,
        ):
            normalized = _normalize_whitespace(text)
            self.assertIn("关闭窗口默认隐藏到通知区域", normalized)
            self.assertNotIn("关闭窗口默认会完全退出", normalized)
            self.assertNotIn("关闭窗口默认会先正常停止", normalized)

    def test_current_build_version_is_consistent_in_user_guides(self):
        version = _VERSION_PATH.read_text(encoding="ascii").strip()
        self.assertIn(f"`{version}`", self.readme_text)
        self.assertIn(
            f"RemoteMicRC003Setup-{version}-unsigned.exe",
            self.installed_readme_text,
        )
        root_readme = _ROOT_README_PATH.read_text(encoding="utf-8")
        self.assertIn(f"`{version}`", root_readme)

    def test_installed_readme_matches_the_current_three_page_workflow(self):
        text = self.installed_readme_text
        for current_term in (
            "设备”“按键”“语音",
            "启动桥接",
            "安装虚拟音频",
            "应用",
            "日志目录",
            "通知区域",
            "完全退出",
            "小米遥控器2 Pro",
        ):
            self.assertIn(current_term, text)
        for obsolete_term in (
            "保存并启动桥接",
            "退出桥接",
            "“连接”“按键”“权限”“诊断”四个页面",
            "从“诊断”页",
            "不会开机自动启动",
        ):
            self.assertNotIn(obsolete_term, text)

    def test_frida_fetch_wording_matches_the_real_build_entrypoints(self):
        self.assertNotIn("不会由构建脚本自动下载", self.readme_text)
        self.assertIn("build-candidate.ps1", self.readme_text)
        self.assertIn("Windows CI", self.readme_text)
        self.assertIn("自动执行", self.readme_text)


class RootDocumentConsistencyTests(unittest.TestCase):
    """XRBM-022 controller pre-review correction (third round): two public
    root documents contradicted the RC003 Windows candidate's own
    documentation. Both contradictions are fixed structurally here so they
    cannot silently return.
    """

    def setUp(self):
        self.root_readme_text = _ROOT_README_PATH.read_text(encoding="utf-8")
        self.notices_text = _THIRD_PARTY_NOTICES_PATH.read_text(encoding="utf-8")

    def test_root_readme_does_not_lump_windows_in_with_planned_research(self):
        self.assertIn("Windows 版本（小米遥控器2 Pro）", self.root_readme_text)
        self.assertIn("Windows 客户端位于", self.root_readme_text)
        self.assertIn("源码/构建候选", self.root_readme_text)
        self.assertIn("不能替代", self.root_readme_text)

    def test_root_readme_leads_with_the_temporary_admin_workaround(self):
        lines = self.root_readme_text.splitlines()
        first_content = next(line.strip() for line in lines[1:] if line.strip())
        self.assertEqual(first_content, "> [!IMPORTANT]")
        for phrase in (
            "当前公开版本请暂时以管理员身份启动",
            "普通权限运行时仍有已知异常",
            "以管理员身份运行",
        ):
            self.assertIn(phrase, self.root_readme_text)
        self.assertLess(
            self.root_readme_text.index("当前公开版本请暂时以管理员身份启动"),
            self.root_readme_text.index("这是 `ZSTDJan/windows-remote-mic-app` 仓库"),
        )

    def test_third_party_notices_does_not_falsely_deny_all_vbcable_reference(self):
        # THIRD_PARTY_NOTICES.md previously claimed the Windows candidate
        # does not "reference VB-CABLE ... in any form" - false:
        # apps/windows/rc003/README.md and installer/readme-rc003.txt both
        # document the official VB-Audio VB-CABLE download as an optional
        # endpoint, and (XRBM-031) the frozen build now bundles the
        # official, verified, unmodified Basic package offline for a
        # user-initiated, UAC-gated install. The accurate claim: not
        # modified/re-licensed/silently installed by this project, and the
        # runtime only ever writes to an explicitly user-selected endpoint.
        self.assertNotIn(
            "does not download, install, configure, or reference VB-CABLE",
            self.notices_text,
        )
        self.assertNotIn(
            "does not bundle, download, install, configure, license, or redistribute VB-CABLE",
            self.notices_text,
        )
        self.assertIn("Donationware", self.notices_text)
        self.assertIn("explicitly selected", self.notices_text)

    def test_third_party_notices_discloses_the_bundled_vb_cable_flow_honestly(self):
        # XRBM-031: the notice must disclose that the official Basic package
        # is now bundled/fetched (not merely mentioned as a link), that only
        # the free Basic package (never paid A+B/C+D) is involved, that
        # installation only happens via an explicit user click plus a real
        # UAC prompt, and that this project's own process never runs
        # elevated and never reports install success from launch alone.
        notices_text = _normalize_whitespace(self.notices_text)
        self.assertIn("fetch-vb-cable.ps1", notices_text)
        self.assertIn("VBCABLE_Driver_Pack45.zip", notices_text)
        self.assertIn("A+B/C+D", notices_text)
        self.assertIn("UAC", notices_text)
        self.assertIn("never runs with administrator privileges", notices_text)
        self.assertIn(
            "never reports a driver install as successful merely because a process was launched",
            notices_text,
        )
        self.assertIn("never changes the Windows system default input/output device", notices_text)

    def test_root_readme_and_windows_readme_agree_rc003_windows_is_a_candidate(self):
        # Cross-file consistency: both docs must describe the RC003 Windows
        # combination the same way (source/build candidate, not
        # real-device verified) rather than one calling it a candidate and
        # the other calling it merely planned/research.
        windows_readme_text = _README_PATH.read_text(encoding="utf-8")
        for text in (self.root_readme_text, windows_readme_text):
            self.assertIn("源码/构建候选", text)


_CJK_CHAR_RE = r"[　-〿぀-ヿ㐀-鿿＀-￯]"


def _normalize_whitespace(text: str) -> str:
    """Strips markdown blockquote '>' line markers, then collapses all
    remaining whitespace (including the line wraps themselves, which are
    visually joined into flowing prose but stored as physical newlines)
    into single spaces - so a contract test can assert a multi-word phrase
    without depending on exactly where a human editor happened to wrap a
    line, or on the literal '>' markers those wrapped lines carry.

    Unlike English, a CJK line wrap carries no real space in the source
    author's intended reading (Chinese text has no inter-word spaces at
    all), so a whitespace run sitting between two CJK characters is
    dropped entirely rather than collapsed to a single space - otherwise a
    phrase like "出来的文件夹" that happens to wrap between "出来" and "的"
    would normalize to "出来 的文件夹" and silently fail an exact-phrase
    assertion that has nothing to do with the wrap point chosen.
    """

    text = re.sub(r"(?m)^>\s?", "", text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(rf"(?<={_CJK_CHAR_RE}) (?={_CJK_CHAR_RE})", "", text)
    return text.strip()


class PrereleaseDownloadInstructionsContractTests(unittest.TestCase):
    """XRBM-027: the public prerelease download flow - a generic Releases
    page link (so it survives a tag not existing yet), the exact asset name
    patterns the CI packaging step actually produces, and the release-tag
    vs internal-build-version distinction - must stay documented and must
    not silently drift from what the CI workflow actually names its
    outputs (see WindowsCiWorkflowTests above for that side of the
    contract).
    """

    def setUp(self):
        self.text = _README_PATH.read_text(encoding="utf-8")
        self.iss_text = _ISS_PATH.read_text(encoding="utf-8")

    def test_links_to_the_generic_releases_page(self):
        self.assertIn(
            "https://github.com/ZSTDJan/windows-remote-mic-app/releases", self.text
        )
        # The bare list page is the stable entry point; any direct
        # /releases/tag/... link must point at a tag this repo actually
        # published (so a future tag bump that forgets to publish 404s the
        # doc instead of silently breaking).
        self.assertIn(
            "/releases/tag/v0.2.0-windows-rc003-candidate.2", self.text
        )
        self.assertNotIn("miaomiaozii/windows-remote-mic-app", self.text)

    def test_current_public_release_is_not_confused_with_the_local_version(self):
        root_readme = _ROOT_README_PATH.read_text(encoding="utf-8")
        release_url = (
            "https://github.com/ZSTDJan/windows-remote-mic-app/"
            "releases/tag/v0.2.0-windows-rc003-candidate.2"
        )
        self.assertIn(release_url, self.text)
        self.assertIn(release_url, root_readme)
        for asset_name in (
            "RemoteMicRC003Setup-0.2.0-candidate.2-unsigned.exe",
            "RemoteMicRC003-0.2.0-candidate.2-portable-unsigned.zip",
            "SHA256SUMS.txt",
        ):
            self.assertIn(asset_name, root_readme)

    def test_does_not_make_a_time_dependent_claim_about_prerelease_existence(self):
        # XRBM-027 RETRY 1 correction: a sentence saying "even if there is
        # currently no published prerelease yet" is temporally awkward and
        # goes stale the moment the first prerelease is published. The
        # Releases list must be described as a stable entry point without
        # asserting anything about whether a prerelease currently exists.
        self.assertNotIn("还没有发布任何预发行版", self.text)
        self.assertNotIn("发布后再回来查看", self.text)

    def test_asset_name_patterns_match_the_ci_workflows_actual_output_names(self):
        # These placeholders must match, character for character, the
        # deterministic names windows-rc003-ci.yml's packaging step
        # actually produces - see
        # WindowsCiWorkflowTests.test_packages_deterministic_zip_and_sha256sums
        # and .test_portable_staging_directory_is_a_single_versioned_top_level_folder
        # above, and the .iss OutputBaseFilename below.
        self.assertIn("RemoteMicRC003Setup-<版本号>-unsigned.exe", self.text)
        self.assertIn(
            "RemoteMicRC003-<版本号>-portable-unsigned.zip", self.text
        )
        self.assertIn("SHA256SUMS.txt", self.text)
        self.assertIn(
            "OutputBaseFilename=RemoteMicRC003Setup-{#AppVersion}-unsigned",
            self.iss_text,
        )

    def test_documents_the_release_tag_vs_internal_build_version_distinction(self):
        self.assertIn("v0.3.0-windows-rc003-candidate.1", self.text)
        version = _VERSION_PATH.read_text(encoding="ascii").strip()
        self.assertIn(version, self.text)
        self.assertIn("src/ovb_rc003/VERSION", self.text)

    def test_installer_and_portable_are_documented_as_either_or_not_both(self):
        self.assertIn("不需要两个都下载", self.text)


class RealWindowsCiEvidenceContractTests(unittest.TestCase):
    """The Windows README must state capabilities and verification limits
    without copying a stale result from the upstream repository.
    """

    def setUp(self):
        self.readme_text = _normalize_whitespace(
            _README_PATH.read_text(encoding="utf-8")
        )

    def test_documents_supported_windows_paths(self):
        for phrase in ("WinRT BLE", "Raw Input", "SendInput", "PortAudio"):
            self.assertIn(phrase, self.readme_text)

    def test_status_is_a_candidate_that_has_passed_real_device_acceptance(self):
        # The candidate has since completed real-device acceptance (key-by-key
        # and voice-link); the README must say so honestly, while keeping the
        # "cannot be replaced by CI" limit.
        self.assertIn("源码/构建候选", self.readme_text)
        self.assertIn("已通过真实硬件验收", self.readme_text)
        self.assertIn("不能替代真机配对、按键和语音链路验收", self.readme_text)
        self.assertNotIn("verified on real rc003 hardware", self.readme_text.lower())

    def test_unsigned_and_ci_limits_are_documented(self):
        self.assertIn("未签名", self.readme_text)
        self.assertIn("CI 没有真实 RC003 硬件", self.readme_text)
        self.assertIn("已通过真实硬件验收", self.readme_text)

    def test_repository_links_to_its_own_actions_and_releases(self):
        self.assertIn("https://github.com/ZSTDJan/windows-remote-mic-app/releases", self.readme_text)
        self.assertIn("https://github.com/ZSTDJan/windows-remote-mic-app/actions", self.readme_text)
        self.assertNotIn("miaomiaozii/windows-remote-mic-app", self.readme_text)


class ApplicationUpdateDocumentationContractTests(unittest.TestCase):
    def setUp(self):
        self.root_readme = _ROOT_README_PATH.read_text(encoding="utf-8")
        self.windows_readme = _README_PATH.read_text(encoding="utf-8")
        self.installed_readme = _INSTALLED_README_PATH.read_text(encoding="utf-8")
        self.portable_readme = _PORTABLE_README_PATH.read_text(encoding="utf-8")
        self.user_docs = (
            self.root_readme,
            self.windows_readme,
            self.installed_readme,
            self.portable_readme,
        )

    def test_all_user_guides_document_the_manual_verified_update_entry(self):
        for text in self.user_docs:
            normalized = _normalize_whitespace(text)
            with self.subTest(document=text[:40]):
                self.assertIn("运行日志", normalized)
                self.assertIn("检查更新", normalized)
                self.assertIn("只在用户点击后", normalized)
                self.assertIn("后台自动", normalized)
                self.assertIn("SHA256SUMS.txt", normalized)
                self.assertIn("GitHub 提供资产摘要时", normalized)
                self.assertIn(r"updates\<版本号>", normalized)
                self.assertIn("打开文件夹", normalized)

    def test_distribution_guides_keep_install_and_portable_updates_separate(self):
        installed = _normalize_whitespace(self.installed_readme)
        portable = _normalize_whitespace(self.portable_readme)
        source = _normalize_whitespace(self.windows_readme)

        self.assertIn("安装版会下载同一次 Release 的安装器", installed)
        self.assertIn("再手动运行已下载的安装器", installed)
        self.assertIn("取消、中断或校验失败不会覆盖当前程序", installed)
        self.assertIn("便携版会下载同一次 Release 的便携 ZIP", portable)
        self.assertIn("再把 ZIP 解压到新的文件夹使用", portable)
        self.assertIn("取消、中断或校验失败不会改变当前程序", portable)
        self.assertIn("便携版或源码运行下载便携 ZIP", source)
        self.assertIn("不会自动运行下载文件", source)


class PortableAndInstallerFlowContractTests(unittest.TestCase):
    """XRBM-027 RETRY 1 correction: the installer and the portable ZIP are
    materially different distributions - the portable ZIP has no Start
    Menu entries, no stop script, and no uninstaller. The bridge now owns a
    notification-area exit command, so the portable flow uses that graceful
    path and keeps Task Manager only as a last-resort fallback. Each flow
    still needs its own settings/start/stop/removal steps, and the portable
    steps must name the real executable and real flags this candidate ships
    (see __main__.py's ``--settings``/no-argument handling and the .spec's
    ``AppExeName``/``RemoteMicRC003.exe``).
    """

    def setUp(self):
        self.text = _README_PATH.read_text(encoding="utf-8")
        self.normalized = _normalize_whitespace(self.text)

    def test_portable_settings_command_is_exact(self):
        self.assertIn(
            r".\RemoteMicRC003.exe --settings", self.text
        )

    def test_portable_root_exposes_only_the_main_executable(self):
        self.assertIn("解压目录根层只保留一个供用户启动的程序", self.normalized)
        self.assertIn("`RemoteMicRC003.exe`", self.text)
        self.assertIn("`_internal`", self.text)
        self.assertIn("不需要也不应手动打开", self.normalized)

    def test_portable_start_command_uses_the_explicit_bridge_flag(self):
        # The no-argument invocation now opens the settings window, so the
        # bridge must be started explicitly with --bridge - paired with
        # prose that says it starts the bridge itself.
        self.assertIn(
            r"`.\RemoteMicRC003.exe --bridge` 启动桥接", self.normalized
        )

    def test_portable_full_exit_prefers_notification_area_with_task_manager_fallback(self):
        self.assertIn("通知区域", self.text)
        self.assertIn("完全退出", self.text)
        self.assertIn("正常清理 BLE、HID、语音热键与音频资源", self.normalized)
        self.assertIn("任务管理器", self.text)
        self.assertIn("只有托盘不可用且程序无法正常退出时", self.normalized)
        # Must explicitly say there is no packaged stop script/Start Menu
        # entry for the portable flow, so this isn't confused with the
        # installer's "停止" Start Menu shortcut.
        self.assertIn("便携版没有停止脚本", self.normalized)

    def test_portable_removal_cleans_shared_permission_before_deleting_folder(self):
        self.assertIn("点击“移除权限”并确认一次 UAC", self.normalized)
        self.assertIn("再删除整个解压文件夹", self.normalized)
        self.assertIn("不会自动删除已授权助手", self.normalized)

    def test_portable_uses_one_uac_then_normal_startup_and_login_startup(self):
        portable_readme = _normalize_whitespace(
            _PORTABLE_README_PATH.read_text(encoding="utf-8")
        )
        for text in (self.normalized, portable_readme):
            self.assertIn("建议打开管理员按键权限", text)
            self.assertIn("“打开”", text)
            self.assertIn("“不打开”", text)
            self.assertIn("确认一次", text)
            self.assertIn("普通", text)
            self.assertIn("随 Windows 启动", text)
            self.assertIn("不再反复弹 UAC", text)
            self.assertIn("移除权限", text)
        self.assertNotIn("便携版本身不会安装预授权 HID 助手", self.text)
        self.assertNotIn("手动以管理员身份启动便携版", self.text)

    def test_installer_flow_still_retains_start_menu_settings_start_stop_uninstall(self):
        installer_section_start = self.text.index("方式一：安装器")
        installer_section_end = self.text.index("方式二：便携版")
        installer_section = self.text[installer_section_start:installer_section_end]
        for entry in ("设置", "启动", "停止", "卸载"):
            self.assertIn(entry, installer_section)
        self.assertIn("Start Menu", installer_section)

    def test_portable_flow_explicitly_denies_start_menu_entries(self):
        portable_section_start = self.text.index("方式二：便携版")
        portable_section_end = self.text.index("### 配对小米遥控器2 Pro")
        portable_section = self.text[portable_section_start:portable_section_end]
        self.assertIn("没有", portable_section)
        self.assertIn("Start Menu", portable_section)

    def test_installer_and_portable_steps_are_documented_in_separate_subsections(self):
        self.assertIn("**安装器用户**", self.text)
        self.assertIn("**便携版 ZIP 用户**", self.text)
        installer_index = self.text.index("**安装器用户**")
        portable_index = self.text.index("**便携版 ZIP 用户**")
        self.assertLess(installer_index, portable_index)


class ConfigLogResidueDisclosureContractTests(unittest.TestCase):
    """XRBM-027 CORRECTION 1: neither uninstalling via the installer nor
    deleting the portable ZIP's extracted folder removes the runtime
    settings/log files, because both write to the same
    ``config.config_root()`` location and the .iss source has no
    ``UninstallDelete`` rule for them. Both public docs must disclose this
    honestly, and the literal path/filename strings they use must be
    cross-checked against the real runtime constants so a future rename in
    config.py/logging_setup.py can't silently leave the docs wrong.
    """

    def setUp(self):
        self.readme_text = _normalize_whitespace(
            _README_PATH.read_text(encoding="utf-8")
        )
        self.installed_readme_text = _normalize_whitespace(
            _INSTALLED_README_PATH.read_text(encoding="utf-8")
        )
        self.both = (self.readme_text, self.installed_readme_text)
        self.iss_text = _ISS_PATH.read_text(encoding="utf-8")

    def test_documented_path_and_filenames_match_the_real_runtime_constants(self):
        # Not hardcoded literals independent of the source of truth: derive
        # the exact strings from config.py/logging_setup.py themselves, so
        # a future rename of APP_ID/PRODUCT_ID/CONFIG_FILENAME/
        # KEY_BINDINGS_FILENAME/LOG_FILENAME breaks this test instead of
        # silently leaving the docs pointing at a stale path/filename.
        expected_root = r"%LOCALAPPDATA%\{}\{}".format(
            config.APP_ID, config.PRODUCT_ID
        )
        for text in self.both:
            self.assertIn(expected_root, text)
            self.assertIn(config.CONFIG_FILENAME, text)
            self.assertIn(config.KEY_BINDINGS_FILENAME, text)
            self.assertIn(logging_setup.LOG_FILENAME, text)
            # The log file lives in a "logs" subdirectory of config_root(),
            # not directly inside it (see logging_setup.get_logger()).
            self.assertIn("logs" + "\\" + logging_setup.LOG_FILENAME, text)

    def test_uninstall_delete_is_limited_to_fixed_upgrade_backups(self):
        uninstall_delete = _strip_semicolon_comments(
            _iss_section(self.iss_text, "UninstallDelete")
        )
        self.assertIn(r'{app}\.installing-previous', uninstall_delete)
        self.assertIn(
            r'{app}\{#AppExeName}.installing-previous',
            uninstall_delete,
        )
        for user_data in ("config.json", "key_bindings.json", "logs", "captures"):
            self.assertNotIn(user_data, uninstall_delete)

    def test_uninstall_does_not_claim_full_directory_removal(self):
        for text in self.both:
            self.assertNotIn("不留系统级文件", text)
        # The installed readme previously claimed uninstall deletes "安装
        # 目录" (the whole install directory) outright - inaccurate, since
        # config_root() is the SAME directory the installer uses
        # (DefaultDirName={localappdata}\RemoteMic\{#AppFolder}) and
        # runtime-written files there are never enumerated by Setup, so
        # Inno's uninstaller does not know to remove them.
        self.assertNotIn(
            "卸载过程会先自动停止正在运行的桥接进程，再删除安装目录",
            self.installed_readme_text,
        )

    def test_both_docs_disclose_settings_and_logs_survive_removal(self):
        for text in self.both:
            self.assertIn("卸载不会自动删除设置和日志", text)

    def test_both_docs_offer_conditional_manual_cleanup(self):
        # Must be conditional (only when no other RC003 install on the same
        # machine needs the shared directory) - not an unconditional "just
        # delete it" instruction, since the installer and portable builds
        # share the exact same config_root() on one machine.
        for text in self.both:
            self.assertIn("如果还会用到", text)
            self.assertIn("请不要删除这个共享目录", text)

    def test_portable_removal_step_names_config_root_not_just_the_extracted_folder(self):
        # The portable "uninstall/removal" step specifically must not stop
        # at "delete the extracted folder" - it must name the separate,
        # shared config_root() location too.
        portable_section_start = self.readme_text.index("**便携版 ZIP 用户**")
        portable_section = self.readme_text[portable_section_start:]
        self.assertIn("便携版运行时同样会把", portable_section)
        self.assertIn(config.CONFIG_FILENAME, portable_section)


class WindowsPrereleaseAssetScopeContractTests(unittest.TestCase):
    """XRBM-027 CORRECTION 1: a bare "every prerelease has exactly these
    three files" claim must not be read as covering unrelated releases or
    other product variants.
    """

    def setUp(self):
        self.text = _README_PATH.read_text(encoding="utf-8")

    def test_asset_count_claim_is_scoped_to_the_windows_candidate(self):
        self.assertIn("每个 RC003 Windows 候选预发行版恰好包含以下三个文件", self.text)
        self.assertNotIn("每个预发行版恰好包含以下三个文件", self.text)


def _spec_hidden_imports(text: str) -> list:
    tree = ast.parse(text, filename=str(_SPEC_PATH))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "hiddenimports"
            for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError("hiddenimports assignment not found in spec")


class QtSettingsUiSpecTests(unittest.TestCase):
    """XRBM-030: static contract that the PyInstaller spec actually bundles
    the Qt Quick/QML settings window's own qml/ sources (never covered by
    any PySide6 hook, which only auto-collects Qt's OWN Quick Controls/QML
    plugin assets - see the spec's own comment) and hidden-imports every
    PySide6 submodule qt_settings_app.py needs - a real Windows CI
    PyInstaller build remains the platform-level confirmation on top of
    this structural one.
    """

    def setUp(self):
        self.spec_text = _SPEC_PATH.read_text(encoding="utf-8")
        self.requirements_text = _REQUIREMENTS_PATH.read_text(encoding="utf-8")

    def test_requirements_pins_pyside6_essentials(self):
        self.assertIn("PySide6-Essentials==", self.requirements_text)
        # Never an actual PySide6-Addons *pin* - checked against effective
        # (non-comment) content, since requirements.txt's own comment
        # legitimately explains in prose why Addons is excluded.
        self.assertNotIn(
            "PySide6-Addons", _strip_hash_comments(self.requirements_text)
        )

    def test_spec_collects_the_qml_source_tree_as_data_under_the_expected_name(self):
        # Must match qt_settings_app.py's _qml_directory() frozen-build
        # lookup path exactly: sys._MEIPASS / "ovb_rc003_qml".
        self.assertIn('QML_SOURCE_DIR = SRC_ROOT / "ovb_rc003" / "qml"', self.spec_text)
        self.assertIn('datas.append((str(QML_SOURCE_DIR), "ovb_rc003_qml"))', self.spec_text)

    def test_spec_hidden_imports_every_pyside6_submodule_qt_settings_app_uses(self):
        hiddenimports = _spec_hidden_imports(self.spec_text)
        for module in (
            "PySide6.QtCore",
            "PySide6.QtGui",
            "PySide6.QtQml",
            "PySide6.QtQuick",
            "PySide6.QtQuickControls2",
            "ovb_rc003.qt_settings_app",
        ):
            self.assertIn(module, hiddenimports)

    def test_qml_directory_name_matches_qt_settings_app_frozen_lookup(self):
        # Cross-file consistency: the exact "ovb_rc003_qml" folder name must
        # agree between the spec (producer) and qt_settings_app.py's
        # _qml_directory() (consumer) - a silent rename on either side would
        # otherwise pass every other test here and only fail at runtime on a
        # real frozen build, the same class of bug XRBM-024's WinRT
        # dependency-closure tests above guard against.
        qt_settings_app_path = (
            _RC003_ROOT / "src" / "ovb_rc003" / "qt_settings_app.py"
        )
        qt_settings_app_text = qt_settings_app_path.read_text(encoding="utf-8")
        self.assertIn('"ovb_rc003_qml"', self.spec_text)
        self.assertIn('"ovb_rc003_qml"', qt_settings_app_text)

    def test_spec_rejects_only_ambient_icuuc_dlls(self):
        tree = ast.parse(self.spec_text, filename=str(_SPEC_PATH))
        predicate_node = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_is_ambient_icuuc"
        )
        namespace = {"Path": Path}
        exec(
            compile(
                ast.Module(body=[predicate_node], type_ignores=[]),
                str(_SPEC_PATH),
                "exec",
            ),
            namespace,
        )
        predicate = namespace["_is_ambient_icuuc"]

        self.assertTrue(
            predicate(
                (
                    "icuuc.dll",
                    r"C:\external-tools\poppler\bin\icuuc.dll",
                    "BINARY",
                )
            )
        )
        self.assertFalse(
            predicate(
                (
                    "icuuc.dll",
                    r"C:\app\.venv\Lib\site-packages\PySide6\icuuc.dll",
                    "BINARY",
                )
            )
        )
        self.assertFalse(
            predicate(
                (
                    "PySide6\\QtCore.pyd",
                    r"C:\app\.venv\Lib\site-packages\PySide6\QtCore.pyd",
                    "BINARY",
                )
            )
        )
        self.assertGreater(
            self.spec_text.index("a.binaries = ["),
            self.spec_text.index("a = Analysis("),
        )


if __name__ == "__main__":
    unittest.main()
