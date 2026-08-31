"""Validate that every pinned Windows build dependency has a public notice."""

from __future__ import annotations

import re
from pathlib import Path


RC003_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[4]
NOTICE_PATH = REPO_ROOT / "THIRD_PARTY_NOTICES.md"

_PIN_RE = re.compile(r"^(?P<name>[A-Za-z0-9_.-]+)==(?P<version>[^;\s]+)")
_CORE_MARKERS = (
    "CPython 3.12.10",
    "OpenSSL 3",
    "PortAudio",
    "Microsoft runtime components",
    "RC003 product photo",
    "VB-CABLE",
)


def _requirements(path: Path, seen: set[Path] | None = None) -> dict[str, str]:
    seen = seen or set()
    path = path.resolve()
    if path in seen:
        return {}
    seen.add(path)

    pinned: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("-r "):
            pinned.update(_requirements(path.parent / line[3:].strip(), seen))
            continue
        match = _PIN_RE.match(line)
        if match:
            pinned[match.group("name")] = match.group("version")
    return pinned


def missing_notice_markers(notice_text: str) -> list[str]:
    requirements = _requirements(RC003_ROOT / "requirements-dev.txt")
    markers = [f"{name}=={version}" for name, version in requirements.items()]
    markers.extend(_CORE_MARKERS)
    return sorted(marker for marker in markers if marker not in notice_text)


def main() -> int:
    notice_text = NOTICE_PATH.read_text(encoding="utf-8")
    missing = missing_notice_markers(notice_text)
    if missing:
        for marker in missing:
            print(f"missing third-party notice marker: {marker}")
        return 1
    print("check-third-party-notices: passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
