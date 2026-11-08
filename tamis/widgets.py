import os
import shutil
from functools import partial
from html import escape

from PyQt6.QtCore import QPoint, QPointF, QRect, QRectF, QSize, QSizeF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFontMetrics, QKeySequence, QPainter, QPainterPath, QPen, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QStyle,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)

from . import cropping
from .constants import (
    COLOR_ACCENT,
    COLOR_CARD,
    COLOR_CARD_HOVER,
    COLOR_KEEP,
    COLOR_MUTED,
    COLOR_REJECT,
    COLOR_STAR,
    COLOR_TEXT,
    EXPORT_DIR,
    KEEP,
    LABEL_COLORS,
    REJECT,
)
from .exportsettings import LONG_EDGE_MAX, QUALITY_RANGE, SCOPES, TOKENS, ExportSettings, file_stem, pattern_problem
from .fileops import import_size, plan_import
from .models import (
    BURST_ROLE,
    EDITED_ROLE,
    LABEL_ROLE,
    RATING_ROLE,
    SOFT_ROLE,
    STATUS_ROLE,
    THUMBNAIL_FAILED_ROLE,
    StatusFilterProxy,
)

STATUS_COLORS = {KEEP: QColor(COLOR_KEEP), REJECT: QColor(COLOR_REJECT)}
BURST_COLORS = ("#f59e0b", "#22d3ee")  # alternating, so that bursts side by side stay apart


def size_text(size):
    """`1.2 GB`."""
    amount = float(size)
    for unit in ("bytes", "KB", "MB", "GB"):
        if amount < 1000:
            return f"{amount:.0f} {unit}" if unit == "bytes" else f"{amount:.1f} {unit}"
        amount /= 1000
    return f"{amount:.1f} TB"


def draw_status_badge(painter, center, radius, status):
    """Filled circle with a check (keep) or a cross (reject)."""
    color = STATUS_COLORS.get(status)
    if color is None:
        return
    painter.save()
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)
    painter.drawEllipse(center, radius, radius)
    pen = QPen(QColor("#0b0b0b"), max(1.5, radius / 4.5))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    r = radius * 0.45
    if status == KEEP:
        painter.drawPolyline([
            QPointF(center.x() - r, center.y()),
            QPointF(center.x() - r * 0.25, center.y() + r * 0.75),
            QPointF(center.x() + r, center.y() - r * 0.7),
        ])
    else:
        painter.drawLine(QPointF(center.x() - r, center.y() - r), QPointF(center.x() + r, center.y() + r))
        painter.drawLine(QPointF(center.x() - r, center.y() + r), QPointF(center.x() + r, center.y() - r))
    painter.restore()


def fit_rect(size, bounds):
    """`size` scaled to fit inside `bounds`, centered."""
    scaled = size.scaled(bounds.size(), Qt.AspectRatioMode.KeepAspectRatio)
    rect = QRect(QPoint(0, 0), scaled)
    rect.moveCenter(bounds.center())
    return rect


# -- thumbnails ------------------------------------------------------------

