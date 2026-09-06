"""Manual GitHub release discovery and verified package download.

The desktop UI calls this module only after the user clicks "check for
updates".  It never runs at startup, never embeds a GitHub credential, and
never launches a downloaded file.  A release is eligible only when its two
RC003 packages and ``SHA256SUMS.txt`` agree on one internal application
version; repository tags are deliberately not used as application versions.
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional


REPOSITORY_OWNER = "ZSTDJan"
REPOSITORY_NAME = "windows-remote-mic-app"
REPOSITORY_SLUG = f"{REPOSITORY_OWNER}/{REPOSITORY_NAME}"
RELEASES_PAGE_URL = f"https://github.com/{REPOSITORY_SLUG}/releases"
RELEASES_API_URL = (
    f"https://api.github.com/repos/{REPOSITORY_SLUG}/releases?per_page=30"
)

NETWORK_TIMEOUT_SECONDS = 10.0
MAX_RELEASES_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_RELEASE_COUNT = 50
MAX_RELEASE_NOTES_CHARS = 12_000
MAX_MANIFEST_BYTES = 64 * 1024
MAX_PACKAGE_BYTES = 1024 * 1024 * 1024
DOWNLOAD_CHUNK_BYTES = 256 * 1024

_LOGGER = logging.getLogger(__name__)

_ALLOWED_NETWORK_HOSTS = frozenset(
    {
        "api.github.com",
        "github.com",
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
    }
)
_VERSION_TEXT_PATTERN = (
    r"(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*)"
    r"(?:-candidate(?:\.(?:0|[1-9][0-9]*))?)?"
)
_VERSION_PATTERN = re.compile(
    r"^(?P<major>0|[1-9][0-9]*)\."
    r"(?P<minor>0|[1-9][0-9]*)\."
    r"(?P<patch>0|[1-9][0-9]*)"
    r"(?:-candidate(?:\.(?P<candidate>0|[1-9][0-9]*))?)?$"
)
_INSTALLER_PATTERN = re.compile(
    rf"^RemoteMicRC003Setup-(?P<version>{_VERSION_TEXT_PATTERN})-unsigned\.exe$"
)
_PORTABLE_PATTERN = re.compile(
    rf"^RemoteMicRC003-(?P<version>{_VERSION_TEXT_PATTERN})-portable-unsigned\.zip$"
)
_SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
_MANIFEST_LINE_PATTERN = re.compile(
    r"^(?P<hash>[0-9a-f]{64})  (?P<name>[^\\/\r\n]+)$"
)


class ApplicationUpdateError(RuntimeError):
    """A sanitized, user-displayable update failure with a stable code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = str(code)
        super().__init__(message)


class ApplicationUpdateCancelled(ApplicationUpdateError):
    def __init__(self) -> None:
        super().__init__("cancelled", "下载已取消。")


class UpdateCheckOutcome(Enum):
    UPDATE_AVAILABLE = "update_available"
    CURRENT = "current"
    LOCAL_NEWER = "local_newer"


class PackageKind(Enum):
    INSTALLER = "installer"
    PORTABLE = "portable"


@functools.total_ordering
@dataclass(frozen=True, eq=False)
class ApplicationVersion:
    text: str
    major: int
    minor: int
    patch: int
    candidate: Optional[int]

    @property
    def is_prerelease(self) -> bool:
        return self.candidate is not None

    @property
    def sort_key(self) -> tuple[int, int, int, int, int]:
        return (
            self.major,
            self.minor,
            self.patch,
            0 if self.is_prerelease else 1,
            self.candidate or 0,
        )

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, ApplicationVersion):
            return NotImplemented
        return self.sort_key < other.sort_key

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ApplicationVersion):
            return NotImplemented
        return self.sort_key == other.sort_key


@dataclass(frozen=True)
class ReleaseAsset:
    name: str
    size: int
    download_url: str
    github_sha256: Optional[str]


@dataclass(frozen=True)
class ApplicationRelease:
    version: ApplicationVersion
    release_url: str
    notes: str
    installer: ReleaseAsset
    portable: ReleaseAsset
    manifest: ReleaseAsset

    def package_for(self, kind: PackageKind) -> ReleaseAsset:
        return self.installer if kind is PackageKind.INSTALLER else self.portable


