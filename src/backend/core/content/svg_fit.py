"""Auto-fit an SVG's ``viewBox`` (and width/height) to its real content bounds.

Models routinely emit SVG diagrams whose root ``viewBox`` is a few pixels
shorter than the drawn content — a bottom box / legend / last layer gets
clipped — and frequently omit ``width``/``height`` entirely (so rendered as an
``<img>`` the SVG collapses to the browser's 300×150 fallback). Both make the
delivered diagram look truncated even though the markup is "complete".

This normaliser parses the geometry of the common primitives, computes the
content bounding box, and **only ever expands** the viewBox (never shrinks it),
filling in width/height when missing. It edits *only* the opening ``<svg>``
tag's attributes via a targeted regex so the rest of the document is preserved
byte-for-byte.

Fully fail-safe: any decode/parse problem returns the original bytes untouched.
"""

from __future__ import annotations

import logging
import re
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# Containers whose descendants are NOT painted onto the canvas directly
# (defs/markers/gradients/etc). Skipping them avoids polluting the bbox with
# e.g. a gradient's ``0%``/``100%`` stops or a marker's tiny local coords.
_SKIP_TAGS = {
    "defs", "marker", "symbol", "clippath", "mask", "pattern", "filter",
    "lineargradient", "radialgradient", "metadata", "title", "desc",
    "style", "script",
}

_NUM = re.compile(r"[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?")
_TRANSLATE = re.compile(r"translate\(\s*([-+0-9.eE]+)[ ,]+([-+0-9.eE]+)?\s*\)")
_SVG_OPEN = re.compile(r"<svg\b[^>]*>", re.IGNORECASE | re.DOTALL)


def _floats(s: Optional[str]) -> list[float]:
    return [float(x) for x in _NUM.findall(s or "")]


def _local(tag) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1].lower()


class _Bounds:
    __slots__ = ("minx", "miny", "maxx", "maxy", "_set")

    def __init__(self) -> None:
        self.minx = self.miny = self.maxx = self.maxy = 0.0
        self._set = False

    def add(self, x0: float, y0: float, x1: float, y1: float) -> None:
        lo_x, hi_x = (x0, x1) if x0 <= x1 else (x1, x0)
        lo_y, hi_y = (y0, y1) if y0 <= y1 else (y1, y0)
        if not self._set:
            self.minx, self.miny, self.maxx, self.maxy = lo_x, lo_y, hi_x, hi_y
            self._set = True
            return
        self.minx = min(self.minx, lo_x)
        self.miny = min(self.miny, lo_y)
        self.maxx = max(self.maxx, hi_x)
        self.maxy = max(self.maxy, hi_y)

    def result(self) -> Optional[Tuple[float, float, float, float]]:
        if not self._set:
            return None
        return (self.minx, self.miny, self.maxx, self.maxy)


def _approx_text_extent(el, font_size: float) -> Tuple[float, float]:
    """Rough text width + how far it drops below the baseline."""
    text = "".join(el.itertext()) or ""
    width = 0.0
    for ch in text:
        # CJK / full-width glyphs are ~1em wide; latin ~0.6em.
        width += font_size if ord(ch) > 0x2E80 else font_size * 0.6
    return width, font_size * 0.3  # descender allowance


