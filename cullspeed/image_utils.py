import os
import rawpy
from PyQt6.QtGui import QImage, QPixmap
from PyQt6.QtCore import Qt


class ImageUtils:
    """Helper utilities for loading scaled thumbnails."""

    @staticmethod
    def load_thumbnail(file_path, target_size=200):
        """Return a QPixmap thumbnail for the given file if possible."""
        try:
            ext = os.path.splitext(file_path)[1].lower()
            image = QImage()

            # RAW handling uses embedded thumbnails for speed.
            if ext in ('.arw', '.cr2', '.nef', '.dng', '.orf', '.raf', '.rw2'):
                with rawpy.imread(file_path) as raw:
                    try:
                        thumb = raw.extract_thumb()
                    except rawpy.LibRawNoThumbnailError:
                        return None

                    if thumb.format == rawpy.ThumbFormat.JPEG:
                        image.loadFromData(thumb.data)
                    else:
                        return None
            else:
                image.load(file_path)

            if image.isNull():
                return None

            return QPixmap.fromImage(
                image.scaled(
                    target_size,
                    target_size,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.FastTransformation,
                )
            )
        except Exception:
            return None
