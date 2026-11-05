"""The main window, driven by its real shortcuts and mouse events, on a throwaway folder."""
import json
import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtCore import QEvent, QPoint, QSettings, QSize, Qt  # noqa: E402
from PyQt6.QtGui import QColor, QImage, QPixmap  # noqa: E402
from PyQt6.QtTest import QTest  # noqa: E402
from PyQt6.QtWidgets import QApplication, QDialog  # noqa: E402

from tamis import develop, fileops, loader, xmp  # noqa: E402
from tamis.app import CULL_TAB, GALLERY_TAB, TamisApp  # noqa: E402
from tamis.constants import KEEP, REJECT, SESSION_FILENAME  # noqa: E402
from tamis.fileops import MoveResult, plan_moves  # noqa: E402
from tamis.imaging import image_to_array  # noqa: E402
from tamis.plugins import PhotoInfo, Plugin, PluginApi  # noqa: E402
from tamis.widgets import ImportDialog, MessageDialog, NameDialog, ProcessDialog  # noqa: E402

CTRL = Qt.KeyboardModifier.ControlModifier
PHOTOS = 6


@pytest.fixture(scope="module")
def qapp(tmp_path_factory):
    app = QApplication.instance() or QApplication([])
    # Never touch the settings of the real app (last folder, window position…).
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path_factory.mktemp("settings")))
    return app


@pytest.fixture
def window(qapp, tmp_path, tmp_path_factory):
    for i in range(PHOTOS):
        image = QImage(300, 200, QImage.Format.Format_RGB32)
        image.fill(QColor(40 * i, 80, 120))
        assert image.save(str(tmp_path / f"IMG_{i}.jpg"))
    QSettings().clear()  # before the window reads them
    # Never the real app's plugins or caches.
    win = TamisApp(
        plugins_dir=str(tmp_path_factory.mktemp("plugins")),
        cache_dir=str(tmp_path_factory.mktemp("cache")),
        presets_file=str(tmp_path_factory.mktemp("presets") / "presets.json"),
    )
    win.settings.clear()
    win.resize(1200, 800)
    win.show()
    assert QTest.qWaitForWindowActive(win)
    win.load_folder(str(tmp_path))
    wait_for_image(win)
    yield win
    win.close()
    win.deleteLater()


def wait_for_image(win):
    assert wait_until(lambda: win.image_view._pixmap is not None)


