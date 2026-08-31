"""Report or enforce non-code gates for a formal Windows release tag."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[4]

_STATUS_REQUIREMENTS = (
    (REPO_ROOT / "ASSET_LICENSES.md", "Release status: APPROVED"),
    (REPO_ROOT / "THIRD_PARTY_SOURCE.md", "Release status: READY"),
)
_REQUIRED_LICENSE_FILES = (
    "Python-3.12/LICENSE.txt",
    "Qt-PySide6-6.11.1/LGPL-3.0-only.txt",
    "Qt-PySide6-6.11.1/GPL-3.0-only.txt",
    "NumPy-2.4.3/LICENSE.txt",
    "sounddevice-0.5.5/LICENSE.txt",
    "PortAudio/LICENSE.txt",
    "Frida-17.15.3/COPYING.txt",
    "uiautomation-2.0.29/LICENSE.txt",
    "comtypes-1.4.16/LICENSE.txt",
    "pywinrt-3.2.1/LICENSE.txt",
    "cffi-2.1.1/LICENSE.txt",
    "pycparser-3.0/LICENSE.txt",
    "typing_extensions-4.16.0/LICENSE.txt",
    "PyInstaller-6.21.0/COPYING.txt",
    "OpenSSL-3/LICENSE.txt",
)

_RELEASE_HISTORY_BASE = "271ed7947eec19c4c691ed3ba97f338461be8051"
_PRIVATE_HISTORY_MARKERS = (
    chr(0x8A00) + chr(0x7075),
    "vibe" + "-flow",
    "Vibe " + "Flow",
    "richlearntodo" + "-debug",
    "Vibe" + "Pad",
    "Key" + "Hop",
    "Say" + "All",
    "D:" + "\\Wuxianmai",
    "D:" + "\\Clear",
    "C:" + "\\Users\\" + "DELL",
)


def _run_git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _is_public_git_email(value: str) -> bool:
    email = value.strip().casefold()
    return email == "noreply@github.com" or email.endswith("@users.noreply.github.com")


def _git_history_blockers() -> list[str]:
    blockers: list[str] = []
    repository = _run_git("rev-parse", "--show-toplevel")
    if repository.returncode != 0:
        return ["Git history is unavailable for the formal release privacy check"]

    baseline = _run_git("cat-file", "-e", f"{_RELEASE_HISTORY_BASE}^{{commit}}")
    if baseline.returncode != 0:
        return ["complete post-baseline Git history is unavailable"]

    revision_range = f"{_RELEASE_HISTORY_BASE}..HEAD"
    metadata = _run_git(
        "log",
        "--format=%H%x00%ae%x00%ce%x00%s",
        revision_range,
    )
    if metadata.returncode != 0:
        return ["Git history metadata could not be checked"]

    private_email_commits: set[str] = set()
    private_subject_commits: set[str] = set()
    folded_markers = tuple(marker.casefold() for marker in _PRIVATE_HISTORY_MARKERS)
    for line in metadata.stdout.splitlines():
        fields = line.split("\x00", 3)
        if len(fields) != 4:
            blockers.append("Git history metadata contains an unreadable commit record")
            break
        commit, author_email, committer_email, subject = fields
        if not _is_public_git_email(author_email) or not _is_public_git_email(
            committer_email
        ):
            private_email_commits.add(commit)
        folded_subject = subject.casefold()
        if any(marker in folded_subject for marker in folded_markers):
            private_subject_commits.add(commit)

    if private_email_commits:
        blockers.append(
            f"{len(private_email_commits)} post-baseline commits expose a non-noreply email"
        )
    if private_subject_commits:
        blockers.append(
            f"{len(private_subject_commits)} post-baseline commit subjects expose internal references"
        )

    private_content_markers = 0
    for marker in _PRIVATE_HISTORY_MARKERS:
        matches = _run_git("log", "--format=%H", "-S", marker, revision_range, "--", ".")
        if matches.returncode != 0:
            blockers.append("Git history content could not be checked")
            break
        if matches.stdout.strip():
            private_content_markers += 1
    if private_content_markers:
        blockers.append(
            f"Git history contains {private_content_markers} private-path or internal-reference markers"
        )
    return blockers


def release_blockers() -> list[str]:
    blockers: list[str] = []
    for path, required_status in _STATUS_REQUIREMENTS:
        if not path.is_file():
            blockers.append(f"missing release record: {path.name}")
            continue
        text = path.read_text(encoding="utf-8")
        if required_status not in text:
            blockers.append(f"{path.name} has not reached '{required_status}'")

    license_root = REPO_ROOT / "THIRD_PARTY_LICENSES"
    for relative_path in _REQUIRED_LICENSE_FILES:
        if not (license_root / relative_path).is_file():
            blockers.append(f"missing third-party license: {relative_path}")
    blockers.extend(_git_history_blockers())
    return blockers


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--enforce",
        action="store_true",
        help="return a non-zero exit code when any formal-release blocker remains",
    )
    args = parser.parse_args()

    blockers = release_blockers()
    if blockers:
        print("formal release blockers:")
        for blocker in blockers:
            print(f"- {blocker}")
        return 1 if args.enforce else 0

    print("check-release-readiness: passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
