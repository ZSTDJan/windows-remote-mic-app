import QtQuick
import QtQuick.Controls

Label {
    id: root

    property var tokens
    property string glyph: ""
    property real glyphSize: 16
    readonly property string iconFontFamily:
        Qt.fontFamilies().indexOf("Segoe Fluent Icons") >= 0
            ? "Segoe Fluent Icons"
            : Qt.fontFamilies().indexOf("Segoe MDL2 Assets") >= 0
                ? "Segoe MDL2 Assets"
                : tokens.fontFamily

    text: glyph
    color: tokens.textSecondary
    font.family: iconFontFamily
    font.pixelSize: glyphSize
    renderType: Text.QtRendering
    horizontalAlignment: Text.AlignHCenter
    verticalAlignment: Text.AlignVCenter
}
