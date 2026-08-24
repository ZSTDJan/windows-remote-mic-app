import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import OvbRc003Settings 1.0

Item {
    id: root
    property var tokens
    signal openMappingRequested()
    signal openDiagnosticsRequested()

    function ensureVisible(item) {
        if (!item)
            return
        Qt.callLater(function() {
            const flickable = permissionsScroll.contentItem
            if (!flickable || flickable.contentHeight <= permissionsScroll.availableHeight)
                return
            const position = item.mapToItem(pageColumn, 0, 0)
            flickable.contentY = Math.max(0, Math.min(
                flickable.contentHeight - permissionsScroll.availableHeight,
                position.y - tokens.spacingMedium
            ))
        })
    }

    ScrollView {
        id: permissionsScroll
        objectName: "permissionsScroll"
        anchors.fill: parent
        clip: true
        contentWidth: availableWidth
        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff

        ColumnLayout {
            id: pageColumn
            objectName: "permissionsPageContent"
            width: Math.max(0, permissionsScroll.availableWidth - tokens.pageHorizontalPadding * 2)
            x: tokens.pageHorizontalPadding
            y: tokens.pageVerticalPadding
            spacing: tokens.spacingLarge

            ColumnLayout {
                Layout.fillWidth: true
                spacing: 5
                UiLabel { tokens: root.tokens; kind: sectionTitleKind; text: qsTr("运行必需") }
                SectionFrame {
                    id: requiredPermissionsSection
                    objectName: "requiredPermissionsSection"
                    tokens: root.tokens
                    Layout.fillWidth: true
                    horizontalPadding: 0
                    verticalPadding: 0
                    contentSpacing: 0

                    SettingsListRow {
                        objectName: "bluetoothPermissionBlock"
                        visible: SettingsController.isRc003Device
                        tokens: root.tokens
                        iconGlyph: "\uE702"
                        titleText: qsTr("蓝牙配对")
                        descriptionText: qsTr("先在系统中配对遥控器，再连接本程序。")
                        showDivider: true
                        CompactButton {
                            objectName: "openBluetoothSettingsButton"
                            tokens: root.tokens
                            compactMinimumWidth: 64
                            text: qsTr("蓝牙设置")
                            onClicked: SettingsController.openBluetoothSettings()
                            onActiveFocusChanged: if (activeFocus) root.ensureVisible(this)
                        }
                    }

                    SettingsListRow {
                        id: microphonePermissionBlock
                        objectName: "microphonePermissionBlock"
                        tokens: root.tokens
                        iconGlyph: "\uE720"
                        titleText: qsTr("目标应用麦克风访问")
                        descriptionObjectName: "microphonePermissionDescription"
                        descriptionText: SettingsController.isRc003Device
                            ? qsTr("允许目标应用录音；虚拟音频请选择 CABLE Output。按键不受影响。")
                            : qsTr("允许目标应用使用 DJI Mic 2 录音。")
                        showDivider: false
                        CompactButton {
                            objectName: "openMicrophonePrivacyButton"
                            tokens: root.tokens
                            compactMinimumWidth: 64
                            text: qsTr("麦克风隐私")
                            onClicked: SettingsController.openMicrophonePrivacySettings()
                            onActiveFocusChanged: if (activeFocus) root.ensureVisible(this)
                        }
                        CompactButton {
                            objectName: "openSoundInputSettingsButton"
                            tokens: root.tokens
                            compactMinimumWidth: 64
                            text: qsTr("声音输入")
                            onClicked: SettingsController.openSoundSettings()
                            onActiveFocusChanged: if (activeFocus) root.ensureVisible(this)
                        }
                    }
                }
            }

            ColumnLayout {
                id: optionalEnhancementsSection
                objectName: "optionalEnhancementsSection"
                visible: SettingsController.isRc003Device
                Layout.fillWidth: true
                spacing: 5
                UiLabel { tokens: root.tokens; kind: sectionTitleKind; text: qsTr("按需使用") }
                SectionFrame {
                    tokens: root.tokens
                    Layout.fillWidth: true
                    horizontalPadding: 0
                    verticalPadding: 0
                    contentSpacing: 0

                    SettingsListRow {
                        tokens: root.tokens
                        iconGlyph: "\uE765"
                        titleText: qsTr("特殊按键支持（HID tap）")
                        descriptionText: qsTr("只有部分按键无响应时才启用；需要管理员权限。")
                        CompactButton {
                            objectName: "openDiagnosticsButton"
                            tokens: root.tokens
                            compactMinimumWidth: 64
                            text: qsTr("前往检查")
                            onClicked: root.openDiagnosticsRequested()
                            onActiveFocusChanged: if (activeFocus) root.ensureVisible(this)
                        }
                    }
                    SettingsListRow {
                        tokens: root.tokens
                        iconGlyph: "\uE95E"
                        titleText: qsTr("虚拟音频（VB-CABLE）")
                        descriptionText: qsTr("用于传送语音；安装需管理员权限和重启，不会改默认设备。")
                        showDivider: false
                        CompactButton {
                            objectName: "openVirtualAudioDiagnosticsButton"
                            tokens: root.tokens
                            compactMinimumWidth: 64
                            text: qsTr("前往检查")
                            onClicked: root.openDiagnosticsRequested()
                            onActiveFocusChanged: if (activeFocus) root.ensureVisible(this)
                        }
                    }
                }
            }

            ColumnLayout {
                id: manualSetupSection
                objectName: "manualSetupSection"
                visible: SettingsController.isRc003Device
                Layout.fillWidth: true
                spacing: 5
                UiLabel { tokens: root.tokens; kind: sectionTitleKind; text: qsTr("相关设置") }
                SectionFrame {
                    tokens: root.tokens
                    Layout.fillWidth: true
                    horizontalPadding: 0
                    verticalPadding: 0
                    contentSpacing: 0

                    SettingsListRow {
                        id: hostVoiceSetupBlock
                        objectName: "hostVoiceSetupBlock"
                        tokens: root.tokens
                        iconGlyph: "\uE713"
                        titleText: qsTr("输入法与应用")
                        descriptionText: qsTr("快捷键要与“按键”页一致，目标应用要选对麦克风。")
                        showDivider: false
                        CompactButton {
                            objectName: "openInputAppSettingsButton"
                            tokens: root.tokens
                            compactMinimumWidth: 64
                            text: qsTr("应用设置")
                            onClicked: SettingsController.openAppsSettings()
                        }
                        CompactButton {
                            objectName: "openMappingButton"
                            tokens: root.tokens
                            compactMinimumWidth: 64
                            text: qsTr("前往按键")
                            onClicked: root.openMappingRequested()
                            onActiveFocusChanged: if (activeFocus) root.ensureVisible(this)
                        }
                    }
                }
            }

            Item { Layout.preferredHeight: tokens.pageVerticalPadding }
        }
    }
}
