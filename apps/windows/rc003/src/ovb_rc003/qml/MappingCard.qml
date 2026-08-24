import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

AbstractButton {
    id: root

    property var tokens
    property string cardId: ""
    property string buttonName: ""
    property string singleText: ""
    property string doubleText: ""
    property string longText: ""
    property bool selected: false
    property bool voiceAction: false
    property bool exposeObjectNames: true

    hoverEnabled: true
    implicitHeight: 42
    padding: 3
    objectName: exposeObjectNames ? "editMapping_" + cardId : ""
    Accessible.name: buttonName + qsTr("按键映射")

    function shown(text, unavailable) {
        if (unavailable)
            return qsTr("不执行")
        return text && text.length > 0 ? text : qsTr("未设置")
    }

    component GestureDivider: Rectangle {
        Layout.preferredWidth: 1
        Layout.minimumWidth: 1
        Layout.maximumWidth: 1
        Layout.fillHeight: true
        color: root.tokens.border
    }

    background: Rectangle {
        radius: root.tokens.cornerRadiusSmall
        color: root.tokens.surface
        border.width: root.selected ? 2 : 1
        border.color: root.selected || root.hovered
            ? root.tokens.accent : root.tokens.cardBorder
    }

    contentItem: GridLayout {
        columns: 2
        columnSpacing: 0

        UiLabel {
            objectName: root.exposeObjectNames ? "mappingKeyCell_" + root.cardId : ""
            tokens: root.tokens
            kind: bodyKind
            Layout.preferredWidth: 32
            Layout.minimumWidth: 32
            Layout.maximumWidth: 32
            Layout.fillHeight: true
            text: root.buttonName
            Accessible.name: text
            font.pixelSize: root.tokens.fontSizeTiny
            font.weight: Font.Medium
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
            elide: Text.ElideRight
        }

        RowLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: 0

            GestureDivider { }

            Item {
                objectName: root.exposeObjectNames ? "mappingSingleCell_" + root.cardId : ""
                Layout.fillWidth: true
                Layout.fillHeight: true
                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: 2
                    spacing: 0
                    UiLabel { tokens: root.tokens; kind: noteKind; Layout.fillWidth: true; text: qsTr("单击"); font.pixelSize: root.tokens.fontSizeTiny; horizontalAlignment: Text.AlignHCenter }
                    UiLabel {
                    objectName: root.exposeObjectNames ? "mappingSinglePrimaryText_" + root.cardId : ""
                        tokens: root.tokens
                        kind: bodyKind
                        Layout.fillWidth: true
                        text: root.shown(root.singleText, false)
                        color: text === qsTr("未设置") ? root.tokens.disabledText : root.tokens.textPrimary
                        font.pixelSize: root.tokens.fontSizeTiny
                        font.weight: text === qsTr("未设置") ? Font.Normal : Font.Medium
                        horizontalAlignment: Text.AlignHCenter
                        elide: Text.ElideRight
                    }
                }
            }

            GestureDivider { }

            Item {
                objectName: root.exposeObjectNames ? "mappingDoubleCell_" + root.cardId : ""
                Layout.fillWidth: true
                Layout.fillHeight: true
                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: 2
                    spacing: 0
                    UiLabel { tokens: root.tokens; kind: noteKind; Layout.fillWidth: true; text: qsTr("双击"); font.pixelSize: root.tokens.fontSizeTiny; horizontalAlignment: Text.AlignHCenter }
                    UiLabel {
                    objectName: root.exposeObjectNames ? "mappingDoublePrimaryText_" + root.cardId : ""
                        tokens: root.tokens
                        kind: bodyKind
                        Layout.fillWidth: true
                        text: root.shown(root.doubleText, root.voiceAction)
                        color: root.voiceAction || text === qsTr("未设置") ? root.tokens.disabledText : root.tokens.textPrimary
                        font.pixelSize: root.tokens.fontSizeTiny
                        font.weight: root.voiceAction || text === qsTr("未设置") ? Font.Normal : Font.Medium
                        horizontalAlignment: Text.AlignHCenter
                        elide: Text.ElideRight
                    }
                }
            }

            GestureDivider { }

            Item {
                objectName: root.exposeObjectNames ? "mappingLongCell_" + root.cardId : ""
                Layout.fillWidth: true
                Layout.fillHeight: true
                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: 2
                    spacing: 0
                    UiLabel { tokens: root.tokens; kind: noteKind; Layout.fillWidth: true; text: qsTr("长按"); font.pixelSize: root.tokens.fontSizeTiny; horizontalAlignment: Text.AlignHCenter }
                    UiLabel {
                    objectName: root.exposeObjectNames ? "mappingLongPrimaryText_" + root.cardId : ""
                        tokens: root.tokens
                        kind: bodyKind
                        Layout.fillWidth: true
                        text: root.shown(root.longText, root.voiceAction)
                        color: root.voiceAction || text === qsTr("未设置") ? root.tokens.disabledText : root.tokens.textPrimary
                        font.pixelSize: root.tokens.fontSizeTiny
                        font.weight: root.voiceAction || text === qsTr("未设置") ? Font.Normal : Font.Medium
                        horizontalAlignment: Text.AlignHCenter
                        elide: Text.ElideRight
                    }
                }
            }
        }
    }
}
