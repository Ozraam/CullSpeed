"""Star ratings and colour labels in XMP sidecars (xmp:Rating, xmp:Label).

These are the properties Lightroom, Bridge and Capture One read from
sidecars, and darktable reads xmp:Rating. There is no pick or reject flag that
they share: Lightroom keeps its flags in its catalog only, so keep and reject
can only be passed on through these two properties (app.XMP_MARKS). Existing sidecars are edited in
place, changing only those two properties; a sidecar that cannot be
understood is left untouched. New sidecars are only created for RAWs, as
`IMG.xmp`: editors read JPEG metadata from the file itself, not a sidecar.

`names` arguments map lowercase file names to actual names for the folder
the photo is in, so sidecars are found whatever their case.
"""
import os
import re
from html import escape, unescape

from .constants import is_raw

LABELS = ("Red", "Yellow", "Green", "Blue", "Purple")

# What "rejected" is in xmp:Rating for darktable and Bridge. Lightroom ignores it.
REJECTED_RATING = -1

_NAMESPACE = "http://ns.adobe.com/xap/1.0/"
_RATING_ATTR = re.compile(r'\s+xmp:Rating\s*=\s*(["\'])(-?\d+)\1')
_RATING_ELEMENT = re.compile(r'[ \t]*<xmp:Rating>\s*(-?\d+)\s*</xmp:Rating>[ \t]*\n?')
_LABEL_ATTR = re.compile(r'\s+xmp:Label\s*=\s*(["\'])(.*?)\1', re.S)
_LABEL_ELEMENT = re.compile(r'[ \t]*<xmp:Label>(.*?)</xmp:Label>[ \t]*\n?', re.S)
_DESCRIPTION = re.compile(r'<rdf:Description\b')

_TEMPLATE = """<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="Tamis">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""/>
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>
"""


def _candidates(path):
    """Sidecar names for `path`, most standard first: `IMG.xmp` (RAWs), `IMG.CR2.xmp`."""
    name = os.path.basename(path)
    names = [os.path.splitext(name)[0] + ".xmp"] if is_raw(path) else []
    return [*names, name + ".xmp"]


def _existing(path, names):
    folder = os.path.dirname(path)
    return [os.path.join(folder, names[c.lower()]) for c in _candidates(path) if c.lower() in names]


# -- reading -----------------------------------------------------------------

def parse(text):
    """(rating 0–5, label or None) from XMP text."""
    match = _RATING_ATTR.search(text)
    rating = match.group(2) if match else None
    if rating is None:
        match = _RATING_ELEMENT.search(text)
        rating = match.group(1) if match else "0"
    match = _LABEL_ATTR.search(text)
    label = match.group(2) if match else None
    if label is None:
        match = _LABEL_ELEMENT.search(text)
        label = match.group(1) if match else None
    label = unescape(label).strip() if label else None
    return min(5, max(0, int(rating))), label or None  # Lightroom's "-1 = rejected" is not a rating


def _rejected(text):
    """Whether the sidecar's rating is the -1 that darktable and Bridge use for a rejected photo."""
    match = _RATING_ATTR.search(text)
    if match:
        return int(match.group(2)) < 0
    match = _RATING_ELEMENT.search(text)
    return bool(match) and int(match.group(1)) < 0


def read(paths, names):
    """(rating, label) from the first readable sidecar of any of `paths`, or None.

    The rating is None when the sidecar says "rejected": a -1 says nothing about
    the stars, which Tamis may have replaced with it, see REJECTED_RATING.
    """
    for path in paths:
        for sidecar in _existing(path, names):
            try:
                with open(sidecar, encoding='utf-8') as file:
                    text = file.read()
                rating, label = parse(text)
                return (None if _rejected(text) else rating), label
            except (OSError, UnicodeDecodeError, ValueError):
                continue
    return None


# -- writing -----------------------------------------------------------------

def update(text, rating, label):
    """`text` with its rating and label replaced, or None if it has nowhere to put them."""
    for pattern in (_RATING_ATTR, _RATING_ELEMENT, _LABEL_ATTR, _LABEL_ELEMENT):
        text = pattern.sub('', text)
    match = _DESCRIPTION.search(text)
    if match is None:
        return None
    tag_end = text.find('>', match.end())
    if tag_end == -1:
        return None

    attributes = ""
    if "xmlns:xmp=" not in text[:tag_end]:  # not declared on this element or an ancestor
        attributes += f' xmlns:xmp="{_NAMESPACE}"'
    if rating:
        attributes += f' xmp:Rating="{rating}"'
    if label:
        attributes += f' xmp:Label="{escape(label)}"'
    return text[:match.end()] + attributes + text[match.end():]


def write(paths, rating, label, names):
    """Store `rating` / `label` for the photo made of `paths` (a RAW and its JPEG, say).

    Updates every existing sidecar; creates `IMG.xmp` for a RAW that has
    none, unless there is nothing to store. Raises OSError on failure.
    """
    targets = []
    for path in paths:
        existing = _existing(path, names)
        if existing:
            targets += existing
        elif is_raw(path) and (rating or label):
            name = _candidates(path)[0]
            names[name.lower()] = name
            targets.append(os.path.join(os.path.dirname(path), name))

    for target in dict.fromkeys(targets):
        try:
            with open(target, encoding='utf-8') as file:
                text = file.read()
        except FileNotFoundError:
            text = _TEMPLATE
        except UnicodeDecodeError:
            continue  # not text we understand: leave it alone
        new_text = update(text, rating, label)
        if new_text is None or new_text == text:
            continue
        tmp_path = target + ".tmp"
        with open(tmp_path, 'w', encoding='utf-8') as file:
            file.write(new_text)
        os.replace(tmp_path, target)
