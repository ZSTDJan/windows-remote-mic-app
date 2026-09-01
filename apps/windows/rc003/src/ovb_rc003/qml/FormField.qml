import QtQuick
import QtQuick.Layouts

GridLayout {
    id: root

    property var tokens
    property string titleText: ""
    property string noteText: ""
    property string errorText: ""
    property string noteObjectName: ""
    property int titleColumnWidth: 64
    property int editorMaximumWidth: 600
    property int inlineMinimumWidth: 360
    readonly property bool stacked: width < inlineMinimumWidth
    default property alias editorData: editorColumn.data

    Layout.fillWidth: true
    columns: stacked ? 1 : 2
    columnSpacing: tokens.spacingSmall
    rowSpacing: tokens.spacingTiny

    UiLabel {
        tokens: root.tokens
        kind: bodyKind
        text: root.titleText
        Layout.row: 0
        Layout.column: 0
        Layout.preferredWidth: root.stacked ? -1 : root.titleColumnWidth
        Layout.minimumWidth: root.stacked ? 0 : root.titleColumnWidth
        Layout.maximumWidth: root.stacked ? Number.POSITIVE_INFINITY : root.titleColumnWidth
        Layout.fillWidth: root.stacked
        Layout.alignment: Qt.AlignLeft | Qt.AlignVCenter
    }

    ColumnLayout {
        id: editorColumn
        Layout.row: root.stacked ? 1 : 0
        Layout.column: root.stacked ? 0 : 1
        Layout.fillWidth: true
        Layout.maximumWidth: root.editorMaximumWidth
        spacing: root.tokens.spacingTiny
    }

    UiLabel {
        tokens: root.tokens
        kind: noteKind
        color: root.tokens.errorColor
        text: root.errorText
        visible: text.length > 0
        wrapMode: Text.WordWrap
        Layout.row: root.stacked ? 2 : 1
        Layout.column: root.stacked ? 0 : 1
        Layout.fillWidth: true
        Layout.maximumWidth: root.editorMaximumWidth
    }

    UiLabel {
        objectName: root.noteObjectName
        tokens: root.tokens
        kind: noteKind
        text: root.noteText
        visible: text.length > 0
        wrapMode: Text.WordWrap
        Layout.row: root.stacked
            ? (root.errorText.length > 0 ? 3 : 2)
            : (root.errorText.length > 0 ? 2 : 1)
        Layout.column: root.stacked ? 0 : 1
        Layout.fillWidth: true
        Layout.maximumWidth: root.editorMaximumWidth
    }
}
