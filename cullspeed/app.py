import json
import os
import shutil

from PyQt6.QtCore import Qt, QThreadPool, QTimer
from PyQt6.QtGui import QColor, QPalette, QIcon, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QLabel,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .constants import GLOBAL_CONFIG_FILENAME, SESSION_FILENAME, SUPPORTED_EXTENSIONS
from .workers import ImageLoaderThread, ThumbnailWorker
from .widgets import HeaderBar, StatsBar, CullView, GalleryView


class CullSpeedApp(QMainWindow):
    def __init__(self):
        super().__init__()

        self.setWindowTitle("CullSpeed Desktop")
        self.resize(1400, 900)
        self.setMinimumSize(800, 600)

        self.folder_path = ""
        self.files = []
        self.current_index = -1
        self.marks = {}
        self.loader_thread = None
        self.current_pixmap = None

        self.thread_pool = QThreadPool()
        self.thread_pool.setMaxThreadCount(4)

        self.thumbnail_queue = []
        self.thumbnail_timer = QTimer()
        self.thumbnail_timer.timeout.connect(self.process_thumbnail_batch)
        self.thumbnail_timer.setInterval(30)

        self.init_ui()
        self.apply_dark_theme()

        self.update_ui_state()

        QTimer.singleShot(10, self.load_global_state)

    def init_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        self.header_bar = HeaderBar()
        self.header_bar.open_folder_requested.connect(self.open_folder)
        self.header_bar.process_requested.connect(self.process_files)
        main_layout.addWidget(self.header_bar)

        self.tabs = QTabWidget()
        self.tabs.setStyleSheet(
            """
            QTabWidget::pane { border: none; }
            QTabBar::tab { background: #222; color: #888; padding: 8px 20px; border-top-left-radius: 4px; border-top-right-radius: 4px; }
            QTabBar::tab:selected { background: #333; color: white; font-weight: bold; border-bottom: 2px solid #2563eb; }
            """
        )

        self.cull_view = CullView()
        self.gallery_view = GalleryView()
        self.cull_view.filmstrip_clicked.connect(self.on_filmstrip_index_selected)
        self.gallery_view.item_clicked.connect(self.on_gallery_index_selected)
        self.gallery_view.item_double_clicked.connect(self.on_gallery_index_double_clicked)

        self.tabs.addTab(self.cull_view, "Cull (Focus)")
        self.tabs.addTab(self.gallery_view, "Gallery (Grid)")
        self.tabs.currentChanged.connect(self.on_tab_changed)
        main_layout.addWidget(self.tabs)

        self.image_container = self.cull_view.image_container
        self.lbl_image = self.cull_view.lbl_image
        self.lbl_overlay = self.cull_view.lbl_overlay
        self.filmstrip = self.cull_view.filmstrip
        self.grid_view = self.gallery_view.grid_view

        self.setup_shortcuts()

        self.stats_bar = StatsBar()
        main_layout.addWidget(self.stats_bar)

        self.statusTip = QLabel("Shortcuts: [P] Keep | [X] Reject | [U] Unmark | [H / L] Nav", self)
        self.statusTip.setStyleSheet("background: rgba(0,0,0,0.5); color: white; padding: 5px; font-family: monospace;")
        self.statusTip.setGeometry(10, 100, 400, 30)

    def setup_shortcuts(self):
        for key in [Qt.Key.Key_1, Qt.Key.Key_P]:
            QShortcut(QKeySequence(key), self).activated.connect(lambda: self.mark_current('keep'))

        for key in [Qt.Key.Key_3, Qt.Key.Key_X]:
            QShortcut(QKeySequence(key), self).activated.connect(lambda: self.mark_current('reject'))

        for key in [Qt.Key.Key_Backspace, Qt.Key.Key_2, Qt.Key.Key_U]:
            QShortcut(QKeySequence(key), self).activated.connect(lambda: self.mark_current(None))

    def apply_dark_theme(self):
        palette = QPalette()
        palette.setColor(QPalette.ColorRole.Window, QColor(20, 20, 20))
        palette.setColor(QPalette.ColorRole.WindowText, Qt.GlobalColor.white)
        self.setPalette(palette)

    def get_session_file(self):
        return os.path.join(self.folder_path, SESSION_FILENAME)

    def save_session(self):
        if not self.folder_path:
            return
        data = {}
        for path, status in self.marks.items():
            data[os.path.basename(path)] = status

        try:
            with open(self.get_session_file(), 'w') as file:
                json.dump(data, file, indent=2)
        except Exception as exc:
            print(f"Failed to save session: {exc}")

    def get_global_config_path(self):
        return os.path.join(os.path.expanduser("~"), GLOBAL_CONFIG_FILENAME)

    def load_global_state(self):
        try:
            config_path = self.get_global_config_path()
            if os.path.exists(config_path):
                with open(config_path, 'r') as file:
                    config = json.load(file)

                last_folder = config.get('last_folder')
                last_file = config.get('last_file')

                if last_folder and os.path.isdir(last_folder):
                    self.folder_path = last_folder
                    self.scan_folder(target_filename=last_file)
        except Exception as exc:
            print(f"Global state load error: {exc}")

    def save_global_state(self):
        if not self.folder_path:
            return

        try:
            current_file = ""
            if self.files and 0 <= self.current_index < len(self.files):
                current_file = os.path.basename(self.files[self.current_index])

            config = {
                'last_folder': self.folder_path,
                'last_file': current_file,
            }

            with open(self.get_global_config_path(), 'w') as file:
                json.dump(config, file)
        except Exception as exc:
            print(f"Global state save error: {exc}")

    def closeEvent(self, event):
        self.save_global_state()
        super().closeEvent(event)

    def open_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Image Folder")
        if folder:
            self.folder_path = folder
            self.scan_folder()

    def scan_folder(self, target_filename=None):
        self.files = []
        self.filmstrip.clear()
        self.grid_view.clear()
        self.marks = {}
        self.thumbnail_queue = []
        self.thumbnail_timer.stop()
        self.thread_pool.clear()
        self.header_bar.set_progress_visible(False)

        def scan_dir(path):
            found = []
            if os.path.exists(path):
                with os.scandir(path) as entries:
                    for entry in entries:
                        if entry.is_file() and entry.name.lower().endswith(SUPPORTED_EXTENSIONS):
                            found.append(entry.path)
            return found

        try:
            root_files = scan_dir(self.folder_path)

            keep_dir = os.path.join(self.folder_path, "_KEEPS")
            reject_dir = os.path.join(self.folder_path, "_REJECTS")

            keep_files = scan_dir(keep_dir)
            reject_files = scan_dir(reject_dir)

            all_files = root_files + keep_files + reject_files
            all_files.sort(key=lambda x: os.path.basename(x))

            self.files = all_files

            if not self.files:
                self.current_index = -1
                self.lbl_image.setText("No images found")
                self.header_bar.set_filename(self.folder_path)
                self.update_ui_state()
                return

            for f in keep_files:
                self.marks[f] = 'keep'
            for f in reject_files:
                self.marks[f] = 'reject'

            session_file = self.get_session_file()
            if os.path.exists(session_file):
                try:
                    with open(session_file, 'r') as file:
                        session_data = json.load(file)

                    name_to_path = {os.path.basename(p): p for p in self.files}

                    for fname, status in session_data.items():
                        if fname in name_to_path:
                            self.marks[name_to_path[fname]] = status
                except Exception as exc:
                    print(f"Session load error: {exc}")

            for i, fpath in enumerate(self.files):
                fname = os.path.basename(fpath)

                item_f = QListWidgetItem(fname)
                item_f.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.filmstrip.addItem(item_f)

                item_g = QListWidgetItem(fname)
                item_g.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.grid_view.addItem(item_g)

                self.thumbnail_queue.append((i, fpath))

            self.header_bar.configure_progress(len(self.files))
            self.header_bar.set_progress_format("Thumbnails: %v / %m")

            if self.tabs.currentIndex() == 1:
                self.thumbnail_timer.start()
                self.header_bar.set_progress_visible(True)

            start_index = 0
            if target_filename:
                for i, fpath in enumerate(self.files):
                    if os.path.basename(fpath) == target_filename:
                        start_index = i
                        break

            self.current_index = start_index
            self.load_current_image()
            self.update_ui_state()
            self.setFocus()

        except Exception as exc:
            QMessageBox.critical(self, "Error", str(exc))

    def on_tab_changed(self, index):
        if index == 1:
            if self.thumbnail_queue:
                self.thumbnail_timer.start()
                self.header_bar.set_progress_visible(True)
        else:
            self.thumbnail_timer.stop()
            self.header_bar.set_progress_visible(False)

    def process_thumbnail_batch(self):
        if not self.thumbnail_queue:
            self.thumbnail_timer.stop()
            self.header_bar.set_progress_visible(False)
            return

        batch_size = 5
        count = 0

        while count < batch_size and self.thumbnail_queue:
            idx, fpath = self.thumbnail_queue.pop(0)

            processed_count = len(self.files) - len(self.thumbnail_queue)
            self.header_bar.update_progress(processed_count)

            worker = ThumbnailWorker(fpath, idx)
            worker.signals.finished.connect(self.update_thumbnail)
            self.thread_pool.start(worker)
            count += 1

    def update_thumbnail(self, index, file_path, pixmap):
        if index < len(self.files) and self.files[index] == file_path:
            icon = QIcon(pixmap)
            if index < self.grid_view.count():
                self.grid_view.item(index).setIcon(icon)

    def on_filmstrip_index_selected(self, index: int):
        if index != -1:
            self.switch_to_image(index)

    def on_gallery_index_selected(self, index: int):
        if index != -1 and len(self.grid_view.selectedItems()) <= 1:
            self.switch_to_image(index)

    def on_gallery_index_double_clicked(self, index: int):
        if index != -1:
            self.switch_to_image(index)
            self.tabs.setCurrentIndex(0)

    def switch_to_image(self, index):
        self.current_index = index
        self.load_current_image()
        self.update_ui_state()
        self.setFocus()

    def load_current_image(self):
        if not self.files or self.current_index < 0:
            return

        filepath = self.files[self.current_index]
        self.header_bar.set_filename(os.path.basename(filepath))

        self.lbl_image.setText("Loading...")

        if self.loader_thread and self.loader_thread.isRunning():
            self.loader_thread.terminate()
            self.loader_thread.wait()

        self.loader_thread = ImageLoaderThread(filepath)
        self.loader_thread.image_loaded.connect(self.on_image_loaded)
        self.loader_thread.start()

        self.update_overlay()

        self.filmstrip.setCurrentRow(self.current_index)
        self.filmstrip.scrollToItem(self.filmstrip.item(self.current_index), QAbstractItemView.ScrollHint.EnsureVisible)
        if self.tabs.currentIndex() != 1:
            self.grid_view.setCurrentRow(self.current_index)

    def on_image_loaded(self, pixmap, error):
        if error:
            self.lbl_image.setText(f"Error: {error}")
        else:
            self.current_pixmap = pixmap
            self.update_image_view()
            self.lbl_image.setText("")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.update_image_view()

    def update_image_view(self):
        if not self.current_pixmap or self.current_pixmap.isNull():
            return
        size = self.image_container.size()
        scaled_pixmap = self.current_pixmap.scaled(
            size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.lbl_image.setPixmap(scaled_pixmap)

    def update_overlay(self):
        if not self.files:
            return
        path = self.files[self.current_index]
        status = self.marks.get(path)

        if status == 'keep':
            self.image_container.setStyleSheet("background-color: #052e16; border: 4px solid #4ade80;")
        elif status == 'reject':
            self.image_container.setStyleSheet("background-color: #450a0a; border: 4px solid #f87171;")
        else:
            self.image_container.setStyleSheet("background-color: #000; border: none;")

        self.update_item_colors()

    def update_item_colors(self):
        for i in range(len(self.files)):
            fpath = self.files[i]
            status = self.marks.get(fpath)
            is_selected = i == self.current_index

            bg = QColor("#222")
            fg = QColor("#ccc")

            if status == 'keep':
                bg = QColor("#1b5e20")
                fg = QColor("#66bb6a")
            elif status == 'reject':
                bg = QColor("#b71c1c")
                fg = QColor("#ef5350")

            if is_selected:
                if status == 'keep':
                    bg = QColor("#2e7d32")
                elif status == 'reject':
                    bg = QColor("#c62828")
                else:
                    bg = QColor("#2563eb")

            if i < self.filmstrip.count():
                item = self.filmstrip.item(i)
                item.setBackground(bg)
                item.setForeground(fg)

            if i < self.grid_view.count():
                item = self.grid_view.item(i)
                item.setBackground(bg)
                item.setForeground(fg)

                base_name = os.path.basename(fpath)
                new_text = base_name
                if status == 'keep':
                    new_text = f"✔ {base_name}"
                elif status == 'reject':
                    new_text = f"✘ {base_name}"

                if item.text() != new_text:
                    item.setText(new_text)

    def update_ui_state(self):
        count = len(self.files)
        if count > 0:
            self.header_bar.set_counter(f"{self.current_index + 1} / {count}")
        else:
            self.header_bar.set_counter("0 / 0")

        keeps = list(self.marks.values()).count('keep')
        rejects = list(self.marks.values()).count('reject')

        self.stats_bar.update_counts(keeps, rejects)

    def next_image(self):
        if self.current_index < len(self.files) - 1:
            self.switch_to_image(self.current_index + 1)

    def prev_image(self):
        if self.current_index > 0:
            self.switch_to_image(self.current_index - 1)

    def mark_current(self, status):
        if not self.files:
            return

        if self.tabs.currentIndex() == 1:
            selected_items = self.grid_view.selectedItems()
            if len(selected_items) > 0:
                for item in selected_items:
                    idx = self.grid_view.row(item)
                    if idx != -1:
                        path_at_idx = self.files[idx]
                        if status is None:
                            if path_at_idx in self.marks:
                                del self.marks[path_at_idx]
                        else:
                            self.marks[path_at_idx] = status

                self.save_session()
                self.update_item_colors()
                self.update_ui_state()
                return

        path = self.files[self.current_index]

        if status is None:
            if path in self.marks:
                del self.marks[path]
        else:
            self.marks[path] = status
            if status is not None and self.tabs.currentIndex() == 0:
                self.next_image()

        self.save_session()
        self.update_overlay()
        self.update_ui_state()

    def process_files(self):
        keeps_pending = []
        rejects_pending = []
        unmarked_pending = []

        keep_dir = os.path.join(self.folder_path, "_KEEPS")
        reject_dir = os.path.join(self.folder_path, "_REJECTS")

        for fpath in self.files:
            status = self.marks.get(fpath)
            current_dir = os.path.dirname(fpath)

            target_dir = self.folder_path
            if status == 'keep':
                target_dir = keep_dir
            elif status == 'reject':
                target_dir = reject_dir

            if os.path.abspath(current_dir) != os.path.abspath(target_dir):
                if status == 'keep':
                    keeps_pending.append(fpath)
                elif status == 'reject':
                    rejects_pending.append(fpath)
                else:
                    unmarked_pending.append(fpath)

        total_moves = len(keeps_pending) + len(rejects_pending) + len(unmarked_pending)

        if total_moves == 0:
            QMessageBox.information(self, "Info", "No file movements needed.")
            return

        msg = (
            f"Processing {total_moves} moves:\n"
            f"{len(keeps_pending)} -> _KEEPS\n"
            f"{len(rejects_pending)} -> _REJECTS\n"
            f"{len(unmarked_pending)} -> Root (Unmarked)\n\n"
            "Proceed?"
        )

        reply = QMessageBox.question(
            self,
            "Process Files",
            msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )

        if reply == QMessageBox.StandardButton.Yes:
            try:
                if not os.path.exists(keep_dir):
                    os.makedirs(keep_dir)
                if not os.path.exists(reject_dir):
                    os.makedirs(reject_dir)

                def safe_move(src, dest_folder):
                    fname = os.path.basename(src)
                    dest = os.path.join(dest_folder, fname)
                    if src != dest:
                        shutil.move(src, dest)

                for f in keeps_pending:
                    safe_move(f, keep_dir)
                for f in rejects_pending:
                    safe_move(f, reject_dir)
                for f in unmarked_pending:
                    safe_move(f, self.folder_path)

                QMessageBox.information(self, "Success", "Files moved successfully!")
                self.save_session()
                self.scan_folder()

            except Exception as exc:
                QMessageBox.critical(self, "Error", f"An error occurred:\n{exc}")

    def keyPressEvent(self, event):
        key = event.key()

        if key in (Qt.Key.Key_Right, Qt.Key.Key_L):
            self.next_image()
        elif key in (Qt.Key.Key_Left, Qt.Key.Key_H):
            self.prev_image()
        elif key == Qt.Key.Key_Up:
            self.mark_current('keep')
        elif key == Qt.Key.Key_Down:
            self.mark_current('reject')
        else:
            super().keyPressEvent(event)
