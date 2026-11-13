"""Plugins: Python files that add actions to the app and react to what happens in it.

A plugin is `<name>.py`, or a `<name>/` package, in the plugins folder, with a
`register(tamis)` function that receives a PluginApi. Plugins are ordinary code
with the user's rights, so they are only ever loaded from the app's data folder,
never from a photo folder. A plugin's failure must not take the app down: everything
they run goes through PluginManager._call, since an exception escaping into a Qt
slot would abort the process.
"""
import copy
import importlib.util
import json
import os
import sys
import traceback
from dataclasses import dataclass, field

from PyQt6.QtCore import QObject, QTimer, QUrl
from PyQt6.QtGui import QAction, QDesktopServices, QKeySequence, QShortcut
from PyQt6.QtWidgets import QMenu

from . import __version__, develop, xmp
from .constants import KEEP, REJECT
from .widgets import inform

EVENTS = frozenset({
    "folder_opened",     # (folder) a folder was opened
    "folder_refreshed",  # (folder) the open folder's photos were scanned again
    "photo_changed",     # (photo) another photo became the current one
    "photos_edited",     # ([photos]) marks, ratings, labels or edits changed, undo included
    "files_moved",       # ({old path: new path}) Process Files moved photos and their companions
    "photos_exported",   # ([paths]) JPEGs were written to _EXPORTS
    "photos_trashed",    # ([paths]) photos of _REJECTS were moved to the Trash
    "closing",           # () the window is closing; changes made now are still saved
})


@dataclass(frozen=True)
class PhotoInfo:
    """What a plugin sees of a photo: a snapshot, changed through the PluginApi."""

    path: str
    name: str
    files: tuple  # the photo and its companions, such as the JPEG of a RAW+JPEG pair
    status: str | None  # KEEP, REJECT or None
    location: str | None  # the folder it sits in: KEEP, REJECT or None (root)
    rating: int
    label: str | None
    edit: dict

    @classmethod
    def of(cls, photo):
        return cls(
            photo.path, photo.name, tuple(photo.files), photo.status, photo.location,
            photo.rating, photo.label, copy.deepcopy(photo.edit),
        )


@dataclass
class Plugin:
    id: str  # file name without extension
    path: str
    name: str  # NAME in the plugin, else its id
    enabled: bool = True
    loaded: bool = False
    problems: list = field(default_factory=list)
    actions: list = field(default_factory=list)


def discover(directory):
    """{plugin id: path of the file to import} in `directory`; a package wins over a same-named file."""
    plugins = {}
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return plugins
    for name in names:
        path = os.path.join(directory, name)
        stem, extension = os.path.splitext(name)
        if name.startswith(("_", ".")) or stem in plugins:
            continue
        if extension == ".py" and os.path.isfile(path):
            plugins[stem] = path
        elif os.path.isfile(os.path.join(path, "__init__.py")):
            plugins[name] = os.path.join(path, "__init__.py")
    return plugins


def _import(plugin):
    module_name = f"tamis_plugin_{plugin.id}"
    # A package's own submodules are found through submodule_search_locations.
    locations = [os.path.dirname(plugin.path)] if os.path.basename(plugin.path) == "__init__.py" else None
    spec = importlib.util.spec_from_file_location(module_name, plugin.path, submodule_search_locations=locations)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[module_name]
        raise
    return module


