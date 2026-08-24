import QtQuick.Controls

Label {
    id: root

    property var tokens

    readonly property int bodyKind: 0
    readonly property int noteKind: 1
    readonly property int sectionTitleKind: 2
    readonly property int pageTitleKind: 3
    property int kind: bodyKind

    color: kind === noteKind ? tokens.textSecondary : tokens.textPrimary
    font.pixelSize: kind === pageTitleKind
        ? tokens.fontSizeTitle
        : kind === noteKind
            ? tokens.fontSizeSmall
            : tokens.fontSizeBody
    font.bold: kind === pageTitleKind || kind === sectionTitleKind
}
