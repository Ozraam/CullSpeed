"""How photos are exported: size, quality, destination and file names, and named profiles of these.

The settings in use and the saved profiles are kept in QSettings as JSON;
anything in them that isn't valid falls back to its default, so a hand-edited
or newer value never stops the app from exporting.
"""
import json
import os
import re
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta

from .constants import EXPORT_DIR, KEEP_DIR, REJECT_DIR

QUALITY_RANGE = (1, 100)
LONG_EDGE_MAX = 20000
TOKENS = ("name", "date", "seq", "folder")  # what a name pattern may hold, as {name}
SCOPES = (("all", "Everything except rejects"), ("keeps", "Keeps only"))

_EPOCH = datetime(1970, 1, 1)
_UNSAFE = re.compile(r'[/\\:\x00-\x1f]')


@dataclass(frozen=True)
class ExportSettings:
    long_edge: int = 0  # longest edge in pixels; 0 keeps the full size
    quality: int = 92  # JPEG quality, 1–100
    folder: str = EXPORT_DIR  # relative to the shoot folder, or absolute (`~` allowed)
    pattern: str = "{name}"  # file name without extension, see TOKENS

    @classmethod
    def from_dict(cls, data):
        """Settings from a dict; what is missing or invalid takes its default."""
        if not isinstance(data, dict):
            return cls()
        default = cls()
        long_edge = data.get("long_edge")
        quality = data.get("quality")
        folder = data.get("folder")
        pattern = data.get("pattern")
        return cls(
            long_edge if _int(long_edge) and 0 <= long_edge <= LONG_EDGE_MAX else default.long_edge,
            quality if _int(quality) and QUALITY_RANGE[0] <= quality <= QUALITY_RANGE[1] else default.quality,
            folder.strip() if isinstance(folder, str) and folder.strip() else default.folder,
            pattern.strip() if isinstance(pattern, str) and pattern_problem(pattern) is None else default.pattern,
        )

    def to_dict(self):
        return asdict(self)

    def with_changes(self, **changes):
        return replace(self, **changes)

    @property
    def resizes(self):
        return self.long_edge > 0

    def dest_dir(self, shoot_folder):
        """Where exports go, as an absolute path."""
        return os.path.normpath(os.path.join(shoot_folder, os.path.expanduser(self.folder)))

    def problem(self, shoot_folder):
        """Why exporting there can't be allowed, None if it can: exports must not land among the photos."""
        dest = os.path.normcase(self.dest_dir(shoot_folder))
        photo_dirs = (shoot_folder, os.path.join(shoot_folder, KEEP_DIR), os.path.join(shoot_folder, REJECT_DIR))
        if any(dest == os.path.normcase(os.path.normpath(folder)) for folder in photo_dirs):
            return f"Exports can't go to {self.folder}: they would land among the photos, and could replace one."
        return None

    def describe(self):
        """One line, for the Process Files dialog."""
        size = f"{self.long_edge} px long edge" if self.resizes else "full size"
        return f"{size}, quality {self.quality}, named {self.pattern}.jpg"


def _int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def pattern_problem(pattern):
    """Why `pattern` can't name files, None if it can."""
    if not isinstance(pattern, str) or not pattern.strip():
        return "The name pattern is empty."
    for match in re.finditer(r'\{([^{}]*)\}', pattern):
        if match.group(1) not in TOKENS:
            return f"{{{match.group(1)}}} is not a field: use " + ", ".join(f"{{{token}}}" for token in TOKENS) + "."
    if re.sub(r'\{[^{}]*\}', "", pattern).count("{") or re.sub(r'\{[^{}]*\}', "", pattern).count("}"):
        return "The name pattern has an unmatched brace."
    return None


def date_text(seconds):
    """`YYYY-MM-DD` of a capture time (seconds on the camera's clock, see exif.capture_time())."""
    if seconds is None:
        return "undated"
    try:
        return (_EPOCH + timedelta(seconds=seconds)).strftime("%Y-%m-%d")
    except (OverflowError, ValueError):
        return "undated"


def file_stem(pattern, name, date="", seq=1, total=1, folder=""):
    """The pattern filled in, made safe as a file name; `name` alone if nothing would be left."""
    values = {
        "name": name,
        "date": date,
        "seq": str(seq).zfill(max(3, len(str(total)))),
        "folder": folder,
    }
    stem = re.sub(r'\{(' + "|".join(TOKENS) + r')\}', lambda match: values[match.group(1)], pattern)
    stem = _UNSAFE.sub("-", stem).strip().strip(".")
    return stem or name


def uses_date(pattern):
    return "{date}" in pattern


def load_profiles(text):
    """{name: ExportSettings} from JSON; {} if it can't be read."""
    try:
        data = json.loads(text) if text else {}
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {name: ExportSettings.from_dict(value) for name, value in data.items() if isinstance(name, str) and name.strip()}


def dump_profiles(profiles):
    return json.dumps({name: settings.to_dict() for name, settings in profiles.items()}, sort_keys=True)


def load_settings(text):
    try:
        return ExportSettings.from_dict(json.loads(text) if text else None)
    except (TypeError, ValueError):
        return ExportSettings()
