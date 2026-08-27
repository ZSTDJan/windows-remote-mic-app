import QtQuick
import QtQuick.Controls

ToolButton {
    id: root

    property var tokens
    signal closeRequested()

    implicitWidth: 28
    implicitHeight: 28
    hoverEnabled: true

    contentItem: IconGlyph {
        tokens: root.tokens
        glyph: "\uE711"
        glyphSize: 13
        color: root.hovered ? root.tokens.textPrimary : root.tokens.textSecondary
    }

    background: Rectangle {
        radius: root.tokens.cornerRadiusControl
        color: root.hovered ? root.tokens.surfaceMuted : "transparent"
    }

    onClicked: closeRequested()
    Accessible.name: qsTr("关闭")
}
