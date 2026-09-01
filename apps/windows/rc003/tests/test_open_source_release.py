"""Contracts for open-source release metadata and non-code release gates."""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path


RC003_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = RC003_ROOT.parents[2]
BUILD_ROOT = RC003_ROOT / "build"


class CommunityHealthFileTests(unittest.TestCase):
    def test_required_public_collaboration_files_exist(self):
        for relative_path in (
            "SECURITY.md",
            "CONTRIBUTING.md",
            "CODE_OF_CONDUCT.md",
            ".github/PULL_REQUEST_TEMPLATE.md",
            ".github/ISSUE_TEMPLATE/bug_report.yml",
            ".github/ISSUE_TEMPLATE/feature_request.yml",
            ".github/ISSUE_TEMPLATE/config.yml",
            ".github/dependabot.yml",
        ):
            self.assertTrue((REPO_ROOT / relative_path).is_file(), relative_path)

    def test_bug_template_requires_version_reproduction_and_privacy_confirmation(self):
        text = (REPO_ROOT / ".github/ISSUE_TEMPLATE/bug_report.yml").read_text(
            encoding="utf-8"
        )
        for marker in ("版本来源", "Windows 环境", "复现步骤", "隐私确认"):
            self.assertIn(marker, text)
        for sensitive_marker in ("蓝牙地址", "HID 路径", "个人绝对路径"):
            self.assertIn(sensitive_marker, text)

    def test_dependabot_covers_python_and_github_actions(self):
        text = (REPO_ROOT / ".github/dependabot.yml").read_text(encoding="utf-8")
        self.assertIn("package-ecosystem: pip", text)
        self.assertIn("directory: /apps/windows/rc003", text)
        self.assertIn("package-ecosystem: github-actions", text)


class ThirdPartyReleaseGateTests(unittest.TestCase):
    def test_pinned_dependencies_have_notice_entries(self):
        result = subprocess.run(
            [sys.executable, str(BUILD_ROOT / "check-third-party-notices.py")],
            cwd=RC003_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("check-third-party-notices: passed", result.stdout)

    def test_release_readiness_can_report_blockers_without_claiming_success(self):
        result = subprocess.run(
            [sys.executable, str(BUILD_ROOT / "check-release-readiness.py")],
            cwd=RC003_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(
            "formal release blockers:" in result.stdout
            or "check-release-readiness: passed" in result.stdout
        )

    def test_formal_release_gate_checks_complete_git_history(self):
        source = (BUILD_ROOT / "check-release-readiness.py").read_text(
            encoding="utf-8"
        )
        workflow = (REPO_ROOT / ".github/workflows/windows-rc003-ci.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("_git_history_blockers", source)
        self.assertIn('revision_range = "HEAD"', source)
        self.assertNotIn("_RELEASE_HISTORY_BASE", source)
        self.assertIn("fetch-depth: 0", workflow)

    def test_license_bundle_contains_every_file_enforced_by_the_release_gate(self):
        source = (BUILD_ROOT / "check-release-readiness.py").read_text(
            encoding="utf-8"
        )
        script_path = BUILD_ROOT / "check-release-readiness.py"
        namespace: dict[str, object] = {
            "__name__": "release_gate_contract",
            "__file__": str(script_path),
        }
        exec(compile(source, str(script_path), "exec"), namespace)
        required = namespace["_REQUIRED_LICENSE_FILES"]
        license_root = REPO_ROOT / "THIRD_PARTY_LICENSES"
        for relative_path in required:
            self.assertTrue((license_root / relative_path).is_file(), relative_path)

    def test_pyinstaller_spec_excludes_the_unused_asio_portaudio_binary(self):
        text = (BUILD_ROOT / "RemoteMicRC003.spec").read_text(encoding="utf-8")
        self.assertIn("_is_unneeded_sounddevice_asio", text)
        self.assertIn('"libportaudio64bit-asio.dll"', text)
        self.assertIn("and not _is_unneeded_sounddevice_asio(binary_entry)", text)


if __name__ == "__main__":
    unittest.main()
