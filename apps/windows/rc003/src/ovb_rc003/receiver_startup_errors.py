"""Bounded receiver-startup evidence, stripped of local identities before IPC.

Ordinary exception logs keep their existing strict metadata policy. This narrow
entry is for receiver startup failures, whose native operation was previously
lost. Diagnostics add no filesystem, process or device queries.
"""
from __future__ import annotations

import os
import re
import hashlib

MAX_MESSAGE = 160  # Worst-case JSON Unicode escaping still fits the 2048-byte pipe.
WORK_DIRECTORY_FAILED = 'hid_work_directory_failed'
_PATH = re.compile(r'''(?i)(?:\\\\\?\\|\\\\\.\\|[a-z]:[\\/]|\\\\)[^\r\n\"'<>|]*''')
_URL = re.compile(r'''(?i)\b(?:https?|file)://[^\s"'<>]+''')
_IDENTITY = re.compile(r'(?i)\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b|\bS-1-\d+(?:-\d+){1,15}\b')
_DEVICE_PATH = re.compile(r'''(?i)\b(?:BluetoothLE#|BTHLE\\+|BTHENUM\\+|HID\\+|USB\\+|PCI\\+)[^\s"'<>|]+''')
_ADDRESS = re.compile(r'(?i)(?<![0-9a-f])(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}(?![0-9a-f])')
_EMAIL = re.compile(r'(?i)(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+')
_PRIVATE_PATH = re.compile(r'''(?i)(?:[a-z]:[\\/]|\\\\)[^\r\n"<>|]*?(?=:\s|[\r\n"<>|]|$)''')


def root_reference(value):
    normalized = os.path.normcase(os.path.normpath(value)) if value else ''
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest()


def failure_environment():
    # The elevated receiver and the ordinary report process may have different
    # temporary roots. Compare opaque references instead of assuming equality.
    roots = [os.environ.get(key, '') for key in ('TEMP', 'TMP', 'TMPDIR')]
    return dict(zip(('temp_ref', 'tmp_ref', 'tmpdir_ref'), map(root_reference, roots)),
                configured_mask=sum((1 << index) for index, value in enumerate(roots) if value))


def private_identity_pattern():
    identities = [os.environ.get(name) for name in ('USERNAME', 'USERDOMAIN', 'COMPUTERNAME')]
    alternatives = '|'.join(re.escape(value) for value in identities if value)
    return re.compile(r'(?i)(?<![\w])(?:' + alternatives + r')(?![\w])' if alternatives else r'(?!)')


def redact_private_text(text, *, identities=None):
    """Strip machine identifiers from native errors and retained log text."""
    text = _URL.sub('<url>', text)
    text = _PRIVATE_PATH.sub('<path>', text)
    text = _PATH.sub('<path>', text)
    text = _DEVICE_PATH.sub('<device>', text)
    text = _ADDRESS.sub('<device>', text)
    text = _EMAIL.sub('<email>', text)
    text = _IDENTITY.sub('<id>', text)
    return (identities or private_identity_pattern()).sub('<identity>', text)


def safe_message(value):
    """Bound text first while keeping the native reason after a path."""
    text = redact_private_text(str(value)[:4096])
    return ''.join(' ' if ord(c) < 32 or ord(c) == 127 else c for c in text)[:MAX_MESSAGE]


def failure_details(error):
    try:
        # Native Frida and OS errors describe this exact failed operation.
        # An arbitrary application exception can contain user content; retain
        # its existing type/hash metadata instead of exporting its text.
        if isinstance(error, OSError):
            import ctypes
            winerror = getattr(error, 'winerror', None)
            code = winerror if winerror is not None else getattr(error, 'errno', None)
            text = (ctypes.FormatError(winerror) if winerror is not None else os.strerror(code)) if isinstance(code, int) else 'OS error without native code'
            return dict(error_message=safe_message(text), message_clipped=0)
        if type(error).__module__ not in ('frida', 'frida._frida', '_frida'):
            return dict(error_message='non-native exception text withheld', message_clipped=0)
        text = str(error)
        return dict(error_message=safe_message(text), message_clipped=int(len(text) > MAX_MESSAGE))
    except Exception:
        return dict(error_message='exception text unavailable', message_clipped=1)


# Fixed operation names are shared with the existing IPC validator. These are
# observations of existing calls, never alternative connection entry points.
DEVICE_OPEN_CALLS = frozenset({
    'open', 'winrt_import', 'adapter_selector', 'watcher_create',
    'watcher_add_added', 'watcher_add_removed', 'watcher_add_stopped',
    'watcher_add_enumeration_completed', 'watcher_start', 'adapter_enumeration',
    'adapter_validate', 'paired_selector', 'paired_enumerate', 'paired_identity_scan',
    'candidate_identity', 'paired_endpoint_select',
    'selected_name_read', 'input_resolve',
    'paired_id_read', 'ble_from_id', 'ble_id_read', 'ble_validate',
})
DEVICE_OPEN_OUTCOMES = frozenset({'pending', 'success', 'failed', 'cancelled', 'timeout', 'skipped'})


