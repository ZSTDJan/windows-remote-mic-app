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
    readonly property int settingsStateColumnWidth: 54
    readonly property int settingsActionColumnWidth: 84
    readonly property real voiceHotkeyEditorWidth:
        Math.max(130, endpointCombo.width / 2)

    readonly property bool voiceProgramManaged:
        SettingsController.voiceProgramManaged
    readonly property bool voiceProgramSystemManaged:
        SettingsController.voiceProgramSystemManaged
    readonly property bool windowsDictationSelected:
        SettingsController.voiceProgramWindowsDictationSelected
    readonly property bool sogouSelected:
        SettingsController.voiceProgramSogouSelected
    readonly property bool wetypeSelected:
        SettingsController.voiceProgramWeTypeSelected
    readonly property bool customProgramSelected:
        SettingsController.voiceProgramCustomSelected
    readonly property bool voiceHotkeyBusy: SettingsController.voiceHotkeyBusy
    readonly property bool endpointPreflightBusy:
        SettingsController.endpointPreflightBusy
    readonly property bool configurationWriteBusy:
        DiagnosticsController.driverActionRunning
        || DiagnosticsController.vbCableTestRunning
        || SettingsController.bridgeLaunchBusy
        || root.endpointPreflightBusy
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
        || SettingsController.voiceProgramStatusCode === "running_not_ready"
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
        if (checkId === "vb_cable_endpoints")
            return String(row.detail || "").indexOf(qsTr("缺少")) >= 0
                ? qsTr("请安装") : qsTr("请重试")
        if (checkId === "output_endpoint")
            return qsTr("请重选")
        return qsTr("请重试")
    }

    function checkColor(checkId) {
        const state = checkState(checkId)
        if (state === qsTr("正常"))
            return tokens.successColor
        if (state === qsTr("请安装") || state === qsTr("请重选")
                || state === qsTr("请重试"))
            return tokens.errorColor
        if (state === qsTr("检查中") || state === qsTr("待实测"))
            return tokens.voiceAccent
        return tokens.disabledText
    }

    function checkDetail(checkId, fallback) {
        const row = checkResult(checkId)
        return row && String(row.detail).length > 0
            ? String(row.detail).trim().replace(/[。；;]+$/, "") : fallback
    }

    function actualSpeechStateCode() {
        if (root.configurationWriteBusy || root.voiceHotkeyBusy
                || SettingsController.inputCaptureInUse
                || SettingsController.hidHelperRepairBusy
                || SettingsController.bridgeReconnectBusy
                || DiagnosticsController.isRefreshing)
            return "busy"
        if (SettingsController.hidHelperIssueVisible)
            return "enable_mapping"
        if (!SettingsController.bridgeRunning)
            return "start_service"
        if (SettingsController.bridgeRestartRecommended)
            return "restart_service"
        if (!SettingsController.bridgeConnected)
            return "connect_remote"
        const hidState = String(SettingsController.hidTapState)
        if (hidState === "attached_waiting_for_hid_io"
                || hidState === "waiting_for_rc003_host"
                || hidState === "waiting_for_gadget_connection")
            return "connect_remote"
        if (hidState === "unknown" || hidState === "verified_not_started"
                || hidState === "starting" || hidState === "injecting")
            return "busy"
        if (hidState !== "ready")
            return "restart_service"
        const physicalizerState = String(
            SettingsController.voiceKeyPhysicalizerState
        )
        if (physicalizerState === "unknown"
                || physicalizerState === "starting"
                || physicalizerState === "recovering")
            return "busy"
        if (physicalizerState !== "ready")
            return "restart_service"
        if (SettingsController.voiceHotkeySaveState === "retry"
                || String(SettingsController.holdVoiceHotkeyText).trim().length === 0)
            return "record_hotkey"
        const cableCheck = root.checkResult("vb_cable_endpoints")
        const outputCheck = root.checkResult("output_endpoint")
        if (!cableCheck || !outputCheck)
            return "run_checks"
        if (String(cableCheck.status) !== "pass") {
            return String(cableCheck.detail || "").indexOf(qsTr("缺少")) >= 0
                ? "install_audio" : "check_audio"
        }
        if (SettingsController.selectedEndpointIndex < 0
                || String(outputCheck.status) !== "pass")
            return "apply_output"
        if (!SettingsController.voiceMappingReady)
            return "configure_mapping"
        if (root.voiceProgramManaged) {
            const programCode = SettingsController.voiceProgramStatusCode
            if (programCode === "unknown")
                return "busy"
            if (programCode === "not_found")
                return root.customProgramSelected
                    ? "select_program" : "install_program"
            if (!root.voiceProgramSystemManaged && programCode === "stopped")
                return "start_program"
            if (programCode === "running_not_ready" && !root.sogouSelected)
                return "restart_program"
            if (root.voiceProgramPrivilegeUnknown
                    || root.voiceProgramPrivilegeMismatch)
                return "restart_program"
        }
        return "ready"
    }

    function actualSpeechDescription() {
        const code = actualSpeechStateCode()
        if (code === "busy")
            return qsTr("当前操作完成后再试")
        if (code === "enable_mapping")
            return qsTr("先到设备页启用改键")
        if (code === "start_service")
            return qsTr("先到设备页启动服务")
        if (code === "restart_service")
            return qsTr("先到设备页重启服务")
        if (code === "connect_remote")
            return qsTr("先按一下遥控器方向键")
        if (code === "record_hotkey")
            return qsTr("先重新录入语音按键")
        if (code === "run_checks")
            return qsTr("先重新检查音频配置")
        if (code === "install_audio")
            return qsTr("先安装虚拟音频并重启电脑")
        if (code === "check_audio")
            return qsTr("先重新检查虚拟音频")
        if (code === "apply_output")
            return qsTr("先应用 CABLE Input 输出端点")
        if (code === "configure_mapping")
            return qsTr("先把话筒键设为“按住说话”并保存")
        if (code === "select_program")
            return qsTr("先在上方选择语音程序")
        if (code === "install_program")
            return qsTr("先在上方安装语音程序")
        if (code === "start_program")
            return qsTr("点击右侧启动语音程序")
        if (code === "restart_program")
            return qsTr("先完全退出语音程序，再点右侧重新检测")
        return qsTr("在输入框中验证语音文字")
    }

    function actualSpeechStateText() {
        const code = actualSpeechStateCode()
        if (code === "busy")
            return qsTr("请稍候")
        if (code === "enable_mapping")
            return qsTr("请启用改键")
        if (code === "start_service")
            return qsTr("请启动服务")
        if (code === "restart_service")
            return qsTr("请重启服务")
        if (code === "connect_remote")
            return qsTr("请按方向键")
        if (code === "record_hotkey")
            return qsTr("请录入")
        if (code === "run_checks" || code === "check_audio")
            return qsTr("请检查")
        if (code === "install_audio")
            return qsTr("请装音频")
        if (code === "apply_output")
            return qsTr("请应用")
        if (code === "configure_mapping")
            return qsTr("请改映射")
        if (code === "select_program")
            return qsTr("请选择程序")
        if (code === "install_program")
            return qsTr("请安装程序")
        if (code === "start_program")
            return qsTr("请启动程序")
        if (code === "restart_program")
            return qsTr("请退出程序")
        return qsTr("待实测")
    }

    function actualSpeechActionText() {
        const code = actualSpeechStateCode()
        if (code === "enable_mapping" || code === "start_service"
                || code === "restart_service")
            return qsTr("打开设备")
        if (code === "record_hotkey")
            return qsTr("重新录入")
        if (code === "run_checks" || code === "check_audio")
            return qsTr("重新检查")
        if (code === "install_audio")
            return qsTr("安装音频")
        if (code === "apply_output")
            return qsTr("应用端点")
        if (code === "configure_mapping")
            return qsTr("设置按键")
        if (code === "select_program")
            return qsTr("选择程序")
        if (code === "install_program")
            return qsTr("打开设置")
        if (code === "start_program")
            return qsTr("启动程序")
        if (code === "restart_program")
            return qsTr("重新检测")
        return qsTr("试说一句")
    }

    function actualSpeechActionEnabled() {
        const code = actualSpeechStateCode()
        return code === "ready" || code === "enable_mapping"
            || code === "start_service" || code === "restart_service"
            || code === "record_hotkey" || code === "run_checks"
            || code === "check_audio" || code === "install_audio"
            || code === "apply_output" || code === "configure_mapping"
            || code === "select_program" || code === "install_program"
            || code === "start_program" || code === "restart_program"
    }

    function runActualSpeechAction() {
        const code = actualSpeechStateCode()
        if (code === "ready")
            speakTestDialog.open()
        else if (code === "enable_mapping" || code === "start_service"
                || code === "restart_service")
            root.openDeviceRequested()
        else if (code === "record_hotkey")
            root.startVoiceHotkeyCapture()
        else if (code === "run_checks" || code === "check_audio")
            DiagnosticsController.refreshDiagnostics()
        else if (code === "install_audio")
            driverConfirmDialog.open()
        else if (code === "apply_output")
            DiagnosticsController.selectDetectedCableInputAsOutput()
        else if (code === "configure_mapping")
            root.openButtonsRequested()
        else if (code === "select_program")
            voiceProgramFileDialog.open()
        else if (code === "install_program")
            SettingsController.openVoiceProgramSettings()
        else if (code === "start_program")
            SettingsController.launchVoiceProgram()
        else if (code === "restart_program")
            SettingsController.refreshVoiceProgramStatus()
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
        if (code === "running_not_ready")
            return qsTr("运行中 · 窗口未就绪")
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

    function voiceProgramLaunchDescription() {
        const code = SettingsController.voiceProgramStatusCode
        if (!voiceProgramManaged)
            return qsTr("只发送语音快捷键，不启动程序")
        if (windowsDictationSelected)
            return qsTr("使用 Windows 听写与联机语音识别")
        if (sogouSelected && code !== "not_found") {
            return voiceProgramStatusSummary()
                + qsTr("；设置：在任务栏（含隐藏图标）右键搜狗语音图标")
        }
        if (voiceProgramNeedsAttention
                || code === "not_found"
                || code === "stopped") {
            return voiceProgramStatusSummary()
        }
        if (voiceProgramSystemManaged)
            return qsTr("由 Windows 管理，无需本程序启动")
        return qsTr("随遥控器服务启动；失败不影响服务")
    }

    function voiceHotkeyDescription() {
        if (sogouSelected && root.voiceHotkeyBusy)
            return qsTr("正在读取搜狗的按住型快捷键。")
        if (sogouSelected
                && SettingsController.voiceHotkeySaveState === "retry")
            return qsTr("未读取到搜狗的按住型快捷键，请重新选择搜狗或直接录入。")
        if (sogouSelected)
            return qsTr("已读取搜狗当前的按住说快捷键。如需修改，请在「搜狗语音界面」修改按住型快捷键，改后自动同步。")
        if (wetypeSelected)
            return qsTr("需手动设置，使「微信语音界面的按住型快捷键」和「语音按键」统一。")
        if (windowsDictationSelected)
            return qsTr("Windows 语音输入固定使用 Win+H")
        return qsTr("仅在%1中记录按住型快捷键")
            .arg(SettingsController.applicationDisplayName)
    }

    function startVoiceHotkeyCapture() {
        if (windowsDictationSelected)
            return
        if (voiceHotkeyRecording) {
            stopVoiceHotkeyCapture()
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

    function stopVoiceHotkeyCapture() {
        pendingVoiceHotkey = ""
        if (!voiceHotkeyRecording)
            return true
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
        modal: true
        popupType: Popup.Item
        anchors.centerIn: parent
        width: Math.min(480, root.width - 36)
        height: Math.min(300, root.height - 28)
        readonly property string headerText: qsTr("实际说话")
        title: headerText
        standardButtons: Dialog.NoButton
        leftPadding: 16
        rightPadding: 16
        topPadding: 0
        bottomPadding: 14
        leftInset: 0
        rightInset: 0
        topInset: 0
        bottomInset: 0
        closePolicy: Popup.CloseOnEscape
        onOpened: Qt.callLater(function() { speakTestInput.forceActiveFocus() })

        background: Rectangle {
            radius: tokens.cornerRadiusLarge
            color: tokens.surface
            border.width: tokens.hairlineWidth
            border.color: tokens.border
        }

        header: Item {
            width: speakTestDialog.width
            implicitHeight: 42

            UiLabel {
                anchors.left: parent.left
                anchors.leftMargin: 16
                anchors.verticalCenter: parent.verticalCenter
                tokens: root.tokens
                kind: sectionTitleKind
                text: speakTestDialog.headerText
                font.pixelSize: tokens.fontSizeTitle
                font.weight: Font.Medium
            }

            DialogCloseButton {
                objectName: "speakTestCloseButton"
                tokens: root.tokens
                anchors.right: parent.right
                anchors.rightMargin: 8
                anchors.verticalCenter: parent.verticalCenter
                onCloseRequested: speakTestDialog.close()
            }
        }

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
                text: qsTr("保持光标在输入框中，按住遥控器话筒键说话，松开后查看文字。")
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
                    placeholderText: qsTr("识别出的文字会直接输入到这里")
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
            if (!root.voiceHotkeyRecording)
                return
            root.pendingVoiceHotkey = ""
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

    Timer {
        objectName: "voiceProgramStatusRefreshTimer"
        interval: 5000
        repeat: true
        running: root.visible && root.voiceProgramManaged
        onTriggered: SettingsController.refreshVoiceProgramStatus()
    }

    onVisibleChanged: {
        if (visible) {
            SettingsController.refreshVoiceProgramStatus()
            SettingsController.refreshVoiceProgramOptions()
            SettingsController.refreshVoiceHotkeyFromProvider()
        } else {
            stopVoiceHotkeyCapture()
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
                        ? qsTr("请重试")
                        : DiagnosticsController.driverStatusMessage.length > 0
                            ? qsTr("正常")
                            : DiagnosticsController.driverInfoMessage.length > 0
                                ? (DiagnosticsController.driverInfoMessage.indexOf(
                                    qsTr("正在")) >= 0
                                    ? qsTr("处理中")
                                    : DiagnosticsController.driverInfoMessage.indexOf(
                                        qsTr("重启电脑")) >= 0
                                        ? qsTr("重启电脑") : qsTr("请重试"))
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
                            text: qsTr("安装虚拟音频")
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
                        text: qsTr("应用")
                        highlighted: true
                        enabled: !root.configurationWriteBusy
                            && !root.voiceHotkeyBusy
                        onClicked: DiagnosticsController.selectDetectedCableInputAsOutput()
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
                    stateText: SettingsController.voiceProgramSettingsDirty
                        ? qsTr("待保存")
                        : root.voiceProgramManaged ? qsTr("已保存") : qsTr("不管理")
                    stateColor: root.voiceProgramStateColor

                    editorData: [
                        SelectionComboBox {
                            id: voiceProgramCombo
                            objectName: "voiceProgramCombo"
                            tokens: root.tokens
                            Layout.fillWidth: true
                            Layout.minimumWidth: 180
                            model: SettingsController.voiceProgramOptions
                            currentIndex: SettingsController.selectedVoiceProgramIndex
                            enabled: !root.voiceHotkeyBusy
                                && !root.configurationWriteBusy
                            onActivated: SettingsController.selectedVoiceProgramIndex = index
                            Accessible.name: qsTr("语音程序")
                        }
                    ]
                    CheckBox {
                        id: voiceProgramElevatedCheckBox
                        objectName: "voiceProgramElevatedCheckBox"
                        visible: !root.windowsDictationSelected
                            && !root.voiceProgramSystemManaged
                        implicitHeight: tokens.controlHeight
                        Layout.fillWidth: true
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
                    editorColumnWidth: root.voiceHotkeyEditorWidth
                    stateColumnWidth: root.settingsStateColumnWidth
                    actionColumnWidth: root.settingsActionColumnWidth
                    titleText: qsTr("语音按键")
                    descriptionText: root.voiceHotkeyCaptureError.length > 0
                        ? root.voiceHotkeyCaptureError
                        : root.voiceHotkeyDescription()
                    stateText: root.voiceHotkeyRecording
                        ? qsTr("录入中")
                        : root.voiceHotkeyBusy ? qsTr("处理中")
                            : root.voiceHotkeyCaptureError.length > 0
                                || SettingsController.voiceHotkeySaveState === "retry"
                                ? qsTr("请重试") : qsTr("已保存")
                    stateColor: root.voiceHotkeyRecording || root.voiceHotkeyBusy
                        ? tokens.voiceAccent
                        : root.voiceHotkeyCaptureError.length > 0
                            || SettingsController.voiceHotkeySaveState === "retry"
                            ? tokens.errorColor : tokens.successColor

                    editorData: [
                        CompactTextField {
                            id: voiceHotkeyField
                            objectName: "holdVoiceHotkeyField"
                            tokens: root.tokens
                            Layout.fillWidth: true
                            readOnly: true
                            enabled: !root.voiceHotkeyBusy
                                && !root.windowsDictationSelected
                                && !root.configurationWriteBusy
                            text: root.voiceHotkeyRecording
                                ? qsTr("请按快捷键")
                                : SettingsController.holdVoiceHotkeyText
                            color: root.voiceHotkeyRecording
                                ? tokens.accent : tokens.textPrimary
                            placeholderText: qsTr("点击录入")
                            Accessible.name: qsTr("语音按键，仅支持录入按住型快捷键")
                            Keys.onEscapePressed: root.stopVoiceHotkeyCapture()
                            onActiveFocusChanged: {
                                if (!activeFocus && root.voiceHotkeyRecording)
                                    root.stopVoiceHotkeyCapture()
                            }
                            TapHandler {
                                enabled: !root.windowsDictationSelected
                                onTapped: root.startVoiceHotkeyCapture()
                            }
                        },
                        CompactButton {
                            objectName: "useWindowsDictationHotkeyButton"
                            visible: root.windowsDictationSelected
                            tokens: root.tokens
                            compactMinimumWidth: tokens.buttonWidth4Chars
                            text: qsTr("Win+H")
                            enabled: !root.voiceHotkeyBusy
                                && !root.configurationWriteBusy
                            onClicked: SettingsController.useWindowsDictationHotkey()
                        }
                    ]
                    CompactButton {
                        objectName: "openVoiceProgramSettingsButton"
                        visible: root.wetypeSelected
                            || root.windowsDictationSelected
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
                    objectName: "voiceProgramSpecificRow"
                    tokens: root.tokens
                    stateColumnWidth: root.settingsStateColumnWidth
                    actionColumnWidth: root.settingsActionColumnWidth
                    titleText: root.windowsDictationSelected
                        ? qsTr("系统设置") : qsTr("程序启动")
                    descriptionObjectName: "voiceProgramLaunchText"
                    descriptionText: root.voiceProgramLaunchDescription()
                    showDivider: false
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
                    titleText: qsTr("声音通道")
                    descriptionText: DiagnosticsController.vbCableTestMessage.length > 0
                        ? DiagnosticsController.vbCableTestMessage
                        : qsTr("测试 CABLE Input → CABLE Output")
                    descriptionObjectName: "soundChannelTestDescription"
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
                            ? qsTr("测试中…") : qsTr("测试通道")
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
                    titleText: qsTr("实际说话")
                    descriptionText: root.actualSpeechDescription()
                    descriptionObjectName: "actualSpeechTestDescription"
                    stateText: root.actualSpeechStateText()
                    stateColor: root.actualSpeechStateCode() === "ready"
                            || root.actualSpeechStateCode() === "busy"
                            || root.actualSpeechStateCode() === "connect_remote"
                        ? tokens.voiceAccent : tokens.errorColor
                    showDivider: false

                    CompactButton {
                        id: trySpeakingButton
                        objectName: "trySpeakingButton"
                        tokens: root.tokens
                        Layout.fillWidth: true
                        text: root.actualSpeechActionText()
                        highlighted: true
                        enabled: root.actualSpeechActionEnabled()
                        onClicked: root.runActualSpeechAction()
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
