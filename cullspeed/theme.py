from PyQt6.QtGui import QColor, QPalette

from .constants import (
    COLOR_ACCENT,
    COLOR_BG,
    COLOR_BORDER,
    COLOR_CARD,
    COLOR_KEEP,
    COLOR_MUTED,
    COLOR_PANEL,
    COLOR_REJECT,
    COLOR_TEXT,
)

STYLESHEET = f"""
QWidget {{ color: {COLOR_TEXT}; }}
QMainWindow, QDialog {{ background: {COLOR_BG}; }}

#HeaderBar, #StatsBar, #GalleryToolbar {{ background: {COLOR_PANEL}; }}
#HeaderBar {{ border-bottom: 1px solid {COLOR_BORDER}; }}
#StatsBar {{ border-top: 1px solid {COLOR_BORDER}; }}
#GalleryToolbar {{ border-bottom: 1px solid {COLOR_BORDER}; }}

#AppTitle {{ color: {COLOR_ACCENT}; font-weight: bold; font-size: 16px; }}
#Muted {{ color: {COLOR_MUTED}; }}
#Counter {{ background: {COLOR_CARD}; color: {COLOR_TEXT}; padding: 3px 9px; border-radius: 4px; }}
#KeepCount {{ color: {COLOR_KEEP}; font-weight: bold; }}
#RejectCount {{ color: {COLOR_REJECT}; font-weight: bold; }}
#DialogTitle {{ font-size: 15px; font-weight: bold; }}
#Toast {{
    background: rgba(20, 20, 20, 235); color: {COLOR_TEXT};
    border: 1px solid {COLOR_BORDER}; border-radius: 8px; padding: 9px 16px;
}}

QPushButton {{
    background: {COLOR_CARD}; border: 1px solid {COLOR_BORDER};
    border-radius: 5px; padding: 6px 14px;
}}
QPushButton:hover {{ background: #2b2b2b; }}
QPushButton:disabled {{ color: #5b5b5b; }}
QPushButton#Primary {{ background: {COLOR_ACCENT}; border-color: {COLOR_ACCENT}; color: white; font-weight: bold; }}
QPushButton#Primary:hover {{ background: #2563eb; }}
QPushButton#Primary:disabled {{ background: #1e3a8a; border-color: #1e3a8a; color: #9ca3af; }}
QPushButton#Chip {{ border-radius: 12px; padding: 3px 12px; }}
QPushButton#Chip:checked {{ background: {COLOR_ACCENT}; border-color: {COLOR_ACCENT}; color: white; }}

QTabWidget::pane {{ border: none; }}
QTabBar {{ background: {COLOR_PANEL}; }}
QTabBar::tab {{
    background: transparent; color: {COLOR_MUTED};
    padding: 7px 18px; border: none; border-bottom: 2px solid transparent;
}}
QTabBar::tab:selected {{ color: {COLOR_TEXT}; border-bottom: 2px solid {COLOR_ACCENT}; }}
QTabBar::tab:hover {{ color: {COLOR_TEXT}; }}

QListView {{ background: {COLOR_BG}; border: none; outline: none; }}
QListView#Filmstrip {{ background: #0e0e0e; border-top: 1px solid {COLOR_BORDER}; }}

QScrollBar:horizontal, QScrollBar:vertical {{ background: transparent; border: none; margin: 0; }}
QScrollBar:horizontal {{ height: 8px; }}
QScrollBar:vertical {{ width: 8px; }}
QScrollBar::handle {{ background: #3a3a3a; border-radius: 4px; min-width: 30px; min-height: 30px; }}
QScrollBar::handle:hover {{ background: #4a4a4a; }}
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{ width: 0; height: 0; background: none; }}

#EditPanel, #EditPanelContent, #EditPanelFooter {{ background: {COLOR_PANEL}; }}
#EditPanelFooter {{ border-top: 1px solid {COLOR_BORDER}; }}
#EditPanel {{ border-left: 1px solid {COLOR_BORDER}; }}
#SectionTitle {{ color: {COLOR_MUTED}; font-size: 11px; font-weight: bold; letter-spacing: 1px; }}
QSlider::groove:horizontal {{ height: 4px; background: {COLOR_BORDER}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: #4b5563; border-radius: 2px; }}
QSlider::handle:horizontal {{
    width: 12px; height: 12px; margin: -5px 0; border-radius: 6px; background: {COLOR_TEXT};
}}
QSlider::handle:horizontal:hover {{ background: #ffffff; }}
QComboBox {{
    background: {COLOR_CARD}; border: 1px solid {COLOR_BORDER}; border-radius: 5px; padding: 4px 8px;
}}
QComboBox QAbstractItemView {{ background: {COLOR_CARD}; selection-background-color: {COLOR_ACCENT}; }}

QPlainTextEdit {{ background: {COLOR_CARD}; border: 1px solid {COLOR_BORDER}; border-radius: 4px; }}
QProgressBar {{ background: {COLOR_CARD}; border: 1px solid {COLOR_BORDER}; border-radius: 4px; text-align: center; }}
QProgressBar::chunk {{ background: {COLOR_ACCENT}; border-radius: 3px; }}
QToolTip {{ background: {COLOR_CARD}; color: {COLOR_TEXT}; border: 1px solid {COLOR_BORDER}; }}
"""


def apply_theme(app):
    app.setStyle("Fusion")
    palette = QPalette()
    roles = {
        QPalette.ColorRole.Window: COLOR_BG,
        QPalette.ColorRole.WindowText: COLOR_TEXT,
        QPalette.ColorRole.Base: COLOR_CARD,
        QPalette.ColorRole.AlternateBase: COLOR_PANEL,
        QPalette.ColorRole.Text: COLOR_TEXT,
        QPalette.ColorRole.Button: COLOR_CARD,
        QPalette.ColorRole.ButtonText: COLOR_TEXT,
        QPalette.ColorRole.Highlight: COLOR_ACCENT,
        QPalette.ColorRole.HighlightedText: "#ffffff",
        QPalette.ColorRole.ToolTipBase: COLOR_CARD,
        QPalette.ColorRole.ToolTipText: COLOR_TEXT,
        QPalette.ColorRole.PlaceholderText: COLOR_MUTED,
    }
    for role, color in roles.items():
        palette.setColor(role, QColor(color))
    app.setPalette(palette)
    app.setStyleSheet(STYLESHEET)
