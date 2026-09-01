import QtQuick
import QtQuick.Layouts

Rectangle {
    id: root

    property var tokens
    property color fillColor: tokens.surface
    property int horizontalPadding: tokens.spacingLarge
    property int verticalPadding: tokens.sectionVerticalPadding
    property int contentSpacing: tokens.spacingSmall
    default property alias contentData: sectionContent.data

    radius: tokens.cornerRadiusLarge
    color: fillColor
    border.color: tokens.border
    border.width: tokens.hairlineWidth
    implicitHeight: sectionContent.implicitHeight + verticalPadding * 2

    ColumnLayout {
        id: sectionContent
        anchors.fill: parent
        anchors.leftMargin: root.horizontalPadding
        anchors.rightMargin: root.horizontalPadding
        anchors.topMargin: root.verticalPadding
        anchors.bottomMargin: root.verticalPadding
        spacing: root.contentSpacing
    }
}
