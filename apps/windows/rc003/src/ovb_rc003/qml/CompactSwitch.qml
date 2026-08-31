import QtQuick
import QtQuick.Controls

Switch {
    id: root

    property var tokens

    hoverEnabled: true
    implicitWidth: 28
    implicitHeight: tokens ? tokens.controlHeight : 28
    padding: 0
    spacing: 0

    indicator: Rectangle {
        objectName: root.objectName + "_track"
        implicitWidth: 28
        implicitHeight: 14
        width: implicitWidth
        height: implicitHeight
        x: 0
        y: (root.height - height) / 2
        radius: height / 2
        color: !root.enabled
            ? root.tokens.surfaceMuted
            : root.checked
                ? root.down
                    ? Qt.darker(root.tokens.accent, 1.08)
                    : root.hovered
                        ? Qt.lighter(root.tokens.accent, 1.06)
                        : root.tokens.accent
                : root.hovered
                    ? root.tokens.surfaceMuted
                    : root.tokens.fieldBackground
        border.width: root.tokens.hairlineWidth
        border.color: root.checked || root.activeFocus
            ? root.tokens.accent : root.tokens.borderStrong
        opacity: root.enabled ? 1 : 0.62

        Rectangle {
            objectName: root.objectName + "_thumb"
            width: 10
            height: 10
            x: root.checked ? parent.width - width - 2 : 2
            y: 2
            radius: width / 2
            color: root.checked
                ? root.tokens.accentText : root.tokens.textSecondary

            Behavior on x {
                NumberAnimation {
                    duration: 90
                    easing.type: Easing.OutCubic
                }
            }
        }

        Behavior on color {
            ColorAnimation { duration: 90 }
        }
    }
}
