"""Shooting info (exposure, aperture, ISO, focal length, camera) from EXIF.

A small reader for the TIFF structure EXIF is stored in, found in JPEG
(APP1), TIFF-based RAWs (CR2, NEF, ARW, DNG, ORF, RW2), Fujifilm RAF
(through its embedded JPEG) and Canon CR3 (CMT1/CMT2 boxes). Anything
else, or anything malformed, simply gives no info.
"""
import struct

# EXIF sits near the start of every format handled here.
_READ_LIMIT = 1 << 20

_MAKE = 0x010F
_MODEL = 0x0110
_EXIF_IFD = 0x8769
_EXPOSURE_TIME = 0x829A
_F_NUMBER = 0x829D
_ISO = 0x8827
_DATE_TIME_ORIGINAL = 0x9003
_FOCAL_LENGTH = 0x920A
_LENS_MODEL = 0xA434
_WANTED = {_MAKE, _MODEL, _EXIF_IFD, _EXPOSURE_TIME, _F_NUMBER, _ISO, _DATE_TIME_ORIGINAL, _FOCAL_LENGTH, _LENS_MODEL}

# TIFF field type -> bytes per value
_TYPE_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8}


def read_info(path):
    """{"exposure", "aperture", "iso", "focal", "camera", "lens", "date"}, each only if known."""
    try:
        with open(path, 'rb') as file:
            data = file.read(_READ_LIMIT)
        return _describe(_find_tags(data))
    except (OSError, struct.error, ValueError, IndexError, ZeroDivisionError):
        return {}


def summary(info):
    """Up to two display lines: exposure settings, then camera / lens / date."""
    lines = []
    for keys in (("exposure", "aperture", "iso", "focal"), ("camera", "lens", "date")):
        line = " · ".join(info[key] for key in keys if info.get(key))
        if line:
            lines.append(line)
    return lines


# -- locating the TIFF structure ---------------------------------------------

def _find_tags(data):
    if data[:2] == b'\xff\xd8':
        return _jpeg_tags(data)
    if data.startswith(b'FUJIFILMCCD-RAW'):
        (offset,) = struct.unpack('>I', data[84:88])
        return _jpeg_tags(data[offset:])
    if data[4:8] == b'ftyp' and data[8:12] == b'crx ':
        return _cr3_tags(data)
    if data[:2] in (b'II', b'MM'):
        return _tiff_tags(data)
    return {}


def _jpeg_tags(data):
    i = 2
    while i + 4 <= len(data) and data[i] == 0xFF:
        marker = data[i + 1]
        if marker in (0xD9, 0xDA):  # end of image / start of scan: no more metadata
            break
        (length,) = struct.unpack('>H', data[i + 2:i + 4])
        segment = data[i + 4:i + 2 + length]
        if marker == 0xE1 and segment.startswith(b'Exif\0\0'):
            return _tiff_tags(segment[6:])
        i += 2 + length
    return {}


def _cr3_tags(data):
    tags = {}
    for box in (b'CMT1', b'CMT2'):  # IFD0 (camera), then the EXIF IFD as its own TIFF
        at = data.find(box)
        if at >= 4:
            (size,) = struct.unpack('>I', data[at - 4:at])
            tags.update(_tiff_tags(data[at + 4:at - 4 + size]))
    return tags


def _tiff_tags(tiff):
    if tiff[:2] == b'II':
        endian = '<'
    elif tiff[:2] == b'MM':
        endian = '>'
    else:
        raise ValueError("not TIFF")
    # The magic number at 2:4 is 42 for TIFF, but ORF and RW2 use their own: not checked.
    (offset,) = struct.unpack(endian + 'I', tiff[4:8])
    tags = _read_ifd(tiff, offset, endian)
    exif_offset = tags.pop(_EXIF_IFD, None)
    if isinstance(exif_offset, int):
        tags.update(_read_ifd(tiff, exif_offset, endian))
    return tags


def _read_ifd(tiff, offset, endian):
    tags = {}
    (count,) = struct.unpack(endian + 'H', tiff[offset:offset + 2])
    for n in range(count):
        entry = tiff[offset + 2 + n * 12:offset + 14 + n * 12]
        if len(entry) < 12:
            break
        tag, kind, values = struct.unpack(endian + 'HHI', entry[:8])
        if tag not in _WANTED or kind not in _TYPE_SIZES:
            continue
        size = _TYPE_SIZES[kind] * values
        if size <= 4:
            raw = entry[8:8 + size]
        else:
            (at,) = struct.unpack(endian + 'I', entry[8:12])
            raw = tiff[at:at + size]
        if len(raw) < size or not values:
            continue
        tags[tag] = _value(raw, kind, values, endian)
    return tags


def _value(raw, kind, values, endian):
    """The first value of a field (or the whole string)."""
    if kind == 2:
        return raw[:values].split(b'\0')[0].decode('utf-8', 'replace').strip()
    if kind == 3:
        return struct.unpack(endian + 'H', raw[:2])[0]
    if kind in (4, 9):
        return struct.unpack(endian + ('I' if kind == 4 else 'i'), raw[:4])[0]
    if kind in (5, 10):
        return struct.unpack(endian + ('II' if kind == 5 else 'ii'), raw[:8])
    return raw[0]


# -- formatting --------------------------------------------------------------

def _ratio(value):
    if isinstance(value, tuple):
        numerator, denominator = value
        return numerator / denominator if denominator else None
    return float(value) if isinstance(value, int) else None


def _describe(tags):
    info = {}

    exposure = _ratio(tags.get(_EXPOSURE_TIME))
    if exposure:
        info["exposure"] = f"{round(exposure, 1):g} s" if exposure >= 0.3 else f"1/{round(1 / exposure)} s"

    aperture = _ratio(tags.get(_F_NUMBER))
    if aperture:
        info["aperture"] = f"f/{round(aperture, 1):g}"

    iso = tags.get(_ISO)
    if isinstance(iso, int) and iso:
        info["iso"] = f"ISO {iso}"

    focal = _ratio(tags.get(_FOCAL_LENGTH))
    if focal:
        info["focal"] = f"{round(focal, 1):g} mm"

    make, model = tags.get(_MAKE), tags.get(_MODEL)
    if isinstance(model, str) and model:
        brand = make.split()[0] if isinstance(make, str) and make else ""
        # "Canon" + "Canon EOS R5" -> "Canon EOS R5"; "SONY" + "ILCE-7M3" -> "SONY ILCE-7M3"
        info["camera"] = model if not brand or brand.casefold() in model.casefold() else f"{brand} {model}"

    lens = tags.get(_LENS_MODEL)
    if isinstance(lens, str) and lens:
        info["lens"] = lens

    date = tags.get(_DATE_TIME_ORIGINAL)
    if isinstance(date, str) and len(date) >= 16:
        info["date"] = f"{date[:10].replace(':', '-')} {date[11:16]}"

    return info
