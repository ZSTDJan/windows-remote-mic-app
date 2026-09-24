import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from ovb_rc003 import voice_program_manager as programs
from ovb_rc003 import wetype_settings_hotkey_windows as reader


class Control:
    def __init__(self, name="", children=(), kind="GroupControl"):
        self.Name = name
        self.children = list(children)
        self.ControlTypeName = kind
        self.IsEnabled = True
        self.IsOffscreen = False

    def GetChildren(self):
        return self.children

def page(voice=True, shortcut="左 Ctrl 左 Win"):
    button = Control(kind="ButtonControl")
    navigation = [Control("输入"), Control("语音输入", [button])]
    fields = [
        Control("启动语音输入 按下可开启语音输入，按任意键均可结束"),
        Control(children=[Control("右 Alt")]),
        Control("按住说话 按住可语音输入，松手结束", kind="ImageControl"),
        Control(children=[Control(shortcut)]),
        Control("麦克风 设置语音输入的默认麦克风"),
    ] if voice else []
    return Control("设置", [Control("语音输入" if voice else "输入", navigation + [Control(children=fields)])]), button


class WeTypeSettingsReaderTests(unittest.TestCase):
    def setUp(self):
        self.target = programs.VoiceProgramSettingsTarget("wetype", "微信输入法", "executable", r"C:\WeType\wetype_update.exe", "-showsetting")
        self.resolve = mock.patch.object(programs, "resolve_voice_program_settings_target", return_value=self.target).start()
        self.open = mock.patch.object(programs, "open_voice_program_settings").start()
        mock.patch.object(reader.time, "sleep").start()
        self.addCleanup(mock.patch.stopall)
        self.access = mock.Mock()
        self.access.foreground.return_value = 22
        self.access.windows.return_value = (22,)
        self.access.root.return_value = page()[0]
        self.cancel = threading.Event()

    def read(self, **kwargs):
        return reader._read_with_access(self.access, self.cancel, **kwargs)

    def assert_error(self, code):
        with self.assertRaises(reader.SettingsReadError) as raised:
            self.read()
        self.assertEqual(raised.exception.code, code)

    def test_existing_voice_page_reads_hold_not_click_shortcut(self):
        self.assertEqual(self.read(), "左 Ctrl 左 Win")
        self.open.assert_not_called()
        self.assertEqual(self.access.root.call_count, 2)

    def test_updated_descriptions_read_each_modes_own_shortcut(self):
        from ovb_rc003.voice_hotkey_sync_windows import _parse_wetype_settings_shortcut

        root, _ = page(shortcut="左Ctrl 左Alt F8")
        fields = root.children[0].children[-1].children
        fields[0].Name = "启动语音输入 按下可开启语音输入，再次按快捷键或 Enter 结束"
        fields[1].children[0].Name = "左Shift 左Alt F8"
        self.access.root.return_value = root
        self.assertEqual(_parse_wetype_settings_shortcut(self.read(trigger="toggle")), "lshift+lalt+f8")
        self.assertEqual(_parse_wetype_settings_shortcut(self.read()), "lctrl+lalt+f8")
        self.open.assert_not_called()
        self.access.select_voice_page.assert_not_called()

    def test_titles_allow_description_changes_but_not_similar_names(self):
        for trigger, index, title in (("toggle", 0, "启动语音输入"), ("hold", 2, "按住说话")):
            for label, matches in ((title, True), (title + "\n新的说明", True),
                                   (title + "设置", False), ("说明 " + title, False)):
                with self.subTest(trigger=trigger, label=label):
                    root, _ = page()
                    fields = root.children[0].children[-1].children
                    fields[index].Name = label
                    value, _, _ = reader._snapshot(root, lambda: None, trigger=trigger)
                    self.assertEqual(bool(value), matches)

    def test_toggle_duplicate_rows_and_invalid_fields_are_rejected(self):
        for variant, code in (("duplicate", "ambiguous_ui"),
                              ("disabled", "unavailable_shortcut"),
                              ("wrong_type", "unavailable_shortcut"),
                              ("multiple_values", "ambiguous_shortcut")):
            with self.subTest(variant=variant):
                root, _ = page()
                fields = root.children[0].children[-1].children
                fields[0].Name = "启动语音输入 新的说明"
                if variant == "duplicate":
                    fields.extend(fields[:2])
                elif variant == "disabled":
                    fields[1].IsEnabled = False
                elif variant == "wrong_type":
                    fields[1].ControlTypeName = "ButtonControl"
                else:
                    fields[1].children.append(Control("F8"))
                with self.assertRaises(reader.SettingsReadError) as raised:
                    reader._snapshot(root, lambda: None, trigger="toggle")
                self.assertEqual(raised.exception.code, code)

    def test_closed_window_is_opened_once_then_voice_page_is_invoked_once(self):
        input_page, button = page(False)
        self.access.windows.side_effect = [(), (22,), (22,), (22,), (22,)]
        self.access.root.side_effect = [input_page, page()[0], page()[0]]
        self.assertEqual(self.read(), "左 Ctrl 左 Win")
        self.open.assert_called_once()
        self.access.select_voice_page.assert_called_once()
        self.assertEqual(self.access.select_voice_page.call_args.args[1].Name, "语音输入")

    def test_missing_entry_does_not_open(self):
        self.resolve.return_value = programs.VoiceProgramSettingsTarget("wetype", "微信输入法", "missing")
        self.assert_error("not_found")
        self.open.assert_not_called()

    def test_open_error_is_not_a_read_success(self):
        self.access.windows.return_value = ()
        self.open.side_effect = OSError("denied")
        self.assert_error("open_failed")

    def test_successful_launch_without_a_window_times_out(self):
        self.access.windows.return_value = ()
        with mock.patch.object(reader.time, "monotonic", side_effect=[0, 0, 0, 0, 1, 6]):
            self.assert_error("timeout")
        self.open.assert_called_once()

    def test_cancellation_before_start_does_not_open_or_read(self):
        self.cancel.set()
        self.assert_error("cancelled")
        self.resolve.assert_not_called()

    def test_focus_change_during_resolution_prevents_open(self):
        self.access.foreground.side_effect = [22, 22, 99]
        self.assert_error("focus_changed")
        self.open.assert_not_called()

    def test_closed_or_replaced_window_does_not_reopen(self):
        for matches in ((), (23,), (22, 23)):
            with self.subTest(matches=matches):
                self.access.windows.side_effect = [(22,), matches]
                self.assert_error("window_changed")
        self.open.assert_not_called()

    def test_multiple_settings_windows_fail_closed(self):
        self.access.windows.return_value = (22, 23)
        self.assert_error("ambiguous_window")

    def test_unavailable_navigation_never_falls_back_to_global_input(self):
        self.access.root.return_value = page(False)[0]
        self.access.select_voice_page.side_effect = reader.SettingsReadError("navigation_unavailable", "unavailable")
        self.assert_error("navigation_unavailable")

    def test_failed_navigation_does_not_retry(self):
        self.access.root.return_value = page(False)[0]
        self.access.select_voice_page.side_effect = reader.SettingsReadError("navigation_failed", "failed")
        self.assert_error("navigation_failed")
        self.access.select_voice_page.assert_called_once()

    def test_values_must_agree_on_consecutive_observations(self):
        self.access.root.side_effect = [page(shortcut="右 Alt")[0], page()[0], page()[0]]
        self.assertEqual(self.read(), "左 Ctrl 左 Win")
        self.assertEqual(self.access.root.call_count, 3)

    def test_old_voice_fields_on_input_page_are_not_accepted(self):
        root, _ = page()
        root.children[0].Name = "输入"
        value, _, voice_page = reader._snapshot(root, lambda: None)
        self.assertEqual(value, "")
        self.assertFalse(voice_page)

    def test_duplicate_hold_fields_are_rejected(self):
        root, _ = page()
        body = root.children[0].children[-1]
        body.children.extend(body.children[2:4])
        self.access.root.return_value = root
        self.assert_error("ambiguous_ui")


