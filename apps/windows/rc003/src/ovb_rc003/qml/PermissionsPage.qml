// Windows surfaces involved in RC003 use. The page never fabricates a
// single "authorized" state: Windows exposes these settings separately, and
// third-party input methods own their own recording permission/input choice.
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
        if (!item) {
            return
        }
        Qt.callLater(function() {
            const flickable = permissionsScroll.contentItem
            if (!flickable || flickable.contentHeight <= permissionsScroll.availableHeight) {
                return
            }
            const position = item.mapToItem(pageColumn, 0, 0)
            const top = pageColumn.y + position.y - tokens.spacingMedium
            const bottom = pageColumn.y + position.y + item.height + tokens.spacingMedium
            const viewportTop = flickable.contentY
            const viewportBottom = viewportTop + permissionsScroll.availableHeight
            const maximumY = Math.max(0, flickable.contentHeight - permissionsScroll.availableHeight)
            if (top < viewportTop) {
                flickable.contentY = Math.max(0, top)
            } else if (bottom > viewportBottom) {
                flickable.contentY = Math.min(maximumY, bottom - permissionsScroll.availableHeight)
            }
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
            width: Math.max(0, Math.min(
                root.width - tokens.pageHorizontalPadding * 2,
                tokens.pageMaxWidth
            ))
            x: Math.max(tokens.pageHorizontalPadding, (root.width - width) / 2)
            y: tokens.spacingLarge
            spacing: tokens.spacingLarge

            Label {
                text: qsTr("权限与系统设置")
                color: tokens.textPrimary
                font.pixelSize: tokens.fontSizeTitle
                font.bold: true
            }

            // -- Required ----------------------------------------------------
            ColumnLayout {
                id: requiredSection
                objectName: "requiredPermissionsSection"
                Layout.fillWidth: true
                spacing: tokens.spacingMedium

                Label {
                    text: qsTr("运行必需")
                    color: tokens.textPrimary
                    font.pixelSize: tokens.fontSizeBody
                    font.bold: true
                }

                ColumnLayout {
                    id: bluetoothPermissionBlock
                    objectName: "bluetoothPermissionBlock"
                    visible: SettingsController.isRc003Device
                    Layout.fillWidth: true
                    spacing: tokens.spacingSmall

                    Label {
                        text: qsTr("蓝牙配对")
                        color: tokens.textPrimary
                        font.pixelSize: tokens.fontSizeBody
                    }
                    Label {
                        Layout.fillWidth: true
                        wrapMode: Text.WordWrap
                        text: qsTr("RC003 需要先在 Windows 中完成蓝牙配对。配对成功只是连接前提，不代表后台桥接已经连上设备。")
                        color: tokens.textSecondary
                        font.pixelSize: tokens.fontSizeSmall
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        Button {
                            id: openBluetoothSettingsButton
                            objectName: "openBluetoothSettingsButton"
                            text: qsTr("打开蓝牙设置")
                            onClicked: SettingsController.openBluetoothSettings()
                            Accessible.name: text
                            KeyNavigation.tab: openMicrophonePrivacyButton
                            onActiveFocusChanged: {
                                if (activeFocus) {
                                    root.ensureVisible(this)
                                }
                            }
                        }
                        Item { Layout.fillWidth: true }
                    }
                }

                Rectangle {
                    visible: SettingsController.isRc003Device
                    Layout.fillWidth: true
                    Layout.leftMargin: tokens.spacingSmall
                    Layout.rightMargin: tokens.spacingSmall
                    Layout.preferredHeight: 1
                    color: tokens.border
                }

                ColumnLayout {
                    id: microphonePermissionBlock
                    objectName: "microphonePermissionBlock"
                    Layout.fillWidth: true
                    spacing: tokens.spacingSmall

                    Label {
                        text: qsTr("目标输入法或应用的麦克风访问（仅语音）")
                        color: tokens.textPrimary
                        font.pixelSize: tokens.fontSizeBody
                    }
                    Label {
                        objectName: "microphonePermissionDescription"
                        Layout.fillWidth: true
                        wrapMode: Text.WordWrap
                        text: SettingsController.isRc003Device
                            ? qsTr("Remote Mic 把 RC003 语音送到选定的播放端点；真正录音的是输入法或目标应用。使用 VB-CABLE 时，它们需要获得麦克风访问权，并把输入设备选为 CABLE Output。普通按键映射不依赖这些设置。")
                            : qsTr("DJI Mic 2 作为 Windows 录音输入，由目标应用直接读取。目标应用需要获得麦克风访问权，并在自己的输入设置中选择 DJI Mic 2；Remote Mic 不会替它修改默认输入设备。")
                        color: tokens.textSecondary
                        font.pixelSize: tokens.fontSizeSmall
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: tokens.spacingSmall

                        Button {
                            id: openMicrophonePrivacyButton
                            objectName: "openMicrophonePrivacyButton"
                            text: qsTr("打开麦克风隐私设置")
                            onClicked: SettingsController.openMicrophonePrivacySettings()
                            Accessible.name: text
                            KeyNavigation.tab: openSoundInputSettingsButton
                            onActiveFocusChanged: {
                                if (activeFocus) {
                                    root.ensureVisible(this)
                                }
                            }
                        }
                        Button {
                            id: openSoundInputSettingsButton
                            objectName: "openSoundInputSettingsButton"
                            text: qsTr("打开声音输入设置")
                            onClicked: SettingsController.openSoundSettings()
                            Accessible.name: text
                            KeyNavigation.tab: SettingsController.isRc003Device
                                ? openDiagnosticsButton
                                : openSpeechSettingsButton
                            onActiveFocusChanged: {
                                if (activeFocus) {
                                    root.ensureVisible(this)
                                }
                            }
                        }
                        Item { Layout.fillWidth: true }
                    }
                }
            }

            Rectangle {
                visible: SettingsController.isRc003Device
                Layout.fillWidth: true
                Layout.leftMargin: tokens.spacingSmall
                Layout.rightMargin: tokens.spacingSmall
                Layout.preferredHeight: 1
                color: tokens.border
            }

            // -- Optional enhancements --------------------------------------
            ColumnLayout {
                id: optionalSection
                objectName: "optionalEnhancementsSection"
                visible: SettingsController.isRc003Device
                Layout.fillWidth: true
                spacing: tokens.spacingMedium

                Label {
                    text: qsTr("可选增强")
                    color: tokens.textPrimary
                    font.pixelSize: tokens.fontSizeBody
                    font.bold: true
                }

                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: tokens.spacingTiny

                    Label {
                        text: qsTr("HID tap（补齐部分特殊键）")
                        color: tokens.textPrimary
                        font.pixelSize: tokens.fontSizeBody
                    }
                    Label {
                        Layout.fillWidth: true
                        wrapMode: Text.WordWrap
                        text: qsTr("只在 Windows 普通输入链路拿不到返回、音量等 usage 时用于补齐。仅使用 HID tap 时需要从管理员终端启动；普通 BLE、Raw Input、语音和其他按键不会因为未提权而整体失效，Remote Mic 也不会自动提权。")
                        color: tokens.textSecondary
                        font.pixelSize: tokens.fontSizeSmall
                    }
                }

                Rectangle {
                    Layout.fillWidth: true
                    Layout.leftMargin: tokens.spacingSmall
                    Layout.rightMargin: tokens.spacingSmall
                    Layout.preferredHeight: 1
                    color: tokens.border
                }

                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: tokens.spacingSmall

                    Label {
                        text: qsTr("VB-CABLE 语音路由")
                        color: tokens.textPrimary
                        font.pixelSize: tokens.fontSizeBody
                    }
                    Label {
                        Layout.fillWidth: true
                        wrapMode: Text.WordWrap
                        text: qsTr("VB-CABLE 是可选的系统音频路由。安装和端点检测仍在“检查与修复”页完成；安装器会单独请求 UAC，Remote Mic 本身不会提权，也不会修改 Windows 默认输入或输出设备。")
                        color: tokens.textSecondary
                        font.pixelSize: tokens.fontSizeSmall
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        Button {
                            id: openDiagnosticsButton
                            objectName: "openDiagnosticsButton"
                            text: qsTr("前往检查与修复")
                            onClicked: root.openDiagnosticsRequested()
                            Accessible.name: text
                            KeyNavigation.tab: openMappingButton
                            onActiveFocusChanged: {
                                if (activeFocus) {
                                    root.ensureVisible(this)
                                }
                            }
                        }
                        Item { Layout.fillWidth: true }
                    }
                }
            }

            Rectangle {
                visible: SettingsController.isRc003Device
                Layout.fillWidth: true
                Layout.leftMargin: tokens.spacingSmall
                Layout.rightMargin: tokens.spacingSmall
                Layout.preferredHeight: 1
                color: tokens.border
            }

            // -- Manual setup ------------------------------------------------
            ColumnLayout {
                id: manualSection
                objectName: "manualSetupSection"
                Layout.fillWidth: true
                spacing: tokens.spacingMedium

                Label {
                    text: qsTr("手动操作")
                    color: tokens.textPrimary
                    font.pixelSize: tokens.fontSizeBody
                    font.bold: true
                }

                ColumnLayout {
                    id: hostVoiceSetupBlock
                    objectName: "hostVoiceSetupBlock"
                    visible: SettingsController.isRc003Device
                    Layout.fillWidth: true
                    spacing: tokens.spacingSmall

                    Label {
                        text: qsTr("宿主语音快捷键与输入设备")
                        color: tokens.textPrimary
                        font.pixelSize: tokens.fontSizeBody
                    }
                    Label {
                        Layout.fillWidth: true
                        wrapMode: Text.WordWrap
                        text: qsTr("输入法设置中的语音快捷键要与“按键映射”页录入的对应快捷键一致；实际由遥控器哪个键触发语音，也在按键映射中决定。语音目标还需要在输入法或应用内选对麦克风输入。")
                        color: tokens.textSecondary
                        font.pixelSize: tokens.fontSizeSmall
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        Button {
                            id: openMappingButton
                            objectName: "openMappingButton"
                            text: SettingsController.mappingPageTitle
                            onClicked: root.openMappingRequested()
                            Accessible.name: qsTr("打开") + text
                            KeyNavigation.tab: openSpeechSettingsButton
                            onActiveFocusChanged: {
                                if (activeFocus) {
                                    root.ensureVisible(this)
                                }
                            }
                        }
                        Item { Layout.fillWidth: true }
                    }
                }

                Rectangle {
                    visible: SettingsController.isRc003Device
                    Layout.fillWidth: true
                    Layout.leftMargin: tokens.spacingSmall
                    Layout.rightMargin: tokens.spacingSmall
                    Layout.preferredHeight: 1
                    color: tokens.border
                }

                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: tokens.spacingSmall

                    Label {
                        text: qsTr("Windows 听写（仅 Win+H）")
                        color: tokens.textPrimary
                        font.pixelSize: tokens.fontSizeBody
                    }
                    Label {
                        Layout.fillWidth: true
                        wrapMode: Text.WordWrap
                        text: qsTr("只有使用 Windows 自带 Win+H 听写时，才需要检查 Windows 联机语音识别设置；它不是搜狗、豆包等第三方输入法语音功能的共同前提。")
                        color: tokens.textSecondary
                        font.pixelSize: tokens.fontSizeSmall
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        Button {
                            id: openSpeechSettingsButton
                            objectName: "openSpeechSettingsButton"
                            text: qsTr("打开语音识别设置")
                            onClicked: SettingsController.openSpeechSettings()
                            Accessible.name: text
                            KeyNavigation.tab: permissionsOpenLogButton
                            onActiveFocusChanged: {
                                if (activeFocus) {
                                    root.ensureVisible(this)
                                }
                            }
                        }
                        Item { Layout.fillWidth: true }
                    }
                }
            }

            // -- Troubleshooting --------------------------------------------
            Rectangle {
                id: troubleshootingSection
                objectName: "permissionsTroubleshootingSection"
                Layout.fillWidth: true
                radius: tokens.cornerRadiusSmall
                color: tokens.surfaceMuted
                border.color: tokens.border
                border.width: 1
                implicitHeight: troubleshootingRow.implicitHeight + tokens.sectionVerticalPadding * 2

                RowLayout {
                    id: troubleshootingRow
                    anchors.fill: parent
                    anchors.leftMargin: tokens.spacingLarge
                    anchors.rightMargin: tokens.spacingLarge
                    anchors.topMargin: tokens.sectionVerticalPadding
                    anchors.bottomMargin: tokens.sectionVerticalPadding
                    spacing: tokens.spacingMedium

                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: tokens.spacingTiny

                        Label {
                            text: qsTr("问题排查")
                            color: tokens.textPrimary
                            font.pixelSize: tokens.fontSizeBody
                            font.bold: true
                        }
                        Label {
                            Layout.fillWidth: true
                            wrapMode: Text.WordWrap
                            text: qsTr("日志不记录语音内容、蓝牙地址或外设标识符。")
                            color: tokens.textSecondary
                            font.pixelSize: tokens.fontSizeSmall
                        }
                    }
                    Button {
                        id: permissionsOpenLogButton
                        objectName: "permissionsOpenLogButton"
                        text: qsTr("打开日志目录")
                        onClicked: SettingsController.openLogLocation()
                        Accessible.name: text
                        onActiveFocusChanged: {
                            if (activeFocus) {
                                root.ensureVisible(this)
                            }
                        }
                    }
                }
            }

            Item { Layout.preferredHeight: tokens.spacingLarge }
        }
    }
}
