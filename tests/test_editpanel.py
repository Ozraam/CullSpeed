import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtWidgets import QApplication  # noqa: E402

from tamis.editpanel import EditPanel  # noqa: E402


@pytest.fixture(scope="module")
def panel():
    app = QApplication.instance() or QApplication([])
    yield EditPanel()
    del app  # kept alive until the tests are done


def tree(menu):
    """The menu as [(text, children or None)], separators skipped."""
    return [
        (action.text(), tree(action.menu()) if action.menu() else None)
        for action in menu.actions() if not action.isSeparator()
    ]


def test_lut_popup_follows_sub_folders(panel):
    ids = ["Kodak Gold.png", "Fuji/Velvia 50.png", "Fuji/XTrans III/Classic Chrome.cube", "Fuji/Astia.png"]
    panel.set_luts([(lut_id, lut_id) for lut_id in ids])
    assert tree(panel.lut_combo.build_menu()) == [
        ("No film simulation", None),
        ("Fuji", [
            ("XTrans III", [("Classic Chrome", None)]),
            ("Astia", None),
            ("Velvia 50", None),
        ]),
        ("Kodak Gold", None),
    ]


def test_choosing_in_a_sub_menu_selects_the_lut(panel):
    panel.set_luts([("Fuji/Velvia 50.png", "Fuji / Velvia 50")])
    fuji = panel.lut_combo.build_menu().actions()[2].menu()
    fuji.actions()[0].trigger()
    assert panel.edit()["lut"] == "Fuji/Velvia 50.png"
    # The folder leading to the current LUT is highlighted.
    assert panel.lut_combo.build_menu().actions()[2].font().bold()
