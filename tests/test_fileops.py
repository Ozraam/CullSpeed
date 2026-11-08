import os
import shutil

import pytest

from tamis import fileops
from tamis.constants import KEEP, KEEP_DIR, REJECT, REJECT_DIR
from tamis.fileops import (
    copy_export,
    execute_moves,
    export_record,
    plan_moves,
    rejected_exports,
    remove_rejected_exports,
    without_export,
)
from tamis.models import Photo


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


def test_plan_exports_renders_edited_and_copies_jpegs(tmp_path):
    from tamis.fileops import plan_exports

    folder = str(tmp_path)
    photos = [
        Photo(f"{folder}/a.CR2", KEEP, companions=[f"{folder}/a.JPG"], edit={"exposure": 1.0}),
        Photo(f"{folder}/b.CR2", KEEP, companions=[f"{folder}/b.JPG"]),
        Photo(f"{folder}/c.jpg"),
        Photo(f"{folder}/d.CR2"),  # no JPEG, no edit: nothing to export
        Photo(f"{folder}/e.jpg", REJECT, edit={"exposure": 1.0}),  # rejected
        Photo(f"{folder}/{KEEP_DIR}/c.png", KEEP, KEEP, edit={"contrast": 10}),  # same stem as c.jpg
    ]
    jobs = [(os.path.basename(j.path), bool(j.edit), j.name) for j in plan_exports(photos)]
    assert jobs == [
        ("a.CR2", True, "a.jpg"),
        ("b.JPG", False, "b.JPG"),
        ("c.jpg", False, "c.jpg"),
        ("c.png", True, "c (1).jpg"),
    ]


def test_copy_export_replaces_earlier_export(tmp_path):
    from tamis.fileops import copy_export

    source = touch(tmp_path / "a.jpg", "new")
    touch(tmp_path / "_EXPORTS" / "a.jpg", "old")
    dest = copy_export(source, str(tmp_path / "_EXPORTS"), "a.jpg")
    assert read(dest) == "new"
    assert listing(str(tmp_path), "_EXPORTS") == ["a.jpg"]


def test_copy_export_without_a_name_picks_a_free_one(tmp_path):
    from tamis.fileops import copy_export
    source = touch(tmp_path / "a.jpg", "new")
    touch(tmp_path / "_EXPORTS" / "a.jpg", "earlier")

    dest = copy_export(source, str(tmp_path / "_EXPORTS"))

    assert os.path.basename(dest) == "a (1).jpg"
    assert read(dest) == "new"
    assert read(tmp_path / "_EXPORTS" / "a.jpg") == "earlier"


def failing_move(monkeypatch, *failing):
    """shutil.move that fails for the sources in `failing`, as with a file locked by another app."""
    real_move = shutil.move

    def move(source, dest):
        if os.path.basename(source) in failing:
            raise PermissionError(13, "Permission denied", source)
        return real_move(source, dest)

    monkeypatch.setattr("tamis.fileops.shutil.move", move)


def test_failed_move_keeps_the_pair_together(tmp_path, monkeypatch):
    folder = str(tmp_path)
    raw = touch(tmp_path / "IMG.CR2")
    jpg = touch(tmp_path / "IMG.JPG")
    touch(tmp_path / "IMG.xmp")
    other = touch(tmp_path / "other.jpg")
    failing_move(monkeypatch, "IMG.JPG")

    result = process(folder, [Photo(raw, KEEP, companions=[jpg]), Photo(other, KEEP)])

    assert len(result.errors) == 1 and result.errors[0].startswith("IMG.CR2: Permission denied")
    assert list(result.moved) == [other]
    assert sorted(listing(folder)) == sorted(["IMG.CR2", "IMG.JPG", "IMG.xmp", KEEP_DIR])
    assert listing(folder, KEEP_DIR) == ["other.jpg"]


def test_failed_move_frees_the_names_it_had_taken(tmp_path, monkeypatch):
    folder = str(tmp_path)
    raw = touch(tmp_path / "IMG.CR2")
    jpg = touch(tmp_path / "IMG.JPG")
    same_name = touch(tmp_path / REJECT_DIR / "IMG.CR2")
    failing_move(monkeypatch, "IMG.JPG")

    result = process(folder, [Photo(raw, KEEP, companions=[jpg]), Photo(same_name, KEEP, REJECT)])

    assert result.moved[same_name] == os.path.join(folder, KEEP_DIR, "IMG.CR2")


