"""Shooting info (exposure, aperture, ISO, focal length, camera) from EXIF, and EXIF for exports.

A small reader for the TIFF structure EXIF is stored in, found in JPEG
(APP1), TIFF-based RAWs (CR2, NEF, ARW, DNG, ORF, RW2), Fujifilm RAF
(through its embedded JPEG) and Canon CR3 (CMT1/CMT2/CMT4 boxes). Anything
else, or anything malformed, simply gives no info.

export_segment() builds the EXIF of an exported JPEG from what its source
says: camera, lens, date, exposure, copyright and GPS, with the orientation
reset, since the exported pixels are upright, and the colour space set to sRGB.
"""
import os
import struct
from datetime import datetime

# EXIF sits near the start of every format handled here.
_READ_LIMIT = 1 << 20

_MAKE = 0x010F
_MODEL = 0x0110
_EXIF_IFD = 0x8769
_EXPOSURE_TIME = 0x829A
_F_NUMBER = 0x829D
_ISO = 0x8827
_DATE_TIME_ORIGINAL = 0x9003
_SUB_SEC_ORIGINAL = 0x9291
_FOCAL_LENGTH = 0x920A
_LENS_MODEL = 0xA434
_WANTED = {
    _MAKE, _MODEL, _EXIF_IFD, _EXPOSURE_TIME, _F_NUMBER, _ISO, _DATE_TIME_ORIGINAL, _SUB_SEC_ORIGINAL, _FOCAL_LENGTH, _LENS_MODEL,
}

# TIFF field type -> bytes per value
_TYPE_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8}

_EPOCH = datetime(1970, 1, 1)


def read_info(path):
    """{"exposure", "aperture", "iso", "focal", "camera", "lens", "date"}, each only if known."""
    try:
        with open(path, 'rb') as file:
            data = file.read(_READ_LIMIT)
        return _describe(_find_tags(data))
    except (OSError, struct.error, ValueError, IndexError, ZeroDivisionError):
        return {}


def _exif_time(path):
    """Seconds of EXIF's DateTimeOriginal, with its fraction, None if the file has no valid one."""
    try:
        with open(path, 'rb') as file:
            tags = _find_tags(file.read(_READ_LIMIT))
        moment = datetime.strptime(tags[_DATE_TIME_ORIGINAL][:19], "%Y:%m:%d %H:%M:%S")
    except (OSError, struct.error, ValueError, IndexError, ZeroDivisionError, KeyError, TypeError):
        return None
    fraction = tags.get(_SUB_SEC_ORIGINAL)
    digits = fraction if isinstance(fraction, str) and fraction.isdecimal() else "0"
    return (moment - _EPOCH).total_seconds() + float(f"0.{digits}")


def capture_time(paths):
    """When the photo made of `paths` (a RAW and its JPEG, say) was taken, in seconds; None if none can be read.

    EXIF has no time zone, so this is the camera's local time, which is what
    comparing photos needs. A file without a date falls back on its
    modification time, in local time likewise.
    """
    for path in paths:
        moment = _exif_time(path)
        if moment is not None:
            return moment
    try:
        return (datetime.fromtimestamp(os.path.getmtime(paths[0])) - _EPOCH).total_seconds()
    except (OSError, ValueError, OverflowError):
        return None


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


# -- EXIF for exports -----------------------------------------------------------

_ORIENTATION = 0x0112
_SOFTWARE = 0x0131
_GPS_IFD = 0x8825
_EXIF_VERSION = 0x9000
_COLOR_SPACE = 0xA001
_PIXEL_X = 0xA002
_PIXEL_Y = 0xA003

# Copied from the source: what describes the shot, not the file (no thumbnail,
# no MakerNote, whose offsets would break, nothing about the source's pixels).
_COPIED = {
    "ifd0": {
        0x010E,  # ImageDescription
        _MAKE, _MODEL,
        0x0132,  # DateTime
        0x013B,  # Artist
        0x8298,  # Copyright
    },
    "exif": {
        _EXPOSURE_TIME, _F_NUMBER,
        0x8822,  # ExposureProgram
        _ISO,
        _EXIF_VERSION,
        _DATE_TIME_ORIGINAL,
        0x9004,  # DateTimeDigitized
        0x9010, 0x9011, 0x9012,  # OffsetTime, OffsetTimeOriginal, OffsetTimeDigitized
        0x9201, 0x9202,  # ShutterSpeedValue, ApertureValue
        0x9204, 0x9205,  # ExposureBiasValue, MaxApertureValue
        0x9207, 0x9208, 0x9209,  # MeteringMode, LightSource, Flash
        _FOCAL_LENGTH,
        0x9290, _SUB_SEC_ORIGINAL, 0x9292,  # SubSecTime, SubSecTimeOriginal, SubSecTimeDigitized
        0xA402, 0xA403,  # ExposureMode, WhiteBalance
        0xA405, 0xA406,  # FocalLengthIn35mmFilm, SceneCaptureType
        0xA430, 0xA431, 0xA432,  # CameraOwnerName, BodySerialNumber, LensSpecification
        0xA433, _LENS_MODEL, 0xA435,  # LensMake, LensModel, LensSerialNumber
    },
    "gps": None,  # all of it
}

