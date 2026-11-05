import json
import os

import pytest

from tamis.constants import KEEP, KEEP_DIR, LEGACY_SESSION_FILENAME, REJECT, REJECT_DIR, SESSION_FILENAME
from tamis.models import Photo, PhotoModel, load_session, save_session, scan_folder


def touch(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w").close()
    return str(path)


def statuses(session):
    return {key: entry["status"] for key, entry in session.items()}


def write_session(folder, data):
    with open(os.path.join(folder, SESSION_FILENAME), "w") as file:
        json.dump(data, file)


# -- scan_folder -----------------------------------------------------------


def test_scan_takes_marks_and_locations_from_folders(tmp_path):
    touch(tmp_path / "a.jpg")
    touch(tmp_path / KEEP_DIR / "b.CR2")
    touch(tmp_path / REJECT_DIR / "c.png")

    photos = {p.name: (p.status, p.location) for p in scan_folder(str(tmp_path))}

    assert photos == {"a.jpg": (None, None), "b.CR2": (KEEP, KEEP), "c.png": (REJECT, REJECT)}


def test_scan_ignores_hidden_and_unsupported_files(tmp_path):
    touch(tmp_path / "a.jpg")
    touch(tmp_path / ".hidden.jpg")
    touch(tmp_path / "notes.txt")
    touch(tmp_path / "a.jpg.xmp")
    os.makedirs(tmp_path / "sub.jpg")

    assert [p.name for p in scan_folder(str(tmp_path))] == ["a.jpg"]


def test_session_marks_win_over_folder_location(tmp_path):
    touch(tmp_path / "a.jpg")
    touch(tmp_path / KEEP_DIR / "b.jpg")
    write_session(tmp_path, {"a.jpg": REJECT, f"{KEEP_DIR}/b.jpg": None})

    photos = {p.name: (p.status, p.location) for p in scan_folder(str(tmp_path))}

    assert photos == {"a.jpg": (REJECT, None), "b.jpg": (None, KEEP)}


def test_legacy_filename_keys_apply_only_to_unique_names(tmp_path):
    # Old sessions were keyed by bare filename, even for photos in _KEEPS / _REJECTS.
    touch(tmp_path / KEEP_DIR / "unique.jpg")
    touch(tmp_path / KEEP_DIR / "twice.jpg")
    touch(tmp_path / REJECT_DIR / "twice.jpg")
    write_session(tmp_path, {"unique.jpg": REJECT, "twice.jpg": KEEP})

    photos = {os.path.relpath(p.path, tmp_path): p.status for p in scan_folder(str(tmp_path))}

    assert photos[os.path.join(KEEP_DIR, "unique.jpg")] == REJECT
    # Ambiguous name: each copy keeps the mark given by its folder.
    assert photos[os.path.join(KEEP_DIR, "twice.jpg")] == KEEP
    assert photos[os.path.join(REJECT_DIR, "twice.jpg")] == REJECT


# -- session file ----------------------------------------------------------


def test_session_round_trip_uses_relative_keys(tmp_path):
    folder = str(tmp_path)
    photos = [Photo(f"{folder}/a.jpg", KEEP), Photo(f"{folder}/{KEEP_DIR}/b.jpg", None, KEEP)]

    save_session(folder, photos)

    assert statuses(load_session(folder)) == {"a.jpg": KEEP, f"{KEEP_DIR}/b.jpg": None}
    assert not os.path.exists(os.path.join(folder, SESSION_FILENAME + ".tmp"))


@pytest.mark.parametrize("content", ["{not json", "[1, 2]", ""])
def test_unreadable_session_is_empty(tmp_path, content):
    (tmp_path / SESSION_FILENAME).write_text(content)
    assert load_session(str(tmp_path)) == {}


def test_session_drops_unknown_statuses(tmp_path):
    write_session(tmp_path, {"a.jpg": KEEP, "b.jpg": "maybe", "c.jpg": 3})
    assert statuses(load_session(str(tmp_path))) == {"a.jpg": KEEP}


def read_only(path):
    os.chmod(path, 0o555)
    return path


@pytest.fixture
def locked_folder(tmp_path):
    folder = tmp_path / "card"
    touch(folder / "a.jpg")
    read_only(folder)
    yield str(folder)
    os.chmod(folder, 0o755)


def test_read_only_folder_saves_to_fallback(locked_folder, tmp_path):
    fallback = str(tmp_path / "fallback")
    photos = [Photo(f"{locked_folder}/a.jpg", KEEP)]

    path = save_session(locked_folder, photos, fallback)

    assert os.path.dirname(path) == fallback
    assert not os.path.exists(os.path.join(locked_folder, SESSION_FILENAME))
    assert statuses(load_session(locked_folder, fallback)) == {"a.jpg": KEEP}
    assert [p.status for p in scan_folder(locked_folder, fallback)] == [KEEP]


def test_without_fallback_a_read_only_folder_raises(locked_folder):
    with pytest.raises(OSError):
        save_session(locked_folder, [Photo(f"{locked_folder}/a.jpg", KEEP)])


def test_session_from_before_the_rename_is_loaded(tmp_path):
    touch(tmp_path / "a.jpg")
    (tmp_path / LEGACY_SESSION_FILENAME).write_text(json.dumps({"a.jpg": KEEP}))

    assert [p.status for p in scan_folder(str(tmp_path))] == [KEEP]


def test_newer_session_wins_over_the_one_from_before_the_rename(tmp_path):
    legacy = tmp_path / LEGACY_SESSION_FILENAME
    legacy.write_text(json.dumps({"a.jpg": KEEP}))
    os.utime(legacy, (1000, 1000))
    (tmp_path / SESSION_FILENAME).write_text(json.dumps({"a.jpg": REJECT}))

    assert statuses(load_session(str(tmp_path))) == {"a.jpg": REJECT}


def test_saving_replaces_the_session_from_before_the_rename(tmp_path):
    folder = str(tmp_path)
    legacy = tmp_path / LEGACY_SESSION_FILENAME
    legacy.write_text(json.dumps({"a.jpg": REJECT}))

    save_session(folder, [Photo(f"{folder}/a.jpg", KEEP)])

    assert not legacy.exists()
    assert statuses(load_session(folder)) == {"a.jpg": KEEP}


def test_read_only_folder_with_a_session_from_before_the_rename(tmp_path):
    folder = tmp_path / "card"
    legacy = folder / LEGACY_SESSION_FILENAME
    touch(legacy)
    legacy.write_text(json.dumps({"a.jpg": REJECT}))
    os.utime(legacy, (1000, 1000))
    fallback = str(tmp_path / "fallback")
    read_only(folder)
    try:
        assert statuses(load_session(str(folder), fallback)) == {"a.jpg": REJECT}

        save_session(str(folder), [Photo(f"{folder}/a.jpg", KEEP)], fallback)

        assert legacy.exists()
        assert statuses(load_session(str(folder), fallback)) == {"a.jpg": KEEP}
    finally:
        os.chmod(folder, 0o755)


def test_saving_in_the_folder_removes_the_stale_fallback(tmp_path):
    folder, fallback = str(tmp_path / "f"), str(tmp_path / "fallback")
    touch(tmp_path / "f" / "a.jpg")
    os.chmod(folder, 0o555)
    old = save_session(folder, [Photo(f"{folder}/a.jpg", REJECT)], fallback)
    os.chmod(folder, 0o755)

    path = save_session(folder, [Photo(f"{folder}/a.jpg", KEEP)], fallback)

    assert path == os.path.join(folder, SESSION_FILENAME)
    assert not os.path.exists(old)
    assert statuses(load_session(folder, fallback)) == {"a.jpg": KEEP}


def test_newest_session_wins(tmp_path):
    folder, fallback = str(tmp_path / "f"), str(tmp_path / "fallback")
    touch(tmp_path / "f" / "a.jpg")
    write_session(folder, {"a.jpg": KEEP})
    os.chmod(folder, 0o555)
    newer = save_session(folder, [Photo(f"{folder}/a.jpg", REJECT)], fallback)
    os.chmod(folder, 0o755)
    os.utime(newer, (2_000_000_000, 2_000_000_000))

    assert statuses(load_session(folder, fallback)) == {"a.jpg": REJECT}


# -- PhotoModel counters -----------------------------------------------------


class _Signal:
    def connect(self, _slot):
        pass


class _Loader:
    thumbnail_loaded = _Signal()
    thumbnail_failed = _Signal()

    def thumbnail(self, _path):
        return None

    def thumbnail_error(self, _path):
        return False


@pytest.fixture
def model():
    model = PhotoModel(_Loader())
    model.set_photos([Photo(f"/f/{i}.jpg") for i in range(4)] + [Photo(f"/f/{KEEP_DIR}/k.jpg", KEEP, KEEP)])
    return model


def test_counts_follow_marks_and_undo(model):
    assert model.counts() == (1, 0, 4)
    assert not model.has_unsorted()

    changes = model.set_status([0, 1], REJECT)
    assert model.counts() == (1, 2, 2)
    assert model.has_unsorted()

    model.restore(changes)
    assert model.counts() == (1, 0, 4)
    assert not model.has_unsorted()


def test_unmarking_a_kept_photo_makes_it_unsorted(model):
    model.set_status([4], None)
    assert model.counts() == (0, 0, 5)
    assert model.has_unsorted()


def test_apply_moves_updates_paths_and_locations(model):
    model.set_status([0], KEEP)
    model.apply_moves({"/f/0.jpg": (f"/f/{KEEP_DIR}/0.jpg", KEEP)})

    assert model.photo(0).path == f"/f/{KEEP_DIR}/0.jpg"
    assert model.row_of(f"/f/{KEEP_DIR}/0.jpg") == 0
    assert model.row_of("/f/0.jpg") == -1
    assert not model.has_unsorted()


def test_model_ratings_and_labels_undo(model):
    seen = []
    model.ratings_changed.connect(seen.append)

    changes = model.set_field([0, 1], "rating", 4) + model.set_field([1], "label", "Red")
    assert (model.photo(1).rating, model.photo(1).label) == (4, "Red")
    assert seen == [["/f/0.jpg", "/f/1.jpg"], ["/f/1.jpg"]]

    model.restore(changes)
    assert [(p.rating, p.label) for p in model.photos[:2]] == [(0, None), (0, None)]
    assert model.counts() == (1, 0, 4)  # ratings do not touch the keep/reject counters


# -- RAW+JPEG pairs, ratings in sessions and sidecars ------------------------


def test_raw_and_jpeg_of_the_same_shot_are_one_photo(tmp_path):
    touch(tmp_path / "IMG_1.CR2")
    touch(tmp_path / "IMG_1.JPG")
    touch(tmp_path / "IMG_2.JPG")
    touch(tmp_path / "IMG_3.CR2")
    touch(tmp_path / "IMG_3.NEF")  # two RAWs: ambiguous, kept apart
    touch(tmp_path / "IMG_3.jpg")

    photos = {p.display_name: [os.path.basename(c) for c in p.companions] for p in scan_folder(str(tmp_path))}

    assert photos == {
        "IMG_1.CR2 + JPG": ["IMG_1.JPG"],
        "IMG_2.JPG": [],
        "IMG_3.CR2": [],
        "IMG_3.NEF": [],
        "IMG_3.jpg": [],
    }


def test_pairs_only_form_within_one_folder(tmp_path):
    touch(tmp_path / "IMG_1.CR2")
    touch(tmp_path / KEEP_DIR / "IMG_1.JPG")

    assert sorted(p.display_name for p in scan_folder(str(tmp_path))) == ["IMG_1.CR2", "IMG_1.JPG"]


def test_session_stores_ratings_and_labels(tmp_path):
    folder = str(tmp_path)
    touch(tmp_path / "a.jpg")
    touch(tmp_path / "b.jpg")
    save_session(folder, [Photo(f"{folder}/a.jpg", KEEP, rating=3, label="Green"), Photo(f"{folder}/b.jpg", REJECT)])

    data = json.loads((tmp_path / SESSION_FILENAME).read_text())
    assert data == {"a.jpg": {"status": KEEP, "rating": 3, "label": "Green"}, "b.jpg": REJECT}
    photos = {p.name: (p.status, p.rating, p.label) for p in scan_folder(folder)}
    assert photos == {"a.jpg": (KEEP, 3, "Green"), "b.jpg": (REJECT, 0, None)}


def test_invalid_ratings_and_labels_are_dropped(tmp_path):
    write_session(tmp_path, {"a.jpg": {"status": KEEP, "rating": 9, "label": "Mauve"}, "b.jpg": {"status": "x"}})
    assert load_session(str(tmp_path)) == {"a.jpg": {"status": KEEP, "rating": 0, "label": None, "edit": {}}}


def test_session_stores_edits(tmp_path):
    folder = str(tmp_path)
    touch(tmp_path / "a.jpg")
    edit = {"exposure": 0.5, "lut": "Fuji/Velvia 50.png"}
    save_session(folder, [Photo(f"{folder}/a.jpg", edit=edit)])

    assert json.loads((tmp_path / SESSION_FILENAME).read_text())["a.jpg"]["edit"] == edit
    [photo] = scan_folder(folder)
    assert photo.edit == edit


def test_edits_are_cleaned_when_loaded(tmp_path):
    write_session(tmp_path, {"a.jpg": {"status": None, "edit": {"exposure": 99, "contrast": 0, "bogus": 1}}})
    assert load_session(str(tmp_path))["a.jpg"]["edit"] == {"exposure": 5.0}


def test_edit_changes_undo_and_do_not_touch_xmp(model):
    seen = []
    model.ratings_changed.connect(seen.append)
    changes = model.set_field([2], "edit", {"exposure": 1.0})
    assert model.photo(2).edit == {"exposure": 1.0} and seen == []
    model.restore(changes)
    assert model.photo(2).edit == {}


def test_sidecar_rating_wins_over_session(tmp_path):
    touch(tmp_path / "IMG.CR2")
    (tmp_path / "IMG.xmp").write_text('<rdf:Description xmp:Rating="5" xmp:Label="Blue"/>')
    write_session(tmp_path, {"IMG.CR2": {"status": KEEP, "rating": 2, "label": None}})

    [photo] = scan_folder(str(tmp_path))

    assert (photo.status, photo.rating, photo.label) == (KEEP, 5, "Blue")


# -- photos that are away, damaged sessions, ratings not yet in the sidecar -------

def test_session_keeps_the_marks_of_photos_that_are_away(tmp_path):
    folder = str(tmp_path)
    touch(tmp_path / KEEP_DIR / "a.jpg")
    touch(tmp_path / "b.jpg")
    photos = scan_folder(folder)
    for photo in photos:
        photo.edit = {"exposure": 1.0}
    save_session(folder, photos)

    os.rename(tmp_path / KEEP_DIR, tmp_path / "away")
    save_session(folder, scan_folder(folder))
    os.rename(tmp_path / "away", tmp_path / KEEP_DIR)

    assert [photo.edit for photo in scan_folder(folder)] == [{"exposure": 1.0}, {"exposure": 1.0}]


def test_session_forgets_moved_photos_and_entries_with_nothing_in_them(tmp_path):
    folder = str(tmp_path)
    write_session(tmp_path, {
        "moved.jpg": KEEP, "gone.jpg": REJECT, "unmarked.jpg": None, "invalid.jpg": "maybe",
    })
    save_session(folder, [], forget={"moved.jpg"})
    assert set(json.loads((tmp_path / SESSION_FILENAME).read_text())) == {"gone.jpg"}


def test_session_drops_a_filename_key_once_a_photo_has_taken_it_over(tmp_path):
    folder = str(tmp_path)
    touch(tmp_path / KEEP_DIR / "a.jpg")
    write_session(tmp_path, {"a.jpg": REJECT})
    photos = scan_folder(folder)
    assert photos[0].status == REJECT

    save_session(folder, photos)

    assert set(json.loads((tmp_path / SESSION_FILENAME).read_text())) == {f"{KEEP_DIR}/a.jpg"}


def test_session_drops_the_entry_of_a_file_that_is_no_longer_a_photo_of_its_own(tmp_path):
    folder = str(tmp_path)
    touch(tmp_path / "IMG.jpg")
    touch(tmp_path / "IMG.CR2")
    write_session(tmp_path, {"IMG.jpg": KEEP})
    save_session(folder, [Photo(f"{folder}/IMG.CR2", companions=[f"{folder}/IMG.jpg"])])
    assert set(json.loads((tmp_path / SESSION_FILENAME).read_text())) == {"IMG.CR2"}


@pytest.mark.parametrize("content", ["{not json", "[1, 2]", ""])
def test_damaged_session_is_set_aside_and_reported(tmp_path, content):
    folder = str(tmp_path)
    (tmp_path / SESSION_FILENAME).write_text(content)
    problems = []

    assert load_session(folder, problems=problems) == {}

    kept = tmp_path / "tamis_session.corrupt.json"
    assert problems == [str(kept)]
    assert kept.read_text() == content
    save_session(folder, [Photo(f"{folder}/a.jpg", KEEP)])
    assert kept.read_text() == content


def test_damaged_session_does_not_replace_an_earlier_one_set_aside(tmp_path):
    (tmp_path / "tamis_session.corrupt.json").write_text("first")
    (tmp_path / SESSION_FILENAME).write_text("second")
    problems = []
    load_session(str(tmp_path), problems=problems)
    assert problems == [str(tmp_path / "tamis_session.corrupt (1).json")]
    assert (tmp_path / "tamis_session.corrupt.json").read_text() == "first"


def test_damaged_session_falls_back_on_the_next_newest(tmp_path):
    folder = str(tmp_path)
    fallback = str(tmp_path / "fallback")
    (tmp_path / LEGACY_SESSION_FILENAME).write_text(json.dumps({"a.jpg": KEEP}))
    os.utime(tmp_path / LEGACY_SESSION_FILENAME, (1000, 1000))
    (tmp_path / SESSION_FILENAME).write_text("{not json")
    problems = []

    assert statuses(load_session(folder, fallback, problems)) == {"a.jpg": KEEP}
    assert problems == [str(tmp_path / "tamis_session.corrupt.json")]


def test_damaged_session_in_a_read_only_folder_stays_put_and_is_reported(tmp_path):
    folder = tmp_path / "card"
    folder.mkdir()
    (folder / SESSION_FILENAME).write_text("{not json")
    read_only(folder)
    try:
        problems = []
        assert load_session(str(folder), problems=problems) == {}
        assert problems == [str(folder / SESSION_FILENAME)]
    finally:
        os.chmod(folder, 0o755)


def test_rating_not_yet_in_the_sidecar_wins_over_it(tmp_path):
    folder = str(tmp_path)
    touch(tmp_path / "a.jpg")
    (tmp_path / "a.jpg.xmp").write_text('<rdf:Description xmp:Rating="2"/>')
    [photo] = scan_folder(folder)
    assert photo.rating == 2
    photo.rating, photo.label, photo.xmp_pending = 5, "Red", True
    save_session(folder, [photo])

    [again] = scan_folder(folder)
    assert (again.rating, again.label, again.xmp_pending) == (5, "Red", True)

    again.xmp_pending = False  # written at last: other editors win again
    save_session(folder, [again])
    [last] = scan_folder(folder)
    assert (last.rating, last.xmp_pending) == (2, False)


def test_removing_a_rating_is_remembered_until_the_sidecar_has_it(tmp_path):
    folder = str(tmp_path)
    touch(tmp_path / "a.jpg")
    (tmp_path / "a.jpg.xmp").write_text('<rdf:Description xmp:Rating="2"/>')
    [photo] = scan_folder(folder)
    photo.rating, photo.xmp_pending = 0, True
    save_session(folder, [photo])
    assert scan_folder(folder)[0].rating == 0


def test_exports_are_saved_and_survive_a_reload(tmp_path):
    path = tmp_path / "a.jpg"
    path.write_text("x")
    record = {"name": "a.jpg", "size": 1, "mtime": 2}
    save_session(str(tmp_path), [Photo(str(path), exports=[record])])
    assert load_session(str(tmp_path))["a.jpg"]["exports"] == [record]
    assert scan_folder(str(tmp_path))[0].exports == [record]


def test_session_exports_only_name_files_directly_in_exports(tmp_path):
    (tmp_path / SESSION_FILENAME).write_text(json.dumps({
        "a.jpg": {"status": "reject", "exports": [
            {"name": "../IMG.CR2", "size": 1, "mtime": 2},
            {"name": "..", "size": 1, "mtime": 2},
            {"name": "b.jpg", "size": "1", "mtime": 2},
            "c.jpg",
            {"name": "ok.jpg", "size": 1, "mtime": 2},
        ]},
    }))
    assert [record["name"] for record in load_session(str(tmp_path))["a.jpg"]["exports"]] == ["ok.jpg"]