def _walk(el, tx: float, ty: float, font_size: float, acc: _Bounds) -> None:
    name = _local(el.tag)
    if name in _SKIP_TAGS:
        return

    # Accumulate translate() only; scale/rotate/matrix are ignored (best-effort,
    # and we only ever expand the viewBox so an approximation never clips).
    transform = el.get("transform")
    if transform:
        m = _TRANSLATE.search(transform)
        if m:
            tx += float(m.group(1))
            ty += float(m.group(2) or 0.0)

    fs = el.get("font-size")
    cur_fs = font_size
    if fs:
        nums = _floats(fs)
        if nums:
            cur_fs = nums[0]

    try:
        if name == "rect":
            x = _f(el, "x"); y = _f(el, "y")
            w = _f(el, "width"); h = _f(el, "height")
            acc.add(tx + x, ty + y, tx + x + w, ty + y + h)
        elif name == "circle":
            cx = _f(el, "cx"); cy = _f(el, "cy"); r = _f(el, "r")
            acc.add(tx + cx - r, ty + cy - r, tx + cx + r, ty + cy + r)
        elif name == "ellipse":
            cx = _f(el, "cx"); cy = _f(el, "cy")
            rx = _f(el, "rx"); ry = _f(el, "ry")
            acc.add(tx + cx - rx, ty + cy - ry, tx + cx + rx, ty + cy + ry)
        elif name == "line":
            x1 = _f(el, "x1"); y1 = _f(el, "y1")
            x2 = _f(el, "x2"); y2 = _f(el, "y2")
            acc.add(tx + x1, ty + y1, tx + x2, ty + y2)
        elif name in ("polygon", "polyline"):
            pts = _floats(el.get("points"))
            for i in range(0, len(pts) - 1, 2):
                acc.add(tx + pts[i], ty + pts[i + 1], tx + pts[i], ty + pts[i + 1])
        elif name == "path":
            for px, py in _path_points(el.get("d") or ""):
                acc.add(tx + px, ty + py, tx + px, ty + py)
        elif name == "text":
            x = _f(el, "x"); y = _f(el, "y")
            anchor = (el.get("text-anchor") or "start").strip().lower()
            tw, drop = _approx_text_extent(el, cur_fs)
            if anchor == "middle":
                left, right = x - tw / 2, x + tw / 2
            elif anchor == "end":
                left, right = x - tw, x
            else:
                left, right = x, x + tw
            acc.add(tx + left, ty + y - cur_fs, tx + right, ty + y + drop)
        elif name in ("image", "use", "foreignobject"):
            x = _f(el, "x"); y = _f(el, "y")
            w = _f(el, "width"); h = _f(el, "height")
            if w or h:
                acc.add(tx + x, ty + y, tx + x + w, ty + y + h)
    except Exception:  # never let one weird node abort the whole fit
        pass

    for child in el:
        _walk(child, tx, ty, cur_fs, acc)


def _f(el, attr: str) -> float:
    nums = _floats(el.get(attr))
    return nums[0] if nums else 0.0


def _path_points(d: str):
    """Yield absolute (x, y) points by tracking the path cursor through commands."""
    tokens = re.findall(r"[a-zA-Z]|[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?", d)
    i = 0
    cx = cy = 0.0
    start_x = start_y = 0.0
    cmd = ""
    n = len(tokens)

    def num():
        nonlocal i
        v = float(tokens[i]); i += 1
        return v

    while i < n:
        t = tokens[i]
        if t.isalpha():
            cmd = t
            i += 1
            if cmd in ("Z", "z"):
                cx, cy = start_x, start_y
                yield cx, cy
            continue
        if not cmd:
            i += 1
            continue
        rel = cmd.islower()
        c = cmd.upper()
        try:
            if c in ("M", "L", "T"):
                x = num(); y = num()
                cx, cy = (cx + x, cy + y) if rel else (x, y)
                if c == "M":
                    start_x, start_y = cx, cy
            elif c == "H":
                x = num(); cx = cx + x if rel else x
            elif c == "V":
                y = num(); cy = cy + y if rel else y
            elif c in ("S", "Q"):
                num(); num(); x = num(); y = num()
                cx, cy = (cx + x, cy + y) if rel else (x, y)
            elif c == "C":
                num(); num(); num(); num(); x = num(); y = num()
                cx, cy = (cx + x, cy + y) if rel else (x, y)
            elif c == "A":
                num(); num(); num(); num(); num(); x = num(); y = num()
                cx, cy = (cx + x, cy + y) if rel else (x, y)
            else:
                i += 1
                continue
        except (IndexError, ValueError):
            break
        yield cx, cy


