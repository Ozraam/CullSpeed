import numpy as np
import pytest

from tamis import develop
from tamis.develop import clean, encode, render, settings


def grey(value, size=(8, 8)):
    return np.full((*size, 3), value, dtype=np.float32)


def gradient(height=60, width=90):
    rng = np.random.default_rng(1)
    return rng.random((height, width, 3), dtype=np.float32) ** 2.2


def test_clean_keeps_only_valid_non_default_settings():
    assert clean({"exposure": 0, "contrast": 20, "grain": 500, "tint": "x", "lut": "", "nope": 1}) == {
        "contrast": 20,
        "grain": 100,
    }
    assert clean({"exposure": 1.234, "lut": "Fuji/Velvia.png"}) == {"exposure": 1.23, "lut": "Fuji/Velvia.png"}
    assert clean(None) == {}
    assert settings({})["lut_strength"] == 100


def test_no_edit_is_plain_srgb_encoding():
    assert render(grey(0.18), {})[0, 0].tolist() == [118, 118, 118]
    assert render(grey(0.0), {})[0, 0].tolist() == [0, 0, 0]
    assert render(grey(1.0), {})[0, 0].tolist() == [255, 255, 255]


def test_exposure_doubles_the_light_per_stop():
    brighter = render(grey(0.18), {"exposure": 1.0})[0, 0, 0]
    assert brighter == round(float(encode(np.float32(0.36))) * 255)


def test_temperature_and_tint_shift_colour_not_brightness():
    warm = render(grey(0.18), {"temperature": 60})[0, 0].astype(int)
    cool = render(grey(0.18), {"temperature": -60})[0, 0].astype(int)
    magenta = render(grey(0.18), {"tint": 60})[0, 0].astype(int)
    assert warm[0] > warm[2] and cool[2] > cool[0]
    assert magenta[1] < magenta[0] and magenta[1] < magenta[2]
    assert abs(int(warm @ [0.2126, 0.7152, 0.0722]) - 118) <= 6


def test_contrast_keeps_black_mid_grey_and_white():
    for value in (0.0, 0.2140, 1.0):  # 0.2140 linear ~ 0.5 encoded
        assert abs(int(render(grey(value), {"contrast": 80})[0, 0, 0]) - int(render(grey(value), {})[0, 0, 0])) <= 1
    dark = render(grey(0.05), {"contrast": 80})[0, 0, 0]
    assert dark < render(grey(0.05), {})[0, 0, 0]


def test_shadows_and_highlights_act_on_their_own_tones():
    shadows = int(render(grey(0.02), {"shadows": 80})[0, 0, 0]) - int(render(grey(0.02), {})[0, 0, 0])
    on_highlight = int(render(grey(0.8), {"shadows": 80})[0, 0, 0]) - int(render(grey(0.8), {})[0, 0, 0])
    assert shadows > 10 and abs(on_highlight) <= 1  # about +1 EV in deep shadows
    assert render(grey(0.7), {"highlights": -80})[0, 0, 0] < render(grey(0.7), {})[0, 0, 0]


def test_saturation_minus_100_is_black_and_white():
    colour = np.zeros((4, 4, 3), dtype=np.float32)
    colour[..., 0] = 0.6
    pixel = render(colour, {"saturation": -100})[0, 0]
    assert pixel[0] == pixel[1] == pixel[2]


def test_vignette_darkens_corners_only():
    out = render(grey(0.18, (101, 151)), {"vignette": -80})
    assert out[50, 75, 0] == 118 and out[0, 0, 0] < 80


def test_grain_is_repeatable_and_keeps_the_average():
    a = render(grey(0.18, (200, 300)), {"grain": 60}, seed=7)
    b = render(grey(0.18, (200, 300)), {"grain": 60}, seed=7)
    assert np.array_equal(a, b)
    assert a.std() > 5 and abs(a.mean() - 118) < 2


@pytest.mark.parametrize("edit", [
    {"exposure": 0.7, "contrast": 30, "shadows": 40, "highlights": -30, "saturation": 25, "vignette": -50},
    {"temperature": 30, "tint": -20, "grain": 40, "grain_size": 2},
])
def test_rendering_in_strips_matches_rendering_whole(edit):
    image = gradient(120, 90)
    whole = render(image, edit, seed=3)
    strips = np.concatenate(
        [render(image[y:y + 40], edit, seed=3, origin=(y, 0), full_size=(120, 90)) for y in range(0, 120, 40)]
    )
    if edit.get("grain"):
        # Grain is drawn per strip; only check it stays the same kind of noise.
        assert abs(float(whole.mean()) - float(strips.mean())) < 1.5
    else:
        assert np.array_equal(whole, strips)


def test_decode_table_inverts_encode():
    values = np.arange(256)
    round_trip = (encode(develop.DECODE_16BIT[values * 257]) * 255 + 0.5).astype(int)  # 8-bit values, as 16 bits
    assert np.array_equal(round_trip, values)
    v = np.arange(65536) / 65535  # 16-bit values decode with the sRGB formula itself
    exact = np.where(v <= 0.04045, v / 12.92, ((v + 0.055) / 1.055) ** 2.4)
    assert np.abs(develop.DECODE_16BIT - exact).max() < 1e-6


# -- framing -------------------------------------------------------------------

def test_clean_keeps_quarter_turns_and_bounds_straightening():
    assert develop.clean({"rotate": 90, "straighten": 2.34}) == {"rotate": 90, "straighten": 2.3}
    assert develop.clean({"rotate": 450}) == {"rotate": 90}
    assert develop.clean({"rotate": -90}) == {"rotate": 270}
    assert develop.clean({"rotate": 360, "straighten": 0}) == {}
    assert develop.clean({"rotate": 45}) == {}
    assert develop.clean({"rotate": True, "straighten": "a"}) == {}
    assert develop.clean({"straighten": 99}) == {"straighten": 15.0}


