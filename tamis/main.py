import faulthandler
import sys
import traceback
from pathlib import Path

from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication

from . import __version__
from .app import TamisApp
from .constants import APP_NAME
from .migration import migrate_from_cullspeed
from .theme import apply_theme


def main():
    # Print a Python traceback if Qt/LibRaw ever crash natively.
    faulthandler.enable()

    app = QApplication(sys.argv)
    app.setOrganizationName(APP_NAME)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setWindowIcon(QIcon(str(Path(__file__).parent / "resources" / "icon.svg")))
    apply_theme(app)
    migrate_from_cullspeed()

    window = TamisApp()
    if window.plugins.active:
        # A plugin's own widgets and timers run outside the plugin manager's guard, and
        # PyQt aborts on an uncaught exception in a slot unless a hook is installed.
        sys.excepthook = traceback.print_exception
    app.aboutToQuit.connect(window.loader.shutdown)
    app.aboutToQuit.connect(window.editor.shutdown)
    window.show()
    return app.exec()
