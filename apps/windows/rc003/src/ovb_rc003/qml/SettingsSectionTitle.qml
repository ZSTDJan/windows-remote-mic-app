import QtQuick
import QtQuick.Layouts

UiLabel {
    id: root

    kind: bodyKind
    font.weight: Font.Bold
    Layout.fillWidth: true
    Layout.leftMargin: tokens.spacingLarge
    Layout.topMargin: tokens.spacingSmall
    Layout.bottomMargin: tokens.spacingSmall
}
