"""Long jobs on the photos' files: exporting, Process Files, importing and the Trash.

`ProcessingMixin` is part of `TamisApp`; it is kept apart because these flows
mostly talk to the file operations of `fileops` rather than to the views.
"""

import errno
import json
import os
import threading
from contextlib import suppress
from html import escape

from PyQt6.QtCore import QEventLoop, Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import QDialog, QProgressDialog

from . import exportsettings
from .constants import KEEP, REJECT, REJECT_DIR
from .editing import export_photo
from .exportsettings import ExportSettings
from .fileops import (
    MoveResult,
    copy_export,
    execute_import,
    execute_moves,
    execute_trash,
    export_record,
    find_card,
    is_jpeg,
    jpeg_file,
    plan_exports,
    plan_moves,
    plan_trash,
    record_path,
    rejected_exports,
    remove_rejected_exports,
    trash_size,
    without_export,
)
from .models import FolderChanges, by_name, relative_key
from .widgets import ExportSettingsDialog, ImportDialog, MessageDialog, ProcessDialog, inform, size_text


def _names(paths, limit=5):
    """`a.jpg, b.jpg and 3 more`, as rich text."""
    names = [escape(os.path.basename(path)) for path in paths]
    extra = f" and {len(names) - limit} more" if len(names) > limit else ""
    return ", ".join(names[:limit]) + extra


class ExportThread(QThread):
    """Renders edited photos to JPEG (or copies unedited ones) off the GUI thread."""

    progressed = pyqtSignal(int)

    def __init__(self, jobs, dest_dir, settings=None, protected=(), parent=None):
        super().__init__(parent)
        # [(path, edit, LUT path, name)]: an unedited JPEG is copied (unless it must be
        # resized), anything else rendered; name None picks a free one
        self.jobs = jobs
        self.dest_dir = dest_dir
        self.settings = settings or ExportSettings()
        # Files never to write over, the photos': only a mistaken destination would get there.
        self.protected = {os.path.normcase(os.path.abspath(path)) for path in protected}
        self.exported = []
        self.sources = {}  # exported path -> the job's path
        self.errors = []
        self.stopped = False  # Stop was pressed before every job was done
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        tables = {}
        for done, (path, edit, lut_path, name) in enumerate(self.jobs):
            if self._stop.is_set():
                self.stopped = True
                break
            self.progressed.emit(done)
            try:
                if name is not None and os.path.normcase(os.path.abspath(os.path.join(self.dest_dir, name))) in self.protected:
                    raise OSError(errno.EEXIST, "a photo has that name in the export folder")
                if edit or not is_jpeg(path) or self.settings.resizes:
                    dest = export_photo(
                        path, edit, lut_path, self.dest_dir, tables,
                        quality=self.settings.quality, name=name, long_edge=self.settings.long_edge,
                    )
                else:
                    dest = copy_export(path, self.dest_dir, name)
                self.exported.append(dest)
                self.sources[dest] = path
            except Exception as exc:  # one bad photo must not stop the others
                self.errors.append(f"{os.path.basename(path)}: {exc}")
        self.progressed.emit(len(self.jobs))


class WorkThread(QThread):
    """Runs `work(progress)` off the GUI thread; `progress(done)` returns False once asked to stop."""

    progressed = pyqtSignal(int)

    def __init__(self, work, parent=None):
        super().__init__(parent)
        self.work = work
        self.result = None
        self.failure = None
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        def progress(done):
            self.progressed.emit(done)
            return not self._stop.is_set()

        try:
            self.result = self.work(progress)
        except Exception as exc:  # an exception escaping run() would abort the process
            self.failure = str(exc) or type(exc).__name__


