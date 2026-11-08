import errno
import json
import os
import threading
from contextlib import suppress
from html import escape

from PyQt6.QtCore import (
    QEvent,
    QEventLoop,
    QFileSystemWatcher,
    QItemSelectionModel,
    QRect,
    QSettings,
    QSize,
    QStandardPaths,
    Qt,
    QThread,
    QTimer,
    pyqtSignal,
)
from PyQt6.QtGui import QAction, QActionGroup, QKeySequence, QPainter, QPixmap, QShortcut, QTransform
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QMainWindow,
    QMenu,
    QProgressDialog,
    QPushButton,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import bursts, cropping, develop, exportsettings, lut, shortcuts, xmp
from .analysis import PhotoAnalyzer
from .constants import (
    FALLBACK_SESSIONS_DIR,
    KEEP,
    KEEP_DIR,
    LEGACY_GLOBAL_CONFIG_FILENAME,
    PLUGINS_DIR,
    PREFETCH_AHEAD,
    PREFETCH_BEHIND,
    PRESETS_FILENAME,
    REJECT,
    REJECT_DIR,
    SESSION_FILENAME,
    UNDO_LIMIT,
)
from .editing import EditRenderer, EditThumbnails, export_photo
from .editpanel import EditPanel
from .exif import summary
from .exportsettings import ExportSettings
from .fileops import (
    copy_export,
    execute_import,
    execute_moves,
    execute_trash,
    export_record,
    find_card,
    is_jpeg,
    jpeg_file,
    plan_exports,
    plan_moves,
    plan_trash,
    record_path,
    rejected_exports,
    remove_rejected_exports,
    trash_size,
    without_export,
)
from .imaging import is_raw
from .loader import ImageLoader
from .models import SORT_KEYS, PhotoModel, StatusFilterProxy, by_name, photo_files, relative_key, save_session, scan_folder
from .peaking import FocusPeaker
from .plugins import PhotoInfo, PluginManager
from .presets import PresetStore
from .shortcutsdialog import ShortcutsDialog, ShortcutsEditor
from .thumbcache import ThumbnailCache
from .tones import ToneReader
from .widgets import (
    CompareView,
    ExportSettingsDialog,
    GalleryToolbar,
    HeaderBar,
    ImageView,
    ImportDialog,
    MessageDialog,
    NameDialog,
    ProcessDialog,
    StatsBar,
    ThumbnailDelegate,
    Toast,
    inform,
    make_thumbnail_view,
    size_text,
)

CULL_TAB, GALLERY_TAB = 0, 1
FILMSTRIP_THUMB_SIZE = 64
COMPARE_MAX = 4
CROP_CACHE_SIZE = 8  # cropped previews and full-resolution images kept

# How keep and reject reach other editors, as no flag is common to them (see xmp.py).
XMP_MARKS = (
    ("off", "Not at all"),
    ("rating", "Rejected as rating −1 (darktable, Bridge)"),
    ("labels", "Keep green, reject red (Lightroom, Capture One…)"),
)
MARK_LABELS = {KEEP: "Green", REJECT: "Red"}


def _names(paths, limit=5):
    """`a.jpg, b.jpg and 3 more`, as rich text."""
    names = [escape(os.path.basename(path)) for path in paths]
    extra = f" and {len(names) - limit} more" if len(names) > limit else ""
    return ", ".join(names[:limit]) + extra


class MoveThread(QThread):
    """Runs execute_moves() off the GUI thread, so slow disks don't freeze the window."""

    progressed = pyqtSignal(int)

    def __init__(self, moves, parent=None):
        super().__init__(parent)
        self.moves = moves
        self.result = None
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        def progress(done):
            self.progressed.emit(done)
            return not self._stop.is_set()

        self.result = execute_moves(self.moves, progress)


class ExportThread(QThread):
    """Renders edited photos to JPEG (or copies unedited ones) off the GUI thread."""

    progressed = pyqtSignal(int)

    def __init__(self, jobs, dest_dir, settings=None, protected=(), parent=None):
        super().__init__(parent)
        # [(path, edit, LUT path, name)]: an unedited JPEG is copied (unless it must be
        # resized), anything else rendered; name None picks a free one
        self.jobs = jobs
        self.dest_dir = dest_dir
        self.settings = settings or ExportSettings()
        # Files never to write over, the photos': only a mistaken destination would get there.
        self.protected = {os.path.normcase(os.path.abspath(path)) for path in protected}
        self.exported = []
        self.sources = {}  # exported path -> the job's path
        self.errors = []
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        tables = {}
        for done, (path, edit, lut_path, name) in enumerate(self.jobs):
            if self._stop.is_set():
                break
            self.progressed.emit(done)
            try:
                if name is not None and os.path.normcase(os.path.abspath(os.path.join(self.dest_dir, name))) in self.protected:
                    raise OSError(errno.EEXIST, "a photo has that name in the export folder")
                if edit or not is_jpeg(path) or self.settings.resizes:
                    dest = export_photo(
                        path, edit, lut_path, self.dest_dir, tables,
                        quality=self.settings.quality, name=name, long_edge=self.settings.long_edge,
                    )
                else:
                    dest = copy_export(path, self.dest_dir, name)
                self.exported.append(dest)
                self.sources[dest] = path
            except Exception as exc:  # one bad photo must not stop the others
                self.errors.append(f"{os.path.basename(path)}: {exc}")
        self.progressed.emit(len(self.jobs))


class WorkThread(QThread):
    """Runs `work(progress)` off the GUI thread; `progress(done)` returns False once asked to stop."""

    progressed = pyqtSignal(int)

    def __init__(self, work, parent=None):
        super().__init__(parent)
        self.work = work
        self.result = None
        self.failure = None
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        def progress(done):
            self.progressed.emit(done)
            return not self._stop.is_set()

        try:
            self.result = self.work(progress)
        except Exception as exc:  # an exception escaping run() would abort the process
            self.failure = str(exc) or type(exc).__name__


