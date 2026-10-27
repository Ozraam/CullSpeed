import json
import os
from collections import Counter
from dataclasses import dataclass
from itertools import groupby

from PyQt6.QtCore import QAbstractListModel, QModelIndex, QSortFilterProxyModel, Qt, pyqtSignal

from .constants import KEEP, KEEP_DIR, REJECT, REJECT_DIR, SESSION_FILENAME, SUPPORTED_EXTENSIONS

STATUS_ROLE = Qt.ItemDataRole.UserRole + 1
PATH_ROLE = Qt.ItemDataRole.UserRole + 2


@dataclass
class Photo:
    path: str
    status: str | None = None
    location: str | None = None  # folder the file sits in: KEEP, REJECT or None (root)

    @property
    def name(self):
        return os.path.basename(self.path)


# -- folder & session ------------------------------------------------------

def _scan_dir(path):
    if not os.path.isdir(path):
        return []
    with os.scandir(path) as entries:
        return [
            entry.path
            for entry in entries
            if entry.is_file()
            and not entry.name.startswith('.')
            and os.path.splitext(entry.name)[1].lower() in SUPPORTED_EXTENSIONS
        ]


def scan_folder(folder):
    """Photos in `folder`, `_KEEPS` and `_REJECTS`, with their saved marks."""
    photos = [Photo(p) for p in _scan_dir(folder)]
    photos += [Photo(p, KEEP, KEEP) for p in _scan_dir(os.path.join(folder, KEEP_DIR))]
    photos += [Photo(p, REJECT, REJECT) for p in _scan_dir(os.path.join(folder, REJECT_DIR))]

    # Saved marks win over folder location, so marks changed after the last
    # "Process Files" are not lost.
    session = load_session(folder)
    name_counts = Counter(photo.name for photo in photos)
    for photo in photos:
        key = _session_key(folder, photo.path)
        if key in session:
            photo.status = session[key]
        elif name_counts[photo.name] == 1 and photo.name in session:
            photo.status = session[photo.name]  # legacy filename-keyed session

    photos.sort(key=lambda p: (p.name.casefold(), p.path))
    return photos


def _session_key(folder, path):
    """Path relative to `folder` with "/" separators, e.g. "_KEEPS/IMG_1.CR2"."""
    return os.path.relpath(path, folder).replace(os.sep, "/")


def load_session(folder):
    try:
        with open(os.path.join(folder, SESSION_FILENAME), encoding='utf-8') as file:
            data = json.load(file)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {name: status for name, status in data.items() if status in (KEEP, REJECT, None)}


def save_session(folder, photos):
    path = os.path.join(folder, SESSION_FILENAME)
    tmp_path = path + ".tmp"
    data = {_session_key(folder, photo.path): photo.status for photo in photos}
    with open(tmp_path, 'w', encoding='utf-8') as file:
        json.dump(data, file, indent=2, ensure_ascii=False)
    os.replace(tmp_path, path)


# -- Qt models -------------------------------------------------------------

class PhotoModel(QAbstractListModel):
    """Shared by the filmstrip and the gallery."""

    statuses_changed = pyqtSignal(list)  # rows

    def __init__(self, loader, parent=None):
        super().__init__(parent)
        self._loader = loader
        self._photos = []
        self._rows = {}
        # Kept up to date on every change so per-keystroke updates stay O(1).
        self._keeps = self._rejects = self._unsorted = 0
        loader.thumbnail_loaded.connect(self._on_thumbnail_loaded)

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._photos)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        photo = self._photos[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return photo.name
        if role == Qt.ItemDataRole.DecorationRole:
            return self._loader.thumbnail(photo.path)
        if role == Qt.ItemDataRole.ToolTipRole:
            return photo.path
        if role == STATUS_ROLE:
            return photo.status
        if role == PATH_ROLE:
            return photo.path
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

    def set_status(self, rows, status):
        """Mark `rows`; returns [(path, previous_status)] for undo."""
        changes = []
        changed_rows = []
        for row in rows:
            photo = self._photos[row]
            if photo.status != status:
                changes.append((photo.path, photo.status))
                self._set(photo, status)
                changed_rows.append(row)
        self._emit_changed(changed_rows)
        return changes

    def restore(self, changes):
        rows = []
        for path, status in changes:
            row = self.row_of(path)
            if row != -1:
                self._set(self._photos[row], status)
                rows.append(row)
        self._emit_changed(rows)
        return rows

    def apply_moves(self, moved):
        """`moved`: old path -> (new path, location it was moved to)."""
        for photo in self._photos:
            if photo.path in moved:
                self._tally(photo, -1)
                photo.path, photo.location = moved[photo.path]
                self._tally(photo, 1)
        self._reindex()
        if self._photos:
            self.dataChanged.emit(self.index(0), self.index(len(self._photos) - 1))

    def counts(self):
        return self._keeps, self._rejects, len(self._photos) - self._keeps - self._rejects

    def has_unsorted(self):
        """Whether any photo is not in the folder its mark sends it to."""
        return self._unsorted > 0

    def _set(self, photo, status):
        self._tally(photo, -1)
        photo.status = status
        self._tally(photo, 1)

    def _tally(self, photo, sign):
        if photo.status == KEEP:
            self._keeps += sign
        elif photo.status == REJECT:
            self._rejects += sign
        if photo.status != photo.location:
            self._unsorted += sign

    def _reindex(self):
        self._rows = {photo.path: row for row, photo in enumerate(self._photos)}

    def _emit_changed(self, rows):
        if not rows:
            return
        # One signal per run of consecutive rows: marking a big selection
        # would otherwise re-filter the gallery once per photo.
        for _, run in groupby(enumerate(sorted(rows)), lambda item: item[1] - item[0]):
            run = [row for _, row in run]
            self.dataChanged.emit(self.index(run[0]), self.index(run[-1]), [STATUS_ROLE])
        self.statuses_changed.emit(rows)

    def _on_thumbnail_loaded(self, path):
        row = self.row_of(path)
        if row != -1:
            index = self.index(row)
            self.dataChanged.emit(index, index, [Qt.ItemDataRole.DecorationRole])


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
