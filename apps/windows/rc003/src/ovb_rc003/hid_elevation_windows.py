"""Pre-authorized elevated helper lifecycle for the RC003 HID tap.

The desktop application stays at normal integrity.  A user-approved setup
copies the narrow helper to Program Files and registers one on-demand Task
Scheduler action for the current administrator account.  The task accepts no
target PID or executable path: the elevated helper resolves and validates the
current RC003 WUDFHost itself before injecting the pinned Gadget.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import html
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence


LEGACY_TASK_NAME = r"\RemoteMic\RC003\HidTapInjector"
LEGACY_HELPER_RELATIVE_PATH = (
    Path("RemoteMic") / "RC003" / "HidHelper" / "RemoteMicRC003HidHelper.exe"
)
TASK_NAME_PREFIX = r"\RemoteMicRC003-HidTap-"
PROTECTED_NAMESPACE = "HidHelperUsersV1"
HELPER_EXE_NAME = "RemoteMicRC003HidHelper.exe"
HELPER_BUNDLE_RELATIVE_PATH = Path("_internal") / HELPER_EXE_NAME
INSTALL_FLAG = "--install-task"
UNINSTALL_FLAG = "--uninstall-task"
INJECT_FLAG = "--inject"
SELF_CHECK_FLAG = "--self-check"
REQUEST_SID_FLAG = "--request-sid"
TASK_EXECUTION_LIMIT = "PT30S"
_HELPER_OPERATION_MUTEX_NAME = r"Global\RemoteMicRC003_HidHelperOperation"
_HELPER_MAINTENANCE_LOCK_TIMEOUT_SECONDS = 110.0
_HELPER_INJECT_LOCK_TIMEOUT_SECONDS = 2.0
_REGISTERED_TASK_COMPLETION_TIMEOUT_SECONDS = 30.0
_REGISTERED_TASK_POLL_SECONDS = 0.05
_ELEVATED_PROCESS_TERMINATION_WAIT_MS = 10_000

MANIFEST_SCHEMA_VERSION = 1
HELPER_PROTOCOL_VERSION = 1
# Generation 5 adds thread-safe Task Scheduler COM use and bounded injection
# lock behavior. Task contract 4 accepts only the empty Triggers container
# that Windows adds while normalizing an otherwise on-demand-only task.
HELPER_GENERATION = 5
TASK_CONTRACT_VERSION = 4
MANIFEST_FILENAME = "helper-manifest.json"

TASK_CREATE_OR_UPDATE = 0x6
TASK_DONT_ADD_PRINCIPAL_ACE = 0x10
TASK_LOGON_INTERACTIVE_TOKEN = 3
TASK_SECURITY_INFORMATION = 0x7
TASK_ENUM_HIDDEN = 0x1

HELPER_EXIT_OK = 0
HELPER_EXIT_REQUIRES_ADMIN = 3
HELPER_EXIT_VALIDATION_FAILED = 4
HELPER_EXIT_UNEXPECTED_FAILURE = 5
HELPER_EXIT_NEWER_PRESERVED = 6
HELPER_EXIT_OPERATION_BUSY = 7

_NEWER_HELPER_DETAILS = frozenset(
    {
        "helper_manifest_newer_schema",
        "helper_protocol_newer",
        "helper_task_contract_newer",
        "helper_available_newer_generation",
        "newer_helper_invalid",
        "newer_helper_preserved",
    }
)

TASK_XML_NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"
_SID_PATTERN = re.compile(r"S-1-(?:\d+-){1,14}\d+", re.IGNORECASE)
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_GENERATION_DIRECTORY_PATTERN = re.compile(
    r"g(?P<generation>\d{4,})-(?P<hash_prefix>[0-9a-f]{12})"
)


class HidElevationError(RuntimeError):
    """Sanitized failure raised to the normal-integrity bridge."""


@dataclass(frozen=True)
class HidHelperState:
    available: bool
    detail: str = ""


def is_newer_helper_state(state: HidHelperState) -> bool:
    return state.detail in _NEWER_HELPER_DETAILS


@dataclass(frozen=True)
class HidHelperManifest:
    schema_version: int
    owner_sid: str
    helper_protocol_version: int
    helper_generation: int
    helper_sha256: str
    helper_relative_path: str
    task_contract_version: int
    task_name: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "owner_sid": self.owner_sid,
            "helper_protocol_version": self.helper_protocol_version,
            "helper_generation": self.helper_generation,
            "helper_sha256": self.helper_sha256,
            "helper_relative_path": self.helper_relative_path,
            "task_contract_version": self.task_contract_version,
            "task_name": self.task_name,
        }


@dataclass(frozen=True)
class _RegisteredTaskSnapshot:
    xml_text: str
    security_sddl: str


@dataclass(frozen=True)
class _ValidatedHelperTarget:
    path: Path
    generation: int
    hash_valid: bool = True


class _SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


class _TOKEN_USER(ctypes.Structure):
    _fields_ = [("User", _SID_AND_ATTRIBUTES)]


class _TOKEN_ELEVATION(ctypes.Structure):
    _fields_ = [("TokenIsElevated", wintypes.DWORD)]


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]


_FOLDERID_PROGRAM_FILES = _GUID(
    0x905E63B6,
    0xC1BF,
    0x494E,
    (ctypes.c_ubyte * 8)(0xB2, 0x9C, 0x65, 0xB7, 0x32, 0xD3, 0xD2, 0x1A),
)

_TOKEN_QUERY = 0x0008
_TOKEN_USER_INFORMATION = 1
_TOKEN_ELEVATION_TYPE_INFORMATION = 18
_TOKEN_ELEVATION_INFORMATION = 20
_TOKEN_ELEVATION_TYPE_LIMITED = 3


def _is_windows() -> bool:
    return os.name == "nt"


def _open_current_process_token() -> tuple[object, object]:
    if not _is_windows():
        raise HidElevationError("windows_only")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    advapi32.OpenProcessToken.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    )
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)
    ):
        raise HidElevationError("current_user_token_unavailable")
    return token, kernel32


def _token_information(information_class: int) -> bytes:
    token, kernel32 = _open_current_process_token()
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi32.GetTokenInformation.argtypes = (
        wintypes.HANDLE,
        ctypes.c_uint32,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    try:
        required = wintypes.DWORD()
        advapi32.GetTokenInformation(
            token,
            information_class,
            None,
            0,
            ctypes.byref(required),
        )
        if required.value <= 0:
            raise HidElevationError("current_user_token_information_unavailable")
        buffer = ctypes.create_string_buffer(required.value)
        if not advapi32.GetTokenInformation(
            token,
            information_class,
            buffer,
            required.value,
            ctypes.byref(required),
        ):
            raise HidElevationError("current_user_token_information_unavailable")
        return bytes(buffer.raw[: required.value])
    finally:
        kernel32.CloseHandle(token)


def query_process_elevated() -> bool:
    """Return the current token state or fail when it cannot be proven."""

    if not _is_windows():
        return False
    try:
        raw = _token_information(_TOKEN_ELEVATION_INFORMATION)
        elevation = _TOKEN_ELEVATION.from_buffer_copy(raw)
        return bool(elevation.TokenIsElevated)
    except HidElevationError:
        raise
    except (OSError, ValueError) as exc:
        raise HidElevationError(
            "current_user_elevation_status_unavailable"
        ) from exc


def is_process_elevated() -> bool:
    try:
        return query_process_elevated()
    except HidElevationError:
        return False


def token_elevation_type() -> int:
    raw = _token_information(_TOKEN_ELEVATION_TYPE_INFORMATION)
    if len(raw) < ctypes.sizeof(wintypes.DWORD):
        raise HidElevationError("current_user_elevation_type_unavailable")
    return int(wintypes.DWORD.from_buffer_copy(raw).value)


def can_current_user_self_elevate() -> bool:
    if not _is_windows():
        return False
    if is_process_elevated():
        return True
    try:
        return token_elevation_type() == _TOKEN_ELEVATION_TYPE_LIMITED
    except (HidElevationError, OSError, ValueError):
        return False


def _program_files_root(environ: Optional[Mapping[str, str]] = None) -> Path:
    if environ is not None:
        raw = environ.get("PROGRAMW6432") or environ.get("PROGRAMFILES")
        if not raw:
            raise HidElevationError("program_files_unavailable")
        return Path(raw)
    if not _is_windows():
        raise HidElevationError("windows_only")
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    shell32.SHGetKnownFolderPath.argtypes = (
        ctypes.POINTER(_GUID),
        wintypes.DWORD,
        wintypes.HANDLE,
        ctypes.POINTER(ctypes.c_wchar_p),
    )
    shell32.SHGetKnownFolderPath.restype = ctypes.c_long
    ole32.CoTaskMemFree.argtypes = (ctypes.c_void_p,)
    path_pointer = ctypes.c_wchar_p()
    result = int(
        shell32.SHGetKnownFolderPath(
            ctypes.byref(_FOLDERID_PROGRAM_FILES),
            0,
            None,
            ctypes.byref(path_pointer),
        )
    )
    if result != 0 or not path_pointer.value:
        raise HidElevationError("program_files_unavailable")
    try:
        return Path(path_pointer.value)
    finally:
        ole32.CoTaskMemFree(ctypes.cast(path_pointer, ctypes.c_void_p))


def canonical_user_sid(user_sid: str) -> str:
    sid = str(user_sid).strip().upper()
    if _SID_PATTERN.fullmatch(sid) is None:
        raise HidElevationError("current_user_sid_invalid")
    return sid


def sid_key(user_sid: str) -> str:
    sid = canonical_user_sid(user_sid)
    return hashlib.sha256(sid.encode("ascii")).hexdigest()[:16]


def task_name_for_sid(user_sid: str) -> str:
    return f"{TASK_NAME_PREFIX}{sid_key(user_sid)}"


def protected_owner_root(
    user_sid: str,
    *,
    program_files_root: Optional[Path] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> Path:
    root = program_files_root or _program_files_root(environ)
    return (
        Path(root)
        / "RemoteMic"
        / "RC003"
        / PROTECTED_NAMESPACE
        / sid_key(user_sid)
    )


def helper_relative_path(
    helper_sha256: str,
    *,
    generation: int = HELPER_GENERATION,
) -> Path:
    digest = str(helper_sha256).strip().lower()
    if _SHA256_PATTERN.fullmatch(digest) is None:
        raise HidElevationError("helper_sha256_invalid")
    if not isinstance(generation, int) or isinstance(generation, bool) or generation <= 0:
        raise HidElevationError("helper_generation_invalid")
    return (
        Path("generations")
        / f"g{generation:04d}-{digest[:12]}"
        / HELPER_EXE_NAME
    )


def protected_helper_path(
    user_sid: str,
    helper_sha256: str,
    *,
    generation: int = HELPER_GENERATION,
    program_files_root: Optional[Path] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> Path:
    return protected_owner_root(
        user_sid,
        program_files_root=program_files_root,
        environ=environ,
    ) / helper_relative_path(helper_sha256, generation=generation)


def legacy_protected_helper_path(
    *,
    program_files_root: Optional[Path] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> Path:
    root = program_files_root or _program_files_root(environ)
    return Path(root) / LEGACY_HELPER_RELATIVE_PATH


def helper_manifest_path(
    user_sid: str,
    *,
    program_files_root: Optional[Path] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> Path:
    return protected_owner_root(
        user_sid,
        program_files_root=program_files_root,
        environ=environ,
    ) / MANIFEST_FILENAME


def bundled_helper_path(
    *, frozen: Optional[bool] = None, executable: Optional[str] = None
) -> Optional[Path]:
    if frozen is None:
        frozen = bool(getattr(sys, "frozen", False))
    if not frozen:
        return None
    base = Path(executable or sys.executable).resolve().parent
    return base / HELPER_BUNDLE_RELATIVE_PATH


def bundled_helper_offer_id(
    *, frozen: Optional[bool] = None, executable: Optional[str] = None
) -> str:
    """Identify the helper contract offered by this application copy."""

    try:
        source = bundled_helper_path(frozen=frozen, executable=executable)
        if source is None or not source.is_file():
            return ""
        return (
            f"{MANIFEST_SCHEMA_VERSION}:{HELPER_PROTOCOL_VERSION}:"
            f"{HELPER_GENERATION}:{TASK_CONTRACT_VERSION}"
        )
    except OSError:
        return ""


def is_installed_distribution(
    *, frozen: Optional[bool] = None, executable: Optional[str] = None
) -> bool:
    if frozen is None:
        frozen = bool(getattr(sys, "frozen", False))
    if not frozen:
        return False
    base = Path(executable or sys.executable).resolve().parent
    return (base / "unins000.exe").is_file()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_reparse_point(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    attributes = int(getattr(info, "st_file_attributes", 0))
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _assert_within(path: Path, trusted_root: Path) -> tuple[Path, Path]:
    candidate = _lexical_absolute(path)
    root = _lexical_absolute(trusted_root)
    try:
        common = Path(os.path.commonpath([candidate, root]))
    except ValueError as exc:
        raise HidElevationError("protected_path_outside_program_files") from exc
    if os.path.normcase(str(common)) != os.path.normcase(str(root)):
        raise HidElevationError("protected_path_outside_program_files")
    return candidate, root


def assert_no_reparse_points(path: Path, *, trusted_root: Path) -> None:
    candidate, root = _assert_within(path, trusted_root)
    if os.path.lexists(root) and _is_reparse_point(root):
        raise HidElevationError("protected_path_reparse_point")
    relative = candidate.relative_to(root)
    current = root
    for part in relative.parts:
        current = current / part
        if os.path.lexists(current) and _is_reparse_point(current):
            raise HidElevationError("protected_path_reparse_point")


def _path_security_sddl_text(user_sid: str, *, directory: bool) -> str:
    sid = canonical_user_sid(user_sid)
    if directory:
        return (
            "O:BAG:BAD:P"
            "(A;OICI;FA;;;SY)"
            "(A;OICI;FA;;;BA)"
            f"(A;OICI;GRGX;;;{sid})"
        )
    return (
        "O:BAG:BAD:P"
        "(A;;FA;;;SY)"
        "(A;;FA;;;BA)"
        f"(A;;GR;;;{sid})"
    )


def _apply_path_security(path: Path, *, user_sid: str, directory: bool) -> None:
    if not _is_windows():
        raise HidElevationError("windows_only")
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    )
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = (
        wintypes.BOOL
    )
    advapi32.SetFileSecurityW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_void_p,
    )
    advapi32.SetFileSecurityW.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
    kernel32.LocalFree.restype = ctypes.c_void_p
    descriptor = ctypes.c_void_p()
    descriptor_size = wintypes.DWORD()
    sddl = _path_security_sddl_text(user_sid, directory=directory)
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl,
        1,
        ctypes.byref(descriptor),
        ctypes.byref(descriptor_size),
    ):
        raise HidElevationError("protected_path_acl_failed")
    try:
        security_information = 0x80000000 | 0x1 | 0x2 | 0x4
        if not advapi32.SetFileSecurityW(
            str(_lexical_absolute(path)),
            security_information,
            descriptor,
        ):
            raise HidElevationError("protected_path_acl_failed")
    finally:
        kernel32.LocalFree(descriptor)


def _read_path_security_sddl(path: Path) -> str:
    if not _is_windows():
        raise HidElevationError("windows_only")
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32.GetFileSecurityW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    advapi32.GetFileSecurityW.restype = wintypes.BOOL
    advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = (
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_wchar_p),
        ctypes.POINTER(wintypes.DWORD),
    )
    advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = (
        wintypes.BOOL
    )
    kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
    kernel32.LocalFree.restype = ctypes.c_void_p
    required = wintypes.DWORD()
    security_information = 0x1 | 0x2 | 0x4
    advapi32.GetFileSecurityW(
        str(_lexical_absolute(path)),
        security_information,
        None,
        0,
        ctypes.byref(required),
    )
    if required.value <= 0:
        raise HidElevationError("protected_path_acl_query_failed")
    descriptor = ctypes.create_string_buffer(required.value)
    if not advapi32.GetFileSecurityW(
        str(_lexical_absolute(path)),
        security_information,
        descriptor,
        required.value,
        ctypes.byref(required),
    ):
        raise HidElevationError("protected_path_acl_query_failed")
    sddl_pointer = ctypes.c_wchar_p()
    sddl_length = wintypes.DWORD()
    if not advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
        descriptor,
        1,
        security_information,
        ctypes.byref(sddl_pointer),
        ctypes.byref(sddl_length),
    ):
        raise HidElevationError("protected_path_acl_query_failed")
    try:
        return str(sddl_pointer.value or "")
    finally:
        kernel32.LocalFree(ctypes.cast(sddl_pointer, ctypes.c_void_p))


def _split_ace_flags(raw_flags: str) -> set[str]:
    flags = str(raw_flags).strip().upper()
    if len(flags) % 2:
        return {"invalid"}
    return {flags[index : index + 2] for index in range(0, len(flags), 2)}


def _path_rights_kind(raw_rights: str) -> str:
    rights = str(raw_rights).strip().upper()
    if rights == "FA" or rights == "0X1F01FF":
        return "full"
    if rights in {"GR", "FR", "0X120089", "0X80000000"}:
        return "read"
    if rights in {"GRGX", "FRFX", "0X1200A9", "0XA0000000"}:
        return "read_execute"
    return "other"


def validate_path_security_sddl(
    sddl: str,
    *,
    user_sid: str,
    directory: bool,
) -> bool:
    try:
        expected_sid = canonical_user_sid(user_sid)
    except HidElevationError:
        return False
    normalized = re.sub(r"\s+", "", str(sddl)).upper()
    match = re.fullmatch(
        r"O:([^:()]+)G:([^:()]+)D:([A-Z]*)(.*)", normalized
    )
    if match is None:
        return False
    owner, group, dacl_flags, ace_blob = match.groups()
    if (
        _canonical_acl_sid(owner) != "S-1-5-32-544"
        or _canonical_acl_sid(group) != "S-1-5-32-544"
        or "P" not in dacl_flags
    ):
        return False
    raw_aces = re.findall(r"\(([^()]*)\)", ace_blob)
    if len(raw_aces) != 3 or "".join(f"({ace})" for ace in raw_aces) != ace_blob:
        return False
    seen: dict[str, tuple[set[str], str]] = {}
    expected_flags = {"OI", "CI"} if directory else set()
    for raw_ace in raw_aces:
        fields = raw_ace.split(";")
        if len(fields) != 6:
            return False
        ace_type, ace_flags, rights, object_guid, inherit_guid, trustee = fields
        if ace_type != "A" or object_guid or inherit_guid:
            return False
        normalized_trustee = _canonical_acl_sid(trustee)
        if normalized_trustee in seen:
            return False
        flags = _split_ace_flags(ace_flags)
        if flags != expected_flags:
            return False
        seen[normalized_trustee] = (flags, _path_rights_kind(rights))
    expected_user_rights = "read_execute" if directory else "read"
    return seen == {
        "S-1-5-18": (expected_flags, "full"),
        "S-1-5-32-544": (expected_flags, "full"),
        expected_sid: (expected_flags, expected_user_rights),
    }


def ensure_protected_directory(
    path: Path,
    *,
    user_sid: str,
    trusted_root: Optional[Path] = None,
    security_root: Optional[Path] = None,
) -> Path:
    root = trusted_root or _program_files_root()
    candidate, root = _assert_within(path, root)
    acl_root = _lexical_absolute(security_root or candidate)
    _assert_within(acl_root, root)
    if os.path.normcase(str(os.path.commonpath([candidate, acl_root]))) != os.path.normcase(
        str(acl_root)
    ):
        raise HidElevationError("protected_path_acl_root_invalid")
    assert_no_reparse_points(candidate, trusted_root=root)
    relative = candidate.relative_to(root)
    current = root
    protected = False
    for part in relative.parts:
        current = current / part
        if os.path.normcase(str(current)) == os.path.normcase(str(acl_root)):
            protected = True
        if current.exists():
            if not current.is_dir() or _is_reparse_point(current):
                raise HidElevationError("protected_path_reparse_point")
        else:
            current.mkdir()
        if protected:
            _apply_path_security(current, user_sid=user_sid, directory=True)
    assert_no_reparse_points(candidate, trusted_root=root)
    return candidate


def _manifest_from_mapping(raw: Mapping[str, Any]) -> HidHelperManifest:
    required = {
        "schema_version",
        "owner_sid",
        "helper_protocol_version",
        "helper_generation",
        "helper_sha256",
        "helper_relative_path",
        "task_contract_version",
        "task_name",
    }
    if set(raw) != required:
        raise HidElevationError("helper_manifest_invalid")
    integers = (
        "schema_version",
        "helper_protocol_version",
        "helper_generation",
        "task_contract_version",
    )
    for name in integers:
        value = raw.get(name)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise HidElevationError("helper_manifest_invalid")
    schema = int(raw["schema_version"])
    if schema > MANIFEST_SCHEMA_VERSION:
        raise HidElevationError("helper_manifest_newer_schema")
    if schema != MANIFEST_SCHEMA_VERSION:
        raise HidElevationError("helper_manifest_invalid")
    sid = canonical_user_sid(str(raw["owner_sid"]))
    digest = str(raw["helper_sha256"]).strip().lower()
    if _SHA256_PATTERN.fullmatch(digest) is None:
        raise HidElevationError("helper_manifest_invalid")
    generation = int(raw["helper_generation"])
    relative_text = str(raw["helper_relative_path"])
    relative = Path(relative_text)
    if (
        not relative_text
        or relative.is_absolute()
        or "\\" in relative_text
        or relative.as_posix() != relative_text
        or any(part in {"", ".", ".."} for part in relative.parts)
        or relative != helper_relative_path(digest, generation=generation)
    ):
        raise HidElevationError("helper_manifest_invalid")
    expected_task = task_name_for_sid(sid)
    if str(raw["task_name"]) != expected_task:
        raise HidElevationError("helper_manifest_invalid")
    return HidHelperManifest(
        schema_version=schema,
        owner_sid=sid,
        helper_protocol_version=int(raw["helper_protocol_version"]),
        helper_generation=generation,
        helper_sha256=digest,
        helper_relative_path=relative_text,
        task_contract_version=int(raw["task_contract_version"]),
        task_name=expected_task,
    )


def _load_manifest(path: Path) -> HidHelperManifest:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise HidElevationError("helper_manifest_missing") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HidElevationError("helper_manifest_invalid") from exc
    if not isinstance(raw, dict):
        raise HidElevationError("helper_manifest_invalid")
    return _manifest_from_mapping(raw)


def _manifest_declares_newer_contract(path: Path) -> bool:
    """Preserve a readable future contract even when its shape is unfamiliar."""

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(raw, dict):
        return False
    versions = (
        ("schema_version", MANIFEST_SCHEMA_VERSION),
        ("helper_protocol_version", HELPER_PROTOCOL_VERSION),
        ("helper_generation", HELPER_GENERATION),
        ("task_contract_version", TASK_CONTRACT_VERSION),
    )
    for name, current in versions:
        value = raw.get(name)
        if isinstance(value, int) and not isinstance(value, bool) and value > current:
            return True
    return False


def _build_manifest(user_sid: str, helper_sha256: str) -> HidHelperManifest:
    sid = canonical_user_sid(user_sid)
    digest = str(helper_sha256).strip().lower()
    relative = helper_relative_path(digest)
    return HidHelperManifest(
        schema_version=MANIFEST_SCHEMA_VERSION,
        owner_sid=sid,
        helper_protocol_version=HELPER_PROTOCOL_VERSION,
        helper_generation=HELPER_GENERATION,
        helper_sha256=digest,
        helper_relative_path=relative.as_posix(),
        task_contract_version=TASK_CONTRACT_VERSION,
        task_name=task_name_for_sid(sid),
    )


def _write_manifest_atomic(
    path: Path,
    manifest: HidHelperManifest,
    *,
    _replace: Callable[[object, object], None] = os.replace,
) -> None:
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp"
    )
    try:
        temporary.write_text(
            json.dumps(manifest.as_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        _apply_path_security(
            temporary,
            user_sid=manifest.owner_sid,
            directory=False,
        )
        _replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _read_optional_file_bytes(path: Path) -> Optional[bytes]:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise HidElevationError("helper_manifest_inspection_failed") from exc


def _restore_optional_file_bytes(
    path: Path,
    contents: Optional[bytes],
    *,
    user_sid: str,
) -> None:
    """Restore the exact pre-operation manifest state after a failed mutation."""

    if contents is None:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise HidElevationError("helper_manifest_restore_failed") from exc
        return

    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{time.time_ns()}.restore.tmp"
    )
    try:
        temporary.write_bytes(contents)
        _apply_path_security(temporary, user_sid=user_sid, directory=False)
        os.replace(temporary, path)
    except OSError as exc:
        raise HidElevationError("helper_manifest_restore_failed") from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def current_user_sid() -> str:
    if not _is_windows():
        raise HidElevationError("windows_only")
    token, kernel32 = _open_current_process_token()
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi32.GetTokenInformation.argtypes = (
        wintypes.HANDLE,
        ctypes.c_uint32,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_wchar_p),
    )
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
    kernel32.LocalFree.restype = ctypes.c_void_p
    try:
        required = wintypes.DWORD()
        advapi32.GetTokenInformation(
            token,
            _TOKEN_USER_INFORMATION,
            None,
            0,
            ctypes.byref(required),
        )
        if required.value <= 0:
            raise HidElevationError("current_user_sid_unavailable")
        buffer = ctypes.create_string_buffer(required.value)
        if not advapi32.GetTokenInformation(
            token,
            _TOKEN_USER_INFORMATION,
            buffer,
            required.value,
            ctypes.byref(required),
        ):
            raise HidElevationError("current_user_sid_unavailable")
        token_user = ctypes.cast(
            buffer, ctypes.POINTER(_TOKEN_USER)
        ).contents
        sid_pointer = ctypes.c_wchar_p()
        if not advapi32.ConvertSidToStringSidW(
            token_user.User.Sid, ctypes.byref(sid_pointer)
        ):
            raise HidElevationError("current_user_sid_unavailable")
        try:
            return canonical_user_sid(sid_pointer.value or "")
        finally:
            kernel32.LocalFree(ctypes.cast(sid_pointer, ctypes.c_void_p))
    finally:
        kernel32.CloseHandle(token)


def _lexical_absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def task_definition_xml(
    helper_path: Path,
    user_sid: str,
    *,
    task_name: Optional[str] = None,
) -> str:
    sid = canonical_user_sid(user_sid)
    expected_task_name = task_name or task_name_for_sid(sid)
    path_text = html.escape(str(_lexical_absolute(Path(helper_path))))
    sid_text = html.escape(sid)
    return f"""<?xml version=\"1.0\" encoding=\"UTF-16\"?>