def test_look_and_framing_split_an_edit():
    edit = {"contrast": 10, "lut": "a.cube", "crop": [0, 0, 0.5, 0.5], "rotate": 90, "straighten": 1.5}
    assert develop.look(edit) == {"contrast": 10, "lut": "a.cube"}
    assert develop.framing(edit) == {"crop": [0, 0, 0.5, 0.5], "rotate": 90, "straighten": 1.5}


def test_turn_array_goes_clockwise():
    pixels = np.array([[1, 2], [3, 4]])
    assert develop.turn_array(pixels, 90).tolist() == [[3, 1], [4, 2]]
    assert develop.turn_array(pixels, 180).tolist() == [[4, 3], [2, 1]]
    assert develop.turn_array(pixels, 270).tolist() == [[2, 4], [1, 3]]
    assert develop.turn_array(pixels, 0) is pixels


@pytest.mark.parametrize("degrees", [90, -90])
def test_a_crop_stays_on_the_same_part_of_the_picture_when_turned(degrees):
    pixels = np.arange(40 * 60).reshape(40, 60)
    edit = {"crop": [0.25, 0.5, 0.5, 0.75], "contrast": 5}
    turned = develop.turned(edit, degrees)
    assert turned["rotate"] == degrees % 360 and turned["contrast"] == 5
    before = develop.crop_array(pixels, edit["crop"])
    after = develop.crop_array(develop.turn_array(pixels, turned["rotate"]), turned["crop"])
    assert sorted(before.ravel()) == sorted(after.ravel())


def test_four_turns_are_no_edit_at_all():
    edit = {}
    for _ in range(4):
        edit = develop.turned(edit, 90)
    assert edit == {}


def test_framed_size_follows_turns_and_crop():
    assert develop.framed_size(200, 300, {}) == (200, 300)
    assert develop.framed_size(200, 300, {"rotate": 90}) == (300, 200)
    assert develop.framed_size(200, 300, {"rotate": 180}) == (200, 300)
    assert develop.framed_size(200, 300, {"rotate": 90, "crop": [0, 0, 0.5, 0.5]}) == (150, 100)


def test_straightening_turns_clockwise_and_leaves_no_empty_corner():
    pixels = np.zeros((101, 201, 3), dtype=np.float32)
    pixels[50, :] = 1.0  # a horizontal line through the centre
    out = develop.straighten_rows(pixels, 5.0, 0, 101)
    assert out.shape == pixels.shape
    rows = out[..., 0].argmax(axis=0)
    assert rows[150] > 50 > rows[50]  # clockwise: the right goes down, the left up
    assert out[50, 100, 0] == pytest.approx(1.0)  # the centre stays

    # The corners of the frame still fall inside the picture: nothing is read past its edges.
    height, width = pixels.shape[:2]
    zoom = develop.straighten_zoom(height, width, 15.0)
    angle = np.radians(15.0)
    for dx, dy in ((width / 2, height / 2), (width / 2, -height / 2)):
        assert abs(np.cos(angle) * dx + np.sin(angle) * dy) / zoom <= width / 2 + 1e-6
        assert abs(np.cos(angle) * dy - np.sin(angle) * dx) / zoom <= height / 2 + 1e-6


def test_straightening_in_strips_gives_the_same_result_and_keeps_the_type():
    pixels = (np.random.default_rng(1).random((60, 90, 3)) * 65535).astype(np.uint16)
    whole = develop.straighten_rows(pixels, -3.2, 0, 60)
    strips = np.concatenate([develop.straighten_rows(pixels, -3.2, top, top + 20) for top in range(0, 60, 20)])
    assert whole.dtype == np.uint16
    assert np.array_equal(whole, strips)


# -- RAW development -------------------------------------------------------------

def test_shoulder_rolls_highlights_off_and_leaves_the_rest_alone():
    values = np.array([0.0, 0.1, develop.RAW_SHOULDER, 0.5, 1.0, 2.0, 4.0, 50.0], dtype=np.float32)
    rolled = develop.shoulder(values)
    assert np.array_equal(rolled[:3], values[:3])
    assert np.all(np.diff(rolled) > 0)  # still increasing
    assert np.all(rolled < 1.0 + 1e-6) and rolled[-2] > 0.99


def test_raw_highlights_beyond_white_can_be_brought_back():
    # Two highlights that both blow out once developed, as LibRaw's automatic brightness would leave them.
    linear = np.array([[[1.5, 1.5, 1.5], [3.0, 3.0, 3.0]]], dtype=np.float32)
    current = render(linear, {}, raw=True)
    assert current[0, 0, 0] < current[0, 1, 0] < 255  # rolled off: still apart
    assert render(linear, {"process": 1}, raw=True)[0, 0, 0] == 255  # the earlier process clips them
    assert render(linear, {}, raw=False)[0, 0, 0] == 255  # not a RAW: no roll-off
    darker = render(linear, {"exposure": -2.0}, raw=True)
    assert darker[0, 0, 0] < darker[0, 1, 0] < current[0, 1, 0]


def test_process_belongs_to_the_photo():
    assert clean({"process": 1}) == {}  # nothing edited: the photo gets the current process
    assert clean({"process": 2, "contrast": 10}) == {"contrast": 10}
    assert clean({"process": 1, "contrast": 10}) == {"process": 1, "contrast": 10}
    edit = {"process": 1, "contrast": 10, "rotate": 90}
    assert develop.look(edit) == {"contrast": 10}
    assert develop.own(edit) == {"process": 1, "rotate": 90}
    assert develop.turned(edit, 90)["process"] == 1
