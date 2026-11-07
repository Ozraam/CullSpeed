"""The quick edit panel shown beside the photo in the Cull view."""
import os

import numpy as np
from PyQt6.QtCore import QPointF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPolygonF
from PyQt6.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QScrollArea,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from . import develop, tones
from .constants import COLOR_BORDER

# key, label, slider range, slider units per setting unit
_SECTIONS = (
    ("Crop", (
        ("straighten", "Straighten", (-150, 150), 10),
    )),
    ("Light", (
        ("exposure", "Exposure", (-500, 500), 100),
        ("contrast", "Contrast", (-100, 100), 1),
        ("highlights", "Highlights", (-100, 100), 1),
        ("shadows", "Shadows", (-100, 100), 1),
    )),
    ("Colour", (
        ("temperature", "Temperature", (-100, 100), 1),
        ("tint", "Tint", (-100, 100), 1),
        ("saturation", "Saturation", (-100, 100), 1),
    )),
    ("Film", (
        ("lut_strength", "Strength", (0, 100), 1),
        ("grain", "Grain", (0, 100), 1),
        ("grain_size", "Grain size", (1, 3), 1),
        ("vignette", "Vignette", (-100, 100), 1),
    )),
)


class _Slider(QSlider):
    """A slider that goes back to its default on double-click."""

    def __init__(self, default, parent=None):
        super().__init__(Qt.Orientation.Horizontal, parent)
        self.default = default
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # keep the window's shortcuts working

    def mouseDoubleClickEvent(self, event):
        self.setValue(self.default)
        self.sliderReleased.emit()  # a deliberate change: commit it


class _LutCombo(QComboBox):
    """A combo box whose popup is a menu tree following the LUT folder's sub-folders.

    Items stay a flat list (text: display name, data: LUT id like "Fuji/Velvia 50.png");
    only the popup is nested, so a big LUT collection is browsed one folder at a time.
    """

    def showPopup(self):
        menu = self.build_menu()
        menu.setMinimumWidth(self.width())
        menu.exec(self.mapToGlobal(self.rect().bottomLeft()))
        menu.deleteLater()
        self.hidePopup()  # resets the combo's pressed state

    def build_menu(self):
        menu = QMenu(self)
        submenus = {}  # folder path -> QMenu

        def folder_menu(parts):
            if not parts:
                return menu
            key = "/".join(parts)
            if key not in submenus:
                submenus[key] = folder_menu(parts[:-1]).addMenu(parts[-1])
            return submenus[key]

        luts = []
        for index in range(self.count()):
            lut_id = self.itemData(index)
            if lut_id and self.itemText(index).startswith("Missing: "):
                lut_id = None  # not in the folder: keep it at the top level
            if lut_id:
                luts.append((lut_id.split("/"), index))
            else:
                self._add_action(menu, self.itemText(index), index)
        if luts:
            menu.addSeparator()
        # Sub-folders before the LUTs next to them, like a file browser.
        for parts, index in sorted(luts, key=lambda item: (
            [p.casefold() for p in item[0][:-1]] + ["￿"], item[0][-1].casefold()
        )):
            *folders, name = parts
            self._add_action(folder_menu(folders), os.path.splitext(name)[0], index)
        return menu

    def _add_action(self, menu, text, index):
        action = menu.addAction(text)
        action.setCheckable(True)
        action.setChecked(index == self.currentIndex())
        action.triggered.connect(lambda _checked, index=index: self.setCurrentIndex(index))
        if index == self.currentIndex():
            # Bold the folders leading to the current LUT, so it's easy to find again.
            while isinstance(menu.parent(), QMenu):
                font = menu.menuAction().font()
                font.setBold(True)
                menu.menuAction().setFont(font)
                menu = menu.parent()


# Label, width / height of the frame in pixels; "original" is the photo's own shape.
CROP_RATIOS = (
    ("Free", None),
    ("Original", "original"),
    ("1:1", 1.0),
    ("3:2", 3 / 2),
    ("2:3", 2 / 3),
    ("4:5", 4 / 5),
    ("5:4", 5 / 4),
    ("16:9", 16 / 9),
    ("9:16", 9 / 16),
)


