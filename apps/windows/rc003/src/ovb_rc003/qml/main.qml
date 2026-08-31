import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Qt.labs.platform as Platform
import OvbRc003Settings 1.0

ApplicationWindow {
    id: window
    title: SettingsController.applicationDisplayName
    width: 720
    height: 560
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
    readonly property color nativeCaptionColor: tokens.windowFrame
    readonly property color nativeCaptionTextColor: tokens.textPrimary
    readonly property color nativeBorderColor: tokens.nativeWindowBorder

    component ClientShellCorner: Item {
        property bool rightSide: false
        property bool bottomSide: false
        property color contentColor: window.tokens.background
        width: window.tokens.windowClientRadius
        height: window.tokens.windowClientRadius
        clip: true

        Rectangle {
            anchors.fill: parent
            color: window.tokens.windowFrame
        }
        Rectangle {
            width: parent.width * 2
            height: parent.height * 2
            x: parent.rightSide ? -parent.width : 0
            y: parent.bottomSide ? -parent.height : 0
            radius: width / 2
            color: parent.contentColor
            antialiasing: true
        }
    }
    property bool initialDiagnosticsStarted: false
    property string applicationExitError: ""
    property string lifecycleErrorTitle: qsTr("操作未完成")
    property int pendingPageIndex: -1
    property bool pendingExitPrompt: false
    property bool applicationExitInProgress: false

    function restoreWindow() {
        window.show()
        window.raise()
        window.requestActivate()
        SettingsController.refreshBridgeState()
    }

    function buttonsPage() {
        return buttonsPageLoader.status === Loader.Ready
            ? buttonsPageLoader.item : null
    }

    function voicePage() {
        return voicePageLoader.status === Loader.Ready
            ? voicePageLoader.item : null
    }

    function currentPageItem() {
        if (tabBar.currentIndex === 0)
            return devicePage
        if (tabBar.currentIndex === 1)
            return buttonsPage()
        return voicePage()
    }

    function currentPageFirstFocusItem() {
        const page = currentPageItem()
        return page ? page.firstFocusItem : null
    }

    function currentPageLastFocusItem() {
        const page = currentPageItem()
        return page ? page.lastFocusItem : null
    }

    function hasPendingMappingDraft() {
        const page = buttonsPage()
        return page ? page.hasPendingEditorDraft : false
    }

    function settleInputUiAfterStop() {
        const mappingPage = buttonsPage()
        if (mappingPage)
            mappingPage.settleInputUiAfterStop()
        const voice = voicePage()
        if (voice)
            voice.settleInputUiAfterStop()
    }

    function showLifecycleError(title, message) {
        restoreWindow()
        lifecycleErrorTitle = title
        applicationExitError = message
        exitFailedDialog.open()
    }

    function requestPage(index) {
        if (index < 0 || index > 2 || index === tabBar.currentIndex
                || SettingsController.settingsSaveBusy
                || unsavedExitDialog.exitCommitInProgress
                || window.applicationExitInProgress)
            return
        pendingExitPrompt = false
        if (SettingsController.inputCaptureInUse) {
            pendingPageIndex = index
            if (!SettingsController.stopInputCapture()) {
                pendingPageIndex = -1
                showLifecycleError(
                    qsTr("无法切换页面"),
                    qsTr("无法停止正在进行的按键录入或检测。"))
            }
            return
        }
        settleInputUiAfterStop()
        pendingPageIndex = -1
        tabBar.currentIndex = index
    }

    function openUnsavedExitPrompt() {
        settleInputUiAfterStop()
        pendingExitPrompt = false
        unsavedExitDialog.saveAttempted = false
        unsavedExitDialog.open()
    }

    function requestWindowHide() {
        SettingsController.prepareForWindowHide()
    }

    function requestFullExit() {
        if (unsavedExitDialog.exitCommitInProgress
                || window.applicationExitInProgress)
            return
        if (SettingsController.settingsSaveBusy) {
            window.applicationExitInProgress = true
            SettingsController.requestApplicationExit()
            return
        }
        if (SettingsController.settingsDirty || hasPendingMappingDraft()) {
            restoreWindow()
            pendingPageIndex = -1
            if (SettingsController.inputCaptureInUse) {
                pendingExitPrompt = true
                if (!SettingsController.stopInputCapture()) {
                    pendingExitPrompt = false
                    showLifecycleError(
                        qsTr("无法准备退出"),
                        qsTr("无法停止正在进行的按键录入或检测。"))
                }
            } else {
                openUnsavedExitPrompt()
            }
            return
        }
        window.applicationExitInProgress = true
        SettingsController.requestApplicationExit()
    }

    function saveAndExit() {
        const page = buttonsPage()
        if (page && !page.commitPendingEditorDraft())
            return
        unsavedExitDialog.saveAttempted = true
        unsavedExitDialog.exitCommitInProgress = true
        window.applicationExitInProgress = true
        SettingsController.saveSettingsAndExit()
    }

    function discardAndExit() {
        const page = buttonsPage()
        if (page)
            page.discardPendingEditorDraft()
        unsavedExitDialog.exitCommitInProgress = true
        window.applicationExitInProgress = true
        unsavedExitDialog.close()
        SettingsController.requestApplicationExit()
    }

    Component.onCompleted: {
        if (SettingsController.startHidden)
            window.hide()
        SettingsController.startBridgeOnApplicationStart()
    }

    onClosing: function(close) {
        if (SettingsController.applicationExitConfirmed) {
            close.accepted = true
            return
        }
        close.accepted = false
        if (SettingsController.closeBehavior === "quit") {
            window.requestFullExit()
        } else {
            window.requestWindowHide()
        }
    }

    Connections {
        target: SettingsController
        function onApplicationExitReady() { Qt.quit() }
        function onApplicationExitFailed(message) {
            window.applicationExitInProgress = false
            unsavedExitDialog.exitCommitInProgress = false
            if (unsavedExitDialog.visible)
                unsavedExitDialog.close()
            window.restoreWindow()
            window.lifecycleErrorTitle = qsTr("无法完全退出")
            window.applicationExitError = message
            exitFailedDialog.open()
        }
        function onSaveSettingsAndExitFinished(saved) {
            if (!saved) {
                window.applicationExitInProgress = false
                unsavedExitDialog.exitCommitInProgress = false
                unsavedExitDialog.saveAttempted = true
                if (!unsavedExitDialog.visible)
                    unsavedExitDialog.open()
            }
        }
        function onInputCleanupReady() {
            window.settleInputUiAfterStop()
            if (window.pendingExitPrompt) {
                window.openUnsavedExitPrompt()
                return
            }
            if (window.pendingPageIndex >= 0) {
                const index = window.pendingPageIndex
                window.pendingPageIndex = -1
                tabBar.currentIndex = index
            }
        }
        function onInputCleanupFailed(message) {
            const exiting = window.pendingExitPrompt
            window.pendingExitPrompt = false
            window.pendingPageIndex = -1
            window.showLifecycleError(
                exiting ? qsTr("无法准备退出") : qsTr("无法切换页面"),
                message)
        }
        function onWindowHideReady() {
            window.settleInputUiAfterStop()
            window.hide()
        }
        function onWindowHideFailed(message) {
            window.restoreWindow()
            window.lifecycleErrorTitle = qsTr("无法隐藏窗口")
            window.applicationExitError = message
            exitFailedDialog.open()
        }
    }

    Dialog {
        id: unsavedExitDialog
        objectName: "unsavedExitDialog"
        anchors.centerIn: parent
        modal: true
        popupType: Popup.Item
        title: qsTr("按键映射尚未保存")
        standardButtons: Dialog.NoButton
        property bool saveAttempted: false
        property bool exitCommitInProgress: false
        closePolicy: SettingsController.settingsSaveBusy
            || exitCommitInProgress ? Popup.NoAutoClose : Popup.CloseOnEscape
        width: Math.min(430, window.width - 32)

        contentItem: ColumnLayout {
            spacing: window.tokens.spacingLarge

            UiLabel {
                tokens: window.tokens
                kind: bodyKind
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                text: SettingsController.settingsSaveBusy
                    ? qsTr("正在保存设置，请稍候…")
                    : unsavedExitDialog.exitCommitInProgress
                        ? qsTr("设置已保存，正在完全退出…")
                        : unsavedExitDialog.saveAttempted
                            && SettingsController.errorMessage.length > 0
                            ? SettingsController.errorMessage
                            : qsTr("退出前要保存本次按键修改吗？")
                color: unsavedExitDialog.saveAttempted
                    && SettingsController.errorMessage.length > 0
                    && !SettingsController.settingsSaveBusy
                    && !unsavedExitDialog.exitCommitInProgress
                    ? window.tokens.errorColor : window.tokens.textPrimary
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: window.tokens.spacingSmall
                Item { Layout.fillWidth: true }
                CompactButton {
                    objectName: "cancelUnsavedExitButton"
                    tokens: window.tokens
                    text: qsTr("取消")
                    enabled: !SettingsController.settingsSaveBusy
                        && !unsavedExitDialog.exitCommitInProgress
                    onClicked: {
                        unsavedExitDialog.saveAttempted = false
                        unsavedExitDialog.close()
                    }
                }
                CompactButton {
                    objectName: "discardUnsavedExitButton"
                    tokens: window.tokens
                    text: qsTr("不保存")
                    enabled: !SettingsController.settingsSaveBusy
                        && !unsavedExitDialog.exitCommitInProgress
                    onClicked: window.discardAndExit()
                }
                CompactButton {
                    objectName: "saveUnsavedExitButton"
                    tokens: window.tokens
                    text: SettingsController.settingsSaveBusy
                        ? qsTr("保存中…") : qsTr("保存并退出")
                    highlighted: true
                    enabled: !SettingsController.settingsSaveBusy
                        && !unsavedExitDialog.exitCommitInProgress
                    onClicked: window.saveAndExit()
                }
            }
        }
    }

    Dialog {
        id: exitFailedDialog
        objectName: "exitFailedDialog"
        anchors.centerIn: parent
        modal: true
        title: window.lifecycleErrorTitle
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
                onTriggered: window.requestFullExit()
            }
        }
    }

    onFrameSwapped: {
        if (initialDiagnosticsStarted)
            return
        initialDiagnosticsStarted = true
        DiagnosticsController.startInitialDiagnostics()
    }
    color: tokens.windowFrame
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

    Rectangle {
        id: clientShell
        objectName: "clientShell"
        anchors.top: parent.top
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        anchors.leftMargin: tokens.windowFrameGap
        anchors.rightMargin: tokens.windowFrameGap
        anchors.bottomMargin: tokens.windowFrameGap
        color: tokens.background
        radius: tokens.windowClientRadius

        RowLayout {
            id: clientContent
            objectName: "clientContent"
            anchors.fill: parent
            spacing: 0

        Rectangle {
            id: navigationBar
            objectName: "navigationBar"
            Layout.preferredWidth: tokens.navigationWidth
            Layout.minimumWidth: tokens.navigationWidth
            Layout.maximumWidth: tokens.navigationWidth
            Layout.fillHeight: true
            enabled: !SettingsController.settingsSaveBusy
                && !unsavedExitDialog.exitCommitInProgress
                && !window.applicationExitInProgress
            color: tokens.sidebar

            Rectangle {
                anchors.right: parent.right
                anchors.top: parent.top
                anchors.bottom: parent.bottom
                width: tokens.structuralDividerWidth
                color: tokens.borderStrong
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
                    onPressed: window.requestPage(0)
                    Accessible.name: text
                    KeyNavigation.tab: mappingTabButton
                    KeyNavigation.backtab: window.currentPageLastFocusItem()
                }
                NavButton {
                    id: mappingTabButton
                    objectName: "mappingTabButton"
                    tokens: window.tokens
                    text: qsTr("按键")
                    glyph: "\uE765"
                    checked: tabBar.currentIndex === 1
                    onPressed: window.requestPage(1)
                    Accessible.name: text
                    KeyNavigation.tab: voiceTabButton
                    KeyNavigation.backtab: deviceTabButton
                }
                NavButton {
                    id: voiceTabButton
                    objectName: "voiceTabButton"
                    tokens: window.tokens
                    text: qsTr("语音")
                    glyph: "\uE720"
                    checked: tabBar.currentIndex === 2
                    onPressed: window.requestPage(2)
                    Accessible.name: text
                    KeyNavigation.tab: window.currentPageFirstFocusItem()
                    KeyNavigation.backtab: mappingTabButton
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
                enabled: !SettingsController.settingsSaveBusy
                    && !unsavedExitDialog.exitCommitInProgress
                    && !window.applicationExitInProgress
                currentIndex: tabBar.currentIndex

                DevicePage {
                    id: devicePage
                    tokens: window.tokens
                    backTabTarget: voiceTabButton
                    tabTarget: deviceTabButton
                    onOpenButtonsRequested: window.requestPage(1)
                }
                Loader {
                    id: buttonsPageLoader
                    objectName: "buttonsPageLoader"
                    active: tabBar.currentIndex === 1 || status === Loader.Ready
                    sourceComponent: Component {
                        ButtonsPage {
                            tokens: window.tokens
                            backTabTarget: voiceTabButton
                            tabTarget: deviceTabButton
                        }
                    }
                }
                Loader {
                    id: voicePageLoader
                    objectName: "voicePageLoader"
                    active: tabBar.currentIndex === 2 || status === Loader.Ready
                    sourceComponent: Component {
                        VoicePage {
                            tokens: window.tokens
                            backTabTarget: voiceTabButton
                            tabTarget: deviceTabButton
                        }
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

        ClientShellCorner {
            anchors.top: parent.top
            anchors.left: parent.left
            contentColor: tokens.sidebar
            z: 10
        }
        ClientShellCorner {
            anchors.top: parent.top
            anchors.right: parent.right
            rightSide: true
            contentColor: tokens.background
            z: 10
        }
        ClientShellCorner {
            anchors.bottom: parent.bottom
            anchors.left: parent.left
            bottomSide: true
            contentColor: tokens.sidebar
            z: 10
        }
        ClientShellCorner {
            anchors.bottom: parent.bottom
            anchors.right: parent.right
            rightSide: true
            bottomSide: true
            contentColor: globalStatusBar.color
            z: 10
        }

        Rectangle {
            id: clientShellOutline
            objectName: "clientShellOutline"
            anchors.fill: parent
            color: "transparent"
            radius: tokens.windowClientRadius
            border.width: tokens.hairlineWidth
            border.color: tokens.windowFrameBorder
            antialiasing: true
            z: 20
        }
    }
}
