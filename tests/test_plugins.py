"""Finding plugins in their folder; what they can do in the window is tested in test_app.py."""
from tamis.plugins import discover


def test_discover_finds_files_and_packages(tmp_path):
    (tmp_path / "simple.py").write_text("")
    package = tmp_path / "packaged"
    package.mkdir()
    (package / "__init__.py").write_text("")
    assert discover(str(tmp_path)) == {"packaged": str(package / "__init__.py"), "simple": str(tmp_path / "simple.py")}


def test_discover_ignores_what_is_not_a_plugin(tmp_path):
    (tmp_path / "_helper.py").write_text("")
    (tmp_path / ".hidden.py").write_text("")
    (tmp_path / "notes.txt").write_text("")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "__init__.py").write_text("")
    (tmp_path / "folder").mkdir()
    assert discover(str(tmp_path)) == {}


def test_discover_prefers_the_package_over_a_same_named_file(tmp_path):
    (tmp_path / "both.py").write_text("")
    (tmp_path / "both").mkdir()
    (tmp_path / "both" / "__init__.py").write_text("")
    assert discover(str(tmp_path)) == {"both": str(tmp_path / "both" / "__init__.py")}


def test_discover_copes_with_a_missing_folder(tmp_path):
    assert discover(str(tmp_path / "none")) == {}
