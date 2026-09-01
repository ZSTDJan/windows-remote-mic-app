"""Developer-session marker shared by source launch descendants."""

from __future__ import annotations

import os
from typing import Iterable, List


DEV_SESSION_FLAG = "--remote-mic-dev-session"
DEV_SESSION_ENV = "REMOTE_MIC_DEV_SESSION"


def consume_marker(arguments: Iterable[str]) -> List[str]:
    """Remove the private CLI marker and enable inheritance for children."""

    source = list(arguments)
    cleaned = [argument for argument in source if argument != DEV_SESSION_FLAG]
    if len(cleaned) != len(source):
        os.environ[DEV_SESSION_ENV] = "1"
    return cleaned


def is_active() -> bool:
    return os.environ.get(DEV_SESSION_ENV) == "1"


def mark_command(command: Iterable[str]) -> List[str]:
    marked = list(command)
    if is_active() and DEV_SESSION_FLAG not in marked:
        marked.append(DEV_SESSION_FLAG)
    return marked
