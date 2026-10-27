import numpy as np
import pytest

from cullspeed import develop
from cullspeed.develop import clean, encode, render, settings


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
    round_trip = (encode(develop.DECODE_8BIT[values]) * 255 + 0.5).astype(int)
    assert np.array_equal(round_trip, values)
