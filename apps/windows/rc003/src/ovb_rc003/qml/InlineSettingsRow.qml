import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: root

    property var tokens
    property string titleText: ""
    property string descriptionText: ""
    property string descriptionObjectName: ""
    property string stateText: ""
    property color stateColor: tokens.textSecondary
    property int titleWidth: 72
    property bool showDivider: true
    default property alias actionData: actionRow.data

    color: "transparent"
    Layout.fillWidth: true
    implicitHeight: 42

    RowLayout {
        anchors.fill: parent
        anchors.leftMargin: 10
        anchors.rightMargin: 10
        anchors.topMargin: 5
        anchors.bottomMargin: 5
        spacing: root.tokens.spacingSmall

        UiLabel {
            tokens: root.tokens
            kind: bodyKind
            Layout.preferredWidth: root.titleWidth
            Layout.minimumWidth: root.titleWidth
            Layout.maximumWidth: root.titleWidth
            text: root.titleText
            font.weight: Font.Medium
            elide: Text.ElideRight
        }

        UiLabel {
            id: descriptionLabel
            objectName: root.descriptionObjectName
            tokens: root.tokens
            kind: noteKind
            Layout.fillWidth: true
            text: root.descriptionText
            elide: Text.ElideRight
            HoverHandler { id: descriptionHover }
            ToolTip.visible: descriptionHover.hovered
                && root.descriptionText.length > 0
            ToolTip.text: root.descriptionText
        }

        UiLabel {
            visible: root.stateText.length > 0
            tokens: root.tokens
            kind: noteKind
            text: root.stateText
            color: root.stateColor
            font.weight: Font.Medium
        }

        RowLayout {
            id: actionRow
            spacing: root.tokens.spacingSmall
            Layout.preferredWidth: implicitWidth
            Layout.alignment: Qt.AlignRight | Qt.AlignVCenter
        }
    }

    Rectangle {
        visible: root.showDivider
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        height: 1
        color: root.tokens.border
    }
}
