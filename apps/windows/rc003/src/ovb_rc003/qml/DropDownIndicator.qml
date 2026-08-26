import QtQuick

Canvas {
    id: root

    property color indicatorColor: "#5f6670"

    implicitWidth: 10
    implicitHeight: 6

    onPaint: {
        const ctx = getContext("2d")
        ctx.reset()
        ctx.beginPath()
        ctx.moveTo(1, 1)
        ctx.lineTo(width - 1, 1)
        ctx.lineTo(width / 2, height - 1)
        ctx.closePath()
        ctx.fillStyle = indicatorColor
        ctx.fill()
    }

    onIndicatorColorChanged: requestPaint()
}
