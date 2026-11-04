"""The main window, driven by its real shortcuts and mouse events, on a throwaway folder."""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtCore import QPoint, QSettings, Qt  # noqa: E402
from PyQt6.QtGui import QColor, QImage  # noqa: E402
from PyQt6.QtTest import QTest  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from cullspeed.app import CULL_TAB, GALLERY_TAB, CullSpeedApp  # noqa: E402
from cullspeed.constants import KEEP, REJECT  # noqa: E402

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
def window(qapp, tmp_path):
    for i in range(PHOTOS):
        image = QImage(300, 200, QImage.Format.Format_RGB32)
        image.fill(QColor(40 * i, 80, 120))
        assert image.save(str(tmp_path / f"IMG_{i}.jpg"))
    win = CullSpeedApp()
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
