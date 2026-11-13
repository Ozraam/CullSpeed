import json
import math
import os
import platform
import time
from html import escape

import rawpy
from PyQt6.QtCore import (
    PYQT_VERSION_STR,
    QT_VERSION_STR,
    QEvent,
    QFileSystemWatcher,
    QItemSelectionModel,
    QRect,
    QSettings,
    QSize,
    QStandardPaths,
    Qt,
    QTimer,
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
    QPushButton,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import __version__, bursts, cropping, develop, exportsettings, lut, shortcuts, xmp
from .analysis import PhotoAnalyzer
from .constants import (
    APP_NAME,
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
    is_raw,
)
from .editing import EditRenderer, EditThumbnails, exposure_shift
from .editpanel import EditPanel
from .exif import summary
from .loader import ImageLoader
from .models import SORT_KEYS, PhotoModel, StatusFilterProxy, relative_key, save_session
from .peaking import FocusPeaker
from .plugins import PhotoInfo, PluginManager
from .presets import PresetStore
from .processing import ProcessingMixin
from .scanning import FolderScanner
from .shortcutsdialog import ShortcutsDialog, ShortcutsEditor
from .thumbcache import ThumbnailCache
from .tones import ToneReader
from .views import CompareView, ImageView
from .widgets import (
    GalleryToolbar,
    HeaderBar,
    MessageDialog,
    NameDialog,
    StatsBar,
    ThumbnailDelegate,
    Toast,
    inform,
    make_thumbnail_view,
)

CULL_TAB, GALLERY_TAB = 0, 1
FILMSTRIP_THUMB_SIZE = 64
COMPARE_MAX = 4
CROP_CACHE_SIZE = 8  # cropped previews and full-resolution images kept
# Changes in the folder are looked for this long after they happen, so that a burst of files is read once;
# after the app's own session writes, a while longer.
REFRESH_DELAY_MS = 800
SCAN_PROGRESS_AFTER = 0.3  # seconds of reading a folder before saying how far it got
OWN_WRITE_SECONDS = 1.0
OWN_WRITE_REFRESH_MS = 4000

# How keep and reject reach other editors, as no flag is common to them (see xmp.py).
XMP_MARKS = (
    ("off", "Not at all"),
    ("rating", "Rejected as rating −1 (darktable, Bridge)"),
    ("labels", "Keep green, reject red (Lightroom, Capture One…)"),
)
MARK_LABELS = {KEEP: "Green", REJECT: "Red"}


class TamisApp(ProcessingMixin, QMainWindow):
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

        self.scanner = FolderScanner(self)
        self.scanner.scanned.connect(self._on_scanned)
        self.scanner.progressed.connect(self._on_scan_progress)

        # New files (an import or a tethered shot landing in the folder) show up by themselves.
        self.watcher = QFileSystemWatcher(self)
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setSingleShot(True)
        self.refresh_timer.timeout.connect(lambda: self.refresh(auto=True))
        self.watcher.directoryChanged.connect(self._on_directory_changed)
        self.saved_at = -math.inf  # time.monotonic() of the last session written in the folder
        self.scan_started = -math.inf

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
        self.edit_panel.process_update_requested.connect(self.update_process)
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
        menu.addSeparator()
        menu.addAction(f"About {APP_NAME}…", self.show_about)
        return menu

    def show_about(self):
        """The app's version, and those of what it is built on, for bug reports."""
        libraw = ".".join(str(part) for part in rawpy.libraw_version)
        text = (
            f"Version <b>{escape(__version__)}</b><br><br>"
            f"Python {platform.python_version()} · Qt {QT_VERSION_STR} · PyQt {PYQT_VERSION_STR} · LibRaw {libraw}<br>"
            f"{escape(platform.platform(terse=True))}"
        )
        inform(self, APP_NAME, text)

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

        The folder is read in the background (see FolderScanner): the photos of
        the folder open until then stay usable, and opening another folder
        meanwhile supersedes this one. `keep_state`, for the open folder, keeps
        cached images and undo.
        """
        if self.moving:
            return
        # A folder dropped on the window ends with a separator: paths are joined to it and compared.
        folder = os.path.normpath(os.path.abspath(folder))
        self._finish_crop(apply=True)
        self.save_session()
        self.refresh_timer.stop()
        self.scan_started = time.monotonic()
        self.scanner.scan(folder, self.session_fallback_dir, ("open", folder, select, keep_state, self.model.revision))

    def _on_scan_progress(self, folder, count):
        if count and time.monotonic() - self.scan_started > SCAN_PROGRESS_AFTER:  # only when it takes a while
            self.toast.show_message(f"Reading {os.path.basename(folder) or folder}… {count} photo(s)", 3000)

    def _on_scanned(self, result):
        kind, folder, *details = result.request
        self._report_damaged_sessions(result.problems)
        if kind == "open":
            self._open_scanned(result, *details)
        elif folder == self.folder and not self.moving:
            self._apply_folder_changes(result, *details)

    def _open_scanned(self, result, select, keep_state, revision):
        folder, photos = result.folder, result.photos
        if result.error:
            inform(self, "Cannot open folder", escape(f"{folder}: {result.error}"))
            return
        same_folder = keep_state and folder == self.folder
        if same_folder and self.model.revision != revision:
            # Marked while being read: what was read is out of date, save it and read again.
            self.load_folder(folder, select, keep_state)
            return
        if self.moving:  # (cannot happen: no long job starts while a folder is being read)
            return
        self.save_session()  # what was marked meanwhile in the folder open until now
        if self.toast.text().startswith("Reading "):
            self.toast.hide()

        self.live_edit = None
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
        self.analyzer.apply_known(photos, result.file_keys)
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
        """Pick up photos added to or removed from the folder, keeping marks, the selection and the current photo.

        Only new photos are read (in the background), and the others stay as they are.
        """
        if not self.folder or self.moving or self.scanner.busy:
            return
        self.loader.retry_failed_thumbnails()
        known = {photo.path: photo.companions for photo in self.model.photos}
        self.scanner.refresh(self.folder, known, self.session_fallback_dir, ("refresh", self.folder, auto))

    def _apply_folder_changes(self, result, auto):
        changes = result.changes
        if result.error or not changes:
            if not auto:
                self.toast.show_message("No new photos")
            return
        # Read from what was shown when the refresh started: since then, a photo may have gone another way.
        changes.removed &= {photo.path for photo in self.model.photos}
        changes.added = [photo for photo in changes.added if self.model.row_of(photo.path) == -1]
        # A RAW landing after its JPEG makes one photo of them: it keeps the JPEG's marks.
        replaced = {photo.path: photo for photo in self.model.photos if photo.path in changes.removed}
        for photo in changes.added:
            earlier = next((replaced[path] for path in photo.companions if path in replaced), None)
            if earlier is not None and (photo.status, photo.rating, photo.label) == (None, 0, None):
                photo.status, photo.rating, photo.label = earlier.status, earlier.rating, earlier.label
        before = {path for photo in self.model.photos for path in photo.files}
        self.analyzer.apply_known(changes.added, result.file_keys)
        self._update_photos(changes)
        after = {path for photo in self.model.photos for path in photo.files}
        added, removed = len(after - before), len(before - after)
        if not (added or removed):
            if not auto:
                self.toast.show_message("No new photos")
            return
        self._watch(self.folder)  # _KEEPS / _REJECTS may have just appeared
        parts = [f"{added} new file(s)" if added else "", f"{removed} gone" if removed else ""]
        self.toast.show_message("Folder updated · " + ", ".join(part for part in parts if part))
        self.plugins.emit("folder_refreshed", self.folder)

    def _update_photos(self, changes, select=None):
        """Add and remove photos of the open folder in place (FolderChanges), staying on the current photo.

        Photos are identified by path: nothing may keep referring to removed ones.
        `select`, a path, is the photo to show when the current one goes.
        """
        current = self.model.photo(self.current_row).path if self.current_row >= 0 else None
        if current in changes.removed and select is None:
            # The next photo that stays, or else the one before.
            rows = range(self.model.rowCount())
            after = [r for r in rows if r > self.current_row and self.model.photo(r).path not in changes.removed]
            before = [r for r in rows if r < self.current_row and self.model.photo(r).path not in changes.removed]
            select = self.model.photo(after[0] if after else before[-1]).path if after or before else None
        compared = [self.model.photo(row).path for row in self.compare_rows]
        gone = changes.removed
        if gone:
            files = [path for photo in self.model.photos if photo.path in gone for path in photo.files]
            self.loader.forget(files)
            for path in gone:
                self.editor.forget(path)
                self.edit_thumbnails.forget(path)
            self.xmp_dirty -= gone
            self.undo_stack = [kept for undo in self.undo_stack if (kept := [c for c in undo if c[0] not in gone])]
            if set(compared) & gone:
                self._end_compare(show=False)
                compared = []
        for photo in changes.added:
            if photo.xmp_pending or (self.xmp_marks == "rating" and photo.status == REJECT):
                self.xmp_dirty.add(photo.path)

        self.model.update_photos(changes, SORT_KEYS[self.sort_order])
        self.compare_rows = [self.model.row_of(path) for path in compared]
        if changes.added or gone:
            self._show_analysis()  # bursts may have changed
            self._request_analysis()
        follow = current if current not in gone else select
        if follow is None or self.model.row_of(follow) == -1:
            self.current_row = -1
            if not self.model.rowCount():
                self.image_view.set_image(None, "No photos in this folder")
                self.header.set_counter("")
            elif current in gone:
                self.go_to(0)
            return
        row = self.model.row_of(follow)
        if follow == current:
            self.current_row = row  # the same photo: only its row moved
            if self.live_edit is None:
                self.go_to(row)  # for the filmstrip, and what to prefetch
            else:
                self._update_counter()  # a slider is being dragged: go_to() would end that
        else:
            self.go_to(row)
            self._keep_current_in_filter()

    def _on_directory_changed(self, path):
        if path == self.folder and time.monotonic() - self.saved_at < OWN_WRITE_SECONDS:
            # Most likely the session that was just saved: saving follows every mark, so look
            # later, once for all of them, rather than reading the folder after each one.
            if not self.refresh_timer.isActive():
                self.refresh_timer.start(OWN_WRITE_REFRESH_MS)
        else:
            self.refresh_timer.start(REFRESH_DELAY_MS)

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
            with open(os.path.join(os.path.expanduser("~"), LEGACY_GLOBAL_CONFIG_FILENAME), encoding="utf-8") as file:
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
            self.saved_at = time.monotonic()
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

    def update_process(self):
        """Develop the current RAW the current way, with the exposure that keeps it as bright as it was."""
        if self.current_row < 0:
            return
        photo = self.model.photo(self.current_row)
        edit = photo.edit
        old = edit.get("process", develop.PROCESS)
        if not is_raw(photo.path) or old >= develop.PROCESS:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            shift = exposure_shift(photo.path, old)
        except Exception as exc:  # a RAW LibRaw can't read: nothing to compare, nothing changed
            self.toast.show_message(f"Could not develop {photo.name}: {exc}", 4000)
            return
        finally:
            QApplication.restoreOverrideCursor()
        exposure = edit.get("exposure", 0.0) + shift
        self.live_edit = None
        self._set_edit([self.current_row], develop.clean({**edit, "process": develop.PROCESS, "exposure": exposure}))
        self.toast.show_message(f"New development · exposure {shift:+.2f} EV, to keep the photo as bright")

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
        self.scanner.shutdown()
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
