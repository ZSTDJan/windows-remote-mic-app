import QtQuick
import QtQuick.Controls
import QtQuick.Dialogs
import QtQuick.Layouts
import OvbRc003Settings 1.0

Item {
    id: root
    signal openDeviceRequested()
    signal openButtonsRequested()
    property var tokens
    property var backTabTarget: null
    property var tabTarget: null
    readonly property var firstFocusItem: installVirtualAudioButton
    readonly property var lastFocusItem: trySpeakingButton
    property bool voiceHotkeyRecording: false
    property string voiceHotkeyCaptureError: ""
    property string pendingVoiceHotkey: ""
    readonly property int settingsStateColumnWidth: 96
    readonly property int settingsActionColumnWidth: tokens.buttonWidth6Chars
    readonly property real voiceHotkeyEditorWidth:
        Math.max(
            130,
            endpointCombo.width / 2 - tokens.buttonWidth4Chars - tokens.spacingSmall
        )

    readonly property bool voiceProgramManaged:
        SettingsController.voiceProgramManaged
    readonly property bool voiceProgramLaunchable:
        SettingsController.voiceProgramLaunchable
    readonly property bool sogouSelected:
        SettingsController.voiceProgramSogouSelected
    readonly property bool wetypeSelected:
        SettingsController.voiceProgramWeTypeSelected
    readonly property bool doubaoSelected:
        SettingsController.voiceProgramDoubaoSelected
    readonly property bool customProgramSelected:
        SettingsController.voiceProgramCustomSelected
    readonly property bool voiceHotkeyBusy: SettingsController.voiceHotkeyBusy
    readonly property bool voiceHotkeyRefreshVisible:
        root.sogouSelected || root.wetypeSelected || root.doubaoSelected
    readonly property bool voiceHotkeyRuntimeBusy:
        String(SettingsController.voiceRuntimeState) === "active"
        || String(SettingsController.voiceRuntimeState) === "mic_confirmed"
        || String(SettingsController.voiceRuntimeState) === "receiving_audio"
        || String(SettingsController.voiceRuntimeState) === "finishing"
    readonly property bool voiceHotkeyRefreshEnabled:
        root.voiceHotkeyRefreshVisible
        && !root.voiceHotkeyBusy
        && !SettingsController.settingsSaveBusy
        && !root.configurationWriteBusy
        && !root.voiceHotkeyRecording
        && !SettingsController.inputCaptureInUse
        && !root.voiceHotkeyRuntimeBusy
    readonly property bool endpointPreflightBusy:
        SettingsController.endpointPreflightBusy
    readonly property bool configurationWriteBusy:
        DiagnosticsController.driverActionRunning
        || DiagnosticsController.vbCableTestRunning
        || SettingsController.bridgeLaunchBusy
        || root.endpointPreflightBusy
    readonly property bool voiceProgramPrivilegeMismatch:
        voiceProgramLaunchable
        && SettingsController.voiceProgramStatusCode === "running"
        && SettingsController.voiceProgramElevationStatus !== "unknown"
        && SettingsController.voiceProgramLaunchElevated
            !== (SettingsController.voiceProgramElevationStatus === "elevated")
    readonly property color voiceProgramStateColor:
        SettingsController.voiceProgramSettingsDirty
            ? tokens.voiceAccent
            : SettingsController.voiceProgramStatusCode === "running"
                ? (voiceProgramPrivilegeMismatch
                    ? tokens.voiceAccent : tokens.successColor)
                : SettingsController.voiceProgramStatusCode === "not_found"
                    ? tokens.errorColor : tokens.disabledText

    function voiceProgramStateText() {
        if (SettingsController.voiceProgramSettingsDirty)
            return qsTr("正在保存")
        const code = String(SettingsController.voiceProgramStatusCode)
        if (!root.voiceProgramManaged)
            return qsTr("不管理")
        if (code === "running") {
            if (root.voiceProgramPrivilegeMismatch)
                return qsTr("退出后点“重新检测”")
            return qsTr("运行中")
        }
        if (code === "stopped")
            return qsTr("已安装")
        if (code === "not_found") {
            if (root.customProgramSelected)
                return qsTr("点击“选择”")
            if (root.sogouSelected)
                return qsTr("点击“去安装”")
            return qsTr("点击“打开设置”")
        }
        return qsTr("正在检查")
    }

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
            return qsTr("正在检查")
        const row = checkResult(checkId)
        if (!row)
            return qsTr("未检查")
        if (row.status === "pass")
            return qsTr("正常")
        if (row.status === "manual")
            return qsTr("请手动验证")
        if (checkId === "vb_cable_endpoints")
            return String(row.detail || "").indexOf(qsTr("缺少")) >= 0
                ? qsTr("点击“安装音频”") : qsTr("点击“重新检查”")
        if (checkId === "output_endpoint")
            return qsTr("请重新选择端点")
        return qsTr("点击“重新检查”")
    }

    function checkColor(checkId) {
        const state = checkState(checkId)
        if (state === qsTr("正常"))
            return tokens.successColor
        if (state === qsTr("点击“安装音频”")
                || state === qsTr("点击“重新检查”")
                || state === qsTr("请重新选择端点"))
            return tokens.errorColor
        if (state === qsTr("正在检查") || state === qsTr("请手动验证"))
            return tokens.voiceAccent
        return tokens.disabledText
    }

    function checkDetail(checkId, fallback) {
        const row = checkResult(checkId)
        return row && String(row.detail).length > 0
            ? String(row.detail).trim().replace(/[。；;]+$/, "") : fallback
    }

    function startVoiceHotkeyCapture() {
        if (voiceHotkeyRecording) {
            stopVoiceHotkeyCapture("capture_field_tapped")
            return
        }
        voiceHotkeyCaptureError = ""
        pendingVoiceHotkey = ""
        if (!SettingsController.startHotkeyCapture()) {
            voiceHotkeyCaptureError = qsTr("无法开始录入，请结束其它按键操作后重试")
            return
        }
        voiceHotkeyRecording = true
        voiceHotkeyField.forceActiveFocus()
    }

    function stopVoiceHotkeyCapture(reason) {
        pendingVoiceHotkey = ""
        if (!voiceHotkeyRecording)
            return true
        SettingsController.reportHotkeyCaptureUiStop(reason || "unknown")
        if (!SettingsController.stopHotkeyCapture()) {
            voiceHotkeyCaptureError = qsTr("无法停止快捷键录入，请重试")
            return false
        }
        if (!SettingsController.hotkeyCaptureActive)
            voiceHotkeyRecording = false
        return true
    }

    function settleInputUiAfterStop() {
        if (!SettingsController.hotkeyCaptureActive) {
            voiceHotkeyRecording = false
            pendingVoiceHotkey = ""
        }
    }

    function finishCapturedVoiceHotkey(chord) {
        pendingVoiceHotkey = chord
        if (!SettingsController.stopHotkeyCapture()) {
            pendingVoiceHotkey = ""
            voiceHotkeyCaptureError = qsTr("无法停止快捷键录入，请重试")
            return
        }
        if (!SettingsController.hotkeyCaptureActive)
            commitCapturedVoiceHotkey()
    }

    function commitCapturedVoiceHotkey() {
        if (pendingVoiceHotkey.length === 0)
            return
        const chord = pendingVoiceHotkey
        pendingVoiceHotkey = ""
        voiceHotkeyRecording = false
        SettingsController.holdVoiceHotkeyText = chord
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

    SettingsDialog {
        id: driverConfirmDialog
        tokens: root.tokens
        preferredWidth: 390
        objectName: "driverConfirmDialog"
        title: qsTr("安装虚拟音频？")
        modal: true
        standardButtons: Dialog.Ok | Dialog.Cancel
        onAccepted: DiagnosticsController.launchVbCableSetup()

        contentItem: UiLabel {
            tokens: root.tokens
            kind: bodyKind
            wrapMode: Text.WordWrap
            text: qsTr("将启动 VB-Audio 官方 VB-CABLE 安装程序并请求管理员权限。完成安装后需要重启电脑，再回到这里点击“选推荐端点”。")
        }
    }

    SettingsDialog {
        id: bridgeTestConfirmDialog
        tokens: root.tokens
        preferredWidth: 390
        objectName: "bridgeTestConfirmDialog"
        title: qsTr("临时停止遥控器服务？")
        modal: true
        standardButtons: Dialog.Ok | Dialog.Cancel
        onAccepted: DiagnosticsController.testVbCableChannelWithBridgeRestart()

        contentItem: UiLabel {
            tokens: root.tokens
            kind: bodyKind
            wrapMode: Text.WordWrap
            text: qsTr("检查虚拟声卡时会发送一小段测试音，并临时停止遥控器服务，结束后自动恢复。此检查不测试语音识别，平时说话不用先点它。")
        }
    }

    SettingsDialog {
        id: speakTestDialog
        objectName: "speakTestDialog"
        tokens: root.tokens
        preferredWidth: 480
        height: Math.min(300, parent.height - 32)
        title: qsTr("试说一句")
        closeButtonObjectName: "speakTestCloseButton"
        onOpened: Qt.callLater(function() { speakTestInput.forceActiveFocus() })

        contentItem: ColumnLayout {
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

            UiLabel {
                objectName: "actualSpeechInstruction"
                tokens: root.tokens
                kind: noteKind
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                text: qsTr("点击输入框，按住遥控器话筒键说话，松开后看文字有没有进来。")
            }

            ScrollView {
                id: speakTestInputFrame
                objectName: "speakTestInputFrame"
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.minimumHeight: 150
                TextArea {
                    id: speakTestInput
                    objectName: "speakTestInput"
                    placeholderText: qsTr("看看说的话有没有变成文字")
                    wrapMode: TextEdit.Wrap
                    selectByMouse: true
                    font.family: tokens.fontFamily
                    font.pixelSize: tokens.fontSizeBody
                    color: tokens.textPrimary
                    placeholderTextColor: tokens.disabledText
                    background: Rectangle {
                        color: tokens.fieldBackground
                        border.width: tokens.hairlineWidth
                        border.color: speakTestInput.activeFocus
                            ? tokens.accent : tokens.border
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
            root.finishCapturedVoiceHotkey(chord)
        }
        function onHotkeyCaptureError(message) {
            root.pendingVoiceHotkey = ""
            if (!SettingsController.hotkeyCaptureActive)
                root.voiceHotkeyRecording = false
            root.voiceHotkeyCaptureError = message
        }
        function onHotkeyCaptureActiveChanged() {
            if (root.voiceHotkeyRecording
                    && !SettingsController.hotkeyCaptureActive) {
                if (root.pendingVoiceHotkey.length > 0)
                    root.commitCapturedVoiceHotkey()
                else
                    root.voiceHotkeyRecording = false
            }
        }
    }

    onVisibleChanged: {
        if (visible) {
            SettingsController.refreshVoiceProgramStatus()
            SettingsController.refreshVoiceProgramOptions()
            SettingsController.loadVoiceHotkeyFromProvider()
        } else {
            stopVoiceHotkeyCapture("page_hidden")
        }
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

                SettingsSectionTitle {
                    objectName: "audioPrerequisiteSectionTitle"
                    tokens: root.tokens
                    text: qsTr("音频前置")
                }

                InlineSettingsRow {
                    objectName: "virtualAudioRow"
                    tokens: root.tokens
                    stateColumnWidth: root.settingsStateColumnWidth
                    actionColumnWidth: root.settingsActionColumnWidth
                    titleText: qsTr("虚拟音频")
                    descriptionText: DiagnosticsController.driverErrorMessage.length > 0
                        ? DiagnosticsController.driverErrorMessage
                        : DiagnosticsController.driverStatusMessage.length > 0
                            ? DiagnosticsController.driverStatusMessage
                            : DiagnosticsController.driverInfoMessage.length > 0
                                ? DiagnosticsController.driverInfoMessage
                                : root.checkDetail(
                                    "vb_cable_endpoints",
                                    qsTr("检测并选择 CABLE Input")
                                )
                    stateText: DiagnosticsController.driverErrorMessage.length > 0
                        ? qsTr("点击“安装音频”")
                        : DiagnosticsController.driverStatusMessage.length > 0
                            ? qsTr("正常")
                            : DiagnosticsController.driverInfoMessage.length > 0
                                ? (DiagnosticsController.driverInfoMessage.indexOf(
                                    qsTr("正在")) >= 0
                                    ? qsTr("正在处理")
                                    : DiagnosticsController.driverInfoMessage.indexOf(
                                        qsTr("重启电脑")) >= 0
                                        ? qsTr("请重启电脑")
                                        : qsTr("点击“安装音频”"))
                                : root.checkState("vb_cable_endpoints")
                    stateColor: DiagnosticsController.driverErrorMessage.length > 0
                        ? tokens.errorColor
                        : DiagnosticsController.driverStatusMessage.length > 0
                            ? tokens.successColor
                            : DiagnosticsController.driverInfoMessage.length > 0
                                ? tokens.voiceAccent
                                : root.checkColor("vb_cable_endpoints")

                    editorData: [
                        CompactButton {
                            id: installVirtualAudioButton
                            objectName: "installVirtualAudioButton"
                            tokens: root.tokens
                            Layout.fillWidth: true
                            text: qsTr("安装音频")
                            enabled: !root.configurationWriteBusy
                                && !root.voiceHotkeyBusy
                            onClicked: driverConfirmDialog.open()
                            KeyNavigation.backtab: root.backTabTarget
                        }
                    ]
                    CompactButton {
                        objectName: "applyVirtualAudioButton"
                        tokens: root.tokens
                        Layout.fillWidth: true
                        text: qsTr("选推荐端点")
                        highlighted: true
                        visible: SettingsController.recommendedEndpointIndex >= 0
                            && SettingsController.selectedEndpointIndex
                                !== SettingsController.recommendedEndpointIndex
                        enabled: !root.configurationWriteBusy
                            && !root.voiceHotkeyBusy
                        onClicked: SettingsController.selectAndPersistOutputEndpointIndex(
                            SettingsController.recommendedEndpointIndex
                        )
                    }
                }

                InlineSettingsRow {
                    objectName: "outputEndpointRow"
                    tokens: root.tokens
                    stateColumnWidth: root.settingsStateColumnWidth
                    actionColumnWidth: root.settingsActionColumnWidth
                    titleText: qsTr("输出端点")
                    descriptionText: ""
                    stateText: root.checkState("output_endpoint")
                    stateColor: root.checkColor("output_endpoint")

                    editorData: [
                        SelectionComboBox {
                            id: endpointCombo
                            objectName: "endpointCombo"
                            tokens: root.tokens
                            recommendedIndex: SettingsController.recommendedEndpointIndex
                            Layout.fillWidth: true
                            Layout.minimumWidth: 180
                            model: SettingsController.endpointOptions
                            currentIndex: SettingsController.selectedEndpointIndex
                            onActivated: SettingsController.selectAndPersistOutputEndpointIndex(index)
                            enabled: !root.configurationWriteBusy
                                && !root.voiceHotkeyBusy
                            Accessible.name: qsTr("输出端点")
                        }
                    ]
                }

                InlineSettingsRow {
                    objectName: "microphonePrivacyRow"
                    tokens: root.tokens
                    actionColumnWidth: root.settingsActionColumnWidth
                    titleText: qsTr("麦克风权限")
                    descriptionText: qsTr("确认桌面语音软件可以访问麦克风")
                    showDivider: false

                    CompactButton {
                        objectName: "openMicrophonePrivacyButton"
                        tokens: root.tokens
                        Layout.fillWidth: true
                        text: qsTr("麦克风隐私")
                        onClicked: SettingsController.openMicrophonePrivacySettings()
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

                SettingsSectionTitle {
                    objectName: "voiceProgramSectionTitle"
                    tokens: root.tokens
                    text: qsTr("语音程序")
                }

                InlineSettingsRow {
                    objectName: "voiceProgramSelectionRow"
                    tokens: root.tokens
                    stateColumnWidth: root.settingsStateColumnWidth
                    actionColumnWidth: root.settingsActionColumnWidth
                    titleText: qsTr("选择程序")
                    descriptionText: ""
                    stateText: root.voiceProgramStateText()
                    stateColor: root.voiceProgramStateColor

                    editorData: [
                        SelectionComboBox {
                            id: voiceProgramCombo
                            objectName: "voiceProgramCombo"
                            tokens: root.tokens
                            Layout.fillWidth: true
                            Layout.minimumWidth: 130
                            model: SettingsController.voiceProgramOptions
                            currentIndex: SettingsController.selectedVoiceProgramIndex
                            enabled: !root.voiceHotkeyBusy
                                && !root.configurationWriteBusy
                            onActivated: SettingsController.selectedVoiceProgramIndex = index
                            Accessible.name: qsTr("语音程序")
                        },
                        CheckBox {
                            id: voiceProgramElevatedCheckBox
                            objectName: "voiceProgramElevatedCheckBox"
                            visible: !root.voiceProgramManaged
                                || root.voiceProgramLaunchable
                            implicitHeight: tokens.controlHeight
                            Layout.preferredWidth: root.settingsActionColumnWidth
                            Layout.minimumWidth: root.settingsActionColumnWidth
                            Layout.maximumWidth: root.settingsActionColumnWidth
                            leftPadding: 0
                            rightPadding: 0
                            spacing: tokens.spacingSmall
                            indicator.width: 16
                            indicator.height: 16
                            enabled: root.voiceProgramManaged
                                && !root.voiceHotkeyBusy
                                && !root.configurationWriteBusy
                            text: qsTr("管理员启动")
                            font.family: tokens.fontFamily
                            font.pixelSize: tokens.fontSizeSmall
                            checked: SettingsController.voiceProgramLaunchElevated
                            onClicked: SettingsController.voiceProgramLaunchElevated = checked
                        }
                    ]
                    CompactButton {
                        objectName: "openVoiceProgramSettingsButton"
                        visible: root.wetypeSelected
                            || root.doubaoSelected
                            || (root.sogouSelected
                                && SettingsController.voiceProgramStatusCode === "not_found")
                        tokens: root.tokens
                        Layout.fillWidth: true
                        text: root.sogouSelected
                            && SettingsController.voiceProgramStatusCode === "not_found"
                            ? qsTr("去安装") : qsTr("打开设置")
                        enabled: !root.voiceHotkeyBusy
                        onClicked: SettingsController.openVoiceProgramSettings()
                    }
                }

                InlineSettingsRow {
                    objectName: "voiceProgramCustomPathRow"
                    visible: root.customProgramSelected
                    tokens: root.tokens
                    stateColumnWidth: root.settingsStateColumnWidth
                    actionColumnWidth: root.settingsActionColumnWidth
                    titleText: qsTr("程序路径")
                    descriptionText: ""

                    editorData: [
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
                    ]
                        CompactButton {
                            objectName: "browseVoiceProgramButton"
                            tokens: root.tokens
                            Layout.fillWidth: true
                            text: qsTr("选择")
                            enabled: !root.voiceHotkeyBusy
                                && !root.configurationWriteBusy
                            onClicked: voiceProgramFileDialog.open()
                        }
                }

                InlineSettingsRow {
                    objectName: "voiceHotkeyRow"
                    tokens: root.tokens
                    showDivider: false
                    editorColumnWidth: root.voiceHotkeyEditorWidth
                    stateColumnWidth: root.settingsStateColumnWidth
                    actionColumnWidth: root.settingsActionColumnWidth
                    titleText: qsTr("语音按键")
                    descriptionText: root.voiceHotkeyCaptureError.length > 0
                        ? root.voiceHotkeyCaptureError
                        : root.wetypeSelected
                        ? qsTr("点击刷新会打开微信设置读取，也可手动录入")
                        : root.doubaoSelected
                        ? qsTr("自动读取豆包输入法的按住型快捷键，也可手动录入")
                        : root.sogouSelected
                        ? qsTr("自动读取搜狗语音的按住型快捷键，也可手动录入")
                        : qsTr("仅支持录入“按住型”快捷键")
                    stateText: root.voiceHotkeyRecording
                        ? SettingsController.hotkeyCaptureReady
                            ? qsTr("录入中") : qsTr("准备中")
                        : root.voiceHotkeyBusy ? qsTr("正在处理")
                            : root.voiceHotkeyCaptureError.length > 0
                                || SettingsController.voiceHotkeySaveState === "retry"
                                ? qsTr("点击手动录入")
                                : SettingsController.voiceHotkeySource === "manual"
                                    ? qsTr("手动录入")
                                    : SettingsController.voiceHotkeySource === "auto"
                                        ? qsTr("自动识别") : qsTr("已保存")
                    stateColor: root.voiceHotkeyRecording || root.voiceHotkeyBusy
                        ? tokens.voiceAccent
                        : root.voiceHotkeyCaptureError.length > 0
                            || SettingsController.voiceHotkeySaveState === "retry"
                            ? tokens.errorColor : tokens.successColor

                    editorData: [
                        Item {
                            Layout.fillWidth: true
                            Layout.minimumWidth: 130
                            implicitHeight: tokens.controlHeight

                            CompactTextField {
                                id: voiceHotkeyField
                                objectName: "holdVoiceHotkeyField"
                                anchors.fill: parent
                                tokens: root.tokens
                                rightPadding: root.voiceHotkeyRefreshVisible ? 34 : 7
                                readOnly: true
                                background.visible: root.voiceHotkeyRecording
                                    || SettingsController.voiceHotkeySource === "manual"
                                    || SettingsController.voiceHotkeySaveState === "processing"
                                rightInset: root.voiceHotkeyRefreshVisible
                                    ? refreshVoiceHotkeyButton.width + tokens.spacingTiny : 0
                                activeFocusOnPress: root.voiceHotkeyRecording
                                activeFocusOnTab: root.voiceHotkeyRecording
                                selectByMouse: false
                                enabled: !root.voiceHotkeyBusy
                                    && !root.configurationWriteBusy
                                text: root.voiceHotkeyRecording
                                    ? SettingsController.hotkeyCaptureReady
                                        ? qsTr("请按快捷键") : qsTr("正在准备…")
                                    : SettingsController.holdVoiceHotkeyText
                                color: root.voiceHotkeyRecording
                                    ? tokens.accent : tokens.textPrimary
                                placeholderText: qsTr("尚未录入")
                                Accessible.name: qsTr("语音按键，仅支持录入按住型快捷键")
                                Keys.onEscapePressed: root.stopVoiceHotkeyCapture(
                                    "escape_pressed")
                                onActiveFocusChanged: {
                                    if (!activeFocus && root.voiceHotkeyRecording)
                                        root.stopVoiceHotkeyCapture("focus_lost")
                                }
                            }
                            CompactButton {
                                id: refreshVoiceHotkeyButton
                                objectName: "refreshVoiceHotkeyButton"
                                visible: root.voiceHotkeyRefreshVisible
                                anchors.right: parent.right
                                anchors.verticalCenter: parent.verticalCenter
                                width: 28
                                height: parent.height
                                tokens: root.tokens
                                flat: true
                                enabled: root.voiceHotkeyRefreshEnabled
                                Accessible.name: qsTr("重新读取语音按键")
                                onClicked: SettingsController.refreshVoiceHotkeyFromProvider()
                                contentItem: IconGlyph {
                                    tokens: root.tokens
                                    glyph: "\uE72C"
                                    glyphSize: 14
                                    color: !refreshVoiceHotkeyButton.enabled
                                        ? tokens.disabledText
                                        : refreshVoiceHotkeyButton.hovered
                                            ? tokens.accent : tokens.textSecondary
                                }
                                HoverHandler { id: refreshVoiceHotkeyHover }
                                CompactToolTip {
                                    tokens: root.tokens
                                    active: refreshVoiceHotkeyHover.hovered
                                    text: root.wetypeSelected
                                        ? qsTr("打开微信设置并读取按住说话快捷键；失败保留原值")
                                        : qsTr("重新读取输入法当前的按住型快捷键")
                                }
                            }
                        }
                    ]
                    CompactButton {
                        id: recordVoiceHotkeyButton
                        objectName: "recordVoiceHotkeyButton"
                        tokens: root.tokens
                        Layout.fillWidth: true
                        compactMinimumWidth: tokens.buttonWidth4Chars
                        text: root.voiceHotkeyRecording
                            ? qsTr("停止") : qsTr("手动录入")
                        enabled: !root.voiceHotkeyBusy
                            && !root.configurationWriteBusy
                        onClicked: root.startVoiceHotkeyCapture()
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

                SettingsSectionTitle {
                    objectName: "voiceTestSectionTitle"
                    tokens: root.tokens
                    text: qsTr("测试验证")
                }

                InlineSettingsRow {
                    objectName: "soundChannelTestRow"
                    tokens: root.tokens
                    stateColumnWidth: root.settingsStateColumnWidth
                    actionColumnWidth: root.settingsActionColumnWidth
                    editorColumnVisible: DiagnosticsController.vbCableBridgeRecoveryNeeded
                    titleText: qsTr("虚拟声卡")
                    descriptionText: DiagnosticsController.vbCableTestMessage.length > 0
                        ? DiagnosticsController.vbCableTestMessage
                        : qsTr("检查声音能否通过虚拟声卡；不测试语音识别")
                    descriptionObjectName: "soundChannelTestDescription"
                    stateText: DiagnosticsController.vbCableBridgeRecoveryNeeded
                        ? qsTr("点击“启动服务”")
                        : DiagnosticsController.vbCableTestRunning
                        ? qsTr("测试中")
                        : DiagnosticsController.vbCableTestStatus === "pass"
                            ? qsTr("正常")
                            : DiagnosticsController.vbCableTestStatus === "fail"
                                ? qsTr("检查未通过") : qsTr("按需检查")
                    stateColor: DiagnosticsController.vbCableBridgeRecoveryNeeded
                        ? tokens.errorColor
                        : DiagnosticsController.vbCableTestRunning
                        ? tokens.voiceAccent
                        : DiagnosticsController.vbCableTestStatus === "pass"
                            ? tokens.successColor
                            : DiagnosticsController.vbCableTestStatus === "fail"
                                ? tokens.errorColor : tokens.disabledText

                    editorData: [
                        CompactButton {
                            objectName: "recoverBridgeButton"
                            visible: DiagnosticsController.vbCableBridgeRecoveryNeeded
                            tokens: root.tokens
                            compactMinimumWidth: tokens.buttonWidth4Chars
                            text: qsTr("启动服务")
                            highlighted: true
                            enabled: !root.configurationWriteBusy
                                && !root.voiceHotkeyBusy
                            onClicked: SettingsController.startBridge()
                        }
                    ]
                    CompactButton {
                        objectName: "testVbCableChannelButton"
                        tokens: root.tokens
                        Layout.fillWidth: true
                        text: DiagnosticsController.vbCableTestRunning
                            ? qsTr("检查中…") : qsTr("检查虚拟声卡")
                        enabled: !DiagnosticsController.isRefreshing
                            && !root.configurationWriteBusy
                            && !root.voiceHotkeyBusy
                        onClicked: SettingsController.bridgeRunning
                            ? bridgeTestConfirmDialog.open()
                            : DiagnosticsController.testVbCableChannel()
                    }
                }

                InlineSettingsRow {
                    objectName: "actualSpeechTestRow"
                    tokens: root.tokens
                    stateColumnWidth: root.settingsStateColumnWidth
                    actionColumnWidth: root.settingsActionColumnWidth
                    titleText: qsTr("试输入")
                    descriptionText: qsTr("打开输入框，看说的话有没有变成文字")
                    descriptionObjectName: "actualSpeechTestDescription"
                    showDivider: false
                    CompactButton {
                        id: trySpeakingButton
                        objectName: "trySpeakingButton"
                        tokens: root.tokens
                        Layout.fillWidth: true
                        text: qsTr("试说一句")
                        highlighted: true
                        onClicked: speakTestDialog.open()
                        KeyNavigation.tab: root.tabTarget
                    }
                }
            }

            Item { Layout.preferredHeight: tokens.pageVerticalPadding }
        }
    }

    TapHandler {
        parent: voiceScroll.contentItem
        objectName: "voiceHotkeyOutsideTapHandler"
        enabled: root.voiceHotkeyRecording
        acceptedButtons: Qt.AllButtons
        onTapped: function(eventPoint, button) {
            const fieldPoint = voiceHotkeyField.mapFromItem(
                voiceScroll.contentItem,
                eventPoint.position.x,
                eventPoint.position.y
            )
            const insideField = fieldPoint.x >= 0
                && fieldPoint.y >= 0
                && fieldPoint.x < voiceHotkeyField.width
                && fieldPoint.y < voiceHotkeyField.height
            if (!insideField)
                root.stopVoiceHotkeyCapture()
        }
    }
}
