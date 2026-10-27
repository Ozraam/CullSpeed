"""Film simulation LUTs: HaldCLUT images (what RawTherapee uses) and .cube files.

A LUT is resampled once into a 256×256×256 table of 8-bit colours, so
applying it is a single lookup per pixel. The resampling is trilinear and
separable (one axis at a time), which keeps building a table well under a
second even for a level-12 HaldCLUT.
"""
import os

import numpy as np
from PyQt6.QtGui import QImage

from .imaging import image_to_array

EXTENSIONS = (".png", ".tif", ".tiff", ".cube")


class LutError(Exception):
    pass


def scan(folder):
    """{LUT id: path} for every LUT under `folder`; ids are relative paths like "Fuji/Velvia 50.png"."""
    found = {}
    for root, dirs, files in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if not d.startswith('.'))
        for name in sorted(files, key=str.casefold):
            if not name.startswith('.') and os.path.splitext(name)[1].lower() in EXTENSIONS:
                path = os.path.join(root, name)
                found[os.path.relpath(path, folder).replace(os.sep, "/")] = path
    return found


def display_name(lut_id):
    """"Fuji XTrans III/Fuji Velvia 50.png" -> "Fuji XTrans III / Fuji Velvia 50"."""
    return " / ".join(os.path.splitext(lut_id)[0].split("/"))


# -- loading -------------------------------------------------------------------

def load_grid(path):
    """The LUT as float32 [blue][green][red] -> RGB in [0, 1]."""
    if path.lower().endswith(".cube"):
        return _read_cube(path)
    return _read_hald(path)


def _read_hald(path):
    image = QImage(path)
    if image.isNull():
        raise LutError(f"cannot read {os.path.basename(path)}")
    side = image.width()
    level = round(side ** (1 / 3))
    if image.height() != side or level ** 3 != side:
        raise LutError(f"{os.path.basename(path)} is not a HaldCLUT image")
    n = level * level
    pixels = image_to_array(image).astype(np.float32) / 65535
    # Pixel i (row-major) holds the colour for red = i % n, green = i // n % n, blue = i // n².
    return pixels.reshape(n, n, n, 3)


def _read_cube(path):
    size, rows = None, []
    with open(path, encoding='utf-8', errors='replace') as file:
        for line in file:
            parts = line.split()
            if not parts or parts[0].startswith('#'):
                continue
            keyword = parts[0].upper()
            if keyword == "LUT_3D_SIZE":
                size = int(parts[1])
            elif keyword == "LUT_1D_SIZE":
                raise LutError("1D .cube LUTs are not supported")
            elif keyword in ("DOMAIN_MIN", "DOMAIN_MAX"):
                if [float(v) for v in parts[1:4]] != ([0.0] * 3 if keyword == "DOMAIN_MIN" else [1.0] * 3):
                    raise LutError("only .cube LUTs with a 0–1 domain are supported")
            elif keyword[0].isdigit() or keyword[0] in "-+.":
                rows.append(parts[:3])
    if not size or len(rows) != size ** 3:
        raise LutError(f"{os.path.basename(path)} is not a valid 3D .cube LUT")
    # Red varies fastest, as in a HaldCLUT.
    return np.clip(np.array(rows, dtype=np.float32), 0.0, 1.0).reshape(size, size, size, 3)


# -- table -----------------------------------------------------------------------

def _axis_weights(n):
    position = np.linspace(0, n - 1, 256)
    low = np.floor(position).astype(np.intp)
    high = np.minimum(low + 1, n - 1)
    return low, high, (position - low).astype(np.float32)


def _resample(grid, axis):
    """Linear interpolation of `grid` to 256 samples along `axis`."""
    low, high, weight = _axis_weights(grid.shape[axis])
    shape = [1] * grid.ndim
    shape[axis] = 256
    weight = weight.reshape(shape)
    return np.take(grid, low, axis=axis) * (1 - weight) + np.take(grid, high, axis=axis) * weight


def build_table(grid):
    """uint8 table of 256³ colours, indexed by blue << 16 | green << 8 | red."""
    partial = _resample(_resample(grid, 2), 1)  # n × 256 × 256 × 3: red, then green
    low, high, weight = _axis_weights(grid.shape[0])
    table = np.empty((256, 256, 256, 3), dtype=np.uint8)
    for start in range(0, 256, 32):  # blue, in chunks to bound the float32 temporaries
        w = weight[start:start + 32, None, None, None]
        chunk = partial[low[start:start + 32]] * (1 - w) + partial[high[start:start + 32]] * w
        table[start:start + 32] = np.clip(chunk * 255 + 0.5, 0, 255).astype(np.uint8)
    return table.reshape(-1, 3)


def apply(rgb8, table):
    """Look `rgb8` (uint8 h×w×3) up in `table`."""
    index = rgb8[..., 2].astype(np.uint32) << 16
    index |= rgb8[..., 1].astype(np.uint32) << 8
    index |= rgb8[..., 0]
    return table[index]
