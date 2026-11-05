import hashlib
import json
import os
from collections import Counter
from contextlib import suppress
from dataclasses import dataclass, field
from itertools import groupby

from PyQt6.QtCore import QAbstractListModel, QModelIndex, QSortFilterProxyModel, Qt, pyqtSignal

from . import develop, xmp
from .constants import (
    KEEP,
    KEEP_DIR,
    LEGACY_SESSION_FILENAME,
    RAW_EXTENSIONS,
    REJECT,
    REJECT_DIR,
    SESSION_FILENAME,
    SUPPORTED_EXTENSIONS,
)

STATUS_ROLE = Qt.ItemDataRole.UserRole + 1
PATH_ROLE = Qt.ItemDataRole.UserRole + 2
THUMBNAIL_FAILED_ROLE = Qt.ItemDataRole.UserRole + 3
RATING_ROLE = Qt.ItemDataRole.UserRole + 4
LABEL_ROLE = Qt.ItemDataRole.UserRole + 5
EDITED_ROLE = Qt.ItemDataRole.UserRole + 6

_FIELD_ROLES = {"status": STATUS_ROLE, "rating": RATING_ROLE, "label": LABEL_ROLE, "edit": EDITED_ROLE}


@dataclass
class Photo:
    path: str
    status: str | None = None
    location: str | None = None  # folder the file sits in: KEEP, REJECT or None (root)
    rating: int = 0  # stars, 0–5
    label: str | None = None  # colour label, one of xmp.LABELS
    # Same-named files culled with this one: the JPEG of a RAW+JPEG pair.
    companions: list = field(default_factory=list)
    # Quick edits, as develop.clean() keeps them: {} when not edited. Always
    # replaced, never changed in place, so undo can keep the previous one.
    edit: dict = field(default_factory=dict)
    # The rating and label could not be written to the XMP sidecar yet: until
    # they are, the session has the newer values and wins over the sidecar.
    xmp_pending: bool = False

    @property
    def name(self):
        return os.path.basename(self.path)

    @property
    def display_name(self):
        """`IMG_1.CR2 + JPG` for a pair."""
        extras = [os.path.splitext(c)[1].lstrip('.').upper() for c in self.companions]
        return " + ".join([self.name, *extras])

    @property
    def files(self):
        return [self.path, *self.companions]


def _is_raw(path):
    return os.path.splitext(path)[1].lower() in RAW_EXTENSIONS


# -- folder & session ------------------------------------------------------

def _list_dir(path):
    """(photo paths, {lowercase name: name} of every entry) in `path`."""
    if not os.path.isdir(path):
        return [], {}
    photos, names = [], {}
    with os.scandir(path) as entries:
        for entry in entries:
            names[entry.name.lower()] = entry.name
            if (
                entry.is_file()
                and not entry.name.startswith('.')
                and os.path.splitext(entry.name)[1].lower() in SUPPORTED_EXTENSIONS
            ):
                photos.append(entry.path)
    return photos, names


def _pair(paths):
    """[(primary, companions)]: a RAW and its same-named JPEG/HEIC/... become one photo."""
    groups = {}
    for path in paths:
        groups.setdefault(os.path.splitext(os.path.basename(path))[0].casefold(), []).append(path)
    result = []
    for files in groups.values():
        raws = [f for f in files if _is_raw(f)]
        if len(raws) == 1 and len(files) > 1:
            result.append((raws[0], sorted(f for f in files if f != raws[0])))
        else:
            result += [(f, []) for f in files]  # nothing to pair, or ambiguous (two RAWs)
    return result


def _photo_dirs(folder):
    return ((folder, None), (os.path.join(folder, KEEP_DIR), KEEP), (os.path.join(folder, REJECT_DIR), REJECT))


def photo_files(folder):
    """Every photo file in `folder`, `_KEEPS` and `_REJECTS`: a cheap check for changes."""
    return {path for directory, _ in _photo_dirs(folder) for path in _list_dir(directory)[0]}


