# PyInstaller spec for Remote Mic · RC003 (Windows source/build candidate).
#
# One-dir desktop build (COLLECT) plus one self-contained, narrow HID helper,
# matching the layout pattern this project's
# upstream reference uses for its own standalone products, minus everything
# out of scope for this candidate: no other-device (T1/V60) code, and no
# licensing/DRM modules (none exist in this tree to begin with). The pinned,
# hash-verified Frida archive is required for every frozen build so a package
# cannot silently lose the HID path for Back and volume buttons. The explicit
# fetch step supplies it; this spec verifies it again and never downloads it.
#
# Build with (inside a Windows virtual environment with requirements-dev.txt
# installed):
#   pyinstaller build/RemoteMicRC003.spec
#
# This produces an UNSIGNED candidate under dist/RemoteMicRC003/. Real
# code signing is out of scope for this source/build candidate.

import os
import sys
import importlib.machinery
from pathlib import Path

from PyInstaller.utils.win32 import versioninfo
from PyInstaller.config import CONF

block_cipher = None

RC003_ROOT = Path(SPECPATH).resolve().parent
SRC_ROOT = RC003_ROOT / "src"
source_root_override = os.environ.get("RC003_BUILD_SOURCE_ROOT", "").strip()
if source_root_override:
    SRC_ROOT = Path(source_root_override).resolve()
if not SRC_ROOT.is_dir():
    raise SystemExit(f"required build source directory is missing: {SRC_ROOT}")
REPO_ROOT = RC003_ROOT.parents[2]
REMOTE_PHOTO = REPO_ROOT / "Resources" / "RC003-remote-photo.png"
CHROMECAST_PHOTO = REPO_ROOT / "Resources" / "Chromecast-remote-photo.png"
QML_SOURCE_DIR = SRC_ROOT / "ovb_rc003" / "qml"
APP_ICON_DIR = SRC_ROOT / "ovb_rc003" / "assets" / "icons"
APP_ICON = APP_ICON_DIR / "remote-mic.ico"
ELEMENT_NAVIGATION_SOURCE_DIR = RC003_ROOT / "scripts"
sys.path.insert(0, str(RC003_ROOT / "build"))
from native_inventory import NAVIGATION_MODULES, import_closure
ELEMENT_NAVIGATION_SOURCE_FILES = tuple(name + ".py" for name in NAVIGATION_MODULES)

LEGACY_COMPARISON = os.environ.get("RC003_BUILD_LEGACY_COMPARISON") == "1"
if LEGACY_COMPARISON:
    # A private same-source benchmark cannot masquerade as a deliverable.
    if not all(Path(path).resolve().is_relative_to(RC003_ROOT / ".build")
               for path in (DISTPATH, CONF["workpath"], SRC_ROOT)):
        raise SystemExit("legacy comparison is restricted to private .build caches")
NATIVE_STAGE = (SRC_ROOT.parent / "native-build.json").is_file() and not LEGACY_COMPARISON
if source_root_override:
    from check_native import verify_stage
    verify_stage(SRC_ROOT.parent, require_full=not LEGACY_COMPARISON)
elif LEGACY_COMPARISON:
    raise SystemExit("legacy comparison requires an explicit staged source root")
navigation_binaries = []
if NATIVE_STAGE:
    ELEMENT_NAVIGATION_SOURCE_DIR = SRC_ROOT.parent / "scripts"
DEVICE_PROFILES_DIR = REPO_ROOT / "device-profiles"
# XRBM-031: build/fetch-vb-cable.ps1 (a REQUIRED step in both
# build-candidate.ps1 and windows-rc003-ci.yml, run before this spec) writes
# the hash-verified official VB-CABLE base package here. Bundled unmodified
# as application data (never re-verified/re-hashed at build time - only at
# RUNTIME, independently, by vb_cable_bundle.verify_bundle() before any
# extraction) so the frozen build's optional driver-helper page works fully
# offline on the end-user machine.
VB_CABLE_BUNDLE_ZIP = RC003_ROOT / "build" / "third_party" / "VBCABLE_Driver_Pack45.zip"
FRIDA_ASSET_DIR = SRC_ROOT / "ovb_rc003" / "frida_assets"
VERSION_FILE = SRC_ROOT / "ovb_rc003" / "VERSION"
HID_HELPER_NAME = "RemoteMicRC003HidHelper"

