"""Build-only inventory shared by compilation and freezing, never shipped.

Source files are authoritative; only the tiny package initializer and EXE
launchers stay as Python glue. Navigation uses the same sources as OrthoFocus.
"""
from __future__ import annotations
import ast
from pathlib import Path

NAVIGATION_MODULES = (
    "element_navigation_prototype", "element_navigation_command_windows",
    "element_navigation_support", "element_navigation_windows_host",
    "element_targeting_core", "spatial_navigation_core",
)
LEGACY_MODULES = (
    "ovb_rc003.hid_elevation_windows", "ovb_rc003.hid_helper_consumers",
)
# CPython's -m loader needs a Python code object. Keep only this generated
# bootstrap as Python; the complete original entrypoint is compiled.
MAIN_BOOTSTRAP = """import sys
from . import _native_main
if __name__ == "__main__":
    _native_main.main()
else:
    sys.modules[__name__] = _native_main
"""


def module_sources(root: Path) -> dict[str, Path]:
    result = {}
    for source in sorted((root / "src" / "ovb_rc003").rglob("*.py")):
        if source.name != "__init__.py":
            name = ".".join(source.relative_to(root / "src").with_suffix("").parts)
            if name == "ovb_rc003.__main__":
                name = "ovb_rc003._native_main"
            if name in result:
                raise ValueError(f"native module name collision: {name}")
            result[name] = source
    for name in NAVIGATION_MODULES:
        source = root / "scripts" / f"{name}.py"
        if not source.is_file():
            raise FileNotFoundError(source)
        result[name] = source
    return result


def import_closure(root: Path, seeds: list[str] | None = None,
                   excludes: tuple[str, ...] = ()) -> list[str]:
    """Capture imports before Cython hides them from the bytecode scanner.

Literal optional/lazy imports are included. Generated imports still belong in
the spec's explicit list. Helper closure follows only its own entry modules.
"""
    sources = module_sources(root)
    pending = list(sources if seeds is None else seeds)
    seen = set()
    while pending:
        name = pending.pop()
        if any(name == item or name.startswith(item + ".") for item in excludes):
            continue
        if name.startswith("ovb_rc003.") and name not in sources:
            continue  # e.g. 'from . import __version__' imports a value.
        if name in seen:
            continue
        seen.add(name)
        source = sources.get(name)
        if source is None:
            continue
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8-sig"))):
            if isinstance(node, ast.Import):
                pending.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    base = name.split(".")[:-node.level]
                    if node.module:
                        pending.append(".".join(base + node.module.split(".")))
                    else:
                        pending.extend(".".join(base + [a.name]) for a in node.names)
                elif node.module:
                    pending.append(node.module)
    return sorted(seen - {"__future__"})
