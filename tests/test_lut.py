import numpy as np
import pytest
from PyQt6.QtGui import QImage

from cullspeed import lut
from cullspeed.develop import render
from cullspeed.imaging import array_to_image

LEVEL = 4  # HaldCLUT level 4: 64×64 image, 16 steps per channel


def identity_hald(path, level=LEVEL, transform=lambda rgb: rgb):
    n = level * level
    steps = np.linspace(0, 255, n)
    b, g, r = np.meshgrid(steps, steps, steps, indexing="ij")
    rgb = transform(np.stack([r, g, b], axis=-1))
    side = level ** 3
    array_to_image(np.rint(rgb).astype(np.uint8).reshape(side, side, 3)).save(str(path))
    return str(path)


def cube(path, size=3, transform=lambda rgb: rgb):
    lines = ["TITLE \"test\"", f"LUT_3D_SIZE {size}"]
    steps = np.linspace(0, 1, size)
    for b in steps:
        for g in steps:
            for r in steps:
                lines.append(" ".join(f"{v:.6f}" for v in transform(np.array([r, g, b]))))
    path.write_text("\n".join(lines) + "\n")
    return str(path)


def all_colours():
    rng = np.random.default_rng(0)
    return rng.integers(0, 256, (64, 64, 3), dtype=np.uint8)


@pytest.mark.parametrize("make", [identity_hald, cube])
def test_identity_lut_changes_nothing(tmp_path, make):
    path = make(tmp_path / ("id.png" if make is identity_hald else "id.cube"))
    table = lut.build_table(lut.load_grid(path))
    colours = all_colours()
    assert np.abs(lut.apply(colours, table).astype(int) - colours).max() <= 1


def test_cube_that_swaps_red_and_blue(tmp_path):
    table = lut.build_table(lut.load_grid(cube(tmp_path / "swap.cube", transform=lambda c: c[::-1])))
    colours = all_colours()
    assert np.abs(lut.apply(colours, table).astype(int) - colours[..., ::-1]).max() <= 1


def test_hald_grade_applies_through_the_pipeline_with_strength(tmp_path):
    path = identity_hald(tmp_path / "invert.png", transform=lambda rgb: 255 - rgb)
    table = lut.build_table(lut.load_grid(path))
    grey = np.full((4, 4, 3), 0.18, dtype=np.float32)  # renders as 118
    assert render(grey, {"lut": "x"}, table)[0, 0, 0] == 255 - 118
    assert abs(int(render(grey, {"lut": "x", "lut_strength": 50}, table)[0, 0, 0]) - 127) <= 1


def test_invalid_luts_are_rejected(tmp_path):
    QImage(50, 50, QImage.Format.Format_RGB32).save(str(tmp_path / "photo.png"))
    (tmp_path / "short.cube").write_text("LUT_3D_SIZE 2\n0 0 0\n")
    (tmp_path / "1d.cube").write_text("LUT_1D_SIZE 2\n0 0 0\n1 1 1\n")
    for name in ("photo.png", "short.cube", "1d.cube", "missing.png"):
        with pytest.raises(lut.LutError):
            lut.load_grid(str(tmp_path / name))


def test_scan_lists_luts_by_relative_id(tmp_path):
    (tmp_path / "Fuji").mkdir()
    for name in ("Fuji/Velvia 50.png", "Fuji/Astia.png", "Kodak.cube", "notes.txt", ".hidden.png"):
        (tmp_path / name).write_text("")
    found = lut.scan(str(tmp_path))
    assert list(found) == ["Kodak.cube", "Fuji/Astia.png", "Fuji/Velvia 50.png"]
    assert lut.display_name("Fuji/Velvia 50.png") == "Fuji / Velvia 50"