# Import only the stdlib-only pin/runtime helper so the build contract has one
# authoritative filename and SHA-256. Source execution may omit the asset, but
# every frozen build must contain the exact pinned archive.
if not VERSION_FILE.is_file():
    raise SystemExit(f"required application version file is missing: {VERSION_FILE}")
sys.path.insert(0, str(SRC_ROOT))
from ovb_rc003 import frida_hid_tap_runtime, product_identity  # noqa: E402

APP_VERSION = product_identity.validate_version(
    VERSION_FILE.read_text(encoding="ascii").strip()
)
MAIN_EXECUTABLE_NAME = product_identity.windows_executable_name(APP_VERSION)
MAIN_EXECUTABLE_STEM = product_identity.windows_executable_stem(APP_VERSION)
WINDOWS_RUNTIME_DIRECTORY_NAME = product_identity.WINDOWS_RUNTIME_DIRECTORY_NAME
HID_HELPER_RELATIVE_PATH = (
    Path(WINDOWS_RUNTIME_DIRECTORY_NAME) / f"{HID_HELPER_NAME}.exe"
)


def _version_resource(metadata):
    fixed_version = metadata["fixed_file_version"]
    return versioninfo.VSVersionInfo(
        ffi=versioninfo.FixedFileInfo(
            filevers=fixed_version,
            prodvers=fixed_version,
            mask=0x3F,
            flags=0x2 if metadata["prerelease"] else 0x0,
            OS=0x40004,
            fileType=0x1,
            subtype=0x0,
            date=(0, 0),
        ),
        kids=[
            versioninfo.StringFileInfo(
                [
                    versioninfo.StringTable(
                        "080404B0",
                        [
                            versioninfo.StringStruct(
                                "FileDescription", metadata["file_description"]
                            ),
                            versioninfo.StringStruct(
                                "FileVersion", metadata["file_version"]
                            ),
                            versioninfo.StringStruct(
                                "InternalName", metadata["internal_name"]
                            ),
                            versioninfo.StringStruct(
                                "OriginalFilename", metadata["original_filename"]
                            ),
                            versioninfo.StringStruct(
                                "ProductName", metadata["product_name"]
                            ),
                            versioninfo.StringStruct(
                                "ProductVersion", metadata["product_version"]
                            ),
                        ],
                    )
                ]
            ),
            versioninfo.VarFileInfo(
                [versioninfo.VarStruct("Translation", [2052, 1200])]
            ),
        ],
    )


MAIN_VERSION_INFO = _version_resource(
    product_identity.windows_main_version_metadata(APP_VERSION)
)
HID_HELPER_VERSION_INFO = _version_resource(
    product_identity.windows_hid_helper_version_metadata(APP_VERSION)
)

FRIDA_GADGET_ARCHIVE = (
    FRIDA_ASSET_DIR / frida_hid_tap_runtime.GADGET_ARCHIVE_NAME
)
if not FRIDA_GADGET_ARCHIVE.is_file():
    raise SystemExit(
        "required verified Frida Gadget archive is missing; run "
        "build/fetch-frida-gadget.ps1 before PyInstaller"
    )
frida_archive_hash = frida_hid_tap_runtime.sha256_file(FRIDA_GADGET_ARCHIVE)
if frida_archive_hash != frida_hid_tap_runtime.GADGET_ARCHIVE_SHA256:
    raise SystemExit(
        "Frida Gadget archive SHA-256 mismatch: "
        f"expected {frida_hid_tap_runtime.GADGET_ARCHIVE_SHA256}, "
        f"got {frida_archive_hash}"
    )

