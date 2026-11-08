import os

import numpy as np
import pytest
from PyQt6.QtGui import QColor, QColorSpace, QImage

from tamis import editing
from tamis.imaging import image_to_array, load_image


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


# -- sources -----------------------------------------------------------------------

class FakeRaw:
    """rawpy.imread() of a RAW whose development is `pixels`, recording how it was asked for."""

    calls = []

    def __init__(self, pixels):
        self.pixels = pixels

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def postprocess(self, **options):
        FakeRaw.calls.append(options)
        return self.pixels


def test_raws_are_developed_without_automatic_brightness(tmp_path, monkeypatch):
    pixels = np.full((20, 30, 3), 6000, dtype=np.uint16)
    monkeypatch.setattr(editing.rawpy, "imread", lambda path: FakeRaw(pixels))
    FakeRaw.calls.clear()
    path = str(tmp_path / "a.CR2")

    base = editing.develop_base(path)
    assert FakeRaw.calls[-1]["no_auto_bright"] and FakeRaw.calls[-1]["highlight_mode"] == editing.rawpy.HighlightMode.Blend
    assert np.allclose(base, 6000 / 65535 * editing.develop.RAW_GAIN)  # one gain for every photo

    legacy = editing.develop_base(path, process=1)
    assert "no_auto_bright" not in FakeRaw.calls[-1]
    assert np.allclose(legacy, 6000 / 65535)
    assert editing.process_of(path, {"exposure": 1, "process": 1}) == 1
    assert editing.process_of(path, {"exposure": 1}) == editing.develop.PROCESS
    assert editing.process_of(str(tmp_path / "a.jpg"), {"process": 1}) == editing.develop.PROCESS


def test_16_bit_sources_keep_their_precision(tmp_path):
    width = 1024
    ramp = np.linspace(20000, 24000, width).astype(np.uint16)  # 4000 levels, about 16 in 8 bits
    rgbx = np.empty((4, width, 4), dtype=np.uint16)
    rgbx[..., :3] = ramp[None, :, None]
    rgbx[..., 3] = 65535
    data = rgbx.tobytes()
    image = QImage(data, width, 4, width * 8, QImage.Format.Format_RGBX64).copy()
    path = str(tmp_path / "deep.png")
    assert image.save(path)

    base = editing.develop_base(path, max_size=None)
    assert len(np.unique(base[0, :, 1])) > 1000
    assert abs(float(base[0, 0, 1]) - float(editing.develop.DECODE_16BIT[20000])) < 1e-4


def test_sources_in_another_colour_space_are_converted_to_srgb(tmp_path):
    image = QImage(40, 30, QImage.Format.Format_RGB32)
    image.fill(QColor(30, 200, 40))
    image.setColorSpace(QColorSpace(QColorSpace.NamedColorSpace.DisplayP3))
    path = str(tmp_path / "p3.png")
    assert image.save(path)  # with its ICC profile

    base = editing.develop_base(path, max_size=None)
    expected = image.convertedToColorSpace(QColorSpace(QColorSpace.NamedColorSpace.SRgb))
    green = editing.develop.DECODE_16BIT[expected.pixelColor(0, 0).green() * 257]
    assert abs(float(base[0, 0, 1]) - float(green)) < 0.01
    # P3 green is beyond sRGB: decoded as sRGB, the photo would be duller.
    assert float(base[0, 0, 1]) > float(editing.develop.DECODE_16BIT[200 * 257]) + 0.02

    preview, _native = load_image(path, None)  # culling shows it alike
    assert abs(preview.pixelColor(0, 0).green() - expected.pixelColor(0, 0).green()) <= 1


def test_exposure_shift_keeps_an_older_raw_edit_as_bright(tmp_path, monkeypatch):
    pixels = np.full((20, 30, 3), 6000, dtype=np.uint16)
    pixels[0, 0] = 65535  # clipped: left out
    monkeypatch.setattr(editing.rawpy, "imread", lambda path: FakeRaw(pixels))
    shift = editing.exposure_shift(str(tmp_path / "a.CR2"), 1)
    assert shift == pytest.approx(-np.log2(editing.develop.RAW_GAIN), abs=1e-4)
