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
    property string singleNoteText: ""
    property string doubleNoteText: ""
    property string longNoteText: ""
    property bool selected: false
    property bool voiceAction: false
    property bool exposeObjectNames: true

    hoverEnabled: true
    implicitHeight: 49
    leftPadding: 4
    rightPadding: 4
    topPadding: 3
    bottomPadding: 3
    objectName: exposeObjectNames ? "editMapping_" + cardId : ""
    Accessible.name: buttonName + qsTr("按键映射")

    function displayBinding(text) {
        const shortcuts = {
            "Escape": "Esc",
            "Return": "Enter",
            "回车": "Enter",
            "Delete": "Del",
            "Delete（退格）": "Del",
            "退格": "Del",
            "方向上": "↑",
            "方向下": "↓",
            "方向左": "←",
            "方向右": "→",
            "系统音量 +": "Vol +",
            "系统音量 −": "Vol -"
        }
        if (shortcuts[text])
            return shortcuts[text]
        return text ? text.replace(/\+/g, " + ") : ""
    }

    component GestureCell: Item {
        id: cell
        property string gestureLabel: ""
        property string bindingText: ""
        property string noteText: ""
        property string valueObjectName: ""
        readonly property bool empty:
            !bindingText || bindingText.trim().length === 0
            || bindingText.trim() === qsTr("未设置")
        readonly property bool usingNote:
            !empty && noteText && noteText.trim().length > 0
            && noteText.trim() !== qsTr("未命名")
        readonly property string shownText: empty
            ? qsTr("未设置")
            : usingNote
                ? noteText.trim() : root.displayBinding(bindingText.trim())

        Layout.fillWidth: true
        Layout.fillHeight: true
        implicitHeight: 30

        Rectangle {
            anchors.left: parent.left
            anchors.top: parent.top
            anchors.bottom: parent.bottom
            width: root.tokens.hairlineWidth
            color: root.tokens.border
        }

        ColumnLayout {
            anchors.fill: parent
            anchors.leftMargin: 3
            anchors.rightMargin: 3
            spacing: 0

            UiLabel {
                id: gestureTitle
                tokens: root.tokens
                kind: noteKind
                Layout.fillWidth: true
                Layout.preferredHeight: 13
                text: cell.gestureLabel
                color: root.tokens.textSecondary
                font.pixelSize: root.tokens.fontSizeMapGesture
                font.weight: Font.Normal
                horizontalAlignment: Text.AlignHCenter
                verticalAlignment: Text.AlignVCenter
                elide: Text.ElideRight
                HoverHandler { id: gestureHover }
                CompactToolTip {
                    tokens: root.tokens
                    active: gestureHover.hovered && !cell.empty
                        && (cell.usingNote || valueLabel.truncated)
                    text: cell.usingNote
                        ? cell.noteText.trim() + " · " + cell.bindingText.trim()
                        : cell.bindingText.trim()
                }
            }

            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: 22
                radius: 3
                color: !cell.usingNote && !cell.empty
                    ? root.tokens.surfaceMuted : "transparent"

                UiLabel {
                    id: valueLabel
                    objectName: cell.valueObjectName
                    anchors.fill: parent
                    anchors.leftMargin: 3
                    anchors.rightMargin: 3
                    tokens: root.tokens
                    kind: bodyKind
                    text: cell.shownText
                    color: cell.empty
                        ? root.tokens.disabledText
                        : root.tokens.textPrimary
                    font.family: cell.usingNote
                        ? root.tokens.fontFamily : root.tokens.fontFamilyMono
                    font.pixelSize: cell.usingNote
                        ? root.tokens.fontSizeMapPrimary
                        : root.tokens.fontSizeMapGesture
                    font.weight: cell.usingNote ? Font.Medium : Font.Normal
                    horizontalAlignment: Text.AlignHCenter
                    verticalAlignment: Text.AlignVCenter
                    elide: Text.ElideRight
                }
            }
        }
    }

    component VoicePausedCell: Item {
        Layout.fillWidth: true
        Layout.fillHeight: true
        Layout.columnSpan: 2
        implicitHeight: 30

        Rectangle {
            anchors.left: parent.left
            anchors.top: parent.top
            anchors.bottom: parent.bottom
            width: root.tokens.hairlineWidth
            color: root.tokens.border
        }

        ColumnLayout {
            anchors.fill: parent
            anchors.leftMargin: 3
            anchors.rightMargin: 3
            spacing: 0

            UiLabel {
                tokens: root.tokens
                kind: noteKind
                Layout.fillWidth: true
                Layout.preferredHeight: 13
                text: qsTr("双击 / 长按")
                color: root.tokens.disabledText
                font.pixelSize: root.tokens.fontSizeMapGesture
                horizontalAlignment: Text.AlignHCenter
                verticalAlignment: Text.AlignVCenter
                elide: Text.ElideRight
            }
            UiLabel {
                objectName: root.exposeObjectNames
                    ? "mappingVoicePausedText_" + root.cardId : ""
                tokens: root.tokens
                kind: noteKind
                Layout.fillWidth: true
                Layout.preferredHeight: 22
                text: qsTr("语音模式下暂停")
                color: root.tokens.disabledText
                font.pixelSize: root.tokens.fontSizeMapGesture
                font.weight: Font.Normal
                horizontalAlignment: Text.AlignHCenter
                verticalAlignment: Text.AlignVCenter
                elide: Text.ElideRight
            }
        }
    }

    background: Rectangle {
        radius: root.tokens.cornerRadiusSmall
        color: root.selected ? root.tokens.accentSoft : root.tokens.surface
        border.width: root.tokens.hairlineWidth
        border.color: root.selected
            ? root.tokens.accent
            : root.hovered
                ? root.tokens.borderStrong : root.tokens.cardBorder
    }

    contentItem: GridLayout {
        columns: 2
        columnSpacing: 4

        MappingKeyLabel {
            objectName: root.exposeObjectNames ? "mappingKeyCell_" + root.cardId : ""
            tokens: root.tokens
            Layout.preferredWidth: 32
            Layout.minimumWidth: 32
            Layout.maximumWidth: 32
            Layout.fillHeight: true
            text: root.buttonName
            Accessible.name: text
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
        }

        GridLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            columns: 3
            columnSpacing: 0
            rowSpacing: 0

            GestureCell {
                objectName: root.exposeObjectNames ? "mappingSingleCell_" + root.cardId : ""
                gestureLabel: qsTr("单击")
                bindingText: root.singleText
                noteText: root.singleNoteText
                valueObjectName: root.exposeObjectNames
                    ? "mappingSinglePrimaryText_" + root.cardId : ""
            }

            GestureCell {
                objectName: root.exposeObjectNames ? "mappingDoubleCell_" + root.cardId : ""
                visible: !root.voiceAction
                gestureLabel: qsTr("双击")
                bindingText: root.doubleText
                noteText: root.doubleNoteText
                valueObjectName: root.exposeObjectNames
                    ? "mappingDoublePrimaryText_" + root.cardId : ""
            }

            GestureCell {
                objectName: root.exposeObjectNames ? "mappingLongCell_" + root.cardId : ""
                visible: !root.voiceAction
                gestureLabel: qsTr("长按")
                bindingText: root.longText
                noteText: root.longNoteText
                valueObjectName: root.exposeObjectNames
                    ? "mappingLongPrimaryText_" + root.cardId : ""
            }

            VoicePausedCell {
                objectName: root.exposeObjectNames
                    ? "mappingVoicePausedCell_" + root.cardId : ""
                visible: root.voiceAction
            }
        }
    }
}
