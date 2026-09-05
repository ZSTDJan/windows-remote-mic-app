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

    function combinedStatus(checkIds) {
        if (DiagnosticsController.isRefreshing)
            return qsTr("检查中")
        var sawManual = false
        var sawPass = false
        for (var i = 0; i < checkIds.length; i++) {
            const row = checkResult(checkIds[i])
            if (!row)
                continue
            if (row.status === "fail" || row.status === "unsupported")
                return qsTr("需处理")
            if (row.status === "manual")
                sawManual = true
            if (row.status === "pass")
                sawPass = true
        }
        if (sawManual)
            return qsTr("待实测")
        return sawPass ? qsTr("正常") : qsTr("未检查")
    }

    function combinedColor(checkIds) {
        const status = combinedStatus(checkIds)
        if (status === qsTr("正常"))
            return tokens.successColor
        if (status === qsTr("需处理"))
            return tokens.errorColor
        if (status === qsTr("检查中") || status === qsTr("待实测"))
            return tokens.voiceAccent
        return tokens.disabledText
    }

    function combinedDetail(checkIds, fallback) {
        var details = []
        for (var i = 0; i < checkIds.length; i++) {
            const row = checkResult(checkIds[i])
            if (row && String(row.detail).length > 0) {
                const detail = String(row.detail).trim().replace(/[。；;]+$/, "")
                if (detail.length > 0)
                    details.push(detail)
            }
        }
        return details.length > 0 ? details.join(qsTr("；")) : fallback
    }

    function bridgeStateText() {
        if (SettingsController.bridgeReconnectBusy)
            return qsTr("重连中")
        if (SettingsController.bridgeLaunchBusy)
            return qsTr("启动中")
        if (SettingsController.bridgeConnected)
            return qsTr("已连接")
        if (SettingsController.bridgeRunning)
            return qsTr("等待连接")
        if (SettingsController.bridgeLaunchPhase === "unknown")
            return qsTr("需检查")
        return qsTr("未运行")
    }

    function bridgeStateColor() {
        if (SettingsController.bridgeConnected)
            return tokens.successColor
        if (SettingsController.bridgeReconnectBusy
                || SettingsController.bridgeRunning
                || SettingsController.bridgeLaunchBusy
                || SettingsController.bridgeLaunchPhase === "unknown")
            return tokens.voiceAccent
        return tokens.errorColor
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
                    titleText: qsTr("当前设备")
                    descriptionText: root.combinedDetail(
                        ["ble_candidate"],
                        SettingsController.remoteDisplayName
                    )
                    stateText: root.combinedStatus(["ble_candidate"])
                    stateColor: root.combinedColor(["ble_candidate"])

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
                    titleText: qsTr("按键接收")
                    descriptionText: SettingsController.hidHelperIssueVisible
                        ? SettingsController.hidHelperIssueText
                        : root.combinedDetail(
                            ["os_version", "raw_input"],
                            qsTr("检查系统与按键接收")
                        )
                    stateText: SettingsController.hidHelperIssueVisible
                        ? (SettingsController.hidHelperCleanupPending
                            ? qsTr("方向改键可用，待清理")
                            : SettingsController.hidHelperSetupRequired
                            ? qsTr("方向改键未启用")
                            : qsTr("管理员按键组件异常"))
                        : root.combinedStatus(["os_version", "raw_input"])
                    stateColor: SettingsController.hidHelperIssueVisible
                        ? (SettingsController.hidHelperCleanupPending
                            ? tokens.textSecondary : tokens.errorColor)
                        : root.combinedColor(["os_version", "raw_input"])

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
                    titleText: qsTr("遥控器服务")
                    descriptionText: SettingsController.launchStatusText
                    stateText: root.bridgeStateText()
                    stateColor: root.bridgeStateColor()

                    CompactButton {
                        objectName: "restartBridgeButton"
                        visible: SettingsController.bridgeRunning
                            && (SettingsController.bridgeReconnectAvailable
                                || SettingsController.bridgeReconnectBusy
                                || SettingsController.bridgeRestartRecommended)
                        tokens: root.tokens
                        compactMinimumWidth: tokens.buttonWidth4Chars
                        text: SettingsController.bridgeReconnectBusy
                            ? qsTr("连接中…")
                            : SettingsController.bridgeReconnectAvailable
                                ? qsTr("立即重连") : qsTr("重新启动")
                        highlighted: true
                        enabled: !SettingsController.bridgeLaunchBusy
                            && !SettingsController.bridgeReconnectBusy
                        onClicked: SettingsController.bridgeReconnectAvailable
                            ? SettingsController.reconnectBridgeNow()
                            : SettingsController.restartBridge()
                    }

                    CompactButton {
                        objectName: "startBridgeButton"
                        visible: !SettingsController.bridgeRunning
                        tokens: root.tokens
                        compactMinimumWidth: tokens.buttonWidth4Chars
                        text: SettingsController.bridgeLaunchBusy
                            ? qsTr("启动中…") : qsTr("启动桥接")
                        highlighted: true
                        enabled: !SettingsController.bridgeLaunchBusy
                            && !SettingsController.hidHelperRepairBusy
                            && !DiagnosticsController.vbCableTestRunning
                            && !DiagnosticsController.driverActionRunning
                            && !SettingsController.voiceHotkeyBusy
                            && !SettingsController.endpointPreflightBusy
                        onClicked: SettingsController.startBridge()
                    }
                }

                InlineSettingsRow {
                    objectName: "runtimeLogRow"
                    tokens: root.tokens
                    titleText: qsTr("运行日志")
                    descriptionText: qsTr("查看连接、按键、音频和程序日志")
                    showDivider: false

                    CompactButton {
                        objectName: "deviceOpenLogButton"
                        tokens: root.tokens
                        compactMinimumWidth: tokens.buttonWidth4Chars
                        text: qsTr("日志目录")
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
                    descriptionText: qsTr("登录后在通知区域后台运行%1")
                        .arg(SettingsController.applicationDisplayName)

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
                    descriptionText: qsTr("相当于自动点击一次“启动桥接”，与随 Windows 启动互不绑定")

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
                    descriptionText: qsTr("默认完全退出；需要后台运行时可改为隐藏到通知区域")
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
