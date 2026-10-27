import json
import os
import threading
from html import escape

from PyQt6.QtCore import QEventLoop, QItemSelectionModel, QSettings, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QMainWindow,
    QProgressDialog,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .constants import (
    KEEP,
    LEGACY_GLOBAL_CONFIG_FILENAME,
    PREFETCH_AHEAD,
    PREFETCH_BEHIND,
    REJECT,
    UNDO_LIMIT,
)
from .fileops import execute_moves, plan_moves
from .loader import ImageLoader
from .models import PhotoModel, StatusFilterProxy, save_session, scan_folder
from .widgets import (
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


class CullSpeedApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("CullSpeed")
        self.setMinimumSize(900, 600)
        self.setAcceptDrops(True)

        self.settings = QSettings()
        self.folder = ""
        self.current_row = -1
        self.undo_stack = []
        self.moving = False

        self.loader = ImageLoader(self)
        self.loader.preview_loaded.connect(self._on_preview_ready)
        self.loader.preview_failed.connect(lambda path, _error: self._on_preview_ready(path))
        self.loader.full_loaded.connect(self._on_full_ready)
        self.loader.full_failed.connect(self._on_full_failed)

        self.model = PhotoModel(self.loader, self)
        self.model.statuses_changed.connect(self._on_statuses_changed)
        self.proxy = StatusFilterProxy(self)
        self.proxy.setSourceModel(self.model)

        self.session_timer = QTimer(self)
        self.session_timer.setSingleShot(True)
        self.session_timer.setInterval(400)
        self.session_timer.timeout.connect(self.save_session)

        self._build_ui()
        self._build_shortcuts()
        self._restore_window()
        QTimer.singleShot(0, self._restore_last_folder)

    # -- UI ----------------------------------------------------------------

    def _build_ui(self):
        self.header = HeaderBar()
        self.header.open_folder_requested.connect(self.choose_folder)
        self.header.process_requested.connect(self.process_files)

        # Cull tab: large image + filmstrip
        self.image_view = ImageView()
        self.image_view.zoom_changed.connect(self._on_zoom_changed)
        self.image_view.set_image(None, "Open a folder of photos to start\n(or drop one onto this window)")
        self.filmstrip = make_thumbnail_view(
            self.model, ThumbnailDelegate(FILMSTRIP_THUMB_SIZE, show_names=False, parent=self), horizontal=True
        )
        self.filmstrip.setObjectName("Filmstrip")
        self.filmstrip.setFixedHeight(FILMSTRIP_THUMB_SIZE + ThumbnailDelegate.PADDING * 2 + 16)
        self.filmstrip.clicked.connect(lambda index: self.go_to(index.row()))

        cull_page = QWidget()
        cull_layout = QVBoxLayout(cull_page)
        cull_layout.setContentsMargins(0, 0, 0, 0)
        cull_layout.setSpacing(0)
        cull_layout.addWidget(self.image_view, 1)
        cull_layout.addWidget(self.filmstrip)

        # Gallery tab: filter chips + grid
        grid_size = int(self.settings.value("grid_thumb_size", 150))
        self.gallery_toolbar = GalleryToolbar(grid_size)
        self.gallery_toolbar.filter_changed.connect(self.proxy.set_filter)
        self.gallery_toolbar.size_changed.connect(self._on_grid_size_changed)
        self.grid_delegate = ThumbnailDelegate(grid_size, show_names=True, parent=self)
        self.grid = make_thumbnail_view(self.proxy, self.grid_delegate, horizontal=False)
        self.grid.clicked.connect(self._on_grid_clicked)
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
        bind([QKeySequence.StandardKey.Undo], self.undo)
        bind([QKeySequence.StandardKey.Open], self.choose_folder)
        bind(["G"], self.toggle_tab)

        # In the gallery the arrow keys move through the grid instead.
        bind(["Up"], lambda: self.mark(KEEP), cull_only=True)
        bind(["Down"], lambda: self.mark(REJECT), cull_only=True)
        bind(["Right", "L", "Space"], lambda: self.go_to(self.current_row + 1), cull_only=True)
        bind(["Left", "H"], lambda: self.go_to(self.current_row - 1), cull_only=True)
        bind(["Home"], lambda: self.go_to(0), cull_only=True)
        bind(["End"], lambda: self.go_to(self.model.rowCount() - 1), cull_only=True)
        bind(["N"], self.next_unmarked, cull_only=True)
        bind(["Z"], self.image_view.toggle_zoom, cull_only=True)
        bind(["Escape"], self.image_view.reset_zoom, cull_only=True)

    # -- folder ------------------------------------------------------------

    def choose_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Select a photo folder", self.folder or os.path.expanduser("~")
        )
        if folder:
            self.load_folder(folder)

    def load_folder(self, folder, select_name=None):
        if self.moving:
            return
        self.save_session()
        try:
            photos = scan_folder(folder)
        except OSError as exc:
            inform(self, "Cannot open folder", escape(f"{folder}: {exc}"))
            return

        self.loader.reset()
        self.folder = folder
        self.undo_stack.clear()
        self.current_row = -1
        self.model.set_photos(photos)
        self.settings.setValue("last_folder", folder)
        self.header.set_folder(folder)

        if not photos:
            self.image_view.set_image(None, "No photos in this folder")
            self.header.set_counter("")
            return

        names = [photo.name for photo in photos]
        self.go_to(names.index(select_name) if select_name in names else 0)

    def _restore_last_folder(self):
        folder = self.settings.value("last_folder", "")
        last_file = self.settings.value("last_file", "")
        if not folder:
            folder, last_file = self._legacy_global_state()
        if folder and os.path.isdir(folder):
            self.load_folder(folder, select_name=last_file)

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
        try:
            save_session(self.folder, self.model.photos)
        except OSError as exc:
            self.toast.show_message(f"Could not save marks: {exc}", 4000)

    # -- navigation --------------------------------------------------------

    def go_to(self, row):
        if not 0 <= row < self.model.rowCount():
            return
        self.current_row = row
        photo = self.model.photo(row)
        self.header.set_counter(f"{row + 1} / {self.model.rowCount()}")
        self.header.set_folder(self.folder, photo.name)

        index = self.model.index(row)
        self.filmstrip.selectionModel().setCurrentIndex(
            index, QItemSelectionModel.SelectionFlag.ClearAndSelect
        )
        self.filmstrip.scrollTo(index, QAbstractItemView.ScrollHint.PositionAtCenter)

        self._show_current()
        count = self.model.rowCount()
        nearby = [*range(row + 1, row + 1 + PREFETCH_AHEAD), *range(row - 1, row - 1 - PREFETCH_BEHIND, -1)]
        self.loader.ensure_previews(
            photo.path, [self.model.photo(r).path for r in nearby if 0 <= r < count]
        )

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

    def _show_current(self):
        if self.current_row < 0:
            return
        photo = self.model.photo(self.current_row)
        pixmap = self.loader.preview(photo.path)
        if pixmap is not None:
            self.image_view.set_image(pixmap)
        else:
            error = self.loader.preview_error(photo.path)
            self.image_view.set_image(None, f"Could not load {photo.name}\n\n{error}" if error else "Loading…")
        self.image_view.set_status(photo.status)

    def _is_current(self, path):
        return self.current_row >= 0 and self.model.photo(self.current_row).path == path

    def _on_preview_ready(self, path):
        if self._is_current(path):
            self._show_current()

    # -- 1:1 zoom ----------------------------------------------------------
    # Previews are downscaled, so zooming loads the photo at full resolution.

    def _on_zoom_changed(self, zoomed):
        if not zoomed or self.current_row < 0:
            return
        path = self.model.photo(self.current_row).path
        pixmap = self.loader.full_pixmap(path)
        if pixmap is not None:
            self.image_view.set_full_image(pixmap)
        else:
            self.image_view.set_full_loading(True)
            self.loader.ensure_full(path)

    def _on_full_ready(self, path):
        if self._is_current(path) and self.image_view.zoomed:
            self.image_view.set_full_image(self.loader.full_pixmap(path))

    def _on_full_failed(self, path, error):
        if self._is_current(path):
            self.image_view.set_full_loading(False)
            self.toast.show_message(f"Full resolution unavailable: {error}", 4000)

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

    def mark(self, status):
        rows = self._target_rows()
        if not rows:
            return
        in_gallery = self.tabs.currentIndex() == GALLERY_TAB
        first_visible = min(self.proxy.mapFromSource(self.model.index(row)).row() for row in rows)
        changes = self.model.set_status(rows, status)
        if changes:
            self.undo_stack.append(changes)
            del self.undo_stack[:-UNDO_LIMIT]
        if in_gallery:
            self._select_after_filtering(first_visible)
        elif status is not None:
            self.go_to(self.current_row + 1)

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
        if rows and self.tabs.currentIndex() == CULL_TAB:
            self.go_to(rows[0])
        self.toast.show_message("Undone" if len(changes) == 1 else f"Undone ({len(changes)} photos)")

    def _on_statuses_changed(self, rows):
        keeps, rejects, unmarked = self.model.counts()
        self.stats.update_counts(keeps, rejects, unmarked)
        self.gallery_toolbar.update_counts(self.model.rowCount(), keeps, rejects, unmarked)
        self.header.set_can_process(bool(self.folder) and self.model.has_unsorted())
        if self.current_row in rows:
            self.image_view.set_status(self.model.photo(self.current_row).status)
        self.session_timer.start()

    # -- gallery -----------------------------------------------------------

    def _on_tab_changed(self, index):
        for shortcut in self.cull_shortcuts:
            shortcut.setEnabled(index == CULL_TAB)
        if index == GALLERY_TAB and self.current_row >= 0:
            proxy_index = self.proxy.mapFromSource(self.model.index(self.current_row))
            if proxy_index.isValid():
                self.grid.selectionModel().setCurrentIndex(
                    proxy_index, QItemSelectionModel.SelectionFlag.ClearAndSelect
                )
                self.grid.scrollTo(proxy_index, QAbstractItemView.ScrollHint.PositionAtCenter)
            self.grid.setFocus()
        else:
            self.image_view.reset_zoom()
            self.setFocus()

    def _on_grid_clicked(self, proxy_index):
        if len(self.grid.selectionModel().selectedIndexes()) <= 1:
            self.go_to(self.proxy.mapToSource(proxy_index).row())

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
        self.save_session()
        moves = plan_moves(self.folder, self.model.photos)
        if not moves:
            self.toast.show_message("Everything is already sorted")
            return

        keeps = sum(1 for m in moves if m.status == KEEP)
        rejects = sum(1 for m in moves if m.status == REJECT)
        unmarked = len(moves) - keeps - rejects
        lines = [f"<b>{len(moves)}</b> file(s) will be moved:"]
        if keeps:
            lines.append(f"&nbsp;&nbsp;• {keeps} → <b>_KEEPS</b>")
        if rejects:
            lines.append(f"&nbsp;&nbsp;• {rejects} → <b>_REJECTS</b>")
        if unmarked:
            lines.append(f"&nbsp;&nbsp;• {unmarked} → folder root (unmarked)")
        lines.append("<br>XMP sidecars move with their photo. Existing files are never overwritten.")
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
        locations = {move.src: move.status for move in moves}
        self.model.apply_moves({old: (new, locations[old]) for old, new in result.moved.items()})
        self.loader.rename_paths(result.moved)
        self.undo_stack.clear()  # undo entries refer to the old paths
        self.save_session()
        self._on_statuses_changed([])
        if self.current_row >= 0:
            self.header.set_folder(self.folder, self.model.photo(self.current_row).name)

        if result.errors:
            inform(
                self,
                "Some files were not moved",
                f"Moved {len(result.moved)} of {len(moves)} file(s). These failed:",
                details=result.errors,
            )
        elif result.cancelled:
            self.toast.show_message(f"Stopped after moving {len(result.moved)} file(s)")
        else:
            self.toast.show_message(f"Moved {len(result.moved)} file(s)")

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
            self.settings.setValue("last_file", self.model.photo(self.current_row).name)
        self.loader.shutdown()
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
