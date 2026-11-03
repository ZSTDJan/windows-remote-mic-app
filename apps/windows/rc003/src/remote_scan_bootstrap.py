"""Early, stdlib-only diagnostics for the disposable paired-device reader.

This source bootstrap intentionally lives outside ovb_rc003: importing that
package or a compiled module may itself fail. The frozen runtime hook and the
source -m entry share this one protocol. Only the scan child installs the fatal
exception handler; UI, voice workers and the privileged helper are unaffected.
The parent owns the temporary journal and passes a narrowly inherited handle.
No paths, device names, exception messages or frame locals enter the journal.
"""
import json
import os
import re
import sys
import time

FLAG = "--list-remote-devices"
HANDLE_ENV = "REMOTE_MIC_SCAN_DIAGNOSTIC_HANDLE"
STAGES = frozenset({"starting", "runtime_hooks", "entrypoint_import", "scan_import",
                    "loop_init", "loop_shutdown", "winrt_import", "selector",
                    "enumerate", "device_rows", "result_write", "finished"})
COUNTS = ("enumerated", "identity_failed", "supported", "unsupported")
_MARKER = re.compile(r"[A-Za-z_][A-Za-z_0-9]{0,79}\Z")
_active = None


def error_fields(error):
    # Kept independent of application imports so a missing native module can
    # still report its original error, including failures in the runtime hooks.
    rows, seen = [], set()
    for _ in range(2):
        if error is None or id(error) in seen:
            break
        seen.add(id(error))
        name = type(error).__name__
        row = {"error_type": name if _MARKER.fullmatch(name) else "Exception"}
        for key in ("winerror", "hresult", "errno"):
            try:
                value = getattr(error, key, None)
            except Exception:
                continue
            if type(value) is int and -(2**31) <= value <= 0xffffffff:
                row[key] = value
        # Fixed module identities and numeric lines locate early RuntimeErrors
        # without retaining user-controlled exception text or filesystem paths.
        frames = []
        try:
            tb = error.__traceback__
        except Exception:
            tb = None
        while tb is not None:
            module = tb.tb_frame.f_globals.get("__name__", "")
            if (isinstance(module, str) and len(module) <= 120
                    and re.fullmatch(r"(?:ovb_rc003\.[a-z_0-9.]+|remote_scan_bootstrap|pyi_rth_[a-z_0-9]+)", module)):
                frames.append({"module": module, "line": tb.tb_lineno})
            tb = tb.tb_next
        if frames:
            row["frames"] = frames[-6:]
        rows.append(row)
        try:
            error = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
        except Exception:
            break
    return {"errors": rows, "native_text": "not_collected"}


class ScanDiagnostic:
    def __init__(self, path=None, fd=None):
        self.path = str(path) if path else None
        self.fd = fd
        self.started = time.monotonic()
        self.record = dict(schema=2, stage="starting", state="running",
                           **dict.fromkeys(COUNTS, 0))
        self.save()

    def stage(self, stage):
        self.record["stage"] = stage
        self.save()

    def error(self, error):
        # Preserve the original query failure if loop shutdown also fails.
        if self.record["state"] != "failed":
            self.record.update(state="failed", failure_stage=self.record["stage"],
                               **error_fields(error))
        self.save()

    def save(self):
        if self.path is None and self.fd is None:
            return
        self.record["elapsed_ms"] = int((time.monotonic() - self.started) * 1000)
        try:
            data = (json.dumps(self.record, separators=(",", ":")) + "\n").encode("ascii")
            if self.fd is not None:
                remaining = memoryview(data)
                while remaining:
                    written = os.write(self.fd, remaining)
                    if written <= 0:
                        raise OSError("diagnostic write made no progress")
                    remaining = remaining[written:]
            else:
                with open(self.path, "ab", buffering=0) as stream:
                    stream.write(data)
        except Exception as exc:
            self.record["write_failures"] = self.record.get("write_failures", 0) + 1
            self.record["write_errors"] = error_fields(exc)["errors"]

    def close(self):
        if self.fd is not None:
            fd, self.fd = self.fd, None
            try:
                os.close(fd)
            except OSError:
                pass


def diagnostic_for(path=None):
    if _active is not None and _active.path == (str(path) if path else None):
        return _active
    return ScanDiagnostic(path)


def mark(stage):
    if _active is not None:
        _active.stage(stage)


def activate():
    """Called before frozen runtime hooks or before importing the source package."""
    global _active
    if len(sys.argv) < 4 or sys.argv[1] != FLAG or not sys.argv[2] or not sys.argv[3]:
        return False
    if _active is not None:
        return True
    path = sys.argv[3]
    raw_handle = os.environ.pop(HANDLE_ENV, None)
    fd, handle_error = None, None
    if raw_handle is not None:
        try:
            handle = int(raw_handle)
            if handle <= 2:
                raise ValueError("invalid diagnostic handle")
            if sys.platform == "win32":
                import msvcrt
                fd = msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_BINARY)
            else:
                fd = handle
            os.set_inheritable(fd, False)
        except Exception as exc:
            handle_error = error_fields(exc)["errors"]
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            fd = None
    _active = ScanDiagnostic(path, fd)
    if handle_error:
        _active.record["channel_errors"] = handle_error
    _active.stage("runtime_hooks")

    def fatal_scan_error(error_type, error, traceback):
        # Returning to the windowed bootloader would display a modal traceback
        # and turn this known failure into the parent's generic timeout.
        try:
            _active.error(error)
        finally:
            _active.close()
            os._exit(1)
    sys.excepthook = fatal_scan_error
    return True


def source_main():
    if not activate():
        raise SystemExit(2)
    mark("entrypoint_import")
    # This bootstrap is also imported by the narrow privileged helper through
    # remote_selection. Do not add the whole GUI to that helper's import graph.
    import importlib
    importlib.import_module("ovb_rc003.__main__").main()


if __name__ == "__main__":
    # Share state with the application importing the bootstrap by module name.
    sys.modules["remote_scan_bootstrap"] = sys.modules[__name__]
    source_main()