class WeTypeNavigationSafetyTests(unittest.TestCase):
    def setUp(self):
        self.access = reader._SettingsAccess.__new__(reader._SettingsAccess)
        self.access.user32 = mock.Mock()
        self.access.user32.GetAncestor.return_value = 22
        self.access.user32.GetForegroundWindow.return_value = 22
        self.access.user32.SendMessageTimeoutW.return_value = 1
        self.access.user32.ScreenToClient.return_value = True
        self.access.user32.ClientToScreen.return_value = True
        def bounds(_hwnd, pointer):
            pointer._obj.right = 800
            pointer._obj.bottom = 600
            return True
        self.access.user32.GetClientRect.side_effect = bounds
        self.nav = Control("语音输入")
        self.nav.NativeWindowHandle = 33
        self.nav.BoundingRectangle = SimpleNamespace(left=30, top=70, right=130, bottom=110)

    def test_only_target_view_receives_paired_navigation_messages(self):
        self.access.select_voice_page(22, self.nav, lambda: None)
        calls = self.access.user32.SendMessageTimeoutW.call_args_list
        self.assertEqual([(c.args[0], c.args[1], c.args[2], c.args[3]) for c in calls], [
            (33, 0x0201, 1, 80 | (90 << 16)), (33, 0x0202, 0, 80 | (90 << 16)),
        ])

    def test_wrong_owner_foreground_hidden_or_renamed_navigation_never_clicks(self):
        for problem in ("owner", "focus", "hidden", "renamed", "outside"):
            with self.subTest(problem=problem):
                self.setUp()
                if problem == "owner":
                    self.access.user32.GetAncestor.return_value = 99
                elif problem == "focus":
                    self.access.user32.GetForegroundWindow.return_value = 99
                elif problem == "hidden":
                    self.nav.IsOffscreen = True
                elif problem == "renamed":
                    self.nav.Name = "按住说话"
                else:
                    self.nav.BoundingRectangle.right = 4000
                with self.assertRaises(reader.SettingsReadError):
                    self.access.select_voice_page(22, self.nav, lambda: None)
                self.access.user32.SendMessageTimeoutW.assert_not_called()

    def test_failed_down_still_releases_only_the_same_target(self):
        self.access.user32.SendMessageTimeoutW.side_effect = [0, 1]
        with self.assertRaises(reader.SettingsReadError):
            self.access.select_voice_page(22, self.nav, lambda: None)
        self.assertEqual(self.access.user32.SendMessageTimeoutW.call_args_list[-1].args[:3], (33, 0x0202, 0))

    def test_moved_window_never_clicks_an_outdated_position(self):
        def moved(_hwnd, pointer):
            pointer._obj.x += 50
            return True
        self.access.user32.ClientToScreen.side_effect = moved
        with self.assertRaises(reader.SettingsReadError):
            self.access.select_voice_page(22, self.nav, lambda: None)
        self.access.user32.SendMessageTimeoutW.assert_not_called()

    def test_cancellation_after_slow_geometry_read_prevents_click(self):
        checks = mock.Mock(side_effect=[None, reader.SettingsReadError("cancelled", "cancelled")])
        with self.assertRaises(reader.SettingsReadError):
            self.access.select_voice_page(22, self.nav, checks)
        self.access.user32.SendMessageTimeoutW.assert_not_called()
