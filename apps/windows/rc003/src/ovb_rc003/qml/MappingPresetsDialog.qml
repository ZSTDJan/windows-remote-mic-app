import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs
import OvbRc003Settings 1.0

SettingsDialog {
    id: root
    objectName: "mappingPresetsDialog"
    title: qsTr("管理按键配置")
    preferredWidth: 520
    closeButtonObjectName: "mappingPresetsCloseButton"
    dismissalEnabled: !nameDialog.visible && !confirmDialog.visible
        && !importDialog.visible && !exportDialog.visible
    property string selectedId: ""
    property string message: ""
    readonly property var rows: SettingsController.mappingPresets
    readonly property int selectedIndex: {
        for (let i = 0; i < rows.length; ++i)
            if (rows[i].id === selectedId) return i
        return -1
    }
    readonly property string selectedName: selectedIndex >= 0 ? rows[selectedIndex].name : ""
    readonly property bool selectedAutomatic: selectedIndex >= 0 && rows[selectedIndex].automatic
    readonly property int appliedIndex: {
        for (let i = 0; i < rows.length; ++i)
            if (rows[i].applied) return i
        return -1
    }
    function syncSelection() { presets.currentIndex = selectedIndex }
    function run(operation, identity, value) {
        const result = SettingsController.mappingPresetAction(operation, identity, value)
        message = result.message
        if (result.ok && operation !== "export") selectedId = result.id
        Qt.callLater(syncSelection)
        return result.ok
    }
    function askName(operation) {
        nameDialog.operation = operation
        nameDialog.targetId = selectedId
        nameDialog.errorText = ""
        nameField.text = operation === "rename" ? selectedName : selectedName.slice(0, 61) + qsTr(" 副本")
        nameDialog.open()
        nameField.forceActiveFocus()
        nameField.selectAll()
    }
    function confirm(operation) {
        confirmDialog.operation = operation
        confirmDialog.targetId = selectedId
        confirmDialog.targetName = selectedName
        confirmDialog.open()
    }
    onAboutToShow: {
        selectedId = ""
        run("refresh", "", "")
    }
    onSelectedIdChanged: Qt.callLater(syncSelection)

    contentItem: ColumnLayout {
        spacing: root.tokens.spacingLarge
        RowLayout {
            Layout.fillWidth: true
            spacing: root.tokens.spacingSmall
            UiLabel { tokens: root.tokens; kind: bodyKind; text: qsTr("配置列表") }
            SelectionComboBox {
                id: presets
                objectName: "mappingPresetCombo"
                tokens: root.tokens
                Layout.fillWidth: true
                Layout.minimumWidth: 0
                model: root.rows
                textRole: "name"
                effectiveIndex: root.appliedIndex
                showEffectiveMarker: true
                currentIndex: -1
                displayText: root.selectedIndex >= 0 ? root.selectedName
                    : qsTr("请选择配置")
                enabled: count > 0
                onActivated: index => root.selectedId = model[index].id
                onModelChanged: Qt.callLater(root.syncSelection)
                onCountChanged: Qt.callLater(root.syncSelection)
            }
            CompactButton {
                objectName: "applyMappingPresetButton"
                tokens: root.tokens; text: qsTr("应用配置"); highlighted: true
                enabled: root.selectedIndex >= 0 && !root.selectedAutomatic
                onClicked: root.confirm("apply")
            }
        }
        RowLayout {
            Layout.fillWidth: true
            spacing: root.tokens.spacingSmall
            CompactButton {
                objectName: "copyMappingPresetButton"
                tokens: root.tokens; text: qsTr("复制配置")
                enabled: root.selectedIndex >= 0
                onClicked: root.askName("copy")
            }
            CompactButton {
                objectName: "renameMappingPresetButton"
                tokens: root.tokens; text: qsTr("重命名")
                enabled: root.selectedIndex >= 0 && !root.selectedAutomatic
                onClicked: root.askName("rename")
            }
            CompactButton {
                objectName: "deleteMappingPresetButton"
                tokens: root.tokens; text: qsTr("删除")
                enabled: root.selectedIndex >= 0 && !root.selectedAutomatic
                onClicked: root.confirm("delete")
            }
            Item { Layout.fillWidth: true }
            CompactButton {
                objectName: "exportMappingPresetButton"
                tokens: root.tokens; text: qsTr("导出")
                enabled: root.selectedIndex >= 0
                onClicked: {
                    exportDialog.targetId = root.selectedId
                    exportDialog.open()
                }
            }
            CompactButton {
                objectName: "importMappingPresetButton"
                tokens: root.tokens; text: qsTr("导入")
                onClicked: importDialog.open()
            }
        }
        ColumnLayout {
            Layout.fillWidth: true
            spacing: root.tokens.spacingSmall
            UiLabel {
                objectName: "mappingPresetMessage"
                tokens: root.tokens; kind: noteKind
                Layout.fillWidth: true
                textFormat: Text.PlainText
                text: root.message
                visible: text.length > 0
                wrapMode: Text.WrapAnywhere
            }
            UiLabel {
                objectName: "mappingPresetHelp"
                tokens: root.tokens; kind: noteKind
                Layout.fillWidth: true
                text: qsTr("按键自动保存；复制可保留副本，导出可备份。仅含当前型号的按键和备注。")
                wrapMode: Text.WordWrap
            }
        }
    }

    SettingsDialog {
        id: nameDialog
        objectName: "mappingPresetNameDialog"
        tokens: root.tokens
        property string operation: "copy"
        property string targetId: ""
        property string errorText: ""
        title: operation === "rename" ? qsTr("重命名配置") : qsTr("复制配置")
        standardButtons: Dialog.Ok | Dialog.Cancel
        contentItem: ColumnLayout {
            spacing: root.tokens.spacingSmall
            CompactTextField {
                id: nameField
                objectName: "mappingPresetNameField"
                tokens: root.tokens
                Layout.fillWidth: true
                maximumLength: 64
                placeholderText: qsTr("输入配置名称")
                onAccepted: nameDialog.accept()
            }
            UiLabel {
                tokens: root.tokens; kind: noteKind
                Layout.fillWidth: true
                visible: text.length > 0
                text: nameDialog.errorText
                textFormat: Text.PlainText
                wrapMode: Text.WrapAnywhere
            }
        }
        onAccepted: {
            if (!root.run(operation, targetId, nameField.text)) {
                errorText = root.message
                Qt.callLater(function() { nameDialog.open(); nameField.forceActiveFocus() })
            }
        }
    }
    SettingsDialog {
        id: confirmDialog
        objectName: "mappingPresetConfirmDialog"
        tokens: root.tokens
        property string operation: "apply"
        property string targetId: ""
        property string targetName: ""
        title: operation === "delete" ? qsTr("删除配置？") : qsTr("应用配置？")
        standardButtons: Dialog.Ok | Dialog.Cancel
        contentItem: UiLabel {
            tokens: root.tokens; kind: bodyKind
            textFormat: Text.PlainText
            text: confirmDialog.operation === "delete"
                ? qsTr("删除“%1”？当前按键设置不受影响。").arg(confirmDialog.targetName)
                : qsTr("用“%1”替换当前按键动作和备注？如需保留当前设置，请先取消，再复制自动保存项。").arg(confirmDialog.targetName)
            wrapMode: Text.WrapAnywhere
        }
        onAccepted: root.run(operation, targetId, "")
    }
    FileDialog {
        id: importDialog
        title: qsTr("导入按键配置")
        fileMode: FileDialog.OpenFile
        nameFilters: [qsTr("按键配置 (*.json)")]
        onAccepted: root.run("import", "", selectedFile.toString())
    }
    FileDialog {
        id: exportDialog
        property string targetId: ""
        title: qsTr("导出按键配置")
        fileMode: FileDialog.SaveFile
        nameFilters: [qsTr("按键配置 (*.json)")]
        defaultSuffix: "json"
        onAccepted: root.run("export", targetId, selectedFile.toString())
    }
}
