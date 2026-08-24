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

            UiLabel {
                tokens: root.tokens
                kind: pageTitleKind
                text: qsTr("连接与启动")
            }

            // -- Device ------------------------------------------------------
            SectionFrame {
                id: deviceSection
                objectName: "deviceSection"
                tokens: root.tokens
                Layout.fillWidth: true

                FormField {
                    tokens: root.tokens
                    titleText: qsTr("设备")
                    errorText: SettingsController.deviceCatalogAvailable
                        ? "" : SettingsController.deviceCatalogErrorText
                    noteText: SettingsController.selectedDeviceDescription
                    Layout.fillWidth: true

                    SelectionComboBox {
                        id: deviceCombo
                        objectName: "deviceCombo"
                        tokens: root.tokens
                        Layout.fillWidth: true
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
                }
            }

            // -- RC003 output route -----------------------------------------
            SectionFrame {
                id: rc003OutputSection
                objectName: "rc003OutputSection"
                tokens: root.tokens
                visible: SettingsController.isRc003Device
                Layout.fillWidth: true

                FormField {
                    tokens: root.tokens
                    titleText: qsTr("语音输出")
                    errorText: SettingsController.endpointOptions.length === 0
                        ? qsTr("当前没有检测到可用的 Windows WASAPI 或 DirectSound 输出设备。")
                        : ""
                    noteObjectName: "endpointAvailabilityNote"
                    noteText: qsTr("Remote Mic 将 RC003 语音写入这里。使用 VB-CABLE 时选择 CABLE Input；目标输入法或应用应监听 CABLE Output。")
                        + "\n"
                        + qsTr("列表可能保留已保存但当前缺失的端点；启用语音映射时，保存会实际验证所选设备，缺失或无法打开时会拒绝保存。更换设备或语音输出后，需要重新启动后台桥接。")
                    Layout.fillWidth: true

                    SelectionComboBox {
                        id: endpointCombo
                        objectName: "endpointCombo"
                        tokens: root.tokens
                        recommendedIndex: SettingsController.recommendedEndpointIndex
                        Layout.fillWidth: true
                        model: SettingsController.endpointOptions
                        currentIndex: SettingsController.selectedEndpointIndex
                        onActivated: SettingsController.selectedEndpointIndex = index
                        Accessible.name: qsTr("语音输出设备")
                        KeyNavigation.tab: restoreDefaultsButton
                        onActiveFocusChanged: {
                            if (activeFocus) {
                                root.ensureVisible(this)
                            }
                        }
                    }
                }
            }

            // -- DJI system-input workflow ---------------------------------
            SectionFrame {
                id: djiInputSection
                objectName: "djiInputSection"
                tokens: root.tokens
                visible: SettingsController.isDjiMic2Device
                Layout.fillWidth: true

                UiLabel {
                    tokens: root.tokens
                    kind: sectionTitleKind
                    text: qsTr("Windows 录音输入")
                }
                UiLabel {
                    tokens: root.tokens
                    kind: bodyKind
                    Layout.fillWidth: true
                    wrapMode: Text.WordWrap
                    text: SettingsController.djiMicStatusText
                }
                UiLabel {
                    tokens: root.tokens
                    kind: noteKind
                    Layout.fillWidth: true
                    wrapMode: Text.WordWrap
                    text: qsTr("DJI Mic 2 直接作为系统麦克风使用，不经过 RC003 的 BLE/HID/ATVV 桥，也不使用 CABLE Input。Remote Mic 不会修改 Windows 默认输入设备。")
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

            // -- Bridge result ----------------------------------------------
            SectionFrame {
                id: bridgeSection
                objectName: "bridgeSection"
                tokens: root.tokens
                visible: SettingsController.isRc003Device
                Layout.fillWidth: true
                fillColor: tokens.surfaceMuted

                UiLabel {
                    Layout.fillWidth: true
                    tokens: root.tokens
                    kind: sectionTitleKind
                    text: qsTr("后台桥接")
                }
                UiLabel {
                    tokens: root.tokens
                    kind: bodyKind
                    objectName: "launchStatusText"
                    Layout.fillWidth: true
                    wrapMode: Text.WordWrap
                    text: SettingsController.launchStatusText
                }
                UiLabel {
                    tokens: root.tokens
                    kind: bodyKind
                    objectName: "bridgeNotRunningWarning"
                    Layout.fillWidth: true
                    visible: !SettingsController.bridgeRunning
                    wrapMode: Text.WordWrap
                    text: qsTr("后台桥接未运行或尚未确认运行：话筒键不会由本程序触发语音。请点击下方“保存并启动桥接”，首次连接可能需要约一分钟。")
                    color: tokens.errorColor
                    font.bold: true
                }
                UiLabel {
                    tokens: root.tokens
                    kind: noteKind
                    Layout.fillWidth: true
                    wrapMode: Text.WordWrap
                    text: qsTr("这里显示窗口打开时的桥接状态或本设置窗口最近一次启动结果，不代表 RC003 已连接。实际连接与语音状态请以日志和真机测试为准。")
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
