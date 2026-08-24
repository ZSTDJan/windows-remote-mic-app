import QtQuick
import QtQuick.Controls

ComboBox {
    id: root

    property var tokens
    property int recommendedIndex: -1

    function decoratedText(index, rawText) {
        const value = rawText === undefined || rawText === null
            ? "" : String(rawText)
        return recommendedIndex >= 0 && index >= 0 && index === recommendedIndex
            ? qsTr("（推荐）") + " " + value
            : value
    }

    editable: false
    displayText: decoratedText(currentIndex, currentText)

    delegate: ItemDelegate {
        objectName: root.objectName + "_option_" + index
        width: ListView.view ? ListView.view.width : root.width
        text: root.decoratedText(index, modelData)
        highlighted: root.highlightedIndex === index
        font.pixelSize: root.tokens
            ? root.tokens.fontSizeBody : 13
        font.bold: index === root.currentIndex
        Accessible.name: text
    }
}
