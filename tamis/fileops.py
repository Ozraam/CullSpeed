"""Moving photos into _KEEPS / _REJECTS (or back to the root), and what goes to _EXPORTS."""
import os
import shutil
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field

from .constants import KEEP, KEEP_DIR, REJECT, REJECT_DIR
from .imaging import is_raw


@dataclass
class Move:
    src: str
    dest_dir: str
    status: str | None
    companions: list = field(default_factory=list)  # moved along, e.g. the JPEG of a RAW+JPEG pair


@dataclass
class ExportJob:
    path: str  # the file rendered or copied
    edit: dict  # {}: copied as it is
    name: str  # file name in _EXPORTS


@dataclass
class MoveResult:
    moved: dict = field(default_factory=dict)  # old path -> new path, photos and companions
    errors: list = field(default_factory=list)
    cancelled: bool = False


def target_dir(folder, status):
    if status == KEEP:
        return os.path.join(folder, KEEP_DIR)
    if status == REJECT:
        return os.path.join(folder, REJECT_DIR)
    return folder


def plan_moves(folder, photos):
    return [
        Move(photo.path, target_dir(folder, photo.status), photo.status, list(photo.companions))
        for photo in photos
        if photo.status != photo.location
    ]


def _sidecars(path, dir_listing):
    """XMP sidecars: `IMG.CR2.xmp` (darktable) and, for RAWs, `IMG.xmp` (Lightroom)."""
    name = os.path.basename(path)
    stems = [name]
    if is_raw(path):
        stems.append(os.path.splitext(name)[0])
    found = []
    for stem in stems:
        for entry in dir_listing:
            if entry.lower() == f"{stem}.xmp".lower():
                found.append((stem, entry))
    return found


def _numbered(stem, n):
    return stem if n == 0 else f"{stem} ({n})"


def _group(files, listing):
    """[(source path, name(n))] for `files` and their sidecars.

    `name(0)` is the current name and `name(n)` adds " (n)" to the stem, so
    a whole group (RAW, JPEG, sidecars) is renamed together or not at all.
    """
    group = {}
    for path in files:
        folder, name = os.path.split(path)
        stem, ext = os.path.splitext(name)
        group[path] = lambda n, stem=stem, ext=ext: _numbered(stem, n) + ext
        for sidecar_stem, sidecar in _sidecars(path, listing):
            sidecar_path = os.path.join(folder, sidecar)
            if sidecar_path in group or not os.path.exists(sidecar_path):
                continue  # already listed, or already moved with a sibling
            suffix = sidecar[len(sidecar_stem):]
            if sidecar_stem == name:  # IMG.CR2.xmp
                group[sidecar_path] = lambda n, stem=stem, ext=ext, suffix=suffix: _numbered(stem, n) + ext + suffix
            else:  # IMG.xmp
                group[sidecar_path] = lambda n, stem=stem, suffix=suffix: _numbered(stem, n) + suffix
    return list(group.items())


def _free_number(dest_dir, group, taken):
    """The smallest n for which every name in `group` is free in `dest_dir`."""
    n = 0
    while any(
        os.path.exists(os.path.join(dest_dir, name(n))) or name(n).lower() in taken for _, name in group
    ):
        n += 1
    return n


def _move_group(group, dest_dir, n):
    """Move a whole group or, if one of its files fails, none; returns {source: new path}.

    The OSError raised on failure has a `stranded` attribute: {source: new path}
    of the files that could not be moved back, so the caller can follow them.
    """
    moved = {}
    try:
        for source, name in group:
            new_path = os.path.join(dest_dir, name(n))
            shutil.move(source, new_path)
            moved[source] = new_path
    except OSError as exc:
        stranded = {}
        for source, new_path in moved.items():
            try:
                shutil.move(new_path, source)
            except OSError:
                stranded[source] = new_path
        exc.stranded = stranded
        raise
    return moved


def execute_moves(moves, progress=None):
    """Move files, never overwriting. `progress(done)` may return False to stop.

    A photo's files (RAW, JPEG, sidecars) move together: when one fails, the
    others go back and the photo is reported as failed.
    """
    result = MoveResult()
    listings = {}
    taken = {}

    for done, move in enumerate(moves):
        if progress and progress(done) is False:
            result.cancelled = True
            break
        try:
            os.makedirs(move.dest_dir, exist_ok=True)
            src_dir = os.path.dirname(move.src)
            if src_dir not in listings:
                listings[src_dir] = os.listdir(src_dir)
            dest_taken = taken.setdefault(move.dest_dir, set())

            photo_files = [move.src, *move.companions]
            group = _group(photo_files, listings[src_dir])
            n = _free_number(move.dest_dir, group, dest_taken)
            moved = _move_group(group, move.dest_dir, n)
            dest_taken.update(os.path.basename(new_path).lower() for new_path in moved.values())
            result.moved.update({source: new_path for source, new_path in moved.items() if source in photo_files})
        except OSError as exc:
            stranded = getattr(exc, "stranded", {})
            result.moved.update({source: new_path for source, new_path in stranded.items() if source in photo_files})
            error = f"{os.path.basename(move.src)}: {exc.strerror or exc}"
            if stranded:
                error += f" (could not be moved back: {', '.join(map(os.path.basename, stranded))})"
            result.errors.append(error)

    if progress:
        progress(len(moves))
    return result