@dataclass(frozen=True)
class ApplicationUpdateCheck:
    outcome: UpdateCheckOutcome
    current_version: ApplicationVersion
    release: ApplicationRelease


@dataclass(frozen=True)
class ApplicationUpdateDownload:
    release: ApplicationRelease
    package_kind: PackageKind
    path: Path
    reused_existing_file: bool


def parse_application_version(value: str) -> ApplicationVersion:
    text = str(value).strip()
    match = _VERSION_PATTERN.fullmatch(text)
    if match is None:
        raise ValueError(f"unsupported application version: {text!r}")
    candidate_group = match.group("candidate")
    has_candidate_suffix = "-candidate" in text
    return ApplicationVersion(
        text=text,
        major=int(match.group("major")),
        minor=int(match.group("minor")),
        patch=int(match.group("patch")),
        candidate=(
            int(candidate_group)
            if candidate_group is not None
            else 0
            if has_candidate_suffix
            else None
        ),
    )


def format_file_size(size: int) -> str:
    value = max(0, int(size))
    if value < 1024:
        return f"{value} B"
    units = ("KB", "MB", "GB")
    scaled = float(value)
    for unit in units:
        scaled /= 1024.0
        if scaled < 1024.0 or unit == units[-1]:
            return f"{scaled:.1f} {unit}"
    return f"{value} B"


def _path_is_link_like(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(os.path, "isjunction", None)
        return bool(is_junction is not None and is_junction(path))
    except OSError:
        return True


def _owned_download_names(version: ApplicationVersion) -> frozenset[str]:
    installer = f"RemoteMicRC003Setup-{version.text}-unsigned.exe"
    portable = f"RemoteMicRC003-{version.text}-portable-unsigned.zip"
    return frozenset(
        {
            installer,
            portable,
            f"{installer}.part",
            f"{portable}.part",
        }
    )


def cleanup_obsolete_update_downloads(
    update_root: Path,
    through_version: ApplicationVersion | str,
    *,
    include_current: bool = True,
) -> int:
    """Remove only obsolete package-cache directories owned by the updater."""

    threshold = (
        through_version
        if isinstance(through_version, ApplicationVersion)
        else parse_application_version(str(through_version))
    )
    root = Path(update_root)
    try:
        if _path_is_link_like(root) or not root.exists():
            return 0
        if not root.is_dir():
            _LOGGER.warning("update cleanup skipped non-directory root: %s", root)
            return 0
        candidates = list(root.iterdir())
    except OSError as exc:
        _LOGGER.warning("update cleanup could not inspect %s: %s", root, exc)
        return 0

    removed = 0
    for candidate in candidates:
        try:
            version = parse_application_version(candidate.name)
        except ValueError:
            continue
        if version > threshold or (not include_current and version == threshold):
            continue
        try:
            if _path_is_link_like(candidate) or not candidate.is_dir():
                continue
            entries = list(candidate.iterdir())
            allowed_names = _owned_download_names(version)
            if any(
                _path_is_link_like(entry)
                or not entry.is_file()
                or entry.name not in allowed_names
                for entry in entries
            ):
                _LOGGER.warning(
                    "update cleanup preserved directory with unknown content: %s",
                    candidate,
                )
                continue
            for entry in entries:
                entry.unlink()
            candidate.rmdir()
        except OSError as exc:
            _LOGGER.warning(
                "update cleanup could not remove %s: %s", candidate, exc
            )
            continue
        removed += 1
    return removed


def _raise(code: str, message: str) -> None:
    raise ApplicationUpdateError(code, message)


def _require_https_host(url: str, allowed_hosts: frozenset[str]) -> str:
    try:
        parsed = urllib.parse.urlsplit(str(url))
        port = parsed.port
    except (TypeError, ValueError):
        _raise("unsafe_url", "更新地址无效，已停止操作。")
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme.lower() != "https"
        or host not in allowed_hosts
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
    ):
        _raise("unsafe_url", "更新地址不属于允许的 GitHub 来源，已停止操作。")
    return host


