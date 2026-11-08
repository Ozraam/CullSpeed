"""The app's keyboard shortcuts: one table that binds them, lists them and lets them be changed.

Keys are kept in Qt's portable text ("Ctrl+Z", "Shift+P"), which is ⌘ on macOS.
Only the commands whose keys differ from the defaults are saved, as JSON in
QSettings, so new defaults reach everyone who hasn't changed that command.
"""
import json
from dataclasses import dataclass

from PyQt6.QtGui import QKeySequence

from . import xmp

SETTINGS_KEY = "shortcuts"
MAX_KEYS = 3  # per command, in the editor


@dataclass(frozen=True)
class Command:
    id: str
    title: str
    group: str
    keys: tuple  # the defaults
    cull_only: bool = False  # in the gallery these keys move through the grid instead
    repeat: bool = True


def _commands():
    marking, navigation, view, editing, files = "Marking", "Navigation", "View", "Editing", "Files"
    commands = [
        Command("keep", "Keep current / selected", marking, ("P", "1")),
        Command("reject", "Reject current / selected", marking, ("X", "3")),
        Command("unmark", "Unmark", marking, ("U", "2", "Backspace")),
        Command("keep_cull", "Keep (Cull view)", marking, ("Up",), cull_only=True),
        Command("reject_cull", "Reject (Cull view)", marking, ("Down",), cull_only=True),
        Command("keep_one_of_burst", "Keep this photo, reject the rest of its burst", marking, ("Shift+P",)),
    ]
    commands += [Command(f"rate_{stars}", f"{stars} star{'s' * (stars != 1)}", marking, (f"Ctrl+{stars}",)) for stars in range(6)]
    # Lightroom's 6–9 leave no digit for the fifth label.
    label_keys = ("Ctrl+6", "Ctrl+7", "Ctrl+8", "Ctrl+9", "Ctrl+Shift+9")
    commands += [
        Command(f"label_{label.lower()}", f"{label} label (again to remove)", marking, (key,))
        for label, key in zip(xmp.LABELS, label_keys, strict=True)
    ]
    commands += [
        Command("undo", "Undo", marking, ("Ctrl+Z",)),
        Command("next", "Next photo", navigation, ("Right", "L", "Space"), cull_only=True),
        Command("previous", "Previous photo", navigation, ("Left", "H"), cull_only=True),
        Command("first", "First photo", navigation, ("Home",), cull_only=True),
        Command("last", "Last photo", navigation, ("End",), cull_only=True),
        Command("next_unmarked", "Next unmarked photo", navigation, ("N",), cull_only=True),
        Command("toggle_tab", "Switch Cull ↔ Gallery", navigation, ("G",)),
        Command("keeps_only", "Keeps only, on / off", view, ("K",), cull_only=True),
        Command("zoom", "Zoom 1:1 / back to fit", view, ("Z",), cull_only=True),
        Command("escape", "Back to fit, leave Compare, cancel the crop", view, ("Escape",), cull_only=True),
        Command("compare", "Compare, on / off", view, ("C",)),
        Command("info", "Shooting info, on / off", view, ("I",)),
        Command("bursts", "Bursts, on / off", view, ("B",)),
        Command("peaking", "Focus peaking, on / off", view, ("F",), cull_only=True),
        Command("clipping", "Clipping warnings, on / off", view, ("J",), cull_only=True),
        Command("before", "Show the photo without its edit, while held", view, ("Y", "\\"), cull_only=True, repeat=False),
        Command("shortcuts", "Keyboard shortcuts", view, ("?",)),
        Command("edit_panel", "Edit panel", editing, ("E",)),
        Command("crop", "Crop: open / apply", editing, ("R",)),
        Command("confirm_crop", "Apply the crop", editing, ("Return", "Enter"), cull_only=True),
        Command("rotate_right", "Rotate right", editing, ("]", "Shift+R")),
        Command("rotate_left", "Rotate left", editing, ("[", "Shift+L")),
        Command("copy_edit", "Copy edit", editing, ("Ctrl+C", "Ctrl+Shift+C")),
        Command("paste_edit", "Paste edit", editing, ("Ctrl+V", "Ctrl+Shift+V")),
        Command("open", "Open folder", files, ("Ctrl+O",)),
        Command("import", "Import from a memory card", files, ("Ctrl+I",)),
        Command("refresh", "Refresh the folder", files, ("Ctrl+R", "F5")),
        Command("export", "Export JPEG with the export settings", files, ("Ctrl+Shift+E",)),
    ]
    return commands


