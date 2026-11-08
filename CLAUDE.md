# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Tamis is a PyQt6 desktop app for culling photo shoots: fast previews, keyboard-driven marking, then sorting files into `_KEEPS` / `_REJECTS` and exporting JPEGs to `_EXPORTS`. The README covers features, shortcuts and a module-by-module table; keep it up to date when behaviour or shortcuts change.

## Commands

The virtualenv lives in `.venv`. CI runs `ruff check .` and `pytest` on macOS with Python 3.10 and 3.13, so code must stay 3.10-compatible.

```bash
pip install -e ".[dev]"                      # setup
.venv/bin/python -m tamis                # run the app
.venv/bin/python -m pytest                   # all tests
.venv/bin/python -m pytest tests/test_fileops.py          # one file
.venv/bin/python -m pytest tests/test_app.py -k undo      # tests matching a name
.venv/bin/ruff check .                       # lint (line length 132)
./scripts/build_app.sh                       # dist/Tamis.app; needs pip install -e ".[build]"
```

After every code change (features, fixes, refactors), rebuild the app with `./scripts/build_app.sh` once tests and lint pass, so that `dist/Tamis.app` can be used straight away. Changes that only touch documentation don't need a rebuild. The script calls `python`, so activate `.venv` first; `dist/` is git-ignored, so the build is not committed.

`tests/test_app.py` drives the real main window with its real shortcuts and mouse events, using Qt's `offscreen` platform on a throwaway folder. Its fixture redirects `QSettings` to a temp directory and passes its own `cache_dir`, so tests never touch the real app's settings or caches; new window-level tests should use that `window` fixture. Anything that goes to the real Trash has to be replaced in tests (`fileops._to_trash`).

## Architecture

`TamisApp` (`app.py`) is the coordinator: it owns the model, both background engines and the views, and most features are wired together there.

### Photos are identified by path
`Photo` (`models.py`) holds `status` (the mark), `location` (the folder it is actually in) and `edit`. Moving a file changes its identity, so every path-keyed structure has to follow. After a move, `TamisApp._move_files` calls `model.apply_moves`, `loader.rename_paths` and `editor.rename_paths`, and clears the undo stack, whose entries hold old paths. Any new path-keyed cache needs the same treatment.

`Photo.files` is the primary file plus its `companions` (the JPEG of a RAW+JPEG pair). Anything operating on a photo's files must include the companions; `fileops` also moves XMP sidecars and renames a whole group together.

All changes to marks, ratings, labels and edits go through `PhotoModel.set_field` / `set_values`. These return the undo changes and keep the O(1) counters and signals correct. `Photo.edit` is always replaced, never mutated in place, because undo keeps a reference to the previous dict.

The results of the background analysis (`capture_time`, `sharpness`, `image_hash`, `burst`) live on the `Photo` itself and aren't marks: they are set directly, never saved in the session, and only ever suggest. Rows change when the sort order does (`PhotoModel.reorder`, `TamisApp._apply_order`), so nothing may keep a row across that.

### Threading
Six engines follow the same pattern, `ImageLoader` (`loader.py`, previews, thumbnails, full resolution), `EditRenderer` (`editing.py`, live edit previews), `EditThumbnails` (`editing.py`, thumbnails of edits, at low priority, kept on disk per edit), `PhotoAnalyzer` (`analysis.py`, dates and scores), `FocusPeaker` (`peaking.py`) and `ToneReader` (`tones.py`, histograms and clipping overlays):

- Workers only produce `QImage`; they send it through an internal queued signal, and the `QPixmap` is created on the GUI thread.
- Each job captures a `_generation` number. `reset()` increments it, and stale results are ignored rather than the threads being terminated. Superseded requests are skipped the same way.
- Worker jobs catch all exceptions, since a bad photo must not kill a worker.

`EditRenderer` keeps only the newest request per photo, on a single thread, and splits the image into strips rendered in parallel. It renders a half-size preview during a slider drag (`quick`), then the full preview on release.

### Edit pipeline
An edit is a plain dict holding only the non-default settings, as normalised by `develop.clean()`; `{}` means not edited. The same dict is stored in the session file and passed to the renderer.

`develop.render()` takes linear float32 RGB and must stay position-independent: each step may depend only on a pixel and its position in the full image (`origin` / `full_size`), so strip-based export gives the same result as a preview. The framing is applied before `render()` in this order, so vignette and grain follow the final frame: quarter turns (`rotate`), straightening (`straighten`, enlarged to fill the frame, same size), crop (in fractions of the turned picture). When adding a setting, update `DEFAULTS` and `RANGES` in `develop.py` and the panel in `editpanel.py`.

`develop.FRAMING` belongs to one photo: copy, paste and presets carry `develop.look(edit)` over and leave each photo's `develop.framing(edit)` alone. `TamisApp._framed` frames with Qt what the renderer didn't make (the preview before a render, the before view, the 1:1 zoom) and must keep matching the numpy pipeline. Presets (`presets.py`) are stored in `presets.json` in the app's data folder, through `develop.clean()` on load; `TamisApp(presets_file=...)` overrides the file, as the tests' `window` fixture does.

