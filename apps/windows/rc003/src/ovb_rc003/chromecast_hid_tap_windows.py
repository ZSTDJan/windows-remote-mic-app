"""Read selected Chromecast HID notifications when BTHPORT omits report bytes.

The callback is private Windows code. A matching module hash or matching public
PDB is required before attaching. An adapter address match is required before
any report is delivered; this module never modifies the Windows report.
"""
from __future__ import annotations

import ctypes
from contextlib import contextmanager
from contextvars import ContextVar
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import struct
import subprocess
import threading
import time
import uuid

from . import remote_layout, remote_selection
from .chromecast_observation import HID_CONSTRUCTOR_PROBE_BYTES, HID_COUNTS, HID_ERROR_TYPES, valid_record


MODULE = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "drivers" / "umdf" / "microsoft.bluetooth.profiles.hidovergatt.dll"
# Exact x64 driver contents, not the (often unchanged) FileVersion string.
# Values are (OnReportValueChanged RVA, Bluetooth address offset in this).
_KNOWN_LAYOUTS = {
    "8a18571ddfa447bd590354ccdf7800b815a098806359ab195d395158f9d07cc5": (0x12FCC, 24),
    "de2b7ff2a61d50473cfe1e00ec012e99bda9361d563249ef93dce59f576e9936": (0x1580C, 8),
    "372c3628e3366c18152199eb8fb554d27bb4f02d1356b1f514f711328df4a8d6": (0x157FC, 8),
    # Additional exact files verified against matching Microsoft DLLs/PDBs.
    # Native evidence: tests/fixtures/chromecast_hid_layouts.json (not shipped).
    "27e8443e8604a87f48301d1920b324876469e6263db01ae71d1d659d52d5a473": (0xCD80, 24),  # 10.0.17763.194
    "b94ed614f20490bc4d625722d4f7632fb2d173df356aeb411cb10299ceeebeda": (0xCD40, 24),  # 10.0.17763.1007
    "dda878748055f423b5c76c7e370bffb20112893ccabd0f6fcdce4e075c35f2da": (0xCD40, 24),  # 10.0.17763.1697
    "0202af88c2720a8cfcbb9cef27390bc83c2d8693910c41a3050c92eda9b0dd89": (0xCD40, 24),  # 10.0.17763.7560
    "04cefb29306c5ba9ce81f249b1cd520cecf3c56dd9efe586b8748cab69ac3fdb": (0xCD40, 24),  # 10.0.17763.9119
    "bd21283baeaa3d01058bce30323b4f930bbd061b68366773a29376684b27e62b": (0xF1B8, 24),  # 10.0.19041.746
    "440a7fec332fa1d4c03070e000c632d1b1a433b13d46434f95606ff8e548848b": (0xF1B8, 24),  # 10.0.19041.1001
    "aa5b49cc5466384d15268fa41b078da0a8cf6aa916f8b3f235d5c8d67754cd4d": (0xF1B8, 24),  # 10.0.19041.4170
    "38bc43fddd625df4a35c27d66a937188cd1b161cecf0bd17d30d8323f1feae76": (0xF1B8, 24),  # 10.0.19041.5125
    "c3ecffe0efcaf9b540d87d9e818c4742a79f6435fbb96ac2a6af937372bf7afe": (0xF378, 24),  # 10.0.19041.7811
    "c69e3628088f395d8395a51f470d126ca47e3cadcd182aa4d0a308ef967ddae9": (0x135C4, 24),  # 10.0.22000.1696
    "c7ec5ce4cce5fc2c0e2154355a380091ad0b76ee4d40109ec92035e8df1aa04f": (0x135C4, 24),  # 10.0.22000.2836
    "be831a14599c2584b4e2a7efb93ba089188f319b436dfd892f23857e65db44bd": (0x135C4, 24),  # 10.0.22406.1000
    "617b31d23907545226cbdee01f1bb733b7ce690188e75f23841cdf2930ad5ca3": (0x12C48, 24),  # 10.0.22621.1078
    "a9f89ded60aebb0432c95350703f3ee0488a83fa8a2bc82cd9b4f7ee4606c574": (0x12FCC, 24),  # 10.0.22621.1409
    "c9db2a4e404bc7fc1a156a79b7b782853a6c83e86a2dfe766c8c785f94c930db": (0x12C48, 24),  # 10.0.22621.1989
    "cff0c7190575ab1cb76c4397d5370be01d066c38f8e7e67c032940620ecda0cb": (0x12C48, 24),  # 10.0.22621.2280
    "7bed7dcd7dd825c49324e27abc63c940c34803a74bcc929fb8032b60e0dd535a": (0x12C48, 24),  # 10.0.22621.3209
    "bcd139ff61e6b60f6cfdc36b19193432fc6d967b5420990a66e36f7c6a893bab": (0x12C48, 24),  # 10.0.22621.3785
    "ba507ef0822257a2e6de743b4cd158639eece92c1916c3d0a52a3261a3beb5be": (0x1760C, 24),  # 10.0.22621.4034
    "d4466dd7d02a8c23d9498c51b9e119fa60be8ab9c0977659d29dd306ffdccf0a": (0x12FCC, 24),  # 10.0.22621.4746
    "b7fa66a37539fc2288b8084dabc2a378c25bf7c972f1ea928803a0ba45d22cdf": (0x12FCC, 24),  # 10.0.22621.5305
    "75f52295a05414298e5523d194fcd77180aaca9a4c75a4a20c195846464f7ac3": (0x1312C, 24),  # 10.0.22621.5541
    "9c7e905adaf694859f68db33767ce33231d2e61f7edeb1b7e7a83982f36a7cb5": (0x1312C, 24),  # 10.0.22621.5771
    "7ceeac8d0a1fabfc92d7e5bb75896862faec6495c4371a95a390a8e1c209a3ef": (0x12FCC, 24),  # 10.0.22621.7622
    "427f9aba196120da2b4c793be9042128382e2203e105d07cd507f69458f0c87b": (0x12458, 24),  # 10.0.26100.1
    "a7353a9040a718da4198387a5daaccfcdfeed595e907f3aac927e7039163e1e6": (0x12458, 24),  # 10.0.26100.2
    "e8ecfa225157d1835029950bde1eeca366a0dee84671b57997c7246d178cce98": (0x12458, 24),  # 10.0.26100.1150
    "20d0be76a07286bd2e910bfd0a0dad087454618af38d9b946d8611af187b4029": (0x1B138, 24),  # 10.0.26100.3037
    "40efcf0aa89577cea6999b69b84830f080e029d4487ed1d6a383c2fb387017d7": (0x1B848, 24),  # 10.0.26100.3323
    "ea98f5babd0d8573e9e9750258dca1ec8b54dc94ff792e59189b3f2a751ac087": (0x1E28C, 24),  # 10.0.26100.3624
    "0ec88c7bca32f59756bb9c423f2f6f54e5fbc8656728a90290a21eb63a953c77": (0x1E28C, 24),  # 10.0.26100.4061
    "6cdaf9e8bb90ddf30988736d9a150713d1d94062cce6fa60c19390e65c5b397b": (0x1FA6C, 24),  # 10.0.26100.4202
    "fdfbed129fd625c07b8bf42f7dfc54af1e5c30becf17806f18348971da56015f": (0x1F9EC, 24),  # 10.0.26100.4484
    "743736b5abc5530210a4efb520731e74961cdffa5a3cf8d58585b29e9fbe509d": (0x1F97C, 24),  # 10.0.26100.4768
    "c208b31f1cad14d94d0dd29f6ed6199480a40fe44f2bc39b15ec64f1a769118b": (0x18C5C, 24),  # 10.0.26100.5074
    "4e5d969af6622c1f13d24a1ce5e3e020041027406562f5b5e4f0d38c3443a1f3": (0x1873C, 24),  # 10.0.26100.6725
    "4340c3e38e91a4c3fac1e43d0c77365be18ee67b306fe076ba93d69c7fb7d2e8": (0x17D0C, 24),  # 10.0.26100.7019
    "23ce3be4a7906cb7c806e11881b12a5abfd534dfb0dde554dd674e7aa4438dcf": (0x177FC, 24),  # 10.0.26100.7309
    "41d2998c7f38e104b0a469404cfb05ddbfe7c0a912b9020f446f005f3f4156d4": (0x178AC, 24),  # 10.0.26100.8115
    "daefe991172faaade504db81796cc970c731b48cb34baa05886c9e0ef18f1d61": (0x178EC, 24),  # 10.0.26100.8328
    "8dcc8f8365ddbb5d2ec711c2601968456ccfc7e71c49109098e6339e5bd0ab9d": (0x178EC, 24),  # 10.0.26100.8474
    "056bcdf18700deceba1f4f9fb9427fd7cb64fca9f49603f089e29e241e6684f8": (0x1622C, 8),  # 10.0.26100.8491
    "ad74c175268e13c103bb4a6697dbce6a0d2bf3d2e42e861a1ab56cf90f61222b": (0x178EC, 24),  # 10.0.26100.8493
    "f1e79360ed0f76adbd13d6f13e8c03a15a79fc1c5c93acf888f8183d199164ff": (0x1622C, 8),  # 10.0.26100.8497
    "9709118ceec764e1b773aa3abbbd5c060d92a78b953374de2bb54de32cde3d38": (0x178EC, 24),  # 10.0.26100.8521
    "f7fa7b8bd7d6185000fa080bf5381444a323eefd62176ce399b7b290ad22837d": (0x1622C, 8),  # 10.0.26100.8544
    "f292381e02a6c8a7d4f62498b1dc7a806924dbc57783f739566f4cf8521c9eb2": (0x1622C, 8),  # 10.0.26100.8553
    "184ee7009b8546dca6db9e4ec1b73dc815dc3ba9036b45cd605a4d8c89ef9285": (0x1580C, 8),  # 10.0.26100.8680
    "0556829a1a986f0eae303831b1418745631c537f899a820e0b9b4fc24eac9630": (0x1580C, 8),  # 10.0.26100.8687
    "bbac057106e21f5fbea4197dbf8ec5fb56b1cf315b5a8bf34dbf9b6851acea75": (0x1580C, 8),  # 10.0.26100.8690
    "198161e0a8c0364df24deb6d51c8f105e8234bc40c8acb76a92dbf464f02b55e": (0x1580C, 8),  # 10.0.26100.8697
    "e68d7f683648414cd3e4242817636d9789955a87e31c8223265461dd1e785b3e": (0x1580C, 8),  # 10.0.26100.8764
    "85f99b06b088256d2fb12e5b983dfded07382e7b7a4647bda454ce306b4d6795": (0x1580C, 8),  # 10.0.26100.8772
    "cca3419676008625b6d34591d1d7b355e6adf0291009466891609c7e21b59690": (0x157FC, 8),  # 10.0.26100.8925
    "63f57bea9a45f71a0873d74a94acb58758efe43dea3f64403505d0ac20a8517d": (0x157FC, 8),  # 10.0.26100.8935
    "d628c3a9c6b34e51d79f91c204a787ad6a598f863bf24395e612fbfd3bbe0b87": (0x157FC, 8),  # 10.0.26100.8972
    "1a3c0a4ed1d01735e2ef4897f82633d175d2f89864c72816c15c02d11ce79087": (0x157FC, 8),  # 10.0.26100.9492
    "d2987ac89fabc155b53a3bbc6a69f5c090a3b6d855761a441ee7423e807967ca": (0x157FC, 8),  # 10.0.26100.9502
    "752a7bef81b3dcbb0e80bf183ddbe8eeb9b31df6b86ddba3c3cd21c80ad994d4": (0x157FC, 8),  # 10.0.26100.9539
    "09648a6511eea778b3b72a149ddbc585422bed55ab35ac19769c6f065bbd61ad": (0x157FC, 8),  # 10.0.26100.9549
    "6e9e3e939fa31092b54538b10eef3314e06f3b036418ea876abba681ded0a725": (0x1964C, 8),  # 10.0.26100.9568
    "9939b2ae9e6d51d6467c6a101bea2b1198012540a2292d9baf45c196ca73fbeb": (0x1964C, 8),  # 10.0.26100.9577
    "4acd0c10707972ef06dbd7b7b564d1662bac02a92b61257e4a9213f6e7d8a359": (0x18DAC, 8),  # 10.0.26100.9587
    "073f4d8f057fd9fa5c479a0e95a77d04035e07d732e8a1badec3d30048f58fae": (0x18DAC, 8),  # 10.0.26100.9596
    "ece5e843787c9e4cfcc4d8ccdb9f7333d2f6649bfd901d021b94814c8bc8c537": (0x18DAC, 8),  # 10.0.26100.9739
    "35532a64bcf8d8126ee5282c92ca4975ee5139ca6cb07535c10a20e3fa423b4a": (0x18DAC, 8),  # 10.0.26100.9750
    "a654b44ea6833e9f5a54958ff958fcafe3fa5f2a6b97ffc747d58172d3332cc4": (0x18DAC, 8),  # 10.0.26100.9756
    "433726b642915874d8adc288b006726d15ce484f04c2d3eed56632a83712757a": (0x18DAC, 8),  # 10.0.26100.9767
    "b9dc053c133e850fd0fbcfa85381c0240306fbe2d789c85f4e0a1d4c47345b78": (0x18DAC, 8),  # 10.0.26100.9807
    "63633289822aa0157adb32e0ec062bf0f3cb31db25bdfc92eb8e39cea0a5475a": (0x18DAC, 8),  # 10.0.26100.9818
    "f1b6274eca6941a8fbc66b59734d0e3ed7a330907612785ba651d63b2356aa6d": (0x186AC, 24),  # 10.0.28000.1
    "69fd00fcdc7164ce3fed849978edf093eac4d88f27a6e53d536debed65aa7035": (0x186AC, 24),  # 10.0.28000.4
    "706da7971d192bc39f575f4a6e2123112d38c35df0541f3dcc52c7808699b0a2": (0x1819C, 24),  # 10.0.28000.1516
    "e697c015864c87585265731c398d107d16e6a99a7d458cff80ef6c7041d29d69": (0x1828C, 24),  # 10.0.28000.1873
    "cf80fe81f0bd89e8bdaed04d000339231461b7ca07fb80f62c4e9b8f3d113929": (0x1828C, 24),  # 10.0.28000.2179
    "dbb06c96cfd73916672ca9ed11a1f4e63b62aa04c4938b61ce8066d69355ca1c": (0x15FAC, 8),  # 10.0.28000.2298
    "59d30fa244df9b34659a4466bca2a39b85cce0199cc2c7930b36dde8f1164e00": (0x15FAC, 8),  # 10.0.28000.2302
    "db56f5663a780aaa9ff2c19f7e17f0c6bfd585530d80179b384c107c32034840": (0x169EC, 8),  # 10.0.28000.2307
    "e680c946c4c607471ac0fa4abedb16c5221507468e85b15725f64227607401fc": (0x169EC, 8),  # 10.0.28000.2315
    "44f928ecc724ca4083160f02999116ed5dadd95ad62f622aade435f08d043216": (0x15FAC, 8),  # 10.0.28000.2336
    "b3800cab8466609a8340380a03d4f89d9e9d2ca1329d4a2982524be16875364f": (0x15FAC, 8),  # 10.0.28000.2380
    "70b6e770e383822f6ec9256b0bec046bd8017dca1877a6f40a8a8dfe13023a70": (0x15FAC, 8),  # 10.0.28000.2387
    "9eaeeb448c31c4e284c0909c8456a757305a55361369f1b4081a1f94e62e6d8f": (0x15FAC, 8),  # 10.0.28000.2605
    "fcac57ab72c460689deeb1541f2f81e83f09ee0875700437b4a5186554336412": (0x15F9C, 8),  # 10.0.28000.2623
    "bc96f69b461f14ee7228e0b9b75c755da6247afa44adf076c8008781549c7f75": (0x15F9C, 8),  # 10.0.28000.2804
    "72ce679353267bdbde2bb8ee8c68fabecefd9843739267bf309e82b7cdc27d9a": (0x18F64, 8),  # 10.0.28000.3262
    "60fba714c161686d9049b4a1eb0a177ecfdfda37d807e412bf06cb54148dc76d": (0x18F64, 8),  # 10.0.28000.3272
    "cfcd870605ee924f0e897c86c148deb552ed659c371ee5cfdd583817c34cd0f9": (0x18F74, 8),  # 10.0.28000.3292
    "25114039c6557454b8983e4e967f7106048da5435c1dcffe5621edad3c67b4cc": (0x18F74, 8),  # 10.0.28000.3302
    "559c6a511c02392278535511ec8efa03aee0ff3404477ba232f6132f9f04b578": (0x18F64, 8),  # 10.0.28000.3351
    "63b1618589ecc11bf26b3445d6b6632d53c45f06cf8cc29ad78e5b8e48cd60bc": (0x18F64, 8),  # 10.0.28000.3365
    "74ba8266056d3f04c52185fbb71d27376873767b266d3c7b5de3efe06a14e54f": (0x18F74, 8),  # 10.0.28000.3382
    "b054e79e0ae2e678a49fac5153f6b4fca791b8ed957987cf5f0da76492e99dc2": (0x18F74, 8),  # 10.0.28000.3393
    "bacfc3f2191db651227837bad23f82c154014ae08cf2e02cf098f22a52d94667": (0x17134, 24),  # 10.0.29591.1000
    "6c61c230adc1a8d8005e8ed789fbf05efdf6bf65d2d58e5423d8fe4634f8968e": (0x17134, 24),  # 10.0.29595.1000
    "79bbf5edce77da933fbc29cbff9a0fccbfc9ae5b988532e71969555e689b418c": (0x17134, 24),  # 10.0.29599.1000
    "c12aae6dedd729b6ffec416a323754b74425b2d73c3e6a6b30260569dab84dbb": (0x17134, 24),  # 10.0.29610.1000
    "192c791dd42668c204e23fd39aec91f9f4e51f3487e92b1c5e02bd66eb2921f4": (0x17134, 24),  # 10.0.29613.1000
    "d424771cb89988f572ab12216d049214409e6909627fc25118adacac18d7b862": (0x17134, 24),  # 10.0.29617.1000
    "f46dffc77f31f267e8cfd9e73621fe4629c6be5115ab8385de23d60070bb448a": (0x1B6A4, 8),  # 10.0.29704.1000
}
_HID_SERVICE = "{00001812-0000-1000-8000-00805f9b34fb}"
_GOOGLE_HARDWARE = (
    "dev_vid&0118d1_pid&9450_rev&0110",
    "dev_vid&0218d1_pid&9450_rev&011b",
)
_ADDRESS_SUFFIX = re.compile(r"_([0-9a-f]{12})$", re.I)
_SYMBOL = "Microsoft::Bluetooth::Profiles::HidOverGatt::HogpGattAdapter::OnReportValueChanged"
_CONSTRUCTOR = "Microsoft::Bluetooth::Profiles::HidOverGatt::HogpGattAdapter::HogpGattAdapter"


