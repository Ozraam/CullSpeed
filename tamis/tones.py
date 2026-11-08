"""What is shown, read for exposure: a histogram and an overlay of the clipped pixels.

Same pattern as the other engines: the worker only produces a QImage (and plain
numbers), which becomes a QPixmap on the GUI thread, and results of an older
generation are ignored. Results are found by the cacheKey() of the pixmap they
were made from, so an edit or a crop gets its own, and moving files changes nothing.
"""
from collections import OrderedDict, namedtuple
from contextlib import suppress

import numpy as np
from PyQt6.QtCore import QObject, Qt, QThreadPool, pyqtSignal
from PyQt6.QtGui import QImage, QPixmap

from .workers import emit_safely

# A little inside the limits: JPEG compression leaves blown areas speckled with 254.
HIGHLIGHT = 254  # a channel at or above this has lost its detail
SHADOW = 1  # every channel at or below this is black
HIGHLIGHT_COLOR = (255, 40, 40)  # every channel clipped: nothing is left there
PARTIAL_COLOR = (255, 150, 0)  # one or two: a saturated colour, flatter but not blown out
PARTIAL_ALPHA = 130  # discreet, so that the photo still shows through
SHADOW_COLOR = (50, 120, 255)
_HISTOGRAM_SIZE = 1024  # longest edge a histogram is counted at
_CACHE_SIZE = 8

# `counts`: 3×256 array, for red, green and blue; `shadows`, `highlights`: fractions of the pixels fully clipped.
Tone = namedtuple("Tone", "counts shadows highlights")


def _rgb(image):
    """QImage -> uint8 array h×w×3 of its pixels, and the image they live in, to keep alive."""
    image = image.convertToFormat(QImage.Format.Format_RGB888)
    pointer = image.constBits()
    pointer.setsize(image.sizeInBytes())
    rows = np.frombuffer(pointer, dtype=np.uint8).reshape(image.height(), image.bytesPerLine())
    return rows[:, :image.width() * 3].reshape(image.height(), image.width(), 3), image


def clipped(rgb):
    """(shadows, highlights, partial): boolean h×w arrays.

    `shadows` and `highlights` have no detail left in any channel; `partial` has
    one or two channels at the top, like a vivid yellow with its red at 255.
    """
    brightest = rgb.max(axis=2)
    highlights = rgb.min(axis=2) >= HIGHLIGHT
    return brightest <= SHADOW, highlights, (brightest >= HIGHLIGHT) & ~highlights


def tone_of(image):
    """The Tone of a QImage."""
    if max(image.width(), image.height()) > _HISTOGRAM_SIZE:
        # Picks pixels instead of averaging them, which would hide what is clipped.
        image = image.scaled(
            _HISTOGRAM_SIZE, _HISTOGRAM_SIZE, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.FastTransformation
        )
    rgb, _keep = _rgb(image)
    counts = np.stack([np.bincount(rgb[..., channel].ravel(), minlength=256) for channel in range(3)])
    shadows, highlights, _partial = clipped(rgb)
    return Tone(counts, float(shadows.mean()), float(highlights.mean()))


def clipping_image(image):
    """QImage, transparent but for the clipped pixels of `image`."""
    rgb, _keep = _rgb(image)
    shadows, highlights, partial = clipped(rgb)
    height, width = rgb.shape[:2]
    pixels = np.zeros((height, width, 4), dtype=np.uint8)
    pixels[shadows] = (*SHADOW_COLOR, 255)
    pixels[highlights] = (*HIGHLIGHT_COLOR, 255)
    pixels[partial] = (*PARTIAL_COLOR, PARTIAL_ALPHA)
    data = pixels.tobytes()
    return QImage(data, width, height, width * 4, QImage.Format.Format_RGBA8888).copy()  # copy(): owns its pixels


class ToneReader(QObject):
    ready = pyqtSignal()

    _done = pyqtSignal(int, object, object)  # generation, (kind, cacheKey), a Tone or a QImage; None if it failed

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._generation = 0
        self._results = OrderedDict()  # (kind, pixmap's cacheKey) -> Tone or QPixmap, LRU order
        self._pending = set()
        self._wanted = set()
        self._done.connect(self._on_done)

    def reset(self):
        self._generation += 1
        self._pool.clear()
        self._results.clear()
        self._pending.clear()
        self._wanted = set()

    def shutdown(self):
        self.reset()
        self._pool.waitForDone()

    def begin(self):
        """A new look at what is shown: what isn't asked for from now on is skipped if still queued."""
        self._wanted = set()

    def tone(self, pixmap):
        """The Tone of `pixmap`, or None and it gets worked out: `ready` tells when."""
        return self._get("tone", pixmap, tone_of)

    def clipping(self, pixmap):
        """The clipping overlay of `pixmap`, or None and it gets made: `ready` tells when."""
        return self._get("clipping", pixmap, clipping_image)

    def _get(self, kind, pixmap, work):
        key = (kind, pixmap.cacheKey())
        self._wanted.add(key)
        result = self._results.get(key)
        if result is not None:
            self._results.move_to_end(key)
            return result
        if key not in self._pending:
            self._pending.add(key)
            generation = self._generation
            image = pixmap.toImage()

            def job():
                result = None
                if generation == self._generation and key in self._wanted:
                    with suppress(Exception):  # a bad photo must not kill a worker
                        result = work(image)
                emit_safely(self._done, generation, key, result)

            self._pool.start(job)
        return None

    def _on_done(self, generation, key, result):
        if generation != self._generation:
            return
        self._pending.discard(key)
        if result is None:
            return
        self._results[key] = QPixmap.fromImage(result) if isinstance(result, QImage) else result
        while len(self._results) > _CACHE_SIZE:
            self._results.popitem(last=False)
        self.ready.emit()
