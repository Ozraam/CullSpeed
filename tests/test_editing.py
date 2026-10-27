import os

import numpy as np
from PyQt6.QtGui import QColor, QImage

from cullspeed import editing
from cullspeed.imaging import image_to_array


def jpeg(path, width=300, height=200, grey=100):
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor(grey, grey, grey))
    image.save(str(path), "JPG", 95)
    return str(path)


def test_develop_base_is_linear_and_downscaled(tmp_path):
    path = jpeg(tmp_path / "a.jpg", 3000, 2000, grey=118)
    base = editing.develop_base(path, max_size=600)
    assert base.shape == (400, 600, 3) and base.dtype == np.float32
    assert abs(float(base.mean()) - 0.18) < 0.01  # sRGB 118 is 18 % grey in linear light


def test_render_parallel_matches_single_render(tmp_path):
    base = editing.develop_base(jpeg(tmp_path / "a.jpg"), max_size=None)
    edit = {"exposure": 0.5, "contrast": 20, "vignette": -40}
    assert np.array_equal(editing.render_parallel(base, edit, None, 1), editing.develop.render(base, edit))


def test_export_writes_an_edited_full_size_jpeg(tmp_path):
    path = jpeg(tmp_path / "a.jpg", 800, 600, grey=100)
    dest = editing.export_photo(path, {"exposure": 1.0}, None, str(tmp_path / "_EXPORTS"))
    again = editing.export_photo(path, {}, None, str(tmp_path / "_EXPORTS"))

    assert sorted(os.listdir(tmp_path / "_EXPORTS")) == ["a (1).jpg", "a.jpg"]
    assert os.path.basename(again) == "a (1).jpg"
    exported = image_to_array(QImage(dest))
    assert exported.shape == (600, 800, 3)
    assert exported.mean() / 257 > 130  # +1 EV over the 100 grey original
    assert abs(image_to_array(QImage(again)).mean() / 257 - 100) < 2  # no edit: unchanged
