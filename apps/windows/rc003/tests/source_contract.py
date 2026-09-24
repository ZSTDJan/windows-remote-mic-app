"""Read authoritative source for AST/text contracts, even in native tests.

Behavioral tests must still import the staged extensions. This helper is only
for checks whose subject is source text, not for substituting runtime code.
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def source_text(obj):
    module_name = getattr(obj, "__module__", None) or obj.__name__
    if module_name == "ovb_rc003._native_main":
        module_name = "ovb_rc003.__main__"
    path = ROOT / "src" / Path(*module_name.split(".")).with_suffix(".py")
    text = path.read_text(encoding="utf-8")
    if getattr(obj, "__module__", None) is None:
        return text
    tree = ast.parse(text)
    node = tree
    for part in obj.__qualname__.split("."):
        node = next(child for child in node.body
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                    and child.name == part)
    return ast.get_source_segment(text, node)


def entry_command(*args):
    """Use the same thin launcher as the EXE for an unfrozen native stage."""
    import os
    import sys
    stage = os.environ.get("RC003_NATIVE_STAGE")
    entry = [str(Path(stage) / "src/launcher.py")] if stage else ["-m", "ovb_rc003"]
    return [sys.executable, *entry, *args]
