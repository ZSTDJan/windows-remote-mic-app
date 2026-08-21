// "按键" tab (XRBM-030 In-scope items 4/5): the RC003 product photo with 13
// clickable hotspots calibrated for the bundled RC003 photo (see
// remote_layout.py) on
// the left, the compact two-column mapping grid (Chinese name / HID usage /
// current action) on the right. Both sides are two views over the SAME ButtonMappingModel row,
// kept in sync through SettingsController.selectButton()/
// SettingsController.selectedButtonId - clicking either one updates both
// (In-scope item 4's "双向定位"). SettingsController/ButtonMappingModel are
// QML singletons - see main.qml's module docstring for why.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import OvbRc003Settings 1.0

Item {
    id: root
    property var tokens

    readonly property real photoAspectRatio: 1030 / 508

    function openShortcutRecorder(buttonId, rowIndex, trigger, voiceMode) {
        shortcutRecorder.buttonId = buttonId
        shortcutRecorder.rowIndex = rowIndex
        shortcutRecorder.trigger = trigger || "single_click"
        shortcutRecorder.voiceMode = voiceMode || ""
        shortcutRecorder.previewText = voiceMode
            ? qsTr("请按下输入法中已设置的语音快捷键")
            : qsTr("请按下希望遥控器发送的键盘快捷键")
        shortcutRecorder.open()
    }

    Timer {
        interval: 100
        repeat: true
        running: SettingsController.keyDetectionActive
        onTriggered: SettingsController.pollKeyDetectionBridge()
    }

    Dialog {
        id: shortcutRecorder
        objectName: "shortcutRecorderDialog"
        modal: true
        anchors.centerIn: parent
        width: 430
        title: qsTr("录制自定义快捷键")
        standardButtons: Dialog.Cancel
        property string buttonId: ""
        property int rowIndex: -1
        property string trigger: "single_click"
        property string voiceMode: ""
        property string previewText: ""

        function commitShortcut(chord) {
            previewText = chord
            if (voiceMode === "toggle")
                SettingsController.toggleVoiceHotkeyText = chord
            else if (voiceMode === "hold")
                SettingsController.holdVoiceHotkeyText = chord
            else if (trigger === "single_click")
                ButtonMappingModel.setActionTextAt(rowIndex, chord)
            else
                ButtonMappingModel.setSecondaryActionTextAt(rowIndex, trigger, chord)
            close()
        }

        onOpened: {
            captureArea.forceActiveFocus()
            SettingsController.startHotkeyCapture()
        }

        onClosed: SettingsController.stopHotkeyCapture()

        Connections {
            target: SettingsController
            function onHotkeyCaptured(chord) {
                if (shortcutRecorder.visible)
                    shortcutRecorder.commitShortcut(chord)
            }
            function onHotkeyCaptureError(message) {
                if (shortcutRecorder.visible)
                    shortcutRecorder.previewText = message
            }
        }

        contentItem: FocusScope {
            id: captureArea
            implicitHeight: 150
            focus: true

            ColumnLayout {
                anchors.fill: parent
                spacing: tokens.spacingMedium
                Label {
                    Layout.fillWidth: true
                    horizontalAlignment: Text.AlignHCenter
                    text: shortcutRecorder.previewText
                    font.pixelSize: tokens.fontSizeTitle
                    color: tokens.accent
                }
                Label {
                    Layout.fillWidth: true
                    wrapMode: Text.WordWrap
                    horizontalAlignment: Text.AlignHCenter
                    text: shortcutRecorder.voiceMode
                        ? qsTr("请在电脑键盘上按下输入法已配置的语音快捷键；本次只记录按键组合。")
                        : qsTr("请在电脑键盘上按下希望遥控器发送的单键或组合键；左右修饰键会分别记录。")
                    color: tokens.textSecondary
                    font.pixelSize: tokens.fontSizeSmall
                }
            }
        }
    }

    RowLayout {
        id: rc003MappingLayout
        objectName: "rc003MappingLayout"
        visible: SettingsController.isRc003Device
        anchors.fill: parent
        anchors.margins: tokens.spacingMedium
        spacing: tokens.spacingMedium

        // -- Left: product photo with hotspots -----------------------------
        ColumnLayout {
            Layout.preferredWidth: 215
            Layout.fillHeight: true
            spacing: tokens.spacingSmall

            Rectangle {
                id: photoFrame
                Layout.preferredWidth: 200
                Layout.preferredHeight: 200 * root.photoAspectRatio
                Layout.alignment: Qt.AlignHCenter | Qt.AlignTop
                radius: tokens.cornerRadiusLarge
                color: tokens.surface
                border.color: tokens.border
                border.width: 1
                clip: true

                Image {
                    id: photoImage
                    objectName: "photoImage"  // stable hook so a real-QML test can read paintedWidth/paintedHeight and the letterbox offset to verify hotspot centers
                    anchors.fill: parent
                    anchors.margins: 8
                    fillMode: Image.PreserveAspectFit
                    source: SettingsController.photoAvailable ? SettingsController.photoSource : ""
                    visible: SettingsController.photoAvailable
                    smooth: true
                    asynchronous: true
                }

                Label {
                    anchors.centerIn: parent
                    anchors.margins: tokens.spacingMedium
                    width: parent.width - tokens.spacingMedium * 2
                    horizontalAlignment: Text.AlignHCenter
                    wrapMode: Text.WordWrap
                    visible: !SettingsController.photoAvailable
                    text: qsTr("实物图资源缺失")
                    color: tokens.textSecondary
                    font.pixelSize: tokens.fontSizeSmall
                }

                Repeater {
                    model: ButtonMappingModel

                    delegate: Item {
                        id: hotspot

                        // Stable, device-identifier-free hook so a real-QML
                        // test can locate any one hotspot Item by button (see
                        // tests/test_qt_settings_app.py). buttonId is an
                        // internal action id ("ok", "power", ...), never a
                        // hardware/BLE identifier.
                        objectName: "photoHotspot_" + buttonId

                        required property string buttonId
                        required property string displayName
                        required property string actionText
                        required property real hotspotX
                        required property real hotspotY
                        required property real hotspotWidth
                        required property real hotspotHeight
                        required property bool isSelected

                        readonly property string normalizedActionText: actionText.trim()
                        readonly property bool primaryIsVoice:
                            normalizedActionText === "开关型语音"
                            || normalizedActionText === "按住型语音"
                            || normalizedActionText === "语音（使用专用组合键）"

                        readonly property real paintedW: photoImage.paintedWidth
                        readonly property real paintedH: photoImage.paintedHeight
                        readonly property real offsetX: photoImage.x + (photoImage.width - paintedW) / 2
                        readonly property real offsetY: photoImage.y + (photoImage.height - paintedH) / 2

                        // hotspotX/hotspotY are the hotspot's CENTER as a
                        // fraction of the painted photo. A QML Item's x/y are
                        // its TOP-LEFT, so convert center -> top-left by
                        // subtracting half the item's own width/height while
                        // preserving the letterbox offset.
                        width: hotspotWidth * paintedW
                        height: hotspotHeight * paintedH
                        x: offsetX + hotspotX * paintedW - width / 2
                        y: offsetY + hotspotY * paintedH - height / 2
                        visible: SettingsController.photoAvailable

                        activeFocusOnTab: true
                        Accessible.role: Accessible.Button
                        Accessible.name: displayName

                        Rectangle {
                            anchors.fill: parent
                            radius: height / 2
                            color: hotspot.isSelected
                                ? Qt.rgba(tokens.accent.r, tokens.accent.g, tokens.accent.b, 0.28)
                                : (hoverHandler.hovered
                                    ? Qt.rgba(tokens.accent.r, tokens.accent.g, tokens.accent.b, 0.14)
                                    : "transparent")
                            border.width: hotspot.isSelected ? 2 : (hotspot.activeFocus ? 1 : 0)
                            border.color: hotspot.primaryIsVoice
                                ? tokens.voiceAccent : tokens.accent
                        }

                        HoverHandler { id: hoverHandler }
                        TapHandler { onTapped: SettingsController.selectButton(hotspot.buttonId) }
                        Keys.onReturnPressed: SettingsController.selectButton(hotspot.buttonId)
                        Keys.onSpacePressed: SettingsController.selectButton(hotspot.buttonId)
                    }
                }
            }

            Label {
                Layout.preferredWidth: 205
                Layout.alignment: Qt.AlignHCenter
                wrapMode: Text.WordWrap
                horizontalAlignment: Text.AlignHCenter
                text: qsTr("点击实物按键定位映射；任意实体按键都可设置为普通动作、开关型语音或按住型语音。遥控器没有独立静音键。")
                color: tokens.textSecondary
                font.pixelSize: tokens.fontSizeSmall
            }
        }

            // -- Right: compact reference-style mapping grid -------------------
        ColumnLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: tokens.spacingSmall

            RowLayout {
                Layout.fillWidth: true
                Label {
                    Layout.fillWidth: true
                    text: qsTr("按键映射")
                    font.pixelSize: tokens.fontSizeTitle
                    font.bold: true
                    color: tokens.textPrimary
                }
                Button {
                    text: qsTr("恢复默认")
                    onClicked: SettingsController.restoreDefaults()
                }
                Button {
                    // XRBM-030 RETRY 1 blocker 4: without this button, a
                    // mapping edit made on this page could only actually be
                    // persisted by switching to "连接" and clicking "仅保存
                    // 设置" - a user who edits a mapping and just closes the
                    // window loses it. Calls the exact same
                    // SettingsController.saveSettings() slot the "连接" page
                    // uses (same validation, same config.save_*() calls) -
                    // no separate/duplicated save path.
                    id: saveMappingButton
                    objectName: "saveMappingButton"
                    text: qsTr("保存映射")
                    highlighted: true
                    onClicked: SettingsController.saveSettings()
                }
            }

            Rectangle {
                Layout.fillWidth: true
                implicitHeight: detectionRow.implicitHeight + tokens.spacingMedium * 2
                radius: tokens.cornerRadiusSmall
                color: tokens.fieldBackground
                border.color: SettingsController.keyDetectionActive
                    ? tokens.accent : tokens.border
                border.width: SettingsController.keyDetectionActive ? 2 : 1

                RowLayout {
                    id: detectionRow
                    anchors.fill: parent
                    anchors.margins: tokens.spacingMedium
                    spacing: tokens.spacingMedium

                    Button {
                        id: detectRealKeyButton
                        objectName: "detectRealKeyButton"
                        text: SettingsController.keyDetectionActive
                            ? qsTr("停止检测") : qsTr("检测真实按键")
                        highlighted: SettingsController.keyDetectionActive
                        onClicked: SettingsController.keyDetectionActive
                            ? SettingsController.stopKeyDetection()
                            : SettingsController.startKeyDetection()
                        Accessible.name: qsTr("检测真实遥控器按键")
                    }

                    Label {
                        Layout.fillWidth: true
                        wrapMode: Text.WordWrap
                        text: SettingsController.keyDetectionText
                        color: SettingsController.keyDetectionActive
                            ? tokens.accent : tokens.textSecondary
                        font.pixelSize: tokens.fontSizeSmall
                    }
                }
            }

            Rectangle {
                id: voiceSettingsPanel
                objectName: "voiceSettingsPanel"
                Layout.fillWidth: true
                implicitHeight: voiceSettingsColumn.implicitHeight + tokens.spacingMedium * 2
                radius: tokens.cornerRadiusSmall
                color: tokens.surface
                border.color: tokens.border
                border.width: 1

                ColumnLayout {
                    id: voiceSettingsColumn
                    anchors.fill: parent
                    anchors.margins: tokens.spacingMedium
                    spacing: tokens.spacingSmall

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: tokens.spacingSmall

                        Label {
                            text: qsTr("语音快捷键")
                            font.pixelSize: tokens.fontSizeTitle
                            font.bold: true
                            color: tokens.textPrimary
                        }
                        Item { Layout.fillWidth: true }
                    }

                    GridLayout {
                        Layout.fillWidth: true
                        columns: 3
                        columnSpacing: tokens.spacingSmall
                        rowSpacing: tokens.spacingTiny

                        Label {
                            text: qsTr("开关型语音快捷键")
                            color: tokens.textPrimary
                            font.pixelSize: tokens.fontSizeSmall
                        }
                        TextField {
                            id: toggleVoiceHotkeyField
                            objectName: "toggleVoiceHotkeyField"
                            Layout.fillWidth: true
                            text: SettingsController.toggleVoiceHotkeyText
                            placeholderText: qsTr("例如 lalt+space")
                            selectByMouse: true
                            onEditingFinished: SettingsController.toggleVoiceHotkeyText = text
                            Accessible.name: qsTr("开关型语音快捷键")
                        }
                        Button {
                            text: qsTr("录")
                            Layout.preferredWidth: 34
                            Layout.minimumWidth: 30
                            onClicked: root.openShortcutRecorder("", -1, "", "toggle")
                            Accessible.name: qsTr("录制开关型语音快捷键")
                        }

                        Label {
                            text: qsTr("按住型语音快捷键")
                            color: tokens.textPrimary
                            font.pixelSize: tokens.fontSizeSmall
                        }
                        TextField {
                            id: holdVoiceHotkeyField
                            objectName: "holdVoiceHotkeyField"
                            Layout.fillWidth: true
                            text: SettingsController.holdVoiceHotkeyText
                            placeholderText: qsTr("例如 ctrl+l")
                            selectByMouse: true
                            onEditingFinished: SettingsController.holdVoiceHotkeyText = text
                            Accessible.name: qsTr("按住型语音快捷键")
                        }
                        Button {
                            text: qsTr("录")
                            Layout.preferredWidth: 34
                            Layout.minimumWidth: 30
                            onClicked: root.openShortcutRecorder("", -1, "", "hold")
                            Accessible.name: qsTr("录制按住型语音快捷键")
                        }
                    }

                    Connections {
                        target: SettingsController
                        function onToggleVoiceHotkeyTextChanged() {
                            toggleVoiceHotkeyField.text = SettingsController.toggleVoiceHotkeyText
                        }
                        function onHoldVoiceHotkeyTextChanged() {
                            holdVoiceHotkeyField.text = SettingsController.holdVoiceHotkeyText
                        }
                    }
                }
            }

            GridView {
                id: mappingList
                objectName: "mappingList"  // stable hook for positioning a mapping card
                Layout.fillWidth: true
                Layout.fillHeight: true
                clip: true
                model: ButtonMappingModel
                cellWidth: Math.max(220, Math.floor(width / 2))
                cellHeight: 118
                currentIndex: ButtonMappingModel.indexOfButton(SettingsController.selectedButtonId)
                highlightFollowsCurrentItem: true
                onCurrentIndexChanged: positionViewAtIndex(currentIndex, GridView.Contain)

                delegate: Rectangle {
                    id: mappingRow

                    required property int index
                    required property string buttonId
                    required property string displayName
                    required property string hidUsage
                    required property string actionText
                    required property string doubleClickText
                    required property string longPressText
                    required property bool isMic
                    required property bool isSelected

                    readonly property string normalizedActionText: actionText.trim()
                    readonly property bool primaryIsVoice:
                        normalizedActionText === "开关型语音"
                        || normalizedActionText === "按住型语音"
                        || normalizedActionText === "语音（使用专用组合键）"

                    width: mappingList.cellWidth - tokens.spacingTiny
                    height: mappingList.cellHeight - tokens.spacingTiny
                    radius: tokens.cornerRadiusSmall
                    color: isSelected
                        ? Qt.rgba(tokens.accent.r, tokens.accent.g, tokens.accent.b, 0.12)
                        : "transparent"
                    border.width: isSelected ? 1 : 0
                    border.color: tokens.accent

                    // Editable QQC2 ComboBox's own internal currentIndex/
                    // editText sync (triggered during its construction, and
                    // again on every user selection) overwrites a plain
                    // declarative `editText: mappingRow.actionText` binding
                    // the moment the component finishes initializing - a
                    // well-known ComboBox(editable:true) pitfall. Setting it
                    // imperatively once after completion, then re-syncing it
                    // explicitly whenever the underlying row data changes
                    // (e.g. "恢复默认"), keeps the visible text correct
                    // without fighting ComboBox's own internal writes.
                    onActionTextChanged: actionCombo.editText = actionText
                    onDoubleClickTextChanged: doubleActionCombo.editText = doubleClickText
                    onLongPressTextChanged: longActionCombo.editText = longPressText

                    TapHandler {
                        onTapped: SettingsController.selectButton(mappingRow.buttonId)
                    }

                    RowLayout {
                        id: rowContent
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.top: parent.top
                        anchors.bottom: gestureRow.top
                        anchors.leftMargin: tokens.spacingSmall
                        anchors.rightMargin: tokens.spacingSmall
                        anchors.topMargin: tokens.spacingSmall
                        anchors.bottomMargin: tokens.spacingTiny
                        spacing: tokens.spacingTiny

                        ColumnLayout {
                            Layout.preferredWidth: 58
                            Layout.minimumWidth: 52
                            Layout.fillHeight: true
                            spacing: 0
                            Label {
                                Layout.fillWidth: true
                                text: mappingRow.displayName
                                color: tokens.textPrimary
                                font.pixelSize: tokens.fontSizeSmall
                                elide: Text.ElideRight
                            }
                            Label {
                                Layout.fillWidth: true
                                text: mappingRow.hidUsage
                                color: tokens.textSecondary
                                font.pixelSize: tokens.fontSizeSmall
                                elide: Text.ElideRight
                            }
                        }

                        ComboBox {
                            id: actionCombo
                            // Per-row name (not a fixed literal) so a test
                            // can locate one specific row's ComboBox, e.g.
                            // findChild(QObject, "actionCombo_power") - a
                            // real, stable hook, not a fake production
                            // behavior.
                            objectName: "actionCombo_" + mappingRow.buttonId
                            Layout.fillWidth: true
                            Layout.minimumWidth: 0
                            editable: true
                            model: SettingsController.primaryActionOptions
                            Accessible.name: mappingRow.displayName
                            ToolTip.visible: hovered
                            ToolTip.text: qsTr("可选择开关型语音、按住型语音或任意单键/组合键；输入“禁用”可关闭此键。")

                            // Guards onEditTextChanged below against the
                            // SAME construction-time noise
                            // Component.onCompleted works around (ComboBox's
                            // own internal currentIndex-driven editText sync
                            // fires during construction, before this flag is
                            // set true) - without it, every row would
                            // immediately persist its transient default
                            // (the first primary action option)
                            // into the model the instant it's created,
                            // reintroducing the "all rows show/save escape"
                            // bug this task's own screenshot step caught
                            // earlier.
                            property bool _initialized: false

                            // NOT `editText: mappingRow.actionText` (a
                            // plain declarative binding) - ComboBox's own
                            // internal currentIndex/editText sync overwrites
                            // that the moment construction finishes (a
                            // well-known ComboBox(editable:true) pitfall).
                            // Set imperatively here instead, strictly AFTER
                            // that internal sync has already run.
                            Component.onCompleted: {
                                editText = mappingRow.actionText
                                _initialized = true
                            }

                            // XRBM-030 RETRY 1 blocker 1: onAccepted (Enter)
                            // and onActivated (picking a dropdown item)
                            // alone are not enough - a user who types a
                            // custom chord (e.g. "ctrl+shift+p") and clicks
                            // a SAVE button without ever pressing Enter
                            // previously left the model (and therefore
                            // _save()'s persisted binding) holding the OLD
                            // value, silently discarding the typed edit.
                            // Committing on every live editText change closes
                            // that gap unconditionally - by the time any
                            // save action runs, the model already holds
                            // whatever is visibly displayed, with no
                            // separate "commit on blur/save" event to miss.
                            // Guarded by _initialized (see above) so this
                            // never fires from ComboBox's own construction-
                            // time internal writes, only from a real,
                            // post-construction edit (by typing or by
                            // restoreDefaults()/onActionTextChanged
                            // resetting editText, which harmlessly re-writes
                            // the model with the exact same value it already
                            // has).
                            onEditTextChanged: {
                                if (_initialized) {
                                    ButtonMappingModel.setActionTextAt(mappingRow.index, editText)
                                }
                            }

                            // Kept for defense-in-depth / clarity of intent
                            // even though onEditTextChanged above already
                            // covers both cases (accepting Enter, or picking
                            // a dropdown item, both change editText too).
                            onAccepted: ButtonMappingModel.setActionTextAt(mappingRow.index, editText)
                            onActivated: ButtonMappingModel.setActionTextAt(mappingRow.index, currentText)
                        }

                        Button {
                            objectName: "recordShortcut_" + mappingRow.buttonId
                            text: qsTr("录")
                            Layout.preferredWidth: 34
                            Layout.minimumWidth: 30
                            onClicked: root.openShortcutRecorder(
                                mappingRow.buttonId, mappingRow.index,
                                "single_click", ""
                            )
                            Accessible.name: qsTr("录制") + mappingRow.displayName + qsTr("快捷键")
                        }
                    }

                    RowLayout {
                        id: gestureRow
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.bottom: parent.bottom
                        anchors.leftMargin: tokens.spacingSmall
                        anchors.rightMargin: tokens.spacingSmall
                        anchors.bottomMargin: tokens.spacingSmall
                        height: 38
                        spacing: tokens.spacingTiny

                        Label {
                            visible: mappingRow.primaryIsVoice
                            Layout.fillWidth: true
                            horizontalAlignment: Text.AlignHCenter
                            text: qsTr("双击/长按暂停，原设置保留")
                            color: tokens.textSecondary
                            font.pixelSize: tokens.fontSizeSmall
                            Accessible.name: text
                        }

                        Label {
                            visible: !mappingRow.primaryIsVoice
                            text: qsTr("双")
                            color: tokens.textSecondary
                            font.pixelSize: tokens.fontSizeSmall
                        }
                        ComboBox {
                            id: doubleActionCombo
                            objectName: "doubleActionCombo_" + mappingRow.buttonId
                            Layout.fillWidth: true
                            Layout.minimumWidth: 0
                            visible: !mappingRow.primaryIsVoice
                            enabled: !mappingRow.primaryIsVoice
                            editable: true
                            model: SettingsController.secondaryActionOptions
                            ToolTip.visible: hovered
                            ToolTip.text: qsTr("双击动作；配置后等待约 0.3 秒区分单击和双击。语音动作仅可用于主映射。")
                            property bool _initialized: false
                            Component.onCompleted: {
                                editText = mappingRow.doubleClickText
                                _initialized = true
                            }
                            onEditTextChanged: {
                                if (_initialized)
                                    ButtonMappingModel.setSecondaryActionTextAt(
                                        mappingRow.index, "double_click", editText
                                    )
                            }
                            onAccepted: ButtonMappingModel.setSecondaryActionTextAt(
                                mappingRow.index, "double_click", editText
                            )
                            onActivated: ButtonMappingModel.setSecondaryActionTextAt(
                                mappingRow.index, "double_click", currentText
                            )
                        }
                        Button {
                            objectName: "recordDoubleShortcut_" + mappingRow.buttonId
                            visible: !mappingRow.primaryIsVoice
                            enabled: !mappingRow.primaryIsVoice
                            text: qsTr("录")
                            Layout.preferredWidth: 30
                            Layout.minimumWidth: 28
                            onClicked: root.openShortcutRecorder(
                                mappingRow.buttonId, mappingRow.index,
                                "double_click", ""
                            )
                            Accessible.name: qsTr("录制双击") + mappingRow.displayName
                        }
                        Label {
                            visible: !mappingRow.primaryIsVoice
                            text: qsTr("长")
                            color: tokens.textSecondary
                            font.pixelSize: tokens.fontSizeSmall
                        }
                        ComboBox {
                            id: longActionCombo
                            objectName: "longActionCombo_" + mappingRow.buttonId
                            Layout.fillWidth: true
                            Layout.minimumWidth: 0
                            visible: !mappingRow.primaryIsVoice
                            enabled: !mappingRow.primaryIsVoice
                            editable: true
                            model: SettingsController.secondaryActionOptions
                            ToolTip.visible: hovered
                            ToolTip.text: qsTr("长按动作；按住约 0.55 秒触发并抑制单击。语音动作仅可用于主映射。")
                            property bool _initialized: false
                            Component.onCompleted: {
                                editText = mappingRow.longPressText
                                _initialized = true
                            }
                            onEditTextChanged: {
                                if (_initialized)
                                    ButtonMappingModel.setSecondaryActionTextAt(
                                        mappingRow.index, "long_press", editText
                                    )
                            }
                            onAccepted: ButtonMappingModel.setSecondaryActionTextAt(
                                mappingRow.index, "long_press", editText
                            )
                            onActivated: ButtonMappingModel.setSecondaryActionTextAt(
                                mappingRow.index, "long_press", currentText
                            )
                        }
                        Button {
                            objectName: "recordLongShortcut_" + mappingRow.buttonId
                            visible: !mappingRow.primaryIsVoice
                            enabled: !mappingRow.primaryIsVoice
                            text: qsTr("录")
                            Layout.preferredWidth: 30
                            Layout.minimumWidth: 28
                            onClicked: root.openShortcutRecorder(
                                mappingRow.buttonId, mappingRow.index,
                                "long_press", ""
                            )
                            Accessible.name: qsTr("录制长按") + mappingRow.displayName
                        }
                    }
                }
            }
        }
    }

    RowLayout {
        id: djiControlLayout
        objectName: "djiControlLayout"
        visible: SettingsController.isDjiMic2Device
        anchors.fill: parent
        anchors.margins: tokens.spacingLarge
        spacing: tokens.spacingLarge

        ColumnLayout {
            Layout.preferredWidth: 250
            Layout.fillHeight: true
            spacing: tokens.spacingSmall

            Rectangle {
                Layout.preferredWidth: 210
                Layout.preferredHeight: 360
                Layout.alignment: Qt.AlignHCenter | Qt.AlignTop
                radius: tokens.cornerRadiusLarge
                color: tokens.surface
                border.color: tokens.border
                border.width: 1

                Rectangle {
                    id: transmitter
                    width: 112
                    height: 250
                    anchors.horizontalCenter: parent.horizontalCenter
                    anchors.top: parent.top
                    anchors.topMargin: 32
                    radius: tokens.cornerRadiusLarge
                    color: tokens.fieldBackground
                    border.color: tokens.border
                    border.width: 1

                    Label {
                        anchors.horizontalCenter: parent.horizontalCenter
                        anchors.top: parent.top
                        anchors.topMargin: 22
                        text: qsTr("DJI MIC 2")
                        font.bold: true
                        color: tokens.textPrimary
                    }

                    Rectangle {
                        width: 36
                        height: 36
                        radius: 18
                        anchors.horizontalCenter: parent.horizontalCenter
                        anchors.top: parent.top
                        anchors.topMargin: 72
                        color: tokens.surface
                        border.color: tokens.voiceAccent
                        border.width: 2
                        Label {
                            anchors.centerIn: parent
                            text: qsTr("录")
                            color: tokens.textPrimary
                            font.bold: true
                        }
                    }

                    Rectangle {
                        width: 64
                        height: 32
                        radius: tokens.cornerRadiusSmall
                        anchors.horizontalCenter: parent.horizontalCenter
                        anchors.top: parent.top
                        anchors.topMargin: 132
                        color: tokens.surface
                        border.color: tokens.border
                        Label {
                            anchors.centerIn: parent
                            text: qsTr("连接")
                            color: tokens.textPrimary
                        }
                    }

                    Rectangle {
                        width: 64
                        height: 32
                        radius: tokens.cornerRadiusSmall
                        anchors.horizontalCenter: parent.horizontalCenter
                        anchors.top: parent.top
                        anchors.topMargin: 184
                        color: tokens.surface
                        border.color: tokens.border
                        Label {
                            anchors.centerIn: parent
                            text: qsTr("电源")
                            color: tokens.textPrimary
                        }
                    }
                }

                Label {
                    anchors.horizontalCenter: parent.horizontalCenter
                    anchors.bottom: parent.bottom
                    anchors.bottomMargin: 22
                    text: qsTr("功能示意，非产品照片")
                    color: tokens.textSecondary
                    font.pixelSize: tokens.fontSizeSmall
                }
            }

            Label {
                Layout.preferredWidth: 230
                Layout.alignment: Qt.AlignHCenter
                horizontalAlignment: Text.AlignHCenter
                wrapMode: Text.WordWrap
                text: qsTr("DJI Mic 2 在 Windows 中首先是录音输入设备，不继承 RC003 的 13 键映射。")
                color: tokens.textSecondary
                font.pixelSize: tokens.fontSizeSmall
            }
        }

        ColumnLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: tokens.spacingSmall

            Label {
                text: qsTr("DJI Mic 2 设备控制")
                font.pixelSize: tokens.fontSizeTitle
                font.bold: true
                color: tokens.textPrimary
            }
            Label {
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                text: qsTr("当前可自定义映射：0。只有在真实 Windows 捕获到某个实体键的独立输入事件后，才会开放该键的映射选项。")
                color: tokens.textSecondary
                font.pixelSize: tokens.fontSizeSmall
            }

            Repeater {
                model: SettingsController.djiControlRows
                delegate: Rectangle {
                    required property var modelData
                    Layout.fillWidth: true
                    implicitHeight: controlRow.implicitHeight + tokens.spacingMedium * 2
                    radius: tokens.cornerRadiusSmall
                    color: tokens.surface
                    border.color: tokens.border
                    border.width: 1

                    RowLayout {
                        id: controlRow
                        anchors.fill: parent
                        anchors.margins: tokens.spacingMedium
                        spacing: tokens.spacingMedium

                        Rectangle {
                            Layout.preferredWidth: 72
                            Layout.preferredHeight: 32
                            radius: tokens.cornerRadiusSmall
                            color: tokens.fieldBackground
                            Label {
                                anchors.centerIn: parent
                                text: modelData.name
                                color: tokens.textPrimary
                                font.bold: true
                            }
                        }
                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 2
                            Label {
                                Layout.fillWidth: true
                                wrapMode: Text.WordWrap
                                text: modelData.behavior
                                color: tokens.textPrimary
                                font.pixelSize: tokens.fontSizeBody
                            }
                            Label {
                                Layout.fillWidth: true
                                wrapMode: Text.WordWrap
                                text: modelData.mapping
                                color: tokens.textSecondary
                                font.pixelSize: tokens.fontSizeSmall
                            }
                        }
                        Label {
                            text: qsTr("硬件内置")
                            color: tokens.disabledText
                            font.pixelSize: tokens.fontSizeSmall
                        }
                    }
                }
            }

            Item { Layout.fillHeight: true }

            RowLayout {
                Layout.alignment: Qt.AlignRight
                Button {
                    text: qsTr("重新检测麦克风")
                    onClicked: SettingsController.refreshDjiMicStatus()
                }
                Button {
                    text: qsTr("打开 Windows 声音输入设置")
                    highlighted: true
                    onClicked: SettingsController.openSoundSettings()
                }
            }
        }
    }
}
