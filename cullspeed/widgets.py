from functools import partial

from PyQt6.QtCore import QPoint, QPointF, QRect, QRectF, QSize, QSizeF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFontMetrics, QKeySequence, QPainter, QPainterPath, QPen, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListView,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSlider,
    QStyle,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)

from .constants import (
    COLOR_ACCENT,
    COLOR_CARD,
    COLOR_CARD_HOVER,
    COLOR_KEEP,
    COLOR_MUTED,
    COLOR_REJECT,
    COLOR_STAR,
    COLOR_TEXT,
    KEEP,
    LABEL_COLORS,
    REJECT,
)
from .models import LABEL_ROLE, RATING_ROLE, STATUS_ROLE, THUMBNAIL_FAILED_ROLE

STATUS_COLORS = {KEEP: QColor(COLOR_KEEP), REJECT: QColor(COLOR_REJECT)}


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
        painter.restore()


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

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pixmap = None
        self._source_key = None  # cacheKey() of the pixmap last given to set_image()
        self._native = None  # size of the photo itself, which a preview is smaller than
        self._scaled = None
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
        if self._zoomed and self._pixmap is not None:
            self.setCursor(Qt.CursorShape.ClosedHandCursor if self._drag_from else Qt.CursorShape.OpenHandCursor)
        else:
            self.unsetCursor()

    # events

    def resizeEvent(self, event):
        self._scaled = None
        super().resizeEvent(event)

    def mouseDoubleClickEvent(self, event):
        self.toggle_zoom(event.position().toPoint())

    def mousePressEvent(self, event):
        self.clicked.emit()
        if self._zoomed and self._pixmap is not None and event.button() == Qt.MouseButton.LeftButton:
            self._drag_from = event.position()
            self._update_cursor()

    def mouseMoveEvent(self, event):
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
        if self._zoomed and self._full_loading:
            self._draw_loading_note(painter)
        self._draw_active(painter)

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

    def _draw_loading_note(self, painter):
        text = "Loading full resolution…"
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
    refresh_requested = pyqtSignal()
    process_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("HeaderBar")

        title = QLabel("CullSpeed")
        title.setObjectName("AppTitle")

        self.lbl_folder = QLabel("No folder open")
        self.lbl_folder.setObjectName("Muted")
        self.lbl_folder.setMinimumWidth(80)

        self.lbl_counter = QLabel("")
        self.lbl_counter.setObjectName("Counter")
        self.lbl_counter.hide()

        btn_open = QPushButton("Open Folder…")
        btn_open.clicked.connect(self.open_folder_requested)

        btn_refresh = QPushButton("Refresh")
        refresh_key = QKeySequence("Ctrl+R").toString(QKeySequence.SequenceFormat.NativeText)
        btn_refresh.setToolTip(f"Look for new photos in the folder ({refresh_key})")
        btn_refresh.clicked.connect(self.refresh_requested)

        self.btn_process = QPushButton("Process Files…")
        self.btn_process.setObjectName("Primary")
        self.btn_process.setEnabled(False)
        self.btn_process.clicked.connect(self.process_requested)

        for button in (btn_open, btn_refresh, self.btn_process):
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 8, 14, 8)
        layout.setSpacing(10)
        layout.addWidget(title)
        layout.addWidget(self.lbl_folder, 1)
        layout.addWidget(self.lbl_counter)
        layout.addWidget(btn_open)
        layout.addWidget(btn_refresh)
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

        native = QKeySequence.SequenceFormat.NativeText
        undo = QKeySequence(QKeySequence.StandardKey.Undo).toString(native)
        mod = QKeySequence("Ctrl+1").toString(native)[:-1]  # "⌘" on macOS, "Ctrl+" elsewhere
        hint = QLabel(
            f"P keep · X reject · U unmark · {mod}1–5 stars · {mod}6–9 label · N next unmarked · "
            f"C compare · Z zoom · I info · G gallery · {undo} undo"
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
        layout.addWidget(hint, 1)
        self.update_counts(0, 0, 0)

    def update_counts(self, keeps, rejects, unmarked):
        self.lbl_keep.setText(f"Keeps  {keeps}")
        self.lbl_reject.setText(f"Rejects  {rejects}")
        self.lbl_unmarked.setText(f"Unmarked  {unmarked}")


class GalleryToolbar(QFrame):
    filter_changed = pyqtSignal(str)
    size_changed = pyqtSignal(int)

    FILTERS = (("all", "All"), ("unmarked", "Unmarked"), (KEEP, "Keeps"), (REJECT, "Rejects"))

    def __init__(self, initial_size, parent=None):
        super().__init__(parent)
        self.setObjectName("GalleryToolbar")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 6, 12, 6)
        layout.setSpacing(6)

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

        layout.addStretch()
        size_label = QLabel("Size")
        size_label.setObjectName("Muted")
        layout.addWidget(size_label)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(90, 240)
        self.slider.setValue(initial_size)
        self.slider.setFixedWidth(140)
        self.slider.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.slider.valueChanged.connect(self.size_changed)
        layout.addWidget(self.slider)

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
        body = QLabel(text)
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
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


def ask(parent, title, text, confirm_text):
    return MessageDialog(parent, title, text, confirm_text=confirm_text).exec() == QDialog.DialogCode.Accepted


def inform(parent, title, text, details=None):
    MessageDialog(parent, title, text, details=details).exec()
