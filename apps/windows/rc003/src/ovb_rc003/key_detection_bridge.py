"""One-shot file IPC between the settings window and the running bridge."""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional


SCHEMA_VERSION = 1
STALE_AFTER_SECONDS = 60.0


@dataclass(frozen=True)
class DetectionRequest:
    token: str
    root: Path

    @property
    def request_path(self) -> Path:
        return self.root / f"request-{self.token}.json"

    @property
    def claimed_path(self) -> Path:
        return self.root / f"claimed-{self.token}.json"

    @property
    def claim_lock_path(self) -> Path:
        return self.root / f"claim-{self.token}.lock"

    @property
    def result_path(self) -> Path:
        return self.root / f"result-{self.token}.json"


def request_detection(
    config_root: Path,
    *,
    now: Callable[[], float] = time.time,
) -> DetectionRequest:
    root = Path(config_root) / "key-detection"
    root.mkdir(parents=True, exist_ok=True)
    _cleanup_stale(root, now=now)
    request = DetectionRequest(token=uuid.uuid4().hex, root=root)
    _write_json_atomic(
        request.request_path,
        {
            "schema": SCHEMA_VERSION,
            "token": request.token,
            "created_at": now(),
        },
    )
    return request


def has_pending_request(
    config_root: Path,
    *,
    now: Callable[[], float] = time.time,
) -> bool:
    root = Path(config_root) / "key-detection"
    if not root.is_dir():
        return False
    _cleanup_stale(root, now=now)
    return any(root.glob("request-*.json"))


def publish_next_button(
    config_root: Path,
    button_id: str,
    *,
    now: Callable[[], float] = time.time,
) -> bool:
    """Claim the oldest request and publish one logical button id."""

    if not button_id or not all(ch.isalnum() or ch == "_" for ch in button_id):
        return False
    root = Path(config_root) / "key-detection"
    if not root.is_dir():
        return False
    _cleanup_stale(root, now=now)
    request_paths = list(root.glob("request-*.json"))
    request_paths.sort(key=_request_age_key)
    for request_path in request_paths:
        prefix = "request-"
        suffix = ".json"
        if not request_path.name.startswith(prefix) or not request_path.name.endswith(
            suffix
        ):
            continue
        candidate = DetectionRequest(
            token=request_path.name[len(prefix) : -len(suffix)],
            root=root,
        )
        try:
            claim_fd = os.open(
                candidate.claim_lock_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            )
        except FileExistsError:
            continue
        os.close(claim_fd)
        request_claimed = False
        try:
            request = _read_request(request_path, now=now)
            if request is None:
                _unlink(request_path)
                continue
            os.replace(request.request_path, request.claimed_path)
            request_claimed = True
            _write_json_atomic(
                request.result_path,
                {
                    "schema": SCHEMA_VERSION,
                    "token": request.token,
                    "button_id": button_id,
                    "created_at": now(),
                },
            )
        except OSError:
            if request_claimed:
                try:
                    os.replace(request.claimed_path, request.request_path)
                except OSError:
                    pass
            continue
        finally:
            _unlink(candidate.claim_lock_path)
        _unlink(request.claimed_path)
        return True
    return False


def _request_age_key(path: Path) -> tuple[int, str]:
    try:
        modified_ns = path.stat().st_mtime_ns
    except OSError:
        modified_ns = 2**63 - 1
    return modified_ns, path.name


def poll_detection(request: DetectionRequest) -> Optional[str]:
    payload = _read_json(request.result_path)
    if not isinstance(payload, dict):
        return None
    if payload.get("schema") != SCHEMA_VERSION or payload.get("token") != request.token:
        return None
    button_id = payload.get("button_id")
    if not isinstance(button_id, str) or not button_id:
        return None
    if not all(ch.isalnum() or ch == "_" for ch in button_id):
        return None
    return button_id


def cancel_detection(request: DetectionRequest) -> None:
    _unlink(request.request_path)
    _unlink(request.claimed_path)
    _unlink(request.claim_lock_path)
    _unlink(request.result_path)


def _read_request(
    path: Path,
    *,
    now: Callable[[], float],
) -> Optional[DetectionRequest]:
    payload = _read_json(path)
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA_VERSION:
        return None
    token = payload.get("token")
    created_at = payload.get("created_at")
    if not isinstance(token, str) or path.name != f"request-{token}.json":
        return None
    if not isinstance(created_at, (int, float)):
        return None
    if now() - float(created_at) > STALE_AFTER_SECONDS:
        return None
    return DetectionRequest(token=token, root=path.parent)


def _cleanup_stale(root: Path, *, now: Callable[[], float]) -> None:
    cutoff = now() - STALE_AFTER_SECONDS
    for pattern in (
        "request-*.json",
        "claimed-*.json",
        "claim-*.lock",
        "result-*.json",
        ".*.tmp",
    ):
        for path in root.glob(pattern):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                pass


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def _write_json_atomic(path: Path, payload: dict) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=True, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        _unlink(temporary)


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass
