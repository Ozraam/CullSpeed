"""Decoding real RAW files: tiny DNGs written by `rawfiles`, opened by LibRaw."""

import os

import numpy as np
import pytest
from PyQt6.QtCore import QBuffer, QIODevice, QSize
from PyQt6.QtGui import QColor, QImage
from rawfiles import write_dng

from tamis import develop, editing
from tamis.imaging import image_to_array, load_image

RED = (200, 20, 20)


def jpeg_bytes(width, height, color=RED):
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor(*color))
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "JPEG", 95)
    return bytes(buffer.data())


def pixel(image, x, y):
    color = image.pixelColor(x, y)
    return color.red(), color.green(), color.blue()


def reddish(rgb):
    return rgb[0] > 150 and rgb[1] < 80 and rgb[2] < 80


def test_a_raw_without_a_preview_is_developed(tmp_path):
    path = write_dng(tmp_path / "IMG_1.dng")
    image, native = load_image(path, 3000)
    assert native == QSize(64, 48)
    assert image.size() == QSize(32, 24)  # developed at half size, which is plenty for a preview
    # The white square is top left, brighter than the rest.
    assert sum(pixel(image, 2, 2)) > sum(pixel(image, 20, 15))


def test_a_raw_is_turned_upright(tmp_path):
    path = write_dng(tmp_path / "IMG_1.dng", orientation=6)  # shot holding the camera on its side
    image, native = load_image(path, 3000)
    assert native == QSize(48, 64) and image.size() == QSize(24, 32)
    # Turned a quarter clockwise, the top left square is now top right.
    assert sum(pixel(image, 22, 2)) > sum(pixel(image, 2, 2))

    full, full_native = load_image(path, None)
    assert full.size() == full_native == QSize(48, 64)


def test_the_embedded_preview_is_used_when_it_is_large_enough(tmp_path):
    path = write_dng(tmp_path / "IMG_1.dng", preview=jpeg_bytes(64, 48))
    image, native = load_image(path, 3000)
    assert native == QSize(64, 48)
    assert reddish(pixel(image, 30, 30))  # the preview's colour, not the RAW's


def test_a_preview_without_orientation_of_its_own_follows_the_raw(tmp_path):
    path = write_dng(tmp_path / "IMG_1.dng", orientation=6, preview=jpeg_bytes(64, 48))
    image, native = load_image(path, 3000)
    assert image.size() == native == QSize(48, 64)
    assert reddish(pixel(image, 20, 30))


def test_a_small_preview_is_shown_but_full_resolution_develops_the_raw(tmp_path):
    path = write_dng(tmp_path / "IMG_1.dng", preview=jpeg_bytes(16, 12))

    preview, native = load_image(path, 3000)
    assert preview.size() == QSize(16, 12) and reddish(pixel(preview, 8, 6))
    assert native == QSize(64, 48)  # the sensor's, for 1:1 zoom

    full, full_native = load_image(path, None)
    assert full.size() == full_native == QSize(64, 48)
    assert not reddish(pixel(full, 40, 30))


def test_a_damaged_raw_is_an_error(tmp_path):
    path = tmp_path / "IMG_1.dng"
    path.write_bytes(b"II*\0" + b"\0" * 64)
    with pytest.raises(Exception):  # noqa: B017 (whatever LibRaw raises: the loader reports it)
        load_image(str(path), 3000)


@pytest.mark.parametrize("process", [1, develop.PROCESS])
def test_develop_base_is_linear_float_of_the_sensor(tmp_path, process):
    path = write_dng(tmp_path / "IMG_1.dng", orientation=6)
    base = editing.develop_base(path, max_size=None, process=process)
    assert base.shape == (64, 48, 3) and base.dtype == np.float32
    assert np.isfinite(base).all() and base.min() >= 0
    assert base[2:12, 38:46].mean() > base[40:60, 10:30].mean()  # upright: the bright square is top right


def test_the_new_raw_process_keeps_the_exposure(tmp_path):
    dark = write_dng(tmp_path / "dark.dng", color=(0.1, 0.1, 0.1), marker=False)
    bright = write_dng(tmp_path / "bright.dng", color=(0.2, 0.2, 0.2), marker=False)

    def ratio(process):
        return editing.develop_base(bright, None, process).mean() / editing.develop_base(dark, None, process).mean()

    assert ratio(develop.PROCESS) == pytest.approx(2, rel=0.05)  # twice the light, twice as bright
    assert ratio(1) == pytest.approx(1, rel=0.05)  # LibRaw's automatic brightness evens them out


def test_develop_base_downscales_a_raw(tmp_path):
    path = write_dng(tmp_path / "IMG_1.dng", width=128, height=96)
    assert editing.develop_base(path, max_size=40).shape == (30, 40, 3)


def test_a_raw_is_exported_from_its_development(tmp_path):
    path = write_dng(tmp_path / "IMG_1.dng", preview=jpeg_bytes(64, 48))
    dest = editing.export_photo(path, {"exposure": 0.5}, None, str(tmp_path / "_EXPORTS"))
    assert os.path.basename(dest) == "IMG_1.jpg"
    exported = image_to_array(QImage(dest))
    assert exported.shape == (48, 64, 3)
    red, green, _blue = exported[30, 40]
    assert not (red > 150 * 257 and green < 80 * 257)  # developed, not the red preview
