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
    readonly property bool windowsDictationSelected:
        SettingsController.selectedVoiceProgramIndex === 3
    readonly property bool customProgramSelected:
        SettingsController.selectedVoiceProgramIndex === 4
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
                ? (voiceProgramNeedsAttention
                    ? tokens.voiceAccent : tokens.successColor)
                : SettingsController.voiceProgramStatusCode === "disabled"
                    ? tokens.accent : tokens.voiceAccent

    function checkResult(checkId) {
        const rows = DiagnosticsController.checkResults
        for (var i = 0; i < rows.length; i++) {
            if (String(rows[i].checkId) === checkId)
                return rows[i]
        }
        return null
    }

    function checkState(checkId) {
        if (DiagnosticsController.isRefreshing)
            return qsTr("检查中")
        const row = checkResult(checkId)
        if (!row)
            return qsTr("未检查")
        if (row.status === "pass")
            return qsTr("正常")
        if (row.status === "manual")
            return qsTr("待实测")
        return qsTr("需处理")
    }

    function checkColor(checkId) {
        const state = checkState(checkId)
        if (state === qsTr("正常"))
            return tokens.successColor
        if (state === qsTr("需处理"))
            return tokens.errorColor
        if (state === qsTr("检查中") || state === qsTr("待实测"))
            return tokens.voiceAccent
        return tokens.disabledText
    }

    function checkDetail(checkId, fallback) {
        const row = checkResult(checkId)
        return row && String(row.detail).length > 0
            ? String(row.detail) : fallback
    }

    function voiceProgramStatusSummary() {
        const code = SettingsController.voiceProgramStatusCode
        if (!voiceProgramManaged)
            return qsTr("不管理")
        if (windowsDictationSelected)
            return qsTr("Windows 内置")
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
        if (code === "not_found")
            return customProgramSelected
                && SettingsController.voiceProgramCustomPath.length === 0
                ? qsTr("请选择程序") : qsTr("未找到程序")
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

    function applyVoiceSettings() {
        if (SettingsController.selectedEndpointIndex < 0
                && SettingsController.recommendedEndpointIndex >= 0) {
            SettingsController.selectedEndpointIndex =
                SettingsController.recommendedEndpointIndex
        }
        SettingsController.saveSettings()
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

    Dialog {
        id: driverConfirmDialog
        objectName: "driverConfirmDialog"
        title: qsTr("安装虚拟音频？")
        modal: true
        anchors.centerIn: parent
        standardButtons: Dialog.Ok | Dialog.Cancel
        onAccepted: DiagnosticsController.launchVbCableSetup()

        UiLabel {
            tokens: root.tokens
            kind: bodyKind
            width: 360
            wrapMode: Text.WordWrap
            text: qsTr("将启动 VB-Audio 官方 VB-CABLE 安装程序并请求管理员权限。完成安装后需要重启电脑，再回到这里点击“应用”。")
        }
    }

    Dialog {
        id: bridgeTestConfirmDialog
        objectName: "bridgeTestConfirmDialog"
        title: qsTr("临时停止遥控器服务？")
        modal: true
        anchors.centerIn: parent
        standardButtons: Dialog.Ok | Dialog.Cancel
        onAccepted: DiagnosticsController.testVbCableChannelWithBridgeRestart()

        UiLabel {
            tokens: root.tokens
            kind: bodyKind
            width: 360
            wrapMode: Text.WordWrap
            text: qsTr("声音通道测试不能和真实语音同时运行。继续后会临时停止遥控器服务，测试结束再自动恢复。")
        }
    }

    Dialog {
        id: speakTestDialog
        objectName: "speakTestDialog"
        title: qsTr("实际说话")
        modal: true
        anchors.centerIn: parent
        width: Math.min(480, root.width - 36)
        height: 250
        standardButtons: Dialog.Close
        onOpened: Qt.callLater(function() { speakTestInput.forceActiveFocus() })

        ColumnLayout {
            anchors.fill: parent
            spacing: tokens.spacingSmall

            UiLabel {
                tokens: root.tokens
                kind: noteKind
                Layout.fillWidth: true
                text: root.voiceProgramManaged
                    ? qsTr("当前：%1").arg(
                        SettingsController.voiceProgramOptions[
                            SettingsController.selectedVoiceProgramIndex
                        ]
                    )
                    : qsTr("当前：不管理语音程序")
                elide: Text.ElideRight
            }

            ScrollView {
                Layout.fillWidth: true
                Layout.fillHeight: true
                TextArea {
                    id: speakTestInput
                    objectName: "speakTestInput"
                    placeholderText: qsTr("识别出的文字会直接输入到这里")
                    wrapMode: TextEdit.Wrap
                    selectByMouse: true
                    font.family: tokens.fontFamily
                    font.pixelSize: tokens.fontSizeBody
                    color: tokens.textPrimary
                    background: Rectangle {
                        color: tokens.fieldBackground
                        border.color: speakTestInput.activeFocus
                            ? tokens.accent : tokens.borderStrong
                        radius: tokens.cornerRadiusControl
                    }
                }
            }

            RowLayout {
                Layout.fillWidth: true
                Item { Layout.fillWidth: true }
                CompactButton {
                    objectName: "clearSpeakTestButton"
                    tokens: root.tokens
                    compactMinimumWidth: tokens.buttonWidth2Chars
                    text: qsTr("清空")
                    onClicked: {
                        speakTestInput.clear()
                        speakTestInput.forceActiveFocus()
                    }
                }
            }
        }
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
        id: voiceScroll
        objectName: "voiceScroll"
        anchors.fill: parent
        clip: true
        contentWidth: availableWidth
        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff

        ColumnLayout {
            id: voicePageContent
            objectName: "voicePageContent"
            width: Math.max(0, voiceScroll.availableWidth
                - tokens.pageHorizontalPadding * 2)
            x: tokens.pageHorizontalPadding
            y: tokens.pageVerticalPadding
            spacing: tokens.spacingSmall

            SectionFrame {
                objectName: "audioPrerequisiteSection"
                tokens: root.tokens
                Layout.fillWidth: true
                horizontalPadding: 0
                verticalPadding: 4
                contentSpacing: 0

                UiLabel {
                    tokens: root.tokens
                    kind: sectionTitleKind
                    Layout.leftMargin: 10
                    text: qsTr("音频前置")
                }

                InlineSettingsRow {
                    objectName: "virtualAudioRow"
                    tokens: root.tokens
                    titleText: qsTr("虚拟音频")
                    descriptionText: root.checkDetail(
                        "vb_cable_endpoints",
                        qsTr("把遥控器声音送给语音程序")
                    )
                    stateText: root.checkState("vb_cable_endpoints")
                    stateColor: root.checkColor("vb_cable_endpoints")

                    CompactButton {
                        objectName: "installVirtualAudioButton"
                        tokens: root.tokens
                        compactMinimumWidth: tokens.buttonWidth6Chars
                        text: qsTr("安装虚拟音频")
                        enabled: !DiagnosticsController.vbCableTestRunning
                        onClicked: driverConfirmDialog.open()
                    }
                }

                InlineSettingsRow {
                    objectName: "outputEndpointRow"
                    tokens: root.tokens
                    titleText: qsTr("输出端点")
                    descriptionText: ""
                    stateText: root.checkState("output_endpoint")
                    stateColor: root.checkColor("output_endpoint")

                    SelectionComboBox {
                        id: endpointCombo
                        objectName: "endpointCombo"
                        tokens: root.tokens
                        recommendedIndex: SettingsController.recommendedEndpointIndex
                        Layout.preferredWidth: 220
                        Layout.minimumWidth: 180
                        model: SettingsController.endpointOptions
                        currentIndex: SettingsController.selectedEndpointIndex
                        onActivated: SettingsController.selectedEndpointIndex = index
                        enabled: !DiagnosticsController.vbCableTestRunning
                        Accessible.name: qsTr("输出端点")
                    }
                    CompactButton {
                        objectName: "applyVoiceSettingsButton"
                        tokens: root.tokens
                        compactMinimumWidth: tokens.buttonWidth2Chars
                        text: qsTr("应用")
                        highlighted: true
                        enabled: !DiagnosticsController.vbCableTestRunning
                            && !SettingsController.bridgeLaunchBusy
                        onClicked: root.applyVoiceSettings()
                    }
                }

                InlineSettingsRow {
                    objectName: "targetApplicationRow"
                    tokens: root.tokens
                    titleText: qsTr("目标应用")
                    descriptionText: qsTr("麦克风输入请选择 CABLE Output")
                    stateText: root.checkState("dictation")
                    stateColor: root.checkColor("dictation")
                    showDivider: false

                    CompactButton {
                        objectName: "openMicrophonePrivacyButton"
                        tokens: root.tokens
                        compactMinimumWidth: tokens.buttonWidth4Chars
                        text: qsTr("麦克风隐私")
                        onClicked: SettingsController.openMicrophonePrivacySettings()
                    }
                    CompactButton {
                        objectName: "openSoundInputSettingsButton"
                        tokens: root.tokens
                        compactMinimumWidth: tokens.buttonWidth4Chars
                        text: qsTr("声音输入")
                        onClicked: SettingsController.openSoundSettings()
                    }
                }
            }

            SectionFrame {
                objectName: "voiceProgramSection"
                tokens: root.tokens
                Layout.fillWidth: true
                horizontalPadding: 0
                verticalPadding: 4
                contentSpacing: 0

                UiLabel {
                    tokens: root.tokens
                    kind: sectionTitleKind
                    Layout.leftMargin: 10
                    text: qsTr("语音程序")
                }

                InlineSettingsRow {
                    objectName: "voiceProgramSelectionRow"
                    tokens: root.tokens
                    titleText: qsTr("选择程序")
                    descriptionObjectName: "voiceProgramStatusLabel"
                    descriptionText: root.voiceProgramStatusSummary()
                    stateText: root.voiceProgramManaged ? qsTr("已选择") : qsTr("不管理")
                    stateColor: root.voiceProgramStateColor

                    SelectionComboBox {
                        id: voiceProgramCombo
                        objectName: "voiceProgramCombo"
                        tokens: root.tokens
                        Layout.preferredWidth: 220
                        Layout.minimumWidth: 180
                        model: SettingsController.voiceProgramOptions
                        currentIndex: SettingsController.selectedVoiceProgramIndex
                        onActivated: SettingsController.selectedVoiceProgramIndex = index
                        Accessible.name: qsTr("语音程序")
                    }
                }

                InlineSettingsRow {
                    objectName: "voiceProgramCustomPathRow"
                    visible: root.customProgramSelected
                    tokens: root.tokens
                    titleText: qsTr("程序路径")
                    descriptionText: ""

                    CompactTextField {
                        objectName: "voiceProgramCustomPathField"
                        tokens: root.tokens
                        Layout.fillWidth: true
                        Layout.minimumWidth: 180
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

                InlineSettingsRow {
                    objectName: "voiceHotkeyRow"
                    tokens: root.tokens
                    titleText: qsTr("语音按键")
                    descriptionText: root.voiceHotkeyCaptureError.length > 0
                        ? root.voiceHotkeyCaptureError
                        : root.windowsDictationSelected
                            ? qsTr("Windows 语音输入建议使用 Win+H")
                            : qsTr("需要与语音程序的唤起键一致")

                    CompactTextField {
                        id: voiceHotkeyField
                        objectName: "holdVoiceHotkeyField"
                        tokens: root.tokens
                        Layout.preferredWidth: 150
                        Layout.minimumWidth: 130
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
                    }
                    CompactButton {
                        objectName: "useWindowsDictationHotkeyButton"
                        visible: root.windowsDictationSelected
                        tokens: root.tokens
                        compactMinimumWidth: tokens.buttonWidth6Chars
                        text: qsTr("使用 Win+H")
                        onClicked: SettingsController.useWindowsDictationHotkey()
                    }
                }

                InlineSettingsRow {
                    objectName: "voiceProgramSpecificRow"
                    tokens: root.tokens
                    titleText: root.windowsDictationSelected
                        ? qsTr("系统设置") : qsTr("程序启动")
                    descriptionObjectName: "voiceProgramLaunchText"
                    descriptionText: root.windowsDictationSelected
                        ? qsTr("由 Windows 提供听写和联机语音识别")
                        : root.voiceProgramSystemManaged
                            ? qsTr("由 Windows 管理，无需本程序启动")
                            : root.voiceProgramManaged
                                ? qsTr("随遥控器服务启动；失败不影响服务")
                                : qsTr("只发送语音按键，不启动外部程序")
                    showDivider: false

                    CompactButton {
                        objectName: "openSpeechSettingsButton"
                        visible: root.windowsDictationSelected
                        tokens: root.tokens
                        compactMinimumWidth: tokens.buttonWidth6Chars
                        text: qsTr("Windows 语音设置")
                        onClicked: SettingsController.openSpeechSettings()
                    }
                    CheckBox {
                        id: voiceProgramElevatedCheckBox
                        objectName: "voiceProgramElevatedCheckBox"
                        visible: !root.windowsDictationSelected
                            && !root.voiceProgramSystemManaged
                        implicitHeight: tokens.controlHeight
                        enabled: root.voiceProgramManaged
                        text: qsTr("管理员启动")
                        font.family: tokens.fontFamily
                        font.pixelSize: tokens.fontSizeSmall
                        checked: SettingsController.voiceProgramLaunchElevated
                        onClicked: SettingsController.voiceProgramLaunchElevated = checked
                    }
                }
            }

            SectionFrame {
                objectName: "voiceTestSection"
                tokens: root.tokens
                Layout.fillWidth: true
                horizontalPadding: 0
                verticalPadding: 4
                contentSpacing: 0

                UiLabel {
                    tokens: root.tokens
                    kind: sectionTitleKind
                    Layout.leftMargin: 10
                    text: qsTr("测试验证")
                }

                InlineSettingsRow {
                    objectName: "soundChannelTestRow"
                    tokens: root.tokens
                    titleText: qsTr("声音通道")
                    descriptionText: DiagnosticsController.vbCableTestMessage.length > 0
                        ? DiagnosticsController.vbCableTestMessage
                        : qsTr("确认 CABLE Input 能到达 CABLE Output")
                    stateText: DiagnosticsController.vbCableTestRunning
                        ? qsTr("测试中")
                        : DiagnosticsController.vbCableTestStatus === "pass"
                            ? qsTr("正常")
                            : DiagnosticsController.vbCableTestStatus === "fail"
                                ? qsTr("未通过") : qsTr("未测试")
                    stateColor: DiagnosticsController.vbCableTestRunning
                        ? tokens.voiceAccent
                        : DiagnosticsController.vbCableTestStatus === "pass"
                            ? tokens.successColor
                            : DiagnosticsController.vbCableTestStatus === "fail"
                                ? tokens.errorColor : tokens.disabledText

                    CompactButton {
                        objectName: "recoverBridgeButton"
                        visible: DiagnosticsController.vbCableBridgeRecoveryNeeded
                        tokens: root.tokens
                        compactMinimumWidth: tokens.buttonWidth4Chars
                        text: qsTr("启动服务")
                        highlighted: true
                        enabled: !SettingsController.bridgeLaunchBusy
                        onClicked: SettingsController.startBridge()
                    }
                    CompactButton {
                        objectName: "testVbCableChannelButton"
                        tokens: root.tokens
                        compactMinimumWidth: tokens.buttonWidth4Chars
                        text: DiagnosticsController.vbCableTestRunning
                            ? qsTr("测试中…") : qsTr("测试通道")
                        enabled: !DiagnosticsController.isRefreshing
                            && !DiagnosticsController.vbCableTestRunning
                            && !SettingsController.bridgeLaunchBusy
                        onClicked: SettingsController.bridgeRunning
                            ? bridgeTestConfirmDialog.open()
                            : DiagnosticsController.testVbCableChannel()
                    }
                }

                InlineSettingsRow {
                    objectName: "actualSpeechTestRow"
                    tokens: root.tokens
                    titleText: qsTr("实际说话")
                    descriptionText: qsTr("打开输入框，由所选语音程序直接输入文字")
                    stateText: qsTr("待实测")
                    stateColor: tokens.voiceAccent
                    showDivider: false

                    CompactButton {
                        objectName: "trySpeakingButton"
                        tokens: root.tokens
                        compactMinimumWidth: tokens.buttonWidth4Chars
                        text: qsTr("试说一句")
                        highlighted: true
                        onClicked: speakTestDialog.open()
                    }
                }
            }

            Item { Layout.preferredHeight: tokens.pageVerticalPadding }
        }
    }
}
