"""Helpers shared by the background engines."""

from contextlib import suppress


def emit_safely(signal, *args):
    """Emit a worker's result, unless its engine was already destroyed during shutdown."""
    with suppress(RuntimeError):
        signal.emit(*args)
