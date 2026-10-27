"""Image decoding.

Everything here runs in worker threads, so it only ever produces QImage
(QPixmap must only be created on the GUI thread).
"""
import os

import numpy as np
import rawpy
from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, QSize, Qt
from PyQt6.QtGui import QImage, QImageIOHandler, QImageReader, QTransform

from .constants import RAW_EXTENSIONS


class ImageLoadError(Exception):
    pass


# LibRaw "flip" value -> clockwise rotation in degrees.
_FLIP_ROTATION = {3: 180, 5: 270, 6: 90}

# At full resolution, an embedded preview smaller than this fraction of the
# sensor is not good enough to judge focus: develop the RAW instead.
_FULL_PREVIEW_MIN_RATIO = 0.6


def is_raw(path):
    return os.path.splitext(path)[1].lower() in RAW_EXTENSIONS


def load_image(path, max_size):
    """Decode `path` into a QImage whose longest edge is at most `max_size`.

    Returns `(image, native_size)`: the size of the photo itself, upright,
    which is what a full-resolution decode of it would give. A downscaled
    preview can then be shown at true 1:1 scale while zoomed.
    `max_size=None` decodes at full resolution (for 1:1 zoom).
    """
    image, native = _load_raw(path, max_size) if is_raw(path) else _read(path, max_size)
    return _fit(image, max_size), native


def _long_edge(size):
    return max(size.width(), size.height())


def _fit(image, max_size):
    if max_size is None:
        return image
    if image.width() > max_size or image.height() > max_size:
        return image.scaled(
            max_size,
            max_size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    return image


def _quarter_turn(flip):
    return _FLIP_ROTATION.get(flip) in (90, 270)


def _rotate(image, flip):
    angle = _FLIP_ROTATION.get(flip)
    if not angle:
        return image
    return image.transformed(QTransform().rotate(angle), Qt.TransformationMode.SmoothTransformation)


def _read(source, max_size, raw_flip=0):
    """Read a file path or QIODevice, honouring EXIF orientation; returns (image, native size).

    `raw_flip` is the RAW file's own orientation, applied only when the
    embedded preview carries no orientation of its own.
    """
    reader = QImageReader(source)
    reader.setAutoTransform(True)

    # Let the decoder downscale (much faster for JPEG) when the target is far
    # smaller than the source; _fit() then does the final smooth resize.
    size = reader.size()
    if max_size is not None and size.isValid() and _long_edge(size) > max_size * 2:
        reader.setScaledSize(
            size.scaled(max_size * 2, max_size * 2, Qt.AspectRatioMode.KeepAspectRatio)
        )

    image = reader.read()
    if image.isNull():
        raise ImageLoadError(reader.errorString())

    transform = reader.transformation()
    rotate_raw = bool(raw_flip) and transform == QImageIOHandler.Transformation.TransformationNone
    if rotate_raw:
        image = _rotate(image, raw_flip)

    if not size.isValid():
        return image, image.size()  # not downscaled, so already the native size
    quarter = _quarter_turn(raw_flip) if rotate_raw else bool(transform & QImageIOHandler.Transformation.TransformationRotate90)
    return image, size.transposed() if quarter else QSize(size)


def _load_raw(path, max_size):
    with rawpy.imread(path) as raw:
        sensor = QSize(raw.sizes.width, raw.sizes.height)
        if _quarter_turn(raw.sizes.flip):
            sensor = sensor.transposed()

        preview = _embedded_preview(raw, max_size)
        if preview is not None:
            image, native = preview
            if _long_edge(native) >= _long_edge(sensor) * _FULL_PREVIEW_MIN_RATIO:
                return image, native
            if max_size is not None:
                return image, sensor  # fine to look at; full resolution develops the RAW

        # No usable embedded preview: develop the RAW (postprocess applies flip itself).
        developed = raw.postprocess(use_camera_wb=True, half_size=max_size is not None, output_bps=8)
        return _from_array(developed), sensor


def _embedded_preview(raw, max_size):
    flip = raw.sizes.flip
    try:
        thumb = raw.extract_thumb()
    except rawpy.LibRawError:
        return None

    if thumb.format == rawpy.ThumbFormat.JPEG:
        buffer = QBuffer()
        buffer.setData(QByteArray(bytes(thumb.data)))
        buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        try:
            return _read(buffer, max_size, flip)
        except ImageLoadError:
            return None  # corrupt preview: fall back to developing the RAW
        finally:
            buffer.close()

    if thumb.format == rawpy.ThumbFormat.BITMAP:
        image = _rotate(_from_array(thumb.data), flip)
        return image, image.size()
    return None


def _from_array(array):
    array = np.ascontiguousarray(array, dtype=np.uint8)
    height, width = array.shape[:2]
    if array.ndim == 2 or array.shape[2] == 1:
        fmt, bytes_per_line = QImage.Format.Format_Grayscale8, width
    else:
        array = np.ascontiguousarray(array[:, :, :3])
        fmt, bytes_per_line = QImage.Format.Format_RGB888, width * 3
    # QImage only borrows `data`; copy() so the result owns its pixels.
    data = array.tobytes()
    image = QImage(data, width, height, bytes_per_line, fmt)
    return image.copy()
