import QtQuick
import QtQuick.Controls

TextField {
    id: root

    property var tokens

    implicitHeight: tokens ? tokens.controlHeight : 26
    leftPadding: 7
    rightPadding: 7
    topPadding: 0
    bottomPadding: 0
    verticalAlignment: TextInput.AlignVCenter
    color: tokens.textPrimary
    placeholderTextColor: tokens.disabledText
    selectionColor: tokens.accent
    selectedTextColor: tokens.accentText
    font.family: tokens.fontFamily
    font.pixelSize: tokens.fontSizeControl

    background: Rectangle {
        radius: root.tokens.cornerRadiusControl
        color: root.tokens.fieldBackground
        border.width: root.tokens.hairlineWidth
        border.color: root.activeFocus ? root.tokens.accent : root.tokens.border
    }
}
