"""Rendering edits: developing photos for editing, live previews and JPEG export.

A photo is developed once into linear RGB (RAWs straight from LibRaw at 16
bits, other images converted to sRGB, then decoded), kept small for previews. Renders run
on one background thread, newest request first, and split each image into
strips processed in parallel (numpy releases the GIL while it computes).
While a slider is being dragged a half-size preview is rendered, which
keeps up with the mouse; the full preview follows when it is released.
EditThumbnails renders small versions of edits for the filmstrip and the
gallery, one at a time, at low priority.
"""
import hashlib
import json
import os
import threading
import zlib
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from itertools import count

import numpy as np
import rawpy
from PyQt6.QtCore import QBuffer, QIODevice, QObject, Qt, QThread, QThreadPool, pyqtSignal
from PyQt6.QtGui import QColorSpace, QImage, QImageReader, QPixmap

from . import develop, exif, lut
from .constants import THUMBNAIL_CACHE_BYTES
from .fileops import atomic_write, free_name
from .imaging import array_to_image, image_to_array, is_raw, resize_array, to_srgb
from .loader import file_state, pixmap_bytes, thumbnail_decode_size

EDIT_PREVIEW_SIZE = 2048  # longest edge of the image edits are previewed on
_STRIP_THREADS = max(1, min(8, (os.cpu_count() or 2)))
_strip_pool = ThreadPoolExecutor(_STRIP_THREADS, thread_name_prefix="tamis-strip")


class EditError(Exception):
    pass


# -- sources -------------------------------------------------------------------

def process_of(path, edit):
    """How `edit` develops `path` (develop.PROCESS): what a developed photo is cached by."""
    return edit.get("process", develop.PROCESS) if is_raw(path) else develop.PROCESS


def _source(path, max_size, process=develop.PROCESS):
    """(pixels, kind): uint16 linear for RAWs ("raw16", or "linear16" for process 1), uint16 sRGB otherwise ("srgb16")."""
    if is_raw(path):
        options = {"gamma": (1, 1), "output_bps": 16, "use_camera_wb": True, "half_size": max_size is not None}
        if process >= 2:
            # A fixed gain (in _linearize) instead of LibRaw's automatic brightness, which clips
            # the brightest 1% and changes from one photo to the next; clipped channels are blended.
            options.update(no_auto_bright=True, highlight_mode=rawpy.HighlightMode.Blend)
        with rawpy.imread(path) as raw:
            pixels = raw.postprocess(**options)
        return (resize_array(pixels, max_size) if max_size else pixels), ("raw16" if process >= 2 else "linear16")

    reader = QImageReader(path)
    reader.setAutoTransform(True)
    size = reader.size()
    if max_size and size.isValid() and max(size.width(), size.height()) > max_size:
        reader.setScaledSize(size.scaled(max_size, max_size, Qt.AspectRatioMode.KeepAspectRatio))
    image = reader.read()
    if image.isNull():
        raise EditError(reader.errorString())
    pixels = image_to_array(to_srgb(image, deep=True))  # 16 bits: a 16-bit source keeps its precision
    return (resize_array(pixels, max_size) if max_size else pixels), "srgb16"


def _linearize(block, kind):
    if kind == "raw16":
        return block.astype(np.float32) * np.float32(develop.RAW_GAIN / 65535)
    if kind == "linear16":
        return block.astype(np.float32) / 65535
    return develop.DECODE_16BIT[block]  # exact for 8- and 16-bit sources alike


def develop_base(path, max_size=EDIT_PREVIEW_SIZE, process=develop.PROCESS):
    """Linear float32 RGB of `path`, longest edge at most `max_size`."""
    pixels, kind = _source(path, max_size, process)
    return _linearize(pixels, kind)


def exposure_shift(path, old, new=develop.PROCESS, max_size=512):
    """The EV that keeps an edit of the RAW `path` as bright when it goes from process `old` to `new`.

    Process 1's automatic brightness depends on each photo, so this is measured: the
    median ratio of the two developments, where neither is black or clipped.
    """
    before = develop_base(path, max_size, old) @ develop.LUMA
    after = develop_base(path, max_size, new) @ develop.LUMA
    usable = (before > 0.002) & (before < 0.9) & (after > 1e-4)
    if not usable.any():
        return 0.0
    return float(np.median(np.log2(before[usable] / after[usable])))


