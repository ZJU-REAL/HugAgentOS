"""Internal color/run-property helpers shared across pptx renderers.

Kept as a module-level utility so ``decorations.py`` and ``slide_types.py``
don't each redefine the same one-line ``RGBColor.from_string`` wrapper.
"""
from __future__ import annotations


def hex_to_rgb(hex_str: str):
    """Hex string (with or without ``#``) → python-pptx ``RGBColor``."""
    from pptx.dml.color import RGBColor
    return RGBColor.from_string(hex_str.lstrip("#"))


def set_char_spacing(run, pt_value: float) -> None:
    """Set character (letter) spacing on a python-pptx run.

    python-pptx exposes no API for this — splice ``spc`` into the run's
    ``rPr`` element directly. Units are 1/100ths of a point.
    """
    rPr = run._r.get_or_add_rPr()
    rPr.set("spc", str(int(pt_value * 100)))
