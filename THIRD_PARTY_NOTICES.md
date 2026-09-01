# Third-party notices

This repository is licensed as `GPL-3.0-only`. The Windows binary also carries
the components listed below under their own licenses. Full license texts shipped
with the installer and portable archive are under `THIRD_PARTY_LICENSES/`.
Corresponding-source information for Qt for Python is recorded in
`THIRD_PARTY_SOURCE.md`.

## Frozen Python and audio runtime

### CPython 3.12.10

- Project: CPython
- Source: <https://www.python.org/downloads/source/>
- License: Python Software Foundation License
- Distributed form: Python interpreter DLLs and standard-library modules
- License text: `THIRD_PARTY_LICENSES/Python-3.12/LICENSE.txt`

### OpenSSL 3

- Project: OpenSSL
- Source: <https://github.com/openssl/openssl>
- License: Apache License 2.0
- Distributed form: OpenSSL runtime DLLs brought in by CPython and Qt
- License text: `THIRD_PARTY_LICENSES/OpenSSL-3/LICENSE.txt`

The exact OpenSSL file versions are checked from the final Windows artifact;
they may differ between the CPython and Qt runtime trees.

### PySide6 Essentials, Shiboken6 and Qt 6.11.1

- Packages: `PySide6-Essentials==6.11.1`, `shiboken6==6.11.1`
- Projects: Qt for Python and Qt
- Sources: <https://code.qt.io/cgit/pyside/pyside-setup.git/> and
  <https://code.qt.io/cgit/qt/qt5.git/>
- License choices published by Qt for Python: `LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only`
- Distributed form: PySide/Shiboken extension modules, replaceable Qt DLLs,
  plugins and QML modules in the one-directory application
- License texts: `THIRD_PARTY_LICENSES/Qt-PySide6-6.11.1/`

This project uses the open-source Qt for Python distribution and does not claim
a commercial Qt license. The application itself is GPL-3.0-only. Exact source
archives, hashes and the still-required project-controlled source location are
recorded in `THIRD_PARTY_SOURCE.md`.

### NumPy

- Package: `numpy==2.4.3`
- Source: <https://github.com/numpy/numpy>
- License expression: `BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0`
- Distributed form: Python modules and native numerical libraries
- Main license text: `THIRD_PARTY_LICENSES/NumPy-2.4.3/LICENSE.txt`

PyInstaller also preserves NumPy's complete package license tree under
`_internal/numpy-2.4.3.dist-info/licenses/` in the frozen application.

### python-sounddevice and PortAudio

- Package: `sounddevice==0.5.5`
- Source: <https://github.com/spatialaudio/python-sounddevice>
- License: MIT
- License text: `THIRD_PARTY_LICENSES/sounddevice-0.5.5/LICENSE.txt`
- Runtime library: PortAudio
- PortAudio source: <https://github.com/PortAudio/portaudio>
- PortAudio license: MIT
- PortAudio license text: `THIRD_PARTY_LICENSES/PortAudio/LICENSE.txt`

Only the normal non-ASIO PortAudio DLL is required and distributed. The
separately supplied ASIO-enabled DLL is excluded from the PyInstaller output.

### pywinrt projections

The following packages are all version 3.2.1 and licensed under MIT:

- `winrt-runtime==3.2.1`
- `winrt-Windows.Devices.Bluetooth==3.2.1`
- `winrt-Windows.Devices.Bluetooth.GenericAttributeProfile==3.2.1`
- `winrt-Windows.Devices.Enumeration==3.2.1`
- `winrt-Windows.Storage.Streams==3.2.1`
- `winrt-Windows.Foundation==3.2.1`
- `winrt-Windows.Foundation.Collections==3.2.1`

Source: <https://github.com/pywinrt/pywinrt>. License text:
`THIRD_PARTY_LICENSES/pywinrt-3.2.1/LICENSE.txt`.

### Frida Python bindings and Frida Gadget

- Package and release: `frida==17.15.3`
- Project: Frida
- Source: <https://github.com/frida/frida>
- License: wxWindows Library Licence, Version 3.1
- License text: `THIRD_PARTY_LICENSES/Frida-17.15.3/COPYING.txt`
- Gadget asset: `frida-gadget-17.15.3-windows-x86_64.dll.xz`
- Gadget SHA-256: `B566D70189B6D551AD8F4E0BEA24DE08A3D4C0F559BB35B2BDB67D45182240C2`