`PhotoAnalyzer` only reads what the current options need (`TamisApp._request_analysis`), since a card can be slow, and remembers results per folder by file name, size and modification time, in the app's cache folder, as does `ThumbnailCache` (`thumbcache.py`) for thumbnails, by path, size and modification time. Neither needs to follow moves: a moved file keeps its name and date, or is a miss. `StatusFilterProxy` filters on `FILTER_ROLE`, which any change a filter depends on must announce, since Qt only re-checks rows for that one role.

RAWs are edited from LibRaw's 16-bit linear development, not their embedded JPEG (`editing._source`), whereas culling previews go through `imaging.load_image`.

### Persistence
- `tamis_session.json` in the photo folder is keyed by relative path (`_KEEPS/IMG_1.CR2`). If the folder is read-only, the session falls back to a file in the app's data folder, and the newer of the two wins on load.
- Older session formats must keep loading: bare status values and filename keys (`models._entry`, `scan_folder`).
- The app used to be called CullSpeed. `cullspeed_session.json` is still read and replaced on the next save in the folder, and `migration.py` copies the old settings and fallback sessions at startup.
- Saved marks win over the folder a photo sits in, so marks changed since the last Process Files survive.
- `save_session` keeps the entries of photos whose file is missing, so anything that moves photos has to `forget` their old keys (`TamisApp.moved_keys`). A session that can't be parsed is renamed `*.corrupt.json` and reported through `problems`, never overwritten.
- `xmp:Rating="-1"` means rejected, not a number of stars: `xmp.read` gives `None` for it and the session's rating stays. Tamis writes it only with the opt-in in **Options** (`TamisApp.xmp_marks`, see `XMP_MARKS`), as no pick or reject flag is common to editors.
- XMP sidecars win over the session for ratings and labels, so changes from other editors take effect. Writes are batched through `xmp_dirty` and flushed by `save_session`. A failed write stays in `xmp_dirty` and sets `Photo.xmp_pending`, stored in the session, which makes the session win until a write succeeds. Existing sidecars are edited in place, touching only `xmp:Rating` and `xmp:Label`.

### Process Files
`fileops` keeps planning separate from execution: `plan_moves` / `execute_moves` and `plan_exports` / `copy_export`. Moves never overwrite (`IMG_1 (1).jpg`), while files in `_EXPORTS` are deliberately replaced so that reprocessing refreshes them. `process_files` plans exports a second time after moving, because the paths have changed. `execute_moves` moves a photo's files all together or not at all, and exports are written through `atomic_write`.

`plan_import` / `execute_import` and `plan_trash` / `execute_trash` work the same way. Imports never change the source and verify every copy; the Trash takes a photo's files all together or not at all, and `TamisApp._forget_trashed` makes everything path-keyed follow. Nothing in the app deletes photos.

`Photo.exports` records what was exported of a photo (name, size, modification time, and `dir`, the export folder as the settings gave it, when it isn't `_EXPORTS`), in the session. `remove_rejected_exports` only deletes files that still match their record, so files the user added or changed in the export folder are safe.

`ExportSettings` (`exportsettings.py`) holds the size, quality, folder and name pattern, in QSettings with the named profiles and the Process Files scope (`export_scope`, "all" but rejects or "keeps"). `ExportSettings.problem` refuses a folder among the photos, since exports replace files of the same name. Rendered exports are tagged sRGB and carry EXIF built by `exif.export_segment` from the source (orientation reset); resizing happens on the assembled output, after `render()`.

### Plugins
`plugins.py` loads `<name>.py` or `<name>/__init__.py` from `plugins/` in the app's data folder at startup and calls `register(tamis)` with a `PluginApi`; the README documents that API for plugin authors, so keep it up to date. `TamisApp(plugins_dir=...)` overrides the folder, and the tests' `window` fixture always does so they never run the real app's plugins.

- Anything a plugin runs goes through `PluginManager._call`, because an exception escaping into a Qt slot aborts the process. `main()` also installs an excepthook when a plugin is active, for a plugin's own timers and widgets.
- Plugins see `PhotoInfo` snapshots, not `Photo`, and change photos through the API, which uses `set_field` and pushes one undo step per call.
- App events go through `self.plugins.emit(name, ...)`; a new event also needs an entry in `plugins.EVENTS` and the README.
- Plugin shortcuts that are already taken are refused, since Qt fires neither of two ambiguous shortcuts. That relies on the app's own being bound before `plugins.load()`.

### Shortcuts
`shortcuts.COMMANDS` is the one table of the app's shortcuts: `TamisApp._build_shortcuts` binds it (slots in `_shortcut_slots`), the `?` overview and the stats bar hint are generated from it, and `ShortcutsEditor` changes it. A new shortcut is a `Command` there plus its slot. The user's keys (`TamisApp.shortcut_keys`) are saved in QSettings as their differences from the defaults; rebinding replaces every `QShortcut` and must keep cull-only ones disabled outside the Cull tab. Hard-coded keys in tooltips and the README describe the defaults.

## Constraints

- Do not use `QMessageBox`: with Qt 6.11 on recent macOS its native `NSAlert` crashes AppKit (`trace trap`). Use `inform` / `MessageDialog` from `widgets.py`.
- `PyQt6>=6.9` is required for `QSortFilterProxyModel.beginFilterChange()`.
- `faulthandler` is enabled at startup, so native Qt/LibRaw crashes print a Python traceback.