if not REMOTE_PHOTO.is_file():
    raise SystemExit(
        "required 小米遥控器2 Pro photo is missing: "
        f"{REMOTE_PHOTO}"
    )

datas = []
datas.append((str(VERSION_FILE), "ovb_rc003"))
# This places the photo under Resources/ inside the one-dir COLLECT output,
# which PyInstaller exposes at runtime as sys._MEIPASS/Resources/. A source
# checkout may still degrade if a user deletes the file after startup, but a
# frozen candidate is incomplete without the real button-layout reference.
datas.append((str(REMOTE_PHOTO), "Resources"))
if not CHROMECAST_PHOTO.is_file():
    raise SystemExit(f"required Chromecast photo is missing: {CHROMECAST_PHOTO}")
datas.append((str(CHROMECAST_PHOTO), "Resources"))
if QML_SOURCE_DIR.is_dir():
    # XRBM-030: the settings window's entire QML source tree is made of real
    # files on disk, not a Python module - PyInstaller's Analysis never
    # discovers them on its own, and no PySide6 hook bundles third-party QML
    # trees
    # (only Qt's OWN Quick Controls/QML plugin assets, handled automatically
    # by PyInstaller's bundled PySide6 hooks). Collected under
    # "ovb_rc003_qml" inside the COLLECT output, matching
    # qt_settings_app.py's ``_qml_directory()``, which looks under
    # ``sys._MEIPASS / "ovb_rc003_qml"`` in a frozen build - same
    # sys._MEIPASS-relative reasoning as the photo above (see
    # resources.py's module docstring).
    datas.append((str(QML_SOURCE_DIR), "ovb_rc003_qml"))
if APP_ICON_DIR.is_dir():
    datas.append((str(APP_ICON_DIR), "app_icons"))
for source_name in ELEMENT_NAVIGATION_SOURCE_FILES:
    if NATIVE_STAGE:
        stem = Path(source_name).stem
        matches = [ELEMENT_NAVIGATION_SOURCE_DIR / (stem + suffix)
                   for suffix in importlib.machinery.EXTENSION_SUFFIXES
                   if (ELEMENT_NAVIGATION_SOURCE_DIR / (stem + suffix)).is_file()]
        if len(matches) != 1:
            raise SystemExit(f"required native navigation module is missing: {stem}")
        navigation_binaries.append((str(matches[0]), "element_navigation"))
        continue
    source_path = ELEMENT_NAVIGATION_SOURCE_DIR / source_name
    if not source_path.is_file():
        raise SystemExit(f"required element-navigation source is missing: {source_path}")
    datas.append((str(source_path), "element_navigation"))
if DEVICE_PROFILES_DIR.is_dir():
    # The exact repository JSON files are the runtime catalog for Windows. The
    # frozen loader reads them from
    # sys._MEIPASS/device-profiles and fails closed if they are absent or
    # invalid; no generated/hard-coded duplicate is bundled.
    datas.append((str(DEVICE_PROFILES_DIR), "device-profiles"))
if VB_CABLE_BUNDLE_ZIP.is_file():
    # Collected under "vb_cable_bundle" inside the COLLECT output, matching
    # vb_cable_bundle.py's _candidate_bundle_paths(), which looks under
    # sys._MEIPASS / "vb_cable_bundle" in a frozen build - same
    # sys._MEIPASS-relative reasoning as the photo/qml entries above. A
    # missing file here (e.g. a local `pyinstaller` invocation that skipped
    # fetch-vb-cable.ps1) is not a spec-time error - build-candidate.ps1 and
    # windows-rc003-ci.yml are what make fetching it a REQUIRED gate before
    # this spec ever runs for a real candidate build; this spec itself stays
    # defensive/optional, matching the existing photo/qml pattern above.
    datas.append((str(VB_CABLE_BUNDLE_ZIP), "vb_cable_bundle"))
