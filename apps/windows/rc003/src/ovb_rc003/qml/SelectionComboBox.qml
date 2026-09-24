import QtQuick
import QtQuick.Controls

ComboBox {
    id: root

    property var tokens
    property int recommendedIndex: -1
    // Immediate-selection consumers keep currentIndex; confirmation dialogs
    // can distinguish the effective choice from a merely browsed option.
    property int effectiveIndex: currentIndex
    property bool showEffectiveMarker: false

    function decoratedText(index, rawText) {
        const value = rawText === undefined || rawText === null
            ? "" : String(rawText)
        return recommendedIndex >= 0 && index >= 0 && index === recommendedIndex
            ? qsTr("（推荐）") + " " + value
            : value
    }

    editable: false
    implicitHeight: tokens ? tokens.controlHeight : 26
    leftPadding: 7
    rightPadding: 24
    displayText: decoratedText(currentIndex, currentText)
    font.family: tokens ? tokens.fontFamily : "Microsoft YaHei UI"
    font.pixelSize: tokens ? tokens.fontSizeControl : 12
    font.weight: Font.Medium

    contentItem: Label {
        text: root.displayText
        textFormat: Text.PlainText
        color: root.enabled ? root.tokens.textPrimary : root.tokens.disabledText
        font: root.font
        verticalAlignment: Text.AlignVCenter
        elide: Text.ElideRight
    }

    indicator: DropDownIndicator {
        x: root.width - width - 7
        y: (root.height - height) / 2
        indicatorColor: root.enabled
            ? root.tokens.textSecondary : root.tokens.disabledText
    }

    background: Rectangle {
        radius: root.tokens.cornerRadiusControl
        color: root.tokens.fieldBackground
        border.width: root.tokens.hairlineWidth
        border.color: root.activeFocus ? root.tokens.accent : root.tokens.border
    }

    delegate: ItemDelegate {
        objectName: root.objectName + "_option_" + index
        width: ListView.view ? ListView.view.width : root.width
        height: 28
        leftPadding: 7 + (root.showEffectiveMarker ? 18 : 0)
        rightPadding: 7
        highlighted: root.highlightedIndex === index
        IconGlyph {
            objectName: root.objectName + "_effectiveMark_" + index
            anchors.left: parent.left
            anchors.leftMargin: 7
            anchors.verticalCenter: parent.verticalCenter
            tokens: root.tokens
            glyph: "\uE73E"
            glyphSize: 12
            visible: root.showEffectiveMarker && index === root.effectiveIndex
        }
        contentItem: Label {
            text: root.decoratedText(index, root.textAt(index))
            textFormat: Text.PlainText
            color: root.tokens.textPrimary
            font.pixelSize: root.tokens.fontSizeControl
            font.weight: index === root.effectiveIndex ? Font.DemiBold : Font.Normal
            verticalAlignment: Text.AlignVCenter
            elide: Text.ElideRight
        }
        background: Rectangle {
            color: parent.highlighted ? root.tokens.accentSoft : root.tokens.surface
        }
        Accessible.name: (root.showEffectiveMarker && index === root.effectiveIndex
            ? qsTr("当前使用：") : "") + contentItem.text
    }
}
