import json

import pytest

from tamis.presets import PresetStore


def test_a_preset_survives_a_new_store_and_never_holds_framing(tmp_path):
    path = tmp_path / "data" / "presets.json"
    store = PresetStore(str(path))
    assert store.names() == [] and store.problem is None
    store.save("Warm", {"temperature": 30, "crop": [0, 0, 0.5, 0.5], "rotate": 90, "straighten": 2.0})

    again = PresetStore(str(path))
    assert "Warm" in again
    assert again.get("Warm") == {"temperature": 30}


def test_names_are_sorted_and_saving_replaces(tmp_path):
    store = PresetStore(str(tmp_path / "presets.json"))
    store.save("b", {"contrast": 1})
    store.save("A", {"contrast": 2})
    store.save("b", {"contrast": 3})
    assert store.names() == ["A", "b"]
    assert store.get("b") == {"contrast": 3}


def test_get_gives_a_copy(tmp_path):
    store = PresetStore(str(tmp_path / "presets.json"))
    store.save("A", {"contrast": 2})
    store.get("A")["contrast"] = 9
    assert store.get("A") == {"contrast": 2}


def test_delete(tmp_path):
    path = tmp_path / "presets.json"
    store = PresetStore(str(path))
    store.save("A", {"contrast": 2})
    store.delete("A")
    assert store.names() == [] and PresetStore(str(path)).names() == []


def test_loading_cleans_what_it_reads(tmp_path):
    path = tmp_path / "presets.json"
    path.write_text(json.dumps({
        "Good": {"contrast": 500, "nonsense": 1, "crop": [0, 0, 0.5, 0.5]},
        "Only framing": {"rotate": 90},
        "Not an edit": 3,
    }))
    store = PresetStore(str(path))
    assert store.names() == ["Good"]
    assert store.get("Good") == {"contrast": 100}


def test_a_damaged_file_is_set_aside_not_overwritten(tmp_path):
    path = tmp_path / "presets.json"
    path.write_text("{ not json")
    store = PresetStore(str(path))
    assert store.names() == [] and "corrupt" in store.problem
    assert (tmp_path / "presets.corrupt.json").read_text() == "{ not json"
    store.save("A", {"contrast": 2})
    assert (tmp_path / "presets.corrupt.json").read_text() == "{ not json"


def test_a_failed_write_changes_nothing(tmp_path, monkeypatch):
    store = PresetStore(str(tmp_path / "presets.json"))
    store.save("A", {"contrast": 2})
    monkeypatch.setattr("json.dump", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        store.save("B", {"contrast": 3})
    assert store.names() == ["A"]
    assert PresetStore(str(tmp_path / "presets.json")).names() == ["A"]