def scan_folder(folder, fallback_dir=None, problems=None):
    """Photos in `folder`, `_KEEPS` and `_REJECTS`, with their saved marks and ratings.

    `problems`, a list, receives the path of any damaged session, see load_session().
    """
    photos = []
    from_sidecars = {}
    for directory, location in _photo_dirs(folder):
        paths, names = _list_dir(directory)
        for primary, companions in _pair(paths):
            photo = Photo(primary, location, location, companions=companions)
            photos.append(photo)
            stored = xmp.read(photo.files, names)
            if stored is not None:
                from_sidecars[primary] = stored

    # Saved marks win over folder location, so marks changed after the last
    # "Process Files" are not lost.
    session = load_session(folder, fallback_dir, problems)
    name_counts = Counter(photo.name for photo in photos)
    for photo in photos:
        key = relative_key(folder, photo.path)
        entry = session.get(key)
        if entry is None and name_counts[photo.name] == 1:
            entry = session.get(photo.name)  # legacy filename-keyed session
        if entry is not None:
            photo.status, photo.rating, photo.label = entry["status"], entry["rating"], entry["label"]
            photo.edit = entry["edit"]
            photo.xmp_pending = entry.get("xmp_pending", False)
        # Sidecars are what other editors change, and Tamis writes them
        # whenever a rating changes: they are the fresher source, unless
        # that write failed.
        if photo.path in from_sidecars and not photo.xmp_pending:
            photo.rating, photo.label = from_sidecars[photo.path]

    photos.sort(key=lambda p: (p.name.casefold(), p.path))
    return photos


def relative_key(folder, path):
    """Path relative to `folder` with "/" separators, e.g. "_KEEPS/IMG_1.CR2"."""
    return os.path.relpath(path, folder).replace(os.sep, "/")


# A folder we cannot write to (locked SD card, read-only share) gets its
# session in `fallback_dir` instead, under a name derived from its path.

def _fallback_path(folder, fallback_dir):
    digest = hashlib.sha1(os.path.abspath(folder).encode("utf-8")).hexdigest()[:16]
    return os.path.join(fallback_dir, f"{digest}.json")


def _session_paths(folder, fallback_dir):
    paths = [os.path.join(folder, SESSION_FILENAME)]
    if fallback_dir:
        paths.append(_fallback_path(folder, fallback_dir))
    return paths


def _entry(value):
    """A session value as {"status", "rating", "label", "edit"}, or None if it is not valid.

    Values are a bare status (older sessions, and photos without rating,
    label or edit) or a dict with those fields.
    """
    if not isinstance(value, dict):
        value = {"status": value}
    status = value.get("status")
    rating = value.get("rating", 0)
    label = value.get("label")
    if status not in (KEEP, REJECT, None):
        return None
    if not isinstance(rating, int) or isinstance(rating, bool) or not 0 <= rating <= 5:
        rating = 0
    if label not in xmp.LABELS:
        label = None
    entry = {"status": status, "rating": rating, "label": label, "edit": develop.clean(value.get("edit"))}
    if value.get("xmp_pending") is True:
        entry["xmp_pending"] = True
    return entry


def _set_aside(path):
    """Rename a damaged session so that saving doesn't replace it; returns where it is now."""
    stem, ext = os.path.splitext(path)
    target, n = f"{stem}.corrupt{ext}", 1
    while os.path.exists(target):
        target, n = f"{stem}.corrupt ({n}){ext}", n + 1
    try:
        os.replace(path, target)
    except OSError:
        return path  # a read-only folder: nothing will overwrite it there either
    return target


def _read_session(folder, fallback_dir, problems):
    """The newest readable session as stored, {} if none. Damaged ones are set aside and listed in `problems`."""
    paths = [*_session_paths(folder, fallback_dir), os.path.join(folder, LEGACY_SESSION_FILENAME)]
    existing = [path for path in paths if os.path.isfile(path)]
    for path in sorted(existing, key=os.path.getmtime, reverse=True):
        try:
            with open(path, encoding='utf-8') as file:
                data = json.load(file)
        except OSError:
            continue
        except ValueError:
            data = None
        if isinstance(data, dict):
            return data
        location = _set_aside(path)
        if problems is not None:
            problems.append(location)
    return {}


def load_session(folder, fallback_dir=None, problems=None):
    """Saved marks, keyed by relative path; the newest of the folder, fallback and pre-rename sessions.

    A damaged file is set aside as `….corrupt.json`, never overwritten, and the
    next newest is used instead. `problems`, a list, receives where it now is.
    """
    entries = {key: _entry(value) for key, value in _read_session(folder, fallback_dir, problems).items()}
    return {key: entry for key, entry in entries.items() if entry is not None}


