"""Style recipes — 4 visual styles controlling corner radius + spacing.

Mirrors the style recipes from
``this skill: references/themes.md + palette-gallery.md``.

Choose a recipe based on presentation tone:
- ``sharp``  — data-dense, formal reports (right-angle corners, tight spacing)
- ``soft``   — corporate / business decks (light rounding, moderate spacing) [default]
- ``rounded``— product intros, marketing (medium-large corners, relaxed spacing)
- ``pill``   — launch events, premium brand (full pill corners, abundant whitespace)

All values are in INCHES (matching python-pptx's ``Inches()`` helper).
"""
from __future__ import annotations

from typing import TypedDict


class StyleRecipe(TypedDict):
    name: str
    radius_small: float
    radius_medium: float
    radius_large: float
    padding_min: float
    padding_max: float
    gap_min: float
    gap_max: float
    page_margin: float
    block_gap_min: float
    block_gap_max: float
    # ── visual character (consumed by the pptxgenjs Node engine via WP1) ──
    shadow_tier: str        # "none" | "resting" | "raised"
    type_density: str       # "dense" | "relaxed"  (drives the type scale baseline)
    title_ratio: float      # page-title size as a multiple of the body baseline
    accent_budget: int      # max number of accent-colored focal elements per slide
    icon_style: str         # "line" | "duotone" | "filled"


RECIPES: dict[str, StyleRecipe] = {
    "sharp": {
        "name": "sharp",
        "radius_small": 0.0,
        "radius_medium": 0.03,
        "radius_large": 0.05,
        "padding_min": 0.10,
        "padding_max": 0.15,
        "gap_min": 0.10,
        "gap_max": 0.20,
        "page_margin": 0.30,
        "block_gap_min": 0.25,
        "block_gap_max": 0.35,
        "shadow_tier": "none",
        "type_density": "dense",
        "title_ratio": 1.45,
        "accent_budget": 1,
        "icon_style": "line",
    },
    "soft": {
        "name": "soft",
        "radius_small": 0.05,
        "radius_medium": 0.08,
        "radius_large": 0.12,
        "padding_min": 0.15,
        "padding_max": 0.20,
        "gap_min": 0.15,
        "gap_max": 0.25,
        "page_margin": 0.40,
        "block_gap_min": 0.35,
        "block_gap_max": 0.50,
        "shadow_tier": "resting",
        "type_density": "dense",
        "title_ratio": 1.6,
        "accent_budget": 2,
        "icon_style": "duotone",
    },
    "rounded": {
        "name": "rounded",
        "radius_small": 0.10,
        "radius_medium": 0.15,
        "radius_large": 0.25,
        "padding_min": 0.20,
        "padding_max": 0.30,
        "gap_min": 0.25,
        "gap_max": 0.40,
        "page_margin": 0.50,
        "block_gap_min": 0.50,
        "block_gap_max": 0.70,
        "shadow_tier": "resting",
        "type_density": "relaxed",
        "title_ratio": 1.75,
        "accent_budget": 2,
        "icon_style": "filled",
    },
    "pill": {
        "name": "pill",
        "radius_small": 0.20,
        "radius_medium": 0.30,
        "radius_large": 0.50,
        "padding_min": 0.25,
        "padding_max": 0.40,
        "gap_min": 0.30,
        "gap_max": 0.50,
        "page_margin": 0.60,
        "block_gap_min": 0.60,
        "block_gap_max": 0.90,
        "shadow_tier": "raised",
        "type_density": "relaxed",
        "title_ratio": 1.9,
        "accent_budget": 2,
        "icon_style": "filled",
    },
}


def get_recipe(name: str | None) -> StyleRecipe:
    """Resolve a recipe name; falls back to ``soft`` (the default)."""
    return RECIPES.get((name or "soft").lower(), RECIPES["soft"])


def list_recipes() -> list[str]:
    return list(RECIPES.keys())