def wait_until(condition, timeout=5000):
    for _ in range(timeout // 10):
        if condition():
            return True
        QTest.qWait(10)
    return condition()


def press(win, key, modifiers=Qt.KeyboardModifier.NoModifier):
    QTest.keyClick(win, key, modifiers)


def statuses(win, **marks):
    win.model.set_status([int(row) for row in marks.get("keep", [])], KEEP)
    win.model.set_status([int(row) for row in marks.get("reject", [])], REJECT)


def drag(view, start, end):
    QTest.mousePress(view, Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(view, end)
    QTest.mouseRelease(view, Qt.MouseButton.LeftButton, pos=end)


# -- copy / paste --------------------------------------------------------------

def test_copy_paste_shortcuts_carry_the_edit_over(window):
    window.model.set_field([0], "edit", {"contrast": 30})
    press(window, Qt.Key.Key_C, CTRL)
    assert window.edit_clipboard == {"contrast": 30}
    window.go_to(3)
    press(window, Qt.Key.Key_V, CTRL)
    assert window.model.photo(3).edit == {"contrast": 30}
    assert window.edit_panel.edit() == {"contrast": 30}


def test_copy_paste_shortcuts_work_in_the_gallery_on_the_selection(window):
    window.model.set_field([0], "edit", {"contrast": 30})
    press(window, Qt.Key.Key_C, CTRL)
    window.tabs.setCurrentIndex(GALLERY_TAB)
    selection = window.grid.selectionModel()
    selection.clearSelection()
    for row in (2, 4):
        selection.select(window.proxy.index(row, 0), selection.SelectionFlag.Select)
    press(window.grid, Qt.Key.Key_V, CTRL)
    assert [window.model.photo(row).edit for row in (1, 2, 4)] == [{}, {"contrast": 30}, {"contrast": 30}]


def test_pasting_keeps_each_photo_s_own_crop_and_is_one_undo(window):
    crop = [0.1, 0.1, 0.9, 0.9]
    window.model.set_field([0], "edit", {"contrast": 30, "crop": crop})
    window.model.set_field([1], "edit", {"crop": [0.2, 0.2, 0.8, 0.8], "grain": 5})
    window.copy_edit()
    assert window.edit_clipboard == {"contrast": 30}  # the source's crop isn't copied

    window.tabs.setCurrentIndex(GALLERY_TAB)
    selection = window.grid.selectionModel()
    selection.clearSelection()
    for row in (1, 2):
        selection.select(window.proxy.index(row, 0), selection.SelectionFlag.Select)
    window.paste_edit()
    assert window.model.photo(1).edit == {"contrast": 30, "crop": [0.2, 0.2, 0.8, 0.8]}
    assert window.model.photo(2).edit == {"contrast": 30}

    window.undo()
    assert window.model.photo(1).edit == {"crop": [0.2, 0.2, 0.8, 0.8], "grain": 5}
    assert window.model.photo(2).edit == {}


# -- keeps only ----------------------------------------------------------------

def test_keeps_only_needs_a_keep(window):
    press(window, Qt.Key.Key_K)
    assert not window.keeps_only
    assert not window.keeps_button.isChecked()


def test_keeps_only_filters_the_filmstrip_and_navigation(window):
    statuses(window, keep=[1, 3, 4], reject=[2])
    press(window, Qt.Key.Key_K)
    assert window.keeps_only and window.keeps_button.isChecked()
    assert window.filmstrip.model().rowCount() == 3
    assert window.current_row == 1  # photo 0 isn't a keep: moved on to the next one
    assert window.header.lbl_counter.text() == "1 / 3 keeps"

    press(window, Qt.Key.Key_Right)
    assert window.current_row == 3
    press(window, Qt.Key.Key_Right)
    assert window.current_row == 4
    assert window.header.lbl_counter.text() == "3 / 3 keeps"
    press(window, Qt.Key.Key_Right)
    assert window.current_row == 4  # no further
    press(window, Qt.Key.Key_Home)
    assert window.current_row == 1
    press(window, Qt.Key.Key_End)
    assert window.current_row == 4
    press(window, Qt.Key.Key_Left)
    assert window.current_row == 3

    press(window, Qt.Key.Key_K)
    assert not window.keeps_only
    assert window.filmstrip.model().rowCount() == PHOTOS
    assert window.current_row == 3
    assert window.header.lbl_counter.text() == f"4 / {PHOTOS}"


def test_filmstrip_click_opens_the_right_photo_when_filtered(window):
    statuses(window, keep=[1, 3, 4])
    window.toggle_keeps_only()
    window.filmstrip.clicked.emit(window.strip_proxy.index(2, 0))
    assert window.current_row == 4


def test_rejecting_a_keep_moves_to_exactly_the_next_keep(window):
    statuses(window, keep=[1, 3, 4])
    window.toggle_keeps_only()
    assert window.current_row == 1
    press(window, Qt.Key.Key_X)
    assert window.model.photo(1).status == REJECT
    assert window.current_row == 3
    assert window.header.lbl_counter.text() == "1 / 2 keeps"


def test_rejecting_the_last_keep_steps_back_then_ends_the_filter(window):
    statuses(window, keep=[1, 3])
    window.toggle_keeps_only()
    window.go_to(3)
    press(window, Qt.Key.Key_X)
    assert window.current_row == 1
    assert window.keeps_only
    press(window, Qt.Key.Key_X)
    assert not window.keeps_only
    assert window.filmstrip.model().rowCount() == PHOTOS


def test_undo_when_filtered_brings_the_keep_back(window):
    statuses(window, keep=[1, 3])
    window.toggle_keeps_only()
    press(window, Qt.Key.Key_X)
    assert window.current_row == 3
    press(window, Qt.Key.Key_Z, CTRL)
    assert window.model.photo(1).status == KEEP
    assert window.current_row == 1
    assert window.strip_proxy.rowCount() == 2


def test_picking_another_photo_in_the_gallery_ends_the_filter(window):
    statuses(window, keep=[1, 3])
    window.toggle_keeps_only()
    window.tabs.setCurrentIndex(GALLERY_TAB)
    assert not window.keeps_button.isVisible()
    window._on_grid_activated(window.proxy.index(5, 0))
    assert window.tabs.currentIndex() == CULL_TAB
    assert window.current_row == 5
    assert not window.keeps_only


def test_next_unmarked_ends_the_filter(window):
    statuses(window, keep=[1, 3])
    window.toggle_keeps_only()
    press(window, Qt.Key.Key_N)
    assert not window.keeps_only
    assert window.model.photo(window.current_row).status is None


def test_compare_when_filtered_uses_the_next_keep(window):
    statuses(window, keep=[1, 3])
    window.toggle_keeps_only()
    press(window, Qt.Key.Key_C)
    assert window.compare_rows == [1, 3]
    press(window, Qt.Key.Key_X)  # stays in Compare
    assert window.compare_rows == [1, 3]
    press(window, Qt.Key.Key_C)  # leaving it notices photo 1 is no longer a keep
    assert window.compare_rows == []
    assert window.current_row == 3


def test_opening_another_folder_ends_the_filter(window, tmp_path_factory):
    statuses(window, keep=[1])
    window.toggle_keeps_only()
    other = tmp_path_factory.mktemp("other")
    image = QImage(30, 20, QImage.Format.Format_RGB32)
    image.fill(QColor("red"))
    assert image.save(str(other / "A.jpg"))
    window.load_folder(str(other))
    assert not window.keeps_only
    assert window.current_row == 0
    assert window.strip_proxy.rowCount() == 1


def test_keeps_only_survives_a_refresh(window, tmp_path):
    statuses(window, keep=[1, 3])
    window.toggle_keeps_only()
    window.go_to(3)
    image = QImage(30, 20, QImage.Format.Format_RGB32)
    image.fill(QColor("red"))
    assert image.save(str(tmp_path / "IMG_9.jpg"))
    window.save_session()
    window.refresh()
    assert window.keeps_only
    assert window.model.photo(window.current_row).name == "IMG_3.jpg"
    assert window.strip_proxy.rowCount() == 2


# -- crop ----------------------------------------------------------------------

def frame_point(view, x, y):
    """Widget position of a point given in fractions of the photo."""
    fit = view._fit_rect()
    return QPoint(round(fit.x() + x * fit.width()), round(fit.y() + y * fit.height()))


def test_cropping_by_dragging_a_corner_then_enter(window):
    press(window, Qt.Key.Key_R)
    view = window.image_view
    assert window.cropping and window.edit_panel.isVisible()
    assert view.crop_frame == (0, 0, 1, 1)

    drag(view, frame_point(view, 1, 1), frame_point(view, 0.5, 0.5))
    assert view.crop_frame == pytest.approx((0, 0, 0.5, 0.5), abs=0.01)
    press(window, Qt.Key.Key_Return)

    assert not window.cropping
    crop = window.model.photo(0).edit["crop"]
    assert crop == pytest.approx([0, 0, 0.5, 0.5], abs=0.01)
    # Shown cropped at once, then rendered.
    assert wait_until(lambda: abs(view._pixmap.width() / view._pixmap.height() - 1.5) < 0.05 and view._pixmap.width() <= 151)


def test_escape_cancels_and_undo_removes_the_crop(window):
    press(window, Qt.Key.Key_R)
    view = window.image_view
    drag(view, frame_point(view, 0, 0), frame_point(view, 0.25, 0.25))
    press(window, Qt.Key.Key_Escape)
    assert not window.cropping
    assert window.model.photo(0).edit == {}

    press(window, Qt.Key.Key_R)
    drag(view, frame_point(view, 0, 0), frame_point(view, 0.25, 0.25))
    press(window, Qt.Key.Key_R)
    assert "crop" in window.model.photo(0).edit
    press(window, Qt.Key.Key_Z, CTRL)
    assert window.model.photo(0).edit == {}


def test_crop_tool_shows_the_whole_photo_of_a_cropped_one(window):
    window.model.set_field([0], "edit", {"crop": [0, 0, 0.5, 0.5]})
    view = window.image_view
    assert wait_until(lambda: view._pixmap.width() == 150)
    press(window, Qt.Key.Key_R)
    assert wait_until(lambda: view._pixmap.width() == 300)
    assert view.crop_frame == (0, 0, 0.5, 0.5)
    press(window, Qt.Key.Key_Escape)
    assert wait_until(lambda: view._pixmap.width() == 150)
    assert window.model.photo(0).edit == {"crop": [0, 0, 0.5, 0.5]}


def test_moving_on_applies_the_frame_to_the_photo_it_was_drawn_on(window):
    press(window, Qt.Key.Key_R)
    view = window.image_view
    drag(view, frame_point(view, 1, 1), frame_point(view, 0.5, 0.5))
    press(window, Qt.Key.Key_Right)
    assert not window.cropping
    assert window.current_row == 1
    assert "crop" in window.model.photo(0).edit
    assert window.model.photo(1).edit == {}


def test_a_slider_change_keeps_the_crop(window):
    window.model.set_field([0], "edit", {"crop": [0, 0, 0.5, 0.5]})
    window.go_to(0)
    window.edit_panel._sliders["contrast"][0].setValue(20)
    assert window.model.photo(0).edit == {"crop": [0, 0, 0.5, 0.5], "contrast": 20}


def test_shape_menu_reshapes_the_frame_and_clear_goes_back(window):
    press(window, Qt.Key.Key_R)
    view = window.image_view
    combo = window.edit_panel.crop_ratio
    combo.setCurrentIndex(combo.findText("1:1"))
    left, top, right, bottom = view.crop_frame
    assert (right - left) * 300 == pytest.approx((bottom - top) * 200)

    drag(view, frame_point(view, 1, 1), frame_point(view, 0.9, 0.7))
    left, top, right, bottom = view.crop_frame
    assert (right - left) * 300 == pytest.approx((bottom - top) * 200)

    window.clear_crop()
    assert view.crop_frame == (0, 0, 1, 1)
    assert combo.currentText() == "Free"
    press(window, Qt.Key.Key_Return)
    assert window.model.photo(0).edit == {}


def test_existing_crop_is_not_reshaped_by_a_remembered_shape(window):
    window.model.set_field([0], "edit", {"crop": [0, 0, 0.5, 0.9]})
    combo = window.edit_panel.crop_ratio
    combo.setCurrentIndex(combo.findText("1:1"))
    press(window, Qt.Key.Key_R)
    assert combo.currentText() == "Free"
    assert window.image_view.crop_frame == (0, 0, 0.5, 0.9)


def test_zoom_on_a_cropped_photo_stays_in_the_frame(window):
    window.model.set_field([0], "edit", {"crop": [0, 0, 0.5, 0.5]})
    view = window.image_view
    press(window, Qt.Key.Key_Z)
    assert view.zoomed
    # The full image is cropped too, so the zoom's position means the same thing.
    assert wait_until(lambda: view._pixmap.width() == 150 and view._native.width() == 150)


def test_zoom_is_off_while_cropping(window):
    press(window, Qt.Key.Key_R)
    press(window, Qt.Key.Key_Z)
    assert not window.image_view.zoomed
    QTest.mouseDClick(window.image_view, Qt.MouseButton.LeftButton, pos=frame_point(window.image_view, 0.5, 0.5))
    assert not window.cropping  # a double-click applies the frame


def test_crop_is_saved_with_the_session(window, tmp_path):
    window.model.set_field([2], "edit", {"crop": [0.1, 0.2, 0.3, 0.4]})
    window.save_session()
    window.load_folder(str(tmp_path))
    assert window.model.photo(2).edit == {"crop": [0.1, 0.2, 0.3, 0.4]}


# -- process files -------------------------------------------------------------

def process(win, monkeypatch, export):
    """Run "Process Files" through its real dialog, setting the export box then confirming."""
    def confirm(dialog):
        assert dialog.export_check.isChecked()  # ticked by default
        dialog.export_check.setChecked(export)
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(ProcessDialog, "exec", confirm)
    win.process_files()


def test_process_files_exports_by_default(window, tmp_path, monkeypatch):
    statuses(window, keep=[0])
    process(window, monkeypatch, export=True)
    assert (tmp_path / "_KEEPS" / "IMG_0.jpg").exists()
    assert wait_until(lambda: (tmp_path / "_EXPORTS" / "IMG_0.jpg").exists())


def test_process_files_can_skip_the_exports(window, tmp_path, monkeypatch):
    statuses(window, keep=[0])
    process(window, monkeypatch, export=False)
    assert (tmp_path / "_KEEPS" / "IMG_0.jpg").exists()
    assert not (tmp_path / "_EXPORTS").exists()


def test_process_dialog_needs_something_to_do(qapp):
    with_moves = ProcessDialog(None, "1 photo will be moved", "Export 1 photo(s)", "details")
    with_moves.export_check.setChecked(False)
    assert with_moves.confirm_button.isEnabled()
    assert not with_moves.export_requested

    exports_only = ProcessDialog(None, "", "Export 1 photo(s)", "details")
    assert exports_only.export_requested
    exports_only.export_check.setChecked(False)
    assert not exports_only.confirm_button.isEnabled()

    moves_only = ProcessDialog(None, "1 photo will be moved")
    assert moves_only.export_check is None
    assert not moves_only.export_requested


def process_seeing_dialog(win, monkeypatch, export=True):
    """Like process(); returns the texts of the dialog, moves then exports."""
    seen = []

    def confirm(dialog):
        seen.append(dialog.body.text())
        seen.append(dialog.export_details.text() if dialog.export_check else "")
        if dialog.export_check:
            dialog.export_check.setChecked(export)
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(ProcessDialog, "exec", confirm)
    win.process_files()
    return seen


def test_stop_during_a_move_stops_the_export_even_after_an_error(window, tmp_path, monkeypatch):
    errors = []
    statuses(window, keep=[0])
    monkeypatch.setattr("tamis.app.execute_moves", lambda *_: MoveResult(errors=["IMG_0.jpg: boom"], cancelled=True))
    monkeypatch.setattr("tamis.app.inform", lambda _parent, _title, _text, details=None: errors.extend(details))

    process(window, monkeypatch, export=True)

    assert errors == ["IMG_0.jpg: boom"]
    assert not (tmp_path / "_EXPORTS").exists()


def test_process_dialog_warns_about_a_missing_film_simulation(window, tmp_path, monkeypatch):
    window.model.set_field([0], "edit", {"lut": "gone/film.png"})
    _moves, exports = process_seeing_dialog(window, monkeypatch, export=False)
    assert "1 photo(s) use a film simulation" in exports and "IMG_0.jpg" in exports
    assert not (tmp_path / "_EXPORTS").exists()


def test_process_dialog_has_no_warning_when_the_film_simulation_is_there(window, tmp_path, monkeypatch):
    window.model.set_field([0], "edit", {"lut": "film.png"})
    window.luts = {"film.png": str(tmp_path / "film.png")}
    _moves, exports = process_seeing_dialog(window, monkeypatch, export=False)
    assert "film simulation" not in exports


@pytest.mark.parametrize("answer, exported", [(QDialog.DialogCode.Rejected, False), (QDialog.DialogCode.Accepted, True)])
def test_export_asks_when_a_film_simulation_is_missing(window, tmp_path, monkeypatch, answer, exported):
    seen = []
    window.model.set_field([0], "edit", {"lut": "gone/film.png"})

    def confirm(dialog):
        seen.append(dialog.confirm_button.text())
        return answer

    monkeypatch.setattr(MessageDialog, "exec", confirm)
    window.export_photos()

    assert seen == ["Export anyway"]
    assert (tmp_path / "_EXPORTS" / "IMG_0.jpg").exists() == exported


def test_process_dialog_names_photos_without_a_jpeg(window, tmp_path, monkeypatch):
    image = QImage(30, 20, QImage.Format.Format_RGB32)
    image.fill(QColor(90, 80, 120))
    assert image.save(str(tmp_path / "other.png"))
    window.load_folder(str(tmp_path))

    _moves, exports = process_seeing_dialog(window, monkeypatch, export=False)

    assert "1 photo(s) have no JPEG and won't be exported: other.png" in exports


def test_photos_without_a_jpeg_are_mentioned_when_there_is_nothing_to_export(window, tmp_path, monkeypatch):
    image = QImage(30, 20, QImage.Format.Format_RGB32)
    image.fill(QColor(90, 80, 120))
    for i in range(PHOTOS):
        (tmp_path / f"IMG_{i}.jpg").unlink()
    assert image.save(str(tmp_path / "other.png"))
    window.load_folder(str(tmp_path))

    window.model.set_status([0], KEEP)
    moves, exports = process_seeing_dialog(window, monkeypatch)
    assert "other.png" in moves and exports == ""

    window.process_files()  # now sorted: no dialog
    assert "1 photo(s) have no JPEG" in window.toast.text()


def test_rejecting_an_exported_photo_removes_its_export(window, tmp_path, monkeypatch):
    statuses(window, keep=[0, 1])
    process(window, monkeypatch, export=True)
    exports = tmp_path / "_EXPORTS"
    assert (exports / "IMG_0.jpg").exists() and (exports / "IMG_1.jpg").exists()
    (exports / "mine.jpg").write_text("added by the user")

    window.model.set_status([window.model.row_of(str(tmp_path / "_KEEPS" / "IMG_0.jpg"))], REJECT)
    _moves, details = process_seeing_dialog(window, monkeypatch)

    assert "1 earlier export(s) of rejected photos" in details
    assert not (exports / "IMG_0.jpg").exists()
    assert (exports / "IMG_1.jpg").exists() and (exports / "mine.jpg").exists()


def test_exports_stay_when_the_export_box_is_unticked(window, tmp_path, monkeypatch):
    statuses(window, keep=[0])
    process(window, monkeypatch, export=True)
    window.model.set_status([window.model.row_of(str(tmp_path / "_KEEPS" / "IMG_0.jpg"))], REJECT)

    process_seeing_dialog(window, monkeypatch, export=False)

    assert (tmp_path / "_EXPORTS" / "IMG_0.jpg").exists()


def test_export_records_survive_reopening_the_folder(window, tmp_path, monkeypatch, tmp_path_factory):
    window.export_photos()  # IMG_0, by the shortcut's way: not through Process Files
    assert (tmp_path / "_EXPORTS" / "IMG_0.jpg").exists()

    window.load_folder(str(tmp_path_factory.mktemp("other")))
    window.load_folder(str(tmp_path))
    window.model.set_status([0], REJECT)
    process_seeing_dialog(window, monkeypatch)

    assert not (tmp_path / "_EXPORTS" / "IMG_0.jpg").exists()


def test_export_removed_with_nothing_else_to_do_still_asks(window, tmp_path, monkeypatch):
    statuses(window, reject=list(range(1, PHOTOS)), keep=[0])
    process(window, monkeypatch, export=True)
    window.model.set_status([window.model.row_of(str(tmp_path / "_KEEPS" / "IMG_0.jpg"))], REJECT)
    process(window, monkeypatch, export=False)  # moved, and the export stays
    assert (tmp_path / "_EXPORTS" / "IMG_0.jpg").exists()

    seen = process_seeing_dialog(window, monkeypatch)

    assert "1 earlier export(s)" in seen[1]
    assert os.listdir(tmp_path / "_EXPORTS") == []


def test_photo_taking_over_a_removed_export_s_name_keeps_it(window, tmp_path, monkeypatch):
    (tmp_path / "_REJECTS").mkdir()
    image = QImage(30, 20, QImage.Format.Format_RGB32)
    image.fill(QColor(90, 80, 120))
    assert image.save(str(tmp_path / "_REJECTS" / "IMG_0.jpg"))  # same name, in another folder
    window.load_folder(str(tmp_path))
    first = window.model.row_of(str(tmp_path / "IMG_0.jpg"))
    other = window.model.row_of(str(tmp_path / "_REJECTS" / "IMG_0.jpg"))
    window.model.set_status([other], KEEP)
    process(window, monkeypatch, export=True)  # IMG_0.jpg and IMG_0 (1).jpg

    window.model.set_status([first], REJECT)
    process(window, monkeypatch, export=True)

    assert (tmp_path / "_EXPORTS" / "IMG_0.jpg").exists()
    window.model.set_status([window.model.row_of(str(tmp_path / "_REJECTS" / "IMG_0.jpg"))], None)
    process_seeing_dialog(window, monkeypatch)
    assert (tmp_path / "_EXPORTS" / "IMG_0.jpg").exists()


# -- thumbnails ----------------------------------------------------------------

def failed_thumbnail(win, tmp_path, monkeypatch):
    """A photo whose first decode failed, as when it is still being copied."""
    path = str(tmp_path / "IMG_0.jpg")
    decode = loader._decode
    monkeypatch.setattr(loader, "_decode", lambda *_: (QImage(), QSize(), "still copying"))
    monkeypatch.setattr(win.loader, "_disk_thumbnails", None)  # it was never read whole, so it can't be in there
    win.loader.reset()
    assert win.loader.thumbnail(path) is None
    assert wait_until(lambda: win.loader.thumbnail_error(path))
    monkeypatch.setattr(loader, "_decode", decode)
    return path


def test_failed_thumbnail_is_not_retried_while_the_file_is_the_same(window, tmp_path, monkeypatch):
    path = failed_thumbnail(window, tmp_path, monkeypatch)
    window.loader.thumbnail(path)
    QTest.qWait(100)
    assert window.loader.thumbnail(path) is None and window.loader.thumbnail_error(path)


def test_failed_thumbnail_is_retried_when_the_file_changes(window, tmp_path, monkeypatch):
    path = failed_thumbnail(window, tmp_path, monkeypatch)
    with open(path, "ab") as file:
        file.write(b"the rest of the file")

    assert wait_until(lambda: window.loader.thumbnail(path) is not None)
    assert not window.loader.thumbnail_error(path)


def test_failed_thumbnail_is_retried_by_a_refresh(window, tmp_path, monkeypatch):
    path = failed_thumbnail(window, tmp_path, monkeypatch)
    window.refresh()
    assert wait_until(lambda: window.loader.thumbnail(path) is not None)
    assert not window.loader.thumbnail_error(path)


def test_failed_thumbnail_follows_a_moved_file(window, tmp_path, monkeypatch):
    path = failed_thumbnail(window, tmp_path, monkeypatch)
    new_path = str(tmp_path / "moved.jpg")
    os.rename(path, new_path)
    window.loader.rename_paths({path: new_path})
    assert window.loader.thumbnail_error(new_path) and not window.loader.thumbnail_error(path)


# -- plugins -------------------------------------------------------------------

def load_plugin(win, code, name="sample"):
    """Load `code` as a plugin of `win`; returns its Plugin."""
    directory = win.plugins.directory
    with open(os.path.join(directory, f"{name}.py"), "w", encoding="utf-8") as file:
        file.write(code)
    known = len(win.plugins.plugins)
    win.plugins.load()
    return next(plugin for plugin in win.plugins.plugins[known:] if plugin.id == name)


def test_plugin_action_runs_from_its_shortcut_and_the_menu(window):
    plugin = load_plugin(window, """
NAME = "Sample"
def register(tamis):
    tamis.add_action("Keep it", lambda: tamis.set_status(tamis.current_photo(), tamis.KEEP), shortcut="Ctrl+Shift+J")
""")
    assert plugin.loaded and plugin.name == "Sample"
    press(window, Qt.Key.Key_J, CTRL | Qt.KeyboardModifier.ShiftModifier)
    assert window.model.photo(0).status == KEEP

    window.plugins_button.menu().aboutToShow.emit()
    assert plugin.actions[0] in window.plugins_button.menu().actions()
    window.go_to(1)
    plugin.actions[0].trigger()
    assert window.model.photo(1).status == KEEP


def test_plugin_changes_can_be_undone_in_one_step(window):
    captured = {}
    window.plugin_test_capture = captured
    load_plugin(window, """
def register(tamis):
    tamis.window.plugin_test_capture["api"] = tamis
""")
    api = captured["api"]
    photos = api.photos()
    api.set_status(photos[:3], api.REJECT)
    api.set_rating(photos[0].path, 4)
    api.set_label(photos[1], "Red")
    assert window.model.photo(0).rating == 4
    assert window.model.photo(1).label == "Red"
    press(window, Qt.Key.Key_Z, CTRL)
    press(window, Qt.Key.Key_Z, CTRL)
    assert window.model.photo(0).rating == 0 and window.model.photo(1).label is None
    assert window.model.counts()[1] == 3
    press(window, Qt.Key.Key_Z, CTRL)
    assert window.model.counts()[1] == 0


def test_plugin_cannot_corrupt_photos_through_what_it_reads(window):
    window.model.set_field([0], "edit", {"crop": [0.1, 0.1, 0.9, 0.9]})
    photo = PhotoInfo.of(window.model.photo(0))
    photo.edit["crop"][0] = 0.5
    assert window.model.photo(0).edit["crop"][0] == 0.1
    with pytest.raises(AttributeError):
        photo.status = KEEP


def test_plugin_set_edit_is_cleaned_and_values_are_checked(window):
    captured = {}
    window.plugin_test_capture = captured
    load_plugin(window, """
def register(tamis):
    tamis.window.plugin_test_capture["api"] = tamis
""")
    api = captured["api"]
    api.set_edit(api.current_photo(), {"contrast": 30, "nonsense": 1})
    assert window.model.photo(0).edit == {"contrast": 30}
    assert window.edit_panel.edit() == {"contrast": 30}
    for call, value in ((api.set_status, "maybe"), (api.set_rating, 9), (api.set_rating, True), (api.set_label, "Mauve")):
        with pytest.raises(ValueError):
            call(api.current_photo(), value)


def test_plugin_events(window):
    seen = []
    window.plugin_test_capture = seen
    load_plugin(window, """
def register(tamis):
    log = tamis.window.plugin_test_capture
    tamis.on("photo_changed", lambda photo: log.append(("photo", photo.name)))
    tamis.on("photos_edited", lambda photos: log.append(("edited", [p.status for p in photos])))
    tamis.on("files_moved", lambda moved: log.append(("moved", len(moved))))
    tamis.on("folder_opened", lambda folder: log.append(("opened", folder)))
    tamis.on("folder_refreshed", lambda folder: log.append(("refreshed", folder)))
    tamis.on("closing", lambda: log.append(("closing",)))
""")
    press(window, Qt.Key.Key_Right)
    assert seen == [("photo", "IMG_1.jpg")]
    seen.clear()

    press(window, Qt.Key.Key_U)  # already unmarked: no change, and no move to another photo
    assert seen == []

    press(window, Qt.Key.Key_P)
    assert seen == [("edited", [KEEP]), ("photo", "IMG_2.jpg")]
    seen.clear()

    press(window, Qt.Key.Key_Z, CTRL)
    assert seen == [("edited", [None]), ("photo", "IMG_1.jpg")]  # undo goes back to the photo
    seen.clear()

    window.load_folder(window.folder)
    assert seen[-1] == ("opened", window.folder)
    assert not any(entry[0] == "edited" for entry in seen)  # loading photos is not editing them
    seen.clear()

    window.load_folder(window.folder, keep_state=True)
    assert seen[-1] == ("refreshed", window.folder)
    seen.clear()

    window.model.set_status([0], KEEP)
    seen.clear()
    moves = plan_moves(window.folder, window.model.photos)
    window._move_files(moves)
    assert ("moved", 1) in seen
    seen.clear()

    window.close()
    assert seen == [("closing",)]


def test_plugin_failures_never_reach_qt(window):
    plugin = load_plugin(window, """
def boom(*args):
    raise RuntimeError("kaboom")

def register(tamis):
    tamis.add_action("Boom", boom)
    tamis.on("photo_changed", boom)
""")
    assert plugin.loaded
    plugin.actions[0].trigger()
    assert window.toast.isVisible() and "kaboom" in window.toast.text()
    press(window, Qt.Key.Key_Right)
    press(window, Qt.Key.Key_Right)
    assert window.current_row == 2  # the app carried on
    assert len(plugin.problems) == 1  # the same failure is said once
    assert "kaboom" in window.plugins.problems()[0]


@pytest.mark.parametrize("code, expected", [
    ("def (", "SyntaxError"),
    ("raise ValueError('at import')", "at import"),
    ("import sys\nsys.exit(3)", "SystemExit"),
    ("NAME = 'No entry point'", "no register"),
    ("def register(tamis):\n    tamis.on('nope', print)", "Unknown event"),
])
def test_plugin_that_fails_to_load_is_reported_and_inactive(window, code, expected):
    plugin = load_plugin(window, code)
    assert not plugin.loaded
    assert expected in plugin.problems[0]
    assert not window.plugins.active


def test_plugin_failing_midway_leaves_nothing_behind(window):
    plugin = load_plugin(window, """
def register(tamis):
    tamis.add_action("Half done", print, shortcut="Ctrl+Shift+J")
    tamis.on("photo_changed", print)
    raise RuntimeError("stop")
""")
    assert not plugin.loaded and not plugin.actions
    assert not window.plugins._handlers["photo_changed"]
    assert "Ctrl+Shift+J" not in window.plugins._taken_shortcuts()


@pytest.mark.parametrize("shortcut, problem", [
    ("P", "already taken"),  # a built-in one
    ("Ctrl+Z", "already taken"),  # a standard key
    ("Ctrl+Nope", "Invalid shortcut"),
])
def test_plugin_shortcut_cannot_break_a_builtin_one(window, shortcut, problem):
    plugin = load_plugin(window, f"""
def register(tamis):
    tamis.add_action("Mine", print, shortcut={shortcut!r})
""")
    assert plugin.loaded and plugin.actions[0].shortcut().isEmpty()
    assert problem in plugin.problems[0]
    press(window, Qt.Key.Key_P)
    assert window.model.photo(0).status == KEEP


def test_plugin_shortcut_conflict_between_plugins(window):
    load_plugin(window, "def register(tamis):\n    tamis.add_action('A', print, shortcut='Ctrl+Shift+J')", name="a")
    second = load_plugin(window, "def register(tamis):\n    tamis.add_action('B', print, shortcut='Ctrl+Shift+J')", name="b")
    assert second.actions[0].shortcut().isEmpty()
    assert "already taken" in second.problems[0]


def test_disabled_plugin_is_not_run(window):
    plugin = load_plugin(window, "def register(tamis):\n    pass")
    window.plugins._set_enabled(plugin, False)
    window.plugins.plugins.clear()
    window.plugins.load()
    assert not window.plugins.plugins[0].loaded and not window.plugins.plugins[0].enabled

    window.plugins._set_enabled(window.plugins.plugins[0], True)
    window.plugins.plugins.clear()
    window.plugins.load()
    assert window.plugins.plugins[0].loaded


def test_plugin_settings_keep_their_type_and_stay_separate(window):
    captured = {}
    window.plugin_test_capture = captured
    for name in ("a", "b"):
        load_plugin(window, f"""
def register(tamis):
    tamis.window.plugin_test_capture["{name}"] = tamis
""", name=name)
    captured["a"].set_setting("stars", 4)
    captured["a"].set_setting("names", ["x"])
    assert captured["a"].get_setting("stars") == 4
    assert captured["a"].get_setting("names") == ["x"]
    assert captured["b"].get_setting("stars", "none") == "none"


def test_example_plugin_works(window):
    example = os.path.join(os.path.dirname(__file__), "..", "examples", "plugins", "keeps_list.py")
    window.plugins.directory = os.path.dirname(example)
    window.plugins.load()
    plugin = window.plugins.plugins[-1]
    assert plugin.loaded and not plugin.problems

    window.model.set_field([1, 2], "rating", 5)
    plugin.actions[1].trigger()
    assert [window.model.photo(row).status for row in (0, 1, 2)] == [None, KEEP, KEEP]
    plugin.actions[0].trigger()
    assert QApplication.clipboard().text() == "IMG_1.jpg\nIMG_2.jpg"


# -- exporting -----------------------------------------------------------------

def test_export_shortcut_copies_an_unedited_jpeg(window, tmp_path):
    source = (tmp_path / "IMG_0.jpg").read_bytes()
    press(window, Qt.Key.Key_E, CTRL | Qt.KeyboardModifier.ShiftModifier)
    assert (tmp_path / "_EXPORTS" / "IMG_0.jpg").read_bytes() == source

    window.export_photos()  # (offscreen, no window is active again after the progress dialog: no more shortcuts)
    assert (tmp_path / "_EXPORTS" / "IMG_0 (1).jpg").read_bytes() == source


def test_export_renders_an_edited_photo(window, tmp_path):
    window.model.set_field([0], "edit", {"exposure": 1.0})
    window.export_photos()
    exported = QImage(str(tmp_path / "_EXPORTS" / "IMG_0.jpg"))
    assert not exported.isNull()
    assert (tmp_path / "_EXPORTS" / "IMG_0.jpg").read_bytes() != (tmp_path / "IMG_0.jpg").read_bytes()


def test_export_renders_an_unedited_photo_without_a_jpeg(window, tmp_path):
    image = QImage(30, 20, QImage.Format.Format_RGB32)
    image.fill(QColor(90, 80, 120))
    assert image.save(str(tmp_path / "other.png"))
    window.load_folder(str(tmp_path), select="other.png")

    window.export_photos()

    assert os.listdir(tmp_path / "_EXPORTS") == ["other.jpg"]
    assert QImage(str(tmp_path / "_EXPORTS" / "other.jpg")).size() == image.size()


def test_export_uses_the_jpeg_of_an_unedited_pair(window, tmp_path):
    (tmp_path / "IMG_0.CR2").write_bytes(b"not developed: the JPEG is what gets copied")
    window.load_folder(str(tmp_path), select="IMG_0.CR2")

    window.export_photos()

    assert (tmp_path / "_EXPORTS" / "IMG_0.jpg").read_bytes() == (tmp_path / "IMG_0.jpg").read_bytes()


# -- sessions ------------------------------------------------------------------

def stored_session(tmp_path):
    return json.loads((tmp_path / SESSION_FILENAME).read_text())


def test_moved_photos_leave_no_entry_behind(window, tmp_path, monkeypatch):
    statuses(window, keep=[0])
    process(window, monkeypatch, export=False)
    window.model.set_status([window.model.row_of(str(tmp_path / "_KEEPS" / "IMG_0.jpg"))], None)
    process(window, monkeypatch, export=False)

    window.save_session()

    assert "_KEEPS/IMG_0.jpg" not in stored_session(tmp_path)
    assert (tmp_path / "IMG_0.jpg").exists()


def test_photo_taken_out_of_the_folder_gets_its_edit_back(window, tmp_path):
    window.model.set_field([1], "edit", {"contrast": 30})
    window.save_session()

    (tmp_path / "IMG_1.jpg").rename(tmp_path.parent / f"{tmp_path.name}_IMG_1.jpg")
    window.refresh()
    window.model.set_status([0], KEEP)
    window.save_session()
    (tmp_path.parent / f"{tmp_path.name}_IMG_1.jpg").rename(tmp_path / "IMG_1.jpg")
    window.refresh()

    assert window.model.photo(1).edit == {"contrast": 30}


def test_damaged_session_is_reported_once_and_kept(window, tmp_path, monkeypatch):
    shown = []
    monkeypatch.setattr("tamis.app.inform", lambda _parent, title, _text, details=None: shown.append((title, details)))
    (tmp_path / SESSION_FILENAME).write_text("{not json")

    window.load_folder(str(tmp_path))

    kept = tmp_path / "tamis_session.corrupt.json"
    assert wait_until(lambda: shown)
    assert shown == [("Saved marks could not be read", [str(kept)])]
    assert kept.read_text() == "{not json"


def test_rating_that_cannot_reach_the_sidecar_survives_and_is_written_later(window, tmp_path, monkeypatch):
    sidecar = tmp_path / "IMG_0.jpg.xmp"
    sidecar.write_text(xmp.update(xmp._TEMPLATE, 2, None))
    window.load_folder(str(tmp_path))
    real_write = xmp.write

    def fail(*_args):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(xmp, "write", fail)
    press(window, Qt.Key.Key_5, CTRL)
    window.save_session()
    assert window.model.photo(0).xmp_pending
    assert stored_session(tmp_path)["IMG_0.jpg"]["xmp_pending"] is True

    window.load_folder(str(tmp_path))  # still failing: the session's value is what counts
    assert window.model.photo(0).rating == 5

    monkeypatch.setattr(xmp, "write", real_write)
    window.save_session()  # tried again without any new change
    assert not window.model.photo(0).xmp_pending
    assert xmp.parse(sidecar.read_text())[0] == 5
    assert "xmp_pending" not in stored_session(tmp_path)["IMG_0.jpg"]


def test_rating_still_waiting_for_its_sidecar_follows_a_move(window, tmp_path, monkeypatch):
    (tmp_path / "IMG_0.jpg.xmp").write_text(xmp.update(xmp._TEMPLATE, 2, None))
    window.load_folder(str(tmp_path))
    real_write = xmp.write

    def fail(*_args):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(xmp, "write", fail)
    press(window, Qt.Key.Key_5, CTRL)
    statuses(window, keep=[0])
    process(window, monkeypatch, export=False)
    moved = str(tmp_path / "_KEEPS" / "IMG_0.jpg")
    assert window.xmp_dirty == {moved}

    monkeypatch.setattr(xmp, "write", real_write)
    window.save_session()
    assert xmp.parse((tmp_path / "_KEEPS" / "IMG_0.jpg.xmp").read_text())[0] == 5


# -- filters by stars and labels -------------------------------------------------

def shown_names(win):
    return [win.proxy.index(row, 0).data(Qt.ItemDataRole.DisplayRole) for row in range(win.proxy.rowCount())]


def test_rating_and_label_filters_combine_with_the_status_filter(window):
    window.model.set_field([0, 1], "rating", 4)
    window.model.set_field([2], "rating", 2)
    window.model.set_field([0, 2], "label", "Green")
    statuses(window, keep=[0, 2], reject=[1])

    window.gallery_toolbar.rating_combo.setCurrentIndex(3)  # 3 stars or more
    assert shown_names(window) == ["IMG_0.jpg", "IMG_1.jpg"]
    window.gallery_toolbar.label_buttons["Green"].click()
    assert shown_names(window) == ["IMG_0.jpg"]
    window.gallery_toolbar.rating_combo.setCurrentIndex(0)
    assert shown_names(window) == ["IMG_0.jpg", "IMG_2.jpg"]
    window.gallery_toolbar.label_buttons["Blue"].click()  # several labels: any of them
    window.model.set_field([3], "label", "Blue")
    assert shown_names(window) == ["IMG_0.jpg", "IMG_2.jpg", "IMG_3.jpg"]
    window.gallery_toolbar._buttons[KEEP][0].click()
    assert shown_names(window) == ["IMG_0.jpg", "IMG_2.jpg"]


def test_rating_and_label_changes_refilter_live(window):
    window.gallery_toolbar.rating_combo.setCurrentIndex(1)
    assert window.proxy.rowCount() == 0
    window.model.set_field([1], "rating", 1)
    assert shown_names(window) == ["IMG_1.jpg"]
    window.model.set_field([1], "rating", 0)
    assert window.proxy.rowCount() == 0

    window.gallery_toolbar.rating_combo.setCurrentIndex(0)
    window.gallery_toolbar.label_buttons["Red"].click()
    window.model.set_field([4], "label", "Red")
    assert shown_names(window) == ["IMG_4.jpg"]
    window.undo_stack.append([(window.model.photo(4).path, "label", None)])
    window.undo()
    assert window.proxy.rowCount() == 0


def test_removing_a_star_under_the_filter_selects_the_next_photo(window):
    window.model.set_field([1, 2, 3], "rating", 3)
    window.gallery_toolbar.rating_combo.setCurrentIndex(3)
    window.tabs.setCurrentIndex(GALLERY_TAB)
    window.grid.setCurrentIndex(window.proxy.index(0, 0))
    window.rate(0)
    assert shown_names(window) == ["IMG_2.jpg", "IMG_3.jpg"]
    assert window.model.photo(window.current_row).name == "IMG_2.jpg"


# -- sorting ---------------------------------------------------------------------

def known_times(win, times):
    """Capture times by row, as the analysis would have found them."""
    for row, seconds in times.items():
        win.model.photo(row).capture_time = seconds


def test_sorting_by_date_stays_on_the_same_photo_and_is_remembered(window):
    known_times(window, {row: 100.0 - row for row in range(PHOTOS)})
    window.go_to(1)
    path = window.model.photo(1).path

    window.gallery_toolbar.sort_combo.setCurrentIndex(1)
    assert [p.name for p in window.model.photos] == [f"IMG_{i}.jpg" for i in reversed(range(PHOTOS))]
    assert window.model.photo(window.current_row).path == path
    assert window.current_row == 4
    assert window.header.lbl_counter.text() == "5 / 6"
    assert window.strip_proxy.index(4, 0).data(Qt.ItemDataRole.DisplayRole) == "IMG_1.jpg"
    assert window.settings.value("sort_order") == "date"

    press(window, Qt.Key.Key_Right)
    assert window.model.photo(window.current_row).name == "IMG_0.jpg"
    window.gallery_toolbar.sort_combo.setCurrentIndex(0)
    assert window.model.photo(0).name == "IMG_0.jpg"


def test_sorting_keeps_the_gallery_selection_on_its_photos(window):
    known_times(window, {row: 100.0 - row for row in range(PHOTOS)})
    window.tabs.setCurrentIndex(GALLERY_TAB)
    selection = window.grid.selectionModel()
    selection.clearSelection()
    for row in (0, 1):
        selection.select(window.proxy.index(row, 0), selection.SelectionFlag.Select)
    window.set_sort_order("date")
    selected = {index.data(Qt.ItemDataRole.DisplayRole) for index in selection.selectedIndexes()}
    assert selected == {"IMG_0.jpg", "IMG_1.jpg"}


def test_sorting_by_date_reads_the_dates_in_the_background(window, tmp_path):
    for i, name in enumerate(["IMG_0", "IMG_1", "IMG_2", "IMG_3", "IMG_4", "IMG_5"]):
        os.utime(tmp_path / f"{name}.jpg", (2_000_000_000 - i * 100, 2_000_000_000 - i * 100))
    window.set_sort_order("date")
    assert window.stats.lbl_analysis.text().startswith("Analysing")
    assert wait_until(lambda: window.model.photo(0).name == "IMG_5.jpg")
    assert window.stats.lbl_analysis.text() == ""
    assert window.gallery_toolbar.sort_combo.currentData() == "date"


def test_the_sort_order_applies_to_the_next_folder_opened(window, tmp_path):
    window.set_sort_order("date")
    assert wait_until(lambda: not window.analyzer._pending)
    os.utime(tmp_path / "IMG_3.jpg", (1_000_000_000, 1_000_000_000))
    window.refresh()
    window.load_folder(str(tmp_path), keep_state=True)
    assert wait_until(lambda: window.model.photo(0).name == "IMG_3.jpg")


def test_exports_are_named_the_same_whatever_the_order_shown(window, tmp_path):
    window.model.set_field([0], "edit", {"contrast": 10})
    by_name_jobs = [job.path for job in window._plan_exports()]
    known_times(window, {row: 100.0 - row for row in range(PHOTOS)})
    window.set_sort_order("date")
    assert [job.path for job in window._plan_exports()] == by_name_jobs


# -- bursts ----------------------------------------------------------------------

def test_bursts_group_photos_and_keep_one_rejects_the_others(window):
    known_times(window, {0: 100.0, 1: 100.5, 2: 101.0, 3: 500.0, 4: 900.0, 5: 900.4})
    press(window, Qt.Key.Key_B)
    assert window.show_bursts and window.gallery_toolbar.bursts_button.isChecked()
    assert [p.burst.size if p.burst else 0 for p in window.model.photos] == [3, 3, 3, 0, 2, 2]

    window.go_to(1)
    QTest.keyClick(window, Qt.Key.Key_P, Qt.KeyboardModifier.ShiftModifier)
    assert [window.model.photo(row).status for row in range(4)] == [REJECT, KEEP, REJECT, None]
    assert window.current_row == 3  # past the burst

    window.undo()  # a single step
    assert [window.model.photo(row).status for row in range(3)] == [None, None, None]


def test_keep_one_needs_a_burst(window):
    press(window, Qt.Key.Key_P, Qt.KeyboardModifier.ShiftModifier)
    assert window.toast.text() == "Show bursts first (B)"
    window.set_bursts(True)
    wait_until(lambda: not window.analyzer._pending)
    for photo in window.model.photos:
        photo.burst = None
    window.go_to(0)
    press(window, Qt.Key.Key_P, Qt.KeyboardModifier.ShiftModifier)
    assert window.model.photo(0).status is None


def test_bursts_are_scored_to_find_their_sharpest_frame(window):
    window.set_bursts(True)  # (files written just now: all one burst, going by their dates)
    assert wait_until(lambda: not window.analyzer._pending and all(p.sharpness is not None for p in window.model.photos))
    window._show_analysis()
    assert all(photo.status is None for photo in window.model.photos)  # only ever a suggestion


def test_bursts_off_clears_the_groups(window):
    known_times(window, {0: 100.0, 1: 100.5})
    window.set_bursts(True)
    assert window.model.photo(0).burst is not None
    window.set_bursts(False)
    assert window.model.photo(0).burst is None


# -- sharpness -------------------------------------------------------------------

def test_focus_filter_and_sharpness_sort_follow_the_scores(window):
    scores = {0: 100.0, 1: 5.0, 2: 90.0, 3: 110.0, 4: 80.0, 5: 95.0}
    for row, score in scores.items():
        window.model.photo(row).sharpness = score
    window.model.analysis_changed()

    window.gallery_toolbar.focus_combo.setCurrentIndex(2)  # looks soft
    assert shown_names(window) == ["IMG_1.jpg"]
    window.gallery_toolbar.focus_combo.setCurrentIndex(1)  # looks sharp
    assert "IMG_1.jpg" not in shown_names(window)
    window.gallery_toolbar.focus_combo.setCurrentIndex(0)

    window.gallery_toolbar.sort_combo.setCurrentIndex(2)
    assert window.model.photo(0).name == "IMG_3.jpg"
    assert window.model.photo(5).name == "IMG_1.jpg"
    assert all(photo.status is None for photo in window.model.photos)


def test_sharpness_shows_in_the_info_lines(window):
    window.model.photo(0).sharpness = 50.0
    window.model.analysis_changed()
    assert any("Sharpness 50" in line for line in window._info_lines(window.model.photo(0)))


# -- focus peaking ---------------------------------------------------------------

def test_focus_peaking_overlays_the_shown_photo(window):
    assert window.image_view._overlay is None
    press(window, Qt.Key.Key_F)
    assert wait_until(lambda: window.image_view._overlay is not None)
    assert window.image_view._overlay.size() == window.image_view._pixmap.size()
    window.grab()  # paints it
    press(window, Qt.Key.Key_F)
    assert window.image_view._overlay is None


def test_focus_peaking_leaves_the_crop_tool_alone(window):
    press(window, Qt.Key.Key_F)
    assert wait_until(lambda: window.image_view._overlay is not None)
    press(window, Qt.Key.Key_R)
    assert window.image_view._overlay is None
    press(window, Qt.Key.Key_Escape)


# -- thumbnails on disk ----------------------------------------------------------

def test_thumbnails_come_back_from_disk_without_decoding(window, tmp_path, monkeypatch):
    path = str(tmp_path / "IMG_0.jpg")
    window.loader.thumbnail(path)
    assert wait_until(lambda: window.loader._thumbnails.get(path) is not None)
    assert wait_until(lambda: any(files for _, _, files in os.walk(window.loader._disk_thumbnails.directory)))

    window.loader.reset()
    monkeypatch.setattr(loader, "_decode", lambda *_: pytest.fail("decoded again"))
    window.loader.thumbnail(path)
    assert wait_until(lambda: window.loader._thumbnails.get(path) is not None)


# -- the Trash -------------------------------------------------------------------

def fake_trash(monkeypatch, tmp_path_factory):
    """The real one would fill the real Trash."""
    bin_dir = tmp_path_factory.mktemp("trash")

    def to_trash(path):
        location = str(bin_dir / os.path.basename(path))
        os.replace(path, location)
        return location

    monkeypatch.setattr(fileops, "_to_trash", to_trash)
    return bin_dir


def confirm_dialogs(monkeypatch, answer=QDialog.DialogCode.Accepted):
    seen = []

    def confirm(dialog):
        seen.append(dialog.body.text())
        return answer

    monkeypatch.setattr(MessageDialog, "exec", confirm)
    return seen


def test_trash_moves_rejects_and_everything_follows(window, tmp_path, monkeypatch, tmp_path_factory):
    bin_dir = fake_trash(monkeypatch, tmp_path_factory)
    seen = confirm_dialogs(monkeypatch)
    statuses(window, keep=[0], reject=[1, 2])
    process(window, monkeypatch, export=False)
    (tmp_path / "_REJECTS" / "IMG_1.jpg.xmp").write_text("<x/>")
    window.go_to(window.model.row_of(str(tmp_path / "_REJECTS" / "IMG_1.jpg")))
    window.model.set_status([window.model.row_of(str(tmp_path / "IMG_3.jpg"))], REJECT)  # not in _REJECTS: stays
    events = []
    window.plugins.emit = lambda name, *args: events.append(name)

    window.trash_rejects()

    assert "<b>2</b>" in seen[0] and "1 rejected photo(s) aren't in _REJECTS" in seen[0]
    assert sorted(os.listdir(bin_dir)) == ["IMG_1.jpg", "IMG_1.jpg.xmp", "IMG_2.jpg"]
    assert not (tmp_path / "_REJECTS").exists()
    assert [p.name for p in window.model.photos] == ["IMG_0.jpg", "IMG_3.jpg", "IMG_4.jpg", "IMG_5.jpg"]
    assert window.model.counts() == (1, 1, 2)
    assert window.current_row >= 0
    assert "photos_trashed" in events
    session = json.loads((tmp_path / SESSION_FILENAME).read_text())
    assert not any("IMG_1" in key or "IMG_2" in key for key in session)
    window.undo()  # nothing refers to the trashed photos any more


def test_trash_can_be_refused_and_needs_something_to_do(window, tmp_path, monkeypatch, tmp_path_factory):
    bin_dir = fake_trash(monkeypatch, tmp_path_factory)
    window.trash_rejects()
    assert window.toast.text() == "No rejected photos in _REJECTS"

    statuses(window, reject=[1])
    process(window, monkeypatch, export=False)
    confirm_dialogs(monkeypatch, QDialog.DialogCode.Rejected)
    window.trash_rejects()
    assert os.listdir(bin_dir) == []
    assert (tmp_path / "_REJECTS" / "IMG_1.jpg").exists()


def test_a_photo_that_cannot_be_trashed_stays_with_its_mark(window, tmp_path, monkeypatch):
    seen = confirm_dialogs(monkeypatch)
    statuses(window, reject=[1])
    process(window, monkeypatch, export=False)

    def refuse(_path):
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(fileops, "_to_trash", refuse)
    window.trash_rejects()
    assert (tmp_path / "_REJECTS" / "IMG_1.jpg").exists()
    assert window.model.counts()[1] == 1
    assert len(seen) == 2  # the confirmation, then what failed


# -- importing -------------------------------------------------------------------

def test_import_copies_a_card_and_opens_the_destination(window, tmp_path_factory, monkeypatch):
    card = tmp_path_factory.mktemp("card")
    (card / "DCIM" / "100CANON").mkdir(parents=True)
    QImage(40, 30, QImage.Format.Format_RGB32).save(str(card / "DCIM" / "100CANON" / "A.jpg"))
    destination = tmp_path_factory.mktemp("shoot") / "new"
    backup = tmp_path_factory.mktemp("backup") / "copy"

    def fill(dialog):
        dialog.source_edit.setText(str(card))
        dialog._scan()
        dialog.destination_edit.setText(str(destination))
        dialog.backup_edit.setText(str(backup))
        dialog.backup_check.setChecked(True)
        assert dialog.confirm_button.isEnabled()
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(ImportDialog, "exec", fill)
    press(window, Qt.Key.Key_I, CTRL)
    assert window.folder == str(destination)
    assert [p.name for p in window.model.photos] == ["A.jpg"]
    assert (backup / "A.jpg").exists()
    assert (card / "DCIM" / "100CANON" / "A.jpg").exists()
    assert window.settings.value("import_destination") == str(destination)


def test_import_dialog_refuses_a_destination_on_the_card(qapp, tmp_path, window):
    (tmp_path / "sub").mkdir()
    dialog = ImportDialog(window, str(tmp_path), str(tmp_path / "sub"))
    assert not dialog.confirm_button.isEnabled()
    assert "inside" in dialog.body.text()
    dialog.destination_edit.setText("")
    assert not dialog.confirm_button.isEnabled()


def test_a_card_opened_directly_points_to_import(window, tmp_path_factory):
    card = tmp_path_factory.mktemp("card")
    (card / "DCIM").mkdir()
    window.load_folder(str(card))
    assert "Import" in window.image_view._message


# -- marks in other editors ------------------------------------------------------

def raw_window_photo(tmp_path):
    (tmp_path / "RAW_1.cr2").write_bytes(b"raw")


def test_rejects_as_rating_minus_one_keep_the_stars_in_tamis(window, tmp_path):
    raw_window_photo(tmp_path)
    window.load_folder(str(tmp_path), keep_state=True)
    row = window.model.row_of(str(tmp_path / "RAW_1.cr2"))
    sidecar = tmp_path / "RAW_1.xmp"
    window.xmp_mark_actions["rating"].trigger()
    window.model.set_field([row], "rating", 3)
    window.model.set_status([row], REJECT)
    window.save_session()
    assert 'xmp:Rating="-1"' in sidecar.read_text()

    window.load_folder(str(tmp_path), keep_state=True)  # the -1 isn't taken for stars
    row = window.model.row_of(str(tmp_path / "RAW_1.cr2"))
    assert window.model.photo(row).rating == 3

    window.model.set_status([row], KEEP)
    window.save_session()
    assert 'xmp:Rating="3"' in sidecar.read_text()


def test_marks_touch_no_sidecar_by_default(window, tmp_path):
    raw_window_photo(tmp_path)
    window.load_folder(str(tmp_path), keep_state=True)
    window.model.set_status([window.model.row_of(str(tmp_path / "RAW_1.cr2"))], REJECT)
    window.save_session()
    assert not (tmp_path / "RAW_1.xmp").exists()


def test_marks_as_labels_leave_the_user_s_own_label_alone(window):
    window.model.set_field([1], "label", "Blue")
    statuses(window, keep=[2])  # marked before the option is on
    window.xmp_mark_actions["labels"].trigger()
    assert window.model.photo(2).label == "Green"

    press(window, Qt.Key.Key_X)  # photo 0
    assert window.model.photo(0).label == "Red"
    window.go_to(1)
    press(window, Qt.Key.Key_P)
    assert window.model.photo(1).label == "Blue"

    window.go_to(0)
    press(window, Qt.Key.Key_U)
    assert window.model.photo(0).label is None
    window.undo()
    assert (window.model.photo(0).status, window.model.photo(0).label) == (REJECT, "Red")


def test_plugins_marks_follow_the_option(window):
    window.xmp_mark_actions["labels"].trigger()
    PluginApi(window.plugins, Plugin("test", "test", "test")).set_status(window.model.photo(3).path, KEEP)
    assert window.model.photo(3).label == "Green"


# -- rotation and straightening -------------------------------------------------

def test_rotating_turns_the_photo_at_once_and_undo_brings_it_back(window):
    window.model.set_field([0], "edit", {"contrast": 10})
    wait_until(lambda: window.image_view._pixmap.width() > window.image_view._pixmap.height())
    press(window, Qt.Key.Key_R, Qt.KeyboardModifier.ShiftModifier)
    assert window.model.photo(0).edit == {"contrast": 10, "rotate": 90}
    assert window.edit_panel.edit()["rotate"] == 90
    assert wait_until(lambda: window.image_view._pixmap.height() > window.image_view._pixmap.width())
    press(window, Qt.Key.Key_Z, CTRL)
    assert window.model.photo(0).edit == {"contrast": 10}
    assert wait_until(lambda: window.image_view._pixmap.width() > window.image_view._pixmap.height())


def test_rotating_also_works_while_culling_on_an_unedited_photo(window):
    press(window, Qt.Key.Key_L, Qt.KeyboardModifier.ShiftModifier)
    assert window.model.photo(0).edit == {"rotate": 270}
    for _ in range(3):
        press(window, Qt.Key.Key_L, Qt.KeyboardModifier.ShiftModifier)
    assert window.model.photo(0).edit == {}  # four turns: nothing left of the edit


def test_rotating_in_the_gallery_applies_to_the_selection_as_one_undo(window):
    press(window, Qt.Key.Key_G)
    window.grid.selectAll()
    window.rotate_photos(90)
    assert all(photo.edit == {"rotate": 90} for photo in window.model.photos)
    press(window, Qt.Key.Key_Z, CTRL)
    assert all(photo.edit == {} for photo in window.model.photos)


def test_rotating_a_cropped_photo_keeps_the_same_part_of_it(window):
    window.model.set_field([0], "edit", {"crop": [0.0, 0.0, 0.5, 1.0]})
    window.rotate_photos(90)
    assert window.model.photo(0).edit == {"crop": [0.0, 0.0, 1.0, 0.5], "rotate": 90}


def test_rotating_applies_an_open_crop_first(window):
    press(window, Qt.Key.Key_R)
    window.image_view.set_crop_frame((0.0, 0.0, 0.5, 1.0))
    window.rotate_photos(90)
    assert not window.cropping
    assert window.model.photo(0).edit == {"crop": [0.0, 0.0, 1.0, 0.5], "rotate": 90}


def test_the_crop_tool_works_on_the_turned_photo(window):
    window.rotate_photos(90)
    press(window, Qt.Key.Key_R)
    assert wait_until(lambda: window.image_view._pixmap.height() > window.image_view._pixmap.width())
    assert window.cropping


def test_preview_shown_before_the_render_is_framed_like_the_render(window, qapp):
    # A photo whose top-left corner is the only bright part.
    photo = window.model.photo(0)
    image = QImage(300, 200, QImage.Format.Format_RGB32)
    image.fill(QColor(0, 0, 0))
    for y in range(50):
        for x in range(50):
            image.setPixelColor(x, y, QColor(255, 255, 255))
    pixmap = QPixmap.fromImage(image)
    assert photo.path

    turned = window._framed(pixmap, {"rotate": 90})
    assert (turned.width(), turned.height()) == (200, 300)
    assert turned.toImage().pixelColor(190, 10).red() > 200  # top-left went to the top right

    level = window._framed(pixmap, {"straighten": 5.0})
    assert level.size() == pixmap.size()
    numpy_level = develop.straighten_rows(image_to_array(image).astype(np.float32) / 65535, 5.0, 0, 200)
    painted = image_to_array(level.toImage()).astype(np.float32) / 65535
    assert np.abs(painted - numpy_level).mean() < 0.03  # Qt and numpy agree, antialiasing aside


def test_straighten_slider_goes_into_the_edit(window):
    slider, _scale = window.edit_panel._sliders["straighten"]
    slider.setValue(25)
    assert window.model.photo(0).edit == {"straighten": 2.5}
    assert window.edit_panel._values["straighten"].text() == "+2.5°"


def test_copying_leaves_turns_and_straightening_behind(window):
    window.model.set_field([0], "edit", {"contrast": 30, "rotate": 90, "straighten": 2.0})
    window.model.set_field([1], "edit", {"rotate": 180})
    window.copy_edit()
    assert window.edit_clipboard == {"contrast": 30}
    window.go_to(1)
    window.paste_edit()
    assert window.model.photo(1).edit == {"contrast": 30, "rotate": 180}


def test_rotation_is_kept_in_the_session(window):
    window.rotate_photos(90)
    window.save_session()
    with open(os.path.join(window.folder, SESSION_FILENAME), encoding="utf-8") as file:
        assert '"rotate": 90' in file.read()
    window.load_folder(window.folder)
    assert window.model.photo(0).edit == {"rotate": 90}


# -- before / after -------------------------------------------------------------

def test_holding_the_key_shows_the_photo_without_its_edit(window):
    original = window.image_view._pixmap.cacheKey()
    window.model.set_field([0], "edit", {"exposure": 2.0, "crop": [0, 0, 0.5, 0.5]})
    assert wait_until(lambda: window.image_view._pixmap.toImage().pixelColor(5, 5).green() > 140)
    edit = dict(window.model.photo(0).edit)
    width = window.image_view._pixmap.width()
    assert original != window.image_view._pixmap.cacheKey()

    QTest.keyPress(window, Qt.Key.Key_Y)
    assert window.before and window.image_view._note == "Before"
    # Without the exposure, and in the same frame.
    assert window.image_view._pixmap.toImage().pixelColor(5, 5).green() < 120
    assert window.image_view._pixmap.width() == width
    assert window.model.photo(0).edit == edit

    QTest.keyRelease(window, Qt.Key.Key_Y)
    assert not window.before and window.image_view._note == ""
    assert wait_until(lambda: window.image_view._pixmap.toImage().pixelColor(5, 5).green() > 140)
    assert window.model.photo(0).edit == edit


def test_backslash_holds_too_and_no_note_without_an_edit(window):
    QTest.keyPress(window, Qt.Key.Key_Backslash)
    assert window.before and window.image_view._note == ""
    QTest.keyRelease(window, Qt.Key.Key_Backslash)
    assert not window.before


def test_before_button_holds_while_pressed(window):
    window.toggle_edit_panel()
    window.model.set_field([0], "edit", {"contrast": 30})
    QTest.mousePress(window.edit_panel.before_button, Qt.MouseButton.LeftButton)
    assert window.before
    QTest.mouseRelease(window.edit_panel.before_button, Qt.MouseButton.LeftButton)
    assert not window.before


def test_leaving_the_window_ends_before(window):
    window._hold_before()
    QApplication.sendEvent(window, QEvent(QEvent.Type.WindowDeactivate))
    assert not window.before


def test_before_keeps_the_crop_tool_alone(window):
    window.model.set_field([0], "edit", {"contrast": 30})
    press(window, Qt.Key.Key_R)
    QTest.keyPress(window, Qt.Key.Key_Y)
    assert window.image_view._note == ""
    QTest.keyRelease(window, Qt.Key.Key_Y)
    assert window.cropping


# -- histogram and clipping -----------------------------------------------------

def test_histogram_follows_the_photo_and_its_edit(window):
    panel = window.edit_panel
    window.toggle_edit_panel()
    assert wait_until(lambda: panel.histogram.tone is not None)
    first = panel.histogram.tone
    window.model.set_field([0], "edit", {"exposure": 5.0})
    assert wait_until(lambda: panel.histogram.tone is not first and panel.histogram.tone.highlights > 0.5)
    panel.histogram.grab()  # paints without trouble, clipped or not


def test_histogram_is_not_worked_out_while_the_panel_is_closed(window):
    assert not window.edit_panel.isVisible()
    QTest.qWait(50)
    assert window.edit_panel.histogram.tone is None


def test_clipping_overlay_with_its_shortcut_and_button(window):
    view = window.image_view
    press(window, Qt.Key.Key_J)
    assert window.show_clipping and window.edit_panel.clipping_button.isChecked()
    assert wait_until(lambda: view._clipping is not None)
    assert view._clipping.size() == view._pixmap.size()
    view.grab()
    press(window, Qt.Key.Key_J)
    assert not window.show_clipping and view._clipping is None
    window.edit_panel.clipping_button.click()
    assert window.show_clipping


def test_clipping_shows_in_red_what_an_edit_blows_out(window):
    window.model.set_field([0], "edit", {"exposure": 5.0})
    press(window, Qt.Key.Key_J)
    assert wait_until(lambda: (
        window.image_view._clipping is not None
        and window.image_view._clipping.toImage().pixelColor(10, 10).alpha() == 255
    ))


def test_clipping_keeps_the_crop_tool_clear(window):
    press(window, Qt.Key.Key_J)
    assert wait_until(lambda: window.image_view._clipping is not None)
    press(window, Qt.Key.Key_R)
    assert window.image_view._clipping is None


# -- presets ---------------------------------------------------------------------

def name_it(monkeypatch, name):
    def fill(dialog):
        dialog.field.setText(name)
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(NameDialog, "exec", fill)


def test_saving_a_preset_and_applying_it_to_the_selection(window, monkeypatch):
    window.model.set_field([0], "edit", {"contrast": 30, "crop": [0, 0, 0.5, 0.5], "rotate": 90})
    window.model.set_field([2], "edit", {"crop": [0.2, 0.2, 0.8, 0.8], "grain": 5})
    name_it(monkeypatch, "  Punchy ")
    window.save_preset()
    assert window.presets.get("Punchy") == {"contrast": 30}
    assert [action.text() for action in window.edit_panel.presets_menu.actions()][0] == "Punchy"

    press(window, Qt.Key.Key_G)
    window.grid.selectAll()
    window.edit_panel.presets_menu.actions()[0].trigger()
    assert window.model.photo(1).edit == {"contrast": 30}
    assert window.model.photo(2).edit == {"contrast": 30, "crop": [0.2, 0.2, 0.8, 0.8]}  # its own framing stays
    assert window.model.photo(0).edit["rotate"] == 90

    press(window, Qt.Key.Key_Z, CTRL)  # one undo step
    assert window.model.photo(1).edit == {}
    assert window.model.photo(2).edit == {"crop": [0.2, 0.2, 0.8, 0.8], "grain": 5}


def test_a_photo_without_a_look_has_nothing_to_save(window, monkeypatch):
    monkeypatch.setattr(NameDialog, "exec", lambda _dialog: pytest.fail("should not ask"))
    window.model.set_field([0], "edit", {"rotate": 90})
    window.save_preset()
    assert window.presets.names() == []


def test_replacing_a_preset_asks_first(window, monkeypatch):
    window.model.set_field([0], "edit", {"contrast": 30})
    name_it(monkeypatch, "A")
    window.save_preset()
    window.model.set_field([0], "edit", {"contrast": 60})

    asked = []
    monkeypatch.setattr(MessageDialog, "exec", lambda _dialog: asked.append(1) or QDialog.DialogCode.Rejected)
    window.save_preset()
    assert asked and window.presets.get("A") == {"contrast": 30}

    monkeypatch.setattr(MessageDialog, "exec", lambda _dialog: QDialog.DialogCode.Accepted)
    window.save_preset()
    assert window.presets.get("A") == {"contrast": 60}


def test_deleting_a_preset_leaves_photos_alone(window, monkeypatch):
    window.model.set_field([0], "edit", {"contrast": 30})
    name_it(monkeypatch, "A")
    window.save_preset()
    monkeypatch.setattr(MessageDialog, "exec", lambda _dialog: QDialog.DialogCode.Accepted)
    delete = next(action.menu() for action in window.edit_panel.presets_menu.actions() if action.menu())
    delete.actions()[0].trigger()
    assert window.presets.names() == []
    assert window.model.photo(0).edit == {"contrast": 30}
    assert not any(action.menu() for action in window.edit_panel.presets_menu.actions())


def test_a_preset_that_cannot_be_written_is_reported(window, monkeypatch):
    window.model.set_field([0], "edit", {"contrast": 30})
    name_it(monkeypatch, "A")
    told = []
    monkeypatch.setattr("tamis.app.inform", lambda _parent, title, _text, **_kwargs: told.append(title))
    monkeypatch.setattr(window.presets, "save", lambda *_args: (_ for _ in ()).throw(OSError("read-only")))
    window.save_preset()
    assert told == ["Could not save the presets"]


# -- thumbnails of edits ---------------------------------------------------------

def thumbnail(window, row):
    return window.model.data(window.model.index(row), Qt.ItemDataRole.DecorationRole)


def test_thumbnails_show_the_edit_and_its_frame(window):
    assert wait_until(lambda: thumbnail(window, 1) is not None)
    plain = thumbnail(window, 1).toImage().pixelColor(10, 10).green()
    window.model.set_field([1], "edit", {"exposure": 2.0, "crop": [0, 0, 0.5, 1.0]})
    assert wait_until(lambda: thumbnail(window, 1).toImage().pixelColor(10, 10).green() > plain + 40)
    edited = thumbnail(window, 1)
    assert edited.height() > edited.width()  # 150 × 200 of the photo

    window.model.set_field([1], "edit", {"rotate": 90})
    assert wait_until(lambda: thumbnail(window, 1).height() > thumbnail(window, 1).width() * 1.4)
    window.model.set_field([1], "edit", {})
    assert wait_until(lambda: thumbnail(window, 1).width() > thumbnail(window, 1).height())


def test_changing_an_edit_tells_the_views(window):
    roles = []
    window.model.dataChanged.connect(lambda _first, _last, changed: roles.extend(changed))
    window.model.set_field([1], "edit", {"exposure": 2.0})
    assert Qt.ItemDataRole.DecorationRole in roles


def test_thumbnails_of_edits_come_back_from_disk(window):
    edit = {"exposure": 2.0}
    window.model.set_field([1], "edit", edit)
    path = window.model.photo(1).path
    lut = window._lut_path
    assert wait_until(lambda: window.edit_thumbnails.pixmap(path, edit, lut(edit)) is not None)
    window.edit_thumbnails.reset()
    developed = []
    original = window.edit_thumbnails._base
    window.edit_thumbnails._base = lambda *args: developed.append(args) or original(*args)
    assert wait_until(lambda: window.edit_thumbnails.pixmap(path, edit, lut(edit)) is not None)
    assert developed == []


def test_a_thumbnail_that_cannot_be_rendered_falls_back_to_the_original(window, monkeypatch):
    assert wait_until(lambda: thumbnail(window, 1) is not None)
    original = thumbnail(window, 1).cacheKey()
    monkeypatch.setattr("tamis.editing.develop_base", lambda *_args: (_ for _ in ()).throw(ValueError("bad")))
    window.model.set_field([1], "edit", {"exposure": 2.0})
    assert wait_until(lambda: window.edit_thumbnails._failed.get(window.model.photo(1).path))
    assert thumbnail(window, 1).cacheKey() == original


def test_thumbnails_of_edits_follow_moves(window):
    edit = {"exposure": 2.0}
    window.model.set_field([1], "edit", edit)
    path = window.model.photo(1).path
    assert wait_until(lambda: window.edit_thumbnails.pixmap(path, edit, None) is not None)
    key = window.edit_thumbnails.pixmap(path, edit, None).cacheKey()
    window.edit_thumbnails.rename_paths({path: path + ".moved"})
    assert window.edit_thumbnails.pixmap(path + ".moved", edit, None).cacheKey() == key