<Task version=\"1.4\" xmlns=\"{TASK_XML_NAMESPACE}\">
  <RegistrationInfo>
    <Description>Remote Mic RC003 narrow HID report injector.</Description>
    <URI>{html.escape(expected_task_name)}</URI>
  </RegistrationInfo>
  <Principals>
    <Principal id=\"Author\">
      <UserId>{sid_text}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>false</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>true</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>{TASK_EXECUTION_LIMIT}</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context=\"Author\">
    <Exec>
      <Command>{path_text}</Command>
      <Arguments>{INJECT_FLAG}</Arguments>
      <WorkingDirectory>{html.escape(str(_lexical_absolute(Path(helper_path)).parent))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def task_security_sddl(user_sid: str) -> str:
    sid = canonical_user_sid(user_sid)
    return (
        "O:BAG:BAD:P"
        "(A;;FA;;;SY)"
        "(A;;FA;;;BA)"
        f"(A;;FRFX;;;{sid})"
    )


def _task_xml_value(root: ET.Element, path: str) -> str:
    node = root.find(path, {"t": TASK_XML_NAMESPACE})
    return "" if node is None or node.text is None else node.text.strip()


def validate_registered_task_xml(
    xml_text: str,
    *,
    helper_path: Path,
    user_sid: str,
    task_name: Optional[str] = None,
) -> bool:
    try:
        root = ET.fromstring(xml_text.lstrip("\ufeff"))
    except (ET.ParseError, TypeError, ValueError):
        return False
    namespace = {"t": TASK_XML_NAMESPACE}
    if root.tag != f"{{{TASK_XML_NAMESPACE}}}Task":
        return False
    actions_nodes = root.findall("./t:Actions", namespace)
    exec_nodes = root.findall("./t:Actions/t:Exec", namespace)
    registration_nodes = root.findall("./t:RegistrationInfo", namespace)
    settings_nodes = root.findall("./t:Settings", namespace)
    principal_containers = root.findall("./t:Principals", namespace)
    principal_nodes = root.findall("./t:Principals/t:Principal", namespace)
    if (
        len(actions_nodes) != 1
        or len(exec_nodes) != 1
        or len(registration_nodes) != 1
        or len(settings_nodes) != 1
        or len(list(actions_nodes[0])) != 1
        or len(principal_containers) != 1
        or len(principal_nodes) != 1
        or len(list(principal_containers[0])) != 1
        or actions_nodes[0].attrib.get("Context") != "Author"
        or principal_nodes[0].attrib.get("id") != "Author"
    ):
        return False
    expected_exec_children = {
        f"{{{TASK_XML_NAMESPACE}}}Command",
        f"{{{TASK_XML_NAMESPACE}}}Arguments",
        f"{{{TASK_XML_NAMESPACE}}}WorkingDirectory",
    }
    if (
        len(list(exec_nodes[0])) != len(expected_exec_children)
        or {child.tag for child in exec_nodes[0]} != expected_exec_children
    ):
        return False
    trigger_nodes = root.findall(".//t:Triggers", namespace)
    if len(trigger_nodes) > 1:
        return False
    if trigger_nodes:
        trigger = trigger_nodes[0]
        if (
            trigger not in list(root)
            or trigger.attrib
            or list(trigger)
            or (trigger.text or "").strip()
        ):
            return False
    if root.find(".//t:RestartOnFailure", namespace) is not None:
        return False
    if root.find(".//t:MaintenanceSettings", namespace) is not None:
        return False
    command = _task_xml_value(root, ".//t:Actions/t:Exec/t:Command")
    arguments = _task_xml_value(root, ".//t:Actions/t:Exec/t:Arguments")
    working_directory = _task_xml_value(
        root, ".//t:Actions/t:Exec/t:WorkingDirectory"
    )
    principal = _task_xml_value(root, ".//t:Principals/t:Principal/t:UserId")
    run_level = _task_xml_value(root, ".//t:Principals/t:Principal/t:RunLevel")
    logon_type = _task_xml_value(root, ".//t:Principals/t:Principal/t:LogonType")
    task_uri = _task_xml_value(root, ".//t:RegistrationInfo/t:URI")
    try:
        sid = canonical_user_sid(user_sid)
    except HidElevationError:
        return False
    expected_task_name = task_name or task_name_for_sid(sid)
    expected = _lexical_absolute(Path(helper_path))
    expected_settings = {
        "MultipleInstancesPolicy": "IgnoreNew",
        "DisallowStartIfOnBatteries": "false",
        "StopIfGoingOnBatteries": "false",
        "AllowHardTerminate": "true",
        "StartWhenAvailable": "false",
        "RunOnlyIfNetworkAvailable": "false",
        "AllowStartOnDemand": "true",
        "Enabled": "true",
        "Hidden": "true",
        "RunOnlyIfIdle": "false",
        "WakeToRun": "false",
        "ExecutionTimeLimit": TASK_EXECUTION_LIMIT,
        "Priority": "7",
    }
    settings_valid = all(
        len(root.findall(f"./t:Settings/t:{name}", namespace)) == 1
        and _task_xml_value(root, f"./t:Settings/t:{name}").casefold()
        == expected.casefold()
        for name, expected in expected_settings.items()
    )
    return (
        os.path.normcase(command) == os.path.normcase(str(expected))
        and arguments == INJECT_FLAG
        and os.path.normcase(working_directory)
        == os.path.normcase(str(expected.parent))
        and principal.casefold() == sid.casefold()
        and run_level == "HighestAvailable"
        and logon_type == "InteractiveToken"
        and settings_valid
        and task_uri == expected_task_name
    )


def _canonical_acl_sid(raw_sid: str) -> str:
    sid = str(raw_sid).strip().upper()
    aliases = {
        "SY": "S-1-5-18",
        "BA": "S-1-5-32-544",
    }
    return aliases.get(sid, sid)


def _task_rights_kind(raw_rights: str) -> str:
    rights = str(raw_rights).strip().upper()
    if rights == "FA" or rights == "0X1F01FF":
        return "full"
    if rights == "FR" or rights == "0X120089":
        return "read"
    if rights in {"FRFX", "GRGX", "0X1200A9", "0XA0000000"}:
        return "read_execute"
    return "other"


def validate_task_security_sddl(sddl: str, *, user_sid: str) -> bool:
    try:
        expected_sid = canonical_user_sid(user_sid)
    except HidElevationError:
        return False
    normalized = re.sub(r"\s+", "", str(sddl)).upper()
    match = re.fullmatch(
        r"O:([^:()]+)G:([^:()]+)D:([A-Z]*)(.*)", normalized
    )
    if match is None:
        return False
    owner, group, dacl_flags, ace_blob = match.groups()
    if (
        _canonical_acl_sid(owner) != "S-1-5-32-544"
        or _canonical_acl_sid(group) != "S-1-5-32-544"
        or "P" not in dacl_flags
    ):
        return False
    raw_aces = re.findall(r"\(([^()]*)\)", ace_blob)
    if len(raw_aces) != 3 or "".join(f"({ace})" for ace in raw_aces) != ace_blob:
        return False
    seen: dict[str, str] = {}
    for raw_ace in raw_aces:
        fields = raw_ace.split(";")
        if len(fields) != 6:
            return False
        ace_type, ace_flags, rights, object_guid, inherit_guid, trustee = fields
        if (
            ace_type != "A"
            or ace_flags
            or object_guid
            or inherit_guid
        ):
            return False
        normalized_trustee = _canonical_acl_sid(trustee)
        if normalized_trustee in seen:
            return False
        seen[normalized_trustee] = _task_rights_kind(rights)
    return seen == {
        "S-1-5-18": "full",
        "S-1-5-32-544": "full",
        expected_sid: "read_execute",
    }


def _task_service_root() -> object:
    if not _is_windows():
        raise HidElevationError("windows_only")
    try:
        import comtypes.client

        service = comtypes.client.CreateObject("Schedule.Service", dynamic=True)
        service.Connect()
        return service.GetFolder("\\")
    except Exception as exc:
        raise HidElevationError("hid_helper_task_service_unavailable") from exc


def _hresult_code(exc: BaseException) -> Optional[int]:
    raw = getattr(exc, "hresult", None)
    if raw is None and getattr(exc, "args", None):
        candidate = exc.args[0]
        raw = candidate if isinstance(candidate, int) else None
    if raw is None:
        return None
    return int(raw) & 0xFFFFFFFF


@contextmanager
def _task_service_session(_root: Optional[object] = None):
    """Use Task Scheduler from any thread with balanced COM initialization."""

    if _root is not None:
        yield _root
        return
    if not _is_windows():
        raise HidElevationError("windows_only")

    try:
        import comtypes
    except Exception as exc:
        raise HidElevationError("hid_helper_task_service_unavailable") from exc

    initialized = False
    try:
        try:
            comtypes.CoInitialize()
            initialized = True
        except OSError as exc:
            # The thread may already use the other COM apartment. It is still
            # initialized and usable; only the matching owner may uninitialize it.
            if _hresult_code(exc) != 0x80010106:  # RPC_E_CHANGED_MODE
                raise HidElevationError(
                    "hid_helper_task_service_unavailable"
                ) from exc
        yield _task_service_root()
    finally:
        if initialized:
            try:
                comtypes.CoUninitialize()
            except Exception:
                pass


def _task_missing_error(exc: BaseException) -> bool:
    raw = getattr(exc, "hresult", None)
    if raw is None and getattr(exc, "args", None):
        candidate = exc.args[0]
        raw = candidate if isinstance(candidate, int) else None
    if raw is None:
        return False
    return (int(raw) & 0xFFFFFFFF) in {0x80070002, 0x8004130F}


def _task_path_parts(task_name: str) -> tuple[tuple[str, ...], str]:
    normalized = str(task_name).strip().replace("/", "\\")
    parts = tuple(part for part in normalized.split("\\") if part)
    if not normalized.startswith("\\") or not parts:
        raise HidElevationError("hid_helper_task_name_invalid")
    return parts[:-1], parts[-1]


def _collection_items(collection: object):
    count = int(collection.Count)
    for index in range(1, count + 1):
        yield collection.Item(index)


def _find_registered_task(root: object, task_name: str) -> Optional[object]:
    folder_parts, leaf_name = _task_path_parts(task_name)
    folder = root
    for part in folder_parts:
        match = None
        for candidate in _collection_items(folder.GetFolders(0)):
            if str(candidate.Name).casefold() == part.casefold():
                match = candidate
                break
        if match is None:
            return None
        folder = match
    for task in _collection_items(folder.GetTasks(TASK_ENUM_HIDDEN)):
        if str(task.Name).casefold() == leaf_name.casefold():
            return task
    return None


def _find_task_folder(root: object, task_name: str) -> tuple[Optional[object], str]:
    folder_parts, leaf_name = _task_path_parts(task_name)
    folder = root
    for part in folder_parts:
        match = None
        for candidate in _collection_items(folder.GetFolders(0)):
            if str(candidate.Name).casefold() == part.casefold():
                match = candidate
                break
        if match is None:
            return None, leaf_name
        folder = match
    return folder, leaf_name


def _read_registered_task(
    task_name: str,
    *,
    _root: Optional[object] = None,
) -> Optional[_RegisteredTaskSnapshot]:
    try:
        with _task_service_session(_root) as root:
            task = _find_registered_task(root, task_name)
            if task is None:
                return None
            return _RegisteredTaskSnapshot(
                xml_text=str(task.Xml),
                security_sddl=str(
                    task.GetSecurityDescriptor(TASK_SECURITY_INFORMATION)
                ),
            )
    except Exception as exc:
        if isinstance(exc, HidElevationError):
            raise
        if _task_missing_error(exc):
            return None
        raise HidElevationError("hid_helper_task_query_failed") from exc


def _register_task(
    task_name: str,
    xml_text: str,
    security_sddl: str,
    *,
    _root: Optional[object] = None,
) -> None:
    try:
        with _task_service_session(_root) as root:
            root.RegisterTask(
                str(task_name),
                str(xml_text),
                TASK_CREATE_OR_UPDATE | TASK_DONT_ADD_PRINCIPAL_ACE,
                None,
                None,
                TASK_LOGON_INTERACTIVE_TOKEN,
                str(security_sddl),
            )
    except Exception as exc:
        if isinstance(exc, HidElevationError):
            raise
        raise HidElevationError("hid_helper_task_registration_failed") from exc


def _delete_task(
    task_name: str,
    *,
    missing_ok: bool,
    _root: Optional[object] = None,
) -> None:
    try:
        with _task_service_session(_root) as root:
            folder, leaf_name = _find_task_folder(root, task_name)
            if folder is None:
                if missing_ok:
                    return
                raise HidElevationError("hid_helper_task_removal_failed")
            if missing_ok and _find_registered_task(root, task_name) is None:
                return
            folder.DeleteTask(leaf_name, 0)
    except Exception as exc:
        if isinstance(exc, HidElevationError):
            raise
        if missing_ok and _task_missing_error(exc):
            return
        raise HidElevationError("hid_helper_task_removal_failed") from exc


def _registered_task_principal_sid(xml_text: str) -> str:
    try:
        root = ET.fromstring(str(xml_text).lstrip("\ufeff"))
    except (ET.ParseError, TypeError, ValueError) as exc:
        raise HidElevationError("legacy_hid_helper_task_invalid") from exc
    namespace = {"t": TASK_XML_NAMESPACE}
    principals = root.findall("./t:Principals/t:Principal", namespace)
    if root.tag != f"{{{TASK_XML_NAMESPACE}}}Task" or len(principals) != 1:
        raise HidElevationError("legacy_hid_helper_task_invalid")
    raw_sid = _task_xml_value(
        root, "./t:Principals/t:Principal/t:UserId"
    )
    try:
        return canonical_user_sid(raw_sid)
    except HidElevationError as exc:
        raise HidElevationError("legacy_hid_helper_task_invalid") from exc


def _cleanup_legacy_contract_for_user(
    user_sid: str,
    *,
    trusted_root: Path,
) -> bool:
    """Remove only the fixed legacy task owned by this Windows account."""

    sid = canonical_user_sid(user_sid)
    snapshot = _read_registered_task(LEGACY_TASK_NAME)
    if snapshot is not None:
        try:
            owner_sid = _registered_task_principal_sid(snapshot.xml_text)
        except HidElevationError as exc:
            raise HidElevationError(
                "legacy_hid_helper_task_owner_unknown"
            ) from exc
        if owner_sid != sid:
            return False

    target = legacy_protected_helper_path(program_files_root=trusted_root)
    assert_no_reparse_points(target, trusted_root=trusted_root)
    if target.exists():
        if not target.is_file():
            raise HidElevationError("legacy_protected_helper_invalid")

    snapshot_contract_valid = False
    if snapshot is not None:
        snapshot_contract_valid = validate_registered_task_xml(
            snapshot.xml_text,
            helper_path=target,
            user_sid=sid,
            task_name=LEGACY_TASK_NAME,
        )
        _delete_task(LEGACY_TASK_NAME, missing_ok=False)
        if _read_registered_task(LEGACY_TASK_NAME) is not None:
            raise HidElevationError("legacy_hid_helper_task_removal_failed")

    try:
        if target.exists():
            target.unlink()
    except OSError as exc:
        if snapshot is not None and snapshot_contract_valid:
            try:
                _restore_registered_task(LEGACY_TASK_NAME, snapshot)
            except Exception as rollback_exc:
                raise HidElevationError(
                    "legacy_hid_helper_cleanup_rollback_failed"
                ) from rollback_exc
        raise HidElevationError(
            "legacy_protected_helper_removal_failed"
        ) from exc
    try:
        target.parent.rmdir()
    except OSError:
        pass
    return snapshot is not None


def _run_task(task_name: str, *, _root: Optional[object] = None) -> None:
    try:
        with _task_service_session(_root) as root:
            task = _find_registered_task(root, task_name)
            if task is None:
                raise HidElevationError("hid_helper_task_start_failed")
            running = task.Run("")
            if not hasattr(running, "State"):
                raise HidElevationError("hid_helper_task_result_unavailable")
            deadline = time.monotonic() + _REGISTERED_TASK_COMPLETION_TIMEOUT_SECONDS
            while int(running.State) in {2, 4}:  # queued or running
                if time.monotonic() >= deadline:
                    raise HidElevationError("hid_helper_task_timeout")
                time.sleep(_REGISTERED_TASK_POLL_SECONDS)
            exit_code = int(task.LastTaskResult)
            if exit_code == HELPER_EXIT_OPERATION_BUSY:
                raise HidElevationError("hid_helper_operation_busy")
            if exit_code != HELPER_EXIT_OK:
                raise HidElevationError(f"hid_helper_task_exit_{exit_code}")
    except Exception as exc:
        if isinstance(exc, HidElevationError):
            raise
        raise HidElevationError("hid_helper_task_start_failed") from exc


def _trusted_registered_task_target(
    snapshot: _RegisteredTaskSnapshot,
    *,
    user_sid: str,
    owner_root: Path,
    trusted_root: Path,
    _read_acl: Callable[[Path], str] = _read_path_security_sddl,
) -> _ValidatedHelperTarget:
    """Recover the fixed helper path without trusting a missing manifest."""

    sid = canonical_user_sid(user_sid)
    if not validate_task_security_sddl(snapshot.security_sddl, user_sid=sid):
        raise HidElevationError("hid_helper_task_acl_invalid")
    try:
        task_root = ET.fromstring(str(snapshot.xml_text).lstrip("\ufeff"))
    except (ET.ParseError, TypeError, ValueError) as exc:
        raise HidElevationError("hid_helper_task_invalid") from exc
    command = _task_xml_value(task_root, ".//t:Actions/t:Exec/t:Command")
    if not command:
        raise HidElevationError("hid_helper_task_invalid")
    target, root = _assert_within(Path(command), owner_root)
    assert_no_reparse_points(target, trusted_root=trusted_root)
    relative = target.relative_to(root)
    if (
        len(relative.parts) != 3
        or relative.parts[0] != "generations"
        or relative.parts[2] != HELPER_EXE_NAME
    ):
        raise HidElevationError("hid_helper_task_invalid")
    match = _GENERATION_DIRECTORY_PATTERN.fullmatch(relative.parts[1])
    if match is None:
        raise HidElevationError("hid_helper_task_invalid")
    generation = int(match.group("generation"))
    hash_prefix = match.group("hash_prefix")
    if relative.parts[1] != f"g{generation:04d}-{hash_prefix}":
        raise HidElevationError("hid_helper_task_invalid")
    if not validate_registered_task_xml(
        snapshot.xml_text,
        helper_path=target,
        user_sid=sid,
        task_name=task_name_for_sid(sid),
    ):
        raise HidElevationError("hid_helper_task_invalid")
    hash_valid = False
    if os.path.lexists(target):
        if not target.is_file():
            raise HidElevationError("protected_helper_orphan_invalid")
        if not validate_path_security_sddl(
            _read_acl(target), user_sid=sid, directory=False
        ):
            raise HidElevationError("protected_helper_acl_invalid")
        try:
            digest = _sha256(target)
        except OSError as exc:
            raise HidElevationError("protected_helper_inspection_failed") from exc
        hash_valid = digest.startswith(hash_prefix)
    return _ValidatedHelperTarget(target, generation, hash_valid)


def _installation_requires_elevated_removal(
    user_sid: str,
    *,
    program_files_root: Optional[Path] = None,
    _read_task: Callable[[str], Optional[_RegisteredTaskSnapshot]] = (
        _read_registered_task
    ),
) -> bool:
    """Return whether this account still owns a privileged helper entry."""

    sid = canonical_user_sid(user_sid)
    if _read_task(task_name_for_sid(sid)) is not None:
        return True
    legacy = _read_task(LEGACY_TASK_NAME)
    if legacy is not None:
        try:
            legacy_sid = _registered_task_principal_sid(legacy.xml_text)
        except HidElevationError:
            return True
        if legacy_sid == sid:
            return True
    else:
        legacy_target = legacy_protected_helper_path(
            program_files_root=program_files_root
        )
        try:
            if os.path.lexists(legacy_target):
                return True
        except OSError:
            return True
    root = protected_owner_root(sid, program_files_root=program_files_root)
    try:
        generations = root / "generations"
        if not os.path.lexists(generations):
            return False
        if not generations.is_dir():
            return True
        for generation_dir in generations.iterdir():
            if os.path.lexists(generation_dir / HELPER_EXE_NAME):
                return True
    except OSError:
        return True
    return False


def _validated_helper_targets(
    owner_root: Path,
    *,
    user_sid: str,
    trusted_root: Path,
    _read_acl: Callable[[Path], str] = _read_path_security_sddl,
    trusted_targets: Sequence[Path] = (),
) -> list[_ValidatedHelperTarget]:
    """Inventory helper files whose path, ACL and hash all match this contract."""

    sid = canonical_user_sid(user_sid)
    root = Path(owner_root)
    assert_no_reparse_points(root, trusted_root=trusted_root)
    if not os.path.lexists(root):
        return []
    if not root.is_dir():
        raise HidElevationError("protected_helper_orphan_invalid")
    if not validate_path_security_sddl(
        _read_acl(root), user_sid=sid, directory=True
    ):
        raise HidElevationError("protected_helper_acl_invalid")

    generations = root / "generations"
    assert_no_reparse_points(generations, trusted_root=trusted_root)
    if not os.path.lexists(generations):
        return []
    if not generations.is_dir():
        raise HidElevationError("protected_helper_orphan_invalid")
    if not validate_path_security_sddl(
        _read_acl(generations), user_sid=sid, directory=True
    ):
        raise HidElevationError("protected_helper_acl_invalid")

    trusted_target_keys: set[str] = set()
    for trusted_target in trusted_targets:
        candidate, _root = _assert_within(Path(trusted_target), root)
        trusted_target_keys.add(os.path.normcase(str(candidate)))

    targets: list[_ValidatedHelperTarget] = []
    try:
        generation_directories = sorted(
            generations.iterdir(), key=lambda path: path.name.casefold()
        )
    except OSError as exc:
        raise HidElevationError("protected_helper_inspection_failed") from exc
    for generation_dir in generation_directories:
        target = generation_dir / HELPER_EXE_NAME
        try:
            target_exists = os.path.lexists(target)
        except OSError as exc:
            raise HidElevationError("protected_helper_inspection_failed") from exc
        if not target_exists:
            continue
        assert_no_reparse_points(target, trusted_root=trusted_root)
        match = _GENERATION_DIRECTORY_PATTERN.fullmatch(generation_dir.name)
        if match is None or not generation_dir.is_dir() or not target.is_file():
            raise HidElevationError("protected_helper_orphan_invalid")
        generation = int(match.group("generation"))
        hash_prefix = match.group("hash_prefix")
        if (
            generation <= 0
            or generation_dir.name
            != f"g{generation:04d}-{hash_prefix}"
        ):
            raise HidElevationError("protected_helper_orphan_invalid")
        for path, directory in ((generation_dir, True), (target, False)):
            if not validate_path_security_sddl(
                _read_acl(path), user_sid=sid, directory=directory
            ):
                raise HidElevationError("protected_helper_acl_invalid")
        try:
            digest = _sha256(target)
        except OSError as exc:
            raise HidElevationError("protected_helper_inspection_failed") from exc
        hash_valid = digest.startswith(hash_prefix)
        target_key = os.path.normcase(str(_lexical_absolute(target)))
        if not hash_valid and target_key not in trusted_target_keys:
            raise HidElevationError("protected_helper_hash_mismatch")
        targets.append(_ValidatedHelperTarget(target, generation, hash_valid))
    return targets


def _validated_orphaned_helper_targets(
    owner_root: Path,
    *,
    user_sid: str,
    trusted_root: Path,
    _read_acl: Callable[[Path], str] = _read_path_security_sddl,
) -> list[Path]:
    """Find removable helpers, preserving every generation newer than this build."""

    targets = _validated_helper_targets(
        owner_root,
        user_sid=user_sid,
        trusted_root=trusted_root,
        _read_acl=_read_acl,
    )
    if any(target.generation > HELPER_GENERATION for target in targets):
        raise HidElevationError("newer_helper_preserved")
    return [target.path for target in targets]


def inspect_installed_helper(
    *,
    frozen: Optional[bool] = None,
    executable: Optional[str] = None,
    user_sid: Optional[str] = None,
    owner_root: Optional[Path] = None,
    program_files_root: Optional[Path] = None,
    _read_task: Callable[..., Optional[_RegisteredTaskSnapshot]] = (
        _read_registered_task
    ),
    _read_acl: Callable[[Path], str] = _read_path_security_sddl,
) -> HidHelperState:
    if not _is_windows():
        return HidHelperState(False, "windows_only")
    bundled = bundled_helper_path(frozen=frozen, executable=executable)
    if bundled is None:
        return HidHelperState(False, "source_runtime")
    if not bundled.is_file():
        return HidHelperState(False, "bundled_helper_missing")
    try:
        sid = canonical_user_sid(user_sid or current_user_sid())
        trusted_root = Path(program_files_root or _program_files_root())
        root = Path(
            owner_root
            or protected_owner_root(sid, program_files_root=trusted_root)
        )
        manifest_file = root / MANIFEST_FILENAME
        assert_no_reparse_points(root, trusted_root=trusted_root)
        assert_no_reparse_points(manifest_file, trusted_root=trusted_root)
        manifest = _load_manifest(manifest_file)
        state = _validate_installed_contract(
            manifest,
            user_sid=sid,
            owner_root=root,
            trusted_root=trusted_root,
            manifest_path=manifest_file,
            _read_task=_read_task,
            _read_acl=_read_acl,
        )
        if manifest.helper_generation > HELPER_GENERATION:
            if state.available:
                return HidHelperState(True, "helper_available_newer_generation")
            return HidHelperState(False, "newer_helper_invalid")
        if state.available:
            target = root / Path(manifest.helper_relative_path)
            try:
                inventory = _validated_helper_targets(
                    root,
                    user_sid=sid,
                    trusted_root=trusted_root,
                    _read_acl=_read_acl,
                    trusted_targets=[target],
                )
                target_key = os.path.normcase(str(_lexical_absolute(target)))
                if any(item.generation > HELPER_GENERATION for item in inventory):
                    return HidHelperState(True, "newer_helper_preserved")
                if any(
                    os.path.normcase(str(_lexical_absolute(item.path))) != target_key
                    for item in inventory
                ):
                    return HidHelperState(True, "helper_cleanup_pending")
                legacy = _read_task(LEGACY_TASK_NAME)
                legacy_target = legacy_protected_helper_path(
                    program_files_root=trusted_root
                )
                legacy_cleanup_pending = False
                if legacy is not None:
                    try:
                        legacy_cleanup_pending = (
                            _registered_task_principal_sid(legacy.xml_text) == sid
                        )
                    except HidElevationError:
                        legacy_cleanup_pending = True
                elif os.path.lexists(legacy_target):
                    legacy_cleanup_pending = True
                if legacy_cleanup_pending:
                    return HidHelperState(True, "helper_cleanup_pending")
            except (HidElevationError, OSError):
                # The active contract is already fully verified. Stale cleanup
                # discovery must not disable working direction remapping.
                pass
        return state
    except HidElevationError as exc:
        detail = str(exc)
        if detail in {"helper_manifest_invalid", "helper_manifest_newer_schema"}:
            if _manifest_declares_newer_contract(manifest_file):
                return HidHelperState(False, "newer_helper_invalid")
        return HidHelperState(False, detail)
    except OSError:
        return HidHelperState(False, "hid_helper_inspection_failed")


def _validate_installed_contract(
    manifest: HidHelperManifest,
    *,
    user_sid: str,
    owner_root: Path,
    trusted_root: Path,
    manifest_path: Path,
    _read_task: Callable[..., Optional[_RegisteredTaskSnapshot]],
    _read_acl: Callable[[Path], str],
) -> HidHelperState:
    sid = canonical_user_sid(user_sid)
    if manifest.owner_sid != sid:
        return HidHelperState(False, "helper_manifest_owner_mismatch")
    if manifest.helper_protocol_version > HELPER_PROTOCOL_VERSION:
        return HidHelperState(False, "helper_protocol_newer")
    if manifest.task_contract_version > TASK_CONTRACT_VERSION:
        return HidHelperState(False, "helper_task_contract_newer")
    if (
        manifest.helper_protocol_version < HELPER_PROTOCOL_VERSION
        or manifest.task_contract_version < TASK_CONTRACT_VERSION
        or manifest.helper_generation < HELPER_GENERATION
    ):
        return HidHelperState(False, "protected_helper_outdated")
    target = owner_root / Path(manifest.helper_relative_path)
    try:
        assert_no_reparse_points(target, trusted_root=trusted_root)
    except HidElevationError:
        return HidHelperState(False, "protected_helper_reparse_point")
    if not target.is_file():
        return HidHelperState(False, "protected_helper_missing")
    try:
        if _sha256(target) != manifest.helper_sha256:
            return HidHelperState(False, "protected_helper_hash_mismatch")
        for directory in (owner_root, target.parent):
            if not validate_path_security_sddl(
                _read_acl(directory), user_sid=sid, directory=True
            ):
                return HidHelperState(False, "protected_helper_acl_invalid")
        for file_path in (manifest_path, target):
            if not validate_path_security_sddl(
                _read_acl(file_path), user_sid=sid, directory=False
            ):
                return HidHelperState(False, "protected_helper_acl_invalid")
        task = _read_task(manifest.task_name)
    except HidElevationError as exc:
        return HidHelperState(False, str(exc))
    except OSError:
        return HidHelperState(False, "protected_helper_inspection_failed")
    if task is None:
        return HidHelperState(False, "hid_helper_task_missing")
    if not validate_registered_task_xml(
        task.xml_text,
        helper_path=target,
        user_sid=sid,
        task_name=manifest.task_name,
    ):
        return HidHelperState(False, "hid_helper_task_invalid")
    if not validate_task_security_sddl(task.security_sddl, user_sid=sid):
        return HidHelperState(False, "hid_helper_task_acl_invalid")
    return HidHelperState(True)


def run_registered_injector(
    expected_pid: int,
    *,
    _host_pid: Optional[Callable[[], Optional[int]]] = None,
    _run_registered_task: Callable[[str], None] = _run_task,
) -> None:
    if not isinstance(expected_pid, int) or isinstance(expected_pid, bool) or expected_pid <= 0:
        raise ValueError("expected PID must be a positive integer")
    if not _is_windows():
        raise HidElevationError("windows_only")
    if _host_pid is None:
        from .frida_hid_tap_runtime import find_rc003_hidogatt_host_pid

        _host_pid = find_rc003_hidogatt_host_pid
    if _host_pid() != expected_pid:
        raise HidElevationError("hid_helper_host_changed")
    state = inspect_installed_helper()
    if not state.available:
        raise HidElevationError(state.detail or "hid_helper_unavailable")
    _run_registered_task(task_name_for_sid(current_user_sid()))


def _copy_verified_helper(
    source: Path,
    destination: Path,
    *,
    user_sid: str,
) -> None:
    source = _lexical_absolute(source)
    if not source.is_file():
        raise FileNotFoundError(source)
    temporary = destination.with_name(
        f".{destination.name}.{os.getpid()}.{time.time_ns()}.tmp"
    )
    try:
        shutil.copy2(source, temporary)
        if _sha256(source) != _sha256(temporary):
            raise HidElevationError("helper_copy_hash_mismatch")
        _apply_path_security(temporary, user_sid=user_sid, directory=False)
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _remove_helper_generation(target: Path, *, owner_root: Path) -> None:
    try:
        target.unlink(missing_ok=True)
    except OSError as exc:
        raise HidElevationError("protected_helper_removal_failed") from exc
    if os.path.lexists(target):
        raise HidElevationError("protected_helper_removal_failed")
    current = target.parent
    stop = _lexical_absolute(owner_root)
    while os.path.normcase(str(_lexical_absolute(current))) != os.path.normcase(
        str(stop)
    ):
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def _restore_registered_task(
    task_name: str,
    snapshot: Optional[_RegisteredTaskSnapshot],
) -> None:
    if snapshot is None:
        _delete_task(task_name, missing_ok=True)
        return
    _register_task(task_name, snapshot.xml_text, snapshot.security_sddl)


def _validate_requesting_sid(request_sid: Optional[str]) -> str:
    if request_sid is None:
        raise HidElevationError("request_sid_missing")
    requested = canonical_user_sid(request_sid)
    actual = canonical_user_sid(current_user_sid())
    if requested != actual:
        raise HidElevationError("request_sid_mismatch")
    return requested


def install_task(
    *,
    source_executable: Optional[Path] = None,
    request_sid: Optional[str] = None,
    owner_root: Optional[Path] = None,
    program_files_root: Optional[Path] = None,
    _read_acl: Callable[[Path], str] = _read_path_security_sddl,
) -> None:
    if not _is_windows() or not is_process_elevated():
        raise PermissionError("administrator elevation required")
    sid = _validate_requesting_sid(request_sid)
    source = _lexical_absolute(Path(source_executable or sys.executable))
    if not source.is_file():
        raise HidElevationError("bundled_helper_missing")
    source_hash = _sha256(source)
    trusted_root = Path(program_files_root or _program_files_root())
    root = Path(
        owner_root
        or protected_owner_root(sid, program_files_root=trusted_root)
    )
    manifest_file = root / MANIFEST_FILENAME
    assert_no_reparse_points(root, trusted_root=trusted_root)
    assert_no_reparse_points(manifest_file, trusted_root=trusted_root)
    previous_manifest_bytes = _read_optional_file_bytes(manifest_file)
    existing_manifest: Optional[HidHelperManifest]
    try:
        existing_manifest = _load_manifest(manifest_file)
    except HidElevationError as exc:
        detail = str(exc)
        if detail in {"helper_manifest_missing", "helper_manifest_invalid"}:
            if detail == "helper_manifest_invalid" and _manifest_declares_newer_contract(
                manifest_file
            ):
                raise HidElevationError("newer_helper_preserved") from exc
            existing_manifest = None
        elif detail == "helper_manifest_newer_schema":
            raise HidElevationError("newer_helper_preserved") from exc
        else:
            raise
    manifest_trusted_target: Optional[Path] = None
    existing_state = HidHelperState(False)
    reuse_existing_manifest = False
    if existing_manifest is not None:
        if existing_manifest.owner_sid != sid:
            if _manifest_declares_newer_contract(manifest_file):
                raise HidElevationError("newer_helper_preserved")
            existing_manifest = None
        else:
            if existing_manifest.schema_version > MANIFEST_SCHEMA_VERSION:
                raise HidElevationError("newer_helper_preserved")
            if existing_manifest.helper_protocol_version > HELPER_PROTOCOL_VERSION:
                raise HidElevationError("newer_helper_preserved")
            if existing_manifest.task_contract_version > TASK_CONTRACT_VERSION:
                raise HidElevationError("newer_helper_preserved")
            try:
                manifest_acl_valid = validate_path_security_sddl(
                    _read_acl(manifest_file), user_sid=sid, directory=False
                )
            except (HidElevationError, OSError):
                manifest_acl_valid = False
            if manifest_acl_valid:
                manifest_trusted_target = root / Path(
                    existing_manifest.helper_relative_path
                )
            if existing_manifest.helper_generation >= HELPER_GENERATION:
                existing_state = _validate_installed_contract(
                    existing_manifest,
                    user_sid=sid,
                    owner_root=root,
                    trusted_root=trusted_root,
                    manifest_path=manifest_file,
                    _read_task=_read_registered_task,
                    _read_acl=_read_acl,
                )
                if existing_state.available:
                    if existing_manifest.helper_generation > HELPER_GENERATION:
                        return
                    reuse_existing_manifest = True
                if existing_manifest.helper_generation > HELPER_GENERATION:
                    raise HidElevationError("newer_helper_preserved")

    task_name = task_name_for_sid(sid)
    previous_task = _read_registered_task(task_name)
    rollback_task: Optional[_RegisteredTaskSnapshot] = None
    recovered_task_target: Optional[_ValidatedHelperTarget] = None
    if previous_task is not None:
        try:
            recovered_task_target = _trusted_registered_task_target(
                previous_task,
                user_sid=sid,
                owner_root=root,
                trusted_root=trusted_root,
                _read_acl=_read_acl,
            )
        except (HidElevationError, OSError):
            # The task name is derived from this SID, so an invalid definition
            # can be replaced. Never trust its command path or restore it.
            recovered_task_target = None
        else:
            if recovered_task_target.generation > HELPER_GENERATION:
                raise HidElevationError("newer_helper_preserved")
            if recovered_task_target.hash_valid:
                rollback_task = previous_task

    trusted_targets = []
    if manifest_trusted_target is not None:
        trusted_targets.append(manifest_trusted_target)
    if recovered_task_target is not None:
        trusted_targets.append(recovered_task_target.path)
    inventory = _validated_helper_targets(
        root,
        user_sid=sid,
        trusted_root=trusted_root,
        _read_acl=_read_acl,
        trusted_targets=trusted_targets,
    )
    if any(item.generation > HELPER_GENERATION for item in inventory):
        raise HidElevationError("newer_helper_preserved")

    if reuse_existing_manifest:
        assert existing_manifest is not None
        manifest = existing_manifest
    else:
        manifest = _build_manifest(sid, source_hash)
    target = root / Path(manifest.helper_relative_path)
    ensure_protected_directory(
        target.parent,
        user_sid=sid,
        trusted_root=trusted_root,
        security_root=root,
    )
    assert_no_reparse_points(target, trusted_root=trusted_root)
    target_existed = os.path.lexists(target)
    if target_existed and not target.is_file():
        raise HidElevationError("protected_helper_orphan_invalid")
    expected_hash = manifest.helper_sha256
    if reuse_existing_manifest:
        if not target_existed or _sha256(target) != expected_hash:
            raise HidElevationError("protected_helper_verification_failed")
    elif not target_existed or _sha256(target) != expected_hash:
        _copy_verified_helper(source, target, user_sid=sid)
    if _sha256(target) != expected_hash or not validate_path_security_sddl(
        _read_acl(target), user_sid=sid, directory=False
    ):
        if not target_existed:
            try:
                _remove_helper_generation(target, owner_root=root)
            except HidElevationError as rollback_exc:
                raise HidElevationError(
                    "hid_helper_install_rollback_failed"
                ) from rollback_exc
        raise HidElevationError("protected_helper_verification_failed")

    xml_text = task_definition_xml(
        target,
        sid,
        task_name=manifest.task_name,
    )
    security_sddl = task_security_sddl(sid)
    task_mutated = False
    manifest_mutated = False
    try:
        task_mutated = True
        _register_task(manifest.task_name, xml_text, security_sddl)
        registered = _read_registered_task(manifest.task_name)
        if registered is None or not validate_registered_task_xml(
            registered.xml_text,
            helper_path=target,
            user_sid=sid,
            task_name=manifest.task_name,
        ):
            raise HidElevationError("hid_helper_task_verification_failed")
        if not validate_task_security_sddl(
            registered.security_sddl, user_sid=sid
        ):
            raise HidElevationError("hid_helper_task_acl_invalid")
        assert_no_reparse_points(manifest_file, trusted_root=trusted_root)
        manifest_mutated = True
        _write_manifest_atomic(manifest_file, manifest)
    except Exception as exc:
        rollback_failed = False
        if task_mutated:
            try:
                _restore_registered_task(manifest.task_name, rollback_task)
            except Exception:
                rollback_failed = True
        if manifest_mutated:
            try:
                _restore_optional_file_bytes(
                    manifest_file,
                    previous_manifest_bytes,
                    user_sid=sid,
                )
            except Exception:
                rollback_failed = True
        if not target_existed:
            try:
                _remove_helper_generation(target, owner_root=root)
            except Exception:
                rollback_failed = True
        if rollback_failed:
            raise HidElevationError("hid_helper_install_rollback_failed") from exc
        if isinstance(exc, HidElevationError):
            raise
        raise HidElevationError("hid_helper_install_failed") from exc

    cleanup_targets = [item.path for item in inventory]
    if existing_manifest is not None:
        cleanup_targets.append(root / Path(existing_manifest.helper_relative_path))
    if recovered_task_target is not None:
        cleanup_targets.append(recovered_task_target.path)
    seen_cleanup_targets: set[str] = set()
    for previous_target in cleanup_targets:
        key = os.path.normcase(str(_lexical_absolute(previous_target)))
        if key == os.path.normcase(str(_lexical_absolute(target))):
            continue
        if key in seen_cleanup_targets:
            continue
        seen_cleanup_targets.add(key)
        try:
            _remove_helper_generation(previous_target, owner_root=root)
        except HidElevationError as exc:
            raise HidElevationError("protected_helper_cleanup_pending") from exc
    _cleanup_legacy_contract_for_user(sid, trusted_root=trusted_root)


def uninstall_task(
    *,
    request_sid: Optional[str] = None,
    owner_root: Optional[Path] = None,
    program_files_root: Optional[Path] = None,
    _read_acl: Callable[[Path], str] = _read_path_security_sddl,
) -> None:
    if not _is_windows() or not is_process_elevated():
        raise PermissionError("administrator elevation required")
    sid = _validate_requesting_sid(request_sid)
    trusted_root = Path(program_files_root or _program_files_root())
    root = Path(
        owner_root
        or protected_owner_root(sid, program_files_root=trusted_root)
    )
    manifest_file = root / MANIFEST_FILENAME
    assert_no_reparse_points(root, trusted_root=trusted_root)
    assert_no_reparse_points(manifest_file, trusted_root=trusted_root)
    previous_manifest_bytes = _read_optional_file_bytes(manifest_file)
    manifest: Optional[HidHelperManifest] = None
    try:
        manifest = _load_manifest(manifest_file)
    except HidElevationError as exc:
        detail = str(exc)
        if detail in {"helper_manifest_missing", "helper_manifest_invalid"}:
            if detail == "helper_manifest_invalid" and _manifest_declares_newer_contract(
                manifest_file
            ):
                raise HidElevationError("newer_helper_preserved") from exc
        elif detail == "helper_manifest_newer_schema":
            raise HidElevationError("newer_helper_preserved") from exc
        else:
            raise

    manifest_trusted_target: Optional[Path] = None
    if manifest is not None:
        if (
            manifest.helper_protocol_version > HELPER_PROTOCOL_VERSION
            or manifest.task_contract_version > TASK_CONTRACT_VERSION
            or manifest.helper_generation > HELPER_GENERATION
        ):
            raise HidElevationError("newer_helper_preserved")
        if manifest.owner_sid == sid:
            try:
                manifest_acl_valid = validate_path_security_sddl(
                    _read_acl(manifest_file), user_sid=sid, directory=False
                )
            except (HidElevationError, OSError):
                manifest_acl_valid = False
            if manifest_acl_valid:
                manifest_trusted_target = root / Path(manifest.helper_relative_path)
        else:
            manifest = None

    task_name = task_name_for_sid(sid)
    previous_task = _read_registered_task(task_name)
    rollback_task: Optional[_RegisteredTaskSnapshot] = None
    active_target: Optional[_ValidatedHelperTarget] = None
    if previous_task is not None:
        try:
            active_target = _trusted_registered_task_target(
                previous_task,
                user_sid=sid,
                owner_root=root,
                trusted_root=trusted_root,
                _read_acl=_read_acl,
            )
        except (HidElevationError, OSError):
            # The fixed SID-derived task can be deleted without trusting its
            # damaged XML, ACL or command path. Never restore that snapshot.
            active_target = None
        else:
            if active_target.generation > HELPER_GENERATION:
                raise HidElevationError("newer_helper_preserved")
            if active_target.hash_valid:
                rollback_task = previous_task

    trusted_targets = []
    if manifest_trusted_target is not None:
        trusted_targets.append(manifest_trusted_target)
    if active_target is not None:
        trusted_targets.append(active_target.path)
    inventory = _validated_helper_targets(
        root,
        user_sid=sid,
        trusted_root=trusted_root,
        _read_acl=_read_acl,
        trusted_targets=trusted_targets,
    )
    if any(item.generation > HELPER_GENERATION for item in inventory):
        raise HidElevationError("newer_helper_preserved")

    _cleanup_legacy_contract_for_user(sid, trusted_root=trusted_root)

    manifest_removed = False
    task_mutated = False
    try:
        if previous_manifest_bytes is not None:
            try:
                manifest_file.unlink()
            except OSError as exc:
                raise HidElevationError("helper_manifest_removal_failed") from exc
            if os.path.lexists(manifest_file):
                raise HidElevationError("helper_manifest_removal_failed")
            manifest_removed = True
        if previous_task is not None:
            task_mutated = True
            _delete_task(task_name, missing_ok=False)
            if _read_registered_task(task_name) is not None:
                raise HidElevationError("hid_helper_task_removal_failed")
    except Exception as exc:
        rollback_failed = False
        if manifest_removed and rollback_task is not None:
            try:
                _restore_optional_file_bytes(
                    manifest_file,
                    previous_manifest_bytes,
                    user_sid=sid,
                )
            except Exception:
                rollback_failed = True
        if task_mutated and rollback_task is not None:
            try:
                current_task = _read_registered_task(task_name)
                if current_task is None:
                    _restore_registered_task(task_name, rollback_task)
            except Exception:
                try:
                    _restore_registered_task(task_name, rollback_task)
                except Exception:
                    rollback_failed = True
        if rollback_failed:
            raise HidElevationError("hid_helper_uninstall_rollback_failed") from exc
        if isinstance(exc, HidElevationError):
            raise
        raise HidElevationError("hid_helper_uninstall_failed") from exc

    active_key = (
        os.path.normcase(str(_lexical_absolute(active_target.path)))
        if active_target is not None
        else None
    )
    ordered_targets = [
        item.path
        for item in inventory
        if active_key is None
        or os.path.normcase(str(_lexical_absolute(item.path))) != active_key
    ]
    if active_target is not None:
        ordered_targets.append(active_target.path)
    cleanup_failed = False
    seen_targets: set[str] = set()
    for target in ordered_targets:
        key = os.path.normcase(str(_lexical_absolute(target)))
        if key in seen_targets:
            continue
        seen_targets.add(key)
        if not os.path.lexists(target):
            continue
        try:
            _remove_helper_generation(target, owner_root=root)
        except HidElevationError:
            cleanup_failed = True
    if cleanup_failed:
        raise HidElevationError("protected_helper_cleanup_pending")

    # A successfully injected Gadget DLL can remain loaded by WUDFHost until
    # that Windows device host restarts. Its verified runtime cache therefore
    # may keep ``root`` non-empty after the task and privileged helper are
    # gone. Permission removal is complete once those executable entry points
    # are absent; leave the inert, administrator-owned cache for a later
    # reinstall or Windows cleanup instead of turning a normal uninstall into
    # a permanent failure.
    try:
        root.rmdir()
    except OSError:
        pass


class _ShellExecuteInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint32),
        ("fMask", ctypes.c_ulong),
        ("hwnd", ctypes.c_void_p),
        ("lpVerb", ctypes.c_wchar_p),
        ("lpFile", ctypes.c_wchar_p),
        ("lpParameters", ctypes.c_wchar_p),
        ("lpDirectory", ctypes.c_wchar_p),
        ("nShow", ctypes.c_int),
        ("hInstApp", ctypes.c_void_p),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", ctypes.c_wchar_p),
        ("hkeyClass", ctypes.c_void_p),
        ("dwHotKey", ctypes.c_uint32),
        ("hIconOrMonitor", ctypes.c_void_p),
        ("hProcess", ctypes.c_void_p),
    ]


