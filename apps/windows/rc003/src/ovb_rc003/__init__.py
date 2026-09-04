"""Remote Mic - RC003 Windows client (source/build candidate).

Partially verified with a real RC003 on Windows. See this package's
README.md and TESTING.md for the exact verified paths and remaining
real-device checks.
"""

from pathlib import Path

__all__ = ["__version__"]

__version__ = Path(__file__).with_name("VERSION").read_text(encoding="ascii").strip()
if not __version__:
    raise RuntimeError("RC003 VERSION file is empty")