class TamisApp(QMainWindow):
    def __init__(self, plugins_dir=None, cache_dir=None, presets_file=None):
        super().__init__()
        self.setWindowTitle("Tamis")
        self.setMinimumSize(900, 600)
        self.setAcceptDrops(True)

        self.settings = QSettings()
        # Where marks go when a photo folder is read-only.
        data_dir = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)
        self.session_fallback_dir = os.path.join(data_dir, FALLBACK_SESSIONS_DIR)
        self.session_warning = None  # last session problem shown, so it is shown once
        self.xmp_warned = False
        self.xmp_dirty = set()  # photos whose rating/label still has to be written to XMP
        self.moved_keys = set()  # session keys of moved photos, until a save has dropped them
        self.damaged_sessions = set()  # session files the user was told about
        self.folder = ""
        self.current_row = -1
        self.compare_rows = []  # rows shown side by side; empty outside Compare
        self.live_edit = None  # (path, edit) while a slider is being dragged, before it is kept
        self.edit_clipboard = None
        self.cropping = False  # the crop tool is open on the current photo
        self.crop_cache = {}  # (pixmap key, framing) -> framed pixmap, for what the renderer doesn't frame
        self.before = False  # the photo is being shown without its edit
        self.show_clipping = False
        self.presets = PresetStore(presets_file or os.path.join(data_dir, PRESETS_FILENAME))
        self.keeps_only = False  # the Cull view only shows and steps through keeps
        self.lut_folder = self.settings.value("lut_folder", "")
        self.luts = {}  # LUT id -> path, from lut_folder
        self.show_info = self.settings.value("show_info", True, type=bool)
        self.undo_stack = []
        self.moving = False
        self.sort_order = self.settings.value("sort_order", "name")
        if self.sort_order not in SORT_KEYS:
            self.sort_order = "name"
        self.show_bursts = self.settings.value("show_bursts", False, type=bool)
        self.peaking = False
        self.xmp_marks = self.settings.value("xmp_marks", "off")
        if self.xmp_marks not in dict(XMP_MARKS):
            self.xmp_marks = "off"
        self.export_settings = exportsettings.load_settings(self.settings.value("export_settings", ""))
        self.export_profiles = exportsettings.load_profiles(self.settings.value("export_profiles", ""))
        # What Process Files exports: "all" but rejects (the default, as before there was a choice) or "keeps".
        self.export_scope = self.settings.value("export_scope", "all")
        if self.export_scope not in dict(exportsettings.SCOPES):
            self.export_scope = "all"

        cache_dir = cache_dir or QStandardPaths.writableLocation(QStandardPaths.StandardLocation.CacheLocation)
        thumbnail_cache = ThumbnailCache(os.path.join(cache_dir, "thumbnails"))
        self.loader = ImageLoader(self, thumbnail_cache)
        self.loader.preview_loaded.connect(self._on_image_ready)
        self.loader.preview_failed.connect(lambda path, _error: self._on_image_ready(path))
        self.loader.full_loaded.connect(self._on_image_ready)
        self.loader.full_failed.connect(self._on_full_failed)

        self.editor = EditRenderer(self)
        self.editor.rendered.connect(self._on_image_ready)
        self.editor.failed.connect(self._on_edit_failed)
        self.edit_thumbnails = EditThumbnails(self.editor, thumbnail_cache, self)

        self.peaker = FocusPeaker(self)
        self.peaker.ready.connect(self._on_peaking_ready)

        self.tones = ToneReader(self)
        self.tones.ready.connect(self._refresh_view)

        self.analyzer = PhotoAnalyzer(os.path.join(cache_dir, "analysis"), self)
        self.analyzer.analyzed.connect(self._on_analyzed)
        self.analyzer.finished.connect(self._on_analysis_finished)
        # What the analysis finds shows up a few times a second, not once per photo.
        self.analysis_timer = QTimer(self)
        self.analysis_timer.setSingleShot(True)
        self.analysis_timer.setInterval(500)
        self.analysis_timer.timeout.connect(self._show_analysis)

        self.model = PhotoModel(self.loader, self)
        self.model.set_edit_thumbnails(self.edit_thumbnails, self._lut_path)
        self.model.statuses_changed.connect(self._on_statuses_changed)
        self.model.ratings_changed.connect(lambda paths: self.xmp_dirty.update(paths))
        self.model.marks_changed.connect(self._on_marks_changed)
        self.proxy = StatusFilterProxy(self)
        self.proxy.setSourceModel(self.model)
        self.strip_proxy = StatusFilterProxy(self)  # the filmstrip's own: filtered by "Keeps only"
        self.strip_proxy.setSourceModel(self.model)

        self.session_timer = QTimer(self)
        self.session_timer.setSingleShot(True)
        self.session_timer.setInterval(400)
        self.session_timer.timeout.connect(self.save_session)

        # New files (an import or a tethered shot landing in the folder) show up by themselves.
        self.watcher = QFileSystemWatcher(self)
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setSingleShot(True)
        self.refresh_timer.setInterval(800)
        self.refresh_timer.timeout.connect(lambda: self.refresh(auto=True))
        self.watcher.directoryChanged.connect(lambda _path: self.refresh_timer.start())

        self._build_ui()
        self.analyzer.progressed.connect(self.stats.set_analysis_progress)
        self.shortcut_keys = shortcuts.load(self.settings)
        self._build_shortcuts()
        self._load_luts(quiet=True)
        self._restore_window()
        self.plugins = PluginManager(self, plugins_dir or os.path.join(data_dir, PLUGINS_DIR))
        self.plugins_button.setMenu(self.plugins.menu)
        self.plugins.load()
        QTimer.singleShot(0, self._restore_last_folder)
        if self.presets.problem:
            QTimer.singleShot(0, lambda: self.toast.show_message(self.presets.problem, 6000))

    # -- UI ----------------------------------------------------------------

    def _build_ui(self):
        self.header = HeaderBar()
        self.header.open_folder_requested.connect(self.choose_folder)
        self.header.import_requested.connect(self.import_photos)
        self.header.trash_requested.connect(self.trash_rejects)
        self.header.refresh_requested.connect(self.refresh)
        self.header.process_requested.connect(self.process_files)

        # Cull tab: large image (or Compare) + filmstrip
        self.image_view = ImageView()
        self.image_view.zoom_changed.connect(self._on_zoom_changed)
        self.image_view.crop_confirmed.connect(self.toggle_crop)
        self.image_view.set_image(None, "Open a folder of photos to start\n(or drop one onto this window)")
        self.compare_view = CompareView()
        self.compare_view.zoom_changed.connect(self._on_zoom_changed)
        self.compare_view.activated.connect(lambda index: self.go_to(self.compare_rows[index]))
        self.cull_stack = QStackedWidget()
        self.cull_stack.addWidget(self.image_view)
        self.cull_stack.addWidget(self.compare_view)

        self.filmstrip = make_thumbnail_view(
            self.strip_proxy, ThumbnailDelegate(FILMSTRIP_THUMB_SIZE, show_names=False, parent=self), horizontal=True
        )
        self.filmstrip.setObjectName("Filmstrip")
        self.filmstrip.setFixedHeight(FILMSTRIP_THUMB_SIZE + ThumbnailDelegate.PADDING * 2 + 16)
        self.filmstrip.clicked.connect(lambda index: self.go_to(self.strip_proxy.mapToSource(index).row()))

        self.edit_panel = EditPanel()
        self.edit_panel.changed.connect(self._on_edit_changed)
        self.edit_panel.copy_requested.connect(self.copy_edit)
        self.edit_panel.paste_requested.connect(self.paste_edit)
        self.edit_panel.reset_requested.connect(self.reset_edit)
        self.edit_panel.export_requested.connect(self.export_photos)
        self.edit_panel.lut_folder_requested.connect(self.choose_lut_folder)
        self.edit_panel.crop_requested.connect(self.toggle_crop)
        self.edit_panel.crop_cleared.connect(self.clear_crop)
        self.edit_panel.crop_ratio_changed.connect(self._on_crop_ratio_changed)
        self.edit_panel.rotate_requested.connect(self.rotate_photos)
        self.edit_panel.clipping_toggled.connect(self.set_clipping)
        self.edit_panel.before_held.connect(self._set_before)
        self.edit_panel.preset_chosen.connect(self.apply_preset)
        self.edit_panel.preset_save_requested.connect(self.save_preset)
        self.edit_panel.preset_delete_requested.connect(self.delete_preset)
        self.edit_panel.set_presets(self.presets.names())
        self.edit_panel.setVisible(self.settings.value("edit_panel", False, type=bool))

        viewer = QHBoxLayout()
        viewer.setContentsMargins(0, 0, 0, 0)
        viewer.setSpacing(0)
        viewer.addWidget(self.cull_stack, 1)
        viewer.addWidget(self.edit_panel)

        cull_page = QWidget()
        cull_layout = QVBoxLayout(cull_page)
        cull_layout.setContentsMargins(0, 0, 0, 0)
        cull_layout.setSpacing(0)
        cull_layout.addLayout(viewer, 1)
        cull_layout.addWidget(self.filmstrip)

        # Gallery tab: filter chips + grid
        grid_size = int(self.settings.value("grid_thumb_size", 150))
        self.gallery_toolbar = GalleryToolbar(grid_size, self.sort_order, self.show_bursts)
        self.gallery_toolbar.filter_changed.connect(self.proxy.set_filter)
        self.gallery_toolbar.rating_changed.connect(self.proxy.set_min_rating)
        self.gallery_toolbar.labels_changed.connect(self.proxy.set_labels)
        self.gallery_toolbar.focus_changed.connect(self._on_focus_filter_changed)
        self.gallery_toolbar.sort_changed.connect(self.set_sort_order)
        self.gallery_toolbar.bursts_toggled.connect(self.set_bursts)
        self.gallery_toolbar.size_changed.connect(self._on_grid_size_changed)
        self.grid_delegate = ThumbnailDelegate(grid_size, show_names=True, parent=self)
        self.grid = make_thumbnail_view(self.proxy, self.grid_delegate, horizontal=False)
        self.grid.selectionModel().currentChanged.connect(self._on_grid_current_changed)
        self.grid.activated.connect(self._on_grid_activated)  # double-click / Enter

        gallery_page = QWidget()
        gallery_layout = QVBoxLayout(gallery_page)
        gallery_layout.setContentsMargins(0, 0, 0, 0)
        gallery_layout.setSpacing(0)
        gallery_layout.addWidget(self.gallery_toolbar)
        gallery_layout.addWidget(self.grid, 1)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.tabBar().setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.tabs.addTab(cull_page, "Cull")
        self.tabs.addTab(gallery_page, "Gallery")
        self.tabs.currentChanged.connect(self._on_tab_changed)

        # In the tab bar's corner, so it costs the photo no height.
        self.keeps_button = QPushButton("Keeps only")
        self.keeps_button.setObjectName("Chip")
        self.keeps_button.setCheckable(True)
        self.keeps_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.keeps_button.setToolTip("Only show and step through the photos you keep (K)")
        self.keeps_button.clicked.connect(lambda _checked: self.toggle_keeps_only())
        self.keeps_button.setFixedHeight(24)  # tall enough for the chip's rounded ends, which Qt drops otherwise
        self.plugins_button = QPushButton("Plugins ▾")
        self.plugins_button.setObjectName("Chip")
        self.plugins_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.plugins_button.setFixedHeight(24)
        self.options_button = QPushButton("Options ▾")
        self.options_button.setObjectName("Chip")
        self.options_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.options_button.setFixedHeight(24)
        self.options_button.setMenu(self._build_options_menu())
        corner = QWidget()
        corner.setObjectName("TabCorner")
        corner_layout = QHBoxLayout(corner)
        corner_layout.setContentsMargins(0, 0, 10, 0)
        corner_layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        corner_layout.addWidget(self.keeps_button)
        corner_layout.addWidget(self.options_button)
        corner_layout.addWidget(self.plugins_button)
        self.tabs.setCornerWidget(corner, Qt.Corner.TopRightCorner)
        self.tabs.tabBar().setDrawBase(False)

        self.stats = StatsBar()

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.header)
        layout.addWidget(self.tabs, 1)
        layout.addWidget(self.stats)
        self.setCentralWidget(central)

        self.toast = Toast(central)

    def _build_options_menu(self):
        menu = QMenu(self)
        marks = menu.addMenu("Show Keep and Reject in Other Editors")
        marks.setToolTipsVisible(True)
        group = QActionGroup(self)
        self.xmp_mark_actions = {}
        for value, title in XMP_MARKS:
            action = QAction(title, self, checkable=True, checked=value == self.xmp_marks)
            action.triggered.connect(lambda _checked, value=value: self.set_xmp_marks(value))
            group.addAction(action)
            marks.addAction(action)
            self.xmp_mark_actions[value] = action
        menu.addAction("Export Settings…", self.edit_export_settings)
        menu.addAction("Keyboard Shortcuts…", self.edit_shortcuts)
        return menu

    def _shortcut_slots(self):
        """What each command of `shortcuts.COMMANDS` does."""
        slots = {
            "keep": lambda: self.mark(KEEP),
            "reject": lambda: self.mark(REJECT),
            "unmark": lambda: self.mark(None),
            "keep_cull": lambda: self.mark(KEEP),
            "reject_cull": lambda: self.mark(REJECT),
            "keep_one_of_burst": self.keep_one_of_burst,
            "undo": self.undo,
            "next": lambda: self.step(1),
            "previous": lambda: self.step(-1),
            "first": lambda: self.go_to_end(first=True),
            "last": lambda: self.go_to_end(first=False),
            "next_unmarked": self.next_unmarked,
            "toggle_tab": self.toggle_tab,
            "keeps_only": self.toggle_keeps_only,
            "zoom": self.toggle_zoom,
            "escape": self.escape,
            "compare": self.toggle_compare,
            "info": self.toggle_info,
            "bursts": lambda: self.set_bursts(not self.show_bursts),
            "peaking": self.toggle_peaking,
            "clipping": lambda: self.set_clipping(not self.show_clipping),
            "before": self._hold_before,
            "shortcuts": self.show_shortcuts,
            "edit_panel": self.toggle_edit_panel,
            "crop": self.toggle_crop,
            "confirm_crop": self._confirm_crop,
            "rotate_right": lambda: self.rotate_photos(90),
            "rotate_left": lambda: self.rotate_photos(-90),
            "copy_edit": self.copy_edit,
            "paste_edit": self.paste_edit,
            "open": self.choose_folder,
            "import": self.import_photos,
            "refresh": self.refresh,
            "export": self.export_photos,
        }
        for stars in range(6):
            slots[f"rate_{stars}"] = lambda stars=stars: self.rate(stars)
        for label in xmp.LABELS:
            slots[f"label_{label.lower()}"] = lambda label=label: self.set_label(label)
        return slots

    def _build_shortcuts(self):
        """Bind the keys of `self.shortcut_keys`, replacing any bound before.

        Must run before plugins load, so that the plugins' shortcuts that are taken are refused.
        """
        for shortcut in getattr(self, "app_shortcuts", ()):
            shortcut.setParent(None)  # at once: plugins look for the taken keys among the children
            shortcut.deleteLater()
        self.app_shortcuts = []
        self.cull_shortcuts = []
        slots = self._shortcut_slots()
        in_cull = self.tabs.currentIndex() == CULL_TAB
        for command in shortcuts.COMMANDS:
            for key in self.shortcut_keys[command.id]:
                shortcut = QShortcut(QKeySequence(key), self)
                shortcut.setAutoRepeat(command.repeat)
                shortcut.activated.connect(slots[command.id])
                self.app_shortcuts.append(shortcut)
                if command.cull_only:  # in the gallery the arrow keys move through the grid instead
                    shortcut.setEnabled(in_cull)
                    self.cull_shortcuts.append(shortcut)
        self.stats.set_hint(shortcuts.hint(self.shortcut_keys))

    def _plugin_shortcuts(self):
        """{key: "Plugins > title"} for the plugins' actions that have a shortcut."""
        return {
            action.shortcut().toString(): f"{plugin.name} > {action.text()}"
            for plugin in self.plugins.plugins
            for action in plugin.actions
            if not action.shortcut().isEmpty()
        }

    def show_shortcuts(self):
        """The overview of every shortcut, from which they can be changed."""
        dialog = ShortcutsDialog(self, self.shortcut_keys, self._plugin_shortcuts())
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.customize:
            self.edit_shortcuts()

    def edit_shortcuts(self):
        """Change the keys; returns whether they were changed."""
        dialog = ShortcutsEditor(self, self.shortcut_keys, self._plugin_shortcuts())
        if dialog.exec() != QDialog.DialogCode.Accepted or dialog.bindings == self.shortcut_keys:
            return False
        self.shortcut_keys = dialog.bindings
        shortcuts.save(self.settings, self.shortcut_keys)
        self._build_shortcuts()
        self.toast.show_message("Keyboard shortcuts saved")
        return True

    # -- folder ------------------------------------------------------------

    def choose_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Select a photo folder", self.folder or os.path.expanduser("~")
        )
        if folder:
            self.load_folder(folder)

    def load_folder(self, folder, select=None, keep_state=False):
        """Open `folder`; `select` is a photo's path relative to it (or, from older versions, its name).

        `keep_state` (refreshing the open folder) keeps cached images and undo.
        """
        if self.moving:
            return
        self._finish_crop(apply=True)
        self.save_session()
        problems = []
        try:
            photos = scan_folder(folder, self.session_fallback_dir, problems)
        except OSError as exc:
            inform(self, "Cannot open folder", escape(f"{folder}: {exc}"))
            return
        self._report_damaged_sessions(problems)

        self.live_edit = None
        same_folder = keep_state and folder == self.folder
        if not same_folder:
            self.loader.reset()
            self.editor.reset()
            self.session_warning = None
            self.xmp_warned = False
            self.undo_stack.clear()
            self.crop_cache.clear()
            self.moved_keys.clear()
            self.peaker.reset()
            self.tones.reset()
            self.edit_thumbnails.reset()
            self.analyzer.open_folder(folder)
        self.analyzer.apply_known(photos)
        if self.show_bursts:
            bursts.assign(photos)
        photos.sort(key=SORT_KEYS[self.sort_order])
        self.xmp_dirty = {photo.path for photo in photos if photo.xmp_pending}  # try those again
        if self.xmp_marks == "rating":
            self.xmp_dirty |= {photo.path for photo in photos if photo.status == REJECT}
        self._end_compare(show=False)
        self.folder = folder
        self.current_row = -1
        if not same_folder:
            self._set_keeps_only(False)
        self.model.set_photos(photos)
        self.model.analysis_changed()
        self._request_analysis()
        self.settings.setValue("last_folder", folder)
        self.header.set_folder(folder)
        self._watch(folder)
        event = "folder_refreshed" if same_folder else "folder_opened"

        if not photos:
            card = os.path.isdir(os.path.join(folder, "DCIM"))
            hint = "\n\nA memory card? Copy its photos with Import…" if card else ""
            self.image_view.set_image(None, f"No photos in this folder{hint}")
            self.header.set_counter("")
            self.plugins.emit(event, folder)
            return

        keys = [relative_key(folder, photo.path) for photo in photos]
        names = [photo.name for photo in photos]
        if select in keys:
            self.go_to(keys.index(select))
        else:
            self.go_to(names.index(select) if select in names else 0)
        self._keep_current_in_filter()
        self.plugins.emit(event, folder)

    def refresh(self, auto=False):
        """Pick up photos added to or removed from the folder, keeping marks and the current photo."""
        if not self.folder or self.moving:
            return
        self.loader.retry_failed_thumbnails()
        try:
            files = photo_files(self.folder)
        except OSError:
            return
        known = {path for photo in self.model.photos for path in photo.files}
        if files == known:
            if not auto:
                self.toast.show_message("No new photos")
            return
        added, removed = len(files - known), len(known - files)
        current = self.model.photo(self.current_row).path if self.current_row >= 0 else None
        self.load_folder(self.folder, select=current and relative_key(self.folder, current), keep_state=True)
        changes = [f"{added} new file(s)" if added else "", f"{removed} gone" if removed else ""]
        self.toast.show_message("Folder updated · " + ", ".join(c for c in changes if c))

    def _watch(self, folder):
        if self.watcher.directories():
            self.watcher.removePaths(self.watcher.directories())
        directories = [d for d in (folder, os.path.join(folder, KEEP_DIR), os.path.join(folder, REJECT_DIR)) if os.path.isdir(d)]
        if directories:
            self.watcher.addPaths(directories)

    def _restore_last_folder(self):
        folder = self.settings.value("last_folder", "")
        last_file = self.settings.value("last_file", "")
        if not folder:
            folder, last_file = self._legacy_global_state()
        if folder and os.path.isdir(folder):
            self.load_folder(folder, select=last_file)

    @staticmethod
    def _legacy_global_state():
        """Last folder from the pre-QSettings `~/.cullspeed_global.json`."""
        try:
            with open(os.path.join(os.path.expanduser("~"), LEGACY_GLOBAL_CONFIG_FILENAME)) as file:
                config = json.load(file)
            return config.get("last_folder") or "", config.get("last_file") or ""
        except (OSError, ValueError, AttributeError):
            return "", ""

    def save_session(self):
        self.session_timer.stop()
        if not self.folder or not self.model.photos:
            return
        self._write_xmp()
        problems = []
        try:
            path = save_session(self.folder, self.model.photos, self.session_fallback_dir, self.moved_keys, problems)
        except OSError as exc:
            self._session_problem("failed", f"Could not save marks: {exc.strerror or exc}")
            return
        finally:
            self._report_damaged_sessions(problems)
        self.moved_keys.clear()
        if path != os.path.join(self.folder, SESSION_FILENAME):
            self._session_problem("fallback", "This folder is read-only: marks are saved in the app's data folder")
        else:
            self.session_warning = None

    def _session_problem(self, kind, message):
        # Saving runs after every mark: say it once, not on every keystroke.
        if self.session_warning != kind:
            self.session_warning = kind
            self.toast.show_message(message, 5000)

    def _report_damaged_sessions(self, paths):
        # In a read-only folder a damaged file stays where it is, and is found again.
        paths = [path for path in paths if path not in self.damaged_sessions]
        self.damaged_sessions.update(paths)
        if paths:
            # Once the current work is done: this can come up in the middle of opening or saving a folder.
            QTimer.singleShot(0, lambda: inform(
                self,
                "Saved marks could not be read",
                "A session file is damaged, so some marks, ratings or edits may be missing. It was kept here:",
                details=paths,
            ))

    def _write_xmp(self):
        """Write pending ratings and labels to the photos' XMP sidecars; what fails is tried again at the next save."""
        dirty, self.xmp_dirty = self.xmp_dirty, set()
        listings = {}
        for path in dirty:
            row = self.model.row_of(path)
            if row == -1:
                continue
            photo = self.model.photo(row)
            directory = os.path.dirname(photo.path)
            try:
                if directory not in listings:
                    listings[directory] = {name.lower(): name for name in os.listdir(directory)}
                rejected = self.xmp_marks == "rating" and photo.status == REJECT
                xmp.write(photo.files, xmp.REJECTED_RATING if rejected else photo.rating, photo.label, listings[directory])
                photo.xmp_pending = False
            except OSError as exc:
                photo.xmp_pending = True
                self.xmp_dirty.add(path)
                if not self.xmp_warned:
                    self.xmp_warned = True
                    self.toast.show_message(
                        f"Could not write XMP sidecars ({exc.strerror or exc}): ratings stay in Tamis only", 5000
                    )

    # -- navigation --------------------------------------------------------

    def go_to(self, row):
        if not 0 <= row < self.model.rowCount():
            return
        moved_on = row != self.current_row
        if moved_on:
            self._finish_crop(apply=True)  # moving on keeps the frame, like any other edit
        if self.compare_rows and row not in self.compare_rows:
            self._end_compare(show=False)
        self.current_row = row
        photo = self.model.photo(row)
        self._update_counter()
        self.header.set_folder(self.folder, photo.display_name)

        # Invalid when the filmstrip's filter hides this photo (opened from the gallery).
        index = self.strip_proxy.mapFromSource(self.model.index(row))
        self.filmstrip.selectionModel().setCurrentIndex(
            index, QItemSelectionModel.SelectionFlag.ClearAndSelect
        )
        if index.isValid():
            self.filmstrip.scrollTo(index, QAbstractItemView.ScrollHint.PositionAtCenter)

        # Request first: a photo that failed before is retried, and shows "Loading…".
        nearby = [*self._walk(row, 1, PREFETCH_AHEAD), *self._walk(row, -1, PREFETCH_BEHIND)]
        others = [r for r in self._shown_rows() if r != row] + nearby
        self.loader.ensure_previews(photo.path, [self.model.photo(r).path for r in dict.fromkeys(others)])
        self.live_edit = None
        self.edit_panel.set_edit(photo.edit, is_raw(photo.path))
        self._refresh_view()
        if moved_on:
            self.plugins.emit("photo_changed", PhotoInfo.of(photo))

    def _in_cull(self, row):
        """Whether the Cull view's filter lets this photo through."""
        return not self.keeps_only or self.model.photo(row).status == KEEP

    def _cull_neighbor(self, row, delta):
        """The closest photo the Cull view shows past `row`, going in the direction of `delta`; None if none."""
        row += delta
        while 0 <= row < self.model.rowCount():
            if self._in_cull(row):
                return row
            row += delta
        return None

    def _walk(self, row, delta, count):
        """Up to `count` photos the Cull view shows past `row`."""
        rows = []
        while len(rows) < count and (row := self._cull_neighbor(row, delta)) is not None:
            rows.append(row)
        return rows

    def _update_counter(self):
        if self.current_row < 0:
            return
        position = self.strip_proxy.mapFromSource(self.model.index(self.current_row))
        if self.keeps_only and position.isValid():
            self.header.set_counter(f"{position.row() + 1} / {self.strip_proxy.rowCount()} keeps")
        else:
            self.header.set_counter(f"{self.current_row + 1} / {self.model.rowCount()}")

    def step(self, delta):
        """Next / previous photo, or the next / previous one being compared."""
        if self.compare_rows:
            i = self.compare_rows.index(self.current_row) + delta
            if 0 <= i < len(self.compare_rows):
                self.go_to(self.compare_rows[i])
            return
        row = self._cull_neighbor(self.current_row, delta)
        if row is not None:
            self.go_to(row)

    def go_to_end(self, first):
        """The first or last photo the Cull view shows."""
        row = self._cull_neighbor(-1, 1) if first else self._cull_neighbor(self.model.rowCount(), -1)
        if row is not None:
            self.go_to(row)

    def toggle_keeps_only(self):
        on = not self.keeps_only
        if on and not self.model.counts()[0]:
            self.keeps_button.setChecked(False)
            self.toast.show_message("No keeps yet: mark photos with P")
            return
        self._set_keeps_only(on)
        self.toast.show_message("Showing keeps only" if on else "Showing all photos")

    def _set_keeps_only(self, on):
        if on == self.keeps_only:
            return
        self._finish_crop(apply=True)
        self._end_compare(show=False)
        self.keeps_only = on
        self.keeps_button.setChecked(on)
        self.strip_proxy.set_filter(KEEP if on else StatusFilterProxy.ALL)
        self._keep_current_in_filter()
        if self.current_row >= 0 and self._in_cull(self.current_row):
            self.go_to(self.current_row)  # the filmstrip was just rebuilt

    def _keep_current_in_filter(self):
        """With "Keeps only", move on when the current photo stops being a keep; stop filtering when none is left."""
        if (
            not self.keeps_only or self.current_row < 0 or self.compare_rows
            or self.tabs.currentIndex() != CULL_TAB or self._in_cull(self.current_row)
        ):
            return
        row = self._cull_neighbor(self.current_row, 1)
        if row is None:
            row = self._cull_neighbor(self.current_row, -1)
        if row is not None:
            self.go_to(row)
        else:
            self._set_keeps_only(False)
            self.toast.show_message("No keeps left · showing all photos")

    def next_unmarked(self):
        if self.keeps_only:
            self._set_keeps_only(False)  # an unmarked photo is not a keep: asking for one ends the filter
        count = self.model.rowCount()
        for offset in range(1, count + 1):
            row = (self.current_row + offset) % count
            if self.model.photo(row).status is None:
                self.go_to(row)
                return
        self.toast.show_message("Every photo is marked")

    def toggle_tab(self):
        self.tabs.setCurrentIndex(GALLERY_TAB if self.tabs.currentIndex() == CULL_TAB else CULL_TAB)

    # -- showing photos ----------------------------------------------------

    def _shown_rows(self):
        if self.compare_rows:
            return list(self.compare_rows)
        return [self.current_row] if self.current_row >= 0 else []

    def _zoomed(self):
        return self.compare_view.zoomed if self.compare_rows else self.image_view.zoomed

    def _refresh_view(self):
        """Show the current photo, or the compared ones, with their marks, info and zoom."""
        if self.current_row < 0:
            return
        if self.compare_rows:
            shown = list(zip(self.compare_view.views, self.compare_rows, strict=True))
        else:
            shown = [(self.image_view, self.current_row)]
        zoomed = self._zoomed()
        self.peaker.begin()
        self.tones.begin()
        # Previews are downscaled, so while zoomed the photos load at full resolution.
        self.loader.ensure_full([self.model.photo(row).path for _, row in shown], load=zoomed)
        self.editor.keep_requests({self.model.photo(row).path for _, row in shown})
        for view, row in shown:
            self._show_in(view, row, zoomed)

    def _show_in(self, view, row, zoomed):
        photo = self.model.photo(row)
        # The crop tool shows the whole photo. Otherwise what the renderer didn't
        # make is framed here, so a photo keeps its frame whatever is shown of it.
        framing = develop.framing(self._shown_edit(photo))
        if self.cropping:
            framing.pop("crop", None)
        pixmap = self._edited_pixmap(photo)
        if pixmap is None:
            pixmap = self._framed(self.loader.preview(photo.path), framing)
        if pixmap is not None:
            view.set_image(pixmap, native_size=self._framed_size(self.loader.native_size(photo.path), framing), keep_zoom=True)
        else:
            error = self.loader.preview_error(photo.path)
            view.set_image(None, f"Could not load {photo.name}\n\n{error}" if error else "Loading…", keep_zoom=True)
        view.set_status(photo.status)
        view.set_rating(photo.rating, photo.label)
        view.set_info(self._info_lines(photo) if self.show_info else [])
        view.set_active(bool(self.compare_rows) and row == self.current_row)
        shown = pixmap
        if zoomed and pixmap is not None:
            full = self.loader.full_pixmap(photo.path)
            if full is not None:
                shown = self._framed(full, framing)
                view.set_full_image(shown)
            else:
                view.set_full_loading(self.loader.full_error(photo.path) is None)
        peaks = self.peaking and shown is not None and not self.cropping
        view.set_overlay(self.peaker.overlay(shown) if peaks else None)
        clipping = self.show_clipping and shown is not None and not self.cropping
        view.set_clipping(self.tones.clipping(shown) if clipping else None, photo.path if clipping else None)
        view.set_note("Before" if self._showing_before(photo) else "")
        if row == self.current_row and self.edit_panel.isVisible():
            if pixmap is None:
                self.edit_panel.histogram.set_tone(None)
            else:
                # Until the new one is known the last one stays up, so that a slider doesn't make it blink.
                tone = self.tones.tone(pixmap)
                if tone is not None:
                    self.edit_panel.histogram.set_tone(tone)

    def _info_lines(self, photo):
        lines = summary(self.loader.info(photo.path))
        median = self.model.median_sharpness()
        if photo.sharpness is not None and median is not None:
            soft = " · looks soft" if self.model.is_soft(self.model.row_of(photo.path)) else ""
            lines.append(f"Sharpness {photo.sharpness:.0f} · folder median {median:.0f}{soft}")
        return lines

    def _shown_edit(self, photo):
        """The photo's edit, or the one being dragged."""
        return self.live_edit[1] if self.live_edit is not None and self.live_edit[0] == photo.path else photo.edit

    def _showing_before(self, photo):
        """Whether the photo is being shown without an edit it has."""
        return self.before and not self.cropping and bool(develop.look(self._shown_edit(photo)))

    def _develops(self, photo):
        """Whether the photo shows its own development even unedited: a RAW, while the edit panel is open.

        A RAW's preview is the camera's JPEG, which edits don't start from: shown while
        editing, it would change all at once with the first slider moved.
        """
        return self.edit_panel.isVisible() and is_raw(photo.path)

    def _edited_pixmap(self, photo):
        """The render of the photo's edit (or of the one being dragged), if it has one, or of its development."""
        develops = self._develops(photo)
        edit = self._shown_edit(photo)
        if self.before and not self.cropping:
            if not develops:
                return None  # the preview any unedited photo shows, which _framed() keeps in its frame
            edit = develop.own(edit)  # the development, framed, without the look
        live = self.live_edit is not None and self.live_edit[0] == photo.path
        if self.cropping:
            edit = {key: value for key, value in edit.items() if key != "crop"}
        if not edit and not develops:
            return None
        return self.editor.pixmap(photo.path, edit, self._lut_path(edit), quick=live, same_crop=self.cropping)

    def _framed(self, pixmap, framing):
        """`pixmap` turned, straightened and cropped as `framing` says, like the renderer does it."""
        if pixmap is None or not framing:
            return pixmap
        key = (pixmap.cacheKey(), json.dumps(framing, sort_keys=True))
        if key not in self.crop_cache:
            framed = pixmap
            if framing.get("rotate"):
                framed = framed.transformed(QTransform().rotate(framing["rotate"]))
            if framing.get("straighten"):
                framed = self._straightened(framed, framing["straighten"])
            if framing.get("crop"):
                top, bottom, left, right = develop.crop_box(framed.height(), framed.width(), framing["crop"])
                framed = framed.copy(QRect(left, top, right - left, bottom - top))
            self.crop_cache[key] = framed
            while len(self.crop_cache) > CROP_CACHE_SIZE:
                del self.crop_cache[next(iter(self.crop_cache))]  # oldest
        return self.crop_cache[key]

    @staticmethod
    def _straightened(pixmap, degrees):
        width, height = pixmap.width(), pixmap.height()
        zoom = develop.straighten_zoom(height, width, degrees)
        result = QPixmap(pixmap.size())
        painter = QPainter(result)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.translate(width / 2, height / 2)
        painter.rotate(degrees)
        painter.scale(zoom, zoom)
        painter.translate(-width / 2, -height / 2)
        painter.drawPixmap(0, 0, pixmap)
        painter.end()
        return result

    @staticmethod
    def _framed_size(size, framing):
        if size is None or not framing or not size.isValid():
            return size
        height, width = develop.framed_size(size.height(), size.width(), framing)
        return QSize(width, height)

    def _lut_path(self, edit):
        return self.luts.get(edit.get("lut")) if edit.get("lut") else None

    def _lut_missing(self, edit):
        """Whether the edit uses a film simulation that isn't in the LUT folder: rendering it would skip the look."""
        return bool(edit.get("lut")) and edit["lut"] not in self.luts

    def _is_shown(self, path):
        return any(self.model.photo(row).path == path for row in self._shown_rows())

    def _on_image_ready(self, path):
        if self._is_shown(path):
            self._refresh_view()

    def _on_edit_failed(self, path, error):
        if self._is_shown(path):
            self.toast.show_message(f"Could not render the edit: {error}", 4000)

    def _on_full_failed(self, path, error):
        if self._is_shown(path):
            self._refresh_view()
            self.toast.show_message(f"Full resolution unavailable: {error}", 4000)

    def _on_zoom_changed(self, _zoomed):
        self._refresh_view()

    def toggle_zoom(self):
        if self.cropping:
            return
        if self.compare_rows:
            self.compare_view.toggle_zoom(self.compare_rows.index(self.current_row))
        else:
            self.image_view.toggle_zoom()

    def escape(self):
        if self.cropping:
            self._finish_crop(apply=False)
        elif self.compare_rows and self.compare_view.zoomed:
            self.compare_view.reset_zoom()
        elif self.compare_rows:
            self._end_compare()
        else:
            self.image_view.reset_zoom()

    def toggle_peaking(self):
        self.peaking = not self.peaking
        self.toast.show_message("Focus peaking on" if self.peaking else "Focus peaking off")
        self._refresh_view()

    def _on_peaking_ready(self):
        if self.peaking:
            self._refresh_view()

    def set_clipping(self, on):
        self.show_clipping = on
        self.edit_panel.set_clipping(on)
        self.toast.show_message("Clipping warnings on" if on else "Clipping warnings off")
        self._refresh_view()

    def _hold_before(self):
        """The key was pressed: show the photo without its edit until a key is let go."""
        QApplication.instance().installEventFilter(self)
        self._set_before(True)

    def _set_before(self, on):
        if not on:
            QApplication.instance().removeEventFilter(self)
        if on != self.before:
            self.before = on
            self._refresh_view()

    def eventFilter(self, watched, event):
        # Only while a key holds the before view. Any key that is let go ends it: with a layout
        # where the key takes a modifier, it may be reported as another one, and this must never stick.
        if event.type() == QEvent.Type.WindowDeactivate or (
            event.type() == QEvent.Type.KeyRelease and not event.isAutoRepeat()
        ):
            self._set_before(False)
        return super().eventFilter(watched, event)

    def toggle_info(self):
        self.show_info = not self.show_info
        self.settings.setValue("show_info", self.show_info)
        self._refresh_view()

    # -- editing -----------------------------------------------------------

    def toggle_edit_panel(self):
        visible = not self.edit_panel.isVisible()
        self.edit_panel.setVisible(visible)
        self.settings.setValue("edit_panel", visible)
        if visible:
            self.tabs.setCurrentIndex(CULL_TAB)
            if self.current_row >= 0:
                self._show_edit_in_panel()
        self.live_edit = None
        self._refresh_view()

    def _show_edit_in_panel(self):
        photo = self.model.photo(self.current_row)
        self.edit_panel.set_edit(photo.edit, is_raw(photo.path))

    def _on_edit_changed(self, edit, final):
        if self.current_row < 0:
            return
        photo = self.model.photo(self.current_row)
        if final:
            self.live_edit = None
            self._set_edit([self.current_row], edit)
        else:
            self.live_edit = (photo.path, edit)
        self._refresh_view()

    def _set_edit(self, rows, edit):
        self._set_edits({row: dict(edit) for row in rows})

    def _set_edits(self, values):
        """Give each row its own edit, as one undo step: `values` is {row: edit}."""
        self._push_undo(self.model.set_values("edit", values))
        for row, edit in values.items():
            if not edit:
                self.editor.forget(self.model.photo(row).path)

    def _apply_look(self, rows, look):
        """Give `rows` this look. Each photo keeps its own framing and process."""
        self._set_edits({row: develop.clean({**look, **develop.own(self.model.photo(row).edit)}) for row in rows})

    def copy_edit(self):
        """Copy the current photo's look. Its framing belongs to that photo: it is not copied."""
        if self.current_row >= 0:
            self.edit_clipboard = develop.look(self.model.photo(self.current_row).edit)
            self.toast.show_message("Edit copied" if self.edit_clipboard else "Copied: no edit")

    def paste_edit(self):
        """Paste the copied look on the target photos, which keep their own framing."""
        rows = self._target_rows()
        if self.edit_clipboard is None:
            self.toast.show_message("Nothing to paste: copy an edit first")
        elif rows:
            self._apply_look(rows, self.edit_clipboard)
            self.toast.show_message(f"Edit pasted on {len(rows)} photo(s)")

    def reset_edit(self):
        self._finish_crop(apply=False)
        rows = self._target_rows()
        if rows:
            self._set_edit(rows, {})

    # -- presets -----------------------------------------------------------

    def save_preset(self):
        """Keep the current photo's look under a name, for this and other shoots."""
        if self.current_row < 0:
            return
        look = develop.look(self.model.photo(self.current_row).edit)
        if not look:
            self.toast.show_message("Nothing to save: this photo has no look to keep")
            return
        dialog = NameDialog(self, "Save preset", "A name for this look. A crop or rotation is not part of it.", "Save")
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        name = dialog.name
        if name in self.presets:
            confirm = MessageDialog(
                self, "Replace preset", f"Replace the preset <b>{escape(name)}</b>?", confirm_text="Replace"
            )
            if confirm.exec() != QDialog.DialogCode.Accepted:
                return
        self._change_presets(lambda: self.presets.save(name, look), f"Preset “{name}” saved")

    def apply_preset(self, name):
        look = self.presets.get(name)
        rows = self._target_rows()
        if look is not None and rows:
            self._apply_look(rows, look)
            self.toast.show_message(f"“{name}” applied to {len(rows)} photo(s)")

    def delete_preset(self, name):
        dialog = MessageDialog(
            self, "Delete preset", f"Delete the preset <b>{escape(name)}</b>? Photos using its look keep it.", confirm_text="Delete"
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._change_presets(lambda: self.presets.delete(name), f"Preset “{name}” deleted")

    def _change_presets(self, change, done):
        try:
            change()
        except OSError as exc:
            inform(self, "Could not save the presets", escape(str(exc)))
            return
        self.edit_panel.set_presets(self.presets.names())
        self.toast.show_message(done)

    # -- rotation ----------------------------------------------------------

    def rotate_photos(self, degrees):
        """Turn the target photos by a quarter turn (90 or -90): one undo step, also while culling."""
        self._finish_crop(apply=True)
        rows = self._target_rows()
        if rows:
            self._set_edits({row: develop.turned(self.model.photo(row).edit, degrees) for row in rows})

    # -- crop --------------------------------------------------------------

    def toggle_crop(self):
        """Open the crop tool on the current photo, or apply the frame and close it."""
        if self.cropping:
            self._finish_crop(apply=True)
            return
        if self.current_row < 0:
            return
        self._end_compare()
        self.tabs.setCurrentIndex(CULL_TAB)
        if not self.edit_panel.isVisible():
            self.toggle_edit_panel()  # the frame's shape is chosen there
        self.image_view.reset_zoom()
        self.live_edit = None
        crop = self.model.photo(self.current_row).edit.get("crop")
        if crop:
            self.edit_panel.set_crop_free()  # never reshape a frame the photo already has
        self.cropping = True
        self.edit_panel.set_cropping(True)
        self._refresh_view()  # the whole photo, before the frame's shape is worked out from it
        self.image_view.start_crop(crop or cropping.FULL, self.edit_panel.crop_pixel_ratio())

    def _finish_crop(self, apply):
        if not self.cropping:
            return
        frame = self.image_view.crop_frame
        self.cropping = False
        self.image_view.stop_crop()
        self.edit_panel.set_cropping(False)
        if apply and self.current_row >= 0:
            edit = {**self.model.photo(self.current_row).edit, "crop": frame}
            self._set_edit([self.current_row], develop.clean(edit))
        self._refresh_view()

    def _confirm_crop(self):
        if self.cropping:
            self._finish_crop(apply=True)

    def clear_crop(self):
        """Back to the whole photo: the frame while cropping, otherwise the photo's crop (undoable)."""
        if self.cropping:
            self.edit_panel.set_crop_free()
            self.image_view.set_crop_ratio(None)
            self.image_view.set_crop_frame(cropping.FULL)
        elif self.current_row >= 0:
            edit = self.model.photo(self.current_row).edit
            self._set_edit([self.current_row], develop.clean({key: value for key, value in edit.items() if key != "crop"}))

    def _on_crop_ratio_changed(self):
        if self.cropping:
            self.image_view.set_crop_ratio(self.edit_panel.crop_pixel_ratio())

    def choose_lut_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Folder of film simulation LUTs (HaldCLUT .png, .cube)", self.lut_folder or os.path.expanduser("~")
        )
        if folder:
            self.lut_folder = folder
            self.settings.setValue("lut_folder", folder)
            self._load_luts()

    def _load_luts(self, quiet=False):
        try:
            self.luts = lut.scan(self.lut_folder) if self.lut_folder and os.path.isdir(self.lut_folder) else {}
        except OSError:
            self.luts = {}
        self.edit_panel.set_luts([(lut_id, lut.display_name(lut_id)) for lut_id in self.luts])
        self.model.thumbnails_changed()
        if not quiet:
            self.toast.show_message(f"{len(self.luts)} film simulation(s) found")
        self._refresh_view()

    def export_photos(self):
        """Export the target photos, with their edits, as full-size JPEGs in _EXPORTS."""
        self._finish_crop(apply=True)
        rows = self._target_rows()
        if not rows or self.moving:
            return
        self.save_session()
        photos = [self.model.photo(row) for row in rows]
        missing = [photo.name for photo in photos if self._lut_missing(photo.edit)]
        if missing:
            dialog = MessageDialog(
                self,
                "Film simulation not found",
                f"{len(missing)} photo(s) use a film simulation that isn't in the LUT folder, "
                "and would be exported without it:",
                confirm_text="Export anyway",
                details=missing,
            )
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
        if not self._export_folder_ok():
            return
        jobs = [(p.path if p.edit else jpeg_file(p) or p.path, p.edit, self._lut_path(p.edit), None) for p in photos]
        thread = self._run_export(jobs, "Export JPEG")
        where = self.export_settings.folder
        if thread.errors:
            inform(
                self,
                "Some photos were not exported",
                f"Exported {len(thread.exported)} of {len(jobs)} photo(s) to {where}. These failed:",
                details=thread.errors,
            )
        else:
            self.toast.show_message(f"Exported {len(thread.exported)} photo(s) to {where}")

    def _export_folder_ok(self):
        """Whether the export folder of the settings can be used here; tells the user if not."""
        problem = self.export_settings.problem(self.folder)
        if problem:
            inform(self, "Choose another export folder", f"{problem} Change it in Options → Export Settings.")
        return problem is None

    def edit_export_settings(self):
        """Open the export settings; returns whether they were changed."""
        def save_profiles(profiles):
            self.export_profiles = profiles
            self.settings.setValue("export_profiles", exportsettings.dump_profiles(profiles))

        dialog = ExportSettingsDialog(self, self.export_settings, self.export_profiles, self.folder, save_profiles)
        if dialog.exec() != QDialog.DialogCode.Accepted or dialog.settings == self.export_settings:
            return False
        self.export_settings = dialog.settings
        self.settings.setValue("export_settings", json.dumps(self.export_settings.to_dict()))
        return True

    def _run_export(self, jobs, title):
        """Run an ExportThread behind a modal progress dialog; returns the finished thread."""
        progress = QProgressDialog("Exporting…", "Stop", 0, len(jobs), self)
        progress.setWindowTitle(title)
        progress.setWindowModality(Qt.WindowModality.ApplicationModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)
        protected = [path for photo in self.model.photos for path in photo.files]
        thread = ExportThread(jobs, self.export_settings.dest_dir(self.folder), self.export_settings, protected, self)
        thread.progressed.connect(progress.setValue)
        progress.canceled.connect(thread.stop)
        loop = QEventLoop()
        thread.finished.connect(loop.quit)
        self.moving = True  # same guard as moving files: no folder change meanwhile
        thread.start()
        loop.exec()
        self.moving = False
        progress.close()
        thread.deleteLater()
        self._record_exports(thread)
        if thread.exported:
            self.plugins.emit("photos_exported", list(thread.exported))
        return thread

    def _record_exports(self, thread):
        """Remember which photo each export came from, so that rejecting it later can remove the export."""
        if not thread.exported:
            return
        def key(path):
            return os.path.normcase(os.path.abspath(path))

        written = {key(dest) for dest in thread.exported}
        owners = {path: photo for photo in self.model.photos for path in photo.files}
        for photo in self.model.photos:  # a file that was replaced is no longer the export of its former photo
            if photo.exports:
                photo.exports = [record for record in photo.exports if key(record_path(self.folder, record)) not in written]
        for dest in thread.exported:
            photo = owners.get(thread.sources[dest])
            # A rejected photo exported on request stays: the user just asked for that file.
            if photo is not None and photo.status != REJECT:
                with suppress(OSError):  # gone already: nothing to remember
                    photo.exports = [*photo.exports, export_record(dest, self.export_settings.folder)]
        self.save_session()

    # -- compare -----------------------------------------------------------

    def toggle_compare(self):
        """Gallery: compare the selected photos. Cull: the current photo and the next one."""
        if self.compare_rows:
            self._end_compare()
            return
        self._finish_crop(apply=True)
        if self.tabs.currentIndex() == GALLERY_TAB:
            rows = sorted(self.proxy.mapToSource(i).row() for i in self.grid.selectionModel().selectedIndexes())
            if not 2 <= len(rows) <= COMPARE_MAX:
                self.toast.show_message(f"Select 2 to {COMPARE_MAX} photos to compare")
                return
            if not all(self._in_cull(row) for row in rows):
                self._set_keeps_only(False)  # asked for by name, whatever the Cull view's filter
        elif self.current_row >= 0 and (following := self._cull_neighbor(self.current_row, 1)) is not None:
            rows = [self.current_row, following]
        else:
            self.toast.show_message("No next photo to compare with")
            return

        self.compare_rows = rows
        self.compare_view.set_count(len(rows))
        self.cull_stack.setCurrentWidget(self.compare_view)
        self.tabs.setCurrentIndex(CULL_TAB)
        self.go_to(self.current_row if self.current_row in rows else rows[0])

    def _end_compare(self, show=True):
        if not self.compare_rows:
            return
        self.compare_rows = []
        self.compare_view.reset_zoom()
        self.cull_stack.setCurrentWidget(self.image_view)
        if show:
            self.go_to(self.current_row)
            self._keep_current_in_filter()  # it may have been rejected while being compared

    # -- marking -----------------------------------------------------------

    def _target_rows(self):
        if self.tabs.currentIndex() == GALLERY_TAB:
            rows = sorted(self.proxy.mapToSource(i).row() for i in self.grid.selectionModel().selectedIndexes())
            if rows:
                return rows
            # Never mark a photo the gallery filter is hiding.
            if not self.proxy.mapFromSource(self.model.index(self.current_row)).isValid():
                return []
        return [self.current_row] if self.current_row >= 0 else []

    def _push_undo(self, changes):
        if changes:
            self.undo_stack.append(changes)
            del self.undo_stack[:-UNDO_LIMIT]

    def mark(self, status):
        rows = self._target_rows()
        if not rows:
            return
        in_gallery = self.tabs.currentIndex() == GALLERY_TAB
        first_visible = min(self.proxy.mapFromSource(self.model.index(row)).row() for row in rows)
        marked_row = self.current_row
        self._push_undo(self.set_marks(dict.fromkeys(rows, status)))
        if in_gallery:
            self._select_after_filtering(first_visible)
        elif status is not None and not self.compare_rows and self.current_row == marked_row:
            # (With "Keeps only", a rejected photo has already moved on to the next keep.)
            following = self._cull_neighbor(self.current_row, 1)
            if following is not None:
                self.go_to(following)
            else:
                self._report_end()

    def set_marks(self, statuses):
        """Mark photos, `statuses` being {row: status}; returns the undo changes.

        With colour labels passing on the marks, the photos get theirs too, unless the user gave them another.
        """
        changes = self.model.set_values("status", statuses)
        if self.xmp_marks == "labels":
            labels = {
                row: MARK_LABELS.get(status) for row, status in statuses.items()
                if self.model.photo(row).label in (None, *MARK_LABELS.values())
            }
            changes += self.model.set_values("label", labels)
        return changes

    def set_xmp_marks(self, value):
        """How keep and reject reach other editors, for all photos from now on, starting with this folder's."""
        was_rating = self.xmp_marks == "rating"
        self.xmp_marks = value
        self.settings.setValue("xmp_marks", value)
        if value == "labels":
            marked = {row: photo.status for row, photo in enumerate(self.model.photos) if photo.status}
            self._push_undo(self.set_marks(marked))
        if value == "rating" or was_rating:
            self.xmp_dirty |= {photo.path for photo in self.model.photos if photo.status == REJECT}
        self.session_timer.start()

    def _on_marks_changed(self, paths):
        if self.xmp_marks == "rating":
            self.xmp_dirty.update(paths)

    def _first_visible(self, rows):
        return min(self.proxy.mapFromSource(self.model.index(row)).row() for row in rows)

    def _reselect_in_gallery(self, proxy_row):
        if self.tabs.currentIndex() == GALLERY_TAB:
            self._select_after_filtering(max(0, proxy_row))

    def rate(self, stars):
        rows = self._target_rows()
        if rows:
            first_visible = self._first_visible(rows)
            self._push_undo(self.model.set_field(rows, "rating", stars))
            self._reselect_in_gallery(first_visible)  # a star filter may have hidden them

    def set_label(self, label):
        """Give `label` to the target photos, or remove it if they all have it already."""
        rows = self._target_rows()
        if not rows:
            return
        if all(self.model.photo(row).label == label for row in rows):
            label = None
        first_visible = self._first_visible(rows)
        self._push_undo(self.model.set_field(rows, "label", label))
        self._reselect_in_gallery(first_visible)  # a label filter may have hidden them

    def _report_end(self):
        unmarked = self.model.counts()[2]
        if self.keeps_only:
            self.toast.show_message("Last keep", 3000)
        elif unmarked:
            self.toast.show_message(f"Last photo · {unmarked} still unmarked (N to jump)", 3000)
        else:
            self.toast.show_message("Last photo · every photo is marked", 3000)

    def _select_after_filtering(self, proxy_row):
        """Marked photos may drop out of a filtered gallery: select the one now in their place."""
        if self.grid.selectionModel().hasSelection() or self.proxy.rowCount() == 0:
            return
        index = self.proxy.index(min(proxy_row, self.proxy.rowCount() - 1), 0)
        self.grid.selectionModel().setCurrentIndex(index, QItemSelectionModel.SelectionFlag.ClearAndSelect)
        self.grid.scrollTo(index)
        self.go_to(self.proxy.mapToSource(index).row())

    def undo(self):
        if self.cropping:
            self._finish_crop(apply=False)
            return
        if not self.undo_stack:
            self.toast.show_message("Nothing to undo")
            return
        changes = self.undo_stack.pop()
        rows = self.model.restore(changes)
        shown = [row for row in rows if self._in_cull(row)]
        if shown and self.tabs.currentIndex() == CULL_TAB and self.current_row not in rows:
            self.go_to(shown[0])
        self.toast.show_message("Undone" if len(changes) == 1 else f"Undone ({len(changes)} photos)")

    def _on_statuses_changed(self, rows):
        keeps, rejects, unmarked = self.model.counts()
        self.stats.update_counts(keeps, rejects, unmarked)
        self.gallery_toolbar.update_counts(self.model.rowCount(), keeps, rejects, unmarked)
        # Even when everything is sorted, processing refreshes _EXPORTS.
        self.header.set_can_process(bool(self.folder) and self.model.rowCount() > 0)
        if self.current_row in rows and self.live_edit is None:
            self._show_edit_in_panel()  # undo, paste, reset…
        if set(rows) & set(self._shown_rows()):
            self._refresh_view()
        self._update_counter()
        self._keep_current_in_filter()
        self.session_timer.start()

    # -- gallery -----------------------------------------------------------

    def _on_tab_changed(self, index):
        self._set_before(False)
        for shortcut in self.cull_shortcuts:
            shortcut.setEnabled(index == CULL_TAB)
        self.keeps_button.setVisible(index == CULL_TAB)  # the gallery has its own filter
        if index == GALLERY_TAB:
            self._finish_crop(apply=True)
            self._end_compare(show=False)
            if self.current_row >= 0:
                proxy_index = self.proxy.mapFromSource(self.model.index(self.current_row))
                if proxy_index.isValid():
                    self.grid.selectionModel().setCurrentIndex(
                        proxy_index, QItemSelectionModel.SelectionFlag.ClearAndSelect
                    )
                    self.grid.scrollTo(proxy_index, QAbstractItemView.ScrollHint.PositionAtCenter)
            self.grid.setFocus()
        else:
            self.setFocus()
            if self.keeps_only and self.current_row >= 0 and not self._in_cull(self.current_row):
                self._set_keeps_only(False)  # this photo was picked in the gallery: show it
                self.toast.show_message("Showing all photos")

    def _on_grid_current_changed(self, proxy_index, _previous):
        # Clicks and arrow keys both move the current photo, so G opens that one.
        # Only while the gallery is shown: marking in the Cull view can also
        # move the grid's current item, when the filter hides the marked photo.
        if self.tabs.currentIndex() != GALLERY_TAB or not proxy_index.isValid():
            return
        row = self.proxy.mapToSource(proxy_index).row()
        if row != self.current_row:
            self.go_to(row)

    def _on_grid_activated(self, proxy_index):
        self.go_to(self.proxy.mapToSource(proxy_index).row())
        self.tabs.setCurrentIndex(CULL_TAB)

    def _on_grid_size_changed(self, size):
        self.grid_delegate.thumb_size = size
        self.grid.doItemsLayout()
        self.settings.setValue("grid_thumb_size", size)

    # -- sorting, analysis and bursts ---------------------------------------

    def set_sort_order(self, order):
        self.sort_order = order
        self.settings.setValue("sort_order", order)
        self.gallery_toolbar.set_sort_order(order)
        self._apply_order()
        self._request_analysis()

    def _apply_order(self):
        """Sort the photos again, staying on the same ones."""
        current = self.model.photo(self.current_row).path if self.current_row >= 0 else None
        compared = [self.model.photo(row).path for row in self.compare_rows]
        if not self.model.reorder(SORT_KEYS[self.sort_order]):
            return
        self.compare_rows = [self.model.row_of(path) for path in compared]
        if current is None:
            return
        self.current_row = self.model.row_of(current)
        if self.live_edit is None:
            self.go_to(self.current_row)  # for the filmstrip, and what to prefetch
        else:
            self._update_counter()  # a slider is being dragged: go_to() would end that
        if self.tabs.currentIndex() == GALLERY_TAB:
            self.grid.scrollTo(self.grid.currentIndex())

    def set_bursts(self, on):
        self.show_bursts = on
        self.settings.setValue("show_bursts", on)
        self.gallery_toolbar.bursts_button.setChecked(on)
        self.toast.show_message("Showing bursts" if on else "Not showing bursts")
        self._show_analysis()
        self._request_analysis()

    def _on_focus_filter_changed(self, value):
        self.proxy.set_focus(value)
        self._request_analysis()

    def _request_analysis(self):
        """Ask for what the current options need, and nothing more: reading every file of a card takes a while."""
        photos = self.model.photos
        capture = photos if self.sort_order == "date" or self.show_bursts else []
        if self.sort_order == "sharpness" or self.gallery_toolbar.focus_combo.currentData() != StatusFilterProxy.ALL:
            images = photos
        else:
            images = [photo for photo in photos if photo.burst]  # to find their sharpest frame
        self.analyzer.request(capture, images)

    def _on_analyzed(self, path, results):
        row = self.model.row_of(path)
        if row == -1:
            return
        photo = self.model.photo(row)
        for name, value in results.items():
            setattr(photo, name, value)
        if not self.analysis_timer.isActive():
            self.analysis_timer.start()

    def _show_analysis(self):
        self.analysis_timer.stop()
        if self.show_bursts:
            bursts.assign(self.model.photos)
        else:
            for photo in self.model.photos:
                photo.burst = None
        self.model.analysis_changed()
        self._refresh_view()

    def _on_analysis_finished(self):
        self._show_analysis()
        self._apply_order()  # only now: the photos shouldn't keep moving while they are looked at
        self._request_analysis()  # bursts are known now, whose frames get scored

    def keep_one_of_burst(self):
        """Keep the current photo and reject the rest of its burst, as one undo step."""
        if self.current_row < 0:
            return
        current = self.model.photo(self.current_row)
        if current.burst is None:
            self.toast.show_message("This photo isn't part of a burst" if self.show_bursts else "Show bursts first (B)")
            return
        rows = [row for row, photo in enumerate(self.model.photos) if photo.burst and photo.burst.id == current.burst.id]
        first_visible = self.proxy.mapFromSource(self.model.index(self.current_row)).row()
        statuses = {row: KEEP if row == self.current_row else REJECT for row in rows}
        self._push_undo(self.set_marks(statuses))
        self.toast.show_message(f"Kept {current.name}, rejected {len(rows) - 1}")
        if self.tabs.currentIndex() == GALLERY_TAB:
            self._reselect_in_gallery(first_visible)
        elif not self.compare_rows and (following := self._cull_neighbor(max(rows), 1)) is not None:
            self.go_to(following)

    # -- long jobs behind a dialog ------------------------------------------

    def _run_work(self, title, text, count, work):
        """Run `work(progress)` behind a modal progress dialog; returns its result, None if it broke down."""
        progress = QProgressDialog(text, "Stop", 0, count, self)
        progress.setWindowTitle(title)
        progress.setWindowModality(Qt.WindowModality.ApplicationModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)
        thread = WorkThread(work, self)
        thread.progressed.connect(progress.setValue)
        progress.canceled.connect(thread.stop)
        loop = QEventLoop()
        thread.finished.connect(loop.quit)
        self.moving = True  # same guard as moving files: no folder change meanwhile
        thread.start()
        loop.exec()
        self.moving = False
        progress.close()
        thread.deleteLater()
        if thread.failure:
            inform(self, title, escape(thread.failure))
        return thread.result

    # -- importing ----------------------------------------------------------

    def import_photos(self):
        """Copy photos from a memory card to a folder, and to a backup if asked, then open that folder."""
        if self.moving:
            return
        dialog = ImportDialog(
            self, find_card(), self.settings.value("import_destination", ""), self.settings.value("import_backup", "")
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.settings.setValue("import_destination", dialog.destination)
        self.settings.setValue("import_backup", dialog.backup)
        groups, destinations = dialog.groups, dialog.destinations
        self._finish_crop(apply=True)
        self.save_session()
        result = self._run_work(
            "Import photos", "Copying and verifying…", len(groups), lambda progress: execute_import(groups, destinations, progress)
        )
        if result is None:
            return
        if result.errors:
            inform(
                self,
                "Some files were not imported",
                f"Copied {result.copied} file(s). These failed, and their originals are untouched:",
                details=result.errors,
            )
        if os.path.isdir(dialog.destination):
            self.load_folder(dialog.destination, keep_state=True)
        skipped = f", {result.skipped} already there" if result.skipped else ""
        stopped = "Stopped: " if result.cancelled else ""
        self.toast.show_message(f"{stopped}{result.copied} file(s) copied and verified{skipped}", 4000)

    # -- the Trash ----------------------------------------------------------

    def trash_rejects(self):
        """Move the photos in _REJECTS to the system Trash, with their pairs and sidecars. Nothing is ever deleted."""
        if not self.folder or self.moving:
            return
        self._finish_crop(apply=True)
        self._end_compare()
        self.save_session()
        photos = plan_trash(self.model.photos)
        waiting = sum(1 for photo in self.model.photos if photo.status == REJECT and photo.location != REJECT)
        note = f"{waiting} rejected photo(s) aren't in {REJECT_DIR} yet: Process Files moves them there."
        if not photos:
            self.toast.show_message(note if waiting else f"No rejected photos in {REJECT_DIR}", 4000)
            return
        text = (
            f"<b>{len(photos)}</b> rejected photo(s) in {REJECT_DIR}, <b>{size_text(trash_size(photos))}</b> with their "
            "RAW+JPEG pairs and XMP sidecars, will go to the Trash. They can be put back from there until it is emptied."
        )
        if waiting:
            text += f"<br><br>{note}"
        if MessageDialog(self, "Move to the Trash", text, confirm_text="Move to Trash").exec() != QDialog.DialogCode.Accepted:
            return
        result = self._run_work(
            "Move to the Trash", "Moving to the Trash…", len(photos), lambda progress: execute_trash(photos, progress)
        )
        if result is None:
            return
        if result.trashed:
            self._forget_trashed(set(result.trashed))
        if result.errors:
            inform(
                self,
                "Some photos stayed where they are",
                f"Moved {len(result.trashed)} of {len(photos)} photo(s) to the Trash. These could not be moved:",
                details=result.errors,
            )
        else:
            self.toast.show_message(f"Moved {len(result.trashed)} photo(s) to the Trash")

    def _forget_trashed(self, gone):
        """Photos are identified by path: nothing may keep referring to these."""
        rows = range(self.model.rowCount())
        current = self.model.photo(self.current_row) if self.current_row >= 0 else None
        select = current.path if current else None
        if current and current.path in gone:  # the next photo left, or else the one before
            after = [r for r in rows if r > self.current_row and self.model.photo(r).path not in gone]
            before = [r for r in rows if r < self.current_row and self.model.photo(r).path not in gone]
            select = self.model.photo(after[0]).path if after else self.model.photo(before[-1]).path if before else None
        files = [path for photo in self.model.photos if photo.path in gone for path in photo.files]
        self.moved_keys.update(relative_key(self.folder, path) for path in files)
        self.loader.forget(files)
        for path in gone:
            self.editor.forget(path)
            self.edit_thumbnails.forget(path)
        self.xmp_dirty -= gone
        self.undo_stack = [kept for changes in self.undo_stack if (kept := [c for c in changes if c[0] not in gone])]
        self.current_row = -1
        self.model.remove_paths(gone)
        self.save_session()
        with suppress(OSError):  # only goes if nothing is left in it
            os.rmdir(os.path.join(self.folder, REJECT_DIR))
        self.plugins.emit("photos_trashed", sorted(gone))
        self.load_folder(self.folder, select=select and relative_key(self.folder, select), keep_state=True)

    # -- processing --------------------------------------------------------

    def process_files(self):
        """Move photos to the folder of their mark, then fill the export folder if asked to.

        Edited photos are exported as JPEGs; unedited ones get their JPEG
        (if they have one) copied. Rejected photos are not exported, nor
        unmarked ones when only keeps are, and what was exported of
        rejected photos earlier is removed.
        """
        if not self.folder:
            return
        self._finish_crop(apply=True)
        self._end_compare()
        self.save_session()
        moves = plan_moves(self.folder, self.model.photos)
        # Whatever the scope: the dialog is where it can be changed.
        anything = self._plan_exports(keeps_only=False)
        no_jpeg = [photo.path for photo in without_export(self.model.photos, self.export_scope == "keeps")]
        removable, _changed = rejected_exports(self.model.photos, self.folder)
        if not moves and not anything and not removable:
            note = f" · {len(no_jpeg)} photo(s) have no JPEG to export" if no_jpeg else ""
            self.toast.show_message(f"Everything is already sorted{note}")
            return

        keeps = sum(1 for m in moves if m.status == KEEP)
        rejects = sum(1 for m in moves if m.status == REJECT)
        unmarked = len(moves) - keeps - rejects
        lines = []
        if moves:
            lines.append(f"<b>{len(moves)}</b> photo(s) will be moved:")
        if keeps:
            lines.append(f"&nbsp;&nbsp;• {keeps} → <b>_KEEPS</b>")
        if rejects:
            lines.append(f"&nbsp;&nbsp;• {rejects} → <b>_REJECTS</b>")
        if unmarked:
            lines.append(f"&nbsp;&nbsp;• {unmarked} → folder root (unmarked)")
        if moves:
            lines.append("<br>RAW+JPEG pairs and XMP sidecars move together. Existing files are never overwritten.")

        if anything or removable:
            export_label, export_text, _can = self._describe_exports(self.export_scope)
        else:
            export_label = export_text = None
            if no_jpeg:
                lines.append(f"{len(no_jpeg)} photo(s) have no JPEG and won't be exported: {_names(no_jpeg)}")

        dialog = ProcessDialog(
            self, "<br>".join(lines), export_label, export_text,
            scope=self.export_scope, describe=self._describe_exports, edit_settings=self.edit_export_settings,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        if dialog.export_check is not None and dialog.scope != self.export_scope:
            self.export_scope = dialog.scope
            self.settings.setValue("export_scope", self.export_scope)

        if moves:
            result = self._move_files(moves)
            moved = sum(1 for move in moves if move.src in result.moved)
            if result.errors:
                inform(
                    self,
                    "Some files were not moved",
                    f"Moved {moved} of {len(moves)} photo(s). These failed:",
                    details=result.errors,
                )
            if result.cancelled:
                self.toast.show_message(f"Stopped after moving {moved} photo(s)")
                return
            summary_text = f"Moved {moved} photo(s)"
        else:
            summary_text = "Nothing to move"

        if not dialog.export_requested or not self._export_folder_ok():
            self.toast.show_message(summary_text)
            return

        where = self.export_settings.folder
        # First, since a photo about to be exported may take over the name of a removed export.
        removed, remove_errors = remove_rejected_exports(self.model.photos, self.folder)
        self.save_session()
        if remove_errors:
            inform(
                self,
                "Some exports were not removed",
                "These exports of rejected photos are still there:",
                details=remove_errors,
            )

        # Planned again: the photos may have just moved.
        exports = self._plan_exports()
        jobs = [(job.path, job.edit, self._lut_path(job.edit), job.name) for job in exports]
        thread = self._run_export(jobs, "Process files") if jobs else None
        if thread is not None and thread.errors:
            inform(
                self,
                "Some photos were not exported",
                f"Exported {len(thread.exported)} of {len(jobs)} photo(s) to {where}. These failed:",
                details=thread.errors,
            )
        else:
            done = [summary_text]
            if thread is not None:
                done.append(f"exported {len(thread.exported)} to {where}")
            if removed:
                done.append(f"removed {len(removed)} export(s) of rejected photos")
            self.toast.show_message(", ".join(done))

    def _describe_exports(self, scope):
        """(label, details, anything to do) of the export part of the Process Files dialog, for a scope."""
        keeps_only = scope == "keeps"
        settings = self.export_settings
        where = escape(settings.folder)
        exports = self._plan_exports(keeps_only)
        no_jpeg = [photo.path for photo in without_export(self.model.photos, keeps_only)]
        removable, changed = rejected_exports(self.model.photos, self.folder)
        edited = sum(1 for job in exports if job.edit)
        if exports:
            label = f"Export {len(exports)} photo(s) to {settings.folder}"
        elif removable:
            label = f"Update {settings.folder}"
        else:
            label = "Nothing to export" + (" (no keeps)" if keeps_only else "")
        details = []
        if edited:
            details.append(f"&nbsp;&nbsp;• {edited} edited → rendered as JPEG")
        if len(exports) - edited:
            how = "rendered as JPEG" if settings.resizes else "JPEG copied"
            details.append(f"&nbsp;&nbsp;• {len(exports) - edited} without edits → {how}")
        if removable:
            details.append(f"&nbsp;&nbsp;• {len(removable)} earlier export(s) of rejected photos → removed")
        if exports:
            details.append(f"Into <b>{where}</b>: {escape(settings.describe())}. Earlier exports of the same photos are replaced.")
        if changed:
            details.append(f"{len(changed)} export(s) of rejected photos changed since and stay: {_names(changed)}")
        if no_jpeg:
            details.append(f"{len(no_jpeg)} photo(s) have no JPEG and won't be exported: {_names(no_jpeg)}")
        missing = [job.path for job in exports if self._lut_missing(job.edit)]
        if missing:
            details.append(
                f"<b>{len(missing)} photo(s) use a film simulation that isn't in the LUT folder "
                f"and would be exported without it:</b> {_names(missing)}"
            )
        problem = settings.problem(self.folder)
        if problem:
            details.append(f"<b>{escape(problem)}</b>")
        return label, "<br>".join(details), bool(exports or removable) and problem is None

    def _plan_exports(self, keeps_only=None):
        """What Process Files exports, for the scope chosen (or `keeps_only`)."""
        if keeps_only is None:
            keeps_only = self.export_scope == "keeps"
        # By name, whatever the order shown: which photo gets `IMG_1 (1).jpg` must not depend on it.
        return plan_exports(
            sorted(self.model.photos, key=by_name), keeps_only, self.export_settings.pattern, os.path.basename(self.folder)
        )

    def _move_files(self, moves):
        """Run the moves behind a modal progress dialog and update the model; returns the MoveResult."""
        progress = QProgressDialog("Moving files…", "Stop", 0, len(moves), self)
        progress.setWindowTitle("Process files")
        progress.setWindowModality(Qt.WindowModality.ApplicationModal)
        # Shown at once: being modal is what keeps marks from changing mid-move.
        progress.setMinimumDuration(0)
        progress.setValue(0)

        thread = MoveThread(moves, self)
        thread.progressed.connect(progress.setValue)
        progress.canceled.connect(thread.stop)
        loop = QEventLoop()
        thread.finished.connect(loop.quit)
        self.moving = True
        thread.start()
        loop.exec()
        self.moving = False
        progress.close()
        result = thread.result
        thread.deleteLater()

        # Locations come from the plan: once Stop is pressed the window is
        # usable again while the last file finishes, so marks may have changed.
        locations = {path: move.status for move in moves for path in (move.src, *move.companions)}
        self.model.apply_moves({old: (new, locations[old]) for old, new in result.moved.items()})
        self.loader.rename_paths(result.moved)
        self.editor.rename_paths(result.moved)
        self.edit_thumbnails.rename_paths(result.moved)
        self.xmp_dirty = {result.moved.get(path, path) for path in self.xmp_dirty}
        self.moved_keys.update(relative_key(self.folder, old) for old in result.moved)
        self.undo_stack.clear()  # undo entries refer to the old paths
        self.save_session()
        self._on_statuses_changed([])
        self.analyzer.restart()  # what was running has the old paths; results are kept by file, so little is lost
        self._request_analysis()
        self._watch(self.folder)  # _KEEPS / _REJECTS may have just been created
        if self.current_row >= 0:
            self.header.set_folder(self.folder, self.model.photo(self.current_row).display_name)
        if result.moved:
            self.plugins.emit("files_moved", dict(result.moved))
        return result

    # -- window ------------------------------------------------------------

    def _restore_window(self):
        geometry = self.settings.value("geometry")
        if geometry is None or not self.restoreGeometry(geometry):
            self.resize(1400, 900)

    def closeEvent(self, event):
        if self.moving:
            event.ignore()  # files are being moved; the progress dialog has a Stop button
            return
        self._finish_crop(apply=True)
        self.plugins.emit("closing")
        self.save_session()
        self.settings.setValue("geometry", self.saveGeometry())
        if self.current_row >= 0:
            self.settings.setValue("last_file", relative_key(self.folder, self.model.photo(self.current_row).path))
        self.loader.shutdown()
        self.editor.shutdown()
        self.peaker.shutdown()
        self.tones.shutdown()
        self.edit_thumbnails.shutdown()
        self.analyzer.shutdown()
        super().closeEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.toast.isVisible():
            self.toast.show_message(self.toast.text())

    def dragEnterEvent(self, event):
        urls = event.mimeData().urls()
        if urls and urls[0].isLocalFile() and os.path.isdir(urls[0].toLocalFile()):
            event.acceptProposedAction()

    def dropEvent(self, event):
        self.load_folder(event.mimeData().urls()[0].toLocalFile())