COMMANDS = _commands()
BY_ID = {command.id: command for command in COMMANDS}
GROUPS = list(dict.fromkeys(command.group for command in COMMANDS))


def normalize(key):
    """Qt's portable text for a key, or "" if it isn't a single valid key."""
    sequence = QKeySequence(key)
    return sequence.toString() if sequence.count() == 1 else ""


def native(key):
    """How a key reads on this system: "⌘Z" on macOS, "Ctrl+Z" elsewhere."""
    return QKeySequence(key).toString(QKeySequence.SequenceFormat.NativeText)


def defaults():
    return {command.id: tuple(normalize(key) for key in command.keys) for command in COMMANDS}


def resolve(overrides):
    """The keys of every command: the defaults with `overrides` ({id: [keys]}) applied.

    Unknown commands and invalid keys are dropped, and a key is only ever given to
    the first command that has it, since Qt fires neither of two ambiguous shortcuts.
    """
    bindings = defaults()
    for command_id, keys in (overrides or {}).items():
        if command_id in bindings and isinstance(keys, (list, tuple)):
            bindings[command_id] = tuple(dict.fromkeys(key for key in map(normalize, (str(key) for key in keys)) if key))
    taken = set()
    for command_id, keys in bindings.items():
        bindings[command_id] = tuple(key for key in keys if key not in taken)
        taken.update(keys)
    return bindings


def overrides(bindings):
    """What differs from the defaults, which is all that gets saved."""
    default = defaults()
    return {command_id: list(keys) for command_id, keys in bindings.items() if keys != default.get(command_id)}


def load(settings):
    try:
        saved = json.loads(settings.value(SETTINGS_KEY, "") or "{}")
    except (TypeError, ValueError):
        saved = {}
    return resolve(saved if isinstance(saved, dict) else {})


def save(settings, bindings):
    changed = overrides(bindings)
    if changed:
        settings.setValue(SETTINGS_KEY, json.dumps(changed))
    else:
        settings.remove(SETTINGS_KEY)


def conflicts(bindings, others=None):
    """{key: [owners]} for every key bound more than once.

    Owners are command ids, or the names in `others` ({key: name}), such as plugin actions.
    """
    owners = {}
    for command_id, keys in bindings.items():
        for key in keys:
            owners.setdefault(key, []).append(command_id)
    for key, name in (others or {}).items():
        owners.setdefault(normalize(key), []).append(name)
    return {key: names for key, names in owners.items() if key and len(names) > 1}


def keys_text(keys):
    return " or ".join(native(key) for key in keys) or "—"


def hint(bindings):
    """The short reminder in the stats bar, from the current keys."""
    def key(command_id):
        keys = bindings.get(command_id, ())
        return native(keys[0]) if keys else ""

    def span(first, last):
        return f"{key(first)}–{key(last)}" if key(first) and key(last) else key(first) or key(last)

    labels = span("label_red", "label_blue")
    if key("label_purple"):
        labels = f"{labels} {key('label_purple')}".strip()
    parts = [
        (key("keep"), "keep"), (key("reject"), "reject"), (key("unmark"), "unmark"),
        (span("rate_1", "rate_5"), "stars"), (labels, "label"), (key("next_unmarked"), "next unmarked"),
        (key("keeps_only"), "keeps only"), (key("bursts"), "bursts"), (key("compare"), "compare"),
        (key("edit_panel"), "edit"), (key("before"), "hold for before"), (key("crop"), "crop"),
        (key("zoom"), "zoom"), (key("toggle_tab"), "gallery"), (key("undo"), "undo"),
    ]
    text = " · ".join(f"{keys} {what}" for keys, what in parts if keys)
    if key("shortcuts"):
        text += f" · {key('shortcuts')} all shortcuts"
    return text