def _run_elevated_and_wait(
    executable: Path,
    arguments: str,
    timeout_seconds: float,
) -> int:
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = (ctypes.POINTER(_ShellExecuteInfo),)
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetExitCodeProcess.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    )
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    info = _ShellExecuteInfo()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x00000040  # SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = "runas"
    info.lpFile = str(executable)
    info.lpParameters = str(arguments)
    info.lpDirectory = str(_lexical_absolute(Path(executable)).parent)
    info.nShow = 0
    ctypes.set_last_error(0)
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        error = int(ctypes.get_last_error())
        if error == 1223:
            raise HidElevationError("uac_cancelled")
        raise HidElevationError("hid_helper_setup_launch_failed")
    if not info.hProcess:
        raise HidElevationError("hid_helper_setup_process_missing")
    try:
        timeout_ms = max(1, min(0xFFFFFFFE, int(float(timeout_seconds) * 1000)))
        wait_result = int(kernel32.WaitForSingleObject(info.hProcess, timeout_ms))
        if wait_result == 0x00000102:
            # Never return while the elevated helper can still alter the task
            # later. Terminate it, then wait until Windows confirms final exit.
            terminated = bool(kernel32.TerminateProcess(
                info.hProcess,
                HELPER_EXIT_UNEXPECTED_FAILURE,
            ))
            termination_wait = int(
                kernel32.WaitForSingleObject(
                    info.hProcess,
                    _ELEVATED_PROCESS_TERMINATION_WAIT_MS,
                )
            )
            if termination_wait != 0:
                detail = (
                    "hid_helper_setup_terminate_wait_failed"
                    if terminated
                    else "hid_helper_setup_terminate_failed"
                )
                raise HidElevationError(detail)
            raise HidElevationError("hid_helper_setup_timeout")
        if wait_result != 0:
            raise HidElevationError("hid_helper_setup_wait_failed")
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(exit_code)):
            raise HidElevationError("hid_helper_setup_result_unavailable")
        return int(exit_code.value)
    finally:
        kernel32.CloseHandle(info.hProcess)


