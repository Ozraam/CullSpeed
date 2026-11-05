import json
import os
import threading
from html import escape

from PyQt6.QtCore import (
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
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QMainWindow,
    QProgressDialog,
    QPushButton,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import cropping, develop, lut, xmp
from .constants import (
    EXPORT_DIR,
    FALLBACK_SESSIONS_DIR,
    KEEP,
    KEEP_DIR,
    LEGACY_GLOBAL_CONFIG_FILENAME,
    PLUGINS_DIR,
    PREFETCH_AHEAD,
    PREFETCH_BEHIND,
    REJECT,
    REJECT_DIR,
    SESSION_FILENAME,
    UNDO_LIMIT,
)
from .editing import EditRenderer, export_photo
from .editpanel import EditPanel
from .exif import summary
from .fileops import copy_export, execute_moves, is_jpeg, jpeg_file, plan_exports, plan_moves
from .loader import ImageLoader
from .models import PhotoModel, StatusFilterProxy, photo_files, relative_key, save_session, scan_folder
from .plugins import PhotoInfo, PluginManager
from .widgets import (
    CompareView,
    GalleryToolbar,
    HeaderBar,
    ImageView,
    ProcessDialog,
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

    def __init__(self, jobs, dest_dir, parent=None):
        super().__init__(parent)
        # [(path, edit, LUT path, name)]: an unedited JPEG is copied, anything else rendered; name None picks a free one
        self.jobs = jobs
        self.dest_dir = dest_dir
        self.exported = []
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
                if edit or not is_jpeg(path):
                    self.exported.append(export_photo(path, edit, lut_path, self.dest_dir, tables, name=name))
                else:
                    self.exported.append(copy_export(path, self.dest_dir, name))
            except Exception as exc:  # one bad photo must not stop the others
                self.errors.append(f"{os.path.basename(path)}: {exc}")
        self.progressed.emit(len(self.jobs))


class TamisApp(QMainWindow):
    def __init__(self, plugins_dir=None):
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
        self.crop_cache = {}  # (pixmap key, crop) -> cropped pixmap, for what the renderer doesn't crop
        self.keeps_only = False  # the Cull view only shows and steps through keeps
        self.lut_folder = self.settings.value("lut_folder", "")
        self.luts = {}  # LUT id -> path, from lut_folder
        self.show_info = self.settings.value("show_info", True, type=bool)
        self.undo_stack = []
        self.moving = False

        self.loader = ImageLoader(self)
        self.loader.preview_loaded.connect(self._on_image_ready)
        self.loader.preview_failed.connect(lambda path, _error: self._on_image_ready(path))
        self.loader.full_loaded.connect(self._on_image_ready)
        self.loader.full_failed.connect(self._on_full_failed)

        self.editor = EditRenderer(self)
        self.editor.rendered.connect(self._on_image_ready)
        self.editor.failed.connect(self._on_edit_failed)

        self.model = PhotoModel(self.loader, self)
        self.model.statuses_changed.connect(self._on_statuses_changed)
        self.model.ratings_changed.connect(lambda paths: self.xmp_dirty.update(paths))
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
        self._build_shortcuts()
        self._load_luts(quiet=True)
        self._restore_window()
        self.plugins = PluginManager(self, plugins_dir or os.path.join(data_dir, PLUGINS_DIR))
        self.plugins_button.setMenu(self.plugins.menu)
        self.plugins.load()
        QTimer.singleShot(0, self._restore_last_folder)

    # -- UI ----------------------------------------------------------------

    def _build_ui(self):
        self.header = HeaderBar()
        self.header.open_folder_requested.connect(self.choose_folder)
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
        self.gallery_toolbar = GalleryToolbar(grid_size)
        self.gallery_toolbar.filter_changed.connect(self.proxy.set_filter)
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
        corner = QWidget()
        corner.setObjectName("TabCorner")
        corner_layout = QHBoxLayout(corner)
        corner_layout.setContentsMargins(0, 0, 10, 0)
        corner_layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        corner_layout.addWidget(self.keeps_button)
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

    def _build_shortcuts(self):
        self.cull_shortcuts = []

        def bind(keys, slot, cull_only=False):
            for key in keys:
                shortcut = QShortcut(QKeySequence(key), self)
                shortcut.activated.connect(slot)
                if cull_only:
                    self.cull_shortcuts.append(shortcut)

        bind(["P", "1"], lambda: self.mark(KEEP))
        bind(["X", "3"], lambda: self.mark(REJECT))
        bind(["U", "2", "Backspace"], lambda: self.mark(None))
        # Stars and colour labels follow Lightroom's 0–5 and 6–9, with ⌘/Ctrl
        # since the bare digits already keep, unmark and reject.
        for stars in range(6):
            bind([f"Ctrl+{stars}"], lambda stars=stars: self.rate(stars))
        for key, label in zip("6789", xmp.LABELS, strict=False):
            bind([f"Ctrl+{key}"], lambda label=label: self.set_label(label))
        bind([QKeySequence.StandardKey.Undo], self.undo)
        bind([QKeySequence.StandardKey.Open], self.choose_folder)
        bind(["Ctrl+R", "F5"], self.refresh)
        bind(["G"], self.toggle_tab)
        bind(["C"], self.toggle_compare)
        bind(["I"], self.toggle_info)
        bind(["E"], self.toggle_edit_panel)
        bind(["R"], self.toggle_crop)
        bind(["Ctrl+C", "Ctrl+Shift+C"], self.copy_edit)
        bind(["Ctrl+V", "Ctrl+Shift+V"], self.paste_edit)
        bind(["Ctrl+Shift+E"], self.export_photos)

        # In the gallery the arrow keys move through the grid instead.
        bind(["Up"], lambda: self.mark(KEEP), cull_only=True)
        bind(["Down"], lambda: self.mark(REJECT), cull_only=True)
        bind(["Right", "L", "Space"], lambda: self.step(1), cull_only=True)
        bind(["Left", "H"], lambda: self.step(-1), cull_only=True)
        bind(["Home"], lambda: self.go_to_end(first=True), cull_only=True)
        bind(["End"], lambda: self.go_to_end(first=False), cull_only=True)
        bind(["N"], self.next_unmarked, cull_only=True)
        bind(["K"], self.toggle_keeps_only, cull_only=True)
        bind(["Z"], self.toggle_zoom, cull_only=True)
        bind(["Escape"], self.escape, cull_only=True)
        bind(["Return", "Enter"], self._confirm_crop, cull_only=True)

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
        self.xmp_dirty = {photo.path for photo in photos if photo.xmp_pending}  # try those again
        self._end_compare(show=False)
        self.folder = folder
        self.current_row = -1
        if not same_folder:
            self._set_keeps_only(False)
        self.model.set_photos(photos)
        self.settings.setValue("last_folder", folder)
        self.header.set_folder(folder)
        self._watch(folder)
        event = "folder_refreshed" if same_folder else "folder_opened"

        if not photos:
            self.image_view.set_image(None, "No photos in this folder")
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
                xmp.write(photo.files, photo.rating, photo.label, listings[directory])
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
        self.edit_panel.set_edit(photo.edit)
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
        # Previews are downscaled, so while zoomed the photos load at full resolution.
        self.loader.ensure_full([self.model.photo(row).path for _, row in shown], load=zoomed)
        for view, row in shown:
            self._show_in(view, row, zoomed)

    def _show_in(self, view, row, zoomed):
        photo = self.model.photo(row)
        # The crop tool shows the whole photo. Otherwise what the renderer didn't
        # make is cropped here, so a photo keeps its frame whatever is shown of it.
        crop = None if self.cropping else self._shown_edit(photo).get("crop")
        pixmap = self._edited_pixmap(photo)
        if pixmap is None:
            pixmap = self._cropped(self.loader.preview(photo.path), crop)
        if pixmap is not None:
            view.set_image(pixmap, native_size=self._framed_size(self.loader.native_size(photo.path), crop), keep_zoom=True)
        else:
            error = self.loader.preview_error(photo.path)
            view.set_image(None, f"Could not load {photo.name}\n\n{error}" if error else "Loading…", keep_zoom=True)
        view.set_status(photo.status)
        view.set_rating(photo.rating, photo.label)
        view.set_info(summary(self.loader.info(photo.path)) if self.show_info else [])
        view.set_active(bool(self.compare_rows) and row == self.current_row)
        if zoomed and pixmap is not None:
            full = self.loader.full_pixmap(photo.path)
            if full is not None:
                view.set_full_image(self._cropped(full, crop))
            else:
                view.set_full_loading(self.loader.full_error(photo.path) is None)

    def _shown_edit(self, photo):
        """The photo's edit, or the one being dragged."""
        return self.live_edit[1] if self.live_edit is not None and self.live_edit[0] == photo.path else photo.edit

    def _edited_pixmap(self, photo):
        """The render of the photo's edit (or of the one being dragged), if it has one."""
        live = self.live_edit is not None and self.live_edit[0] == photo.path
        edit = self._shown_edit(photo)
        if self.cropping:
            edit = {key: value for key, value in edit.items() if key != "crop"}
        if not edit:
            return None
        return self.editor.pixmap(photo.path, edit, self._lut_path(edit), quick=live, same_crop=self.cropping)

    def _cropped(self, pixmap, crop):
        if pixmap is None or not crop:
            return pixmap
        key = (pixmap.cacheKey(), tuple(crop))
        if key not in self.crop_cache:
            top, bottom, left, right = develop.crop_box(pixmap.height(), pixmap.width(), crop)
            self.crop_cache[key] = pixmap.copy(QRect(left, top, right - left, bottom - top))
            while len(self.crop_cache) > CROP_CACHE_SIZE:
                del self.crop_cache[next(iter(self.crop_cache))]  # oldest
        return self.crop_cache[key]

    @staticmethod
    def _framed_size(size, crop):
        if size is None or not crop or not size.isValid():
            return size
        top, bottom, left, right = develop.crop_box(size.height(), size.width(), crop)
        return QSize(right - left, bottom - top)

    def _lut_path(self, edit):
        return self.luts.get(edit.get("lut")) if edit.get("lut") else None

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
                self.edit_panel.set_edit(self.model.photo(self.current_row).edit)
        self.live_edit = None
        self._refresh_view()

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
        self._push_undo(self.model.set_field(rows, "edit", dict(edit)))
        if not edit:
            for row in rows:
                self.editor.forget(self.model.photo(row).path)

    def copy_edit(self):
        """Copy the current photo's look. A crop belongs to its photo's framing: it is not copied."""
        if self.current_row >= 0:
            edit = self.model.photo(self.current_row).edit
            self.edit_clipboard = {key: value for key, value in edit.items() if key != "crop"}
            self.toast.show_message("Edit copied" if self.edit_clipboard else "Copied: no edit")

    def paste_edit(self):
        """Paste the copied look on the target photos, which keep their own crop."""
        rows = self._target_rows()
        if self.edit_clipboard is None:
            self.toast.show_message("Nothing to paste: copy an edit first")
        elif rows:
            values = {}
            for row in rows:
                crop = self.model.photo(row).edit.get("crop")
                values[row] = {**self.edit_clipboard, "crop": crop} if crop else dict(self.edit_clipboard)
            self._push_undo(self.model.set_values("edit", values))
            for row in rows:
                if not values[row]:
                    self.editor.forget(self.model.photo(row).path)
            self.toast.show_message(f"Edit pasted on {len(rows)} photo(s)")

    def reset_edit(self):
        self._finish_crop(apply=False)
        rows = self._target_rows()
        if rows:
            self._set_edit(rows, {})

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
            self._set_edit([self.current_row], {key: value for key, value in edit.items() if key != "crop"})

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
        jobs = [
            (p.path if p.edit else jpeg_file(p) or p.path, p.edit, self._lut_path(p.edit), None)
            for p in (self.model.photo(row) for row in rows)
        ]
        thread = self._run_export(jobs, "Export JPEG")
        if thread.errors:
            inform(
                self,
                "Some photos were not exported",
                f"Exported {len(thread.exported)} of {len(jobs)} photo(s) to {EXPORT_DIR}. These failed:",
                details=thread.errors,
            )
        else:
            self.toast.show_message(f"Exported {len(thread.exported)} photo(s) to {EXPORT_DIR}")

    def _run_export(self, jobs, title):
        """Run an ExportThread behind a modal progress dialog; returns the finished thread."""
        progress = QProgressDialog("Exporting…", "Stop", 0, len(jobs), self)
        progress.setWindowTitle(title)
        progress.setWindowModality(Qt.WindowModality.ApplicationModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)
        thread = ExportThread(jobs, os.path.join(self.folder, EXPORT_DIR), self)
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
        if thread.exported:
            self.plugins.emit("photos_exported", list(thread.exported))
        return thread

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
        self._push_undo(self.model.set_status(rows, status))
        if in_gallery:
            self._select_after_filtering(first_visible)
        elif status is not None and not self.compare_rows and self.current_row == marked_row:
            # (With "Keeps only", a rejected photo has already moved on to the next keep.)
            following = self._cull_neighbor(self.current_row, 1)
            if following is not None:
                self.go_to(following)
            else:
                self._report_end()

    def rate(self, stars):
        rows = self._target_rows()
        if rows:
            self._push_undo(self.model.set_field(rows, "rating", stars))

    def set_label(self, label):
        """Give `label` to the target photos, or remove it if they all have it already."""
        rows = self._target_rows()
        if not rows:
            return
        if all(self.model.photo(row).label == label for row in rows):
            label = None
        self._push_undo(self.model.set_field(rows, "label", label))

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
            self.edit_panel.set_edit(self.model.photo(self.current_row).edit)  # undo, paste, reset…
        if set(rows) & set(self._shown_rows()):
            self._refresh_view()
        self._update_counter()
        self._keep_current_in_filter()
        self.session_timer.start()

    # -- gallery -----------------------------------------------------------

    def _on_tab_changed(self, index):
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

    # -- processing --------------------------------------------------------

    def process_files(self):
        """Move photos to the folder of their mark, then fill _EXPORTS if asked to.

        Edited photos are exported as JPEGs; unedited ones get their JPEG
        (if they have one) copied. Rejected photos are not exported.
        """
        if not self.folder:
            return
        self._finish_crop(apply=True)
        self._end_compare()
        self.save_session()
        moves = plan_moves(self.folder, self.model.photos)
        exports = plan_exports(self.model.photos)
        if not moves and not exports:
            self.toast.show_message("Everything is already sorted")
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

        export_label = export_text = None
        if exports:
            edited = sum(1 for job in exports if job.edit)
            export_label = f"Export {len(exports)} photo(s) to {EXPORT_DIR}"
            details = []
            if edited:
                details.append(f"&nbsp;&nbsp;• {edited} edited → rendered as JPEG")
            if len(exports) - edited:
                details.append(f"&nbsp;&nbsp;• {len(exports) - edited} without edits → JPEG copied")
            details.append("Earlier exports of the same photos are replaced.")
            export_text = "<br>".join(details)

        dialog = ProcessDialog(self, "<br>".join(lines), export_label, export_text)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

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
            elif result.cancelled:
                self.toast.show_message(f"Stopped after moving {moved} photo(s)")
                return
            summary_text = f"Moved {moved} photo(s)"
        else:
            summary_text = "Nothing to move"

        if not dialog.export_requested:
            self.toast.show_message(summary_text)
            return

        # Planned again: the photos may have just moved.
        exports = plan_exports(self.model.photos)
        jobs = [(job.path, job.edit, self._lut_path(job.edit), job.name) for job in exports]
        thread = self._run_export(jobs, "Process files")
        if thread.errors:
            inform(
                self,
                "Some photos were not exported",
                f"Exported {len(thread.exported)} of {len(jobs)} photo(s) to {EXPORT_DIR}. These failed:",
                details=thread.errors,
            )
        else:
            self.toast.show_message(f"{summary_text}, exported {len(thread.exported)} to {EXPORT_DIR}")

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
        self.xmp_dirty = {result.moved.get(path, path) for path in self.xmp_dirty}
        self.moved_keys.update(relative_key(self.folder, old) for old in result.moved)
        self.undo_stack.clear()  # undo entries refer to the old paths
        self.save_session()
        self._on_statuses_changed([])
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
