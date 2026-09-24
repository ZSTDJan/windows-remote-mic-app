"""Exercise secondary dialogs with isolated settings and no live device I/O."""
import os
import subprocess
import sys
import tempfile
import unittest

from tests.test_remote_selection import _QML_PROBE


_PROBE = _QML_PROBE.split("click('selectRemoteButton')")[0] + r'''
from PySide6.QtCore import QPointF, Qt
from PySide6.QtTest import QTest
for font_name in ('SegoeIcons.ttf', 'segmdl2.ttf'):
    icon_font = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts' / font_name
    if icon_font.is_file():
        QFontDatabase.addApplicationFont(str(icon_font))

def settle():
    for _ in range(8):
        app.processEvents()
        window.grabWindow()

def bounds(control):
    point = control.mapToScene(QPointF(0, 0))
    return (point.x(), point.y(), control.width(), control.height())

def screenshot(name):
    directory = os.environ.get('SECONDARY_DIALOG_SCREENSHOTS')
    if directory:
        window.grabWindow().save(str(Path(directory) / (name + '.png')))

results = []
for width, height in ((640, 480), (720, 560)):
    window.setWidth(width)
    window.setHeight(height)
    settle()
    click('selectRemoteButton')
    settle()
    dialog = item('remoteDeviceDialog')
    assert dialog.property('width') <= 520
    assert dialog.property('height') < 250
    for combo, action, other in (
        ('remoteDeviceCombo', 'useRemoteButton', 'refreshRemotesButton'),
    ):
        cb, bt, extra = [bounds(item(n)) for n in (combo, action, other)]
        assert 120 <= cb[2] < dialog.property('width'), cb
        assert abs(cb[1] - bt[1]) < 1, (cb, bt)
        assert abs(cb[1] - extra[1]) < 1, (cb, extra)
        assert cb[0] + cb[2] <= bt[0], (cb, bt)
        assert extra[0] + extra[2] < width, extra
    screenshot(f'{width}-device')
    dialog.setProperty('titleNote', '当前使用：' + '很长的遥控器名称' * 12)
    settle()
    assert dialog.property('width') <= 520
    click('closeRemoteDialogButton')

    item('tabBar').setProperty('currentIndex', 2)
    settle()
    for name in ('driverConfirmDialog', 'bridgeTestConfirmDialog'):
        dialog = item(name)
        QMetaObject.invokeMethod(dialog, 'open')
        settle()
        assert dialog.property('width') <= 390
        assert 100 < dialog.property('height') < 250, (name, dialog.property('height'))
        screenshot(f'{width}-{name}')
        QTest.keyClick(window, Qt.Key_Escape)
        settle()
        assert not dialog.property('visible')
    assert not diagnostics.vbCableTestRunning

    click('trySpeakingButton')
    settle()
    dialog = item('speakTestDialog')
    field = item('speakTestInput')
    assert dialog.property('visible') and field.property('activeFocus')
    assert dialog.property('height') <= 300
    for character in 'voice input':
        QTest.keyClick(window, character)
    assert field.property('text') == 'voice input', repr(field.property('text'))
    assert item('actualSpeechTestRow').property('stateText') == ''
    screenshot(f'{width}-input')
    click('clearSpeakTestButton')
    assert field.property('text') == '' and field.property('activeFocus')
    click('speakTestCloseButton')
    assert not dialog.property('visible')
    results.append({'size': [width, height], 'input': 'typed-cleared-closed'})
    item('tabBar').setProperty('currentIndex', 0)

from PySide6.QtQml import QQmlComponent
component = QQmlComponent(engine)
component.setData(b"""
import QtQuick
import QtQuick.Controls
import "."
SettingsDialog {
    tokens: Tokens {}
    standardButtons: Dialog.Ok | Dialog.Cancel
    property int accepts: 0
    property int rejects: 0
    onAccepted: accepts += 1
    onRejected: rejects += 1
    function confirmForProbe() { standardButton(Dialog.Ok).clicked() }
    function cancelForProbe() { standardButton(Dialog.Cancel).clicked() }
}
""", QUrl.fromLocalFile(str(m._qml_directory() / 'DialogProbe.qml')))
confirmation = component.createWithInitialProperties({'parent': window.contentItem()})
assert confirmation is not None, component.errors()
confirmation.setParent(window)
QMetaObject.invokeMethod(confirmation, 'open')
settle()
QMetaObject.invokeMethod(confirmation, 'confirmForProbe')
assert confirmation.property('accepts') == 1, confirmation.property('accepts')
QMetaObject.invokeMethod(confirmation, 'open')
settle()
QMetaObject.invokeMethod(confirmation, 'cancelForProbe')
assert confirmation.property('rejects') == 1, confirmation.property('rejects')
confirmation.deleteLater()
controller.shutdownBackgroundTasks()
m._shutdown_diagnostics_workers()
assert not warnings, warnings
print(json.dumps(results))
'''


class SecondaryDialogsQmlTests(unittest.TestCase):
    def test_compact_dialogs_and_plain_input(self):
        for style in ('Basic', 'Fusion'):
            with self.subTest(style=style), tempfile.TemporaryDirectory() as directory:
                env = dict(os.environ, LOCALAPPDATA=directory, QT_QPA_PLATFORM='offscreen',
                           RC003_DISABLE_LIVE_INPUT='1', PYTHONUTF8='1', PYTHONIOENCODING='utf-8',
                           REMOTE_SELECTION_STYLE=style)
                probe = _PROBE
                result = subprocess.run([sys.executable, '-c', probe], env=env,
                                        capture_output=True, text=True, encoding='utf-8', timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