def _set_attr(tag: str, attr: str, value: str) -> str:
    pat = re.compile(r"(\b" + re.escape(attr) + r"\s*=\s*)(\"[^\"]*\"|'[^']*')", re.IGNORECASE)
    if pat.search(tag):
        return pat.sub(lambda m: m.group(1) + '"' + value + '"', tag, count=1)
    return re.sub(r"(<svg\b)", r'\1 ' + attr + '="' + value + '"', tag, count=1, flags=re.IGNORECASE)


def _fmt(v: float) -> str:
    return f"{v:.2f}".rstrip("0").rstrip(".")


def fit_svg_viewbox(content: bytes, *, pad: float = 12.0) -> bytes:
    """Return ``content`` with the root SVG viewBox expanded to fit all content.

    No-op (returns the original bytes) when the content is not an SVG, can't be
    parsed, or already covers everything.
    """
    try:
        text = content.decode("utf-8")
    except Exception:
        return content
    if "<svg" not in text[:2048].lower():
        return content

    try:
        from lxml import etree

        root = etree.fromstring(content, parser=etree.XMLParser(recover=True, huge_tree=True))
    except Exception:
        return content
    if root is None or _local(root.tag) != "svg":
        return content

    try:
        acc = _Bounds()
        root_fs = _floats(root.get("font-size"))
        for child in root:
            _walk(child, 0.0, 0.0, root_fs[0] if root_fs else 16.0, acc)
        bbox = acc.result()
    except Exception as exc:
        logger.debug("svg bbox computation failed: %s", exc)
        return content
    if bbox is None:
        return content

    minx, miny, maxx, maxy = bbox
    # Sanity guard against runaway numbers from malformed paths.
    if not all(abs(v) < 1_000_000 for v in bbox):
        return content

    vb = root.get("viewBox") or root.get("viewbox")
    vb_nums = _floats(vb) if vb else []
    has_vb = len(vb_nums) == 4
    if has_vb:
        vx, vy, vw, vh = vb_nums
        cur_minx, cur_miny = vx, vy
        cur_maxx, cur_maxy = vx + vw, vy + vh
        # Only grow a side when the raw content actually overflows it; pad only
        # the side that grew, so an already-correct viewBox is left untouched.
        new_minx = (minx - pad) if minx < cur_minx else cur_minx
        new_miny = (miny - pad) if miny < cur_miny else cur_miny
        new_maxx = (maxx + pad) if maxx > cur_maxx else cur_maxx
        new_maxy = (maxy + pad) if maxy > cur_maxy else cur_maxy
    else:
        new_minx, new_miny = minx - pad, miny - pad
        new_maxx, new_maxy = maxx + pad, maxy + pad

    new_w = new_maxx - new_minx
    new_h = new_maxy - new_miny
    if new_w <= 0 or new_h <= 0:
        return content

    w_attr = root.get("width")
    h_attr = root.get("height")
    width_missing = not w_attr or not h_attr
    width_numeric = bool(w_attr and h_attr and not re.search(r"[%a-zA-Z]", w_attr + h_attr))

    grew = (
        not has_vb
        or abs(new_minx - cur_minx) > 0.5
        or abs(new_miny - cur_miny) > 0.5
        or abs(new_maxx - cur_maxx) > 0.5
        or abs(new_maxy - cur_maxy) > 0.5
    )
    if not grew and not width_missing:
        return content

    new_vb = f"{_fmt(new_minx)} {_fmt(new_miny)} {_fmt(new_w)} {_fmt(new_h)}"

    m = _SVG_OPEN.search(text)
    if not m:
        return content
    tag = m.group(0)
    # Strip any existing viewBox (either case) then insert the canonical one.
    tag = re.sub(r"\bviewbox\s*=\s*(\"[^\"]*\"|'[^']*')", "", tag, flags=re.IGNORECASE)
    tag = re.sub(r"(<svg\b)", r'\1 viewBox="' + new_vb + '"', tag, count=1, flags=re.IGNORECASE)
    if width_missing or width_numeric:
        tag = _set_attr(tag, "width", _fmt(new_w))
        tag = _set_attr(tag, "height", _fmt(new_h))

    new_text = text[: m.start()] + tag + text[m.end():]
    try:
        return new_text.encode("utf-8")
    except Exception:
        return content
