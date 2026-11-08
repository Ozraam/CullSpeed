"""Moving photos into _KEEPS / _REJECTS (or back to the root), what goes to _EXPORTS, the Trash and importing."""
import errno
import hashlib
import os
import shutil
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field

from PyQt6.QtCore import QFile

from . import exif, exportsettings
from .constants import EXPORT_DIR, KEEP, KEEP_DIR, REJECT, REJECT_DIR, SUPPORTED_EXTENSIONS
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
    name: str  # file name in the export folder


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


def exported_statuses(keeps_only=False):
    """The marks whose photos Process Files exports."""
    return (KEEP,) if keeps_only else (KEEP, None)


def plan_exports(photos, keeps_only=False, pattern="{name}", folder_name=""):
    """What "Process Files" exports: edited photos rendered, the JPEG of the others copied.

    Rejected photos are left out, and unmarked ones too with `keeps_only`.
    Files are named by `pattern` (see exportsettings.file_stem()), made unique
    within the plan (` (n)` added when two photos share one name); a file of
    the same name already at the destination is replaced, so processing again
    refreshes the exports.
    """
    wanted = exported_statuses(keeps_only)
    chosen = []
    for photo in photos:
        if photo.status not in wanted:
            continue
        if photo.edit:
            chosen.append((photo, photo.path, os.path.splitext(photo.name)[0], ".jpg"))
        elif (jpeg := jpeg_file(photo)) is not None:
            chosen.append((photo, jpeg, *os.path.splitext(os.path.basename(jpeg))))

    jobs, taken = [], set()

    def unique(stem, ext):
        name, n = stem + ext, 1
        while name.lower() in taken:
            name, n = f"{stem} ({n}){ext}", n + 1
        taken.add(name.lower())
        return name

    for seq, (photo, path, stem, ext) in enumerate(chosen, 1):
        date = ""
        if exportsettings.uses_date(pattern):
            moment = photo.capture_time if photo.capture_time is not None else exif.capture_time(photo.files)
            date = exportsettings.date_text(moment)
        stem = exportsettings.file_stem(pattern, stem, date, seq, len(chosen), folder_name)
        jobs.append(ExportJob(path, photo.edit, unique(stem, ext)))
    return jobs


def without_export(photos, keeps_only=False):
    """The photos plan_exports() leaves out for having no edit and no JPEG, those it leaves out anyway aside."""
    wanted = exported_statuses(keeps_only)
    return [photo for photo in photos if photo.status in wanted and not photo.edit and jpeg_file(photo) is None]


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


def export_record(path, folder=EXPORT_DIR):
    """What Photo.exports remembers of an export, to tell later whether the file is still that export.

    `folder` is the export folder as the settings give it (relative to the
    shoot folder, or absolute); _EXPORTS, the default, isn't written down.
    """
    stat = os.stat(path)
    record = {"name": os.path.basename(path), "size": stat.st_size, "mtime": stat.st_mtime_ns}
    if folder != EXPORT_DIR:
        record["dir"] = folder
    return record


def record_path(shoot_folder, record):
    """Where the export a record describes is."""
    folder = os.path.expanduser(record.get("dir", EXPORT_DIR))
    return os.path.normpath(os.path.join(shoot_folder, folder, record["name"]))


def _same_record(path, record):
    current = export_record(path)
    return all(current[key] == record[key] for key in ("name", "size", "mtime"))


def rejected_exports(photos, shoot_folder):
    """(removable, changed): paths of the exports of rejected photos, as they were written or changed since.

    Only files recorded as exports of a photo are considered, so what the user
    added to the export folder is never touched, even under a photo's name.
    """
    removable, changed = [], []
    for photo in photos:
        if photo.status == REJECT:
            for record in photo.exports:
                path = record_path(shoot_folder, record)
                with suppress(OSError):  # already gone
                    (removable if _same_record(path, record) else changed).append(path)
    return removable, changed


def remove_rejected_exports(photos, shoot_folder):
    """Delete what rejected_exports() calls removable; returns (paths removed, errors).

    Rejected photos then only remember the exports that could not be removed.
    """
    removed, errors, failed = [], [], set()
    for path in rejected_exports(photos, shoot_folder)[0]:
        try:
            os.remove(path)
            removed.append(path)
        except OSError as exc:
            errors.append(f"{os.path.basename(path)}: {exc.strerror or exc}")
            failed.add(path)
    for photo in photos:
        if photo.status == REJECT:
            photo.exports = [record for record in photo.exports if record_path(shoot_folder, record) in failed]
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


# -- the Trash -----------------------------------------------------------------

@dataclass
class TrashResult:
    trashed: list = field(default_factory=list)  # primary paths of the photos now in the Trash
    errors: list = field(default_factory=list)
    cancelled: bool = False


def plan_trash(photos):
    """The photos sitting in _REJECTS and still marked reject: a photo marked otherwise since is on its way out."""
    return [photo for photo in photos if photo.location == REJECT and photo.status == REJECT]


def _photo_group(photo):
    """The photo's files and sidecars, all in one folder."""
    return [source for source, _name in _group(photo.files, os.listdir(os.path.dirname(photo.path)))]


def trash_size(photos):
    """Bytes the photos' files and sidecars take."""
    total = 0
    for photo in photos:
        with suppress(OSError):
            total += sum(os.path.getsize(path) for path in _photo_group(photo))
    return total


