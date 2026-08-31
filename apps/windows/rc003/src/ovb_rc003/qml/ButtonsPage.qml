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
    readonly property var leftButtonIds: ["power", "up", "left", "back", "home", "menu"]
    readonly property real mappingCardGap: 2
    readonly property real mappingBoardGap: 6
    property int mappingViewIndex: 0
    property bool connectorRepaintQueued: false
    property var backTabTarget: null
    property var tabTarget: null
    readonly property var firstFocusItem: singleMappingViewButton
    readonly property var lastFocusItem: saveMappingButton
    readonly property bool hasPendingEditorDraft:
        actionEditor.visible && actionEditor.draftDirty

    function commitPendingEditorDraft() {
        if (!actionEditor.visible)
            return true
        return actionEditor.saveDraft()
    }

    function discardPendingEditorDraft() {
        if (actionEditor.visible)
            actionEditor.close()
    }

    function settleInputUiAfterStop() {
        if (shortcutRecorder.visible && !SettingsController.hotkeyCaptureActive)
            shortcutRecorder.finishClose()
    }

    function prepareForLifecyclePrompt() {
        if (!shortcutRecorder.visible)
            return true
        return shortcutRecorder.requestClose()
    }

    onWidthChanged: scheduleConnectorRepaint()
    onHeightChanged: scheduleConnectorRepaint()
    Component.onCompleted: scheduleConnectorRepaint()

    function scheduleConnectorRepaint() {
        if (connectorRepaintQueued)
            return
        connectorRepaintQueued = true
        Qt.callLater(function() {
            connectorRepaintQueued = false
            mappingLines.requestPaint()
            activeMappingLine.requestPaint()
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
        const preferred = Math.max(12, Math.min(72, span * 0.56))
        return Math.min(preferred, span * 0.48)
    }

    function connectorStrokeColor(active) {
        if (active)
            return tokens.accent
        return tokens.borderStrong
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
        const endY = center.y
        const direction = leftSide ? 1 : -1
        const controlRadius = root.connectorControlRadius(start.x, endX)
        const control1X = start.x + direction * controlRadius
        const control1Y = start.y
        const control2X = endX - direction * controlRadius
        const control2Y = endY
        return {
            startX: start.x,
            startY: start.y,
            control1X: control1X,
            control1Y: control1Y,
            control2X: control2X,
            control2Y: control2Y,
            endX: endX,
            endY: endY
        }
    }

    function paintConnectors(canvas, activeOnly) {
        const ctx = canvas.getContext("2d")
        ctx.reset()
        for (let i = 0; i < ButtonMappingModel.rowCount(); i++) {
            const leftCard = leftCardRepeater.itemAt(i)
            const rightCard = rightCardRepeater.itemAt(i)
            const hotspot = photoHotspotRepeater.itemAt(i)
            const buttonId = hotspot ? hotspot.buttonId : ""
            const active = SettingsController.selectedButtonId === buttonId
            if (active !== activeOnly)
                continue
            const card = root.isLeftButton(buttonId) ? leftCard : rightCard
            if (!card || !hotspot || !card.visible || !hotspot.visible)
                continue
            const route = root.connectorRoute(card, hotspot, canvas)
            if (!route)
                continue
            ctx.beginPath()
            ctx.lineCap = "round"
            ctx.lineJoin = "round"
            ctx.moveTo(route.startX, route.startY)
            ctx.bezierCurveTo(
                route.control1X,
                route.control1Y,
                route.control2X,
                route.control2Y,
                route.endX,
                route.endY
            )
            ctx.strokeStyle = root.connectorStrokeColor(active)
            ctx.lineWidth = active ? 2.2 : 1.25
            ctx.stroke()
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

    function openShortcutRecorder(buttonId, rowIndex, trigger, targetEditor) {
        shortcutRecorder.buttonId = buttonId
        shortcutRecorder.rowIndex = rowIndex
        shortcutRecorder.trigger = trigger || "single_click"
        shortcutRecorder.targetEditor = targetEditor || null
        shortcutRecorder.previewText = qsTr("请按下希望遥控器发送的键盘快捷键")
        shortcutRecorder.open()
    }

    Timer {
        interval: 100
        repeat: true
        running: SettingsController.keyDetectionActive
        onTriggered: SettingsController.pollKeyDetectionBridge()
    }

    Connections {
        target: ButtonMappingModel
        function onDataChanged() {
            root.scheduleConnectorRepaint()
        }
    }

    Dialog {
        id: restoreMappingDefaultsDialog
        objectName: "restoreMappingDefaultsDialog"
        title: qsTr("恢复内置默认？")
        modal: true
        anchors.centerIn: parent
        standardButtons: Dialog.Ok | Dialog.Cancel
        onAccepted: SettingsController.restoreMappingDefaults()

        UiLabel {
            tokens: root.tokens
            kind: bodyKind
            width: 340
            wrapMode: Text.WordWrap
            text: qsTr("这会把 13 个按键恢复为程序内置映射，并清空遥控器组合。确认后仍需点击“保存映射”才会写入设置。")
        }
    }


    Dialog {
        id: shortcutRecorder
        objectName: "shortcutRecorderDialog"
        modal: true
        popupType: Popup.Item
        anchors.centerIn: parent
        width: Math.min(430, root.width - 28)
        readonly property string headerText: qsTr("录入快捷键")
        title: headerText
        standardButtons: Dialog.NoButton
        leftPadding: 14
        rightPadding: 14
        topPadding: 0
        bottomPadding: 11
        leftInset: 0
        rightInset: 0
        topInset: 0
        bottomInset: 0
        closePolicy: Popup.NoAutoClose
        property string buttonId: ""
        property int rowIndex: -1
        property string trigger: "single_click"
        property string previewText: ""
        property var targetEditor: null
        property bool pendingClose: false
        property string pendingChord: ""

        function commitShortcut(chord) {
            previewText = chord
            pendingChord = chord
            requestClose()
        }

        function finishClose() {
            if (pendingChord.length > 0) {
                if (targetEditor) {
                    targetEditor.editText = pendingChord
                } else {
                    actionEditor.applyCapturedShortcut(
                        rowIndex, trigger, pendingChord
                    )
                }
            }
            pendingChord = ""
            pendingClose = false
            close()
        }

        function requestClose() {
            pendingClose = true
            if (!SettingsController.hotkeyCaptureActive) {
                finishClose()
                return true
            }
            if (!SettingsController.stopHotkeyCapture()) {
                pendingClose = false
                previewText = qsTr("无法停止快捷键录入，请重试")
                return false
            }
            if (!SettingsController.hotkeyCaptureActive)
                finishClose()
            return true
        }

        onOpened: {
            pendingClose = false
            pendingChord = ""
            captureArea.forceActiveFocus()
            SettingsController.startHotkeyCapture()
        }

        onClosed: {
            if (SettingsController.hotkeyCaptureActive)
                SettingsController.stopHotkeyCapture()
            targetEditor = null
            pendingClose = false
            pendingChord = ""
        }

        background: Rectangle {
            radius: tokens.cornerRadiusLarge
            color: tokens.surface
            border.width: tokens.hairlineWidth
            border.color: tokens.border
        }

        header: Item {
            width: shortcutRecorder.width
            implicitHeight: 38

            UiLabel {
                anchors.left: parent.left
                anchors.leftMargin: 14
                anchors.verticalCenter: parent.verticalCenter
                tokens: root.tokens
                kind: sectionTitleKind
                text: shortcutRecorder.headerText
                font.weight: Font.Medium
            }

            DialogCloseButton {
                id: shortcutRecorderCloseButton
                objectName: "shortcutRecorderCloseButton"
                tokens: root.tokens
                anchors.right: parent.right
                anchors.rightMargin: 7
                anchors.verticalCenter: parent.verticalCenter
                onCloseRequested: shortcutRecorder.requestClose()
            }
        }

        Connections {
            target: SettingsController
            function onHotkeyCaptured(chord) {
                if (shortcutRecorder.visible)
                    shortcutRecorder.commitShortcut(chord)
            }
            function onHotkeyCaptureError(message) {
                if (shortcutRecorder.visible) {
                    shortcutRecorder.previewText = message
                    shortcutRecorder.pendingClose = false
                }
            }
            function onHotkeyCaptureActiveChanged() {
                if (shortcutRecorder.visible
                        && shortcutRecorder.pendingClose
                        && !SettingsController.hotkeyCaptureActive) {
                    shortcutRecorder.finishClose()
                }
            }
        }

        contentItem: FocusScope {
            id: captureArea
            implicitHeight: 104
            focus: true
            Keys.onEscapePressed: shortcutRecorder.requestClose()

            ColumnLayout {
                anchors.fill: parent
                spacing: tokens.spacingMedium
                UiLabel {
                    tokens: root.tokens
                    kind: sectionTitleKind
                    Layout.fillWidth: true
                    horizontalAlignment: Text.AlignHCenter
                    text: shortcutRecorder.previewText
                    color: tokens.accent
                }
                UiLabel {
                    tokens: root.tokens
                    kind: noteKind
                    Layout.fillWidth: true
                    horizontalAlignment: Text.AlignHCenter
                    text: qsTr("按下要发送的单键或组合键")
                    elide: Text.ElideRight
                }
                RowLayout {
                    Layout.fillWidth: true
                    Item { Layout.fillWidth: true }
                    CompactButton {
                        tokens: root.tokens
                        compactMinimumWidth: tokens.buttonWidth2Chars
                        text: qsTr("取消")
                        onClicked: shortcutRecorder.close()
                    }
                }
            }
        }
    }

    component EditorActionCombo: ComboBox {
        id: editorCombo
        property var tokens

        editable: true
        selectTextByMouse: true
        implicitHeight: tokens.controlHeight
        leftPadding: 7
        rightPadding: 22
        font.family: tokens.fontFamily
        font.pixelSize: tokens.fontSizeControl

        indicator: DropDownIndicator {
            x: editorCombo.width - width - 7
            y: (editorCombo.height - height) / 2
            indicatorColor: editorCombo.enabled
                ? tokens.textSecondary : tokens.disabledText
        }

        background: Rectangle {
            radius: tokens.cornerRadiusControl
            color: tokens.fieldBackground
            border.width: tokens.hairlineWidth
            border.color: editorCombo.activeFocus ? tokens.accent : tokens.border
        }

        delegate: ItemDelegate {
            id: optionDelegate
            readonly property string groupTitle:
                SettingsController.actionOptionGroupTitle(String(modelData))
            readonly property bool startsGroup:
                SettingsController.actionOptionStartsGroup(String(modelData))
            readonly property int groupHeaderHeight: startsGroup
                ? Math.ceil(tokens.fontSizeTiny) + tokens.spacingMedium : 0
            objectName: editorCombo.objectName + "_option_" + index
            width: ListView.view ? ListView.view.width : editorCombo.width
            height: tokens.controlHeight + groupHeaderHeight
            topPadding: groupHeaderHeight
            bottomPadding: 0
            leftPadding: 7
            rightPadding: 7
            highlighted: editorCombo.highlightedIndex === index
            contentItem: Label {
                text: modelData
                color: tokens.textPrimary
                font.family: tokens.fontFamily
                font.pixelSize: tokens.fontSizeControl
                font.weight: index === editorCombo.currentIndex
                    ? Font.DemiBold : Font.Normal
                verticalAlignment: Text.AlignVCenter
                elide: Text.ElideRight
            }
            background: Item {
                Rectangle {
                    visible: optionDelegate.startsGroup
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.top: parent.top
                    height: tokens.hairlineWidth
                    color: tokens.border
                }
                Label {
                    visible: optionDelegate.startsGroup
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.top: parent.top
                    anchors.leftMargin: 7
                    anchors.rightMargin: 7
                    anchors.topMargin: 1
                    height: optionDelegate.groupHeaderHeight - 1
                    text: optionDelegate.groupTitle
                    color: tokens.textSecondary
                    font.family: tokens.fontFamily
                    font.pixelSize: tokens.fontSizeTiny
                    font.weight: Font.Medium
                    verticalAlignment: Text.AlignVCenter
                    elide: Text.ElideRight
                }
                Rectangle {
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.bottom: parent.bottom
                    height: tokens.controlHeight
                    color: optionDelegate.highlighted
                    ? tokens.accentSoft : tokens.surface
                }
            }
            MouseArea {
                visible: optionDelegate.startsGroup
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.top: parent.top
                height: optionDelegate.groupHeaderHeight
                acceptedButtons: Qt.LeftButton
            }
            Accessible.name: String(modelData)
        }
    }

    Dialog {
        id: actionEditor
        objectName: "actionEditorDialog"
        modal: true
        popupType: Popup.Item
        anchors.centerIn: parent
        width: Math.min(430, root.width - tokens.spacingLarge * 2)
        readonly property string headerText: buttonName.length > 0
            ? qsTr("编辑：") + buttonName
            : qsTr("编辑")
        title: headerText
        standardButtons: Dialog.NoButton
        leftPadding: 14
        rightPadding: 14
        topPadding: 0
        bottomPadding: 11
        leftInset: 0
        rightInset: 0
        topInset: 0
        bottomInset: 0
        closePolicy: Popup.CloseOnEscape

        background: Rectangle {
            radius: tokens.cornerRadiusLarge
            color: tokens.surface
            border.width: tokens.hairlineWidth
            border.color: tokens.border
        }

        header: Item {
            width: actionEditor.width
            implicitHeight: 34

            UiLabel {
                anchors.left: parent.left
                anchors.leftMargin: 14
                anchors.verticalCenter: parent.verticalCenter
                tokens: root.tokens
                kind: sectionTitleKind
                text: actionEditor.headerText
                font.pixelSize: tokens.fontSizeTitle
                font.weight: Font.Medium
            }

            DialogCloseButton {
                id: actionEditorCloseButton
                objectName: "actionEditorCloseButton"
                tokens: root.tokens
                anchors.right: parent.right
                anchors.rightMargin: 7
                anchors.verticalCenter: parent.verticalCenter
                onCloseRequested: actionEditor.close()
            }
        }

        property int rowIndex: -1
        property string buttonId: ""
        property string buttonName: ""
        property string primaryText: ""
        property string doubleText: "未设置"
        property string longText: "未设置"
        property string primaryNote: ""
        property string doubleNote: ""
        property string longNote: ""
        property string originalPrimaryText: ""
        property string originalDoubleText: "未设置"
        property string originalLongText: "未设置"
        property string originalPrimaryNote: ""
        property string originalDoubleNote: ""
        property string originalLongNote: ""
        property bool syncing: false
        readonly property string normalizedPrimaryText: primaryText.trim()
        readonly property bool primaryIsVoice:
            normalizedPrimaryText === "按住说话"
            || normalizedPrimaryText.indexOf("已停用：旧语音配置") === 0
        readonly property bool draftDirty:
            primaryText !== originalPrimaryText
            || doubleText !== originalDoubleText
            || longText !== originalLongText
            || primaryNote !== originalPrimaryNote
            || doubleNote !== originalDoubleNote
            || longNote !== originalLongNote

        function openForRow(rowIndexValue, buttonIdValue, buttonNameValue,
                            primaryValue, doubleValue, longValue,
                            primaryNoteValue, doubleNoteValue, longNoteValue) {
            syncing = true
            rowIndex = rowIndexValue
            buttonId = buttonIdValue
            buttonName = buttonNameValue
            primaryText = primaryValue
            doubleText = doubleValue
            longText = longValue
            primaryNote = primaryNoteValue
            doubleNote = doubleNoteValue
            longNote = longNoteValue
            originalPrimaryText = primaryValue
            originalDoubleText = doubleValue
            originalLongText = longValue
            originalPrimaryNote = primaryNoteValue
            originalDoubleNote = doubleNoteValue
            originalLongNote = longNoteValue
            primaryCombo.editText = primaryValue
            doubleCombo.editText = doubleValue
            longCombo.editText = longValue
            primaryNoteField.text = primaryNoteValue
            doubleNoteField.text = doubleNoteValue
            longNoteField.text = longNoteValue
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

        function saveDraft() {
            if (rowIndex < 0)
                return false
            ButtonMappingModel.setActionTextAt(rowIndex, primaryText)
            ButtonMappingModel.setSecondaryActionTextAt(
                rowIndex, "double_click", doubleText
            )
            ButtonMappingModel.setSecondaryActionTextAt(
                rowIndex, "long_press", longText
            )
            ButtonMappingModel.setDisplayNoteAt(
                rowIndex, "single_click", primaryNote
            )
            ButtonMappingModel.setDisplayNoteAt(
                rowIndex, "double_click", doubleNote
            )
            ButtonMappingModel.setDisplayNoteAt(
                rowIndex, "long_press", longNote
            )
            close()
            return true
        }

        onClosed: {
            syncing = true
            rowIndex = -1
        }

        contentItem: ColumnLayout {
            spacing: 9

            GridLayout {
                Layout.fillWidth: true
                columns: 3
                columnSpacing: tokens.spacingMedium
                rowSpacing: tokens.spacingSmall

                Item {
                    Layout.preferredWidth: 34
                    Layout.minimumWidth: 34
                    Layout.maximumWidth: 34
                }
                UiLabel {
                    tokens: root.tokens
                    kind: noteKind
                    text: qsTr("按键录入")
                }
                UiLabel {
                    tokens: root.tokens
                    kind: noteKind
                    Layout.preferredWidth: 126
                    Layout.minimumWidth: 126
                    Layout.maximumWidth: 126
                    text: qsTr("备注名称")
                }

                UiLabel {
                    id: primaryGestureTitle
                    objectName: "actionEditorPrimaryTitle"
                    tokens: root.tokens
                    kind: bodyKind
                    text: qsTr("单击")
                    font.pixelSize: tokens.fontSizeControl
                    font.weight: Font.Medium
                    Layout.fillHeight: true
                    verticalAlignment: Text.AlignVCenter
                    HoverHandler { id: primaryGestureTitleHover }
                    CompactToolTip {
                        objectName: "actionEditorPrimaryHelp"
                        tokens: root.tokens
                        active: primaryGestureTitleHover.hovered
                        text: actionEditor.buttonId === "mic"
                            ? qsTr("话筒键可选按住说话、普通动作、快捷键或 Quicker URI")
                            : qsTr("可选普通动作、快捷键或 Quicker URI")
                    }
                }
                RowLayout {
                    Layout.fillWidth: true
                    spacing: tokens.spacingSmall

                    EditorActionCombo {
                    id: primaryCombo
                    objectName: "actionEditorPrimaryCombo"
                        tokens: root.tokens
                    Layout.fillWidth: true
                    model: SettingsController.primaryActionOptionsFor(
                        actionEditor.buttonId
                    )
                    Accessible.name: actionEditor.buttonName + qsTr("单击动作")
                    onEditTextChanged: {
                            if (!actionEditor.syncing)
                            actionEditor.primaryText = editText
                    }
                    onActivated: {
                        const selectedText = currentText
                        actionEditor.syncing = true
                        editText = selectedText
                        actionEditor.primaryText = selectedText
                        actionEditor.syncing = false
                    }
                }
                    CompactButton {
                    objectName: "actionEditorPrimaryRecordButton"
                        tokens: root.tokens
                        compactMinimumWidth: tokens.buttonWidth2Chars
                    text: qsTr("录入")
                    onClicked: root.openShortcutRecorder(
                        actionEditor.buttonId, actionEditor.rowIndex,
                        "single_click", ""
                    )
                    Accessible.name: qsTr("录制单击快捷键")
                }
                }
                CompactTextField {
                    id: primaryNoteField
                    objectName: "actionEditorPrimaryNoteField"
                    tokens: root.tokens
                    Layout.preferredWidth: 126
                    Layout.minimumWidth: 126
                    Layout.maximumWidth: 126
                    placeholderText: qsTr("如：复制")
                    onTextChanged: {
                        if (!actionEditor.syncing)
                            actionEditor.primaryNote = text
                    }
                    Accessible.name: qsTr("单击备注名称")
                }

                UiLabel {
                    id: doubleGestureTitle
                    objectName: "actionEditorDoubleTitle"
                    tokens: root.tokens
                    kind: bodyKind
                    text: qsTr("双击")
                    font.pixelSize: tokens.fontSizeControl
                    font.weight: Font.Medium
                    Layout.fillHeight: true
                    verticalAlignment: Text.AlignVCenter
                    HoverHandler { id: doubleGestureTitleHover }
                    CompactToolTip {
                        tokens: root.tokens
                        active: doubleGestureTitleHover.hovered
                        text: qsTr("会等待约 0.3 秒区分单击和双击；设置双击或长按后，此键不再支持按住连发")
                    }
                }
                RowLayout {
                    Layout.fillWidth: true
                    spacing: tokens.spacingSmall

                    EditorActionCombo {
                    id: doubleCombo
                    objectName: "actionEditorDoubleCombo"
                        tokens: root.tokens
                    Layout.fillWidth: true
                    enabled: !actionEditor.primaryIsVoice
                    model: SettingsController.secondaryActionOptions
                    Accessible.name: actionEditor.buttonName + qsTr("双击动作")
                    onEditTextChanged: {
                            if (!actionEditor.syncing)
                            actionEditor.doubleText = editText
                    }
                    onActivated: {
                        const selectedText = currentText
                        actionEditor.syncing = true
                        editText = selectedText
                        actionEditor.doubleText = selectedText
                        actionEditor.syncing = false
                    }
                }
                    CompactButton {
                    objectName: "actionEditorDoubleRecordButton"
                        tokens: root.tokens
                        compactMinimumWidth: tokens.buttonWidth2Chars
                    enabled: !actionEditor.primaryIsVoice
                    text: qsTr("录入")
                    onClicked: root.openShortcutRecorder(
                        actionEditor.buttonId, actionEditor.rowIndex,
                        "double_click", ""
                    )
                    Accessible.name: qsTr("录制双击快捷键")
                }
                }
                CompactTextField {
                    id: doubleNoteField
                    objectName: "actionEditorDoubleNoteField"
                    tokens: root.tokens
                    Layout.preferredWidth: 126
                    Layout.minimumWidth: 126
                    Layout.maximumWidth: 126
                    enabled: !actionEditor.primaryIsVoice
                    placeholderText: qsTr("如：复制")
                    onTextChanged: {
                        if (!actionEditor.syncing)
                            actionEditor.doubleNote = text
                    }
                    Accessible.name: qsTr("双击备注名称")
                }

                UiLabel {
                    id: longGestureTitle
                    objectName: "actionEditorLongTitle"
                    tokens: root.tokens
                    kind: bodyKind
                    text: qsTr("长按")
                    font.pixelSize: tokens.fontSizeControl
                    font.weight: Font.Medium
                    Layout.fillHeight: true
                    verticalAlignment: Text.AlignVCenter
                    HoverHandler { id: longGestureTitleHover }
                    CompactToolTip {
                        tokens: root.tokens
                        active: longGestureTitleHover.hovered
                        text: qsTr("按住约 0.55 秒触发；设置双击或长按后，此键不再支持按住连发")
                    }
                }
                RowLayout {
                    Layout.fillWidth: true
                    spacing: tokens.spacingSmall

                    EditorActionCombo {
                    id: longCombo
                    objectName: "actionEditorLongCombo"
                        tokens: root.tokens
                    Layout.fillWidth: true
                    enabled: !actionEditor.primaryIsVoice
                    model: SettingsController.secondaryActionOptions
                    Accessible.name: actionEditor.buttonName + qsTr("长按动作")
                    onEditTextChanged: {
                            if (!actionEditor.syncing)
                            actionEditor.longText = editText
                    }
                    onActivated: {
                        const selectedText = currentText
                        actionEditor.syncing = true
                        editText = selectedText
                        actionEditor.longText = selectedText
                        actionEditor.syncing = false
                    }
                }
                    CompactButton {
                    objectName: "actionEditorLongRecordButton"
                        tokens: root.tokens
                        compactMinimumWidth: tokens.buttonWidth2Chars
                    enabled: !actionEditor.primaryIsVoice
                    text: qsTr("录入")
                    onClicked: root.openShortcutRecorder(
                        actionEditor.buttonId, actionEditor.rowIndex,
                        "long_press", ""
                    )
                    Accessible.name: qsTr("录制长按快捷键")
                }
                }
                CompactTextField {
                    id: longNoteField
                    objectName: "actionEditorLongNoteField"
                    tokens: root.tokens
                    Layout.preferredWidth: 126
                    Layout.minimumWidth: 126
                    Layout.maximumWidth: 126
                    enabled: !actionEditor.primaryIsVoice
                    placeholderText: qsTr("如：复制")
                    onTextChanged: {
                        if (!actionEditor.syncing)
                            actionEditor.longNote = text
                    }
                    Accessible.name: qsTr("长按备注名称")
                }
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: tokens.spacingSmall
                Item { Layout.fillWidth: true }
                CompactButton {
                    objectName: "actionEditorCancelButton"
                    tokens: root.tokens
                    compactMinimumWidth: tokens.buttonWidth2Chars
                    text: qsTr("取消")
                    onClicked: actionEditor.close()
                }
                CompactButton {
                    objectName: "actionEditorSaveButton"
                    tokens: root.tokens
                    compactMinimumWidth: tokens.buttonWidth2Chars
                    text: qsTr("完成")
                    highlighted: true
                    onClicked: actionEditor.saveDraft()
                }
            }
        }
    }

    Item {
        id: rc003MappingLayout
        objectName: "rc003MappingLayout"
        anchors.fill: parent

        ColumnLayout {
            anchors.fill: parent
            anchors.margins: tokens.pageHorizontalPadding
            spacing: tokens.spacingSmall

            SectionFrame {
                id: mappingViewSwitcher
                objectName: "mappingViewSwitcher"
                tokens: root.tokens
                Layout.fillWidth: true
                Layout.preferredHeight: 36
                horizontalPadding: 3
                verticalPadding: 3
                radius: tokens.cornerRadiusSmall

                RowLayout {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    spacing: 3

                    CompactButton {
                        id: singleMappingViewButton
                        objectName: "singleMappingViewButton"
                        tokens: root.tokens
                        Layout.fillWidth: true
                        Layout.preferredWidth: 1
                        text: qsTr("单键映射")
                        highlighted: root.mappingViewIndex === 0
                        onClicked: root.mappingViewIndex = 0
                        KeyNavigation.backtab: root.backTabTarget
                    }
                    CompactButton {
                        id: comboMappingViewButton
                        objectName: "comboMappingViewButton"
                        tokens: root.tokens
                        Layout.fillWidth: true
                        Layout.preferredWidth: 1
                        text: qsTr("组合按键映射")
                        highlighted: root.mappingViewIndex === 1
                        onClicked: root.mappingViewIndex = 1
                    }
                }
            }

            Item {
                id: mappingList
                objectName: "mappingList"
                visible: root.mappingViewIndex === 0
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.minimumHeight: 306
                onWidthChanged: root.scheduleConnectorRepaint()
                onHeightChanged: root.scheduleConnectorRepaint()
                property int count: 13
                property int currentIndex: ButtonMappingModel.indexOfButton(
                    SettingsController.selectedButtonId
                )

                Canvas {
                    id: mappingLines
                    objectName: "mappingLines"
                    anchors.fill: parent
                    z: 0
                    antialiasing: true

                    onPaint: root.paintConnectors(mappingLines, false)

                    onWidthChanged: root.scheduleConnectorRepaint()
                    onHeightChanged: root.scheduleConnectorRepaint()

                    Connections {
                        target: SettingsController
                        function onSelectedButtonIdChanged() {
                            root.scheduleConnectorRepaint()
                        }
                    }
                }

                Canvas {
                    id: activeMappingLine
                    objectName: "activeMappingLine"
                    anchors.fill: parent
                    z: 2
                    antialiasing: true
                    onPaint: root.paintConnectors(activeMappingLine, true)
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
                    width: (parent.width - photoSidebar.width - root.mappingBoardGap * 2) / 2
                    columns: 1
                    rows: 6
                    rowSpacing: root.mappingCardGap
                    z: 3
                    onXChanged: root.scheduleConnectorRepaint()
                    onYChanged: root.scheduleConnectorRepaint()
                    onWidthChanged: root.scheduleConnectorRepaint()
                    onHeightChanged: root.scheduleConnectorRepaint()

                    Repeater {
                        id: leftCardRepeater
                        model: ButtonMappingModel
                        onItemAdded: root.scheduleConnectorRepaint()
                        delegate: MappingCard {
                            required property int index
                            required property string buttonId
                            required property string displayName
                            required property string actionText
                            required property string doubleClickText
                            required property string longPressText
                            required property string singleNote
                            required property string doubleNote
                            required property string longNote
                            required property bool isSelected

                            visible: root.isLeftButton(buttonId)
                            exposeObjectNames: visible
                            tokens: root.tokens
                            cardId: buttonId
                            Layout.row: root.visualRow(buttonId)
                            Layout.fillWidth: true
                            Layout.preferredHeight: visible ? implicitHeight : 0
                            buttonName: root.shortButtonName(buttonId)
                            singleText: actionText
                            doubleText: doubleClickText
                            longText: longPressText
                            singleNoteText: singleNote
                            doubleNoteText: doubleNote
                            longNoteText: longNote
                            selected: isSelected
                            voiceAction: actionText.trim() === "按住说话"
                                || actionText.indexOf("已停用：旧语音配置") === 0
                            onXChanged: root.scheduleConnectorRepaint()
                            onYChanged: root.scheduleConnectorRepaint()
                            onWidthChanged: root.scheduleConnectorRepaint()
                            onHeightChanged: root.scheduleConnectorRepaint()
                            onVisibleChanged: root.scheduleConnectorRepaint()
                            Component.onCompleted: root.scheduleConnectorRepaint()
                            onClicked: {
                                SettingsController.selectButton(buttonId)
                                actionEditor.openForRow(
                                    index, buttonId, root.shortButtonName(buttonId), actionText,
                                    doubleClickText, longPressText,
                                    singleNote, doubleNote, longNote
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
                            onXChanged: root.scheduleConnectorRepaint()
                            onYChanged: root.scheduleConnectorRepaint()
                            onWidthChanged: root.scheduleConnectorRepaint()
                            onHeightChanged: root.scheduleConnectorRepaint()
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
                                onXChanged: root.scheduleConnectorRepaint()
                                onYChanged: root.scheduleConnectorRepaint()
                                onWidthChanged: root.scheduleConnectorRepaint()
                                onHeightChanged: root.scheduleConnectorRepaint()
                                onVisibleChanged: root.scheduleConnectorRepaint()
                                Component.onCompleted: root.scheduleConnectorRepaint()

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
                    width: (parent.width - photoSidebar.width - root.mappingBoardGap * 2) / 2
                    columns: 1
                    rows: 7
                    rowSpacing: root.mappingCardGap
                    z: 3
                    onXChanged: root.scheduleConnectorRepaint()
                    onYChanged: root.scheduleConnectorRepaint()
                    onWidthChanged: root.scheduleConnectorRepaint()
                    onHeightChanged: root.scheduleConnectorRepaint()

                    Repeater {
                        id: rightCardRepeater
                        model: ButtonMappingModel
                        onItemAdded: root.scheduleConnectorRepaint()
                        delegate: MappingCard {
                            required property int index
                            required property string buttonId
                            required property string displayName
                            required property string actionText
                            required property string doubleClickText
                            required property string longPressText
                            required property string singleNote
                            required property string doubleNote
                            required property string longNote
                            required property bool isSelected

                            visible: !root.isLeftButton(buttonId)
                            exposeObjectNames: visible
                            tokens: root.tokens
                            cardId: buttonId
                            Layout.row: root.visualRow(buttonId)
                            Layout.fillWidth: true
                            Layout.preferredHeight: visible ? implicitHeight : 0
                            buttonName: root.shortButtonName(buttonId)
                            singleText: actionText
                            doubleText: doubleClickText
                            longText: longPressText
                            singleNoteText: singleNote
                            doubleNoteText: doubleNote
                            longNoteText: longNote
                            selected: isSelected
                            voiceAction: actionText.trim() === "按住说话"
                                || actionText.indexOf("已停用：旧语音配置") === 0
                            onXChanged: root.scheduleConnectorRepaint()
                            onYChanged: root.scheduleConnectorRepaint()
                            onWidthChanged: root.scheduleConnectorRepaint()
                            onHeightChanged: root.scheduleConnectorRepaint()
                            onVisibleChanged: root.scheduleConnectorRepaint()
                            Component.onCompleted: root.scheduleConnectorRepaint()
                            onClicked: {
                                SettingsController.selectButton(buttonId)
                                actionEditor.openForRow(
                                    index, buttonId, root.shortButtonName(buttonId), actionText,
                                    doubleClickText, longPressText,
                                    singleNote, doubleNote, longNote
                                )
                            }
                        }
                    }
                }
            }

            Item {
                id: comboMappingList
                objectName: "comboMappingList"
                visible: root.mappingViewIndex === 1
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.minimumHeight: 306

                ColumnLayout {
                    anchors.fill: parent
                    spacing: tokens.spacingSmall

                    SectionFrame {
                        id: comboModifierPanel
                        objectName: "comboModifierPanel"
                        tokens: root.tokens
                        Layout.fillWidth: true
                        Layout.preferredHeight: 38
                        horizontalPadding: tokens.spacingMedium
                        verticalPadding: tokens.spacingSmall
                        radius: tokens.cornerRadiusSmall

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: tokens.spacingMedium

                            UiLabel {
                                tokens: root.tokens
                                kind: bodyKind
                                text: qsTr("组合主键")
                                font.weight: Font.Medium
                            }
                            SelectionComboBox {
                                id: comboModifierCombo
                                objectName: "comboModifierCombo"
                                tokens: root.tokens
                                Layout.preferredWidth: 112
                                model: SettingsController.comboModifierOptions
                                currentIndex: SettingsController.comboModifierIndex
                                onActivated: SettingsController.comboModifierIndex = index
                                Accessible.name: qsTr("遥控器组合主键")
                            }
                            UiLabel {
                                objectName: "comboModifierRestrictionText"
                                tokens: root.tokens
                                kind: noteKind
                                Layout.fillWidth: true
                                text: qsTr("主键限 TV、菜单或主页；启用后不能设置该键的双击和长按")
                                elide: Text.ElideRight
                            }
                        }
                    }

                    Rectangle {
                        objectName: "comboMappingHeader"
                        Layout.fillWidth: true
                        Layout.preferredHeight: tokens.comboMappingHeaderHeight
                        radius: tokens.cornerRadiusControl
                        color: tokens.surfaceMuted
                        border.color: tokens.border
                        border.width: tokens.hairlineWidth

                        RowLayout {
                            anchors.fill: parent
                            anchors.leftMargin: tokens.comboMappingHorizontalPadding
                            anchors.rightMargin: tokens.comboMappingHorizontalPadding
                            spacing: tokens.comboMappingColumnSpacing

                            UiLabel {
                                tokens: root.tokens
                                kind: noteKind
                                Layout.preferredWidth: tokens.comboMappingKeyColumnWidth
                                text: qsTr("遥控器按键")
                            }
                            UiLabel {
                                tokens: root.tokens
                                kind: noteKind
                                Layout.fillWidth: true
                                text: qsTr("执行动作")
                            }
                            Item { Layout.preferredWidth: tokens.buttonWidth2Chars }
                            UiLabel {
                                tokens: root.tokens
                                kind: noteKind
                                Layout.preferredWidth: tokens.comboMappingNoteColumnWidth
                                text: qsTr("备注名称")
                            }
                        }
                    }

                    ColumnLayout {
                        id: comboMappingRows
                        objectName: "comboMappingRows"
                        Layout.fillWidth: true
                        Layout.fillHeight: false
                        spacing: tokens.comboMappingRowSpacing

                        Repeater {
                            model: SettingsController.comboRows

                            delegate: Rectangle {
                                required property int index
                                required property string buttonId
                                required property string buttonName
                                required property string actionText
                                required property string noteText

                                Layout.fillWidth: true
                                Layout.preferredHeight: tokens.comboMappingRowHeight
                                radius: tokens.cornerRadiusControl
                                color: index % 2 === 0 ? tokens.surface : tokens.fieldBackground
                                border.color: tokens.border
                                border.width: tokens.hairlineWidth
                                objectName: "comboMappingRow_" + buttonId

                                RowLayout {
                                    anchors.fill: parent
                                    anchors.leftMargin: tokens.comboMappingHorizontalPadding
                                    anchors.rightMargin: tokens.comboMappingHorizontalPadding
                                    spacing: tokens.comboMappingColumnSpacing

                                    MappingKeyLabel {
                                        id: comboGestureTitle
                                        objectName: "comboMappingTitle_" + buttonId
                                        tokens: root.tokens
                                        Layout.preferredWidth: tokens.comboMappingKeyColumnWidth
                                        Layout.fillHeight: true
                                        text: SettingsController.comboModifierText
                                            + " + " + root.shortButtonName(buttonId)
                                        verticalAlignment: Text.AlignVCenter
                                        HoverHandler { id: comboGestureTitleHover }
                                        CompactToolTip {
                                            tokens: root.tokens
                                            active: comboGestureTitleHover.hovered
                                            text: qsTr("可选普通动作、快捷键或 Quicker URI")
                                        }
                                    }
                                    EditorActionCombo {
                                        id: comboActionEditor
                                        objectName: "comboActionEditor_" + buttonId
                                        property bool acceptsUserEdits: false
                                        tokens: root.tokens
                                        Layout.fillWidth: true
                                        model: SettingsController.secondaryActionOptions
                                        Component.onCompleted: {
                                            editText = actionText
                                            acceptsUserEdits = true
                                        }
                                        onEditTextChanged: {
                                            if (acceptsUserEdits) {
                                                SettingsController.setComboActionText(
                                                    buttonId, editText
                                                )
                                            }
                                        }
                                        onActivated:
                                            editText = currentText
                                        Accessible.name: SettingsController.comboModifierText
                                            + " + " + buttonName + qsTr("执行动作")
                                    }
                                    CompactButton {
                                        objectName: "comboRecordButton_" + buttonId
                                        tokens: root.tokens
                                        compactMinimumWidth: tokens.buttonWidth2Chars
                                        text: qsTr("录入")
                                        onClicked: root.openShortcutRecorder(
                                            buttonId, -1, "combo", comboActionEditor
                                        )
                                        Accessible.name: SettingsController.comboModifierText
                                            + " + " + buttonName + qsTr("录制电脑快捷键")
                                    }
                                    CompactTextField {
                                        id: comboNoteEditor
                                        objectName: "comboNoteEditor_" + buttonId
                                        tokens: root.tokens
                                        Layout.preferredWidth: tokens.comboMappingNoteColumnWidth
                                        text: noteText
                                        placeholderText: qsTr("如：置顶窗口")
                                        onTextChanged: SettingsController.setComboNoteText(
                                            buttonId, text
                                        )
                                        Accessible.name: SettingsController.comboModifierText
                                            + " + " + buttonName + qsTr("备注名称")
                                    }
                                }
                            }
                        }
                    }

                    Item { Layout.fillHeight: true }
                }
            }

            SectionFrame {
                id: mappingActionsPanel
                objectName: "mappingActionsPanel"
                tokens: root.tokens
                Layout.fillWidth: true
                Layout.preferredHeight: 36
                horizontalPadding: 4
                verticalPadding: 3
                radius: tokens.cornerRadiusSmall

                RowLayout {
                    Layout.fillWidth: true
                    spacing: 4

                    CompactButton {
                        id: detectRealKeyButton
                        objectName: "detectRealKeyButton"
                        visible: root.mappingViewIndex === 0
                        tokens: root.tokens
                        compactMinimumWidth: tokens.buttonWidth6Chars
                        text: SettingsController.keyDetectionActive
                            ? qsTr("停止检测") : qsTr("检测真实按键")
                        highlighted: SettingsController.keyDetectionActive
                        onClicked: SettingsController.keyDetectionActive
                            ? SettingsController.stopKeyDetection()
                            : SettingsController.startKeyDetection()
                        Accessible.name: qsTr("检测真实遥控器按键")
                    }
                    UiLabel {
                        objectName: "voiceGestureRestrictionText"
                        tokens: root.tokens
                        kind: noteKind
                        Layout.fillWidth: true
                        text: root.mappingViewIndex === 0
                            ? (SettingsController.keyDetectionActive
                                ? SettingsController.keyDetectionText
                                : qsTr("设为语音动作后，双击和长按不可用"))
                            : qsTr("组合触发后，不执行两个按键的单键动作")
                        elide: Text.ElideRight
                    }
                    Item { Layout.fillWidth: true }
                    CompactButton {
                        objectName: "restoreMappingDefaultsButton"
                        tokens: root.tokens
                        compactMinimumWidth: tokens.buttonWidth6Chars
                        text: qsTr("恢复内置默认")
                        onClicked: restoreMappingDefaultsDialog.open()
                    }
                    CompactButton {
                        id: saveMappingButton
                        objectName: "saveMappingButton"
                        tokens: root.tokens
                        compactMinimumWidth: tokens.buttonWidth4Chars
                        text: qsTr("保存映射")
                        highlighted: true
                        enabled: !SettingsController.voiceHotkeyBusy
                            && !SettingsController.bridgeLaunchBusy
                            && !SettingsController.endpointPreflightBusy
                            && !DiagnosticsController.driverActionRunning
                            && !DiagnosticsController.vbCableTestRunning
                        KeyNavigation.tab: root.tabTarget
                        onClicked: {
                            SettingsController.saveSettings()
                            root.scheduleConnectorRepaint()
                        }
                    }
                }
            }

        }

    }

}
