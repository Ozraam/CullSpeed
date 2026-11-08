"""Quick, non-destructive edits: the processing pipeline.

Input is linear RGB (sRGB primaries) as float32. In order:

- linear: white balance, exposure, shadows / highlights;
- display (sRGB-encoded): contrast, saturation, film simulation LUT,
  vignette, grain.

Every step depends only on a pixel and its position in the full image, so
a large image can be processed in strips (export) with the same result.
An edit is a plain dict holding only the settings that differ from
DEFAULTS, which is also how it is stored in the session.

The framing (quarter turns, straightening, then the crop, which is in
fractions of the turned picture) is applied before all of this, so the vignette
and the grain follow the final frame: render() is given the framed pixels.
"""
import math

import numpy as np

from .lut import apply as apply_lut

MIN_CROP_SIZE = 0.005  # smallest stored crop side, as a fraction of the photo's

DEFAULTS = {
    "crop": None,  # [left, top, right, bottom], fractions of the turned photo's width and height
    "rotate": 0,  # quarter turns clockwise, in degrees: 0, 90, 180 or 270
    "straighten": 0.0,  # degrees clockwise; the picture is enlarged to fill the frame
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
    # How a RAW is developed (see PROCESS); edits made before process 2 keep 1, so they keep their look.
    "process": 2,
}
RANGES = {
    "straighten": (-15.0, 15.0),
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
    "process": (1, 2),
}

# What belongs to one photo's framing: not part of its look, so neither copied nor kept in a preset.
FRAMING = ("crop", "rotate", "straighten")
# What belongs to one photo, its framing and how it is developed: what copy, paste and presets leave alone.
OWN = (*FRAMING, "process")

# How RAWs are developed. 1: LibRaw's automatic brightness, which clips the brightest 1%
# and differs from photo to photo. 2: no automatic brightness and blended highlights,
# then a fixed gain and, in render(), a shoulder that rolls the highlights off instead of
# clipping them, so that Exposure and Highlights can bring them back. Gain and shoulder
# were fitted to camera JPEGs (Panasonic RW2), so that a RAW looks close to its preview.
PROCESS = 2
RAW_GAIN = 2.0 ** 2.5
RAW_SHOULDER = 0.2  # linear value where the roll-off starts

LUMA = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)

# sRGB transfer functions as lookup tables: a gather is far cheaper than pow().
_ENCODE_STEPS = 65535
_x = np.linspace(0.0, 1.0, _ENCODE_STEPS + 1, dtype=np.float64)
_ENCODE = np.where(_x <= 0.0031308, _x * 12.92, 1.055 * np.power(_x, 1 / 2.4) - 0.055).astype(np.float32)
# 16-bit sRGB -> linear, exact for 8-bit sources too (an 8-bit value v arrives as v * 257).
_v = np.arange(65536, dtype=np.float64) / 65535
DECODE_16BIT = np.where(_v <= 0.04045, _v / 12.92, np.power((_v + 0.055) / 1.055, 2.4)).astype(np.float32)
del _x, _v


def clean_crop(crop):
    """`crop` as stored: [left, top, right, bottom] in 0–1, or None if invalid or the whole photo."""
    if not isinstance(crop, (list, tuple)) or len(crop) != 4:
        return None
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in crop):
        return None
    left, top, right, bottom = (round(min(1.0, max(0.0, float(v))), 5) for v in crop)
    if right - left < MIN_CROP_SIZE or bottom - top < MIN_CROP_SIZE:
        return None
    if (left, top, right, bottom) == (0.0, 0.0, 1.0, 1.0):
        return None
    return [left, top, right, bottom]


def crop_box(height, width, crop):
    """(top, bottom, left, right) in pixels of `crop` on an image of that size, at least 1×1."""
    left, top, right, bottom = crop
    x0, y0 = min(width - 1, round(left * width)), min(height - 1, round(top * height))
    return y0, max(y0 + 1, round(bottom * height)), x0, max(x0 + 1, round(right * width))


def crop_array(pixels, crop):
    """The `crop` of `pixels` (h×w×…), without copying; `pixels` itself when `crop` is None."""
    if not crop:
        return pixels
    top, bottom, left, right = crop_box(*pixels.shape[:2], crop)
    return pixels[top:bottom, left:right]


def look(edit):
    """`edit` without what belongs to its photo (see OWN): what can be carried over to another photo."""
    return {key: value for key, value in edit.items() if key not in OWN}


def own(edit):
    """Only what of `edit` belongs to its photo: its framing and how it is developed."""
    return {key: edit[key] for key in OWN if key in edit}


def framing(edit):
    """Only the framing of `edit`."""
    return {key: edit[key] for key in FRAMING if edit.get(key)}


