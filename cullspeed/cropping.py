"""Geometry of the crop frame while it is being dragged.

A frame is (left, top, right, bottom), in fractions of the photo's width and
height. `aspect` is the frame's width / height in those same fractions: a
photo's own shape is 1.0, and a pixel ratio r is r × photo height / width.
"""
import math

FULL = (0.0, 0.0, 1.0, 1.0)
MIN_SIZE = 0.02  # smallest frame side the mouse can make

# Handle names: any of l, r, t, b for the edges they move, or MOVE for the whole frame.
MOVE = "move"
HANDLES = ("tl", "tr", "bl", "br", "t", "b", "l", "r")


def _clamp(value, low, high):
    return max(low, min(high, value))


def _sized(width, height, max_width, max_height, aspect):
    """The (width, height) of `aspect` closest to the size asked for that fits the limits."""
    width = max(width, height * aspect, MIN_SIZE, MIN_SIZE * aspect)
    width = min(width, max_width, max_height * aspect)
    return width, width / aspect


def drag(frame, handle, dx, dy, aspect=None):
    """`frame` after `handle` has been dragged by (dx, dy); it stays inside the photo."""
    left, top, right, bottom = frame
    if handle == MOVE:
        dx, dy = _clamp(dx, -left, 1 - right), _clamp(dy, -top, 1 - bottom)
        return left + dx, top + dy, right + dx, bottom + dy

    if "l" in handle:
        left = _clamp(left + dx, 0.0, right - MIN_SIZE)
    if "r" in handle:
        right = _clamp(right + dx, left + MIN_SIZE, 1.0)
    if "t" in handle:
        top = _clamp(top + dy, 0.0, bottom - MIN_SIZE)
    if "b" in handle:
        bottom = _clamp(bottom + dy, top + MIN_SIZE, 1.0)
    if aspect is None:
        return left, top, right, bottom

    # Corners keep the opposite corner in place; edges keep the opposite edge
    # and the frame's centre along that edge.
    moves_x, moves_y = "l" in handle or "r" in handle, "t" in handle or "b" in handle
    centre_x, centre_y = (frame[0] + frame[2]) / 2, (frame[1] + frame[3]) / 2
    max_width = (right if "l" in handle else 1 - left) if moves_x else 2 * min(centre_x, 1 - centre_x)
    max_height = (bottom if "t" in handle else 1 - top) if moves_y else 2 * min(centre_y, 1 - centre_y)
    if moves_x and moves_y:
        wanted_width, wanted_height = right - left, bottom - top
    elif moves_x:
        wanted_width, wanted_height = right - left, 0.0
    else:
        wanted_width, wanted_height = 0.0, bottom - top
    width, height = _sized(wanted_width, wanted_height, max_width, max_height, aspect)

    if moves_x:
        left, right = (right - width, right) if "l" in handle else (left, left + width)
    else:
        left, right = centre_x - width / 2, centre_x + width / 2
    if moves_y:
        top, bottom = (bottom - height, bottom) if "t" in handle else (top, top + height)
    else:
        top, bottom = centre_y - height / 2, centre_y + height / 2
    return left, top, right, bottom


def fit_aspect(frame, aspect):
    """A frame of `aspect` with about the area and centre of `frame`, inside the photo."""
    if aspect is None:
        return frame
    left, top, right, bottom = frame
    area = (right - left) * (bottom - top)
    width = math.sqrt(area * aspect)
    height = width / aspect
    scale = min(1.0, 1 / width, 1 / height)  # too big for the photo in one direction
    width, height = width * scale, height * scale
    x = _clamp((left + right) / 2 - width / 2, 0.0, 1 - width)
    y = _clamp((top + bottom) / 2 - height / 2, 0.0, 1 - height)
    return x, y, x + width, y + height