def request_install_elevation(
    *,
    helper_path: Optional[Path] = None,
    timeout_seconds: float = 120.0,
    _launch: Callable[[Path, str, float], int] = _run_elevated_and_wait,
    _inspect: Callable[[], HidHelperState] = inspect_installed_helper,
    _can_self_elevate: Callable[[], bool] = can_current_user_self_elevate,
    _current_sid: Callable[[], str] = current_user_sid,
) -> HidHelperState:
    if not _is_windows():
        return HidHelperState(False, "windows_only")
    source = helper_path or bundled_helper_path()
    if source is None or not source.is_file():
        return HidHelperState(False, "bundled_helper_missing")
    try:
        current_state = _inspect()
    except Exception:
        current_state = HidHelperState(False, "hid_helper_inspection_failed")
    if current_state.available and current_state.detail != "helper_cleanup_pending":
        return current_state
    if is_newer_helper_state(current_state):
        return current_state
    if not _can_self_elevate():
        if current_state.available:
            return current_state
        return HidHelperState(False, "current_account_cannot_self_elevate")
    try:
        sid = canonical_user_sid(_current_sid())
        arguments = subprocess.list2cmdline(
            [INSTALL_FLAG, REQUEST_SID_FLAG, sid]
        )
        exit_code = _launch(source, arguments, timeout_seconds)
    except HidElevationError as exc:
        try:
            final_state = _inspect()
        except Exception:
            final_state = HidHelperState(False, "hid_helper_inspection_failed")
        if final_state.available:
            return final_state
        if str(exc) == "uac_cancelled" and current_state.available:
            return current_state
        return HidHelperState(False, str(exc))
    except (OSError, ValueError):
        return HidHelperState(False, "hid_helper_setup_launch_failed")
    try:
        installed = _inspect()
    except Exception:
        installed = HidHelperState(False, "hid_helper_inspection_failed")
    if exit_code == HELPER_EXIT_NEWER_PRESERVED:
        if installed.available:
            return installed
        return HidHelperState(False, "newer_helper_preserved")
    if installed.available:
        if exit_code != HELPER_EXIT_OK:
            return HidHelperState(True, "helper_cleanup_pending")
        return installed
    if exit_code != HELPER_EXIT_OK:
        return HidHelperState(False, f"hid_helper_setup_exit_{exit_code}")
    return installed


