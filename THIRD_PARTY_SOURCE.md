# Third-party corresponding source

Release status: READY

The Windows binary bundles the open-source Qt for Python 6.11.1 runtime. The
application source is already available in this GPL-3.0-only repository, but a
formal binary release must also keep the exact Qt and PySide/Shiboken
corresponding source available from a location controlled by this project.

## Required source archives

### Qt 6.11.1

- File: `qt-everywhere-src-6.11.1.tar.xz`
- Official source: <https://download.qt.io/official_releases/qt/6.11/6.11.1/single/qt-everywhere-src-6.11.1.tar.xz>
- Size: 1,017,723,080 bytes
- SHA-256: `252acef8c5ae68074d91cadba2ee4a83465051bbb970dd26e8f0daa0f3904e03`
- Project-controlled release URL: <https://github.com/ZSTDJan/windows-remote-mic-app/releases/download/third-party-source-qt-6.11.1/qt-everywhere-src-6.11.1.tar.xz>

### Qt for Python / PySide6 6.11.1

- File: `pyside-setup-everywhere-src-6.11.1.tar.xz`
- Official source: <https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-6.11.1-src/pyside-setup-everywhere-src-6.11.1.tar.xz>
- Size: 17,963,432 bytes
- SHA-256: `6ffd9835bb0dd2c56f061d62f1616bb1707cfc0202b80e3165d6be087f3965e2`
- Project-controlled release URL: <https://github.com/ZSTDJan/windows-remote-mic-app/releases/download/third-party-source-qt-6.11.1/pyside-setup-everywhere-src-6.11.1.tar.xz>

## Distribution statement

The Windows build installs the official `PySide6-Essentials==6.11.1` and
`shiboken6==6.11.1` PyPI wheels and does not patch their source. PyInstaller
keeps Qt as replaceable DLLs and QML/plugin files under the application's
`_internal/PySide6` directory rather than statically linking them into the main
executable.

The corresponding-source delivery was verified on 2026-09-01:

1. both archives were uploaded to the project-controlled GitHub prerelease;
2. both public URLs were downloaded once after publication;
3. the downloaded sizes and SHA-256 values matched the records above.

Official Qt download links remain recorded as provenance. The project-controlled
release URLs above are the corresponding-source delivery used by the Windows
binary release gate.
