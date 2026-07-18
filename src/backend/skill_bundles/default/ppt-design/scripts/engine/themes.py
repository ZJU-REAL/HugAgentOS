"""Color palettes for pptx slides — curated theme catalog.

2 core themes (``swiss_klein`` default + ``navy_gold`` tech) plus an extended
gallery distilled from open-source decks (see
``references/palette-gallery.md``): 11 light-professional, 8 dark-tech, 3
creative. Each theme pairs with a design *pack* (visual personality, in
``build_presentation.js``) — see ``THEME_DEFAULT_PACK`` there for the mapping.

Each palette has six 6-char hex strings (no leading ``#``, matching
python-pptx's ``RGBColor.from_string``):

  - ``primary``         — main heading / dark accent / cover background
  - ``secondary``       — sub-heading / body emphasis
  - ``accent``          — callouts, highlights, page badges
  - ``light``           — subtle background tint, borders
  - ``bg``              — default slide background
  - ``text_on_primary`` — text color when overlaid on the primary swatch

Full catalog: 24 named themes (2 core + 22 gallery). ``list_palettes()`` /
``ppt-cli list-themes`` print them all.
"""
from __future__ import annotations

from typing import TypedDict


class Palette(TypedDict):
    primary: str
    secondary: str
    accent: str
    light: str
    bg: str
    text_on_primary: str


# ── Curated palettes ────────────────────────────────────────────────────
# Hex values copied from references/palette-gallery.md.
# Mapping: 5-color skill palette → {primary, secondary, accent, light, bg};
# ``text_on_primary`` is white unless the primary is a light tint.

PALETTES: dict[str, Palette] = {
    # Swiss Klein Blue — 默认通用风（白底 + 克莱因蓝单锚点色）
    "swiss_klein": {
        "primary": "0A0A0A", "secondary": "1F1F1F", "accent": "002FA7",
        "light": "E8EAF1", "bg": "F5F5F5", "text_on_primary": "FFFFFF",
    },
    # Navy Gold — 技术风格（深蓝 + 金；配 pack=dark_gold）
    "navy_gold": {
        "primary": "16294D", "secondary": "5B6B8C", "accent": "E6A92E",
        "light": "E7ECF4", "bg": "F4F6FA", "text_on_primary": "FFFFFF",
    },

    # ── A · 浅色专业（从开源 deck 提炼，见 references/palette-gallery.md）──
    "swiss_grid": {
        "primary": "1A1A1A", "secondary": "666666", "accent": "D9251D",
        "light": "E8E8E8", "bg": "FFFFFF", "text_on_primary": "FFFFFF",
    },
    "academic_blue": {
        "primary": "1A202C", "secondary": "4A5568", "accent": "3182CE",
        "light": "E2E8F0", "bg": "F5F7FA", "text_on_primary": "FFFFFF",
    },
    "consulting_navy": {
        "primary": "1A3A5C", "secondary": "5D6D7E", "accent": "E8A838",
        "light": "F0F2F5", "bg": "FFFFFF", "text_on_primary": "FFFFFF",
    },
    "tech_report_light": {
        "primary": "1B3A5C", "secondary": "5B6776", "accent": "E8743B",
        "light": "D8DEE6", "bg": "F7F9FB", "text_on_primary": "FFFFFF",
    },
    "urban_project": {
        "primary": "2B3A4A", "secondary": "5A6B7A", "accent": "C2410C",
        "light": "D6CFC0", "bg": "F5F2EC", "text_on_primary": "FFFFFF",
    },
    "warm_editorial": {
        "primary": "1F1B16", "secondary": "6A6258", "accent": "A44A3F",
        "light": "D8CBB8", "bg": "F6F1E8", "text_on_primary": "FFFFFF",
    },
    "bronze_premium": {
        "primary": "1C1C1C", "secondary": "5C5852", "accent": "B8935A",
        "light": "D4CFC4", "bg": "F5F2EC", "text_on_primary": "FFFFFF",
    },
    "ink_chinese": {
        "primary": "1A1A1A", "secondary": "5C5852", "accent": "A52A2A",
        "light": "C8C0AE", "bg": "F5F1E8", "text_on_primary": "FFFFFF",
    },
    "nature_soft": {
        "primary": "3A3530", "secondary": "7A7068", "accent": "C99E62",
        "light": "EDE5D3", "bg": "F7F2E8", "text_on_primary": "FFFFFF",
    },
    "corp_multi": {
        "primary": "231F20", "secondary": "50798A", "accent": "61A150",
        "light": "E7E6E1", "bg": "FFFFFF", "text_on_primary": "FFFFFF",
    },
    "academic_teal": {
        "primary": "1A1A1A", "secondary": "50798A", "accent": "005C69",
        "light": "E7E6E6", "bg": "FFFFFF", "text_on_primary": "FFFFFF",
    },

    # ── B · 深色科技 / 高级（bg 为深色，引擎自动反白文字）──
    "glass_dashboard": {
        "primary": "1A2150", "secondary": "A8B0D0", "accent": "3DDDFC",
        "light": "E8ECFF", "bg": "0A0E27", "text_on_primary": "FFFFFF",
    },
    "terminal_dark": {
        "primary": "1C2333", "secondary": "8B949E", "accent": "D4A574",
        "light": "E6EDF3", "bg": "161B26", "text_on_primary": "FFFFFF",
    },
    "agent_dark": {
        "primary": "1A1D27", "secondary": "9CA3AF", "accent": "D4845A",
        "light": "E8E8EC", "bg": "12141C", "text_on_primary": "FFFFFF",
    },
    "blueprint": {
        "primary": "0E2A47", "secondary": "A0B8D0", "accent": "FFB627",
        "light": "DCE8F5", "bg": "0C2237", "text_on_primary": "FFFFFF",
    },
    "capital_dark": {
        "primary": "2A2F36", "secondary": "8A857E", "accent": "E63946",
        "light": "E8E6E1", "bg": "0E1116", "text_on_primary": "FFFFFF",
    },
    "editorial_gold": {
        "primary": "1A1A2E", "secondary": "A0A0B0", "accent": "C9A96E",
        "light": "E8E4DC", "bg": "16162A", "text_on_primary": "FFFFFF",
    },
    "luxe_interior": {
        "primary": "1A1714", "secondary": "B0A08E", "accent": "C4A882",
        "light": "E8E0D4", "bg": "14110E", "text_on_primary": "FFFFFF",
    },
    "magazine_black": {
        "primary": "1A1A1A", "secondary": "9E9690", "accent": "C9A96E",
        "light": "E8E4DC", "bg": "0A0A0A", "text_on_primary": "FFFFFF",
    },

    # ── C · 创意 / 小众撞色（默认流程别用）──
    "newsprint_brutal": {
        "primary": "111111", "secondary": "6B6B6B", "accent": "C8102E",
        "light": "CBC8B7", "bg": "F4F1EA", "text_on_primary": "FFFFFF",
    },
    "riso_zine": {
        "primary": "1A1A1A", "secondary": "5A5A5A", "accent": "FF5C8A",
        "light": "F0E6D8", "bg": "F5EFE0", "text_on_primary": "FFFFFF",
    },
    "memphis_pop": {
        "primary": "1A1A2E", "secondary": "5C5C7A", "accent": "FF3DA5",
        "light": "FFE9C7", "bg": "FFF8EE", "text_on_primary": "FFFFFF",
    },
}