def test_file_that_cannot_go_back_is_reported_and_followed(tmp_path, monkeypatch):
    folder = str(tmp_path)
    raw = touch(tmp_path / "IMG.CR2")
    jpg = touch(tmp_path / "IMG.JPG")
    real_move = shutil.move
    moved_out = os.path.join(folder, KEEP_DIR, "IMG.CR2")

    def move(source, dest):
        if os.path.basename(source) == "IMG.JPG" or source == moved_out:
            raise PermissionError(13, "Permission denied", source)
        return real_move(source, dest)

    monkeypatch.setattr("tamis.fileops.shutil.move", move)

    result = process(folder, [Photo(raw, KEEP, companions=[jpg])])

    assert "could not be moved back: IMG.CR2" in result.errors[0]
    assert result.moved == {raw: moved_out}
    assert os.path.exists(moved_out)


def test_without_export_lists_what_plan_exports_leaves_out(tmp_path):
    folder = str(tmp_path)
    left_out = Photo(f"{folder}/a.CR2", KEEP)
    png = Photo(f"{folder}/b.png")
    photos = [
        left_out,
        png,
        Photo(f"{folder}/c.CR2", KEEP, companions=[f"{folder}/c.JPG"]),
        Photo(f"{folder}/d.CR2", KEEP, edit={"exposure": 1.0}),
        Photo(f"{folder}/e.CR2", REJECT),
    ]
    assert without_export(photos) == [left_out, png]


def test_failed_copy_leaves_the_earlier_export_alone(tmp_path, monkeypatch):
    source = touch(tmp_path / "a.jpg", "new")
    earlier = touch(tmp_path / "_EXPORTS" / "a.jpg", "earlier")

    def truncated_copy(_source, dest):
        with open(dest, "w") as file:
            file.write("ne")
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("tamis.fileops.shutil.copy2", truncated_copy)

    with pytest.raises(OSError):
        copy_export(source, str(tmp_path / "_EXPORTS"), "a.jpg")

    assert read(earlier) == "earlier"
    assert os.listdir(tmp_path / "_EXPORTS") == ["a.jpg"]


def exported_photo(tmp_path, name="a.jpg", content="export", status=REJECT):
    path = touch(tmp_path / "_EXPORTS" / name, content)
    return Photo(str(tmp_path / name), status, exports=[export_record(path)]), path


def test_exports_of_rejected_photos_are_removed(tmp_path):
    rejected, path = exported_photo(tmp_path)
    kept, kept_path = exported_photo(tmp_path, "b.jpg", status=KEEP)
    users_own = touch(tmp_path / "_EXPORTS" / "c.jpg")
    photos = [rejected, kept, Photo(str(tmp_path / "c.jpg"), REJECT)]  # c.jpg never was an export of that photo

    removed, errors = remove_rejected_exports(photos, str(tmp_path))

    assert (removed, errors) == ([path], [])
    assert not os.path.exists(path)
    assert os.path.exists(kept_path) and os.path.exists(users_own)
    assert rejected.exports == [] and kept.exports


def test_export_changed_since_is_not_removed(tmp_path):
    photo, path = exported_photo(tmp_path)
    touch(path, "retouched by hand")

    dest_dir = str(tmp_path)  # the shoot folder: records are of _EXPORTS by default
    assert rejected_exports([photo], dest_dir) == ([], [path])
    assert remove_rejected_exports([photo], dest_dir) == ([], [])
    assert read(path) == "retouched by hand"
    assert photo.exports == []  # no longer ours: not reported again


def test_export_that_cannot_be_removed_is_remembered(tmp_path, monkeypatch):
    photo, path = exported_photo(tmp_path)

    def locked(_path):
        raise PermissionError(13, "Permission denied", _path)

    monkeypatch.setattr("tamis.fileops.os.remove", locked)
    removed, errors = remove_rejected_exports([photo], str(tmp_path))

    assert removed == [] and errors == ["a.jpg: Permission denied"]
    assert photo.exports


def test_export_already_gone_is_ignored(tmp_path):
    photo, path = exported_photo(tmp_path)
    os.remove(path)
    assert rejected_exports([photo], str(tmp_path)) == ([], [])


# -- the Trash ---------------------------------------------------------------

def _trash_into(monkeypatch, tmp_path, fail_on=None):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    def to_trash(path):
        if os.path.basename(path) == fail_on:
            raise OSError(5, "refused")
        location = str(bin_dir / os.path.basename(path))
        os.replace(path, location)
        return location

    monkeypatch.setattr(fileops, "_to_trash", to_trash)
    return bin_dir


