import os

from cullspeed.constants import KEEP, KEEP_DIR, REJECT, REJECT_DIR
from cullspeed.fileops import execute_moves, plan_moves
from cullspeed.models import Photo


def touch(path, content="x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as file:
        file.write(content)
    return str(path)


def read(path):
    with open(path) as file:
        return file.read()


def listing(folder, sub=""):
    path = os.path.join(folder, sub)
    return sorted(os.listdir(path)) if os.path.isdir(path) else []


def process(folder, photos, progress=None):
    return execute_moves(plan_moves(folder, photos), progress)


def test_plan_moves_only_misplaced_photos(tmp_path):
    folder = str(tmp_path)
    photos = [
        Photo(f"{folder}/a.jpg", KEEP),
        Photo(f"{folder}/b.jpg"),
        Photo(f"{folder}/{KEEP_DIR}/c.jpg", KEEP, KEEP),
        Photo(f"{folder}/{KEEP_DIR}/d.jpg", None, KEEP),
        Photo(f"{folder}/{REJECT_DIR}/e.jpg", KEEP, REJECT),
    ]
    moves = {os.path.basename(m.src): m.dest_dir for m in plan_moves(folder, photos)}
    assert moves == {
        "a.jpg": os.path.join(folder, KEEP_DIR),
        "d.jpg": folder,
        "e.jpg": os.path.join(folder, KEEP_DIR),
    }


def test_moves_into_status_folders(tmp_path):
    folder = str(tmp_path)
    keep = touch(tmp_path / "keep.jpg")
    reject = touch(tmp_path / "reject.jpg")
    back = touch(tmp_path / KEEP_DIR / "back.jpg")

    result = process(folder, [Photo(keep, KEEP), Photo(reject, REJECT), Photo(back, None, KEEP)])

    assert not result.errors
    assert listing(folder, KEEP_DIR) == ["keep.jpg"]
    assert listing(folder, REJECT_DIR) == ["reject.jpg"]
    assert "back.jpg" in listing(folder)
    assert result.moved[keep] == os.path.join(folder, KEEP_DIR, "keep.jpg")


def test_never_overwrites_existing_file(tmp_path):
    folder = str(tmp_path)
    existing = touch(tmp_path / KEEP_DIR / "IMG.jpg", "old")
    photo = touch(tmp_path / "IMG.jpg", "new")

    result = process(folder, [Photo(photo, KEEP), Photo(existing, KEEP, KEEP)])

    assert listing(folder, KEEP_DIR) == ["IMG (1).jpg", "IMG.jpg"]
    assert read(existing) == "old"
    assert result.moved[photo].endswith("IMG (1).jpg")


def test_same_name_from_two_folders_gets_distinct_names(tmp_path):
    folder = str(tmp_path)
    a = touch(tmp_path / "IMG.jpg", "a")
    b = touch(tmp_path / REJECT_DIR / "IMG.jpg", "b")

    process(folder, [Photo(a, KEEP), Photo(b, KEEP, REJECT)])

    names = listing(folder, KEEP_DIR)
    assert names == ["IMG (1).jpg", "IMG.jpg"]
    contents = {read(os.path.join(folder, KEEP_DIR, n)) for n in names}
    assert contents == {"a", "b"}


def test_sidecars_follow_their_photo(tmp_path):
    folder = str(tmp_path)
    raw = touch(tmp_path / "IMG_1.CR2")
    touch(tmp_path / "IMG_1.CR2.xmp")  # darktable style
    touch(tmp_path / "IMG_1.xmp")  # Lightroom style, RAWs only
    jpg = touch(tmp_path / "IMG_2.jpg")
    touch(tmp_path / "IMG_2.xmp")  # not a sidecar of a JPEG

    process(folder, [Photo(raw, KEEP), Photo(jpg, KEEP)])

    assert listing(folder, KEEP_DIR) == ["IMG_1.CR2", "IMG_1.CR2.xmp", "IMG_1.xmp", "IMG_2.jpg"]
    assert listing(folder) == ["IMG_2.xmp", KEEP_DIR]


def test_renamed_photo_takes_its_sidecars_along(tmp_path):
    folder = str(tmp_path)
    touch(tmp_path / KEEP_DIR / "IMG.CR2")
    raw = touch(tmp_path / "IMG.CR2")
    touch(tmp_path / "IMG.xmp")
    touch(tmp_path / "IMG.CR2.xmp")

    process(folder, [Photo(raw, KEEP)])

    assert listing(folder, KEEP_DIR) == ["IMG (1).CR2", "IMG (1).CR2.xmp", "IMG (1).xmp", "IMG.CR2"]


def test_photo_is_renamed_when_only_its_sidecar_name_is_taken(tmp_path):
    folder = str(tmp_path)
    touch(tmp_path / KEEP_DIR / "IMG.xmp", "someone else's")
    raw = touch(tmp_path / "IMG.CR2")
    touch(tmp_path / "IMG.xmp", "mine")

    result = process(folder, [Photo(raw, KEEP)])

    # IMG.CR2 alone would be free, but then its sidecar would not match it.
    assert result.moved[raw].endswith("IMG (1).CR2")
    assert read(os.path.join(folder, KEEP_DIR, "IMG (1).xmp")) == "mine"
    assert read(os.path.join(folder, KEEP_DIR, "IMG.xmp")) == "someone else's"


def test_stop_from_progress_callback(tmp_path):
    folder = str(tmp_path)
    photos = [Photo(touch(tmp_path / f"{i}.jpg"), KEEP) for i in range(5)]

    result = process(folder, photos, progress=lambda done: done < 2)

    assert result.cancelled
    assert len(result.moved) == 2
    assert len(listing(folder, KEEP_DIR)) == 2


def test_errors_are_collected_and_other_moves_continue(tmp_path):
    folder = str(tmp_path)
    missing = str(tmp_path / "gone.jpg")
    ok = touch(tmp_path / "ok.jpg")

    result = process(folder, [Photo(missing, KEEP), Photo(ok, KEEP)])

    assert len(result.errors) == 1 and result.errors[0].startswith("gone.jpg")
    assert list(result.moved) == [ok]


def test_raw_jpeg_pair_moves_and_renames_together(tmp_path):
    folder = str(tmp_path)
    touch(tmp_path / KEEP_DIR / "IMG.JPG")  # only the JPEG name is taken
    raw = touch(tmp_path / "IMG.CR2")
    jpg = touch(tmp_path / "IMG.JPG")
    touch(tmp_path / "IMG.xmp")

    result = process(folder, [Photo(raw, KEEP, companions=[jpg])])

    assert listing(folder, KEEP_DIR) == ["IMG (1).CR2", "IMG (1).JPG", "IMG (1).xmp", "IMG.JPG"]
    assert result.moved == {
        raw: os.path.join(folder, KEEP_DIR, "IMG (1).CR2"),
        jpg: os.path.join(folder, KEEP_DIR, "IMG (1).JPG"),
    }
