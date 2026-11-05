"""Bring over what the app left behind when it was called CullSpeed.

The old settings and files are copied, not moved, so nothing is lost if the copy fails.
"""
import os
import shutil
from contextlib import suppress

from PyQt6.QtCore import QSettings, QStandardPaths

from .constants import FALLBACK_SESSIONS_DIR, LEGACY_APP_NAME


def migrate_settings(old, new):
    """Copy `old` into `new`, unless `new` already holds settings."""
    # Otherwise macOS adds its system-wide preferences to allKeys().
    old.setFallbacksEnabled(False)
    new.setFallbacksEnabled(False)
    if new.allKeys():
        return
    for key in old.allKeys():
        new.setValue(key, old.value(key))
    new.sync()


def migrate_fallback_sessions(old_dir, new_dir):
    """Copy the sessions of read-only folders, whose file names don't depend on the app's name."""
    if os.path.exists(new_dir) or not os.path.isdir(old_dir):
        return
    with suppress(OSError):
        shutil.copytree(old_dir, new_dir)


def migrate_from_cullspeed():
    """Call once the application's name is set, before the main window reads its settings."""
    old = QSettings(QSettings.defaultFormat(), QSettings.Scope.UserScope, LEGACY_APP_NAME, LEGACY_APP_NAME)
    migrate_settings(old, QSettings())

    # AppDataLocation ends in <organization>/<application>.
    data_dir = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)
    old_data_dir = os.path.join(os.path.dirname(os.path.dirname(data_dir)), LEGACY_APP_NAME, LEGACY_APP_NAME)
    migrate_fallback_sessions(os.path.join(old_data_dir, FALLBACK_SESSIONS_DIR), os.path.join(data_dir, FALLBACK_SESSIONS_DIR))