class TapError(RuntimeError):
    def __init__(self, message, *, detail="none", native_code=-1):
        super().__init__(message)
        self.detail = detail
        if native_code >= 0:
            self.winerror = native_code


_startup_sink = ContextVar("chromecast_hid_startup_sink", default=None)
_startup_cancel = ContextVar("chromecast_hid_startup_cancel", default=None)
_STARTUP_LIMIT = 128
_PDB_DOWNLOAD_ATTEMPTS = 5
_PDB_DOWNLOAD_SECONDS = 20


def _check_start_cancelled():
    cancelled = _startup_cancel.get()
    if cancelled is not None and cancelled.is_set():
        raise TapError("hid_symbol_unavailable", detail="startup_cancelled")


def _download_pdb(arguments, deadline):
    """Own and reap curl so stopping startup never leaves a downloader running."""
    _check_start_cancelled()
    with subprocess.Popen(arguments, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)) as process:
        try:
            while True:
                _check_start_cancelled()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired("curl", _PDB_DOWNLOAD_SECONDS)
                try:
                    stdout, stderr = process.communicate(timeout=min(.1, remaining))
                    _check_start_cancelled()
                    return subprocess.CompletedProcess(arguments, process.returncode, stdout, stderr)
                except subprocess.TimeoutExpired:
                    continue
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate()