def _write_json(path, data):
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, 'w', encoding='utf-8') as file:
            json.dump(data, file, indent=2, ensure_ascii=False)
            file.flush()
            os.fsync(file.fileno())  # or a power cut can leave an empty file after the rename
        os.replace(tmp_path, path)
    except OSError:
        with suppress(OSError):
            os.remove(tmp_path)
        raise


def _session_value(photo):
    if not photo.rating and photo.label is None and not photo.edit and not photo.xmp_pending:
        return photo.status  # same as older sessions
    value = {"status": photo.status, "rating": photo.rating, "label": photo.label}
    if photo.edit:
        value["edit"] = photo.edit
    if photo.xmp_pending:
        value["xmp_pending"] = True
    return value


def _absent_entries(folder, stored, photos, known, forget):
    """What `stored` says about photos that are not in `photos` for now, such as a `_KEEPS` taken out and brought back.

    A file may be back already: a refresh saves before it scans the folder again.
    """
    names = {key.rsplit("/", 1)[-1] for key in known}
    companions = {relative_key(folder, path) for photo in photos for path in photo.companions}
    kept = {}
    for key, value in stored.items():
        if key in known or key in forget or key in companions:  # (the JPEG of a pair is not a photo of its own)
            continue
        if "/" not in key and key in names:
            continue  # a legacy filename key (or a move made elsewhere): that photo has the entry now
        entry = _entry(value)
        if entry is not None and (entry["status"] or entry["rating"] or entry["label"] or entry["edit"]):
            kept[key] = value
    return kept


def save_session(folder, photos, fallback_dir=None, forget=(), problems=None):
    """Save marks in `folder`, else in `fallback_dir`; returns the path written.

    Entries of photos not in `photos` are kept, unless their relative path
    is in `forget` (the photo was moved). `problems` is as in load_session().
    Raises OSError when neither can be written.
    """
    data = {relative_key(folder, photo.path): _session_value(photo) for photo in photos}
    data.update(_absent_entries(folder, _read_session(folder, fallback_dir, problems), photos, data, forget))
    folder_path, *fallback = _session_paths(folder, fallback_dir)
    try:
        _write_json(folder_path, data)
    except OSError:
        if not fallback:
            raise
        os.makedirs(fallback_dir, exist_ok=True)
        _write_json(fallback[0], data)
        return fallback[0]
    # Both are stale now that the folder's session has the marks.
    for stale in (*fallback, os.path.join(folder, LEGACY_SESSION_FILENAME)):
        with suppress(OSError):
            os.remove(stale)
    return folder_path


# -- Qt models -------------------------------------------------------------

