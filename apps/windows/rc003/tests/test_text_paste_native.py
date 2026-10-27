"""Real clipboard and EDIT on a private Windows window station, never the user's clipboard."""
import os
import subprocess
import sys
import unittest

PROBE = r'''
import ctypes, os, threading
from ctypes import wintypes as w
from ovb_rc003 import win32_input
u = ctypes.WinDLL('user32', use_last_error=True)
k = ctypes.WinDLL('kernel32', use_last_error=True)
def api(lib, name, args, result):
    fn = getattr(lib, name)
    fn.argtypes, fn.restype = args, result
    return fn
api(u, 'GetProcessWindowStation', (), w.HANDLE)
api(u, 'CreateWindowStationW', (w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID), w.HANDLE)
api(u, 'SetProcessWindowStation', (w.HANDLE,), w.BOOL)
api(u, 'CloseWindowStation', (w.HANDLE,), w.BOOL)
api(u, 'CreateDesktopW', (w.LPCWSTR, w.LPCWSTR, w.LPVOID, w.DWORD, w.DWORD, w.LPVOID), w.HANDLE)
api(u, 'GetThreadDesktop', (w.DWORD,), w.HANDLE)
api(u, 'SetThreadDesktop', (w.HANDLE,), w.BOOL)
api(u, 'CloseDesktop', (w.HANDLE,), w.BOOL)
api(k, 'GetCurrentThreadId', (), w.DWORD)
api(u, 'CreateWindowExW', (w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, w.HWND, w.HMENU, w.HINSTANCE, w.LPVOID), w.HWND)
api(u, 'DestroyWindow', (w.HWND,), w.BOOL)
api(u, 'SendMessageW', (w.HWND, w.UINT, w.WPARAM, w.LPARAM), w.LPARAM)
api(u, 'GetWindowTextW', (w.HWND, w.LPWSTR, ctypes.c_int), ctypes.c_int)
api(u, 'OpenClipboard', (w.HWND,), w.BOOL)
api(u, 'CloseClipboard', (), w.BOOL)
api(u, 'GetClipboardData', (w.UINT,), w.HANDLE)
api(k, 'GlobalLock', (w.HGLOBAL,), w.LPVOID)
api(k, 'GlobalUnlock', (w.HGLOBAL,), w.BOOL)
original_station = u.GetProcessWindowStation()
original_desktop = u.GetThreadDesktop(k.GetCurrentThreadId())
# CREATE_ONLY refuses an existing station rather than touching another
# process's clipboard. Named stations require an elevated administrator token.
station = u.CreateWindowStationW('RemoteMicTextTest' + str(os.getpid()), 1, 0x37f, None)
if not station and ctypes.get_last_error() == 5:
    station = u.CreateWindowStationW(None, 1, 0x37f, None)
if not station and ctypes.get_last_error() in (5, 183):
    print('SKIP: private window station unavailable without elevation; user clipboard untouched')
    raise SystemExit(77)
assert station, ('CreateWindowStation', ctypes.get_last_error())
desktop = edit = None
try:
    assert u.SetProcessWindowStation(station)
    desktop = u.CreateDesktopW('TextPasteTest', None, None, 0, 0x1ff, None)
    assert desktop, ('CreateDesktop', ctypes.get_last_error())
    assert u.SetThreadDesktop(desktop)
    edit = u.CreateWindowExW(0, 'EDIT', '', 0x0004 | 0x0040 | 0x1000, 0, 0, 500, 300, None, None, None, None)
    assert edit
    samples = [' 你好😀\nCtrl+C\nEnter\nquicker:runaction:literal\t ', '第二段\r\n第三段']
    emitted = []
    def sender(events):
        emitted.append(events)
        u.SendMessageW(edit, 0x302, 0, 0) # WM_PASTE into this disposable EDIT only.
        return len(events)
    def read_clipboard():
        assert u.OpenClipboard(edit)
        try:
            handle = u.GetClipboardData(13)
            assert handle
            ptr = k.GlobalLock(handle)
            assert ptr
            try: return ctypes.wstring_at(ptr)
            finally: k.GlobalUnlock(handle)
        finally: u.CloseClipboard()
    expected = ''
    for text in samples:
        win32_input.paste_text(text, _writer=win32_input._write_clipboard_text,
            _target_query=lambda: (int(edit), int(edit)), _key_down_query=lambda vk: False, _sender=sender)
        clipboard = text.replace('\r\n', '\n').replace('\r', '\n').replace('\n', '\r\n')
        assert read_clipboard() == clipboard, 'clipboard content/ownership did not survive owner destruction'
        expected += clipboard
        buffer = ctypes.create_unicode_buffer(2000)
        u.GetWindowTextW(edit, buffer, len(buffer))
        assert buffer.value == expected, (repr(buffer.value), repr(expected))
    assert len(emitted) == 2
    # A second thread holds this isolated clipboard. Failure must not send Ctrl+V.
    locked, release = threading.Event(), threading.Event()
    errors = []
    def holder():
        try:
            assert u.SetThreadDesktop(desktop)
            assert u.OpenClipboard(None)
            locked.set()
            assert release.wait(3)
            assert u.CloseClipboard()
        except BaseException as exc: errors.append(type(exc).__name__)
    thread = threading.Thread(target=holder)
    thread.start()
    try:
        assert locked.wait(3), errors
        try:
            win32_input.paste_text('must not land', _writer=win32_input._write_clipboard_text,
                _target_query=lambda: (int(edit), int(edit)), _key_down_query=lambda vk: False, _sender=sender)
        except OSError: pass
        else: raise AssertionError('busy clipboard must fail')
        assert len(emitted) == 2
    finally:
        release.set()
        thread.join(4)
    assert not errors, errors
    assert read_clipboard() == samples[-1]
    print('native private clipboard, multiline/emoji, EDIT paste, persistence and busy failure: passed')
finally:
    if edit: u.DestroyWindow(edit)
    if desktop:
        assert u.SetThreadDesktop(original_desktop)
        u.CloseDesktop(desktop)
    assert u.SetProcessWindowStation(original_station)
    u.CloseWindowStation(station)
'''


@unittest.skipUnless(sys.platform == "win32", "Windows private window-station clipboard")
class TextPasteNativeTests(unittest.TestCase):
    def test_clipboard_and_multiline_edit_without_user_desktop_access(self):
        result = subprocess.run([sys.executable, "-B", "-c", PROBE],
                                env=dict(os.environ, RC003_DISABLE_LIVE_INPUT="1"),
                                capture_output=True, text=True, encoding="utf-8", timeout=20)
        if result.returncode == 77 and "SKIP: private window station unavailable" in result.stdout:
            self.skipTest(result.stdout.strip())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("passed", result.stdout)
