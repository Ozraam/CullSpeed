"""Background image loading with caching and prefetching.

Workers only decode QImages; results come back to the GUI thread through
queued signals on this long-lived QObject, where they become QPixmaps.
Superseded requests are skipped rather than terminated.
"""
import math
from collections import OrderedDict
from contextlib import suppress
from itertools import count

from PyQt6.QtCore import QObject, QSize, QThread, QThreadPool, pyqtSignal
from PyQt6.QtGui import QGuiApplication, QImage, QPixmap

from .constants import PREVIEW_CACHE_SIZE, PREVIEW_MAX_SIZE, THUMBNAIL_CACHE_BYTES, THUMBNAIL_MAX_SIZE
from .exif import read_info
from .imaging import load_image

_CANCELLED = "\0cancelled"


def _emit(signal, *args):
    with suppress(RuntimeError):  # loader already destroyed during shutdown
        signal.emit(*args)


def _pixmap_bytes(pixmap):
    return pixmap.width() * pixmap.height() * pixmap.depth() // 8


def _thumbnail_decode_size():
    """THUMBNAIL_MAX_SIZE in device pixels, so thumbnails stay sharp on HiDPI screens."""
    app = QGuiApplication.instance()
    ratio = app.devicePixelRatio() if app is not None else 1.0
    return math.ceil(THUMBNAIL_MAX_SIZE * ratio)


def _decode(path, max_size):
    """(image, native size, error message)."""
    try:
        image, native = load_image(path, max_size)
        return image, native, ""
    except Exception as exc:  # decoding must never kill a worker thread
        return QImage(), QSize(), str(exc) or type(exc).__name__