def turned(edit, degrees):
    """`edit` after a further quarter turn (90 or -90): a crop stays on the same part of the picture."""
    crop = edit.get("crop")
    if crop:
        left, top, right, bottom = crop
        crop = [1 - bottom, left, 1 - top, right] if degrees > 0 else [top, 1 - right, bottom, 1 - left]
    return clean({**edit, "rotate": (edit.get("rotate", 0) + degrees) % 360, "crop": crop})


def framed_size(height, width, edit):
    """(height, width) of a picture of that size once `edit` has framed it."""
    if edit.get("rotate", 0) % 180:
        height, width = width, height
    if edit.get("crop"):
        top, bottom, left, right = crop_box(height, width, edit["crop"])
        return bottom - top, right - left
    return height, width


def turn_array(pixels, rotate):
    """`pixels` turned clockwise by `rotate` degrees, without copying."""
    return np.rot90(pixels, -(rotate // 90)) if rotate else pixels


def straighten_zoom(height, width, degrees):
    """How much a picture is enlarged so that, turned by `degrees`, it still fills its frame."""
    angle = math.radians(abs(degrees))
    return math.cos(angle) + math.sin(angle) * max(height / width, width / height)


def straighten_rows(pixels, degrees, top, bottom):
    """Rows `top` to `bottom` of `pixels` straightened by `degrees` clockwise, about the centre (bilinear)."""
    height, width = pixels.shape[:2]
    angle = math.radians(degrees)
    cos, sin = math.cos(angle), math.sin(angle)
    zoom = straighten_zoom(height, width, degrees)
    centre_x, centre_y = (width - 1) / 2, (height - 1) / 2
    dx = np.arange(width, dtype=np.float32) - centre_x
    dy = (np.arange(top, bottom, dtype=np.float32) - centre_y)[:, None]
    # Where each pixel of the result comes from: the turn and the enlargement, inverted.
    x = np.clip((cos * dx + sin * dy) / zoom + centre_x, 0, width - 1)
    y = np.clip((cos * dy - sin * dx) / zoom + centre_y, 0, height - 1)
    x0, y0 = x.astype(np.intp), y.astype(np.intp)
    x1, y1 = np.minimum(x0 + 1, width - 1), np.minimum(y0 + 1, height - 1)
    fx, fy = (x - x0)[..., None], (y - y0)[..., None]
    upper = pixels[y0, x0] * (1 - fx) + pixels[y0, x1] * fx
    lower = pixels[y1, x0] * (1 - fx) + pixels[y1, x1] * fx
    out = upper * (1 - fy) + lower * fy
    return (out + 0.5).astype(pixels.dtype) if np.issubdtype(pixels.dtype, np.integer) else out


def clean(edit):
    """`edit` without default or invalid values: what is stored. {} means no edit."""
    result = {}
    if not isinstance(edit, dict):
        return result
    for key, default in DEFAULTS.items():
        value = edit.get(key, default)
        if key == "crop":
            crop = clean_crop(value)
            if crop:
                result[key] = crop
            continue
        if key == "rotate":
            valid = not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)
            if valid and value % 90 == 0 and value % 360:
                result[key] = int(value % 360)
            continue
        if key == "lut":
            if isinstance(value, str) and value:
                result[key] = value
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            continue
        low, high = RANGES[key]
        value = min(high, max(low, value))
        if key == "straighten":
            value = round(float(value), 1)
        else:
            value = round(float(value), 2) if key == "exposure" else int(round(value))
        if value != default:
            result[key] = value
    if set(result) == {"process"}:
        return {}  # nothing else is edited: the photo isn't, and gets the current process when it is
    return result


def settings(edit):
    """Every setting of `edit`, defaults included."""
    return {**DEFAULTS, **clean(edit)}


def encode(linear):
    """Linear [0, 1] -> sRGB-encoded [0, 1] (values outside are clipped)."""
    index = (np.clip(linear, 0.0, 1.0) * _ENCODE_STEPS).astype(np.uint16)
    return _ENCODE[index]


def shoulder(rgb):
    """The highlight roll-off of RAWs (process 2): unchanged below RAW_SHOULDER, then towards 1, never reaching it."""
    t = np.float32(RAW_SHOULDER)
    above = rgb > t
    if not above.any():
        return rgb
    rolled = t + (1 - t) * -np.expm1(-(np.maximum(rgb, t) - t) / (1 - t))
    return np.where(above, rolled, rgb).astype(np.float32, copy=False)


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


def render(linear, edit, table=None, seed=0, origin=(0, 0), full_size=None, raw=False):
    """Apply `edit` to `linear` (float32, h×w×3); returns sRGB uint8 h×w×3.

    `raw` says that `linear` is a RAW's development, which process 2 rolls off
    in the highlights. `table` is the film simulation LUT from lut.build_table() (None: no LUT).
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

    if raw and edit.get("process", PROCESS) >= 2:  # (not settings(): clean() drops a process left alone)
        rgb = shoulder(rgb)

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
