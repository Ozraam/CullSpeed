import sys

from PyQt6.QtWidgets import QApplication

from cullspeed.app import CullSpeedApp


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = CullSpeedApp()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
