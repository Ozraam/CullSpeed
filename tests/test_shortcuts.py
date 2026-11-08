import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtWidgets import QApplication  # noqa: E402

from tamis import shortcuts, xmp  # noqa: E402

app = QApplication.instance() or QApplication([])


class FakeSettings(dict):
    def value(self, key, default=None):
        return self.get(key, default)

    def setValue(self, key, value):  # noqa: N802
        self[key] = value

    def remove(self, key):
        self.pop(key, None)


def test_defaults_bind_every_label_and_share_no_key():
    defaults = shortcuts.defaults()
    assert all(defaults[f"label_{label.lower()}"] for label in xmp.LABELS)
    assert defaults["label_purple"] == ("Ctrl+Shift+9",)
    assert shortcuts.conflicts(defaults) == {}


def test_only_changes_are_saved_and_they_load_back():
    settings = FakeSettings()
    bindings = dict(shortcuts.defaults(), keep=("Q",))
    shortcuts.save(settings, bindings)
    assert set(json_keys(settings)) == {"keep"}
    assert shortcuts.load(settings) == bindings

    shortcuts.save(settings, shortcuts.defaults())
    assert shortcuts.SETTINGS_KEY not in settings


def json_keys(settings):
    import json
    return json.loads(settings[shortcuts.SETTINGS_KEY])


def test_a_damaged_setting_falls_back_to_the_defaults():
    for saved in ("not json", "[1, 2]", '{"keep": "P", "nope": ["Q"]}'):
        assert shortcuts.load(FakeSettings({shortcuts.SETTINGS_KEY: saved})) == shortcuts.defaults()


def test_invalid_and_shared_keys_are_dropped_on_load():
    bindings = shortcuts.resolve({"keep": ["", "Q", "Q"], "reject": ["Q", "W"]})
    assert bindings["keep"] == ("Q",)
    assert bindings["reject"] == ("W",)  # Q stays with the first command that has it


def test_conflicts_include_other_owners():
    bindings = dict(shortcuts.defaults(), reject=("P",))
    assert shortcuts.conflicts(bindings) == {"P": ["keep", "reject"]}
    assert shortcuts.conflicts(shortcuts.defaults(), {"Ctrl+Z": "Mine > Undo too"}) == {"Ctrl+Z": ["undo", "Mine > Undo too"]}


def test_the_hint_follows_the_keys():
    defaults = shortcuts.defaults()
    hint = shortcuts.hint(defaults)
    assert shortcuts.native("Ctrl+Shift+9") in hint and "? all shortcuts" in hint
    assert "Q keep" in shortcuts.hint(dict(defaults, keep=("Q",)))
    assert "keep ·" not in shortcuts.hint(dict(defaults, keep=()))
