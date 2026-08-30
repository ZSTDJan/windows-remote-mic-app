import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import OvbRc003Settings 1.0

Item {
    id: root
    property var tokens

    ScrollView {
        id: generalScroll
        anchors.fill: parent
        clip: true
        contentWidth: availableWidth
        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff

        ColumnLayout {
            width: Math.max(0, generalScroll.availableWidth
                - tokens.pageHorizontalPadding * 2)
            x: tokens.pageHorizontalPadding
            y: tokens.pageVerticalPadding
            spacing: tokens.spacingMedium

            SectionFrame {
                objectName: "generalBehaviorSection"
                tokens: root.tokens
                Layout.fillWidth: true
                horizontalPadding: 0
                verticalPadding: 0
                contentSpacing: 0

                SettingsListRow {
                    objectName: "launchAtLoginRow"
                    tokens: root.tokens
                    iconGlyph: "\uE7E8"
                    titleText: qsTr("随 Windows 启动")
                    descriptionText: qsTr("登录后在通知区域后台运行 Remote Mic")

                    Switch {
                        id: launchAtLoginSwitch
                        objectName: "launchAtLoginSwitch"
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
                    iconGlyph: "\uE768"
                    titleText: qsTr("启动程序时运行遥控器服务")
                    descriptionText: qsTr("包括随 Windows 启动时自动连接 RC003")

                    Switch {
                        id: launchBridgeOnAppStartSwitch
                        objectName: "launchBridgeOnAppStartSwitch"
                        checked: SettingsController.launchBridgeOnAppStart
                        Accessible.name: qsTr("启动程序时运行遥控器服务")
                        onToggled: {
                            if (checked !== SettingsController.launchBridgeOnAppStart)
                                SettingsController.setLaunchBridgeOnAppStart(checked)
                        }
                    }
                }

                SettingsListRow {
                    objectName: "closeBehaviorRow"
                    tokens: root.tokens
                    iconGlyph: "\uE711"
                    titleText: qsTr("关闭窗口时")
                    descriptionText: qsTr("最小化按钮仍正常保留在任务栏")
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
                    }
                }
            }
        }
    }
}
