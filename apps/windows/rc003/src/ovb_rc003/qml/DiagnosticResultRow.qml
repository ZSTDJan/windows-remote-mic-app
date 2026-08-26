import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

RowLayout {
    id: root

    property var tokens
    property color indicatorColor: tokens.textSecondary
    property string titleText: ""
    property string statusText: ""
    property string detailText: ""
    property int titleColumnWidth: 160

    Layout.fillWidth: true
    spacing: tokens.spacingSmall

    Rectangle {
        Layout.preferredWidth: 8
        Layout.preferredHeight: 8
        radius: 4
        color: root.indicatorColor
    }

    UiLabel {
        tokens: root.tokens
        kind: noteKind
        Layout.preferredWidth: root.titleColumnWidth
        Layout.minimumWidth: root.titleColumnWidth
        Layout.maximumWidth: root.titleColumnWidth
        text: root.titleText
        color: root.tokens.textPrimary
        elide: Text.ElideRight
    }

    UiLabel {
        tokens: root.tokens
        kind: noteKind
        Layout.fillWidth: true
        maximumLineCount: 1
        elide: Text.ElideRight
        wrapMode: Text.NoWrap
        text: root.statusText + "：" + root.detailText
        HoverHandler { id: detailHover }
        ToolTip.visible: detailHover.hovered
        ToolTip.text: text
    }
}
