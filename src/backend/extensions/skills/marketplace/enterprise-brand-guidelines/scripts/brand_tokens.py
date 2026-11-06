#!/usr/bin/env python3
"""把企业视觉规范 brand.json 校验后转换成其他设计技能能直接用的格式。

做三件模型容易算错的事：
  1. 校验：色值格式、必填项；
  2. 对比度：按 WCAG 2.x 计算每个品牌色作为文字放在底色上的对比度，
     不达 4.5:1 的，沿同一色相加深到刚好达标，给出“作文字用”的替代色（品牌原色仍可用于大色块）；
  3. 导出：tokens.css（CSS 变量）、tokens.json、theme-factory 主题文件、diagram-design 配色表。

用法：
    python brand_tokens.py brand.json -o brand-tokens/
"""
from __future__ import annotations

import argparse
import colorsys
import json
import re
import sys
from pathlib import Path

HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
REQUIRED_COLORS = ["primary", "ink", "paper"]
TEXT_MIN = 4.5  # 正文文字的 WCAG AA 门槛
MARK_MIN = 3.0  # 线条、图标、大字号文字的门槛


def luminance(hex_color: str) -> float:
    def channel(c: int) -> float:
        s = c / 255
        return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast(a: str, b: str) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return round((la + 0.05) / (lb + 0.05), 2)


def to_hex(r: float, g: float, b: float) -> str:
    return "#" + "".join(f"{round(max(0.0, min(1.0, v)) * 255):02x}" for v in (r, g, b))


def accessible_variant(color: str, background: str, target: float = TEXT_MIN) -> str:
    """沿同一色相、同一饱和度调整明度，直到对 background 的对比度达到 target。

    底色是浅色就往暗调，底色是深色就往亮调；返回最接近原色的达标颜色。
    """
    if contrast(color, background) >= target:
        return color
    r, g, b = (int(color[i:i + 2], 16) / 255 for i in (1, 3, 5))
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    darken = luminance(background) > 0.18
    step = -0.005 if darken else 0.005
    while 0.0 <= l <= 1.0:
        l += step
        cand = to_hex(*colorsys.hls_to_rgb(h, max(0.0, min(1.0, l)), s))
        if contrast(cand, background) >= target:
            return cand
    return "#000000" if darken else "#ffffff"


def validate(spec: dict) -> list[str]:
    errors = []
    if not spec.get("name"):
        errors.append("缺少 name（企业或品牌名称）")
    colors = spec.get("colors") or {}
    for key in REQUIRED_COLORS:
        if key not in colors:
            errors.append(f"colors 缺少必填项 {key}")
    for key, value in colors.items():
        if not isinstance(value, str) or not HEX_RE.match(value):
            errors.append(f"colors.{key} 应为 #RRGGBB 格式，收到：{value}")
    return errors


def analyse(colors: dict[str, str]) -> list[dict]:
    paper, ink = colors["paper"], colors["ink"]
    rows = []
    for key, value in colors.items():
        if key == "paper":
            continue
        ratio = contrast(value, paper)
        rows.append({
            "role": key, "color": value, "contrast_on_paper": ratio,
            "ok_as_text": ratio >= TEXT_MIN, "ok_as_mark": ratio >= MARK_MIN,
            "text_variant": accessible_variant(value, paper),
            # 品牌色做底时，上面的字用白色还是墨色，取对比度更高的那个
            "text_on_fill": "#ffffff" if contrast("#ffffff", value) >= contrast(ink, value) else ink,
        })
    return rows


GENERIC_FAMILIES = {"serif", "sans-serif", "monospace", "cursive", "fantasy", "system-ui",
                    "ui-serif", "ui-sans-serif", "ui-monospace", "math", "emoji"}


def css_family(name: str) -> str:
    """通用字体族不能加引号，具体字体名一律加引号（带空格、中文名都安全）。"""
    name = name.strip().strip("'\"")
    return name if name in GENERIC_FAMILIES else f"'{name}'"


