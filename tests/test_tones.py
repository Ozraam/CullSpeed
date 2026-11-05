import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtGui import QColor, QImage  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from tamis import tones  # noqa: E402
from tamis.imaging import array_to_image  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_tone_counts_each_channel(qapp):
    image = QImage(100, 50, QImage.Format.Format_RGB32)
    image.fill(QColor(10, 100, 200))
    tone = tones.tone_of(image)
    assert tone.counts.shape == (3, 256)
    assert tone.counts[0, 10] == tone.counts[1, 100] == tone.counts[2, 200] == 5000
    assert tone.shadows == 0 and tone.highlights == 0


def test_tone_reports_what_is_clipped(qapp):
    pixels = np.full((100, 100, 3), 128, dtype=np.uint8)
    pixels[:10] = 0  # 10 % black
    pixels[10:30, :, 0] = 255  # 20 % with one channel blown
    tone = tones.tone_of(array_to_image(pixels))
    assert tone.shadows == pytest.approx(0.1)
    assert tone.highlights == pytest.approx(0.2)


def test_a_saturated_colour_is_not_black(qapp):
    pixels = np.zeros((10, 10, 3), dtype=np.uint8)
    pixels[..., 2] = 200  # red and green at 0, but it is far from a blocked shadow
    assert tones.tone_of(array_to_image(pixels)).shadows == 0


def test_a_big_image_counts_on_a_smaller_copy_and_still_sees_the_clipping(qapp):
    pixels = np.full((3000, 3000, 3), 128, dtype=np.uint8)
    pixels[:300] = 255
    tone = tones.tone_of(array_to_image(pixels))
    assert tone.counts[0].sum() == 1024 ** 2
    assert tone.highlights == pytest.approx(0.1, abs=0.01)


def test_clipping_overlay_only_marks_the_clipped_pixels(qapp):
    pixels = np.full((4, 3, 3), 128, dtype=np.uint8)
    pixels[0, 0] = 255
    pixels[3, 2] = 0
    overlay = tones.clipping_image(array_to_image(pixels))
    assert (overlay.width(), overlay.height()) == (3, 4)
    assert overlay.pixelColor(0, 0).getRgb()[:3] == tones.HIGHLIGHT_COLOR and overlay.pixelColor(0, 0).alpha() == 255
    assert overlay.pixelColor(2, 3).getRgb()[:3] == tones.SHADOW_COLOR
    assert overlay.pixelColor(1, 1).alpha() == 0