def device_open_message(value):
    """Native opening errors may include an unprefixed AEP or Bluetooth address."""
    text = str(value)[:4096]
    text = re.sub(r'(?i)\b(?:BluetoothLE#|BTHLE\\|BTHENUM\\|USB\\|PCI\\)[^\s\"\'<>|]*', '<device>', text)
    text = re.sub(r'(?i)(?<![0-9a-f])(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}(?![0-9a-f])|\b[0-9a-f]{12}\b', '<device>', text)
    return safe_message(text)


def _open_error_details(error):
    # Preserve the original native message (including WinRT restricted error
    # information), not just FormatError's generic HRESULT translation.
    if not isinstance(error, (OSError, ImportError)):
        return dict(error_message='non-native exception text withheld', message_clipped=0)
    text = str(error)
    return dict(error_message=device_open_message(text), message_clipped=int(len(text) > MAX_MESSAGE),
        error_chars=min(0xffffffff, len(text)), error_sha256=hashlib.sha256(text.encode('utf-8', 'replace')).hexdigest())


class DeviceOpenLog:
    """One opening attempt; bounded existing-IPC records, no I/O or native query.

    The original operation owns its value, exception, cancellation and resources.
    Ordinary rows stop at 64; failures and the final summary remain distinguishable.
    """
    def __init__(self, emit):
        self.emit = emit
        self.sequence = self.rows = self.omitted = 0
        self.identifier = {}

    def _record(self, call, state, started, details, error=None, *, summary=False):
        try:
            if self.emit is None:
                return
            critical = state == 'failed' and details.get('open_outcome') != 'skipped'
            if self.rows >= 64 and not critical and not summary:
                self.omitted += 1
                return
            import time
            from .chromecast_hid_tap_windows import startup_record
            self.rows += 1
            row = startup_record('device_open_summary' if summary else 'device_open_call', state,
                started=started, error=error,
                details=dict(details, open_call=call, rows_omitted=min(0xffffffff, self.omitted)))
            if row is not None:
                self.emit(row)
            else:
                self.omitted += 1
            seen = set()
            for depth in range(4):
                if not isinstance(error, Exception) or id(error) in seen:
                    break
                seen.add(id(error))
                origin, line = 'native_or_external', -1
                tb = error.__traceback__
                while tb is not None:
                    name = tb.tb_frame.f_globals.get('__name__', '').rsplit('.', 1)[-1]
                    if name in ('remote_selection', 'chromecast_device_windows'):
                        origin, line = name, tb.tb_lineno
                    tb = tb.tb_next
                row = startup_record('device_open_error', 'failed', error=error,
                    details=dict(_open_error_details(error), open_call=call,
                        call_index=details['call_index'], chain_depth=depth, error_origin=origin, source_line=line))
                if row is not None:
                    self.rows += 1
                    self.emit(row)
                else:
                    self.omitted += 1
                error = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
        except Exception:
            self.omitted += 1  # Never replace the original operation on logging failure.

    def operation(self, call, *, expected=(), summary=False, **details):
        from contextlib import contextmanager

        @contextmanager
        def observe():
            if self.emit is None:
                yield details
                return
            import asyncio
            import time
            self.sequence += 1
            details.update(call_index=min(0xffffffff, self.sequence), open_outcome='pending')
            if call == 'ble_from_id':
                details.update(self.identifier)
            started = time.monotonic()
            self._record(call, 'begin', started, details, summary=summary)
            try:
                yield details
            except BaseException as error:
                details['open_outcome'] = ('skipped' if isinstance(error, expected) else
                    'cancelled' if isinstance(error, asyncio.CancelledError) else
                    'timeout' if isinstance(error, TimeoutError) else 'failed')
                if call == 'ble_from_id':
                    details.update(self.identifier)
                self._record(call, 'failed', started, details, error, summary=summary)
                raise
            else:
                details['open_outcome'] = 'success'
                self._record(call, 'success', started, details, summary=summary)
        return observe()

    def call(self, name, function, *args, expected=()):
        with self.operation(name, expected=expected) as details:
            value = function(*args)
            if self.emit is not None and name == 'paired_id_read':
                # Fingerprint the already-read value; never read the property twice
                # for logging or retain an address in the evidence.
                try:
                    text = value if isinstance(value, str) else ''
                    self.identifier = dict(device_id_size=min(0xffffffff, len(text)),
                        device_id_kind=('paired_ble' if text.casefold().startswith('bluetoothle#bluetoothle')
                            else 'interface' if text.startswith('\\\\?\\') else 'other' if text else 'not_string'),
                        device_id_ref=hashlib.sha256(text[:4096].encode('utf-8')).hexdigest())
                    details.update(self.identifier)
                except Exception:
                    pass
            return value
