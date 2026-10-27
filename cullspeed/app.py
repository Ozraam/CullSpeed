import json
import os
import threading
from html import escape

from PyQt6.QtCore import (
    QEventLoop,
    QFileSystemWatcher,
    QItemSelectionModel,
    QSettings,
    QStandardPaths,
    Qt,
    QThread,
    QTimer,
    pyqtSignal,
)
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QMainWindow,
    QProgressDialog,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import lut, xmp
from .constants import (
    EXPORT_DIR,
    KEEP,
    KEEP_DIR,
    LEGACY_GLOBAL_CONFIG_FILENAME,
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
from .fileops import execute_moves, plan_moves
from .loader import ImageLoader
from .models import PhotoModel, StatusFilterProxy, photo_files, relative_key, save_session, scan_folder
from .widgets import (
    CompareView,
    GalleryToolbar,
    HeaderBar,
    ImageView,
    StatsBar,
    ThumbnailDelegate,
    Toast,
    ask,
    inform,
    make_thumbnail_view,
)

CULL_TAB, GALLERY_TAB = 0, 1
FILMSTRIP_THUMB_SIZE = 64
COMPARE_MAX = 4


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
    """Renders edited photos to JPEG off the GUI thread."""

    progressed = pyqtSignal(int)

    def __init__(self, jobs, dest_dir, parent=None):
        super().__init__(parent)
        self.jobs = jobs  # [(path, edit, LUT path)]
        self.dest_dir = dest_dir
        self.exported = []
        self.errors = []
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        tables = {}
        for done, (path, edit, lut_path) in enumerate(self.jobs):
            if self._stop.is_set():
                break
            self.progressed.emit(done)
            try:
                self.exported.append(export_photo(path, edit, lut_path, self.dest_dir, tables))
            except Exception as exc:  # one bad photo must not stop the others
                self.errors.append(f"{os.path.basename(path)}: {exc}")
        self.progressed.emit(len(self.jobs))


class CullSpeedApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("CullSpeed")
        self.setMinimumSize(900, 600)
        self.setAcceptDrops(True)

        self.settings = QSettings()
        # Where marks go when a photo folder is read-only.
        self.session_fallback_dir = os.path.join(
            QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation), "sessions"
        )
        self.session_warning = None  # last session problem shown, so it is shown once
        self.xmp_warned = False
        self.xmp_dirty = set()  # photos whose rating/label still has to be written to XMP
        self.folder = ""
        self.current_row = -1
        self.compare_rows = []  # rows shown side by side; empty outside Compare
        self.live_edit = None  # (path, edit) while a slider is being dragged, before it is kept
        self.edit_clipboard = None
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
        self.image_view.set_image(None, "Open a folder of photos to start\n(or drop one onto this window)")
        self.compare_view = CompareView()
        self.compare_view.zoom_changed.connect(self._on_zoom_changed)
        self.compare_view.activated.connect(lambda index: self.go_to(self.compare_rows[index]))
        self.cull_stack = QStackedWidget()
        self.cull_stack.addWidget(self.image_view)
        self.cull_stack.addWidget(self.compare_view)

        self.filmstrip = make_thumbnail_view(
            self.model, ThumbnailDelegate(FILMSTRIP_THUMB_SIZE, show_names=False, parent=self), horizontal=True
        )
        self.filmstrip.setObjectName("Filmstrip")
        self.filmstrip.setFixedHeight(FILMSTRIP_THUMB_SIZE + ThumbnailDelegate.PADDING * 2 + 16)
        self.filmstrip.clicked.connect(lambda index: self.go_to(index.row()))

        self.edit_panel = EditPanel()
        self.edit_panel.changed.connect(self._on_edit_changed)
        self.edit_panel.copy_requested.connect(self.copy_edit)
        self.edit_panel.paste_requested.connect(self.paste_edit)
        self.edit_panel.reset_requested.connect(self.reset_edit)
        self.edit_panel.export_requested.connect(self.export_photos)
        self.edit_panel.lut_folder_requested.connect(self.choose_lut_folder)
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
        bind(["Ctrl+Shift+C"], self.copy_edit)
        bind(["Ctrl+Shift+V"], self.paste_edit)
        bind(["Ctrl+Shift+E"], self.export_photos)

        # In the gallery the arrow keys move through the grid instead.
        bind(["Up"], lambda: self.mark(KEEP), cull_only=True)
        bind(["Down"], lambda: self.mark(REJECT), cull_only=True)
        bind(["Right", "L", "Space"], lambda: self.step(1), cull_only=True)
        bind(["Left", "H"], lambda: self.step(-1), cull_only=True)
        bind(["Home"], lambda: self.go_to(0), cull_only=True)
        bind(["End"], lambda: self.go_to(self.model.rowCount() - 1), cull_only=True)
        bind(["N"], self.next_unmarked, cull_only=True)
        bind(["Z"], self.toggle_zoom, cull_only=True)
        bind(["Escape"], self.escape, cull_only=True)

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
        self.save_session()
        try:
            photos = scan_folder(folder, self.session_fallback_dir)
        except OSError as exc:
            inform(self, "Cannot open folder", escape(f"{folder}: {exc}"))
            return

        self.live_edit = None
        if not (keep_state and folder == self.folder):
            self.loader.reset()
            self.editor.reset()
            self.session_warning = None
            self.xmp_warned = False
            self.undo_stack.clear()
        self._end_compare(show=False)
        self.folder = folder
        self.current_row = -1
        self.model.set_photos(photos)
        self.settings.setValue("last_folder", folder)
        self.header.set_folder(folder)
        self._watch(folder)

        if not photos:
            self.image_view.set_image(None, "No photos in this folder")
            self.header.set_counter("")
            return

        keys = [relative_key(folder, photo.path) for photo in photos]
        names = [photo.name for photo in photos]
        if select in keys:
            self.go_to(keys.index(select))
        else:
            self.go_to(names.index(select) if select in names else 0)

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
        try:
            path = save_session(self.folder, self.model.photos, self.session_fallback_dir)
        except OSError as exc:
            self._session_problem("failed", f"Could not save marks: {exc.strerror or exc}")
            return
        if path != os.path.join(self.folder, SESSION_FILENAME):
            self._session_problem("fallback", "This folder is read-only: marks are saved in CullSpeed's data folder")
        else:
            self.session_warning = None

    def _session_problem(self, kind, message):
        # Saving runs after every mark: say it once, not on every keystroke.
        if self.session_warning != kind:
            self.session_warning = kind
            self.toast.show_message(message, 5000)

    def _write_xmp(self):
        """Write pending ratings and labels to the photos' XMP sidecars."""
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
            except OSError as exc:
                if not self.xmp_warned:
                    self.xmp_warned = True
                    self.toast.show_message(
                        f"Could not write XMP sidecars ({exc.strerror or exc}): ratings stay in CullSpeed only", 5000
                    )

    # -- navigation --------------------------------------------------------

    def go_to(self, row):
        if not 0 <= row < self.model.rowCount():
            return
        if self.compare_rows and row not in self.compare_rows:
            self._end_compare(show=False)
        self.current_row = row
        photo = self.model.photo(row)
        self.header.set_counter(f"{row + 1} / {self.model.rowCount()}")
        self.header.set_folder(self.folder, photo.display_name)

        index = self.model.index(row)
        self.filmstrip.selectionModel().setCurrentIndex(
            index, QItemSelectionModel.SelectionFlag.ClearAndSelect
        )
        self.filmstrip.scrollTo(index, QAbstractItemView.ScrollHint.PositionAtCenter)

        # Request first: a photo that failed before is retried, and shows "Loading…".
        count = self.model.rowCount()
        nearby = [*range(row + 1, row + 1 + PREFETCH_AHEAD), *range(row - 1, row - 1 - PREFETCH_BEHIND, -1)]
        others = [r for r in self._shown_rows() if r != row] + [r for r in nearby if 0 <= r < count]
        self.loader.ensure_previews(photo.path, [self.model.photo(r).path for r in dict.fromkeys(others)])
        self.live_edit = None
        self.edit_panel.set_edit(photo.edit)
        self._refresh_view()

    def step(self, delta):
        """Next / previous photo, or the next / previous one being compared."""
        if self.compare_rows:
            i = self.compare_rows.index(self.current_row) + delta
            if 0 <= i < len(self.compare_rows):
                self.go_to(self.compare_rows[i])
            return
        self.go_to(self.current_row + delta)

    def next_unmarked(self):
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
        pixmap = self._edited_pixmap(photo)
        if pixmap is None:
            pixmap = self.loader.preview(photo.path)
        if pixmap is not None:
            view.set_image(pixmap, native_size=self.loader.native_size(photo.path), keep_zoom=True)
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
                view.set_full_image(full)
            else:
                view.set_full_loading(self.loader.full_error(photo.path) is None)

    def _edited_pixmap(self, photo):
        """The render of the photo's edit (or of the one being dragged), if it has one."""
        live = self.live_edit is not None and self.live_edit[0] == photo.path
        edit = self.live_edit[1] if live else photo.edit
        if not edit:
            return None
        return self.editor.pixmap(photo.path, edit, self._lut_path(edit), quick=live)

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
        if self.compare_rows:
            self.compare_view.toggle_zoom(self.compare_rows.index(self.current_row))
        else:
            self.image_view.toggle_zoom()

    def escape(self):
        if self.compare_rows and self.compare_view.zoomed:
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
        if self.current_row >= 0:
            self.edit_clipboard = dict(self.model.photo(self.current_row).edit)
            self.toast.show_message("Edit copied" if self.edit_clipboard else "Copied: no edit")

    def paste_edit(self):
        rows = self._target_rows()
        if self.edit_clipboard is None:
            self.toast.show_message("Nothing to paste: copy an edit first")
        elif rows:
            self._set_edit(rows, self.edit_clipboard)
            self.toast.show_message(f"Edit pasted on {len(rows)} photo(s)")

    def reset_edit(self):
        rows = self._target_rows()
        if rows:
            self._set_edit(rows, {})

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
        rows = self._target_rows()
        if not rows or self.moving:
            return
        self.save_session()
        jobs = [(p.path, p.edit, self._lut_path(p.edit)) for p in (self.model.photo(row) for row in rows)]
        dest = os.path.join(self.folder, EXPORT_DIR)

        progress = QProgressDialog("Exporting…", "Stop", 0, len(jobs), self)
        progress.setWindowTitle("Export JPEG")
        progress.setWindowModality(Qt.WindowModality.ApplicationModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)
        thread = ExportThread(jobs, dest, self)
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

        if thread.errors:
            inform(
                self,
                "Some photos were not exported",
                f"Exported {len(thread.exported)} of {len(jobs)} photo(s) to {EXPORT_DIR}. These failed:",
                details=thread.errors,
            )
        else:
            self.toast.show_message(f"Exported {len(thread.exported)} photo(s) to {EXPORT_DIR}")

    # -- compare -----------------------------------------------------------

    def toggle_compare(self):
        """Gallery: compare the selected photos. Cull: the current photo and the next one."""
        if self.compare_rows:
            self._end_compare()
            return
        if self.tabs.currentIndex() == GALLERY_TAB:
            rows = sorted(self.proxy.mapToSource(i).row() for i in self.grid.selectionModel().selectedIndexes())
            if not 2 <= len(rows) <= COMPARE_MAX:
                self.toast.show_message(f"Select 2 to {COMPARE_MAX} photos to compare")
                return
        elif self.current_row + 1 < self.model.rowCount() and self.current_row >= 0:
            rows = [self.current_row, self.current_row + 1]
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
        self._push_undo(self.model.set_status(rows, status))
        if in_gallery:
            self._select_after_filtering(first_visible)
        elif status is not None and not self.compare_rows:
            if self.current_row + 1 < self.model.rowCount():
                self.go_to(self.current_row + 1)
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
        if unmarked:
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
        if not self.undo_stack:
            self.toast.show_message("Nothing to undo")
            return
        changes = self.undo_stack.pop()
        rows = self.model.restore(changes)
        if rows and self.tabs.currentIndex() == CULL_TAB and self.current_row not in rows:
            self.go_to(rows[0])
        self.toast.show_message("Undone" if len(changes) == 1 else f"Undone ({len(changes)} photos)")

    def _on_statuses_changed(self, rows):
        keeps, rejects, unmarked = self.model.counts()
        self.stats.update_counts(keeps, rejects, unmarked)
        self.gallery_toolbar.update_counts(self.model.rowCount(), keeps, rejects, unmarked)
        self.header.set_can_process(bool(self.folder) and self.model.has_unsorted())
        if self.current_row in rows and self.live_edit is None:
            self.edit_panel.set_edit(self.model.photo(self.current_row).edit)  # undo, paste, reset…
        if set(rows) & set(self._shown_rows()):
            self._refresh_view()
        self.session_timer.start()

    # -- gallery -----------------------------------------------------------

    def _on_tab_changed(self, index):
        for shortcut in self.cull_shortcuts:
            shortcut.setEnabled(index == CULL_TAB)
        if index == GALLERY_TAB:
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
        if not self.folder:
            return
        self._end_compare()
        self.save_session()
        moves = plan_moves(self.folder, self.model.photos)
        if not moves:
            self.toast.show_message("Everything is already sorted")
            return

        keeps = sum(1 for m in moves if m.status == KEEP)
        rejects = sum(1 for m in moves if m.status == REJECT)
        unmarked = len(moves) - keeps - rejects
        lines = [f"<b>{len(moves)}</b> photo(s) will be moved:"]
        if keeps:
            lines.append(f"&nbsp;&nbsp;• {keeps} → <b>_KEEPS</b>")
        if rejects:
            lines.append(f"&nbsp;&nbsp;• {rejects} → <b>_REJECTS</b>")
        if unmarked:
            lines.append(f"&nbsp;&nbsp;• {unmarked} → folder root (unmarked)")
        lines.append(
            "<br>RAW+JPEG pairs and XMP sidecars move together. Existing files are never overwritten."
        )
        if not ask(self, "Process files", "<br>".join(lines), "Move Files"):
            return

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
        self.undo_stack.clear()  # undo entries refer to the old paths
        self.save_session()
        self._on_statuses_changed([])
        self._watch(self.folder)  # _KEEPS / _REJECTS may have just been created
        if self.current_row >= 0:
            self.header.set_folder(self.folder, self.model.photo(self.current_row).display_name)

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
        else:
            self.toast.show_message(f"Moved {moved} photo(s)")

    # -- window ------------------------------------------------------------

    def _restore_window(self):
        geometry = self.settings.value("geometry")
        if geometry is None or not self.restoreGeometry(geometry):
            self.resize(1400, 900)

    def closeEvent(self, event):
        if self.moving:
            event.ignore()  # files are being moved; the progress dialog has a Stop button
            return
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
