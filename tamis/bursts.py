"""Grouping shots taken in quick succession. Only reads capture times, sharpness and hashes off photos."""
from .constants import BURST_GAP_SECONDS
from .models import Burst

# Out of 64 bits. Generous, because the frames of a burst do change: this only
# separates shots that are clearly of something else.
MAX_HASH_DISTANCE = 26


def _similar(a, b):
    """False only when both hashes are known and far apart."""
    return a.image_hash is None or b.image_hash is None or (a.image_hash ^ b.image_hash).bit_count() <= MAX_HASH_DISTANCE


def groups(photos, gap=BURST_GAP_SECONDS):
    """Lists of two or more photos, in capture order: each within `gap` seconds of the previous one, and looking like it."""
    dated = sorted((photo for photo in photos if photo.capture_time is not None), key=lambda photo: photo.capture_time)
    runs = []
    for photo in dated:
        if runs and photo.capture_time - runs[-1][-1].capture_time <= gap and _similar(runs[-1][-1], photo):
            runs[-1].append(photo)
        else:
            runs.append([photo])
    return [run for run in runs if len(run) >= 2]


def assign(photos, gap=BURST_GAP_SECONDS):
    """Set each photo's `burst`, marking the sharpest frame of each once at least two are scored."""
    for photo in photos:
        photo.burst = None
    for burst_id, run in enumerate(groups(photos, gap)):
        scored = [photo for photo in run if photo.sharpness is not None]
        sharpest = max(scored, key=lambda photo: photo.sharpness) if len(scored) >= 2 else None
        for position, photo in enumerate(run):
            photo.burst = Burst(burst_id, position, len(run), photo is sharpest)
