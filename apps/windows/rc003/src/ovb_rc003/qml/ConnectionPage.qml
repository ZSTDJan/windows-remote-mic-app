import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import OvbRc003Settings 1.0

Item {
    id: root
    property var tokens

    function ensureVisible(item) {
        if (!item)
            return
        Qt.callLater(function() {
            const flickable = connectionScroll.contentItem
            if (!flickable || flickable.contentHeight <= connectionScroll.availableHeight)
                return
            const position = item.mapToItem(pageContent, 0, 0)
            flickable.contentY = Math.max(0, Math.min(
                flickable.contentHeight - connectionScroll.availableHeight,
                position.y - tokens.spacingMedium
            ))
        })
    }

    ScrollView {
        id: connectionScroll
        objectName: "connectionScroll"
        anchors.fill: parent
        clip: true
        contentWidth: availableWidth
        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff

        Item {
            id: pageContent
            objectName: "connectionPageContent"
            width: connectionScroll.availableWidth
            implicitHeight: connectionPanel.height + tokens.pageVerticalPadding * 2

            RowLayout {
                id: connectionPanel
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.top: parent.top
                anchors.margins: tokens.pageHorizontalPadding
                height: 398
                spacing: tokens.spacingMedium

                SectionFrame {
                    id: deviceCard
                    objectName: "deviceSection"
                    tokens: root.tokens
                    Layout.preferredWidth: 168
                    Layout.minimumWidth: 168
                    Layout.maximumWidth: 168
                    Layout.fillHeight: true
                    horizontalPadding: 12
                    verticalPadding: 12
                    contentSpacing: 0

                    ColumnLayout {
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        spacing: 0

                        UiLabel {
                            tokens: root.tokens
                            kind: sectionTitleKind
                            Layout.fillWidth: true
                            horizontalAlignment: Text.AlignHCenter
                            text: SettingsController.isRc003Device
                                ? qsTr("小米蓝牙遥控器 2 Pro") : qsTr("DJI Mic 2")
                            font.pixelSize: 14
                            elide: Text.ElideRight
                        }
                        UiLabel {
                            tokens: root.tokens
                            kind: noteKind
                            Layout.fillWidth: true
                            horizontalAlignment: Text.AlignHCenter
                            text: SettingsController.isRc003Device
                                ? qsTr("RC003 · 已选择") : qsTr("无线麦克风 · 已选择")
                        }

                        Item {
                            Layout.fillWidth: true
                            Layout.preferredHeight: 190
                            Layout.topMargin: 8
                            Layout.bottomMargin: 6

                            Image {
                                id: connectionPhoto
                                anchors.centerIn: parent
                                width: height * 240 / 360
                                height: 190
                                source: SettingsController.photoAvailable
                                    && SettingsController.isRc003Device
                                    ? SettingsController.photoSource : ""
                                visible: source.toString().length > 0
                                fillMode: Image.Stretch
                                smooth: true
                                mipmap: true
                            }
                            IconGlyph {
                                anchors.centerIn: parent
                                visible: !connectionPhoto.visible
                                tokens: root.tokens
                                glyph: SettingsController.isRc003Device ? "\uE7F8" : "\uE720"
                                glyphSize: 42
                                color: tokens.disabledText
                            }
                        }

                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 5

                            RowLayout {
                                Layout.fillWidth: true
                                spacing: 5
                                IconGlyph { tokens: root.tokens; glyph: "\uE702"; glyphSize: 13; Layout.preferredWidth: 15; color: tokens.accent }
                                UiLabel { tokens: root.tokens; kind: bodyKind; text: qsTr("蓝牙配对"); Layout.fillWidth: true; font.pixelSize: tokens.fontSizeSmall }
                                UiLabel { tokens: root.tokens; kind: noteKind; text: qsTr("系统管理"); font.pixelSize: tokens.fontSizeTiny }
                            }
                            RowLayout {
                                Layout.fillWidth: true
                                spacing: 5
                                IconGlyph { tokens: root.tokens; glyph: "\uE767"; glyphSize: 13; Layout.preferredWidth: 15; color: tokens.accent }
                                UiLabel { tokens: root.tokens; kind: bodyKind; text: qsTr("语音输出"); Layout.fillWidth: true; font.pixelSize: tokens.fontSizeSmall }
                                UiLabel { tokens: root.tokens; kind: noteKind; text: qsTr("保存时检查"); font.pixelSize: tokens.fontSizeTiny }
                            }
                            RowLayout {
                                Layout.fillWidth: true
                                spacing: 5
                                IconGlyph { tokens: root.tokens; glyph: "\uE9D9"; glyphSize: 13; Layout.preferredWidth: 15; color: SettingsController.bridgeRunning ? tokens.successColor : tokens.accent }
                                UiLabel { tokens: root.tokens; kind: bodyKind; text: qsTr("后台桥接"); Layout.fillWidth: true; font.pixelSize: tokens.fontSizeSmall }
                                UiLabel { tokens: root.tokens; kind: noteKind; text: qsTr("自动更新"); font.pixelSize: tokens.fontSizeTiny }
                            }
                        }

                        Item { Layout.fillHeight: true }
                    }
                }

                SectionFrame {
                    id: settingsSheet
                    objectName: "rc003OutputSection"
                    tokens: root.tokens
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    horizontalPadding: 14
                    verticalPadding: 0
                    contentSpacing: 0

                    ColumnLayout {
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        Layout.bottomMargin: 10
                        spacing: 0

                        Item {
                            Layout.fillWidth: true
                            Layout.preferredHeight: 70

                            FormField {
                                anchors.left: parent.left
                                anchors.right: parent.right
                                anchors.verticalCenter: parent.verticalCenter
                                tokens: root.tokens
                                titleText: qsTr("当前设备")
                                noteText: SettingsController.isRc003Device
                                    ? qsTr("用于遥控按键和按住说话。")
                                    : qsTr("直接使用 Windows 录音输入，不经过 RC003 桥接。")
                                errorText: SettingsController.deviceCatalogAvailable
                                    ? "" : SettingsController.deviceCatalogErrorText

                                SelectionComboBox {
                                    id: deviceCombo
                                    objectName: "deviceCombo"
                                    tokens: root.tokens
                                    Layout.fillWidth: true
                                    model: SettingsController.deviceOptions
                                    currentIndex: SettingsController.selectedDeviceIndex
                                    onActivated: SettingsController.selectedDeviceIndex = index
                                    enabled: SettingsController.deviceCatalogAvailable
                                        && !DiagnosticsController.vbCableTestRunning
                                    Accessible.name: qsTr("当前设备")
                                    KeyNavigation.tab: SettingsController.isRc003Device ? endpointCombo : refreshDjiButton
                                    onActiveFocusChanged: if (activeFocus) root.ensureVisible(this)
                                }
                            }
                        }

                        Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: tokens.border }

                        Item {
                            id: outputBlock
                            visible: SettingsController.isRc003Device
                            Layout.fillWidth: true
                            Layout.preferredHeight: visible ? 70 : 0

                            FormField {
                                anchors.left: parent.left
                                anchors.right: parent.right
                                anchors.verticalCenter: parent.verticalCenter
                                tokens: root.tokens
                                titleText: qsTr("输出端点")
                                noteObjectName: "endpointAvailabilityNote"
                                noteText: qsTr("保存时检查；更换设备或输出后需重启。")
                                errorText: SettingsController.endpointOptions.length === 0
                                    ? qsTr("没有可用的 WASAPI 或 DirectSound 输出设备。") : ""

                                SelectionComboBox {
                                    id: endpointCombo
                                    objectName: "endpointCombo"
                                    tokens: root.tokens
                                    recommendedIndex: SettingsController.recommendedEndpointIndex
                                    Layout.fillWidth: true
                                    model: SettingsController.endpointOptions
                                    currentIndex: SettingsController.selectedEndpointIndex
                                    onActivated: SettingsController.selectedEndpointIndex = index
                                    enabled: !DiagnosticsController.vbCableTestRunning
                                    Accessible.name: qsTr("输出端点")
                                    KeyNavigation.tab: restoreDefaultsButton
                                    onActiveFocusChanged: if (activeFocus) root.ensureVisible(this)
                                }
                            }
                        }

                        Item {
                            id: djiInputSection
                            objectName: "djiInputSection"
                            visible: SettingsController.isDjiMic2Device
                            Layout.fillWidth: true
                            Layout.preferredHeight: visible ? 100 : 0

                            ColumnLayout {
                                anchors.fill: parent
                                anchors.topMargin: 10
                                anchors.bottomMargin: 10
                                spacing: 5
                                UiLabel { tokens: root.tokens; kind: sectionTitleKind; text: qsTr("Windows 录音输入") }
                                UiLabel { tokens: root.tokens; kind: bodyKind; Layout.fillWidth: true; text: SettingsController.djiMicStatusText; wrapMode: Text.WordWrap }
                                UiLabel { tokens: root.tokens; kind: noteKind; Layout.fillWidth: true; text: qsTr("不会修改 Windows 默认输入设备。"); wrapMode: Text.WordWrap }
                                RowLayout {
                                    Layout.fillWidth: true
                                    spacing: tokens.spacingSmall
                                    CompactButton {
                                        id: refreshDjiButton
                                        objectName: "refreshDjiButton"
                                        tokens: root.tokens
                                        text: qsTr("重新检测")
                                        onClicked: SettingsController.refreshDjiMicStatus()
                                        KeyNavigation.tab: openSoundSettingsButton
                                    }
                                    CompactButton {
                                        id: openSoundSettingsButton
                                        objectName: "openSoundSettingsButton"
                                        tokens: root.tokens
                                        text: qsTr("声音输入")
                                        highlighted: true
                                        onClicked: SettingsController.openSoundSettings()
                                        KeyNavigation.tab: saveOnlyButton
                                    }
                                    Item { Layout.fillWidth: true }
                                }
                            }
                        }

                        Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: tokens.border }

                        ColumnLayout {
                            id: bridgeSection
                            objectName: "bridgeSection"
                            visible: SettingsController.isRc003Device
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            Layout.topMargin: 10
                            spacing: 7

                            UiLabel { tokens: root.tokens; kind: sectionTitleKind; text: qsTr("后台桥接") }
                            RowLayout {
                                id: bridgeLaunchProgress
                                objectName: "bridgeLaunchProgress"
                                Layout.fillWidth: true
                                spacing: 7

                                BusyIndicator {
                                    id: bridgeLaunchBusyIndicator
                                    objectName: "bridgeLaunchBusyIndicator"
                                    Layout.preferredWidth: 18
                                    Layout.preferredHeight: 18
                                    visible: running
                                    running: SettingsController.bridgeLaunchPhase === "saving"
                                        || SettingsController.bridgeLaunchPhase === "starting"
                                        || SettingsController.bridgeLaunchPhase === "waiting"
                                }
                                UiLabel {
                                    id: bridgeLaunchStageText
                                    objectName: "bridgeLaunchStageText"
                                    tokens: root.tokens
                                    kind: noteKind
                                    Layout.fillWidth: true
                                    text: SettingsController.bridgeLaunchPhase === "saving"
                                        ? qsTr("保存设置… → 启动桥接 → 等待设备连接")
                                        : SettingsController.bridgeLaunchPhase === "starting"
                                            ? qsTr("保存设置 ✓ → 启动桥接… → 等待设备连接")
                                            : SettingsController.bridgeLaunchPhase === "waiting"
                                                ? qsTr("保存设置 ✓ → 桥接进程已启动 ✓ → 等待 RC003 连接…")
                                                : SettingsController.bridgeLaunchPhase === "connected"
                                                    ? qsTr("保存设置 ✓ → 桥接进程已启动 ✓ → RC003 已连接 ✓")
                                                    : SettingsController.bridgeLaunchPhase === "failed"
                                                        ? qsTr("保存或启动未完成；RC003 未连接")
                                                        : SettingsController.bridgeLaunchPhase === "unknown"
                                                            ? qsTr("桥接状态暂时无法确认")
                                                            : qsTr("保存设置 → 启动桥接 → 等待设备连接")
                                    elide: Text.ElideRight
                                }
                                UiLabel {
                                    id: bridgeLaunchElapsedText
                                    objectName: "bridgeLaunchElapsedText"
                                    tokens: root.tokens
                                    kind: noteKind
                                    visible: bridgeLaunchBusyIndicator.running
                                    text: qsTr("已等待 %1 秒").arg(
                                        SettingsController.bridgeLaunchElapsedSeconds
                                    )
                                }
                            }
                            RowLayout {
                                Layout.fillWidth: true
                                spacing: 7
                                Rectangle {
                                    Layout.preferredWidth: 8
                                    Layout.preferredHeight: 8
                                    radius: 4
                                    color: SettingsController.bridgeRunning ? tokens.successColor : tokens.voiceAccent
                                }
                                UiLabel {
                                    id: launchStatusText
                                    objectName: "launchStatusText"
                                    tokens: root.tokens
                                    kind: bodyKind
                                    Layout.fillWidth: true
                                    text: SettingsController.launchStatusText
                                    font.weight: Font.Medium
                                    wrapMode: Text.WordWrap
                                }
                            }
                            UiLabel {
                                objectName: "bridgeNotRunningWarning"
                                tokens: root.tokens
                                kind: noteKind
                                Layout.fillWidth: true
                                text: SettingsController.bridgeRunning
                                    ? qsTr("按键、声音和文字输入仍需实际测试。")
                                    : qsTr("未启动时语音键无效；按键、声音和文字输入需实际测试。")
                                wrapMode: Text.WordWrap
                            }

                            RowLayout {
                                id: actionRow
                                objectName: "connectionActionRow"
                                Layout.fillWidth: true
                                spacing: tokens.spacingSmall

                                CompactButton {
                                    id: restoreDefaultsButton
                                    objectName: "restoreDefaultsButton"
                                    tokens: root.tokens
                                    text: qsTr("恢复按键与语音默认")
                                    onClicked: SettingsController.restoreDefaults()
                                    KeyNavigation.tab: saveOnlyButton
                                }
                                Item { Layout.fillWidth: true }
                                CompactButton {
                                    id: saveOnlyButton
                                    objectName: "deviceSaveButton"
                                    tokens: root.tokens
                                    compactMinimumWidth: 116
                                    text: SettingsController.isRc003Device ? qsTr("仅保存设置") : qsTr("保存设备选择")
                                    highlighted: !SettingsController.isRc003Device
                                    enabled: !DiagnosticsController.vbCableTestRunning
                                    onClicked: SettingsController.saveSettings()
                                }
                                CompactButton {
                                    id: saveAndLaunchButton
                                    objectName: "saveAndLaunchButton"
                                    tokens: root.tokens
                                    visible: SettingsController.isRc003Device
                                    compactMinimumWidth: 116
                                    text: SettingsController.bridgeLaunchBusy
                                        ? qsTr("正在启动桥接") : qsTr("保存并启动桥接")
                                    highlighted: true
                                    enabled: !SettingsController.bridgeLaunchBusy
                                        && !DiagnosticsController.vbCableTestRunning
                                    onClicked: SettingsController.saveAndLaunch()
                                }
                            }

                            Item { Layout.fillHeight: true }
                        }
                    }
                }
            }
        }
    }
}
