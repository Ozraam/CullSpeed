# Tamis

Tamis is a PyQt6 desktop application for rapidly reviewing and triaging large photo shoots. It focuses on fast image loading (embedded RAW previews, background decoding and prefetching), keyboard-driven marking, and one-click sorting of files into keep/reject folders.

## Features
- Supports JPEG, PNG, WebP, TIFF, HEIC (when Qt has a plugin for it) and common RAW formats (ARW, CR2, CR3, NEF, DNG, ORF, RAF, RW2) via `rawpy`.
- **Cull** view: the current photo fitted to the window with a filmstrip; next/previous photos are prefetched so stepping through is instant. Double-click or `Z` zooms to 1:1 (the photo is loaded at full resolution for focus checking), drag to pan. The zoom and its position stay as you move to the next photo, so a burst can be checked at the same spot.
- **Keeps only**: `K` (or the chip in the Cull tab's corner) limits the filmstrip and all navigation to the photos marked keep, for a second pass. Rejecting or unmarking a photo moves on to the next keep; picking another photo in the Gallery, or `N`, ends the filter.
- **Compare**: `C` shows photos side by side (the current and next one, or the 2–4 selected in the Gallery) with shared zoom and pan. `←`/`→` pick the photo that marks apply to.
- **Gallery** view: thumbnail grid with multi-select marking and adjustable thumbnail size. Filters combine: status (All / Unmarked / Keeps / Rejects), minimum stars, colour labels (several can be on) and focus, and follow changes live.
- **Sorting**: by name, with numbers compared as numbers (`IMG_9` before `IMG_10`), by capture date (with sub-seconds; a file without a date uses its modification time) or by sharpness. The choice is remembered, and dates are read in the background, so the order changes once they are all known.
- **Bursts** (`B`, or the chip in the Gallery): shots less than 2 seconds apart are grouped in the filmstrip and Gallery with a coloured bar and their place in the burst (`2/5`), unless the picture changes completely. `⇧P` keeps the current photo and rejects the rest of its burst, as one undo step.
- **Sharpness**, computed in the background when something needs it (the sort, the focus filter, bursts): the variance of the Laplacian in the sharpest regions of a photo. It is only a suggestion: `◎` marks the sharpest frame of a burst and `soft` a photo far below the folder's median, and marks stay yours. Scores are compared within a folder, and the thresholds haven't been tuned on many real shoots yet.
- **Focus peaking** (`F` in the Cull view): highlights the sharp edges of what is shown, edits, crop and 1:1 zoom included.
- **Import** (`⌘I`): finds the photos in the sub-folders of a memory card, copies them with their sidecars to a folder and optionally to a backup, reads every copy back to compare it with the original, then opens the folder. Nothing on the card changes, files never overwrite others (`IMG_1 (1).CR2`), and those already imported are skipped, even if sorted into `_KEEPS` or `_REJECTS` since.
- **Trash Rejects**: moves the photos in `_REJECTS` that are still marked reject to the system Trash, with their pairs and sidecars, after a confirmation with their count and size. Nothing is ever deleted: a photo that can't go to the Trash stays where it is, whole.
- Thumbnails are kept on disk (up to 512 MB, in the app's cache folder), so reopening a folder is fast. They are found by a file's path, size and modification time, so a changed or moved file just gets a new one.
- **Edited thumbnails**: the filmstrip and the Gallery show photos with their edit and frame, rendered in the background at low priority and kept on disk per edit; until then they show the original.
- **Star ratings and colour labels**, written to XMP sidecars (`xmp:Rating`, `xmp:Label`) so Lightroom, Bridge, Capture One and darktable see them. Existing sidecars are edited in place, touching only those two properties; new ones are only created for RAWs (`IMG.xmp`).
- **Keep and reject in other editors**, off by default (**Options ▾ > Show Keep and Reject in Other Editors**), as no flag is common to them. *Rejected as rating −1* writes the `xmp:Rating="-1"` that darktable and Bridge read as rejected; the photo keeps its stars in Tamis, but other editors no longer see them. *Keep green, reject red* gives photos those colour labels, for editors that read `xmp:Label` such as Lightroom, whose own flags are not in XMP at all. A label you gave a photo yourself stays, except that unmarking clears a green or red one. As with ratings, new sidecars are only created for RAWs.
- **RAW+JPEG pairs** (same name, same folder) are culled as one photo and always move together.
- **Shooting info** (exposure, aperture, ISO, focal length, camera, lens, date) read from EXIF; `I` shows or hides it.
- **Quick edits** (`E`): exposure, contrast, highlights, shadows, white balance (temperature / tint), saturation, film simulation LUTs with strength, grain and vignette. Edits are non-destructive (stored in the session, originals untouched), previewed live, can be copied and pasted across photos, and exported as JPEGs (see **Export settings**). RAWs are edited from LibRaw's 16-bit linear development, not from their embedded JPEG.
- **Histogram and clipping**: the edit panel shows the red, green and blue histogram of what is on screen, updated in the background, with the share of the photo clipped at either end. `J` (or **Clipping**) shows them in the Cull view, panel open or not: red where all three channels are at the top (blown out, no detail left), a discreet orange where only one or two are (a vivid colour, flatter but not blown out), and blue where all three are at the bottom. The histogram's figures only count the red and the blue, so a very colourful photo doesn't look overexposed.
- **Before / after**: hold `Y` or `\` (or the **Before** button) to see the photo without its edit, in the same frame; the edit itself doesn't change. It shows the same preview as an unedited photo, so for a RAW that is the camera's embedded JPEG, which can differ from LibRaw's development even with no edit.
- **Rotate and straighten**: `]` / `⇧R` and `[` / `⇧L` turn the current or selected photos by 90°, while culling too; the **Straighten** slider levels a horizon by up to 15°, enlarging the picture so that no empty corner shows. A crop stays on the same part of the picture through a quarter turn. Like a crop, these belong to their photo's framing: they are undoable, exported and cleared by Reset, but neither copied nor kept in a preset, and vignette and grain follow the final frame.
- **Presets**: **Presets > Save Current Look…** keeps a look (without its framing) under a name, and choosing it applies it to the current or selected photos, on this shoot or any other, as one undo step. They are stored in `presets.json` in the app's data folder.
- **Crop** (`R`): drag the frame's corners and edges, or inside it to move it, with a free or fixed shape (original, 1:1, 3:2, 4:5, 16:9…). `Enter`, `R` or a double-click applies it, `Esc` cancels; moving to another photo applies it too. Vignette and grain follow the final frame. A crop is part of the photo's edit (undoable, exported, cleared by Reset), but it stays with its photo: copying and pasting an edit carries the look over and leaves each photo's own crop alone.
- New or removed files are picked up automatically while the folder is open (or with **Refresh**, `⌘R`).
- Undo for every marking, rating and labelling action.
- Marks are saved automatically in `tamis_session.json` inside each photo folder.
- **Process Files** moves photos into `_KEEPS` / `_REJECTS` (or back to the root when unmarked). XMP sidecars move with their photo, and existing files are never overwritten (`IMG_1 (1).jpg` instead). Unless you untick the export box in its confirmation (ticked by default), it then fills the export folder (`_EXPORTS` unless the export settings say otherwise): edited photos are exported as JPEGs and unedited ones get their JPEG (the photo itself, or the JPEG of a RAW+JPEG pair) copied. The confirmation lets you choose what is exported: *everything except rejects* (the default) or *keeps only*, remembered for next time. Earlier exports of the same photos are replaced, so processing again refreshes them, and exports of photos rejected since are removed, from whichever folder they were written to (Tamis remembers what it exported: other files there, and exports changed since, stay). The confirmation lists the photos without a JPEG, which are not exported (`⌘⇧E` renders them), and those whose film simulation is no longer in the LUT folder, which would be exported without it. A photo whose files can't all be moved stays where it is, with its pair and sidecars, and exports are written to a temporary file first, so a failure never leaves a truncated JPEG behind.
- **Export settings** (**Options ▾ > Export Settings…**, or the button in the Process Files confirmation): the longest edge (full size by default), the JPEG quality (92), the folder (relative to the shoot folder, such as `_EXPORTS` or `web/large`, or a full path; never the shoot folder itself, `_KEEPS` or `_REJECTS`) and a name pattern made of `{name}` (the original name), `{date}` (capture date, `YYYY-MM-DD`), `{seq}` (`001`, `002`… in the order of one export) and `{folder}` (the shoot folder's name). Save them as named profiles (web, client, print…) to switch between them. Rendered exports are tagged sRGB, with an ICC profile, and carry the EXIF of their original (camera, lens, date, exposure, copyright, GPS), with the orientation reset since their pixels are upright. Unedited JPEGs are copied as they are, unless they have to be resized.
- Reopens the last folder and photo on launch. Drop a folder onto the window to open it.
- **Plugins**: add your own actions and shortcuts with a small Python file (see [Plugins](#plugins)).

## Requirements
- Python 3.10+
- PyQt6 (6.9+), rawpy and numpy, installed from `pyproject.toml`

## Quick Start
```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
tamis                  # or: python Tamis.py, python -m tamis
```

## Keyboard Shortcuts
| Action | Keys |
| --- | --- |
| Keep current / selected | `P`, `1`, or `↑` (Cull view) |
| Reject current / selected | `X`, `3`, or `↓` (Cull view) |
| Unmark | `U`, `2`, or `Backspace` |
| Stars 0–5 | `⌘0`–`⌘5` / `Ctrl+0`–`Ctrl+5` |
| Colour label red / yellow / green / blue (again to remove) | `⌘6`–`⌘9` / `Ctrl+6`–`Ctrl+9` |
| Undo | `⌘Z` / `Ctrl+Z` |
| Next / previous photo | `→` `L` `Space` / `←` `H` |
| First / last photo | `Home` / `End` |
| Next unmarked photo | `N` |
| Keeps only in the Cull view, on / off | `K` |
| Zoom 1:1 / back to fit | `Z` or double-click / `Esc` |
| Compare (current + next, or the Gallery selection) / leave | `C` / `C` or `Esc` |
| Show / hide shooting info | `I` |
| Edit panel | `E` |
| Crop: open / apply, cancel | `R` or `Enter`, `Esc` |
| Rotate right / left (current or selected) | `]` or `⇧R` / `[` or `⇧L` |
| Show the photo without its edit, while held (Cull view) | `Y` or `\` |
| Clipping warnings, on / off (Cull view) | `J` |
| Copy / paste edit (paste applies to the selection) | `⌘C` / `⌘V` or `⌘⇧C` / `⌘⇧V` (`Ctrl+C` / `V`, `Ctrl+Shift+C` / `V`) |
| Export JPEG with the export settings (current or selected photos: edited ones and those without a JPEG are rendered, others copied unless resized) | `⌘⇧E` / `Ctrl+Shift+E` |
| Refresh the folder | `⌘R` / `Ctrl+R`, `F5` |
| Show bursts, on / off | `B` |
| Keep this photo, reject the rest of its burst | `⇧P` / `Shift+P` |
| Focus peaking, on / off (Cull view) | `F` |
| Import from a memory card | `⌘I` / `Ctrl+I` |
| Switch Cull ↔ Gallery | `G` (Enter or double-click in the gallery opens a photo) |
| Open folder | `⌘O` / `Ctrl+O` |

In the Cull view, marking a photo as keep or reject moves on to the next one (in Compare, it stays on the same photos). In the Gallery, marks, stars and labels apply to every selected photo.

## Film simulations
Film simulations are 3D LUTs, in the same HaldCLUT format RawTherapee uses (`.png` / `.tif`), or `.cube` files. Tamis does not ship any: choose a folder of them with **Folder…** in the edit panel (sub-folders become groups). RawTherapee's [film simulation collection](https://rawpedia.rawtherapee.com/Film_Simulation) (CC BY-SA) works as is, and gives the same looks as in RawTherapee. The base development of RAWs (LibRaw) is close to, but not identical with, RawTherapee's own.

Things the first version does not do yet: the 1:1 zoom shows the original inside the frame (for focus checking).

## Plugins
A plugin is a Python file (or a package folder) in the plugins folder, which **Plugins ▾ > Open Plugins Folder** creates and opens. Plugins load at launch, so restart Tamis after adding one. This is all it takes:

```python
NAME = "Keep the 5-star photos"  # optional: the name shown in the menu

def register(tamis):
    def keep_best():
        best = [photo for photo in tamis.photos() if photo.rating == 5]
        tamis.set_status(best, tamis.KEEP)  # one undo step
        tamis.toast(f"{len(best)} photo(s) kept")

    tamis.add_action("Keep 5-Star Photos", keep_best, shortcut="Ctrl+Shift+J")
    tamis.on("photo_changed", lambda photo: print(photo.path))
```

[`examples/plugins/keeps_list.py`](examples/plugins/keeps_list.py) is a complete example to copy.

| `tamis.` | What it does |
| --- | --- |
| `add_action(title, callback, shortcut=None)` | Adds an entry to the Plugins menu; returns its `QAction`. A shortcut that is invalid or already taken is dropped and reported |
| `on(event, callback)` | Calls `callback` when an event happens (below) |
| `folder`, `photos()`, `current_photo()`, `selected_photos()` | The open folder and its photos. The current photo can be `None`; `selected_photos()` is what a mark would apply to |
| `go_to(photo)` | Shows that photo |
| `set_status(photos, status)` | `tamis.KEEP`, `tamis.REJECT` or `None` |
| `set_rating(photos, stars)`, `set_label(photos, label)` | 0–5 stars; a colour label (`xmp.LABELS`) or `None` |
| `set_edit(photos, edit)` | Replaces the edit with the settings of `develop.DEFAULTS`; `{}` removes it |
| `toast(text)`, `message(title, text, details=None)` | A short message, or a dialog |
| `get_setting(key, default=None)`, `set_setting(key, value)` | Values kept across launches, private to the plugin; anything JSON can represent |
| `window` | The main window, for anything else. It is not a stable interface: it may change between versions |

`photos` is a photo or a path, or a list of them, and every `set_…` call is one undo step. A photo is a read-only snapshot with `path`, `name`, `files` (with its companions), `status`, `location`, `rating`, `label` and `edit`.

| Event | Arguments | When |
| --- | --- | --- |
| `folder_opened` / `folder_refreshed` | `folder` | A folder was opened / the open one was scanned again |
| `photo_changed` | `photo` | Another photo became the current one |
| `photos_edited` | `[photos]` | Marks, ratings, labels or edits changed, undo included |
| `files_moved` | `{old path: new path}` | Process Files moved photos. Photos are identified by path, so a plugin keeping data per photo must follow this |
| `photos_exported` | `[paths]` | JPEGs were written to the export folder |
| `photos_trashed` | `[paths]` | Photos of `_REJECTS` were moved to the Trash |
| `closing` | | The window is closing; changes made now are still saved |

Good to know:
- **Plugins run with your rights**, like any program: they can read and delete your files. Only install those you trust. They are only loaded from the plugins folder, never from a photo folder.
- Everything runs on the window's thread, so slow work in a plugin freezes the window: use a `QThread` for it.
- A failing plugin doesn't stop the app: the error appears in a toast and in **Plugins ▾ > Show Problems…**. **Plugins ▾ > Installed** turns a plugin off or on, from the next launch.
- In the built `Tamis.app`, plugins can import `tamis`, numpy, PyQt6's `QtCore`, `QtGui` and `QtWidgets`, and the parts of the standard library the app itself uses. A plugin that needs more runs from source (`python -m tamis`) or adds it to `scripts/build_app.sh` with `--hidden-import`.

## Sessions
`tamis_session.json` stores marks keyed by path relative to the photo folder (e.g. `_KEEPS/IMG_1.CR2`), so the folder can be moved or renamed without losing decisions. Older sessions keyed by filename are still read, and so are `cullspeed_session.json` files from when the app was called CullSpeed; the next save replaces them. Ratings and labels are stored there too; when a photo has an XMP sidecar, its rating and label are read from the sidecar instead, so changes made in another editor win. If a rating can't be written to its sidecar, the session's value wins until a later save manages to write it.

Marks of photos whose file is missing are kept, so a `_KEEPS` taken out of the folder and brought back gets them back. A session file that can't be read is never overwritten: it is renamed `tamis_session.corrupt.json`, the app says so, and the next most recent session is used if there is one.

If the photo folder is read-only (locked SD card, read-only share), marks are saved in the app's own data folder instead and reloaded from there when the folder is reopened.

## Code Layout
| Module | Role |
| --- | --- |
| `tamis/main.py` | Application entry point |
| `tamis/migration.py` | Brings over settings and sessions from when the app was called CullSpeed |
| `tamis/app.py` | Main window: navigation, marking, processing |
| `tamis/models.py` | Photo list model, gallery filter, folder scan and session file |
| `tamis/loader.py` | Background preview/thumbnail loading, caching and prefetching |
| `tamis/thumbcache.py` | Thumbnails on disk |
| `tamis/analysis.py` | Background capture dates, sharpness scores and perceptual hashes, remembered on disk |
| `tamis/bursts.py` | Grouping shots taken in quick succession |
| `tamis/peaking.py` | Focus peaking overlay and its background worker |
| `tamis/imaging.py` | Thread-safe decoding of RAW and standard images (QImage only) |
| `tamis/exif.py` | Minimal EXIF reader for shooting info, and the EXIF of exported JPEGs |
| `tamis/xmp.py` | Reading and editing ratings and labels in XMP sidecars |
| `tamis/develop.py` | Edit pipeline (numpy): turns and straightening, exposure, tone, colour, LUT, grain, vignette |
| `tamis/cropping.py` | Geometry of the crop frame while it is dragged |
| `tamis/lut.py` | HaldCLUT / `.cube` loading and fast 256³ lookup tables |
| `tamis/editing.py` | Developing photos for editing, background previews and thumbnails of edits, JPEG export |
| `tamis/editpanel.py` | The edit panel and its histogram |
| `tamis/tones.py` | Histograms and clipping overlays, and their background worker |
| `tamis/presets.py` | Named looks, kept in the app's data folder |
| `tamis/exportsettings.py` | Export size, quality, folder, name pattern and profiles |
| `tamis/fileops.py` | Planning and executing file moves, exports, the Trash and imports |
| `tamis/plugins.py` | Loading plugins, the API they receive and the Plugins menu |
| `tamis/widgets.py` | Image and compare views, thumbnail delegate, bars, toast and dialogs |
| `tamis/theme.py` | Dark palette and stylesheet |

## Development
```bash
pip install -e ".[dev]"
pytest                     # main window, plugins, edits, crop, presets, tones, LUTs, file moves, sessions, XMP, EXIF
ruff check .
```
CI runs both on every push and pull request (`.github/workflows/ci.yml`).

To build a standalone app (`dist/Tamis.app` on macOS):
```bash
pip install -e ".[build]"
./scripts/build_app.sh
```

## Development Notes
- Worker threads only produce `QImage`; `QPixmap` is created on the GUI thread.
- The app uses its own `QDialog`s instead of `QMessageBox`: with Qt 6.11 on recent macOS, `QMessageBox`'s native `NSAlert` crashes AppKit (`trace trap`).
- `faulthandler` is enabled at startup, so native crashes print a Python traceback.