def _format(key, value):
    if key == "exposure":
        return f"{value:+.2f} EV"
    if key == "straighten":
        return f"{value:+.1f}°" if value else "0°"
    if key in ("lut_strength", "grain"):
        return f"{value}"
    if key == "grain_size":
        return ("fine", "medium", "coarse")[value - 1]
    return f"{value:+d}" if value else "0"


class Histogram(QWidget):
    """The red, green and blue histogram of what is shown, with how much of it is clipped at either end."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.tone = None
        self.setFixedHeight(84)

    def set_tone(self, tone):
        if tone is not self.tone:
            self.tone = tone
            self.update()

    def _curve(self, counts, peak):
        """The outline of one channel, as a closed polygon filling the widget's width."""
        width, height = self.width(), self.height()
        heights = np.minimum(counts / peak, 1.0)
        points = [QPointF(0, height)]
        points += [QPointF(i / 255 * width, height * (1 - float(value))) for i, value in enumerate(heights)]
        points.append(QPointF(width, height))
        return QPolygonF(points)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#000000"))
        if self.tone is not None:
            counts = self.tone.counts
            # The ends can tower over everything else: they are told apart by the figures below.
            peak = max(1, int(counts[:, 2:-2].max()))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
            for channel, color in enumerate(("#c03030", "#30a040", "#3060d0")):
                painter.setBrush(QColor(color))
                painter.drawPolygon(self._curve(counts[channel], peak))
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
            self._draw_clipped(painter, self.tone.shadows, tones.SHADOW_COLOR, Qt.AlignmentFlag.AlignLeft)
            self._draw_clipped(painter, self.tone.highlights, tones.HIGHLIGHT_COLOR, Qt.AlignmentFlag.AlignRight)
        painter.setPen(QColor(COLOR_BORDER))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(self.rect().adjusted(0, 0, -1, -1))

    def _draw_clipped(self, painter, fraction, color, side):
        if fraction >= 0.0005:  # shown as at least 0.1 %
            painter.setPen(QColor(*color))
            painter.drawText(self.rect().adjusted(5, 3, -5, -3), side | Qt.AlignmentFlag.AlignTop, f"{fraction * 100:.1f}%")