class PluginApi:
    """What `register(tamis)` receives: the one object a plugin needs."""

    KEEP = KEEP
    REJECT = REJECT

    def __init__(self, manager, plugin):
        self._manager = manager
        self._plugin = plugin
        self.window = manager.window  # the main window, for what this API doesn't cover: it may change between versions

    # -- extending the app -------------------------------------------------

    def add_action(self, title, callback, shortcut=None):
        """Add an entry to the Plugins menu, with an optional shortcut; returns its QAction.

        `callback` takes no arguments. A shortcut that is invalid or already taken is
        dropped and reported, since Qt would otherwise stop firing both of them.
        """
        action = QAction(title, self.window)
        action.triggered.connect(lambda _checked=False: self._manager._call(self._plugin, callback))
        if shortcut:
            self._manager._bind_shortcut(self._plugin, action, shortcut)
        self.window.addAction(action)
        self._plugin.actions.append(action)
        return action

    def on(self, event, callback):
        """Call `callback` when `event` happens; see EVENTS for the events and their arguments."""
        if event not in EVENTS:
            raise ValueError(f"Unknown event {event!r}; the events are: {', '.join(sorted(EVENTS))}")
        self._manager._handlers.setdefault(event, []).append((self._plugin, callback))

    # -- reading -----------------------------------------------------------

    @property
    def version(self):
        """Tamis's version, such as "0.1.0" ("unknown" when run from a checkout that was never installed)."""
        return __version__

    @property
    def folder(self):
        """The open folder, "" if none."""
        return self.window.folder

    def photos(self):
        """Every photo of the open folder, as PhotoInfo."""
        return [PhotoInfo.of(photo) for photo in self.window.model.photos]

    def current_photo(self):
        """The photo being shown, or None."""
        row = self.window.current_row
        return PhotoInfo.of(self.window.model.photo(row)) if row >= 0 else None

    def selected_photos(self):
        """The photos a mark would apply to now: the Gallery's selection, or the current photo."""
        return [PhotoInfo.of(self.window.model.photo(row)) for row in self.window._target_rows()]

    # -- changing photos ---------------------------------------------------
    # `photos` is a PhotoInfo or path, or a list of them. Each call is one undo step.

    def go_to(self, photo):
        """Show this photo."""
        for row in self._rows(photo):
            self.window.go_to(row)

    def set_status(self, photos, status):
        """Mark as tamis.KEEP, tamis.REJECT, or None to unmark."""
        if status not in (KEEP, REJECT, None):
            raise ValueError(f"Invalid status: {status!r}")
        rows = self._rows(photos)
        self.window._push_undo(self.window.set_marks(dict.fromkeys(rows, status)))

    def set_rating(self, photos, rating):
        """Give 0 to 5 stars."""
        if isinstance(rating, bool) or not isinstance(rating, int) or not 0 <= rating <= 5:
            raise ValueError(f"Invalid rating: {rating!r}")
        self._set(photos, "rating", rating)

    def set_label(self, photos, label):
        """Give a colour label from xmp.LABELS, or None to remove it."""
        if label is not None and label not in xmp.LABELS:
            raise ValueError(f"Invalid label: {label!r}; the labels are: {', '.join(xmp.LABELS)}")
        self._set(photos, "label", label)

    def set_edit(self, photos, edit):
        """Replace the edit: a dict of develop.DEFAULTS settings; {} removes it. Invalid values are dropped."""
        rows = self._rows(photos)
        if rows:
            self.window._set_edit(rows, develop.clean(edit))

    def _set(self, photos, name, value):
        self.window._push_undo(self.window.model.set_field(self._rows(photos), name, value))

    def _rows(self, photos):
        if isinstance(photos, (str, PhotoInfo)):
            photos = [photos]
        paths = (photo if isinstance(photo, str) else photo.path for photo in photos)
        return [row for row in map(self.window.model.row_of, paths) if row != -1]

    # -- talking to the user -----------------------------------------------

    def toast(self, text, msecs=2200):
        """A short message over the window."""
        self.window.toast.show_message(text, msecs)

    def message(self, title, text, details=None):
        """A dialog with a rich `text`, and optionally a list of lines to scroll through."""
        inform(self.window, title, text, details=details)

    # -- settings ----------------------------------------------------------

    def get_setting(self, key, default=None):
        """A value saved with set_setting, kept across launches."""
        stored = self.window.settings.value(self._setting_key(key))
        try:
            return default if stored is None else json.loads(stored)
        except (TypeError, ValueError):
            return default

    def set_setting(self, key, value):
        """Save a value that JSON can represent."""
        # As JSON because QSettings gives types back differently on each platform.
        self.window.settings.setValue(self._setting_key(key), json.dumps(value))

    def _setting_key(self, key):
        return f"plugin_settings/{self._plugin.id}/{key}"


