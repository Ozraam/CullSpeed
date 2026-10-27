"""Moving photos into _KEEPS / _REJECTS (or back to the root)."""
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


@dataclass
class MoveResult:
    moved: dict = field(default_factory=dict)  # old path -> new path
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
        Move(photo.path, target_dir(folder, photo.status), photo.status)
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


def _sidecar_name(photo_name, old_name, stem, sidecar):
    """`sidecar`'s name once its photo is called `photo_name`."""
    base = photo_name if stem == old_name else os.path.splitext(photo_name)[0]
    return base + sidecar[len(stem):]


def _free_name(dest_dir, name, taken, companions=None):
    """`name`, or `name (1)`, `name (2)`… so that it and its companions are all free.

    `companions(candidate)` lists the other names that go with `candidate`
    (its sidecars), so a photo and its sidecars are always renamed together.
    """
    base, ext = os.path.splitext(name)
    candidate, n = name, 1

    def used(names):
        return any(os.path.exists(os.path.join(dest_dir, c)) or c.lower() in taken for c in names)

    while used([candidate, *(companions(candidate) if companions else [])]):
        candidate = f"{base} ({n}){ext}"
        n += 1
    return candidate


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

            old_name = os.path.basename(move.src)
            sidecars = [
                (stem, sidecar)
                for stem, sidecar in _sidecars(move.src, listings[src_dir])
                if os.path.exists(os.path.join(src_dir, sidecar))  # else already moved with a sibling
            ]
            new_name = _free_name(
                move.dest_dir,
                old_name,
                dest_taken,
                lambda candidate, old=old_name, found=sidecars: [
                    _sidecar_name(candidate, old, stem, sidecar) for stem, sidecar in found
                ],
            )
            new_path = os.path.join(move.dest_dir, new_name)
            shutil.move(move.src, new_path)
            dest_taken.add(new_name.lower())
            result.moved[move.src] = new_path

            for stem, sidecar in sidecars:
                sidecar_name = _sidecar_name(new_name, old_name, stem, sidecar)
                shutil.move(os.path.join(src_dir, sidecar), os.path.join(move.dest_dir, sidecar_name))
                dest_taken.add(sidecar_name.lower())
        except OSError as exc:
            result.errors.append(f"{os.path.basename(move.src)}: {exc.strerror or exc}")

    if progress:
        progress(len(moves))
    return result
