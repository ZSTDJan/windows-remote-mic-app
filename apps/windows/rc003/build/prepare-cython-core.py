"""Build the selected RC003 permission modules as Cython extensions.

The normal source tree remains untouched. Candidate builds consume the staged
copy under ``build/cython-stage/src`` after the two original Python files in
that copy have been replaced by compiled extension modules.
"""

from __future__ import annotations

import importlib.machinery
import os
import shutil
import sys
from pathlib import Path

from Cython.Build import cythonize
from setuptools import Distribution, Extension
from setuptools.command.build_ext import build_ext


RC003_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = RC003_ROOT / "src"
STAGE_ROOT = RC003_ROOT / "build" / "cython-stage"
STAGE_SOURCE_ROOT = STAGE_ROOT / "src"
WORK_ROOT = RC003_ROOT / "build" / "cython-work"

CORE_MODULES = (
    "ovb_rc003.hid_elevation_windows",
    "ovb_rc003.hid_helper_consumers",
)

CYTHON_DIRECTIVES = {
    "language_level": 3,
    "annotation_typing": False,
    "binding": True,
    "embedsignature": True,
    "infer_types": False,
    "always_allow_keywords": True,
}


def _module_source(root: Path, module_name: str) -> Path:
    return root.joinpath(*module_name.split(".")).with_suffix(".py")


def _copy_source_tree() -> None:
    if not SOURCE_ROOT.is_dir():
        raise RuntimeError(f"source directory not found: {SOURCE_ROOT}")
    shutil.rmtree(STAGE_ROOT, ignore_errors=True)
    shutil.rmtree(WORK_ROOT, ignore_errors=True)
    STAGE_ROOT.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        SOURCE_ROOT,
        STAGE_SOURCE_ROOT,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyd"),
    )


def _compiled_extensions() -> list[Extension]:
    extensions = []
    for module_name in CORE_MODULES:
        source = _module_source(STAGE_SOURCE_ROOT, module_name)
        if not source.is_file():
            raise RuntimeError(f"Cython source not found: {source}")
        extensions.append(Extension(module_name, [str(source.relative_to(RC003_ROOT))]))
    return cythonize(
        extensions,
        build_dir=str(WORK_ROOT.relative_to(RC003_ROOT) / "c"),
        compiler_directives=CYTHON_DIRECTIVES,
        force=True,
        quiet=True,
    )


def _build_extensions(extensions: list[Extension]) -> None:
    distribution = Distribution({"name": "remote-mic-rc003-cython-core", "ext_modules": extensions})
    command = build_ext(distribution)
    command.build_lib = str(STAGE_SOURCE_ROOT.relative_to(RC003_ROOT))
    command.build_temp = str(WORK_ROOT.relative_to(RC003_ROOT) / "t")
    command.ensure_finalized()
    command.run()


def _verify_and_remove_staged_sources() -> None:
    suffixes = tuple(importlib.machinery.EXTENSION_SUFFIXES)
    for module_name in CORE_MODULES:
        source = _module_source(STAGE_SOURCE_ROOT, module_name)
        package_dir = source.parent
        stem = source.stem
        matches = [
            path
            for path in package_dir.glob(f"{stem}*")
            if path.is_file() and path.name.endswith(suffixes)
        ]
        if len(matches) != 1:
            raise RuntimeError(
                f"expected one compiled extension for {module_name}, found: {matches}"
            )
        source.unlink()
        if source.exists():
            raise RuntimeError(f"staged Python source was not removed: {source}")
        print(f"compiled {module_name}: {matches[0].relative_to(RC003_ROOT)}")


def main() -> int:
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError(
            "RC003 Cython candidate builds require Python 3.12; "
            f"got {sys.version_info.major}.{sys.version_info.minor}"
        )
    _copy_source_tree()
    previous_cwd = Path.cwd()
    try:
        # Relative compiler paths keep the generated MSVC command line below
        # Windows' practical path-length limit on ordinary developer machines.
        os.chdir(RC003_ROOT)
        extensions = _compiled_extensions()
        _build_extensions(extensions)
    finally:
        os.chdir(previous_cwd)
    _verify_and_remove_staged_sources()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