def css(spec: dict, rows: list[dict]) -> str:
    fonts = spec.get("fonts") or {}
    lines = [f"/* {spec['name']} 视觉规范 · 由 brand_tokens.py 生成，请改 brand.json 后重新生成 */", ":root {"]
    lines.append(f"  --brand-paper: {spec['colors']['paper']};")
    for r in rows:
        lines.append(f"  --brand-{r['role']}: {r['color']};")
        if r["text_variant"] != r["color"]:
            lines.append(f"  --brand-{r['role']}-text: {r['text_variant']};  /* 作为文字时使用 */")
        lines.append(f"  --brand-on-{r['role']}: {r['text_on_fill']};")
    for key, stack in (("heading", fonts.get("heading")), ("body", fonts.get("body")), ("mono", fonts.get("mono"))):
        if stack:
            lines.append(f"  --brand-font-{key}: {', '.join(css_family(f) for f in stack)};")
    lines.append("}")
    return "\n".join(lines) + "\n"


def theme_md(spec: dict, rows: list[dict]) -> str:
    """theme-factory 的主题文件格式（标题、配色清单、字体、适用场景）。"""
    fonts = spec.get("fonts") or {}
    out = [f"# {spec['name']}", "", spec.get("tagline") or f"{spec['name']} 企业视觉规范主题。", "", "## Color Palette", ""]
    out.append(f"- **Paper**: `{spec['colors']['paper']}` - 页面与幻灯片底色")
    for r in rows:
        use = "可直接作文字" if r["ok_as_text"] else f"作文字时用 `{r['text_variant']}`"
        out.append(f"- **{r['role'].title()}**: `{r['color']}` - {use}；做底色时上面的字用 `{r['text_on_fill']}`")
    out += ["", "## Typography", ""]
    if fonts.get("heading"):
        out.append(f"- **Headers**: {fonts['heading'][0]}")
    if fonts.get("body"):
        out.append(f"- **Body Text**: {fonts['body'][0]}")
    out += ["", "## Best Used For", "", f"{spec['name']} 对内对外的所有正式物料。", ""]
    return "\n".join(out)


def diagram_roles(spec: dict, rows: list[dict]) -> str:
    """diagram-design 的语义角色映射：只映射它真正需要的几个角色，其余沿用默认。"""
    c = spec["colors"]
    by_role = {r["role"]: r for r in rows}
    accent = by_role.get("accent") or by_role["primary"]
    link = by_role.get("secondary") or by_role["primary"]
    muted = by_role.get("muted")
    table = [
        ("paper", c["paper"]), ("ink", c["ink"]),
        ("muted", muted["text_variant"] if muted else None),
        ("accent", accent["text_variant"]), ("link", link["text_variant"]),
    ]
    out = [f"# {spec['name']} · diagram-design 配色映射", "",
           "按 diagram-design 的 `references/profiles.md` 保存为配置文件时，用下表替换 Semantic roles 中对应行；"
           "表中的颜色已经保证作文字时对比度不低于 4.5:1。", "",
           "| Role | Light |", "|---|---|"]
    out += [f"| `{role}` | `{value}` |" for role, value in table if value]
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="企业视觉规范转设计令牌")
    ap.add_argument("spec", help="brand.json")
    ap.add_argument("-o", "--output-dir", default="brand-tokens")
    args = ap.parse_args()

    try:
        spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"读取 {args.spec} 失败：{exc}", file=sys.stderr)
        return 2
    errors = validate(spec)
    if errors:
        print(json.dumps({"errors": errors}, ensure_ascii=False, indent=2))
        return 2
    rows = analyse(spec["colors"])
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "tokens.css").write_text(css(spec, rows), encoding="utf-8")
    (out / "tokens.json").write_text(json.dumps({"name": spec["name"], "colors": rows,
                                                 "fonts": spec.get("fonts") or {}},
                                                ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "theme.md").write_text(theme_md(spec, rows), encoding="utf-8")
    (out / "diagram-roles.md").write_text(diagram_roles(spec, rows), encoding="utf-8")
    ink_ratio = contrast(spec["colors"]["ink"], spec["colors"]["paper"])
    print(json.dumps({
        "brand": spec["name"], "output_dir": str(out),
        "ink_on_paper": ink_ratio,
        "warnings": ([f"正文色 ink 与底色对比度只有 {ink_ratio}，低于 4.5:1，正文会不清楚"] if ink_ratio < TEXT_MIN else [])
        + [f"{r['role']} {r['color']} 作文字对比度 {r['contrast_on_paper']}，作文字时改用 {r['text_variant']}"
           for r in rows if not r["ok_as_text"]],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
