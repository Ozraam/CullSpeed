import json
import os

import pytest

from cullspeed.constants import KEEP, KEEP_DIR, REJECT, REJECT_DIR, SESSION_FILENAME
from cullspeed.models import Photo, PhotoModel, load_session, save_session, scan_folder


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
    assert load_session(str(tmp_path)) == {"a.jpg": {"status": KEEP, "rating": 0, "label": None}}


def test_sidecar_rating_wins_over_session(tmp_path):
    touch(tmp_path / "IMG.CR2")
    (tmp_path / "IMG.xmp").write_text('<rdf:Description xmp:Rating="5" xmp:Label="Blue"/>')
    write_session(tmp_path, {"IMG.CR2": {"status": KEEP, "rating": 2, "label": None}})

    [photo] = scan_folder(str(tmp_path))

    assert (photo.status, photo.rating, photo.label) == (KEEP, 5, "Blue")
