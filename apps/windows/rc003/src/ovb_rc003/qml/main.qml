import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import OvbRc003Settings 1.0

ApplicationWindow {
    id: window
    title: qsTr("Remote Mic 设置")
    width: 720
    height: 464
    minimumWidth: 640
    minimumHeight: 440
    visible: true

    property Tokens tokens: Tokens {}
    color: tokens.background

    palette.window: tokens.background
    palette.windowText: tokens.textPrimary
    palette.button: tokens.buttonBackground
    palette.buttonText: tokens.buttonText
    palette.base: tokens.fieldBackground
    palette.text: tokens.textPrimary
    palette.highlight: tokens.accent
    palette.highlightedText: tokens.accentText

    Timer {
        id: bridgeStatusRefreshTimer
        objectName: "bridgeStatusRefreshTimer"
        interval: 2000
        repeat: true
        running: window.visible
        onTriggered: SettingsController.refreshBridgeState()
    }

    onActiveChanged: {
        if (active) SettingsController.refreshBridgeState()
    }

    Item {
        id: tabBar
        objectName: "tabBar"
        visible: false
        property int currentIndex: 0
    }

    RowLayout {
        anchors.fill: parent
        spacing: 0

        Rectangle {
            id: navigationBar
            objectName: "navigationBar"
            Layout.preferredWidth: tokens.navigationWidth
            Layout.minimumWidth: tokens.navigationWidth
            Layout.maximumWidth: tokens.navigationWidth
            Layout.fillHeight: true
            color: tokens.sidebar

            Rectangle {
                anchors.right: parent.right
                anchors.top: parent.top
                anchors.bottom: parent.bottom
                width: 1
                color: tokens.border
            }

            Column {
                anchors.top: parent.top
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.topMargin: 8
                anchors.leftMargin: 4
                anchors.rightMargin: 4
                spacing: 3

                NavButton {
                    id: connectionTabButton
                    objectName: "connectionTabButton"
                    tokens: window.tokens
                    text: qsTr("连接")
                    glyph: "\uE71B"
                    checked: tabBar.currentIndex === 0
                    onClicked: tabBar.currentIndex = 0
                    Accessible.name: text
                    KeyNavigation.tab: mappingTabButton
                }
                NavButton {
                    id: mappingTabButton
                    objectName: "mappingTabButton"
                    tokens: window.tokens
                    text: qsTr("按键")
                    glyph: "\uE765"
                    checked: tabBar.currentIndex === 1
                    onClicked: tabBar.currentIndex = 1
                    Accessible.name: text
                    KeyNavigation.tab: permissionsTabButton
                }
                NavButton {
                    id: permissionsTabButton
                    objectName: "permissionsTabButton"
                    tokens: window.tokens
                    text: qsTr("权限")
                    glyph: "\uEA18"
                    checked: tabBar.currentIndex === 2
                    onClicked: tabBar.currentIndex = 2
                    Accessible.name: text
                    KeyNavigation.tab: diagnosticsTabButton
                }
                NavButton {
                    id: diagnosticsTabButton
                    objectName: "diagnosticsTabButton"
                    tokens: window.tokens
                    text: qsTr("诊断")
                    glyph: "\uE90F"
                    checked: tabBar.currentIndex === 3
                    onClicked: tabBar.currentIndex = 3
                    Accessible.name: text
                    KeyNavigation.tab: connectionTabButton
                }
            }
        }

        ColumnLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: 0

            StackLayout {
                id: pageStack
                Layout.fillWidth: true
                Layout.fillHeight: true
                currentIndex: tabBar.currentIndex

                ConnectionPage { tokens: window.tokens }
                ButtonsPage { tokens: window.tokens }
                PermissionsPage {
                    tokens: window.tokens
                    onOpenMappingRequested: tabBar.currentIndex = 1
                    onOpenDiagnosticsRequested: tabBar.currentIndex = 3
                }
                DiagnosticsPage { tokens: window.tokens }
            }

            Rectangle {
                id: globalStatusBar
                objectName: "globalStatusBar"
                Layout.fillWidth: true
                Layout.minimumHeight: visible ? tokens.statusBarMinHeight : 0
                Layout.preferredHeight: visible ? tokens.statusBarMinHeight : 0
                visible: SettingsController.errorMessage.length > 0
                    || SettingsController.settingsDirty
                    || SettingsController.statusMessage.length > 0
                color: SettingsController.errorMessage.length > 0
                    || SettingsController.settingsDirty
                    ? tokens.errorBackground : tokens.statusBackground

                Rectangle {
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.top: parent.top
                    height: 1
                    color: SettingsController.errorMessage.length > 0
                        || SettingsController.settingsDirty
                        ? tokens.errorColor : tokens.accent
                }

                Label {
                    id: globalStatusText
                    objectName: "globalStatusText"
                    anchors.fill: parent
                    anchors.leftMargin: tokens.spacingMedium
                    anchors.rightMargin: tokens.spacingMedium
                    text: SettingsController.errorMessage.length > 0
                        ? SettingsController.errorMessage
                        : SettingsController.settingsDirty
                            ? (SettingsController.statusMessage.length > 0
                                ? SettingsController.statusMessage
                                : qsTr("设置已修改，尚未保存。"))
                            : SettingsController.statusMessage
                    color: SettingsController.errorMessage.length > 0
                        || SettingsController.settingsDirty
                        ? tokens.errorColor : tokens.textSecondary
                    font.pixelSize: tokens.fontSizeSmall
                    verticalAlignment: Text.AlignVCenter
                    elide: Text.ElideRight
                    Accessible.name: text
                }
            }
        }
    }
}
