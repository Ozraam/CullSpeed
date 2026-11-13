"""Tiny DNG files written on the fly, so RAW decoding is tested on real files without shipping any."""

import struct

import numpy as np

# TIFF field types
_SHORT, _LONG, _RATIONAL, _BYTE, _SRATIONAL, _ASCII = 3, 4, 5, 1, 10, 2
_SIZES = {_BYTE: 1, _ASCII: 1, _SHORT: 2, _LONG: 4, _RATIONAL: 8, _SRATIONAL: 8}


def _pack(kind, values):
    if kind == _ASCII:
        return values.encode() + b"\0"
    if kind in (_RATIONAL, _SRATIONAL):
        code = "i" if kind == _SRATIONAL else "I"
        return b"".join(struct.pack(f"<{code}{code}", round(v * 10000), 10000) for v in values)
    code = {_BYTE: "B", _SHORT: "H", _LONG: "I"}[kind]
    return struct.pack(f"<{len(values)}{code}", *values)


class _Tiff:
    """A little-endian TIFF built IFD by IFD; data is placed after all IFDs."""

    def __init__(self):
        self.ifds = []  # [{tag: (kind, values)}], values may be ("data", bytes) for an offset to bytes

    def ifd(self, tags):
        self.ifds.append(tags)
        return len(self.ifds) - 1

    def build(self):
        # Layout: header, IFDs one after the other, then out-of-line values and image data.
        ifd_sizes = [2 + 12 * len(tags) + 4 for tags in self.ifds]
        ifd_offsets, offset = [], 8
        for size in ifd_sizes:
            ifd_offsets.append(offset)
            offset += size
        data = bytearray()
        data_start = offset

        def place(blob):
            if len(data) % 2:
                data.append(0)
            position = data_start + len(data)
            data.extend(blob)
            return position

        out = bytearray(b"II*\0" + struct.pack("<I", ifd_offsets[0]))
        for tags in self.ifds:
            entries = []
            for tag in sorted(tags):
                kind, values = tags[tag]
                if kind == "data":  # StripOffsets to an image blob
                    kind, values = _LONG, [place(values)]
                elif kind == "subifd":
                    kind, values = _LONG, [ifd_offsets[values]]
                blob = _pack(kind, values)
                count = len(blob) // _SIZES[kind]
                field = blob.ljust(4, b"\0") if len(blob) <= 4 else struct.pack("<I", place(blob))
                entries.append(struct.pack("<HHI", tag, kind, count) + field)
            out += struct.pack("<H", len(entries)) + b"".join(entries) + struct.pack("<I", 0)
        assert len(out) == data_start
        return bytes(out + data)


def write_dng(path, width=64, height=48, orientation=1, preview=None, color=(0.5, 0.4, 0.3), marker=True):
    """Write an RGGB DNG of a flat `color` (linear, 0-1 per channel), with a white square top left if `marker`.

    `preview` is an optional embedded JPEG (bytes) of the photo, stored as IFD0 like cameras do.
    """
    levels = np.empty((height, width, 3), np.float32)
    levels[:] = color
    if marker:
        levels[: height // 4, : width // 4] = 0.9  # tells which way is up
    cfa = np.empty((height, width), np.uint16)
    white = 4095
    cfa[0::2, 0::2] = levels[0::2, 0::2, 0] * white
    cfa[0::2, 1::2] = levels[0::2, 1::2, 1] * white
    cfa[1::2, 0::2] = levels[1::2, 0::2, 1] * white
    cfa[1::2, 1::2] = levels[1::2, 1::2, 2] * white

    tiff = _Tiff()
    raw_tags = {
        254: (_LONG, [0]),  # NewSubFileType: the main image
        256: (_LONG, [width]),
        257: (_LONG, [height]),
        258: (_SHORT, [16]),
        259: (_SHORT, [1]),  # uncompressed
        262: (_SHORT, [32803]),  # CFA
        273: ("data", cfa.astype("<u2").tobytes()),
        277: (_SHORT, [1]),
        278: (_LONG, [height]),
        279: (_LONG, [width * height * 2]),
        284: (_SHORT, [1]),
        33421: (_SHORT, [2, 2]),  # CFARepeatPatternDim
        33422: (_BYTE, [0, 1, 1, 2]),  # CFAPattern: RGGB
        50717: (_LONG, [white]),  # WhiteLevel
    }
    common = {
        271: (_ASCII, "Tamis"),
        272: (_ASCII, "Test DNG"),
        274: (_SHORT, [orientation]),
        50706: (_BYTE, [1, 4, 0, 0]),  # DNGVersion
        50708: (_ASCII, "Tamis Test DNG"),  # UniqueCameraModel
        50721: (_SRATIONAL, [1, 0, 0, 0, 1, 0, 0, 0, 1]),  # ColorMatrix1: camera = XYZ
        50778: (_SHORT, [21]),  # CalibrationIlluminant1: D65
        50728: (_RATIONAL, [1, 1, 1]),  # AsShotNeutral
    }
    if preview is None:
        tiff.ifd({**common, **raw_tags})
    else:
        main = tiff.ifd({})
        sub = tiff.ifd(raw_tags)
        tiff.ifds[main] = {
            **common,
            254: (_LONG, [1]),  # a reduced-resolution copy
            256: (_LONG, [width]),
            257: (_LONG, [height]),
            258: (_SHORT, [8, 8, 8]),
            259: (_SHORT, [7]),  # JPEG
            262: (_SHORT, [6]),  # YCbCr
            273: ("data", preview),
            277: (_SHORT, [3]),
            278: (_LONG, [height]),
            279: (_LONG, [len(preview)]),
            330: ("subifd", sub),  # SubIFDs
        }
    with open(path, "wb") as file:
        file.write(tiff.build())
    return str(path)
