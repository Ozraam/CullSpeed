import faulthandler
import sys
from pathlib import Path

from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication

from .app import CullSpeedApp
from .theme import apply_theme


def main():
    # Print a Python traceback if Qt/LibRaw ever crash natively.
    faulthandler.enable()

    app = QApplication(sys.argv)
    app.setOrganizationName("CullSpeed")
    app.setApplicationName("CullSpeed")
    app.setWindowIcon(QIcon(str(Path(__file__).parent / "resources" / "icon.svg")))
    apply_theme(app)

    window = CullSpeedApp()
    app.aboutToQuit.connect(window.loader.shutdown)
    app.aboutToQuit.connect(window.editor.shutdown)
    window.show()
    return app.exec()
