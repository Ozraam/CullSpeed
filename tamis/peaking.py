"""Focus peaking: an overlay marking the high-contrast edges of what is shown.

Same pattern as the other engines: the worker only produces a QImage, which
becomes a QPixmap on the GUI thread, and results of an older generation are
ignored. Overlays are found by the cacheKey() of the pixmap they were made from,
so an edit or a crop gets its own, and moving files changes nothing.
"""
from collections import OrderedDict
from contextlib import suppress

import numpy as np
from PyQt6.QtCore import QObject, QThreadPool, pyqtSignal
from PyQt6.QtGui import QImage, QPixmap

from .imaging import gray_array

# Difference between neighbouring pixels (out of 255) that counts as in focus.
# A fixed value, like on cameras: a photo that is sharp nowhere must show nothing.
THRESHOLD = 34
COLOR = (255, 45, 45)
_CACHE_SIZE = 6


def peak_mask(gray):
    """Boolean h×w array of the sharp edges of a uint8 h×w array, thickened to stay visible once scaled down."""
    image = gray.astype(np.int16)
    edges = np.zeros(gray.shape, dtype=bool)
    edges[:, :-1] |= np.abs(image[:, 1:] - image[:, :-1]) > THRESHOLD
    edges[:-1, :] |= np.abs(image[1:, :] - image[:-1, :]) > THRESHOLD
    thick = edges.copy()
    thick[1:, :] |= edges[:-1, :]
    thick[:-1, :] |= edges[1:, :]
    thick[:, 1:] |= edges[:, :-1]
    thick[:, :-1] |= edges[:, 1:]
    return thick


def overlay_image(image):
    """QImage, transparent but for the sharp edges of `image`."""
    mask = peak_mask(gray_array(image))
    height, width = mask.shape
    pixels = np.zeros((height, width, 4), dtype=np.uint8)
    pixels[mask] = (*COLOR, 255)
    data = pixels.tobytes()
    return QImage(data, width, height, width * 4, QImage.Format.Format_RGBA8888).copy()  # copy(): owns its pixels


class FocusPeaker(QObject):
    ready = pyqtSignal()

    _done = pyqtSignal(int, object, QImage)  # (object: a cacheKey doesn't fit an int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._generation = 0
        self._overlays = OrderedDict()  # pixmap's cacheKey -> QPixmap, LRU order
        self._pending = set()
        self._wanted = set()
        self._done.connect(self._on_done)

    def reset(self):
        self._generation += 1
        self._pool.clear()
        self._overlays.clear()
        self._pending.clear()
        self._wanted = set()

    def shutdown(self):
        self.reset()
        self._pool.waitForDone()

    def begin(self):
        """A new look at what is shown: what overlay() isn't asked for from now on is skipped if still queued."""
        self._wanted = set()

    def overlay(self, pixmap):
        """The overlay for `pixmap`, or None and it gets made: `ready` tells when."""
        key = pixmap.cacheKey()
        self._wanted.add(key)
        overlay = self._overlays.get(key)
        if overlay is not None:
            self._overlays.move_to_end(key)
            return overlay
        if key not in self._pending:
            self._pending.add(key)
            generation = self._generation
            image = pixmap.toImage()

            def job():
                result = QImage()
                if generation == self._generation and key in self._wanted:
                    with suppress(Exception):  # a bad photo must not kill a worker
                        result = overlay_image(image)
                with suppress(RuntimeError):  # already destroyed during shutdown
                    self._done.emit(generation, key, result)

            self._pool.start(job)
        return None

    def _on_done(self, generation, key, image):
        if generation != self._generation:
            return
        self._pending.discard(key)
        if image.isNull():
            return
        self._overlays[key] = QPixmap.fromImage(image)
        while len(self._overlays) > _CACHE_SIZE:
            self._overlays.popitem(last=False)
        self.ready.emit()
