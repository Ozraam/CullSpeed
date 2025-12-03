from PyQt6.QtCore import Qt, QSize, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
    QProgressBar,
)


class HeaderBar(QFrame):
    open_folder_requested = pyqtSignal()
    process_requested = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setStyleSheet("background-color: #1a1a1a; border-bottom: 1px solid #333;")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        self.lbl_title = QLabel("CullSpeed")
        self.lbl_title.setStyleSheet("font-weight: bold; font-size: 16px; color: #60a5fa;")

        self.lbl_filename = QLabel("No Folder Selected")
        self.lbl_filename.setStyleSheet("color: #9ca3af; font-size: 14px;")

        self.progress_bar = QProgressBar()
        self.progress_bar.setFixedWidth(200)
        self.progress_bar.setStyleSheet(
            """
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
            """
        )
        self.progress_bar.hide()

        self.lbl_counter = QLabel("")
        self.lbl_counter.setStyleSheet("background-color: #333; color: #bbb; padding: 4px 8px; border-radius: 4px;")

        self.btn_open = QPushButton("Open Folder")
        self.btn_open.setStyleSheet(
            """
            QPushButton { background-color: #2563eb; color: white; border: none; padding: 6px 12px; border-radius: 4px; font-weight: bold; }
            QPushButton:hover { background-color: #1d4ed8; }
            """
        )
        self.btn_open.clicked.connect(self.open_folder_requested)

        self.btn_process = QPushButton("Process Files")
        self.btn_process.setStyleSheet(
            """
            QPushButton { background-color: #333; color: white; border: 1px solid #555; padding: 6px 12px; border-radius: 4px; }
            QPushButton:hover { background-color: #444; }
            """
        )
        self.btn_process.clicked.connect(self.process_requested)

        layout.addWidget(self.lbl_title)
        layout.addSpacing(10)
        layout.addWidget(self.lbl_filename)
        layout.addStretch()
        layout.addWidget(self.progress_bar)
        layout.addWidget(self.lbl_counter)
        layout.addWidget(self.btn_open)
        layout.addWidget(self.btn_process)

    def set_filename(self, text: str):
        self.lbl_filename.setText(text)

    def set_counter(self, text: str):
        self.lbl_counter.setText(text)

    def configure_progress(self, maximum: int):
        self.progress_bar.setRange(0, maximum)
        self.progress_bar.setValue(0)

    def update_progress(self, value: int):
        self.progress_bar.setValue(value)

    def set_progress_visible(self, visible: bool):
        self.progress_bar.setVisible(visible)

    def set_progress_format(self, fmt: str):
        self.progress_bar.setFormat(fmt)


class StatsBar(QFrame):
    def __init__(self):
        super().__init__()
        self.setStyleSheet("background-color: #1a1a1a; border-top: 1px solid #333;")

        layout = QHBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setContentsMargins(0, 0, 0, 0)

        self.lbl_stat_keep = self._create_stat_label("Keeps: 0", "#4ade80")
        self.lbl_stat_reject = self._create_stat_label("Rejects: 0", "#f87171")

        layout.addWidget(self.lbl_stat_keep)
        layout.addWidget(QLabel("|", parent=self))
        layout.addWidget(self.lbl_stat_reject)

    @staticmethod
    def _create_stat_label(text, color):
        lbl = QLabel(text)
        lbl.setStyleSheet(f"color: {color}; font-weight: bold; font-size: 14px; margin: 0 10px;")
        return lbl

    def update_counts(self, keeps: int, rejects: int):
        self.lbl_stat_keep.setText(f"Keeps: {keeps}")
        self.lbl_stat_reject.setText(f"Rejects: {rejects}")


class CullView(QWidget):
    filmstrip_clicked = pyqtSignal(int)

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.image_container = QWidget()
        self.image_container.setStyleSheet("background-color: #000;")
        img_layout = QVBoxLayout(self.image_container)
        img_layout.setContentsMargins(0, 0, 0, 0)

        self.lbl_image = QLabel()
        self.lbl_image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_image.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        img_layout.addWidget(self.lbl_image)

        self.lbl_overlay = QLabel(self.image_container)
        self.lbl_overlay.hide()

        layout.addWidget(self.image_container, stretch=1)

        self.filmstrip = QListWidget()
        self.filmstrip.setViewMode(QListWidget.ViewMode.ListMode)
        self.filmstrip.setFlow(QListWidget.Flow.LeftToRight)
        self.filmstrip.setWrapping(False)
        self.filmstrip.setSpacing(5)
        self.filmstrip.setFixedHeight(60)
        self.filmstrip.setStyleSheet(
            """
            QListWidget { background-color: #111; border-top: 1px solid #333; }
            QListWidget::item { color: #888; border-radius: 6px; margin: 2px; padding: 5px; }
            QListWidget::item:selected { border: 2px solid #60a5fa; }
            """
        )
        self.filmstrip.itemClicked.connect(self._emit_filmstrip_click)
        layout.addWidget(self.filmstrip)

    def _emit_filmstrip_click(self, item):
        index = self.filmstrip.row(item)
        if index != -1:
            self.filmstrip_clicked.emit(index)


class GalleryView(QWidget):
    item_clicked = pyqtSignal(int)
    item_double_clicked = pyqtSignal(int)

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.grid_view = QListWidget()
        self.grid_view.setViewMode(QListWidget.ViewMode.IconMode)
        self.grid_view.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.grid_view.setSpacing(10)
        self.grid_view.setIconSize(QSize(150, 150))
        self.grid_view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.grid_view.setGridSize(QSize(180, 220))
        self.grid_view.setStyleSheet(
            """
            QListWidget { background-color: #1a1a1a; padding: 10px; }
            QListWidget::item { color: #ccc; border-radius: 6px; padding: 5px; }
            QListWidget::item:selected { border: 2px solid #60a5fa; }
            QListWidget::item:hover { background-color: #333; }
            """
        )
        self.grid_view.itemClicked.connect(self._emit_item_click)
        self.grid_view.itemDoubleClicked.connect(self._emit_item_double_click)

        layout.addWidget(self.grid_view)

    def _emit_item_click(self, item):
        index = self.grid_view.row(item)
        if index != -1:
            self.item_clicked.emit(index)

    def _emit_item_double_click(self, item):
        index = self.grid_view.row(item)
        if index != -1:
            self.item_double_clicked.emit(index)
