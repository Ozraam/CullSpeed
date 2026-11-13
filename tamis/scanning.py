"""Reading folders off the GUI thread: a slow disk or share must not freeze the window."""

from dataclasses import dataclass, field

from PyQt6.QtCore import QObject, QThreadPool, pyqtSignal

from .analysis import file_key
from .models import scan_changes, scan_folder
from .workers import emit_safely


@dataclass
class ScanResult:
    folder: str
    photos: list | None = None  # a full scan's photos
    changes: object = None  # a refresh's FolderChanges
    problems: list = field(default_factory=list)  # damaged sessions, see load_session()
    error: str = ""  # the folder could not be read
    file_keys: dict = field(default_factory=dict)  # path -> analysis.file_key() of the photos read
    request: object = None  # what the caller passed along, given back as it was


class FolderScanner(QObject):
    """Reads folders in a worker; results arrive as `scanned(ScanResult)` on the GUI thread.

    Each request supersedes the ones before: the results of a stale one are
    ignored, like those of a request made before `reset()`.
    """

    scanned = pyqtSignal(object)
    progressed = pyqtSignal(str, int)  # folder, photos found so far

    _done = pyqtSignal(int, object)  # generation, ScanResult
    _progress = pyqtSignal(int, str, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._generation = 0
        self._pending = 0  # generation of the request not answered yet, 0 if none
        self._requests = {}  # generation -> its request, while not answered
        self._done.connect(self._on_done)
        self._progress.connect(self._on_progress)

    @property
    def busy(self):
        return self._pending != 0

    def reset(self):
        """Forget every request not answered yet: what it would find is out of date."""
        self.cancel()

    def cancel(self):
        """Like reset(); returns the request not answered yet (what was passed as `request`), None if none."""
        request = self._requests.pop(self._pending, None) if self._pending else None
        self._generation += 1
        self._pending = 0
        self._requests.clear()
        return request

    def scan(self, folder, fallback_dir, request=None):
        """Read every photo of `folder` with what was saved of them (models.scan_folder)."""
        def work(generation):
            problems = []
            def progress(count):
                emit_safely(self._progress, generation, folder, count)

            photos = scan_folder(folder, fallback_dir, problems, progress)
            keys = {photo.path: file_key(photo.path) for photo in photos}
            return ScanResult(folder, photos=photos, problems=problems, file_keys=keys)

        self._start(folder, request, work)

    def refresh(self, folder, known, fallback_dir, request=None):
        """Find what changed in `folder` since `known` ({primary path: companions}) was read (models.scan_changes)."""
        known = {path: tuple(companions) for path, companions in known.items()}  # the worker's own copy

        def work(_generation):
            problems = []
            changes = scan_changes(folder, known, fallback_dir, problems)
            keys = {photo.path: file_key(photo.path) for photo in changes.added}
            return ScanResult(folder, changes=changes, problems=problems, file_keys=keys)

        self._start(folder, request, work)

    def shutdown(self):
        self.reset()
        self._pool.clear()
        self._pool.waitForDone()

    def _start(self, folder, request, work):
        self._generation += 1
        generation = self._pending = self._generation
        self._requests = {generation: request}

        def job():
            try:
                result = work(generation)
            except Exception as exc:  # an unreadable folder must not kill the worker
                result = ScanResult(folder, error=getattr(exc, "strerror", None) or str(exc) or type(exc).__name__)
            result.request = request
            emit_safely(self._done, generation, result)

        self._pool.clear()  # a request still waiting is superseded
        self._pool.start(job)

    def _on_done(self, generation, result):
        if generation != self._generation:
            return
        self._pending = 0
        self._requests.clear()
        self.scanned.emit(result)

    def _on_progress(self, generation, folder, count):
        if generation == self._generation:
            self.progressed.emit(folder, count)
