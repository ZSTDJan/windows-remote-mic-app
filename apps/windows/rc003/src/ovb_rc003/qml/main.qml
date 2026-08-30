import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Qt.labs.platform as Platform
import OvbRc003Settings 1.0

ApplicationWindow {
    id: window
    title: qsTr("%1 设置").arg(SettingsController.applicationDisplayName)
    width: 720
    height: 500
    minimumWidth: 640
    minimumHeight: 480
    visible: true
    readonly property string preferredWindowsUiFont: "Microsoft YaHei UI"
    readonly property bool preferredWindowsUiFontAvailable:
        Qt.platform.os === "windows"
        && Qt.fontFamilies().indexOf(preferredWindowsUiFont) >= 0

    property Tokens tokens: Tokens {
        fontFamily: window.preferredWindowsUiFontAvailable
            ? window.preferredWindowsUiFont : Qt.application.font.family
    }
    property bool initialDiagnosticsStarted: false
    property string applicationExitError: ""

    function restoreWindow() {
        window.show()
        window.raise()
        window.requestActivate()
        SettingsController.refreshBridgeState()
    }

    Component.onCompleted: {
        if (SettingsController.startHidden)
            window.hide()
        SettingsController.startBridgeOnApplicationStart()
    }

    onClosing: function(close) {
        close.accepted = false
        if (SettingsController.closeBehavior === "quit") {
            window.hide()
            SettingsController.requestApplicationExit()
        } else {
            window.hide()
        }
    }

    Connections {
        target: SettingsController
        function onApplicationExitReady() { Qt.quit() }
        function onApplicationExitFailed(message) {
            window.restoreWindow()
            window.applicationExitError = message
            exitFailedDialog.open()
        }
    }

    Dialog {
        id: exitFailedDialog
        objectName: "exitFailedDialog"
        anchors.centerIn: parent
        modal: true
        title: qsTr("无法完全退出")
        standardButtons: Dialog.Ok
        Label {
            width: 360
            text: window.applicationExitError
            wrapMode: Text.Wrap
            color: window.tokens.textPrimary
        }
    }

    Platform.SystemTrayIcon {
        id: systemTrayIcon
        objectName: "systemTrayIcon"
        visible: true
        icon.source: SettingsController.trayIconSource
        tooltip: SettingsController.trayTooltip
        onActivated: function(reason) {
            if (reason === Platform.SystemTrayIcon.Trigger
                    || reason === Platform.SystemTrayIcon.DoubleClick)
                window.restoreWindow()
        }
        menu: Platform.Menu {
            Platform.MenuItem {
                text: qsTr("打开%1").arg(SettingsController.applicationDisplayName)
                onTriggered: window.restoreWindow()
            }
            Platform.MenuSeparator {}
            Platform.MenuItem {
                text: qsTr("完全退出")
                onTriggered: SettingsController.requestApplicationExit()
            }
        }
    }

    onFrameSwapped: {
        if (initialDiagnosticsStarted)
            return
        initialDiagnosticsStarted = true
        DiagnosticsController.startInitialDiagnostics()
    }
    color: tokens.background
    font.family: tokens.fontFamily

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
        interval: SettingsController.bridgeLaunchPhase === "saving"
            || SettingsController.bridgeLaunchPhase === "starting"
            || SettingsController.bridgeLaunchPhase === "waiting"
            ? 1000 : 2000
        repeat: true
        running: true
        onTriggered: SettingsController.refreshBridgeState()
    }

    Timer {
        id: bridgeLaunchPollTimer
        objectName: "bridgeLaunchPollTimer"
        interval: 150
        repeat: true
        running: SettingsController.bridgeLaunchBusy
        onTriggered: SettingsController.pollBridgeLaunch()
    }

    onActiveChanged: {
        if (active) SettingsController.refreshBridgeState()
    }

    Item {
        id: tabBar
        objectName: "tabBar"
        visible: false
        property int currentIndex: 0
        onCurrentIndexChanged:
            SettingsController.activePageIndex = currentIndex
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
                    id: deviceTabButton
                    objectName: "deviceTabButton"
                    tokens: window.tokens
                    text: qsTr("设备")
                    glyph: "\uE71B"
                    checked: tabBar.currentIndex === 0
                    onPressed: tabBar.currentIndex = 0
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
                    onPressed: tabBar.currentIndex = 1
                    Accessible.name: text
                    KeyNavigation.tab: voiceTabButton
                }
                NavButton {
                    id: voiceTabButton
                    objectName: "voiceTabButton"
                    tokens: window.tokens
                    text: qsTr("语音")
                    glyph: "\uE720"
                    checked: tabBar.currentIndex === 2
                    onPressed: tabBar.currentIndex = 2
                    Accessible.name: text
                    KeyNavigation.tab: deviceTabButton
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

                DevicePage {
                    tokens: window.tokens
                    onOpenButtonsRequested: tabBar.currentIndex = 1
                }
                Loader {
                    id: buttonsPageLoader
                    objectName: "buttonsPageLoader"
                    active: tabBar.currentIndex === 1 || status === Loader.Ready
                    sourceComponent: Component {
                        ButtonsPage { tokens: window.tokens }
                    }
                }
                Loader {
                    id: voicePageLoader
                    objectName: "voicePageLoader"
                    active: tabBar.currentIndex === 2 || status === Loader.Ready
                    sourceComponent: Component {
                        VoicePage { tokens: window.tokens }
                    }
                }
            }

            Rectangle {
                id: globalStatusBar
                objectName: "globalStatusBar"
                readonly property bool feedbackBelongsToCurrentPage:
                    SettingsController.feedbackPageIndex === tabBar.currentIndex
                readonly property bool hasError:
                    feedbackBelongsToCurrentPage
                    && SettingsController.errorMessage.length > 0
                readonly property bool hasDirtySettings:
                    tabBar.currentIndex === 1 && SettingsController.settingsDirty
                readonly property bool hasMessage:
                    feedbackBelongsToCurrentPage
                    && SettingsController.statusMessage.length > 0
                readonly property bool hasStatus:
                    hasError || hasDirtySettings || hasMessage
                Layout.fillWidth: true
                Layout.minimumHeight: tokens.statusBarMinHeight
                Layout.preferredHeight: tokens.statusBarMinHeight
                color: hasStatus
                    ? hasError || hasDirtySettings
                        ? tokens.errorBackground : tokens.statusBackground
                    : tokens.background

                Rectangle {
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.top: parent.top
                    height: 1
                    visible: globalStatusBar.hasStatus
                    color: globalStatusBar.hasError
                        || globalStatusBar.hasDirtySettings
                        ? tokens.errorColor : tokens.accent
                }

                Label {
                    id: globalStatusText
                    objectName: "globalStatusText"
                    anchors.fill: parent
                    anchors.leftMargin: tokens.spacingMedium
                    anchors.rightMargin: tokens.spacingMedium
                    visible: globalStatusBar.hasStatus
                    text: globalStatusBar.hasError
                        ? SettingsController.errorMessage
                        : globalStatusBar.hasDirtySettings
                            ? (globalStatusBar.hasMessage
                                ? SettingsController.statusMessage
                                : qsTr("设置已修改，尚未保存。"))
                            : SettingsController.statusMessage
                    color: globalStatusBar.hasError
                        || globalStatusBar.hasDirtySettings
                        ? tokens.errorColor : tokens.textSecondary
                    font.pixelSize: tokens.fontSizeSmall
                    verticalAlignment: Text.AlignVCenter
                    elide: Text.ElideRight
                    Accessible.name: text
                    HoverHandler { id: globalStatusHover }
                    CompactToolTip {
                        tokens: window.tokens
                        active: globalStatusHover.hovered
                            && globalStatusText.truncated
                        text: globalStatusText.text
                        maximumTextWidth: 420
                    }
                }
            }
        }
    }
}