def request_uninstall_elevation(
    *,
    helper_path: Optional[Path] = None,
    timeout_seconds: float = 120.0,
    _launch: Callable[[Path, str, float], int] = _run_elevated_and_wait,
    _can_self_elevate: Callable[[], bool] = can_current_user_self_elevate,
    _current_sid: Callable[[], str] = current_user_sid,
    _inspect: Callable[[], HidHelperState] = inspect_installed_helper,
    _requires_removal: Callable[[str], bool] = (
        _installation_requires_elevated_removal
    ),
) -> HidHelperState:
    if not _is_windows():
        return HidHelperState(False, "windows_only")
    sid: Optional[str] = None
    try:
        sid = canonical_user_sid(_current_sid())
        if not _requires_removal(sid):
            return HidHelperState(True)
    except HidElevationError as exc:
        if sid is not None:
            try:
                if not _requires_removal(sid):
                    return HidHelperState(True)
            except (HidElevationError, OSError, ValueError):
                pass
        return HidHelperState(False, str(exc))
    except (OSError, ValueError):
        return HidHelperState(False, "hid_helper_inspection_failed")
    try:
        installed = _inspect()
    except Exception:
        installed = HidHelperState(False, "hid_helper_inspection_failed")
    if installed.detail in _NEWER_HELPER_DETAILS:
        return HidHelperState(True, "helper_preserved_newer_contract")
    source = helper_path or bundled_helper_path()
    if source is None or not source.is_file():
        return HidHelperState(False, "bundled_helper_missing")
    if not _can_self_elevate():
        return HidHelperState(False, "current_account_cannot_self_elevate")
    try:
        arguments = subprocess.list2cmdline(
            [UNINSTALL_FLAG, REQUEST_SID_FLAG, sid]
        )
        exit_code = _launch(source, arguments, timeout_seconds)
    except HidElevationError as exc:
        return HidHelperState(False, str(exc))
    except (OSError, ValueError):
        return HidHelperState(False, "hid_helper_setup_launch_failed")
    if exit_code == HELPER_EXIT_NEWER_PRESERVED:
        return HidHelperState(True, "helper_preserved_newer_contract")
    try:
        removal_still_required = _requires_removal(sid)
    except (HidElevationError, OSError, ValueError):
        return HidHelperState(False, "hid_helper_inspection_failed")
    if exit_code != HELPER_EXIT_OK:
        return HidHelperState(False, f"hid_helper_setup_exit_{exit_code}")
    if not removal_still_required:
        return HidHelperState(True)
    return HidHelperState(False, "hid_helper_removal_incomplete")


