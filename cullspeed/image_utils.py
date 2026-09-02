import os
import rawpy
from PyQt6.QtCore import QByteArray, QBuffer, QIODevice, Qt
from PyQt6.QtGui import QImage, QImageReader, QPixmap


class ImageUtils:
    """Helper utilities for loading scaled thumbnails."""

    @staticmethod
    def _read_image_with_orientation(source):
        reader = QImageReader(source)
        reader.setAutoTransform(True)
        image = reader.read()
        if image.isNull():
            return None
        return image

    @staticmethod
    def _read_image_from_bytes(data):
        byte_array = QByteArray(data)
        buffer = QBuffer(byte_array)
        buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        try:
            return ImageUtils._read_image_with_orientation(buffer)
        finally:
            buffer.close()

    @staticmethod
    def load_thumbnail(file_path, target_size=200):
        """Return a QPixmap thumbnail for the given file if possible."""
        try:
            ext = os.path.splitext(file_path)[1].lower()
            image = None

            # RAW handling uses embedded thumbnails for speed.
            if ext in ('.arw', '.cr2', '.nef', '.dng', '.orf', '.raf', '.rw2'):
                with rawpy.imread(file_path) as raw:
                    try:
                        thumb = raw.extract_thumb()
                    except rawpy.LibRawNoThumbnailError:
                        return None

                    if thumb.format == rawpy.ThumbFormat.JPEG:
                        image = ImageUtils._read_image_from_bytes(thumb.data)
                    else:
                        return None
            else:
                image = ImageUtils._read_image_with_orientation(file_path)

            if image is None or image.isNull():
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
