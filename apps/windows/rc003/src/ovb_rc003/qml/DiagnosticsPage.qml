import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import OvbRc003Settings 1.0

Item {
    id: root
    property var tokens

    readonly property string groupOrdinaryButtons: "ordinary_buttons"
    readonly property string groupVoiceBridge: "voice_bridge"
    readonly property string groupDictation: "dictation"
    readonly property string groupOptionalDriver: "optional_driver"
    readonly property string groupExternalMicrophone: "external_microphone"

    function rowsForGroups(groupIds) {
        var rows = []
        var results = DiagnosticsController.checkResults
        for (var i = 0; i < results.length; i++) {
            if (groupIds.indexOf(results[i].group) >= 0)
                rows.push(results[i])
        }
        return rows
    }

    function statusLabel(status) {
        if (status === "pass") return qsTr("正常")
        if (status === "fail") return qsTr("未通过")
        if (status === "manual") return qsTr("待实测")
        return qsTr("不可用")
    }

    function groupStatus(groupIds) {
        var rows = rowsForGroups(groupIds)
        if (DiagnosticsController.isRefreshing)
            return "refreshing"
        if (rows.length === 0)
            return "pending"
        var hasManual = false
        var hasUnsupported = false
        for (var i = 0; i < rows.length; i++) {
            if (rows[i].status === "fail") return "fail"
            if (rows[i].status === "manual") hasManual = true
            if (rows[i].status !== "pass" && rows[i].status !== "manual") hasUnsupported = true
        }
        if (hasManual) return "manual"
        if (hasUnsupported) return "unsupported"
        return "pass"
    }

    function groupStatusText(groupIds) {
        var status = groupStatus(groupIds)
        if (status === "refreshing") return qsTr("检查中")
        if (status === "pending") return qsTr("尚未检查")
        if (status === "pass") return qsTr("正常")
        if (status === "fail") return qsTr("有问题")
        if (status === "manual") return qsTr("待实测")
        return qsTr("部分不可用")
    }

    function groupStatusColor(groupIds) {
        var status = groupStatus(groupIds)
        if (status === "pass") return tokens.successColor
        if (status === "fail") return tokens.errorColor
        if (status === "manual") return tokens.voiceAccent
        return tokens.disabledText
    }

    function groupDetail(groupIds, fallback) {
        var rows = rowsForGroups(groupIds)
        if (rows.length === 0)
            return fallback
        var details = []
        for (var i = 0; i < rows.length; i++)
            details.push(rows[i].title + "：" + statusLabel(rows[i].status))
        return details.join("；")
    }

    Dialog {
        id: driverConfirmDialog
        objectName: "driverConfirmDialog"
        title: qsTr("启动 VB-CABLE 官方安装程序？")
        modal: true
        anchors.centerIn: parent
        standardButtons: Dialog.Ok | Dialog.Cancel
        onAccepted: DiagnosticsController.launchVbCableSetup()

        UiLabel {
            tokens: root.tokens
            kind: bodyKind
            width: 360
            wrapMode: Text.WordWrap
            text: qsTr("将启动 VB-Audio 官方 VB-CABLE 安装程序，并请求管理员权限。安装或卸载会改变系统状态，完成后需要重启电脑。确定继续吗？")
        }
    }

    ScrollView {
        id: diagnosticsScroll
        objectName: "diagnosticsScroll"
        anchors.fill: parent
        clip: true
        contentWidth: availableWidth
        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff

        ColumnLayout {
            id: diagnosticsPageContent
            objectName: "diagnosticsPageContent"
            width: Math.max(0, diagnosticsScroll.availableWidth - tokens.pageHorizontalPadding * 2)
            x: tokens.pageHorizontalPadding
            y: tokens.pageVerticalPadding
            spacing: tokens.spacingLarge

            SectionFrame {
                tokens: root.tokens
                Layout.fillWidth: true
                Layout.preferredHeight: 44
                horizontalPadding: 9
                verticalPadding: 6

                RowLayout {
                    Layout.fillWidth: true
                    spacing: 7
                    ColumnLayout {
                        spacing: 1
                        UiLabel {
                            tokens: root.tokens
                            kind: bodyKind
                            text: DiagnosticsController.isRefreshing
                                ? qsTr("正在检查")
                                : DiagnosticsController.diagnosticsErrorMessage.length > 0
                                    ? qsTr("检查失败")
                                    : DiagnosticsController.checkResults.length > 0
                                        ? qsTr("检查已完成") : qsTr("尚未运行检查")
                            font.weight: Font.Medium
                        }
                        UiLabel { tokens: root.tokens; kind: noteKind; text: qsTr("检查系统、遥控器和语音连接") }
                    }
                    Item { Layout.fillWidth: true }
                    BusyIndicator {
                        id: refreshBusyIndicator
                        objectName: "refreshBusyIndicator"
                        running: DiagnosticsController.isRefreshing
                        visible: running
                        implicitWidth: 20
                        implicitHeight: 20
                    }
                    CompactButton {
                        id: refreshButton
                        objectName: "refreshButton"
                        tokens: root.tokens
                        text: DiagnosticsController.checkResults.length > 0 ? qsTr("重新检查") : qsTr("开始检查")
                        highlighted: true
                        enabled: !DiagnosticsController.isRefreshing
                        onClicked: DiagnosticsController.refreshDiagnostics()
                    }
                }
            }

            Rectangle {
                id: diagnosticsErrorBanner
                objectName: "diagnosticsErrorBanner"
                Layout.fillWidth: true
                visible: DiagnosticsController.diagnosticsErrorMessage.length > 0
                radius: tokens.cornerRadiusSmall
                color: tokens.errorBackground
                border.color: tokens.errorColor
                border.width: 1
                implicitHeight: diagnosticsErrorLabel.implicitHeight + 12
                UiLabel {
                    id: diagnosticsErrorLabel
                    anchors.fill: parent
                    anchors.margins: 6
                    tokens: root.tokens
                    kind: noteKind
                    color: tokens.errorColor
                    wrapMode: Text.WordWrap
                    text: DiagnosticsController.diagnosticsErrorMessage
                }
            }

            ColumnLayout {
                Layout.fillWidth: true
                spacing: 5
                UiLabel { tokens: root.tokens; kind: sectionTitleKind; text: qsTr("自动检查") }
                SectionFrame {
                    tokens: root.tokens
                    Layout.fillWidth: true
                    horizontalPadding: 0
                    verticalPadding: 0
                    contentSpacing: 0

                    SettingsListRow {
                        tokens: root.tokens
                        iconGlyph: "\uE713"
                        titleText: qsTr("运行环境")
                        descriptionText: root.groupDetail(
                            [root.groupOrdinaryButtons],
                            qsTr("检查系统版本、程序目录、声音支持和日志位置。")
                        )
                        UiLabel {
                            tokens: root.tokens
                            kind: noteKind
                            text: root.groupStatusText([root.groupOrdinaryButtons])
                            color: root.groupStatusColor([root.groupOrdinaryButtons])
                        }
                    }
                    SettingsListRow {
                        tokens: root.tokens
                        iconGlyph: "\uE702"
                        titleText: qsTr("遥控器连接")
                        descriptionText: root.groupDetail(
                            [root.groupVoiceBridge],
                            qsTr("检查蓝牙连接、后台程序和特殊按键。")
                        )
                        UiLabel {
                            tokens: root.tokens
                            kind: noteKind
                            text: root.groupStatusText([root.groupVoiceBridge])
                            color: root.groupStatusColor([root.groupVoiceBridge])
                        }
                    }
                    SettingsListRow {
                        tokens: root.tokens
                        iconGlyph: "\uE767"
                        titleText: qsTr("语音传输")
                        descriptionText: root.groupDetail(
                            [root.groupExternalMicrophone, root.groupDictation],
                            qsTr("检查语音输出；文字能否输入仍需实测。")
                        )
                        showDivider: false
                        UiLabel {
                            tokens: root.tokens
                            kind: noteKind
                            text: root.groupStatusText([root.groupExternalMicrophone, root.groupDictation])
                            color: root.groupStatusColor([root.groupExternalMicrophone, root.groupDictation])
                        }
                    }
                }
            }

            ColumnLayout {
                Layout.fillWidth: true
                spacing: 5
                UiLabel { tokens: root.tokens; kind: sectionTitleKind; text: qsTr("修复与快捷入口") }
                SectionFrame {
                    tokens: root.tokens
                    Layout.fillWidth: true
                    horizontalPadding: 0
                    verticalPadding: 0
                    contentSpacing: 0

                    SettingsListRow {
                        id: optionalDriverSection
                        objectName: "optionalDriverSection"
                        tokens: root.tokens
                        iconGlyph: "\uE95E"
                        titleText: qsTr("虚拟音频")
                        descriptionText: DiagnosticsController.driverErrorMessage.length > 0
                            ? DiagnosticsController.driverErrorMessage
                            : DiagnosticsController.driverStatusMessage.length > 0
                                ? DiagnosticsController.driverStatusMessage
                                : DiagnosticsController.driverInfoMessage.length > 0
                                    ? DiagnosticsController.driverInfoMessage
                                    : root.groupDetail([root.groupOptionalDriver], qsTr("选择虚拟音频输入，或安装、修复 VB-CABLE。"))
                        CompactButton {
                            objectName: "selectCableInputButton"
                            tokens: root.tokens
                            compactMinimumWidth: 64
                            text: qsTr("选择端点")
                            onClicked: DiagnosticsController.selectDetectedCableInputAsOutput()
                        }
                        CompactButton {
                            objectName: "launchDriverSetupButton"
                            tokens: root.tokens
                            compactMinimumWidth: 64
                            text: qsTr("安装修复")
                            onClicked: driverConfirmDialog.open()
                        }
                    }

                    SettingsListRow {
                        id: diagnosticsFooterSection
                        objectName: "diagnosticsFooterSection"
                        tokens: root.tokens
                        iconGlyph: "\uE8B7"
                        titleText: qsTr("系统与日志")
                        descriptionText: qsTr("打开系统设置或日志；日志不保存语音和设备信息。")
                        showDivider: false
                        CompactButton {
                            objectName: "openAppsSettingsButton"
                            tokens: root.tokens
                            compactMinimumWidth: 64
                            text: qsTr("系统设置")
                            onClicked: SettingsController.openSpeechSettings()
                        }
                        CompactButton {
                            objectName: "diagnosticsOpenLogButton"
                            tokens: root.tokens
                            compactMinimumWidth: 64
                            text: qsTr("日志目录")
                            onClicked: SettingsController.openLogLocation()
                        }
                    }
                }
            }

            Item { Layout.preferredHeight: tokens.pageVerticalPadding }
        }
    }
}