def startup_record(step, state, *, started=None, error=None, script_line=-1, host_pid=-1,
                   reason="none", details=None):
    """Fixed metadata only; usable before a HID tap exists as well as in its thread."""
    def error_type(value):
        name = type(value).__name__ if value is not None else "none"
        return name if name in HID_ERROR_TYPES else "other"

    def code(value):
        for name in ("winerror", "hresult", "errno"):
            number = getattr(value, name, None)
            if type(number) is int:
                return number & 0xffffffff
        return -1

    try:
        cause = getattr(error, "__cause__", None) or (None if getattr(error, '__suppress_context__', False)
                                                     else getattr(error, '__context__', None))
        row = dict(kind="hid_startup", elapsed_ms=0, step=step, state=state,
            duration_ms=0 if started is None else min(2147483647, max(0, round((time.monotonic() - started) * 1000))),
            error_type=error_type(error), cause_type=error_type(cause), native_code=code(error), cause_code=code(cause),
            host_pid=host_pid if type(host_pid) is int else -1,
            script_line=script_line if type(script_line) is int and 0 <= script_line <= 65535 else -1,
            reason=getattr(error, "detail", reason), details=dict(details or {}))
        return row if valid_record(row) else None
    except Exception:
        return None


@contextmanager
def _trace_step(name):
    # Context is local to this startup thread. Runtime source polling stays quiet.
    sink = _startup_sink.get()
    started = time.monotonic()
    details = {}
    def emit(state, error=None):
        if sink is not None:
            try:
                sink(name, state, started=started, error=error, details=details)
            except Exception:
                pass
    emit("begin")
    try:
        yield details
    except Exception as error:
        emit("failed", error)
        raise
    else:
        emit("success")


