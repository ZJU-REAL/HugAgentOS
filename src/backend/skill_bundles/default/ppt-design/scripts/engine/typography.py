"""Typography scale for pptx slides — 6 size tokens.

Mirrors the typography scale from
``this skill: references/themes.md + palette-gallery.md``.
All values are in points (pt) and consumed via ``pptx.util.Pt`` at the
render call site.

Rule of thumb:
- Titles must be at least 2x larger than body to create visual hierarchy.
- Adjacent text elements should never be within 20% of each other in size.
"""
from __future__ import annotations


CAPTION = 11        # annotations / sources / footnotes
BODY = 14           # standard paragraph / list item
SUBTITLE = 20       # section sub-heading
TITLE = 28          # page title
LARGE_TITLE = 44    # cover / section divider title
DATA_CALLOUT = 72   # hero number display


SCALE: dict[str, int] = {
    "caption": CAPTION,
    "body": BODY,
    "subtitle": SUBTITLE,
    "title": TITLE,
    "large_title": LARGE_TITLE,
    "data_callout": DATA_CALLOUT,
}


def size(name: str) -> int:
    """Resolve a typography token to its point size; falls back to BODY.

    NOTE: kept byte-for-byte stable — the python-pptx fallback path
    (``slide_types.py``) depends on these exact values. New work uses
    :func:`scale` instead.
    """
    return SCALE.get(name.lower(), BODY)


# ── Density-anchored modular scale (mirrors typeScale() in the Node engine) ──
# All sizes derive from a body baseline chosen by content density, so adjacent
# roles never fall within 20% of each other (avoids the flat "AI" look).

_BASELINE = {"relaxed": 17, "dense": 14}

# Multipliers off the body baseline. Adjacent roles keep a >=1.25x gap.
_RATIOS = {
    "kicker": 0.62,
    "caption": 0.78,
    "support": 0.85,
    "body": 1.0,
    "subtitle": 1.35,
    "title": 1.7,
    "large_title": 3.0,
    "data_callout": 5.0,
}


def scale(role: str, density: str = "dense", title_ratio: float | None = None) -> int:
    """Resolve a role to a point size for the given content ``density``.

    ``title_ratio`` (from the active :class:`StyleRecipe`) overrides the
    default title multiplier so the four styles get distinct title weights.
    """
    base = _BASELINE.get(density, _BASELINE["dense"])
    ratio = _RATIOS.get(role.lower(), 1.0)
    if role.lower() == "title" and title_ratio:
        ratio = title_ratio
    return max(9, int(round(base * ratio)))