def _seed(path):
    return zlib.crc32(os.path.basename(path).encode("utf-8"))


def render_parallel(linear, edit, table, seed, raw=False):
    """develop.render() over strips of `linear`, in parallel."""
    height, width = linear.shape[:2]
    step = max(16, -(-height // (_STRIP_THREADS * 2)))
    starts = range(0, height, step)
    parts = _strip_pool.map(
        lambda top: develop.render(linear[top:top + step], edit, table, seed, (top, 0), (height, width), raw), starts
    )
    return np.concatenate(list(parts))


def straighten_parallel(pixels, degrees):
    """develop.straighten_rows() over the whole of `pixels`, in parallel."""
    height = pixels.shape[0]
    step = max(16, -(-height // (_STRIP_THREADS * 2)))
    parts = _strip_pool.map(
        lambda top: develop.straighten_rows(pixels, degrees, top, min(height, top + step)), range(0, height, step)
    )
    return np.concatenate(list(parts))


# -- export --------------------------------------------------------------------

def export_photo(path, edit, lut_path, dest_dir, tables=None, quality=92, name=None, long_edge=0):
    """Render `path` with `edit` into `dest_dir` as JPEG; returns the new path.

    The photo is rendered at full resolution, then scaled down to `long_edge`
    pixels if that is set and smaller. The JPEG is tagged sRGB (with an ICC
    profile) and carries the EXIF of its source, see exif.export_segment().
    `tables` caches LUT tables between photos of one export (path -> table).
    With `name` the file is written under that name, replacing one already
    there; without, it gets a free name (`IMG_1 (1).jpg`).
    """
    table = None
    if lut_path:
        if tables is None:
            tables = {}
        if lut_path not in tables:
            tables[lut_path] = lut.build_table(lut.load_grid(lut_path))
        table = tables[lut_path]

    pixels, kind = _source(path, None, process_of(path, edit))
    raw = is_raw(path)
    pixels = develop.turn_array(pixels, edit.get("rotate", 0))
    if edit.get("straighten"):
        pixels = straighten_parallel(pixels, edit["straighten"])
    pixels = develop.crop_array(pixels, edit.get("crop"))
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
                block[y:y + 240], edit, table, seed, (top + y, 0), (height, width), raw
            ),
            steps,
        )
        out[top:top + rows] = np.concatenate(list(parts))
    del pixels

    os.makedirs(dest_dir, exist_ok=True)
    if name is None:
        name = free_name(dest_dir, os.path.splitext(os.path.basename(path))[0], ".jpg")
    dest = os.path.join(dest_dir, name)
    image = array_to_image(out)
    del out
    if long_edge and max(width, height) > long_edge:
        image = image.scaled(
            long_edge, long_edge, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
        )
    image.setColorSpace(QColorSpace(QColorSpace.NamedColorSpace.SRgb))  # written as an ICC profile
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not image.save(buffer, "JPG", quality):
        raise EditError(f"could not write {name}")
    data = exif.with_exif(bytes(buffer.data()), exif.export_segment(path, (image.width(), image.height())))
    with atomic_write(dest) as tmp_path, open(tmp_path, "wb") as file:
        file.write(data)
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
    TURNED = 2  # turned or straightened photos kept, so that a slider doesn't turn them again each time

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._lock = threading.Lock()  # guards everything a worker touches
        self._generation = 0
        self._bases = OrderedDict()  # (path, process) -> (full preview, half-size preview)
        self._tables = OrderedDict()  # LUT path -> table
        self._turned = OrderedDict()  # (path, quick, rotate, straighten) -> pixels
        self._wanted = {}  # path -> (key, edit, lut path, quick): the newest request
        self._running = set()  # paths with a job queued or running
        self._renders = OrderedDict()  # path -> (key, quick, QPixmap); GUI thread only
        self._errors = {}  # path -> key that failed; GUI thread only
        self._done.connect(self._on_done)

    @staticmethod
    def _key(edit, lut_path):
        return json.dumps([edit, lut_path], sort_keys=True)

    def pixmap(self, path, edit, lut_path, quick=False, same_crop=False):
        """The render of `edit`, or the latest one while it is being made (None if none yet).

        `quick` asks for the half-size preview, for use while dragging a slider.
        `same_crop` only accepts a latest render framed like `edit`, for the crop
        tool, whose frame would otherwise sit on the wrong picture for a moment.
        """
        key = self._key(edit, lut_path)
        current = self._renders.get(path)
        if current is not None and current[0] == key and (quick or not current[1]):
            return current[2]
        if self._errors.get(path) != key:
            self._request(path, key, edit, lut_path, quick)
        if current is None or (same_crop and develop.framing(json.loads(current[0])[0]) != develop.framing(edit)):
            return None
        return current[2]

    def error(self, path):
        return path in self._errors

    def keep_requests(self, paths):
        """Drop the requests not rendered yet of photos other than `paths`: they are no longer shown."""
        with self._lock:
            for path in [path for path in self._wanted if path not in paths]:
                del self._wanted[path]

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
                    process = process_of(path, edit)
                    full, half = self._base(path, process)
                    table = self.table(lut_path) if lut_path else None
                    upright = self._upright(path, process, quick, half if quick else full, edit)
                    linear = develop.crop_array(upright, edit.get("crop"))
                    image = array_to_image(render_parallel(linear, edit, table, _seed(path), is_raw(path)))
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

    def _base(self, path, process):
        key = (path, process)
        with self._lock:
            if key in self._bases:
                self._bases.move_to_end(key)
                return self._bases[key]
        full = develop_base(path, process=process)
        half = np.ascontiguousarray(full[::2, ::2])
        with self._lock:
            self._bases[key] = (full, half)
            while len(self._bases) > self.BASES:
                self._bases.popitem(last=False)
        return full, half

    def _upright(self, path, process, quick, base, edit):
        """`base` turned and straightened as `edit` says."""
        rotate, straighten = edit.get("rotate", 0), edit.get("straighten", 0)
        if not rotate and not straighten:
            return base
        key = (path, process, quick, rotate, straighten)
        with self._lock:
            if key in self._turned:
                self._turned.move_to_end(key)
                return self._turned[key]
        pixels = np.ascontiguousarray(develop.turn_array(base, rotate))
        if straighten:
            pixels = straighten_parallel(pixels, straighten)
        with self._lock:
            self._turned[key] = pixels
            while len(self._turned) > self.TURNED:
                self._turned.popitem(last=False)
        return pixels

    def table(self, lut_path):
        """The lookup table of a LUT file; called from worker threads."""
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
            _rename_keys(self._bases, mapping)
            self._turned.clear()
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
            self._turned.clear()
        self._renders.clear()
        self._errors.clear()

    def shutdown(self):
        self.reset()
        self._pool.waitForDone()


def _rename_keys(cache, mapping):
    """Move the entries of `cache`, keyed by (path, …), to the new paths in `mapping`."""
    for key in [key for key in cache if key[0] in mapping]:
        cache[(mapping[key[0]], *key[1:])] = cache.pop(key)


# -- thumbnails ------------------------------------------------------------------

PIPELINE = 2  # goes up when renders of the same edit change (2: RAW process 2, colour spaces, 16-bit sources)

class EditThumbnails(QObject):
    """Small renders of edits; results arrive as `ready(path)`.

    Only what is on screen is asked for, newest first. A photo keeps showing its
    latest thumbnail until the one of a new edit is ready. Renders are also kept on
    disk, by edit: the key holds the file's and the LUT's state, so a change is a miss.
    """

    ready = pyqtSignal(str)

    _done = pyqtSignal(int, str, str, QImage)  # generation, path, key, image (null if it failed)

    BASES = 16  # small developed photos kept, so that a new edit of one doesn't develop it again

    def __init__(self, renderer, disk_cache=None, parent=None):
        super().__init__(parent)
        self._renderer = renderer  # shares its LUT tables, which are big
        self._disk = disk_cache
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._priority = count()
        self._lock = threading.Lock()  # guards everything a worker touches
        self._generation = 0
        self._bases = OrderedDict()  # (path, process) -> linear pixels
        self._wanted = {}  # path -> (key, edit, lut path, size): the newest request
        self._running = set()  # paths with a job queued or running
        self._pixmaps = OrderedDict()  # path -> (key, QPixmap), LRU order; GUI thread only
        self._bytes = 0
        self._failed = {}  # path -> key that failed; GUI thread only
        self._done.connect(self._on_done)

    def pixmap(self, path, edit, lut_path):
        """The thumbnail of `edit`, or the latest one while it is being made (None if none yet)."""
        key = EditRenderer._key(edit, lut_path)
        current = self._pixmaps.get(path)
        if current is not None:
            self._pixmaps.move_to_end(path)
            if current[0] == key:
                return current[1]
        if self._failed.get(path) != key:
            self._request(path, key, edit, lut_path)
        return current[1] if current is not None else None

    def _request(self, path, key, edit, lut_path):
        with self._lock:
            self._wanted[path] = (key, edit, lut_path, thumbnail_decode_size())
            if path in self._running:
                return  # the running job picks the newest request up when it is done
            self._running.add(path)
        self._pool.start(self._job(path, self._generation), next(self._priority) % 2**30)

    def _job(self, path, generation):
        def job():
            QThread.currentThread().setPriority(QThread.Priority.LowPriority)
            while True:
                with self._lock:
                    wanted = self._wanted.get(path)
                    if wanted is None or generation != self._generation:
                        self._running.discard(path)
                        return
                try:
                    image = self._render(path, *wanted)
                except Exception:  # a bad photo or LUT must not kill the worker; it keeps the original's thumbnail
                    image = QImage()
                with suppress(RuntimeError):  # destroyed during shutdown
                    self._done.emit(generation, path, wanted[0], image)
                with self._lock:
                    if self._wanted.get(path) == wanted:
                        del self._wanted[path]
                        self._running.discard(path)
                        return
                # A newer request arrived meanwhile: render again.

        return job

    def _render(self, path, key, edit, lut_path, size):
        state = file_state(path)
        lut_state = file_state(lut_path) if lut_path else None
        # PIPELINE: thumbnails rendered by an older pipeline, from an edit that reads the same, are a miss.
        digest = hashlib.sha1(f"{PIPELINE}\0{key}\0{lut_state}".encode("utf-8", "surrogateescape")).hexdigest()
        disk_key = f"{size}:{digest}"
        if self._disk and state:
            cached = self._disk.load(path, state, disk_key)
            if cached is not None:
                return cached
        table = self._renderer.table(lut_path) if lut_path else None
        linear = develop.turn_array(self._base(path, size, process_of(path, edit)), edit.get("rotate", 0))
        if edit.get("straighten"):
            linear = develop.straighten_rows(linear, edit["straighten"], 0, linear.shape[0])
        linear = develop.crop_array(linear, edit.get("crop"))
        image = array_to_image(develop.render(linear, edit, table, _seed(path), raw=is_raw(path))).scaled(
            size, size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
        )
        if self._disk and state:
            self._disk.save(path, state, disk_key, image)
        return image

    def _base(self, path, size, process):
        key = (path, process)
        with self._lock:
            if key in self._bases:
                self._bases.move_to_end(key)
                return self._bases[key]
        base = develop_base(path, size * 2, process)  # twice the size, for what a crop leaves
        with self._lock:
            self._bases[key] = base
            while len(self._bases) > self.BASES:
                self._bases.popitem(last=False)
        return base

    def _on_done(self, generation, path, key, image):
        if generation != self._generation:
            return
        if image.isNull():
            self._failed[path] = key
            return
        self._failed.pop(path, None)
        self._drop(path)
        pixmap = QPixmap.fromImage(image)
        self._pixmaps[path] = (key, pixmap)
        self._bytes += pixmap_bytes(pixmap)
        while self._bytes > THUMBNAIL_CACHE_BYTES // 2 and len(self._pixmaps) > 1:
            self._drop(next(iter(self._pixmaps)))  # rendered or read again if scrolled back to
        self.ready.emit(path)

    def _drop(self, path):
        current = self._pixmaps.pop(path, None)
        if current is not None:
            self._bytes -= pixmap_bytes(current[1])

    def forget(self, path):
        """This file is gone."""
        self._drop(path)
        self._failed.pop(path, None)
        with self._lock:
            for key in [key for key in self._bases if key[0] == path]:
                del self._bases[key]

    def rename_paths(self, mapping):
        with self._lock:
            _rename_keys(self._bases, mapping)
        for cache in (self._pixmaps, self._failed):
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
        self._pixmaps.clear()
        self._bytes = 0
        self._failed.clear()

    def shutdown(self):
        self.reset()
        self._pool.waitForDone()