def selected_host_and_address(entity: str) -> tuple[int, str]:
    with _trace_step("host_scan") as details:
        return _selected_host_and_address(entity, details)


def _selected_host_and_address(entity, details):
    """Resolve exactly one selected Google HID service from Windows PnP."""
    import winreg

    if not remote_selection.valid_key(entity):
        raise TapError("hid_source_unconfirmed", detail="invalid_entity")
    found = set()
    details.update(hardware_matches=0, selected_matches=0, invalid_address=0, missing_container=0, invalid_pid=0)
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Enum\BTHLEDevice") as root:
            count = winreg.QueryInfoKey(root)[0]
            details["registry_nodes"] = count
            for index in range(count):
                name = winreg.EnumKey(root, index)
                folded = name.casefold()
                if not folded.startswith(_HID_SERVICE) or not any(hardware in folded for hardware in _GOOGLE_HARDWARE):
                    continue
                details["hardware_matches"] += 1
                address = _ADDRESS_SUFFIX.search(name)
                if address is None:
                    details["invalid_address"] += 1
                    continue
                with winreg.OpenKey(root, name) as service:
                    for member in range(winreg.QueryInfoKey(service)[0]):
                        instance = winreg.EnumKey(service, member)
                        with winreg.OpenKey(service, instance) as key:
                            try:
                                container, _ = winreg.QueryValueEx(key, "ContainerID")
                            except OSError:
                                details["missing_container"] += 1
                                continue
                            if remote_selection.container_key(container) != entity:
                                continue
                            details["selected_matches"] += 1
                            with winreg.OpenKey(key, r"Device Parameters\WUDFDiagnosticInfo") as info:
                                pid, _ = winreg.QueryValueEx(info, "HostPid")
                            if type(pid) is int and pid > 0:
                                found.add((pid, address.group(1).lower()))
                            else:
                                details["invalid_pid"] += 1
    except (OSError, ValueError) as exc:
        raise TapError("hid_source_unconfirmed", detail="registry_read") from exc
    details["host_candidates"] = len(found)
    if len(found) != 1:
        raise TapError("hid_source_unconfirmed", detail="no_selected_host" if not found else "ambiguous_host")
    return found.pop()


def _pdb_identity(path: Path) -> tuple[str, str]:
    with _trace_step("pdb_identity") as details:
        return _read_pdb_identity(path, details)


def _read_pdb_identity(path, details):
    import pefile

    image = pefile.PE(str(path), fast_load=False)
    try:
        entries = [entry for entry in image.DIRECTORY_ENTRY_DEBUG if entry.struct.Type == 2]
        details["debug_entries"] = len(entries)
        if len(entries) != 1:
            raise TapError("hid_symbol_unavailable", detail="debug_entries")
        entry = entries[0]
        with path.open("rb") as stream:
            stream.seek(entry.struct.PointerToRawData)
            record = stream.read(entry.struct.SizeOfData)
        if record[:4] != b"RSDS" or len(record) < 28:
            raise TapError("hid_symbol_unavailable", detail="debug_signature")
        name = record[24:].split(b"\0", 1)[0].decode("ascii")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+\.pdb", name, re.I):
            raise TapError("hid_symbol_unavailable", detail="pdb_name")
        index = uuid.UUID(bytes_le=record[4:20]).hex.upper() + f"{struct.unpack_from('<I', record, 20)[0]:X}"
        details["pdb_key"] = index
        return name, index
    finally:
        image.close()


def _symbol_cache() -> Path:
    root = os.environ.get("LOCALAPPDATA")
    if not root:
        raise TapError("hid_symbol_unavailable", detail="cache_root")
    return Path(root) / "RemoteMic" / "HidSymbols"


def _ensure_pdb(module: Path) -> Path:
    name, index = _pdb_identity(module)
    with _trace_step("cache_lookup") as details:
        target = _symbol_cache() / name / index / name
        present = target.is_file()
        details["cache_present"] = int(present)
        details["cache_size"] = target.stat().st_size if present else -1
        cached = target.read_bytes() if present and details["cache_size"] <= 2_097_152 else b""
        valid = present and details["cache_size"] <= 2_097_152 and cached[:32].startswith(b"Microsoft C/C++ MSF 7.00")
        if cached:
            details["pdb_sha256"] = hashlib.sha256(cached).hexdigest()
        details["cache_valid"] = int(valid)
        if valid:
            return target
    with _trace_step("cache_directory"):
        target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f"{name}.{os.getpid()}.{threading.get_ident()}.tmp")
    url = f"https://msdl.microsoft.com/download/symbols/{name}/{index}/{name}"
    curl = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "curl.exe"
    deadline = time.monotonic() + _PDB_DOWNLOAD_SECONDS
    try:
        for attempt in range(1, _PDB_DOWNLOAD_ATTEMPTS + 1):
            retryable = False
            try:
                with _trace_step("pdb_download") as details:
                    details["download_attempt"] = attempt
                    _check_start_cancelled()
                    temporary.unlink(missing_ok=True)
                    result = _download_pdb(
                        [str(curl), "--fail", "--location", "--silent", "--show-error", "--max-time", "15",
                         "--max-filesize", "2097152", "--write-out", "%{http_code}", "--output", str(temporary), url],
                        deadline,
                    )
                    details["return_code"] = result.returncode & 0xffffffff
                    status = result.stdout.strip()
                    http = int(status) if re.fullmatch(rb"[0-9]{3}", status) else -1
                    details["http_status"] = http
                    if result.returncode == 0:
                        break
                    retryable = result.returncode in {5, 6, 7, 18, 28, 35, 52, 55, 56, 92} or (
                        result.returncode == 22 and (http in {408, 429} or 500 <= http <= 599))
                    if retryable and attempt < _PDB_DOWNLOAD_ATTEMPTS:
                        details["retry_scheduled"] = 1
                    raise TapError("hid_symbol_unavailable", detail="download_exit")
            except TapError as error:
                if error.detail != "download_exit" or not retryable or attempt == _PDB_DOWNLOAD_ATTEMPTS:
                    raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired("curl", _PDB_DOWNLOAD_SECONDS)
            cancelled = _startup_cancel.get()
            if cancelled is None:
                time.sleep(min(.2, remaining))
            else:
                cancelled.wait(min(.2, remaining))
            _check_start_cancelled()
        _check_start_cancelled()
        with _trace_step("download_validate") as details:
            details["size"] = temporary.stat().st_size if temporary.is_file() else -1
            if not 100_000 <= details["size"] <= 2_097_152:
                raise TapError("hid_symbol_unavailable", detail="download_size")
            downloaded = temporary.read_bytes()
            details["pdb_sha256"] = hashlib.sha256(downloaded).hexdigest()
            if not downloaded[:32].startswith(b"Microsoft C/C++ MSF 7.00"):
                raise TapError("hid_symbol_unavailable", detail="download_signature")
        with _trace_step("cache_commit"):
            _check_start_cancelled()
            os.replace(temporary, target)
        return target
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TapError("hid_symbol_unavailable") from exc
    finally:
        with _trace_step("cache_cleanup"):
            temporary.unlink(missing_ok=True)


