# Third-party notices

## remote-bridge-hub

- Project: `xxb26553663-star/remote-bridge-hub`
- Source: <https://github.com/xxb26553663-star/remote-bridge-hub>
- Reference revision: `8a93f321ac71a602300c6cd77f7256fa4b63068e`
- License: GNU General Public License v3.0 only (`GPL-3.0-only`)

The Xiaomi RC003 ATVV UUIDs, microphone command behavior, IMA/DVI ADPCM decoding
order, capability parsing, and HID usage mapping are protocol references for this
Windows client. The client does not include upstream customer data or commercial
branding.

The RC003 HID report tap also adapts the upstream Xiaomi Frida Gadget transport:
it observes the completed HidOverGatt read inside the paired Windows WUDF host so
that the `0xF1`, `0x80`, and `0x81` usages which Windows Raw Input drops can be
translated without guessing scan codes. The optional Gadget archive is fetched
only by the explicit `apps/windows/rc003/build/fetch-frida-gadget.ps1` script and
is verified against its pinned SHA-256 before use or extraction.

## Frida Gadget

- Project: `frida/frida`
- Source: <https://github.com/frida/frida>
- Release: `17.15.3` Windows x86_64 Gadget
- Asset: `frida-gadget-17.15.3-windows-x86_64.dll.xz`
- SHA-256: `B566D70189B6D551AD8F4E0BEA24DE08A3D4C0F559BB35B2BDB67D45182240C2`
- License: Frida core license
- License text: <https://raw.githubusercontent.com/frida/frida-core/main/COPYING>

The binary is not committed to this repository. If installed, the tap requests
UAC elevation only to inject the verified DLL into the validated RC003
`WUDFHost.exe`; declining UAC leaves the rest of the client usable.

## open-voice-bridge Windows implementation

- Project: `nijez/open-voice-bridge`
- Source: <https://github.com/nijez/open-voice-bridge>
- License: GNU General Public License v3.0 only (`GPL-3.0-only`)

The Windows RC003 implementation in `apps/windows/rc003` is adapted from the
upstream Windows client. Attribution and the changes made in this repository
are summarized in `apps/windows/rc003/ATTRIBUTION.md`.

## Python UIAutomation for Windows

- Project: `yinkaisheng/Python-UIAutomation-for-Windows`
- Version: `2.0.29`
- Source: <https://github.com/yinkaisheng/Python-UIAutomation-for-Windows>
- License: Apache License 2.0

Remote Mic uses this package only to read and operate the semantic Windows UI
Automation controls exposed by supported voice-program settings windows. It
does not inject code into those programs or read their private process memory.

## comtypes

- Project: `enthought/comtypes`
- Version: `1.4.16`
- Source: <https://github.com/enthought/comtypes>
- License: MIT

`comtypes` is the COM runtime dependency used by Python UIAutomation for
Windows.

## RC003 product photo

The RC003 product photo bundled as `RC003-remote-photo.png` was supplied by the user on 2026-07-17 for the physical-button mapping interface. It is preserved at its original 508 x 1030 aspect ratio. Copyright and trademark rights in the photo and depicted products remain with their respective owners; the GPL-3.0-only license for the program does not grant additional rights to this image or the Xiaomi marks.

## Noto Sans SC

- Project: `notofonts/noto-cjk`
- Source: <https://github.com/notofonts/noto-cjk>
- Bundled weights: Regular, Medium, SemiBold
- License: SIL Open Font License 1.1 (`OFL-1.1`)

The Windows settings interface bundles these font files unchanged for consistent
Simplified Chinese rendering. The copyright notice and full license text are
included at `apps/windows/rc003/src/ovb_rc003/qml/fonts/OFL.txt` and are copied
with the QML resource tree into frozen builds.

## VB-CABLE

- Project: `VB-Audio VB-CABLE`
- Source: <https://vb-audio.com/Cable/>
- Package used by the optional Windows helper: `VBCABLE_Driver_Pack45.zip`
- License: VB-Audio Donationware / the vendor's own terms; it is not GPL code

The Windows client does not modify, re-license, silently install, or select
VB-CABLE as the Windows default device. Audio is written only to the endpoint explicitly selected by the user; the client never changes the Windows system default input/output device. The optional bundle contains only the free Basic package, not the paid A+B/C+D products. The build helper
`apps/windows/rc003/build/fetch-vb-cable.ps1` downloads and hash-verifies the
official package only as an explicit build step. At runtime, installation is
available only after an explicit user click and a real Windows UAC prompt; the
Remote Mic process never runs with administrator privileges and never reports a driver install as successful merely because a process was launched.
