"""The Cull tab's photo view, with zoom, crop and overlays, and the views of Compare."""

from functools import partial

from PyQt6.QtCore import QPointF, QRectF, QSize, QSizeF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFontMetrics, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QGridLayout, QWidget

from . import cropping
from .constants import (
    COLOR_ACCENT,
    COLOR_MUTED,
    COLOR_STAR,
    COLOR_TEXT,
    KEEP,
    LABEL_COLORS,
)
from .widgets import STATUS_COLORS, draw_status_badge, fill_pill, fit_rect, stars_text


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
        fill_pill(painter, note)
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
        fill_pill(painter, pill)
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
        fill_pill(painter, pill)
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
        fill_pill(painter, note)
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