class _SymbolInfo(ctypes.Structure):
    _fields_ = [("SizeOfStruct", wintypes.ULONG), ("TypeIndex", wintypes.ULONG),
                ("Reserved", ctypes.c_ulonglong * 2), ("Index", wintypes.ULONG),
                ("Size", wintypes.ULONG), ("ModBase", ctypes.c_ulonglong),
                ("Flags", wintypes.ULONG), ("Value", ctypes.c_ulonglong),
                ("Address", ctypes.c_ulonglong), ("Register", wintypes.ULONG),
                ("Scope", wintypes.ULONG), ("Tag", wintypes.ULONG),
                ("NameLen", wintypes.ULONG), ("MaxNameLen", wintypes.ULONG),
                ("Name", wintypes.WCHAR * 1)]


def _resolve_pdb_symbols(module: Path) -> tuple[int, int]:
    _ensure_pdb(module)
    with _trace_step("dbghelp_load"):
        dbghelp = ctypes.WinDLL("dbghelp", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    process = kernel32.GetCurrentProcess()
    dbghelp.SymInitializeW.argtypes = (wintypes.HANDLE, wintypes.LPCWSTR, wintypes.BOOL)
    dbghelp.SymInitializeW.restype = wintypes.BOOL
    dbghelp.SymLoadModuleExW.argtypes = (wintypes.HANDLE, wintypes.HANDLE, wintypes.LPCWSTR,
        wintypes.LPCWSTR, ctypes.c_ulonglong, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD)
    dbghelp.SymLoadModuleExW.restype = ctypes.c_ulonglong
    dbghelp.SymEnumSymbolsW.argtypes = (wintypes.HANDLE, ctypes.c_ulonglong, wintypes.LPCWSTR,
                                       ctypes.c_void_p, wintypes.LPVOID)
    dbghelp.SymEnumSymbolsW.restype = wintypes.BOOL
    dbghelp.SymCleanup.argtypes = (wintypes.HANDLE,)
    dbghelp.SymCleanup.restype = wintypes.BOOL
    with _trace_step("symbol_initialize"):
        ctypes.set_last_error(0)
        if not dbghelp.SymInitializeW(process, str(_symbol_cache()), False):
            raise TapError("hid_symbol_unavailable", detail="symbol_initialize_failed", native_code=ctypes.get_last_error())
    try:
        with _trace_step("symbol_load"):
            ctypes.set_last_error(0)
            base = dbghelp.SymLoadModuleExW(process, None, str(module), None, 0, 0, None, 0)
            if not base:
                raise TapError("hid_symbol_unavailable", detail="symbol_load_failed", native_code=ctypes.get_last_error())
        matches = {_SYMBOL: [], _CONSTRUCTOR: []}
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, ctypes.POINTER(_SymbolInfo), wintypes.ULONG, wintypes.LPVOID)

        def collect(pointer, _size, _context):
            item = pointer.contents
            name = ctypes.wstring_at(ctypes.addressof(item) + _SymbolInfo.Name.offset, item.NameLen).rstrip("\0")
            if name in matches:
                matches[name].append(item.Address - base)
            return True

        callback = callback_type(collect)
        with _trace_step("symbol_enumerate") as details:
            ctypes.set_last_error(0)
            success = dbghelp.SymEnumSymbolsW(process, base, "*", callback, None)
            code = ctypes.get_last_error()
            details.update(callback_matches=len(matches[_SYMBOL]), constructor_matches=len(matches[_CONSTRUCTOR]))
            if not success:
                raise TapError("hid_symbol_unavailable", detail="symbol_enumerate_failed", native_code=code)
        with _trace_step("symbol_match") as details:
            details.update(callback_matches=len(matches[_SYMBOL]), constructor_matches=len(matches[_CONSTRUCTOR]))
            if any(len(items) != 1 for items in matches.values()):
                raise TapError("hid_symbol_unavailable", detail="symbol_missing_or_ambiguous")
            details.update(callback_rva=matches[_SYMBOL][0], constructor_rva=matches[_CONSTRUCTOR][0])
        return matches[_SYMBOL][0], matches[_CONSTRUCTOR][0]
    finally:
        # This API's failure was nonfatal before; observe its result without
        # turning a successful layout lookup into a different product behavior.
        with _trace_step("symbol_cleanup") as details:
            ctypes.set_last_error(0)
            details["native_result"] = int(bool(dbghelp.SymCleanup(process)))
            details["native_code"] = ctypes.get_last_error() if not details["native_result"] else -1


def _constructor_address_offset(code: bytes) -> int:
    """Recognize complete verified x64 prologues before RCX/RDX can change.

    The constructor's first argument (RDX) is the Bluetooth address. Only the
    RIP-relative vtable displacement may vary; never scan object memory or
    search arbitrary instructions for a coincidental address/store.
    """
    prefix = bytes.fromhex(
        "4c894c2420 48894c2408 53 55 56 57 4154 4156 4157 4883ec50 "
        "4d8bf1 410fb7e8 488bf9"
    )
    # 26100.9818 saves R13/R14 instead of R14/R15; RCX and the RDX
    # address store remain unchanged. Admit only that complete observed form.
    register_variant = bytes.fromhex(
        "4c894c2420 48894c2408 53 55 56 57 4154 4155 4156 4883ec50 "
        "4d8bf1 410fb7e8 488bf9"
    )
    # 17763.7671 uses RAX for the initial stack saves. The complete
    # RDX address store at this+24 ends just beyond the former 64-byte probe.
    stack_save_variant = bytes.fromhex(
        "488bc4 4c894820 48894808 55 56 57 4156 4157 4883ec50 "
        "48c740c8feffffff 48895818 498bf1 410fb7e8 488bf9 "
        "4533f6 4c897108 4c897110"
    )
    layouts = (
        (prefix, 24, bytes.fromhex("4533e4 4c896108 4c896110")),
        (prefix, 8, b""),
        (register_variant, 8, b""),
        (stack_save_variant, 24, b""),
    )
    for verified_prefix, offset, initialize in layouts:
        start = verified_prefix + initialize + bytes.fromhex("488d05")
        end = bytes.fromhex("488901 488951") + bytes([offset])
        if code.startswith(start) and code[len(start) + 4:len(start) + 4 + len(end)] == end:
            return offset
    raise TapError("hid_symbol_unavailable", detail="constructor_unrecognized")


