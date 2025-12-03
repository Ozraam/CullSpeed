import sys
import os
import shutil
import rawpy
import time
import json
from PyQt6.QtWidgets import (QApplication, QMainWindow, QLabel, QVBoxLayout, 
                             QWidget, QFileDialog, QMessageBox, QHBoxLayout, 
                             QPushButton, QFrame, QSizePolicy, QProgressBar,
                             QTabWidget, QListWidget, QListWidgetItem, 
                             QAbstractItemView)
from PyQt6.QtGui import (QPixmap, QImage, QColor, QPalette, QFont, QIcon, 
                         QKeySequence, QAction, QShortcut)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QSize, QEvent, QDir, QObject, QRunnable, QThreadPool, QTimer

# --- Configuration ---
SUPPORTED_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.webp', '.arw', '.cr2', '.nef', '.dng', '.orf', '.raf', '.rw2')
SESSION_FILENAME = "cullspeed_session.json"
GLOBAL_CONFIG_FILENAME = ".cullspeed_global.json"

class ImageUtils:
    @staticmethod
    def load_thumbnail(file_path, target_size=200):
        """
        Helper to load a small thumbnail efficiently.
        CRITICAL OPTIMIZATION: Only extracts embedded thumbnails for RAWs.
        """
        try:
            ext = os.path.splitext(file_path)[1].lower()
            image = QImage()
            
            # RAW Handling
            if ext in ('.arw', '.cr2', '.nef', '.dng', '.orf', '.raf', '.rw2'):
                with rawpy.imread(file_path) as raw:
                    try:
                        # FAST: Extract embedded JPEG
                        thumb = raw.extract_thumb()
                    except rawpy.LibRawNoThumbnailError:
                        return None
                    
                    if thumb.format == rawpy.ThumbFormat.JPEG:
                        image.loadFromData(thumb.data)
                    else:
                        return None 
            else:
                # Standard Image
                image.load(file_path)

            if image.isNull():
                return None
                
            # Scale for thumbnail
            return QPixmap.fromImage(image.scaled(target_size, target_size, 
                                                Qt.AspectRatioMode.KeepAspectRatio, 
                                                Qt.TransformationMode.FastTransformation))
        except:
            return None

class WorkerSignals(QObject):
    finished = pyqtSignal(int, str, QPixmap) # Added str for filepath verification

class ThumbnailWorker(QRunnable):
    def __init__(self, file_path, row_index):
        super().__init__()
        self.file_path = file_path
        self.row_index = row_index
        self.signals = WorkerSignals()

    def run(self):
        pixmap = ImageUtils.load_thumbnail(self.file_path)
        if pixmap:
            self.signals.finished.emit(self.row_index, self.file_path, pixmap)

class ImageLoaderThread(QThread):
    image_loaded = pyqtSignal(QPixmap, str) 

    def __init__(self, file_path):
        super().__init__()
        self.file_path = file_path

    def run(self):
        try:
            ext = os.path.splitext(self.file_path)[1].lower()
            pixmap = None

            if ext in ('.arw', '.cr2', '.nef', '.dng', '.orf', '.raf', '.rw2'):
                try:
                    with rawpy.imread(self.file_path) as raw:
                        try:
                            thumb = raw.extract_thumb()
                        except rawpy.LibRawNoThumbnailError:
                            thumb = None
                        
                        if thumb:
                            if thumb.format == rawpy.ThumbFormat.JPEG:
                                image = QImage()
                                image.loadFromData(thumb.data)
                            else:
                                h, w = thumb.data.shape[0], thumb.data.shape[1] 
                                image = QImage() 
                        else:
                            rgb = raw.postprocess(use_camera_wb=True, half_size=True)
                            h, w, c = rgb.shape
                            image = QImage(rgb.data, w, h, w * 3, QImage.Format.Format_RGB888)

                        if not image.isNull():
                            pixmap = QPixmap.fromImage(image)
                except Exception as e:
                    self.image_loaded.emit(QPixmap(), f"RAW Error: {str(e)}")
                    return
            else:
                pixmap = QPixmap(self.file_path)

            if pixmap and not pixmap.isNull():
                if pixmap.width() > 3000 or pixmap.height() > 3000:
                     pixmap = pixmap.scaled(3000, 3000, 
                                          Qt.AspectRatioMode.KeepAspectRatio, 
                                          Qt.TransformationMode.SmoothTransformation)
                self.image_loaded.emit(pixmap, "")
            else:
                self.image_loaded.emit(QPixmap(), "Could not load image")

        except Exception as e:
            self.image_loaded.emit(QPixmap(), str(e))


