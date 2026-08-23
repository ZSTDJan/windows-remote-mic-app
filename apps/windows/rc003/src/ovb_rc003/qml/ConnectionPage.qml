// Device, audio-route and bridge-start workflow. This page deliberately
// keeps saving and launching as separate commands: connection/output changes
// need a bridge restart, while button mappings can be picked up live.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import OvbRc003Settings 1.0

Item {
    id: root
    property var tokens

    function ensureVisible(item) {
        if (!item) {
            return
        }
        Qt.callLater(function() {
            const flickable = connectionScroll.contentItem
            if (!flickable || flickable.contentHeight <= connectionScroll.availableHeight) {
                return
            }
            const position = item.mapToItem(pageColumn, 0, 0)
            const top = pageColumn.y + position.y - tokens.spacingMedium
            const bottom = pageColumn.y + position.y + item.height + tokens.spacingMedium
            const viewportTop = flickable.contentY
            const viewportBottom = viewportTop + connectionScroll.availableHeight
            const maximumY = Math.max(0, flickable.contentHeight - connectionScroll.availableHeight)
            if (top < viewportTop) {
                flickable.contentY = Math.max(0, top)
            } else if (bottom > viewportBottom) {
                flickable.contentY = Math.min(maximumY, bottom - connectionScroll.availableHeight)
            }
        })
    }

    ScrollView {
        id: connectionScroll
        objectName: "connectionScroll"
        anchors.fill: parent
        clip: true
        contentWidth: availableWidth
        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff

        ColumnLayout {
            id: pageColumn
            objectName: "connectionPageContent"
            width: Math.max(0, Math.min(
                root.width - tokens.pageHorizontalPadding * 2,
                tokens.pageMaxWidth
            ))
            x: Math.max(tokens.pageHorizontalPadding, (root.width - width) / 2)
            y: tokens.spacingLarge
            spacing: tokens.spacingLarge

            Label {
                text: qsTr("连接与启动")
                color: tokens.textPrimary
                font.pixelSize: tokens.fontSizeTitle
                font.bold: true
            }

            // -- Device ------------------------------------------------------
            ColumnLayout {
                id: deviceSection
                objectName: "deviceSection"
                Layout.fillWidth: true
                spacing: tokens.spacingSmall

                Label {
                    text: qsTr("设备")
                    color: tokens.textPrimary
                    font.pixelSize: tokens.fontSizeBody
                    font.bold: true
                }
                ComboBox {
                    id: deviceCombo
                    objectName: "deviceCombo"
                    Layout.fillWidth: true
                    Layout.maximumWidth: 560
                    model: SettingsController.deviceOptions
                    currentIndex: SettingsController.selectedDeviceIndex
                    onActivated: SettingsController.selectedDeviceIndex = index
                    enabled: SettingsController.deviceCatalogAvailable
                    Accessible.name: qsTr("设备")
                    KeyNavigation.tab: SettingsController.isRc003Device
                        ? endpointCombo
                        : refreshDjiButton
                    onActiveFocusChanged: {
                        if (activeFocus) {
                            root.ensureVisible(this)
                        }
                    }
                }
                Label {
                    visible: !SettingsController.deviceCatalogAvailable
                    Layout.fillWidth: true
                    wrapMode: Text.WordWrap
                    text: SettingsController.deviceCatalogErrorText
                    color: tokens.errorColor
                    font.pixelSize: tokens.fontSizeSmall
                }
                Label {
                    Layout.fillWidth: true
                    wrapMode: Text.WordWrap
                    text: SettingsController.selectedDeviceDescription
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

            // -- RC003 output route -----------------------------------------
            ColumnLayout {
                id: rc003OutputSection
                objectName: "rc003OutputSection"
                visible: SettingsController.isRc003Device
                Layout.fillWidth: true
                spacing: tokens.spacingSmall

                Label {
                    text: qsTr("语音输出")
                    color: tokens.textPrimary
                    font.pixelSize: tokens.fontSizeBody
                    font.bold: true
                }
                Label {
                    Layout.fillWidth: true
                    wrapMode: Text.WordWrap
                    text: qsTr("Remote Mic 将 RC003 语音写入这里。使用 VB-CABLE 时选择 CABLE Input；目标输入法或应用应监听 CABLE Output。")
                    color: tokens.textSecondary
                    font.pixelSize: tokens.fontSizeSmall
                }
                ComboBox {
                    id: endpointCombo
                    objectName: "endpointCombo"
                    Layout.fillWidth: true
                    Layout.maximumWidth: 560
                    model: SettingsController.endpointOptions
                    currentIndex: SettingsController.selectedEndpointIndex
                    onActivated: SettingsController.selectedEndpointIndex = index
                    Accessible.name: qsTr("语音输出设备")
                    KeyNavigation.tab: openLogButton
                    onActiveFocusChanged: {
                        if (activeFocus) {
                            root.ensureVisible(this)
                        }
                    }
                }
                Label {
                    visible: SettingsController.endpointOptions.length === 0
                    Layout.fillWidth: true
                    wrapMode: Text.WordWrap
                    text: qsTr("当前没有检测到可用的 Windows WASAPI 或 DirectSound 输出设备。")
                    color: tokens.errorColor
                    font.pixelSize: tokens.fontSizeSmall
                }
                Label {
                    Layout.fillWidth: true
                    wrapMode: Text.WordWrap
                    objectName: "endpointAvailabilityNote"
                    text: qsTr("列表可能保留已保存但当前缺失的端点；启用语音映射时，保存会实际验证所选设备，缺失或无法打开时会拒绝保存。更换设备或语音输出后，需要重新启动后台桥接。")
                    color: tokens.textSecondary
                    font.pixelSize: tokens.fontSizeSmall
                }
            }

            // -- DJI system-input workflow ---------------------------------
            ColumnLayout {
                id: djiInputSection
                objectName: "djiInputSection"
                visible: SettingsController.isDjiMic2Device
                Layout.fillWidth: true
                spacing: tokens.spacingSmall

                Label {
                    text: qsTr("Windows 录音输入")
                    color: tokens.textPrimary
                    font.pixelSize: tokens.fontSizeBody
                    font.bold: true
                }
                Label {
                    Layout.fillWidth: true
                    wrapMode: Text.WordWrap
                    text: SettingsController.djiMicStatusText
                    color: tokens.textPrimary
                    font.pixelSize: tokens.fontSizeBody
                }
                Label {
                    Layout.fillWidth: true
                    wrapMode: Text.WordWrap
                    text: qsTr("DJI Mic 2 直接作为系统麦克风使用，不经过 RC003 的 BLE/HID/ATVV 桥，也不使用 CABLE Input。Remote Mic 不会修改 Windows 默认输入设备。")
                    color: tokens.textSecondary
                    font.pixelSize: tokens.fontSizeSmall
                }
                RowLayout {
                    Layout.fillWidth: true
                    spacing: tokens.spacingSmall

                    Button {
                        id: refreshDjiButton
                        objectName: "refreshDjiButton"
                        text: qsTr("重新检测")
                        onClicked: SettingsController.refreshDjiMicStatus()
                        Accessible.name: text
                        KeyNavigation.tab: openSoundSettingsButton
                        onActiveFocusChanged: {
                            if (activeFocus) {
                                root.ensureVisible(this)
                            }
                        }
                    }
                    Button {
                        id: openSoundSettingsButton
                        objectName: "openSoundSettingsButton"
                        text: qsTr("打开 Windows 声音输入设置")
                        highlighted: true
                        onClicked: SettingsController.openSoundSettings()
                        Accessible.name: text
                        KeyNavigation.tab: saveOnlyButton
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

            // -- Bridge result ----------------------------------------------
            Rectangle {
                id: bridgeSection
                objectName: "bridgeSection"
                visible: SettingsController.isRc003Device
                Layout.fillWidth: true
                radius: tokens.cornerRadiusSmall
                color: tokens.surfaceMuted
                border.color: tokens.border
                border.width: 1
                implicitHeight: bridgeColumn.implicitHeight + tokens.sectionVerticalPadding * 2

                ColumnLayout {
                    id: bridgeColumn
                    anchors.fill: parent
                    anchors.leftMargin: tokens.spacingLarge
                    anchors.rightMargin: tokens.spacingLarge
                    anchors.topMargin: tokens.sectionVerticalPadding
                    anchors.bottomMargin: tokens.sectionVerticalPadding
                    spacing: tokens.spacingSmall

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: tokens.spacingMedium

                        Label {
                            text: qsTr("后台桥接")
                            color: tokens.textPrimary
                            font.pixelSize: tokens.fontSizeBody
                            font.bold: true
                        }
                        Item { Layout.fillWidth: true }
                        Button {
                            id: openLogButton
                            objectName: "openLogButton"  // rendered contrast regression hook
                            text: qsTr("打开日志目录")
                            flat: true
                            onClicked: SettingsController.openLogLocation()
                            Accessible.name: text
                            KeyNavigation.tab: restoreDefaultsButton
                            onActiveFocusChanged: {
                                if (activeFocus) {
                                    root.ensureVisible(this)
                                }
                            }
                        }
                    }
                    Label {
                        objectName: "launchStatusText"
                        Layout.fillWidth: true
                        wrapMode: Text.WordWrap
                        text: SettingsController.launchStatusText
                        color: tokens.textPrimary
                        font.pixelSize: tokens.fontSizeBody
                    }
                    Label {
                        objectName: "bridgeNotRunningWarning"
                        Layout.fillWidth: true
                        visible: !SettingsController.bridgeRunning
                        wrapMode: Text.WordWrap
                        text: qsTr("后台桥接未运行或尚未确认运行：话筒键不会由本程序触发语音。请点击下方“保存并启动桥接”，首次连接可能需要约一分钟。")
                        color: tokens.errorColor
                        font.pixelSize: tokens.fontSizeBody
                        font.bold: true
                    }
                    Label {
                        Layout.fillWidth: true
                        wrapMode: Text.WordWrap
                        text: qsTr("这里显示本设置窗口最近一次启动结果，不代表 RC003 已连接。实际连接与语音状态请以日志和真机测试为准。")
                        color: tokens.textSecondary
                        font.pixelSize: tokens.fontSizeSmall
                    }
                }
            }

            // -- Save/start commands ----------------------------------------
            RowLayout {
                id: actionRow
                objectName: "connectionActionRow"
                Layout.fillWidth: true
                spacing: tokens.spacingSmall

                Button {
                    id: restoreDefaultsButton
                    objectName: "restoreDefaultsButton"
                    visible: SettingsController.isRc003Device
                    text: qsTr("恢复按键与语音默认")
                    flat: true
                    onClicked: SettingsController.restoreDefaults()
                    Accessible.name: text
                    KeyNavigation.tab: saveOnlyButton
                    onActiveFocusChanged: {
                        if (activeFocus) {
                            root.ensureVisible(this)
                        }
                    }
                }
                Item { Layout.fillWidth: true }
                Button {
                    id: saveOnlyButton
                    objectName: "deviceSaveButton"
                    text: SettingsController.isRc003Device
                        ? qsTr("仅保存设置")
                        : qsTr("保存设备选择")
                    highlighted: !SettingsController.isRc003Device
                    onClicked: SettingsController.saveSettings()
                    Accessible.name: text
                    onActiveFocusChanged: {
                        if (activeFocus) {
                            root.ensureVisible(this)
                        }
                    }
                }
                Button {
                    id: saveAndLaunchButton
                    objectName: "saveAndLaunchButton"
                    visible: SettingsController.isRc003Device
                    text: qsTr("保存并启动桥接")
                    highlighted: true
                    onClicked: SettingsController.saveAndLaunch()
                    Accessible.name: text
                    onActiveFocusChanged: {
                        if (activeFocus) {
                            root.ensureVisible(this)
                        }
                    }
                }
            }

            Item { Layout.preferredHeight: tokens.spacingLarge }
        }
    }
}
