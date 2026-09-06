import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import OvbRc003Settings 1.0

Item {
    id: root
    property var tokens
    property var backTabTarget: null
    property var tabTarget: null
    readonly property var firstFocusItem: refreshDeviceChecksButton
    readonly property var lastFocusItem: closeBehaviorCombo
    readonly property int deviceStateColumnWidth: 96
    readonly property int deviceActionColumnWidth: 150
    signal openButtonsRequested()
    signal enableHidHelperRequested()
    signal removeHidHelperRequested()

    function checkResult(checkId) {
        const rows = DiagnosticsController.checkResults
        for (var i = 0; i < rows.length; i++) {
            if (String(rows[i].checkId) === checkId)
                return rows[i]
        }
        return null
    }

    function currentDeviceStateCode() {
        if (DiagnosticsController.isRefreshing)
            return "checking"
        const row = checkResult("ble_candidate")
        if (!row)
            return "unchecked"
        const resultCode = String(row.resultCode || "")
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
        case "checking": return qsTr("检查中")
        case "paired": return qsTr("已配对")
        case "waiting": return qsTr("待确认")
        case "conflict": return qsTr("多设备")
        case "unpaired": return qsTr("未配对")
        case "error": return qsTr("请重新检查")
        default: return qsTr("未检查")
        }
    }

    function currentDeviceDetailText() {
        const code = currentDeviceStateCode()
        if (code === "checking")
            return qsTr("请稍候")
        if (code === "paired")
            return SettingsController.remoteDisplayName
        if (code === "conflict")
            return qsTr("仅保留一台")
        if (code === "unpaired")
            return qsTr("请先配对")
        if (code === "waiting")
            return qsTr("请稍候")
        return qsTr("请重新检查")
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
        return valueIn(SettingsController.hidTapState, [
            "attached_waiting_for_hid_io", "ready"
        ])
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

    function buttonReceiverWaitsForRemote() {
        return rawInputWaitsForRemote() && hidTapWaitsForRemote()
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
            if (buttonReceiverWaitsForRemote()) {
                if (deviceCode === "unpaired")
                    return "unpaired"
                if (deviceCode === "conflict")
                    return "conflict"
                if (!SettingsController.bridgeConnected)
                    return "waiting_remote"
                if (hidTapWaitsForRemote())
                    return "checking"
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
            return "detected"
        return "error"
    }

    function buttonReceiverStateText() {
        switch (buttonReceiverStateCode()) {
        case "checking": return qsTr("检查中")
        case "cleanup": return qsTr("请完成清理")
        case "account": return qsTr("请换账号")
        case "disabled": return qsTr("请启用改键")
        case "permission": return qsTr("请启用改键")
        case "version": return qsTr("请安装新版")
        case "waiting_remote": return qsTr("待唤醒")
        case "unpaired": return qsTr("未配对")
        case "conflict": return qsTr("多设备")
        case "ready": return qsTr("正常")
        case "detected": return qsTr("已找到")
        case "custom_only": return qsTr("请重新连接")
        case "original_only": return qsTr("请重新连接")
        case "voice_error": return qsTr("请重新连接")
        case "system_error": return qsTr("请升级系统")
        case "error": return SettingsController.bridgeRunning
            ? qsTr("请重新连接") : qsTr("请重新检查")
        default: return qsTr("未检查")
        }
    }

    function buttonReceiverStateColor() {
        const code = buttonReceiverStateCode()
        if (code === "ready" || code === "detected")
            return tokens.successColor
        if (code === "checking" || code === "waiting_remote"
                || code === "custom_only" || code === "original_only")
            return tokens.voiceAccent
        if (code === "cleanup" || code === "unchecked")
            return tokens.disabledText
        return tokens.errorColor
    }

    function bridgeStateText() {
        if (SettingsController.bridgeReconnectBusy)
            return qsTr("连接中")
        if (SettingsController.bridgeLaunchBusy)
            return qsTr("启动中")
        if (bridgeNeedsRestartAction())
            return qsTr("请重新连接")
        const receiverCode = buttonReceiverStateCode()
        if (SettingsController.bridgeConnected) {
            if (receiverCode === "checking")
                return qsTr("确认中")
            return qsTr("已连接")
        }
        if (SettingsController.bridgeRunning) {
            if (receiverCode === "waiting_remote")
                return qsTr("待唤醒")
            if (receiverCode === "unpaired" || receiverCode === "conflict")
                return qsTr("未连接")
            if (bridgeReconnectActionAvailable())
                return qsTr("请重新连接")
            if (rawInputChecking() && hidTapChecking())
                return qsTr("确认中")
            return qsTr("连接中")
        }
        if (SettingsController.bridgeLaunchPhase === "unknown"
                || SettingsController.bridgeLaunchPhase === "failed")
            return qsTr("启动失败")
        return qsTr("未运行")
    }

    function bridgeReconnectActionAvailable() {
        if (!SettingsController.bridgeReconnectAvailable)
            return false
        const code = currentDeviceStateCode()
        return code !== "unpaired" && code !== "conflict"
            && code !== "error" && rawInputReady() && hidTapReady()
            && !voiceKeyChecking() && !voiceKeyFailed()
    }

    function bridgeNeedsRestartAction() {
        if (!SettingsController.bridgeRunning)
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
        return qsTr("重新连接")
    }

    function bridgeStateColor() {
        if (bridgeNeedsRestartAction())
            return tokens.errorColor
        if (SettingsController.bridgeConnected
                && buttonReceiverStateCode() !== "checking")
            return tokens.successColor
        if (SettingsController.bridgeReconnectBusy
                || SettingsController.bridgeRunning
                || SettingsController.bridgeLaunchBusy)
            return tokens.voiceAccent
        if (SettingsController.bridgeLaunchPhase === "unknown"
                || SettingsController.bridgeLaunchPhase === "failed")
            return tokens.errorColor
        return tokens.disabledText
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
                    titleText: qsTr("当前设备")
                    descriptionText: root.currentDeviceDetailText()
                    descriptionNeverElide: true
                    stateText: root.currentDeviceStateText()
                    stateColor: root.currentDeviceStateColor()

                    Item { Layout.fillWidth: true }

                    CompactButton {
                        objectName: "refreshDeviceChecksButton"
                        id: refreshDeviceChecksButton
                        tokens: root.tokens
                        compactMinimumWidth: tokens.buttonWidth4Chars
                        text: DiagnosticsController.isRefreshing
                            ? qsTr("检查中…") : qsTr("重新检查")
                        enabled: !DiagnosticsController.isRefreshing
                            && !DiagnosticsController.vbCableTestRunning
                        onClicked: DiagnosticsController.refreshDiagnostics()
                        KeyNavigation.backtab: root.backTabTarget
                    }
                    CompactButton {
                        objectName: "openBluetoothSettingsButton"
                        tokens: root.tokens
                        compactMinimumWidth: tokens.buttonWidth4Chars
                        text: qsTr("蓝牙设置")
                        onClicked: SettingsController.openBluetoothSettings()
                    }
                }

                InlineSettingsRow {
                    objectName: "buttonReceiverRow"
                    tokens: root.tokens
                    stateColumnWidth: root.deviceStateColumnWidth
                    actionColumnWidth: root.deviceActionColumnWidth
                    titleText: qsTr("按键接收")
                    descriptionText: qsTr("让遥控器按键在电脑上生效")
                    descriptionNeverElide: true
                    stateText: root.buttonReceiverStateText()
                    stateColor: root.buttonReceiverStateColor()

                    Item { Layout.fillWidth: true }

                    CompactButton {
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
                    }

                    CompactButton {
                        objectName: "removeHidHelperButton"
                        visible: SettingsController.hidHelperRemovalVisible
                        tokens: root.tokens
                        compactMinimumWidth: tokens.buttonWidth4Chars
                        text: qsTr("移除权限")
                        enabled: !SettingsController.hidHelperRepairBusy
                            && !SettingsController.bridgeLaunchBusy
                        onClicked: root.removeHidHelperRequested()
                    }

                    CompactButton {
                        objectName: "openButtonSettingsButton"
                        tokens: root.tokens
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

                    Item { Layout.fillWidth: true }

                    CompactButton {
                        objectName: "bridgeActionButton"
                        visible: root.bridgeActionVisible()
                        tokens: root.tokens
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
                    stateColumnWidth: root.deviceStateColumnWidth
                    actionColumnWidth: root.deviceActionColumnWidth
                    titleText: qsTr("运行日志")
                    descriptionText: ""
                    showDivider: false

                    CompactButton {
                        id: checkApplicationUpdateButton
                        objectName: "checkApplicationUpdateButton"
                        tokens: root.tokens
                        compactMinimumWidth: tokens.buttonWidth4Chars
                        text: SettingsController.applicationUpdateCheckBusy
                            ? qsTr("检查中…") : qsTr("检查更新")
                        enabled: !SettingsController.applicationUpdateBusy
                        KeyNavigation.tab: deviceOpenLogButton
                        onClicked: SettingsController.checkForApplicationUpdate()
                    }

                    CompactButton {
                        id: deviceOpenLogButton
                        objectName: "deviceOpenLogButton"
                        tokens: root.tokens
                        compactMinimumWidth: tokens.buttonWidth4Chars
                        text: qsTr("日志目录")
                        KeyNavigation.backtab: checkApplicationUpdateButton
                        onClicked: SettingsController.openLogLocation()
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
                    titleText: qsTr("启动程序时自动启动桥接")
                    descriptionText: qsTr("自动连接遥控器")

                    CompactSwitch {
                        id: launchBridgeOnAppStartSwitch
                        objectName: "launchBridgeOnAppStartSwitch"
                        tokens: root.tokens
                        checked: SettingsController.launchBridgeOnAppStart
                        Accessible.name: qsTr("启动程序时自动启动桥接")
                        onToggled: {
                            if (checked !== SettingsController.launchBridgeOnAppStart)
                                SettingsController.setLaunchBridgeOnAppStart(checked)
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
                        implicitWidth: 148
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
