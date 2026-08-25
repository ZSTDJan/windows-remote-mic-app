// "按键" tab: an RC003 product-photo reference with a selected-button marker
// sits beside the existing one-row-per-button mapping matrix. Mapping selection
// and editing remain owned by the matrix and real-key detection.
// SettingsController/ButtonMappingModel are QML singletons - see main.qml.
import QtQuick
import QtQuick.Controls
import QtQuick.Dialogs
import QtQuick.Layouts
import OvbRc003Settings 1.0

Item {
    id: root
    property var tokens
    readonly property var leftButtonIds: ["power", "up", "left", "back", "home", "menu"]
    readonly property real mappingCardGap: 4
    readonly property real mappingCardHeight: Math.max(
        38,
        Math.min(45, (mappingList.height - mappingCardGap * 6) / 7)
    )
    property bool connectorRepaintQueued: false

    function scheduleConnectorRepaint() {
        if (connectorRepaintQueued)
            return
        connectorRepaintQueued = true
        Qt.callLater(function() {
            connectorRepaintQueued = false
            mappingLines.requestPaint()
            for (let i = 0; i < photoHotspotRepeater.count; i++) {
                const hotspot = photoHotspotRepeater.itemAt(i)
                if (hotspot)
                    hotspot.requestConnectorPaint()
            }
        })
    }

    function isLeftButton(buttonId) {
        return leftButtonIds.indexOf(buttonId) >= 0
    }

    function visualRow(buttonId) {
        const rows = {
            "power": 0, "up": 1, "left": 2, "back": 3, "home": 4, "menu": 5,
            "mic": 0, "right": 1, "ok": 2, "down": 3,
            "volume_up": 4, "volume_down": 5, "tv": 6
        }
        return rows[buttonId]
    }

    function connectorControlRadius(startX, endX) {
        const span = Math.abs(endX - startX)
        return Math.min(32, Math.max(6, span * 0.42), span * 0.48)
    }

    function connectorStrokeColor(active) {
        if (active)
            return tokens.accent
        return Qt.rgba(
            tokens.borderStrong.r,
            tokens.borderStrong.g,
            tokens.borderStrong.b,
            0.75
        )
    }

    function connectorSplitParameter(
        startX, control1X, control2X, endX, splitX
    ) {
        let low = 0
        let high = 1
        const increasing = endX >= startX
        for (let i = 0; i < 16; i++) {
            const t = (low + high) / 2
            const oneMinusT = 1 - t
            const x = oneMinusT * oneMinusT * oneMinusT * startX
                + 3 * oneMinusT * oneMinusT * t * control1X
                + 3 * oneMinusT * t * t * control2X
                + t * t * t * endX
            if ((x < splitX) === increasing)
                low = t
            else
                high = t
        }
        return (low + high) / 2
    }

    function connectorRoute(card, hotspot, coordinateItem) {
        if (!card || !hotspot)
            return null

        const leftSide = root.isLeftButton(hotspot.buttonId)
        const start = card.mapToItem(
            coordinateItem,
            leftSide ? card.width : 0,
            card.height / 2
        )
        const center = hotspot.mapToItem(
            coordinateItem,
            hotspot.width / 2,
            hotspot.height / 2
        )
        const endX = center.x
            + (leftSide ? -hotspot.width / 2 : hotspot.width / 2)
        const endY = center.y
        const direction = leftSide ? 1 : -1
        const controlRadius = root.connectorControlRadius(start.x, endX)
        const control1X = start.x + direction * controlRadius
        const control1Y = start.y
        const control2X = endX - direction * controlRadius
        const control2Y = endY
        const framePoint = photoFrame.mapToItem(
            coordinateItem,
            leftSide ? 0 : photoFrame.width,
            0
        )
        const splitT = root.connectorSplitParameter(
            start.x, control1X, control2X, endX, framePoint.x
        )
        const aX = start.x + (control1X - start.x) * splitT
        const aY = start.y + (control1Y - start.y) * splitT
        const bX = control1X + (control2X - control1X) * splitT
        const bY = control1Y + (control2Y - control1Y) * splitT
        const cX = control2X + (endX - control2X) * splitT
        const cY = control2Y + (endY - control2Y) * splitT
        const dX = aX + (bX - aX) * splitT
        const dY = aY + (bY - aY) * splitT
        const eX = bX + (cX - bX) * splitT
        const eY = bY + (cY - bY) * splitT
        const splitX = dX + (eX - dX) * splitT
        const splitY = dY + (eY - dY) * splitT
        return {
            startX: start.x,
            startY: start.y,
            firstControl1X: aX,
            firstControl1Y: aY,
            firstControl2X: dX,
            firstControl2Y: dY,
            splitX: splitX,
            splitY: splitY,
            secondControl1X: eX,
            secondControl1Y: eY,
            secondControl2X: cX,
            secondControl2Y: cY,
            endX: endX,
            endY: endY
        }
    }

    function shortButtonName(buttonId) {
        const names = {
            "power": qsTr("电源"), "up": qsTr("上"), "left": qsTr("左"),
            "back": qsTr("返回"), "home": qsTr("主页"), "menu": qsTr("菜单"),
            "mic": qsTr("语音"), "right": qsTr("右"), "ok": qsTr("确定"),
            "down": qsTr("下"), "volume_up": "+", "volume_down": "-", "tv": "TV"
        }
        return names[buttonId] || buttonId
    }

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

    FileDialog {
        id: voiceProgramFileDialog
        title: qsTr("选择语音程序")
        nameFilters: [
            qsTr("程序或快捷方式 (*.exe *.lnk)"),
            qsTr("所有文件 (*)")
        ]
        onAccepted: SettingsController.voiceProgramCustomPath = selectedFile
    }

    Dialog {
        id: voiceProgramDialog
        objectName: "voiceProgramDialog"
        modal: true
        anchors.centerIn: parent
        width: Math.min(540, root.width - tokens.spacingLarge * 2)
        title: qsTr("语音程序")
        standardButtons: Dialog.Close
        onOpened: SettingsController.refreshVoiceProgramStatus()

        contentItem: ColumnLayout {
            spacing: tokens.spacingMedium

            RowLayout {
                Layout.fillWidth: true
                spacing: tokens.spacingSmall
                Label {
                    Layout.preferredWidth: 92
                    text: qsTr("语音输入程序")
                    color: tokens.textPrimary
                    font.weight: Font.Medium
                }
                SelectionComboBox {
                    id: voiceProgramCombo
                    objectName: "voiceProgramCombo"
                    tokens: root.tokens
                    Layout.fillWidth: true
                    model: SettingsController.voiceProgramOptions
                    currentIndex: SettingsController.selectedVoiceProgramIndex
                    onActivated: SettingsController.selectedVoiceProgramIndex = index
                    Accessible.name: qsTr("语音输入程序")
                }
            }

            RowLayout {
                Layout.fillWidth: true
                visible: SettingsController.selectedVoiceProgramIndex === 2
                spacing: tokens.spacingSmall
                Label {
                    Layout.preferredWidth: 92
                    text: qsTr("程序路径")
                    color: tokens.textPrimary
                    font.weight: Font.Medium
                }
                CompactTextField {
                    objectName: "voiceProgramCustomPathField"
                    tokens: root.tokens
                    Layout.fillWidth: true
                    text: SettingsController.voiceProgramCustomPath
                    placeholderText: qsTr("选择 .exe 或 .lnk")
                    onEditingFinished: SettingsController.voiceProgramCustomPath = text
                    Accessible.name: qsTr("自定义语音程序路径")
                }
                CompactButton {
                    objectName: "browseVoiceProgramButton"
                    tokens: root.tokens
                    compactMinimumWidth: 58
                    text: qsTr("选择")
                    onClicked: voiceProgramFileDialog.open()
                }
            }

            CheckBox {
                objectName: "voiceProgramAutoStartCheckBox"
                text: qsTr("随桥接启动")
                checked: SettingsController.voiceProgramLaunchOnBridgeStart
                enabled: SettingsController.selectedVoiceProgramIndex !== 0
                onClicked: SettingsController.voiceProgramLaunchOnBridgeStart = checked
            }

            CheckBox {
                objectName: "voiceProgramElevatedCheckBox"
                text: qsTr("以管理员权限启动")
                checked: SettingsController.voiceProgramLaunchElevated
                enabled: SettingsController.selectedVoiceProgramIndex !== 0
                onClicked: SettingsController.voiceProgramLaunchElevated = checked
            }

            Label {
                Layout.fillWidth: true
                visible: SettingsController.voiceProgramLaunchElevated
                    && SettingsController.selectedVoiceProgramIndex !== 0
                text: qsTr("启动时会显示 Windows 管理员确认；取消不会影响 Remote Mic。")
                wrapMode: Text.WordWrap
                color: tokens.textSecondary
                font.pixelSize: tokens.fontSizeSmall
            }

            Label {
                objectName: "voiceProgramStatusLabel"
                Layout.fillWidth: true
                text: SettingsController.voiceProgramStatusText
                wrapMode: Text.WordWrap
                color: tokens.textSecondary
                font.pixelSize: tokens.fontSizeSmall
            }

            RowLayout {
                Layout.fillWidth: true
                Layout.alignment: Qt.AlignRight
                spacing: tokens.spacingSmall
                Item { Layout.fillWidth: true }
                CompactButton {
                    objectName: "refreshVoiceProgramButton"
                    tokens: root.tokens
                    compactMinimumWidth: 64
                    text: qsTr("重新检测")
                    onClicked: SettingsController.refreshVoiceProgramStatus()
                }
                CompactButton {
                    objectName: "launchVoiceProgramButton"
                    tokens: root.tokens
                    compactMinimumWidth: 58
                    text: qsTr("启动")
                    enabled: SettingsController.selectedVoiceProgramIndex !== 0
                    onClicked: SettingsController.launchVoiceProgram()
                }
                CompactButton {
                    objectName: "saveVoiceProgramButton"
                    tokens: root.tokens
                    compactMinimumWidth: 58
                    text: qsTr("保存")
                    highlighted: true
                    onClicked: {
                        if (SettingsController.saveSettings())
                            voiceProgramDialog.close()
                    }
                }
            }
        }
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

    Item {
        id: rc003MappingLayout
        objectName: "rc003MappingLayout"
        visible: SettingsController.isRc003Device
        anchors.fill: parent

        ColumnLayout {
            anchors.fill: parent
            anchors.margins: tokens.pageHorizontalPadding
            spacing: tokens.spacingSmall

            SectionFrame {
                id: voiceSettingsPanel
                objectName: "voiceSettingsPanel"
                tokens: root.tokens
                Layout.fillWidth: true
                Layout.preferredHeight: 45
                horizontalPadding: 9
                verticalPadding: 8

                RowLayout {
                    Layout.fillWidth: true
                    spacing: tokens.spacingMedium

                    UiLabel {
                        tokens: root.tokens
                        kind: bodyKind
                        Layout.preferredWidth: 126
                        Layout.minimumWidth: 88
                        Layout.maximumWidth: 88
                        text: qsTr("语音快捷键")
                        font.weight: Font.Medium
                    }
                    CompactTextField {
                        id: holdVoiceHotkeyField
                        objectName: "holdVoiceHotkeyField"
                        tokens: root.tokens
                        Layout.fillWidth: true
                        text: SettingsController.holdVoiceHotkeyText
                        placeholderText: qsTr("例如 ralt")
                        selectByMouse: true
                        onEditingFinished: SettingsController.holdVoiceHotkeyText = text
                        Accessible.name: qsTr("语音快捷键")
                        ToolTip.text: qsTr("默认右侧 Alt；请与目标语音软件的快捷键保持一致。")
                    }
                    CompactButton {
                        tokens: root.tokens
                        compactMinimumWidth: 58
                        text: qsTr("录入")
                        onClicked: root.openShortcutRecorder("", -1, "", "hold")
                        Accessible.name: qsTr("录入语音快捷键")
                    }
                    CompactButton {
                        objectName: "voiceProgramButton"
                        tokens: root.tokens
                        compactMinimumWidth: 72
                        text: qsTr("语音程序")
                        onClicked: voiceProgramDialog.open()
                        Accessible.name: qsTr("管理语音程序")
                    }
                }
            }

            Item {
                id: mappingList
                objectName: "mappingList"
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.minimumHeight: 306
                property int count: 13
                property int currentIndex: ButtonMappingModel.indexOfButton(
                    SettingsController.selectedButtonId
                )
                onXChanged: root.scheduleConnectorRepaint()
                onYChanged: root.scheduleConnectorRepaint()
                onWidthChanged: root.scheduleConnectorRepaint()
                onHeightChanged: root.scheduleConnectorRepaint()

                Canvas {
                    id: mappingLines
                    anchors.fill: parent
                    z: 0
                    antialiasing: true

                    onPaint: {
                        const ctx = getContext("2d")
                        ctx.reset()
                        for (let i = 0; i < ButtonMappingModel.rowCount(); i++) {
                            const leftCard = leftCardRepeater.itemAt(i)
                            const rightCard = rightCardRepeater.itemAt(i)
                            const hotspot = photoHotspotRepeater.itemAt(i)
                            const buttonId = hotspot ? hotspot.buttonId : ""
                            const leftSide = root.isLeftButton(buttonId)
                            const card = leftSide ? leftCard : rightCard
                            if (!card || !hotspot || !card.visible || !hotspot.visible)
                                continue

                            const active = SettingsController.selectedButtonId === buttonId
                            const route = root.connectorRoute(
                                card, hotspot, mappingLines
                            )
                            if (!route)
                                continue
                            ctx.beginPath()
                            ctx.moveTo(route.startX, route.startY)
                            ctx.bezierCurveTo(
                                route.firstControl1X,
                                route.firstControl1Y,
                                route.firstControl2X,
                                route.firstControl2Y,
                                route.splitX,
                                route.splitY
                            )
                            ctx.strokeStyle = root.connectorStrokeColor(active)
                            ctx.lineWidth = active ? 1.5 : 0.8
                            ctx.stroke()
                        }
                    }

                    onWidthChanged: root.scheduleConnectorRepaint()
                    onHeightChanged: root.scheduleConnectorRepaint()

                    Connections {
                        target: SettingsController
                        function onSelectedButtonIdChanged() {
                            root.scheduleConnectorRepaint()
                        }
                    }
                }

                GridLayout {
                    id: leftSideCards
                    objectName: "leftMappingCards"
                    anchors.left: parent.left
                    anchors.verticalCenter: parent.verticalCenter
                    width: (parent.width - photoSidebar.width - tokens.spacingMedium * 2) / 2
                    columns: 1
                    rows: 6
                    rowSpacing: root.mappingCardGap
                    z: 1
                    onXChanged: root.scheduleConnectorRepaint()
                    onYChanged: root.scheduleConnectorRepaint()
                    onWidthChanged: root.scheduleConnectorRepaint()
                    onHeightChanged: root.scheduleConnectorRepaint()

                    Repeater {
                        id: leftCardRepeater
                        model: ButtonMappingModel
                        onItemAdded: root.scheduleConnectorRepaint()
                        onItemRemoved: root.scheduleConnectorRepaint()
                        delegate: MappingCard {
                            required property int index
                            required property string buttonId
                            required property string displayName
                            required property string actionText
                            required property string doubleClickText
                            required property string longPressText
                            required property bool isSelected

                            visible: root.isLeftButton(buttonId)
                            exposeObjectNames: visible
                            tokens: root.tokens
                            cardId: buttonId
                            Layout.row: root.visualRow(buttonId)
                            Layout.fillWidth: true
                            Layout.preferredHeight: visible ? root.mappingCardHeight : 0
                            buttonName: root.shortButtonName(buttonId)
                            singleText: actionText
                            doubleText: doubleClickText
                            longText: longPressText
                            selected: isSelected
                            voiceAction: actionText.trim() === "按住说话"
                                || actionText.indexOf("已停用：旧语音配置") === 0
                            onXChanged: root.scheduleConnectorRepaint()
                            onYChanged: root.scheduleConnectorRepaint()
                            onWidthChanged: root.scheduleConnectorRepaint()
                            onHeightChanged: root.scheduleConnectorRepaint()
                            onVisibleChanged: root.scheduleConnectorRepaint()
                            onClicked: {
                                SettingsController.selectButton(buttonId)
                                actionEditor.openForRow(
                                    index, buttonId, displayName, actionText,
                                    doubleClickText, longPressText
                                )
                            }
                        }
                    }
                }

                Item {
                    id: photoSidebar
                    objectName: "photoSidebar"
                    anchors.horizontalCenter: parent.horizontalCenter
                    anchors.verticalCenter: parent.verticalCenter
                    width: 86
                    height: 230
                    z: 1
                    onXChanged: root.scheduleConnectorRepaint()
                    onYChanged: root.scheduleConnectorRepaint()
                    onWidthChanged: root.scheduleConnectorRepaint()
                    onHeightChanged: root.scheduleConnectorRepaint()

                    Item {
                        id: photoFrame
                        objectName: "photoFrame"
                        anchors.top: parent.top
                        anchors.horizontalCenter: parent.horizontalCenter
                        width: 86
                        height: 210
                        clip: true
                        onXChanged: root.scheduleConnectorRepaint()
                        onYChanged: root.scheduleConnectorRepaint()
                        onWidthChanged: root.scheduleConnectorRepaint()
                        onHeightChanged: root.scheduleConnectorRepaint()

                        Image {
                            id: photoImage
                            objectName: "photoImage"
                            anchors.horizontalCenter: parent.horizontalCenter
                            anchors.verticalCenter: parent.verticalCenter
                            width: height * 240 / 360
                            height: parent.height
                            source: SettingsController.photoAvailable
                                ? SettingsController.photoSource : ""
                            visible: SettingsController.photoAvailable
                            fillMode: Image.Stretch
                            smooth: true
                            mipmap: true
                        }

                        UiLabel {
                            anchors.centerIn: parent
                            width: parent.width
                            visible: !SettingsController.photoAvailable
                            tokens: root.tokens
                            kind: noteKind
                            text: qsTr("实物图缺失")
                            horizontalAlignment: Text.AlignHCenter
                        }

                        Repeater {
                            id: photoHotspotRepeater
                            model: ButtonMappingModel
                            onItemAdded: root.scheduleConnectorRepaint()
                            onItemRemoved: root.scheduleConnectorRepaint()
                            delegate: Item {
                                id: photoHotspot
                                objectName: "photoHotspot_" + buttonId

                                required property int index
                                required property string buttonId
                                required property real hotspotX
                                required property real hotspotY
                                required property real hotspotWidth
                                required property real hotspotHeight
                                required property bool isSelected
                                required property bool isVoice

                                width: hotspotWidth * photoImage.paintedWidth
                                height: hotspotHeight * photoImage.paintedHeight
                                x: photoImage.x
                                    + (photoImage.width - photoImage.paintedWidth) / 2
                                    + hotspotX * photoImage.paintedWidth
                                    - width / 2
                                y: photoImage.y
                                    + (photoImage.height - photoImage.paintedHeight) / 2
                                    + hotspotY * photoImage.paintedHeight
                                    - height / 2
                                visible: SettingsController.photoAvailable
                                z: 2

                                function requestConnectorPaint() {
                                    hotspotConnector.requestPaint()
                                }

                                onXChanged: root.scheduleConnectorRepaint()
                                onYChanged: root.scheduleConnectorRepaint()
                                onWidthChanged: root.scheduleConnectorRepaint()
                                onHeightChanged: root.scheduleConnectorRepaint()
                                onVisibleChanged: root.scheduleConnectorRepaint()

                                Canvas {
                                    id: hotspotConnector
                                    objectName: "photoHotspotConnector_"
                                        + photoHotspot.buttonId
                                    readonly property bool leftSide:
                                        root.isLeftButton(photoHotspot.buttonId)
                                    readonly property bool active:
                                        photoHotspot.isSelected

                                    x: -photoHotspot.x
                                    y: -photoHotspot.y
                                    width: photoFrame.width
                                    height: photoFrame.height
                                    antialiasing: true

                                    onPaint: {
                                        const ctx = getContext("2d")
                                        ctx.reset()
                                        const card = leftSide
                                            ? leftCardRepeater.itemAt(photoHotspot.index)
                                            : rightCardRepeater.itemAt(photoHotspot.index)
                                        const route = root.connectorRoute(
                                            card, photoHotspot, hotspotConnector
                                        )
                                        if (!route)
                                            return
                                        ctx.beginPath()
                                        ctx.moveTo(route.splitX, route.splitY)
                                        ctx.bezierCurveTo(
                                            route.secondControl1X,
                                            route.secondControl1Y,
                                            route.secondControl2X,
                                            route.secondControl2Y,
                                            route.endX,
                                            route.endY
                                        )
                                        ctx.strokeStyle =
                                            root.connectorStrokeColor(active)
                                        ctx.lineWidth = active ? 1.5 : 0.8
                                        ctx.stroke()
                                    }

                                    onWidthChanged: root.scheduleConnectorRepaint()
                                    onHeightChanged: root.scheduleConnectorRepaint()
                                    onXChanged: root.scheduleConnectorRepaint()
                                    onYChanged: root.scheduleConnectorRepaint()
                                    onActiveChanged: root.scheduleConnectorRepaint()

                                    Connections {
                                        target: mappingList
                                        function onWidthChanged() {
                                            root.scheduleConnectorRepaint()
                                        }
                                        function onHeightChanged() {
                                            root.scheduleConnectorRepaint()
                                        }
                                    }
                                }

                                Rectangle {
                                    objectName: "photoHotspotMarker_" + photoHotspot.buttonId
                                    anchors.fill: parent
                                    z: 1
                                    visible: photoHotspot.isSelected
                                    radius: Math.min(width, height) / 2
                                    color: photoHotspot.isVoice
                                        ? Qt.rgba(tokens.voiceAccent.r, tokens.voiceAccent.g,
                                                  tokens.voiceAccent.b, 0.24)
                                        : Qt.rgba(tokens.accent.r, tokens.accent.g,
                                                  tokens.accent.b, 0.20)
                                    border.width: 2
                                    border.color: photoHotspot.isVoice
                                        ? tokens.voiceAccent : tokens.accent
                                }

                                TapHandler {
                                    onTapped: SettingsController.selectButton(photoHotspot.buttonId)
                                }
                                HoverHandler { id: hotspotHover }
                                Rectangle {
                                    anchors.fill: parent
                                    z: 1
                                    visible: hotspotHover.hovered && !photoHotspot.isSelected
                                    radius: Math.min(width, height) / 2
                                    color: Qt.rgba(tokens.accent.r, tokens.accent.g,
                                                   tokens.accent.b, 0.10)
                                    border.color: tokens.accent
                                }
                            }
                        }

                        Connections {
                            target: photoImage
                            function onPaintedWidthChanged() {
                                root.scheduleConnectorRepaint()
                            }
                            function onPaintedHeightChanged() {
                                root.scheduleConnectorRepaint()
                            }
                        }
                    }

                    UiLabel {
                        anchors.bottom: parent.bottom
                        anchors.horizontalCenter: parent.horizontalCenter
                        tokens: root.tokens
                        kind: noteKind
                        text: qsTr("当前：") + root.shortButtonName(
                            SettingsController.selectedButtonId
                        )
                        horizontalAlignment: Text.AlignHCenter
                    }
                }

                GridLayout {
                    id: rightSideCards
                    objectName: "rightMappingCards"
                    anchors.right: parent.right
                    anchors.verticalCenter: parent.verticalCenter
                    width: (parent.width - photoSidebar.width - tokens.spacingMedium * 2) / 2
                    columns: 1
                    rows: 7
                    rowSpacing: root.mappingCardGap
                    z: 1
                    onXChanged: root.scheduleConnectorRepaint()
                    onYChanged: root.scheduleConnectorRepaint()
                    onWidthChanged: root.scheduleConnectorRepaint()
                    onHeightChanged: root.scheduleConnectorRepaint()

                    Repeater {
                        id: rightCardRepeater
                        model: ButtonMappingModel
                        onItemAdded: root.scheduleConnectorRepaint()
                        onItemRemoved: root.scheduleConnectorRepaint()
                        delegate: MappingCard {
                            required property int index
                            required property string buttonId
                            required property string displayName
                            required property string actionText
                            required property string doubleClickText
                            required property string longPressText
                            required property bool isSelected

                            visible: !root.isLeftButton(buttonId)
                            exposeObjectNames: visible
                            tokens: root.tokens
                            cardId: buttonId
                            Layout.row: root.visualRow(buttonId)
                            Layout.fillWidth: true
                            Layout.preferredHeight: visible ? root.mappingCardHeight : 0
                            buttonName: root.shortButtonName(buttonId)
                            singleText: actionText
                            doubleText: doubleClickText
                            longText: longPressText
                            selected: isSelected
                            voiceAction: actionText.trim() === "按住说话"
                                || actionText.indexOf("已停用：旧语音配置") === 0
                            onXChanged: root.scheduleConnectorRepaint()
                            onYChanged: root.scheduleConnectorRepaint()
                            onWidthChanged: root.scheduleConnectorRepaint()
                            onHeightChanged: root.scheduleConnectorRepaint()
                            onVisibleChanged: root.scheduleConnectorRepaint()
                            onClicked: {
                                SettingsController.selectButton(buttonId)
                                actionEditor.openForRow(
                                    index, buttonId, displayName, actionText,
                                    doubleClickText, longPressText
                                )
                            }
                        }
                    }
                }
            }

            SectionFrame {
                id: mappingListFrame
                objectName: "mappingListFrame"
                tokens: root.tokens
                Layout.fillWidth: true
                Layout.preferredHeight: 40
                horizontalPadding: 7
                verticalPadding: 5

                RowLayout {
                    Layout.fillWidth: true
                    spacing: tokens.spacingSmall

                    Rectangle {
                        Layout.preferredWidth: 8
                        Layout.preferredHeight: 8
                        radius: 4
                        color: SettingsController.keyDetectionActive
                            ? tokens.accent : tokens.voiceAccent
                    }
                    UiLabel {
                        id: bridgeRequiredWarning
                        objectName: "mappingBridgeWarning"
                        tokens: root.tokens
                        kind: noteKind
                        Layout.fillWidth: true
                        text: SettingsController.keyDetectionActive
                            ? SettingsController.keyDetectionText
                            : !SettingsController.bridgeRunning
                                ? qsTr("后台桥接未运行；语音键和补充检测通道不可用")
                                : qsTr("语音设为“按住说话”后，双击和长按无效")
                        elide: Text.ElideRight
                    }
                    CompactButton {
                        id: detectRealKeyButton
                        objectName: "detectRealKeyButton"
                        tokens: root.tokens
                        compactMinimumWidth: 88
                        text: SettingsController.keyDetectionActive
                            ? qsTr("停止检测") : qsTr("检测真实按键")
                        highlighted: SettingsController.keyDetectionActive
                        onClicked: SettingsController.keyDetectionActive
                            ? SettingsController.stopKeyDetection()
                            : SettingsController.startKeyDetection()
                        Accessible.name: qsTr("检测真实遥控器按键")
                    }
                    CompactButton {
                        objectName: "restoreMappingDefaultsButton"
                        tokens: root.tokens
                        compactMinimumWidth: 68
                        text: qsTr("恢复默认")
                        onClicked: SettingsController.restoreDefaults()
                    }
                    CompactButton {
                        id: saveMappingButton
                        objectName: "saveMappingButton"
                        tokens: root.tokens
                        compactMinimumWidth: 68
                        text: qsTr("保存映射")
                        highlighted: true
                        onClicked: SettingsController.saveSettings()
                    }
                }
            }
        }

        Connections {
            target: SettingsController
            function onHoldVoiceHotkeyTextChanged() {
                holdVoiceHotkeyField.text = SettingsController.holdVoiceHotkeyText
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
