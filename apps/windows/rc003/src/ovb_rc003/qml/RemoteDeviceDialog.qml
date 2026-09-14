import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import OvbRc003Settings 1.0

SettingsDialog {
    id: root
    objectName: "remoteDeviceDialog"
    title: qsTr("选择设备")
    titleNote: qsTr("当前使用：") + SettingsController.activeRemoteLabel
    dismissalEnabled: !SettingsController.remoteSelectionBusy
    closeButtonObjectName: "closeRemoteDialogButton"
    preferredWidth: 520
    onOpened: {
        const rows = SettingsController.registeredRemotes
        let activeIndex = 0
        for (let i = 0; i < rows.length; i++) {
            if (rows[i].key === SettingsController.activeRemoteKey)
                activeIndex = i
        }
        registered.currentIndex = rows.length ? activeIndex : -1
        SettingsController.refreshRemoteDevices()
    }
    contentItem: ColumnLayout {
        spacing: root.tokens.spacingLarge
        GridLayout {
            Layout.fillWidth: true
            columns: 4
            columnSpacing: root.tokens.spacingSmall
            rowSpacing: root.tokens.spacingLarge
            UiLabel { tokens: root.tokens; kind: bodyKind; text: qsTr("已添加设备") }
            SelectionComboBox {
                id: registered
                objectName: "registeredRemoteCombo"
                tokens: root.tokens
                Layout.fillWidth: true
                model: SettingsController.registeredRemotes
                textRole: "label"
                displayText: count > 0 ? currentText : qsTr("请先在下方添加设备")
                enabled: !SettingsController.remoteSelectionBusy && count > 0
                Accessible.name: qsTr("已添加的遥控器")
            }
            CompactButton {
                objectName: "useRemoteButton"
                tokens: root.tokens
                text: qsTr("使用此设备")
                highlighted: true
                enabled: !SettingsController.remoteSelectionBusy && registered.currentIndex >= 0
                onClicked: SettingsController.useRemoteDevice(registered.model[registered.currentIndex].key)
            }
            CompactButton {
                objectName: "removeRemoteButton"
                tokens: root.tokens
                text: qsTr("移除设备")
                enabled: !SettingsController.remoteSelectionBusy && registered.currentIndex >= 0
                onClicked: SettingsController.removeRemoteDevice(registered.model[registered.currentIndex].key)
            }
            UiLabel {
                tokens: root.tokens
                kind: bodyKind
                text: qsTr("已配对设备")
                HoverHandler { id: pairedLabelHover }
                CompactToolTip {
                    tokens: root.tokens
                    active: pairedLabelHover.hovered
                    text: qsTr("Windows 已配对、尚未添加的设备")
                }
            }
            SelectionComboBox {
                id: available
                objectName: "availableRemoteCombo"
                tokens: root.tokens
                Layout.fillWidth: true
                model: SettingsController.availableRemotes
                textRole: "label"
                displayText: count > 0 ? currentText : qsTr("没有可添加的已配对设备")
                enabled: !SettingsController.remoteSelectionBusy && count > 0
                Accessible.name: qsTr("可添加的已配对设备")
            }
            CompactButton {
                objectName: "addRemoteButton"
                tokens: root.tokens
                text: qsTr("添加设备")
                enabled: !SettingsController.remoteSelectionBusy && available.currentIndex >= 0
                    && available.model[available.currentIndex].profile === "xiaomi-rc003"
                onClicked: SettingsController.addRemoteDevice(available.model[available.currentIndex].key)
            }
            CompactButton {
                objectName: "refreshRemotesButton"
                tokens: root.tokens
                text: qsTr("重新读取")
                enabled: !SettingsController.remoteSelectionBusy
                onClicked: SettingsController.refreshRemoteDevices()
            }
        }
        UiLabel {
            objectName: "remoteSelectionMessage"
            tokens: root.tokens
            kind: noteKind
            Layout.fillWidth: true
            text: SettingsController.remoteSelectionMessage
            visible: text.length > 0
            wrapMode: Text.WordWrap
        }
        UiLabel {
            tokens: root.tokens
            kind: noteKind
            Layout.fillWidth: true
            text: qsTr("切换会结束当前录音。移除当前设备会停止服务，保留 Windows 蓝牙配对。")
            wrapMode: Text.WordWrap
        }
    }
}