def _validate_release_page_url(url: str) -> str:
    _require_https_host(url, frozenset({"github.com"}))
    parsed = urllib.parse.urlsplit(url)
    prefix = f"/{REPOSITORY_SLUG}/releases/"
    if not parsed.path.startswith(prefix) or parsed.query or parsed.fragment:
        _raise("invalid_release", "GitHub 发布页信息不完整，无法安全检查更新。")
    return url


def _validate_asset_download_url(url: str) -> str:
    _require_https_host(url, frozenset({"github.com"}))
    parsed = urllib.parse.urlsplit(url)
    prefix = f"/{REPOSITORY_SLUG}/releases/download/"
    if not parsed.path.startswith(prefix) or parsed.query or parsed.fragment:
        _raise("invalid_release", "GitHub 更新包地址不符合项目发布规则。")
    return url


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urljoin(req.full_url, newurl)
        _require_https_host(target, _ALLOWED_NETWORK_HOSTS)
        return super().redirect_request(req, fp, code, msg, headers, target)


_SAFE_OPENER = urllib.request.build_opener(_SafeRedirectHandler())


def _default_open(request: urllib.request.Request, *, timeout: float):
    return _SAFE_OPENER.open(request, timeout=timeout)


def _request(url: str, accept: str) -> urllib.request.Request:
    return urllib.request.Request(
        url,
        headers={
            "Accept": accept,
            "User-Agent": "RemoteMic-RC003-Updater",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method="GET",
    )


def _translate_open_error(exc: Exception) -> ApplicationUpdateError:
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in {403, 429}:
            return ApplicationUpdateError(
                "rate_limited",
                "GitHub 暂时限制了检查或下载次数，请稍后再试，或打开发布页查看。",
            )
        if exc.code == 404:
            return ApplicationUpdateError(
                "source_unavailable", "GitHub 更新源暂时不可用，请打开发布页确认。"
            )
        if 500 <= exc.code <= 599:
            return ApplicationUpdateError(
                "service_unavailable", "GitHub 服务暂时不可用，请稍后重试。"
            )
        return ApplicationUpdateError(
            "http_error", f"GitHub 请求失败（HTTP {exc.code}），请稍后重试。"
        )
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return ApplicationUpdateError("timeout", "连接 GitHub 超时，请检查网络后重试。")
    if isinstance(exc, urllib.error.URLError):
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            return ApplicationUpdateError(
                "timeout", "连接 GitHub 超时，请检查网络后重试。"
            )
        return ApplicationUpdateError(
            "network_unavailable", "无法连接 GitHub，请检查网络后重试。"
        )
    if isinstance(exc, OSError):
        return ApplicationUpdateError(
            "network_unavailable", "无法连接 GitHub，请检查网络后重试。"
        )
    return ApplicationUpdateError("unexpected", "更新请求失败，请稍后重试。")


def _response_status(response: Any) -> int:
    status = getattr(response, "status", None)
    if status is None:
        getter = getattr(response, "getcode", None)
        status = getter() if getter is not None else 200
    return int(status)


def _response_url(response: Any, fallback: str) -> str:
    getter = getattr(response, "geturl", None)
    return str(getter() if getter is not None else fallback)


def _content_length(response: Any) -> Optional[int]:
    headers = getattr(response, "headers", None)
    raw = headers.get("Content-Length") if headers is not None else None
    if raw in (None, ""):
        return None
    try:
        length = int(raw)
    except (TypeError, ValueError):
        _raise("invalid_response", "GitHub 返回了无效的文件大小。")
    if length < 0:
        _raise("invalid_response", "GitHub 返回了无效的文件大小。")
    return length


def _open_response(
    request: urllib.request.Request,
    *,
    opener: Callable[..., Any],
    timeout: float,
):
    try:
        return opener(request, timeout=timeout)
    except ApplicationUpdateError:
        raise
    except Exception as exc:
        translated = _translate_open_error(exc)
        if isinstance(exc, urllib.error.HTTPError):
            try:
                exc.close()
            except Exception:
                pass
        raise translated from exc


def _read_limited_response(
    response: Any,
    *,
    request_url: str,
    allowed_final_hosts: frozenset[str],
    maximum_bytes: int,
    cancel_event: Any = None,
) -> bytes:
    if _response_status(response) != 200:
        _raise("http_error", "GitHub 请求未成功，请稍后重试。")
    _require_https_host(_response_url(response, request_url), allowed_final_hosts)
    declared_length = _content_length(response)
    if declared_length is not None and declared_length > maximum_bytes:
        _raise("response_too_large", "GitHub 返回的数据过大，已停止操作。")
    chunks: list[bytes] = []
    received = 0
    while True:
        if cancel_event is not None and cancel_event.is_set():
            raise ApplicationUpdateCancelled()
        try:
            chunk = response.read(min(DOWNLOAD_CHUNK_BYTES, maximum_bytes + 1))
        except Exception as exc:
            raise _translate_open_error(exc) from exc
        if not chunk:
            break
        received += len(chunk)
        if received > maximum_bytes:
            _raise("response_too_large", "GitHub 返回的数据过大，已停止操作。")
        chunks.append(bytes(chunk))
    if declared_length is not None and received != declared_length:
        _raise("incomplete_response", "GitHub 返回的数据不完整，请重试。")
    return b"".join(chunks)


def _close_response(response: Any) -> None:
    close = getattr(response, "close", None)
    if close is not None:
        close()


def _fetch_bytes(
    url: str,
    *,
    accept: str,
    allowed_final_hosts: frozenset[str],
    maximum_bytes: int,
    opener: Callable[..., Any],
    timeout: float,
    cancel_event: Any = None,
) -> bytes:
    request = _request(url, accept)
    response = _open_response(request, opener=opener, timeout=timeout)
    try:
        return _read_limited_response(
            response,
            request_url=url,
            allowed_final_hosts=allowed_final_hosts,
            maximum_bytes=maximum_bytes,
            cancel_event=cancel_event,
        )
    finally:
        _close_response(response)


def _parse_github_digest(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not value.lower().startswith("sha256:"):
        _raise("invalid_release", "GitHub 更新包摘要格式无效。")
    digest = value.split(":", 1)[1].lower()
    if _SHA256_PATTERN.fullmatch(digest) is None:
        _raise("invalid_release", "GitHub 更新包摘要格式无效。")
    return digest


def _parse_asset(item: Any, expected_name: str, maximum_size: int) -> ReleaseAsset:
    if not isinstance(item, dict) or item.get("name") != expected_name:
        _raise("invalid_release", "GitHub 更新包信息不完整。")
    size = item.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or not (0 < size <= maximum_size):
        _raise("invalid_release", "GitHub 更新包大小无效。")
    if item.get("state") != "uploaded":
        _raise("invalid_release", "GitHub 更新包尚未上传完成。")
    download_url = item.get("browser_download_url")
    if not isinstance(download_url, str):
        _raise("invalid_release", "GitHub 更新包地址缺失。")
    _validate_asset_download_url(download_url)
    return ReleaseAsset(
        name=expected_name,
        size=size,
        download_url=download_url,
        github_sha256=_parse_github_digest(item.get("digest")),
    )


def _parse_release(
    item: Any,
) -> tuple[Optional[ApplicationRelease], bool, Optional[ApplicationVersion]]:
    if not isinstance(item, dict):
        _raise("invalid_response", "GitHub 返回了无法识别的发布信息。")
    draft = item.get("draft")
    prerelease = item.get("prerelease")
    assets = item.get("assets")
    if not isinstance(draft, bool) or not isinstance(prerelease, bool) or not isinstance(assets, list):
        _raise("invalid_response", "GitHub 返回了不完整的发布信息。")
    if draft:
        return None, False, None

    installer_matches: list[tuple[Any, re.Match[str]]] = []
    portable_matches: list[tuple[Any, re.Match[str]]] = []
    manifest_matches: list[Any] = []
    for asset in assets:
        if not isinstance(asset, dict):
            _raise("invalid_response", "GitHub 返回了无法识别的更新包信息。")
        name = asset.get("name")
        if not isinstance(name, str):
            continue
        installer_match = _INSTALLER_PATTERN.fullmatch(name)
        portable_match = _PORTABLE_PATTERN.fullmatch(name)
        if installer_match is not None:
            installer_matches.append((asset, installer_match))
        if portable_match is not None:
            portable_matches.append((asset, portable_match))
        if name == "SHA256SUMS.txt":
            manifest_matches.append(asset)

    recognized = bool(installer_matches or portable_matches)
    if not recognized:
        return None, False, None
    version_hints = [
        parse_application_version(match.group("version"))
        for _asset_item, match in (*installer_matches, *portable_matches)
    ]
    version_hint = max(version_hints) if version_hints else None
    if len(installer_matches) != 1 or len(portable_matches) != 1 or len(manifest_matches) != 1:
        return None, True, version_hint

    installer_item, installer_match = installer_matches[0]
    portable_item, portable_match = portable_matches[0]
    installer_version_text = installer_match.group("version")
    portable_version_text = portable_match.group("version")
    if installer_version_text != portable_version_text:
        return None, True, version_hint
    try:
        version = parse_application_version(installer_version_text)
    except ValueError:
        return None, True, version_hint
    if prerelease != version.is_prerelease:
        return None, True, version

    release_url = item.get("html_url")
    if not isinstance(release_url, str):
        return None, True, version
    try:
        _validate_release_page_url(release_url)
        installer = _parse_asset(
            installer_item,
            f"RemoteMicRC003Setup-{version.text}-unsigned.exe",
            MAX_PACKAGE_BYTES,
        )
        portable = _parse_asset(
            portable_item,
            f"RemoteMicRC003-{version.text}-portable-unsigned.zip",
            MAX_PACKAGE_BYTES,
        )
        manifest = _parse_asset(
            manifest_matches[0], "SHA256SUMS.txt", MAX_MANIFEST_BYTES
        )
    except ApplicationUpdateError:
        return None, True, version

    raw_notes = item.get("body")
    if raw_notes is None:
        notes = ""
    elif isinstance(raw_notes, str):
        notes = raw_notes.strip()
    else:
        return None, True, version
    if len(notes) > MAX_RELEASE_NOTES_CHARS:
        notes = notes[:MAX_RELEASE_NOTES_CHARS].rstrip() + "\n\n（更新说明过长，已截断）"
    return (
        ApplicationRelease(
            version=version,
            release_url=release_url,
            notes=notes,
            installer=installer,
            portable=portable,
            manifest=manifest,
        ),
        True,
        version,
    )


def _select_release(payload: Any, current: ApplicationVersion) -> ApplicationRelease:
    if not isinstance(payload, list) or len(payload) > MAX_RELEASE_COUNT:
        _raise("invalid_response", "GitHub 返回了无法识别的发布列表。")
    releases: list[ApplicationRelease] = []
    recognized_invalid = False
    invalid_versions: list[ApplicationVersion] = []
    seen_versions: set[tuple[int, int, int, int, int]] = set()
    for item in payload:
        release, recognized, version_hint = _parse_release(item)
        if release is None:
            eligible_invalid = bool(
                recognized
                and version_hint is not None
                and (current.is_prerelease or not version_hint.is_prerelease)
            )
            recognized_invalid = recognized_invalid or eligible_invalid
            if eligible_invalid:
                invalid_versions.append(version_hint)
            continue
        if not current.is_prerelease and release.version.is_prerelease:
            continue
        if release.version.sort_key in seen_versions:
            _raise("duplicate_release", "GitHub 上存在重复的应用版本，无法安全选择更新包。")
        seen_versions.add(release.version.sort_key)
        releases.append(release)
    if not releases:
        if recognized_invalid:
            _raise("invalid_release", "GitHub 上的 Windows 更新包不完整，请打开发布页确认。")
        _raise("no_release", "GitHub 上暂未找到可用于 Windows 的完整更新包。")
    selected = max(releases, key=lambda release: release.version.sort_key)
    if invalid_versions:
        newest_invalid = max(invalid_versions)
        if newest_invalid >= current and newest_invalid >= selected.version:
            _raise(
                "invalid_release",
                "GitHub 上的最新 Windows 更新包尚未上传完整，请稍后重试。",
            )
    return selected


def check_for_update(
    current_version: str,
    *,
    opener: Callable[..., Any] = _default_open,
    timeout: float = NETWORK_TIMEOUT_SECONDS,
) -> ApplicationUpdateCheck:
    try:
        current = parse_application_version(current_version)
    except ValueError as exc:
        raise ApplicationUpdateError(
            "invalid_local_version", "当前程序版本无法识别，不能安全比较更新。"
        ) from exc
    _require_https_host(RELEASES_API_URL, frozenset({"api.github.com"}))
    raw = _fetch_bytes(
        RELEASES_API_URL,
        accept="application/vnd.github+json",
        allowed_final_hosts=frozenset({"api.github.com"}),
        maximum_bytes=MAX_RELEASES_RESPONSE_BYTES,
        opener=opener,
        timeout=timeout,
    )
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApplicationUpdateError(
            "invalid_response", "GitHub 返回了无法识别的更新信息。"
        ) from exc
    release = _select_release(payload, current)
    if release.version > current:
        outcome = UpdateCheckOutcome.UPDATE_AVAILABLE
    elif release.version == current:
        outcome = UpdateCheckOutcome.CURRENT
    else:
        outcome = UpdateCheckOutcome.LOCAL_NEWER
    return ApplicationUpdateCheck(outcome, current, release)


def _verify_bytes_digest(data: bytes, expected: Optional[str]) -> None:
    if expected is None:
        return
    if hashlib.sha256(data).hexdigest() != expected:
        _raise("digest_mismatch", "GitHub 文件摘要校验失败，已停止操作。")


def _parse_manifest(data: bytes, release: ApplicationRelease) -> dict[str, str]:
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ApplicationUpdateError(
            "invalid_manifest", "SHA256SUMS.txt 不是有效的 ASCII 清单。"
        ) from exc
    lines = text.splitlines()
    if len(lines) != 2:
        _raise("invalid_manifest", "SHA256SUMS.txt 的文件数量不符合发布规则。")
    entries: dict[str, str] = {}
    for line in lines:
        match = _MANIFEST_LINE_PATTERN.fullmatch(line)
        if match is None or match.group("name") in entries:
            _raise("invalid_manifest", "SHA256SUMS.txt 的内容格式无效。")
        entries[match.group("name")] = match.group("hash")
    expected_names = {release.installer.name, release.portable.name}
    if set(entries) != expected_names:
        _raise("invalid_manifest", "SHA256SUMS.txt 与本次发布的更新包不一致。")
    return entries


def _sha256_file(path: Path, cancel_event: Any = None) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise ApplicationUpdateCancelled()
                chunk = stream.read(DOWNLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                digest.update(chunk)
    except ApplicationUpdateError:
        raise
    except OSError as exc:
        raise ApplicationUpdateError(
            "file_unavailable", "无法读取已有更新包，请检查目录权限。"
        ) from exc
    return digest.hexdigest()


def _remove_partial(path: Path) -> None:
    try:
        if path.exists() or path.is_symlink():
            if path.is_dir() and not path.is_symlink():
                _raise("invalid_destination", "更新临时路径不是文件，无法继续下载。")
            path.unlink()
    except ApplicationUpdateError:
        raise
    except OSError as exc:
        raise ApplicationUpdateError(
            "file_cleanup_failed", "无法清理未完成的更新包，请检查目录权限。"
        ) from exc


def _existing_file_is_valid(
    path: Path,
    asset: ReleaseAsset,
    expected_sha256: str,
    cancel_event: Any,
) -> bool:
    if not path.exists():
        return False
    if path.is_symlink() or not path.is_file():
        _raise("invalid_destination", "更新包目标路径不是普通文件。")
    try:
        if path.stat().st_size != asset.size:
            return False
    except OSError as exc:
        raise ApplicationUpdateError(
            "file_unavailable", "无法检查已有更新包，请检查目录权限。"
        ) from exc
    actual = _sha256_file(path, cancel_event)
    return actual == expected_sha256 and (
        asset.github_sha256 is None or actual == asset.github_sha256
    )


def _download_asset(
    asset: ReleaseAsset,
    part_path: Path,
    *,
    expected_sha256: str,
    opener: Callable[..., Any],
    timeout: float,
    cancel_event: Any,
    progress_callback: Optional[Callable[[int, int], None]],
) -> None:
    request = _request(asset.download_url, "application/octet-stream")
    response = _open_response(request, opener=opener, timeout=timeout)
    received = 0
    digest = hashlib.sha256()
    try:
        if _response_status(response) != 200:
            _raise("http_error", "GitHub 更新包下载失败，请稍后重试。")
        _require_https_host(
            _response_url(response, asset.download_url), _ALLOWED_NETWORK_HOSTS
        )
        declared_length = _content_length(response)
        if declared_length is not None and declared_length != asset.size:
            _raise("size_mismatch", "GitHub 更新包大小与发布信息不一致。")
        if progress_callback is not None:
            progress_callback(0, asset.size)
        try:
            with part_path.open("xb") as stream:
                while True:
                    if cancel_event is not None and cancel_event.is_set():
                        raise ApplicationUpdateCancelled()
                    try:
                        chunk = response.read(DOWNLOAD_CHUNK_BYTES)
                    except Exception as exc:
                        raise _translate_open_error(exc) from exc
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > asset.size or received > MAX_PACKAGE_BYTES:
                        _raise("size_mismatch", "下载的更新包大小超过发布信息。")
                    stream.write(chunk)
                    digest.update(chunk)
                    if progress_callback is not None:
                        progress_callback(received, asset.size)
                stream.flush()
                os.fsync(stream.fileno())
        except ApplicationUpdateError:
            raise
        except OSError as exc:
            raise ApplicationUpdateError(
                "file_write_failed", "无法保存更新包，请检查磁盘空间或目录权限。"
            ) from exc
    finally:
        _close_response(response)
    if received != asset.size:
        _raise("incomplete_download", "更新包下载不完整，临时文件已清理。")
    actual_sha256 = digest.hexdigest()
    if actual_sha256 != expected_sha256:
        _raise("checksum_mismatch", "更新包 SHA-256 校验失败，临时文件已清理。")
    if asset.github_sha256 is not None and actual_sha256 != asset.github_sha256:
        _raise("digest_mismatch", "更新包与 GitHub 摘要不一致，临时文件已清理。")


def download_update_package(
    release: ApplicationRelease,
    package_kind: PackageKind | str,
    destination_directory: Path,
    *,
    opener: Callable[..., Any] = _default_open,
    timeout: float = NETWORK_TIMEOUT_SECONDS,
    cancel_event: Any = None,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> ApplicationUpdateDownload:
    try:
        kind = (
            package_kind
            if isinstance(package_kind, PackageKind)
            else PackageKind(str(package_kind))
        )
    except ValueError as exc:
        raise ApplicationUpdateError(
            "invalid_package_kind", "无法识别要下载的更新包类型。"
        ) from exc
    if cancel_event is not None and cancel_event.is_set():
        raise ApplicationUpdateCancelled()
    asset = release.package_for(kind)
    manifest_data = _fetch_bytes(
        release.manifest.download_url,
        accept="application/octet-stream",
        allowed_final_hosts=_ALLOWED_NETWORK_HOSTS,
        maximum_bytes=MAX_MANIFEST_BYTES,
        opener=opener,
        timeout=timeout,
        cancel_event=cancel_event,
    )
    if len(manifest_data) != release.manifest.size:
        _raise("size_mismatch", "SHA256SUMS.txt 大小与发布信息不一致。")
    _verify_bytes_digest(manifest_data, release.manifest.github_sha256)
    expected_sha256 = _parse_manifest(manifest_data, release)[asset.name]

    directory = Path(destination_directory)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ApplicationUpdateError(
            "directory_unavailable", "无法创建更新下载目录，请检查目录权限。"
        ) from exc
    if not directory.is_dir():
        _raise("invalid_destination", "更新下载位置不是文件夹。")
    final_path = directory / asset.name
    part_path = directory / f"{asset.name}.part"
    _remove_partial(part_path)

    if _existing_file_is_valid(
        final_path, asset, expected_sha256, cancel_event
    ):
        if progress_callback is not None:
            progress_callback(asset.size, asset.size)
        return ApplicationUpdateDownload(release, kind, final_path, True)

    try:
        _download_asset(
            asset,
            part_path,
            expected_sha256=expected_sha256,
            opener=opener,
            timeout=timeout,
            cancel_event=cancel_event,
            progress_callback=progress_callback,
        )
        try:
            os.replace(part_path, final_path)
        except OSError as exc:
            raise ApplicationUpdateError(
                "file_replace_failed", "更新包已校验，但无法写入最终文件名。"
            ) from exc
    except Exception:
        _remove_partial(part_path)
        raise
    return ApplicationUpdateDownload(release, kind, final_path, False)
