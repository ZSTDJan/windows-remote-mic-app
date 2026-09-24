"""Compile business modules in an isolated, incremental build cache.

Only staged Python files are removed. --scope legacy is for same-source
performance comparisons, not the production build. C/object files never ship.
"""
from __future__ import annotations
import argparse
import hashlib
import importlib.machinery
import json
import os
import shutil
import sys
from pathlib import Path
import Cython
import setuptools
from Cython.Build import cythonize
from setuptools import Distribution, Extension
from setuptools.command.build_ext import build_ext
from setuptools._distutils.ccompiler import new_compiler
from native_inventory import LEGACY_MODULES, MAIN_BOOTSTRAP, module_sources

RC003_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = RC003_ROOT / "src"
STAGE_ROOT = RC003_ROOT / "build" / "cython-stage"
WORK_ROOT = RC003_ROOT / "build" / "cython-work"
CYTHON_DIRECTIVES = {
    "language_level": 3,
    "annotation_typing": False,
    "binding": True,
    "embedsignature": True,
    "infer_types": False,
    "always_allow_keywords": True,
}


def compiler_identity() -> dict:
    compiler = new_compiler()
    if hasattr(compiler, "initialize"):
        compiler.initialize()
    native_tools = {}
    for name in ("cc", "linker"):
        path = Path(getattr(compiler, name, ""))
        if path.is_file():
            native_tools[name] = [str(path), hashlib.sha256(path.read_bytes()).hexdigest()]
    return {"python": sys.version, "cython": Cython.__version__,
            "setuptools": setuptools.__version__, "directives": CYTHON_DIRECTIVES,
            "native_tools": native_tools,
            "flags": {key: os.environ.get(key, "") for key in
                      ("CL", "_CL_", "LINK", "CFLAGS", "CPPFLAGS", "LDFLAGS")}}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", choices=("full", "legacy"), default="full")
    parser.add_argument("--stage", type=Path, default=STAGE_ROOT)
    parser.add_argument("--work", type=Path, default=WORK_ROOT)
    parser.add_argument("--jobs", type=int, default=2)
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError("native builds require Python 3.12")
    for directory in (args.stage, args.work):
        resolved = directory.resolve()
        if not any(resolved.is_relative_to(RC003_ROOT / parent)
                   and resolved != RC003_ROOT / parent for parent in ("build", ".build")):
            raise ValueError(f"not a build-cache subdirectory: {directory}")
    stage, work = args.stage.resolve(), args.work.resolve()
    if stage.is_relative_to(work) or work.is_relative_to(stage):
        raise ValueError("stage and work caches must not overlap")
    sources = module_sources(RC003_ROOT)
    source_hashes = {name: hashlib.sha256(path.read_bytes()).hexdigest()
                     for name, path in sources.items()}
    compiler = compiler_identity()
    compiler_stamp = work / "compiler.json"
    force = (not compiler_stamp.is_file()
             or json.loads(compiler_stamp.read_text(encoding="utf-8")) != compiler)
    receipt = stage / "native-build.json"
    previous_records = {}
    if receipt.is_file():
        try:
            previous_records = json.loads(receipt.read_text(encoding="utf-8"))["modules"]
        except (ValueError, KeyError):
            pass  # A broken receipt forces fresh conversion, never acceptance.
    receipt.unlink(missing_ok=True)
    # These are disposable staged inputs, never the repository sources.
    # Remove old bytecode and deleted/renamed source files before copying.
    for pattern in ("*.py", "*.pyc"):
        for old in stage.rglob(pattern):
            old.unlink()
    for origin, target in (
        (SOURCE_ROOT, stage / "src"),
        *((RC003_ROOT.parents[2] / name, stage / name)
          for name in ("Resources", "device-profiles")),
    ):
        for old in target.rglob("*"):
            if (old.is_file() and old.suffix != ".pyd"
                    and not (origin / old.relative_to(target)).is_file()):
                old.unlink()
    shutil.copytree(SOURCE_ROOT, stage / "src", dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyd"))
    for resource_name in ("Resources", "device-profiles"):
        shutil.copytree(RC003_ROOT.parents[2] / resource_name,
                        stage / resource_name, dirs_exist_ok=True)
    names = list(sources) if args.scope == "full" else list(LEGACY_MODULES)
    navigation = stage / "scripts"
    navigation.mkdir(parents=True, exist_ok=True)
    for name, source in sources.items():
        if not name.startswith("ovb_rc003."):
            shutil.copy2(source, navigation / source.name)
    staged = {name: (stage / "src" / Path(*name.split(".")).with_suffix(".py")
                     if name.startswith("ovb_rc003.") else navigation / f"{name}.py")
              for name in names}
    if args.scope == "full":
        shutil.copy2(sources["ovb_rc003._native_main"], staged["ovb_rc003._native_main"])
        (stage / "src/ovb_rc003/__main__.py").write_text(MAIN_BOOTSTRAP, encoding="utf-8")
    expected_stems = {path.stem for path in staged.values()}
    for native in stage.rglob("*.pyd"):
        if native.name.split(".")[0] not in expected_stems:
            native.unlink()
    previous = Path.cwd()
    failures, extensions = [], []
    try:
        os.chdir(RC003_ROOT)
        for name, source in staged.items():
            try:
                previous_record = previous_records.get(name, {})
                force_module = (force or
                                previous_record.get("source_sha256") != source_hashes[name])
                native_candidates = [source.with_name(source.stem + suffix)
                                     for suffix in importlib.machinery.EXTENSION_SUFFIXES
                                     if source.with_name(source.stem + suffix).is_file()]
                if (len(native_candidates) != 1 or
                        hashlib.sha256(native_candidates[0].read_bytes()).hexdigest()
                        != previous_record.get("sha256")):
                    force_module = True
                extensions.extend(cythonize(
                    [Extension(name, [str(source.relative_to(RC003_ROOT))])],
                    build_dir=str(work.relative_to(RC003_ROOT) / "c"),
                    compiler_directives=CYTHON_DIRECTIVES, quiet=True,
                    force=force_module,
                ))
            except Exception as exc:
                failures.append(f"{name}: {type(exc).__name__}: {exc}")
        if failures:
            raise RuntimeError("Cython failures:\n" + "\n".join(failures))
        distribution = Distribution({"name": "remote-mic-native", "ext_modules": extensions})
        command = build_ext(distribution)
        command.build_lib = str((stage / "src").relative_to(RC003_ROOT))
        command.build_temp = str(work.relative_to(RC003_ROOT) / "objects")
        command.parallel = max(1, args.jobs)
        command.force = force
        command.ensure_finalized()
        command.run()
    finally:
        os.chdir(previous)
    records = {}
    if source_hashes != {name: hashlib.sha256(path.read_bytes()).hexdigest()
                         for name, path in module_sources(RC003_ROOT).items()}:
        raise RuntimeError("business sources changed during compilation; retry required")
    for name, source in staged.items():
        output_dir = stage / "src" / Path(*name.split(".")[:-1])
        matches = [output_dir / (name.split(".")[-1] + suffix)
                   for suffix in importlib.machinery.EXTENSION_SUFFIXES]
        matches = [path for path in matches if path.is_file()]
        if len(matches) != 1:
            raise RuntimeError(f"expected one compiled extension: {name}: {matches}")
        native = matches[0]
        if not name.startswith("ovb_rc003."):
            target = navigation / native.name
            shutil.move(str(native), target)
            native = target
        source.unlink()
        records[name] = {
            "source_sha256": source_hashes[name],
            "output": native.relative_to(stage).as_posix(),
            "sha256": hashlib.sha256(native.read_bytes()).hexdigest(),
        }
        print(f"compiled {name}", flush=True)
    work.mkdir(parents=True, exist_ok=True)
    compiler_stamp.write_text(json.dumps(compiler), encoding="utf-8")
    receipt.write_text(json.dumps({"scope": args.scope, "python": sys.version,
                                  "compiler": compiler,
                                  "modules": records}, indent=2), encoding="utf-8")
    print(f"native stage verified: {len(records)} modules", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