def _driver_layout(module: Path = MODULE) -> tuple[int, int]:
    with _trace_step("driver_read") as details:
        data = module.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        known = _KNOWN_LAYOUTS.get(digest)
        details.update(driver_sha256=digest, size=len(data), known_layout=int(known is not None))
    constructor = None
    if known is None:
        rva, constructor = _resolve_pdb_symbols(module)
    else:
        rva, address_offset = known
    import pefile

    with _trace_step("driver_pe"):
        image = pefile.PE(str(module), fast_load=True)
    try:
        with _trace_step("driver_arch") as details:
            details["machine"] = image.FILE_HEADER.Machine
            if image.FILE_HEADER.Machine != 0x8664:
                raise TapError("hid_symbol_unavailable", detail="unsupported_architecture")
        constructor_probe_size = 64
        for location, size in ((rva, 1),) + (((constructor, constructor_probe_size),) if constructor is not None else ()):
            with _trace_step("callback_range" if size == 1 else "constructor_range") as details:
                details["callback_rva" if size == 1 else "constructor_rva"] = location
                executable = [section for section in image.sections if section.Characteristics & 0x20000000
                              and section.VirtualAddress <= location
                              and location + size <= section.VirtualAddress + min(section.Misc_VirtualSize, section.SizeOfRawData)]
                details["executable_sections"] = len(executable)
                if len(executable) != 1:
                    raise TapError("hid_symbol_unavailable", detail="nonexecutable_rva")
                if size != 1:
                    section = executable[0]
                    backed_end = section.VirtualAddress + min(section.Misc_VirtualSize, section.SizeOfRawData)
                    constructor_probe_size = min(HID_CONSTRUCTOR_PROBE_BYTES, backed_end - location)
        if constructor is not None:
            with _trace_step("constructor_layout") as details:
                code = image.get_data(constructor, constructor_probe_size)
                details["size"] = len(code)
                details["constructor_prefix"] = code.hex()
                address_offset = _constructor_address_offset(code)
                details["address_offset"] = address_offset
    finally:
        image.close()
    return rva, address_offset


_SCRIPT = r"""
const expectedPath = __PATH__;
const expectedAddress = __ADDRESS__;
const consumerCodes = __CONSUMER_CODES__;
const copyIoctl = 0x80018483;
const module = Process.findModuleByName("microsoft.bluetooth.profiles.hidovergatt.dll");
function setupEvidence(step, state, reason, details) {
  try { send({kind:"hid_setup", step, state, reason, details}); } catch (_) {}
}
const pathMatches = module !== null && module.path.toLowerCase().replace(/\\/g, "/") === expectedPath;
setupEvidence("runtime_module", module !== null && pathMatches ? "success" : "failed",
  module === null ? "module_missing" : pathMatches ? "none" : "module_path_mismatch", {
  module_present:module === null ? 0 : 1,
  module_path_match:pathMatches ? 1 : 0,
  pointer_size:Process.pointerSize, loaded_size:module === null ? -1 : module.size
});
if (module === null || module.path.toLowerCase().replace(/\\/g, "/") !== expectedPath)
  throw new Error("module_mismatch");
const callback = module.base.add(__RVA__);
const iid = Memory.alloc(16);
iid.writeByteArray([0xef,0x0f,0x5a,0x90,0x53,0xbc,0xdf,0x11,0x8c,0x49,0x00,0x1e,0x4f,0xc6,0x86,0xda]);
let addressSeen = false;
// Cumulative metadata only; no report content or device addresses in diagnostics.
const counts = Object.fromEntries(__COUNTS__.map(name => [name, 0]));
const lengths = new Map();
const copyLengths = new Map();
let copyHookState = "unavailable";
const started = Date.now();
let errorStep = "none", lastErrorStep = "none", errorDiagnosticsSent = false;
function count(name) { counts[name] = Math.min(2147483647, counts[name] + 1); }
function emitDiagnostics() {
  try {
    send({kind:"hid_flow", elapsed_ms:0,
      sample_ms:Math.max(0, Math.min(2147483647, Date.now() - started)),
      counts:{...counts}, lengths:Array.from(lengths), copy_buffer_lengths:Array.from(copyLengths),
      copy_hook_state:copyHookState, copy_scope:"selected_host_unattributed",
      last_error_step:lastErrorStep});
  } catch (_) { /* Diagnostic delivery must never alter input handling. */ }
}
function method(object, slot, result, args) {
  return new NativeFunction(object.readPointer().add(slot * Process.pointerSize).readPointer(), result, args);
}
function addressMatches(adapter) {
  const bytes = new Uint8Array(adapter.add(__ADDRESS_OFFSET__).readByteArray(6));
  for (let i = 0; i < 6; i++) if (bytes[i] !== expectedAddress[i]) return false;
  return true;
}
function report(buffer) {
  let access = ptr(0);
  try {
    if (buffer.isNull()) { count("buffer_null"); return null; }
    errorStep = "length";
    const sizeOut = Memory.alloc(4);
    if (method(buffer, 7, "int", ["pointer", "pointer"])(buffer, sizeOut) !== 0) {
      count("length_error"); return null;
    }
    const size = sizeOut.readU32();
    if (lengths.has(size)) lengths.set(size, Math.min(2147483647, lengths.get(size) + 1));
    else if (lengths.size < 16) lengths.set(size, 1);
    else count("lengths_overflow");
    if (size !== 8 && size !== 2) { count("length_rejected"); return null; }
    errorStep = "access";
    const accessOut = Memory.alloc(Process.pointerSize);
    if (method(buffer, 0, "int", ["pointer", "pointer", "pointer"])(buffer, iid, accessOut) !== 0) {
      count("access_error"); return null;
    }
    access = accessOut.readPointer();
    errorStep = "data";
    const bytesOut = Memory.alloc(Process.pointerSize);
    if (method(access, 3, "int", ["pointer", "pointer"])(access, bytesOut) !== 0) {
      count("data_error"); return null;
    }
    const bytes = new Uint8Array(bytesOut.readPointer().readByteArray(size));
    if (size === 2) {
      // GATT omits the 0x02 report ID added by the downstream Windows copy.
      // Normalize only known Consumer usages from the selected adapter.
      const usage = bytes[0] | (bytes[1] << 8);
      const code = usage === 0 ? 0 : consumerCodes[usage];
      if (code === undefined) { count("code_rejected"); return null; }
      return new Uint8Array([code, 0, 0, 0, 0, 0, 0, 0]);
    }
    return bytes;
  } finally {
    if (!access.isNull()) {
      const previousStep = errorStep;
      errorStep = "release";
      method(access, 2, "uint32", ["pointer"])(access);
      errorStep = previousStep;
    }
  }
}
// Observe the downstream HID copy used by vibe-mote. This host can also serve
// other devices, so these counts are never treated as selected-device reports.
// This private UMDF copy can consume the output parameter as a source buffer.
// IO_STATUS_BLOCK.Information is not its report length (observed as zero).
// Match vibe-mote's successful three-byte buffer observation; never edit it.
function installCopyObservation() {
  try {
    const ntdll = Process.findModuleByName("ntdll.dll");
    const target = ntdll ? ntdll.findExportByName("NtDeviceIoControlFile") : null;
    if (target === null) { count("copy_hook_error"); return; }
    Interceptor.attach(target, {
      onEnter(args) {
        this.copy = false;
        try {
          if (args[5].toUInt32() !== copyIoctl) return;
          this.output = args[8];
          this.capacity = args[9].toUInt32();
          this.copy = true;
          count("copy_calls");
          if (this.capacity === 3) count("copy_capacity_three");
        } catch (_) { count("copy_read_error"); }
      },
      onLeave(retval) {
        if (!this.copy) return;
        try {
          const result = retval.toUInt32();
          if (result === 0x103) { count("copy_pending"); return; }
          if (result !== 0) { count("copy_failed"); return; }
          count("copy_success");
          const size = this.capacity;
          if (copyLengths.has(size))
            copyLengths.set(size, Math.min(2147483647, copyLengths.get(size) + 1));
          else if (copyLengths.size < 16) copyLengths.set(size, 1);
          else count("copy_lengths_overflow");
          if (size !== 3) return;
          if (this.output.isNull()) { count("copy_read_error"); return; }
          const bytes = new Uint8Array(this.output.readByteArray(3));
          if (bytes.length !== 3) { count("copy_read_error"); return; }
          count("copy_read_three");
          if (bytes[0] !== 0x02) return;
          count("copy_report_two");
          const usage = bytes[1] | (bytes[2] << 8);
          if (usage === 0) count("copy_release");
          else if (usage === 0x42) count("copy_up");
          else if (usage === 0x43) count("copy_down");
          else if (usage === 0x44) count("copy_left");
          else if (usage === 0x45) count("copy_right");
          else if (usage === 0x41) count("copy_ok");
          else if (consumerCodes[usage] !== undefined)
            count("copy_known_other");
          else count("copy_other_usage");
        } catch (_) { count("copy_read_error"); }
      }
    });
    copyHookState = "ready";
  } catch (_) { count("copy_hook_error"); }
}
installCopyObservation();
setupEvidence("runtime_hook", "begin", "none", {});
try {
Interceptor.attach(callback, {onEnter(args) {
  try {
    count("callbacks");
    errorStep = "address";
    if (!addressMatches(args[0])) { count("address_mismatch"); return; }
    count("address_match");
    addressSeen = true;
    const bytes = report(args[2]);
    if (bytes === null) return;
    if (!bytes.slice(1).every(value => value === 0)) { count("tail_rejected"); return; }
    if (!(bytes[0] === 0 || [1,3,4,5,6,7,8,10,11,12,13,14,15,17].includes(bytes[0]))) {
      count("code_rejected"); return;
    }
    // The sender interface pointer changes between reports on this driver.
    // Bind by the selected adapter address, never by that transient pointer.
    errorStep = "send";
    send({kind:"report", value:Array.from(bytes).map(x => x.toString(16).padStart(2,"0")).join("")});
    count("accepted");
  } catch (_) {
    count("callback_error");
    lastErrorStep = errorStep;
    if (!errorDiagnosticsSent) {
      emitDiagnostics();
      errorDiagnosticsSent = true;
    }
    if (addressSeen) send({kind:"error", reason:"hid_callback_failed"});
  }
}});
} catch (error) {
  setupEvidence("runtime_hook", "failed", "hook_exception", {});
  throw error;
}
setupEvidence("runtime_hook", "success", "none", {});
send({kind:"hook_ready"});
emitDiagnostics();
setInterval(emitDiagnostics, 2000);
"""