# ── Aliases ─────────────────────────────────────────────────────────────
# Human-friendly names (incl. 中文) that resolve to canonical palette keys.

_ALIASES: dict[str, str] = {
    "default": "swiss_klein",
    "克莱因蓝": "swiss_klein",
    "深蓝金": "navy_gold",
    "深色金": "navy_gold",
    # 扩展色板的中文别名（见 references/palette-gallery.md）
    "瑞士网格": "swiss_grid",
    "学术蓝": "academic_blue",
    "咨询蓝": "consulting_navy",
    "浅色科技": "tech_report_light",
    "城市更新": "urban_project",
    "暖色编辑": "warm_editorial",
    "高级灰金": "bronze_premium",
    "中式水墨": "ink_chinese",
    "国风": "ink_chinese",
    "自然草木": "nature_soft",
    "玻璃拟态": "glass_dashboard",
    "暗夜终端": "terminal_dark",
    "深色科技卡": "agent_dark",
    "蓝图": "blueprint",
    "深色财经": "capital_dark",
    "深色金编辑": "editorial_gold",
    "深色奢华": "luxe_interior",
    "黑底杂志": "magazine_black",
    "报纸": "newsprint_brutal",
    "野兽派": "newsprint_brutal",
    "孟菲斯": "memphis_pop",
}


# ── Tonal helpers (60-30-10 depth; mirrored in build_presentation.js) ────
# Same-hue lightness variation is the consultant-grade alternative to
# introducing extra colors for data series / structural fills.


def _clamp8(v: float) -> int:
    return max(0, min(255, int(round(v))))


def tint(hex_str: str, pct: float) -> str:
    """Lighten a hex color toward white by ``pct`` (0.0–1.0). No leading ``#``."""
    h = hex_str.lstrip("#")
    if len(h) != 6:
        return hex_str.lstrip("#").upper()
    r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
    p = max(0.0, min(1.0, pct))
    return "".join(
        f"{_clamp8(c + (255 - c) * p):02X}" for c in (r, g, b)
    )


def shade(hex_str: str, pct: float) -> str:
    """Darken a hex color toward black by ``pct`` (0.0–1.0). No leading ``#``."""
    h = hex_str.lstrip("#")
    if len(h) != 6:
        return hex_str.lstrip("#").upper()
    r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
    p = max(0.0, min(1.0, pct))
    return "".join(f"{_clamp8(c * (1.0 - p)):02X}" for c in (r, g, b))


def get_palette(name: str | None) -> Palette:
    """Resolve a palette name (or alias) to a Palette dict; falls back to ``swiss_klein``."""
    if not name:
        return PALETTES["swiss_klein"]
    key = _ALIASES.get(name, name)
    return PALETTES.get(key, PALETTES["swiss_klein"])


def list_palettes() -> list[str]:
    """Public palette names (canonical only, aliases excluded)."""
    return list(PALETTES.keys())


def list_aliases() -> dict[str, str]:
    """Alias name → canonical palette name."""
    return dict(_ALIASES)
