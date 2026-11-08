import os

import numpy as np
import pytest
from PyQt6.QtGui import QColor, QImage

from tamis import editing
from tamis.imaging import image_to_array


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


def test_failed_export_leaves_the_earlier_one_alone(tmp_path, monkeypatch):
    source = jpeg(tmp_path / "a.jpg")
    exports = tmp_path / "_EXPORTS"
    exports.mkdir()
    (exports / "a.jpg").write_text("earlier")

    class Unencodable(QImage):
        def save(self, *_args):
            return False

    monkeypatch.setattr(editing, "array_to_image", lambda _pixels: Unencodable(8, 8, QImage.Format.Format_RGB32))
    with pytest.raises(editing.EditError):
        editing.export_photo(source, {"exposure": 1.0}, None, str(exports), name="a.jpg")
    monkeypatch.undo()

    def full_disk(_fd):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("tamis.fileops.os.fsync", full_disk)
    with pytest.raises(OSError):
        editing.export_photo(source, {"exposure": 1.0}, None, str(exports), name="a.jpg")

    assert (exports / "a.jpg").read_text() == "earlier"
    assert os.listdir(exports) == ["a.jpg"]


def half_white(path, width=300, height=200):
    """Black, with the right half white."""
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor(0, 0, 0))
    for y in range(height):
        for x in range(width // 2, width):
            image.setPixelColor(x, y, QColor(255, 255, 255))
    image.save(str(path), "PNG")
    return str(path)


def test_export_applies_turns_before_the_crop(tmp_path):
    path = half_white(tmp_path / "a.png")
    # Turned clockwise, the white half is at the bottom; the crop keeps that half.
    dest = editing.export_photo(path, {"rotate": 90, "crop": [0, 0.5, 1, 1]}, None, str(tmp_path / "_EXPORTS"))
    exported = image_to_array(QImage(dest))
    assert exported.shape == (150, 200, 3)
    assert exported.mean() / 257 > 250


def test_export_straightens_at_the_same_size(tmp_path):
    path = jpeg(tmp_path / "a.jpg", 400, 300, grey=100)
    dest = editing.export_photo(path, {"straighten": 4.0}, None, str(tmp_path / "_EXPORTS"))
    exported = image_to_array(QImage(dest))
    assert exported.shape == (300, 400, 3)
    assert abs(exported.mean() / 257 - 100) < 2  # no empty corner darkens it


def test_export_and_preview_frame_alike(tmp_path):
    path = half_white(tmp_path / "a.png")
    edit = {"straighten": 6.0, "crop": [0.1, 0.1, 0.9, 0.9]}
    dest = editing.export_photo(path, edit, None, str(tmp_path / "_EXPORTS"))
    exported = image_to_array(QImage(dest)) / 257
    base = editing.develop_base(path, max_size=None)
    upright = editing.straighten_parallel(base, edit["straighten"])
    preview = editing.develop.render(editing.develop.crop_array(upright, edit["crop"]), edit).astype(np.float32)
    assert exported.shape == preview.shape
    assert np.abs(exported - preview).mean() < 6  # JPEG blurs the edge a little


def test_export_carries_an_srgb_profile_and_the_source_exif(tmp_path):
    from tamis import exif

    source = jpeg(tmp_path / "a.jpg")
    with open(source, "rb") as file:
        plain = file.read()
    shot = b"MM\0*" + bytes.fromhex("00000008") + bytes.fromhex("0001" "0110 0002 00000004" "4F4D3100" "00000000")
    app1 = b"Exif\0\0" + shot
    with open(source, "wb") as file:
        file.write(plain[:2] + b"\xff\xe1" + len(app1 + b"..").to_bytes(2, "big") + app1 + plain[2:])

    dest = editing.export_photo(source, {"exposure": 0.5}, None, str(tmp_path / "_EXPORTS"))

    image = QImage(dest)
    assert image.colorSpace().isValid() and image.colorSpace().description() == "sRGB"
    assert exif.read_info(dest)["camera"] == "OM1"


def test_export_scales_down_to_the_long_edge(tmp_path):
    source = jpeg(tmp_path / "a.jpg", 300, 200)
    small = editing.export_photo(source, {}, None, str(tmp_path / "out"), name="small.jpg", long_edge=150, quality=60)
    large = editing.export_photo(source, {}, None, str(tmp_path / "out"), name="large.jpg", long_edge=1000)
    assert QImage(small).size().width() == 150 and QImage(small).size().height() == 100
    assert QImage(large).size().width() == 300  # never enlarged
