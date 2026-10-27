"""Quick, non-destructive edits: the processing pipeline.

Input is linear RGB (sRGB primaries) as float32. In order:

- linear: white balance, exposure, shadows / highlights;
- display (sRGB-encoded): contrast, saturation, film simulation LUT,
  vignette, grain.

Every step depends only on a pixel and its position in the full image, so
a large image can be processed in strips (export) with the same result.
An edit is a plain dict holding only the settings that differ from
DEFAULTS, which is also how it is stored in the session.
"""
import numpy as np

from .lut import apply as apply_lut

DEFAULTS = {
    "exposure": 0.0,  # EV
    "temperature": 0,  # -100 cooler … +100 warmer
    "tint": 0,  # -100 greener … +100 more magenta
    "contrast": 0,
    "highlights": 0,
    "shadows": 0,
    "saturation": 0,
    "lut": None,  # film simulation: LUT id, see lut.scan()
    "lut_strength": 100,
    "grain": 0,
    "grain_size": 1,
    "vignette": 0,  # negative darkens the corners
}
RANGES = {
    "exposure": (-5.0, 5.0),
    "temperature": (-100, 100),
    "tint": (-100, 100),
    "contrast": (-100, 100),
    "highlights": (-100, 100),
    "shadows": (-100, 100),
    "saturation": (-100, 100),
    "lut_strength": (0, 100),
    "grain": (0, 100),
    "grain_size": (1, 3),
    "vignette": (-100, 100),
}

LUMA = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)

# sRGB transfer functions as lookup tables: a gather is far cheaper than pow().
_ENCODE_STEPS = 65535
_x = np.linspace(0.0, 1.0, _ENCODE_STEPS + 1, dtype=np.float64)
_ENCODE = np.where(_x <= 0.0031308, _x * 12.92, 1.055 * np.power(_x, 1 / 2.4) - 0.055).astype(np.float32)
_v = np.arange(256, dtype=np.float64) / 255
DECODE_8BIT = np.where(_v <= 0.04045, _v / 12.92, np.power((_v + 0.055) / 1.055, 2.4)).astype(np.float32)
del _x, _v


def clean(edit):
    """`edit` without default or invalid values: what is stored. {} means no edit."""
    result = {}
    if not isinstance(edit, dict):
        return result
    for key, default in DEFAULTS.items():
        value = edit.get(key, default)
        if key == "lut":
            if isinstance(value, str) and value:
                result[key] = value
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        low, high = RANGES[key]
        value = min(high, max(low, value))
        value = round(float(value), 2) if key == "exposure" else int(round(value))
        if value != default:
            result[key] = value
    return result


def settings(edit):
    """Every setting of `edit`, defaults included."""
    return {**DEFAULTS, **clean(edit)}


def encode(linear):
    """Linear [0, 1] -> sRGB-encoded [0, 1] (values outside are clipped)."""
    index = (np.clip(linear, 0.0, 1.0) * _ENCODE_STEPS).astype(np.uint16)
    return _ENCODE[index]


def _smoothstep(low, high, x):
    t = np.clip((x - low) / (high - low), 0.0, 1.0)
    return t * t * (3 - 2 * t)


def _white_balance(temperature, tint):
    """Per-channel gains for a relative white balance shift, at constant brightness."""
    warm = temperature / 100 * 0.6
    magenta = tint / 100 * 0.4
    gains = np.exp2(np.array([warm, -magenta, -warm], dtype=np.float32))
    return gains / float(gains @ LUMA)


def _tone_table(contrast):
    """encode() followed by the contrast S-curve, as one table indexed like encode()."""
    if not contrast:
        return _ENCODE
    power = 2.0 ** (contrast / 100)  # 0.5 (flat) … 2 (punchy); 0, 0.5 and 1 stay put
    x = _ENCODE.astype(np.float64)
    curved = np.where(x < 0.5, 0.5 * np.power(2 * x, power), 1 - 0.5 * np.power(2 - 2 * x, power))
    return curved.astype(np.float32)


def _grain(shape, origin, full_width, amount, size, seed):
    """Gaussian grain of `shape` (h, w), in cells that scale with the full image width."""
    height, width = shape
    cell = max(1, round(full_width / 2000 * size))
    top, left = origin
    first_row, row_offset = divmod(top, cell)
    first_col, col_offset = divmod(left, cell)
    rows = -(-(height + row_offset) // cell)
    cols = -(-(width + col_offset) // cell)
    rng = np.random.default_rng([seed, first_row, first_col])
    noise = rng.standard_normal((rows, cols), dtype=np.float32)
    if cell > 1:
        noise = np.repeat(np.repeat(noise, cell, axis=0), cell, axis=1)
    return noise[row_offset:row_offset + height, col_offset:col_offset + width] * (amount / 100 * 0.12)


def render(linear, edit, table=None, seed=0, origin=(0, 0), full_size=None):
    """Apply `edit` to `linear` (float32, h×w×3); returns sRGB uint8 h×w×3.

    `table` is the film simulation LUT from lut.build_table() (None: no LUT).
    `origin` (row, column) and `full_size` (height, width) place this block
    in the full image when rendering in strips.
    """
    s = settings(edit)
    height, width = linear.shape[:2]
    full_height, full_width = full_size or (height, width)

    rgb = linear * (_white_balance(s["temperature"], s["tint"]) * np.float32(2.0 ** s["exposure"]))

    if s["highlights"] or s["shadows"]:
        perceived = encode(rgb @ LUMA)
        stops = np.zeros((height, width), dtype=np.float32)
        if s["highlights"]:
            stops += s["highlights"] / 100 * _smoothstep(0.45, 1.0, perceived)
        if s["shadows"]:
            stops += s["shadows"] / 100 * 1.5 * (1 - _smoothstep(0.0, 0.5, perceived))
        rgb *= np.exp2(stops)[..., None]

    tone = _tone_table(s["contrast"])
    out = tone[(np.clip(rgb, 0.0, 1.0) * _ENCODE_STEPS).astype(np.uint16)]

    if s["saturation"]:
        grey = (out @ LUMA)[..., None]
        out = np.clip(grey + (out - grey) * np.float32(1 + s["saturation"] / 100), 0.0, 1.0)

    if table is not None and s["lut_strength"]:
        graded = apply_lut((out * 255 + 0.5).astype(np.uint8), table).astype(np.float32) / 255
        strength = s["lut_strength"] / 100
        out = graded if strength >= 1 else out + (graded - out) * np.float32(strength)

    if s["vignette"]:
        rows = (np.arange(origin[0], origin[0] + height, dtype=np.float32) + 0.5) / full_height * 2 - 1
        cols = (np.arange(origin[1], origin[1] + width, dtype=np.float32) + 0.5) / full_width * 2 - 1
        radius = (rows[:, None] ** 2 + cols[None, :] ** 2) / 2  # 0 at the centre, 1 in the corners
        out = out * (1 + s["vignette"] / 100 * 0.9 * _smoothstep(0.1, 1.0, radius))[..., None]

    if s["grain"]:
        noise = _grain((height, width), origin, full_width, s["grain"], s["grain_size"], seed)
        luma = np.clip(out @ LUMA, 0.0, 1.0)
        out = out + (noise * (0.35 + 2.6 * luma * (1 - luma)))[..., None]  # strongest in the midtones

    return (np.clip(out, 0.0, 1.0) * 255 + 0.5).astype(np.uint8)