JPEG_EXTENSIONS = frozenset({'.jpg', '.jpeg'})


def is_jpeg(path):
    return os.path.splitext(path)[1].lower() in JPEG_EXTENSIONS


def jpeg_file(photo):
    """The photo's JPEG: itself, or the JPEG of a RAW+JPEG pair. None if it has none."""
    return next(filter(is_jpeg, photo.files), None)


def free_name(dest_dir, stem, ext):
    """`stem + ext`, or `stem (n) + ext` if that is taken in `dest_dir`."""
    name, n = stem + ext, 1
    while os.path.exists(os.path.join(dest_dir, name)):
        name, n = f"{stem} ({n}){ext}", n + 1
    return name


def plan_exports(photos):
    """What "Process Files" puts in _EXPORTS: edited photos rendered, the JPEG of the others copied.

    Rejected photos are left out. Names are unique within the plan (` (n)`
    added when two photos share one); a file of the same name already in
    _EXPORTS is replaced, so processing again refreshes the exports.
    """
    jobs, taken = [], set()

    def unique(stem, ext):
        name, n = stem + ext, 1
        while name.lower() in taken:
            name, n = f"{stem} ({n}){ext}", n + 1
        taken.add(name.lower())
        return name

    for photo in photos:
        if photo.status == REJECT:
            continue
        if photo.edit:
            stem = os.path.splitext(photo.name)[0]
            jobs.append(ExportJob(photo.path, photo.edit, unique(stem, ".jpg")))
        elif (jpeg := jpeg_file(photo)) is not None:
            jobs.append(ExportJob(jpeg, {}, unique(*os.path.splitext(os.path.basename(jpeg)))))
    return jobs


def without_export(photos):
    """The photos plan_exports() leaves out for having no edit and no JPEG, rejected ones aside."""
    return [photo for photo in photos if photo.status != REJECT and not photo.edit and jpeg_file(photo) is None]


@contextmanager
def atomic_write(dest):
    """Gives a temporary path to write to, which replaces `dest` once the write has succeeded.

    A crash or a full disk then cannot leave a truncated file in place of a good one.
    """
    folder, name = os.path.split(dest)
    tmp_path = os.path.join(folder, f".{name}.tmp")
    try:
        yield tmp_path
        with open(tmp_path, "rb+") as file:
            os.fsync(file.fileno())  # or a power cut can leave an empty file after the rename
        os.replace(tmp_path, dest)
    except Exception:
        with suppress(OSError):
            os.remove(tmp_path)
        raise


def export_record(path):
    """What Photo.exports remembers of an export, to tell later whether the file is still that export."""
    stat = os.stat(path)
    return {"name": os.path.basename(path), "size": stat.st_size, "mtime": stat.st_mtime_ns}


def rejected_exports(photos, dest_dir):
    """(removable, changed): paths of the exports of rejected photos, as they were written or changed since.

    Only files recorded as exports of a photo are considered, so what the user
    added to _EXPORTS is never touched, even under a photo's name.
    """
    removable, changed = [], []
    for photo in photos:
        if photo.status == REJECT:
            for record in photo.exports:
                path = os.path.join(dest_dir, record["name"])
                with suppress(OSError):  # already gone
                    (removable if export_record(path) == record else changed).append(path)
    return removable, changed


def remove_rejected_exports(photos, dest_dir):
    """Delete what rejected_exports() calls removable; returns (paths removed, errors).

    Rejected photos then only remember the exports that could not be removed.
    """
    removed, errors, failed = [], [], set()
    for path in rejected_exports(photos, dest_dir)[0]:
        try:
            os.remove(path)
            removed.append(path)
        except OSError as exc:
            errors.append(f"{os.path.basename(path)}: {exc.strerror or exc}")
            failed.add(os.path.basename(path))
    for photo in photos:
        if photo.status == REJECT:
            photo.exports = [record for record in photo.exports if record["name"] in failed]
    return removed, errors


def copy_export(path, dest_dir, name=None):
    """Copy `path` into `dest_dir`; returns the new path.

    With `name` the file is written under that name, replacing one already
    there; without, it gets a free name (`IMG_1 (1).jpg`).
    """
    os.makedirs(dest_dir, exist_ok=True)
    if name is None:
        name = free_name(dest_dir, *os.path.splitext(os.path.basename(path)))
    dest = os.path.join(dest_dir, name)
    with atomic_write(dest) as tmp_path:
        shutil.copy2(path, tmp_path)
    return dest
