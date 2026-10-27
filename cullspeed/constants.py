RAW_EXTENSIONS = frozenset({'.arw', '.cr2', '.cr3', '.nef', '.dng', '.orf', '.raf', '.rw2'})
STANDARD_EXTENSIONS = frozenset({'.jpg', '.jpeg', '.png', '.webp', '.tif', '.tiff', '.heic'})
SUPPORTED_EXTENSIONS = RAW_EXTENSIONS | STANDARD_EXTENSIONS

KEEP = "keep"
REJECT = "reject"

KEEP_DIR = "_KEEPS"
REJECT_DIR = "_REJECTS"

SESSION_FILENAME = "cullspeed_session.json"
LEGACY_GLOBAL_CONFIG_FILENAME = ".cullspeed_global.json"

# Longest edge, in pixels, of decoded images.
PREVIEW_MAX_SIZE = 3000
THUMBNAIL_MAX_SIZE = 240

# How many decoded previews to keep in memory, and how far to prefetch.
PREVIEW_CACHE_SIZE = 8
PREFETCH_AHEAD = 3
PREFETCH_BEHIND = 1

# How many thumbnails to keep in memory (~150 KB each at THUMBNAIL_MAX_SIZE).
THUMBNAIL_CACHE_SIZE = 1000

UNDO_LIMIT = 500

# Colors
COLOR_BG = "#121212"
COLOR_PANEL = "#1a1a1a"
COLOR_CARD = "#1f1f1f"
COLOR_CARD_HOVER = "#2a2a2a"
COLOR_BORDER = "#2c2c2c"
COLOR_TEXT = "#e5e5e5"
COLOR_MUTED = "#9ca3af"
COLOR_ACCENT = "#3b82f6"
COLOR_KEEP = "#4ade80"
COLOR_REJECT = "#f87171"
