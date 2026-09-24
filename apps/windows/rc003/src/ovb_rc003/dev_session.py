"""Developer-session marker shared by source launch descendants."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Iterable, List


DEV_SESSION_FLAG = "--remote-mic-dev-session"
DEV_SESSION_ENV = "REMOTE_MIC_DEV_SESSION"
ISOLATED_FLAG = "--isolated-test"
ISOLATED_ENV = "REMOTE_MIC_ISOLATED_TEST"


def is_isolated() -> bool:
    return os.environ.get(ISOLATED_ENV) == "1"


def isolated_root() -> Path:
    """Source-only fixed data root; never redirect OS application discovery."""
    if getattr(sys, "frozen", False):
        raise ValueError("隔离测试入口仅限源码运行。")
    workspace = Path(__file__).resolve().parents[2]
    root = workspace / ".build" / "isolated-runtime"
    if root.resolve() != root:
        raise ValueError("隔离测试目录被重定向，拒绝启动。")
    return root


def consume_marker(arguments: Iterable[str]) -> List[str]:
    """Remove the private CLI marker and enable inheritance for children."""

    source = list(arguments)
    if ISOLATED_FLAG in source:
        isolated_root()  # Validate before enabling inheritance.
        os.environ[ISOLATED_ENV] = "1"
        source = [argument for argument in source if argument != ISOLATED_FLAG]
    if is_isolated():
        isolated_root()
    cleaned = [argument for argument in source if argument != DEV_SESSION_FLAG]
    if len(cleaned) != len(source):
        os.environ[DEV_SESSION_ENV] = "1"
    return cleaned


def is_active() -> bool:
    return os.environ.get(DEV_SESSION_ENV) == "1"


def mark_command(command: Iterable[str]) -> List[str]:
    marked = list(command)
    if is_isolated() and ISOLATED_FLAG not in marked:
        marked.append(ISOLATED_FLAG)
    if is_active() and DEV_SESSION_FLAG not in marked:
        marked.append(DEV_SESSION_FLAG)
    return marked
