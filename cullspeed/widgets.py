from PyQt6.QtCore import QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFontMetrics, QKeySequence, QPainter, QPainterPath, QPen, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListView,
    QPlainTextEdit,
    QPushButton,
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
    COLOR_TEXT,
    KEEP,
    REJECT,
)
from .models import STATUS_ROLE, THUMBNAIL_FAILED_ROLE

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

class ImageView(QWidget):
    """Shows the current photo fitted to the window, or at 1:1 with panning."""

    MARGIN = 16

    zoom_changed = pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pixmap = None
        self._full_loading = False
        self._scaled = None
        self._message = ""
        self._status = None
        self._zoomed = False
        self._center = QPointF(0.5, 0.5)  # zoom focus, relative to the image
        self._drag_from = None
        self.setMinimumSize(200, 200)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def set_image(self, pixmap, message=""):
        self._pixmap = pixmap if pixmap is not None and not pixmap.isNull() else None
        self._scaled = None
        self._message = message
        self._zoomed = False
        self._full_loading = False
        self._update_cursor()
        self.update()

    def set_full_image(self, pixmap):
        """Swap in the full-resolution image, keeping the zoom position."""
        self._full_loading = False
        if pixmap is not None and not pixmap.isNull() and self._pixmap is not None:
            self._pixmap = pixmap
            self._scaled = None
        self.update()

    def set_full_loading(self, loading):
        if loading != self._full_loading:
            self._full_loading = loading
            self.update()

    @property
    def zoomed(self):
        return self._zoomed

    def set_status(self, status):
        if status != self._status:
            self._status = status
            self.update()

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

    def reset_zoom(self):
        if self._zoomed:
            self.toggle_zoom()

    # geometry

    def _fit_rect(self):
        bounds = self.rect().adjusted(self.MARGIN, self.MARGIN, -self.MARGIN, -self.MARGIN)
        return fit_rect(self._pixmap.size(), bounds)

    def _zoom_rect(self):
        size = self._pixmap.size() / self._pixmap.devicePixelRatio() / self.devicePixelRatioF()
        x = self.width() / 2 - self._center.x() * size.width()
        y = self.height() / 2 - self._center.y() * size.height()
        # Keep the image on screen; center it on any axis where it is smaller.
        x = (self.width() - size.width()) / 2 if size.width() <= self.width() else min(0, max(self.width() - size.width(), x))
        y = (self.height() - size.height()) / 2 if size.height() <= self.height() else min(0, max(self.height() - size.height(), y))
        return QRectF(x, y, size.width(), size.height())

    def _update_cursor(self):
        if self._zoomed:
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
        if self._zoomed and event.button() == Qt.MouseButton.LeftButton:
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

        color = STATUS_COLORS.get(self._status)
        if color is not None:
            visible = target.intersected(QRectF(self.rect()))
            painter.setPen(QPen(color, 4))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(visible.adjusted(2, 2, -2, -2))
            self._draw_status_pill(painter, visible, color)

        if self._zoomed and self._full_loading:
            self._draw_loading_note(painter)

    def _draw_loading_note(self, painter):
        text = "Loading full resolution…"
        metrics = QFontMetrics(painter.font())
        note = QRectF(14, self.height() - metrics.height() - 24, metrics.horizontalAdvance(text) + 20, metrics.height() + 10)
        path = QPainterPath()
        path.addRoundedRect(note, note.height() / 2, note.height() / 2)
        painter.fillPath(path, QColor(0, 0, 0, 170))
        painter.setPen(QColor(COLOR_TEXT))
        painter.drawText(note, Qt.AlignmentFlag.AlignCenter, text)

    def _draw_status_pill(self, painter, rect, color):
        label = "KEEP" if self._status == KEEP else "REJECT"
        font = painter.font()
        font.setBold(True)
        painter.setFont(font)
        metrics = QFontMetrics(font)
        height = metrics.height() + 10
        width = metrics.horizontalAdvance(label) + height + 14
        pill = QRectF(rect.left() + 14, rect.top() + 14, width, height)
        path = QPainterPath()
        path.addRoundedRect(pill, height / 2, height / 2)
        painter.fillPath(path, QColor(0, 0, 0, 170))
        radius = height / 2 - 4
        draw_status_badge(painter, QPointF(pill.left() + height / 2, pill.center().y()), radius, self._status)
        painter.setPen(color)
        painter.drawText(
            pill.adjusted(height, 0, -8, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, label
        )


# -- bars ------------------------------------------------------------------

class HeaderBar(QFrame):
    open_folder_requested = pyqtSignal()
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

        self.btn_process = QPushButton("Process Files…")
        self.btn_process.setObjectName("Primary")
        self.btn_process.setEnabled(False)
        self.btn_process.clicked.connect(self.process_requested)

        for button in (btn_open, self.btn_process):
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 8, 14, 8)
        layout.setSpacing(10)
        layout.addWidget(title)
        layout.addWidget(self.lbl_folder, 1)
        layout.addWidget(self.lbl_counter)
        layout.addWidget(btn_open)
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

        undo = QKeySequence(QKeySequence.StandardKey.Undo).toString(QKeySequence.SequenceFormat.NativeText)
        hint = QLabel(f"P keep · X reject · U unmark · ←/→ navigate · N next unmarked · Z zoom · G gallery · {undo} undo")
        hint.setObjectName("Muted")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 6, 14, 6)
        layout.setSpacing(18)
        layout.addWidget(self.lbl_keep)
        layout.addWidget(self.lbl_reject)
        layout.addWidget(self.lbl_unmarked)
        layout.addStretch()
        layout.addWidget(hint)
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