datas.append((str(FRIDA_GADGET_ARCHIVE), "ovb_rc003/frida_assets"))

hiddenimports = [
    # Imports made inside the two Cython extension modules are opaque to
    # PyInstaller's Python bytecode scanner. Keep their stdlib closure
    # explicit so both the main program and the narrow helper can initialize
    # the compiled modules without the original .py files.
    "argparse",
    "contextlib",
    "ctypes",
    "ctypes.wintypes",
    "dataclasses",
    "enum",
    "hashlib",
    "html",
    "json",
    "os",
    "pathlib",
    "re",
    "shutil",
    "stat",
    "subprocess",
    "sys",
    "tempfile",
    "threading",
    "time",
    "typing",
    "xml.etree.ElementTree",
    "ovb_rc003.app",
    "ovb_rc003.device_catalog",  # XRBM-036: multi-device settings/runtime gate
    "ovb_rc003.settings_ui",
    "ovb_rc003.element_navigation_control_windows",
    "ovb_rc003.element_navigation_runtime",
    "ovb_rc003.qt_settings_app",  # XRBM-030
    "ovb_rc003.windows_diagnostics",  # XRBM-031
    "ovb_rc003.vb_cable_bundle",  # XRBM-031
    "ovb_rc003.voice_hotkey_sync_windows",
    "ovb_rc003.voice_program_manager",
    "ovb_rc003.ble_transport_winrt",
    "ovb_rc003.raw_input_windows",
    "ovb_rc003.audio_playback",
    "ovb_rc003.win32_input",
    "ovb_rc003.connection_supervisor",
    "ovb_rc003.doubao_rpc",
    "ovb_rc003.frida_compat",
    "ovb_rc003.frida_hid_tap_runtime",
    "ovb_rc003.frida_hid_tap_injector",
    "ovb_rc003.hid_elevation_windows",
    "ovb_rc003.hid_helper_consumers",
    "ovb_rc003.product_identity",
    # Chromecast Remote runs several child-process and lazy-import paths.
    # Keep the whole first-party closure explicit: the contract test below
    # derives this inventory from every chromecast_*.py source file, so a new
    # runtime module cannot silently miss the frozen application.
    "ovb_rc003.chromecast_buttons",
    "ovb_rc003.chromecast_channel",
    "ovb_rc003.chromecast_client",
    "ovb_rc003.chromecast_runtime",
    "ovb_rc003.chromecast_device_windows",
    "ovb_rc003.chromecast_diagnostics_windows",
    "ovb_rc003.chromecast_doubao_handsfree",
    "ovb_rc003.chromecast_etw_windows",
    "ovb_rc003.chromecast_hid_tap_windows",
    "ovb_rc003.chromecast_hid_worker",
    "ovb_rc003.chromecast_host_activity",
    "ovb_rc003.chromecast_observation",
    "ovb_rc003.chromecast_pipe_windows",
    "ovb_rc003.chromecast_voice",
    "ovb_rc003.chromecast_voice_host",
    "ovb_rc003.voice_audio_session",
    "ovb_rc003.voice_shortcut_session",
    "ovb_rc003.chromecast_voice_receiver",
    "ovb_rc003.chromecast_wetype_finish",
    "ovb_rc003.chromecast_wetype_toggle",
    "ovb_rc003.chromecast_worker",
    "ovb_rc003.single_instance",  # XRBM-021: imported lazily inside
    # __main__.py's _run_bridge(), same as the other lazily-imported
    # modules above.
    "frida",
    "pefile",
    "uiautomation",
    "comtypes",
    "comtypes.client",
    # Optional runtime dependencies, imported lazily inside functions in
    # audio_output.py/audio_playback.py/ble_transport_winrt.py, which
    # PyInstaller's static analysis cannot always auto-detect:
    "sounddevice",
    "numpy",
    "winrt.windows.devices.bluetooth",
    "winrt.windows.devices.bluetooth.genericattributeprofile",
    "winrt.windows.devices.enumeration",
    "winrt.windows.storage.streams",
    # XRBM-024: find_all_async_aqs_filter()'s returned IAsyncOperation and
    # DeviceInformationCollection's iterator both come from these two
    # projections at runtime (see requirements.txt's XRBM-024 comment) -
    # PyInstaller's static analysis cannot see that dependency because
    # ble_transport_winrt.py never imports these modules by name, so they
    # must be listed explicitly or the frozen build passes analysis and
    # then crashes on first real BLE discovery.
    "winrt.windows.foundation",
    "winrt.windows.foundation.collections",
    # XRBM-030: qt_settings_app.py imports these lazily inside a function
    # body (so importing the module itself never requires PySide6 - see its
    # docstring), which PyInstaller's static import-graph analysis follows
    # regardless of the surrounding try/except, but listed explicitly here
    # too since QtQuickControls2/the FluentWinUI3 style module in
    # particular is loaded through Qt's own plugin system rather than a
    # plain Python import graph PyInstaller can always trace.
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "PySide6.QtQuickControls2",
    "PySide6.QtSvg",
    "PySide6.QtWidgets",
]

