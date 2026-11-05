"""An example Tamis plugin. Copy it to the plugins folder (Plugins > Open Plugins Folder) and restart Tamis."""
from PyQt6.QtWidgets import QApplication

NAME = "Keeps list"


def register(tamis):
    def copy_keeps():
        names = [photo.name for photo in tamis.photos() if photo.status == tamis.KEEP]
        QApplication.clipboard().setText("\n".join(names))
        tamis.toast(f"Copied {len(names)} name(s)")

    def keep_top_rated():
        minimum = tamis.get_setting("minimum_stars", 5)
        tamis.set_status([photo for photo in tamis.photos() if photo.rating >= minimum], tamis.KEEP)  # one undo step

    tamis.add_action("Copy Names of Keeps", copy_keeps, shortcut="Ctrl+Shift+L")
    tamis.add_action("Keep Top-Rated Photos", keep_top_rated)
    tamis.on("files_moved", lambda moved: tamis.toast(f"{len(moved)} file(s) moved"))
