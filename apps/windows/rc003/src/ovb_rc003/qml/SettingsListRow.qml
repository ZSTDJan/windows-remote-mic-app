import QtQuick
import QtQuick.Layouts

Rectangle {
    id: root

    property var tokens
    property string iconGlyph: ""
    property string titleText: ""
    property string descriptionText: ""
    property string descriptionObjectName: ""
    property bool showDivider: true
    default property alias actionData: actionRow.data

    color: "transparent"
    Layout.fillWidth: true
    implicitHeight: Math.max(54, rowLayout.implicitHeight + 12)

    RowLayout {
        id: rowLayout
        anchors.fill: parent
        anchors.leftMargin: 9
        anchors.rightMargin: 9
        anchors.topMargin: 6
        anchors.bottomMargin: 6
        spacing: 9

        Rectangle {
            Layout.preferredWidth: 30
            Layout.preferredHeight: 30
            radius: 15
            color: root.tokens.accentSoft
            IconGlyph {
                anchors.centerIn: parent
                tokens: root.tokens
                glyph: root.iconGlyph
                glyphSize: 15
                color: root.tokens.accent
            }
        }

        ColumnLayout {
            Layout.fillWidth: true
            spacing: 1
            UiLabel {
                tokens: root.tokens
                kind: bodyKind
                Layout.fillWidth: true
                text: root.titleText
                font.pixelSize: root.tokens.fontSizeBody
                font.weight: Font.Medium
                elide: Text.ElideRight
            }
            UiLabel {
                objectName: root.descriptionObjectName
                tokens: root.tokens
                kind: noteKind
                Layout.fillWidth: true
                text: root.descriptionText
                maximumLineCount: 2
                elide: Text.ElideRight
                wrapMode: Text.WordWrap
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
