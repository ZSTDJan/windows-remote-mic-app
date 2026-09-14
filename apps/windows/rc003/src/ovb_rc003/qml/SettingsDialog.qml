import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Dialog {
    id: root
    property var tokens
    property real preferredWidth: 480
    property string titleNote: ""
    property string closeButtonObjectName: ""
    property bool dismissalEnabled: true

    parent: Overlay.overlay
    modal: true
    popupType: Popup.Item
    anchors.centerIn: parent
    width: Math.min(preferredWidth, parent.width - 32)
    padding: 16
    topPadding: 0
    leftInset: 0
    rightInset: 0
    topInset: 0
    bottomInset: 0
    standardButtons: Dialog.NoButton
    closePolicy: dismissalEnabled ? Popup.CloseOnEscape : Popup.NoAutoClose

    background: Rectangle {
        radius: root.tokens.cornerRadiusLarge
        color: root.tokens.surface
        border.width: root.tokens.hairlineWidth
        border.color: root.tokens.border
    }

    header: Item {
        implicitHeight: 42
        RowLayout {
            anchors.fill: parent
            anchors.leftMargin: root.leftPadding
            anchors.rightMargin: root.tokens.spacingMedium
            spacing: root.tokens.spacingLarge
            UiLabel {
                tokens: root.tokens
                kind: sectionTitleKind
                text: root.title
                font.pixelSize: root.tokens.fontSizeTitle
                font.weight: Font.Medium
            }
            UiLabel {
                id: note
                tokens: root.tokens
                kind: noteKind
                Layout.fillWidth: true
                Layout.minimumWidth: 0
                text: root.titleNote
                textFormat: Text.PlainText
                elide: Text.ElideRight
                HoverHandler { id: noteHover }
                CompactToolTip {
                    tokens: root.tokens
                    active: noteHover.hovered && note.truncated
                    text: root.titleNote
                }
            }
            DialogCloseButton {
                objectName: root.closeButtonObjectName
                tokens: root.tokens
                enabled: root.dismissalEnabled
                onCloseRequested: root.reject()
            }
        }
    }

    footer: DialogButtonBox {
        id: buttons
        visible: root.standardButtons !== Dialog.NoButton
        standardButtons: root.standardButtons
        alignment: Qt.AlignRight
        spacing: root.tokens.spacingSmall
        leftPadding: root.leftPadding
        rightPadding: root.rightPadding
        topPadding: root.tokens.spacingLarge
        bottomPadding: root.bottomPadding
        background: Item {}
        function translateButtons() {
            const ok = standardButton(Dialog.Ok)
            const cancel = standardButton(Dialog.Cancel)
            if (ok) ok.text = qsTr("确定")
            if (cancel) cancel.text = qsTr("取消")
        }
        Component.onCompleted: translateButtons()
        onStandardButtonsChanged: Qt.callLater(translateButtons)
        delegate: CompactButton {
            tokens: root.tokens
            compactMinimumWidth: root.tokens.buttonWidth4Chars
        }
    }
}
