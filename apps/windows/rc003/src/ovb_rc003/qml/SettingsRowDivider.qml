import QtQuick

Rectangle {
    property var tokens
    anchors.left: parent.left
    anchors.right: parent.right
    anchors.bottom: parent.bottom
    height: tokens.hairlineWidth
    color: tokens.border
}
