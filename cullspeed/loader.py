"""Background image loading with caching and prefetching.

Workers only decode QImages; results come back to the GUI thread through
queued signals on this long-lived QObject, where they become QPixmaps.
Superseded requests are skipped rather than terminated.
"""
from collections import OrderedDict
from itertools import count

from PyQt6.QtCore import QObject, QThread, QThreadPool, pyqtSignal
from PyQt6.QtGui import QImage, QPixmap

from .constants import PREVIEW_CACHE_SIZE, PREVIEW_MAX_SIZE, THUMBNAIL_CACHE_SIZE, THUMBNAIL_MAX_SIZE
from .imaging import load_image

_CANCELLED = "\0cancelled"


def _emit(signal, *args):
    try:
        signal.emit(*args)
    except RuntimeError:
        pass  # loader already destroyed during shutdown


def _decode(path, max_size):
    try:
        return load_image(path, max_size), ""
    except Exception as exc:  # decoding must never kill a worker thread
        return QImage(), str(exc) or type(exc).__name__


class ImageLoader(QObject):
    preview_loaded = pyqtSignal(str)
    preview_failed = pyqtSignal(str, str)
    thumbnail_loaded = pyqtSignal(str)
    full_loaded = pyqtSignal(str)
    full_failed = pyqtSignal(str, str)

    # Internal: emitted from worker threads, delivered on the GUI thread.
    _preview_done = pyqtSignal(int, str, QImage, str)
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

        self._previews = OrderedDict()  # path -> QImage, LRU order
        self._preview_errors = {}
        self._preview_pending = set()
        self._wanted_previews = set()

        # Full resolution (for 1:1 zoom) is kept for one photo only: it is huge.
        self._full_path = None
        self._full_image = None

        self._thumbnails = OrderedDict()  # path -> QPixmap, LRU order
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
        self._release_full()
        self._thumbnails.clear()
        self._thumbnail_failed.clear()
        self._thumbnail_pending.clear()

    def shutdown(self):
        self.reset()
        self._preview_pool.waitForDone()
        self._thumbnail_pool.waitForDone()

    def rename_paths(self, mapping):
        """Carry cached images over after files have been moved."""
        for cache in (self._previews, self._preview_errors, self._thumbnails):
            for old, new in mapping.items():
                if old in cache:
                    cache[new] = cache.pop(old)
        for old, new in mapping.items():
            if old in self._thumbnail_failed:
                self._thumbnail_failed.discard(old)
                self._thumbnail_failed.add(new)
        if self._full_path in mapping:
            self._full_path = mapping[self._full_path]

    # -- previews ----------------------------------------------------------

    def preview(self, path):
        image = self._previews.get(path)
        if image is not None:
            self._previews.move_to_end(path)
        return image

    def preview_error(self, path):
        return self._preview_errors.get(path)

    def ensure_previews(self, current, neighbors):
        """Load `current` first, then prefetch `neighbors`; drop other queued work."""
        self._wanted_previews = {current, *neighbors}
        if current != self._full_path:
            self._release_full()
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
                _emit(self._preview_done, generation, path, QImage(), _CANCELLED)
                return
            image, error = _decode(path, PREVIEW_MAX_SIZE)
            _emit(self._preview_done, generation, path, image, error)

        self._preview_pool.start(job, 1 if urgent else 0)

    def _on_preview_done(self, generation, path, image, error):
        if generation != self._generation:
            return
        self._preview_pending.discard(path)
        if error == _CANCELLED:
            return
        if error:
            self._preview_errors[path] = error
            self.preview_failed.emit(path, error)
            return

        self._previews[path] = image
        self._trim_previews()
        self.preview_loaded.emit(path)

    def _trim_previews(self):
        for path in list(self._previews):
            if len(self._previews) <= PREVIEW_CACHE_SIZE:
                break
            if path not in self._wanted_previews:
                del self._previews[path]

    # -- full resolution ---------------------------------------------------

    def full_image(self, path):
        return self._full_image if path == self._full_path else None

    def ensure_full(self, path):
        """Decode `path` without downscaling; replaces any other full image."""
        if path == self._full_path:
            return  # loaded or in flight
        self._full_path = path
        self._full_image = None
        generation = self._generation

        def job():
            if generation != self._generation or path != self._full_path:
                _emit(self._full_done, generation, path, QImage(), _CANCELLED)
                return
            image, error = _decode(path, None)
            _emit(self._full_done, generation, path, image, error)

        self._preview_pool.start(job, 2)

    def _release_full(self):
        self._full_path = None
        self._full_image = None

    def _on_full_done(self, generation, path, image, error):
        if generation != self._generation or path != self._full_path or error == _CANCELLED:
            return
        if error:
            self._full_path = None  # allow a retry on the next zoom
            self.full_failed.emit(path, error)
            return
        self._full_image = image
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

    def _request_thumbnail(self, path):
        self._thumbnail_pending.add(path)
        generation = self._generation

        def job():
            if generation != self._generation:
                return
            image, _error = _decode(path, THUMBNAIL_MAX_SIZE)
            _emit(self._thumbnail_done, generation, path, image)

        # Most recently requested first, so whatever is on screen now wins.
        self._thumbnail_pool.start(job, next(self._priority) % 2**30)

    def _on_thumbnail_done(self, generation, path, image):
        if generation != self._generation:
            return
        self._thumbnail_pending.discard(path)
        if image.isNull():
            self._thumbnail_failed.add(path)
            return
        self._thumbnails[path] = QPixmap.fromImage(image)
        while len(self._thumbnails) > THUMBNAIL_CACHE_SIZE:
            self._thumbnails.popitem(last=False)  # reloaded on demand if scrolled back to
        self.thumbnail_loaded.emit(path)
