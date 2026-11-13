"""Tamis application package."""

from importlib.metadata import PackageNotFoundError, version

try:
    # From the installed package's metadata, which scripts/build_app.sh copies into the app bundle.
    __version__ = version("tamis")
except PackageNotFoundError:  # run from a checkout that was never installed
    __version__ = "unknown"
