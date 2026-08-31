import QtQuick
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
    property real editorColumnWidth: 0
    property bool editorColumnVisible: editorRow.children.length > 0
    property int stateColumnWidth: 0
    property int actionColumnWidth: 0
    property bool showDivider: true
    property alias editorData: editorRow.data
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
            id: titleLabel
            tokens: root.tokens
            kind: bodyKind
            Layout.preferredWidth: root.titleWidth
            Layout.minimumWidth: root.titleWidth
            Layout.maximumWidth: root.titleWidth
            text: root.titleText
            font.weight: Font.Medium
            elide: Text.ElideRight
            HoverHandler { id: titleHover }
            CompactToolTip {
                tokens: root.tokens
                active: titleHover.hovered && titleLabel.truncated
                text: root.titleText
            }
        }

        RowLayout {
            id: editorRow
            objectName: root.objectName.length > 0
                ? root.objectName + "_editorColumn" : ""
            visible: root.editorColumnVisible
            spacing: root.tokens.spacingSmall
            Layout.fillWidth: visible && root.editorColumnWidth <= 0
            Layout.preferredWidth: root.editorColumnWidth > 0
                ? root.editorColumnWidth : implicitWidth
            Layout.minimumWidth: root.editorColumnWidth > 0
                ? root.editorColumnWidth : 0
            Layout.maximumWidth: root.editorColumnWidth > 0
                ? root.editorColumnWidth : 16777215
        }

        UiLabel {
            id: descriptionLabel
            objectName: root.descriptionObjectName
            visible: root.descriptionText.length > 0
            tokens: root.tokens
            kind: noteKind
            Layout.fillWidth: true
            Layout.minimumWidth: 0
            text: root.descriptionText
            elide: Text.ElideRight
            HoverHandler { id: descriptionHover }
            CompactToolTip {
                tokens: root.tokens
                active: descriptionHover.hovered && descriptionLabel.truncated
                text: root.descriptionText
            }
        }

        Item {
            visible: !editorRow.visible && !descriptionLabel.visible
            Layout.fillWidth: true
        }

        Item {
            id: stateColumn
            objectName: root.objectName.length > 0
                ? root.objectName + "_stateColumn" : ""
            readonly property real columnWidth: root.stateColumnWidth > 0
                ? root.stateColumnWidth : stateLabel.implicitWidth
            visible: root.stateText.length > 0 || root.stateColumnWidth > 0
            implicitHeight: stateLabel.implicitHeight
            Layout.preferredWidth: columnWidth
            Layout.minimumWidth: columnWidth
            Layout.maximumWidth: columnWidth

            UiLabel {
                id: stateLabel
                anchors.fill: parent
                tokens: root.tokens
                kind: noteKind
                text: root.stateText
                color: root.stateColor
                font.weight: Font.Medium
                horizontalAlignment: root.stateColumnWidth > 0
                    ? Text.AlignRight : Text.AlignLeft
                verticalAlignment: Text.AlignVCenter
                elide: Text.ElideRight
            }
        }

        RowLayout {
            id: actionRow
            objectName: root.objectName.length > 0
                ? root.objectName + "_actionColumn" : ""
            readonly property real columnWidth: root.actionColumnWidth > 0
                ? root.actionColumnWidth : implicitWidth
            spacing: root.tokens.spacingSmall
            Layout.preferredWidth: columnWidth
            Layout.minimumWidth: columnWidth
            Layout.maximumWidth: columnWidth
            Layout.alignment: Qt.AlignRight | Qt.AlignVCenter
        }
    }

    Rectangle {
        visible: root.showDivider
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        height: root.tokens.hairlineWidth
        color: root.tokens.border
    }
}
