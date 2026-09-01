import QtQuick

QtObject {
    id: tokens

    property SystemPalette palette: SystemPalette {
        colorGroup: SystemPalette.Active
    }
    readonly property bool darkMode:
        palette.window.r * 0.299
        + palette.window.g * 0.587
        + palette.window.b * 0.114 < 0.5

    property color background: darkMode ? "#171a1f" : "#f5f6f8"
    property color windowFrame: darkMode ? "#101318" : "#eef0f2"
    property color nativeWindowBorder: darkMode ? "#4a5059" : "#a8adb4"
    property color windowFrameBorder: darkMode ? "#343941" : "#dfe1e4"
    property color surface: darkMode ? "#20242b" : "#ffffff"
    property color surfaceMuted: darkMode ? "#282d35" : "#f1f3f5"
    property color sidebar: darkMode ? "#1b1e24" : "#f5f6f8"
    property color textPrimary: darkMode ? "#f3f4f6" : "#17191d"
    property color textSecondary: darkMode ? "#aeb4be" : "#5f6670"
    property color disabledText: darkMode ? "#888f99" : "#858c95"
    property color border: darkMode ? "#343941" : "#e2e4e7"
    property color borderStrong: darkMode ? "#474d57" : "#cfd4da"
    property color cardBorder: border
    property color accent: "#087cf0"
    property color accentText: "#ffffff"
    property color accentSoft: darkMode ? "#183653" : "#e7f2ff"
    property color fieldBackground: darkMode ? "#252a31" : "#f7f8f9"
    property color buttonBackground: surfaceMuted
    property color buttonHover: darkMode ? "#303640" : "#e9ecef"
    property color buttonText: textPrimary
    property color voiceAccent: darkMode ? "#efb66b" : "#aa6410"
    property color successColor: darkMode ? "#64d78d" : "#119c4c"
    property color errorColor: darkMode ? "#ff6764" : "#c42b1c"
    property color statusBackground: accentSoft
    property color errorBackground: darkMode ? "#422421" : "#fdeceb"

    property string fontFamily: "Microsoft YaHei UI"
    property string fontFamilyMono: "Consolas"
    property real fontSizeTiny: 9.5
    property real fontSizeMapGesture: 9.5
    property real fontSizeMapPrimary: 10.5
    property real fontSizeSmall: 10.5
    property real fontSizeMappingKey: fontSizeSmall
    property real fontSizeControl: 11
    property int fontSizeBody: 12
    property int fontSizeSection: 14
    property int fontSizeTitle: 15

    property int cornerRadiusControl: 5
    property int cornerRadiusSmall: 7
    property int cornerRadiusLarge: 8
    property int windowClientRadius: 9
    property int windowFrameGap: 3
    property real hairlineWidth: 0.5
    property int structuralDividerWidth: 1
    property int spacingTiny: 3
    property int spacingSmall: 6
    property int spacingMedium: 8
    property int spacingLarge: 10

    property int navigationWidth: 48
    property int navigationItemHeight: 46
    property int controlHeight: 28
    property int buttonHeight: controlHeight
    property int buttonWidth2Chars: 48
    property int buttonWidth4Chars: 72
    property int buttonWidth6Chars: 92
    property int buttonWidth9Chars: 120
    property int statusBarMinHeight: 20
    property int pageMaxWidth: 920
    property int pageHorizontalPadding: 10
    property int pageVerticalPadding: 10
    property int sectionVerticalPadding: 10

    property int comboMappingKeyColumnWidth: 92
    property int comboMappingNoteColumnWidth: 154
    property int comboMappingHeaderHeight: 24
    property int comboMappingRowHeight: 32
    property int comboMappingRowSpacing: 2
    property int comboMappingHorizontalPadding: spacingMedium
    property int comboMappingColumnSpacing: spacingSmall
}
