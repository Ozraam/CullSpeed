# CullSpeed

CullSpeed is a PyQt6 desktop application for rapidly reviewing and triaging large photo shoots. It focuses on fast thumbnail loading (including embedded RAW previews), streamlined keyboard navigation, and one-click sorting of files into keep/reject folders.

## Features
- Supports JPEG, PNG, WebP, and common RAW formats (ARW, CR2, NEF, DNG, ORF, RAF, RW2) via `rawpy`.
- Dual workflow views: **Cull** view for single-image focus with filmstrip, and **Gallery** grid view with multi-select editing.
- Non-destructive marking system that persists between sessions via `cullspeed_session.json` per folder and a global `.cullspeed_global.json` for last session recall.
- Batch processing that moves images into `_KEEPS` or `_REJECTS` folders (or back to the root) according to their marks.
- Keyboard-driven workflow with fast keeps/rejects, navigation, and bulk updates.

## Requirements
- Python 3.10+ (PyQt6 wheels require a modern Python release).
- System packages required by `rawpy`/LibRaw (install via your distribution if missing).
- Python dependencies from `requirements.txt`:
  - PyQt6
  - rawpy
  - imageio

## Quick Start
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python CullSpeed.py
```

## Using CullSpeed
1. Launch the app and click **Open Folder** to choose a directory of photos. The current selection is saved so reopening is instant.
2. Browse images in the Cull or Gallery tabs. Thumbnails are generated on demand using embedded RAW previews when available.
3. Use marks to flag photos as keep, reject, or unmarked. Marks are saved automatically.
4. Click **Process Files** when ready. The app will summarize the planned moves and, once confirmed, create `_KEEPS` and `_REJECTS` subfolders (if needed) and move files accordingly.

### Keyboard Shortcuts
| Action | Keys |
| --- | --- |
| Keep current / selected | `P`, `1`, or `↑` |
| Reject current / selected | `X`, `3`, or `↓` |
| Unmark | `U`, `2`, or `Backspace` |
| Next image | `L` or `→` |
| Previous image | `H` or `←` |

## Sessions and Global Config
- `cullspeed_session.json` lives inside each photo folder and stores marks keyed by filename so you can move the folder without losing decisions.
- `.cullspeed_global.json` sits in your home directory and remembers the last opened folder/file so the app can reopen automatically.

## Development Notes
- The UI logic resides in `CullSpeed.py`. No additional build step is required—run the script directly.
- When modifying keyboard shortcuts or UI themes, keep platform consistency in mind (PyQt6 ships with Fusion style by default here).
- Consider running the app with `python -X faulthandler CullSpeed.py` while debugging any native crashes related to LibRaw/Qt.

Happy culling!
