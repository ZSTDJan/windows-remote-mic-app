import QtQuick

UiLabel {
    id: root

    font.pixelSize: root.tokens.fontSizeMappingKey
    font.weight: Font.Medium
    elide: Text.ElideRight
}