# Native imports cannot be discovered from bytecode. Keep runtime-generated
# imports above, and derive ordinary imports from the original source inventory.
if NATIVE_STAGE:
    hiddenimports = sorted(set(hiddenimports) | (set(import_closure(RC003_ROOT))
                                                - set(NAVIGATION_MODULES)))

a = Analysis(
    # XRBM-021: analyze the standalone src/launcher.py, NOT the package's
    # own src/ovb_rc003/__main__.py. PyInstaller treats its entry script as
    # a top-level module with no parent package, so __main__.py's
    # package-relative imports (correct for `python -m ovb_rc003`) raised
    # "attempted relative import with no known parent package" the moment
    # the previously-built frozen executable actually ran - see the red
    # baseline in the XRBM-021 task book and
    # tests/test_build_artifacts.py::LauncherEntryPointTests. launcher.py
    # instead does one absolute import (`from ovb_rc003.__main__ import
    # main`), which needs no parent package.
    [str(SRC_ROOT / "launcher.py")],
    pathex=[str(SRC_ROOT)],
    binaries=navigation_binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Defensive: this tree never contains these, but excluding them keeps
        # the boundary explicit if the spec is ever copy-pasted elsewhere.
        "bridges.t1",
        "bridges.hanvon",
        "licensing",
        "customer_license",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)


def _is_ambient_icu(binary_entry):
    """Reject unrelated ICU DLLs discovered through the build PATH.

    Modern Windows provides its own ICU forwarder. PyInstaller may instead
    discover another application's ``icuuc.dll`` through PATH, then also
    collect its companion ``icuinXX.dll`` / ``icudtXX.dll`` files. Copying
    that foreign set can make QtCore fail before the settings window starts,
    while leaving only a companion file wastes tens of megabytes. Keep a
    future PySide6-owned copy, but never bundle ambient ICU files from another
    toolchain.
    """

    destination_name, source_path, _type_code = binary_entry
    destination_path = Path(destination_name)
    stem = destination_path.stem.casefold()
    is_icu_runtime = any(
        stem == prefix or (
            stem.startswith(prefix) and stem[len(prefix):].isdigit()
        )
        for prefix in ("icudt", "icuin", "icuuc")
    )
    if destination_path.suffix.casefold() != ".dll" or not is_icu_runtime:
        return False
    return "pyside6" not in {
        part.casefold() for part in Path(source_path).parts
    }