def run_with_progress(window, thread, title, text, count):
    """Run `thread` behind a modal progress dialog with a Stop button, until it finishes.

    Shown at once: being modal is what keeps marks from changing meanwhile, and
    `window.moving` refuses a folder change. `thread` has `progressed(int)` and `stop()`.
    """
    # What a folder read under way finds would be out of date once files have moved: read again after.
    dropped = window.scanner.cancel()
    progress = QProgressDialog(text, "Stop", 0, count, window)
    progress.setWindowTitle(title)
    progress.setWindowModality(Qt.WindowModality.ApplicationModal)
    progress.setMinimumDuration(0)
    progress.setValue(0)
    thread.progressed.connect(progress.setValue)
    progress.canceled.connect(thread.stop)
    loop = QEventLoop()
    thread.finished.connect(loop.quit)
    window.moving = True
    thread.start()
    loop.exec()
    window.moving = False
    progress.close()
    thread.deleteLater()
    if dropped is not None and dropped[0] == "open":
        _kind, folder, select, keep_state, _revision = dropped
        window.load_folder(folder, select, keep_state)


class ProcessingMixin:
    """Exporting, Process Files, importing and the Trash, for `TamisApp`."""

    # -- exporting ---------------------------------------------------------

    def export_photos(self):
        """Export the target photos, with their edits, as full-size JPEGs in _EXPORTS."""
        self._finish_crop(apply=True)
        rows = self._target_rows()
        if not rows or self.moving:
            return
        self.save_session()
        photos = [self.model.photo(row) for row in rows]
        missing = [photo.name for photo in photos if self._lut_missing(photo.edit)]
        if missing:
            dialog = MessageDialog(
                self,
                "Film simulation not found",
                f"{len(missing)} photo(s) use a film simulation that isn't in the LUT folder, "
                "and would be exported without it:",
                confirm_text="Export anyway",
                details=missing,
            )
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
        if not self._export_folder_ok():
            return
        jobs = [(p.path if p.edit else jpeg_file(p) or p.path, p.edit, self._lut_path(p.edit), None) for p in photos]
        thread = self._run_export(jobs, "Export JPEG")
        if not self._report_export_errors(thread, len(jobs)):
            count = f"{len(thread.exported)} of {len(jobs)}" if thread.stopped else len(thread.exported)
            done = "Stopped: exported" if thread.stopped else "Exported"
            self.toast.show_message(f"{done} {count} photo(s) to {self.export_settings.folder}")

    def _report_export_errors(self, thread, count):
        """Tell the user which of `count` photos the finished ExportThread failed on; returns whether any did."""
        if thread.errors:
            inform(
                self,
                "Some photos were not exported",
                f"Exported {len(thread.exported)} of {count} photo(s) to {self.export_settings.folder}. These failed:",
                details=thread.errors,
            )
        return bool(thread.errors)

    def _export_folder_ok(self):
        """Whether the export folder of the settings can be used here; tells the user if not."""
        problem = self.export_settings.problem(self.folder)
        if problem:
            inform(self, "Choose another export folder", f"{problem} Change it in Options → Export Settings.")
        return problem is None

    def edit_export_settings(self):
        """Open the export settings; returns whether they were changed."""
        def save_profiles(profiles):
            self.export_profiles = profiles
            self.settings.setValue("export_profiles", exportsettings.dump_profiles(profiles))

        dialog = ExportSettingsDialog(self, self.export_settings, self.export_profiles, self.folder, save_profiles)
        if dialog.exec() != QDialog.DialogCode.Accepted or dialog.settings == self.export_settings:
            return False
        self.export_settings = dialog.settings
        self.settings.setValue("export_settings", json.dumps(self.export_settings.to_dict()))
        return True

    def _run_export(self, jobs, title):
        """Run an ExportThread behind a modal progress dialog; returns the finished thread."""
        protected = [path for photo in self.model.photos for path in photo.files]
        thread = ExportThread(jobs, self.export_settings.dest_dir(self.folder), self.export_settings, protected, self)
        run_with_progress(self, thread, title, "Exporting…", len(jobs))
        self._record_exports(thread)
        if thread.exported:
            self.plugins.emit("photos_exported", list(thread.exported))
        return thread

    def _record_exports(self, thread):
        """Remember which photo each export came from, so that rejecting it later can remove the export."""
        if not thread.exported:
            return
        def key(path):
            return os.path.normcase(os.path.abspath(path))

        written = {key(dest) for dest in thread.exported}
        owners = {path: photo for photo in self.model.photos for path in photo.files}
        for photo in self.model.photos:  # a file that was replaced is no longer the export of its former photo
            if photo.exports:
                photo.exports = [record for record in photo.exports if key(record_path(self.folder, record)) not in written]
        for dest in thread.exported:
            photo = owners.get(thread.sources[dest])
            # A rejected photo exported on request stays: the user just asked for that file.
            if photo is not None and photo.status != REJECT:
                with suppress(OSError):  # gone already: nothing to remember
                    photo.exports = [*photo.exports, export_record(dest, self.export_settings.folder)]
        self.save_session()


    def _run_work(self, title, text, count, work):
        """Run `work(progress)` behind a modal progress dialog; returns its result, None if it broke down."""
        thread = WorkThread(work, self)
        run_with_progress(self, thread, title, text, count)
        if thread.failure:
            inform(self, title, escape(thread.failure))
        return thread.result
    # -- importing ----------------------------------------------------------

    def import_photos(self):
        """Copy photos from a memory card to a folder, and to a backup if asked, then open that folder."""
        if self.moving:
            return
        dialog = ImportDialog(
            self, find_card(), self.settings.value("import_destination", ""), self.settings.value("import_backup", "")
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.settings.setValue("import_destination", dialog.destination)
        self.settings.setValue("import_backup", dialog.backup)
        groups, destinations = dialog.groups, dialog.destinations
        self._finish_crop(apply=True)
        self.save_session()
        result = self._run_work(
            "Import photos", "Copying and verifying…", len(groups), lambda progress: execute_import(groups, destinations, progress)
        )
        if result is None:
            return
        if result.errors:
            inform(
                self,
                "Some files were not imported",
                f"Copied {result.copied} file(s). These failed, and their originals are untouched:",
                details=result.errors,
            )
        if os.path.normcase(os.path.abspath(dialog.destination)) == os.path.normcase(os.path.abspath(self.folder or ".")):
            self.refresh(auto=True)  # already open: the new photos are added to it
        elif os.path.isdir(dialog.destination):
            self.load_folder(dialog.destination)
        skipped = f", {result.skipped} already there" if result.skipped else ""
        stopped = "Stopped: " if result.cancelled else ""
        self.toast.show_message(f"{stopped}{result.copied} file(s) copied and verified{skipped}", 4000)

    # -- the Trash ----------------------------------------------------------

    def trash_rejects(self):
        """Move the photos in _REJECTS to the system Trash, with their pairs and sidecars. Nothing is ever deleted."""
        if not self.folder or self.moving:
            return
        self._finish_crop(apply=True)
        self._end_compare()
        self.save_session()
        photos = plan_trash(self.model.photos)
        waiting = sum(1 for photo in self.model.photos if photo.status == REJECT and photo.location != REJECT)
        note = f"{waiting} rejected photo(s) aren't in {REJECT_DIR} yet: Process Files moves them there."
        if not photos:
            self.toast.show_message(note if waiting else f"No rejected photos in {REJECT_DIR}", 4000)
            return
        text = (
            f"<b>{len(photos)}</b> rejected photo(s) in {REJECT_DIR}, <b>{size_text(trash_size(photos))}</b> with their "
            "RAW+JPEG pairs and XMP sidecars, will go to the Trash. They can be put back from there until it is emptied."
        )
        if waiting:
            text += f"<br><br>{note}"
        if MessageDialog(self, "Move to the Trash", text, confirm_text="Move to Trash").exec() != QDialog.DialogCode.Accepted:
            return
        result = self._run_work(
            "Move to the Trash", "Moving to the Trash…", len(photos), lambda progress: execute_trash(photos, progress)
        )
        if result is None:
            return
        if result.trashed:
            self._forget_trashed(set(result.trashed))
        if result.errors:
            inform(
                self,
                "Some photos stayed where they are",
                f"Moved {len(result.trashed)} of {len(photos)} photo(s) to the Trash. These could not be moved:",
                details=result.errors,
            )
        else:
            self.toast.show_message(f"Moved {len(result.trashed)} photo(s) to the Trash")

    def _forget_trashed(self, gone):
        """Photos are identified by path: nothing may keep referring to these."""
        files = [path for photo in self.model.photos if photo.path in gone for path in photo.files]
        self.moved_keys.update(relative_key(self.folder, path) for path in files)
        self._update_photos(FolderChanges(removed=set(gone)))
        self.save_session()
        with suppress(OSError):  # only goes if nothing is left in it
            os.rmdir(os.path.join(self.folder, REJECT_DIR))
        self._watch(self.folder)
        self.plugins.emit("photos_trashed", sorted(gone))

    # -- processing --------------------------------------------------------

    def process_files(self):
        """Move photos to the folder of their mark, then fill the export folder if asked to.

        Edited photos are exported as JPEGs; unedited ones get their JPEG
        (if they have one) copied. Rejected photos are not exported, nor
        unmarked ones when only keeps are, and what was exported of
        rejected photos earlier is removed.
        """
        if not self.folder:
            return
        self._finish_crop(apply=True)
        self._end_compare()
        self.save_session()
        moves = plan_moves(self.folder, self.model.photos)
        # Whatever the scope: the dialog is where it can be changed.
        anything = self._plan_exports(keeps_only=False)
        no_jpeg = [photo.path for photo in without_export(self.model.photos, self.export_scope == "keeps")]
        removable, _changed = rejected_exports(self.model.photos, self.folder)
        if not moves and not anything and not removable:
            note = f" · {len(no_jpeg)} photo(s) have no JPEG to export" if no_jpeg else ""
            self.toast.show_message(f"Everything is already sorted{note}")
            return

        keeps = sum(1 for m in moves if m.status == KEEP)
        rejects = sum(1 for m in moves if m.status == REJECT)
        unmarked = len(moves) - keeps - rejects
        lines = []
        if moves:
            lines.append(f"<b>{len(moves)}</b> photo(s) will be moved:")
        if keeps:
            lines.append(f"&nbsp;&nbsp;• {keeps} → <b>_KEEPS</b>")
        if rejects:
            lines.append(f"&nbsp;&nbsp;• {rejects} → <b>_REJECTS</b>")
        if unmarked:
            lines.append(f"&nbsp;&nbsp;• {unmarked} → folder root (unmarked)")
        if moves:
            lines.append("<br>RAW+JPEG pairs and XMP sidecars move together. Existing files are never overwritten.")

        if anything or removable:
            export_label, export_text, _can = self._describe_exports(self.export_scope)
        else:
            export_label = export_text = None
            if no_jpeg:
                lines.append(f"{len(no_jpeg)} photo(s) have no JPEG and won't be exported: {_names(no_jpeg)}")

        dialog = ProcessDialog(
            self, "<br>".join(lines), export_label, export_text,
            scope=self.export_scope, describe=self._describe_exports, edit_settings=self.edit_export_settings,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        if dialog.export_check is not None and dialog.scope != self.export_scope:
            self.export_scope = dialog.scope
            self.settings.setValue("export_scope", self.export_scope)

        if moves:
            result = self._move_files(moves)
            moved = sum(1 for move in moves if move.src in result.moved)
            if result.errors:
                inform(
                    self,
                    "Some files were not moved",
                    f"Moved {moved} of {len(moves)} photo(s). These failed:",
                    details=result.errors,
                )
            if result.cancelled:
                self.toast.show_message(f"Stopped after moving {moved} photo(s)")
                return
            summary_text = f"Moved {moved} photo(s)"
        else:
            summary_text = "Nothing to move"

        if not dialog.export_requested or not self._export_folder_ok():
            self.toast.show_message(summary_text)
            return

        where = self.export_settings.folder
        # First, since a photo about to be exported may take over the name of a removed export.
        removed, remove_errors = remove_rejected_exports(self.model.photos, self.folder)
        self.save_session()
        if remove_errors:
            inform(
                self,
                "Some exports were not removed",
                "These exports of rejected photos are still there:",
                details=remove_errors,
            )

        # Planned again: the photos may have just moved.
        exports = self._plan_exports()
        jobs = [(job.path, job.edit, self._lut_path(job.edit), job.name) for job in exports]
        thread = self._run_export(jobs, "Process files") if jobs else None
        if thread is None or not self._report_export_errors(thread, len(jobs)):
            done = [summary_text]
            if thread is not None:
                if thread.stopped:
                    done.append(f"stopped after exporting {len(thread.exported)} of {len(jobs)} to {where}")
                else:
                    done.append(f"exported {len(thread.exported)} to {where}")
            if removed:
                done.append(f"removed {len(removed)} export(s) of rejected photos")
            self.toast.show_message(", ".join(done))

    def _describe_exports(self, scope):
        """(label, details, anything to do) of the export part of the Process Files dialog, for a scope."""
        keeps_only = scope == "keeps"
        settings = self.export_settings
        where = escape(settings.folder)
        exports = self._plan_exports(keeps_only)
        no_jpeg = [photo.path for photo in without_export(self.model.photos, keeps_only)]
        removable, changed = rejected_exports(self.model.photos, self.folder)
        edited = sum(1 for job in exports if job.edit)
        if exports:
            label = f"Export {len(exports)} photo(s) to {settings.folder}"
        elif removable:
            label = f"Update {settings.folder}"
        else:
            label = "Nothing to export" + (" (no keeps)" if keeps_only else "")
        details = []
        if edited:
            details.append(f"&nbsp;&nbsp;• {edited} edited → rendered as JPEG")
        if len(exports) - edited:
            how = "rendered as JPEG" if settings.resizes else "JPEG copied"
            details.append(f"&nbsp;&nbsp;• {len(exports) - edited} without edits → {how}")
        if removable:
            details.append(f"&nbsp;&nbsp;• {len(removable)} earlier export(s) of rejected photos → removed")
        if exports:
            details.append(f"Into <b>{where}</b>: {escape(settings.describe())}. Earlier exports of the same photos are replaced.")
        if changed:
            details.append(f"{len(changed)} export(s) of rejected photos changed since and stay: {_names(changed)}")
        if no_jpeg:
            details.append(f"{len(no_jpeg)} photo(s) have no JPEG and won't be exported: {_names(no_jpeg)}")
        missing = [job.path for job in exports if self._lut_missing(job.edit)]
        if missing:
            details.append(
                f"<b>{len(missing)} photo(s) use a film simulation that isn't in the LUT folder "
                f"and would be exported without it:</b> {_names(missing)}"
            )
        problem = settings.problem(self.folder)
        if problem:
            details.append(f"<b>{escape(problem)}</b>")
        return label, "<br>".join(details), bool(exports or removable) and problem is None

    def _plan_exports(self, keeps_only=None):
        """What Process Files exports, for the scope chosen (or `keeps_only`)."""
        if keeps_only is None:
            keeps_only = self.export_scope == "keeps"
        # By name, whatever the order shown: which photo gets `IMG_1 (1).jpg` must not depend on it.
        return plan_exports(
            sorted(self.model.photos, key=by_name), keeps_only, self.export_settings.pattern, os.path.basename(self.folder)
        )

    def _move_files(self, moves):
        """Run the moves behind a modal progress dialog and update the model; returns the MoveResult."""
        result = self._run_work("Process files", "Moving files…", len(moves), lambda progress: execute_moves(moves, progress))
        if result is None:  # broke down, already reported: nothing moved that is known of
            result = MoveResult(cancelled=True)

        # Locations come from the plan: once Stop is pressed the window is
        # usable again while the last file finishes, so marks may have changed.
        locations = {path: move.status for move in moves for path in (move.src, *move.companions)}
        self.model.apply_moves({old: (new, locations[old]) for old, new in result.moved.items()})
        self.loader.rename_paths(result.moved)
        self.editor.rename_paths(result.moved)
        self.edit_thumbnails.rename_paths(result.moved)
        self.xmp_dirty = {result.moved.get(path, path) for path in self.xmp_dirty}
        self.moved_keys.update(relative_key(self.folder, old) for old in result.moved)
        self.undo_stack.clear()  # undo entries refer to the old paths
        self.save_session()
        self._on_statuses_changed([])
        self.analyzer.restart()  # what was running has the old paths; results are kept by file, so little is lost
        self._request_analysis()
        self._watch(self.folder)  # _KEEPS / _REJECTS may have just been created
        if self.current_row >= 0:
            self.header.set_folder(self.folder, self.model.photo(self.current_row).display_name)
        if result.moved:
            self.plugins.emit("files_moved", dict(result.moved))
        return result
