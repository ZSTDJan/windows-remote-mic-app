import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs
import OvbRc003Settings 1.0

Item {
    id: root
    property var tokens
    property var backTabTarget: null
    property var tabTarget: null
    readonly property var firstFocusItem: openBluetoothSettingsButton
    readonly property var lastFocusItem: closeBehaviorCombo
    readonly property int deviceStateColumnWidth: 96
    readonly property int deviceActionColumnWidth: tokens.buttonWidth4Chars
    signal openButtonsRequested()
    signal enableHidHelperRequested()
    signal removeHidHelperRequested()

    function restoreLogExportFocus() {
        Qt.callLater(function() {
            if (root.Window.window)
                root.Window.window.requestActivate()
            if (deviceOpenLogButton.enabled)
                deviceOpenLogButton.forceActiveFocus()
            else
                diagnosticTraceSwitch.forceActiveFocus()
        })
    }

    FileDialog {
        id: logExportDialog
        objectName: "logExportDialog"
        title: qsTr("导出日志")
        fileMode: FileDialog.SaveFile
        nameFilters: [qsTr("ZIP 压缩文件 (*.zip)")]
        defaultSuffix: "zip"
        onAccepted: {
            SettingsController.exportLogs(selectedFile)
            root.restoreLogExportFocus()
        }
        onRejected: root.restoreLogExportFocus()
    }

    function checkResult(checkId) {
        const rows = DiagnosticsController.checkResults
        for (var i = 0; i < rows.length; i++) {
            if (String(rows[i].checkId) === checkId)
                return rows[i]
        }
        return null
    }

    function currentDeviceStateCode() {
        if (!SettingsController.activeRemoteKey)
            return "unselected"
        if (DiagnosticsController.isRefreshing)
            return "checking"
        const row = checkResult("ble_candidate")
        if (!row)
            return "unchecked"
        const resultCode = String(row.resultCode || "")
        if (resultCode === "selected_missing")
            return "selected_missing"
        if (resultCode === "ambiguous")
            return "conflict"
        if (resultCode === "no_candidate")
            return "unpaired"
        if (row.status === "pass")
            return "paired"
        if (row.status === "manual")
            return "waiting"
        return "error"
    }

    function currentDeviceStateText() {
        switch (currentDeviceStateCode()) {
        case "unselected": return qsTr("请选择设备")
        case "selected_missing": return qsTr("所选设备未连接")
        case "checking": return qsTr("正在检查")
        case "paired": return qsTr("已配对")
        case "waiting": return qsTr("正在确认")
        case "conflict": return qsTr("设备识别异常")
        case "unpaired": return qsTr("点击“蓝牙设置”")
        case "error": return qsTr("点击“重新检查”")
        default: return qsTr("未检查")
        }
    }

    function currentDeviceDetailText() {
        return SettingsController.activeRemoteLabel
    }

    function currentDeviceStateColor() {
        const code = currentDeviceStateCode()
        if (code === "paired")
            return tokens.successColor
        if (code === "checking" || code === "waiting")
            return tokens.voiceAccent
        if (code === "unchecked")
            return tokens.disabledText
        return tokens.errorColor
    }

    function valueIn(value, choices) {
        return choices.indexOf(String(value)) >= 0
    }

    function rawInputReady() {
        return SettingsController.rawInputState === "ready"
    }

    function hidTapReady() {
        return SettingsController.hidTapState === "ready"
    }

    function rawInputWaitsForRemote() {
        return valueIn(SettingsController.rawInputState, [
            "no_device", "device_removed"
        ])
    }

    function hidTapWaitsForRemote() {
        return valueIn(SettingsController.hidTapState, [
            "waiting_for_rc003_host", "waiting_for_gadget_connection"
        ])
    }

    function hidTapWaitsForFirstInput() {
        return SettingsController.hidTapState === "attached_waiting_for_hid_io"
    }

    function buttonReceiverWaitsForRemote() {
        return hidTapWaitsForRemote()
            && (rawInputWaitsForRemote() || rawInputReady())
    }

    function rawInputChecking() {
        return valueIn(SettingsController.rawInputState, [
            "unknown", "starting", "recovering"
        ])
    }

    function hidTapChecking() {
        return valueIn(SettingsController.hidTapState, [
            "unknown", "verified_not_started", "starting", "injecting"
        ])
    }

    function voiceKeyChecking() {
        return valueIn(SettingsController.voiceKeyPhysicalizerState, [
            "unknown", "starting", "recovering"
        ])
    }

    function voiceKeyFailed() {
        return valueIn(SettingsController.voiceKeyPhysicalizerState, [
            "failed", "stopped"
        ])
    }

    function buttonReceiverStateCode() {
        if (!SettingsController.activeRemoteKey)
            return "unselected"
        if (SettingsController.hidHelperRepairBusy)
            return "checking"
        if (SettingsController.hidHelperIssueVisible) {
            if (SettingsController.hidHelperAccountUnsupported)
                return "account"
            if (SettingsController.hidHelperCleanupPending)
                return "cleanup"
            if (!SettingsController.hidHelperRepairVisible)
                return "version"
            return SettingsController.hidHelperSetupRequired
                ? "disabled" : "permission"
        }
        if (SettingsController.bridgeRunning) {
            if (SettingsController.hidTapState === "selected_device_shared_host")
                return "shared_host"
            if (SettingsController.hidTapState === "restart_required")
                return "restart_computer"
            if (SettingsController.bridgeLaunchBusy
                    || SettingsController.bridgeReconnectBusy)
                return "checking"
            if (SettingsController.rawInputState === "ambiguous")
                return "conflict"
            const rawReady = rawInputReady()
            const tapReady = hidTapReady()
            const deviceCode = currentDeviceStateCode()
            if (rawReady && tapReady) {
                if (voiceKeyFailed())
                    return "voice_error"
                if (voiceKeyChecking())
                    return "checking"
                return "ready"
            }
            if (hidTapWaitsForFirstInput()) {
                if (deviceCode === "unpaired")
                    return "unpaired"
                if (deviceCode === "conflict")
                    return "conflict"
                return "waiting_input"
            }
            if (buttonReceiverWaitsForRemote()) {
                if (deviceCode === "unpaired")
                    return "unpaired"
                if (deviceCode === "conflict")
                    return "conflict"
                return "waiting_remote"
            }
            if (rawReady)
                return hidTapChecking() || hidTapWaitsForRemote()
                    ? "checking" : "original_only"
            if (tapReady)
                return rawInputChecking() ? "checking" : "custom_only"
            if (rawInputChecking() || hidTapChecking())
                return "checking"
            return "error"
        }
        if (DiagnosticsController.isRefreshing)
            return "checking"
        const osRow = checkResult("os_version")
        if (osRow && (osRow.status === "fail" || osRow.status === "unsupported"))
            return "system_error"
        const rawRow = checkResult("raw_input")
        if (!rawRow)
            return "unchecked"
        const rawCode = String(rawRow.resultCode || "")
        if (rawCode === "ambiguous")
            return "conflict"
        if (rawCode === "no_device") {
            const deviceCode = currentDeviceStateCode()
            if (deviceCode === "unpaired")
                return "unpaired"
            if (deviceCode === "conflict")
                return "conflict"
            return "waiting_remote"
        }
        if (rawRow.status === "pass")
            return "service_stopped"
        return "error"
    }

    function buttonReceiverStateText() {
        switch (buttonReceiverStateCode()) {
        case "unselected": return qsTr("请先选择设备")
        case "checking": return qsTr("正在检查")
        case "cleanup": return qsTr("点击“完成清理”")
        case "account": return qsTr("请更换账号")
        case "disabled": return qsTr("点击“启用改键”")
        case "permission": return qsTr("点击“修复权限”")
        case "version": return qsTr("请安装兼容版")
        case "restart_computer": return qsTr("请重启电脑一次")
        case "shared_host": return qsTr("按键来源待区分")
        case "waiting_remote": return qsTr("请按遥控器方向键")
        case "waiting_input": return qsTr("请按遥控器方向键")
        case "unpaired": return qsTr("请先配对遥控器")
        case "conflict": return qsTr("设备识别异常")
        case "ready": return qsTr("正常")
        case "service_stopped": return qsTr("等待服务启动")
        case "custom_only": return qsTr("点击“重启服务”")
        case "original_only": return qsTr("点击“重启服务”")
        case "voice_error": return qsTr("点击“重启服务”")
        case "system_error": return qsTr("请升级系统")
        case "error": return SettingsController.bridgeRunning
            ? qsTr("点击“重启服务”") : qsTr("点击“重新检查”")
        default: return qsTr("未检查")
        }
    }

    function buttonReceiverStateColor() {
        const code = buttonReceiverStateCode()
        if (code === "ready")
            return tokens.successColor
        if (code === "checking" || code === "waiting_remote"
                || code === "waiting_input" || code === "custom_only"
                || code === "original_only")
            return tokens.voiceAccent
        if (code === "cleanup" || code === "unchecked"
                || code === "service_stopped")
            return tokens.disabledText
        return tokens.errorColor
    }

    function bridgeStateText() {
        if (SettingsController.bridgeReconnectBusy)
            return qsTr("连接中")
        if (SettingsController.bridgeLaunchBusy)
            return qsTr("启动中")
        if (bridgeNeedsRestartAction())
            return qsTr("点击“重启服务”")
        const receiverCode = buttonReceiverStateCode()
        if (SettingsController.bridgeConnected)
            return qsTr("已连接")
        if (SettingsController.bridgeRunning) {
            const connectionCode = String(SettingsController.bridgeConnectionState)
            if (connectionCode === "retry_wait")
                return qsTr("点击“重新连接”")
            if (connectionCode === "connecting")
                return qsTr("正在连接遥控器")
            if (receiverCode === "waiting_remote")
                return qsTr("请按遥控器方向键")
            if (receiverCode === "waiting_input")
                return qsTr("请按遥控器方向键")
            if (receiverCode === "unpaired" || receiverCode === "conflict")
                return qsTr("请先处理设备")
            if (bridgeReconnectActionAvailable())
                return qsTr("点击“重新连接”")
            return qsTr("正在等待连接")
        }
        if (SettingsController.bridgeLaunchPhase === "unknown"
                || SettingsController.bridgeLaunchPhase === "failed")
            return qsTr("启动失败")
        return qsTr("点击“启动服务”")
    }

    function bridgeReconnectActionAvailable() {
        if (!SettingsController.bridgeReconnectAvailable)
            return false
        const code = currentDeviceStateCode()
        if (code === "unpaired" || code === "conflict" || code === "error")
            return false
        const connectionCode = String(SettingsController.bridgeConnectionState)
        if (connectionCode === "retry_wait")
            return true
        if (connectionCode !== "waiting_for_device")
            return false
        return rawInputReady() && hidTapReady()
            && !voiceKeyChecking() && !voiceKeyFailed()
    }

    function bridgeNeedsRestartAction() {
        if (!SettingsController.bridgeRunning)
            return false
        if (SettingsController.hidTapState === "restart_required")
            return false
        if (SettingsController.bridgeRestartRecommended)
            return true
        const code = buttonReceiverStateCode()
        return code === "custom_only" || code === "original_only"
            || code === "voice_error" || code === "error"
    }

    function bridgeActionVisible() {
        if (!SettingsController.bridgeRunning)
            return true
        if (SettingsController.bridgeLaunchBusy
                || SettingsController.bridgeReconnectBusy)
            return true
        if (bridgeNeedsRestartAction())
            return true
        if (SettingsController.bridgeConnected)
            return false
        return bridgeReconnectActionAvailable()
    }

    function bridgeActionText() {
        if (SettingsController.bridgeReconnectBusy)
            return qsTr("连接中…")
        if (SettingsController.bridgeLaunchBusy)
            return qsTr("启动中…")
        if (!SettingsController.bridgeRunning)
            return qsTr("启动服务")
        if (bridgeNeedsRestartAction())
            return qsTr("重启服务")
        return qsTr("重新连接")
    }

    function bridgeStateColor() {
        if (bridgeNeedsRestartAction())
            return tokens.errorColor
        if (SettingsController.bridgeConnectionState === "retry_wait")
            return tokens.errorColor
        if (SettingsController.bridgeConnected)
            return tokens.successColor
        const receiverCode = buttonReceiverStateCode()
        if (receiverCode === "unpaired" || receiverCode === "conflict")
            return tokens.errorColor
        if (SettingsController.bridgeLaunchPhase === "unknown"
                || SettingsController.bridgeLaunchPhase === "failed")
            return tokens.errorColor
        if (SettingsController.bridgeReconnectBusy
                || SettingsController.bridgeRunning
                || SettingsController.bridgeLaunchBusy
                || !SettingsController.bridgeRunning)
            return tokens.voiceAccent
        return tokens.disabledText
    }

    RemoteDeviceDialog {
        id: remoteDeviceDialog
        tokens: root.tokens
        anchors.centerIn: parent
    }

    ScrollView {
        id: deviceScroll
        objectName: "deviceScroll"
        anchors.fill: parent
        clip: true
        contentWidth: availableWidth
        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff

        ColumnLayout {
            id: devicePageContent
            objectName: "devicePageContent"
            width: Math.max(0, deviceScroll.availableWidth
                - tokens.pageHorizontalPadding * 2)
            x: tokens.pageHorizontalPadding
            y: tokens.pageVerticalPadding
            spacing: tokens.spacingMedium

            SectionFrame {
                objectName: "devicePrerequisiteSection"
                tokens: root.tokens
                Layout.fillWidth: true
                horizontalPadding: 0
                verticalPadding: 4
                contentSpacing: 0

                SettingsSectionTitle {
                    objectName: "devicePrerequisiteSectionTitle"
                    tokens: root.tokens
                    text: qsTr("设备状态")
                }

                InlineSettingsRow {
                    objectName: "currentDeviceRow"
                    tokens: root.tokens
                    stateColumnWidth: root.deviceStateColumnWidth
                    actionColumnWidth: root.deviceActionColumnWidth
                    editorColumnWidth: root.deviceActionColumnWidth * 2 + tokens.spacingSmall
                    titleText: qsTr("当前设备")
                    descriptionText: root.currentDeviceDetailText()
                    descriptionNeverElide: false
                    stateText: root.currentDeviceStateText()
                    stateColor: root.currentDeviceStateColor()

                    editorData: [CompactButton {
                        id: openBluetoothSettingsButton
                        objectName: "openBluetoothSettingsButton"
                        tokens: root.tokens
                        Layout.fillWidth: true
                        compactMinimumWidth: tokens.buttonWidth4Chars
                        text: qsTr("蓝牙设置")
                        onClicked: SettingsController.openBluetoothSettings()
                        KeyNavigation.backtab: root.backTabTarget
                        KeyNavigation.tab: selectRemoteButton
                    }, CompactButton {
                        id: selectRemoteButton
                        objectName: "selectRemoteButton"
                        tokens: root.tokens
                        Layout.fillWidth: true
                        compactMinimumWidth: tokens.buttonWidth4Chars
                        text: qsTr("选择设备")
                        onClicked: remoteDeviceDialog.open()
                        KeyNavigation.backtab: openBluetoothSettingsButton
                        KeyNavigation.tab: refreshDeviceChecksButton
                    }]

                    CompactButton {
                        objectName: "refreshDeviceChecksButton"
                        id: refreshDeviceChecksButton
                        tokens: root.tokens
                        Layout.fillWidth: true
                        compactMinimumWidth: tokens.buttonWidth4Chars
                        text: DiagnosticsController.isRefreshing
                            ? qsTr("检查中…") : qsTr("重新检查")
                        enabled: !DiagnosticsController.isRefreshing
                            && !DiagnosticsController.vbCableTestRunning
                        onClicked: DiagnosticsController.refreshDiagnostics()
                        KeyNavigation.backtab: selectRemoteButton
                    }
                }

                InlineSettingsRow {
                    objectName: "buttonReceiverRow"
                    tokens: root.tokens
                    stateColumnWidth: root.deviceStateColumnWidth
                    actionColumnWidth: root.deviceActionColumnWidth
                    titleText: qsTr("按键接收")
                    editorColumnVisible: SettingsController.hidHelperRepairVisible
                        || SettingsController.hidHelperRemovalVisible
                    editorColumnWidth: root.deviceActionColumnWidth
                    descriptionText: root.buttonReceiverStateCode() === "restart_computer"
                        ? qsTr("旧按键组件未释放，重启后重新打开程序")
                        : qsTr("让遥控器按键在电脑上生效")
                    descriptionNeverElide: true
                    stateText: root.buttonReceiverStateText()
                    stateColor: root.buttonReceiverStateColor()

                    editorData: [CompactButton {
                        objectName: "repairHidHelperButton"
                        visible: SettingsController.hidHelperRepairVisible
                        tokens: root.tokens
                        compactMinimumWidth: tokens.buttonWidth4Chars
                        text: SettingsController.hidHelperRepairBusy
                            ? qsTr("处理中…")
                            : (SettingsController.hidHelperSetupRequired
                                ? qsTr("启用改键")
                                : SettingsController.hidHelperCleanupPending
                                ? qsTr("完成清理") : qsTr("修复权限"))
                        highlighted: true
                        enabled: !SettingsController.hidHelperRepairBusy
                            && !SettingsController.bridgeLaunchBusy
                        onClicked: root.enableHidHelperRequested()
                    }, CompactButton {
                        objectName: "removeHidHelperButton"
                        visible: SettingsController.hidHelperRemovalVisible
                        tokens: root.tokens
                        compactMinimumWidth: tokens.buttonWidth4Chars
                        text: qsTr("移除权限")
                        enabled: !SettingsController.hidHelperRepairBusy
                            && !SettingsController.bridgeLaunchBusy
                        onClicked: root.removeHidHelperRequested()
                    }]

                    CompactButton {
                        objectName: "openButtonSettingsButton"
                        tokens: root.tokens
                        Layout.fillWidth: true
                        compactMinimumWidth: tokens.buttonWidth4Chars
                        text: qsTr("按键设置")
                        onClicked: root.openButtonsRequested()
                    }
                }

                InlineSettingsRow {
                    objectName: "remoteServiceRow"
                    tokens: root.tokens
                    stateColumnWidth: root.deviceStateColumnWidth
                    actionColumnWidth: root.deviceActionColumnWidth
                    titleText: qsTr("遥控器服务")
                    descriptionText: qsTr("保持遥控器连接，让按键和语音持续可用")
                    descriptionNeverElide: true
                    stateText: root.bridgeStateText()
                    stateColor: root.bridgeStateColor()

                    CompactButton {
                        objectName: "bridgeActionButton"
                        visible: root.bridgeActionVisible()
                        tokens: root.tokens
                        Layout.fillWidth: true
                        compactMinimumWidth: tokens.buttonWidth4Chars
                        text: root.bridgeActionText()
                        highlighted: true
                        enabled: !SettingsController.bridgeLaunchBusy
                            && !SettingsController.bridgeReconnectBusy
                            && !SettingsController.hidHelperRepairBusy
                            && !DiagnosticsController.vbCableTestRunning
                            && !DiagnosticsController.driverActionRunning
                            && !SettingsController.voiceHotkeyBusy
                            && !SettingsController.endpointPreflightBusy
                        onClicked: {
                            if (!SettingsController.bridgeRunning)
                                SettingsController.startBridge()
                            else if (root.bridgeNeedsRestartAction())
                                SettingsController.restartBridge()
                            else if (root.bridgeReconnectActionAvailable())
                                SettingsController.reconnectBridgeNow()
                        }
                    }
                }

                InlineSettingsRow {
                    objectName: "runtimeLogRow"
                    tokens: root.tokens
                    actionColumnWidth: root.deviceActionColumnWidth
                    titleText: qsTr("检查更新")
                    descriptionText: ""
                    showDivider: false

                    editorData: Item {
                        Layout.fillWidth: true
                        implicitWidth: deviceUsageLink.implicitWidth
                        implicitHeight: deviceUsageLink.implicitHeight

                        CompactLink {
                            id: deviceUsageLink
                            objectName: "deviceUsageLink"
                            anchors.right: parent.right
                            tokens: root.tokens
                            text: qsTr("使用方法")
                            KeyNavigation.tab: checkApplicationUpdateButton
                            onClicked: SettingsController.openUsageGuide()
                        }
                    }

                    CompactButton {
                        id: checkApplicationUpdateButton
                        objectName: "checkApplicationUpdateButton"
                        tokens: root.tokens
                        Layout.fillWidth: true
                        compactMinimumWidth: tokens.buttonWidth4Chars
                        text: SettingsController.applicationUpdateCheckBusy
                            ? qsTr("检查中…") : qsTr("检查更新")
                        enabled: !SettingsController.applicationUpdateBusy
                        KeyNavigation.backtab: deviceUsageLink
                        KeyNavigation.tab: launchAtLoginSwitch
                        onClicked: SettingsController.checkForApplicationUpdate()
                    }
                }
            }

            SectionFrame {
                objectName: "desktopBehaviorSection"
                tokens: root.tokens
                Layout.fillWidth: true
                horizontalPadding: 0
                verticalPadding: 4
                contentSpacing: 0

                SettingsSectionTitle {
                    objectName: "desktopBehaviorSectionTitle"
                    tokens: root.tokens
                    text: qsTr("通用设置")
                }

                SettingsListRow {
                    objectName: "launchAtLoginRow"
                    tokens: root.tokens
                    titleText: qsTr("随 Windows 启动")
                    descriptionText: qsTr("登录后后台运行")

                    CompactSwitch {
                        id: launchAtLoginSwitch
                        objectName: "launchAtLoginSwitch"
                        tokens: root.tokens
                        checked: SettingsController.launchAtLogin
                        Accessible.name: qsTr("随 Windows 启动")
                        onToggled: {
                            if (checked !== SettingsController.launchAtLogin)
                                SettingsController.setLaunchAtLogin(checked)
                        }
                    }
                }

                SettingsListRow {
                    objectName: "launchBridgeOnAppStartRow"
                    tokens: root.tokens
                    titleText: qsTr("启动程序时自动运行服务")
                    descriptionText: qsTr("自动连接遥控器")

                    CompactSwitch {
                        id: launchBridgeOnAppStartSwitch
                        objectName: "launchBridgeOnAppStartSwitch"
                        tokens: root.tokens
                        checked: SettingsController.launchBridgeOnAppStart
                        Accessible.name: qsTr("启动程序时自动运行遥控器服务")
                        onToggled: {
                            if (checked !== SettingsController.launchBridgeOnAppStart)
                                SettingsController.setLaunchBridgeOnAppStart(checked)
                        }
                    }
                }

                SettingsListRow {
                    objectName: "diagnosticTraceRow"
                    tokens: root.tokens
                    titleText: qsTr("诊断日志")
                    descriptionText: qsTr("记录按键与语音排查信息")

                    CompactLink {
                        id: deviceOpenLogButton
                        objectName: "deviceOpenLogButton"
                        tokens: root.tokens
                        text: SettingsController.logExportBusy ? qsTr("导出中…") : qsTr("导出日志")
                        enabled: !SettingsController.logExportBusy
                        Layout.rightMargin: root.deviceActionColumnWidth
                            - diagnosticTraceSwitch.implicitWidth
                        KeyNavigation.backtab: launchBridgeOnAppStartSwitch
                        KeyNavigation.tab: diagnosticTraceSwitch
                        onClicked: {
                            logExportDialog.selectedFile = SettingsController.logExportDefaultFile()
                            logExportDialog.open()
                        }
                    }

                    CompactSwitch {
                        id: diagnosticTraceSwitch
                        objectName: "diagnosticTraceSwitch"
                        tokens: root.tokens
                        checked: SettingsController.diagnosticTraceEnabled
                        Accessible.name: qsTr("诊断日志")
                        KeyNavigation.backtab: deviceOpenLogButton
                        onToggled: {
                            if (checked !== SettingsController.diagnosticTraceEnabled)
                                SettingsController.setDiagnosticTraceEnabled(checked)
                        }
                    }
                }

                SettingsListRow {
                    objectName: "closeBehaviorRow"
                    tokens: root.tokens
                    titleText: qsTr("关闭窗口时")
                    descriptionText: qsTr("右上角 × 的行为")
                    showDivider: false

                    SelectionComboBox {
                        id: closeBehaviorCombo
                        objectName: "closeBehaviorCombo"
                        tokens: root.tokens
                        implicitWidth: tokens.buttonWidth4Chars
                        rightPadding: indicator.width + leftPadding + tokens.spacingTiny
                        model: SettingsController.closeBehaviorOptions
                        currentIndex: SettingsController.closeBehavior === "quit" ? 1 : 0
                        Accessible.name: qsTr("关闭窗口时")
                        onActivated: SettingsController.setCloseBehaviorIndex(index)
                        KeyNavigation.tab: root.tabTarget
                    }
                }
            }

            Item { Layout.preferredHeight: tokens.pageVerticalPadding }
        }
    }
}
