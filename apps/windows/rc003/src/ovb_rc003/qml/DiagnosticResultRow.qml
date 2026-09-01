import QtQuick
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
        id: titleLabel
        tokens: root.tokens
        kind: noteKind
        Layout.preferredWidth: root.titleColumnWidth
        Layout.minimumWidth: root.titleColumnWidth
        Layout.maximumWidth: root.titleColumnWidth
        text: root.titleText
        color: root.tokens.textPrimary
        elide: Text.ElideRight
        HoverHandler { id: titleHover }
        CompactToolTip {
            tokens: root.tokens
            active: titleHover.hovered && titleLabel.truncated
            text: root.titleText
        }
    }

    UiLabel {
        id: detailLabel
        tokens: root.tokens
        kind: noteKind
        Layout.fillWidth: true
        maximumLineCount: 1
        elide: Text.ElideRight
        wrapMode: Text.NoWrap
        text: root.statusText + "：" + root.detailText
        HoverHandler { id: detailHover }
        CompactToolTip {
            tokens: root.tokens
            active: detailHover.hovered && detailLabel.truncated
            text: detailLabel.text
        }
    }
}
