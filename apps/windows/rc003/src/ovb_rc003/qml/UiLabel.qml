import QtQuick
import QtQuick.Controls

Label {
    id: root

    property var tokens
    readonly property int bodyKind: 0
    readonly property int noteKind: 1
    readonly property int sectionTitleKind: 2
    readonly property int pageTitleKind: 3
    property int kind: bodyKind

    color: kind === noteKind ? tokens.disabledText : tokens.textPrimary
    font.family: tokens.fontFamily
    font.pixelSize: kind === pageTitleKind || kind === sectionTitleKind
        ? tokens.fontSizeSection
        : kind === noteKind
            ? tokens.fontSizeSmall
            : tokens.fontSizeBody
    font.weight: kind === pageTitleKind || kind === sectionTitleKind
        ? Font.Medium : Font.Normal
    lineHeightMode: Text.ProportionalHeight
    lineHeight: kind === noteKind ? 1.32 : 1.2
}
