import QtQuick
import QtQuick.Controls

ToolTip {
    id: root

    property var tokens
    property bool active: false
    property real maximumTextWidth: 260

    visible: active && text.length > 0
    delay: 450
    timeout: 5000
    x: 0
    y: -implicitHeight - root.tokens.spacingSmall - tooltipBackground.border.width
    leftInset: 0
    rightInset: 0
    topInset: 0
    bottomInset: 0
    leftMargin: tokens.spacingSmall
    rightMargin: tokens.spacingSmall
    topMargin: tokens.spacingSmall
    bottomMargin: tokens.spacingSmall
    leftPadding: 7
    rightPadding: 7
    topPadding: 4
    bottomPadding: 4

    TextMetrics {
        id: tipMetrics
        font.family: root.tokens.fontFamily
        font.pixelSize: root.tokens.fontSizeTiny
        text: root.text
    }

    contentItem: Text {
        text: root.text
        width: Math.min(tipMetrics.advanceWidth, root.maximumTextWidth)
        color: root.tokens.textSecondary
        font.family: root.tokens.fontFamily
        font.pixelSize: root.tokens.fontSizeTiny
        font.weight: Font.Normal
        wrapMode: Text.Wrap
        lineHeight: 1.15
        lineHeightMode: Text.ProportionalHeight
    }

    background: Rectangle {
        id: tooltipBackground
        objectName: root.objectName.length > 0 ? root.objectName + "_background" : ""
        radius: root.tokens.cornerRadiusControl
        color: root.tokens.surface
        border.width: root.tokens.hairlineWidth
        border.color: root.tokens.border
    }
}
