from pathlib import Path
import unittest


QML_DIR = Path(__file__).resolve().parents[1] / "src" / "ovb_rc003" / "qml"


class SystemFontPackagingTests(unittest.TestCase):
    def test_settings_use_windows_system_font_without_bundled_font_assets(self):
        qml_text = "\n".join(
            path.read_text(encoding="utf-8") for path in QML_DIR.glob("*.qml")
        )

        self.assertIn(
            'property string fontFamily: "Microsoft YaHei UI"',
            (QML_DIR / "Tokens.qml").read_text(encoding="utf-8"),
        )
        self.assertNotIn("Noto Sans", qml_text)
        self.assertNotIn("NotoSans", qml_text)
        main_qml = (QML_DIR / "main.qml").read_text(encoding="utf-8")
        self.assertNotIn("FontLoader", main_qml)
        self.assertFalse((QML_DIR / "fonts").exists())


if __name__ == "__main__":
    unittest.main()
