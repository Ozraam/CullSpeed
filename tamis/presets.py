"""Named looks kept across shoots, in a JSON file in the app's data folder: {name: edit}.

A preset is a look: what develop.look() leaves of an edit, so never a photo's
framing. The file is passed through develop.clean() on load, so a hand-edited or
newer one only loses what isn't valid, and one that can't be parsed is renamed
`*.corrupt.json` and reported through `problem`, never overwritten.
"""
import json
import os
from contextlib import suppress

from . import develop
from .fileops import atomic_write


class PresetStore:
    def __init__(self, path):
        self.path = path
        self.problem = None  # what went wrong reading the file, to tell the user
        self._presets = self._load()

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as file:
                data = json.load(file)
            if not isinstance(data, dict):
                raise ValueError("not a list of presets")
        except FileNotFoundError:
            return {}
        except OSError as exc:
            self.problem = str(exc)
            return {}
        except ValueError as exc:  # (JSON errors and bad encodings are ValueErrors)
            root = os.path.splitext(self.path)[0]
            with suppress(OSError):
                os.replace(self.path, f"{root}.corrupt.json")
            self.problem = f"The presets file could not be read and was renamed {os.path.basename(root)}.corrupt.json: {exc}"
            return {}
        presets = {}
        for name, edit in data.items():
            look = develop.look(develop.clean(edit))
            if name.strip() and look:
                presets[name] = look
        return presets

    def names(self):
        return sorted(self._presets, key=str.casefold)

    def __contains__(self, name):
        return name in self._presets

    def get(self, name):
        """The look saved under `name` (a copy), or None."""
        look = self._presets.get(name)
        return dict(look) if look is not None else None

    def save(self, name, edit):
        """Keep the look of `edit` under `name`, replacing one of that name; raises OSError if it can't be written."""
        self._write({**self._presets, name: develop.look(develop.clean(edit))})

    def delete(self, name):
        self._write({key: look for key, look in self._presets.items() if key != name})

    def _write(self, presets):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with atomic_write(self.path) as tmp_path, open(tmp_path, "w", encoding="utf-8") as file:
            json.dump(presets, file, indent=2, ensure_ascii=False, sort_keys=True)
        self._presets = presets  # only once it is safe on disk