def _validate_helper_execution_identity() -> None:
    sid = current_user_sid()
    trusted_root = _program_files_root()
    root = protected_owner_root(sid, program_files_root=trusted_root)
    manifest_file = root / MANIFEST_FILENAME
    manifest = _load_manifest(manifest_file)
    if (
        manifest.owner_sid != sid
        or manifest.helper_protocol_version != HELPER_PROTOCOL_VERSION
        or manifest.helper_generation != HELPER_GENERATION
        or manifest.task_contract_version != TASK_CONTRACT_VERSION
    ):
        raise HidElevationError("installed_helper_identity_invalid")
    expected = root / Path(manifest.helper_relative_path)
    actual = _lexical_absolute(Path(sys.executable))
    if os.path.normcase(str(actual)) != os.path.normcase(str(_lexical_absolute(expected))):
        raise HidElevationError("installed_helper_path_mismatch")
    assert_no_reparse_points(actual, trusted_root=trusted_root)
    if _sha256(actual) != manifest.helper_sha256:
        raise HidElevationError("installed_helper_hash_mismatch")


def _inject_once() -> None:
    from .frida_hid_tap_injector import inject_current_process
    from .frida_hid_tap_runtime import find_rc003_hidogatt_host_pid

    _validate_helper_execution_identity()
    pid = find_rc003_hidogatt_host_pid()
    if pid is None:
        raise RuntimeError("RC003 WUDFHost is unavailable")
    inject_current_process(pid)


