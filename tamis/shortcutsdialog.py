"""The overview of the keyboard shortcuts (`?`) and the editor that changes them."""
from html import escape

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QKeySequence
from PyQt6.QtWidgets import (
    QDialogButtonBox,
    QGridLayout,
    QHBoxLayout,
    QKeySequenceEdit,
    QLabel,
    QPushButton,
    QScrollArea,
    QWidget,
)

from . import shortcuts
from .constants import COLOR_MUTED, COLOR_REJECT
from .widgets import MessageDialog

PLUGINS_GROUP = "Plugins"


def _scroll(widget, height):
    area = QScrollArea()
    area.setWidget(widget)
    area.setWidgetResizable(True)
    area.setFrameShape(QScrollArea.Shape.NoFrame)
    area.setMinimumHeight(height)
    return area


class ShortcutsDialog(MessageDialog):
    """Every shortcut, the app's and the plugins', grouped. Accepted, `customize` asks for the editor."""

    def __init__(self, parent, bindings, plugin_keys=None):
        super().__init__(parent, "Keyboard shortcuts", "", confirm_text="Customize…")
        self.body.hide()
        self.setMinimumWidth(560)
        self.customize = False
        for button in self.findChild(QDialogButtonBox).buttons():
            if button is not self.confirm_button:
                button.setText("Close")
        self.confirm_button.setDefault(False)
        self.confirm_button.setObjectName("")
        self.accepted.connect(lambda: setattr(self, "customize", True))

        rows = {group: [] for group in shortcuts.GROUPS}
        for command in shortcuts.COMMANDS:
            keys = bindings.get(command.id, ())
            if keys:
                rows[command.group].append((command.title, shortcuts.keys_text(keys)))
        for key, name in sorted((plugin_keys or {}).items(), key=lambda item: item[1].lower()):
            rows.setdefault(PLUGINS_GROUP, []).append((name, shortcuts.native(key)))
        self.groups = {group: entries for group, entries in rows.items() if entries}

        html = []
        for group, entries in self.groups.items():
            html.append(f"<h4 style='margin: 10px 0 4px 0; color: {COLOR_MUTED}'>{escape(group.upper())}</h4>")
            html.append("<table cellspacing='0' cellpadding='3' width='100%'>")
            for title, keys in entries:
                html.append(f"<tr><td>{escape(title)}</td><td align='right'><b>{escape(keys)}</b></td></tr>")
            html.append("</table>")
        html.append(f"<p style='color: {COLOR_MUTED}'>The Cull view's keys move through the grid in the Gallery.</p>")
        self.table = QLabel("".join(html))
        self.table.setTextFormat(Qt.TextFormat.RichText)
        self.table.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.table.setContentsMargins(0, 0, 8, 0)
        layout = self.layout()
        layout.insertWidget(layout.count() - 1, _scroll(self.table, 460))


class ShortcutsEditor(MessageDialog):
    """Change the keys of every command. Accepted, `bindings` holds them ({id: (key, …)}).

    A key may only do one thing, among the app's commands and the plugins' actions
    (`plugin_keys`, {key: name}): Save waits until no two share one.
    """

    def __init__(self, parent, bindings, plugin_keys=None):
        super().__init__(
            parent, "Keyboard shortcuts",
            f"Click a field and press the key, with ⌘, ⌥, ⌃ or ⇧ if you like; up to {shortcuts.MAX_KEYS} "
            "keys per action. Plugins' shortcuts can't be changed here.",
            confirm_text="Save",
        )
        self.setMinimumWidth(680)
        self.plugin_keys = dict(plugin_keys or {})
        self.edits = {}  # command id -> [QKeySequenceEdit]

        grid_widget = QWidget()
        grid = QGridLayout(grid_widget)
        grid.setContentsMargins(0, 0, 8, 0)
        grid.setHorizontalSpacing(8)
        grid.setColumnStretch(0, 1)
        row = 0
        for group in shortcuts.GROUPS:
            title = QLabel(group.upper())
            title.setObjectName("SectionTitle")
            grid.addWidget(title, row, 0, 1, shortcuts.MAX_KEYS + 1)
            row += 1
            for command in (command for command in shortcuts.COMMANDS if command.group == group):
                grid.addWidget(QLabel(command.title), row, 0)
                self.edits[command.id] = []
                for column in range(shortcuts.MAX_KEYS):
                    edit = QKeySequenceEdit()
                    edit.setMaximumSequenceLength(1)
                    edit.setClearButtonEnabled(True)
                    edit.setMinimumWidth(110)
                    edit.keySequenceChanged.connect(self._update)
                    grid.addWidget(edit, row, column + 1)
                    self.edits[command.id].append(edit)
                row += 1

        self.conflict_label = QLabel()
        self.conflict_label.setWordWrap(True)
        self.conflict_label.setStyleSheet(f"color: {COLOR_REJECT};")
        defaults_button = QPushButton("Restore Defaults")
        defaults_button.clicked.connect(lambda: self.set_bindings(shortcuts.defaults()))
        footer = QHBoxLayout()
        footer.addWidget(self.conflict_label, 1)
        footer.addWidget(defaults_button)

        layout = self.layout()
        layout.insertWidget(layout.count() - 1, _scroll(grid_widget, 440))
        layout.insertLayout(layout.count() - 1, footer)
        self.set_bindings(bindings)
        self.confirm_button.setFocus()  # not a key field, which would record the first key pressed

    def set_bindings(self, bindings):
        for command_id, edits in self.edits.items():
            keys = list(bindings.get(command_id, ()))[: len(edits)]
            for index, edit in enumerate(edits):
                edit.blockSignals(True)
                edit.setKeySequence(QKeySequence(keys[index]) if index < len(keys) else QKeySequence())
                edit.blockSignals(False)
        self._update()

    @property
    def bindings(self):
        result = {}
        for command_id, edits in self.edits.items():
            keys = (shortcuts.normalize(edit.keySequence().toString()) for edit in edits)
            result[command_id] = tuple(dict.fromkeys(key for key in keys if key))
        return result

    def conflicts(self):
        return shortcuts.conflicts(self.bindings, self.plugin_keys)

    def _update(self):
        conflicts = self.conflicts()
        for edits in self.edits.values():
            for edit in edits:
                clash = shortcuts.normalize(edit.keySequence().toString()) in conflicts
                edit.setStyleSheet(f"QLineEdit {{ border: 1px solid {COLOR_REJECT}; }}" if clash else "")
        lines = []
        for key, owners in conflicts.items():
            names = [shortcuts.BY_ID[owner].title if owner in shortcuts.BY_ID else owner for owner in owners]
            lines.append(f"{shortcuts.native(key)} is used by " + " and ".join(names))
        self.conflict_label.setText("\n".join(lines))
        self.confirm_button.setEnabled(not conflicts)
