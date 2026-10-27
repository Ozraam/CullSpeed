"""Rendering edits: developing photos for editing, live previews and JPEG export.

A photo is developed once into linear RGB (RAWs straight from LibRaw at 16
bits, other images decoded from sRGB), kept small for previews. Renders run
on one background thread, newest request first, and split each image into
strips processed in parallel (numpy releases the GIL while it computes).
While a slider is being dragged a half-size preview is rendered, which
keeps up with the mouse; the full preview follows when it is released.
"""
import json
import os
import threading
import zlib
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress

import numpy as np
import rawpy
from PyQt6.QtCore import QObject, Qt, QThreadPool, pyqtSignal
from PyQt6.QtGui import QImage, QImageReader, QPixmap

from . import develop, lut
from .imaging import array_to_image, image_to_array, is_raw, resize_array

EDIT_PREVIEW_SIZE = 2048  # longest edge of the image edits are previewed on
_STRIP_THREADS = max(1, min(8, (os.cpu_count() or 2)))
_strip_pool = ThreadPoolExecutor(_STRIP_THREADS, thread_name_prefix="cullspeed-strip")


class EditError(Exception):
    pass


# -- sources -------------------------------------------------------------------

def _source(path, max_size):
    """(pixels, kind): uint16 linear for RAWs ("linear16"), uint16 sRGB-encoded otherwise ("srgb16")."""
    if is_raw(path):
        with rawpy.imread(path) as raw:
            # Linear output with LibRaw's automatic brightness (the darkest 99% of the
            # range fills the output), which looks like a neutral starting point.
            pixels = raw.postprocess(
                gamma=(1, 1), output_bps=16, use_camera_wb=True, half_size=max_size is not None
            )
        return (resize_array(pixels, max_size) if max_size else pixels), "linear16"

    reader = QImageReader(path)
    reader.setAutoTransform(True)
    size = reader.size()
    if max_size and size.isValid() and max(size.width(), size.height()) > max_size:
        reader.setScaledSize(size.scaled(max_size, max_size, Qt.AspectRatioMode.KeepAspectRatio))
    image = reader.read()
    if image.isNull():
        raise EditError(reader.errorString())
    pixels = image_to_array(image)
    return (resize_array(pixels, max_size) if max_size else pixels), "srgb16"


def _linearize(block, kind):
    if kind == "linear16":
        return block.astype(np.float32) / 65535
    # 16-bit sRGB from an 8-bit source: decode through the exact 8-bit table.
    return develop.DECODE_8BIT[(block >> 8).astype(np.uint8)]


def develop_base(path, max_size=EDIT_PREVIEW_SIZE):
    """Linear float32 RGB of `path`, longest edge at most `max_size`."""
    pixels, kind = _source(path, max_size)
    return _linearize(pixels, kind)


def _seed(path):
    return zlib.crc32(os.path.basename(path).encode("utf-8"))


