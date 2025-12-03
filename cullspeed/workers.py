import os
import rawpy
from PyQt6.QtCore import QObject, pyqtSignal, QRunnable, QThread
from PyQt6.QtGui import QImage, QPixmap
from PyQt6.QtCore import Qt

from .image_utils import ImageUtils


class WorkerSignals(QObject):
    finished = pyqtSignal(int, str, QPixmap)


class ThumbnailWorker(QRunnable):
    """Background worker for generating thumbnails without blocking UI."""

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
                                image = QImage()
                        else:
                            rgb = raw.postprocess(use_camera_wb=True, half_size=True)
                            h, w, _ = rgb.shape
                            image = QImage(
                                rgb.data,
                                w,
                                h,
                                w * 3,
                                QImage.Format.Format_RGB888,
                            )

                        if not image.isNull():
                            pixmap = QPixmap.fromImage(image)
                except Exception as exc:
                    self.image_loaded.emit(QPixmap(), f"RAW Error: {exc}")
                    return
            else:
                pixmap = QPixmap(self.file_path)

            if pixmap and not pixmap.isNull():
                if pixmap.width() > 3000 or pixmap.height() > 3000:
                    pixmap = pixmap.scaled(
                        3000,
                        3000,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                self.image_loaded.emit(pixmap, "")
            else:
                self.image_loaded.emit(QPixmap(), "Could not load image")

        except Exception as exc:
            self.image_loaded.emit(QPixmap(), str(exc))
