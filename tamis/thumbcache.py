"""Thumbnails on disk, so that reopening a folder doesn't decode every photo again.

A thumbnail is found by a hash of the file's path, size and modification time,
plus the size it was decoded at. A file changed by another app, or moved, is
therefore a miss: there is nothing to invalidate. Called from worker threads,
so it only handles QImage; a problem with the cache only ever means decoding again.
"""
import hashlib
import os
import threading
from contextlib import suppress

from PyQt6.QtGui import QImage

from .constants import THUMBNAIL_DISK_CACHE_BYTES

_TRIM_EVERY = 256  # thumbnails written, and a first time at the first one


class ThumbnailCache:
    def __init__(self, directory, limit=THUMBNAIL_DISK_CACHE_BYTES):
        self.directory = directory
        self.limit = limit
        self._writes = 0
        self._lock = threading.Lock()

    def _path(self, path, state, size):
        size_bytes, mtime = state
        digest = hashlib.sha1(f"{path}\0{size_bytes}\0{mtime}\0{size}".encode("utf-8", "surrogateescape")).hexdigest()
        return os.path.join(self.directory, digest[:2], digest)

    def load(self, path, state, size):
        """The cached QImage, or None."""
        cached = self._path(path, state, size)
        image = QImage(cached)  # (the format is found from the contents)
        if image.isNull():
            return None
        with suppress(OSError):
            os.utime(cached)  # what is used stays: trim() removes the oldest
        return image

    def save(self, path, state, size, image):
        cached = self._path(path, state, size)
        tmp_path = f"{cached}.{threading.get_ident()}.tmp"
        try:
            os.makedirs(os.path.dirname(cached), exist_ok=True)
            # JPEG has no transparency: a PNG with some must stay what it is.
            saved = image.save(tmp_path, "PNG") if image.hasAlphaChannel() else image.save(tmp_path, "JPEG", 85)
            if saved:
                os.replace(tmp_path, cached)
            else:
                os.remove(tmp_path)
        except OSError:
            with suppress(OSError):
                os.remove(tmp_path)
            return
        with self._lock:
            self._writes += 1
            trim = self._writes % _TRIM_EVERY == 1
        if trim:
            self.trim()

    def trim(self):
        """Remove the least recently used thumbnails until the cache fits its limit."""
        files = []
        try:
            for folder in os.scandir(self.directory):
                if folder.is_dir():
                    for entry in os.scandir(folder.path):
                        stat = entry.stat()
                        files.append((stat.st_mtime, stat.st_size, entry.path))
        except OSError:
            return  # another worker may be trimming too; the next time will do
        total = sum(size for _mtime, size, _path in files)
        for _mtime, size, path in sorted(files):
            if total <= self.limit:
                break
            with suppress(OSError):
                os.remove(path)
                total -= size