class EditPanel(QFrame):
    """Sliders for the edit of the current photo.

    `changed(edit, final)`: `final` is False while a slider is being dragged
    (preview only) and True once the change should be kept (and undoable).
    """

    changed = pyqtSignal(dict, bool)
    copy_requested = pyqtSignal()
    paste_requested = pyqtSignal()
    reset_requested = pyqtSignal()
    export_requested = pyqtSignal()
    lut_folder_requested = pyqtSignal()
    crop_requested = pyqtSignal()
    crop_cleared = pyqtSignal()
    crop_ratio_changed = pyqtSignal()
    rotate_requested = pyqtSignal(int)  # degrees: 90 or -90
    clipping_toggled = pyqtSignal(bool)
    before_held = pyqtSignal(bool)  # the Before button is being held down, or was let go
    preset_chosen = pyqtSignal(str)
    preset_save_requested = pyqtSignal()
    preset_delete_requested = pyqtSignal(str)

    WIDTH = 300

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("EditPanel")
        self.setFixedWidth(self.WIDTH)
        self._sliders = {}
        self._values = {}
        self._updating = False
        self._crop = None  # the photo's crop and quarter turns: not sliders, but part of edit()
        self._rotate = 0

        # Stays visible above the scrolling sliders, which are what it is there to judge.
        header = QWidget()
        header.setObjectName("EditPanelHeader")
        top = QVBoxLayout(header)
        top.setContentsMargins(14, 12, 14, 10)
        top.setSpacing(6)
        title = QLabel("Edit")
        title.setObjectName("DialogTitle")
        self.histogram = Histogram()
        top.addWidget(title)
        top.addWidget(self.histogram)
        top.addLayout(self._tone_row())

        content = QWidget()
        content.setObjectName("EditPanelContent")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(14, 8, 14, 14)
        layout.setSpacing(6)

        hint = QLabel("Double-click a slider to reset it.")
        hint.setObjectName("Muted")
        layout.addWidget(hint)

        for section, rows in _SECTIONS:
            heading = QLabel(section.upper())
            heading.setObjectName("SectionTitle")
            layout.addSpacing(8)
            layout.addWidget(heading)
            if section == "Crop":
                layout.addLayout(self._crop_row())
                layout.addLayout(self._rotate_row())
            if section == "Film":
                layout.addLayout(self._lut_row())
            grid = QGridLayout()
            grid.setHorizontalSpacing(8)
            grid.setVerticalSpacing(2)
            for row, (key, label, (low, high), scale) in enumerate(rows):
                default = develop.DEFAULTS[key]
                slider = _Slider(round(default * scale))
                slider.setRange(low, high)
                slider.setValue(round(default * scale))
                slider.valueChanged.connect(lambda _value, key=key: self._on_slider(key))
                slider.sliderReleased.connect(self._commit)
                name = QLabel(label)
                value = QLabel(_format(key, default))
                value.setObjectName("Muted")
                value.setMinimumWidth(64)
                value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                grid.addWidget(name, row * 2, 0)
                grid.addWidget(value, row * 2, 1)
                grid.addWidget(slider, row * 2 + 1, 0, 1, 2)
                self._sliders[key] = (slider, scale)
                self._values[key] = value
            layout.addLayout(grid)

        layout.addStretch()

        scroll = QScrollArea()
        scroll.setWidget(content)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        # Actions stay visible below the scrolling sliders.
        footer = QWidget()
        footer.setObjectName("EditPanelFooter")
        buttons = QGridLayout(footer)
        buttons.setContentsMargins(14, 10, 14, 12)
        buttons.setSpacing(6)
        for i, (text, signal) in enumerate((
            ("Copy", self.copy_requested),
            ("Paste", self.paste_requested),
            ("Reset", self.reset_requested),
        )):
            buttons.addWidget(self._button(text, signal), 0, i)
        self.presets_button = QPushButton("Presets")
        self.presets_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.presets_button.setToolTip("Save this look under a name, or apply a saved one to the selection")
        self.presets_menu = QMenu(self)
        self.presets_button.setMenu(self.presets_menu)
        self.set_presets([])
        buttons.addWidget(self.presets_button, 1, 0, 1, 3)
        export = self._button("Export JPEG…", self.export_requested)
        export.setObjectName("Primary")
        buttons.addWidget(export, 2, 0, 1, 3)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(header)
        outer.addWidget(scroll, 1)
        outer.addWidget(footer)

    def _button(self, text, signal):
        button = QPushButton(text)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.clicked.connect(signal)
        return button

    def _tone_row(self):
        self.clipping_button = QPushButton("Clipping")
        self.clipping_button.setCheckable(True)
        self.clipping_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.clipping_button.setToolTip("Show blown highlights in red, clipped colours in orange and blocked shadows in blue (J)")
        self.clipping_button.clicked.connect(self.clipping_toggled)
        self.before_button = QPushButton("Before")
        self.before_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.before_button.setToolTip("Hold to see the photo without its edit (or hold Y)")
        self.before_button.pressed.connect(lambda: self.before_held.emit(True))
        self.before_button.released.connect(lambda: self.before_held.emit(False))
        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(self.clipping_button)
        row.addWidget(self.before_button)
        return row

    def _rotate_row(self):
        left = QPushButton("↺ 90°")
        left.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        left.setToolTip("Rotate left ([ or ⇧L)")
        left.clicked.connect(lambda: self.rotate_requested.emit(-90))
        right = QPushButton("↻ 90°")
        right.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        right.setToolTip("Rotate right (] or ⇧R)")
        right.clicked.connect(lambda: self.rotate_requested.emit(90))
        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(left)
        row.addWidget(right)
        return row

    def _crop_row(self):
        self.crop_button = self._button("Crop", self.crop_requested)
        self.crop_button.setToolTip("Crop this photo (R)")
        self.crop_ratio = QComboBox()
        self.crop_ratio.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.crop_ratio.setToolTip("Shape of the crop frame")
        for label, ratio in CROP_RATIOS:
            self.crop_ratio.addItem(label, ratio)
        self.crop_ratio.currentIndexChanged.connect(lambda _index: self.crop_ratio_changed.emit())
        clear = self._button("Clear", self.crop_cleared)
        clear.setToolTip("Back to the whole photo")
        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(self.crop_button)
        row.addWidget(self.crop_ratio, 1)
        row.addWidget(clear)
        return row

    def _lut_row(self):
        self.lut_combo = _LutCombo()
        self.lut_combo.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.lut_combo.setMinimumWidth(0)
        self.lut_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.lut_combo.addItem("No film simulation", None)
        self.lut_combo.currentIndexChanged.connect(self._on_lut)
        folder = self._button("Folder…", self.lut_folder_requested)
        folder.setToolTip("Choose the folder of film simulation LUTs (HaldCLUT .png, .cube)")
        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(self.lut_combo, 1)
        row.addWidget(folder)
        return row

    # -- state ---------------------------------------------------------------

    def set_presets(self, names):
        """Rebuild the Presets menu: the presets to apply, saving, and deleting."""
        menu = self.presets_menu
        menu.clear()
        for name in names:
            menu.addAction(name).triggered.connect(lambda _checked, name=name: self.preset_chosen.emit(name))
        if names:
            menu.addSeparator()
        menu.addAction("Save Current Look…").triggered.connect(lambda _checked: self.preset_save_requested.emit())
        if names:
            delete = menu.addMenu("Delete")
            for name in names:
                delete.addAction(name).triggered.connect(lambda _checked, name=name: self.preset_delete_requested.emit(name))

    def set_clipping(self, on):
        """Show whether clipping is on, without emitting `clipping_toggled`."""
        self.clipping_button.setChecked(on)

    def set_luts(self, entries):
        """`entries`: [(LUT id, display name)]."""
        current = self.lut_combo.currentData()
        self._updating = True
        self.lut_combo.clear()
        self.lut_combo.addItem("No film simulation" if entries else "No film simulation (choose a LUT folder…)", None)
        for lut_id, name in entries:
            self.lut_combo.addItem(name, lut_id)
        self._select_lut(current)
        self._updating = False

    def _select_lut(self, lut_id):
        index = self.lut_combo.findData(lut_id) if lut_id else 0
        if index < 0:  # stored with the photo, but not in the current LUT folder
            self.lut_combo.addItem(f"Missing: {lut_id}", lut_id)
            index = self.lut_combo.count() - 1
        self.lut_combo.setCurrentIndex(index)

    def set_edit(self, edit):
        """Show `edit` without emitting `changed`."""
        values = develop.settings(edit)
        self._crop = values["crop"]
        self._rotate = values["rotate"]
        self._updating = True
        for key, (slider, scale) in self._sliders.items():
            slider.setValue(round(values[key] * scale))
            self._values[key].setText(_format(key, values[key]))
        self._select_lut(values["lut"])
        self._updating = False

    def edit(self):
        values = {key: slider.value() / scale for key, (slider, scale) in self._sliders.items()}
        values["lut"] = self.lut_combo.currentData()
        values["crop"] = self._crop
        values["rotate"] = self._rotate
        return develop.clean(values)

    def set_cropping(self, cropping):
        self.crop_button.setText("Done" if cropping else "Crop")
        self.crop_button.setObjectName("Primary" if cropping else "")
        self.crop_button.style().unpolish(self.crop_button)
        self.crop_button.style().polish(self.crop_button)

    def crop_pixel_ratio(self):
        """The chosen frame shape: width / height in pixels, "original", or None when free."""
        return self.crop_ratio.currentData()

    def set_crop_free(self):
        """Choose the free shape, without emitting `crop_ratio_changed`."""
        blocked = self.crop_ratio.blockSignals(True)
        self.crop_ratio.setCurrentIndex(0)
        self.crop_ratio.blockSignals(blocked)

    # -- signals -------------------------------------------------------------

    def _on_slider(self, key):
        slider, scale = self._sliders[key]
        value = slider.value() / scale
        self._values[key].setText(_format(key, value if key in ("exposure", "straighten") else int(value)))
        if not self._updating:
            # Dragging previews; a click on the track or the wheel is a final change.
            self.changed.emit(self.edit(), not slider.isSliderDown())

    def _commit(self):
        if not self._updating:
            self.changed.emit(self.edit(), True)

    def _on_lut(self, _index):
        if not self._updating:
            self.changed.emit(self.edit(), True)