def _rejected_pair(tmp_path):
    folder = tmp_path / "_REJECTS"
    folder.mkdir()
    for name in ("IMG.CR2", "IMG.JPG", "IMG.xmp"):
        (folder / name).write_bytes(b"12345")
    return Photo(str(folder / "IMG.CR2"), REJECT, REJECT, companions=[str(folder / "IMG.JPG")])


def test_the_real_trash_reports_what_it_cannot_do(tmp_path):
    with pytest.raises(OSError):
        fileops._to_trash(str(tmp_path / "missing.jpg"))


def test_plan_trash_only_takes_rejects_in_rejects():
    photos = [Photo("a", REJECT, REJECT), Photo("b", KEEP, REJECT), Photo("c", REJECT, None), Photo("d", None, None)]
    assert [p.path for p in fileops.plan_trash(photos)] == ["a"]


def test_trash_takes_companions_and_sidecars(tmp_path, monkeypatch):
    bin_dir = _trash_into(monkeypatch, tmp_path)
    photo = _rejected_pair(tmp_path)
    assert fileops.trash_size([photo]) == 15
    result = fileops.execute_trash([photo])
    assert result.trashed == [photo.path] and not result.errors
    assert sorted(os.listdir(bin_dir)) == ["IMG.CR2", "IMG.JPG", "IMG.xmp"]


def test_trash_is_all_or_nothing_for_a_photo(tmp_path, monkeypatch):
    bin_dir = _trash_into(monkeypatch, tmp_path, fail_on="IMG.JPG")
    photo = _rejected_pair(tmp_path)
    result = fileops.execute_trash([photo])
    assert result.trashed == [] and "IMG.CR2" in result.errors[0]
    assert sorted(os.listdir(tmp_path / "_REJECTS")) == ["IMG.CR2", "IMG.JPG", "IMG.xmp"]
    assert os.listdir(bin_dir) == []


def test_trash_can_be_stopped(tmp_path, monkeypatch):
    _trash_into(monkeypatch, tmp_path)
    photo = _rejected_pair(tmp_path)
    result = fileops.execute_trash([photo], lambda done: False)
    assert result.cancelled and not result.trashed
    assert os.path.exists(photo.path)


def test_a_missing_folder_is_reported_not_raised(tmp_path, monkeypatch):
    _trash_into(monkeypatch, tmp_path)
    result = fileops.execute_trash([Photo(str(tmp_path / "gone" / "IMG.jpg"), REJECT, REJECT)])
    assert len(result.errors) == 1


# -- importing ---------------------------------------------------------------

def _card(tmp_path):
    first = tmp_path / "card" / "DCIM" / "100CANON"
    second = tmp_path / "card" / "DCIM" / "101CANON"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    (first / "IMG_1.CR2").write_bytes(b"raw one")
    (first / "IMG_1.JPG").write_bytes(b"jpeg one")
    (first / "IMG_1.xmp").write_bytes(b"sidecar")
    (first / "._IMG_1.JPG").write_bytes(b"apple double")
    (first / "notes.txt").write_bytes(b"not a photo")
    (second / "IMG_1.CR2").write_bytes(b"raw two")  # the camera's counter rolled over
    hidden = tmp_path / "card" / ".Trashes"
    hidden.mkdir()
    (hidden / "old.jpg").write_bytes(b"deleted")
    return tmp_path / "card"


def test_plan_import_finds_photos_in_sub_folders_and_keeps_pairs_together(tmp_path):
    card = _card(tmp_path)
    groups = fileops.plan_import(str(card))
    assert [[os.path.basename(path) for path in group] for group in groups] == [["IMG_1.CR2", "IMG_1.JPG"], ["IMG_1.CR2"]]
    assert fileops.import_size(groups) == 22


def test_import_renames_whole_groups_never_overwrites_and_leaves_the_card_alone(tmp_path):
    card = _card(tmp_path)
    before = {path: os.path.getsize(path) for group in fileops.plan_import(str(card)) for path in group}
    dest = tmp_path / "shoot"
    result = fileops.execute_import(fileops.plan_import(str(card)), [str(dest)])
    assert not result.errors
    assert sorted(os.listdir(dest)) == ["IMG_1 (1).CR2", "IMG_1.CR2", "IMG_1.JPG", "IMG_1.xmp"]
    assert (dest / "IMG_1.CR2").read_bytes() == b"raw one"
    assert (dest / "IMG_1 (1).CR2").read_bytes() == b"raw two"
    assert {path: os.path.getsize(path) for path in before} == before


