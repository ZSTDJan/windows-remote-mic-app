"""Real modal-panel flow in disposable Qt processes; no native remote input."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

from tests import test_qt_settings_app as qt_tests


PROBE = r'''
import json, os, time
from pathlib import Path
from PySide6.QtCore import QObject, QMetaObject, QUrl, QPointF, Qt
from PySide6.QtGui import QPalette, QColor, QFontDatabase
from PySide6.QtTest import QTest
from ovb_rc003 import qt_settings_app as m, config, mapping_presets
from tests.test_qt_settings_app import SettingsControllerTests

classes = m._load_qt_classes()
classes['QQuickStyle'].setStyle('FluentWinUI3')
app = classes['QGuiApplication']([])
# The offscreen platform does not enumerate all Windows system font fallbacks.
for font in ('msyh.ttc', 'msyhbd.ttc', 'segmdl2.ttf'):
    path = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts' / font
    if path.is_file():
        QFontDatabase.addApplicationFont(str(path))
if os.environ.get('PRESET_TEST_DARK') == '1':
    palette = QPalette()
    palette.setColor(QPalette.Window, QColor('#171a1f'))
    palette.setColor(QPalette.WindowText, QColor('#f3f4f6'))
    app.setPalette(palette)
fixture = SettingsControllerTests()
fixture.setUp()
try:
    profile = os.environ['PRESET_TEST_PROFILE']
    settings = config.load_config(config.config_path())
    settings['selected_device_profile'] = profile
    config.save_settings_pair(config.config_path(), settings, config.key_bindings_path(), config.default_key_bindings(profile))
    controller, model = fixture._make_controller()
    diagnostics = classes['DiagnosticsController'](controller, controller._config_root, auto_refresh=False)
    for key, instance in [('SettingsController', controller), ('ButtonMappingModel', model), ('DiagnosticsController', diagnostics)]:
        classes['qmlRegisterSingletonInstance'](classes[key], 'OvbRc003Settings', 1, 0, key, instance)
    engine = classes['QQmlApplicationEngine']()
    warnings = []
    engine.warnings.connect(lambda items: warnings.extend(item.toString() for item in items))
    engine.load(QUrl.fromLocalFile(str(m._qml_directory() / 'main.qml')))
    assert engine.rootObjects(), warnings
    window = engine.rootObjects()[0]
    window.setWidth(640)
    window.setHeight(480)
    def tick():
        for _ in range(3):
            app.processEvents()
        QTest.qWait(200)  # Fluent popup transitions finish before the next click.
    def find(name):
        def walk(root):
            if root.objectName() == name:
                return root
            children = list(root.children())
            if hasattr(root, 'childItems'):
                children.extend(child for child in root.childItems() if child not in children)
            for child in children:
                result = walk(child)
                if result is not None:
                    return result
            return None
        obj = walk(window)
        assert obj is not None, name
        return obj
    def invoke(name, method):
        assert QMetaObject.invokeMethod(find(name), method), (name, method)
        tick()
    def wait_for(predicate):
        deadline = time.monotonic() + 3
        while not predicate() and time.monotonic() < deadline:
            QTest.qWait(20)
            app.processEvents()
        assert predicate(), 'popup transition did not finish'
    def click(name):
        button = find(name)
        assert button.property('enabled'), name
        point = button.mapToScene(QPointF(button.width() / 2, button.height() / 2))
        assert 0 <= point.x() <= window.width() and 0 <= point.y() <= window.height(), (name, point)
        QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, point.toPoint())
        tick()
    find('tabBar').setProperty('currentIndex', 1)
    tick()
    click('editMapping_ok')
    assert find('actionEditorDialog').property('visible')
    find('actionEditorLongCombo').setProperty('editText', '鼠标左键长按')
    click('actionEditorSaveButton')
    assert controller._bindings['secondary_bindings']['ok']['long_press']['kind'] == 'mouse_left_hold'
    click('manageMappingPresetsButton')
    dialog = find('mappingPresetsDialog')
    assert dialog.property('visible')
    assert not find('deleteMappingPresetButton').property('enabled')
    assert not find('renameMappingPresetButton').property('enabled')
    assert len(controller.mappingPresets) == 1
    assert controller.mappingPresets[0]['automatic'] and controller.mappingPresets[0]['applied']
    combo = find('mappingPresetCombo')
    assert combo.property('currentIndex') == combo.property('effectiveIndex') == 0
    assert '自动保存' in combo.property('displayText')
    assert find('applyMappingPresetButton').property('text') == '应用配置'
    # All management actions form one row; every button stays within the dialog.
    buttons = [find(name + 'MappingPresetButton') for name in ('copy', 'rename', 'delete', 'export', 'import')]
    positions = [button.mapToScene(QPointF(0, 0)) for button in buttons]
    assert max(point.y() for point in positions) - min(point.y() for point in positions) < 1
    for button, point in zip(buttons, positions):
        assert point.x() >= dialog.property('x')
        assert point.x() + button.width() <= dialog.property('x') + dialog.property('width')
    screenshot_dir = os.environ.get('PRESET_TEST_SCREENSHOT_DIR')
    if screenshot_dir:
        window.grabWindow().save(str(Path(screenshot_dir) / (profile + '-initial.png')))
    click('copyMappingPresetButton')
    assert find('mappingPresetNameDialog').property('visible')
    # Invalid input remains editable in the naming dialog.
    field = find('mappingPresetNameField')
    field.setProperty('text', '')
    invoke('mappingPresetNameDialog', 'accept')
    wait_for(lambda: find('mappingPresetNameDialog').property('opened'))
    field.setProperty('text', '我的按键配置')
    invoke('mappingPresetNameDialog', 'accept')
    wait_for(lambda: not find('mappingPresetNameDialog').property('visible'))
    assert len(controller.mappingPresets) == 2
    first = controller.mappingPresets[1]['id']
    assert combo.property('currentIndex') == 1 and combo.property('effectiveIndex') == 0
    click('copyMappingPresetButton')
    assert find('mappingPresetNameDialog').property('visible')
    # Browsing another row cannot redirect the copy's source.
    dialog.setProperty('selectedId', mapping_presets.AUTO_ID)
    invoke('mappingPresetNameDialog', 'accept')
    second = dialog.property('selectedId')
    assert second != first
    # Changing the selection after opening the name dialog cannot redirect rename.
    click('renameMappingPresetButton')
    dialog.setProperty('selectedId', first)
    field.setProperty('text', '副本改名')
    invoke('mappingPresetNameDialog', 'accept')
    store = mapping_presets.PresetStore(controller._config_root)
    assert store.get(second)['name'] == '副本改名'
    assert store.get(first)['name'] == '我的按键配置'
    before = dict(controller._bindings)
    click('applyMappingPresetButton')
    assert find('mappingPresetConfirmDialog').property('visible')
    invoke('mappingPresetConfirmDialog', 'reject')
    assert controller._bindings == before
    click('applyMappingPresetButton')
    invoke('mappingPresetConfirmDialog', 'accept')
    assert '已应用' in dialog.property('message'), dialog.property('message')
    applied_index = next(index for index, row in enumerate(controller.mappingPresets) if row['applied'])
    assert combo.property('effectiveIndex') == applied_index
    # Browsing an unapplied row must not move the check mark or bold type.
    dialog.setProperty('selectedId', first)
    tick()
    assert combo.property('effectiveIndex') == applied_index
    click('mappingPresetCombo')
    for index in range(len(controller.mappingPresets)):
        option = find('mappingPresetCombo_option_' + str(index))
        weight = option.property('contentItem').property('font').weight()
        assert int(weight) == (600 if index == applied_index else 400), (index, weight)
    if screenshot_dir:
        window.grabWindow().save(str(Path(screenshot_dir) / (profile + '.png')))
    QTest.keyClick(window, Qt.Key_Escape)
    tick()
    if screenshot_dir:
        window.grabWindow().save(str(Path(screenshot_dir) / (profile + '-panel.png')))
    invoke('mappingPresetsDialog', 'reject')
    click('manageMappingPresetsButton')
    assert combo.property('currentIndex') == combo.property('effectiveIndex') == applied_index
    # Both records can be removed; the applied values remain intact.
    for identity in (first, second):
        dialog.setProperty('selectedId', identity)
        tick()
        click('deleteMappingPresetButton')
        invoke('mappingPresetConfirmDialog', 'accept')
    assert len(controller.mappingPresets) == 1
    assert controller.mappingPresets[0]['automatic'] and controller.mappingPresets[0]['applied']
    assert not find('deleteMappingPresetButton').property('enabled')
    invoke('mappingPresetsDialog', 'reject')
    assert not dialog.property('visible')
    # A legacy mapping that needs correction must not hide valid named copies.
    store = mapping_presets.PresetStore(controller._config_root)
    compatible = store.create('兼容验证副本', profile, mapping_presets.snapshot(controller._bindings, profile))
    export = controller._config_root.parent / 'compatible.json'
    store.export_file(compatible, export)
    controller._bindings['bindings']['mic'] = {'kind': 'voice_toggle', 'keys': []}
    controller._removed_voice_bindings = config.normalize_voice_product_boundary(controller._config, controller._bindings)
    controller._load_bindings_into_model()
    controller._set_mapping_dirty(True)
    legacy_before = json.dumps(controller._bindings, sort_keys=True)
    click('manageMappingPresetsButton')
    assert dialog.property('visible') and combo.property('count') == 2
    assert combo.property('currentIndex') == combo.property('effectiveIndex') == 0
    assert dialog.property('message') == ''
    imported = controller.mappingPresetAction('import', '', QUrl.fromLocalFile(str(export)).toString())
    assert imported['ok'], imported
    dialog.setProperty('selectedId', imported['id'])
    tick()
    assert combo.property('count') == 3 and combo.property('currentIndex') == 2
    assert json.dumps(controller._bindings, sort_keys=True) == legacy_before
    invoke('mappingPresetsDialog', 'reject')
    assert not warnings, warnings
    print(json.dumps({'profile': profile, 'warnings': warnings, 'width': 640, 'height': 480}))
    window.hide()
    engine.deleteLater()
    app.processEvents()
finally:
    fixture.tearDown()
    fixture.doCleanups()
    m._shutdown_diagnostics_workers()
'''


@unittest.skipUnless(qt_tests._HAS_PYSIDE6, qt_tests._SKIP_REASON)
class MappingPresetsQmlTests(unittest.TestCase):
    def test_real_panel_flow_in_both_profiles_and_light_dark_scaled_windows(self):
        for profile, scale, dark in (("xiaomi-rc003", "1", "0"), ("chromecast-remote", "1.5", "1")):
            with self.subTest(profile=profile, scale=scale, dark=dark):
                env = dict(os.environ, QT_QPA_PLATFORM="offscreen", RC003_DISABLE_LIVE_INPUT="1",
                           PRESET_TEST_PROFILE=profile, PRESET_TEST_DARK=dark, QT_SCALE_FACTOR=scale)
                result = subprocess.run([sys.executable, "-c", PROBE], env=env,
                                        capture_output=True, text=True, encoding="utf-8", timeout=45)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(json.loads(result.stdout.strip().splitlines()[-1])["warnings"], [])
