import struct

from cullspeed.exif import read_info, summary


def tiff(entries, exif_entries, endian="<"):
    """A little TIFF with IFD0 `entries` and an EXIF IFD; values are (tag, type, value)."""
    def ifd(items, start):
        body, extra = b"", b""
        data_at = start + 2 + 12 * len(items) + 4
        for tag, kind, value in items:
            if kind == 2:
                raw = value.encode() + b"\0"
            elif kind == 3:
                raw = struct.pack(endian + "H", value)
            elif kind == 4:
                raw = struct.pack(endian + "I", value)
            else:  # rational
                raw = struct.pack(endian + "II", *value)
            count = len(raw) if kind == 2 else 1
            if len(raw) <= 4:
                field = raw.ljust(4, b"\0")
            else:
                field = struct.pack(endian + "I", data_at + len(extra))
                extra += raw
            body += struct.pack(endian + "HHI", tag, kind, count) + field
        return struct.pack(endian + "H", len(items)) + body + b"\0\0\0\0" + extra

    header = (b"II" if endian == "<" else b"MM") + struct.pack(endian + "HI", 42, 8)
    ifd0_size = len(ifd(entries + [(0x8769, 4, 0)], 8))
    ifd0 = ifd(entries + [(0x8769, 4, 8 + ifd0_size)], 8)
    return header + ifd0 + ifd(exif_entries, 8 + ifd0_size)


SHOT = tiff(
    [(0x010F, 2, "Canon"), (0x0110, 2, "Canon EOS R5")],
    [
        (0x829A, 5, (1, 250)),
        (0x829D, 5, (28, 10)),
        (0x8827, 3, 400),
        (0x920A, 5, (50, 1)),
        (0x9003, 2, "2024:05:01 12:34:56"),
        (0xA434, 2, "RF50mm F1.8 STM"),
    ],
)


def jpeg_with_exif(tiff_bytes):
    app1 = b"Exif\0\0" + tiff_bytes
    return b"\xff\xd8" + b"\xff\xe1" + struct.pack(">H", len(app1) + 2) + app1 + b"\xff\xda\0\0"


def test_jpeg(tmp_path):
    path = tmp_path / "a.jpg"
    path.write_bytes(jpeg_with_exif(SHOT))

    info = read_info(str(path))

    assert info == {
        "exposure": "1/250 s",
        "aperture": "f/2.8",
        "iso": "ISO 400",
        "focal": "50 mm",
        "camera": "Canon EOS R5",
        "lens": "RF50mm F1.8 STM",
        "date": "2024-05-01 12:34",
    }
    assert summary(info) == ["1/250 s · f/2.8 · ISO 400 · 50 mm", "Canon EOS R5 · RF50mm F1.8 STM · 2024-05-01 12:34"]


def test_big_endian_tiff_raw_and_brand_prefix(tmp_path):
    path = tmp_path / "a.NEF"
    path.write_bytes(tiff([(0x010F, 2, "SONY"), (0x0110, 2, "ILCE-7M3")], [(0x829A, 5, (2, 1))], endian=">"))
    assert read_info(str(path)) == {"exposure": "2 s", "camera": "SONY ILCE-7M3"}


def test_unknown_or_broken_files_give_nothing(tmp_path):
    (tmp_path / "a.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (tmp_path / "b.jpg").write_bytes(b"\xff\xd8\xff\xe1\x00\x40Exif\0\0II*\0\xff\xff\xff\x7f")
    assert read_info(str(tmp_path / "a.png")) == {}
    assert read_info(str(tmp_path / "b.jpg")) == {}
    assert read_info(str(tmp_path / "missing.jpg")) == {}
    assert summary({}) == []