class PhotoModel(QAbstractListModel):
    """Shared by the filmstrip and the gallery."""

    statuses_changed = pyqtSignal(list)  # rows whose status, rating, label or edit changed
    ratings_changed = pyqtSignal(list)  # paths whose rating or label changed
    photos_edited = pyqtSignal(list)  # like statuses_changed, but not when a folder's photos are loaded

    def __init__(self, loader, parent=None):
        super().__init__(parent)
        self._loader = loader
        self._photos = []
        self._rows = {}
        # Kept up to date on every change so per-keystroke updates stay O(1).
        self._keeps = self._rejects = self._unsorted = 0
        loader.thumbnail_loaded.connect(self._on_thumbnail_loaded)
        loader.thumbnail_failed.connect(self._on_thumbnail_loaded)

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._photos)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        photo = self._photos[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return photo.display_name
        if role == Qt.ItemDataRole.DecorationRole:
            return self._loader.thumbnail(photo.path)
        if role == Qt.ItemDataRole.ToolTipRole:
            return "\n".join(photo.files)
        if role == STATUS_ROLE:
            return photo.status
        if role == PATH_ROLE:
            return photo.path
        if role == THUMBNAIL_FAILED_ROLE:
            return self._loader.thumbnail_error(photo.path)
        if role == RATING_ROLE:
            return photo.rating
        if role == LABEL_ROLE:
            return photo.label
        if role == EDITED_ROLE:
            return bool(photo.edit)
        return None

    @property
    def photos(self):
        return self._photos

    def photo(self, row):
        return self._photos[row]

    def row_of(self, path):
        return self._rows.get(path, -1)

    def set_photos(self, photos):
        self.beginResetModel()
        self._photos = photos
        self._reindex()
        self._keeps = self._rejects = self._unsorted = 0
        for photo in photos:
            self._tally(photo, 1)
        self.endResetModel()
        self.statuses_changed.emit(list(range(len(photos))))

    def set_field(self, rows, name, value):
        """Set `name` ("status", "rating", "label" or "edit") on `rows`; returns undo changes."""
        return self.set_values(name, {row: value for row in rows})

    def set_values(self, name, values):
        """Like set_field(), with a value per row: `values` is {row: value}."""
        changes = []
        for row, value in values.items():
            photo = self._photos[row]
            if getattr(photo, name) != value:
                changes.append((photo.path, name, getattr(photo, name)))
                self._set(photo, name, value)
        self._emit_changed(changes)
        return changes

    def set_status(self, rows, status):
        return self.set_field(rows, "status", status)

    def restore(self, changes):
        """Undo `changes`; returns the rows affected."""
        applied = []
        for path, name, value in changes:
            row = self.row_of(path)
            if row != -1:
                self._set(self._photos[row], name, value)
                applied.append((path, name, value))
        self._emit_changed(applied)
        return sorted({self.row_of(path) for path, _, _ in applied})

    def apply_moves(self, moved):
        """`moved`: old path -> (new path, location it was moved to), for photos and companions."""
        for photo in self._photos:
            if photo.path in moved:
                self._tally(photo, -1)
                photo.path, photo.location = moved[photo.path]
                photo.companions = [moved[c][0] if c in moved else c for c in photo.companions]
                self._tally(photo, 1)
        self._reindex()
        if self._photos:
            self.dataChanged.emit(self.index(0), self.index(len(self._photos) - 1))

    def counts(self):
        return self._keeps, self._rejects, len(self._photos) - self._keeps - self._rejects

    def has_unsorted(self):
        """Whether any photo is not in the folder its mark sends it to."""
        return self._unsorted > 0

    def _set(self, photo, name, value):
        if name == "status":
            self._tally(photo, -1)
            photo.status = value
            self._tally(photo, 1)
        else:
            setattr(photo, name, value)

    def _tally(self, photo, sign):
        if photo.status == KEEP:
            self._keeps += sign
        elif photo.status == REJECT:
            self._rejects += sign
        if photo.status != photo.location:
            self._unsorted += sign

    def _reindex(self):
        self._rows = {photo.path: row for row, photo in enumerate(self._photos)}

    def _emit_changed(self, changes):
        if not changes:
            return
        for name in {name for _, name, _ in changes}:
            rows = sorted({self.row_of(path) for path, field_name, _ in changes if field_name == name})
            # One signal per run of consecutive rows: marking a big selection
            # would otherwise re-filter the gallery once per photo.
            for _, run in groupby(enumerate(rows), lambda item: item[1] - item[0]):
                run = [row for _, row in run]
                self.dataChanged.emit(self.index(run[0]), self.index(run[-1]), [_FIELD_ROLES[name]])
        self.statuses_changed.emit(sorted({self.row_of(path) for path, _, _ in changes}))
        rated = [path for path, name, _ in changes if name in ("rating", "label")]
        if rated:
            self.ratings_changed.emit(list(dict.fromkeys(rated)))
        self.photos_edited.emit(sorted({self.row_of(path) for path, _, _ in changes}))

    def _on_thumbnail_loaded(self, path):
        row = self.row_of(path)
        if row != -1:
            index = self.index(row)
            self.dataChanged.emit(index, index, [Qt.ItemDataRole.DecorationRole, THUMBNAIL_FAILED_ROLE])


class StatusFilterProxy(QSortFilterProxyModel):
    """Gallery filter: all / unmarked / keep / reject."""

    ALL = "all"
    UNMARKED = "unmarked"

    def __init__(self, parent=None):
        super().__init__(parent)
        self._filter = self.ALL
        # Re-filter live when a status changes (Qt only re-checks for this role).
        self.setFilterRole(STATUS_ROLE)

    def set_filter(self, value):
        if value != self._filter:
            self.beginFilterChange()
            self._filter = value
            self.endFilterChange()

    def filterAcceptsRow(self, source_row, source_parent):
        if self._filter == self.ALL:
            return True
        status = self.sourceModel().photo(source_row).status
        if self._filter == self.UNMARKED:
            return status is None
        return status == self._filter
