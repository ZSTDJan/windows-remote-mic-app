import QtQuick
import QtQuick.Controls

Label {
    id: root

    property var tokens
    property string glyph: ""
    property real glyphSize: 16

    text: glyph
    color: tokens.textSecondary
    font.family: "Segoe Fluent Icons"
    font.pixelSize: glyphSize
    horizontalAlignment: Text.AlignHCenter
    verticalAlignment: Text.AlignVCenter
}
