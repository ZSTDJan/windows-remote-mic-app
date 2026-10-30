"""Exercise the real mapping editor and file-dialog result in isolated Qt apps."""
import json
import os
import subprocess
import sys
import unittest

from tests import test_qt_settings_app as qt_tests

PROBE = r'''
import json, os, sys
from pathlib import Path
from PySide6.QtCore import QMetaObject, QUrl, QPointF, Qt
from PySide6.QtGui import QFontDatabase, QPalette, QColor
from PySide6.QtTest import QTest
from ovb_rc003 import qt_settings_app as m, config, key_mapping
from tests.test_qt_settings_app import SettingsControllerTests
classes = m._load_qt_classes()
classes['QQuickStyle'].setStyle('FluentWinUI3')
app = classes['QGuiApplication']([])
for font in ('msyh.ttc', 'msyhbd.ttc', 'segmdl2.ttf', 'seguiemj.ttf'):
    path = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts' / font
    if path.is_file(): QFontDatabase.addApplicationFont(str(path))
if os.environ['PROGRAM_TEST_DARK'] == '1':
    palette = QPalette()
    palette.setColor(QPalette.Window, QColor('#171a1f'))
    palette.setColor(QPalette.WindowText, QColor('#f3f4f6'))
    app.setPalette(palette)
fixture = SettingsControllerTests()
fixture.setUp()
try:
    profile = os.environ['PROGRAM_TEST_PROFILE']
    settings = config.default_config()
    settings['selected_device_profile'] = profile
    config.save_settings_pair(config.config_path(), settings, config.key_bindings_path(), config.default_key_bindings(profile))
    controller, model = fixture._make_controller()
    diagnostics = classes['DiagnosticsController'](controller, controller._config_root, auto_refresh=False)
    for name, instance in [('SettingsController', controller), ('ButtonMappingModel', model), ('DiagnosticsController', diagnostics)]:
        classes['qmlRegisterSingletonInstance'](classes[name], 'OvbRc003Settings', 1, 0, name, instance)
    engine = classes['QQmlApplicationEngine']()
    warnings = []
    engine.warnings.connect(lambda items: warnings.extend(item.toString() for item in items))
    engine.load(QUrl.fromLocalFile(str(m._qml_directory() / 'main.qml')))
    assert engine.rootObjects(), warnings
    window = engine.rootObjects()[0]
    window.setWidth(640)
    window.setHeight(480)
    def tick():
        app.processEvents()
        QTest.qWait(240)
        app.processEvents()
    def find(name):
        def walk(root):
            if root.objectName() == name: return root
            children = list(root.children())
            if hasattr(root, 'childItems'):
                children.extend(child for child in root.childItems() if child not in children)
            for child in children:
                found = walk(child)
                if found is not None: return found
        result = walk(window)
        assert result is not None, name
        return result
    def click(name):
        item = find(name)
        assert item.property('visible') and item.property('enabled'), name
        point = item.mapToScene(QPointF(item.width()/2, item.height()/2))
        assert 0 <= point.x() <= window.width() and 0 <= point.y() <= window.height(), (name, point)
        QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, point.toPoint())
        tick()
    find('tabBar').setProperty('currentIndex', 1)
    tick()
    click('editMapping_ok')
    dialog = find('actionEditorDialog')
    combo = find('actionEditorPrimaryCombo')
    options = controller.primaryActionOptionsFor('ok')
    arrow = combo.mapToScene(QPointF(combo.width() - 8, combo.height()/2))
    QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, arrow.toPoint())
    tick()
    QTest.keyClick(window, Qt.Key_Home)
    for _ in range(options.index('打开程序')): QTest.keyClick(window, Qt.Key_Down)
    QTest.keyClick(window, Qt.Key_Return)
    tick()
    assert find('actionEditorPrimaryPath').property('visible')
    assert not find('actionEditorPrimaryRecordButton').property('visible')
    assert not find('actionEditorPrimaryContent').property('visible')
    click('actionEditorSaveButton')
    assert dialog.property('visible')
    assert '填写' in find('actionEditorValidationError').property('text')
    typed = 'C:\\Program Files\\' + '较长目录 & 测试\\' * 8 + '程序.exe'
    field = find('actionEditorPrimaryPathField')
    field.setProperty('text', '"' + typed + '"')
    tick()
    click('actionEditorSaveButton')
    assert not dialog.property('visible'), (dialog.property('validationError'), field.property('text'), warnings)
    assert controller._bindings['bindings']['ok']['program_path'] == typed
    click('editMapping_ok')
    assert field.property('text') == typed
    assert not dialog.property('draftDirty')
    preview = find('actionEditorPrimaryPathPreview')
    assert preview.property('visible') and preview.property('truncated')
    path_label = find('actionEditorPrimaryPathLabel')
    label_center = path_label.mapToScene(QPointF(0, path_label.height()/2))
    field_center = field.mapToScene(QPointF(0, field.height()/2))
    assert abs(label_center.y() - field_center.y()) < 2, 'path label and field must share a row'
    click('actionEditorPrimaryPathField')
    assert not preview.property('visible')
    QTest.keyClick(window, Qt.Key_Home)
    assert field.property('cursorPosition') == 0
    combo.forceActiveFocus()
    tick()
    assert preview.property('visible') and field.property('text') == typed
    # Exercise the same QML acceptance callback as the native picker. The
    # non-native dialog lets an isolated offscreen test verify its lifecycle.
    picker = find('mappingProgramFileDialog')
    picker.setProperty('options', 8)  # FileDialog.DontUseNativeDialog
    click('actionEditorPrimaryPathBrowse')
    assert picker.property('visible')
    QMetaObject.invokeMethod(picker, 'reject')
    tick()
    assert field.property('text') == typed
    assert not dialog.property('draftDirty')
    picked_path = controller._config_root / '选择 测试' / '程序.exe'
    picked_path.parent.mkdir()
    picked_path.touch()
    picker.setProperty('currentFolder', QUrl.fromLocalFile(str(picked_path.parent)))
    click('actionEditorPrimaryPathBrowse')
    chosen = find('fileDialogDelegate0')
    assert chosen.property('fileName') == picked_path.name
    point = chosen.mapToScene(QPointF(chosen.width()/2, chosen.height()/2))
    QTest.mouseDClick(chosen.window(), Qt.LeftButton, Qt.NoModifier, point.toPoint())
    tick()
    assert not picker.property('visible'), 'file selection did not complete'
    assert key_mapping.normalize_program_path(field.property('text')) == str(picked_path), (picker.property('selectedFile'), field.property('text'))
    click('actionEditorSaveButton')
    assert controller._bindings['bindings']['ok']['program_path'] == str(picked_path)
    click('editMapping_ok')
    field.setProperty('text', typed)
    assert dialog.property('draftDirty')
    for name, trigger in (('Double', 'double_click'), ('Long', 'long_press')):
        find('actionEditor' + name + 'Combo').setProperty('editText', '打开程序')
        find('actionEditor' + name + 'PathField').setProperty('text', typed.replace('程序.exe', trigger + '.exe'))
    tick()
    assert dialog.property('height') <= window.height()
    screenshot = os.environ.get('PROGRAM_TEST_SCREENSHOT_DIR')
    if screenshot:
        window.grabWindow().save(str(Path(screenshot) / (profile + '-program-paths.png')))
    click('actionEditorSaveButton')
    assert controller._bindings['bindings']['ok']['program_path'] == typed
    for name, trigger in (('Double', 'double_click'), ('Long', 'long_press')):
        assert controller._bindings['secondary_bindings']['ok'][trigger]['program_path'] == typed.replace('程序.exe', trigger + '.exe')
    click('editMapping_ok')
    field.setProperty('text', r'C:\cancelled.exe')
    click('actionEditorCancelButton')
    click('editMapping_ok')
    assert field.property('text') == typed
    assert not dialog.property('draftDirty')
    # Switching modes keeps each draft distinct; saved payload follows the mode.
    combo.setProperty('editText', '输入文字')
    find('actionEditorPrimaryContentField').setProperty('text', typed)
    tick()
    assert not find('actionEditorPrimaryPath').property('visible')
    click('actionEditorSaveButton')
    assert controller._bindings['bindings']['ok']['text'] == typed
    assert 'program_path' not in controller._bindings['bindings']['ok']
    click('editMapping_ok')
    combo.setProperty('editText', 'Ctrl+C')
    tick()
    assert combo.property('editable')
    assert find('actionEditorPrimaryRecordButton').property('visible')
    click('actionEditorSaveButton')
    assert controller._bindings['bindings']['ok'] == {'kind': 'key_combo', 'keys': ['ctrl', 'c']}
    assert not warnings, warnings
    print(json.dumps({'profile': profile, 'warnings': warnings}))
    window.hide()
    engine.deleteLater()
    app.processEvents()
finally:
    fixture.tearDown()
    fixture.doCleanups()
    m._shutdown_diagnostics_workers()
'''


@unittest.skipUnless(qt_tests._HAS_PYSIDE6, qt_tests._SKIP_REASON)
class ProgramMappingQmlTests(unittest.TestCase):
    def test_manual_browse_save_reopen_cancel_and_switch_modes(self):
        for profile, scale, dark in (('xiaomi-rc003', '1', '0'), ('chromecast-remote', '1.5', '1')):
            with self.subTest(profile=profile):
                env = dict(os.environ, QT_QPA_PLATFORM='offscreen', RC003_DISABLE_LIVE_INPUT='1',
                           PROGRAM_TEST_PROFILE=profile, PROGRAM_TEST_DARK=dark, QT_SCALE_FACTOR=scale)
                result = subprocess.run([sys.executable, '-B', '-c', PROBE], env=env,
                                        capture_output=True, text=True, encoding='utf-8', timeout=50)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(json.loads(result.stdout.strip().splitlines()[-1])['warnings'], [])
