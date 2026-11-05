"""Settings and files left behind by the app when it was called CullSpeed."""
from PyQt6.QtCore import QByteArray, QSettings

from tamis.migration import migrate_fallback_sessions, migrate_settings


def settings(tmp_path, name):
    return QSettings(str(tmp_path / f"{name}.ini"), QSettings.Format.IniFormat)


def test_settings_are_copied(tmp_path):
    old, new = settings(tmp_path, "old"), settings(tmp_path, "new")
    old.setValue("last_folder", "/photos")
    old.setValue("geometry", QByteArray(b"\x01\x02"))

    migrate_settings(old, new)

    assert new.value("last_folder") == "/photos"
    assert new.value("geometry") == QByteArray(b"\x01\x02")
    assert old.value("last_folder") == "/photos"  # left in place


def test_existing_settings_are_not_overwritten(tmp_path):
    old, new = settings(tmp_path, "old"), settings(tmp_path, "new")
    old.setValue("last_folder", "/old")
    old.setValue("show_info", True)
    new.setValue("last_folder", "/new")

    migrate_settings(old, new)

    assert new.value("last_folder") == "/new"
    assert not new.contains("show_info")


def test_fallback_sessions_are_copied(tmp_path):
    old, new = tmp_path / "old" / "sessions", tmp_path / "new" / "sessions"
    old.mkdir(parents=True)
    (old / "abc.json").write_text("{}")

    migrate_fallback_sessions(str(old), str(new))

    assert (new / "abc.json").read_text() == "{}"
    assert (old / "abc.json").exists()


def test_existing_fallback_sessions_are_left_alone(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    (old / "abc.json").write_text("old")
    (new / "abc.json").write_text("new")

    migrate_fallback_sessions(str(old), str(new))

    assert (new / "abc.json").read_text() == "new"


def test_nothing_to_migrate(tmp_path):
    new = tmp_path / "new"

    migrate_fallback_sessions(str(tmp_path / "missing"), str(new))

    assert not new.exists()