class ThumbnailDelegate(QStyledItemDelegate):
    PADDING = 6

    def __init__(self, thumb_size, show_names, parent=None):
        super().__init__(parent)
        self.thumb_size = thumb_size
        self.show_names = show_names

    def text_height(self, option):
        return QFontMetrics(option.font).height() + 6 if self.show_names else 0

    def sizeHint(self, option, index):
        side = self.thumb_size + self.PADDING * 2
        return QSize(side, side + self.text_height(option))

    def paint(self, painter, option, index):
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        status = index.data(STATUS_ROLE)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)

        card = QRectF(option.rect).adjusted(2, 2, -2, -2)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(COLOR_CARD_HOVER if hovered else COLOR_CARD))
        painter.drawRoundedRect(card, 6, 6)

        pad = self.PADDING
        image_box = QRect(
            option.rect.left() + pad,
            option.rect.top() + pad,
            option.rect.width() - pad * 2,
            option.rect.height() - pad * 2 - self.text_height(option),
        )
        pixmap = index.data(Qt.ItemDataRole.DecorationRole)
        if isinstance(pixmap, QPixmap) and not pixmap.isNull():
            target = fit_rect(pixmap.size(), image_box)
            if status == REJECT:
                painter.setOpacity(0.4)
            painter.drawPixmap(target, pixmap)
            painter.setOpacity(1.0)
        else:
            target = image_box
            painter.setPen(QColor(COLOR_MUTED))
            text = "…"
            if index.data(THUMBNAIL_FAILED_ROLE):
                text = "⚠\nUnreadable" if image_box.width() >= 80 else "⚠"
            painter.drawText(image_box, Qt.AlignmentFlag.AlignCenter, text)

        if self.show_names:
            text_rect = QRect(
                option.rect.left() + pad,
                image_box.bottom() + 3,
                option.rect.width() - pad * 2,
                self.text_height(option),
            )
            name = option.fontMetrics.elidedText(
                index.data(Qt.ItemDataRole.DisplayRole) or "", Qt.TextElideMode.ElideMiddle, text_rect.width()
            )
            painter.setPen(QColor(COLOR_TEXT if selected else COLOR_MUTED))
            painter.drawText(text_rect, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop, name)

        painter.setBrush(Qt.BrushStyle.NoBrush)
        if status in STATUS_COLORS:
            painter.setPen(QPen(STATUS_COLORS[status], 2))
            painter.drawRoundedRect(QRectF(target).adjusted(-1, -1, 1, 1), 3, 3)
        if selected:
            painter.setPen(QPen(QColor(COLOR_ACCENT), 3))
            painter.drawRoundedRect(card.adjusted(1, 1, -1, -1), 6, 6)

        radius = max(6, min(11, self.thumb_size // 12))
        draw_status_badge(
            painter, QPointF(target.right() - radius - 2, target.top() + radius + 2), radius, status
        )

        label = index.data(LABEL_ROLE)
        if label:
            painter.setPen(QPen(QColor(0, 0, 0, 160), 1.5))
            painter.setBrush(QColor(LABEL_COLORS.get(label, COLOR_MUTED)))
            painter.drawEllipse(QPointF(target.left() + radius + 2, target.top() + radius + 2), radius * 0.7, radius * 0.7)
        if index.data(EDITED_ROLE) and isinstance(pixmap, QPixmap):
            # Edited: a small "half-tone" disc in the bottom-left corner.
            centre = QPointF(target.left() + radius + 2, target.bottom() - radius - 2)
            painter.setPen(QPen(QColor(0, 0, 0, 160), 1.5))
            painter.setBrush(QColor(COLOR_TEXT))
            painter.drawEllipse(centre, radius * 0.7, radius * 0.7)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#0b0b0b"))
            disc = QRectF(0, 0, radius * 1.4, radius * 1.4)
            disc.moveCenter(centre)
            painter.drawPie(disc, 90 * 16, 180 * 16)  # left half dark
        rating = index.data(RATING_ROLE) or 0
        if rating:
            font = painter.font()
            font.setPointSizeF(max(7.0, font.pointSizeF() * (0.8 if self.thumb_size < 100 else 0.95)))
            painter.setFont(font)
            metrics = QFontMetrics(font)
            text = "★" * rating
            strip = QRectF(0, 0, metrics.horizontalAdvance(text) + 10, metrics.height() + 2)
            strip.moveCenter(QPointF(target.center().x(), target.bottom() - strip.height() / 2 - 3))
            _fill_pill(painter, strip, QColor(0, 0, 0, 150))
            painter.setPen(QColor(COLOR_STAR))
            painter.drawText(strip, Qt.AlignmentFlag.AlignCenter, text)
        self._draw_analysis(painter, option, index, target)
        painter.restore()

    def _draw_analysis(self, painter, option, index, target):
        """A bar across the frames of a burst, and a pill with the frame's place in it and what focus suggests."""
        burst = index.data(BURST_ROLE)
        soft = index.data(SOFT_ROLE)
        if burst:
            bar = QRectF(option.rect.left(), option.rect.top(), option.rect.width(), 3)
            painter.fillRect(bar, QColor(BURST_COLORS[burst.id % len(BURST_COLORS)]))
        parts = []
        if burst:
            parts.append(f"{'◎ ' if burst.sharpest else ''}{burst.position + 1}/{burst.size}")
        if soft:
            parts.append("soft")
        if not parts:
            return
        font = option.font
        font.setPointSizeF(max(7.0, font.pointSizeF() * 0.8))
        painter.setFont(font)
        metrics = QFontMetrics(font)
        text = " · ".join(parts)
        pill = QRectF(0, 0, metrics.horizontalAdvance(text) + 10, metrics.height() + 2)
        pill.moveCenter(QPointF(target.center().x(), target.top() + pill.height() / 2 + 3))
        _fill_pill(painter, pill, QColor(0, 0, 0, 170))
        painter.setPen(QColor(COLOR_KEEP if burst and burst.sharpest else COLOR_TEXT))
        painter.drawText(pill, Qt.AlignmentFlag.AlignCenter, text)


def make_thumbnail_view(model, delegate, horizontal):
    view = QListView()
    view.setModel(model)
    view.setItemDelegate(delegate)
    view.setUniformItemSizes(True)
    view.setMouseTracking(True)
    view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
    view.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
    if horizontal:
        view.setFlow(QListView.Flow.LeftToRight)
        view.setWrapping(False)
        view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    else:
        view.setViewMode(QListView.ViewMode.IconMode)
        view.setFlow(QListView.Flow.LeftToRight)
        view.setWrapping(True)
        view.setResizeMode(QListView.ResizeMode.Adjust)
        view.setMovement(QListView.Movement.Static)
        view.setSpacing(4)
        view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
    return view


# -- main image ------------------------------------------------------------

def _fill_pill(painter, rect, color=None):
    path = QPainterPath()
    path.addRoundedRect(rect, rect.height() / 2, rect.height() / 2)
    painter.fillPath(path, color or QColor(0, 0, 0, 170))


def stars_text(rating):
    return "★" * rating + "☆" * (5 - rating)


class ImageView(QWidget):
    """Shows a photo fitted to the window, or at 1:1 with panning.

    Zoom survives a change of photo (`set_image(..., keep_zoom=True)`), so a
    burst can be checked for focus at the same spot, photo after photo.
    """

    MARGIN = 16

    zoom_changed = pyqtSignal(bool)
    view_changed = pyqtSignal(bool, QPointF)  # zoomed, center: the user zoomed or panned
    clicked = pyqtSignal()
    crop_confirmed = pyqtSignal()  # double-click on the crop frame

    CROP_HANDLE_REACH = 12  # pixels around a crop handle that grab it

    def __init__(self, parent=None):
        super().__init__(parent)
        self._crop = None  # the crop frame, while cropping (see cropping.py)
        self._crop_ratio = None  # its shape: width / height in pixels, "original" or None
        self._crop_drag = None  # (handle, mouse position, frame) when the drag started
        self._pixmap = None
        self._source_key = None  # cacheKey() of the pixmap last given to set_image()
        self._native = None  # size of the photo itself, which a preview is smaller than
        self._scaled = None
        self._overlay = None  # focus peaking, drawn over the photo at whatever size it is shown
        self._clipping = None  # clipped pixels, drawn the same way, under focus peaking
        self._clipping_tag = None  # the photo it is of
        self._note = ""  # a word over the photo, like "Before"
        self._message = ""
        self._status = None
        self._rating = 0
        self._label = None
        self._info = []
        self._active = False
        self._full_loading = False
        self._zoomed = False
        self._center = QPointF(0.5, 0.5)  # zoom focus, relative to the image
        self._drag_from = None
        self.setMinimumSize(120, 120)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def set_image(self, pixmap, message="", native_size=None, keep_zoom=False):
        if keep_zoom and pixmap is not None and pixmap.cacheKey() == self._source_key:
            return  # same photo: keep whatever is shown, including a full-resolution swap
        self._source_key = pixmap.cacheKey() if pixmap is not None else None
        self._pixmap = pixmap if pixmap is not None and not pixmap.isNull() else None
        if native_size is not None and native_size.isValid():
            self._native = QSize(native_size)
        else:
            self._native = self._pixmap.size() if self._pixmap is not None else None
        self._scaled = None
        self._message = message
        self._full_loading = False
        if not keep_zoom:
            self._zoomed = False
        self._update_cursor()
        self.update()

    def set_full_image(self, pixmap):
        """Swap in the full-resolution image, keeping the zoom position."""
        self._full_loading = False
        if pixmap is not None and not pixmap.isNull() and self._pixmap is not None:
            self._pixmap = pixmap
            self._native = pixmap.size()
            self._scaled = None
        self.update()

    def set_overlay(self, pixmap):
        if pixmap is not self._overlay:
            self._overlay = pixmap
            self.update()

    def set_clipping(self, pixmap, tag=None):
        """`tag`: what the overlay is of. While a new one is made (None), that of the same `tag` stays up."""
        if pixmap is None and tag is not None and tag == self._clipping_tag:
            return
        self._clipping_tag = tag
        if pixmap is not self._clipping:
            self._clipping = pixmap
            self.update()

    def set_note(self, text):
        if text != self._note:
            self._note = text
            self.update()

    def set_full_loading(self, loading):
        if loading != self._full_loading:
            self._full_loading = loading
            self.update()

    def set_status(self, status):
        if status != self._status:
            self._status = status
            self.update()

    def set_rating(self, rating, label):
        if (rating, label) != (self._rating, self._label):
            self._rating, self._label = rating, label
            self.update()

    def set_info(self, lines):
        if lines != self._info:
            self._info = list(lines)
            self.update()

    def set_active(self, active):
        if active != self._active:
            self._active = active
            self.update()

    @property
    def zoomed(self):
        return self._zoomed

    # crop tool: the whole photo is shown with the frame over it

    @property
    def crop_frame(self):
        return self._crop

    def start_crop(self, frame, ratio):
        self._crop = tuple(frame)
        self._crop_drag = None
        self.setMouseTracking(True)
        self.set_crop_ratio(ratio)
        self.update()

    def stop_crop(self):
        self._crop = self._crop_drag = None
        self.setMouseTracking(False)
        self._update_cursor()
        self.update()

    def set_crop_ratio(self, ratio):
        """Give the frame a new shape (see EditPanel.crop_pixel_ratio())."""
        self._crop_ratio = ratio
        self.set_crop_frame(self._crop)

    def set_crop_frame(self, frame):
        self._crop = cropping.fit_aspect(tuple(frame), self._crop_aspect())
        self.update()

    def _crop_aspect(self):
        if self._crop_ratio is None or self._pixmap is None:
            return None
        if self._crop_ratio == "original":
            return 1.0
        return self._crop_ratio * self._pixmap.height() / self._pixmap.width()

    def _crop_rect(self, fit):
        """The frame in widget coordinates."""
        left, top, right, bottom = self._crop
        return QRectF(
            fit.x() + left * fit.width(), fit.y() + top * fit.height(),
            (right - left) * fit.width(), (bottom - top) * fit.height(),
        )

    def _crop_handle_at(self, position):
        rect = self._crop_rect(self._fit_rect())
        xs = {"l": rect.left(), "r": rect.right()}
        ys = {"t": rect.top(), "b": rect.bottom()}
        reach = self.CROP_HANDLE_REACH
        near_x = next((k for k, x in xs.items() if abs(position.x() - x) <= reach), "")
        near_y = next((k for k, y in ys.items() if abs(position.y() - y) <= reach), "")
        in_x = rect.left() - reach <= position.x() <= rect.right() + reach
        in_y = rect.top() - reach <= position.y() <= rect.bottom() + reach
        if near_x and near_y:
            return near_y + near_x
        if near_x and in_y:
            return near_x
        if near_y and in_x:
            return near_y
        return cropping.MOVE if rect.contains(position) else None

    def _update_crop_cursor(self, position):
        handle = self._crop_drag[0] if self._crop_drag else self._crop_handle_at(position)
        shapes = {
            "tl": Qt.CursorShape.SizeFDiagCursor, "br": Qt.CursorShape.SizeFDiagCursor,
            "tr": Qt.CursorShape.SizeBDiagCursor, "bl": Qt.CursorShape.SizeBDiagCursor,
            "l": Qt.CursorShape.SizeHorCursor, "r": Qt.CursorShape.SizeHorCursor,
            "t": Qt.CursorShape.SizeVerCursor, "b": Qt.CursorShape.SizeVerCursor,
            cropping.MOVE: Qt.CursorShape.ClosedHandCursor if self._crop_drag else Qt.CursorShape.OpenHandCursor,
        }
        if handle in shapes:
            self.setCursor(shapes[handle])
        else:
            self.unsetCursor()

    def _draw_crop(self, painter, fit):
        rect = self._crop_rect(fit)
        outside = QPainterPath()
        outside.addRect(QRectF(fit))
        outside.addRect(rect)
        painter.fillPath(outside, QColor(0, 0, 0, 165))  # odd-even fill: everything but the frame

        painter.setPen(QPen(QColor(255, 255, 255, 110), 1))
        for i in (1, 2):  # rule of thirds
            x, y = rect.left() + rect.width() * i / 3, rect.top() + rect.height() * i / 3
            painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
            painter.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
        painter.setPen(QPen(QColor("#ffffff"), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(rect)

        pen = QPen(QColor("#ffffff"), 3)
        pen.setCapStyle(Qt.PenCapStyle.FlatCap)
        painter.setPen(pen)
        arm = min(18.0, rect.width() / 2, rect.height() / 2)
        for x, sx in ((rect.left(), 1), (rect.right(), -1)):
            for y, sy in ((rect.top(), 1), (rect.bottom(), -1)):
                painter.drawLine(QPointF(x, y), QPointF(x + sx * arm, y))
                painter.drawLine(QPointF(x, y), QPointF(x, y + sy * arm))
        for x, y in ((rect.center().x(), rect.top()), (rect.center().x(), rect.bottom())):
            painter.drawLine(QPointF(x - arm / 2, y), QPointF(x + arm / 2, y))
        for x, y in ((rect.left(), rect.center().y()), (rect.right(), rect.center().y())):
            painter.drawLine(QPointF(x, y - arm / 2), QPointF(x, y + arm / 2))

        native = self._native or self._pixmap.size()
        left, top, right, bottom = self._crop
        text = (
            f"{round((right - left) * native.width())} × {round((bottom - top) * native.height())}"
            "  ·  Enter to apply  ·  Esc to cancel"
        )
        metrics = QFontMetrics(painter.font())
        note = QRectF(0, 0, metrics.horizontalAdvance(text) + 24, metrics.height() + 10)
        note.moveCenter(QPointF(self.width() / 2, self.height() - note.height() / 2 - 22))
        _fill_pill(painter, note)
        painter.setPen(QColor(COLOR_TEXT))
        painter.drawText(note, Qt.AlignmentFlag.AlignCenter, text)

    def toggle_zoom(self, at=None):
        if self._pixmap is None:
            return
        if not self._zoomed and at is not None:
            fit = self._fit_rect()
            if fit.contains(at):
                self._center = QPointF((at.x() - fit.x()) / fit.width(), (at.y() - fit.y()) / fit.height())
            else:
                self._center = QPointF(0.5, 0.5)
        elif not self._zoomed:
            self._center = QPointF(0.5, 0.5)
        self._zoomed = not self._zoomed
        self._update_cursor()
        self.update()
        self.zoom_changed.emit(self._zoomed)
        self.view_changed.emit(self._zoomed, self._center)

    def reset_zoom(self):
        if self._zoomed:
            self.toggle_zoom()

    def set_view(self, zoomed, center):
        """Follow another view's zoom and pan (Compare), without emitting signals."""
        self._zoomed = zoomed
        self._center = QPointF(center)
        self._update_cursor()
        self.update()

    # geometry

    def _fit_rect(self):
        bounds = self.rect().adjusted(self.MARGIN, self.MARGIN, -self.MARGIN, -self.MARGIN)
        return fit_rect(self._pixmap.size(), bounds)

    def _zoom_rect(self):
        # At the photo's own size, whatever the size of the pixmap shown so far.
        size = QSizeF(self._native or self._pixmap.size()) / self.devicePixelRatioF()
        x = self.width() / 2 - self._center.x() * size.width()
        y = self.height() / 2 - self._center.y() * size.height()
        # Keep the image on screen; center it on any axis where it is smaller.
        x = (self.width() - size.width()) / 2 if size.width() <= self.width() else min(0, max(self.width() - size.width(), x))
        y = (self.height() - size.height()) / 2 if size.height() <= self.height() else min(0, max(self.height() - size.height(), y))
        return QRectF(x, y, size.width(), size.height())

    def _update_cursor(self):
        if self._crop is not None:
            return  # follows the handle under the mouse
        if self._zoomed and self._pixmap is not None:
            self.setCursor(Qt.CursorShape.ClosedHandCursor if self._drag_from else Qt.CursorShape.OpenHandCursor)
        else:
            self.unsetCursor()

    # events

    def resizeEvent(self, event):
        self._scaled = None
        super().resizeEvent(event)

    def mouseDoubleClickEvent(self, event):
        if self._crop is not None:
            self.crop_confirmed.emit()
            return
        self.toggle_zoom(event.position().toPoint())

    def mousePressEvent(self, event):
        self.clicked.emit()
        if self._crop is not None:
            if self._pixmap is not None and event.button() == Qt.MouseButton.LeftButton:
                handle = self._crop_handle_at(event.position())
                if handle:
                    self._crop_drag = (handle, event.position(), self._crop)
                    self._update_crop_cursor(event.position())
            return
        if self._zoomed and self._pixmap is not None and event.button() == Qt.MouseButton.LeftButton:
            self._drag_from = event.position()
            self._update_cursor()

    def mouseMoveEvent(self, event):
        if self._crop is not None:
            if self._crop_drag and self._pixmap is not None:
                handle, start, frame = self._crop_drag
                fit = self._fit_rect()
                delta = event.position() - start
                self._crop = cropping.drag(
                    frame, handle, delta.x() / fit.width(), delta.y() / fit.height(), self._crop_aspect()
                )
                self.update()
            if self._pixmap is not None:
                self._update_crop_cursor(event.position())
            return
        if self._drag_from is None:
            return
        rect = self._zoom_rect()
        delta = event.position() - self._drag_from
        self._drag_from = event.position()
        self._center = QPointF(
            min(1.0, max(0.0, self._center.x() - delta.x() / rect.width())),
            min(1.0, max(0.0, self._center.y() - delta.y() / rect.height())),
        )
        self.update()
        self.view_changed.emit(True, self._center)

    def mouseReleaseEvent(self, event):
        if self._crop is not None:
            self._crop_drag = None
            if self._pixmap is not None:
                self._update_crop_cursor(event.position())
            return
        self._drag_from = None
        self._update_cursor()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#000000"))

        if self._pixmap is None:
            painter.setPen(QColor(COLOR_MUTED))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._message)
            self._draw_active(painter)
            return

        if self._zoomed:
            target = self._zoom_rect()
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.drawPixmap(target, self._pixmap, QRectF(self._pixmap.rect()))
            self._draw_overlays(painter, target)
        else:
            fit = self._fit_rect()
            dpr = self.devicePixelRatioF()
            if self._scaled is None or self._scaled.size() != fit.size() * dpr:
                self._scaled = self._pixmap.scaled(
                    fit.size() * dpr,
                    Qt.AspectRatioMode.IgnoreAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                self._scaled.setDevicePixelRatio(dpr)
            painter.drawPixmap(fit.topLeft(), self._scaled)
            target = QRectF(fit)
            self._draw_overlays(painter, target)
            if self._crop is not None:
                self._draw_crop(painter, fit)
                return

        visible = target.intersected(QRectF(self.rect()))
        color = STATUS_COLORS.get(self._status)
        if color is not None:
            painter.setPen(QPen(color, 4))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(visible.adjusted(2, 2, -2, -2))
            self._draw_status_pill(painter, visible, color)
        if self._rating or self._label:
            self._draw_rating_pill(painter, visible)
        if self._info:
            self._draw_info(painter, visible)
        if self._note:
            self._draw_note(painter, self._note)
        elif self._zoomed and self._full_loading:
            self._draw_note(painter, "Loading full resolution…")
        self._draw_active(painter)

    def _draw_overlays(self, painter, target):
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        for overlay in (self._clipping, self._overlay):
            if overlay is not None:
                painter.drawPixmap(target, overlay, QRectF(overlay.rect()))

    def _draw_status_pill(self, painter, rect, color):
        painter.save()
        label = "KEEP" if self._status == KEEP else "REJECT"
        font = painter.font()
        font.setBold(True)
        painter.setFont(font)
        metrics = QFontMetrics(font)
        height = metrics.height() + 10
        width = metrics.horizontalAdvance(label) + height + 14
        pill = QRectF(rect.left() + 14, rect.top() + 14, width, height)
        _fill_pill(painter, pill)
        radius = height / 2 - 4
        draw_status_badge(painter, QPointF(pill.left() + height / 2, pill.center().y()), radius, self._status)
        painter.setPen(color)
        painter.drawText(
            pill.adjusted(height, 0, -8, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, label
        )
        painter.restore()

    def _draw_rating_pill(self, painter, rect):
        painter.save()
        metrics = QFontMetrics(painter.font())
        height = metrics.height() + 10
        stars = stars_text(self._rating) if self._rating else ""
        dot = height - 14 if self._label else 0
        gap = 8 if stars and dot else 0
        width = metrics.horizontalAdvance(stars) + dot + gap + 20
        pill = QRectF(rect.right() - 14 - width, rect.top() + 14, width, height)
        _fill_pill(painter, pill)
        x = pill.left() + 10
        if dot:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(LABEL_COLORS.get(self._label, COLOR_MUTED)))
            painter.drawEllipse(QRectF(x, pill.center().y() - dot / 2, dot, dot))
            x += dot + gap
        if stars:
            painter.setPen(QColor(COLOR_STAR))
            painter.drawText(QRectF(x, pill.top(), pill.right() - x, height), Qt.AlignmentFlag.AlignVCenter, stars)
        painter.restore()

    def _draw_info(self, painter, rect):
        painter.save()
        font = painter.font()
        font.setPointSizeF(font.pointSizeF() * 0.92)
        painter.setFont(font)
        metrics = QFontMetrics(font)
        line_height = metrics.height()
        width = max(metrics.horizontalAdvance(line) for line in self._info) + 24
        height = line_height * len(self._info) + 12
        box = QRectF(rect.right() - width - 14, rect.bottom() - height - 14, width, height)
        path = QPainterPath()
        path.addRoundedRect(box, 8, 8)
        painter.fillPath(path, QColor(0, 0, 0, 170))
        painter.setPen(QColor(COLOR_TEXT))
        for i, line in enumerate(self._info):
            painter.drawText(
                QRectF(box.left() + 12, box.top() + 6 + i * line_height, width - 24, line_height),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                line,
            )
        painter.restore()

    def _draw_note(self, painter, text):
        metrics = QFontMetrics(painter.font())
        note = QRectF(14, self.height() - metrics.height() - 24, metrics.horizontalAdvance(text) + 20, metrics.height() + 10)
        _fill_pill(painter, note)
        painter.setPen(QColor(COLOR_TEXT))
        painter.drawText(note, Qt.AlignmentFlag.AlignCenter, text)

    def _draw_active(self, painter):
        if self._active:
            painter.setPen(QPen(QColor(COLOR_ACCENT), 3))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5))


class CompareView(QWidget):
    """Two to four photos side by side; zoom and pan are shared between them."""

    activated = pyqtSignal(int)  # index of the view the user clicked
    zoom_changed = pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._views = []
        self._zoomed = False
        self._layout = QGridLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(2)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    @property
    def views(self):
        return self._views

    @property
    def zoomed(self):
        return self._zoomed

    def set_count(self, count):
        while len(self._views) < count:
            view = ImageView()
            view.MARGIN = 8
            view.view_changed.connect(partial(self._sync, view))
            view.clicked.connect(partial(self._on_clicked, view))
            self._views.append(view)
        while len(self._views) > count:
            self._views.pop().deleteLater()
        for view in self._views:
            self._layout.removeWidget(view)
        columns = 2 if count == 4 else max(1, count)
        for i, view in enumerate(self._views):
            self._layout.addWidget(view, i // columns, i % columns)
        self._zoomed = False
        for view in self._views:
            view.set_view(False, QPointF(0.5, 0.5))

    def toggle_zoom(self, index):
        self._views[index].toggle_zoom()

    def reset_zoom(self):
        for view in self._views:
            view.set_view(False, QPointF(0.5, 0.5))
        self._set_zoomed(False)

    def _sync(self, source, zoomed, center):
        for view in self._views:
            if view is not source:
                view.set_view(zoomed, center)
        self._set_zoomed(zoomed)

    def _set_zoomed(self, zoomed):
        if zoomed != self._zoomed:
            self._zoomed = zoomed
            self.zoom_changed.emit(zoomed)

    def _on_clicked(self, view):
        self.activated.emit(self._views.index(view))


# -- bars ------------------------------------------------------------------

class HeaderBar(QFrame):
    open_folder_requested = pyqtSignal()
    import_requested = pyqtSignal()
    refresh_requested = pyqtSignal()
    trash_requested = pyqtSignal()
    process_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("HeaderBar")

        title = QLabel("Tamis")
        title.setObjectName("AppTitle")

        self.lbl_folder = QLabel("No folder open")
        self.lbl_folder.setObjectName("Muted")
        self.lbl_folder.setMinimumWidth(80)

        self.lbl_counter = QLabel("")
        self.lbl_counter.setObjectName("Counter")
        self.lbl_counter.hide()

        btn_open = QPushButton("Open Folder…")
        btn_open.clicked.connect(self.open_folder_requested)

        btn_import = QPushButton("Import…")
        import_key = QKeySequence("Ctrl+I").toString(QKeySequence.SequenceFormat.NativeText)
        btn_import.setToolTip(f"Copy photos from a memory card ({import_key})")
        btn_import.clicked.connect(self.import_requested)

        btn_refresh = QPushButton("Refresh")
        refresh_key = QKeySequence("Ctrl+R").toString(QKeySequence.SequenceFormat.NativeText)
        btn_refresh.setToolTip(f"Look for new photos in the folder ({refresh_key})")
        btn_refresh.clicked.connect(self.refresh_requested)

        self.btn_trash = QPushButton("Trash Rejects…")
        self.btn_trash.setToolTip("Move the photos in _REJECTS to the system Trash")
        self.btn_trash.setEnabled(False)
        self.btn_trash.clicked.connect(self.trash_requested)

        self.btn_process = QPushButton("Process Files…")
        self.btn_process.setObjectName("Primary")
        self.btn_process.setEnabled(False)
        self.btn_process.clicked.connect(self.process_requested)

        for button in (btn_open, btn_import, btn_refresh, self.btn_trash, self.btn_process):
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 8, 14, 8)
        layout.setSpacing(10)
        layout.addWidget(title)
        layout.addWidget(self.lbl_folder, 1)
        layout.addWidget(self.lbl_counter)
        layout.addWidget(btn_open)
        layout.addWidget(btn_import)
        layout.addWidget(btn_refresh)
        layout.addWidget(self.btn_trash)
        layout.addWidget(self.btn_process)
        self._folder_text = ""

    def set_folder(self, path, filename=""):
        self._folder_text = f"{path}  ›  {filename}" if filename else path
        self._elide_folder()
        self.lbl_folder.setToolTip(path)

    def set_counter(self, text):
        self.lbl_counter.setText(text)
        self.lbl_counter.setVisible(bool(text))

    def set_can_process(self, enabled):
        self.btn_process.setEnabled(enabled)
        self.btn_trash.setEnabled(enabled)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._elide_folder()

    def _elide_folder(self):
        metrics = self.lbl_folder.fontMetrics()
        self.lbl_folder.setText(
            metrics.elidedText(self._folder_text, Qt.TextElideMode.ElideLeft, max(40, self.lbl_folder.width()))
        )


class StatsBar(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("StatsBar")

        self.lbl_keep = QLabel()
        self.lbl_keep.setObjectName("KeepCount")
        self.lbl_reject = QLabel()
        self.lbl_reject.setObjectName("RejectCount")
        self.lbl_unmarked = QLabel()
        self.lbl_unmarked.setObjectName("Muted")
        self.lbl_analysis = QLabel()
        self.lbl_analysis.setObjectName("Muted")

        native = QKeySequence.SequenceFormat.NativeText
        undo = QKeySequence(QKeySequence.StandardKey.Undo).toString(native)
        mod = QKeySequence("Ctrl+1").toString(native)[:-1]  # "⌘" on macOS, "Ctrl+" elsewhere
        hint = QLabel(
            f"P keep · X reject · U unmark · {mod}1–5 stars · {mod}6–9 label · N next unmarked · K keeps only · "
            f"B bursts · ⇧P keep one of a burst · C compare · E edit · Y hold for before · R crop · [ ] rotate · "
            f"J clipping · {mod}C / {mod}V copy / paste edit · "
            f"Z zoom · F focus peaking · I info · G gallery · {undo} undo"
        )
        hint.setToolTip(hint.text())
        # May be cut off in a narrow window rather than force the window wider.
        hint.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        hint.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        hint.setObjectName("Muted")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 6, 14, 6)
        layout.setSpacing(18)
        layout.addWidget(self.lbl_keep)
        layout.addWidget(self.lbl_reject)
        layout.addWidget(self.lbl_unmarked)
        layout.addWidget(self.lbl_analysis)
        layout.addWidget(hint, 1)
        self.update_counts(0, 0, 0)

    def update_counts(self, keeps, rejects, unmarked):
        self.lbl_keep.setText(f"Keeps  {keeps}")
        self.lbl_reject.setText(f"Rejects  {rejects}")
        self.lbl_unmarked.setText(f"Unmarked  {unmarked}")

    def set_analysis_progress(self, done, total):
        self.lbl_analysis.setText(f"Analysing {done} / {total}" if total else "")


class GalleryToolbar(QFrame):
    filter_changed = pyqtSignal(str)
    rating_changed = pyqtSignal(int)  # minimum stars
    labels_changed = pyqtSignal(list)  # colour labels to show; empty: all
    focus_changed = pyqtSignal(str)
    sort_changed = pyqtSignal(str)
    bursts_toggled = pyqtSignal(bool)
    size_changed = pyqtSignal(int)

    FILTERS = (("all", "All"), ("unmarked", "Unmarked"), (KEEP, "Keeps"), (REJECT, "Rejects"))
    FOCUS = ((StatusFilterProxy.ALL, "Any focus"), (StatusFilterProxy.SHARP, "Looks sharp"), (StatusFilterProxy.SOFT, "Looks soft"))
    SORTS = (("name", "Sort by name"), ("date", "Sort by date"), ("sharpness", "Sort by sharpness"))

    def __init__(self, initial_size, sort_order="name", bursts=False, parent=None):
        super().__init__(parent)
        self.setObjectName("GalleryToolbar")
        rows = QVBoxLayout(self)
        rows.setContentsMargins(12, 6, 12, 6)
        rows.setSpacing(6)
        layout = QHBoxLayout()
        layout.setSpacing(6)
        settings = QHBoxLayout()
        settings.setSpacing(6)
        rows.addLayout(layout)
        rows.addLayout(settings)

        self._buttons = {}
        group = QButtonGroup(self)
        group.setExclusive(True)
        for key, label in self.FILTERS:
            button = QPushButton(label)
            button.setObjectName("Chip")
            button.setCheckable(True)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.clicked.connect(lambda _checked, k=key: self.filter_changed.emit(k))
            group.addButton(button)
            layout.addWidget(button)
            self._buttons[key] = (button, label)
        self._buttons["all"][0].setChecked(True)
        layout.addSpacing(10)

        self.rating_combo = self._combo([(stars, f"{'★' * stars} or more" if stars else "Any stars") for stars in range(6)])
        self.rating_combo.setToolTip("Only show photos with at least this many stars")
        self.rating_combo.currentIndexChanged.connect(lambda _index: self.rating_changed.emit(self.rating_combo.currentData()))
        layout.addWidget(self.rating_combo)

        self.label_buttons = {}
        for label, color in LABEL_COLORS.items():
            button = QPushButton()
            button.setCheckable(True)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.setFixedSize(18, 18)
            button.setToolTip(f"Only show photos labelled {label.lower()} (several can be on)")
            button.setStyleSheet(
                f"QPushButton {{ background: {color}; border: 2px solid transparent; border-radius: 9px; padding: 0; }}"
                f"QPushButton:hover {{ background: {color}; border-color: {COLOR_MUTED}; }}"
                f"QPushButton:checked {{ background: {color}; border-color: white; }}"
            )
            button.clicked.connect(lambda _checked: self.labels_changed.emit(self.checked_labels()))
            layout.addWidget(button)
            self.label_buttons[label] = button
        layout.addSpacing(10)

        self.focus_combo = self._combo(self.FOCUS)
        self.focus_combo.setToolTip("A suggestion, from comparing each photo's sharpness with the folder's median")
        self.focus_combo.currentIndexChanged.connect(lambda _index: self.focus_changed.emit(self.focus_combo.currentData()))
        layout.addWidget(self.focus_combo)
        layout.addStretch()

        self.sort_combo = self._combo(self.SORTS)
        self.sort_combo.setCurrentIndex(max(0, self.sort_combo.findData(sort_order)))
        self.sort_combo.currentIndexChanged.connect(lambda _index: self.sort_changed.emit(self.sort_combo.currentData()))
        settings.addWidget(self.sort_combo)

        self.bursts_button = QPushButton("Bursts")
        self.bursts_button.setObjectName("Chip")
        self.bursts_button.setCheckable(True)
        self.bursts_button.setChecked(bursts)
        self.bursts_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.bursts_button.setToolTip("Group the shots taken in quick succession (B)")
        self.bursts_button.clicked.connect(self.bursts_toggled)
        settings.addWidget(self.bursts_button)

        settings.addStretch()
        size_label = QLabel("Size")
        size_label.setObjectName("Muted")
        settings.addWidget(size_label)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(90, 240)
        self.slider.setValue(initial_size)
        self.slider.setFixedWidth(140)
        self.slider.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.slider.valueChanged.connect(self.size_changed)
        settings.addWidget(self.slider)

    @staticmethod
    def _combo(items):
        combo = QComboBox()
        combo.setObjectName("Compact")
        combo.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        for value, text in items:
            combo.addItem(text, value)
        return combo

    def set_sort_order(self, order):
        self.sort_combo.blockSignals(True)  # the app is what is telling
        self.sort_combo.setCurrentIndex(max(0, self.sort_combo.findData(order)))
        self.sort_combo.blockSignals(False)

    def checked_labels(self):
        return [label for label, button in self.label_buttons.items() if button.isChecked()]

    def update_counts(self, total, keeps, rejects, unmarked):
        counts = {"all": total, "unmarked": unmarked, KEEP: keeps, REJECT: rejects}
        for key, (button, label) in self._buttons.items():
            button.setText(f"{label}  {counts[key]}")


class Toast(QLabel):
    """Short non-blocking message shown over the window."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setObjectName("Toast")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.hide()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)

    def show_message(self, text, msecs=2200):
        self.setText(text)
        self.adjustSize()
        parent = self.parentWidget()
        self.move((parent.width() - self.width()) // 2, parent.height() - self.height() - 70)
        self.raise_()
        self.show()
        self._timer.start(msecs)


# -- dialogs ---------------------------------------------------------------
# Plain QDialogs rather than QMessageBox: on recent macOS, QMessageBox's
# native NSAlert crashes AppKit (SIGTRAP during layout).

class MessageDialog(QDialog):
    def __init__(self, parent, title, text, confirm_text=None, details=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(420)

        heading = QLabel(title)
        heading.setObjectName("DialogTitle")
        self.body = body = QLabel(text)
        body.setWordWrap(True)
        body.setTextFormat(Qt.TextFormat.RichText)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(12)
        layout.addWidget(heading)
        layout.addWidget(body)

        if details:
            box = QPlainTextEdit("\n".join(details))
            box.setReadOnly(True)
            box.setMinimumHeight(140)
            layout.addWidget(box)

        buttons = QDialogButtonBox()
        if confirm_text:
            buttons.addButton("Cancel", QDialogButtonBox.ButtonRole.RejectRole)
            ok = buttons.addButton(confirm_text, QDialogButtonBox.ButtonRole.AcceptRole)
        else:
            ok = buttons.addButton("OK", QDialogButtonBox.ButtonRole.AcceptRole)
        ok.setObjectName("Primary")
        ok.setDefault(True)
        self.confirm_button = ok
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class NameDialog(MessageDialog):
    """Asks for a name. Accepted, `name` is what was typed, never blank."""

    def __init__(self, parent, title, text, confirm_text):
        super().__init__(parent, title, text, confirm_text=confirm_text)
        self.field = QLineEdit()
        self.field.textChanged.connect(lambda text: self.confirm_button.setEnabled(bool(text.strip())))
        self.confirm_button.setEnabled(False)
        layout = self.layout()
        layout.insertWidget(layout.count() - 1, self.field)  # between the text and the buttons

    @property
    def name(self):
        return self.field.text().strip()


class ProcessDialog(MessageDialog):
    """Confirms "Process Files", with a choice of whether to fill the export folder too.

    Without an export_label there is nothing to export, so no choice is shown.
    With `describe`, the dialog also offers what to export (exportsettings.SCOPES)
    and a button for the export settings: `describe(scope)` gives the
    (label, details, anything to export) to show for a scope, and is asked
    again when the scope or the settings change. `edit_settings()` opens the
    settings, returning whether they changed.
    """

    def __init__(self, parent, moves_text, export_label=None, export_text="", scope="all", describe=None, edit_settings=None):
        super().__init__(parent, "Process files", moves_text, confirm_text="Process")
        self.body.setVisible(bool(moves_text))
        self._has_moves = bool(moves_text)
        self._describe = describe
        self._can_export = True
        self.export_check = None
        self.scope_combo = None
        if not export_label:
            return
        self.export_check = QCheckBox(export_label)
        self.export_check.setChecked(True)
        self.export_details = QLabel(export_text)
        self.export_details.setWordWrap(True)
        self.export_details.setTextFormat(Qt.TextFormat.RichText)
        self.export_check.toggled.connect(self._update_confirm)
        widgets = [self.export_check]
        if describe is not None:
            self.scope_combo = QComboBox()
            for value, title in SCOPES:
                self.scope_combo.addItem(title, value)
            self.scope_combo.setCurrentIndex(max(0, self.scope_combo.findData(scope)))
            self.scope_combo.currentIndexChanged.connect(lambda _index: self.refresh())
            row = QHBoxLayout()
            row.setContentsMargins(24, 0, 0, 0)
            row.addWidget(QLabel("Export"))
            row.addWidget(self.scope_combo, 1)
            if edit_settings is not None:
                settings_button = QPushButton("Export Settings…")
                settings_button.clicked.connect(lambda: edit_settings() and self.refresh())
                row.addWidget(settings_button)
            widgets.append(row)
        widgets.append(self.export_details)
        # Between the text and the buttons, which are the last item.
        layout = self.layout()
        for widget in widgets:
            if isinstance(widget, QHBoxLayout):
                layout.insertLayout(layout.count() - 1, widget)
            else:
                layout.insertWidget(layout.count() - 1, widget)
        if describe is not None:
            self.refresh()

    @property
    def scope(self):
        return self.scope_combo.currentData() if self.scope_combo is not None else "all"

    def refresh(self):
        """Show what the chosen scope would export."""
        label, details, can_export = self._describe(self.scope)
        self.export_check.setText(label)
        self.export_details.setText(details)
        self._can_export = can_export
        self.export_check.setEnabled(can_export)
        self._update_confirm()

    def _update_confirm(self, _checked=None):
        self.export_details.setEnabled(self.export_requested)
        self.confirm_button.setEnabled(self.export_requested or self._has_moves)  # otherwise there is nothing to do

    @property
    def export_requested(self):
        return self.export_check is not None and self._can_export and self.export_check.isChecked()


class ExportSettingsDialog(MessageDialog):
    """Size, quality, destination and names of exports, with named profiles.

    Accepted, `settings` is what was chosen. Profiles are saved and deleted
    straight away, through `save_profiles(profiles)`, which may raise OSError.
    """

    def __init__(self, parent, settings, profiles, shoot_folder="", save_profiles=None):
        super().__init__(parent, "Export settings", "", confirm_text="OK")
        self.setMinimumWidth(520)
        self.profiles = dict(profiles)
        self.shoot_folder = shoot_folder
        self._save_profiles = save_profiles

        self.profile_combo = QComboBox()
        self.save_profile_button = QPushButton("Save as Profile…")
        self.delete_profile_button = QPushButton("Delete")
        self.long_edge = QSpinBox()
        self.long_edge.setRange(0, LONG_EDGE_MAX)
        self.long_edge.setSingleStep(100)
        self.long_edge.setSpecialValueText("Full size")
        self.long_edge.setSuffix(" px")
        self.quality = QSpinBox()
        self.quality.setRange(*QUALITY_RANGE)
        self.folder_edit = QLineEdit()
        self.folder_edit.setPlaceholderText(EXPORT_DIR)
        self.folder_edit.setToolTip("Relative to the shoot folder, or a full path")
        browse = QPushButton("Choose…")
        browse.clicked.connect(self._browse)
        self.pattern_edit = QLineEdit()
        self.pattern_edit.setToolTip("Fields: " + ", ".join(f"{{{token}}}" for token in TOKENS))

        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        profile_row = QHBoxLayout()
        profile_row.addWidget(self.profile_combo, 1)
        profile_row.addWidget(self.save_profile_button)
        profile_row.addWidget(self.delete_profile_button)
        folder_row = QHBoxLayout()
        folder_row.addWidget(self.folder_edit, 1)
        folder_row.addWidget(browse)
        rows = (
            ("Profile", profile_row),
            ("Long edge", self.long_edge),
            ("Quality", self.quality),
            ("Folder", folder_row),
            ("Name", self.pattern_edit),
        )
        for row, (title, item) in enumerate(rows):
            grid.addWidget(QLabel(title), row, 0)
            if isinstance(item, QHBoxLayout):
                grid.addLayout(item, row, 1)
            else:
                grid.addWidget(item, row, 1)
        self.layout().insertLayout(1, grid)  # under the heading, above the summary

        self._fill_profiles()
        self._show(settings)
        self.profile_combo.activated.connect(self._on_profile_chosen)
        self.save_profile_button.clicked.connect(self._save_profile)
        self.delete_profile_button.clicked.connect(self._delete_profile)
        for spin in (self.long_edge, self.quality):
            spin.valueChanged.connect(self._update)
        for edit in (self.folder_edit, self.pattern_edit):
            edit.textChanged.connect(self._update)
        self._update()

    @property
    def settings(self):
        return ExportSettings(
            self.long_edge.value(),
            self.quality.value(),
            self.folder_edit.text().strip() or EXPORT_DIR,
            self.pattern_edit.text().strip(),
        )

    def _show(self, settings):
        self.long_edge.setValue(settings.long_edge)
        self.quality.setValue(settings.quality)
        self.folder_edit.setText(settings.folder)
        self.pattern_edit.setText(settings.pattern)

    def _fill_profiles(self):
        self.profile_combo.clear()
        self.profile_combo.addItem("Choose a profile…", None)
        for name in sorted(self.profiles, key=str.casefold):
            self.profile_combo.addItem(name, name)

    def _matching_profile(self):
        settings = self.settings
        return next((name for name, saved in self.profiles.items() if saved == settings), None)

    def _on_profile_chosen(self, index):
        name = self.profile_combo.itemData(index)
        if name in self.profiles:
            self._show(self.profiles[name])

    def _store(self, profiles):
        try:
            if self._save_profiles is not None:
                self._save_profiles(profiles)
        except OSError as exc:
            inform(self, "The profiles could not be saved", str(exc))
            return
        self.profiles = profiles
        self._fill_profiles()
        self._update()

    def _save_profile(self):
        dialog = NameDialog(self, "Save export profile", "Name of the profile:", "Save")
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._store({**self.profiles, dialog.name: self.settings})

    def _delete_profile(self):
        name = self._matching_profile()
        if name is not None:
            self._store({key: value for key, value in self.profiles.items() if key != name})

    def _browse(self):
        start = self.settings.dest_dir(self.shoot_folder) if self.shoot_folder else os.path.expanduser("~")
        folder = QFileDialog.getExistingDirectory(self, "Export folder", start)
        if folder:
            self.folder_edit.setText(folder)

    def _problem(self):
        problem = pattern_problem(self.pattern_edit.text())
        if problem is None and self.shoot_folder:
            problem = self.settings.problem(self.shoot_folder)
        return problem

    def _update(self):
        name = self._matching_profile()
        self.profile_combo.setCurrentIndex(max(0, self.profile_combo.findData(name)) if name else 0)
        self.delete_profile_button.setEnabled(name is not None)
        problem = self._problem()
        self.confirm_button.setEnabled(problem is None)
        if problem:
            self.body.setText(f"<b>{escape(problem)}</b>")
            return
        settings = self.settings
        example = file_stem(settings.pattern, "IMG_0042", "2026-05-17", 7, 120, "Wedding")
        self.body.setText(
            f"For example: IMG_0042 → <b>{escape(example)}.jpg</b>"
            "<br>Fields: " + ", ".join(f"{{{token}}}" for token in TOKENS) + " ({seq} counts the photos of one export)."
            "<br>Unedited JPEGs are copied as they are unless they need resizing; rendered ones "
            "carry the original's EXIF and an sRGB profile."
        )


class ImportDialog(MessageDialog):
    """Asks what to copy where. Accepted, `groups`, `destinations` and `destination` say what was chosen."""

    def __init__(self, parent, source="", destination="", backup=""):
        super().__init__(parent, "Import photos", "", confirm_text="Import")
        self.groups = []
        self.size = 0
        self.setMinimumWidth(560)
        self.source_edit = QLineEdit(source)
        self.destination_edit = QLineEdit(destination)
        self.backup_check = QCheckBox("Also copy to a backup folder")
        self.backup_check.setChecked(bool(backup))
        self.backup_edit = QLineEdit(backup)

        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        rows = (
            (QLabel("From"), self.source_edit, "From the card or folder"),
            (QLabel("To"), self.destination_edit, "Destination folder"),
            (self.backup_check, self.backup_edit, "Backup folder"),
        )
        for row, (label, edit, title) in enumerate(rows):
            browse = QPushButton("Choose…")
            browse.clicked.connect(partial(self._browse, edit, title))
            grid.addWidget(label, row, 0)
            grid.addWidget(edit, row, 1)
            grid.addWidget(browse, row, 2)
        layout = self.layout()
        layout.insertLayout(1, grid)  # under the heading, above the summary

        self.source_edit.editingFinished.connect(self._scan)
        self.destination_edit.textChanged.connect(self._update)
        self.backup_edit.textChanged.connect(self._update)
        self.backup_check.toggled.connect(self._update)
        self._scan()

    @property
    def destination(self):
        return self.destination_edit.text().strip()

    @property
    def backup(self):
        return self.backup_edit.text().strip() if self.backup_check.isChecked() else ""

    @property
    def destinations(self):
        return [self.destination, *([self.backup] if self.backup else [])]

    def _browse(self, edit, title):
        folder = QFileDialog.getExistingDirectory(self, title, edit.text() or os.path.expanduser("~"))
        if folder:
            edit.setText(folder)
            if edit is self.source_edit:
                self._scan()
            elif edit is self.backup_edit:
                self.backup_check.setChecked(True)

    def _scan(self):
        source = self.source_edit.text().strip()
        self.groups = plan_import(source) if source and os.path.isdir(source) else []
        self.size = import_size(self.groups)  # once: the summary is worked out at every key pressed
        self._update()

    def _problem(self):
        """Why importing can't go ahead, None if it can."""
        source = self.source_edit.text().strip()
        if not self.groups:
            return "No photos found there." if source else "Choose where to import from."
        if not self.destination:
            return "Choose a destination folder."
        if self.backup_check.isChecked() and not self.backup:
            return "Choose a backup folder."
        if self.backup and os.path.abspath(self.backup) == os.path.abspath(self.destination):
            return "The backup folder must not be the destination."
        for target in self.destinations:
            inside = os.path.commonpath([os.path.abspath(source), os.path.abspath(target)]) == os.path.abspath(source)
            if inside:
                return f"{target} is inside the folder being imported."
            existing = target
            while existing and not os.path.isdir(existing) and existing != os.path.dirname(existing):
                existing = os.path.dirname(existing)
            if os.path.isdir(existing) and shutil.disk_usage(existing).free < self.size:
                return f"Not enough free space in {target}."
        return None

    def _update(self):
        problem = self._problem()
        self.confirm_button.setEnabled(problem is None)
        if problem:
            self.body.setText(problem)
        else:
            self.body.setText(
                f"<b>{len(self.groups)}</b> photo(s), {size_text(self.size)}. Every copy is read back and "
                "compared with the original, files already there are skipped, and nothing on the card changes."
            )


def inform(parent, title, text, details=None):
    MessageDialog(parent, title, text, details=details).exec()
