// "按键" tab: an RC003 product-photo reference with a selected-button marker
// sits beside the existing one-row-per-button mapping matrix. Mapping selection
// and editing remain owned by the matrix and real-key detection.
// SettingsController/ButtonMappingModel are QML singletons - see main.qml.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import OvbRc003Settings 1.0

Item {
    id: root
    property var tokens
    readonly property real photoAspectRatio: 1030 / 508
    readonly property real photoSidebarWidth: Math.round(
        Math.min(200, Math.max(136, root.width * 0.20))
    )
    readonly property real mappingKeyColumnWidth: 118
    readonly property real mappingEditColumnWidth: 56
    readonly property real mappingActionColumnWidth: Math.max(
        0,
        (mappingList.width
         - tokens.spacingMedium * 2
         - tokens.spacingSmall * 4
         - mappingKeyColumnWidth
         - mappingEditColumnWidth) / 3
    )

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
            if (voiceMode === "hold")
                SettingsController.holdVoiceHotkeyText = chord
            else if (trigger === "single_click") {
                ButtonMappingModel.setActionTextAt(rowIndex, chord)
                actionEditor.applyCapturedShortcut(rowIndex, trigger, chord)
            } else {
                ButtonMappingModel.setSecondaryActionTextAt(rowIndex, trigger, chord)
                actionEditor.applyCapturedShortcut(rowIndex, trigger, chord)
            }
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

    Dialog {
        id: actionEditor
        objectName: "actionEditorDialog"
        modal: true
        anchors.centerIn: parent
        width: Math.min(620, root.width - tokens.spacingLarge * 2)
        title: buttonName.length > 0
            ? qsTr("编辑按键：") + buttonName
            : qsTr("编辑按键")

        property int rowIndex: -1
        property string buttonId: ""
        property string buttonName: ""
        property string primaryText: ""
        property string doubleText: "未设置"
        property string longText: "未设置"
        property bool syncing: false
        readonly property string normalizedPrimaryText: primaryText.trim()
        readonly property bool primaryIsVoice:
            normalizedPrimaryText === "按住说话"
            || normalizedPrimaryText.indexOf("已停用：旧语音配置") === 0

        function openForRow(rowIndexValue, buttonIdValue, buttonNameValue,
                            primaryValue, doubleValue, longValue) {
            syncing = true
            rowIndex = rowIndexValue
            buttonId = buttonIdValue
            buttonName = buttonNameValue
            primaryText = primaryValue
            doubleText = doubleValue
            longText = longValue
            primaryCombo.editText = primaryValue
            doubleCombo.editText = doubleValue
            longCombo.editText = longValue
            syncing = false
            open()
            primaryCombo.forceActiveFocus()
        }

        function applyCapturedShortcut(targetRow, trigger, chord) {
            if (!visible || targetRow !== rowIndex)
                return
            syncing = true
            if (trigger === "single_click") {
                primaryText = chord
                primaryCombo.editText = chord
            } else if (trigger === "double_click") {
                doubleText = chord
                doubleCombo.editText = chord
            } else if (trigger === "long_press") {
                longText = chord
                longCombo.editText = chord
            }
            syncing = false
        }

        onClosed: syncing = true

        contentItem: ColumnLayout {
            spacing: tokens.spacingMedium

            Label {
                Layout.fillWidth: true
                text: qsTr("为这个遥控器按键分别设置单击、双击和长按动作。")
                color: tokens.textSecondary
                font.pixelSize: tokens.fontSizeSmall
                wrapMode: Text.WordWrap
            }

            GridLayout {
                Layout.fillWidth: true
                columns: 3
                columnSpacing: tokens.spacingSmall
                rowSpacing: tokens.spacingSmall

                Label {
                    text: qsTr("单击")
                    color: tokens.textPrimary
                    font.bold: true
                }
                ComboBox {
                    id: primaryCombo
                    objectName: "actionEditorPrimaryCombo"
                    Layout.fillWidth: true
                    editable: true
                    model: SettingsController.primaryActionOptionsFor(
                        actionEditor.buttonId
                    )
                    Accessible.name: actionEditor.buttonName + qsTr("单击动作")
                    ToolTip.visible: hovered
                    ToolTip.text: actionEditor.buttonId === "mic"
                        ? qsTr("话筒键可选择按住说话、普通动作或自定义组合键。")
                        : qsTr("可选择普通动作或输入自定义组合键。")
                    onEditTextChanged: {
                        if (!actionEditor.syncing && actionEditor.rowIndex >= 0) {
                            actionEditor.primaryText = editText
                            ButtonMappingModel.setActionTextAt(
                                actionEditor.rowIndex, editText
                            )
                        }
                    }
                    onAccepted: ButtonMappingModel.setActionTextAt(
                        actionEditor.rowIndex, editText
                    )
                    onActivated: {
                        const selectedText = currentText
                        actionEditor.syncing = true
                        editText = selectedText
                        actionEditor.primaryText = selectedText
                        actionEditor.syncing = false
                        ButtonMappingModel.setActionTextAt(
                            actionEditor.rowIndex, selectedText
                        )
                    }
                }
                Button {
                    objectName: "actionEditorPrimaryRecordButton"
                    text: qsTr("录入")
                    onClicked: root.openShortcutRecorder(
                        actionEditor.buttonId, actionEditor.rowIndex,
                        "single_click", ""
                    )
                    Accessible.name: qsTr("录制单击快捷键")
                }

                Label {
                    visible: !actionEditor.primaryIsVoice
                    text: qsTr("双击")
                    color: tokens.textPrimary
                    font.bold: true
                }
                ComboBox {
                    id: doubleCombo
                    objectName: "actionEditorDoubleCombo"
                    Layout.fillWidth: true
                    visible: !actionEditor.primaryIsVoice
                    enabled: !actionEditor.primaryIsVoice
                    editable: true
                    model: SettingsController.secondaryActionOptions
                    Accessible.name: actionEditor.buttonName + qsTr("双击动作")
                    ToolTip.visible: hovered
                    ToolTip.text: qsTr("配置后，程序会等待约 0.3 秒区分单击和双击。")
                    onEditTextChanged: {
                        if (!actionEditor.syncing && actionEditor.rowIndex >= 0) {
                            actionEditor.doubleText = editText
                            ButtonMappingModel.setSecondaryActionTextAt(
                                actionEditor.rowIndex, "double_click", editText
                            )
                        }
                    }
                    onAccepted: ButtonMappingModel.setSecondaryActionTextAt(
                        actionEditor.rowIndex, "double_click", editText
                    )
                    onActivated: {
                        const selectedText = currentText
                        actionEditor.syncing = true
                        editText = selectedText
                        actionEditor.doubleText = selectedText
                        actionEditor.syncing = false
                        ButtonMappingModel.setSecondaryActionTextAt(
                            actionEditor.rowIndex, "double_click", selectedText
                        )
                    }
                }
                Button {
                    objectName: "actionEditorDoubleRecordButton"
                    visible: !actionEditor.primaryIsVoice
                    enabled: !actionEditor.primaryIsVoice
                    text: qsTr("录入")
                    onClicked: root.openShortcutRecorder(
                        actionEditor.buttonId, actionEditor.rowIndex,
                        "double_click", ""
                    )
                    Accessible.name: qsTr("录制双击快捷键")
                }

                Label {
                    visible: !actionEditor.primaryIsVoice
                    text: qsTr("长按")
                    color: tokens.textPrimary
                    font.bold: true
                }
                ComboBox {
                    id: longCombo
                    objectName: "actionEditorLongCombo"
                    Layout.fillWidth: true
                    visible: !actionEditor.primaryIsVoice
                    enabled: !actionEditor.primaryIsVoice
                    editable: true
                    model: SettingsController.secondaryActionOptions
                    Accessible.name: actionEditor.buttonName + qsTr("长按动作")
                    ToolTip.visible: hovered
                    ToolTip.text: qsTr("按住约 0.55 秒触发，并抑制本次单击动作。")
                    onEditTextChanged: {
                        if (!actionEditor.syncing && actionEditor.rowIndex >= 0) {
                            actionEditor.longText = editText
                            ButtonMappingModel.setSecondaryActionTextAt(
                                actionEditor.rowIndex, "long_press", editText
                            )
                        }
                    }
                    onAccepted: ButtonMappingModel.setSecondaryActionTextAt(
                        actionEditor.rowIndex, "long_press", editText
                    )
                    onActivated: {
                        const selectedText = currentText
                        actionEditor.syncing = true
                        editText = selectedText
                        actionEditor.longText = selectedText
                        actionEditor.syncing = false
                        ButtonMappingModel.setSecondaryActionTextAt(
                            actionEditor.rowIndex, "long_press", selectedText
                        )
                    }
                }
                Button {
                    objectName: "actionEditorLongRecordButton"
                    visible: !actionEditor.primaryIsVoice
                    enabled: !actionEditor.primaryIsVoice
                    text: qsTr("录入")
                    onClicked: root.openShortcutRecorder(
                        actionEditor.buttonId, actionEditor.rowIndex,
                        "long_press", ""
                    )
                    Accessible.name: qsTr("录制长按快捷键")
                }
            }

            Label {
                Layout.fillWidth: true
                visible: actionEditor.primaryIsVoice
                text: qsTr("语音动作占用完整按下/松开周期，双击和长按设置会保留，但本次不执行。")
                color: tokens.textSecondary
                font.pixelSize: tokens.fontSizeSmall
                wrapMode: Text.WordWrap
            }

            RowLayout {
                Layout.fillWidth: true
                Item { Layout.fillWidth: true }
                Button {
                    objectName: "actionEditorDoneButton"
                    text: qsTr("完成")
                    highlighted: true
                    onClicked: actionEditor.close()
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
        spacing: tokens.spacingSmall

        // -- Fixed product-photo sidebar -----------------------------------
        ColumnLayout {
            id: photoSidebar
            objectName: "photoSidebar"
            Layout.preferredWidth: root.photoSidebarWidth
            Layout.minimumWidth: root.photoSidebarWidth
            Layout.maximumWidth: root.photoSidebarWidth
            Layout.fillHeight: true
            spacing: tokens.spacingSmall

            Rectangle {
                id: photoFrame
                objectName: "photoFrame"
                Layout.fillWidth: true
                Layout.preferredHeight: root.photoSidebarWidth * root.photoAspectRatio
                Layout.alignment: Qt.AlignHCenter | Qt.AlignTop
                radius: tokens.cornerRadiusLarge
                color: tokens.surface
                border.color: tokens.border
                border.width: 1
                clip: true

                Image {
                    id: photoImage
                    objectName: "photoImage"
                    anchors.fill: parent
                    anchors.margins: tokens.spacingSmall
                    fillMode: Image.PreserveAspectFit
                    source: SettingsController.photoAvailable
                        ? SettingsController.photoSource : ""
                    visible: SettingsController.photoAvailable
                    smooth: true
                    asynchronous: true
                }

                Label {
                    anchors.centerIn: parent
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
                        id: photoHotspot
                        objectName: "photoHotspot_" + buttonId

                        required property string buttonId
                        required property real hotspotX
                        required property real hotspotY
                        required property real hotspotWidth
                        required property real hotspotHeight
                        required property bool isSelected
                        required property bool isVoice

                        readonly property real paintedWidth: photoImage.paintedWidth
                        readonly property real paintedHeight: photoImage.paintedHeight
                        readonly property real paintedOffsetX:
                            photoImage.x + (photoImage.width - paintedWidth) / 2
                        readonly property real paintedOffsetY:
                            photoImage.y + (photoImage.height - paintedHeight) / 2

                        width: hotspotWidth * paintedWidth
                        height: hotspotHeight * paintedHeight
                        x: paintedOffsetX + hotspotX * paintedWidth - width / 2
                        y: paintedOffsetY + hotspotY * paintedHeight - height / 2
                        visible: SettingsController.photoAvailable
                        z: 1

                        Rectangle {
                            objectName: "photoHotspotMarker_" + photoHotspot.buttonId
                            anchors.fill: parent
                            visible: photoHotspot.isSelected
                            radius: Math.min(width, height) / 2
                            color: photoHotspot.isVoice
                                ? Qt.rgba(
                                    tokens.voiceAccent.r,
                                    tokens.voiceAccent.g,
                                    tokens.voiceAccent.b,
                                    0.30
                                )
                                : Qt.rgba(
                                    tokens.accent.r,
                                    tokens.accent.g,
                                    tokens.accent.b,
                                    0.30
                                )
                            border.width: 2
                            border.color: photoHotspot.isVoice
                                ? tokens.voiceAccent : tokens.accent
                        }
                    }
                }

            }

            Label {
                id: photoCaption
                Layout.fillWidth: true
                horizontalAlignment: Text.AlignHCenter
                wrapMode: Text.WordWrap
                text: qsTr("遥控器按键位置参考")
                color: tokens.textSecondary
                font.pixelSize: tokens.fontSizeSmall
            }

            Item { Layout.fillHeight: true }
        }

        // -- One-row-per-button mapping matrix -----------------------------
        ColumnLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: tokens.spacingSmall

            RowLayout {
                Layout.fillWidth: true
                UiLabel {
                    tokens: root.tokens
                    kind: pageTitleKind
                    Layout.fillWidth: true
                    text: qsTr("按键映射")
                }
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
                id: bridgeRequiredWarning
                objectName: "mappingBridgeWarning"
                Layout.fillWidth: true
                visible: !SettingsController.bridgeRunning
                implicitHeight: bridgeRequiredWarningText.implicitHeight + tokens.spacingMedium * 2
                radius: tokens.cornerRadiusSmall
                color: tokens.errorBackground
                border.color: tokens.errorColor
                border.width: 1

                Label {
                    id: bridgeRequiredWarningText
                    anchors.fill: parent
                    anchors.margins: tokens.spacingMedium
                    wrapMode: Text.WordWrap
                    text: qsTr("后台桥接未运行或尚未确认运行。话筒键不会由本程序触发语音；真实按键检测的补充通道首次连接可能需要约一分钟。请先到“连接”页点击“保存并启动桥接”。")
                    color: tokens.errorColor
                    font.pixelSize: tokens.fontSizeSmall
                    font.bold: true
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

            SectionFrame {
                id: voiceSettingsPanel
                objectName: "voiceSettingsPanel"
                tokens: root.tokens
                Layout.fillWidth: true
                horizontalPadding: tokens.spacingMedium
                verticalPadding: tokens.spacingMedium

                UiLabel {
                    tokens: root.tokens
                    kind: sectionTitleKind
                    text: qsTr("按住说话")
                }

                GridLayout {
                    Layout.fillWidth: true
                    columns: 3
                    columnSpacing: tokens.spacingSmall
                    rowSpacing: tokens.spacingTiny

                    UiLabel {
                        tokens: root.tokens
                        kind: bodyKind
                        text: qsTr("快捷键")
                    }
                    TextField {
                        id: holdVoiceHotkeyField
                        objectName: "holdVoiceHotkeyField"
                        Layout.fillWidth: true
                        text: SettingsController.holdVoiceHotkeyText
                        placeholderText: qsTr("例如 ralt")
                        selectByMouse: true
                        onEditingFinished: SettingsController.holdVoiceHotkeyText = text
                        Accessible.name: qsTr("按住说话快捷键")
                        Accessible.description: qsTr(
                            "默认右侧 Alt；请与目标语音软件的快捷键保持一致。"
                        )
                        ToolTip.visible: hovered
                        ToolTip.text: qsTr(
                            "默认右侧 Alt；若被系统或其他软件占用，请重新录制。"
                        )
                    }
                    Button {
                        text: qsTr("录")
                        Layout.preferredWidth: 34
                        Layout.minimumWidth: 30
                        onClicked: root.openShortcutRecorder("", -1, "", "hold")
                        Accessible.name: qsTr("录制按住说话快捷键")
                    }
                }

                Connections {
                    target: SettingsController
                    function onHoldVoiceHotkeyTextChanged() {
                        holdVoiceHotkeyField.text = SettingsController.holdVoiceHotkeyText
                    }
                }
            }

            Rectangle {
                id: mappingHeader
                objectName: "mappingMatrixHeader"
                Layout.fillWidth: true
                Layout.preferredHeight: 46
                color: tokens.surface
                border.color: tokens.border
                border.width: 1
                radius: tokens.cornerRadiusSmall

                GridLayout {
                    anchors.fill: parent
                    anchors.leftMargin: tokens.spacingMedium
                    anchors.rightMargin: tokens.spacingMedium
                    columns: 5
                    columnSpacing: tokens.spacingSmall

                    Label {
                        objectName: "mappingHeaderKeyColumn"
                        Layout.preferredWidth: root.mappingKeyColumnWidth
                        Layout.minimumWidth: root.mappingKeyColumnWidth
                        Layout.maximumWidth: root.mappingKeyColumnWidth
                        Layout.alignment: Qt.AlignVCenter
                        text: qsTr("遥控器按键")
                        color: tokens.textPrimary
                        font.pixelSize: tokens.fontSizeBody
                        font.bold: true
                        horizontalAlignment: Text.AlignLeft
                    }
                    Label {
                        objectName: "mappingHeaderSingleColumn"
                        Layout.preferredWidth: root.mappingActionColumnWidth
                        Layout.minimumWidth: root.mappingActionColumnWidth
                        Layout.maximumWidth: root.mappingActionColumnWidth
                        Layout.alignment: Qt.AlignVCenter
                        text: qsTr("单击")
                        color: tokens.textPrimary
                        font.pixelSize: tokens.fontSizeBody
                        font.bold: true
                        horizontalAlignment: Text.AlignLeft
                    }
                    Label {
                        objectName: "mappingHeaderDoubleColumn"
                        Layout.preferredWidth: root.mappingActionColumnWidth
                        Layout.minimumWidth: root.mappingActionColumnWidth
                        Layout.maximumWidth: root.mappingActionColumnWidth
                        Layout.alignment: Qt.AlignVCenter
                        text: qsTr("双击")
                        color: tokens.textPrimary
                        font.pixelSize: tokens.fontSizeBody
                        font.bold: true
                        horizontalAlignment: Text.AlignLeft
                    }
                    Label {
                        objectName: "mappingHeaderLongColumn"
                        Layout.preferredWidth: root.mappingActionColumnWidth
                        Layout.minimumWidth: root.mappingActionColumnWidth
                        Layout.maximumWidth: root.mappingActionColumnWidth
                        Layout.alignment: Qt.AlignVCenter
                        text: qsTr("长按")
                        color: tokens.textPrimary
                        font.pixelSize: tokens.fontSizeBody
                        font.bold: true
                        horizontalAlignment: Text.AlignLeft
                    }
                    Item {
                        objectName: "mappingHeaderEditColumn"
                        Layout.preferredWidth: root.mappingEditColumnWidth
                        Layout.minimumWidth: root.mappingEditColumnWidth
                        Layout.maximumWidth: root.mappingEditColumnWidth
                    }
                }
            }

            Rectangle {
                id: mappingListFrame
                objectName: "mappingListFrame"
                Layout.fillWidth: true
                Layout.fillHeight: true
                radius: tokens.cornerRadiusSmall
                color: tokens.surface
                border.color: tokens.border
                border.width: 1
                clip: true

                ListView {
                    id: mappingList
                    objectName: "mappingList"
                    anchors.fill: parent
                    clip: true
                    model: ButtonMappingModel
                    spacing: 0
                    boundsBehavior: Flickable.StopAtBounds
                    ScrollBar.vertical: ScrollBar {}
                    currentIndex: ButtonMappingModel.indexOfButton(SettingsController.selectedButtonId)
                    highlightFollowsCurrentItem: true
                    onCurrentIndexChanged: positionViewAtIndex(currentIndex, ListView.Contain)

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
                        normalizedActionText === "按住说话"
                        || normalizedActionText.indexOf("已停用：旧语音配置") === 0

                    width: mappingList.width
                    height: 64
                    radius: 0
                    color: isSelected
                        ? Qt.rgba(tokens.accent.r, tokens.accent.g, tokens.accent.b, 0.10)
                        : (rowHover.hovered || index % 2 === 1
                            ? tokens.surfaceMuted : tokens.surface)
                    border.width: 0

                    TapHandler {
                        onTapped: SettingsController.selectButton(mappingRow.buttonId)
                    }

                    HoverHandler { id: rowHover }

                    Rectangle {
                        anchors.left: parent.left
                        anchors.verticalCenter: parent.verticalCenter
                        width: 3
                        height: parent.height - tokens.spacingMedium
                        radius: 1
                        visible: mappingRow.isSelected
                        color: mappingRow.primaryIsVoice
                            ? tokens.voiceAccent : tokens.accent
                    }

                    GridLayout {
                        id: matrixRowContent
                        anchors.fill: parent
                        anchors.leftMargin: tokens.spacingMedium
                        anchors.rightMargin: tokens.spacingMedium
                        columns: 5
                        columnSpacing: tokens.spacingSmall

                        ColumnLayout {
                            objectName: "mappingKeyCell_" + mappingRow.buttonId
                            Layout.preferredWidth: root.mappingKeyColumnWidth
                            Layout.minimumWidth: root.mappingKeyColumnWidth
                            Layout.maximumWidth: root.mappingKeyColumnWidth
                            Layout.fillWidth: false
                            Layout.alignment: Qt.AlignVCenter
                            spacing: 1
                            Item {
                                Layout.fillWidth: true
                                Layout.preferredHeight: 20
                                Label {
                                    objectName: "mappingKeyPrimaryText_"
                                        + mappingRow.buttonId
                                    anchors.left: parent.left
                                    anchors.top: parent.top
                                    anchors.bottom: parent.bottom
                                    anchors.right: parent.right
                                    anchors.rightMargin: micBadge.visible
                                        ? micBadge.width + tokens.spacingTiny : 0
                                    text: mappingRow.displayName
                                    color: tokens.textPrimary
                                    font.pixelSize: tokens.fontSizeBody
                                    font.bold: mappingRow.isSelected
                                    elide: Text.ElideRight
                                }
                                Rectangle {
                                    id: micBadge
                                    visible: mappingRow.isMic
                                    anchors.right: parent.right
                                    anchors.verticalCenter: parent.verticalCenter
                                    width: micBadgeText.implicitWidth + 8
                                    height: micBadgeText.implicitHeight + 4
                                    radius: 3
                                    color: Qt.rgba(
                                        tokens.voiceAccent.r,
                                        tokens.voiceAccent.g,
                                        tokens.voiceAccent.b,
                                        0.14
                                    )
                                    Label {
                                        id: micBadgeText
                                        anchors.centerIn: parent
                                        text: qsTr("语音键")
                                        color: tokens.voiceAccent
                                        font.pixelSize: tokens.fontSizeSmall
                                    }
                                }
                            }
                            Label {
                                Layout.fillWidth: true
                                Layout.preferredHeight: 16
                                text: mappingRow.isMic
                                    ? qsTr("ATVV 通道（非 HID）")
                                    : qsTr("HID 编号 ") + mappingRow.hidUsage
                                color: tokens.textSecondary
                                font.pixelSize: tokens.fontSizeSmall
                                elide: Text.ElideRight
                            }
                        }

                        ColumnLayout {
                            objectName: "mappingSingleCell_" + mappingRow.buttonId
                            Layout.preferredWidth: root.mappingActionColumnWidth
                            Layout.minimumWidth: root.mappingActionColumnWidth
                            Layout.maximumWidth: root.mappingActionColumnWidth
                            Layout.alignment: Qt.AlignVCenter
                            spacing: 1
                            Label {
                                objectName: "mappingSinglePrimaryText_"
                                    + mappingRow.buttonId
                                Layout.fillWidth: true
                                Layout.preferredHeight: 20
                                text: mappingRow.actionText.length > 0
                                    ? mappingRow.actionText : qsTr("未设置")
                                color: mappingRow.actionText.length > 0
                                    ? tokens.textPrimary : tokens.disabledText
                                font.pixelSize: tokens.fontSizeBody
                                elide: Text.ElideRight
                                horizontalAlignment: Text.AlignLeft
                            }
                            Label {
                                Layout.fillWidth: true
                                Layout.preferredHeight: 16
                                text: mappingRow.normalizedActionText === "方向上"
                                    || mappingRow.normalizedActionText === "方向下"
                                    || mappingRow.normalizedActionText === "方向左"
                                    || mappingRow.normalizedActionText === "方向右"
                                    ? qsTr("按住可连续触发") : ""
                                color: tokens.textSecondary
                                font.pixelSize: tokens.fontSizeSmall
                                elide: Text.ElideRight
                            }
                        }

                        ColumnLayout {
                            objectName: "mappingDoubleCell_" + mappingRow.buttonId
                            Layout.preferredWidth: root.mappingActionColumnWidth
                            Layout.minimumWidth: root.mappingActionColumnWidth
                            Layout.maximumWidth: root.mappingActionColumnWidth
                            Layout.alignment: Qt.AlignVCenter
                            spacing: 1
                            Label {
                                objectName: "mappingDoublePrimaryText_"
                                    + mappingRow.buttonId
                                Layout.fillWidth: true
                                Layout.preferredHeight: 20
                                text: mappingRow.primaryIsVoice
                                    ? qsTr("不执行") : mappingRow.doubleClickText
                                color: mappingRow.primaryIsVoice
                                    || mappingRow.doubleClickText === "未设置"
                                    ? tokens.disabledText : tokens.textPrimary
                                font.pixelSize: tokens.fontSizeBody
                                elide: Text.ElideRight
                                horizontalAlignment: Text.AlignLeft
                            }
                            Item { Layout.preferredHeight: 16 }
                        }

                        ColumnLayout {
                            objectName: "mappingLongCell_" + mappingRow.buttonId
                            Layout.preferredWidth: root.mappingActionColumnWidth
                            Layout.minimumWidth: root.mappingActionColumnWidth
                            Layout.maximumWidth: root.mappingActionColumnWidth
                            Layout.alignment: Qt.AlignVCenter
                            spacing: 1
                            Label {
                                objectName: "mappingLongPrimaryText_"
                                    + mappingRow.buttonId
                                Layout.fillWidth: true
                                Layout.preferredHeight: 20
                                text: mappingRow.primaryIsVoice
                                    ? qsTr("不执行") : mappingRow.longPressText
                                color: mappingRow.primaryIsVoice
                                    || mappingRow.longPressText === "未设置"
                                    ? tokens.disabledText : tokens.textPrimary
                                font.pixelSize: tokens.fontSizeBody
                                elide: Text.ElideRight
                                horizontalAlignment: Text.AlignLeft
                            }
                            Item { Layout.preferredHeight: 16 }
                        }

                        Button {
                            objectName: "editMapping_" + mappingRow.buttonId
                            Layout.preferredWidth: root.mappingEditColumnWidth
                            Layout.minimumWidth: root.mappingEditColumnWidth
                            Layout.maximumWidth: root.mappingEditColumnWidth
                            text: qsTr("编辑")
                            flat: true
                            onClicked: {
                                SettingsController.selectButton(mappingRow.buttonId)
                                actionEditor.openForRow(
                                    mappingRow.index,
                                    mappingRow.buttonId,
                                    mappingRow.displayName,
                                    mappingRow.actionText,
                                    mappingRow.doubleClickText,
                                    mappingRow.longPressText
                                )
                            }
                            Accessible.name: qsTr("编辑") + mappingRow.displayName
                        }
                    }

                    }
                }
            }

            UiLabel {
                tokens: root.tokens
                kind: noteKind
                Layout.fillWidth: true
                text: qsTr("设置双击或长按后，快速连点和按住重复的识别方式可能随之改变。")
                wrapMode: Text.WordWrap
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
