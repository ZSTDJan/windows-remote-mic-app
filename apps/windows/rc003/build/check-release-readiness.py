"""Report or enforce non-code gates for a formal Windows release tag."""

from __future__ import annotations

import argparse
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