def _is_unneeded_sounddevice_asio(binary_entry):
    """Exclude the optional ASIO build that the application never selects.

    python-sounddevice's Windows wheel carries both a normal PortAudio DLL and
    a second ASIO-enabled DLL. The latter expands the redistribution terms but
    provides no supported host API in this application.
    """

    destination_name, source_path, _type_code = binary_entry
    return (
        Path(destination_name).name.casefold() == "libportaudio64bit-asio.dll"
        or Path(source_path).name.casefold() == "libportaudio64bit-asio.dll"
    )


a.binaries = [
    binary_entry
    for binary_entry in a.binaries
    if not _is_ambient_icu(binary_entry)
    and not _is_unneeded_sounddevice_asio(binary_entry)
]

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=MAIN_EXECUTABLE_STEM,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=str(APP_ICON),
    version=MAIN_VERSION_INFO,
    uac_admin=False,
    contents_directory=WINDOWS_RUNTIME_DIRECTORY_NAME,
)

# The helper is deliberately a one-file executable because setup copies this
# executable alone into Program Files. It contains only the fixed task
# lifecycle, WUDFHost validation/injection code, and the pinned Gadget asset;
# It reads only the validated remote-selection digest from user configuration;
# UI, BLE scans, audio and configuration normalization dependencies are excluded.
HELPER_EXCLUDES = (
    "PySide6", "numpy", "sounddevice", "winrt", "uiautomation",
    "ovb_rc003.app", "ovb_rc003.qt_settings_app", "ovb_rc003.ble_transport_winrt",
    "ovb_rc003.audio_playback", "ovb_rc003.windows_diagnostics",
    "ovb_rc003.settings_ui", "ovb_rc003.key_mapping",
    "ovb_rc003.voice_hotkey_sync_windows", "ovb_rc003.voice_program_manager",
)
helper_native_imports = import_closure(
    RC003_ROOT, ["ovb_rc003.hid_elevation_windows", "ovb_rc003.frida_hid_tap_injector"],
    excludes=HELPER_EXCLUDES,
) if NATIVE_STAGE else []
helper_a = Analysis(
    [str(SRC_ROOT / "hid_helper_launcher.py")],
    pathex=[str(SRC_ROOT)],
    binaries=[],
    datas=[
        (str(VERSION_FILE), "ovb_rc003"),
        (str(FRIDA_GADGET_ARCHIVE), "ovb_rc003/frida_assets"),
    ],
    hiddenimports=[
        "argparse",
        "contextlib",
        "ctypes",
        "ctypes.wintypes",
        "dataclasses",
        "hashlib",
        "html",
        "json",
        "os",
        "pathlib",
        "re",
        "shutil",
        "stat",
        "subprocess",
        "sys",
        "tempfile",
        "time",
        "typing",
        "xml.etree.ElementTree",
        "ovb_rc003.hid_elevation_windows",
        "ovb_rc003.product_identity",
        "ovb_rc003.remote_selection",
        "ovb_rc003.config",
        "ovb_rc003.frida_hid_tap_injector",
        "ovb_rc003.frida_hid_tap_runtime",
        "ovb_rc003.single_instance",
        "comtypes",
        "comtypes.client",
    ] + helper_native_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=list(HELPER_EXCLUDES),
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

helper_pyz = PYZ(helper_a.pure, helper_a.zipped_data, cipher=block_cipher)

helper_exe = EXE(
    helper_pyz,
    helper_a.scripts,
    helper_a.binaries,
    helper_a.datas,
    [],
    name=HID_HELPER_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=str(APP_ICON),
    version=HID_HELPER_VERSION_INFO,
    uac_admin=False,
)

# Keep the privileged implementation available to setup and repair flows
# without presenting it beside the only user-facing executable. The helper
# remains a self-contained one-file EXE; only its distribution location moves.
helper_collect_toc = [
    (str(HID_HELPER_RELATIVE_PATH), helper_exe.name, "EXECUTABLE"),
    *helper_exe.dependencies,
]

coll = COLLECT(
    exe,
    helper_collect_toc,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="RemoteMicRC003",
)
