import QtQuick
import QtQuick.Controls

Item {
    id: root

    property var tokens
    property int level: -1
    readonly property bool available: level >= 0 && level <= 100
    readonly property color trackColor: tokens.borderStrong
    readonly property color progressColor: Qt.hsla(
        tokens.accent.hslHue,
        tokens.accent.hslSaturation * 0.35,
        tokens.disabledText.hslLightness,
        1)

    readonly property real sizeScale: 0.85
    implicitWidth: 28 * sizeScale
    implicitHeight: 28 * sizeScale
    activeFocusOnTab: false
    Accessible.role: Accessible.StaticText
    Accessible.name: available
        ? qsTr("小米遥控器2 Pro 电量 %1%").arg(level)
        : qsTr("小米遥控器2 Pro 电量不可用")

    Canvas {
        id: ring
        anchors.fill: parent
        antialiasing: true

        onPaint: {
            const ctx = getContext("2d")
            const strokeWidth = root.tokens.structuralDividerWidth * 2
            const radius = Math.min(width, height) / 2 - strokeWidth
            const centerX = width / 2
            const centerY = height / 2
            const startAngle = 2 * Math.PI / 3
            const totalAngle = 5 * Math.PI / 3

            ctx.clearRect(0, 0, width, height)
            ctx.lineWidth = strokeWidth
            ctx.lineCap = "round"
            ctx.beginPath()
            ctx.strokeStyle = root.trackColor
            ctx.arc(centerX, centerY, radius,
                    startAngle, startAngle + totalAngle, false)
            ctx.stroke()

            if (root.available && root.level > 0) {
                ctx.beginPath()
                ctx.strokeStyle = root.progressColor
                ctx.arc(centerX, centerY, radius, startAngle,
                        startAngle + totalAngle * root.level / 100, false)
                ctx.stroke()
            }
        }
    }

    UiLabel {
        anchors.fill: parent
        tokens: root.tokens
        text: root.available ? String(root.level) : "—"
        color: root.available ? root.tokens.textSecondary : root.tokens.disabledText
        font.pixelSize: root.tokens.fontSizeSmall * root.sizeScale
        font.weight: Font.Normal
        horizontalAlignment: Text.AlignHCenter
        verticalAlignment: Text.AlignVCenter
        lineHeight: 1
    }

    onLevelChanged: ring.requestPaint()
    onTrackColorChanged: ring.requestPaint()
    onProgressColorChanged: ring.requestPaint()
}
