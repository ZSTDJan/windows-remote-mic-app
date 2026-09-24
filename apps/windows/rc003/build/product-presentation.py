"""Export the shared Windows presentation contract for build scripts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    source_root = args.source_root.resolve()
    version_path = source_root / "ovb_rc003" / "VERSION"
    if not version_path.is_file():
        raise SystemExit(f"required VERSION file not found: {version_path}")
    sys.path.insert(0, str(source_root))
    from ovb_rc003 import product_identity  # noqa: PLC0415

    version = product_identity.validate_version(
        version_path.read_text(encoding="ascii").strip()
    )
    main_metadata = product_identity.windows_main_version_metadata(version)
    helper_metadata = product_identity.windows_hid_helper_version_metadata(version)
    payload = {
        "schema_version": 1,
        "version": version,
        "presentation_label": product_identity.windows_presentation_label(version),
        "main_executable_name": product_identity.windows_executable_name(version),
        "main_executable_stem": product_identity.windows_executable_stem(version),
        "portable_folder_name": product_identity.windows_portable_folder_name(version),
        "runtime_directory_name": product_identity.WINDOWS_RUNTIME_DIRECTORY_NAME,
        "documentation_directory_name": product_identity.WINDOWS_DOCUMENTATION_DIRECTORY_NAME,
        "portable_readme_name": product_identity.WINDOWS_PORTABLE_README_NAME,
        "hid_helper_executable_name": product_identity.HID_HELPER_EXECUTABLE_NAME,
        "hid_helper_file_description": product_identity.HID_HELPER_FILE_DESCRIPTION,
        "main_file_description": main_metadata["file_description"],
        "product_name": main_metadata["product_name"],
        "main_original_filename": main_metadata["original_filename"],
        "helper_original_filename": helper_metadata["original_filename"],
    }
    print(json.dumps(payload, ensure_ascii=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
