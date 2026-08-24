// Top-level Windows settings window (XRBM-030 In-scope item 2). It uses a
// native Qt Quick window and Windows' own title-bar/Fluent chrome.
//
// `SettingsController`/`ButtonMappingModel` below are QML SINGLETON types
// (registered via qmlRegisterSingletonInstance in
// qt_settings_app.run_settings_window(), imported from the
// "OvbRc003Settings" module) - deliberately not exposed as
// engine.rootContext() context properties. During this task, an
// isolated repro proved that a context property can read back as null the
// first time it is accessed from a binding evaluated during a nested
// component's own construction (e.g. inside a ScrollView's deferred content,
// or a ListView's currentIndex binding, before an externally-supplied
// property has finished propagating down to it) - a QML singleton has no
// such hazard, since every file that imports the module gets the same
// already-fully-constructed instance immediately, resolved once by the
// type system rather than walked through a context hierarchy each time.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import OvbRc003Settings 1.0

ApplicationWindow {
    id: window
    title: qsTr("Remote Mic 设置")
    // Compact default that still preserves the full tab bar and a 640 px
    // mapping matrix beside the fixed product-photo sidebar.
    width: 840
    height: 720
    minimumWidth: 640
    minimumHeight: 480
    visible: true

    property Tokens tokens: Tokens {}

    color: tokens.background

    Timer {
        id: bridgeStatusRefreshTimer
        objectName: "bridgeStatusRefreshTimer"
        interval: 2000
        repeat: true
        running: window.visible
        onTriggered: SettingsController.refreshBridgeState()
    }

    onActiveChanged: {
        if (active) {
            SettingsController.refreshBridgeState()
        }
    }

    // XRBM-030 RETRY 1 blocker 2: Qt Quick Controls' "FluentWinUI3" style
    // resolves its OWN default text/background colors from
    // Qt.styleHints.colorScheme independently of Tokens.qml's colors (which
    // come from the plain QtQuick `SystemPalette` type instead) - under the
    // offscreen QPA platform (no real desktop/compositor), colorScheme
    // reports `Unknown`, and FluentWinUI3 falls back to a DARK-styled
    // control appearance (white button/tab/field text) while SystemPalette
    // separately falls back to a LIGHT one (white base, black text) - two
    // independent "default" guesses that disagree, producing white text on
    // a light background. Explicitly setting this window's own `palette`
    // (which Qt Quick Controls propagates down to every descendant Button/
    // TabButton/ComboBox/TextField automatically, verified by a minimal
    // isolated repro during this task) from the SAME SystemPalette-derived
    // tokens used for the rest of this window keeps both systems reading
    // from one source of truth in both light AND dark real Windows
    // sessions - this does not hard-code a light-only palette, since every
    // value below is itself OS-derived via Tokens.qml.
    palette.window: tokens.background
    palette.windowText: tokens.textPrimary
    palette.button: tokens.buttonBackground
    palette.buttonText: tokens.buttonText
    palette.base: tokens.fieldBackground
    palette.text: tokens.textPrimary
    palette.highlight: tokens.accent
    palette.highlightedText: tokens.accentText

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        Rectangle {
            id: navigationBar
            objectName: "navigationBar"
            Layout.fillWidth: true
            Layout.preferredHeight: tokens.navigationHeight
            color: tokens.surface

            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: tokens.pageHorizontalPadding
                anchors.rightMargin: tokens.pageHorizontalPadding
                spacing: tokens.spacingLarge

                ColumnLayout {
                    Layout.preferredWidth: 150
                    Layout.maximumWidth: 170
                    spacing: 0

                    Label {
                        text: qsTr("Remote Mic")
                        color: tokens.textPrimary
                        font.pixelSize: tokens.fontSizeTitle
                        font.bold: true
                    }
                    Label {
                        text: qsTr("Windows 设置")
                        color: tokens.textSecondary
                        font.pixelSize: tokens.fontSizeSmall
                    }
                }

                TabBar {
                    id: tabBar
                    objectName: "tabBar"  // lets tooling (e.g. a screenshot script) drive tab switching via QObject.findChild
                    Layout.fillWidth: true
                    Layout.preferredWidth: 620
                    Layout.maximumWidth: 620
                    Layout.alignment: Qt.AlignRight
                    implicitWidth: 620
                    background: Item {}

                    TabButton {
                        width: tabBar.width / 4
                        objectName: "connectionTabButton"  // test hook: for the rendered contrast regression test
                        text: qsTr("连接")
                        Accessible.name: text
                    }
                    TabButton {
                        width: tabBar.width / 4
                        objectName: "mappingTabButton"
                        text: SettingsController.mappingPageTitle
                        Accessible.name: text
                    }
                    TabButton {
                        width: tabBar.width / 4
                        objectName: "permissionsTabButton"
                        text: qsTr("权限")
                        Accessible.name: text
                    }
                    TabButton {
                        width: tabBar.width / 4
                        objectName: "diagnosticsTabButton"  // test hook: for driving this tab in the offscreen screenshot/interaction tests
                        text: qsTr("检查与修复")
                        Accessible.name: text
                    }
                }
            }

            Rectangle {
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.bottom: parent.bottom
                height: 1
                color: tokens.border
            }
        }

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
            Layout.preferredHeight: visible
                ? Math.max(tokens.statusBarMinHeight, globalStatusText.implicitHeight + tokens.spacingSmall * 2)
                : 0
            visible: SettingsController.errorMessage.length > 0
                || SettingsController.settingsDirty
                || SettingsController.statusMessage.length > 0
            color: SettingsController.errorMessage.length > 0
                || SettingsController.settingsDirty
                ? tokens.errorBackground
                : tokens.statusBackground

            Rectangle {
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.top: parent.top
                height: 1
                color: SettingsController.errorMessage.length > 0
                    || SettingsController.settingsDirty
                    ? tokens.errorColor
                    : tokens.accent
            }

            Label {
                id: globalStatusText
                objectName: "globalStatusText"
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                anchors.leftMargin: tokens.pageHorizontalPadding
                anchors.rightMargin: tokens.pageHorizontalPadding
                text: SettingsController.errorMessage.length > 0
                    ? SettingsController.errorMessage
                    : SettingsController.settingsDirty
                        ? (SettingsController.statusMessage.length > 0
                            ? SettingsController.statusMessage
                            : qsTr("设置已修改，尚未保存。"))
                    : SettingsController.statusMessage
                color: SettingsController.errorMessage.length > 0
                    || SettingsController.settingsDirty
                    ? tokens.errorColor
                    : tokens.textPrimary
                font.pixelSize: tokens.fontSizeSmall
                wrapMode: Text.WordWrap
                Accessible.name: text
            }
        }
    }
}
