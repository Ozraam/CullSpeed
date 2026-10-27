# CullSpeed

CullSpeed is a PyQt6 desktop application for rapidly reviewing and triaging large photo shoots. It focuses on fast image loading (embedded RAW previews, background decoding and prefetching), keyboard-driven marking, and one-click sorting of files into keep/reject folders.

## Features
- Supports JPEG, PNG, WebP, TIFF, HEIC (when Qt has a plugin for it) and common RAW formats (ARW, CR2, CR3, NEF, DNG, ORF, RAF, RW2) via `rawpy`.
- **Cull** view: the current photo fitted to the window with a filmstrip; next/previous photos are prefetched so stepping through is instant. Double-click or `Z` zooms to 1:1 (the photo is loaded at full resolution for focus checking), drag to pan.
- **Gallery** view: thumbnail grid with multi-select marking, filters (All / Unmarked / Keeps / Rejects) and adjustable thumbnail size.
- Undo for every marking action.
- Marks are saved automatically in `cullspeed_session.json` inside each photo folder.
- **Process Files** moves photos into `_KEEPS` / `_REJECTS` (or back to the root when unmarked). XMP sidecars move with their photo, and existing files are never overwritten (`IMG_1 (1).jpg` instead).
- Reopens the last folder and photo on launch. Drop a folder onto the window to open it.

## Requirements
- Python 3.10+
- PyQt6 (6.9+), rawpy and numpy, installed from `pyproject.toml`

## Quick Start
```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
cullspeed                  # or: python CullSpeed.py, python -m cullspeed
```

## Keyboard Shortcuts
| Action | Keys |
| --- | --- |
| Keep current / selected | `P`, `1`, or `↑` (Cull view) |
| Reject current / selected | `X`, `3`, or `↓` (Cull view) |
| Unmark | `U`, `2`, or `Backspace` |
| Undo | `⌘Z` / `Ctrl+Z` |
| Next / previous photo | `→` `L` `Space` / `←` `H` |
| First / last photo | `Home` / `End` |
| Next unmarked photo | `N` |
| Zoom 1:1 / back to fit | `Z` or double-click / `Esc` |
| Switch Cull ↔ Gallery | `G` (Enter or double-click in the gallery opens a photo) |
| Open folder | `⌘O` / `Ctrl+O` |

In the Cull view, marking a photo as keep or reject moves on to the next one. In the Gallery, marks apply to every selected photo.

## Sessions
`cullspeed_session.json` stores marks keyed by path relative to the photo folder (e.g. `_KEEPS/IMG_1.CR2`), so the folder can be moved or renamed without losing decisions. Older sessions keyed by filename are still read.

## Code Layout
| Module | Role |
| --- | --- |
| `cullspeed/main.py` | Application entry point |
| `cullspeed/app.py` | Main window: navigation, marking, processing |
| `cullspeed/models.py` | Photo list model, gallery filter, folder scan and session file |
| `cullspeed/loader.py` | Background preview/thumbnail loading, caching and prefetching |
| `cullspeed/imaging.py` | Thread-safe decoding of RAW and standard images (QImage only) |
| `cullspeed/fileops.py` | Planning and executing file moves |
| `cullspeed/widgets.py` | Image view, thumbnail delegate, bars, toast and dialogs |
| `cullspeed/theme.py` | Dark palette and stylesheet |

## Development
```bash
pip install -e ".[dev]"
pytest                     # file moves, session file, model counters
ruff check .
```
CI runs both on every push and pull request (`.github/workflows/ci.yml`).

To build a standalone app (`dist/CullSpeed.app` on macOS):
```bash
pip install -e ".[build]"
./scripts/build_app.sh
```

## Development Notes
- Worker threads only produce `QImage`; `QPixmap` is created on the GUI thread.
- The app uses its own `QDialog`s instead of `QMessageBox`: with Qt 6.11 on recent macOS, `QMessageBox`'s native `NSAlert` crashes AppKit (`trace trap`).
- `faulthandler` is enabled at startup, so native crashes print a Python traceback.