The Gadget archive is not committed to this repository. Candidate builds fetch
the pinned official asset and verify its hash before freezing it. Runtime
verifies the archive again before use.

### Windows UI Automation support

- Package: `uiautomation==2.0.29`
- Project: Python-UIAutomation-for-Windows
- Source: <https://github.com/yinkaisheng/Python-UIAutomation-for-Windows>
- License: Apache License 2.0
- License text: `THIRD_PARTY_LICENSES/uiautomation-2.0.29/LICENSE.txt`

- Package: `comtypes==1.4.16`
- Source: <https://github.com/enthought/comtypes>
- License: MIT
- License text: `THIRD_PARTY_LICENSES/comtypes-1.4.16/LICENSE.txt`

### Supporting Python packages

- `cffi==2.1.1`, MIT-0:
  `THIRD_PARTY_LICENSES/cffi-2.1.1/LICENSE.txt`
- `pycparser==3.0`, BSD-3-Clause:
  `THIRD_PARTY_LICENSES/pycparser-3.0/LICENSE.txt`
- `typing_extensions==4.16.0`, PSF-2.0:
  `THIRD_PARTY_LICENSES/typing_extensions-4.16.0/LICENSE.txt`

These packages are included only when reached by the frozen runtime import
graph. The final artifact remains the source of truth for what was distributed.

### PyInstaller

- Build package: `pyinstaller==6.21.0`
- Source: <https://github.com/pyinstaller/pyinstaller>
- License: GPL-2.0-or-later with the PyInstaller bootloader exception
- License text: `THIRD_PARTY_LICENSES/PyInstaller-6.21.0/COPYING.txt`

PyInstaller is used to build the application. Its bootloader exception permits
distribution of the resulting application under this project's GPL-3.0-only
license.

## Upstream protocol and implementation sources

### open-voice-bridge Windows implementation

- Project: `nijez/open-voice-bridge`
- Source: <https://github.com/nijez/open-voice-bridge>
- License: GNU General Public License v3.0 only (`GPL-3.0-only`)

The Windows RC003 implementation in `apps/windows/rc003` is adapted from the
upstream Windows client. Attribution and local changes are summarized in
`apps/windows/rc003/ATTRIBUTION.md`.

### remote-bridge-hub

- Project: `xxb26553663-star/remote-bridge-hub`
- Source: <https://github.com/xxb26553663-star/remote-bridge-hub>
- Reference revision: `8a93f321ac71a602300c6cd77f7256fa4b63068e`
- License: GNU General Public License v3.0 only (`GPL-3.0-only`)

The Xiaomi RC003 ATVV UUIDs, microphone command behavior, IMA/DVI ADPCM
decoding order, capability parsing and HID usage mapping are protocol
references for this Windows client. No upstream customer data or commercial
branding is included.

## Separately licensed assets and software

### RC003 product photo

The bundled file `Resources/RC003-remote-photo.png` was supplied by the user on
2026-07-17. The repository does not currently contain a written public
redistribution license from the photo copyright owner. This notice does not
grant that missing permission. Formal distribution remains blocked until the
status in `ASSET_LICENSES.md` is approved or the file is replaced with an asset
the project may redistribute.

### VB-CABLE

- Project: VB-Audio VB-CABLE
- Source and donation page: <https://vb-audio.com/Cable/>
- Package: `VBCABLE_Driver_Pack45.zip`
- License: VB-Audio Donationware / vendor terms; not GPL code

The `fetch-vb-cable.ps1` build helper downloads the official unmodified Basic
package and verifies its pinned SHA-256. The bundle never includes the paid
A+B/C+D products. The client does not re-license or silently install VB-CABLE,
and it never changes the Windows system default input/output device; audio is
written only to an explicitly selected endpoint. Installation starts only after
an explicit user action and Windows UAC confirmation. That action elevates only
the vendor installer; Remote Mic does not automatically elevate or change its
own current privileges, and it never reports a driver install as successful
merely because a process was launched. Users may obtain the same
package directly from VB-Audio and are encouraged to support the vendor through
its Donationware model.

## Microsoft runtime components

The frozen Windows application may include Microsoft Universal C Runtime and
Visual C++ runtime DLLs redistributed under Microsoft's applicable runtime
terms. They are not part of this project's GPL source code.