class HidTap:
    def __init__(self, entity: str):
        self.entity = entity
        self.events = queue.Queue(maxsize=256)
        self.overflow = threading.Event()
        self.session = self.script = None
        self._gadget_device = None
        self.pid = self.address = None
        self._diagnostic_lock = threading.Lock()
        self._diagnostic = None
        self._startup = queue.Queue(maxsize=_STARTUP_LIMIT)
        self._startup_lock = threading.Lock()
        self._startup_dropped = 0
        self._startup_rejected = 0
        self._script_error_recorded = False
        self._startup_cleanup_ok = True
        self._abort_start = threading.Event()

    def abort_start(self):
        self._abort_start.set()

    def _startup_record(self, step, state, **fields):
        try:
            row = startup_record(step, state, host_pid=self.pid, **fields)
            if row is not None:
                with self._startup_lock:
                    if self._startup.full():
                        # Keep the latest failure and explicitly report loss.
                        self._startup.get_nowait()
                        self._startup_dropped = min(2147483647, self._startup_dropped + 1)
                    self._startup.put_nowait(row)
            else:
                with self._startup_lock:
                    self._startup_rejected = min(2147483647, self._startup_rejected + 1)
        except Exception:
            pass  # Evidence cannot alter startup or resource cleanup.

    @contextmanager
    def _step(self, name):
        started = time.monotonic()
        details = {}
        self._startup_record(name, "begin")
        try:
            yield details
        except Exception as error:
            self._startup_record(name, "failed", started=started, error=error, details=details)
            raise
        else:
            self._startup_record(name, "success", started=started, details=details)

    def poll_startup(self):
        rows = []
        with self._startup_lock:
            if self._startup_dropped:
                rows.append(startup_record("evidence_overflow", "success", reason="records_dropped",
                                           details={"dropped": self._startup_dropped}))
                self._startup_dropped = 0
            if self._startup_rejected:
                rows.append(startup_record('evidence_overflow', 'success', reason='records_rejected',
                                           details={'dropped': self._startup_rejected}))
                self._startup_rejected = 0
            for _ in range(_STARTUP_LIMIT):
                try:
                    rows.append(self._startup.get_nowait())
                except queue.Empty:
                    break
        return rows

    def _store_diagnostic(self, item):
        if valid_record(item) and item.get("kind") == "hid_flow":
            # One replaceable slot, independent of the press/release queue.
            with self._diagnostic_lock:
                self._diagnostic = item

    def _put(self, event):
        try:
            self.events.put_nowait(event)
        except queue.Full:
            # A dropped release is unsafe. Discard the backlog and stop.
            self.overflow.set()

    def start(self):
        token = _startup_sink.set(self._startup_record)
        cancel_token = _startup_cancel.set(self._abort_start)
        try:
            _check_start_cancelled()
            return self._start()
        finally:
            _startup_cancel.reset(cancel_token)
            _startup_sink.reset(token)

    def _start(self):
        with self._step("import_frida"):
            import frida
        with self._step("selected_host"):
            pid, address = selected_host_and_address(self.entity)
            self.pid, self.address = pid, address
        with self._step("driver_layout") as details:
            rva, address_offset = _driver_layout()
            details.update(callback_rva=rva, address_offset=address_offset)
        _check_start_cancelled()
        with self._step("script_prepare"):
            source = (_SCRIPT.replace("__PATH__", json.dumps(str(MODULE).lower().replace("\\", "/")))
                 .replace("__ADDRESS__", json.dumps(list(bytes.fromhex(address)[::-1])))
                 .replace("__ADDRESS_OFFSET__", str(address_offset))
                 .replace("__RVA__", str(rva))
                 .replace("__CONSUMER_CODES__", json.dumps({usage: remote_layout.CHROMECAST_REPORT_CODES[key]
                     for key, usage in remote_layout.CHROMECAST_CONSUMER_USAGES.items()}))
                 .replace("__COUNTS__", json.dumps(sorted(HID_COUNTS))))
        try:
            with self._step("attach"):
                self.session = self._attach(frida, pid)
                self.session.on("detached", self._detached)
            with self._step("create_script"):
                self.script = self.session.create_script(source)

            def on_message(message, _data):
                import time
                if message.get("type") != "send":
                    if not self._script_error_recorded:
                        self._script_error_recorded = True
                        from .chromecast_attach_diagnostics_windows import exception_details
                        self._startup_record("script_message", "failed", script_line=message.get("lineNumber", -1),
                            details=exception_details(RuntimeError(str(message.get('description', '')))))
                    self._put(("error", "hid_callback_failed", 0.0))
                    return
                item = message.get("payload", {})
                if item.get("kind") == "hid_setup":
                    if item.get("step") in ("runtime_module", "runtime_hook"):
                        self._startup_record(item["step"], item.get("state"), reason=item.get("reason"),
                                             details=item.get("details"))
                    return
                if item.get("kind") == "hid_flow":
                    self._store_diagnostic(item)
                    return
                if item.get("kind") == "report":
                    self._put(("report", item.get("value"), time.monotonic()))
                elif item.get("kind") == "hook_ready":
                    self._put(("hook_ready", "", time.monotonic()))
                elif item.get("kind") == "error":
                    self._put(("error", item.get("reason", "hid_callback_failed"), 0.0))

            with self._step("load_script"):
                self.script.on("message", on_message)
                self.script.load()
            with self._step("verify_source"):
                if selected_host_and_address(self.entity) != (pid, address):
                    raise TapError("hid_source_unconfirmed")
        except Exception:
            self._startup_cleanup_ok = self.close(startup=True)
            raise

    def _attach(self, frida, pid):
        from . import chromecast_gadget_windows as gadget

        if self._abort_start.is_set():
            raise TapError("hid_source_unconfirmed")
        # Reuse Gadget only when this exact host already loaded it or has an
        # uncertain injection. A new host may still succeed through direct Frida.
        selected_address = isinstance(self.address, str) and re.fullmatch(r"[0-9a-f]{12}", self.address)
        if selected_address and gadget.loaded_or_pending_for_host(pid):
            return self._attach_gadget(frida, gadget, pid)
        evidence = None
        try:
            from .chromecast_attach_diagnostics_windows import BoundedAttachEvidence
            evidence = BoundedAttachEvidence(pid, self._startup_record)
        except Exception as error:
            self._startup_record('attach_evidence', 'failed', error=error)
        def sample(operation):
            if evidence is None:
                return
            try:
                operation()
            except Exception as error:
                self._startup_record('attach_evidence', 'failed', error=error)
        if evidence is not None:
            sample(evidence.begin)
        error = None
        session = None
        try:
            session = frida.attach(pid)
        except Exception as caught:
            error = caught
        finally:
            if evidence is not None:
                sample(lambda: evidence.finish(error))
        if error is None:
            return session
        nonresponding = getattr(frida, "ProcessNotRespondingError", None)
        if selected_address and isinstance(nonresponding, type) and isinstance(error, nonresponding):
            if self._abort_start.is_set():
                raise TapError("hid_source_unconfirmed") from error
            return self._attach_gadget(frida, gadget, pid)
        raise error

    def _attach_gadget(self, frida, gadget, pid):
        with self._step("gadget_fallback"):
            if self._abort_start.is_set():
                raise gadget.GadgetAttachError("gadget_cancelled")
            session, device, device_address = gadget.attach(
                frida, pid, self.address,
                lambda: selected_host_and_address(self.entity),
                cancelled=self._abort_start.is_set,
            )
            self._gadget_device = (device, device_address)
            return session

    def _detached(self, reason=None, crash=None):
        from .chromecast_observation import HID_DETAIL_CHOICES
        reason = reason if reason in HID_DETAIL_CHOICES['detach_reason'] else 'unknown'
        self._startup_record('session_detached', 'success',
                             details={'detach_reason': reason, 'crash_present': int(crash is not None)})
        self._put(("error", "hid_host_detached", 0.0))

    def poll(self):
        result = []
        if self.overflow.is_set():
            result.append(("error", "hid_queue_overflow", 0.0))
        else:
            for _ in range(128):
                try:
                    result.append(self.events.get_nowait())
                except queue.Empty:
                    break
        with self._diagnostic_lock:
            if self._diagnostic is not None:
                # Preserve the failure snapshot before an error ends the worker.
                result.insert(0, ("hid_flow", self._diagnostic, 0.0))
                self._diagnostic = None
        return result

    def source_alive(self):
        return selected_host_and_address(self.entity) == (self.pid, self.address)

    def close(self, *, diagnostic=None, startup=False):
        succeeded = self._startup_cleanup_ok

        def invoke(target, method, *args):
            call = getattr(target, method)
            if self._gadget_device is None:
                return call(*args)
            import frida
            from . import chromecast_gadget_windows as gadget
            return gadget.bounded_remote_cleanup(
                frida, lambda cancel: call(*args, cancellable=cancel))
        def report(stage, phase):
            if diagnostic is not None:
                try:
                    diagnostic(stage, phase)
                except Exception:
                    pass
        if self.script is not None:
            started = time.monotonic()
            report("hid_script_unload", "begin")
            try:
                invoke(self.script, "unload")
            except Exception as error:
                succeeded = False
                report("hid_script_unload", "failed")
                self._startup_record("startup_script_unload" if startup else "cleanup_script_unload",
                                     "failed", started=started, error=error)
            else:
                report("hid_script_unload", "done")
            self.script = None
        if self.session is not None:
            started = time.monotonic()
            report("hid_session_detach", "begin")
            try:
                invoke(self.session, "detach")
            except Exception as error:
                succeeded = False
                report("hid_session_detach", "failed")
                self._startup_record("startup_session_detach" if startup else "cleanup_session_detach",
                                     "failed", started=started, error=error)
            else:
                report("hid_session_detach", "done")
            self.session = None
        if self._gadget_device is not None:
            started = time.monotonic()
            try:
                import frida
                invoke(frida.get_device_manager(), "remove_remote_device", self._gadget_device[1])
            except Exception as error:
                succeeded = False
                self._startup_record("cleanup_gadget_device_remove", "failed",
                                     started=started, error=error)
            self._gadget_device = None
        return succeeded
