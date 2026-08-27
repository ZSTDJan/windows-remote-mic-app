import QtQuick
import QtQuick.Controls
import QtQuick.Dialogs
import QtQuick.Layouts
import OvbRc003Settings 1.0

Item {
    id: root
    property var tokens
    property bool voiceHotkeyRecording: false
    property string voiceHotkeyCaptureError: ""

    readonly property bool voiceProgramManaged:
        SettingsController.selectedVoiceProgramIndex !== 0
    readonly property bool voiceProgramSystemManaged:
        SettingsController.voiceProgramSystemManaged
    readonly property bool bridgeLaunchInProgress:
        SettingsController.bridgeLaunchBusy
        || SettingsController.bridgeLaunchPhase === "waiting"
    readonly property bool voiceProgramPrivilegeUnknown:
        !voiceProgramSystemManaged
        && SettingsController.voiceProgramStatusCode === "running"
        && SettingsController.voiceProgramElevationStatus === "unknown"
    readonly property bool voiceProgramPrivilegeMismatch:
        !voiceProgramSystemManaged
        && SettingsController.voiceProgramStatusCode === "running"
        && SettingsController.voiceProgramElevationStatus !== "unknown"
        && SettingsController.voiceProgramLaunchElevated
            !== (SettingsController.voiceProgramElevationStatus === "elevated")
    readonly property bool voiceProgramNeedsAttention:
        voiceProgramPrivilegeUnknown || voiceProgramPrivilegeMismatch
    readonly property color voiceProgramStateColor:
        voiceProgramSystemManaged
            && (SettingsController.voiceProgramStatusCode === "running"
                || SettingsController.voiceProgramStatusCode === "stopped")
            ? tokens.successColor
            : SettingsController.voiceProgramStatusCode === "running"
            ? (voiceProgramNeedsAttention ? tokens.voiceAccent : tokens.successColor)
            : SettingsController.voiceProgramStatusCode === "disabled"
                ? tokens.accent : tokens.voiceAccent

    function voiceProgramStatusSummary() {
        const code = SettingsController.voiceProgramStatusCode
        if (!voiceProgramManaged)
            return ""
        if (voiceProgramSystemManaged) {
            if (code === "running" || code === "stopped")
                return qsTr("已识别 · 系统管理")
            if (code === "not_found")
                return qsTr("未找到程序")
            return qsTr("需检查")
        }
        if (code === "running") {
            const privilege = SettingsController.voiceProgramElevationStatus
            if (privilege === "unknown")
                return qsTr("运行中 · 权限未知")
            const elevated = privilege === "elevated"
            if (SettingsController.voiceProgramLaunchElevated !== elevated) {
                return SettingsController.voiceProgramLaunchElevated
                    ? qsTr("需重启为管理员") : qsTr("需重启为普通权限")
            }
            return elevated ? qsTr("管理员运行中") : qsTr("普通权限运行中")
        }
        if (code === "stopped")
            return SettingsController.bridgeRunning
                && SettingsController.voiceProgramSettingsDirty
                ? qsTr("已修改 · 待应用") : qsTr("已找到 · 待启动")
        if (code === "not_found") {
            return SettingsController.selectedVoiceProgramIndex === 3
                && SettingsController.voiceProgramCustomPath.length === 0
                ? qsTr("请选择程序") : qsTr("未找到程序")
        }
        return qsTr("需检查")
    }

    function voiceProgramReadinessText() {
        if (!voiceProgramManaged)
            return qsTr("不管理")
        const code = SettingsController.voiceProgramStatusCode
        if (voiceProgramSystemManaged)
            return code === "running" || code === "stopped"
                ? qsTr("已识别") : qsTr("需检查")
        if (code === "running")
            return voiceProgramNeedsAttention ? qsTr("需检查") : qsTr("运行中")
        if (code === "stopped")
            return SettingsController.bridgeRunning
                && SettingsController.voiceProgramSettingsDirty
                ? qsTr("待应用") : qsTr("待启动")
        return qsTr("需检查")
    }

    function startVoiceHotkeyCapture() {
        if (voiceHotkeyRecording) {
            stopVoiceHotkeyCapture()
            return
        }
        voiceHotkeyCaptureError = ""
        voiceHotkeyRecording = true
        SettingsController.startHotkeyCapture()
        voiceHotkeyField.forceActiveFocus()
    }

    function stopVoiceHotkeyCapture() {
        if (!voiceHotkeyRecording)
            return
        voiceHotkeyRecording = false
        SettingsController.stopHotkeyCapture()
    }

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

    component ReadinessRow: RowLayout {
        property string labelText: ""
        property string stateText: ""
        property string detailText: ""
        property color stateColor: root.tokens.accent

        Layout.fillWidth: true
        spacing: 5

        Rectangle {
            Layout.preferredWidth: 8
            Layout.preferredHeight: 8
            radius: 4
            color: parent.stateColor
        }
        UiLabel {
            tokens: root.tokens
            kind: bodyKind
            Layout.fillWidth: true
            text: parent.labelText
            font.pixelSize: root.tokens.fontSizeSmall
            elide: Text.ElideRight
            HoverHandler { id: readinessHover }
            ToolTip.visible: readinessHover.hovered && parent.detailText.length > 0
            ToolTip.text: parent.detailText
        }
        UiLabel {
            tokens: root.tokens
            kind: bodyKind
            text: parent.stateText
            color: parent.stateColor
            font.pixelSize: root.tokens.fontSizeSmall
            font.weight: Font.Medium
        }
    }

    FileDialog {
        id: voiceProgramFileDialog
        title: qsTr("选择语音程序")
        nameFilters: [
            qsTr("程序或快捷方式 (*.exe *.lnk)"),
            qsTr("所有文件 (*)")
        ]
        onAccepted: SettingsController.voiceProgramCustomPath = selectedFile
    }

    Connections {
        target: SettingsController
        function onHotkeyCaptured(chord) {
            if (!root.voiceHotkeyRecording)
                return
            SettingsController.holdVoiceHotkeyText = chord
            root.stopVoiceHotkeyCapture()
        }
        function onHotkeyCaptureError(message) {
            if (!root.voiceHotkeyRecording)
                return
            root.voiceHotkeyCaptureError = message
            root.stopVoiceHotkeyCapture()
        }
    }

    Timer {
        objectName: "voiceProgramStatusRefreshTimer"
        interval: 1500
        repeat: true
        running: root.visible && root.voiceProgramManaged
        onTriggered: SettingsController.refreshVoiceProgramStatus()
    }

    onVisibleChanged: {
        if (visible)
            SettingsController.refreshVoiceProgramStatus()
        else
            stopVoiceHotkeyCapture()
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
            height: Math.max(418, connectionScroll.height)

            RowLayout {
                id: connectionPanel
                anchors.fill: parent
                anchors.margins: tokens.pageHorizontalPadding
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

                        Item {
                            Layout.fillWidth: true
                            Layout.preferredHeight: 190
                            Layout.topMargin: 7
                            Layout.bottomMargin: 5
                            clip: true

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

                        Item { Layout.fillHeight: true }

                        Rectangle {
                            Layout.fillWidth: true
                            Layout.preferredHeight: 1
                            Layout.bottomMargin: 6
                            color: tokens.border
                        }

                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 4

                            ReadinessRow {
                                labelText: qsTr("后台桥接")
                                detailText: SettingsController.launchStatusText
                                stateText: !SettingsController.isRc003Device
                                    ? qsTr("不使用")
                                    : SettingsController.bridgeLaunchPhase === "unknown"
                                        ? qsTr("需检查")
                                        : SettingsController.bridgeRunning
                                            ? qsTr("运行中") : qsTr("未运行")
                                stateColor: !SettingsController.isRc003Device
                                    ? tokens.accent
                                    : SettingsController.bridgeLaunchPhase === "unknown"
                                        ? tokens.voiceAccent
                                        : SettingsController.bridgeRunning
                                            ? tokens.successColor : tokens.voiceAccent
                            }
                            ReadinessRow {
                                labelText: qsTr("当前设备")
                                detailText: SettingsController.deviceOptions.length > 0
                                    && SettingsController.selectedDeviceIndex >= 0
                                    ? SettingsController.deviceOptions[
                                        SettingsController.selectedDeviceIndex
                                    ] : ""
                                stateText: SettingsController.deviceCatalogAvailable
                                    ? qsTr("已配置") : qsTr("需检查")
                                stateColor: SettingsController.deviceCatalogAvailable
                                    ? tokens.accent : tokens.voiceAccent
                            }
                            ReadinessRow {
                                labelText: SettingsController.isRc003Device
                                    ? qsTr("输出端点") : qsTr("录音输入")
                                detailText: SettingsController.isRc003Device
                                    && SettingsController.endpointOptions.length > 0
                                    && SettingsController.selectedEndpointIndex >= 0
                                    ? SettingsController.endpointOptions[
                                        SettingsController.selectedEndpointIndex
                                    ] : ""
                                stateText: SettingsController.isRc003Device
                                    ? (SettingsController.endpointOptions.length > 0
                                        && SettingsController.selectedEndpointIndex >= 0
                                            ? qsTr("已配置") : qsTr("未配置"))
                                    : qsTr("系统管理")
                                stateColor: SettingsController.isRc003Device
                                    && (SettingsController.endpointOptions.length === 0
                                        || SettingsController.selectedEndpointIndex < 0)
                                    ? tokens.voiceAccent : tokens.accent
                            }
                            ReadinessRow {
                                visible: SettingsController.isRc003Device
                                labelText: qsTr("语音程序")
                                detailText: root.voiceProgramManaged
                                    ? SettingsController.voiceProgramStatusText : qsTr("不管理")
                                stateText: root.voiceProgramReadinessText()
                                stateColor: root.voiceProgramStateColor
                            }
                        }

                    }
                }

                SectionFrame {
                    id: settingsSheet
                    objectName: "rc003OutputSection"
                    tokens: root.tokens
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    horizontalPadding: 12
                    verticalPadding: 8
                    contentSpacing: 0

                    ColumnLayout {
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        spacing: 0

                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 3

                            RowLayout {
                                Layout.fillWidth: true
                                Layout.preferredHeight: tokens.controlHeight
                                spacing: tokens.spacingSmall
                                UiLabel {
                                    tokens: root.tokens
                                    kind: bodyKind
                                    Layout.preferredWidth: 64
                                    Layout.minimumWidth: 64
                                    text: qsTr("当前设备")
                                }
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
                                    KeyNavigation.tab: SettingsController.isRc003Device
                                        ? endpointCombo : refreshDjiButton
                                    onActiveFocusChanged: if (activeFocus) root.ensureVisible(this)
                                }
                            }
                            UiLabel {
                                visible: SettingsController.isDjiMic2Device
                                tokens: root.tokens
                                kind: noteKind
                                Layout.fillWidth: true
                                Layout.leftMargin: 70
                                text: qsTr("直接使用 Windows 麦克风输入。")
                                maximumLineCount: 1
                                elide: Text.ElideRight
                            }
                            UiLabel {
                                visible: !SettingsController.deviceCatalogAvailable
                                tokens: root.tokens
                                kind: noteKind
                                Layout.fillWidth: true
                                Layout.leftMargin: 70
                                text: SettingsController.deviceCatalogErrorText
                                color: tokens.errorColor
                                maximumLineCount: 1
                                elide: Text.ElideRight
                            }
                        }

                        Rectangle {
                            Layout.fillWidth: true
                            Layout.preferredHeight: 1
                            Layout.topMargin: 6
                            Layout.bottomMargin: 6
                            color: tokens.border
                        }

                        ColumnLayout {
                            id: outputBlock
                            visible: SettingsController.isRc003Device
                            Layout.fillWidth: true
                            spacing: 3

                            RowLayout {
                                Layout.fillWidth: true
                                Layout.preferredHeight: tokens.controlHeight
                                spacing: tokens.spacingSmall
                                UiLabel {
                                    tokens: root.tokens
                                    kind: bodyKind
                                    Layout.preferredWidth: 64
                                    Layout.minimumWidth: 64
                                    text: qsTr("输出端点")
                                }
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
                                    KeyNavigation.tab: voiceProgramCombo
                                    onActiveFocusChanged: if (activeFocus) root.ensureVisible(this)
                                }
                            }
                            UiLabel {
                                objectName: "endpointAvailabilityNote"
                                tokens: root.tokens
                                kind: noteKind
                                Layout.fillWidth: true
                                Layout.leftMargin: 70
                                text: SettingsController.endpointOptions.length === 0
                                    ? qsTr("没有可用的 WASAPI 或 DirectSound 输出设备。")
                                    : qsTr("保存时检查；更换后需重启桥接。")
                                color: SettingsController.endpointOptions.length === 0
                                    ? tokens.errorColor : tokens.disabledText
                                maximumLineCount: 1
                                elide: Text.ElideRight
                            }
                        }

                        ColumnLayout {
                            id: djiInputSection
                            objectName: "djiInputSection"
                            visible: SettingsController.isDjiMic2Device
                            Layout.fillWidth: true
                            spacing: 4

                            UiLabel {
                                tokens: root.tokens
                                kind: bodyKind
                                Layout.fillWidth: true
                                text: SettingsController.djiMicStatusText
                                maximumLineCount: 1
                                elide: Text.ElideRight
                            }
                            RowLayout {
                                Layout.fillWidth: true
                                spacing: tokens.spacingSmall
                                CompactButton {
                                    id: refreshDjiButton
                                    objectName: "refreshDjiButton"
                                    tokens: root.tokens
                                    compactMinimumWidth: tokens.buttonWidth4Chars
                                    text: qsTr("重新检测")
                                    onClicked: SettingsController.refreshDjiMicStatus()
                                    KeyNavigation.tab: openSoundSettingsButton
                                }
                                CompactButton {
                                    id: openSoundSettingsButton
                                    objectName: "openSoundSettingsButton"
                                    tokens: root.tokens
                                    compactMinimumWidth: tokens.buttonWidth4Chars
                                    text: qsTr("声音输入")
                                    highlighted: true
                                    onClicked: SettingsController.openSoundSettings()
                                    KeyNavigation.tab: saveOnlyButton
                                }
                                Item { Layout.fillWidth: true }
                            }
                        }

                        Rectangle {
                            visible: SettingsController.isRc003Device
                            Layout.fillWidth: true
                            Layout.preferredHeight: visible ? 1 : 0
                            Layout.topMargin: visible ? 6 : 0
                            Layout.bottomMargin: visible ? 6 : 0
                            color: tokens.border
                        }

                        ColumnLayout {
                            id: voiceInputSection
                            objectName: "voiceInputSection"
                            visible: SettingsController.isRc003Device
                            Layout.fillWidth: true
                            spacing: 3

                            UiLabel {
                                tokens: root.tokens
                                kind: sectionTitleKind
                                text: qsTr("语音输入")
                            }

                            RowLayout {
                                Layout.fillWidth: true
                                Layout.preferredHeight: tokens.controlHeight
                                spacing: tokens.spacingSmall
                                UiLabel {
                                    tokens: root.tokens
                                    kind: bodyKind
                                    Layout.preferredWidth: 64
                                    Layout.minimumWidth: 64
                                    text: qsTr("语音程序")
                                }
                                SelectionComboBox {
                                    id: voiceProgramCombo
                                    objectName: "voiceProgramCombo"
                                    tokens: root.tokens
                                    Layout.preferredWidth: 168
                                    Layout.minimumWidth: 168
                                    Layout.maximumWidth: 168
                                    model: SettingsController.voiceProgramOptions
                                    currentIndex: SettingsController.selectedVoiceProgramIndex
                                    onActivated: SettingsController.selectedVoiceProgramIndex = index
                                    Accessible.name: qsTr("语音程序")
                                }
                                UiLabel {
                                    id: voiceProgramStatusLabel
                                    objectName: "voiceProgramStatusLabel"
                                    tokens: root.tokens
                                    kind: noteKind
                                    visible: root.voiceProgramManaged
                                    Layout.fillWidth: true
                                    text: root.voiceProgramStatusSummary()
                                    color: root.voiceProgramStateColor
                                    font.weight: Font.Medium
                                    elide: Text.ElideRight
                                    HoverHandler { id: voiceProgramStatusHover }
                                    ToolTip.visible: voiceProgramStatusHover.hovered
                                    ToolTip.text: SettingsController.voiceProgramStatusText
                                }
                                Item {
                                    visible: !root.voiceProgramManaged
                                    Layout.fillWidth: true
                                }
                            }

                            RowLayout {
                                visible: SettingsController.selectedVoiceProgramIndex === 3
                                Layout.fillWidth: true
                                Layout.preferredHeight: visible ? tokens.controlHeight : 0
                                spacing: tokens.spacingSmall
                                UiLabel {
                                    tokens: root.tokens
                                    kind: bodyKind
                                    Layout.preferredWidth: 64
                                    Layout.minimumWidth: 64
                                    text: qsTr("程序路径")
                                }
                                CompactTextField {
                                    objectName: "voiceProgramCustomPathField"
                                    tokens: root.tokens
                                    Layout.fillWidth: true
                                    readOnly: true
                                    text: SettingsController.voiceProgramCustomPath
                                    placeholderText: qsTr("选择 .exe 或 .lnk")
                                    Accessible.name: qsTr("自定义语音程序路径")
                                }
                                CompactButton {
                                    objectName: "browseVoiceProgramButton"
                                    tokens: root.tokens
                                    compactMinimumWidth: tokens.buttonWidth2Chars
                                    text: qsTr("选择")
                                    onClicked: voiceProgramFileDialog.open()
                                }
                            }

                            RowLayout {
                                Layout.fillWidth: true
                                Layout.preferredHeight: tokens.controlHeight
                                spacing: tokens.spacingSmall
                                UiLabel {
                                    id: voiceHotkeyLabel
                                    tokens: root.tokens
                                    kind: bodyKind
                                    Layout.preferredWidth: 64
                                    Layout.minimumWidth: 64
                                    text: qsTr("语音按键")
                                    HoverHandler { id: voiceHotkeyLabelHover }
                                    ToolTip.visible: voiceHotkeyLabelHover.hovered
                                    ToolTip.text: qsTr("需要与输入法语音唤起键对应")
                                }
                                CompactTextField {
                                    id: voiceHotkeyField
                                    objectName: "holdVoiceHotkeyField"
                                    tokens: root.tokens
                                    Layout.preferredWidth: 168
                                    Layout.minimumWidth: 168
                                    Layout.maximumWidth: 168
                                    readOnly: true
                                    text: root.voiceHotkeyRecording
                                        ? qsTr("请按快捷键")
                                        : SettingsController.holdVoiceHotkeyText
                                    color: root.voiceHotkeyRecording
                                        ? tokens.accent : tokens.textPrimary
                                    placeholderText: qsTr("点击录入")
                                    Accessible.name: qsTr("语音按键，点击后直接录入")
                                    Keys.onEscapePressed: root.stopVoiceHotkeyCapture()
                                    TapHandler { onTapped: root.startVoiceHotkeyCapture() }
                                    HoverHandler { id: voiceHotkeyHover }
                                    ToolTip.visible: voiceHotkeyHover.hovered
                                    ToolTip.text: root.voiceHotkeyCaptureError.length > 0
                                        ? root.voiceHotkeyCaptureError
                                        : qsTr("点击后按下快捷键")
                                }
                                Item { Layout.fillWidth: true }
                            }

                            RowLayout {
                                Layout.fillWidth: true
                                Layout.preferredHeight: tokens.controlHeight
                                spacing: tokens.spacingSmall
                                UiLabel {
                                    tokens: root.tokens
                                    kind: bodyKind
                                    Layout.preferredWidth: 64
                                    Layout.minimumWidth: 64
                                    text: qsTr("程序启动")
                                }
                                UiLabel {
                                    objectName: "voiceProgramLaunchText"
                                    tokens: root.tokens
                                    kind: noteKind
                                    Layout.fillWidth: true
                                    text: root.voiceProgramSystemManaged
                                        ? qsTr("由 Windows 管理，无需本程序启动。")
                                        : root.voiceProgramManaged
                                            ? qsTr("随桥接启动；失败不影响桥接。")
                                            : qsTr("不管理时仅发送语音按键，不启动外部程序。")
                                    maximumLineCount: 1
                                    elide: Text.ElideRight
                                }
                                CheckBox {
                                    id: voiceProgramElevatedCheckBox
                                    objectName: "voiceProgramElevatedCheckBox"
                                    visible: !root.voiceProgramSystemManaged
                                    implicitHeight: tokens.controlHeight
                                    enabled: root.voiceProgramManaged
                                    text: qsTr("管理员启动")
                                    font.family: tokens.fontFamily
                                    font.pixelSize: tokens.fontSizeSmall
                                    checked: SettingsController.voiceProgramLaunchElevated
                                    onClicked: SettingsController.voiceProgramLaunchElevated = checked
                                    indicator: Rectangle {
                                        implicitWidth: 14
                                        implicitHeight: 14
                                        x: 0
                                        y: (voiceProgramElevatedCheckBox.height - height) / 2
                                        radius: 3
                                        color: voiceProgramElevatedCheckBox.checked
                                            ? tokens.accent : tokens.fieldBackground
                                        border.width: 1
                                        border.color: voiceProgramElevatedCheckBox.checked
                                            ? tokens.accent : tokens.borderStrong

                                        UiLabel {
                                            anchors.centerIn: parent
                                            visible: voiceProgramElevatedCheckBox.checked
                                            tokens: root.tokens
                                            kind: noteKind
                                            text: "✓"
                                            color: tokens.accentText
                                            font.pixelSize: 10
                                            font.weight: Font.DemiBold
                                        }
                                    }
                                    contentItem: UiLabel {
                                        tokens: root.tokens
                                        kind: bodyKind
                                        text: voiceProgramElevatedCheckBox.text
                                        font.pixelSize: tokens.fontSizeSmall
                                        color: voiceProgramElevatedCheckBox.enabled
                                            ? tokens.textPrimary : tokens.disabledText
                                        leftPadding: voiceProgramElevatedCheckBox.indicator.width + 5
                                        verticalAlignment: Text.AlignVCenter
                                    }
                                    HoverHandler { id: elevatedHover }
                                    ToolTip.visible: elevatedHover.hovered
                                    ToolTip.text: qsTr("只提升语音程序；取消管理员确认不影响桥接")
                                }
                            }
                        }

                        Rectangle {
                            visible: SettingsController.isRc003Device
                            Layout.fillWidth: true
                            Layout.preferredHeight: visible ? 1 : 0
                            Layout.topMargin: visible ? 6 : 0
                            Layout.bottomMargin: visible ? 6 : 0
                            color: tokens.border
                        }

                        ColumnLayout {
                            id: bridgeSection
                            objectName: "bridgeSection"
                            visible: SettingsController.isRc003Device
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            spacing: 5

                            UiLabel {
                                tokens: root.tokens
                                kind: sectionTitleKind
                                text: qsTr("保存与启动")
                            }
                            RowLayout {
                                id: bridgeLaunchProgress
                                objectName: "bridgeLaunchProgress"
                                visible: root.bridgeLaunchInProgress
                                    || SettingsController.bridgeLaunchPhase === "connected"
                                    || SettingsController.bridgeLaunchPhase === "failed"
                                    || SettingsController.bridgeLaunchPhase === "unknown"
                                Layout.fillWidth: true
                                Layout.preferredHeight: visible ? 20 : 0
                                Layout.maximumHeight: visible ? 20 : 0
                                spacing: tokens.spacingSmall

                                BusyIndicator {
                                    id: bridgeLaunchBusyIndicator
                                    objectName: "bridgeLaunchBusyIndicator"
                                    Layout.preferredWidth: 16
                                    Layout.preferredHeight: 16
                                    visible: running
                                    running: root.bridgeLaunchInProgress
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
                                                ? qsTr("保存设置 ✓ → 桥接已启动 ✓ → 等待 RC003 连接…")
                                                : SettingsController.bridgeLaunchPhase === "connected"
                                                    ? qsTr("保存设置 ✓ → 桥接已启动 ✓ → RC003 已连接 ✓")
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
                                    visible: SettingsController.bridgeLaunchBusy
                                        || (SettingsController.bridgeLaunchPhase === "waiting"
                                            && SettingsController.bridgeLaunchElapsedSeconds > 0)
                                    text: qsTr("%1 秒").arg(
                                        SettingsController.bridgeLaunchElapsedSeconds
                                    )
                                }
                            }
                            UiLabel {
                                id: launchStatusText
                                objectName: "launchStatusText"
                                visible: false
                                Layout.preferredHeight: 0
                                tokens: root.tokens
                                kind: noteKind
                                text: SettingsController.launchStatusText
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
                                    text: SettingsController.isRc003Device
                                        ? qsTr("仅保存设置") : qsTr("保存设备选择")
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
                            UiLabel {
                                objectName: "bridgeNotRunningWarning"
                                tokens: root.tokens
                                kind: noteKind
                                Layout.fillWidth: true
                                text: SettingsController.bridgeLaunchPhase === "unknown"
                                    ? qsTr("桥接状态无法确认，请检查后再测试。")
                                    : SettingsController.bridgeRunning
                                        ? qsTr("桥接已运行；设备、按键、声音和文字输入仍需实测。")
                                        : qsTr("桥接未运行，语音键无效；请保存并启动后再测试。")
                                maximumLineCount: 1
                                elide: Text.ElideRight
                                Accessible.description: SettingsController.launchStatusText
                                HoverHandler { id: launchStatusHover }
                                ToolTip.visible: launchStatusHover.hovered
                                ToolTip.text: SettingsController.launchStatusText
                            }
                        }
                    }
                }
            }
        }
    }
}