class CullSpeedApp(QMainWindow):
    def __init__(self):
        super().__init__()

        self.setWindowTitle("CullSpeed Desktop")
        self.resize(1400, 900)
        self.setMinimumSize(800, 600)

        # State
        self.folder_path = ""
        self.files = []
        self.current_index = -1
        self.marks = {} # { filepath: 'keep' | 'reject' }
        self.loader_thread = None
        self.current_pixmap = None 
        
        self.thread_pool = QThreadPool()
        self.thread_pool.setMaxThreadCount(4)
        
        # Batch loading state
        self.thumbnail_queue = []
        self.thumbnail_timer = QTimer()
        self.thumbnail_timer.timeout.connect(self.process_thumbnail_batch)
        self.thumbnail_timer.setInterval(30) # Slower interval (30ms) to reduce UI freezing

        # UI Setup
        self.init_ui()
        self.apply_dark_theme()
        
        self.update_ui_state()
        
        # Restore last global session (auto-open folder)
        # Use a single shot timer to let the window show up FIRST, preventing perceived freeze
        QTimer.singleShot(10, self.load_global_state)

    def init_ui(self):
        # Top Bar
        self.top_bar = QFrame()
        self.top_bar.setStyleSheet("background-color: #1a1a1a; border-bottom: 1px solid #333;")
        top_layout = QHBoxLayout(self.top_bar)
        
        self.lbl_title = QLabel("CullSpeed")
        self.lbl_title.setStyleSheet("font-weight: bold; font-size: 16px; color: #60a5fa;")
        
        self.lbl_filename = QLabel("No Folder Selected")
        self.lbl_filename.setStyleSheet("color: #9ca3af; font-size: 14px;")

        # Progress Bar for Thumbnails
        self.progress_bar = QProgressBar()
        self.progress_bar.setFixedWidth(200)
        self.progress_bar.setStyleSheet("""
            QProgressBar {
                border: 1px solid #333;
                border-radius: 4px;
                background-color: #222;
                text-align: center;
                color: #aaa;
                font-size: 10px;
            }
            QProgressBar::chunk {
                background-color: #2563eb;
            }
        """)
        self.progress_bar.hide()
        
        self.lbl_counter = QLabel("")
        self.lbl_counter.setStyleSheet("background-color: #333; color: #bbb; padding: 4px 8px; border-radius: 4px;")

        self.btn_open = QPushButton("Open Folder")
        self.btn_open.setStyleSheet("""
            QPushButton { background-color: #2563eb; color: white; border: none; padding: 6px 12px; border-radius: 4px; font-weight: bold; }
            QPushButton:hover { background-color: #1d4ed8; }
        """)
        self.btn_open.clicked.connect(self.open_folder)

        self.btn_process = QPushButton("Process Files")
        self.btn_process.setStyleSheet("""
            QPushButton { background-color: #333; color: white; border: 1px solid #555; padding: 6px 12px; border-radius: 4px; }
            QPushButton:hover { background-color: #444; }
        """)
        self.btn_process.clicked.connect(self.process_files)

        top_layout.addWidget(self.lbl_title)
        top_layout.addSpacing(20)
        top_layout.addWidget(self.lbl_filename)
        top_layout.addStretch()
        top_layout.addWidget(self.progress_bar) # Added Progress Bar here
        top_layout.addSpacing(10)
        top_layout.addWidget(self.lbl_counter)
        top_layout.addSpacing(10)
        top_layout.addWidget(self.btn_open)
        top_layout.addWidget(self.btn_process)

        # Main Layout
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        
        main_layout.addWidget(self.top_bar)

        # Tabs
        self.tabs = QTabWidget()
        self.tabs.setStyleSheet("""
            QTabWidget::pane { border: none; }
            QTabBar::tab { background: #222; color: #888; padding: 8px 20px; border-top-left-radius: 4px; border-top-right-radius: 4px; }
            QTabBar::tab:selected { background: #333; color: white; font-weight: bold; border-bottom: 2px solid #2563eb; }
        """)
        
        self.setup_cull_tab()
        self.setup_gallery_tab()
        self.setup_shortcuts()
        
        self.tabs.addTab(self.tab_cull, "Cull (Focus)")
        self.tabs.addTab(self.tab_gallery, "Gallery (Grid)")
        self.tabs.currentChanged.connect(self.on_tab_changed) # Detect tab switch
        
        main_layout.addWidget(self.tabs)

        # Bottom Stats Bar
        bottom_bar = QFrame()
        bottom_bar.setStyleSheet("background-color: #1a1a1a; border-top: 1px solid #333;")
        bottom_layout = QHBoxLayout(bottom_bar)
        bottom_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        def create_stat_label(text, color):
            lbl = QLabel(text)
            lbl.setStyleSheet(f"color: {color}; font-weight: bold; font-size: 14px; margin: 0 10px;")
            return lbl

        self.lbl_stat_keep = create_stat_label("Keeps: 0", "#4ade80")
        self.lbl_stat_reject = create_stat_label("Rejects: 0", "#f87171")
        
        bottom_layout.addWidget(self.lbl_stat_keep)
        bottom_layout.addWidget(QLabel("|", parent=bottom_bar))
        bottom_layout.addWidget(self.lbl_stat_reject)

        main_layout.addWidget(bottom_bar)

        # Tooltip
        self.statusTip = QLabel("Shortcuts: [P] Keep | [X] Reject | [U] Unmark | [H / L] Nav", self)
        self.statusTip.setStyleSheet("background: rgba(0,0,0,0.5); color: white; padding: 5px; font-family: monospace;")
        self.statusTip.setGeometry(10, 100, 400, 30)

    def setup_shortcuts(self):
        # Global Shortcuts (Work even when Grid/List has focus)
        
        # Keep - P, 1
        for key in [Qt.Key.Key_1, Qt.Key.Key_P]:
            QShortcut(QKeySequence(key), self).activated.connect(lambda: self.mark_current('keep'))
            
        # Reject - X, 3
        for key in [Qt.Key.Key_3, Qt.Key.Key_X]:
            QShortcut(QKeySequence(key), self).activated.connect(lambda: self.mark_current('reject'))
            
        # Unmark - U, 2, Backspace
        for key in [Qt.Key.Key_Backspace, Qt.Key.Key_2, Qt.Key.Key_U]:
            QShortcut(QKeySequence(key), self).activated.connect(lambda: self.mark_current(None))

    def setup_cull_tab(self):
        self.tab_cull = QWidget()
        layout = QVBoxLayout(self.tab_cull)
        layout.setContentsMargins(0,0,0,0)
        layout.setSpacing(0)

        # Main Image Area
        self.image_container = QWidget()
        self.image_container.setStyleSheet("background-color: #000;")
        img_layout = QVBoxLayout(self.image_container)
        img_layout.setContentsMargins(0,0,0,0)
        
        self.lbl_image = QLabel()
        self.lbl_image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_image.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        img_layout.addWidget(self.lbl_image)
        
        # Overlay Label
        self.lbl_overlay = QLabel(self.image_container)
        self.lbl_overlay.hide()

        layout.addWidget(self.image_container, stretch=1)

        # Filmstrip
        self.filmstrip = QListWidget()
        self.filmstrip.setViewMode(QListWidget.ViewMode.ListMode)
        self.filmstrip.setFlow(QListWidget.Flow.LeftToRight)
        self.filmstrip.setWrapping(False)
        self.filmstrip.setSpacing(5) 
        self.filmstrip.setFixedHeight(60) 
        self.filmstrip.setStyleSheet("""
            QListWidget { background-color: #111; border-top: 1px solid #333; }
            QListWidget::item { color: #888; border-radius: 6px; margin: 2px; padding: 5px; } 
            QListWidget::item:selected { border: 2px solid #60a5fa; }
        """)
        self.filmstrip.itemClicked.connect(self.on_filmstrip_clicked)
        layout.addWidget(self.filmstrip)

    def setup_gallery_tab(self):
        self.tab_gallery = QWidget()
        layout = QVBoxLayout(self.tab_gallery)
        layout.setContentsMargins(0,0,0,0)
        
        self.grid_view = QListWidget()
        self.grid_view.setViewMode(QListWidget.ViewMode.IconMode)
        self.grid_view.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.grid_view.setSpacing(10) 
        self.grid_view.setIconSize(QSize(150, 150))
        # Enable Multi-Selection
        self.grid_view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        # Enable Double Click Navigation
        self.grid_view.itemDoubleClicked.connect(self.on_grid_double_clicked)
        
        # Important: Set Grid Size explicitly to prevent overlap. Size = Icon + Padding + Text
        self.grid_view.setGridSize(QSize(180, 220))
        self.grid_view.setStyleSheet("""
            QListWidget { background-color: #1a1a1a; padding: 10px; }
            QListWidget::item { color: #ccc; border-radius: 6px; padding: 5px; } 
            QListWidget::item:selected { border: 2px solid #60a5fa; }
            QListWidget::item:hover { background-color: #333; }
        """)
        self.grid_view.itemClicked.connect(self.on_grid_clicked)
        layout.addWidget(self.grid_view)

    def apply_dark_theme(self):
        palette = QPalette()
        palette.setColor(QPalette.ColorRole.Window, QColor(20, 20, 20))
        palette.setColor(QPalette.ColorRole.WindowText, Qt.GlobalColor.white)
        self.setPalette(palette)

    # --- Session Management ---

    def get_session_file(self):
        return os.path.join(self.folder_path, SESSION_FILENAME)

    def save_session(self):
        if not self.folder_path: return
        data = {}
        # Store just filenames to allow folder moving (though marks key is full path)
        for path, status in self.marks.items():
            data[os.path.basename(path)] = status
        
        try:
            with open(self.get_session_file(), 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"Failed to save session: {e}")

    # --- Global State Management ---

    def get_global_config_path(self):
        return os.path.join(os.path.expanduser("~"), GLOBAL_CONFIG_FILENAME)

    def load_global_state(self):
        try:
            config_path = self.get_global_config_path()
            if os.path.exists(config_path):
                with open(config_path, 'r') as f:
                    config = json.load(f)
                
                last_folder = config.get('last_folder')
                last_file = config.get('last_file')
                
                if last_folder and os.path.isdir(last_folder):
                    self.folder_path = last_folder
                    # Pass target filename so scan_folder doesn't needlessly load index 0 first
                    self.scan_folder(target_filename=last_file) 
        except Exception as e:
            print(f"Global state load error: {e}")

    def save_global_state(self):
        if not self.folder_path: return
        
        try:
            current_file = ""
            if self.files and 0 <= self.current_index < len(self.files):
                current_file = os.path.basename(self.files[self.current_index])
                
            config = {
                'last_folder': self.folder_path,
                'last_file': current_file
            }
            
            with open(self.get_global_config_path(), 'w') as f:
                json.dump(config, f)
        except Exception as e:
            print(f"Global state save error: {e}")

    def closeEvent(self, event):
        self.save_global_state()
        super().closeEvent(event)

    # --- Logic ---

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
        self.thumbnail_timer.stop() # Ensure timer is stopped
        self.thread_pool.clear() 
        self.progress_bar.hide()
        
        # Helper to scan a specific directory
        def scan_dir(path):
            found = []
            if os.path.exists(path):
                with os.scandir(path) as entries:
                    for entry in entries:
                        if entry.is_file() and entry.name.lower().endswith(SUPPORTED_EXTENSIONS):
                            found.append(entry.path)
            return found

        try:
            # 1. Scan Root
            root_files = scan_dir(self.folder_path)
            
            # 2. Scan Subfolders (_KEEPS and _REJECTS)
            keep_dir = os.path.join(self.folder_path, "_KEEPS")
            reject_dir = os.path.join(self.folder_path, "_REJECTS")
            
            keep_files = scan_dir(keep_dir)
            reject_files = scan_dir(reject_dir)
            
            # 3. Combine and Sort (by filename, so they appear mixed/in-order)
            all_files = root_files + keep_files + reject_files
            all_files.sort(key=lambda x: os.path.basename(x))
            
            self.files = all_files

            if not self.files:
                self.current_index = -1
                self.lbl_image.setText("No images found")
                self.lbl_filename.setText(self.folder_path)
                self.update_ui_state()
                return

            # 4. Initialize Marks based on Location
            for f in keep_files:
                self.marks[f] = 'keep'
            for f in reject_files:
                self.marks[f] = 'reject'

            # 5. Load Session (Overrides location if present in JSON)
            session_file = self.get_session_file()
            if os.path.exists(session_file):
                try:
                    with open(session_file, 'r') as f:
                        session_data = json.load(f)
                    
                    name_to_path = {os.path.basename(p): p for p in self.files}
                    
                    for fname, status in session_data.items():
                        if fname in name_to_path:
                            self.marks[name_to_path[fname]] = status
                except Exception as e:
                    print(f"Session load error: {e}")

            # 6. Populate Lists (UI Only)
            for i, fpath in enumerate(self.files):
                fname = os.path.basename(fpath)
                
                # Filmstrip Item
                item_f = QListWidgetItem(fname)
                item_f.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.filmstrip.addItem(item_f)
                
                # Grid Item
                item_g = QListWidgetItem(fname)
                item_g.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                # No icon initially to prevent freeze
                self.grid_view.addItem(item_g)
                
                # Add to queue for background processing
                self.thumbnail_queue.append((i, fpath))
            
            # Setup Progress Bar
            self.progress_bar.setRange(0, len(self.files))
            self.progress_bar.setValue(0)
            self.progress_bar.setFormat("Thumbnails: %v / %m")
            
            # DO NOT START TIMER HERE. Only start when Gallery tab is active.
            if self.tabs.currentIndex() == 1:
                self.thumbnail_timer.start()
                self.progress_bar.show()

            # Set Initial Index
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

        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))

    def on_tab_changed(self, index):
        """
        Start generating thumbnails ONLY when the Gallery tab (index 1) is active.
        Stop when switching away to save resources for culling.
        """
        if index == 1: # Gallery Tab
            if self.thumbnail_queue:
                self.thumbnail_timer.start()
                self.progress_bar.show()
        else: # Cull Tab
            self.thumbnail_timer.stop()
            self.progress_bar.hide()

    def process_thumbnail_batch(self):
        """
        Processes a small batch of thumbnails to keep UI responsive.
        """
        if not self.thumbnail_queue:
            self.thumbnail_timer.stop()
            self.progress_bar.hide()
            return
        
        # Process up to 5 items per batch (reduced from 10)
        BATCH_SIZE = 5
        count = 0
        
        while count < BATCH_SIZE and self.thumbnail_queue:
            idx, fpath = self.thumbnail_queue.pop(0)
            
            # Update progress bar
            processed_count = len(self.files) - len(self.thumbnail_queue)
            self.progress_bar.setValue(processed_count)

            # Check if we already have an icon (maybe from previous run?)
            # If not, spawn worker
            worker = ThumbnailWorker(fpath, idx)
            worker.signals.finished.connect(self.update_thumbnail)
            self.thread_pool.start(worker)
            count += 1

    def update_thumbnail(self, index, file_path, pixmap):
        # Verify if the index still matches the file path (in case folder changed)
        if index < len(self.files) and self.files[index] == file_path:
            icon = QIcon(pixmap)
            if index < self.grid_view.count():
                self.grid_view.item(index).setIcon(icon)

    def on_filmstrip_clicked(self, item):
        index = self.filmstrip.row(item)
        if index != -1:
            self.switch_to_image(index)

    def on_grid_clicked(self, item):
        index = self.grid_view.row(item)
        if index != -1:
            # Don't auto-switch in multi-select mode if user is modifying selection
            if len(self.grid_view.selectedItems()) <= 1:
                self.switch_to_image(index)
    
    def on_grid_double_clicked(self, item):
        index = self.grid_view.row(item)
        if index != -1:
            self.switch_to_image(index)
            self.tabs.setCurrentIndex(0) # Switch to Cull Tab

    def switch_to_image(self, index):
        self.current_index = index
        self.load_current_image()
        self.update_ui_state()
        self.setFocus()

    def load_current_image(self):
        if not self.files or self.current_index < 0:
            return

        filepath = self.files[self.current_index]
        self.lbl_filename.setText(os.path.basename(filepath))
        
        self.lbl_image.setText("Loading...")
        
        if self.loader_thread and self.loader_thread.isRunning():
            self.loader_thread.terminate()
            self.loader_thread.wait()

        self.loader_thread = ImageLoaderThread(filepath)
        self.loader_thread.image_loaded.connect(self.on_image_loaded)
        self.loader_thread.start()
        
        self.update_overlay()
        
        # Sync Selections
        self.filmstrip.setCurrentRow(self.current_index)
        self.filmstrip.scrollToItem(self.filmstrip.item(self.current_index), QAbstractItemView.ScrollHint.EnsureVisible)
        # Only clear/set grid selection if not multi-selecting in Gallery
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
            Qt.TransformationMode.SmoothTransformation
        )
        self.lbl_image.setPixmap(scaled_pixmap)

    def update_overlay(self):
        if not self.files: return
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
        # Update colors in both views
        for i in range(len(self.files)):
            fpath = self.files[i]
            status = self.marks.get(fpath)
            is_selected = (i == self.current_index)
            
            # Default Colors
            bg = QColor("#222")
            fg = QColor("#ccc")
            
            # Status Colors (Brighter)
            if status == 'keep':
                bg = QColor("#1b5e20") # Keep Green (Brighter)
                fg = QColor("#66bb6a")
            elif status == 'reject':
                bg = QColor("#b71c1c") # Reject Red (Brighter)
                fg = QColor("#ef5350")
            
            # Selection Highlight
            if is_selected:
                if status == 'keep':
                    bg = QColor("#2e7d32")
                elif status == 'reject':
                    bg = QColor("#c62828")
                else:
                    bg = QColor("#2563eb") # Blue for neutral selection

            # Apply to Filmstrip
            if i < self.filmstrip.count():
                item = self.filmstrip.item(i)
                item.setBackground(bg)
                item.setForeground(fg)

            # Apply to Grid
            if i < self.grid_view.count():
                item = self.grid_view.item(i)
                item.setBackground(bg)
                item.setForeground(fg)
                
                # Update Text to indicate status clearly with Symbols
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
            self.lbl_counter.setText(f"{self.current_index + 1} / {count}")
        else:
            self.lbl_counter.setText("0 / 0")

        keeps = list(self.marks.values()).count('keep')
        rejects = list(self.marks.values()).count('reject')
        
        self.lbl_stat_keep.setText(f"Keeps: {keeps}")
        self.lbl_stat_reject.setText(f"Rejects: {rejects}")

    # --- Actions ---

    def next_image(self):
        if self.current_index < len(self.files) - 1:
            self.switch_to_image(self.current_index + 1)

    def prev_image(self):
        if self.current_index > 0:
            self.switch_to_image(self.current_index - 1)

    def mark_current(self, status):
        if not self.files: return

        # Check for Gallery Multi-Selection
        if self.tabs.currentIndex() == 1:
            selected_items = self.grid_view.selectedItems()
            if len(selected_items) > 0:
                # Apply mark to ALL selected items
                for item in selected_items:
                    idx = self.grid_view.row(item)
                    if idx != -1:
                        path_at_idx = self.files[idx]
                        if status is None:
                            if path_at_idx in self.marks: del self.marks[path_at_idx]
                        else:
                            self.marks[path_at_idx] = status
                
                self.save_session()
                self.update_item_colors() # Refreshes visual state
                self.update_ui_state()
                return # Skip standard single-image logic

        # Standard Single Image Logic (Cull Tab or no selection in Gallery)
        path = self.files[self.current_index]
        
        if status is None:
            if path in self.marks:
                del self.marks[path]
        else:
            self.marks[path] = status
            # Auto advance only in Cull View
            if status is not None and self.tabs.currentIndex() == 0:
                self.next_image()

        self.save_session() # Auto-save session
        self.update_overlay()
        self.update_ui_state()

    def process_files(self):
        # We process ALL files in self.files based on their current marks and location
        # This handles moves from Root->Keep, Keep->Reject, Keep->Root, etc.
        
        keeps_pending = []
        rejects_pending = []
        unmarked_pending = []

        keep_dir = os.path.join(self.folder_path, "_KEEPS")
        reject_dir = os.path.join(self.folder_path, "_REJECTS")

        for fpath in self.files:
            status = self.marks.get(fpath)
            current_dir = os.path.dirname(fpath)
            
            target_dir = self.folder_path # Default to root
            if status == 'keep':
                target_dir = keep_dir
            elif status == 'reject':
                target_dir = reject_dir
            
            # If current location != target location, it needs moving
            # We compare absolute paths
            if os.path.abspath(current_dir) != os.path.abspath(target_dir):
                if status == 'keep': keeps_pending.append(fpath)
                elif status == 'reject': rejects_pending.append(fpath)
                else: unmarked_pending.append(fpath)

        total_moves = len(keeps_pending) + len(rejects_pending) + len(unmarked_pending)
        
        if total_moves == 0:
            QMessageBox.information(self, "Info", "No file movements needed.")
            return

        msg = (f"Processing {total_moves} moves:\n"
               f"{len(keeps_pending)} -> _KEEPS\n"
               f"{len(rejects_pending)} -> _REJECTS\n"
               f"{len(unmarked_pending)} -> Root (Unmarked)\n\n"
               "Proceed?")
               
        reply = QMessageBox.question(self, "Process Files", msg, 
                                   QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        
        if reply == QMessageBox.StandardButton.Yes:
            try:
                if not os.path.exists(keep_dir): os.makedirs(keep_dir)
                if not os.path.exists(reject_dir): os.makedirs(reject_dir)

                def safe_move(src, dest_folder):
                    fname = os.path.basename(src)
                    dest = os.path.join(dest_folder, fname)
                    if src != dest:
                        shutil.move(src, dest)

                for f in keeps_pending: safe_move(f, keep_dir)
                for f in rejects_pending: safe_move(f, reject_dir)
                for f in unmarked_pending: safe_move(f, self.folder_path)

                QMessageBox.information(self, "Success", "Files moved successfully!")
                self.save_session() # Save final state
                self.scan_folder() # Refresh view

            except Exception as e:
                QMessageBox.critical(self, "Error", f"An error occurred:\n{str(e)}")

    # --- Keyboard ---

    def keyPressEvent(self, event):
        key = event.key()
        
        # Navigation & Arrow-based Marking (Context Sensitive)
        # If a widget (like Grid) consumes these, this event won't fire, preserving Grid navigation.
        # If no widget consumes them (Cull mode), they trigger actions.
        
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

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = CullSpeedApp()
    window.show()
    sys.exit(app.exec())