def render_parallel(linear, edit, table, seed):
    """develop.render() over strips of `linear`, in parallel."""
    height, width = linear.shape[:2]
    step = max(16, -(-height // (_STRIP_THREADS * 2)))
    starts = range(0, height, step)
    parts = _strip_pool.map(
        lambda top: develop.render(linear[top:top + step], edit, table, seed, (top, 0), (height, width)), starts
    )
    return np.concatenate(list(parts))


# -- export --------------------------------------------------------------------

def export_photo(path, edit, lut_path, dest_dir, tables=None, quality=92):
    """Render `path` with `edit` at full resolution into `dest_dir` as JPEG; returns the new path.

    `tables` caches LUT tables between photos of one export (path -> table).
    """
    table = None
    if lut_path:
        if tables is None:
            tables = {}
        if lut_path not in tables:
            tables[lut_path] = lut.build_table(lut.load_grid(lut_path))
        table = tables[lut_path]

    pixels, kind = _source(path, None)
    height, width = pixels.shape[:2]
    out = np.empty((height, width, 3), dtype=np.uint8)
    seed = _seed(path)
    strip = 240 * _STRIP_THREADS  # sequential chunks bound memory; each is rendered in parallel
    for top in range(0, height, strip):
        block = _linearize(pixels[top:top + strip], kind)
        rows = block.shape[0]
        steps = range(0, rows, 240)
        parts = _strip_pool.map(
            lambda y, block=block, top=top: develop.render(
                block[y:y + 240], edit, table, seed, (top + y, 0), (height, width)
            ),
            steps,
        )
        out[top:top + rows] = np.concatenate(list(parts))
    del pixels

    os.makedirs(dest_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(path))[0]
    name, n = f"{stem}.jpg", 1
    while os.path.exists(os.path.join(dest_dir, name)):
        name, n = f"{stem} ({n}).jpg", n + 1
    dest = os.path.join(dest_dir, name)
    if not array_to_image(out).save(dest, "JPG", quality):
        raise EditError(f"could not write {name}")
    return dest


# -- live previews ---------------------------------------------------------------

def _emit(signal, *args):
    with suppress(RuntimeError):  # renderer destroyed during shutdown
        signal.emit(*args)


class EditRenderer(QObject):
    """Renders edited previews in the background; results arrive as `rendered(path)`."""

    rendered = pyqtSignal(str)
    failed = pyqtSignal(str, str)

    _done = pyqtSignal(int, str, str, QImage, str)  # generation, path, key, image, error

    BASES = 4  # developed photos kept (≈ 33 MB each at EDIT_PREVIEW_SIZE)
    TABLES = 2  # LUT tables kept (48 MB each)
    RENDERS = 8

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._lock = threading.Lock()  # guards everything a worker touches
        self._generation = 0
        self._bases = OrderedDict()  # path -> (full preview, half-size preview)
        self._tables = OrderedDict()  # LUT path -> table
        self._wanted = {}  # path -> (key, edit, lut path, quick): the newest request
        self._running = set()  # paths with a job queued or running
        self._renders = OrderedDict()  # path -> (key, quick, QPixmap); GUI thread only
        self._errors = {}  # path -> key that failed; GUI thread only
        self._done.connect(self._on_done)

    @staticmethod
    def _key(edit, lut_path):
        return json.dumps([edit, lut_path], sort_keys=True)

    def pixmap(self, path, edit, lut_path, quick=False):
        """The render of `edit`, or the latest one while it is being made (None if none yet).

        `quick` asks for the half-size preview, for use while dragging a slider.
        """
        key = self._key(edit, lut_path)
        current = self._renders.get(path)
        if current is not None and current[0] == key and (quick or not current[1]):
            return current[2]
        if self._errors.get(path) != key:
            self._request(path, key, edit, lut_path, quick)
        return current[2] if current is not None else None

    def error(self, path):
        return path in self._errors

    def _request(self, path, key, edit, lut_path, quick):
        with self._lock:
            self._wanted[path] = (key, edit, lut_path, quick)
            if path in self._running:
                return  # the running job picks the newest request up when it is done
            self._running.add(path)
        self._pool.start(self._job(path, self._generation))

    def _job(self, path, generation):
        def job():
            while True:
                with self._lock:
                    wanted = self._wanted.get(path)
                    if wanted is None or generation != self._generation:
                        self._running.discard(path)
                        return
                key, edit, lut_path, quick = wanted
                try:
                    full, half = self._base(path)
                    table = self._table(lut_path) if lut_path else None
                    linear = half if quick else full
                    image = array_to_image(render_parallel(linear, edit, table, _seed(path)))
                    error = ""
                except Exception as exc:  # a bad photo or LUT must not kill the worker
                    image, error = QImage(), str(exc) or type(exc).__name__
                _emit(self._done, generation, path, key + ("#quick" if quick else ""), image, error)
                with self._lock:
                    if self._wanted.get(path) == wanted:
                        del self._wanted[path]
                        self._running.discard(path)
                        return
                # A newer request arrived meanwhile: render again.

        return job

    def _base(self, path):
        with self._lock:
            if path in self._bases:
                self._bases.move_to_end(path)
                return self._bases[path]
        full = develop_base(path)
        half = np.ascontiguousarray(full[::2, ::2])
        with self._lock:
            self._bases[path] = (full, half)
            while len(self._bases) > self.BASES:
                self._bases.popitem(last=False)
        return full, half

    def _table(self, lut_path):
        with self._lock:
            if lut_path in self._tables:
                self._tables.move_to_end(lut_path)
                return self._tables[lut_path]
        table = lut.build_table(lut.load_grid(lut_path))
        with self._lock:
            self._tables[lut_path] = table
            while len(self._tables) > self.TABLES:
                self._tables.popitem(last=False)
        return table

    def _on_done(self, generation, path, key, image, error):
        if generation != self._generation:
            return
        quick = key.endswith("#quick")
        key = key.removesuffix("#quick")
        if error:
            self._errors[path] = key
            self.failed.emit(path, error)
            return
        self._errors.pop(path, None)
        self._renders[path] = (key, quick, QPixmap.fromImage(image))
        self._renders.move_to_end(path)
        while len(self._renders) > self.RENDERS:
            self._renders.popitem(last=False)
        self.rendered.emit(path)

    def forget(self, path):
        """Drop the render of `path` (its edit was removed)."""
        self._renders.pop(path, None)
        self._errors.pop(path, None)

    def rename_paths(self, mapping):
        with self._lock:
            for old, new in mapping.items():
                if old in self._bases:
                    self._bases[new] = self._bases.pop(old)
        for cache in (self._renders, self._errors):
            for old, new in mapping.items():
                if old in cache:
                    cache[new] = cache.pop(old)

    def reset(self):
        self._generation += 1
        self._pool.clear()
        with self._lock:
            self._wanted.clear()
            self._running.clear()
            self._bases.clear()
        self._renders.clear()
        self._errors.clear()

    def shutdown(self):
        self.reset()
        self._pool.waitForDone()