# TIFF field type -> bytes per element (rationals are two elements)
_ELEMENT_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 4, 6: 1, 7: 1, 8: 2, 9: 4, 10: 4, 11: 4, 12: 8}
_ELEMENTS_PER_VALUE = {5: 2, 10: 2}

# A JPEG segment holds at most 65533 bytes after its length.
_SEGMENT_LIMIT = 65533


def _raw_ifd(tiff, offset, endian, wanted):
    """{tag: (type, count, little-endian bytes)} of an IFD, `wanted` tags only (None: all)."""
    fields = {}
    (count,) = struct.unpack(endian + 'H', tiff[offset:offset + 2])
    for n in range(count):
        entry = tiff[offset + 2 + n * 12:offset + 14 + n * 12]
        if len(entry) < 12:
            break
        tag, kind, values = struct.unpack(endian + 'HHI', entry[:8])
        if kind not in _ELEMENT_SIZES or (wanted is not None and tag not in wanted):
            continue
        element = _ELEMENT_SIZES[kind]
        size = element * _ELEMENTS_PER_VALUE.get(kind, 1) * values
        if size <= 4:
            raw = entry[8:8 + size]
        else:
            (at,) = struct.unpack(endian + 'I', entry[8:12])
            raw = tiff[at:at + size]
        if not values or len(raw) < size:
            continue
        if endian == '>' and element > 1:  # each element to little-endian
            raw = b''.join(raw[i:i + element][::-1] for i in range(0, size, element))
        fields[tag] = (kind, values, raw)
    return fields


def _tiff_ifds(tiff):
    """{"ifd0", "exif", "gps"} of a TIFF structure: {tag: (type, count, bytes)} each."""
    endian = {b'II': '<', b'MM': '>'}.get(tiff[:2])
    if endian is None:
        raise ValueError("not TIFF")
    (offset,) = struct.unpack(endian + 'I', tiff[4:8])
    pointers = _raw_ifd(tiff, offset, endian, {_EXIF_IFD, _GPS_IFD})
    ifds = {"ifd0": _raw_ifd(tiff, offset, endian, _COPIED["ifd0"])}
    for name, tag in (("exif", _EXIF_IFD), ("gps", _GPS_IFD)):
        if tag in pointers and pointers[tag][0] in (4, 13):
            (at,) = struct.unpack('<I', pointers[tag][2][:4])
            ifds[name] = _raw_ifd(tiff, at, endian, _COPIED[name])
    return ifds


def _jpeg_tiff(data):
    """The TIFF structure in a JPEG's EXIF segment, b'' if none."""
    i = 2
    while i + 4 <= len(data) and data[i] == 0xFF:
        marker = data[i + 1]
        if marker in (0xD9, 0xDA):
            break
        (length,) = struct.unpack('>H', data[i + 2:i + 4])
        segment = data[i + 4:i + 2 + length]
        if marker == 0xE1 and segment.startswith(b'Exif\0\0'):
            return segment[6:]
        i += 2 + length
    return b''


def _source_ifds(data):
    if data[:2] == b'\xff\xd8':
        tiff = _jpeg_tiff(data)
        return _tiff_ifds(tiff) if tiff else {}
    if data.startswith(b'FUJIFILMCCD-RAW'):
        (offset,) = struct.unpack('>I', data[84:88])
        tiff = _jpeg_tiff(data[offset:])
        return _tiff_ifds(tiff) if tiff else {}
    if data[4:8] == b'ftyp' and data[8:12] == b'crx ':
        ifds = {}
        # Each box is a TIFF structure of its own, its IFD0 holding that part.
        for box, name in ((b'CMT1', "ifd0"), (b'CMT2', "exif"), (b'CMT4', "gps")):
            at = data.find(box)
            if at >= 4:
                (size,) = struct.unpack('>I', data[at - 4:at])
                tiff = data[at + 4:at - 4 + size]
                endian = {b'II': '<', b'MM': '>'}.get(tiff[:2])
                if endian:
                    (offset,) = struct.unpack(endian + 'I', tiff[4:8])
                    ifds[name] = _raw_ifd(tiff, offset, endian, _COPIED[name])
        return ifds
    if data[:2] in (b'II', b'MM'):
        return _tiff_ifds(data)
    return {}


