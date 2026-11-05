"""The main window, driven by its real shortcuts and mouse events, on a throwaway folder."""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtCore import QPoint, QSettings, Qt  # noqa: E402
from PyQt6.QtGui import QColor, QImage  # noqa: E402
from PyQt6.QtTest import QTest  # noqa: E402
from PyQt6.QtWidgets import QApplication, QDialog  # noqa: E402

from tamis.app import CULL_TAB, GALLERY_TAB, TamisApp  # noqa: E402
from tamis.constants import KEEP, REJECT  # noqa: E402
from tamis.fileops import plan_moves  # noqa: E402
from tamis.plugins import PhotoInfo  # noqa: E402
from tamis.widgets import ProcessDialog  # noqa: E402

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
    win = TamisApp(plugins_dir=str(tmp_path_factory.mktemp("plugins")))  # never the real app's plugins
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
