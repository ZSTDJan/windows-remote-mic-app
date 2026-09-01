import QtQuick
import QtQuick.Controls

Button {
    id: root

    property var tokens
    property int compactMinimumWidth: 0

    hoverEnabled: true
    implicitHeight: tokens ? tokens.buttonHeight : 30
    implicitWidth: Math.max(compactMinimumWidth, contentItem.implicitWidth + 14)
    leftPadding: 7
    rightPadding: 7
    font.family: tokens ? tokens.fontFamily : "Microsoft YaHei UI"
    font.pixelSize: tokens ? tokens.fontSizeControl : 12
    font.weight: Font.Normal

    contentItem: Label {
        text: root.text
        color: !root.enabled
            ? root.tokens.disabledText
            : root.highlighted
                ? root.tokens.accentText
                : root.tokens.textPrimary
        font: root.font
        horizontalAlignment: Text.AlignHCenter
        verticalAlignment: Text.AlignVCenter
        elide: Text.ElideRight
    }

    background: Rectangle {
        radius: root.tokens.cornerRadiusControl
        color: !root.enabled
            ? root.tokens.surfaceMuted
            : root.highlighted
                ? (root.down ? Qt.darker(root.tokens.accent, 1.12) : root.tokens.accent)
                : root.down
                    ? Qt.darker(root.tokens.buttonHover, 1.05)
                    : root.hovered
                        ? root.tokens.buttonHover
                        : root.tokens.buttonBackground
        border.width: root.highlighted ? 0 : root.tokens.hairlineWidth
        border.color: root.hovered ? root.tokens.borderStrong : root.tokens.border
        opacity: root.enabled ? 1 : 0.62
    }
}