def _self_check() -> None:
    import comtypes.client  # noqa: F401

    from . import frida_hid_tap_injector, single_instance  # noqa: F401
    from . import frida_hid_tap_runtime

    archive = frida_hid_tap_runtime.gadget_archive_path()
    if not archive.is_file():
        raise HidElevationError("frida_gadget_archive_missing")
    if (
        frida_hid_tap_runtime.sha256_file(archive)
        != frida_hid_tap_runtime.GADGET_ARCHIVE_SHA256
    ):
        raise HidElevationError("frida_gadget_archive_hash_mismatch")


def _run_serialized_helper_operation(
    operation: Callable[[], None],
    *,
    lock_timeout_seconds: float,
) -> None:
    """Keep elevated helper work serialized even if its parent exits."""

    from . import single_instance

    deadline = time.monotonic() + max(0.0, float(lock_timeout_seconds))
    guard: single_instance.BridgeInstanceGuard | None = None
    while guard is None:
        candidate = single_instance.BridgeInstanceGuard(
            name=_HELPER_OPERATION_MUTEX_NAME,
            _duplicate_message="HID helper operation is already running",
            _access_denied_means_duplicate=True,
        )
        try:
            candidate.__enter__()
        except single_instance.DuplicateInstanceError as exc:
            if time.monotonic() >= deadline:
                raise HidElevationError("hid_helper_operation_busy") from exc
            time.sleep(0.05)
            continue
        except Exception as exc:
            raise HidElevationError("hid_helper_operation_unavailable") from exc
        guard = candidate
    try:
        operation()
    finally:
        guard.__exit__(None, None, None)


