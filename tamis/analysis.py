"""Reading capture dates and scoring photos in the background, with what was found kept on disk.

Same pattern as the other engines: each job captures a generation number, and
results of an older one are ignored; jobs catch everything, since a bad photo
must not kill a worker. Results are plain numbers kept by file name, size and
modification time, so sorting photos into _KEEPS doesn't lose them.
"""
import hashlib
import json
import os
from contextlib import suppress

import numpy as np
from PyQt6.QtCore import QObject, QThread, QThreadPool, pyqtSignal

from .constants import ANALYSIS_MAX_SIZE
from .exif import capture_time
from .imaging import gray_array, load_image
from .models import write_json

_TILES = 8  # per side: the score comes from the sharpest tiles, as a subject is often small
_SHARPEST_TILES = 0.95  # percentile


def sharpness(gray):
    """Focus score of a uint8 h×w array: variance of the Laplacian in the sharpest regions.

    Only comparable between similar photos, at the same size, and only a suggestion.
    """
    if min(gray.shape) < 3:
        return 0.0
    image = gray.astype(np.float32)
    laplacian = image[:-2, 1:-1] + image[2:, 1:-1] + image[1:-1, :-2] + image[1:-1, 2:] - 4 * image[1:-1, 1:-1]
    height, width = laplacian.shape
    rows = np.array_split(np.arange(height), min(_TILES, height))
    columns = np.array_split(np.arange(width), min(_TILES, width))
    tiles = [laplacian[r[0]:r[-1] + 1, c[0]:c[-1] + 1].var() for r in rows for c in columns]
    return float(np.percentile(tiles, _SHARPEST_TILES * 100))


def difference_hash(gray):
    """64-bit perceptual hash: whether each of 8×8 areas is brighter than the one to its right."""
    height, width = gray.shape
    rows = np.linspace(0, height, 9).astype(int)
    columns = np.linspace(0, width, 10).astype(int)
    blocks = np.add.reduceat(np.add.reduceat(gray.astype(np.float64), rows[:-1], axis=0), columns[:-1], axis=1)
    areas = np.outer(np.diff(rows), np.diff(columns))
    means = blocks / np.maximum(areas, 1)
    bits = (means[:, :-1] > means[:, 1:]).ravel()
    return int(sum(1 << i for i, bit in enumerate(bits) if bit))


def _score(path):
    """{"sharpness", "image_hash"} of a photo, {} if it can't be read."""
    try:
        image, _native = load_image(path, ANALYSIS_MAX_SIZE)
        gray = gray_array(image)
        return {"sharpness": sharpness(gray), "image_hash": difference_hash(gray)}
    except Exception:  # decoding must never kill a worker thread
        return {}


def _file_key(path):
    """What a result is valid for, None if the file can't be read."""
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return f"{os.path.basename(path)}|{stat.st_size}|{stat.st_mtime_ns}"


class PhotoAnalyzer(QObject):
    analyzed = pyqtSignal(str, object)  # primary path, {field of Photo: value}
    progressed = pyqtSignal(int, int)  # done, total
    finished = pyqtSignal()  # nothing left to do

    _done = pyqtSignal(int, object, object, object)  # generation, (path, kind), file key, results

    def __init__(self, cache_dir=None, parent=None):
        super().__init__(parent)
        self._cache_dir = cache_dir
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(max(1, min(3, QThread.idealThreadCount() - 2)))
        self._generation = 0
        self._folder = None
        self._results = {}  # file key -> {field: value}, a field being there once it has been tried
        self._dirty = False
        self._pending = set()  # (path, "capture" or "image")
        self._total = 0
        self._completed = 0
        self._done.connect(self._on_done)

    # -- what is known -------------------------------------------------------

    def _cache_file(self, folder):
        digest = hashlib.sha1(os.path.abspath(folder).encode("utf-8", "surrogateescape")).hexdigest()[:16]
        return os.path.join(self._cache_dir, f"{digest}.json")

    def open_folder(self, folder):
        """Forget the running work, and load what is known of `folder`."""
        self.save()
        self.restart()
        self._folder = folder
        self._results = {}
        if self._cache_dir:
            with suppress(OSError, ValueError), open(self._cache_file(folder), encoding="utf-8") as file:
                data = json.load(file)
                if isinstance(data, dict):
                    self._results = {key: value for key, value in data.items() if isinstance(value, dict)}

    def apply_known(self, photos):
        """Give the photos what is known of their files already."""
        for photo in photos:
            known = self._results.get(_file_key(photo.path), {})
            photo.capture_time = known.get("capture_time")
            photo.sharpness = known.get("sharpness")
            photo.image_hash = known.get("image_hash")

    def save(self):
        """Write what is known to disk. Results of failures aren't kept, so they are tried again next time."""
        if not (self._cache_dir and self._folder and self._dirty):
            return
        self._dirty = False
        data = {
            key: kept for key, value in self._results.items()
            if (kept := {name: item for name, item in value.items() if item is not None})
        }
        with suppress(OSError):
            os.makedirs(self._cache_dir, exist_ok=True)
            write_json(self._cache_file(self._folder), data)

    # -- work ----------------------------------------------------------------

    def restart(self):
        """Ignore running jobs, as their paths may be out of date."""
        self._generation += 1
        self._pool.clear()
        self._pending.clear()
        self._total = self._completed = 0
        self.progressed.emit(0, 0)

    def shutdown(self):
        self.save()
        self.restart()
        self._pool.waitForDone()

    def request(self, capture=(), images=()):
        """Queue the capture time of the photos in `capture` and the scores of those in `images`, unless already tried."""
        for photo in capture:
            self._queue(photo, "capture", "capture_time", 1)
        for photo in images:
            self._queue(photo, "image", "sharpness", 0)
        if self._pending:
            self.progressed.emit(self._completed, self._total)

    def _queue(self, photo, kind, field, priority):
        path = photo.path
        key = _file_key(path)
        if key is None or field in self._results.get(key, {}) or (path, kind) in self._pending:
            return
        self._pending.add((path, kind))
        self._total += 1
        generation = self._generation
        files = list(photo.files)

        def job():
            if generation != self._generation:
                return
            try:
                results = {"capture_time": capture_time(files)} if kind == "capture" else {"sharpness": None, **_score(path)}
            except Exception:  # a bad photo must not kill a worker
                results = {field: None}
            with suppress(RuntimeError):  # analyzer already destroyed during shutdown
                self._done.emit(generation, (path, kind), key, results)

        self._pool.start(job, priority)

    def _on_done(self, generation, job, key, results):
        if generation != self._generation:
            return
        self._pending.discard(job)
        path = job[0]
        self._completed += 1
        self._results.setdefault(key, {}).update(results)
        self._dirty = True
        self.analyzed.emit(path, results)
        self.progressed.emit(self._completed, self._total)
        if not self._pending:
            self._total = self._completed = 0
            self.progressed.emit(0, 0)
            self.save()
            self.finished.emit()
