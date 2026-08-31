# PyInstaller spec for Remote Mic · RC003 (Windows source/build candidate).
#
# One-dir build (COLLECT), matching the layout pattern this project's
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

import sys
from pathlib import Path

block_cipher = None

RC003_ROOT = Path(SPECPATH).resolve().parent
SRC_ROOT = RC003_ROOT / "src"
REPO_ROOT = RC003_ROOT.parents[2]
REMOTE_PHOTO = REPO_ROOT / "Resources" / "RC003-remote-photo.png"
QML_SOURCE_DIR = SRC_ROOT / "ovb_rc003" / "qml"
APP_ICON_DIR = SRC_ROOT / "ovb_rc003" / "assets" / "icons"
APP_ICON = APP_ICON_DIR / "remote-mic.ico"
ELEMENT_NAVIGATION_SOURCE_DIR = RC003_ROOT / "scripts"
ELEMENT_NAVIGATION_SOURCE_FILES = (
    "element_navigation_prototype.py",
    "element_navigation_command_windows.py",
    "element_navigation_support.py",
    "element_navigation_windows_host.py",
    "element_targeting_core.py",
    "spatial_navigation_core.py",
)
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

# Import only the stdlib-only pin/runtime helper so the build contract has one
# authoritative filename and SHA-256. Source execution may omit the asset, but
# every frozen build must contain the exact pinned archive.
sys.path.insert(0, str(SRC_ROOT))
from ovb_rc003 import frida_hid_tap_runtime  # noqa: E402

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
# This places the photo under Resources/ inside the one-dir COLLECT output,
# which PyInstaller exposes at runtime as sys._MEIPASS/Resources/. A source
# checkout may still degrade if a user deletes the file after startup, but a
# frozen candidate is incomplete without the real button-layout reference.
datas.append((str(REMOTE_PHOTO), "Resources"))
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
    "ovb_rc003.single_instance",  # XRBM-021: imported lazily inside
    # __main__.py's _run_bridge(), same as the other lazily-imported
    # modules above.
    "frida",
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
    binaries=[],
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


def _is_ambient_icuuc(binary_entry):
    """Reject an unrelated ICU DLL discovered through the build PATH.

    Modern Windows provides its own ICU forwarder. PyInstaller may instead
    discover another application's ``icuuc.dll`` through PATH and copy it to
    the package root, where it shadows the Windows DLL and can make QtCore
    fail before the settings window starts. Keep a future PySide6-owned copy,
    but never bundle an ambient copy from another toolchain.
    """

    destination_name, source_path, _type_code = binary_entry
    if Path(destination_name).name.casefold() != "icuuc.dll":
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
    if not _is_ambient_icuuc(binary_entry)
    and not _is_unneeded_sounddevice_asio(binary_entry)
]

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="RemoteMicRC003",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=str(APP_ICON),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="RemoteMicRC003",
)
