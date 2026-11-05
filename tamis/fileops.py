"""Moving photos into _KEEPS / _REJECTS (or back to the root), and what goes to _EXPORTS."""
import os
import shutil
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


def execute_moves(moves, progress=None):
    """Move files, never overwriting. `progress(done)` may return False to stop."""
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
            for source, name in group:
                new_path = os.path.join(move.dest_dir, name(n))
                shutil.move(source, new_path)
                dest_taken.add(name(n).lower())
                if source in photo_files:
                    result.moved[source] = new_path
        except OSError as exc:
            result.errors.append(f"{os.path.basename(move.src)}: {exc.strerror or exc}")

    if progress:
        progress(len(moves))
    return result


JPEG_EXTENSIONS = frozenset({'.jpg', '.jpeg'})


def _jpeg(photo):
    """The photo's JPEG: itself, or the JPEG of a RAW+JPEG pair. None if it has none."""
    for path in photo.files:
        if os.path.splitext(path)[1].lower() in JPEG_EXTENSIONS:
            return path
    return None


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
        elif (jpeg := _jpeg(photo)) is not None:
            jobs.append(ExportJob(jpeg, {}, unique(*os.path.splitext(os.path.basename(jpeg)))))
    return jobs


def copy_export(path, dest_dir, name):
    """Copy `path` into `dest_dir` as `name`, replacing it; returns the new path."""
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, name)
    shutil.copy2(path, dest)
    return dest
