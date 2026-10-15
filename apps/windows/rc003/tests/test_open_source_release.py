"""Contracts for open-source release metadata and non-code release gates."""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock


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
    def _history_blockers(self, commit, *, subject="contribution", patch="", content="", content_by_marker=None):
        script_path = BUILD_ROOT / "check-release-readiness.py"
        namespace = {"__name__": "release_gate_contract", "__file__": str(script_path)}
        exec(compile(script_path.read_text(encoding="utf-8"), str(script_path), "exec"), namespace)

        def run_git(*args):
            output = ""
            if args[0] == "rev-parse":
                output = str(REPO_ROOT)
            elif args[:2] == ("log", "--format=%H%x00%ae%x00%ce%x00%s"):
                output = "\x00".join((commit, "author@example.test", "committer@example.test", subject))
            elif "--patch" in args:
                output = patch
            elif "-S" in args:
                output = content if content_by_marker is None else content_by_marker.get(args[args.index("-S") + 1], "")
            return subprocess.CompletedProcess(args, 0, output, "")

        with mock.patch.dict(namespace, {"_run_git": run_git}):
            return namespace["_git_history_blockers"]()

    def test_explicitly_approved_contributor_commit_keeps_original_email(self):
        self.assertEqual(self._history_blockers("2dc521834df4b30d3492f9ab5355ccf4a125b8c5"), [])

    def test_other_commits_with_ordinary_email_are_still_blocked(self):
        self.assertIn("1 commits expose a non-noreply email", self._history_blockers("0" * 40))

    def test_approved_email_does_not_exempt_internal_subjects(self):
        marker = "".join(map(chr, (0x56, 0x69, 0x62, 0x65, 0x50, 0x61, 0x64)))
        self.assertIn("1 commit subjects expose internal references", self._history_blockers(
            "2dc521834df4b30d3492f9ab5355ccf4a125b8c5", subject=marker,
        ))

    def test_approved_email_does_not_exempt_internal_content(self):
        self.assertTrue(any("internal-reference markers" in item for item in self._history_blockers(
            "2dc521834df4b30d3492f9ab5355ccf4a125b8c5", content="fixture",
        )))

    def test_approved_email_does_not_exempt_private_paths(self):
        private_path = chr(92).join(("C:", "Users", "fixture", "private"))
        self.assertIn("Git history contains a private absolute path", self._history_blockers(
            "2dc521834df4b30d3492f9ab5355ccf4a125b8c5", patch=private_path,
        ))

    def test_approved_public_path_insertion_and_removal_are_allowed(self):
        marker = "".join(map(chr, (0x44, 0x3A, 0x5C, 0x57, 0x75, 0x78, 0x69, 0x61, 0x6E, 0x6D, 0x61, 0x69)))
        commits = "3f685c42b6a99a83162db81ef80c647fde6dfc66\nf8a50650082ca25f821d57bab7bf50470de1e3ae\n"
        self.assertEqual(self._history_blockers(
            "2dc521834df4b30d3492f9ab5355ccf4a125b8c5", content_by_marker={marker: commits},
        ), [])

    def test_new_occurrence_of_approved_path_is_still_blocked(self):
        marker = "".join(map(chr, (0x44, 0x3A, 0x5C, 0x57, 0x75, 0x78, 0x69, 0x61, 0x6E, 0x6D, 0x61, 0x69)))
        commits = "3f685c42b6a99a83162db81ef80c647fde6dfc66\n" + "0" * 40 + "\n"
        self.assertTrue(any("internal-reference markers" in item for item in self._history_blockers(
            "2dc521834df4b30d3492f9ab5355ccf4a125b8c5", content_by_marker={marker: commits},
        )))

    def test_approved_path_commits_do_not_exempt_other_markers(self):
        marker = "".join(map(chr, (0x44, 0x3A, 0x5C, 0x43, 0x6C, 0x65, 0x61, 0x72)))
        self.assertTrue(any("internal-reference markers" in item for item in self._history_blockers(
            "2dc521834df4b30d3492f9ab5355ccf4a125b8c5",
            content_by_marker={marker: "3f685c42b6a99a83162db81ef80c647fde6dfc66\n"},
        )))

    def test_approved_path_commits_keep_email_subject_and_profile_checks(self):
        marker = "".join(map(chr, (0x44, 0x3A, 0x5C, 0x57, 0x75, 0x78, 0x69, 0x61, 0x6E, 0x6D, 0x61, 0x69)))
        commit = "3f685c42b6a99a83162db81ef80c647fde6dfc66"
        blockers = self._history_blockers(
            commit, subject=marker, patch=chr(92).join(("C:", "Users", "fixture", "private")),
            content_by_marker={marker: commit + "\n"},
        )
        self.assertIn("1 commits expose a non-noreply email", blockers)
        self.assertIn("1 commit subjects expose internal references", blockers)
        self.assertIn("Git history contains a private absolute path", blockers)

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
