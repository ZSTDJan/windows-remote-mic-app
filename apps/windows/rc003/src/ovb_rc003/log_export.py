"""User-requested ZIP of existing diagnostic logs, never config or recordings."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import zipfile

from . import __version__, diagnostic_trace, logging_setup

# Log writers rotate at 5 MiB. Bound older/unrotated helper logs as well.
MAX_FILE_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class ExportResult:
    outcome: str
    path: Path | None = None
    file_count: int = 0
    incomplete: bool = False


def default_filename() -> str:
    return datetime.now().strftime('无线麦日志-%Y%m%d-%H%M%S.zip')


def _log_names() -> tuple[str, ...]:
    names = []
    for name, backups in ((logging_setup.LOG_FILENAME, logging_setup.LOG_BACKUP_COUNT),
                          (diagnostic_trace.TRACE_FILENAME, diagnostic_trace.TRACE_BACKUP_COUNT)):
        names.extend([name] + [f'{name}.{i}' for i in range(1, backups + 1)])
    return (*names, logging_setup.HID_HELPER_LOG_FILENAME, diagnostic_trace.REPORT_FILENAME)


def _snapshot(path: Path) -> tuple[bytes, dict]:
    before = path.lstat()
    if (not stat.S_ISREG(before.st_mode)
            or getattr(before, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
        raise OSError('unsupported_log_file')
    with path.open('rb') as source:
        opened = os.fstat(source.fileno())
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise OSError('log_replaced_during_open')
        offset = max(0, opened.st_size - MAX_FILE_BYTES)
        source.seek(offset)
        content = source.read(min(opened.st_size, MAX_FILE_BYTES))
        after = os.fstat(source.fileno())
    # A truncated text log starts at its first complete line, not in a UTF-8 character.
    if offset and path.name != diagnostic_trace.REPORT_FILENAME:
        newline = content.find(b'\n')
        content = content[newline + 1:] if newline >= 0 else b''
    return content, dict(source_bytes=opened.st_size, exported_bytes=len(content),
                         truncated=bool(offset), modified_ns=opened.st_mtime_ns,
                         changed_during_read=(after.st_size, after.st_mtime_ns)
                         != (opened.st_size, opened.st_mtime_ns))


def export_logs(destination: Path, *, root: Path | None = None,
                cancel: threading.Event | None = None) -> ExportResult:
    """Replace only the selected ZIP, atomically after success; keep original logs intact."""
    destination = Path(destination)
    if not destination.is_absolute() or destination.suffix.casefold() != '.zip':
        return ExportResult('invalid_destination')
    cancelled = cancel.is_set if cancel is not None else lambda: False
    temporary = None
    try:
        if cancelled():
            return ExportResult('cancelled')
        directory = logging_setup.log_dir(root)
        records, captured = [], []
        # Read a bounded snapshot first so empty/wholly unreadable logs create no archive.
        for name in _log_names():
            if cancelled():
                return ExportResult('cancelled')
            try:
                content, metadata = _snapshot(directory / name)
            except FileNotFoundError:
                records.append(dict(name=name, status='missing'))
                continue
            except OSError:
                records.append(dict(name=name, status='unreadable'))
                continue
            records.append(dict(name=name, status='included', **metadata))
            captured.append((name, content))
        if not captured:
            outcome = 'read_failed' if any(r['status'] == 'unreadable' for r in records) else 'no_logs'
            return ExportResult(outcome)
        incomplete = any(r['status'] == 'unreadable' or r.get('truncated') for r in records)
        manifest = dict(schema_version=1, app_version=__version__,
                        exported_at=datetime.now().astimezone().isoformat(),
                        snapshot='bounded_live_files_not_atomic', files=records,
                        includes_configuration=False, includes_recordings=False)
        with tempfile.NamedTemporaryFile(prefix='.remote-mic-logs-', suffix='.tmp',
                                         dir=destination.parent, delete=False) as output:
            temporary = Path(output.name)
        with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED, compresslevel=3) as archive:
            for name, content in captured:
                if cancelled():
                    return ExportResult('cancelled')
                archive.writestr(name, content)
            archive.writestr('export-info.json', json.dumps(manifest, ensure_ascii=False, indent=2))
        if cancelled():
            return ExportResult('cancelled')
        os.replace(temporary, destination)
        return ExportResult('exported', destination, len(captured), incomplete)
    except (OSError, ValueError, zipfile.BadZipFile):
        return ExportResult('write_failed')
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def describe_result(result: ExportResult) -> str:
    if result.outcome == 'exported':
        note = '；部分日志已截取或未能读取，详情见包内说明' if result.incomplete else ''
        return f'已导出 {result.file_count} 个日志文件：{result.path}{note}'
    return {
        'no_logs': '暂无可导出的日志。请先运行服务，复现问题后再导出。',
        'read_failed': '日志暂时无法读取，请稍后重试。',
        'invalid_destination': '请选择一个 ZIP 文件保存位置。',
        'cancelled': '已取消日志导出。',
        'write_failed': '日志导出失败，请检查保存位置是否可写、磁盘空间是否充足后重试。',
    }.get(result.outcome, '日志导出失败，请重试。')
