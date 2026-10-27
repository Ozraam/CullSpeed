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


def _free_name(dest_dir, name, taken):
    """`name`, or `name (1)`, `name (2)`… if something already uses it."""
    base, ext = os.path.splitext(name)
    candidate, n = name, 1
    while os.path.exists(os.path.join(dest_dir, candidate)) or candidate.lower() in taken:
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
            new_name = _free_name(move.dest_dir, old_name, dest_taken)
            new_path = os.path.join(move.dest_dir, new_name)
            shutil.move(move.src, new_path)
            dest_taken.add(new_name.lower())
            result.moved[move.src] = new_path

            for stem, sidecar in _sidecars(move.src, listings[src_dir]):
                sidecar_src = os.path.join(src_dir, sidecar)
                if not os.path.exists(sidecar_src):
                    continue  # already moved with a sibling
                new_stem = new_name if stem == old_name else os.path.splitext(new_name)[0]
                sidecar_name = _free_name(move.dest_dir, new_stem + sidecar[len(stem):], dest_taken)
                shutil.move(sidecar_src, os.path.join(move.dest_dir, sidecar_name))
                dest_taken.add(sidecar_name.lower())
        except OSError as exc:
            result.errors.append(f"{os.path.basename(move.src)}: {exc.strerror or exc}")

    if progress:
        progress(len(moves))
    return result
