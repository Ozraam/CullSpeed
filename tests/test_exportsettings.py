import os

from tamis.exportsettings import (
    ExportSettings,
    date_text,
    dump_profiles,
    file_stem,
    load_profiles,
    load_settings,
    pattern_problem,
)


def test_invalid_values_take_their_default():
    settings = ExportSettings.from_dict({"long_edge": -5, "quality": 300, "folder": "  ", "pattern": "{nope}"})
    assert settings == ExportSettings()
    assert ExportSettings.from_dict({"long_edge": True, "quality": "high"}) == ExportSettings()
    assert ExportSettings.from_dict("nonsense") == ExportSettings()
    assert load_settings("{not json") == ExportSettings()


def test_settings_and_profiles_round_trip():
    web = ExportSettings(2048, 80, "~/Web", "{date}_{seq}")
    assert ExportSettings.from_dict(web.to_dict()) == web
    assert load_profiles(dump_profiles({"Web": web, "Print": ExportSettings()})) == {"Web": web, "Print": ExportSettings()}
    assert load_profiles("[1, 2]") == {}


def test_file_stem_fills_the_pattern_and_stays_a_file_name():
    assert file_stem("{name}", "IMG_1") == "IMG_1"
    assert file_stem("{date}_{name}_{seq}", "IMG_1", "2024-05-01", 7, 12) == "2024-05-01_IMG_1_007"
    assert file_stem("{seq}", "IMG_1", seq=42, total=1500) == "0042"
    assert file_stem("{folder}/{name}", "IMG_1", folder="a:b") == "a-b-IMG_1"
    assert file_stem("...", "IMG_1") == "IMG_1"  # nothing usable left


def test_pattern_problems():
    assert pattern_problem("{name}-{seq}") is None
    assert "{size}" in pattern_problem("{name}-{size}")
    assert pattern_problem("{name") is not None
    assert pattern_problem(" ") is not None


def test_date_text():
    assert date_text(0) == "1970-01-01"
    assert date_text(None) == "undated"


def test_exports_may_not_land_among_the_photos(tmp_path):
    folder = str(tmp_path)
    assert ExportSettings().problem(folder) is None
    assert ExportSettings(folder="web/large").problem(folder) is None
    assert ExportSettings(folder=".").problem(folder) is not None
    assert ExportSettings(folder="_KEEPS").problem(folder) is not None
    assert ExportSettings(folder=os.path.join(folder, "_REJECTS")).problem(folder) is not None
    elsewhere = os.path.abspath(str(tmp_path.parent / "elsewhere"))  # absolute on any system
    assert ExportSettings(folder=elsewhere).dest_dir(folder) == elsewhere