class PluginManager(QObject):
    """Loads the plugins, calls them when events happen, and fills the Plugins menu."""

    DISABLED_KEY = "disabled_plugins"

    def __init__(self, window, directory):
        super().__init__(window)
        self.window = window
        self.directory = directory
        self.plugins = []
        self.menu = QMenu(window)
        self.menu.aboutToShow.connect(self._fill_menu)
        self._installed_menu = QMenu("Installed", self.menu)
        self._handlers = {}  # event -> [(plugin, callback)]
        self._loading = False
        window.model.photos_edited.connect(self._on_photos_edited)

    @property
    def active(self):
        return any(plugin.loaded for plugin in self.plugins)

    def load(self):
        """Load the folder's plugins; call once the app's own shortcuts exist, so conflicts are seen."""
        self._loading = True
        disabled = self._disabled()
        for plugin_id, path in discover(self.directory).items():
            plugin = Plugin(plugin_id, path, plugin_id, enabled=plugin_id not in disabled)
            self.plugins.append(plugin)
            if plugin.enabled:
                self._load_one(plugin)
        self._loading = False
        if self.problems():
            # Once the window is shown: it isn't laid out yet, and the toast places itself in it.
            QTimer.singleShot(0, self._report_load_problems)

    def _load_one(self, plugin):
        try:
            module = _import(plugin)
            plugin.name = str(getattr(module, "NAME", plugin.id))
            if not callable(getattr(module, "register", None)):
                raise TypeError("the plugin has no register(tamis) function")
            module.register(PluginApi(self, plugin))
        except (Exception, SystemExit) as exc:
            self._forget(plugin)
            self._problem(plugin, exc)
        else:
            plugin.loaded = True

    def _forget(self, plugin):
        """Remove what a plugin registered before failing."""
        for action in plugin.actions:
            self.window.removeAction(action)
            action.deleteLater()
        plugin.actions.clear()
        for event, handlers in self._handlers.items():
            self._handlers[event] = [handler for handler in handlers if handler[0] is not plugin]

    def emit(self, event, *args):
        for plugin, callback in list(self._handlers.get(event, ())):
            self._call(plugin, callback, *args)

    def _on_photos_edited(self, rows):
        if self._handlers.get("photos_edited"):  # no snapshots of a big selection for nobody
            self.emit("photos_edited", [PhotoInfo.of(self.window.model.photo(row)) for row in rows])

    def _call(self, plugin, callback, *args):
        try:
            callback(*args)
        except (Exception, SystemExit) as exc:
            self._problem(plugin, exc)

    def _bind_shortcut(self, plugin, action, shortcut):
        sequence = QKeySequence(shortcut)
        text = sequence.toString()
        if not text:
            self._warn(plugin, f'Invalid shortcut "{shortcut}" for "{action.text()}"')
        elif text in self._taken_shortcuts():
            self._warn(plugin, f'Shortcut "{text}" for "{action.text()}" is already taken')
        else:
            action.setShortcut(sequence)

    def _taken_shortcuts(self):
        taken = {shortcut.key().toString() for shortcut in self.window.findChildren(QShortcut)}
        taken.update(action.shortcut().toString() for plugin in self.plugins for action in plugin.actions)
        return taken

    # -- problems ----------------------------------------------------------

    def problems(self):
        return [f"{plugin.name}: {problem}" for plugin in self.plugins for problem in plugin.problems]

    def _problem(self, plugin, exc):
        lines = traceback.format_exception(type(exc), exc, exc.__traceback__)
        self._warn(plugin, "".join(lines).rstrip(), toast=f'Plugin "{plugin.name}" failed: {exc}')

    def _warn(self, plugin, problem, toast=None):
        if problem in plugin.problems:
            return  # a handler failing on every photo is said once
        plugin.problems.append(problem)
        print(f"Plugin {plugin.id}: {problem}", file=sys.stderr)
        if not self._loading:
            self.window.toast.show_message(toast or f'Plugin "{plugin.name}": {problem}', 5000)

    def _report_load_problems(self):
        self.window.toast.show_message("A plugin had a problem: see the Plugins menu", 5000)

    def show_problems(self):
        inform(self.window, "Plugin problems", "What went wrong with the plugins since launch:", details=self.problems())

    # -- menu --------------------------------------------------------------

    def _fill_menu(self):
        self.menu.clear()
        self._installed_menu.clear()
        for plugin in self.plugins:
            if plugin.actions:
                self.menu.addActions(plugin.actions)
                self.menu.addSeparator()
            entry = self._installed_menu.addAction(plugin.name)
            entry.setCheckable(True)
            entry.setChecked(plugin.enabled)
            entry.toggled.connect(lambda on, plugin=plugin: self._set_enabled(plugin, on))
        if self.plugins:
            self.menu.addMenu(self._installed_menu)
        if self.problems():
            self.menu.addAction("Show Problems…", self.show_problems)
        self.menu.addAction("Open Plugins Folder", self.open_folder)

    def _disabled(self):
        try:
            return set(json.loads(self.window.settings.value(self.DISABLED_KEY, "[]")))
        except (TypeError, ValueError):
            return set()

    def _set_enabled(self, plugin, on):
        plugin.enabled = on
        disabled = (self._disabled() - {plugin.id}) | (set() if on else {plugin.id})
        self.window.settings.setValue(self.DISABLED_KEY, json.dumps(sorted(disabled)))
        self.window.toast.show_message(f'Restart Tamis to {"enable" if on else "disable"} "{plugin.name}"', 4000)

    def open_folder(self):
        try:
            os.makedirs(self.directory, exist_ok=True)
        except OSError as exc:
            self.window.toast.show_message(f"Could not create the plugins folder: {exc.strerror or exc}", 5000)
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(self.directory))
