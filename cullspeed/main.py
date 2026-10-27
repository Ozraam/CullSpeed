import faulthandler
import sys

from PyQt6.QtWidgets import QApplication

from .app import CullSpeedApp
from .theme import apply_theme


def main():
    # Print a Python traceback if Qt/LibRaw ever crash natively.
    faulthandler.enable()

    app = QApplication(sys.argv)
    app.setOrganizationName("CullSpeed")
    app.setApplicationName("CullSpeed")
    apply_theme(app)

    window = CullSpeedApp()
    app.aboutToQuit.connect(window.loader.shutdown)
    window.show()
    return app.exec()