def helper_main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(INSTALL_FLAG, action="store_true")
    group.add_argument(UNINSTALL_FLAG, action="store_true")
    group.add_argument(INJECT_FLAG, action="store_true")
    group.add_argument(SELF_CHECK_FLAG, action="store_true")
    parser.add_argument(REQUEST_SID_FLAG)
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
        if getattr(args, INSTALL_FLAG[2:].replace("-", "_")):
            _run_serialized_helper_operation(
                lambda: install_task(request_sid=args.request_sid),
                lock_timeout_seconds=_HELPER_MAINTENANCE_LOCK_TIMEOUT_SECONDS,
            )
        elif getattr(args, UNINSTALL_FLAG[2:].replace("-", "_")):
            _run_serialized_helper_operation(
                lambda: uninstall_task(request_sid=args.request_sid),
                lock_timeout_seconds=_HELPER_MAINTENANCE_LOCK_TIMEOUT_SECONDS,
            )
        elif getattr(args, INJECT_FLAG[2:].replace("-", "_")):
            if args.request_sid is not None:
                raise HidElevationError("unexpected_request_sid")
            _run_serialized_helper_operation(
                _inject_once,
                lock_timeout_seconds=_HELPER_INJECT_LOCK_TIMEOUT_SECONDS,
            )
        else:
            if args.request_sid is not None:
                raise HidElevationError("unexpected_request_sid")
            _self_check()
        return HELPER_EXIT_OK
    except PermissionError:
        return HELPER_EXIT_REQUIRES_ADMIN
    except HidElevationError as exc:
        if str(exc) == "newer_helper_preserved":
            return HELPER_EXIT_NEWER_PRESERVED
        if str(exc) == "hid_helper_operation_busy":
            return HELPER_EXIT_OPERATION_BUSY
        return HELPER_EXIT_VALIDATION_FAILED
    except (OSError, RuntimeError, ValueError):
        return HELPER_EXIT_VALIDATION_FAILED
    except Exception:
        return HELPER_EXIT_UNEXPECTED_FAILURE
