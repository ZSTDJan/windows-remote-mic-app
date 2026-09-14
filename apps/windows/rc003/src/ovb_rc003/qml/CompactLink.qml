import QtQuick
import QtQuick.Controls

CompactButton {
    id: root

    flat: true
    implicitWidth: contentItem.implicitWidth
    leftPadding: 0
    rightPadding: 0
    font.pixelSize: tokens.fontSizeSmall
    font.underline: true
    background: Item {}

    contentItem: Label {
        text: root.text
        font: root.font
        color: root.tokens.disabledText
        horizontalAlignment: Text.AlignHCenter
        verticalAlignment: Text.AlignVCenter
    }

    HoverHandler {
        cursorShape: root.enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
    }
}