def test_importing_again_only_brings_what_is_new(tmp_path):
    card = _card(tmp_path)
    dest = tmp_path / "shoot"
    fileops.execute_import(fileops.plan_import(str(card)), [str(dest)])
    (dest / "IMG_1.xmp").write_bytes(b"edited since")
    (dest / "_KEEPS").mkdir()
    os.replace(dest / "IMG_1.JPG", dest / "_KEEPS" / "IMG_1.JPG")  # sorted since

    result = fileops.execute_import(fileops.plan_import(str(card)), [str(dest)])
    assert result.copied == 0 and result.skipped == 4
    assert (dest / "IMG_1.xmp").read_bytes() == b"edited since"
    assert not (dest / "IMG_1.JPG").exists()


def test_import_writes_every_destination(tmp_path):
    card = _card(tmp_path)
    result = fileops.execute_import(fileops.plan_import(str(card)), [str(tmp_path / "a"), str(tmp_path / "b")])
    assert result.copied == 8
    assert sorted(os.listdir(tmp_path / "a")) == sorted(os.listdir(tmp_path / "b"))


def test_a_copy_that_differs_is_reported_and_removed(tmp_path, monkeypatch):
    card = _card(tmp_path)
    real_digest = fileops._digest
    monkeypatch.setattr(fileops, "_digest", lambda path: "wrong" if ".tmp" in path else real_digest(path))
    dest = tmp_path / "shoot"
    result = fileops.execute_import(fileops.plan_import(str(card)), [str(dest)])
    assert result.errors and result.copied == 0
    assert os.listdir(dest) == []


def test_find_card_looks_for_a_dcim_folder(tmp_path):
    (tmp_path / "Macintosh HD").mkdir()
    (tmp_path / "EOS_DIGITAL" / "DCIM").mkdir(parents=True)
    assert fileops.find_card([str(tmp_path)]) == str(tmp_path / "EOS_DIGITAL")
    assert fileops.find_card([str(tmp_path / "Macintosh HD")]) == ""


def test_plan_exports_keeps_only_leaves_unmarked_photos_out(tmp_path):
    from tamis.fileops import plan_exports

    folder = str(tmp_path)
    photos = [Photo(f"{folder}/a.jpg", KEEP), Photo(f"{folder}/b.jpg"), Photo(f"{folder}/c.png"), Photo(f"{folder}/d.jpg", REJECT)]
    assert [job.name for job in plan_exports(photos)] == ["a.jpg", "b.jpg"]
    assert [job.name for job in plan_exports(photos, keeps_only=True)] == ["a.jpg"]
    assert without_export(photos) == [photos[2]]
    assert without_export(photos, keeps_only=True) == []


def test_plan_exports_names_files_by_the_pattern(tmp_path):
    from tamis.fileops import plan_exports

    folder = str(tmp_path)
    photos = [
        Photo(f"{folder}/a.CR2", KEEP, companions=[f"{folder}/a.JPG"], capture_time=0.0),
        Photo(f"{folder}/b.png", KEEP, edit={"exposure": 1.0}, capture_time=86400.0),
        Photo(f"{folder}/c.jpg", KEEP, capture_time=86400.0),
    ]
    names = [job.name for job in plan_exports(photos, pattern="{folder}_{date}", folder_name="Trip")]
    assert names == ["Trip_1970-01-01.JPG", "Trip_1970-01-02.jpg", "Trip_1970-01-02 (1).jpg"]
    assert [job.name for job in plan_exports(photos, pattern="x{seq}")] == ["x001.JPG", "x002.jpg", "x003.jpg"]


def test_exports_in_another_folder_are_removed_there(tmp_path):
    path = touch(tmp_path / "web" / "a.jpg", "export")
    photo = Photo(str(tmp_path / "a.jpg"), REJECT, exports=[export_record(path, "web")])
    assert photo.exports[0]["dir"] == "web"
    assert export_record(path)  # (no "dir" for _EXPORTS)
    assert "dir" not in export_record(path)

    removed, errors = remove_rejected_exports([photo], str(tmp_path))

    assert (removed, errors) == ([os.path.normpath(path)], [])
    assert not os.path.exists(path) and photo.exports == []
