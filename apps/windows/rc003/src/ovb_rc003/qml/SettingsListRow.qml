import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: root

    property var tokens
    property string iconGlyph: ""
    property string titleText: ""
    property string descriptionText: ""
    property string descriptionObjectName: ""
    property bool inlineDescription: false
    property bool showDivider: true
    default property alias actionData: actionRow.data

    color: "transparent"
    Layout.fillWidth: true
    implicitHeight: inlineDescription
        ? rowLayout.implicitHeight + 10
        : Math.max(50, rowLayout.implicitHeight + 10)

    RowLayout {
        id: rowLayout
        anchors.fill: parent
        anchors.leftMargin: 9
        anchors.rightMargin: 9
        anchors.topMargin: 5
        anchors.bottomMargin: 5
        spacing: 9

        Rectangle {
            Layout.preferredWidth: 28
            Layout.preferredHeight: 28
            radius: 14
            color: root.tokens.accentSoft
            IconGlyph {
                anchors.centerIn: parent
                tokens: root.tokens
                glyph: root.iconGlyph
                glyphSize: 15
                color: root.tokens.accent
            }
        }

        GridLayout {
            Layout.fillWidth: true
            columns: root.inlineDescription ? 2 : 1
            columnSpacing: root.tokens.spacingSmall
            rowSpacing: 1
            UiLabel {
                tokens: root.tokens
                kind: bodyKind
                Layout.row: 0
                Layout.column: 0
                Layout.fillWidth: !root.inlineDescription
                Layout.alignment: Qt.AlignVCenter
                text: root.titleText
                font.pixelSize: root.tokens.fontSizeBody
                font.weight: Font.Medium
                elide: Text.ElideRight
            }
            UiLabel {
                objectName: root.descriptionObjectName
                tokens: root.tokens
                kind: noteKind
                Layout.row: root.inlineDescription ? 0 : 1
                Layout.column: root.inlineDescription ? 1 : 0
                Layout.fillWidth: true
                Layout.alignment: Qt.AlignVCenter
                text: root.descriptionText
                maximumLineCount: 1
                elide: Text.ElideRight
                wrapMode: Text.NoWrap
                HoverHandler { id: descriptionHover }
                ToolTip.visible: descriptionHover.hovered
                ToolTip.text: root.descriptionText
            }
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
