import numpy as np
import pytest
from PyQt6.QtGui import QImage

from cullspeed import cropping, develop
from cullspeed.develop import clean, clean_crop, crop_array
from cullspeed.editing import export_photo
from cullspeed.imaging import array_to_image


def test_clean_crop_keeps_only_a_real_frame():
    assert clean_crop([0.1, 0.2, 0.9, 0.8]) == [0.1, 0.2, 0.9, 0.8]
    assert clean_crop((-1, 0.2, 2, 0.8)) == [0.0, 0.2, 1.0, 0.8]
    assert clean_crop([0, 0, 1, 1]) is None  # the whole photo: no crop
    assert clean_crop([0.5, 0.5, 0.5, 0.9]) is None  # empty
    assert clean_crop([0.9, 0.1, 0.1, 0.9]) is None  # inside out
    assert clean_crop([0, 0, 1]) is None
    assert clean_crop("nope") is None
    assert clean_crop([True, 0, 1, 1]) is None
    assert clean_crop([float("nan"), 0, 1, 1]) is None


def test_crop_is_stored_with_the_edit():
    assert clean({"crop": [0, 0, 0.5, 0.5], "contrast": 10}) == {"crop": [0, 0, 0.5, 0.5], "contrast": 10}
    assert clean({"crop": [0, 0, 1, 1]}) == {}
    assert develop.settings({})["crop"] is None


def test_crop_array_cuts_the_frame():
    pixels = np.arange(100 * 200 * 3).reshape(100, 200, 3)
    cropped = crop_array(pixels, [0.25, 0.5, 0.75, 1.0])
    assert cropped.shape == (50, 100, 3)
    assert cropped[0, 0, 0] == pixels[50, 50, 0]
    assert crop_array(pixels, None) is pixels


def test_crop_array_never_empties_the_image():
    pixels = np.zeros((10, 10, 3))
    assert crop_array(pixels, [0.999, 0.999, 1.0, 1.0]).shape == (1, 1, 3)


def test_export_applies_the_crop_and_vignette_follows_it(tmp_path):
    source = tmp_path / "photo.png"
    assert array_to_image(np.full((100, 200, 3), 128, dtype=np.uint8)).save(str(source))

    dest = export_photo(str(source), {"crop": [0.5, 0.0, 1.0, 0.5], "vignette": -100}, None, str(tmp_path / "out"))
    image = QImage(dest)
    assert (image.width(), image.height()) == (100, 50)
    # The frame's own corners are darkened, and its centre isn't.
    assert image.pixelColor(1, 1).red() < 60
    assert image.pixelColor(50, 25).red() > 110


# -- dragging the frame --------------------------------------------------------

FRAME = (0.2, 0.2, 0.8, 0.8)


def assert_inside(frame):
    left, top, right, bottom = frame
    assert 0 <= left < right <= 1 + 1e-9
    assert 0 <= top < bottom <= 1 + 1e-9


def test_moving_keeps_the_size_and_stays_inside():
    assert cropping.drag(FRAME, cropping.MOVE, 0.1, -0.1) == pytest.approx((0.3, 0.1, 0.9, 0.7))
    assert cropping.drag(FRAME, cropping.MOVE, 5, 5) == pytest.approx((0.4, 0.4, 1.0, 1.0))


def test_handles_move_their_edges():
    assert cropping.drag(FRAME, "l", 0.1, 0.3) == pytest.approx((0.3, 0.2, 0.8, 0.8))
    assert cropping.drag(FRAME, "br", 0.1, -0.1) == pytest.approx((0.2, 0.2, 0.9, 0.7))
    assert cropping.drag(FRAME, "t", 0, -9) == pytest.approx((0.2, 0.0, 0.8, 0.8))


def test_frame_cannot_collapse_or_flip():
    left, _, right, _ = cropping.drag(FRAME, "l", 9, 0)
    assert right - left == pytest.approx(cropping.MIN_SIZE)
    assert right == pytest.approx(0.8)


@pytest.mark.parametrize("handle", cropping.HANDLES)
@pytest.mark.parametrize("delta", [(0.1, 0.05), (-0.1, -0.05), (9, 9), (-9, -9), (9, -9), (-9, 9)])
@pytest.mark.parametrize("aspect", [1.0, 2 / 3, 1.5])
def test_a_shaped_frame_keeps_its_shape_and_stays_inside(handle, delta, aspect):
    frame = cropping.fit_aspect(FRAME, aspect)
    result = cropping.drag(frame, handle, *delta, aspect)
    assert_inside(result)
    assert (result[2] - result[0]) / (result[3] - result[1]) == pytest.approx(aspect)


def test_a_shaped_corner_keeps_the_opposite_corner():
    result = cropping.drag((0.2, 0.2, 0.6, 0.6), "br", 0.1, 0.1, 1.0)
    assert result == pytest.approx((0.2, 0.2, 0.7, 0.7))
    result = cropping.drag((0.2, 0.2, 0.6, 0.6), "tl", -0.1, -0.1, 1.0)
    assert result == pytest.approx((0.1, 0.1, 0.6, 0.6))


def test_fit_aspect_fills_the_photo_when_starting_from_it():
    assert cropping.fit_aspect(cropping.FULL, None) == cropping.FULL
    assert cropping.fit_aspect(cropping.FULL, 1.0) == pytest.approx(cropping.FULL)
    left, top, right, bottom = cropping.fit_aspect(cropping.FULL, 2 / 3)  # a square on a 3:2 photo
    assert (top, bottom) == pytest.approx((0, 1))
    assert (left, right) == pytest.approx((1 / 6, 5 / 6))


def test_fit_aspect_stays_inside_near_an_edge():
    frame = cropping.fit_aspect((0.0, 0.0, 0.1, 0.9), 1.0)
    assert_inside(frame)
    assert frame[2] - frame[0] == pytest.approx(frame[3] - frame[1])