def _to_trash(path):
    """Move a file to the system Trash; returns where it is now. Raises OSError, leaving the file, if that can't be done."""
    done, location = QFile.moveToTrash(path)
    if not done:
        raise OSError(errno.EIO, "could not be moved to the Trash")
    return location


def execute_trash(photos, progress=None):
    """Move photos to the Trash, never deleting anything. `progress(done)` may return False to stop.

    A photo's files go together: when one can't, those already in the Trash come back.
    """
    result = TrashResult()
    for done, photo in enumerate(photos):
        if progress and progress(done) is False:
            result.cancelled = True
            break
        trashed = {}
        try:
            for path in _photo_group(photo):
                trashed[path] = _to_trash(path)
            result.trashed.append(photo.path)
        except OSError as exc:
            for path, location in trashed.items():
                with suppress(OSError):
                    shutil.move(location, path)
            result.errors.append(f"{photo.name}: {exc.strerror or exc}")
    if progress:
        progress(len(photos))
    return result


# -- importing -----------------------------------------------------------------

@dataclass
class ImportResult:
    copied: int = 0  # files, sidecars included
    skipped: int = 0  # files already at the destination
    errors: list = field(default_factory=list)
    cancelled: bool = False


def plan_import(source):
    """Photos found under `source`, as lists of paths: files with the same name in a folder stay together.

    Hidden entries (`.Trashes`, macOS's `._IMG_1.jpg`) and _EXPORTS are left out.
    """
    groups = []
    for folder, directories, files in os.walk(source):
        directories[:] = sorted(d for d in directories if not d.startswith('.') and d != EXPORT_DIR)
        by_stem = {}
        for name in sorted(files):
            stem, ext = os.path.splitext(name)
            if not name.startswith('.') and ext.lower() in SUPPORTED_EXTENSIONS:
                by_stem.setdefault(stem.casefold(), []).append(os.path.join(folder, name))
        groups += by_stem.values()
    return groups


def find_card(roots=("/Volumes", "/media", "/run/media")):
    """The first mounted volume with a DCIM folder, for what to import from; "" if none.

    (On Linux, volumes are one level further down, in a folder per user.)
    """
    pending = list(roots)
    while pending:
        root = pending.pop(0)
        with suppress(OSError):
            for entry in sorted(os.scandir(root), key=lambda entry: entry.name):
                if os.path.isdir(os.path.join(entry.path, "DCIM")):
                    return entry.path
                if root in ("/media", "/run/media") and entry.is_dir():
                    pending.append(entry.path)
    return ""


def import_size(groups):
    total = 0
    for group in groups:
        for path in group:
            with suppress(OSError):
                total += os.path.getsize(path)
    return total


def _digest(path):
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        while chunk := file.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _same_file(a, b):
    return os.path.getsize(a) == os.path.getsize(b) and _digest(a) == _digest(b)


def _copy_verified(source, dest):
    """Copy `source`, then read the copy back and compare; nothing is left at `dest` if they differ."""
    digest = hashlib.sha256()
    with atomic_write(dest) as tmp_path:
        with open(source, "rb") as reader, open(tmp_path, "wb") as writer:
            while chunk := reader.read(1 << 20):
                digest.update(chunk)
                writer.write(chunk)
        shutil.copystat(source, tmp_path)
        if _digest(tmp_path) != digest.hexdigest():
            raise OSError(errno.EIO, "the copy differs from the original")


def _already_imported(source, name, dest_dir):
    """Whether this very file is at the destination, even if it was sorted since."""
    candidates = (dest_dir, os.path.join(dest_dir, KEEP_DIR), os.path.join(dest_dir, REJECT_DIR))
    return any(os.path.isfile(path := os.path.join(d, name)) and _same_file(source, path) for d in candidates)


def _import_group(group, dest_dir, result):
    """Copy a photo's files and sidecars, under a number free for all of them, never overwriting."""
    folder = os.path.dirname(group[0])
    files = _group(group, os.listdir(folder))
    sidecars = {source for source, _name in files} - set(group)
    n = 0
    while True:
        # A sidecar that exists already may have been edited since: it is kept, and is no reason for a new name.
        names = [(source, os.path.join(dest_dir, name(n)), name(n)) for source, name in files]
        taken = any(source not in sidecars and os.path.exists(dest) and not _same_file(source, dest) for source, dest, _ in names)
        if not taken:
            break
        n += 1
    for source, dest, name in names:
        if os.path.exists(dest) or (source not in sidecars and _already_imported(source, name, dest_dir)):
            result.skipped += 1
        else:
            _copy_verified(source, dest)
            result.copied += 1


def execute_import(groups, destinations, progress=None):
    """Copy `groups` (see plan_import()) to every folder of `destinations`. Originals are never changed.

    `progress(done)` may return False to stop. Files already there are skipped,
    so importing a card again only brings what is new.
    """
    result = ImportResult()
    for done, group in enumerate(groups):
        if progress and progress(done) is False:
            result.cancelled = True
            break
        for dest_dir in destinations:
            try:
                os.makedirs(dest_dir, exist_ok=True)
                _import_group(group, dest_dir, result)
            except OSError as exc:
                result.errors.append(f"{os.path.basename(group[0])} → {dest_dir}: {exc.strerror or exc}")
    if progress:
        progress(len(groups))
    return result
