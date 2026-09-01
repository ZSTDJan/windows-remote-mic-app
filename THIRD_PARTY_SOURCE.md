# Third-party corresponding source

Release status: BLOCKED

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
- Project-controlled release URL: PENDING

### Qt for Python / PySide6 6.11.1

- File: `pyside-setup-everywhere-src-6.11.1.tar.xz`
- Official source: <https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-6.11.1-src/pyside-setup-everywhere-src-6.11.1.tar.xz>
- Size: 17,963,432 bytes
- SHA-256: `6ffd9835bb0dd2c56f061d62f1616bb1707cfc0202b80e3165d6be087f3965e2`
- Project-controlled release URL: PENDING

## Distribution statement

The Windows build installs the official `PySide6-Essentials==6.11.1` and
`shiboken6==6.11.1` PyPI wheels and does not patch their source. PyInstaller
keeps Qt as replaceable DLLs and QML/plugin files under the application's
`_internal/PySide6` directory rather than statically linking them into the main
executable.

Before a formal candidate tag is built, the maintainer must:

1. upload both verified source archives to the same GitHub prerelease or another
   durable project-controlled location;
2. replace both `PENDING` entries above with the public URLs;
3. download those URLs once and verify the recorded SHA-256 values;
4. change `Release status` to `READY` and run
   `python apps/windows/rc003/build/check-release-readiness.py --enforce`.

Official Qt download links are recorded as provenance, not as a substitute for
the pending project-controlled source delivery.
