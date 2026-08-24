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
    property string optionalDriverDetail: ""

    ListModel {
        id: diagnosticsRowsModel
    }

    function refreshRenderedCheckRows() {
        diagnosticsRowsModel.clear()
        optionalDriverDetail = ""
        var results = DiagnosticsController.checkResults
        for (var i = 0; i < results.length; i++) {
            var row = results[i]
            diagnosticsRowsModel.append({
                "checkId": String(row.checkId),
                "title": String(row.title),
                "group": String(row.group),
                "status": String(row.status),
                "detail": String(row.detail)
            })
            if (row.group === groupOptionalDriver)
                optionalDriverDetail = row.title + "：" + row.detail
        }
    }

    function statusLabel(status) {
        if (status === "pass") return qsTr("正常")
        if (status === "fail") return qsTr("未通过")
        if (status === "manual") return qsTr("待实测")
        return qsTr("不可用")
    }

    function statusColor(status) {
        if (status === "pass") return tokens.successColor
        if (status === "fail") return tokens.errorColor
        if (status === "manual") return tokens.voiceAccent
        return tokens.disabledText
    }

    Component.onCompleted: refreshRenderedCheckRows()

    Connections {
        target: DiagnosticsController
        function onCheckResultsChanged() {
            root.refreshRenderedCheckRows()
        }
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
                    horizontalPadding: 9
                    verticalPadding: 7
                    contentSpacing: 6

                    Repeater {
                        model: diagnosticsRowsModel
                        delegate: DiagnosticResultRow {
                            required property string checkId
                            required property string title
                            required property string group
                            required property string status
                            required property string detail

                            objectName: "diagnosticResult_" + checkId
                            visible: group !== root.groupOptionalDriver
                            tokens: root.tokens
                            indicatorColor: root.statusColor(status)
                            titleText: title
                            statusText: root.statusLabel(status)
                            detailText: detail
                            titleColumnWidth: 116
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
                                        : root.optionalDriverDetail.length > 0
                                            ? root.optionalDriverDetail
                                            : qsTr("选择虚拟音频输入，或安装、修复 VB-CABLE。")
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
                        descriptionText: qsTr("打开语音设置或日志；日志不保存语音和设备信息。")
                        showDivider: false
                        CompactButton {
                            objectName: "openSpeechSettingsButton"
                            tokens: root.tokens
                            compactMinimumWidth: 64
                            text: qsTr("语音设置")
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
