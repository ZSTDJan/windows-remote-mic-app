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
    readonly property var deviceRows: SettingsController.remoteDeviceChoices
    property string pendingRemoteKey: ""
    readonly property int pendingRemoteIndex: indexForKey(pendingRemoteKey)
    readonly property var pendingChoice: {
        for (const row of deviceRows) {
            if (row.key === pendingRemoteKey)
                return row
        }
        return null
    }
    onPendingRemoteKeyChanged: syncSelection()

    function syncSelection() {
        devices.currentIndex = indexForKey(pendingRemoteKey)
    }

    function indexForKey(key) {
        const rows = deviceRows
        for (let i = 0; i < rows.length; i++) {
            if (rows[i].key === key)
                return i
        }
        return -1
    }
    onAboutToShow: {
        pendingRemoteKey = SettingsController.activeRemoteKey
        SettingsController.refreshRemoteDevices()
    }
    onAboutToHide: pendingRemoteKey = ""
    contentItem: ColumnLayout {
        spacing: root.tokens.spacingLarge
        RowLayout {
            Layout.fillWidth: true
            spacing: root.tokens.spacingSmall
            UiLabel { tokens: root.tokens; kind: bodyKind; text: qsTr("遥控器") }
            SelectionComboBox {
                id: devices
                objectName: "remoteDeviceCombo"
                tokens: root.tokens
                Layout.fillWidth: true
                Layout.minimumWidth: 0
                model: root.deviceRows
                textRole: "label"
                currentIndex: -1
                // ComboBox may reset to row zero when its model is replaced.
                // Restore the explicit draft after its native model update.
                onModelChanged: Qt.callLater(root.syncSelection)
                onCountChanged: Qt.callLater(root.syncSelection)
                effectiveIndex: root.indexForKey(SettingsController.activeRemoteKey)
                showEffectiveMarker: true
                displayText: root.pendingChoice
                    ? decoratedText(root.pendingRemoteIndex, root.pendingChoice.label)
                    : SettingsController.remoteDevicesRefreshing ? qsTr("正在读取设备…")
                    : count > 0 ? qsTr("请选择遥控器") : qsTr("没有可用的已配对遥控器")
                enabled: !SettingsController.remoteSelectionBusy && count > 0
                Accessible.name: qsTr("已配对的受支持遥控器")
                onActivated: index => root.pendingRemoteKey = model[index].key
            }
            CompactButton {
                objectName: "useRemoteButton"
                tokens: root.tokens
                text: qsTr("使用此设备")
                highlighted: true
                enabled: !SettingsController.remoteSelectionBusy
                    && !SettingsController.remoteDevicesRefreshing
                    && root.pendingRemoteKey.length > 0
                    && root.pendingChoice !== null && root.pendingChoice.canUse
                onClicked: SettingsController.useRemoteDevice(root.pendingRemoteKey)
            }
            CompactButton {
                objectName: "refreshRemotesButton"
                tokens: root.tokens
                text: qsTr("刷新")
                enabled: !SettingsController.remoteSelectionBusy && !SettingsController.remoteDevicesRefreshing
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
            objectName: "remoteDeviceHelpNote"
            tokens: root.tokens
            kind: noteKind
            Layout.fillWidth: true
            text: qsTr("切换会结束录音，各设备设置分别保留。")
            wrapMode: Text.WordWrap
        }
    }
}