class ImageLoader(QObject):
    preview_loaded = pyqtSignal(str)
    preview_failed = pyqtSignal(str, str)
    thumbnail_loaded = pyqtSignal(str)
    thumbnail_failed = pyqtSignal(str)
    full_loaded = pyqtSignal(str)
    full_failed = pyqtSignal(str, str)

    # Internal: emitted from worker threads, delivered on the GUI thread.
    _preview_done = pyqtSignal(int, str, QImage, QSize, object, str)
    _full_done = pyqtSignal(int, str, QImage, str)
    _thumbnail_done = pyqtSignal(int, str, QImage)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._preview_pool = QThreadPool(self)
        self._preview_pool.setMaxThreadCount(2)
        self._thumbnail_pool = QThreadPool(self)
        self._thumbnail_pool.setMaxThreadCount(max(2, min(6, QThread.idealThreadCount() - 2)))

        self._generation = 0
        self._priority = count()

        self._previews = OrderedDict()  # path -> QPixmap, LRU order
        self._preview_errors = {}
        self._preview_pending = set()
        self._wanted_previews = set()
        self._native_sizes = {}  # path -> QSize of the photo itself
        self._info = {}  # path -> EXIF summary, read alongside the preview

        # Full resolution (for 1:1 zoom) is huge: only kept for the photos on
        # screen while zoomed (one, or a few side by side in Compare).
        self._full = {}  # path -> QPixmap
        self._full_wanted = set()
        self._full_pending = set()
        self._full_errors = {}

        self._thumbnails = OrderedDict()  # path -> QPixmap, LRU order
        self._thumbnail_bytes = 0
        self._thumbnail_failed = set()
        self._thumbnail_pending = set()

        self._preview_done.connect(self._on_preview_done)
        self._full_done.connect(self._on_full_done)
        self._thumbnail_done.connect(self._on_thumbnail_done)

    # -- lifecycle ---------------------------------------------------------

    def reset(self):
        """Forget everything; results of in-flight jobs will be ignored."""
        self._generation += 1
        self._preview_pool.clear()
        self._thumbnail_pool.clear()
        self._previews.clear()
        self._preview_errors.clear()
        self._preview_pending.clear()
        self._wanted_previews = set()
        self._native_sizes.clear()
        self._info.clear()
        self._full.clear()
        self._full_wanted = set()
        self._full_pending.clear()
        self._full_errors.clear()
        self._thumbnails.clear()
        self._thumbnail_bytes = 0
        self._thumbnail_failed.clear()
        self._thumbnail_pending.clear()

    def shutdown(self):
        self.reset()
        self._preview_pool.waitForDone()
        self._thumbnail_pool.waitForDone()

    def rename_paths(self, mapping):
        """Carry cached images over after files have been moved."""
        caches = (self._previews, self._preview_errors, self._native_sizes, self._info, self._thumbnails,
                  self._full, self._full_errors)
        for cache in caches:
            for old, new in mapping.items():
                if old in cache:
                    cache[new] = cache.pop(old)
        for paths in (self._thumbnail_failed, self._full_wanted):
            for old, new in mapping.items():
                if old in paths:
                    paths.discard(old)
                    paths.add(new)

    # -- previews ----------------------------------------------------------

    def preview(self, path):
        pixmap = self._previews.get(path)
        if pixmap is not None:
            self._previews.move_to_end(path)
        return pixmap

    def preview_error(self, path):
        return self._preview_errors.get(path)

    def native_size(self, path):
        return self._native_sizes.get(path)

    def info(self, path):
        return self._info.get(path, {})

    def ensure_previews(self, current, neighbors):
        """Load `current` first, then prefetch `neighbors`; drop other queued work."""
        self._wanted_previews = {current, *neighbors}
        # Coming back to a photo retries it: the error may have been transient
        # (file still copying, network share hiccup). Neighbours are not retried.
        self._preview_errors.pop(current, None)
        self._request_preview(current, urgent=True)
        for path in neighbors:
            self._request_preview(path, urgent=False)

    def _request_preview(self, path, urgent):
        if path in self._previews or path in self._preview_errors or path in self._preview_pending:
            return
        self._preview_pending.add(path)
        generation = self._generation

        def job():
            if generation != self._generation or path not in self._wanted_previews:
                _emit(self._preview_done, generation, path, QImage(), QSize(), {}, _CANCELLED)
                return
            image, native, error = _decode(path, PREVIEW_MAX_SIZE)
            info = {} if error else read_info(path)
            _emit(self._preview_done, generation, path, image, native, info, error)

        self._preview_pool.start(job, 1 if urgent else 0)

    def _on_preview_done(self, generation, path, image, native, info, error):
        if generation != self._generation:
            return
        self._preview_pending.discard(path)
        if error == _CANCELLED:
            return
        if error:
            self._preview_errors[path] = error
            self.preview_failed.emit(path, error)
            return

        # Converted here, ahead of time for prefetched photos, so that
        # stepping to a photo does not pay for QPixmap.fromImage().
        self._previews[path] = QPixmap.fromImage(image)
        self._native_sizes[path] = native
        self._info[path] = info
        self._trim_previews()
        self.preview_loaded.emit(path)

    def _trim_previews(self):
        for path in list(self._previews):
            if len(self._previews) <= PREVIEW_CACHE_SIZE:
                break
            if path not in self._wanted_previews:
                del self._previews[path]

    # -- full resolution ---------------------------------------------------

    def full_pixmap(self, path):
        return self._full.get(path)

    def full_error(self, path):
        return self._full_errors.get(path)

    def ensure_full(self, paths, load=True):
        """Keep full resolution for `paths` only; decode the missing ones if `load`.

        With `load=False` nothing new is decoded, but what is already loaded
        for `paths` stays, so zooming back in is instant.
        """
        self._full_wanted = set(paths)
        for path in list(self._full):
            if path not in self._full_wanted:
                del self._full[path]
        if not load:
            return
        for path in paths:
            if path in self._full or path in self._full_pending or path in self._full_errors:
                continue
            self._full_pending.add(path)
            self._preview_pool.start(self._full_job(path, self._generation), 2)

    def _full_job(self, path, generation):
        def job():
            if generation != self._generation or path not in self._full_wanted:
                _emit(self._full_done, generation, path, QImage(), _CANCELLED)
                return
            image, _native, error = _decode(path, None)
            _emit(self._full_done, generation, path, image, error)

        return job

    def _on_full_done(self, generation, path, image, error):
        if generation != self._generation:
            return
        self._full_pending.discard(path)
        if error == _CANCELLED or path not in self._full_wanted:
            return
        if error:
            self._full_errors[path] = error
            self.full_failed.emit(path, error)
            return
        self._full[path] = QPixmap.fromImage(image)
        self.full_loaded.emit(path)

    # -- thumbnails --------------------------------------------------------

    def thumbnail(self, path):
        """Return the cached thumbnail, or None and queue it for loading."""
        pixmap = self._thumbnails.get(path)
        if pixmap is not None:
            self._thumbnails.move_to_end(path)
        elif path not in self._thumbnail_pending and path not in self._thumbnail_failed:
            self._request_thumbnail(path)
        return pixmap

    def thumbnail_error(self, path):
        return path in self._thumbnail_failed

    def _request_thumbnail(self, path):
        self._thumbnail_pending.add(path)
        generation = self._generation
        size = _thumbnail_decode_size()

        def job():
            if generation != self._generation:
                return
            image, _native, _error = _decode(path, size)
            _emit(self._thumbnail_done, generation, path, image)

        # Most recently requested first, so whatever is on screen now wins.
        self._thumbnail_pool.start(job, next(self._priority) % 2**30)

    def _on_thumbnail_done(self, generation, path, image):
        if generation != self._generation:
            return
        self._thumbnail_pending.discard(path)
        if image.isNull():
            self._thumbnail_failed.add(path)
            self.thumbnail_failed.emit(path)
            return
        pixmap = QPixmap.fromImage(image)
        self._thumbnails[path] = pixmap
        self._thumbnail_bytes += _pixmap_bytes(pixmap)
        while self._thumbnail_bytes > THUMBNAIL_CACHE_BYTES and len(self._thumbnails) > 1:
            _old, evicted = self._thumbnails.popitem(last=False)  # reloaded if scrolled back to
            self._thumbnail_bytes -= _pixmap_bytes(evicted)
        self.thumbnail_loaded.emit(path)