def _short(value):
    return (3, 1, struct.pack('<H', value))


def _long(value):
    return (4, 1, struct.pack('<I', value))


def _ascii(text):
    raw = text.encode('ascii', 'replace') + b'\0'
    return (2, len(raw), raw)


def _write_tiff(ifds):
    """Little-endian TIFF bytes of [IFD0, EXIF, GPS] (empty ones left out but for IFD0), pointers filled in."""
    names = [name for name in ("ifd0", "exif", "gps") if name == "ifd0" or ifds.get(name)]
    tables = {name: dict(ifds.get(name, {})) for name in names}
    for name, tag in (("exif", _EXIF_IFD), ("gps", _GPS_IFD)):
        if name in tables:
            tables["ifd0"][tag] = _long(0)  # placeholder, so the sizes are right

    def data_size(table):
        return sum(len(raw) + len(raw) % 2 for _kind, _count, raw in table.values() if len(raw) > 4)

    offsets, at = {}, 8
    for name in names:
        offsets[name] = at
        at += 2 + 12 * len(tables[name]) + 4 + data_size(tables[name])
    for name, tag in (("exif", _EXIF_IFD), ("gps", _GPS_IFD)):
        if name in tables:
            tables["ifd0"][tag] = _long(offsets[name])

    out = bytearray(b'II*\0' + struct.pack('<I', 8))
    for name in names:
        table = tables[name]
        data_at = offsets[name] + 2 + 12 * len(table) + 4
        entries, data = bytearray(struct.pack('<H', len(table))), bytearray()
        for tag in sorted(table):
            kind, count, raw = table[tag]
            if len(raw) <= 4:
                entries += struct.pack('<HHI', tag, kind, count) + raw.ljust(4, b'\0')
            else:
                entries += struct.pack('<HHII', tag, kind, count, data_at + len(data))
                data += raw + b'\0' * (len(raw) % 2)
        entries += struct.pack('<I', 0)  # no next IFD: no thumbnail
        out += entries + data
    return bytes(out)


def export_segment(path, size):
    """The APP1 payload (`Exif\\0\\0` + TIFF) for an export of `path` measuring `size` (width, height).

    Holds what the source says of the shot, with orientation 1 and sRGB.
    b'' if the source can't be read; anything that doesn't fit a JPEG segment
    is dropped, GPS first, then the copied tags.
    """
    try:
        with open(path, 'rb') as file:
            ifds = _source_ifds(file.read(_READ_LIMIT))
    except (OSError, struct.error, ValueError, IndexError):
        ifds = {}
    width, height = size
    base = {
        "ifd0": {_ORIENTATION: _short(1), _SOFTWARE: _ascii("Tamis")},
        "exif": {_EXIF_VERSION: (7, 4, b'0232'), _COLOR_SPACE: _short(1), _PIXEL_X: _long(width), _PIXEL_Y: _long(height)},
    }
    attempts = (
        ifds,
        {name: table for name, table in ifds.items() if name != "gps"},
        {},
    )
    for copied in attempts:
        merged = {
            name: {**copied.get(name, {}), **base.get(name, {})} for name in ("ifd0", "exif", "gps")
        }
        tiff = b'Exif\0\0' + _write_tiff(merged)
        if len(tiff) <= _SEGMENT_LIMIT:
            return tiff
    return b''


def with_exif(jpeg, segment):
    """`jpeg` (bytes) with `segment` as its EXIF, put after SOI and a JFIF APP0; any EXIF it had is replaced."""
    if not segment or jpeg[:2] != b'\xff\xd8':
        return jpeg
    out, i, placed = bytearray(b'\xff\xd8'), 2, False
    app1 = b'\xff\xe1' + struct.pack('>H', len(segment) + 2) + segment
    while i + 4 <= len(jpeg) and jpeg[i] == 0xFF and jpeg[i + 1] not in (0xD9, 0xDA):
        marker = jpeg[i + 1]
        (length,) = struct.unpack('>H', jpeg[i + 2:i + 4])
        part = jpeg[i:i + 2 + length]
        if not placed and marker != 0xE0:
            out += app1
            placed = True
        if not (marker == 0xE1 and part[4:10] == b'Exif\0\0'):
            out += part
        i += 2 + length
    if not placed:
        out += app1
    return bytes(out + jpeg[i:])


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
