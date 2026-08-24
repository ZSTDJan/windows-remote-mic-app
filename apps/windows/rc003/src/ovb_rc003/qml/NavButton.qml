import QtQuick
import QtQuick.Controls

AbstractButton {
    id: root

    property var tokens
    property string glyph: ""

    checkable: true
    hoverEnabled: true
    implicitWidth: 40
    implicitHeight: tokens.navigationItemHeight

    contentItem: Column {
        spacing: 2
        IconGlyph {
            tokens: root.tokens
            glyph: root.glyph
            glyphSize: 16
            width: parent.width
            height: 19
            color: root.checked ? root.tokens.accent : root.tokens.disabledText
        }
        Label {
            width: parent.width
            text: root.text
            color: root.checked ? root.tokens.accent : root.tokens.disabledText
            font.pixelSize: 10
            font.weight: root.checked ? Font.Medium : Font.Normal
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
        }
    }

    background: Rectangle {
        radius: root.tokens.cornerRadiusSmall
        color: root.checked
            ? root.tokens.accentSoft
            : root.hovered
                ? root.tokens.surfaceMuted
                : "transparent"
    }
}
