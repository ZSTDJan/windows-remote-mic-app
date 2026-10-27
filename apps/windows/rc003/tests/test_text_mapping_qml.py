"""Drive the real text editor in isolated Qt processes and temporary configs."""
import json
import os
import subprocess
import sys
import unittest

from tests import test_qt_settings_app as qt_tests

PROBE = r'''
import json, os
from pathlib import Path
from PySide6.QtCore import QMetaObject, QUrl, QPointF, Qt
from PySide6.QtGui import QFontDatabase, QPalette, QColor
from PySide6.QtTest import QTest
from ovb_rc003 import qt_settings_app as m, config, key_mapping, mapping_presets
from tests.test_qt_settings_app import SettingsControllerTests
classes = m._load_qt_classes()
classes['QQuickStyle'].setStyle('FluentWinUI3')
app = classes['QGuiApplication']([])
for font in ('msyh.ttc', 'msyhbd.ttc', 'segmdl2.ttf', 'seguiemj.ttf'):
    path = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts' / font
    if path.is_file(): QFontDatabase.addApplicationFont(str(path))
if os.environ.get('TEXT_TEST_DARK') == '1':
    palette = QPalette()
    palette.setColor(QPalette.Window, QColor('#171a1f'))
    palette.setColor(QPalette.WindowText, QColor('#f3f4f6'))
    app.setPalette(palette)
fixture = SettingsControllerTests()
fixture.setUp()
try:
    profile = os.environ['TEXT_TEST_PROFILE']
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
        QTest.qWait(220)
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
    # Select the actual dropdown option; do not simulate a text body as a command.
    options = controller.primaryActionOptionsFor('ok')
    arrow = combo.mapToScene(QPointF(combo.width() - 8, combo.height()/2))
    QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, arrow.toPoint())
    tick()
    QTest.keyClick(window, Qt.Key_Home)
    for _ in range(options.index('输入文字')):
        QTest.keyClick(window, Qt.Key_Down)
    QTest.keyClick(window, Qt.Key_Return)
    tick()
    assert find('actionEditorPrimaryContent').property('visible')
    assert not find('actionEditorPrimaryRecordButton').property('visible')
    click('actionEditorSaveButton')
    assert dialog.property('visible')
    assert '填写' in find('actionEditorValidationError').property('text')
    text = ' 你好😀\nCtrl+C\nEnter\nquicker:runaction:literal\t '
    find('actionEditorPrimaryContentField').setProperty('text', text)
    tick()
    click('actionEditorSaveButton')
    assert not dialog.property('visible')
    assert controller._bindings['bindings']['ok']['text'] == text
    imported_text = text.replace('\n', '\r\n')
    model.setTextContentAt(model.index_of('ok'), 'single_click', imported_text)
    assert controller._save_mapping()
    click('editMapping_ok')
    assert find('actionEditorPrimaryContentField').property('text') == text
    assert not dialog.property('draftDirty'), 'CRLF rendering must not count as an edit'
    # All three gestures can contain text without pushing Save out of the window.
    for name in ('Double', 'Long'):
        find('actionEditor' + name + 'Combo').setProperty('editText', '输入文字')
        find('actionEditor' + name + 'ContentField').setProperty('text', text + name)
    tick()
    assert dialog.property('height') <= window.height()
    screenshot = os.environ.get('TEXT_TEST_SCREENSHOT_DIR')
    if screenshot:
        window.grabWindow().save(str(Path(screenshot) / (profile + '-three-text.png')))
    click('actionEditorSaveButton')
    assert controller._bindings['bindings']['ok']['text'] == imported_text
    for name, trigger in (('Double', 'double_click'), ('Long', 'long_press')):
        assert controller._bindings['secondary_bindings']['ok'][trigger]['text'] == text + name
    click('editMapping_ok')
    # Editing only the body must make a dirty draft and persist on completion.
    find('actionEditorPrimaryContentField').setProperty('text', '第二段')
    assert dialog.property('draftDirty')
    click('actionEditorSaveButton')
    assert controller._bindings['bindings']['ok']['text'] == '第二段'
    click('editMapping_ok')
    find('actionEditorPrimaryContentField').setProperty('text', '不保存')
    click('actionEditorCancelButton')
    click('editMapping_ok')
    assert find('actionEditorPrimaryContentField').property('text') == '第二段'
    # Switching back makes manual shortcut entry available; it is still a chord.
    arrow = combo.mapToScene(QPointF(combo.width() - 8, combo.height()/2))
    QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, arrow.toPoint())
    tick()
    QTest.keyClick(window, Qt.Key_Home)
    QTest.keyClick(window, Qt.Key_Return)
    tick()
    assert combo.property('editable')
    combo.setProperty('editText', 'Ctrl+C')
    tick()
    assert find('actionEditorPrimaryRecordButton').property('visible')
    assert not find('actionEditorPrimaryContent').property('visible')
    click('actionEditorSaveButton')
    assert controller._bindings['bindings']['ok'] == {'kind':'key_combo', 'keys':['ctrl','c']}
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
class TextMappingQmlTests(unittest.TestCase):
    def test_edit_save_reopen_and_cancel_in_both_profiles(self):
        for profile, scale, dark in (("xiaomi-rc003", "1", "0"), ("chromecast-remote", "1.5", "1")):
            with self.subTest(profile=profile):
                env = dict(os.environ, QT_QPA_PLATFORM="offscreen", RC003_DISABLE_LIVE_INPUT="1",
                           TEXT_TEST_PROFILE=profile, TEXT_TEST_DARK=dark, QT_SCALE_FACTOR=scale)
                result = subprocess.run([sys.executable, "-B", "-c", PROBE], env=env,
                                        capture_output=True, text=True, encoding="utf-8", timeout=40)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(json.loads(result.stdout.strip().splitlines()[-1])["warnings"], [])
