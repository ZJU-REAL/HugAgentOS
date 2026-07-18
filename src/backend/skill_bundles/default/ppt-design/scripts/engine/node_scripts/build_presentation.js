#!/usr/bin/env node
/**
 * pptxgenjs-based presentation builder used by the `mcp` container's
 * pptxgenjs engine (see `office_lib/pptx/pptxgenjs_engine.py`).
 *
 * Visually aligned with the user's `pptx` skill design rules:
 *   - NO accent line under titles (skill calls these out as "AI-generated")
 *   - Sandwich structure: dark cover + light content + dark summary
 *   - One visual motif (accent dot) carried across cover/section/summary
 *   - icon_rows / stat_callout / grid / timeline content sub-layouts
 *   - Subtle shadows on summary card and highlight cards
 *   - Italic accent text for taglines / stat captions
 *
 * Spec schema (stdin JSON):
 *   {
 *     title?, author?, subject?,
 *     theme?: <name string> | { primary, secondary, accent, light, bg },
 *     fonts?: { header?, body? },                 // override default fonts
 *     slides: [{ type: "cover"|"toc"|"section"|"content"|"summary", ... }]
 *   }
 *
 * Slide spec details:
 *   cover:    { title, subtitle?, tagline?, body?, cover_style? "dark"|"light", image? }
 *   toc:      { title?, items: [...] }
 *   section:  { title, subtitle?, half_bleed? = true, image? }
 *   content:  { title, layout?, bullets?, body?,
 *               leftTitle?/rightTitle?/leftBullets?/rightBullets?,   // two_col
 *               highlights?: [<=3],                                  // highlights
 *               items?: [{glyph,title,desc} | {title,desc}],         // icon_rows | grid
 *               stats?: [{value,label,tagline?}],                    // stat_callout
 *               steps?: [{step,title,desc}],                         // timeline
 *               bars?: [{value,label,percentage?,desc?}],            // bar_chart_kpi (≤4) / horizontal_bars (>4)
 *               rings?: [{title,items?}],                            // concentric (3 entries, inner→outer)
 *               ledger?: [{value,label,desc?}],                      // kpi_ledger (4-6)
 *               image? }
 *   summary:  { title?, bullets?, body?, summary_style? "dark"|"light", image? }
 *
 *   Any slide's image field:
 *     { data_base64, mime?,
 *       slot? "right"|"left"|"below_title"|"hero"|"full",            // semantic slot
 *       x?, y?, w?, h?,                                              // explicit inches
 *       caption? }                                                    // small italic caption
 *
 * Output: --out <path>; emits a single JSON line on stdout.
 */

const fs = require("fs");
const path = require("path");
// Vendored Tabler Icons (MIT) — concept→vector path data. See
// ./assets/tabler-outline/LICENSE. Resolved per-icon at render time.
const TABLER = require("./tabler_icons.js");
const { gradientPng } = require("./gradient.js");

function loadPptxGenJS() {
  try {
    return require("pptxgenjs");
  } catch (_) {}
  try {
    const root = require("child_process")
      .execSync("npm root -g", { stdio: ["ignore", "pipe", "ignore"] })
      .toString()
      .trim();
    return require(path.join(root, "pptxgenjs"));
  } catch (err) {
    console.error(JSON.stringify({
      status: "error",
      error: `pptxgenjs not found: ${String(err)}`,
      hint: "Install with: npm install -g pptxgenjs",
    }));
    process.exit(2);
  }
}

// ── Palettes (mirror office_lib/pptx/themes.py) ──────────────────────

const PALETTES = {
  // 默认通用风 — 白底 + 克莱因蓝单锚点色
  swiss_klein:          { primary: "0A0A0A", secondary: "1F1F1F", accent: "002FA7", light: "E8EAF1", bg: "F5F5F5", text_on_primary: "FFFFFF" },
  // 技术风格 — 深蓝 + 金（配 pack=dark_gold）
  navy_gold:            { primary: "16294D", secondary: "5B6B8C", accent: "E6A92E", light: "E7ECF4", bg: "F4F6FA", text_on_primary: "FFFFFF" },

  // ── A · 浅色专业（见 references/palette-gallery.md；与 themes.py 同步）──
  swiss_grid:           { primary: "1A1A1A", secondary: "666666", accent: "D9251D", light: "E8E8E8", bg: "FFFFFF", text_on_primary: "FFFFFF" },
  academic_blue:        { primary: "1A202C", secondary: "4A5568", accent: "3182CE", light: "E2E8F0", bg: "F5F7FA", text_on_primary: "FFFFFF" },
  consulting_navy:      { primary: "1A3A5C", secondary: "5D6D7E", accent: "E8A838", light: "F0F2F5", bg: "FFFFFF", text_on_primary: "FFFFFF" },
  tech_report_light:    { primary: "1B3A5C", secondary: "5B6776", accent: "E8743B", light: "D8DEE6", bg: "F7F9FB", text_on_primary: "FFFFFF" },
  urban_project:        { primary: "2B3A4A", secondary: "5A6B7A", accent: "C2410C", light: "D6CFC0", bg: "F5F2EC", text_on_primary: "FFFFFF" },
  warm_editorial:       { primary: "1F1B16", secondary: "6A6258", accent: "A44A3F", light: "D8CBB8", bg: "F6F1E8", text_on_primary: "FFFFFF" },
  bronze_premium:       { primary: "1C1C1C", secondary: "5C5852", accent: "B8935A", light: "D4CFC4", bg: "F5F2EC", text_on_primary: "FFFFFF" },
  ink_chinese:          { primary: "1A1A1A", secondary: "5C5852", accent: "A52A2A", light: "C8C0AE", bg: "F5F1E8", text_on_primary: "FFFFFF" },
  nature_soft:          { primary: "3A3530", secondary: "7A7068", accent: "C99E62", light: "EDE5D3", bg: "F7F2E8", text_on_primary: "FFFFFF" },
  corp_multi:           { primary: "231F20", secondary: "50798A", accent: "61A150", light: "E7E6E1", bg: "FFFFFF", text_on_primary: "FFFFFF" },
  academic_teal:        { primary: "1A1A1A", secondary: "50798A", accent: "005C69", light: "E7E6E6", bg: "FFFFFF", text_on_primary: "FFFFFF" },

  // ── B · 深色科技 / 高级（bg 深色，引擎自动反白）──
  glass_dashboard:      { primary: "1A2150", secondary: "A8B0D0", accent: "3DDDFC", light: "E8ECFF", bg: "0A0E27", text_on_primary: "FFFFFF" },
  terminal_dark:        { primary: "1C2333", secondary: "8B949E", accent: "D4A574", light: "E6EDF3", bg: "161B26", text_on_primary: "FFFFFF" },
  agent_dark:           { primary: "1A1D27", secondary: "9CA3AF", accent: "D4845A", light: "E8E8EC", bg: "12141C", text_on_primary: "FFFFFF" },
  blueprint:            { primary: "0E2A47", secondary: "A0B8D0", accent: "FFB627", light: "DCE8F5", bg: "0C2237", text_on_primary: "FFFFFF" },
  capital_dark:         { primary: "2A2F36", secondary: "8A857E", accent: "E63946", light: "E8E6E1", bg: "0E1116", text_on_primary: "FFFFFF" },
  editorial_gold:       { primary: "1A1A2E", secondary: "A0A0B0", accent: "C9A96E", light: "E8E4DC", bg: "16162A", text_on_primary: "FFFFFF" },
  luxe_interior:        { primary: "1A1714", secondary: "B0A08E", accent: "C4A882", light: "E8E0D4", bg: "14110E", text_on_primary: "FFFFFF" },
  magazine_black:       { primary: "1A1A1A", secondary: "9E9690", accent: "C9A96E", light: "E8E4DC", bg: "0A0A0A", text_on_primary: "FFFFFF" },

  // ── C · 创意 / 小众撞色 ──
  newsprint_brutal:     { primary: "111111", secondary: "6B6B6B", accent: "C8102E", light: "CBC8B7", bg: "F4F1EA", text_on_primary: "FFFFFF" },
  riso_zine:            { primary: "1A1A1A", secondary: "5A5A5A", accent: "FF5C8A", light: "F0E6D8", bg: "F5EFE0", text_on_primary: "FFFFFF" },
  memphis_pop:          { primary: "1A1A2E", secondary: "5C5C7A", accent: "FF3DA5", light: "FFE9C7", bg: "FFF8EE", text_on_primary: "FFFFFF" },
};

// Each theme's default design pack (visual personality). spec.pack overrides.
// Packs are implemented further down; unmapped/unknown → "default".
const THEME_DEFAULT_PACK = {
  navy_gold: "dark_gold",
  swiss_grid: "swiss", academic_blue: "swiss", corp_multi: "swiss", academic_teal: "swiss",
  consulting_navy: "default", tech_report_light: "default",
  urban_project: "editorial", warm_editorial: "editorial", bronze_premium: "editorial",
  nature_soft: "editorial",
  ink_chinese: "ink",
  glass_dashboard: "glass",
  terminal_dark: "dark_gold", agent_dark: "dark_gold", capital_dark: "dark_gold",
  editorial_gold: "editorial", luxe_interior: "editorial", magazine_black: "editorial",
  blueprint: "blueprint",
  newsprint_brutal: "newsprint",
  memphis_pop: "memphis", riso_zine: "memphis",
};

const ALIASES = {
  default: "swiss_klein",
  "克莱因蓝": "swiss_klein",
  "深蓝金": "navy_gold",
  "深色金": "navy_gold",
  "瑞士网格": "swiss_grid", "学术蓝": "academic_blue", "咨询蓝": "consulting_navy",
  "浅色科技": "tech_report_light", "城市更新": "urban_project", "暖色编辑": "warm_editorial",
  "高级灰金": "bronze_premium", "中式水墨": "ink_chinese", "国风": "ink_chinese",
  "自然草木": "nature_soft", "玻璃拟态": "glass_dashboard", "暗夜终端": "terminal_dark",
  "深色科技卡": "agent_dark", "蓝图": "blueprint", "深色财经": "capital_dark",
  "深色金编辑": "editorial_gold", "深色奢华": "luxe_interior", "黑底杂志": "magazine_black",
  "报纸": "newsprint_brutal", "野兽派": "newsprint_brutal", "孟菲斯": "memphis_pop",
};

function normalizeHex(value, fallback) {
  const raw = String(value || fallback || "").replace(/^#/, "").trim();
  return /^[0-9A-Fa-f]{6}$/.test(raw) ? raw.toUpperCase() : fallback;
}

function resolveTheme(themeSpec) {
  if (!themeSpec) return { ...PALETTES.swiss_klein };
  if (typeof themeSpec === "string") {
    const key = ALIASES[themeSpec] || themeSpec;
    return { ...(PALETTES[key] || PALETTES.swiss_klein) };
  }
  return {
    primary: normalizeHex(themeSpec.primary, "0A0A0A"),
    secondary: normalizeHex(themeSpec.secondary, "1F1F1F"),
    accent: normalizeHex(themeSpec.accent, "002FA7"),
    light: normalizeHex(themeSpec.light, "E8EAF1"),
    bg: normalizeHex(themeSpec.bg, "F5F5F5"),
    text_on_primary: normalizeHex(themeSpec.text_on_primary, "FFFFFF"),
  };
}

// ── Fonts ────────────────────────────────────────────────────────────

const DEFAULT_FONTS = { header: "Microsoft YaHei", body: "Microsoft YaHei" };

function resolveFonts(fontsSpec) {
  if (!fontsSpec || typeof fontsSpec !== "object") return { ...DEFAULT_FONTS };
  return {
    header: fontsSpec.header || DEFAULT_FONTS.header,
    body: fontsSpec.body || DEFAULT_FONTS.body,
  };
}

// ── WP0 shared toolkit (grid / type scale / tiered shadow / tints / style) ──
// Pure helpers — no pptxgenjs calls here. Mirrors office_lib/pptx/{themes,
// typography,style_recipes}.py so the Node engine and the python-pptx
// fallback stay visually consistent.

// Unified canvas grid (16:9 = 10" × 5.625"). Replaces scattered magic
// coordinates so every layout shares the same margins / content box.
const GRID = {
  MARGIN_X: 0.72,
  TITLE_Y: 0.42,
  TITLE_H: 0.70,
  CONTENT_TOP: 1.45,
  CONTENT_BOTTOM: 4.85,
  CONTENT_W: 8.56,         // 10 - 2*MARGIN_X
  GUTTER: 0.22,
  get CONTENT_RIGHT() { return 10 - this.MARGIN_X; },
};

function clamp8(v) { return Math.max(0, Math.min(255, Math.round(v))); }

function tintJS(hex, pct) {
  const h = String(hex || "").replace(/^#/, "");
  if (!/^[0-9A-Fa-f]{6}$/.test(h)) return h.toUpperCase();
  const p = Math.max(0, Math.min(1, pct));
  return [0, 2, 4]
    .map(i => {
      const c = parseInt(h.slice(i, i + 2), 16);
      return clamp8(c + (255 - c) * p).toString(16).padStart(2, "0");
    })
    .join("")
    .toUpperCase();
}

function shadeJS(hex, pct) {
  const h = String(hex || "").replace(/^#/, "");
  if (!/^[0-9A-Fa-f]{6}$/.test(h)) return h.toUpperCase();
  const p = Math.max(0, Math.min(1, pct));
  return [0, 2, 4]
    .map(i => {
      const c = parseInt(h.slice(i, i + 2), 16);
      return clamp8(c * (1 - p)).toString(16).padStart(2, "0");
    })
    .join("")
    .toUpperCase();
}

// Fixed secondary accents for "multi-color card wall" layouts (kpi_cards /
// card_list / icon_cards under dark_gold). Used as `[theme.accent, ...MULTI_ACCENTS]`
// so card 0 keeps the theme accent and the rest cycle through distinct hues.
const MULTI_ACCENTS = ["2E75B6", "2E9E5B", "D7493A", "8A5CD6", "0E9BA6"];

// ── Data color ramp (WP8) ────────────────────────────────────────────────
// The old per-bar fill `tintJS(accent, 0.42 + (i%3)*0.16)` produced tints up
// to 0.74/0.82 — secondary bars & outer rings washed out to near-white and
// read as "unfinished". A data series must stay clearly the accent HUE.
//
// dataRamp() returns one color per value, ranked by magnitude: the largest
// value gets full accent, the rest step toward white but NEVER past `maxTint`
// (default 0.46 → lowest item is still ≥54% saturated). Ranking by value (not
// spec order) means "darker = bigger" reads instantly.
function dataRamp(accentHex, values, opts = {}) {
  const maxTint = opts.maxTint != null ? opts.maxTint : 0.46;
  const n = values.length;
  if (n <= 1) return values.map(() => accentHex);
  // rank: 0 = largest value → full accent; n-1 = smallest → maxTint
  const order = values
    .map((v, i) => ({ v: Number(v) || 0, i }))
    .sort((a, b) => b.v - a.v);
  const rankOf = new Array(n);
  order.forEach((o, rank) => { rankOf[o.i] = rank; });
  return values.map((_, i) => {
    const t = (rankOf[i] / (n - 1)) * maxTint;
    return t < 0.02 ? accentHex.replace(/^#/, "").toUpperCase() : tintJS(accentHex, t);
  });
}

// A fixed n-step depth ladder for ordinal layers (concentric / pyramid /
// funnel) where item 0 is the focal "core". Evenly spaced, capped so the
// lightest step stays distinguishable from a white/light background.
function depthLadder(accentHex, n, maxTint = 0.46) {
  if (n <= 1) return [accentHex.replace(/^#/, "").toUpperCase()];
  return Array.from({ length: n }, (_, i) =>
    i === 0 ? accentHex.replace(/^#/, "").toUpperCase() : tintJS(accentHex, (i / (n - 1)) * maxTint));
}

// Density-anchored modular type scale (pt). Adjacent roles keep >=1.25x gap.
const _TYPE_BASELINE = { relaxed: 17, dense: 14 };
const _TYPE_RATIOS = {
  kicker: 0.62, caption: 0.78, support: 0.85, body: 1.0,
  subtitle: 1.35, title: 1.7, large_title: 3.0, hero: 5.0,
};
function typeScale(density, titleRatio) {
  const base = _TYPE_BASELINE[density] || _TYPE_BASELINE.dense;
  const out = {};
  for (const [role, r] of Object.entries(_TYPE_RATIOS)) {
    const ratio = role === "title" && titleRatio ? titleRatio : r;
    out[role] = Math.max(9, Math.round(base * ratio));
  }
  return out;
}

// True when a color is essentially grey/black/white (no real hue) — used to
// detect Swiss-style palettes whose primary is near-black.
function isAchromatic(hex) {
  const h = String(hex || "").replace(/^#/, "");
  if (!/^[0-9A-Fa-f]{6}$/.test(h)) return true;
  const r = parseInt(h.slice(0, 2), 16);
  const g = parseInt(h.slice(2, 4), 16);
  const b = parseInt(h.slice(4, 6), 16);
  return (Math.max(r, g, b) - Math.min(r, g, b)) <= 22;
}

// The slide-filling "brand" color for section dividers / dark cover / dark
// summary. Priority:
//  1. If `primary` carries the theme's hue (most palettes) → use primary.
//  2. If `primary` is achromatic (near-black) BUT `bg` is itself a dark, hued
//     color (a dark hued bg, e.g. #001026 navy) → the brand identity lives in bg;
//     use it so a dark-navy theme gets a navy section, not a darkened amber
//     section.
//  3. Otherwise (Swiss-style: black primary on white bg) → the hue lives in
//     the accent. Darken bright accents (lemon/gold) so they still host
//     white text without glare.
function brandBg(theme) {
  if (!isAchromatic(theme.primary)) return theme.primary;
  if (isDarkHex(theme.bg) && !isAchromatic(theme.bg)) return theme.bg;
  const a = theme.accent;
  return isDarkHex(a) ? a : shadeJS(a, 0.5);
}

// WP9: subtle tonal-gradient background for the "anchor" slides (cover /
// section / dark summary). A flat fill reads as cheap; a *restrained* diagonal
// gradient adds depth without the gaudy "AI gradient" look. Gradients are
// applied ONLY to anchor slides (never content) and stay within a few % of
// luminance for light bgs. Returns a pptxgenjs background object.
//   variant: "light" (white/cream bg) | "dark" (brand-colored fill)
// Set GRADIENT_BG=false (spec.bg_gradient:false) to fall back to flat fills.
function anchorBackground(theme, variant, baseColor) {
  if (!GRADIENT_BG) return { color: baseColor };
  if (variant === "dark") {
    // brand fill → slightly deeper toward the bottom-right for dimensional depth
    return {
      data: gradientPng({
        stops: [
          { pos: 0, color: tintJS(baseColor, 0.06) },
          { pos: 1, color: shadeJS(baseColor, 0.30) },
        ],
        angleDeg: 125, grain: 2,
      }),
    };
  }
  // light: a faint accent wash in one corner melting into the bg — keeps the
  // page bright (≈4-8% luminance shift) but no longer flat.
  return {
    data: gradientPng({
      stops: [
        { pos: 0, color: tintJS(theme.accent, 0.93) },
        { pos: 0.55, color: baseColor },
        { pos: 1, color: baseColor },
      ],
      angleDeg: 135, grain: 2,
    }),
  };
}

// Tiered shadows — single 135° light source. Premium look = restraint.
function noShadow() { return undefined; }
function restingShadow() {
  return { type: "outer", color: "000000", blur: 6, offset: 3, angle: 135, opacity: 0.10 };
}
function raisedShadow() {
  return { type: "outer", color: "000000", blur: 13, offset: 7, angle: 135, opacity: 0.16 };
}

// Per-slide shadow budget — never let a slide get muddy with depth.
function makeShadowBudget(max = 3) {
  let used = 0;
  return {
    take(fn) {
      if (used >= max || typeof fn !== "function") return undefined;
      const s = fn();
      if (s) used += 1;
      return s;
    },
  };
}

// StyleRecipe mirror (office_lib/pptx/style_recipes.py). resolveStyle() turns
// the spec.style string into the visual variables every renderer reads.
const STYLE_RECIPES = {
  sharp:   { radius: 0.0,  shadow: "none",    density: "dense",   titleRatio: 1.45, accentBudget: 1, iconStyle: "line"    },
  soft:    { radius: 0.08, shadow: "resting", density: "dense",   titleRatio: 1.6,  accentBudget: 2, iconStyle: "duotone" },
  rounded: { radius: 0.16, shadow: "resting", density: "relaxed", titleRatio: 1.75, accentBudget: 2, iconStyle: "filled"  },
  pill:    { radius: 0.30, shadow: "raised",  density: "relaxed", titleRatio: 1.9,  accentBudget: 2, iconStyle: "filled"  },
};

function resolveStyle(name) {
  const r = STYLE_RECIPES[String(name || "soft").toLowerCase()] || STYLE_RECIPES.soft;
  const shadowFn =
    r.shadow === "none" ? noShadow :
    r.shadow === "raised" ? raisedShadow : restingShadow;
  return {
    name: STYLE_RECIPES[String(name || "soft").toLowerCase()] ? String(name).toLowerCase() : "soft",
    radius: r.radius,
    cardShadow: shadowFn,
    floatShadow: r.shadow === "none" ? noShadow : raisedShadow,
    type: typeScale(r.density, r.titleRatio),
    density: r.density,
    accentBudget: r.accentBudget,
    iconStyle: r.iconStyle,
  };
}

// ── Args / IO ────────────────────────────────────────────────────────

function parseArgs(argv) {
  const args = { out: null, input: null };
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === "--out" && argv[i + 1]) args.out = argv[++i];
    else if (argv[i] === "--input" && argv[i + 1]) args.input = argv[++i];
  }
  return args;
}

async function readStdin() {
  return await new Promise((resolve, reject) => {
    let data = "";
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", chunk => { data += chunk; });
    process.stdin.on("end", () => resolve(data));
    process.stdin.on("error", reject);
  });
}

// ── Helpers (NOTE: each `addShape` call gets a FRESH options object — pptxgenjs mutates) ──

// WP3: budgeted, tiered shadows. cardShadow = resting (grid/highlight cards),
// floatShadow = raised (the one elevated element, e.g. summary card).
// Returns undefined when the per-slide budget is spent or style=sharp.
function cardShadow() { return SHADOW_BUDGET.take(STYLE.cardShadow); }
function floatShadow() { return SHADOW_BUDGET.take(STYLE.floatShadow); }

// WP1: at radius≈0 use a true RECTANGLE so `sharp` is genuinely sharp —
// pptxgenjs ROUNDED_RECTANGLE still renders soft corners at rectRadius:0
// under LibreOffice, which muddied the style differentiation.
function cardShapeType(pres) {
  return STYLE.radius <= 0.02 ? pres.shapes.RECTANGLE : pres.shapes.ROUNDED_RECTANGLE;
}

// WP3/WP4 small helper: only attach a shadow key when one was granted
// (pptxgenjs treats shadow:undefined fine, but keeps option objects clean).
function withShadow(opts, shadow) {
  if (shadow) opts.shadow = shadow;
  return opts;
}

function addBadge(pres, slide, theme, index, opts = {}) {
  // WP-OC: footer chrome — a thin full-width hairline rule pinned near the
  // bottom + a small muted page number at the right (reverse-engineered from
  // the OpenClaw deck). Replaces the old gray rounded pill, which read as a
  // dated UI chip and looked identical across every theme. The hairline is a
  // structural element: neutral, never the page accent.
  //   onDark — forced true by callers drawing a dark section/cover on an
  //   otherwise-light theme; otherwise inferred from the theme bg so dark-bg
  //   themes (glass / terminal / …) flip the number to a light tint.
  const onDark = opts.onDark != null ? opts.onDark : isDarkHex(theme.bg);
  const ruleColor = onDark ? tintJS(theme.bg, 0.20) : shadeJS(theme.bg, 0.12);
  const numColor = opts.textColor ||
    (onDark ? tintJS(theme.bg, 0.62) : tintJS(theme.secondary, 0.32));
  slide.addShape(pres.shapes.RECTANGLE, {
    x: GRID.MARGIN_X, y: 5.17, w: GRID.CONTENT_W, h: 0.012,
    fill: { color: ruleColor },
    line: { color: ruleColor },
  });
  slide.addText(String(index).padStart(2, "0"), {
    x: GRID.CONTENT_RIGHT - 0.9, y: 5.19, w: 0.9, h: 0.30,
    fontFace: "Arial",
    fontSize: 9,
    color: numColor,
    align: "right",
    valign: "mid",
    margin: 0,
    charSpacing: 1,
  });
}

function addAccentDot(pres, slide, x, y, diameter, color) {
  slide.addShape(pres.shapes.OVAL, {
    x, y, w: diameter, h: diameter,
    fill: { color },
    line: { color },
  });
}

// WP7: curated glyph set — clean geometric marks that read as "designed"
// rather than the old unicode-in-a-flat-circle. Mapped from common concept
// names so spec authors can pass semantic icons (it.icon = "chart" etc.).
const ICON_GLYPHS = {
  chart: "◔", data: "▦", users: "‹›", team: "‹›", gear: "✦", settings: "✦",
  shield: "◈", security: "◈", rocket: "◭", growth: "◹", grow: "◹",
  check: "✓", done: "✓", target: "◎", goal: "◎", clock: "◷", time: "◷",
  idea: "✷", insight: "✷", link: "∞", network: "∞", doc: "▤", report: "▤",
  flag: "◤", milestone: "◤", star: "★", money: "¥", finance: "¥",
  globe: "◯", world: "◯", lock: "▢", arrow: "→", up: "↑",
};

function _glyphFor(name, glyph) {
  const key = String(name || "").toLowerCase().trim();
  if (ICON_GLYPHS[key]) return ICON_GLYPHS[key];
  const g = String(glyph || "").trim();
  return g ? g.slice(0, 2) : "◆";
}

// Build a recolored Tabler-outline SVG (viewBox 24) as a base64 data URI.
function _tablerDataUri(paths, strokeHex) {
  const body = paths
    .map(d => `<path d="${d}"/>`)
    .join("");
  const svg =
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" ` +
    `fill="none" stroke="#${String(strokeHex).replace(/^#/, "")}" ` +
    `stroke-width="2" stroke-linecap="round" stroke-linejoin="round">` +
    body + `</svg>`;
  return "data:image/svg+xml;base64," + Buffer.from(svg, "utf8").toString("base64");
}

// Infer an icon concept from free CN/EN text (item title/desc) so specs
// that only pass a generic glyph ("◆") or no icon still get a real vector.
// Ordered: first keyword hit wins → concept token TABLER.iconPaths resolves.
const _ICON_KEYWORDS = [
  [/算力|算法|模型|芯片|计算|服务器|硬件|大模型|gpu|cpu/i, "cpu"],
  [/人工智能|智能体|\bai\b|机器学习|深度学习/i, "ai"],
  [/安全|防护|可信|保密|风险|防御|加密|信创/i, "security"],
  [/数据|数智|信息化|数字化|数据要素/i, "data"],
  [/政策|制度|法规|治理|合规|监管|标准|规范/i, "policy"],
  [/融资|投资|资金|资本|金融|经费|预算|营收|成本|收入/i, "finance"],
  [/专利|知识产权|成果|获奖|认证|荣誉/i, "award"],
  [/企业|公司|机构|单位|集团|产业园|基地/i, "building"],
  [/区域|城市|地区|分布|布局|省份|地图|空间/i, "map"],
  [/趋势|增长|提升|增速|发展|攀升|上升/i, "growth"],
  [/人才|团队|人员|队伍|专家|组织/i, "team"],
  [/绿色|低碳|环保|可持续|生态|碳/i, "eco"],
  [/能源|电力|算电|双碳/i, "bolt"],
  [/云|平台|saas|底座|基础设施|设施/i, "cloud"],
  [/机器人|自动化|智能装备|无人/i, "robot"],
  [/医疗|健康|医药|生物/i, "health"],
  [/网络|连接|生态|集成|协同|链路|通信/i, "link"],
  [/创新|研发|创意|想法|洞察/i, "idea"],
  [/目标|指标|绩效|kpi|考核/i, "target"],
  [/时间|周期|阶段|进度|时序|节点/i, "clock"],
  [/报告|文件|文档|方案|规划|计划/i, "doc"],
  [/应用|落地|场景|行业|领域|业务/i, "briefcase"],
  [/教育|培训|学习|科普/i, "idea"],
  [/质量|品质|优质|精品/i, "star"],
];

function _inferIcon(text) {
  const s = String(text || "");
  if (!s) return "";
  for (const [re, concept] of _ICON_KEYWORDS) {
    if (re.test(s)) return concept;
  }
  return "";
}

// WP7: style-aware icon BADGE (rounded-square, not a flat circle).
//   line    → hollow accent outline   + accent icon
//   duotone → soft accent tint plate  + accent icon
//   filled  → solid accent plate      + light icon
// Renders a REAL vendored Tabler vector when the concept resolves; otherwise
// falls back to the curated glyph (and `forceNumber` keeps step numbers).
function addIcon(pres, slide, opts) {
  const { x, y, diameter, color, glyph, name, hint, forceNumber } = opts;
  const istyle = STYLE.iconStyle;
  const radius = Math.min(diameter * 0.5, STYLE.radius + 0.06);
  let plateFill, plateLine, glyphColor;
  if (istyle === "line") {
    plateFill = "FFFFFF"; plateLine = color; glyphColor = color;
  } else if (istyle === "filled") {
    plateFill = color; plateLine = color;
    glyphColor = isDarkHex(color) ? "FFFFFF" : "0A0A0A";
  } else { // duotone (default)
    plateFill = tintJS(color, 0.80); plateLine = tintJS(color, 0.80);
    glyphColor = isDarkHex(color) ? color : shadeJS(color, 0.10);
  }
  slide.addShape(cardShapeType(pres), {
    x, y, w: diameter, h: diameter,
    fill: { color: plateFill },
    line: { color: plateLine, pt: istyle === "line" ? 1.5 : 0.5 },
    rectRadius: radius,
  });

  // Prefer a real vector icon (skip for numbered timeline steps). Resolve
  // order: explicit semantic name → glyph-as-concept → inferred from the
  // name → inferred from the hint (item title/desc) → glyph fallback.
  const vec = forceNumber ? null : (
    TABLER.iconPaths(name)
    || TABLER.iconPaths(glyph)
    || TABLER.iconPaths(_inferIcon(name))
    || TABLER.iconPaths(_inferIcon(hint))
  );
  if (vec) {
    const pad = diameter * 0.22;
    try {
      slide.addImage({
        data: _tablerDataUri(vec, glyphColor),
        x: x + pad, y: y + pad, w: diameter - 2 * pad, h: diameter - 2 * pad,
        sizing: { type: "contain", w: diameter - 2 * pad, h: diameter - 2 * pad },
      });
      return;
    } catch (_) { /* fall through to glyph */ }
  }

  const mark = forceNumber ? String(glyph || "").slice(0, 2) : _glyphFor(name, glyph);
  slide.addText(mark, {
    x, y, w: diameter, h: diameter,
    fontFace: forceNumber ? "Arial" : "Segoe UI Symbol",
    fontSize: Math.max(Math.floor(diameter * (forceNumber ? 26 : 30)), 10),
    color: glyphColor,
    bold: true,
    align: "center",
    valign: "mid",
    margin: 0,
  });
}

function addBullets(slide, bullets, box, theme, fonts, opts = {}) {
  const items = Array.isArray(bullets) ? bullets.filter(Boolean).map(String) : [];
  if (items.length === 0) return 0;
  // WP2: size from the active modular scale; relaxed styles also breathe
  // more between lines.
  const fs = opts.fontSize || STYLE.type.body;
  const lead = STYLE.density === "relaxed" ? 12 : 8;
  slide.addText(
    items.map(text => ({ text, options: { bullet: { indent: 18 }, breakLine: true } })),
    {
      ...box,
      fontFace: fonts.body,
      fontSize: fs,
      color: opts.color || theme.secondary,
      paraSpaceAfterPt: opts.paraSpaceAfterPt ?? lead,
      margin: 0.05,
      valign: "top",
    },
  );
  return items.length;
}

// WP2: addTitle now pulls its size from the modular scale and supports an
// optional uppercase KICKER (overline) — the cheap, high-signal "designed"
// element. opts.kicker = string; opts.kickerColor optional.
function addTitle(pres, slide, title, theme, fonts, opts = {}) {
  const baseX = opts.x ?? GRID.MARGIN_X;
  const baseY = opts.y ?? GRID.TITLE_Y;
  const w = opts.w ?? GRID.CONTENT_W;
  const titleSize = opts.fontSize ?? STYLE.type.title;
  let titleY = baseY;

  // WP12 dark_gold: gold-square + bilingual kicker, bold title, short gold
  // underline. Renders the whole header and returns (replaces default below).
  if (PACK === "dark_gold" && pres) {
    const gold = theme.accent;
    let ty = baseY;
    if (opts.kicker) {
      const sq = 0.17;
      slide.addShape(pres.shapes.RECTANGLE, { x: baseX, y: baseY - 0.01, w: sq, h: sq, fill: { color: gold }, line: { color: gold } });
      slide.addText(String(opts.kicker).toUpperCase(), {
        x: baseX + sq + 0.14, y: baseY - 0.08, w: w - sq - 0.2, h: 0.32,
        fontFace: fonts.body, fontSize: STYLE.type.kicker, color: opts.kickerColor ?? gold,
        bold: true, charSpacing: 3, valign: "mid", margin: 0,
      });
      ty = baseY + 0.36;
    }
    const th = opts.h ?? GRID.TITLE_H;
    slide.addText(String(title || ""), {
      x: baseX, y: ty, w, h: th, fontFace: opts.fontFace ?? fonts.header, fontSize: titleSize,
      color: opts.color ?? theme.primary, bold: true, align: opts.align ?? "left",
      valign: "mid", margin: 0, charSpacing: opts.charSpacing ?? 0.4, fit: "shrink",
    });
    if (opts.govRule !== false) {
      slide.addShape(pres.shapes.RECTANGLE, {
        x: baseX, y: ty + th - 0.04, w: 0.95, h: 0.05, fill: { color: gold }, line: { color: gold },
      });
    }
    return;
  }

  // WP-OC: default-pack signature header — a short accent tab to the left of
  // the title (reverse-engineered from the OpenClaw 训练营 deck). This is the
  // one element that makes every content/toc/summary page read as a coherent,
  // intentionally-branded set rather than bare centered text. Specialty packs
  // (swiss/editorial/dark_gold/…) keep their own header identity, so the tab
  // is gated to the default pack and can be force-disabled via opts.noTab.
  const titleH = opts.h ?? GRID.TITLE_H;
  // Only draw the tab when there's an actual title to anchor — quote /
  // big_number / statement "breather" pages pass an empty title, and a lone
  // floating tab reads as a rendering glitch.
  const hasTitle = String(title || "").trim().length > 0;
  const useTab = PACK === "default" && opts.tab !== false && hasTitle &&
    (opts.align ?? "left") === "left";
  const tabW = 0.12;
  const titleX = useTab ? baseX + 0.28 : baseX;
  const titleW = useTab ? w - 0.28 : w;

  if (opts.kicker) {
    slide.addText(String(opts.kicker).toUpperCase(), {
      x: titleX, y: baseY - 0.06, w: titleW, h: 0.26,
      fontFace: fonts.body,
      fontSize: STYLE.type.kicker,
      color: opts.kickerColor ?? theme.accent,
      bold: true,
      align: opts.align ?? "left",
      valign: "mid",
      margin: 0,
      charSpacing: 3,
    });
    titleY = baseY + 0.30;
  }

  if (useTab) {
    const tabH = Math.min(0.36, titleH - 0.04);
    slide.addShape(pres.shapes.RECTANGLE, {
      x: baseX, y: titleY + titleH / 2 - tabH / 2, w: tabW, h: tabH,
      fill: { color: opts.tabColor ?? theme.accent },
      line: { color: opts.tabColor ?? theme.accent },
    });
  }

  slide.addText(String(title || ""), {
    x: titleX,
    y: titleY,
    w: titleW,
    h: titleH,
    fontFace: opts.fontFace ?? fonts.header,
    fontSize: titleSize,
    color: opts.color ?? theme.primary,
    bold: opts.bold ?? true,
    align: opts.align ?? "left",
    valign: "mid",
    margin: 0,
    charSpacing: opts.charSpacing ?? 0.4,
    fit: "shrink",
  });

}

// ── Image placement (slot-based) ─────────────────────────────────────

/**
 * Resolve a semantic slot or explicit coords into an image region.
 * Returns { x, y, w, h } in inches, or null when no image should be drawn.
 *
 * Slots (16:9 canvas, 10" × 5.625"):
 *   right        — right half of body area; pairs with bullets on the left
 *   left         — mirror of right
 *   below_title  — full-width body area below the title
 *   hero         — large image filling most of the slide (title stays at top)
 *   full         — full-bleed background (no margins)
 */
function imageRegion(imageSpec) {
  if (!imageSpec) return null;
  if (typeof imageSpec.x === "number" && typeof imageSpec.y === "number") {
    return {
      x: imageSpec.x,
      y: imageSpec.y,
      w: typeof imageSpec.w === "number" ? imageSpec.w : 4.0,
      h: typeof imageSpec.h === "number" ? imageSpec.h : 3.0,
    };
  }
  const slot = String(imageSpec.slot || "right").toLowerCase();
  switch (slot) {
    case "right":         return { x: 5.20, y: 1.45, w: 4.10, h: 3.20 };
    case "left":          return { x: 0.70, y: 1.45, w: 4.10, h: 3.20 };
    case "below_title":   return { x: 0.78, y: 1.45, w: 8.44, h: 3.20 };
    case "hero":          return { x: 0.40, y: 1.25, w: 9.20, h: 3.85 };
    case "full":          return { x: 0.00, y: 0.00, w: 10.00, h: 5.625 };
    default:              return { x: 5.20, y: 1.45, w: 4.10, h: 3.20 };
  }
}

/**
 * Place an image on a slide and (optionally) a small italic caption below it.
 * Returns the region used so callers can avoid overlapping with text.
 */
function placeSlideImage(slide, imageSpec, theme, fonts) {
  if (!imageSpec || !imageSpec.data_base64) return null;
  const region = imageRegion(imageSpec);
  if (!region) return null;
  const mime = String(imageSpec.mime || "image/png");
  slide.addImage({
    data: `data:${mime};base64,${imageSpec.data_base64}`,
    x: region.x, y: region.y, w: region.w, h: region.h,
    sizing: { type: "contain", w: region.w, h: region.h },
  });
  if (imageSpec.caption) {
    const capY = Math.min(region.y + region.h + 0.06, 5.10);
    slide.addText(String(imageSpec.caption), {
      x: region.x, y: capY, w: region.w, h: 0.30,
      fontFace: fonts.body, fontSize: 11,
      color: CARD.body, italic: true,
      align: "center", margin: 0, fit: "shrink",
    });
  }
  return region;
}

/**
 * For content slides with a right/left side image, return the compact text
 * region available for bullets on the opposite side. Returns null when the
 * image slot does not split the slide (below_title / hero / full).
 */
function sideImageTextRegion(imageSpec) {
  if (!imageSpec) return null;
  const slot = String(imageSpec.slot || "").toLowerCase();
  if (slot === "right") return { x: 0.78, y: 1.50, w: 4.20, h: 3.20 };
  if (slot === "left")  return { x: 5.10, y: 1.50, w: 4.20, h: 3.20 };
  return null;
}

// ── Module render state ──────────────────────────────────────────────
// This is a single-shot CLI (one process per build, no concurrency), so
// the active style + per-slide shadow budget live at module scope rather
// than threading through ~20 renderer signatures.
let STYLE = resolveStyle("soft");
let SHADOW_BUDGET = makeShadowBudget(3);
let GRADIENT_BG = true;   // WP9: subtle gradient on anchor slides; spec.bg_gradient:false disables
// WP12: "design pack" — a per-deck VISUAL PERSONALITY (cover composition, section
// style, header treatment, fonts) layered on top of theme(color)+style(shape).
// Two decks with the same theme but different packs look genuinely different,
// not just recolored. "default" = the original look. spec.pack selects one.
let PACK = "default";

// WP14: adaptive CARD colors. Content layouts historically hard-coded white
// card fills + theme.primary/secondary text — invisible on a dark-bg theme.
// CARD flips the whole card treatment by bg luminance so dark themes
// (glass/terminal/agent/capital/…) render dark elevated panels with light
// text instead of white slabs. Set once per build in main(); used everywhere
// a floating card is drawn.
let CARD = { fill: "FFFFFF", line: "ECECEC", title: "0A0A0A", body: "555555" };
function computeCard(theme) {
  if (isDarkHex(theme.bg)) {
    const glass = PACK === "glass";
    return {
      // glass: slightly brighter "frosted" panel + an accent-tinted glow edge.
      fill: tintJS(theme.bg, glass ? 0.11 : 0.08),
      line: glass ? tintJS(theme.accent, 0.45) : tintJS(theme.bg, 0.20),
      title: theme.text_on_primary || "FFFFFF",
      body: isDarkHex(theme.light) ? "C9CEDA" : theme.light,
    };
  }
  return { fill: "FFFFFF", line: shadeJS(theme.bg, 0.07), title: theme.primary, body: theme.secondary };
}

// ── Renderers ────────────────────────────────────────────────────────

// ── WP12: dark_gold design pack (深色高对比·深蓝金商务汇报) ──
// Reverse-engineered from a hand-made 周报 deck: dark navy anchor pages + gold
// accent + bilingual gold-square kickers + bold sans titles + ghost circles +
// multi-color KPI cards. Content pages stay LIGHT (sandwich), anchors go dark.

function renderCoverDarkGold(pres, slide, spec, theme, fonts) {
  const navy = brandBg(theme);
  const gold = theme.accent;
  const T = STYLE.type;
  slide.background = anchorBackground(theme, "dark", navy);

  // Ghost circles bleeding off the right (depth, tonal shifts of the navy).
  slide.addShape(pres.shapes.OVAL, { x: 7.9, y: -1.7, w: 4.9, h: 4.9, fill: { color: tintJS(navy, 0.10) }, line: { color: tintJS(navy, 0.10) } });
  slide.addShape(pres.shapes.OVAL, { x: 10.3, y: 3.6, w: 2.6, h: 2.6, fill: { color: shadeJS(navy, 0.28) }, line: { color: shadeJS(navy, 0.28) } });

  // Top + bottom thin gold rules frame the cover.
  slide.addShape(pres.shapes.RECTANGLE, { x: 0, y: 0, w: 10, h: 0.07, fill: { color: gold }, line: { color: gold } });
  slide.addShape(pres.shapes.RECTANGLE, { x: 0, y: 5.555, w: 10, h: 0.07, fill: { color: gold }, line: { color: gold } });

  // Gold square + bilingual kicker.
  const kicker = spec.kicker || spec.subject || "";
  if (kicker) {
    slide.addShape(pres.shapes.RECTANGLE, { x: 0.72, y: 1.30, w: 0.18, h: 0.18, fill: { color: gold }, line: { color: gold } });
    slide.addText(String(kicker).toUpperCase(), {
      x: 1.02, y: 1.22, w: 8.2, h: 0.34, fontFace: fonts.body, fontSize: T.kicker,
      color: gold, bold: true, charSpacing: 5, valign: "mid", margin: 0,
    });
  }

  // Big bold white title.
  slide.addText(String(spec.title || ""), {
    x: 0.72, y: 1.92, w: 8.4, h: 1.5, fontFace: fonts.header, fontSize: T.large_title,
    color: "FFFFFF", bold: true, valign: "mid", margin: 0, charSpacing: 0.5, fit: "shrink",
  });
  if (spec.subtitle) {
    slide.addText(String(spec.subtitle), {
      x: 0.74, y: 3.42, w: 7.8, h: 0.6, fontFace: fonts.header, fontSize: T.subtitle,
      color: "FFFFFF", valign: "mid", margin: 0, fit: "shrink",
    });
  }
  if (spec.tagline) {
    slide.addText(String(spec.tagline), {
      x: 0.74, y: 4.06, w: 7.8, h: 0.45, fontFace: fonts.body, fontSize: T.support,
      color: tintJS(navy, 0.62), italic: true, valign: "mid", margin: 0, fit: "shrink",
    });
  }
  // Double-rule motif (short gold) + meta line bottom-left.
  slide.addShape(pres.shapes.RECTANGLE, { x: 0.74, y: 4.72, w: 0.95, h: 0.05, fill: { color: gold }, line: { color: gold } });
  const meta = spec.body || spec.author || "";
  if (meta) {
    slide.addText(String(meta), {
      x: 0.74, y: 4.88, w: 7.0, h: 0.34, fontFace: fonts.header, fontSize: T.support,
      color: "FFFFFF", bold: true, valign: "mid", margin: 0, fit: "shrink",
    });
  }
  // Optional bottom stat strip: spec.footer (string) or spec.stats:[{value,label}].
  let footer = spec.footer;
  if (!footer && Array.isArray(spec.stats) && spec.stats.length) {
    footer = spec.stats.map(s => (typeof s === "object" ? `${s.value || ""} ${s.label || ""}`.trim() : String(s))).join("    ·    ");
  }
  if (footer) {
    slide.addText(String(footer), {
      x: 0.74, y: 5.21, w: 8.5, h: 0.3, fontFace: fonts.body, fontSize: T.caption,
      color: tintJS(navy, 0.6), charSpacing: 1, valign: "mid", margin: 0, fit: "shrink",
    });
  }
}

function renderSectionDarkGold(pres, slide, spec, theme, fonts, index) {
  const navy = brandBg(theme);
  const gold = theme.accent;
  const T = STYLE.type;
  slide.background = anchorBackground(theme, "dark", navy);
  slide.addShape(pres.shapes.OVAL, { x: 8.5, y: -1.4, w: 4.4, h: 4.4, fill: { color: tintJS(navy, 0.09) }, line: { color: tintJS(navy, 0.09) } });

  const numText = spec.number != null ? String(spec.number) : String(index).padStart(2, "0");
  // Faint oversized number watermark.
  slide.addText(numText, {
    x: 0.6, y: 0.7, w: 4.5, h: 3.2, fontFace: fonts.header, fontSize: Math.round(T.large_title * 2.6),
    color: tintJS(navy, 0.14), bold: true, valign: "mid", margin: 0,
  });
  if (spec.kicker) {
    slide.addShape(pres.shapes.RECTANGLE, { x: 0.72, y: 2.18, w: 0.17, h: 0.17, fill: { color: gold }, line: { color: gold } });
    slide.addText(String(spec.kicker).toUpperCase(), {
      x: 1.0, y: 2.11, w: 8, h: 0.3, fontFace: fonts.body, fontSize: T.kicker, color: gold,
      bold: true, charSpacing: 4, valign: "mid", margin: 0,
    });
  }
  slide.addText(String(spec.title || ""), {
    x: 0.72, y: 2.5, w: 8.6, h: 1.1, fontFace: fonts.header, fontSize: T.large_title,
    color: "FFFFFF", bold: true, valign: "mid", margin: 0, charSpacing: 0.5, fit: "shrink",
  });
  slide.addShape(pres.shapes.RECTANGLE, { x: 0.74, y: 3.72, w: 0.95, h: 0.05, fill: { color: gold }, line: { color: gold } });
  if (spec.subtitle) {
    slide.addText(String(spec.subtitle), {
      x: 0.74, y: 3.92, w: 8.0, h: 0.5, fontFace: fonts.body, fontSize: T.support,
      color: tintJS(navy, 0.6), valign: "mid", margin: 0, fit: "shrink",
    });
  }
  addBadge(pres, slide, theme, index, { onDark: true });
}

// ── WP15: design-family packs (reverse-engineered from the 23 open-source
// decks). Each supplies a cover + section renderer with a distinct visual
// personality; content pages inherit the shared bg-adaptive layouts (CARD).
// packContentBackdrop() adds per-pack texture to content/toc/summary slides.

function packMultis(theme) { return [theme.accent, ...MULTI_ACCENTS]; }

function drawBlueprintGrid(pres, slide, baseColor) {
  const line = tintJS(baseColor, 0.13);
  for (let gx = 0; gx <= 10.001; gx += 0.66)
    slide.addShape(pres.shapes.LINE, { x: gx, y: 0, w: 0, h: 5.625, line: { color: line, width: 0.5, dashType: "dash" } });
  for (let gy = 0; gy <= 5.626; gy += 0.66)
    slide.addShape(pres.shapes.LINE, { x: 0, y: gy, w: 10, h: 0, line: { color: line, width: 0.5, dashType: "dash" } });
}

// Layered onto content/toc/summary AFTER they set their own bg.
function packContentBackdrop(pres, slide, theme) {
  if (PACK === "blueprint") {
    const base = brandBg(theme);
    slide.background = anchorBackground(theme, "dark", base);
    drawBlueprintGrid(pres, slide, base);
  } else if (PACK === "glass") {
    slide.background = { data: gradientPng({ stops: [
      { pos: 0, color: tintJS(theme.bg, 0.10) },
      { pos: 0.6, color: theme.bg },
      { pos: 1, color: shadeJS(theme.bg, 0.16) }], angleDeg: 130, grain: 2 }) };
  } else if (PACK === "newsprint") {
    slide.addShape(pres.shapes.RECTANGLE, { x: 0, y: 0, w: 10, h: 0.05, fill: { color: theme.primary }, line: { color: theme.primary } });
  }
}

// ── editorial (image-led, serif numerals, hairlines; light + dark variants) ──
function renderCoverEditorial(pres, slide, spec, theme, fonts) {
  const onDark = isDarkHex(theme.bg);
  const base = onDark ? brandBg(theme) : theme.bg;
  slide.background = anchorBackground(theme, onDark ? "dark" : "light", base);
  const ink = onDark ? "FFFFFF" : theme.primary;
  const sub = onDark ? tintJS(base, 0.58) : theme.secondary;
  const T = STYLE.type;
  const hasImg = spec.image && typeof spec.image === "object" && spec.image.data_base64;
  if (hasImg) placeSlideImage(slide, { ...spec.image, slot: spec.image.slot || "right" }, theme, fonts);
  const w = hasImg ? 4.3 : 7.7;
  slide.addShape(pres.shapes.RECTANGLE, { x: 0.72, y: 0.78, w: hasImg ? 4.3 : 8.56, h: 0.016, fill: { color: ink }, line: { color: ink } });
  const kicker = spec.kicker || spec.subject || "";
  if (kicker) slide.addText(String(kicker).toUpperCase(), { x: 0.72, y: 0.9, w, h: 0.3, fontFace: fonts.body, fontSize: T.kicker, color: theme.accent, bold: true, charSpacing: 5, margin: 0 });
  slide.addText(String(spec.title || ""), { x: 0.72, y: 1.95, w, h: 1.8, fontFace: fonts.header, fontSize: Math.round(T.large_title * 0.92), color: ink, bold: true, valign: "mid", margin: 0, charSpacing: 0.3, fit: "shrink" });
  if (spec.subtitle) slide.addText(String(spec.subtitle), { x: 0.74, y: 3.78, w, h: 0.6, fontFace: fonts.body, fontSize: T.subtitle, color: sub, italic: true, margin: 0, fit: "shrink" });
  slide.addShape(pres.shapes.RECTANGLE, { x: 0.74, y: 4.7, w: 0.8, h: 0.04, fill: { color: theme.accent }, line: { color: theme.accent } });
  const meta = spec.body || spec.author || "";
  if (meta) slide.addText(String(meta), { x: 0.74, y: 4.84, w, h: 0.34, fontFace: fonts.body, fontSize: T.caption, color: sub, charSpacing: 1, margin: 0 });
}
function renderSectionEditorial(pres, slide, spec, theme, fonts, index) {
  const onDark = isDarkHex(theme.bg);
  const base = onDark ? brandBg(theme) : theme.bg;
  slide.background = anchorBackground(theme, onDark ? "dark" : "light", base);
  const ink = onDark ? "FFFFFF" : theme.primary;
  const sub = onDark ? tintJS(base, 0.58) : theme.secondary;
  const div = onDark ? tintJS(base, 0.30) : shadeJS(base, 0.12);
  const T = STYLE.type;
  const num = spec.number != null ? String(spec.number) : String(index).padStart(2, "0");
  slide.addText(num, { x: 0.6, y: 1.0, w: 3.4, h: 3.4, fontFace: fonts.header, fontSize: Math.round(T.large_title * 1.7), color: theme.accent, bold: true, align: "left", valign: "mid", margin: 0 });
  slide.addShape(pres.shapes.RECTANGLE, { x: 4.0, y: 1.5, w: 0.014, h: 2.6, fill: { color: div }, line: { color: div } });
  if (spec.kicker) slide.addText(String(spec.kicker).toUpperCase(), { x: 4.3, y: 2.0, w: 5.2, h: 0.3, fontFace: fonts.body, fontSize: T.kicker, color: theme.accent, bold: true, charSpacing: 4, margin: 0 });
  slide.addText(String(spec.title || ""), { x: 4.3, y: 2.34, w: 5.2, h: 1.2, fontFace: fonts.header, fontSize: T.title, color: ink, bold: true, valign: "mid", margin: 0, fit: "shrink" });
  if (spec.subtitle) slide.addText(String(spec.subtitle), { x: 4.3, y: 3.55, w: 5.2, h: 0.6, fontFace: fonts.body, fontSize: T.support, color: sub, italic: true, margin: 0, fit: "shrink" });
  addBadge(pres, slide, theme, index, { onDark });
}

// ── swiss (typographic grid, oversized title + index, hairlines, no cards) ──
function renderCoverSwiss(pres, slide, spec, theme, fonts) {
  slide.background = { color: theme.bg };
  const T = STYLE.type;
  const gl = shadeJS(theme.bg, 0.06);
  for (const gx of [0.72, 3.14, 5.56, 7.98, 9.28]) slide.addShape(pres.shapes.LINE, { x: gx, y: 0.72, w: 0, h: 4.18, line: { color: gl, width: 0.5 } });
  slide.addShape(pres.shapes.RECTANGLE, { x: 9.0, y: 0.72, w: 0.28, h: 0.28, fill: { color: theme.accent }, line: { color: theme.accent } });
  const kicker = spec.kicker || spec.subject || "";
  if (kicker) slide.addText(String(kicker).toUpperCase(), { x: 0.72, y: 0.82, w: 7, h: 0.3, fontFace: fonts.body, fontSize: T.kicker, color: theme.accent, bold: true, charSpacing: 5, margin: 0 });
  slide.addText(String(spec.title || ""), { x: 0.72, y: 1.65, w: 8.4, h: 2.2, fontFace: fonts.header, fontSize: Math.round(T.large_title * 1.08), color: theme.primary, bold: true, valign: "top", margin: 0, fit: "shrink" });
  if (spec.subtitle) slide.addText(String(spec.subtitle), { x: 0.74, y: 4.0, w: 7.5, h: 0.6, fontFace: fonts.body, fontSize: T.subtitle, color: theme.secondary, margin: 0, fit: "shrink" });
  slide.addShape(pres.shapes.RECTANGLE, { x: 0.72, y: 4.78, w: 0.4, h: 0.05, fill: { color: theme.accent }, line: { color: theme.accent } });
  const meta = spec.body || spec.author || "";
  if (meta) slide.addText(String(meta), { x: 0.72, y: 4.9, w: 8, h: 0.32, fontFace: fonts.body, fontSize: T.caption, color: theme.secondary, charSpacing: 1, margin: 0 });
}
function renderSectionSwiss(pres, slide, spec, theme, fonts, index) {
  slide.background = { color: theme.bg };
  const T = STYLE.type;
  const num = spec.number != null ? String(spec.number) : String(index).padStart(2, "0");
  slide.addShape(pres.shapes.RECTANGLE, { x: 0.72, y: 0.9, w: 8.56, h: 0.02, fill: { color: theme.primary }, line: { color: theme.primary } });
  slide.addText(num, { x: 0.66, y: 1.2, w: 8.6, h: 2.6, fontFace: fonts.header, fontSize: Math.round(T.large_title * 1.8), color: theme.accent, bold: true, align: "left", valign: "mid", margin: 0 });
  if (spec.kicker) slide.addText(String(spec.kicker).toUpperCase(), { x: 0.74, y: 4.02, w: 8, h: 0.3, fontFace: fonts.body, fontSize: T.kicker, color: theme.secondary, bold: true, charSpacing: 4, margin: 0 });
  slide.addText(String(spec.title || ""), { x: 0.72, y: 4.32, w: 8.4, h: 0.9, fontFace: fonts.header, fontSize: T.title, color: theme.primary, bold: true, margin: 0, fit: "shrink" });
  addBadge(pres, slide, theme, index);
}

// ── ink (中式水墨：宣纸 + 朱砂印章 + 竖线 + 极简) ──
function renderCoverInk(pres, slide, spec, theme, fonts) {
  slide.background = { color: theme.bg };
  const T = STYLE.type; const red = theme.accent;
  slide.addShape(pres.shapes.RECTANGLE, { x: 0.9, y: 1.4, w: 0.05, h: 2.8, fill: { color: red }, line: { color: red } });
  slide.addShape(pres.shapes.RECTANGLE, { x: 8.5, y: 0.8, w: 0.7, h: 0.7, fill: { color: red }, line: { color: red } });
  slide.addText(String(spec.seal || "印"), { x: 8.5, y: 0.8, w: 0.7, h: 0.7, fontFace: fonts.header, fontSize: T.subtitle, color: theme.bg, bold: true, align: "center", valign: "mid", margin: 0 });
  slide.addText(String(spec.title || ""), { x: 1.2, y: 1.5, w: 7, h: 1.6, fontFace: fonts.header, fontSize: Math.round(T.large_title * 1.02), color: theme.primary, bold: true, valign: "mid", margin: 0, charSpacing: 2, fit: "shrink" });
  if (spec.subtitle) slide.addText(String(spec.subtitle), { x: 1.22, y: 3.2, w: 6.5, h: 0.6, fontFace: fonts.body, fontSize: T.subtitle, color: theme.secondary, margin: 0, fit: "shrink" });
  const meta = spec.body || spec.author || "";
  if (meta) slide.addText(String(meta), { x: 1.22, y: 4.6, w: 7, h: 0.34, fontFace: fonts.body, fontSize: T.caption, color: theme.secondary, charSpacing: 2, margin: 0 });
}
function renderSectionInk(pres, slide, spec, theme, fonts, index) {
  slide.background = { color: theme.bg };
  const T = STYLE.type; const red = theme.accent;
  const num = spec.number != null ? String(spec.number) : String(index).padStart(2, "0");
  slide.addText(num, { x: 0.8, y: 0.8, w: 4, h: 3.4, fontFace: fonts.header, fontSize: Math.round(T.large_title * 2.2), color: tintJS(theme.secondary, 0.55), bold: true, valign: "mid", margin: 0 });
  slide.addShape(pres.shapes.RECTANGLE, { x: 5.2, y: 1.8, w: 0.05, h: 1.8, fill: { color: red }, line: { color: red } });
  slide.addShape(pres.shapes.RECTANGLE, { x: 5.5, y: 1.7, w: 0.5, h: 0.5, fill: { color: red }, line: { color: red } });
  slide.addText(String(spec.title || ""), { x: 5.5, y: 2.4, w: 3.8, h: 1.1, fontFace: fonts.header, fontSize: T.title, color: theme.primary, bold: true, charSpacing: 2, valign: "mid", margin: 0, fit: "shrink" });
  if (spec.subtitle) slide.addText(String(spec.subtitle), { x: 5.5, y: 3.5, w: 3.8, h: 0.5, fontFace: fonts.body, fontSize: T.support, color: theme.secondary, margin: 0, fit: "shrink" });
  addBadge(pres, slide, theme, index);
}

// ── glass (玻璃拟态：深色渐变 + 圆角半透面板 + 霓虹辉光) ──
function renderCoverGlass(pres, slide, spec, theme, fonts) {
  const base = theme.bg;
  slide.background = { data: gradientPng({ stops: [{ pos: 0, color: tintJS(base, 0.16) }, { pos: 0.55, color: base }, { pos: 1, color: shadeJS(base, 0.22) }], angleDeg: 125, grain: 2 }) };
  const T = STYLE.type; const cyan = theme.accent;
  slide.addShape(pres.shapes.OVAL, { x: 6.6, y: -1.4, w: 5.4, h: 5.4, fill: { color: tintJS(base, 0.10) }, line: { color: tintJS(base, 0.10) } });
  slide.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: 0.72, y: 1.5, w: 6.4, h: 2.7, fill: { color: tintJS(base, 0.06) }, line: { color: tintJS(base, 0.22), pt: 1 }, rectRadius: 0.16 });
  addAccentDot(pres, slide, 1.0, 1.86, 0.2, cyan);
  const kicker = spec.kicker || spec.subject || "";
  if (kicker) slide.addText(String(kicker).toUpperCase(), { x: 1.32, y: 1.78, w: 5.5, h: 0.3, fontFace: fonts.body, fontSize: T.kicker, color: cyan, bold: true, charSpacing: 4, margin: 0 });
  slide.addText(String(spec.title || ""), { x: 1.0, y: 2.25, w: 5.9, h: 1.3, fontFace: fonts.header, fontSize: Math.round(T.large_title * 0.9), color: "FFFFFF", bold: true, valign: "mid", margin: 0, fit: "shrink" });
  if (spec.subtitle) slide.addText(String(spec.subtitle), { x: 1.02, y: 3.55, w: 5.6, h: 0.5, fontFace: fonts.body, fontSize: T.support, color: tintJS(base, 0.62), margin: 0, fit: "shrink" });
  slide.addShape(pres.shapes.RECTANGLE, { x: 1.02, y: 4.02, w: 1.0, h: 0.04, fill: { color: cyan }, line: { color: cyan } });
  const meta = spec.body || spec.author || "";
  if (meta) slide.addText(String(meta), { x: 0.74, y: 4.9, w: 8, h: 0.32, fontFace: fonts.body, fontSize: T.caption, color: tintJS(base, 0.55), charSpacing: 1, margin: 0 });
}
function renderSectionGlass(pres, slide, spec, theme, fonts, index) {
  const base = theme.bg;
  slide.background = { data: gradientPng({ stops: [{ pos: 0, color: tintJS(base, 0.14) }, { pos: 1, color: shadeJS(base, 0.2) }], angleDeg: 125, grain: 2 }) };
  const T = STYLE.type; const cyan = theme.accent;
  const num = spec.number != null ? String(spec.number) : String(index).padStart(2, "0");
  slide.addText(num, { x: 0.6, y: 0.7, w: 4.5, h: 3.2, fontFace: fonts.header, fontSize: Math.round(T.large_title * 2.4), color: tintJS(base, 0.14), bold: true, valign: "mid", margin: 0 });
  addAccentDot(pres, slide, 0.74, 2.2, 0.18, cyan);
  if (spec.kicker) slide.addText(String(spec.kicker).toUpperCase(), { x: 1.02, y: 2.12, w: 8, h: 0.3, fontFace: fonts.body, fontSize: T.kicker, color: cyan, bold: true, charSpacing: 4, margin: 0 });
  slide.addText(String(spec.title || ""), { x: 0.72, y: 2.5, w: 8.6, h: 1.0, fontFace: fonts.header, fontSize: Math.round(T.large_title * 0.8), color: "FFFFFF", bold: true, valign: "mid", margin: 0, fit: "shrink" });
  slide.addShape(pres.shapes.RECTANGLE, { x: 0.74, y: 3.62, w: 1.0, h: 0.04, fill: { color: cyan }, line: { color: cyan } });
  addBadge(pres, slide, theme, index, { onDark: true });
}

// ── blueprint (蓝图：网格底 + 琥珀线框 + 角标) ──
function renderCoverBlueprint(pres, slide, spec, theme, fonts) {
  const base = brandBg(theme);
  slide.background = anchorBackground(theme, "dark", base);
  drawBlueprintGrid(pres, slide, base);
  const T = STYLE.type; const amber = theme.accent; const tick = 0.35;
  for (const [cx, cy, dx, dy] of [[0.6, 0.6, 1, 1], [9.4, 0.6, -1, 1], [0.6, 5.0, 1, -1], [9.4, 5.0, -1, -1]]) {
    slide.addShape(pres.shapes.LINE, { x: cx, y: cy, w: tick * dx, h: 0, line: { color: amber, width: 1.5 } });
    slide.addShape(pres.shapes.LINE, { x: cx, y: cy, w: 0, h: tick * dy, line: { color: amber, width: 1.5 } });
  }
  const kicker = spec.kicker || spec.subject || "";
  if (kicker) slide.addText(String(kicker).toUpperCase(), { x: 0.9, y: 1.5, w: 8, h: 0.3, fontFace: fonts.body, fontSize: T.kicker, color: amber, bold: true, charSpacing: 5, margin: 0 });
  slide.addText(String(spec.title || ""), { x: 0.9, y: 2.1, w: 8.2, h: 1.4, fontFace: fonts.header, fontSize: Math.round(T.large_title * 0.95), color: "FFFFFF", bold: true, valign: "mid", margin: 0, fit: "shrink" });
  if (spec.subtitle) slide.addText(String(spec.subtitle), { x: 0.92, y: 3.6, w: 8, h: 0.5, fontFace: fonts.body, fontSize: T.support, color: tintJS(base, 0.6), margin: 0, fit: "shrink" });
  const meta = spec.body || spec.author || "";
  if (meta) slide.addText(String(meta), { x: 0.92, y: 4.6, w: 8, h: 0.32, fontFace: "Consolas", fontSize: T.caption, color: amber, charSpacing: 1, margin: 0 });
}
function renderSectionBlueprint(pres, slide, spec, theme, fonts, index) {
  const base = brandBg(theme);
  slide.background = anchorBackground(theme, "dark", base);
  drawBlueprintGrid(pres, slide, base);
  const T = STYLE.type; const amber = theme.accent;
  const num = spec.number != null ? String(spec.number) : String(index).padStart(2, "0");
  slide.addShape(pres.shapes.RECTANGLE, { x: 0.9, y: 1.6, w: 1.7, h: 1.7, fill: { color: base }, line: { color: amber, width: 1.2 } });
  slide.addText(num, { x: 0.9, y: 1.6, w: 1.7, h: 1.7, fontFace: fonts.header, fontSize: T.large_title, color: amber, bold: true, align: "center", valign: "mid", margin: 0 });
  if (spec.kicker) slide.addText(String(spec.kicker).toUpperCase(), { x: 3.0, y: 1.9, w: 6, h: 0.3, fontFace: fonts.body, fontSize: T.kicker, color: amber, bold: true, charSpacing: 4, margin: 0 });
  slide.addText(String(spec.title || ""), { x: 3.0, y: 2.25, w: 6.1, h: 1.0, fontFace: fonts.header, fontSize: T.title, color: "FFFFFF", bold: true, valign: "mid", margin: 0, fit: "shrink" });
  addBadge(pres, slide, theme, index, { onDark: true });
}

// ── newsprint (报纸/野兽派：报头 + 衬线大标题 + 分栏线) ──
function renderCoverNewsprint(pres, slide, spec, theme, fonts) {
  slide.background = { color: theme.bg };
  const T = STYLE.type; const ink = theme.primary; const red = theme.accent;
  slide.addShape(pres.shapes.RECTANGLE, { x: 0.6, y: 0.5, w: 8.8, h: 0.05, fill: { color: ink }, line: { color: ink } });
  const kicker = spec.kicker || spec.subject || "THE BRIEF";
  slide.addText(String(kicker).toUpperCase(), { x: 0.6, y: 0.6, w: 8.8, h: 0.34, fontFace: fonts.body, fontSize: T.support, color: ink, bold: true, align: "center", charSpacing: 8, margin: 0 });
  slide.addShape(pres.shapes.RECTANGLE, { x: 0.6, y: 1.02, w: 8.8, h: 0.014, fill: { color: ink }, line: { color: ink } });
  slide.addText(String(spec.title || ""), { x: 0.6, y: 1.5, w: 8.8, h: 2.0, fontFace: fonts.header, fontSize: Math.round(T.large_title * 1.08), color: ink, bold: true, align: "center", valign: "mid", margin: 0, fit: "shrink" });
  if (spec.subtitle) slide.addText(String(spec.subtitle), { x: 0.8, y: 3.6, w: 8.4, h: 0.5, fontFace: fonts.body, fontSize: T.subtitle, color: red, italic: true, align: "center", margin: 0, fit: "shrink" });
  slide.addShape(pres.shapes.RECTANGLE, { x: 0.6, y: 4.4, w: 8.8, h: 0.014, fill: { color: ink }, line: { color: ink } });
  for (const cx of [3.13, 5.66]) slide.addShape(pres.shapes.LINE, { x: cx, y: 4.55, w: 0, h: 0.7, line: { color: shadeJS(theme.bg, 0.18), width: 0.5 } });
  const meta = spec.body || spec.author || "";
  if (meta) slide.addText(String(meta).toUpperCase(), { x: 0.6, y: 4.5, w: 8.8, h: 0.3, fontFace: fonts.body, fontSize: T.caption, color: theme.secondary, align: "center", charSpacing: 2, margin: 0 });
}
function renderSectionNewsprint(pres, slide, spec, theme, fonts, index) {
  slide.background = { color: theme.bg };
  const T = STYLE.type; const ink = theme.primary; const red = theme.accent;
  const num = spec.number != null ? String(spec.number) : String(index).padStart(2, "0");
  slide.addShape(pres.shapes.RECTANGLE, { x: 0.6, y: 1.4, w: 8.8, h: 0.05, fill: { color: ink }, line: { color: ink } });
  slide.addText("SECTION " + num, { x: 0.6, y: 1.5, w: 8.8, h: 0.3, fontFace: fonts.body, fontSize: T.kicker, color: red, bold: true, charSpacing: 6, align: "center", margin: 0 });
  slide.addText(String(spec.title || ""), { x: 0.6, y: 2.2, w: 8.8, h: 1.3, fontFace: fonts.header, fontSize: Math.round(T.large_title * 0.92), color: ink, bold: true, align: "center", valign: "mid", margin: 0, fit: "shrink" });
  slide.addShape(pres.shapes.RECTANGLE, { x: 0.6, y: 3.7, w: 8.8, h: 0.014, fill: { color: ink }, line: { color: ink } });
  if (spec.subtitle) slide.addText(String(spec.subtitle), { x: 0.8, y: 3.85, w: 8.4, h: 0.4, fontFace: fonts.body, fontSize: T.support, color: theme.secondary, italic: true, align: "center", margin: 0, fit: "shrink" });
  addBadge(pres, slide, theme, index);
}

// ── memphis (孟菲斯/Riso：几何撞色 + 贴纸 + 大圆序号) ──
function renderCoverMemphis(pres, slide, spec, theme, fonts) {
  slide.background = { color: theme.bg };
  const T = STYLE.type; const cols = packMultis(theme);
  [["OVAL", 8.4, 0.5, 0.7, 0.7, cols[1]], ["RECTANGLE", 9.1, 1.6, 0.5, 0.5, cols[2]], ["OVAL", 0.5, 4.6, 0.6, 0.6, cols[3]], ["RECTANGLE", 8.7, 4.4, 0.6, 0.6, cols[4]], ["OVAL", 9.0, 3.0, 0.4, 0.4, cols[0]]]
    .forEach(([sh, x, y, w, h, c]) => slide.addShape(pres.shapes[sh], { x, y, w, h, fill: { color: c }, line: { color: c } }));
  const kicker = spec.kicker || spec.subject || "";
  if (kicker) {
    slide.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: 0.72, y: 1.0, w: 2.7, h: 0.46, fill: { color: theme.accent }, line: { color: theme.accent }, rectRadius: 0.22 });
    slide.addText(String(kicker).toUpperCase(), { x: 0.72, y: 1.0, w: 2.7, h: 0.46, fontFace: fonts.body, fontSize: T.kicker, color: "FFFFFF", bold: true, align: "center", valign: "mid", charSpacing: 2, margin: 0 });
  }
  slide.addText(String(spec.title || ""), { x: 0.7, y: 1.7, w: 7.5, h: 1.8, fontFace: fonts.header, fontSize: Math.round(T.large_title * 1.05), color: theme.primary, bold: true, valign: "mid", margin: 0, fit: "shrink" });
  if (spec.subtitle) slide.addText(String(spec.subtitle), { x: 0.74, y: 3.6, w: 7, h: 0.6, fontFace: fonts.body, fontSize: T.subtitle, color: theme.secondary, margin: 0, fit: "shrink" });
  const meta = spec.body || spec.author || "";
  if (meta) slide.addText(String(meta), { x: 0.74, y: 4.8, w: 7, h: 0.32, fontFace: fonts.body, fontSize: T.caption, color: theme.secondary, charSpacing: 1, margin: 0 });
}
function renderSectionMemphis(pres, slide, spec, theme, fonts, index) {
  slide.background = { color: theme.bg };
  const T = STYLE.type; const cols = packMultis(theme);
  const num = spec.number != null ? String(spec.number) : String(index).padStart(2, "0");
  slide.addShape(pres.shapes.OVAL, { x: 0.7, y: 1.3, w: 2.4, h: 2.4, fill: { color: theme.accent }, line: { color: theme.accent } });
  slide.addText(num, { x: 0.7, y: 1.3, w: 2.4, h: 2.4, fontFace: fonts.header, fontSize: T.large_title, color: "FFFFFF", bold: true, align: "center", valign: "mid", margin: 0 });
  [["RECTANGLE", 8.8, 0.7, 0.5, 0.5, cols[1]], ["OVAL", 8.9, 4.2, 0.6, 0.6, cols[3]]].forEach(([sh, x, y, w, h, c]) => slide.addShape(pres.shapes[sh], { x, y, w, h, fill: { color: c }, line: { color: c } }));
  if (spec.kicker) slide.addText(String(spec.kicker).toUpperCase(), { x: 3.5, y: 1.9, w: 5.5, h: 0.3, fontFace: fonts.body, fontSize: T.kicker, color: cols[2], bold: true, charSpacing: 3, margin: 0 });
  slide.addText(String(spec.title || ""), { x: 3.5, y: 2.3, w: 5.6, h: 1.1, fontFace: fonts.header, fontSize: T.title, color: theme.primary, bold: true, valign: "mid", margin: 0, fit: "shrink" });
  addBadge(pres, slide, theme, index);
}

const COVER_PACKS = {
  dark_gold: renderCoverDarkGold, editorial: renderCoverEditorial, swiss: renderCoverSwiss,
  ink: renderCoverInk, glass: renderCoverGlass, blueprint: renderCoverBlueprint,
  newsprint: renderCoverNewsprint, memphis: renderCoverMemphis,
};
const SECTION_PACKS = {
  dark_gold: renderSectionDarkGold, editorial: renderSectionEditorial, swiss: renderSectionSwiss,
  ink: renderSectionInk, glass: renderSectionGlass, blueprint: renderSectionBlueprint,
  newsprint: renderSectionNewsprint, memphis: renderSectionMemphis,
};

function renderCover(pres, slide, spec, theme, fonts) {
  if (COVER_PACKS[PACK]) return COVER_PACKS[PACK](pres, slide, spec, theme, fonts);
  // Default to light cover (政府汇报风格); explicit "dark" for high-impact decks.
  const cstyle = String(spec.cover_style || "light").toLowerCase();
  const dark = cstyle === "dark";
  // Dark cover fills with the theme's BRAND color (blue for a blue theme),
  // not a black slab — see brandBg().
  const bg = dark ? brandBg(theme) : theme.bg;
  const onDark = dark && isDarkHex(bg);
  // Light style on a dark-bg palette (dark-bg pages): theme.primary is
  // itself dark, so we'd render dark-on-dark and lose title/subtitle. Use the
  // same bg-luminance adaptation as content pages — textColorsForBg flips to
  // text_on_primary (white) when bg is dark.
  const tcLight = textColorsForBg(theme);
  const bgIsDark = isDarkHex(theme.bg);
  const titleColor = dark ? (onDark ? "FFFFFF" : "0A0A0A") : tcLight.title;
  const subColor = dark
    ? (onDark ? tintJS(bg, 0.62) : shadeJS(bg, 0.45))
    : (bgIsDark ? tcLight.body : theme.secondary);
  const metaColor = subColor;
  // On a brand-colored dark cover the accent itself is the bg, so the motif
  // flips to white; on a light cover the accent is visible against bg
  // (a dark-bg page uses a bright accent — e.g. gold — so it reads
  // correctly on both light and dark bgs).
  const motif = dark ? (onDark ? "FFFFFF" : "0A0A0A") : theme.accent;
  const T = STYLE.type;

  // WP9: subtle tonal gradient instead of a flat slab (anchor slides only).
  slide.background = anchorBackground(theme, (dark && onDark) || bgIsDark ? "dark" : "light", bg);

  // WP5: oversized faint GHOST graphic bleeding off the right edge — gives
  // the cover depth without clutter. Tonal shift of the bg itself.
  const ghost = dark ? (onDark ? tintJS(bg, 0.12) : shadeJS(bg, 0.10)) : tintJS(theme.accent, 0.90);
  slide.addShape(pres.shapes.OVAL, {
    x: 6.55, y: -1.25, w: 5.6, h: 5.6,
    fill: { color: ghost }, line: { color: ghost },
  });

  // Visual motif retained: accent dot top-left + vertical accent bar.
  addAccentDot(pres, slide, GRID.MARGIN_X, 0.60, 0.20, motif);
  slide.addShape(pres.shapes.RECTANGLE, {
    x: GRID.MARGIN_X, y: 1.92, w: 0.09, h: 2.18,
    fill: { color: motif }, line: { color: motif },
  });

  // WP5: KICKER overline (spec.kicker, else subject) — the premium tell.
  const kicker = spec.kicker || spec.subject;
  if (kicker) {
    slide.addText(String(kicker).toUpperCase(), {
      x: 0.95, y: 1.42, w: 7.6, h: 0.30,
      fontFace: fonts.body, fontSize: T.kicker, color: motif,
      bold: true, charSpacing: 4, valign: "mid", margin: 0,
    });
  }

  slide.addText(String(spec.title || ""), {
    x: 0.95, y: kicker ? 1.78 : 1.62, w: 7.7, h: 1.5,
    fontFace: fonts.header,
    fontSize: T.large_title,
    color: titleColor,
    bold: true,
    fit: "shrink",
    valign: "mid",
    charSpacing: 0.5,
  });

  if (spec.subtitle) {
    slide.addText(String(spec.subtitle), {
      x: 0.97, y: 3.30, w: 7.4, h: 0.6,
      fontFace: fonts.body, fontSize: T.subtitle, color: subColor,
      fit: "shrink", valign: "mid",
    });
  }

  if (spec.tagline) {
    slide.addText(String(spec.tagline), {
      x: 0.97, y: 4.00, w: 7.4, h: 0.5,
      fontFace: fonts.body, fontSize: T.support, color: subColor,
      italic: true, fit: "shrink", valign: "mid",
    });
  }

  // WP5: hairline rule + metadata row pinned to the bottom-left.
  if (spec.body || spec.author) {
    slide.addShape(pres.shapes.RECTANGLE, {
      x: 0.97, y: 4.78, w: 2.4, h: 0.014,
      fill: { color: subColor }, line: { color: subColor },
    });
    slide.addText(String(spec.body || spec.author), {
      x: 0.97, y: 4.86, w: 5.0, h: 0.32,
      fontFace: fonts.body, fontSize: T.caption, color: metaColor,
      charSpacing: 1,
    });
  }

  // Optional hero image — for launch / brand covers
  if (spec.image && typeof spec.image === "object") {
    placeSlideImage(slide, spec.image, theme, fonts);
  }
}

function renderToc(pres, slide, spec, theme, fonts, index, sectionTitles) {
  slide.background = { color: theme.bg };
  packContentBackdrop(pres, slide, theme);
  // Dot top-right so it doesn't collide with the left-anchored title
  addAccentDot(pres, slide, GRID.CONTENT_RIGHT - 0.18, 0.55, 0.18, theme.accent);
  addTitle(pres, slide, spec.title || "目录", theme, fonts, { kicker: spec.kicker });

  let items = (Array.isArray(spec.items) ? spec.items : (spec.bullets || []))
    .filter(Boolean).map(String).map(s => s.trim()).filter(Boolean);
  // Resilience: the model sometimes leaves the 目录 as scaffolding ("第一章 …",
  // "XXXX", bare leader dots). When ANY entry is filler — or the list is empty —
  // rebuild the TOC from the deck's actual `section` titles so 目录 always
  // mirrors the real chapters instead of rendering placeholders verbatim.
  const hasFiller = items.length === 0 || items.some(isFillerText);
  if (hasFiller && Array.isArray(sectionTitles) && sectionTitles.length) {
    items = sectionTitles.slice();
  } else if (hasFiller) {
    items = items.filter(t => !isFillerText(t));  // no sections to fall back to: drop fillers
  }
  if (items.length) {
    // TOC items sit directly on slide bg; adapt to bg darkness so chapter
    // titles aren't lost on dark-bg pages.
    const tocTc = textColorsForBg(theme);
    slide.addText(
      items.map((text, i) => ([
        { text: `${String(i + 1).padStart(2, "0")}`, options: { color: theme.accent, bold: true } },
        { text: `    ${text}`, options: { breakLine: true } },
      ])).flat(),
      {
        x: GRID.MARGIN_X + 0.23, y: GRID.CONTENT_TOP, w: GRID.CONTENT_W - 0.4, h: 3.4,
        fontFace: fonts.body,
        fontSize: STYLE.type.subtitle,
        color: tocTc.body,
        paraSpaceAfterPt: STYLE.density === "relaxed" ? 14 : 10,
        valign: "top",
      },
    );
  }
  addBadge(pres, slide, theme, index);
}

// True when a `content` slide carries no renderable body — no recognized
// layout fields, no real bullets/body, no image. Such a slide would otherwise
// fall through to contentSingle and render a blank page (the classic "最后一页
//是空的"). We route these to renderClosing instead so a trailing 谢谢/致谢 page
// (or any accidentally-empty slide) renders as a polished page, never blank.
function isEmptyContentSpec(spec) {
  if (!spec || typeof spec !== "object") return true;
  normalizeSpecKeys(spec);                                       // WP18: map layout-name keys → fields first
  if (spec.layout) return false;                                 // explicit layout → trust the model
  if (spec.image && typeof spec.image === "object") return false;
  if (typeof spec.body === "string" && !isFillerText(spec.body)) return false;
  if (spec.bigNumber || spec.metric || spec.hub || spec.swot) return false;
  if (spec.feature && typeof spec.feature === "object") return false;   // WP13 split_feature
  if (typeof spec.statement === "string" && spec.statement.trim()) return false;   // WP16
  if (spec.specimen && typeof spec.specimen === "object") return false;
  if (spec.table && typeof spec.table === "object" && Array.isArray(spec.table.rows) && spec.table.rows.length) return false;
  if (spec.callout && (typeof spec.callout === "string" ? spec.callout.trim() : spec.callout.text)) return false;   // WP17
  if (spec.lines && typeof spec.lines === "object" && Array.isArray(spec.lines.series) && spec.lines.series.length) return false;
  if (spec.groupedBars && typeof spec.groupedBars === "object" && Array.isArray(spec.groupedBars.series) && spec.groupedBars.series.length) return false;
  if (spec.ribbons && typeof spec.ribbons === "object" && Array.isArray(spec.ribbons.left) && spec.ribbons.left.length) return false;
  const arrayKeys = ["bento", "team", "testimonials", "logos", "kpis", "funnel",
    "roadmap", "spokes", "progress", "waterfall", "venn", "pros", "cons",
    "pillars", "cycle", "gantt", "quadrants", "flow", "pyramid", "rows", "items",
    "stats", "steps", "bars", "rings", "ledger", "leftBullets", "rightBullets",
    "highlights", "bullets",
    "panels", "cards", "milestones", "metrics", "points", "tiles", "journey", "commands",
    "donut", "trend", "gauges", "numbers", "defs", "vertical", "article", "pricing", "stack",
    "pie", "radar", "scatter", "deltas", "sparkcards", "statusList", "checklist", "calendar", "defCards", "numbered"];
  for (const k of arrayKeys) {
    const v = spec[k];
    if (Array.isArray(v) && v.some(x => x != null
        && (typeof x !== "string" || !isFillerText(x)))) return false;
  }
  if (spec.testimonial && typeof spec.testimonial === "object") return false;
  if (typeof spec.quote === "string" && !isFillerText(spec.quote)) return false;
  return true;
}

function renderSection(pres, slide, spec, theme, fonts, index) {
  if (SECTION_PACKS[PACK]) return SECTION_PACKS[PACK](pres, slide, spec, theme, fonts, index);
  // Section divider fills the slide with the theme's BRAND color (blue for a
  // blue theme), not a black slab — see brandBg().
  const SB = brandBg(theme);
  const onDark = isDarkHex(SB);
  const sText = onDark ? "FFFFFF" : "0A0A0A";
  const sSub = onDark ? tintJS(SB, 0.62) : shadeJS(SB, 0.45);

  // WP9: gradient anchor background (replaces the old flat full-bleed slab).
  slide.background = anchorBackground(theme, onDark ? "dark" : "light", SB);

  const halfBleed = spec.half_bleed !== false;
  if (halfBleed) {
    // Cohesive darker band of the same hue (not theme.secondary, which is
    // grey in Swiss palettes and clashes with a blue field).
    const band = onDark ? shadeJS(SB, 0.22) : tintJS(SB, 0.18);
    slide.addShape(pres.shapes.RECTANGLE, {
      x: 0, y: 0, w: 3.2, h: 5.625,
      fill: { color: band }, line: { color: band },
    });
    addAccentDot(pres, slide, 9.10, 0.62, 0.22, onDark ? "FFFFFF" : SB);
  } else {
    addAccentDot(pres, slide, 0.78, 0.55, 0.22, onDark ? "FFFFFF" : SB);
  }

  const titleLeft = halfBleed ? 3.55 : GRID.MARGIN_X;
  const titleWidth = halfBleed ? 5.9 : GRID.CONTENT_W;
  const T = STYLE.type;

  // Chapter number — explicit spec.number overrides the auto page index.
  const numText = spec.number != null ? String(spec.number) : String(index).padStart(2, "0");
  const numbered = String(spec.section_style || "").toLowerCase() === "numbered";
  if (numbered) {
    // WP11: bold, high-contrast chapter number as a deliberate design element
    // (vs. the faint watermark). On the half-bleed band it sits left of the
    // title; without a band it stacks above the title.
    slide.addText(numText, {
      x: halfBleed ? 0.15 : GRID.MARGIN_X, y: halfBleed ? 1.45 : 0.85,
      w: halfBleed ? 3.05 : 3.5, h: halfBleed ? 2.6 : 1.5,
      fontFace: fonts.header,
      fontSize: Math.round(T.large_title * (halfBleed ? 2.4 : 1.6)),
      color: onDark ? tintJS(SB, 0.70) : theme.accent,
      bold: true, align: halfBleed ? "center" : "left", valign: "mid", margin: 0,
    });
  } else {
    // WP5: giant faint section number — a subtle tonal shift of the bg itself.
    slide.addText(numText, {
      x: halfBleed ? 3.35 : GRID.MARGIN_X - 0.1, y: 0.55, w: 4.2, h: 3.0,
      fontFace: fonts.header,
      fontSize: Math.round(T.large_title * 3.0),
      color: onDark ? tintJS(SB, 0.16) : shadeJS(SB, 0.12),
      bold: true, align: "left", valign: "mid", margin: 0,
    });
  }

  if (spec.kicker) {
    slide.addText(String(spec.kicker).toUpperCase(), {
      x: titleLeft, y: 1.78, w: titleWidth, h: 0.30,
      fontFace: fonts.body, fontSize: T.kicker,
      color: onDark ? tintJS(SB, 0.55) : sText,
      bold: true, charSpacing: 4, margin: 0,
    });
  }

  slide.addText(String(spec.title || ""), {
    x: titleLeft, y: 2.06, w: titleWidth, h: 1.4,
    fontFace: fonts.header,
    fontSize: T.large_title,
    color: sText,
    bold: true,
    fit: "shrink",
    charSpacing: 1.5,  // letter-spacing for premium feel
  });

  if (spec.subtitle) {
    slide.addText(String(spec.subtitle), {
      x: titleLeft, y: 3.50, w: titleWidth, h: 0.6,
      fontFace: fonts.body,
      fontSize: T.support,
      color: sSub,
      fit: "shrink",
    });
  }

  addBadge(pres, slide, theme, index, { onDark });
  // Optional decorative image on section divider
  if (spec.image && typeof spec.image === "object") {
    placeSlideImage(slide, spec.image, theme, fonts);
  }
}

// True when a string is an empty/placeholder/filler entry rather than real
// content — e.g. "第一章 …", "XXXX", "待补充", bare leader dots "·····". Used to
// keep the TOC (and similar) from rendering the model's scaffolding verbatim.
function isFillerText(s) {
  const t = String(s == null ? "" : s).trim();
  if (!t) return true;
  if (/(待补充|待定|占位符?|placeholder|lorem ipsum|^lorem$|\bTBD\b|\bTODO\b|^n\/?a$)/i.test(t)) return true;
  if (/^x{2,}$/i.test(t.replace(/\s/g, ""))) return true;
  // Strip generic chapter scaffolding (序号/章节标记) and trailing leaders, then
  // see whether anything meaningful remains.
  const core = t
    .replace(/^[\s第]*[0-9一二三四五六七八九十]+\s*[、.\.\)）章节部分篇:：]*\s*/, "")
    .replace(/[…\.·•・\-_\s]+$/, "")
    .trim();
  if (!core) return true;                           // only scaffolding + leaders
  if (/^[…\.·•・\-_\s]+$/.test(core)) return true;  // only leader chars
  return false;
}

// Parse a human number out of strings like "29,738件" / "2.3万亿元" /
// "31.1%" → a comparable float (handles 万/亿 magnitudes). null if none.
function parseNumLike(v) {
  if (typeof v === "number") return v;
  const s = String(v == null ? "" : v).replace(/,/g, "");
  const m = s.match(/-?\d+(\.\d+)?/);
  if (!m) return null;
  let n = parseFloat(m[0]);
  if (/亿/.test(s)) n *= 1e8;
  else if (/万/.test(s)) n *= 1e4;
  return Number.isFinite(n) ? n : null;
}

// True when `items` is really a ranked DATA list — objects carrying
// value/percentage, no glyph/icon. The model frequently packs such data
// into `items`; without this it falls to a grid and the numbers vanish.
function itemsLookLikeData(items) {
  if (!Array.isArray(items) || !items.length) return false;
  let dataish = 0;
  for (const it of items) {
    if (!it || typeof it !== "object") return false;
    if (it.glyph || it.icon) return false;            // that's an icon list
    const hasVal = it.value != null && String(it.value).trim() !== "";
    const hasPct = Number.isFinite(Number(it.percentage));
    if (hasVal || hasPct) dataish += 1;
  }
  return dataish >= Math.max(1, Math.ceil(items.length * 0.6));
}

// WP18: the model very often uses a LAYOUT NAME as the data key (e.g.
// "horizontal_bars":[...] instead of "bars":[...], "kpi_ledger" instead of
// "ledger", "kpi_cards" instead of "kpis"). The field name and the layout name
// historically differ, so those slides silently rendered BLANK (detectLayout
// never matched the alias key → fell through to single → empty). Normalize:
// copy any layout-name key onto its canonical field when the field is absent.
const LAYOUT_KEY_ALIASES = {
  horizontal_bars: "bars", bar_chart_kpi: "bars", barchart: "bars",
  kpi_cards: "kpis", kpicards: "kpis", kpi_ledger: "ledger",
  stat_callout: "stats", statcallout: "stats", big_number: "bigNumber",
  icon_rows: "items", iconrows: "items", icon_cards: "tiles", iconcards: "tiles",
  roadmap_vertical: "roadmap", card_list: "cards", cardlist: "cards",
  compare_panels: "panels", split_feature: "feature", logo_wall: "logos",
  data_table: "table", datatable: "table", number_wall: "numbers", numberwall: "numbers",
  kpi_delta: "deltas", grouped_bars: "groupedBars", def_cards: "defCards",
  status_list: "statusList", hub_spoke: "hub", concentric: "rings",
  timeline: "steps", process: "flow", two_col: "leftBullets",
  // text aliases → bullets/body so a "single"/list slide is never lost
  points: "bullets", list: "bullets", content: "body", text: "body", description: "body",
};
function normalizeSpecKeys(spec) {
  if (!spec || typeof spec !== "object") return spec;
  for (const alias in LAYOUT_KEY_ALIASES) {
    const field = LAYOUT_KEY_ALIASES[alias];
    if (spec[alias] != null && (spec[field] == null
        || (Array.isArray(spec[field]) && spec[field].length === 0))) {
      spec[field] = spec[alias];
    }
  }
  return spec;
}

function detectLayout(spec) {
  if (spec.layout) {
    const L = String(spec.layout).toLowerCase();
    // explicit layout name might also be the data key (already normalized) —
    // trust it, but if the canonical data is missing fall through to detection.
    return L;
  }
  // ── WP16 enriched components (distinct spec keys → richer per-template vocab) ──
  if (Array.isArray(spec.donut) && spec.donut.length) return "donut";
  if (Array.isArray(spec.trend) && spec.trend.length) return "trend";
  if (Array.isArray(spec.gauges) && spec.gauges.length) return "gauges";
  if (Array.isArray(spec.numbers) && spec.numbers.length) return "number_wall";
  if (spec.table && typeof spec.table === "object" && Array.isArray(spec.table.rows) && spec.table.rows.length) return "data_table";
  if (typeof spec.statement === "string" && spec.statement.trim()) return "statement";
  if (spec.specimen && typeof spec.specimen === "object") return "specimen";
  if (Array.isArray(spec.defs) && spec.defs.length) return "defs";
  if (Array.isArray(spec.vertical) && spec.vertical.length) return "vertical";
  if (Array.isArray(spec.article) && spec.article.length) return "article";
  if (Array.isArray(spec.pricing) && spec.pricing.length) return "pricing";
  if (Array.isArray(spec.stack) && spec.stack.length) return "stack";
  // ── WP17 second wave ──
  if (Array.isArray(spec.pie) && spec.pie.length) return "pie";
  if (Array.isArray(spec.radar) && spec.radar.length) return "radar";
  if (Array.isArray(spec.scatter) && spec.scatter.length) return "scatter";
  if (spec.lines && typeof spec.lines === "object" && Array.isArray(spec.lines.series) && spec.lines.series.length) return "lines";
  if (spec.groupedBars && typeof spec.groupedBars === "object" && Array.isArray(spec.groupedBars.series) && spec.groupedBars.series.length) return "grouped_bars";
  if (Array.isArray(spec.deltas) && spec.deltas.length) return "kpi_delta";
  if (Array.isArray(spec.sparkcards) && spec.sparkcards.length) return "sparkcards";
  if (spec.ribbons && typeof spec.ribbons === "object" && Array.isArray(spec.ribbons.left)) return "ribbons";
  if (Array.isArray(spec.statusList) && spec.statusList.length) return "status_list";
  if (spec.callout && (typeof spec.callout === "string" ? spec.callout.trim() : spec.callout.text)) return "callout";
  if (Array.isArray(spec.checklist) && spec.checklist.length) return "checklist";
  if (Array.isArray(spec.calendar) && spec.calendar.length) return "calendar";
  if (Array.isArray(spec.defCards) && spec.defCards.length) return "def_cards";
  if (Array.isArray(spec.numbered) && spec.numbered.length) return "numbered";
  // ── WP7 new layouts (auto-detected from shape of the spec) ──
  if (Array.isArray(spec.bento) && spec.bento.length) return "bento";
  // ── WP11 people / social-proof / partner layouts ──
  if (Array.isArray(spec.team) && spec.team.length) return "team";
  if ((spec.testimonial && typeof spec.testimonial === "object")
      || (Array.isArray(spec.testimonials) && spec.testimonials.length)) return "testimonial";
  if (Array.isArray(spec.logos) && spec.logos.length) return "logo_wall";
  // WP13 composite / rich layouts
  if (spec.feature && typeof spec.feature === "object") return "split_feature";
  if (Array.isArray(spec.panels) && spec.panels.length) return "compare_panels";
  if (Array.isArray(spec.cards) && spec.cards.length) return "card_list";
  if (Array.isArray(spec.milestones) && spec.milestones.length) return "milestones";
  if (Array.isArray(spec.tiles) && spec.tiles.length) return "icon_cards";
  if (Array.isArray(spec.journey) && spec.journey.length) return "journey";
  if (Array.isArray(spec.commands) && spec.commands.length) return "commands";
  if (spec.quote) return "quote";
  if (spec.bigNumber || spec.metric) return "big_number";
  if (Array.isArray(spec.kpis) && spec.kpis.length) return "kpi_cards";
  if (Array.isArray(spec.funnel) && spec.funnel.length) return "funnel";
  if (Array.isArray(spec.roadmap) && spec.roadmap.length) return "roadmap_vertical";
  if ((spec.hub && Array.isArray(spec.hub.nodes) && spec.hub.nodes.length)
      || (Array.isArray(spec.spokes) && spec.spokes.length)) return "hub_spoke";
  if (Array.isArray(spec.progress) && spec.progress.length) return "progress";
  if (Array.isArray(spec.waterfall) && spec.waterfall.length) return "waterfall";
  if (Array.isArray(spec.venn) && spec.venn.length) return "venn";
  if (Array.isArray(spec.pros) || Array.isArray(spec.cons)) return "pros_cons";
  if (Array.isArray(spec.pillars) && spec.pillars.length) return "pillars";
  if (Array.isArray(spec.cycle) && spec.cycle.length) return "cycle";
  if (Array.isArray(spec.gantt) && spec.gantt.length) return "gantt";
  if (spec.swot && typeof spec.swot === "object") return "swot";
  if (Array.isArray(spec.quadrants) && spec.quadrants.length) return "matrix";
  if (Array.isArray(spec.flow) && spec.flow.length) return "process";
  if (Array.isArray(spec.pyramid) && spec.pyramid.length) return "pyramid";
  if (Array.isArray(spec.columns) && Array.isArray(spec.rows) && spec.rows.length) return "comparison_table";
  if (Array.isArray(spec.items) && spec.items.length && spec.items.every(it => it && typeof it === "object" && (it.glyph || it.icon))) {
    return "icon_rows";
  }
  if (Array.isArray(spec.stats) && spec.stats.length) return "stat_callout";
  if (Array.isArray(spec.steps) && spec.steps.length) return "timeline";
  // Auto-detect new Swiss-inspired layouts
  if (Array.isArray(spec.bars) && spec.bars.length) {
    return spec.bars.length <= 4 ? "bar_chart_kpi" : "horizontal_bars";
  }
  if (Array.isArray(spec.rings) && spec.rings.length) return "concentric";
  if (Array.isArray(spec.ledger) && spec.ledger.length) return "kpi_ledger";
  if (Array.isArray(spec.items) && spec.items.length) return "grid";
  if (Array.isArray(spec.leftBullets) || Array.isArray(spec.rightBullets)) return "two_col";
  if (Array.isArray(spec.highlights) && spec.highlights.length) return "highlights";
  return "single";
}

// ── WP16: enriched component library (extracted from the 23 open-source
// templates so each style can use its OWN component vocabulary, not one
// shared set). Data: donut · trend · gauges · number_wall · data_table.
// Style: statement · specimen · defs · vertical · article · pricing · stack.

function contentDonut(pres, slide, spec, theme, fonts, tc) {
  const d = (spec.donut || []).filter(x => x && typeof x === "object").slice(0, 6);
  if (!d.length) return;
  const labels = d.map(x => String(x.label || ""));
  const values = d.map(x => { const n = parseNumLike(x.value); return Number.isFinite(n) ? n : 0; });
  const colors = depthLadder(theme.accent, d.length, 0.5);
  slide.addChart(pres.charts.DOUGHNUT, [{ name: spec.seriesName || "", labels, values }], {
    x: 0.8, y: 1.65, w: 4.0, h: 3.0, holeSize: 60, chartColors: colors,
    showLegend: false, showValue: false, dataBorder: { pt: 1.5, color: theme.bg },
  });
  const lx = 5.3, lw = 4.0; let ly = 1.95; const rowH = Math.min(0.6, 3.0 / d.length);
  d.forEach((it, i) => {
    slide.addShape(pres.shapes.RECTANGLE, { x: lx, y: ly + 0.06, w: 0.22, h: 0.22, fill: { color: colors[i] }, line: { color: colors[i] } });
    slide.addText(String(it.label || ""), { x: lx + 0.34, y: ly, w: lw - 1.2, h: rowH, fontFace: fonts.body, fontSize: STYLE.type.support, color: tc.title, bold: true, valign: "mid", margin: 0, fit: "shrink" });
    slide.addText(String(it.value != null ? it.value : ""), { x: lx + lw - 1.0, y: ly, w: 1.0, h: rowH, fontFace: fonts.header, fontSize: STYLE.type.support, color: colors[i], bold: true, align: "right", valign: "mid", margin: 0 });
    ly += rowH;
  });
}

function contentTrend(pres, slide, spec, theme, fonts, tc) {
  const t = (spec.trend || []).filter(x => x && typeof x === "object").slice(0, 14);
  if (!t.length) return;
  const labels = t.map(x => String(x.label || ""));
  const values = t.map(x => { const n = parseNumLike(x.value); return Number.isFinite(n) ? n : 0; });
  slide.addChart(pres.charts.AREA, [{ name: spec.seriesName || "", labels, values }], {
    x: 0.72, y: 1.6, w: 8.56, h: 3.15, chartColors: [theme.accent], chartColorsOpacity: [30],
    lineSize: 2.5, lineSmooth: true, showLegend: false, showValue: false,
    catAxisLabelColor: tc.body, valAxisLabelColor: tc.body, catAxisLabelFontSize: 9, valAxisLabelFontSize: 9,
    valGridLine: { color: CARD.line, size: 0.5 }, catGridLine: { style: "none" },
    valAxisLineColor: CARD.line, catAxisLineColor: CARD.line,
  });
}

function contentGauges(pres, slide, spec, theme, fonts, tc) {
  const g = (spec.gauges || []).filter(x => x && typeof x === "object").slice(0, 4);
  const n = g.length; if (!n) return;
  const gap = GRID.GUTTER, w = (GRID.CONTENT_W - gap * (n - 1)) / n;
  const size = Math.min(2.3, w * 0.86);
  const ringBg = isDarkHex(theme.bg) ? tintJS(theme.bg, 0.16) : tintJS(theme.secondary, 0.80);
  g.forEach((it, i) => {
    const cx = GRID.MARGIN_X + i * (w + gap) + w / 2;
    const x = cx - size / 2, y = 1.65;
    let v = parseNumLike(it.value); if (!Number.isFinite(v)) v = 0; v = Math.max(0, Math.min(100, v));
    slide.addChart(pres.charts.DOUGHNUT, [{ name: "", labels: ["", ""], values: [v, 100 - v] }], {
      x, y, w: size, h: size, holeSize: 72, chartColors: [theme.accent, ringBg],
      showLegend: false, showValue: false, dataBorder: { pt: 0, color: theme.bg },
    });
    slide.addText(String(it.display != null ? it.display : Math.round(v)) + String(it.unit != null ? it.unit : "%"), {
      x, y: y + size * 0.30, w: size, h: size * 0.4, fontFace: fonts.header, fontSize: STYLE.type.title, color: tc.title, bold: true, align: "center", valign: "mid", margin: 0 });
    slide.addText(String(it.label || ""), { x: cx - w / 2, y: y + size + 0.08, w, h: 0.5, fontFace: fonts.body, fontSize: STYLE.type.support, color: tc.body, align: "center", valign: "top", margin: 0, fit: "shrink" });
  });
}

function contentNumberWall(pres, slide, spec, theme, fonts, tc) {
  const nums = (spec.numbers || []).filter(x => x && typeof x === "object").slice(0, 5);
  const n = nums.length; if (!n) return;
  const w = GRID.CONTENT_W / n; const y = 2.0;
  const ramp = depthLadder(theme.accent, n, 0.4);
  nums.forEach((it, i) => {
    const x = GRID.MARGIN_X + i * w;
    if (i > 0) slide.addShape(pres.shapes.RECTANGLE, { x, y: y + 0.1, w: 0.012, h: 1.7, fill: { color: CARD.line }, line: { color: CARD.line } });
    slide.addText(String(it.value != null ? it.value : ""), { x: x + 0.1, y, w: w - 0.2, h: 1.15, fontFace: fonts.header, fontSize: Math.round(STYLE.type.large_title * 0.95), color: ramp[i], bold: true, align: "center", valign: "mid", margin: 0, fit: "shrink" });
    slide.addText(String(it.label || ""), { x: x + 0.1, y: y + 1.2, w: w - 0.2, h: 0.7, fontFace: fonts.body, fontSize: STYLE.type.support, color: tc.body, align: "center", valign: "top", margin: 0, fit: "shrink" });
  });
}

function contentDataTable(pres, slide, spec, theme, fonts, tc) {
  const tb = spec.table || {};
  const headers = (tb.headers || tb.columns || []).map(String);
  const rows = (tb.rows || []).map(r => Array.isArray(r) ? r : [r]);
  if (!rows.length) return;
  const dark = isDarkHex(theme.bg);
  const STATUS = { green: "2E9E5B", ok: "2E9E5B", "on track": "2E9E5B", yellow: "E0A82E", warn: "E0A82E", at_risk: "E0A82E", red: "D7493A", bad: "D7493A", late: "D7493A" };
  const head = headers.map((h, ci) => ({ text: h, options: { fill: { color: theme.accent }, color: "FFFFFF", bold: true, align: ci === 0 ? "left" : "center" } }));
  const body = rows.map((r, ri) => {
    const fill = ri % 2 ? (dark ? tintJS(theme.bg, 0.11) : "F4F5F7") : CARD.fill;
    return r.map((c, ci) => {
      let txt = String(c); let col = ci === 0 ? CARD.title : tc.body;
      const sk = txt.toLowerCase();
      if (STATUS[sk]) { txt = "●"; col = STATUS[sk]; }
      return { text: txt, options: { fill: { color: fill }, color: col, align: ci === 0 ? "left" : "center", bold: ci === 0 } };
    });
  });
  const data = headers.length ? [head, ...body] : body;
  slide.addTable(data, {
    x: GRID.MARGIN_X, y: 1.62, w: GRID.CONTENT_W,
    border: { type: "solid", pt: 0.5, color: CARD.line },
    fontFace: fonts.body, fontSize: 12.5, valign: "mid",
    rowH: Math.min(0.5, 3.0 / Math.max(1, data.length)), autoPage: false,
  });
}

function contentStatement(pres, slide, spec, theme, fonts, tc) {
  const txt = String(spec.statement || spec.title || "");
  slide.addShape(pres.shapes.RECTANGLE, { x: GRID.MARGIN_X, y: 1.7, w: 0.7, h: 0.06, fill: { color: theme.accent }, line: { color: theme.accent } });
  slide.addText(txt, { x: GRID.MARGIN_X, y: 2.0, w: GRID.CONTENT_W, h: 2.2, fontFace: fonts.header, fontSize: Math.round(STYLE.type.large_title * 0.9), color: tc.title, bold: true, valign: "mid", margin: 0, charSpacing: 0.5, fit: "shrink" });
  if (spec.attribution || spec.author) slide.addText(String(spec.attribution || spec.author), { x: GRID.MARGIN_X, y: 4.35, w: GRID.CONTENT_W, h: 0.5, fontFace: fonts.body, fontSize: STYLE.type.support, color: tc.body, italic: true, margin: 0, fit: "shrink" });
}

function contentSpecimen(pres, slide, spec, theme, fonts, tc) {
  const sp = spec.specimen || {}; const glyph = String(sp.glyph || sp.text || "Aa");
  slide.addText(glyph, { x: 0.5, y: 1.3, w: 4.6, h: 3.4, fontFace: fonts.header, fontSize: Math.round(STYLE.type.hero * 1.4), color: theme.accent, bold: true, align: "center", valign: "mid", margin: 0, fit: "shrink" });
  const weights = (sp.weights || sp.samples || ["Aa 常规体", "Aa 加粗", "Aa 标题"]).slice(0, 4);
  const x = 5.3, w = 4.0; let y = 1.5; const rh = Math.min(0.86, 3.2 / weights.length);
  weights.forEach((wd, i) => {
    slide.addText(String(wd), { x, y, w, h: rh, fontFace: fonts.header, fontSize: STYLE.type.subtitle, color: tc.title, bold: i === weights.length - 1, valign: "mid", margin: 0, fit: "shrink" });
    slide.addShape(pres.shapes.RECTANGLE, { x, y: y + rh - 0.02, w, h: 0.01, fill: { color: CARD.line }, line: { color: CARD.line } });
    y += rh;
  });
  if (sp.note) slide.addText(String(sp.note), { x, y: y + 0.1, w, h: 0.5, fontFace: fonts.body, fontSize: STYLE.type.caption, color: tc.body, italic: true, margin: 0, fit: "shrink" });
}

function contentDefs(pres, slide, spec, theme, fonts, tc) {
  const defs = (spec.defs || []).filter(x => x && typeof x === "object").slice(0, 8);
  if (!defs.length) return;
  let y = GRID.CONTENT_TOP; const rh = Math.min(0.62, (GRID.CONTENT_BOTTOM - GRID.CONTENT_TOP) / defs.length);
  defs.forEach((it) => {
    slide.addText(String(it.term || it.label || ""), { x: GRID.MARGIN_X, y, w: 3.4, h: rh, fontFace: fonts.header, fontSize: STYLE.type.support, color: tc.title, bold: true, valign: "mid", margin: 0, fit: "shrink" });
    slide.addText(String(it.value || it.desc || ""), { x: GRID.MARGIN_X + 3.6, y, w: GRID.CONTENT_W - 3.6, h: rh, fontFace: fonts.body, fontSize: STYLE.type.support, color: tc.body, valign: "mid", margin: 0, fit: "shrink" });
    slide.addShape(pres.shapes.RECTANGLE, { x: GRID.MARGIN_X, y: y + rh - 0.01, w: GRID.CONTENT_W, h: 0.01, fill: { color: CARD.line }, line: { color: CARD.line } });
    y += rh;
  });
}

function contentVertical(pres, slide, spec, theme, fonts, tc) {
  const lines = (spec.vertical || []).map(String).filter(Boolean).slice(0, 4);
  if (!lines.length) return;
  const colW = 1.0, gap = 0.5;
  let x = GRID.CONTENT_RIGHT - colW;  // right-to-left columns
  lines.forEach((ln, i) => {
    const rt = [...ln].map(c => ({ text: c, options: { breakLine: true } }));
    slide.addText(rt, { x, y: 1.4, w: colW, h: 3.4, fontFace: fonts.header, fontSize: STYLE.type.subtitle, color: i === 0 ? theme.accent : tc.title, bold: i === 0, align: "center", valign: "top", lineSpacingMultiple: 1.0, margin: 0 });
    x -= (colW + gap);
  });
  slide.addShape(pres.shapes.RECTANGLE, { x: GRID.MARGIN_X, y: 4.25, w: 0.5, h: 0.5, fill: { color: theme.accent }, line: { color: theme.accent } });
  slide.addText(String(spec.seal || "印"), { x: GRID.MARGIN_X, y: 4.25, w: 0.5, h: 0.5, fontFace: fonts.header, fontSize: STYLE.type.support, color: theme.bg, bold: true, align: "center", valign: "mid", margin: 0 });
}

function contentArticle(pres, slide, spec, theme, fonts, tc) {
  const cols = (spec.article || []).map(String).filter(Boolean).slice(0, 3);
  if (!cols.length) return;
  const n = cols.length; const gap = 0.5; const w = (GRID.CONTENT_W - gap * (n - 1)) / n;
  cols.forEach((txt, i) => {
    const x = GRID.MARGIN_X + i * (w + gap);
    if (i > 0) slide.addShape(pres.shapes.RECTANGLE, { x: x - gap / 2, y: GRID.CONTENT_TOP, w: 0.01, h: 3.3, fill: { color: CARD.line }, line: { color: CARD.line } });
    slide.addText(txt, { x, y: GRID.CONTENT_TOP, w, h: 3.3, fontFace: fonts.body, fontSize: STYLE.type.body, color: tc.body, align: "left", valign: "top", lineSpacingMultiple: 1.12, paraSpaceAfterPt: 6, margin: 0 });
  });
}

function contentPricing(pres, slide, spec, theme, fonts, tc) {
  const tiers = (spec.pricing || []).filter(x => x && typeof x === "object").slice(0, 4);
  const n = tiers.length; if (!n) return;
  const gap = GRID.GUTTER; const w = (GRID.CONTENT_W - gap * (n - 1)) / n; const y = 1.55, h = 3.3;
  tiers.forEach((it, i) => {
    const x = GRID.MARGIN_X + i * (w + gap);
    const feat = it.featured === true || String(it.featured) === "true";
    const fill = feat ? theme.accent : CARD.fill;
    const titleC = feat ? "FFFFFF" : CARD.title;
    const bodyC = feat ? tintJS(theme.accent, 0.85) : CARD.body;
    slide.addShape(cardShapeType(pres), withShadow({ x, y, w, h, fill: { color: fill }, line: { color: feat ? theme.accent : CARD.line, pt: 0.75 }, rectRadius: STYLE.radius }, cardShadow()));
    slide.addText(String(it.plan || it.name || ""), { x: x + 0.18, y: y + 0.22, w: w - 0.36, h: 0.5, fontFace: fonts.header, fontSize: STYLE.type.support, color: titleC, bold: true, align: "center", margin: 0, fit: "shrink" });
    slide.addText(String(it.price || ""), { x: x + 0.18, y: y + 0.74, w: w - 0.36, h: 0.8, fontFace: fonts.header, fontSize: STYLE.type.title, color: feat ? "FFFFFF" : theme.accent, bold: true, align: "center", valign: "mid", margin: 0, fit: "shrink" });
    if (it.period) slide.addText(String(it.period), { x: x + 0.18, y: y + 1.52, w: w - 0.36, h: 0.3, fontFace: fonts.body, fontSize: STYLE.type.caption, color: bodyC, align: "center", margin: 0 });
    const fs = (it.features || []).map(String).slice(0, 5);
    if (fs.length) slide.addText(fs.map(t => ({ text: t, options: { bullet: { indent: 14 }, breakLine: true } })), { x: x + 0.24, y: y + 1.92, w: w - 0.48, h: h - 2.05, fontFace: fonts.body, fontSize: STYLE.type.caption, color: bodyC, valign: "top", paraSpaceAfterPt: 5, margin: 0 });
  });
}

function contentStack(pres, slide, spec, theme, fonts, tc) {
  const layers = (spec.stack || []).filter(x => x && typeof x === "object").slice(0, 5);
  const n = layers.length; if (!n) return;
  const gap = 0.16; const top = GRID.CONTENT_TOP; const h = (GRID.CONTENT_BOTTOM - top - gap * (n - 1)) / n;
  const ramp = depthLadder(theme.accent, n, 0.42);
  layers.forEach((it, i) => {
    const y = top + i * (h + gap);
    slide.addShape(cardShapeType(pres), { x: GRID.MARGIN_X, y, w: GRID.CONTENT_W, h, fill: { color: CARD.fill }, line: { color: CARD.line, pt: 0.75 }, rectRadius: STYLE.radius });
    slide.addShape(pres.shapes.RECTANGLE, { x: GRID.MARGIN_X, y, w: 0.12, h, fill: { color: ramp[i] }, line: { color: ramp[i] } });
    slide.addText(String(it.layer || it.title || ""), { x: GRID.MARGIN_X + 0.3, y, w: 2.6, h, fontFace: fonts.header, fontSize: STYLE.type.support, color: CARD.title, bold: true, valign: "mid", margin: 0, fit: "shrink" });
    const items = (it.items || []).map(String).slice(0, 4);
    if (items.length) {
      const ix = GRID.MARGIN_X + 3.0, iw = GRID.CONTENT_W - 3.0; const cw = iw / items.length;
      items.forEach((c, j) => {
        slide.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: ix + j * cw + 0.06, y: y + h * 0.2, w: cw - 0.12, h: h * 0.6, fill: { color: isDarkHex(theme.bg) ? tintJS(theme.bg, 0.14) : tintJS(ramp[i], 0.84) }, line: { color: CARD.line, pt: 0.5 }, rectRadius: 0.06 });
        slide.addText(c, { x: ix + j * cw + 0.06, y: y + h * 0.2, w: cw - 0.12, h: h * 0.6, fontFace: fonts.body, fontSize: STYLE.type.caption, color: CARD.title, align: "center", valign: "mid", margin: 0, fit: "shrink" });
      });
    }
  });
}

// ── WP17: second wave of components (charts + dashboard/editorial widgets)
// so a single deck can field 20+ distinct slide styles like the real decks.
// Charts: pie · radar · scatter · lines(multi) · grouped_bars. Widgets:
// kpi_delta · sparkcards · ribbons · status_list · callout · checklist ·
// calendar · def_cards · numbered.

function _chartLegend(pres, slide, items, colors, fonts, tc, x, w) {
  let y = 1.95; const rowH = Math.min(0.58, 2.9 / Math.max(1, items.length));
  items.forEach((it, i) => {
    slide.addShape(pres.shapes.RECTANGLE, { x, y: y + 0.06, w: 0.22, h: 0.22, fill: { color: colors[i % colors.length] }, line: { color: colors[i % colors.length] } });
    slide.addText(String(it.label || it.name || ""), { x: x + 0.34, y, w: w - 1.2, h: rowH, fontFace: fonts.body, fontSize: STYLE.type.support, color: tc.title, bold: true, valign: "mid", margin: 0, fit: "shrink" });
    if (it.value != null) slide.addText(String(it.value), { x: x + w - 1.0, y, w: 1.0, h: rowH, fontFace: fonts.header, fontSize: STYLE.type.support, color: colors[i % colors.length], bold: true, align: "right", valign: "mid", margin: 0 });
    y += rowH;
  });
}

function contentPie(pres, slide, spec, theme, fonts, tc) {
  const d = (spec.pie || []).filter(x => x && typeof x === "object").slice(0, 6); if (!d.length) return;
  const colors = depthLadder(theme.accent, d.length, 0.55);
  slide.addChart(pres.charts.PIE, [{ name: "", labels: d.map(x => String(x.label || "")), values: d.map(x => { const n = parseNumLike(x.value); return Number.isFinite(n) ? n : 0; }) }],
    { x: 0.8, y: 1.65, w: 4.0, h: 3.0, chartColors: colors, showLegend: false, showValue: true, dataLabelColor: "FFFFFF", dataLabelFontSize: 10, dataBorder: { pt: 1.5, color: theme.bg } });
  _chartLegend(pres, slide, d, colors, fonts, tc, 5.3, 4.0);
}

function contentRadar(pres, slide, spec, theme, fonts, tc) {
  const d = (spec.radar || []).filter(x => x && typeof x === "object").slice(0, 8); if (!d.length) return;
  slide.addChart(pres.charts.RADAR, [{ name: spec.seriesName || "", labels: d.map(x => String(x.label || "")), values: d.map(x => { const n = parseNumLike(x.value); return Number.isFinite(n) ? n : 0; }) }],
    { x: 1.8, y: 1.45, w: 6.4, h: 3.35, chartColors: [theme.accent], radarStyle: "filled",
      catAxisLabelColor: tc.body, catAxisLabelFontSize: 10, valAxisLabelColor: tc.body, showLegend: false });
}

function contentScatter(pres, slide, spec, theme, fonts, tc) {
  const pts = (spec.scatter || []).filter(x => x && typeof x === "object").slice(0, 40); if (!pts.length) return;
  const xs = pts.map(p => { const n = parseNumLike(p.x); return Number.isFinite(n) ? n : 0; });
  const ys = pts.map(p => { const n = parseNumLike(p.y); return Number.isFinite(n) ? n : 0; });
  slide.addChart(pres.charts.SCATTER, [{ name: "X", values: xs }, { name: spec.seriesName || "项", values: ys }],
    { x: 0.72, y: 1.55, w: 8.56, h: 3.15, chartColors: [theme.accent], lineSize: 0, lineDataSymbolSize: 8,
      catAxisLabelColor: tc.body, valAxisLabelColor: tc.body, valGridLine: { color: CARD.line, size: 0.5 }, showLegend: false });
}

function contentLines(pres, slide, spec, theme, fonts, tc) {
  const L = spec.lines || {}; const labels = (L.labels || []).map(String);
  const series = (L.series || []).filter(s => s && Array.isArray(s.values)).slice(0, 4); if (!series.length) return;
  const colors = [theme.accent, ...MULTI_ACCENTS];
  const data = series.map((s, i) => ({ name: s.name || ("系列" + (i + 1)), labels, values: s.values }));
  slide.addChart(pres.charts.LINE, data, {
    x: 0.72, y: 1.6, w: 8.56, h: 3.1, chartColors: colors, lineSize: 2.5, lineSmooth: true,
    showLegend: true, legendPos: "b", legendColor: tc.body, legendFontSize: 10, showValue: false,
    catAxisLabelColor: tc.body, valAxisLabelColor: tc.body, catAxisLabelFontSize: 9, valAxisLabelFontSize: 9,
    valGridLine: { color: CARD.line, size: 0.5 }, catGridLine: { style: "none" }, valAxisLineColor: CARD.line, catAxisLineColor: CARD.line,
  });
}

function contentGroupedBars(pres, slide, spec, theme, fonts, tc) {
  const G = spec.groupedBars || {}; const labels = (G.labels || []).map(String);
  const series = (G.series || []).filter(s => s && Array.isArray(s.values)).slice(0, 4); if (!series.length) return;
  const colors = [theme.accent, ...MULTI_ACCENTS];
  const data = series.map((s, i) => ({ name: s.name || ("系列" + (i + 1)), labels, values: s.values }));
  slide.addChart(pres.charts.BAR, data, {
    x: 0.72, y: 1.6, w: 8.56, h: 3.1, barDir: "col", barGrouping: "clustered", chartColors: colors,
    showLegend: true, legendPos: "b", legendColor: tc.body, legendFontSize: 10, showValue: false,
    catAxisLabelColor: tc.body, valAxisLabelColor: tc.body, valGridLine: { color: CARD.line, size: 0.5 }, catGridLine: { style: "none" },
    valAxisLineColor: CARD.line, catAxisLineColor: CARD.line,
  });
}

function contentKpiDelta(pres, slide, spec, theme, fonts, tc) {
  const ds = (spec.deltas || []).filter(x => x && typeof x === "object").slice(0, 4); const n = ds.length; if (!n) return;
  const gap = GRID.GUTTER, w = (GRID.CONTENT_W - gap * (n - 1)) / n; const y = 1.7, h = 2.7;
  ds.forEach((it, i) => {
    const x = GRID.MARGIN_X + i * (w + gap);
    slide.addShape(cardShapeType(pres), withShadow({ x, y, w, h, fill: { color: CARD.fill }, line: { color: CARD.line, pt: 0.75 }, rectRadius: STYLE.radius }, cardShadow()));
    slide.addText(String(it.value != null ? it.value : ""), { x: x + 0.16, y: y + 0.35, w: w - 0.32, h: 1.0, fontFace: fonts.header, fontSize: STYLE.type.title, color: theme.accent, bold: true, align: "center", valign: "mid", margin: 0, fit: "shrink" });
    if (it.change != null) {
      const up = String(it.dir || (parseNumLike(it.change) >= 0 ? "up" : "down")).toLowerCase() !== "down";
      const col = up ? "2E9E5B" : "D7493A";
      slide.addText((up ? "▲ " : "▼ ") + String(it.change), { x: x + 0.16, y: y + 1.35, w: w - 0.32, h: 0.4, fontFace: fonts.body, fontSize: STYLE.type.support, color: col, bold: true, align: "center", margin: 0 });
    }
    slide.addText(String(it.label || ""), { x: x + 0.16, y: y + h - 0.65, w: w - 0.32, h: 0.55, fontFace: fonts.body, fontSize: STYLE.type.support, color: CARD.body, align: "center", valign: "top", margin: 0, fit: "shrink" });
  });
}

function contentSparkcards(pres, slide, spec, theme, fonts, tc) {
  const sc = (spec.sparkcards || []).filter(x => x && typeof x === "object").slice(0, 4); const n = sc.length; if (!n) return;
  const gap = GRID.GUTTER, w = (GRID.CONTENT_W - gap * (n - 1)) / n; const y = 1.7, h = 2.8;
  sc.forEach((it, i) => {
    const x = GRID.MARGIN_X + i * (w + gap);
    slide.addShape(cardShapeType(pres), withShadow({ x, y, w, h, fill: { color: CARD.fill }, line: { color: CARD.line, pt: 0.75 }, rectRadius: STYLE.radius }, cardShadow()));
    slide.addText(String(it.value != null ? it.value : ""), { x: x + 0.18, y: y + 0.22, w: w - 0.36, h: 0.7, fontFace: fonts.header, fontSize: STYLE.type.title, color: theme.accent, bold: true, valign: "mid", margin: 0, fit: "shrink" });
    slide.addText(String(it.label || ""), { x: x + 0.18, y: y + 0.92, w: w - 0.36, h: 0.4, fontFace: fonts.body, fontSize: STYLE.type.caption, color: CARD.body, margin: 0, fit: "shrink" });
    const sp = (it.spark || []).map(v => { const n2 = parseNumLike(v); return Number.isFinite(n2) ? n2 : 0; });
    if (sp.length) slide.addChart(pres.charts.AREA, [{ name: "", labels: sp.map(() => ""), values: sp }],
      { x: x + 0.1, y: y + 1.45, w: w - 0.2, h: 1.1, chartColors: [theme.accent], chartColorsOpacity: [30], lineSize: 2, lineSmooth: true,
        showLegend: false, showValue: false, catAxisHidden: true, valAxisHidden: true, catGridLine: { style: "none" }, valGridLine: { style: "none" } });
  });
}

function contentRibbons(pres, slide, spec, theme, fonts, tc) {
  const R = spec.ribbons || {}; const left = (R.left || []).map(String).slice(0, 5); const right = (R.right || []).map(String).slice(0, 5);
  if (!left.length || !right.length) return;
  const lx = GRID.MARGIN_X, rx = GRID.CONTENT_RIGHT - 2.4, bw = 2.4; const top = 1.6;
  const lh = (3.3 - 0.2) / left.length, rh = (3.3 - 0.2) / right.length;
  const ramp = depthLadder(theme.accent, left.length, 0.4);
  // connectors first (behind boxes)
  left.forEach((_, i) => right.forEach((_, j) => {
    const y1 = top + i * (lh + 0.05) + lh / 2, y2 = top + j * (rh + 0.05) + rh / 2;
    slide.addShape(pres.shapes.LINE, { x: lx + bw, y: y1, w: (rx - lx - bw), h: y2 - y1, line: { color: tintJS(theme.accent, 0.62), width: 0.75 } });
  }));
  left.forEach((t, i) => {
    const y = top + i * (lh + 0.05);
    slide.addShape(cardShapeType(pres), { x: lx, y, w: bw, h: lh, fill: { color: ramp[i] }, line: { color: ramp[i] }, rectRadius: STYLE.radius });
    slide.addText(t, { x: lx, y, w: bw, h: lh, fontFace: fonts.body, fontSize: STYLE.type.caption, color: "FFFFFF", bold: true, align: "center", valign: "mid", margin: 0, fit: "shrink" });
  });
  right.forEach((t, j) => {
    const y = top + j * (rh + 0.05);
    slide.addShape(cardShapeType(pres), { x: rx, y, w: bw, h: rh, fill: { color: CARD.fill }, line: { color: CARD.line, pt: 0.75 }, rectRadius: STYLE.radius });
    slide.addText(t, { x: rx, y, w: bw, h: rh, fontFace: fonts.body, fontSize: STYLE.type.caption, color: CARD.title, bold: true, align: "center", valign: "mid", margin: 0, fit: "shrink" });
  });
}

function contentStatusList(pres, slide, spec, theme, fonts, tc) {
  const sl = (spec.statusList || []).filter(x => x && typeof x === "object").slice(0, 6); if (!sl.length) return;
  const STAT = { green: "2E9E5B", ok: "2E9E5B", yellow: "E0A82E", warn: "E0A82E", red: "D7493A", bad: "D7493A" };
  let y = GRID.CONTENT_TOP; const rh = Math.min(0.66, (GRID.CONTENT_BOTTOM - GRID.CONTENT_TOP) / sl.length);
  sl.forEach((it) => {
    slide.addShape(cardShapeType(pres), { x: GRID.MARGIN_X, y, w: GRID.CONTENT_W, h: rh - 0.1, fill: { color: CARD.fill }, line: { color: CARD.line, pt: 0.5 }, rectRadius: STYLE.radius });
    slide.addText(String(it.label || ""), { x: GRID.MARGIN_X + 0.2, y, w: 5.0, h: rh - 0.1, fontFace: fonts.header, fontSize: STYLE.type.support, color: CARD.title, bold: true, valign: "mid", margin: 0, fit: "shrink" });
    if (it.value != null) slide.addText(String(it.value), { x: GRID.MARGIN_X + 5.2, y, w: 2.0, h: rh - 0.1, fontFace: fonts.body, fontSize: STYLE.type.support, color: CARD.body, valign: "mid", margin: 0, fit: "shrink" });
    const sk = String(it.status || "").toLowerCase(); const col = STAT[sk] || theme.accent;
    slide.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: GRID.CONTENT_RIGHT - 1.5, y: y + (rh - 0.1) / 2 - 0.16, w: 1.4, h: 0.32, fill: { color: tintJS(col, isDarkHex(theme.bg) ? 0.0 : 0.82) }, line: { color: col, pt: 0.75 }, rectRadius: 0.16 });
    slide.addText(String(it.status || ""), { x: GRID.CONTENT_RIGHT - 1.5, y: y + (rh - 0.1) / 2 - 0.16, w: 1.4, h: 0.32, fontFace: fonts.body, fontSize: STYLE.type.caption, color: isDarkHex(theme.bg) ? "FFFFFF" : col, bold: true, align: "center", valign: "mid", margin: 0, fit: "shrink" });
    y += rh;
  });
}

function contentCallout(pres, slide, spec, theme, fonts, tc) {
  const co = typeof spec.callout === "object" ? spec.callout : { text: String(spec.callout || "") };
  const y = 2.0, h = 1.9;
  slide.addShape(cardShapeType(pres), withShadow({ x: GRID.MARGIN_X, y, w: GRID.CONTENT_W, h, fill: { color: theme.accent }, line: { color: theme.accent }, rectRadius: STYLE.radius }, cardShadow()));
  slide.addText(String(co.text || ""), { x: GRID.MARGIN_X + 0.5, y: y + 0.3, w: GRID.CONTENT_W - 1.0, h: co.sub ? 0.9 : 1.3, fontFace: fonts.header, fontSize: STYLE.type.subtitle, color: "FFFFFF", bold: true, align: "center", valign: "mid", margin: 0, fit: "shrink" });
  if (co.sub) slide.addText(String(co.sub), { x: GRID.MARGIN_X + 0.5, y: y + 1.2, w: GRID.CONTENT_W - 1.0, h: 0.5, fontFace: fonts.body, fontSize: STYLE.type.support, color: tintJS(theme.accent, 0.85), align: "center", margin: 0, fit: "shrink" });
}

function contentChecklist(pres, slide, spec, theme, fonts, tc) {
  const ck = (spec.checklist || []).map(x => typeof x === "object" ? x : { text: String(x) }).slice(0, 8); if (!ck.length) return;
  const cols = ck.length > 4 ? 2 : 1; const per = Math.ceil(ck.length / cols);
  ck.forEach((it, i) => {
    const c = Math.floor(i / per), r = i % per;
    const x = GRID.MARGIN_X + c * (GRID.CONTENT_W / cols); const w = GRID.CONTENT_W / cols - 0.3;
    const y = GRID.CONTENT_TOP + r * 0.66;
    addIcon(pres, slide, { x: x + 0.05, y: y + 0.04, diameter: 0.34, color: theme.accent, name: "circle-check" });
    slide.addText(String(it.text || it.title || ""), { x: x + 0.55, y, w: w - 0.55, h: 0.6, fontFace: fonts.body, fontSize: STYLE.type.support, color: tc.body, valign: "mid", margin: 0, fit: "shrink" });
  });
}

function contentCalendar(pres, slide, spec, theme, fonts, tc) {
  const cal = (spec.calendar || []).filter(x => x && typeof x === "object").slice(0, 7); const n = cal.length; if (!n) return;
  const gap = 0.14, w = (GRID.CONTENT_W - gap * (n - 1)) / n; const top = GRID.CONTENT_TOP, h = GRID.CONTENT_BOTTOM - top;
  cal.forEach((it, i) => {
    const x = GRID.MARGIN_X + i * (w + gap);
    slide.addShape(pres.shapes.RECTANGLE, { x, y: top, w, h: 0.5, fill: { color: theme.accent }, line: { color: theme.accent } });
    slide.addText(String(it.day || it.label || ""), { x, y: top, w, h: 0.5, fontFace: fonts.header, fontSize: STYLE.type.caption, color: "FFFFFF", bold: true, align: "center", valign: "mid", margin: 0, fit: "shrink" });
    slide.addShape(pres.shapes.RECTANGLE, { x, y: top + 0.5, w, h: h - 0.5, fill: { color: CARD.fill }, line: { color: CARD.line, pt: 0.5 } });
    const items = (it.items || []).map(String).slice(0, 5);
    if (items.length) slide.addText(items.map(t => ({ text: t, options: { breakLine: true } })), { x: x + 0.08, y: top + 0.6, w: w - 0.16, h: h - 0.7, fontFace: fonts.body, fontSize: STYLE.type.caption, color: tc.body, valign: "top", paraSpaceAfterPt: 4, margin: 0 });
  });
}

function contentDefCards(pres, slide, spec, theme, fonts, tc) {
  const dc = (spec.defCards || []).filter(x => x && typeof x === "object").slice(0, 6); const n = dc.length; if (!n) return;
  const cols = n <= 2 ? n : (n <= 4 ? 2 : 3); const rows = Math.ceil(n / cols);
  const gap = GRID.GUTTER; const w = (GRID.CONTENT_W - gap * (cols - 1)) / cols;
  const availH = GRID.CONTENT_BOTTOM - GRID.CONTENT_TOP; const h = (availH - gap * (rows - 1)) / rows;
  const ramp = depthLadder(theme.accent, n, 0.45);
  dc.forEach((it, i) => {
    const r = Math.floor(i / cols), c = i % cols;
    const x = GRID.MARGIN_X + c * (w + gap), y = GRID.CONTENT_TOP + r * (h + gap);
    slide.addShape(cardShapeType(pres), withShadow({ x, y, w, h, fill: { color: CARD.fill }, line: { color: CARD.line, pt: 0.75 }, rectRadius: STYLE.radius }, cardShadow()));
    slide.addShape(pres.shapes.RECTANGLE, { x, y, w: 0.1, h, fill: { color: ramp[i] }, line: { color: ramp[i] } });
    slide.addText(String(it.term || it.title || ""), { x: x + 0.24, y: y + 0.16, w: w - 0.4, h: 0.5, fontFace: fonts.header, fontSize: STYLE.type.support, color: CARD.title, bold: true, margin: 0, fit: "shrink" });
    slide.addText(String(it.desc || it.value || ""), { x: x + 0.24, y: y + 0.66, w: w - 0.4, h: h - 0.8, fontFace: fonts.body, fontSize: STYLE.type.caption, color: CARD.body, valign: "top", margin: 0, fit: "shrink" });
  });
}

function contentNumbered(pres, slide, spec, theme, fonts, tc) {
  const nb = (spec.numbered || []).filter(x => x && typeof x === "object").slice(0, 4); const n = nb.length; if (!n) return;
  const gap = GRID.GUTTER, w = (GRID.CONTENT_W - gap * (n - 1)) / n; const top = GRID.CONTENT_TOP;
  const ramp = depthLadder(theme.accent, n, 0.4);
  nb.forEach((it, i) => {
    const x = GRID.MARGIN_X + i * (w + gap);
    slide.addText(String(it.number || String(i + 1).padStart(2, "0")), { x, y: top, w, h: 1.0, fontFace: fonts.header, fontSize: Math.round(STYLE.type.large_title * 0.95), color: ramp[i], bold: true, align: "left", valign: "mid", margin: 0 });
    slide.addShape(pres.shapes.RECTANGLE, { x, y: top + 1.05, w: 0.5, h: 0.05, fill: { color: ramp[i] }, line: { color: ramp[i] } });
    slide.addText(String(it.title || ""), { x, y: top + 1.2, w, h: 0.6, fontFace: fonts.header, fontSize: STYLE.type.subtitle, color: tc.title, bold: true, valign: "top", margin: 0, fit: "shrink" });
    if (it.desc) slide.addText(String(it.desc), { x, y: top + 1.9, w, h: 1.2, fontFace: fonts.body, fontSize: STYLE.type.support, color: tc.body, valign: "top", margin: 0, fit: "shrink" });
  });
}

function renderContent(pres, slide, spec, theme, fonts, index) {
  slide.background = { color: theme.bg };
  packContentBackdrop(pres, slide, theme);
  normalizeSpecKeys(spec);   // WP18: accept layout-name keys (horizontal_bars→bars …) so slides never blank
  // Text colors for elements drawn directly on the slide bg — flipped to
  // light when bg is dark (dark-bg pages) so titles aren't lost.
  const tc = textColorsForBg(theme);
  addTitle(pres, slide, spec.title || "", theme, fonts, { color: tc.title, kicker: spec.kicker });

  // ── Image handling ───────────────────────────────────────────────
  // When an image is provided, the text region adapts:
  //   - right/left slot: bullets only, on the opposite side
  //   - below_title/hero/full: image dominates, other content suppressed
  //   - explicit x/y/w/h: image is placed but layout runs normally
  //     (LLM is responsible for non-overlap)
  const imgSpec = spec.image && typeof spec.image === "object" ? spec.image : null;
  const imgRegion = placeSlideImage(slide, imgSpec, theme, fonts);
  const sideRegion = sideImageTextRegion(imgSpec);
  const dominantImage = imgSpec && (
    String(imgSpec.slot || "").toLowerCase() === "below_title" ||
    String(imgSpec.slot || "").toLowerCase() === "hero" ||
    String(imgSpec.slot || "").toLowerCase() === "full"
  );

  let layout = detectLayout(spec);

  // Robustness: the model often packs ranked data into `items` as
  // {title,value,percentage} with no glyph/desc. detectLayout calls that a
  // grid → cards render with an empty body (the value is silently dropped,
  // and grid caps at 6 so extra rows vanish too). Re-route genuine data
  // rows to a bar chart so every number actually shows.
  if ((layout === "grid" || layout === "icon_rows") && itemsLookLikeData(spec.items)) {
    const src = spec.items;
    const nums = src.map(it => parseNumLike(it.value));
    const maxN = Math.max(0, ...nums.filter(n => Number.isFinite(n)));
    spec = Object.assign({}, spec, {
      bars: src.map((it, i) => {
        let pct = Number(it.percentage);
        if (!Number.isFinite(pct)) {
          pct = (maxN > 0 && Number.isFinite(nums[i]))
            ? Math.round((nums[i] / maxN) * 100) : null;
        }
        const o = {
          label: it.title || it.label || it.name || "",
          value: it.value != null ? String(it.value) : "",
        };
        if (Number.isFinite(pct)) o.percentage = pct;
        if (it.desc || it.description) o.desc = it.desc || it.description;
        return o;
      }),
    });
    layout = spec.bars.length <= 4 ? "bar_chart_kpi" : "horizontal_bars";
  }

  // stat_callout is the big-hero-number layout for ≤3 metrics. When the
  // model supplies 4+ stats, don't silently drop the extras — route to the
  // kpi_cards wall (value/label/tagline map 1:1).
  if (layout === "stat_callout" && Array.isArray(spec.stats) && spec.stats.length > 3) {
    spec = Object.assign({}, spec, {
      kpis: spec.stats.map(s => ({
        value: s.value, label: s.label, desc: s.tagline || s.desc,
      })),
    });
    layout = "kpi_cards";
  }

  // SWOT is a 2×2 matrix with fixed quadrant semantics — normalize to the
  // matrix renderer so we don't duplicate layout code.
  if (layout === "swot") {
    const s = spec.swot || {};
    const pick = (k) => Array.isArray(s[k]) ? s[k].join("、") : (s[k] || "");
    spec = Object.assign({}, spec, {
      quadrants: [
        { title: "优势 Strengths", desc: pick("strengths") || pick("S") },
        { title: "劣势 Weaknesses", desc: pick("weaknesses") || pick("W") },
        { title: "机会 Opportunities", desc: pick("opportunities") || pick("O") },
        { title: "威胁 Threats", desc: pick("threats") || pick("T") },
      ],
    });
    layout = "matrix";
  }

  if (sideRegion) {
    // Force a compact single-column bullets layout on the opposite side.
    layout = "side_image";
    addBullets(slide, spec.bullets, sideRegion, theme, fonts, { fontSize: 16, color: tc.body });
  } else if (dominantImage) {
    // Image-dominant slides skip body layout entirely.
    layout = "image_only";
  } else {
    if (layout === "pie") contentPie(pres, slide, spec, theme, fonts, tc);
    else if (layout === "radar") contentRadar(pres, slide, spec, theme, fonts, tc);
    else if (layout === "scatter") contentScatter(pres, slide, spec, theme, fonts, tc);
    else if (layout === "lines") contentLines(pres, slide, spec, theme, fonts, tc);
    else if (layout === "grouped_bars") contentGroupedBars(pres, slide, spec, theme, fonts, tc);
    else if (layout === "kpi_delta") contentKpiDelta(pres, slide, spec, theme, fonts, tc);
    else if (layout === "sparkcards") contentSparkcards(pres, slide, spec, theme, fonts, tc);
    else if (layout === "ribbons") contentRibbons(pres, slide, spec, theme, fonts, tc);
    else if (layout === "status_list") contentStatusList(pres, slide, spec, theme, fonts, tc);
    else if (layout === "callout") contentCallout(pres, slide, spec, theme, fonts, tc);
    else if (layout === "checklist") contentChecklist(pres, slide, spec, theme, fonts, tc);
    else if (layout === "calendar") contentCalendar(pres, slide, spec, theme, fonts, tc);
    else if (layout === "def_cards") contentDefCards(pres, slide, spec, theme, fonts, tc);
    else if (layout === "numbered") contentNumbered(pres, slide, spec, theme, fonts, tc);
    else if (layout === "donut") contentDonut(pres, slide, spec, theme, fonts, tc);
    else if (layout === "trend") contentTrend(pres, slide, spec, theme, fonts, tc);
    else if (layout === "gauges") contentGauges(pres, slide, spec, theme, fonts, tc);
    else if (layout === "number_wall") contentNumberWall(pres, slide, spec, theme, fonts, tc);
    else if (layout === "data_table") contentDataTable(pres, slide, spec, theme, fonts, tc);
    else if (layout === "statement") contentStatement(pres, slide, spec, theme, fonts, tc);
    else if (layout === "specimen") contentSpecimen(pres, slide, spec, theme, fonts, tc);
    else if (layout === "defs") contentDefs(pres, slide, spec, theme, fonts, tc);
    else if (layout === "vertical") contentVertical(pres, slide, spec, theme, fonts, tc);
    else if (layout === "article") contentArticle(pres, slide, spec, theme, fonts, tc);
    else if (layout === "pricing") contentPricing(pres, slide, spec, theme, fonts, tc);
    else if (layout === "stack") contentStack(pres, slide, spec, theme, fonts, tc);
    else if (layout === "bento") contentBento(pres, slide, spec, theme, fonts, tc);
    else if (layout === "icon_rows") contentIconRows(pres, slide, spec, theme, fonts, tc);
    else if (layout === "stat_callout") contentStatCallout(pres, slide, spec, theme, fonts, tc);
    else if (layout === "grid") contentGrid(pres, slide, spec, theme, fonts, tc);
    else if (layout === "timeline") contentTimeline(pres, slide, spec, theme, fonts, tc);
    else if (layout === "two_col") contentTwoCol(pres, slide, spec, theme, fonts, tc);
    else if (layout === "highlights") contentHighlights(pres, slide, spec, theme, fonts, tc);
    else if (layout === "bar_chart_kpi") contentBarChartKpi(pres, slide, spec, theme, fonts, tc);
    else if (layout === "horizontal_bars") contentHorizontalBars(pres, slide, spec, theme, fonts, tc);
    else if (layout === "concentric") contentConcentric(pres, slide, spec, theme, fonts, tc);
    else if (layout === "kpi_ledger") contentKpiLedger(pres, slide, spec, theme, fonts, tc);
    else if (layout === "quote") contentQuote(pres, slide, spec, theme, fonts, tc);
    else if (layout === "big_number") contentBigNumber(pres, slide, spec, theme, fonts, tc);
    else if (layout === "matrix") contentMatrix(pres, slide, spec, theme, fonts, tc);
    else if (layout === "kpi_cards") contentKpiCards(pres, slide, spec, theme, fonts, tc);
    else if (layout === "funnel") contentFunnel(pres, slide, spec, theme, fonts, tc);
    else if (layout === "hub_spoke") contentHubSpoke(pres, slide, spec, theme, fonts, tc);
    else if (layout === "roadmap_vertical") contentRoadmapVertical(pres, slide, spec, theme, fonts, tc);
    else if (layout === "progress") contentProgress(pres, slide, spec, theme, fonts, tc);
    else if (layout === "waterfall") contentWaterfall(pres, slide, spec, theme, fonts, tc);
    else if (layout === "venn") contentVenn(pres, slide, spec, theme, fonts, tc);
    else if (layout === "pros_cons") contentProsCons(pres, slide, spec, theme, fonts, tc);
    else if (layout === "pillars") contentPillars(pres, slide, spec, theme, fonts, tc);
    else if (layout === "cycle") contentCycle(pres, slide, spec, theme, fonts, tc);
    else if (layout === "gantt") contentGantt(pres, slide, spec, theme, fonts, tc);
    else if (layout === "process") contentProcess(pres, slide, spec, theme, fonts, tc);
    else if (layout === "comparison_table") contentComparisonTable(pres, slide, spec, theme, fonts, tc);
    else if (layout === "pyramid") contentPyramid(pres, slide, spec, theme, fonts, tc);
    else if (layout === "team") contentTeam(pres, slide, spec, theme, fonts, tc);
    else if (layout === "testimonial") contentTestimonial(pres, slide, spec, theme, fonts, tc);
    else if (layout === "logo_wall") contentLogoWall(pres, slide, spec, theme, fonts, tc);
    else if (layout === "split_feature") contentSplitFeature(pres, slide, spec, theme, fonts, tc);
    else if (layout === "compare_panels") contentComparePanels(pres, slide, spec, theme, fonts, tc);
    else if (layout === "card_list") contentCardList(pres, slide, spec, theme, fonts, tc);
    else if (layout === "milestones") contentMilestones(pres, slide, spec, theme, fonts, tc);
    else if (layout === "icon_cards") contentIconCards(pres, slide, spec, theme, fonts, tc);
    else if (layout === "journey") contentJourney(pres, slide, spec, theme, fonts, tc);
    else if (layout === "commands") contentCommands(pres, slide, spec, theme, fonts, tc);
    else contentSingle(pres, slide, spec, theme, fonts, tc);
  }

  const SUPPRESS_BODY = new Set(["bento", "highlights", "stat_callout", "bar_chart_kpi", "horizontal_bars", "concentric", "kpi_ledger", "side_image", "image_only", "quote", "big_number", "matrix", "process", "comparison_table", "pyramid", "kpi_cards", "funnel", "hub_spoke", "roadmap_vertical", "progress", "waterfall", "venn", "pros_cons", "pillars", "cycle", "gantt", "team", "testimonial", "logo_wall", "split_feature", "compare_panels", "card_list", "milestones", "icon_cards", "journey", "commands", "single", "donut", "trend", "gauges", "number_wall", "data_table", "statement", "specimen", "defs", "vertical", "article", "pricing", "stack", "pie", "radar", "scatter", "lines", "grouped_bars", "kpi_delta", "sparkcards", "ribbons", "status_list", "callout", "checklist", "calendar", "def_cards", "numbered"]);
  if (spec.body && !SUPPRESS_BODY.has(layout)) {
    slide.addText(String(spec.body), {
      x: GRID.MARGIN_X, y: 4.86, w: GRID.CONTENT_W, h: 0.38,
      fontFace: fonts.body,
      fontSize: STYLE.type.caption,
      color: tc.body,
      margin: 0,
      italic: true,
      fit: "shrink",
    });
  }

  addBadge(pres, slide, theme, index);
  return layout;
}

function contentSingle(pres, slide, spec, theme, fonts, tc) {
  tc = tc || textColorsForBg(theme);
  const box = { x: GRID.MARGIN_X, y: GRID.CONTENT_TOP, w: GRID.CONTENT_W, h: 3.2 };
  // WP18: universal fallback — never render a title-only blank page. Pull bullets
  // from any reasonable source; if there are none, render `body` as a paragraph;
  // last resort, surface any array of objects as readable bullet lines.
  let bullets = Array.isArray(spec.bullets) ? spec.bullets.filter(Boolean).map(String) : [];
  if (!bullets.length && Array.isArray(spec.items)) {
    bullets = spec.items.map(it => typeof it === "object"
      ? [it.title, it.label, it.name, it.desc, it.value].filter(Boolean).join("：")
      : String(it)).filter(Boolean);
  }
  if (!bullets.length) {
    // scan remaining array fields for something printable (objects → "k：v" lines)
    for (const k of Object.keys(spec)) {
      if (["type", "title", "kicker", "subtitle", "image", "layout"].includes(k)) continue;
      const v = spec[k];
      if (Array.isArray(v) && v.length) {
        bullets = v.map(it => typeof it === "object"
          ? Object.values(it).filter(x => x != null && typeof x !== "object").map(String).join("　")
          : String(it)).filter(Boolean);
        if (bullets.length) break;
      }
    }
  }
  if (bullets.length) {
    addBullets(slide, bullets, box, theme, fonts, { color: tc.body });
    return;
  }
  const body = spec.body || spec.summary || spec.note;
  if (body) {
    slide.addText(String(body), {
      ...box, fontFace: fonts.body, fontSize: STYLE.type.subtitle, color: tc.body,
      valign: "top", margin: 0, lineSpacingMultiple: 1.15, paraSpaceAfterPt: 8, fit: "shrink",
    });
  }
}

// bento — modern modular dashboard (WP10): one accent-filled "feature" tile on
// the left + supporting white tiles on the right. Reads as a designed
// dashboard, not a uniform grid. Each tile: optional icon, big value, title,
// short desc. Serves both data-report (headline KPI + metrics) and
// gov/enterprise (key message + supporting points).
// Spec: bento: [{title, value?, desc?, icon?}]  (item 0 = feature; 2-5 total)
function contentBento(pres, slide, spec, theme, fonts, tc) {
  tc = tc || textColorsForBg(theme);
  const items = (spec.bento || []).filter(Boolean).slice(0, 5)
    .map(it => (typeof it === "object" ? it : { title: String(it) }));
  const n = items.length;
  if (!n) return;
  const top = GRID.CONTENT_TOP;
  const H = GRID.CONTENT_BOTTOM - top;
  const gap = GRID.GUTTER;
  const featureW = n === 1 ? GRID.CONTENT_W : GRID.CONTENT_W * 0.46;
  const rightX = GRID.MARGIN_X + featureW + gap;
  const rightW = GRID.CONTENT_RIGHT - rightX;
  const pad = 0.26;

  // ── feature tile (accent-filled) ──
  const f = items[0];
  const featDark = isDarkHex(theme.accent);
  const featText = featDark ? "FFFFFF" : "0A0A0A";
  const featMuted = featDark ? tintJS(theme.accent, 0.70) : shadeJS(theme.accent, 0.45);
  slide.addShape(cardShapeType(pres), withShadow({
    x: GRID.MARGIN_X, y: top, w: featureW, h: H,
    fill: { color: theme.accent }, line: { color: theme.accent },
    rectRadius: STYLE.radius,
  }, cardShadow()));
  // A small "kicker" chip at the top of the feature tile (label/category)
  // instead of an icon badge — keeps the accent fill clean.
  if (f.kicker) {
    slide.addText(String(f.kicker).toUpperCase(), {
      x: GRID.MARGIN_X + pad, y: top + pad, w: featureW - pad * 2, h: 0.30,
      fontFace: fonts.body, fontSize: STYLE.type.kicker, color: featMuted,
      bold: true, charSpacing: 3, valign: "mid", margin: 0,
    });
  }
  const hasVal = f.value != null && String(f.value).trim() !== "";
  if (hasVal) {
    slide.addText(String(f.value), {
      x: GRID.MARGIN_X + pad, y: top + H * 0.30, w: featureW - pad * 2, h: 1.05,
      fontFace: fonts.header, fontSize: STYLE.type.large_title, color: featText,
      bold: true, valign: "mid", margin: 0, wrap: false, fit: "shrink",
    });
  }
  slide.addText(String(f.title || ""), {
    x: GRID.MARGIN_X + pad, y: top + (hasVal ? H * 0.30 + 1.05 : H * 0.30), w: featureW - pad * 2,
    h: hasVal ? 0.5 : 1.0,
    fontFace: fonts.header, fontSize: hasVal ? STYLE.type.subtitle : STYLE.type.title,
    color: featText, bold: true, valign: hasVal ? "top" : "mid", margin: 0, fit: "shrink",
  });
  if (f.desc) {
    slide.addText(String(f.desc), {
      x: GRID.MARGIN_X + pad, y: top + H - 1.05, w: featureW - pad * 2, h: 0.95,
      fontFace: fonts.body, fontSize: STYLE.type.support, color: featMuted,
      valign: "bottom", margin: 0, fit: "shrink",
    });
  }

  // ── supporting tiles (white cards on the right) ──
  const rest = items.slice(1);
  const m = rest.length;
  if (!m) return;
  const cols = m >= 4 ? 2 : 1;
  const rows = Math.ceil(m / cols);
  const cW = (rightW - gap * (cols - 1)) / cols;
  const cH = (H - gap * (rows - 1)) / rows;
  const onDarkBg = isDarkHex(theme.bg);
  const tileFill = onDarkBg ? tintJS(theme.bg, 0.08) : "FFFFFF";
  rest.forEach((it, i) => {
    const r = Math.floor(i / cols), c = i % cols;
    const x = rightX + c * (cW + gap), y = top + r * (cH + gap);
    slide.addShape(cardShapeType(pres), withShadow({
      x, y, w: cW, h: cH,
      fill: { color: tileFill },
      line: { color: shadeJS(theme.bg, 0.08), pt: 0.75 },
      rectRadius: STYLE.radius,
    }, cardShadow()));
    // Fit-aware compact layout: stack icon/value row → title → desc top-down
    // from a running cursor, and only draw desc when it actually fits inside
    // the card. Prevents text spilling into the next tile on short cards.
    const ic = it.icon || _inferIcon(`${it.title || ""} ${it.desc || ""}`);
    const hasIcon = ic && TABLER.iconPaths(ic);
    const tv = it.value != null && String(it.value).trim() !== "";
    const ipad = 0.18;
    const x0 = x + ipad;
    const innerW = cW - ipad * 2;
    const d = Math.min(0.40, cH * 0.30);
    const cardBottom = y + cH - ipad;
    let cy = y + ipad;
    if (hasIcon) {
      addIcon(pres, slide, { x: x0, y: cy, diameter: d, color: theme.accent, name: ic });
    }
    if (tv) {
      const vx = hasIcon ? x0 + d + 0.12 : x0;
      slide.addText(String(it.value), {
        x: vx, y: cy - 0.02, w: x + cW - ipad - vx, h: d + 0.04,
        fontFace: fonts.header, fontSize: STYLE.type.subtitle, color: theme.accent,
        bold: true, valign: "mid", margin: 0, wrap: false, fit: "shrink",
      });
    }
    if (hasIcon || tv) cy += d + 0.10;
    const titleH = 0.30;
    slide.addText(String(it.title || ""), {
      x: x0, y: cy, w: innerW, h: titleH,
      fontFace: fonts.header, fontSize: STYLE.type.support, color: tc.title,
      bold: true, valign: "mid", margin: 0, fit: "shrink",
    });
    cy += titleH + 0.02;
    if (it.desc) {
      const dh = cardBottom - cy;
      if (dh >= 0.18) {
        slide.addText(String(it.desc), {
          x: x0, y: cy, w: innerW, h: dh,
          fontFace: fonts.body, fontSize: STYLE.type.caption, color: tc.body,
          valign: "top", margin: 0, fit: "shrink",
        });
      }
    }
  });
}

function contentTwoCol(pres, slide, spec, theme, fonts, tc) {
  tc = tc || textColorsForBg(theme);
  const mid = 5.0;
  const colW = mid - GRID.MARGIN_X - 0.18;
  if (spec.leftTitle) {
    slide.addText(String(spec.leftTitle), {
      x: GRID.MARGIN_X, y: GRID.CONTENT_TOP - 0.05, w: colW, h: 0.45,
      fontFace: fonts.header, fontSize: STYLE.type.subtitle, color: tc.title, bold: true,
    });
  }
  if (spec.rightTitle) {
    slide.addText(String(spec.rightTitle), {
      x: mid + 0.18, y: GRID.CONTENT_TOP - 0.05, w: colW, h: 0.45,
      fontFace: fonts.header, fontSize: STYLE.type.subtitle, color: tc.title, bold: true,
    });
  }
  // WP4: hairline separator — neutral, not accent.
  slide.addShape(pres.shapes.RECTANGLE, {
    x: mid - 0.02, y: GRID.CONTENT_TOP + 0.10, w: 0.02, h: 2.9,
    fill: { color: shadeJS(theme.bg, 0.10) },
    line: { color: shadeJS(theme.bg, 0.10) },
  });
  addBullets(slide, spec.leftBullets,
    { x: GRID.MARGIN_X, y: GRID.CONTENT_TOP + 0.5, w: colW, h: 2.6 }, theme, fonts, { color: tc.body });
  addBullets(slide, spec.rightBullets,
    { x: mid + 0.18, y: GRID.CONTENT_TOP + 0.5, w: colW, h: 2.6 }, theme, fonts, { color: tc.body });
}

function contentHighlights(pres, slide, spec, theme, fonts, tc) {
  tc = tc || textColorsForBg(theme);
  addBullets(slide, spec.bullets,
    { x: GRID.MARGIN_X, y: GRID.CONTENT_TOP, w: GRID.CONTENT_W, h: 2.5 },
    theme, fonts, { color: tc.body });
  const hi = (spec.highlights || []).slice(0, 3).filter(Boolean).map(String);
  const n = hi.length || 1;
  const cardW = (GRID.CONTENT_W - GRID.GUTTER * (n - 1)) / n;
  hi.forEach((item, i) => {
    const x = GRID.MARGIN_X + i * (cardW + GRID.GUTTER);
    slide.addShape(pres.shapes.ROUNDED_RECTANGLE, withShadow({
      x, y: 4.10, w: cardW, h: 0.66,
      fill: { color: tintJS(theme.accent, 0.90) },
      line: { color: tintJS(theme.accent, 0.72), pt: 0.75 },
      rectRadius: STYLE.radius,
    }, cardShadow()));
    slide.addText(item, {
      x: x + 0.12, y: 4.20, w: cardW - 0.24, h: 0.46,
      fontFace: fonts.header,
      fontSize: STYLE.type.support,
      color: CARD.title,
      bold: true,
      fit: "shrink",
      align: "center",
      valign: "mid",
    });
  });
}

function contentIconRows(pres, slide, spec, theme, fonts, tc) {
  tc = tc || textColorsForBg(theme);
  const items = (spec.items || []).filter(it => it && typeof it === "object").slice(0, 5);
  if (!items.length) return;
  const areaTop = GRID.CONTENT_TOP + 0.05;
  const available = GRID.CONTENT_BOTTOM - areaTop;
  const gap = STYLE.density === "relaxed" ? 0.16 : 0.10;
  const rowH = Math.min(0.92, (available - gap * (items.length - 1)) / items.length);
  const iconDiam = Math.min(rowH - 0.06, 0.62);
  // Center the row block so a 3-item list doesn't cluster at the top with
  // a big empty bottom.
  const usedH = rowH * items.length + gap * (items.length - 1);
  const top = areaTop + Math.max(0, (available - usedH) / 2);

  items.forEach((it, i) => {
    const y = top + i * (rowH + gap);
    addIcon(pres, slide, {
      x: GRID.MARGIN_X, y, diameter: iconDiam,
      color: theme.accent,
      glyph: String(it.glyph || it.icon || "").slice(0, 2),
      name: it.icon || it.glyph,
      hint: `${it.title || ""} ${it.desc || it.description || ""}`,
    });
    const textLeft = GRID.MARGIN_X + iconDiam + 0.24;
    const textW = GRID.CONTENT_RIGHT - textLeft;
    slide.addText(String(it.title || ""), {
      x: textLeft, y: y - 0.02, w: textW, h: 0.42,
      fontFace: fonts.header, fontSize: STYLE.type.subtitle, bold: true, color: tc.title,
      fit: "shrink", margin: 0,
    });
    const desc = it.desc || it.description || it.subtitle
      || (it.value != null && String(it.value).trim() !== "" ? String(it.value) : "");
    if (desc) {
      slide.addText(String(desc), {
        x: textLeft, y: y + 0.38, w: textW, h: rowH - 0.38,
        fontFace: fonts.body, fontSize: STYLE.type.support, color: tc.body,
        margin: 0,
      });
    }
  });
}

function contentStatCallout(pres, slide, spec, theme, fonts, tc) {
  tc = tc || textColorsForBg(theme);
  const stats = (spec.stats || []).filter(s => s && typeof s === "object").slice(0, 3);
  if (!stats.length) return;
  const n = stats.length;
  // Widen the value strip — give every column max room so big numbers stay on one line.
  const baseX = GRID.MARGIN_X;
  const totalW = GRID.CONTENT_W;
  const colW = totalW / n;
  const colInset = 0.05;
  const valueW = colW - colInset * 2;
  // Vertically center the stat block in the body area so a 3-metric slide
  // doesn't leave a big empty band underneath.
  const hasLabel = stats.some(s => s.label);
  const hasTag = stats.some(s => s.tagline);
  const blockH = 1.40 + 0.16 + (hasLabel ? 0.46 : 0) + (hasTag ? 0.54 : 0);
  const top = GRID.CONTENT_TOP +
    Math.max(0, ((GRID.CONTENT_BOTTOM - GRID.CONTENT_TOP) - blockH) / 2);

  // Adaptive font size based on string length. Tuned so CJK-heavy values
  // (e.g. "320万台" = 5 chars) fit a single line — LibreOffice + pptxgenjs
  // do NOT reliably honor wrap:false for CJK, so we size down preemptively.
  function fontForValue(value) {
    const len = String(value || "").length;
    if (n === 1) {
      if (len <= 4) return 88;
      if (len <= 6) return 72;
      return 56;
    }
    if (n === 2) {
      if (len <= 3) return 76;
      if (len <= 5) return 60;
      if (len <= 7) return 48;
      return 38;
    }
    // n === 3 (tightest column)
    if (len <= 2) return 60;
    if (len <= 3) return 52;
    if (len <= 4) return 44;
    if (len === 5) return 36;
    return 30;
  }

  stats.forEach((s, i) => {
    const x = baseX + i * colW + colInset;
    // WP4: the big number IS the page's single accent focal point.
    slide.addText(String(s.value || ""), {
      x, y: top, w: valueW, h: 1.40,
      fontFace: fonts.header,
      fontSize: fontForValue(s.value),
      color: theme.accent,
      bold: true,
      wrap: false,
      valign: "mid",
      margin: 0,
    });
    // Thin accent rule under the number — anchors the column.
    slide.addShape(pres.shapes.RECTANGLE, {
      x: x + valueW / 2 - 0.30, y: top + 1.42, w: 0.60, h: 0.022,
      fill: { color: tintJS(theme.accent, 0.35) },
      line: { color: tintJS(theme.accent, 0.35) },
    });
    if (s.label) {
      slide.addText(String(s.label), {
        x, y: top + 1.56, w: valueW, h: 0.42,
        fontFace: fonts.body, fontSize: STYLE.type.body, color: tc.title,
        bold: true, valign: "top", margin: 0, fit: "shrink", align: "center",
      });
    }
    if (s.tagline) {
      slide.addText(String(s.tagline), {
        x, y: top + 2.04, w: valueW, h: 0.50,
        fontFace: fonts.body, fontSize: STYLE.type.support, color: tc.body, italic: true,
        valign: "top", margin: 0, fit: "shrink", align: "center",
      });
    }
  });
}

function contentGrid(pres, slide, spec, theme, fonts, tc) {
  // Grid uses WHITE cards on the bg, so card-internal text keeps theme.primary
  // regardless of bg luminance (always readable on white).
  void tc;
  const items = (spec.items || []).filter(it => it && typeof it === "object").slice(0, 6);
  const n = items.length;
  if (!n) return;

  let rows, cols;
  if (n <= 2) { rows = 1; cols = n; }
  else if (n === 3) { rows = 1; cols = 3; }
  else if (n === 4) { rows = 2; cols = 2; }
  else { rows = 2; cols = 3; }

  const availW = GRID.CONTENT_W;
  const availH = GRID.CONTENT_BOTTOM - GRID.CONTENT_TOP;
  const gap = GRID.GUTTER;
  const cardW = (availW - gap * (cols - 1)) / cols;
  const cardH = (availH - gap * (rows - 1)) / rows;

  items.forEach((it, idx) => {
    const r = Math.floor(idx / cols);
    const c = idx % cols;
    const x = GRID.MARGIN_X + c * (cardW + gap);
    const y = GRID.CONTENT_TOP + r * (cardH + gap);
    slide.addShape(pres.shapes.ROUNDED_RECTANGLE, withShadow({
      x, y, w: cardW, h: cardH,
      fill: { color: CARD.fill },
      line: { color: CARD.line, pt: 0.75 },
      rectRadius: STYLE.radius,
    }, cardShadow()));
    // WP4: thin accent edge on top of each card — the only accent here.
    slide.addShape(pres.shapes.RECTANGLE, {
      x: x + 0.0, y, w: cardW, h: 0.045,
      fill: { color: tintJS(theme.accent, 0.15 + (idx % 3) * 0.22) },
      line: { color: tintJS(theme.accent, 0.15 + (idx % 3) * 0.22) },
    });
    const pad = 0.20;
    // Inferred vector icon (Tabler) from the card's title/desc — only drawn
    // when a concept resolves, so generic cards stay clean.
    const desc = it.desc || it.description || it.subtitle
      || (it.value != null && String(it.value).trim() !== "" ? String(it.value) : "");
    const concept = it.icon || _inferIcon(`${it.title || ""} ${desc}`);
    const hasIcon = !!(concept && TABLER.iconPaths(concept));
    let titleX = x + pad;
    let titleW = cardW - 2 * pad;
    if (hasIcon) {
      const d = Math.min(0.62, cardH * 0.32);
      addIcon(pres, slide, {
        x: x + pad, y: y + pad, diameter: d,
        color: theme.accent, name: concept,
      });
      titleX = x + pad + d + 0.16;
      titleW = cardW - pad - (titleX - x);
    }
    slide.addText(String(it.title || ""), {
      x: titleX, y: y + pad, w: titleW, h: hasIcon ? 0.6 : 0.45,
      fontFace: fonts.header, fontSize: STYLE.type.subtitle, bold: true, color: CARD.title,
      fit: "shrink", margin: 0, valign: hasIcon ? "mid" : "top",
    });
    if (desc) {
      const dy = y + pad + (hasIcon ? 0.70 : 0.45);
      slide.addText(String(desc), {
        x: x + pad, y: dy, w: cardW - 2 * pad, h: y + cardH - dy - 0.14,
        fontFace: fonts.body, fontSize: STYLE.type.support, color: CARD.body,
        margin: 0,
      });
    }
  });
}

function contentTimeline(pres, slide, spec, theme, fonts, tc) {
  tc = tc || textColorsForBg(theme);
  const steps = (spec.steps || []).filter(s => s && typeof s === "object").slice(0, 5);
  const n = steps.length;
  if (!n) return;

  const yCenter = 2.55;
  const left = GRID.MARGIN_X + 0.28;
  const right = GRID.CONTENT_RIGHT - 0.28;
  const avail = right - left;
  const spacing = n > 1 ? avail / (n - 1) : 0;
  const diameter = 0.58;

  // WP4: connector line is structural → neutral, not accent.
  if (n > 1) {
    slide.addShape(pres.shapes.RECTANGLE, {
      x: left + diameter / 2,
      y: yCenter + diameter / 2 - 0.013,
      w: spacing * (n - 1),
      h: 0.026,
      fill: { color: shadeJS(theme.bg, 0.09) },
      line: { color: shadeJS(theme.bg, 0.09) },
    });
  }

  steps.forEach((s, i) => {
    const cx = n > 1 ? left + i * spacing : left + avail / 2;
    addIcon(pres, slide, {
      x: cx, y: yCenter, diameter,
      color: theme.accent,
      glyph: String(s.step || (i + 1)).slice(0, 2),
      name: s.icon,
      forceNumber: true,
    });
    const colW = n > 1 ? Math.max(Math.min(spacing * 0.95, 2.4), 1.2) : 4.5;
    const colX = cx + diameter / 2 - colW / 2;
    slide.addText(String(s.title || ""), {
      x: colX, y: yCenter - 0.92, w: colW, h: 0.4,
      fontFace: fonts.header, fontSize: STYLE.type.body, bold: true, color: tc.title,
      align: "center", fit: "shrink",
    });
    const desc = s.desc || s.description;
    if (desc) {
      slide.addText(String(desc), {
        x: colX, y: yCenter + diameter + 0.12, w: colW, h: 1.5,
        fontFace: fonts.body, fontSize: STYLE.type.support, color: tc.body,
        align: "center",
      });
    }
  });
}

// ── Swiss-inspired layouts (S06 / S07 / S17 / S20) ───────────────────────

function contentBarChartKpi(pres, slide, spec, theme, fonts, tc) {
  tc = tc || textColorsForBg(theme);
  // S06: 4 metrics shown as vertical bars of differing height.
  // Spec: bars: [{value, label, percentage?, desc?}]
  // percentage controls bar height (0-100). Falls back to even heights.
  const bars = (spec.bars || []).filter(b => b && typeof b === "object").slice(0, 4);
  const n = bars.length;
  if (!n) return;

  // Aligned value band at the top (collision-proof: bars never enter it),
  // then bars, then labels — fills the slide vertically (no empty bottom).
  const valueY = GRID.CONTENT_TOP + 0.05;   // big numbers, aligned across cols
  const valueH = 0.78;
  const barTopLimit = valueY + valueH + 0.20;  // bars start strictly below
  const baseline = 4.50;       // bar bottoms
  const maxH = baseline - barTopLimit;         // tallest bar height
  const labelY = baseline + 0.12;              // label below baseline
  const colArea = { x: GRID.MARGIN_X, w: GRID.CONTENT_W };
  const gap = 0.30;
  const colW = (colArea.w - gap * (n - 1)) / n;
  const barInset = colW * 0.18;
  const barW = colW - barInset * 2;

  // Normalize percentages — if any missing, derive from value rank
  const pcts = bars.map((b, i) => {
    const p = Number(b.percentage);
    if (Number.isFinite(p)) return Math.max(15, Math.min(100, p));
    // fallback: 100, 80, 60, 40 by index
    return Math.max(35, 100 - i * 22);
  });

  // WP8: ranked color ramp — largest bar = full accent, others stay clearly
  // the accent hue (never washed past maxTint). Replaces the old 0.42+0.16·i
  // tints that bleached the 62%/38% bars to near-white.
  const rampColors = dataRamp(theme.accent, pcts, { maxTint: 0.40 });
  const maxPctV = Math.max(...pcts);

  // Faint baseline rule under all bars — a quiet "chart axis" cue that makes
  // the column block read as a designed chart, not floating boxes.
  slide.addShape(pres.shapes.RECTANGLE, {
    x: colArea.x - 0.04, y: baseline, w: colArea.w + 0.08, h: 0.015,
    fill: { color: shadeJS(theme.bg, 0.12) }, line: { color: shadeJS(theme.bg, 0.12) },
  });

  bars.forEach((b, i) => {
    const x = colArea.x + i * (colW + gap);
    const h = (pcts[i] / 100) * maxH;
    const top = baseline - h;

    const isPrimary = pcts[i] === maxPctV;
    const fillColor = rampColors[i];
    // Rounded top via ROUNDED_RECTANGLE (bottom corners sit on the baseline
    // and read as square); sharp style keeps true rectangles.
    const barRadius = STYLE.radius <= 0.02 ? 0 : Math.min(0.06, barW * 0.10);
    slide.addShape(barRadius > 0 ? pres.shapes.ROUNDED_RECTANGLE : pres.shapes.RECTANGLE, {
      x: x + barInset, y: top, w: barW, h: h,
      fill: { color: fillColor },
      line: { color: fillColor },
      rectRadius: barRadius,
    });

    // Big number — fixed aligned band at the top, never overlaps the bar.
    slide.addText(String(b.value || ""), {
      x: x, y: valueY, w: colW, h: valueH,
      fontFace: fonts.header, fontSize: STYLE.type.title,
      color: isPrimary ? theme.accent : tc.title,
      bold: true, align: "center", valign: "mid", margin: 0,
      wrap: false, fit: "shrink",
    });

    // Label BELOW the bar
    slide.addText(String(b.label || ""), {
      x: x, y: labelY, w: colW, h: 0.34,
      fontFace: fonts.body, fontSize: STYLE.type.support, color: tc.body,
      align: "center", margin: 0, fit: "shrink",
    });

    // Optional desc (tiny line)
    if (b.desc) {
      slide.addText(String(b.desc), {
        x: x, y: labelY + 0.34, w: colW, h: 0.28,
        fontFace: fonts.body, fontSize: STYLE.type.caption, color: tc.body,
        align: "center", italic: true, margin: 0, fit: "shrink",
      });
    }
  });
}

function contentHorizontalBars(pres, slide, spec, theme, fonts, tc) {
  tc = tc || textColorsForBg(theme);
  // S07: 5-10 horizontal ranked bars.
  // Spec: bars: [{label, value, percentage}]
  const bars = (spec.bars || []).filter(b => b && typeof b === "object").slice(0, 10);
  const n = bars.length;
  if (!n) return;

  const areaTop = GRID.CONTENT_TOP + 0.05;
  const bottom = GRID.CONTENT_BOTTOM;
  const rowH = Math.min(0.55, (bottom - areaTop) / n);
  // Vertically center the row block so a short list doesn't leave a big
  // empty band at the bottom.
  const usedH = rowH * n;
  const top = areaTop + Math.max(0, ((bottom - areaTop) - usedH) / 2);
  const labelW = 2.30;            // left label column
  // Value column adapts to the longest value so CJK figures like
  // "31,745 亿元" never wrap onto a second line.
  const maxValLen = Math.max(...bars.map(b => String(b.value || "").length), 1);
  const valueW = Math.min(2.15, Math.max(0.85, 0.40 + maxValLen * 0.135));
  const trackLeft = GRID.MARGIN_X + labelW + 0.13;
  const trackRight = GRID.CONTENT_RIGHT - valueW - 0.12;
  const trackW = trackRight - trackLeft;
  const barH = Math.min(rowH - 0.18, 0.28);

  // Derive max percentage for normalization
  const pcts = bars.map(b => {
    const p = Number(b.percentage);
    return Number.isFinite(p) ? p : Number(b.value) || 50;
  });
  const maxPct = Math.max(...pcts, 1);
  // WP8: ranked ramp — #1 full accent, rest stay saturated (no wash-out).
  const rampColors = dataRamp(theme.accent, pcts, { maxTint: 0.44 });

  bars.forEach((b, i) => {
    const y = top + i * rowH;
    const barY = y + (rowH - barH) / 2;
    const fillW = (pcts[i] / maxPct) * trackW;
    const isTop = pcts[i] === maxPct;
    // Label (left)
    slide.addText(String(b.label || ""), {
      x: GRID.MARGIN_X, y, w: labelW, h: rowH,
      fontFace: fonts.body, fontSize: STYLE.type.support, color: tc.title,
      bold: true, valign: "mid", margin: 0, fit: "shrink",
    });
    // WP4: track is structural → neutral; only the #1 bar gets full accent,
    // the rest are lighter tints of the same hue (ranking reads instantly).
    slide.addShape(pres.shapes.RECTANGLE, {
      x: trackLeft, y: barY, w: trackW, h: barH,
      fill: { color: shadeJS(theme.bg, 0.06) },
      line: { color: shadeJS(theme.bg, 0.06) },
    });
    const barColor = rampColors[i];
    const hbRadius = STYLE.radius <= 0.02 ? 0 : Math.min(barH / 2, 0.08);
    slide.addShape(hbRadius > 0 ? pres.shapes.ROUNDED_RECTANGLE : pres.shapes.RECTANGLE, {
      x: trackLeft, y: barY, w: Math.max(fillW, 0.08), h: barH,
      fill: { color: barColor },
      line: { color: barColor },
      rectRadius: hbRadius,
    });
    // Value (right) — adaptive column, single line guaranteed.
    slide.addText(String(b.value || ""), {
      x: GRID.CONTENT_RIGHT - valueW, y, w: valueW, h: rowH,
      fontFace: fonts.header, fontSize: STYLE.type.support,
      color: isTop ? theme.accent : tc.title,
      bold: true, align: "right", valign: "mid", margin: 0,
      wrap: false, fit: "shrink",
    });
  });
}

function isDarkHex(hex) {
  const h = String(hex || "").replace(/^#/, "");
  if (!/^[0-9A-Fa-f]{6}$/.test(h)) return true;
  const r = parseInt(h.slice(0, 2), 16);
  const g = parseInt(h.slice(2, 4), 16);
  const b = parseInt(h.slice(4, 6), 16);
  // Perceived brightness (ITU-R BT.601). < 0.55 → treat as dark, prefer white text.
  return (0.299 * r + 0.587 * g + 0.114 * b) / 255 < 0.55;
}

/**
 * Resolve text colors for elements drawn DIRECTLY on the content slide bg.
 * On light backgrounds use the palette's primary/secondary (already designed
 * for that). On dark backgrounds (dark-bg pages) flip to white / light
 * tints so titles and bullets remain readable.
 *
 * Note: this only applies to text on `theme.bg`. Text inside white floating
 * cards (grid / highlights) continues to use the palette primary/secondary
 * via the existing code paths.
 */
function textColorsForBg(theme) {
  if (!isDarkHex(theme.bg)) {
    return { title: theme.primary, body: theme.secondary };
  }
  const titleColor = theme.text_on_primary || "FFFFFF";
  // Use palette.light only if it is itself light enough to read on dark bg;
  // otherwise fall back to white so body copy is never lost in the void.
  const bodyColor = isDarkHex(theme.light) ? titleColor : theme.light;
  return { title: titleColor, body: bodyColor };
}

function contentConcentric(pres, slide, spec, theme, fonts, tc) {
  tc = tc || textColorsForBg(theme);
  // S17: 3 concentric rings — core / middle / outer ecosystem map.
  // Spec: rings: [{title, items?}] — 3 entries, INNERMOST first (rings[0] = core).
  const rings = (spec.rings || []).filter(r => r && typeof r === "object").slice(0, 3);
  if (!rings.length) return;

  // Shift the diagram left to give the right-hand legend ~2.9" so CJK ring
  // titles never wrap onto a second line.
  const cx = 3.55;
  const cy = 3.05;
  // Diameters by ring index: 0=core (smallest), 1=middle, 2=outer
  const diamByRing = [1.55, 2.85, 4.20];
  // WP8: depth ladder capped at 0.44 — core = full accent, outward stays
  // clearly the accent hue (the old 0.50/0.82 tints bleached the outer ring
  // to near-white so the three layers blurred together).
  const fillByRing = depthLadder(theme.accent, rings.length, 0.44);
  const lineColor = shadeJS(theme.bg, 0.10);
  const coreText = isDarkHex(theme.accent) ? "FFFFFF" : "0A0A0A";
  const textByRing = [coreText, "0A0A0A", "0A0A0A"];

  // Draw OUTER first (largest first) so inner rings overlay on top
  for (let i = rings.length - 1; i >= 0; i--) {
    const d = diamByRing[i];
    slide.addShape(pres.shapes.OVAL, {
      x: cx - d / 2, y: cy - d / 2, w: d, h: d,
      fill: { color: fillByRing[i] },
      line: { color: lineColor, pt: 0.6 },
    });
  }

  // Core label — centered inside the inner circle
  if (rings[0]) {
    slide.addText(String(rings[0].title || rings[0].label || ""), {
      x: cx - 0.70, y: cy - 0.30, w: 1.40, h: 0.60,
      fontFace: fonts.header, fontSize: STYLE.type.support, color: textByRing[0],
      bold: true, align: "center", valign: "mid", margin: 0, fit: "shrink",
    });
  }
  // Middle ring label — placed in the upper portion of the ring (above inner core)
  if (rings[1]) {
    slide.addText(String(rings[1].title || rings[1].label || ""), {
      x: cx - 1.30, y: cy - 1.20, w: 2.60, h: 0.36,
      fontFace: fonts.header, fontSize: STYLE.type.caption, color: textByRing[1],
      bold: true, align: "center", valign: "mid", margin: 0, fit: "shrink",
    });
  }
  // Outer ring label — placed in the upper portion of the ring
  if (rings[2]) {
    slide.addText(String(rings[2].title || rings[2].label || ""), {
      x: cx - 2.00, y: cy - 1.90, w: 4.00, h: 0.36,
      fontFace: fonts.header, fontSize: STYLE.type.caption, color: textByRing[2],
      bold: true, align: "center", valign: "mid", margin: 0, fit: "shrink",
    });
  }

  // Right side legend listing items per ring (core → outer, matches reading order)
  const legendX = 6.20;
  const legendW = GRID.CONTENT_RIGHT - legendX - 0.34;
  const legendFloor = 4.95;   // keep clear of the page badge (y≈5.08)
  let legendY = GRID.CONTENT_TOP + 0.06;
  rings.forEach((r, i) => {
    if (legendY > legendFloor) return;
    // Colored dot matching the ring's fill
    slide.addShape(pres.shapes.OVAL, {
      x: legendX, y: legendY + 0.06, w: 0.20, h: 0.20,
      fill: { color: fillByRing[i] },
      line: { color: lineColor, pt: 0.5 },
    });
    // De-dup the layer prefix: if the author already led the title with the
    // layer name ("核心层 · 大脑与小脑"), don't prepend "核心层 · " again.
    const layerName = ["核心层", "中间层", "外延层"][i] || `层 ${i + 1}`;
    const rawTitle = String(r.title || r.label || "");
    const title = rawTitle.startsWith(layerName) ? rawTitle : `${layerName} · ${rawTitle}`;
    slide.addText(title, {
      x: legendX + 0.34, y: legendY, w: legendW, h: 0.30,
      fontFace: fonts.header, fontSize: STYLE.type.support, color: tc.title,
      bold: true, margin: 0, wrap: false, fit: "shrink",
    });
    legendY += 0.335;
    const items = (r.items || []).slice(0, 4);
    items.forEach(it => {
      if (legendY > legendFloor) return;
      slide.addText(`· ${it}`, {
        x: legendX + 0.34, y: legendY, w: legendW, h: 0.25,
        fontFace: fonts.body, fontSize: STYLE.type.caption, color: tc.body,
        margin: 0, wrap: false, fit: "shrink",
      });
      legendY += 0.238;
    });
    legendY += 0.12;
  });
}

function contentKpiLedger(pres, slide, spec, theme, fonts, tc) {
  tc = tc || textColorsForBg(theme);
  // S20: 4-6 row vertical KPI ledger with horizontal separators.
  // Spec: ledger: [{value, label, desc?}]
  const rows = (spec.ledger || []).filter(r => r && typeof r === "object").slice(0, 6);
  const n = rows.length;
  if (!n) return;

  const top = GRID.CONTENT_TOP + 0.10;
  const bottom = GRID.CONTENT_BOTTOM;
  const rowH = (bottom - top) / n;
  const xL = GRID.MARGIN_X;
  const xR = GRID.CONTENT_RIGHT;
  const labelX = xL + 3.55;

  rows.forEach((r, i) => {
    const y = top + i * rowH;
    // Big value (left) — the focal accent for this row.
    slide.addText(String(r.value || ""), {
      x: xL, y: y + 0.05, w: 3.40, h: rowH - 0.15,
      fontFace: fonts.header, fontSize: STYLE.type.title, color: theme.accent,
      bold: true, valign: "mid", margin: 0, fit: "shrink",
    });
    // Label (right, big)
    slide.addText(String(r.label || ""), {
      x: labelX, y: y + 0.05, w: xR - labelX, h: (rowH - 0.15) * 0.55,
      fontFace: fonts.header, fontSize: STYLE.type.subtitle, color: tc.title,
      bold: true, valign: "bottom", margin: 0, fit: "shrink",
    });
    // Desc (right, small under label)
    if (r.desc) {
      slide.addText(String(r.desc), {
        x: labelX, y: y + 0.05 + (rowH - 0.15) * 0.55, w: xR - labelX, h: (rowH - 0.15) * 0.45,
        fontFace: fonts.body, fontSize: STYLE.type.support, color: tc.body,
        valign: "top", margin: 0, fit: "shrink",
      });
    }
    // WP4: row separator is structural → neutral hairline, not accent.
    if (i < n - 1) {
      slide.addShape(pres.shapes.RECTANGLE, {
        x: xL, y: y + rowH - 0.01, w: xR - xL, h: 0.013,
        fill: { color: shadeJS(theme.bg, 0.08) },
        line: { color: shadeJS(theme.bg, 0.08) },
      });
    }
  });
}

// ── WP7 new layouts ──────────────────────────────────────────────────

// Breathing page: a single large statement. Low density, high impact.
function contentQuote(pres, slide, spec, theme, fonts, tc) {
  tc = tc || textColorsForBg(theme);
  // Oversized faint quotation mark anchored top-left.
  slide.addText("“", {
    x: GRID.MARGIN_X - 0.05, y: 1.05, w: 2.0, h: 1.6,
    fontFace: "Georgia", fontSize: Math.round(STYLE.type.large_title * 2.4),
    color: tintJS(theme.accent, 0.55), bold: true, margin: 0, valign: "top",
  });
  slide.addText(String(spec.quote || ""), {
    x: GRID.MARGIN_X + 0.6, y: 1.85, w: GRID.CONTENT_W - 1.2, h: 2.0,
    fontFace: fonts.header,
    fontSize: STYLE.type.subtitle + 8,
    color: tc.title, bold: true, italic: true,
    align: "left", valign: "mid", margin: 0, fit: "shrink",
  });
  const attrib = spec.attribution || spec.author || spec.body;
  if (attrib) {
    slide.addShape(pres.shapes.RECTANGLE, {
      x: GRID.MARGIN_X + 0.6, y: 4.05, w: 0.5, h: 0.026,
      fill: { color: theme.accent }, line: { color: theme.accent },
    });
    slide.addText(String(attrib), {
      x: GRID.MARGIN_X + 0.6, y: 4.14, w: GRID.CONTENT_W - 1.2, h: 0.4,
      fontFace: fonts.body, fontSize: STYLE.type.support, color: tc.body,
      charSpacing: 1, margin: 0,
    });
  }
}

// Breathing page: one hero metric, centered.
function contentBigNumber(pres, slide, spec, theme, fonts, tc) {
  tc = tc || textColorsForBg(theme);
  const m = spec.bigNumber || spec.metric || {};
  const value = typeof m === "object" ? (m.value || m.number || "") : String(m);
  const label = (typeof m === "object" && (m.label || m.title)) || spec.metricLabel || "";
  const sub = (typeof m === "object" && (m.sublabel || m.desc)) || "";
  slide.addText(String(value), {
    x: GRID.MARGIN_X, y: 1.75, w: GRID.CONTENT_W, h: 1.9,
    fontFace: fonts.header, fontSize: STYLE.type.hero,
    color: theme.accent, bold: true, align: "center", valign: "mid",
    margin: 0, wrap: false, fit: "shrink",
  });
  if (label) {
    slide.addText(String(label), {
      x: GRID.MARGIN_X, y: 3.75, w: GRID.CONTENT_W, h: 0.5,
      fontFace: fonts.header, fontSize: STYLE.type.subtitle, color: tc.title,
      bold: true, align: "center", margin: 0, fit: "shrink",
    });
  }
  if (sub) {
    slide.addText(String(sub), {
      x: GRID.MARGIN_X, y: 4.28, w: GRID.CONTENT_W, h: 0.4,
      fontFace: fonts.body, fontSize: STYLE.type.support, color: tc.body,
      italic: true, align: "center", margin: 0, fit: "shrink",
    });
  }
}

// 2x2 strategy matrix with optional axis captions.
function contentMatrix(pres, slide, spec, theme, fonts, tc) {
  tc = tc || textColorsForBg(theme);
  const q = (spec.quadrants || []).filter(Boolean).slice(0, 4);
  while (q.length < 4) q.push({});
  const top = GRID.CONTENT_TOP + 0.15;
  const areaH = GRID.CONTENT_BOTTOM - top - 0.05;
  const areaW = GRID.CONTENT_W - 0.55;
  const x0 = GRID.MARGIN_X + 0.55;
  const cellW = (areaW - GRID.GUTTER) / 2;
  const cellH = (areaH - GRID.GUTTER) / 2;
  const tints = [0.10, 0.40, 0.62, 0.84];
  q.forEach((c, idx) => {
    const r = Math.floor(idx / 2), col = idx % 2;
    const x = x0 + col * (cellW + GRID.GUTTER);
    const y = top + r * (cellH + GRID.GUTTER);
    slide.addShape(pres.shapes.ROUNDED_RECTANGLE, withShadow({
      x, y, w: cellW, h: cellH,
      fill: { color: tintJS(theme.accent, tints[idx]) },
      line: { color: tintJS(theme.accent, Math.max(0, tints[idx] - 0.12)), pt: 0.5 },
      rectRadius: STYLE.radius,
    }, cardShadow()));
    const onDark = !isDarkHex(theme.accent) ? false : tints[idx] < 0.4;
    const txt = onDark ? "FFFFFF" : "0A0A0A";
    slide.addText(String(c.title || ""), {
      x: x + 0.22, y: y + 0.20, w: cellW - 0.44, h: 0.5,
      fontFace: fonts.header, fontSize: STYLE.type.subtitle, color: txt,
      bold: true, margin: 0, fit: "shrink",
    });
    if (c.desc || c.description) {
      slide.addText(String(c.desc || c.description), {
        x: x + 0.22, y: y + 0.72, w: cellW - 0.44, h: cellH - 0.9,
        fontFace: fonts.body, fontSize: STYLE.type.support, color: txt,
        margin: 0,
      });
    }
  });
  // Axis captions
  if (Array.isArray(spec.axisX)) {
    slide.addText(`${spec.axisX[0] || ""}  →  ${spec.axisX[1] || ""}`, {
      x: x0, y: GRID.CONTENT_BOTTOM + 0.02, w: areaW, h: 0.26,
      fontFace: fonts.body, fontSize: STYLE.type.caption, color: tc.body,
      align: "center", margin: 0,
    });
  }
}

// Horizontal arrow flow of phases.
function contentProcess(pres, slide, spec, theme, fonts, tc) {
  tc = tc || textColorsForBg(theme);
  const steps = (spec.flow || []).filter(Boolean).slice(0, 5)
    .map(s => (typeof s === "object" ? s : { title: String(s) }));
  const n = steps.length;
  if (!n) return;
  const y = 2.35;
  const gap = 0.28;
  const cardW = (GRID.CONTENT_W - gap * (n - 1)) / n;
  const cardH = 1.5;
  steps.forEach((s, i) => {
    const x = GRID.MARGIN_X + i * (cardW + gap);
    slide.addShape(pres.shapes.ROUNDED_RECTANGLE, withShadow({
      x, y, w: cardW, h: cardH,
      fill: { color: tintJS(theme.accent, 0.86 - i * 0.0) },
      line: { color: tintJS(theme.accent, 0.66), pt: 0.5 },
      rectRadius: STYLE.radius,
    }, cardShadow()));
    slide.addText(String(i + 1).padStart(2, "0"), {
      x: x + 0.16, y: y + 0.12, w: cardW - 0.3, h: 0.4,
      fontFace: fonts.header, fontSize: STYLE.type.subtitle, color: theme.accent,
      bold: true, margin: 0,
    });
    slide.addText(String(s.title || ""), {
      x: x + 0.16, y: y + 0.55, w: cardW - 0.3, h: 0.4,
      fontFace: fonts.header, fontSize: STYLE.type.body, color: CARD.title,
      bold: true, margin: 0, fit: "shrink",
    });
    if (s.desc || s.description) {
      slide.addText(String(s.desc || s.description), {
        x: x + 0.16, y: y + 0.95, w: cardW - 0.3, h: cardH - 1.0,
        fontFace: fonts.body, fontSize: STYLE.type.caption, color: CARD.body,
        margin: 0,
      });
    }
    if (i < n - 1) {
      slide.addText("›", {
        x: x + cardW - 0.02, y: y, w: gap, h: cardH,
        fontFace: "Arial", fontSize: STYLE.type.subtitle, color: theme.accent,
        bold: true, align: "center", valign: "mid", margin: 0,
      });
    }
  });
}

// Comparison table: header row + zebra body.
function contentComparisonTable(pres, slide, spec, theme, fonts, tc) {
  tc = tc || textColorsForBg(theme);
  const cols = (spec.columns || []).map(String);
  const rows = (spec.rows || []).map(r => (Array.isArray(r) ? r : (r.cells || []))).slice(0, 7);
  const nCols = cols.length || (rows[0] ? rows[0].length : 0);
  if (!nCols) return;
  const top = GRID.CONTENT_TOP + 0.05;
  const tableH = GRID.CONTENT_BOTTOM - top;
  const headH = 0.5;
  const bodyRowH = rows.length ? (tableH - headH) / rows.length : 0;
  const colW = GRID.CONTENT_W / nCols;
  // Header
  slide.addShape(pres.shapes.RECTANGLE, {
    x: GRID.MARGIN_X, y: top, w: GRID.CONTENT_W, h: headH,
    fill: { color: theme.accent }, line: { color: theme.accent },
  });
  const headTxt = isDarkHex(theme.accent) ? "FFFFFF" : "0A0A0A";
  cols.forEach((c, i) => {
    slide.addText(c, {
      x: GRID.MARGIN_X + i * colW + 0.14, y: top, w: colW - 0.28, h: headH,
      fontFace: fonts.header, fontSize: STYLE.type.support, color: headTxt,
      bold: true, valign: "mid", margin: 0, fit: "shrink",
    });
  });
  // Alt-row fill must contrast against the row text. On light-bg themes use a
  // pale accent tint (looks zebra-like); on dark-bg themes a pale accent tint
  // would turn near-white and our white tc.title text would vanish — so use a
  // subtle shade of bg instead so the row stays dark and white text reads.
  const altRowFill = isDarkHex(theme.bg) ? tintJS(theme.bg, 0.06) : tintJS(theme.accent, 0.92);
  rows.forEach((cells, ri) => {
    const y = top + headH + ri * bodyRowH;
    if (ri % 2 === 1) {
      slide.addShape(pres.shapes.RECTANGLE, {
        x: GRID.MARGIN_X, y, w: GRID.CONTENT_W, h: bodyRowH,
        fill: { color: altRowFill }, line: { color: altRowFill },
      });
    }
    cells.slice(0, nCols).forEach((cell, ci) => {
      slide.addText(String(cell), {
        x: GRID.MARGIN_X + ci * colW + 0.14, y, w: colW - 0.28, h: bodyRowH,
        fontFace: ci === 0 ? fonts.header : fonts.body,
        fontSize: STYLE.type.support, color: ci === 0 ? tc.title : tc.body,
        bold: ci === 0, valign: "mid", margin: 0, fit: "shrink",
      });
    });
  });
}

// Pyramid / hierarchy: stacked tiers, widening downward.
function contentPyramid(pres, slide, spec, theme, fonts, tc) {
  tc = tc || textColorsForBg(theme);
  const layers = (spec.pyramid || []).filter(Boolean).slice(0, 5)
    .map(l => (typeof l === "object" ? l : { title: String(l) }));
  const n = layers.length;
  if (!n) return;
  const top = GRID.CONTENT_TOP + 0.10;
  const totalH = GRID.CONTENT_BOTTOM - top;
  const tierH = (totalH - (n - 1) * 0.12) / n;
  const cx = 5.0;
  // WP8: capped depth ladder (apex full accent → base ≤0.46 tint) so the
  // widest base tier never bleaches to near-white.
  const tierFills = depthLadder(theme.accent, n, 0.46);
  layers.forEach((l, i) => {
    const frac = (i + 1) / n;                 // widen toward the base
    const w = GRID.CONTENT_W * (0.40 + 0.60 * frac);
    const y = top + i * (tierH + 0.12);
    slide.addShape(pres.shapes.ROUNDED_RECTANGLE, withShadow({
      x: cx - w / 2, y, w, h: tierH,
      fill: { color: tierFills[i] },
      line: { color: shadeJS(theme.bg, 0.06), pt: 0.5 },
      rectRadius: STYLE.radius,
    }, cardShadow()));
    const onDark = isDarkHex(tierFills[i]);
    slide.addText(
      String(l.title || "") + (l.desc ? `  —  ${l.desc}` : ""),
      {
        x: cx - w / 2, y, w, h: tierH,
        fontFace: fonts.header, fontSize: STYLE.type.body,
        color: onDark ? "FFFFFF" : "0A0A0A",
        bold: true, align: "center", valign: "mid", margin: 0, fit: "shrink",
      },
    );
  });
}

// kpi_cards — 4-8 standalone metric cards (overview wall).
function contentKpiCards(pres, slide, spec, theme, fonts, tc) {
  tc = tc || textColorsForBg(theme);
  const k = (spec.kpis || []).filter(Boolean).slice(0, 8);
  const n = k.length;
  if (!n) return;
  let cols;
  if (n <= 3) cols = n;
  else if (n === 4) cols = 2;
  else if (n <= 6) cols = 3;
  else cols = 4;
  const rows = Math.ceil(n / cols);
  const gap = GRID.GUTTER;
  const availH = GRID.CONTENT_BOTTOM - GRID.CONTENT_TOP;
  const cardW = (GRID.CONTENT_W - gap * (cols - 1)) / cols;
  const cardH = (availH - gap * (rows - 1)) / rows;
  // WP12 dark_gold / WP15 memphis: each KPI card gets a distinct top-border
  // color (the signature multi-color card wall), and its value takes that color.
  const dg = PACK === "dark_gold" || PACK === "memphis";
  const dgPalette = PACK === "memphis" ? packMultis(theme) : [theme.accent, ...MULTI_ACCENTS];
  k.forEach((it, i) => {
    const r = Math.floor(i / cols), c = i % cols;
    const x = GRID.MARGIN_X + c * (cardW + gap);
    const y = GRID.CONTENT_TOP + r * (cardH + gap);
    const cc = dg ? dgPalette[i % dgPalette.length] : theme.accent;
    slide.addShape(cardShapeType(pres), withShadow({
      x, y, w: cardW, h: cardH,
      fill: { color: CARD.fill },
      line: { color: CARD.line, pt: 0.75 },
      rectRadius: STYLE.radius,
    }, cardShadow()));
    if (dg) {
      slide.addShape(pres.shapes.RECTANGLE, {
        x, y, w: cardW, h: 0.07, fill: { color: cc }, line: { color: cc },
      });
    }
    // Subtle inferred icon, top-left corner (only when a concept resolves).
    const kc = it.icon || _inferIcon(`${it.label || it.title || ""} ${it.desc || it.sublabel || ""}`);
    if (kc && TABLER.iconPaths(kc)) {
      const d = Math.min(0.46, cardH * 0.26);
      addIcon(pres, slide, {
        x: x + 0.16, y: y + (dg ? 0.2 : 0.14), diameter: d, color: cc, name: kc,
      });
    }
    slide.addText(String(it.value || it.number || ""), {
      x: x + 0.16, y: y + cardH * 0.16, w: cardW - 0.32, h: cardH * 0.42,
      fontFace: fonts.header, fontSize: STYLE.type.title, color: cc,
      bold: true, align: "center", valign: "mid", margin: 0, wrap: false, fit: "shrink",
    });
    slide.addText(String(it.label || it.title || ""), {
      x: x + 0.16, y: y + cardH * 0.58, w: cardW - 0.32, h: cardH * 0.22,
      fontFace: fonts.header, fontSize: STYLE.type.support, color: CARD.title,
      bold: true, align: "center", margin: 0, fit: "shrink",
    });
    if (it.desc || it.sublabel) {
      slide.addText(String(it.desc || it.sublabel), {
        x: x + 0.16, y: y + cardH * 0.78, w: cardW - 0.32, h: cardH * 0.20,
        fontFace: fonts.body, fontSize: STYLE.type.caption, color: CARD.body,
        align: "center", italic: true, margin: 0, fit: "shrink",
      });
    }
  });
}

// funnel — 3-5 sequential stages, each band narrower than the one above.
function contentFunnel(pres, slide, spec, theme, fonts, tc) {
  tc = tc || textColorsForBg(theme);
  const st = (spec.funnel || []).filter(Boolean).slice(0, 5)
    .map(s => (typeof s === "object" ? s : { label: String(s) }));
  const n = st.length;
  if (!n) return;
  const top = GRID.CONTENT_TOP + 0.15;
  const gap = 0.16;
  const bandH = ((GRID.CONTENT_BOTTOM - top) - gap * (n - 1)) / n;
  const cx = 5.0;
  const wMax = GRID.CONTENT_W * 0.92;
  const wMin = GRID.CONTENT_W * 0.34;
  // WP8: capped depth ladder so the bottom funnel stages stay saturated.
  const bandFills = depthLadder(theme.accent, n, 0.48);
  st.forEach((s, i) => {
    const w = wMax - (wMax - wMin) * (i / Math.max(1, n - 1));
    const y = top + i * (bandH + gap);
    const fill = bandFills[i];
    slide.addShape(cardShapeType(pres), withShadow({
      x: cx - w / 2, y, w, h: bandH,
      fill: { color: fill }, line: { color: fill },
      rectRadius: STYLE.radius,
    }, cardShadow()));
    const onDark = isDarkHex(fill);
    const txt = onDark ? "FFFFFF" : "0A0A0A";
    slide.addText(
      String(s.label || s.title || "") + (s.value ? `   ·   ${s.value}` : ""),
      {
        x: cx - w / 2, y, w, h: bandH,
        fontFace: fonts.header, fontSize: STYLE.type.body, color: txt,
        bold: true, align: "center", valign: "mid", margin: 0, fit: "shrink",
      },
    );
  });
}

// hub_spoke — central node + up to 7 satellites with connectors.
function contentHubSpoke(pres, slide, spec, theme, fonts, tc) {
  tc = tc || textColorsForBg(theme);
  const hub = spec.hub || {};
  const nodes = (hub.nodes || spec.spokes || []).filter(Boolean).slice(0, 7)
    .map(d => (typeof d === "object" ? d : { title: String(d) }));
  const n = nodes.length;
  if (!n) return;
  const cx = 5.0, cy = 3.05;
  const R = 1.95;                 // satellite orbit radius
  const hubD = 1.55;              // center node diameter
  const nodeW = 1.95, nodeH = 0.78;

  // Connectors first (under everything) — thin rotated bars.
  nodes.forEach((it, i) => {
    const ang = (-90 + i * (360 / n)) * Math.PI / 180;
    const nx = cx + R * Math.cos(ang);
    const ny = cy + R * Math.sin(ang);
    const mx = (cx + nx) / 2, my = (cy + ny) / 2;
    const len = Math.hypot(nx - cx, ny - cy);
    slide.addShape(pres.shapes.RECTANGLE, {
      x: mx - len / 2, y: my - 0.011, w: len, h: 0.022,
      fill: { color: shadeJS(theme.bg, 0.10) },
      line: { color: shadeJS(theme.bg, 0.10) },
      rotate: ang * 180 / Math.PI,
    });
  });

  // Satellites.
  nodes.forEach((it, i) => {
    const ang = (-90 + i * (360 / n)) * Math.PI / 180;
    const nx = cx + R * Math.cos(ang);
    const ny = cy + R * Math.sin(ang);
    slide.addShape(cardShapeType(pres), withShadow({
      x: nx - nodeW / 2, y: ny - nodeH / 2, w: nodeW, h: nodeH,
      fill: { color: tintJS(theme.accent, 0.84) },
      line: { color: tintJS(theme.accent, 0.62), pt: 0.5 },
      rectRadius: STYLE.radius,
    }, cardShadow()));
    slide.addText(String(it.title || it.label || ""), {
      x: nx - nodeW / 2 + 0.08, y: ny - nodeH / 2, w: nodeW - 0.16, h: nodeH,
      fontFace: fonts.header, fontSize: STYLE.type.support, color: CARD.title,
      bold: true, align: "center", valign: "mid", margin: 0, fit: "shrink",
    });
  });

  // Center hub on top.
  slide.addShape(pres.shapes.OVAL, {
    x: cx - hubD / 2, y: cy - hubD / 2, w: hubD, h: hubD,
    fill: { color: theme.accent }, line: { color: theme.accent },
  });
  slide.addText(String(hub.center || hub.title || spec.center || "核心"), {
    x: cx - hubD / 2, y: cy - hubD / 2, w: hubD, h: hubD,
    fontFace: fonts.header, fontSize: STYLE.type.body,
    color: isDarkHex(theme.accent) ? "FFFFFF" : "0A0A0A",
    bold: true, align: "center", valign: "mid", margin: 0, fit: "shrink",
  });
}

// roadmap_vertical — 4-6 milestones on a vertical timeline.
function contentRoadmapVertical(pres, slide, spec, theme, fonts, tc) {
  tc = tc || textColorsForBg(theme);
  const ms = (spec.roadmap || []).filter(Boolean).slice(0, 6)
    .map(m => (typeof m === "object" ? m : { title: String(m) }));
  const n = ms.length;
  if (!n) return;
  const top = GRID.CONTENT_TOP + 0.12;
  const bottom = GRID.CONTENT_BOTTOM;
  const rowH = (bottom - top) / n;
  const railX = GRID.MARGIN_X + 1.55;     // vertical rail position
  const dotD = 0.26;
  // Rail
  slide.addShape(pres.shapes.RECTANGLE, {
    x: railX - 0.011, y: top + rowH * 0.5, w: 0.022, h: rowH * (n - 1),
    fill: { color: shadeJS(theme.bg, 0.10) },
    line: { color: shadeJS(theme.bg, 0.10) },
  });
  ms.forEach((m, i) => {
    const yc = top + i * rowH + rowH * 0.5;
    // Left: phase/date
    slide.addText(String(m.phase || m.date || m.step || `第 ${i + 1} 阶段`), {
      x: GRID.MARGIN_X, y: yc - rowH * 0.4, w: 1.35, h: rowH * 0.8,
      fontFace: fonts.header, fontSize: STYLE.type.support, color: theme.accent,
      bold: true, align: "right", valign: "mid", margin: 0, fit: "shrink",
    });
    // Dot
    slide.addShape(pres.shapes.OVAL, {
      x: railX - dotD / 2, y: yc - dotD / 2, w: dotD, h: dotD,
      fill: { color: theme.accent }, line: { color: "FFFFFF", pt: 1.2 },
    });
    // Right: title + desc
    const tx = railX + 0.34;
    const tw = GRID.CONTENT_RIGHT - tx;
    slide.addText(String(m.title || ""), {
      x: tx, y: yc - rowH * 0.42, w: tw, h: rowH * 0.46,
      fontFace: fonts.header, fontSize: STYLE.type.subtitle, color: tc.title,
      bold: true, valign: "bottom", margin: 0, fit: "shrink",
    });
    if (m.desc || m.description) {
      slide.addText(String(m.desc || m.description), {
        x: tx, y: yc + 0.02, w: tw, h: rowH * 0.42,
        fontFace: fonts.body, fontSize: STYLE.type.support, color: tc.body,
        valign: "top", margin: 0, fit: "shrink",
      });
    }
  });
}

// progress — 3-8 items each with a completion bar.
function contentProgress(pres, slide, spec, theme, fonts, tc) {
  tc = tc || textColorsForBg(theme);
  const rows = (spec.progress || []).filter(Boolean).slice(0, 8)
    .map(r => (typeof r === "object" ? r : { label: String(r) }));
  const n = rows.length;
  if (!n) return;
  const top = GRID.CONTENT_TOP + 0.10;
  const avail = GRID.CONTENT_BOTTOM - top;
  const rowH = Math.min(0.78, avail / n);
  const usedH = rowH * n;
  const y0 = top + Math.max(0, (avail - usedH) / 2);
  const labelW = 2.6;
  const trackX = GRID.MARGIN_X + labelW + 0.15;
  const trackW = GRID.CONTENT_RIGHT - trackX - 0.85;
  const barH = Math.min(0.26, rowH - 0.30);
  rows.forEach((r, i) => {
    const y = y0 + i * rowH;
    const pct = Math.max(0, Math.min(100,
      Number.isFinite(Number(r.percentage)) ? Number(r.percentage)
        : parseNumLike(r.value) || 0));
    slide.addText(String(r.label || r.title || ""), {
      x: GRID.MARGIN_X, y, w: labelW, h: rowH,
      fontFace: fonts.header, fontSize: STYLE.type.support, color: tc.title,
      bold: true, valign: "mid", margin: 0, fit: "shrink",
    });
    const by = y + (rowH - barH) / 2;
    slide.addShape(pres.shapes.RECTANGLE, {
      x: trackX, y: by, w: trackW, h: barH,
      fill: { color: shadeJS(theme.bg, 0.06) }, line: { color: shadeJS(theme.bg, 0.06) },
    });
    slide.addShape(pres.shapes.RECTANGLE, {
      x: trackX, y: by, w: Math.max(0.06, trackW * pct / 100), h: barH,
      fill: { color: theme.accent }, line: { color: theme.accent },
    });
    slide.addText((r.value != null ? String(r.value) : `${Math.round(pct)}%`), {
      x: GRID.CONTENT_RIGHT - 0.80, y, w: 0.80, h: rowH,
      fontFace: fonts.header, fontSize: STYLE.type.support, color: theme.accent,
      bold: true, align: "right", valign: "mid", margin: 0, wrap: false, fit: "shrink",
    });
  });
}

// waterfall — start → +/- steps → end, bridging bars.
function contentWaterfall(pres, slide, spec, theme, fonts, tc) {
  tc = tc || textColorsForBg(theme);
  const st = (spec.waterfall || []).filter(Boolean).slice(0, 7)
    .map(s => (typeof s === "object" ? s : { label: String(s) }));
  const n = st.length;
  if (!n) return;
  // Running total → cumulative levels.
  let run = 0;
  const segs = st.map((s, i) => {
    const v = parseNumLike(s.value) || 0;
    const isTotal = s.type === "total" || s.total || i === 0 || i === n - 1;
    const from = isTotal ? 0 : run;
    const to = isTotal ? v : run + v;
    if (!isTotal) run += v; else run = v;
    return { s, from, to, isTotal, v };
  });
  const lo = Math.min(0, ...segs.map(g => Math.min(g.from, g.to)));
  const hi = Math.max(...segs.map(g => Math.max(g.from, g.to)), 1);
  const plotTop = GRID.CONTENT_TOP + 0.45;
  const plotBot = GRID.CONTENT_BOTTOM - 0.45;
  const plotH = plotBot - plotTop;
  const yOf = v => plotBot - ((v - lo) / (hi - lo)) * plotH;
  const gap = 0.26;
  const colW = (GRID.CONTENT_W - gap * (n - 1)) / n;
  segs.forEach((g, i) => {
    const x = GRID.MARGIN_X + i * (colW + gap);
    const yTop = yOf(Math.max(g.from, g.to));
    const yBot = yOf(Math.min(g.from, g.to));
    const h = Math.max(0.06, yBot - yTop);
    const up = g.to >= g.from;
    const fill = g.isTotal ? theme.accent
      : (up ? tintJS(theme.accent, 0.34) : shadeJS(theme.bg, 0.28));
    slide.addShape(cardShapeType(pres), {
      x: x + colW * 0.12, y: yTop, w: colW * 0.76, h,
      fill: { color: fill }, line: { color: fill },
      rectRadius: Math.min(STYLE.radius, 0.06),
    });
    slide.addText(String(g.s.value != null ? g.s.value : ""), {
      x, y: yTop - 0.34, w: colW, h: 0.32,
      fontFace: fonts.header, fontSize: STYLE.type.caption, color: tc.title,
      bold: true, align: "center", margin: 0, wrap: false, fit: "shrink",
    });
    slide.addText(String(g.s.label || ""), {
      x, y: plotBot + 0.06, w: colW, h: 0.34,
      fontFace: fonts.body, fontSize: STYLE.type.caption, color: tc.body,
      align: "center", margin: 0, fit: "shrink",
    });
  });
}

// venn — 2-3 overlapping sets.
function contentVenn(pres, slide, spec, theme, fonts, tc) {
  tc = tc || textColorsForBg(theme);
  const sets = (spec.venn || []).filter(Boolean).slice(0, 3)
    .map(v => (typeof v === "object" ? v : { label: String(v) }));
  const n = sets.length;
  if (n < 2) return;
  const cy = 3.05, D = 2.9;
  const cols = n === 2
    ? [{ x: 3.65 }, { x: 5.45 }]
    : [{ x: 3.45 }, { x: 5.55 }, { x: 4.5, y: cy + 0.95 }];
  const fills = [theme.accent, tintJS(theme.accent, 0.45), tintJS(theme.accent, 0.7)];
  sets.forEach((s, i) => {
    const cxp = cols[i].x;
    const cyp = (cols[i].y || cy) - D / 2;
    slide.addShape(pres.shapes.OVAL, {
      x: cxp - D / 2, y: cyp, w: D, h: D,
      fill: { color: fills[i], transparency: 38 },
      line: { color: fills[i], transparency: 20 },
    });
  });
  sets.forEach((s, i) => {
    const lx = n === 2 ? (i === 0 ? 2.5 : 6.0) : [3.0, 6.0, 4.5][i];
    const ly = n === 2 ? 2.0 : [1.7, 1.7, 4.55][i];
    slide.addText(String(s.label || s.title || ""), {
      x: lx - 1.0, y: ly, w: 2.0, h: 0.5,
      fontFace: fonts.header, fontSize: STYLE.type.support, color: tc.title,
      bold: true, align: "center", margin: 0, fit: "shrink",
    });
  });
}

// pros_cons — bilateral pros / cons columns.
function contentProsCons(pres, slide, spec, theme, fonts, tc) {
  tc = tc || textColorsForBg(theme);
  const pros = (spec.pros || []).filter(Boolean).map(String);
  const cons = (spec.cons || []).filter(Boolean).map(String);
  const mid = 5.0;
  const colW = mid - GRID.MARGIN_X - 0.18;
  const top = GRID.CONTENT_TOP + 0.05;
  const cardH = GRID.CONTENT_BOTTOM - top;
  const cols = [
    { x: GRID.MARGIN_X, title: spec.prosTitle || "优势 / Pros", items: pros, good: true },
    { x: mid + 0.18, title: spec.consTitle || "不足 / Cons", items: cons, good: false },
  ];
  cols.forEach(c => {
    const tint = c.good ? tintJS(theme.accent, 0.90) : tintJS(theme.secondary, 0.90);
    slide.addShape(cardShapeType(pres), withShadow({
      x: c.x, y: top, w: colW, h: cardH,
      fill: { color: CARD.fill }, line: { color: CARD.line, pt: 0.5 },
      rectRadius: STYLE.radius,
    }, cardShadow()));
    slide.addShape(pres.shapes.RECTANGLE, {
      x: c.x, y: top, w: colW, h: 0.52, fill: { color: tint }, line: { color: tint },
    });
    slide.addText(c.title, {
      x: c.x + 0.2, y: top, w: colW - 0.4, h: 0.52,
      fontFace: fonts.header, fontSize: STYLE.type.body, color: CARD.title,
      bold: true, valign: "mid", margin: 0, fit: "shrink",
    });
    slide.addText(
      c.items.map(t => ({
        text: (c.good ? "＋  " : "－  ") + t,
        options: { breakLine: true, color: c.good ? shadeJS(theme.accent, 0.05) : theme.secondary },
      })),
      {
        x: c.x + 0.24, y: top + 0.72, w: colW - 0.48, h: cardH - 0.9,
        fontFace: fonts.body, fontSize: STYLE.type.support, color: tc.body,
        paraSpaceAfterPt: 10, valign: "top", margin: 0,
      },
    );
  });
}

// pillars — 3-5 vertical columns (PEST / capability blocks).
function contentPillars(pres, slide, spec, theme, fonts, tc) {
  tc = tc || textColorsForBg(theme);
  const p = (spec.pillars || []).filter(Boolean).slice(0, 5)
    .map(x => (typeof x === "object" ? x : { title: String(x) }));
  const n = p.length;
  if (!n) return;
  const gap = GRID.GUTTER;
  const colW = (GRID.CONTENT_W - gap * (n - 1)) / n;
  const top = GRID.CONTENT_TOP + 0.05;
  const colH = GRID.CONTENT_BOTTOM - top;
  p.forEach((it, i) => {
    const x = GRID.MARGIN_X + i * (colW + gap);
    slide.addShape(cardShapeType(pres), withShadow({
      x, y: top, w: colW, h: colH,
      fill: { color: CARD.fill }, line: { color: CARD.line, pt: 0.5 },
      rectRadius: STYLE.radius,
    }, cardShadow()));
    const cap = tintJS(theme.accent, 0.10 + i * (0.5 / Math.max(1, n - 1)));
    slide.addShape(pres.shapes.RECTANGLE, {
      x, y: top, w: colW, h: 0.62, fill: { color: cap }, line: { color: cap },
    });
    slide.addText(String(it.title || ""), {
      x: x + 0.14, y: top, w: colW - 0.28, h: 0.62,
      fontFace: fonts.header, fontSize: STYLE.type.body,
      color: isDarkHex(cap) ? "FFFFFF" : "0A0A0A",
      bold: true, align: "center", valign: "mid", margin: 0, fit: "shrink",
    });
    const items = Array.isArray(it.items) ? it.items
      : (it.desc ? [it.desc] : []);
    if (items.length) {
      slide.addText(
        items.map(t => ({ text: String(t), options: { breakLine: true } })),
        {
          x: x + 0.18, y: top + 0.82, w: colW - 0.36, h: colH - 1.0,
          fontFace: fonts.body, fontSize: STYLE.type.support, color: CARD.body,
          bullet: { indent: 14 }, paraSpaceAfterPt: 8, valign: "top", margin: 0,
        },
      );
    }
  });
}

// cycle — 3-6 stages on a closed loop (PDCA / flywheel).
function contentCycle(pres, slide, spec, theme, fonts, tc) {
  tc = tc || textColorsForBg(theme);
  const cyc = (spec.cycle || []).filter(Boolean).slice(0, 6)
    .map(c => (typeof c === "object" ? c : { title: String(c) }));
  const n = cyc.length;
  if (!n) return;
  const cx = 5.0, cy = 3.05, R = 1.7;
  // Faint guide ring.
  const ringD = R * 2 + 0.9;
  slide.addShape(pres.shapes.OVAL, {
    x: cx - ringD / 2, y: cy - ringD / 2, w: ringD, h: ringD,
    fill: { color: theme.bg }, line: { color: shadeJS(theme.bg, 0.10), pt: 1 },
  });
  const stageFills = depthLadder(theme.accent, n, 0.42);
  cyc.forEach((c, i) => {
    const ang = (-90 + i * (360 / n)) * Math.PI / 180;
    const nx = cx + R * Math.cos(ang);
    const ny = cy + R * Math.sin(ang);
    const d = 1.5;
    slide.addShape(pres.shapes.OVAL, withShadow({
      x: nx - d / 2, y: ny - d / 2, w: d, h: d,
      fill: { color: stageFills[i] },
      line: { color: tintJS(theme.accent, 0.5) },
    }, cardShadow()));
    const onDark = isDarkHex(stageFills[i]);
    slide.addText(`${i + 1}. ${c.title || c.label || ""}`, {
      x: nx - d / 2, y: ny - d / 2, w: d, h: d,
      fontFace: fonts.header, fontSize: STYLE.type.support,
      color: onDark ? "FFFFFF" : "0A0A0A",
      bold: true, align: "center", valign: "mid", margin: 0.04, fit: "shrink",
    });
  });
}

// gantt — task schedule bars across a normalized timeline.
function contentGantt(pres, slide, spec, theme, fonts, tc) {
  tc = tc || textColorsForBg(theme);
  const tasks = (spec.gantt || []).filter(Boolean).slice(0, 8).map(t => ({
    name: t.task || t.title || t.label || "",
    s: Number(t.start != null ? t.start : t.from) || 0,
    e: Number(t.end != null ? t.end : t.to) || 0,
  }));
  const n = tasks.length;
  if (!n) return;
  const lo = Math.min(...tasks.map(t => t.s));
  const hi = Math.max(...tasks.map(t => t.e), lo + 1);
  const top = GRID.CONTENT_TOP + 0.15;
  const avail = GRID.CONTENT_BOTTOM - top;
  const rowH = Math.min(0.62, avail / n);
  const y0 = top + Math.max(0, (avail - rowH * n) / 2);
  const labelW = 2.4;
  const trackX = GRID.MARGIN_X + labelW + 0.15;
  const trackW = GRID.CONTENT_RIGHT - trackX;
  const barH = Math.min(0.32, rowH - 0.18);
  const rowFills = depthLadder(theme.accent, n, 0.40);
  tasks.forEach((t, i) => {
    const y = y0 + i * rowH;
    slide.addText(t.name, {
      x: GRID.MARGIN_X, y, w: labelW, h: rowH,
      fontFace: fonts.header, fontSize: STYLE.type.support, color: tc.title,
      bold: true, valign: "mid", margin: 0, fit: "shrink",
    });
    if (i < n) {
      slide.addShape(pres.shapes.RECTANGLE, {
        x: trackX, y: y + rowH - 0.006, w: trackW, h: 0.012,
        fill: { color: shadeJS(theme.bg, 0.07) }, line: { color: shadeJS(theme.bg, 0.07) },
      });
    }
    const bx = trackX + ((t.s - lo) / (hi - lo)) * trackW;
    const bw = Math.max(0.12, ((t.e - t.s) / (hi - lo)) * trackW);
    slide.addShape(cardShapeType(pres), {
      x: bx, y: y + (rowH - barH) / 2, w: bw, h: barH,
      fill: { color: rowFills[i] },
      line: { color: "FFFFFF", pt: 0.25 },
      rectRadius: Math.min(STYLE.radius, 0.05),
    });
  });
}

// ── WP11: people / social-proof / partner / closing ─────────────────────

// team — people cards (avatar plate + name + role + optional desc).
// Spec: team: [{name, role?, desc?, icon?}]  (3-8). 1 row up to 4, else wraps.
function contentTeam(pres, slide, spec, theme, fonts, tc) {
  tc = tc || textColorsForBg(theme);
  const people = (spec.team || []).filter(Boolean).slice(0, 8)
    .map(p => (typeof p === "object" ? p : { name: String(p) }));
  const n = people.length;
  if (!n) return;
  const cols = n <= 4 ? n : (n <= 6 ? 3 : 4);
  const rows = Math.ceil(n / cols);
  const gap = GRID.GUTTER;
  const availH = GRID.CONTENT_BOTTOM - GRID.CONTENT_TOP;
  const cardW = (GRID.CONTENT_W - gap * (cols - 1)) / cols;
  const cardH = (availH - gap * (rows - 1)) / rows;
  people.forEach((p, i) => {
    const r = Math.floor(i / cols), c = i % cols;
    const x = GRID.MARGIN_X + c * (cardW + gap);
    const y = GRID.CONTENT_TOP + r * (cardH + gap);
    slide.addShape(cardShapeType(pres), withShadow({
      x, y, w: cardW, h: cardH,
      fill: { color: CARD.fill },
      line: { color: CARD.line, pt: 0.75 },
      rectRadius: STYLE.radius,
    }, cardShadow()));
    const av = Math.min(0.95, cardH * 0.42);
    const avTop = y + cardH * 0.12;
    addIcon(pres, slide, {
      x: x + (cardW - av) / 2, y: avTop, diameter: av,
      color: theme.accent, name: p.icon || "team",
      hint: `${p.role || p.title || ""} ${p.desc || ""}`,
    });
    slide.addText(String(p.name || ""), {
      x: x + 0.1, y: avTop + av + 0.06, w: cardW - 0.2, h: 0.34,
      fontFace: fonts.header, fontSize: STYLE.type.support, color: CARD.title,
      bold: true, align: "center", margin: 0, fit: "shrink",
    });
    if (p.role || p.title) {
      slide.addText(String(p.role || p.title), {
        x: x + 0.1, y: avTop + av + 0.40, w: cardW - 0.2, h: 0.28,
        fontFace: fonts.body, fontSize: STYLE.type.caption, color: theme.accent,
        bold: true, align: "center", margin: 0, fit: "shrink",
      });
    }
    if ((p.desc || p.description) && rows === 1) {
      slide.addText(String(p.desc || p.description), {
        x: x + 0.16, y: avTop + av + 0.74, w: cardW - 0.32, h: cardH - (avTop + av + 0.74 - y) - 0.14,
        fontFace: fonts.body, fontSize: STYLE.type.caption, color: CARD.body,
        align: "center", valign: "top", margin: 0, fit: "shrink",
      });
    }
  });
}

// testimonial — 1-2 endorsement cards: quote + author identity.
// Spec: testimonial: {quote, name?, role?, icon?}  OR testimonials: [ ... ] (≤2)
function contentTestimonial(pres, slide, spec, theme, fonts, tc) {
  tc = tc || textColorsForBg(theme);
  let cards = Array.isArray(spec.testimonials) ? spec.testimonials
    : (spec.testimonial ? [spec.testimonial] : []);
  cards = cards.filter(Boolean)
    .map(c => (typeof c === "object" ? c : { quote: String(c) })).slice(0, 2);
  const n = cards.length;
  if (!n) return;
  const gap = GRID.GUTTER;
  const top = GRID.CONTENT_TOP + 0.1;
  const cardH = GRID.CONTENT_BOTTOM - top;
  const cardW = (GRID.CONTENT_W - gap * (n - 1)) / n;
  cards.forEach((c, i) => {
    const x = GRID.MARGIN_X + i * (cardW + gap);
    slide.addShape(cardShapeType(pres), withShadow({
      x, y: top, w: cardW, h: cardH,
      fill: { color: CARD.fill },
      line: { color: CARD.line, pt: 0.75 },
      rectRadius: Math.max(STYLE.radius, 0.06),
    }, cardShadow()));
    slide.addText("“", {
      x: x + 0.18, y: top + 0.04, w: 1.2, h: 0.9,
      fontFace: "Georgia", fontSize: Math.round(STYLE.type.large_title * 1.3),
      color: tintJS(theme.accent, 0.4), bold: true, margin: 0, valign: "top",
    });
    slide.addText(String(c.quote || c.text || ""), {
      x: x + 0.30, y: top + 0.9, w: cardW - 0.60, h: cardH - 1.95,
      fontFace: fonts.header, fontSize: STYLE.type.body + (n === 1 ? 3 : 1),
      color: CARD.title, italic: true, valign: "top", margin: 0, fit: "shrink",
    });
    const ay = top + cardH - 0.88;
    const av = 0.62;
    addIcon(pres, slide, {
      x: x + 0.30, y: ay, diameter: av, color: theme.accent,
      name: c.icon || "users", hint: c.role || c.title || "",
    });
    slide.addText(String(c.name || c.author || ""), {
      x: x + 0.30 + av + 0.16, y: ay + 0.02, w: cardW - 0.30 - av - 0.42, h: 0.32,
      fontFace: fonts.header, fontSize: STYLE.type.support, color: CARD.title,
      bold: true, margin: 0, fit: "shrink",
    });
    if (c.role || c.title) {
      slide.addText(String(c.role || c.title), {
        x: x + 0.30 + av + 0.16, y: ay + 0.34, w: cardW - 0.30 - av - 0.42, h: 0.28,
        fontFace: fonts.body, fontSize: STYLE.type.caption, color: CARD.body,
        margin: 0, fit: "shrink",
      });
    }
  });
}

// logo_wall — partner / client wall. Each cell: a logo image (base64) when
// provided, else an icon + name chip. Spec: logos: [{name, data_base64?/image?, icon?}] (4-12)
function contentLogoWall(pres, slide, spec, theme, fonts, tc) {
  tc = tc || textColorsForBg(theme);
  const items = (spec.logos || []).filter(Boolean).slice(0, 12)
    .map(l => (typeof l === "object" ? l : { name: String(l) }));
  const n = items.length;
  if (!n) return;
  const cols = n <= 3 ? n : (n === 4 ? 2 : (n <= 6 ? 3 : 4));
  const rows = Math.ceil(n / cols);
  const gap = GRID.GUTTER;
  const availH = GRID.CONTENT_BOTTOM - GRID.CONTENT_TOP;
  const cardW = (GRID.CONTENT_W - gap * (cols - 1)) / cols;
  const cardH = (availH - gap * (rows - 1)) / rows;
  items.forEach((it, i) => {
    const r = Math.floor(i / cols), c = i % cols;
    const x = GRID.MARGIN_X + c * (cardW + gap);
    const y = GRID.CONTENT_TOP + r * (cardH + gap);
    slide.addShape(cardShapeType(pres), withShadow({
      x, y, w: cardW, h: cardH,
      fill: { color: CARD.fill },
      line: { color: shadeJS(theme.bg, 0.08), pt: 0.75 },
      rectRadius: STYLE.radius,
    }, cardShadow()));
    const b64 = it.data_base64 || it.image;
    if (typeof b64 === "string" && b64.length > 50) {
      const pad = Math.min(cardW, cardH) * 0.18;
      slide.addImage({
        data: `data:${it.mime || "image/png"};base64,${b64}`,
        x: x + pad, y: y + pad, w: cardW - 2 * pad, h: cardH - 2 * pad,
        sizing: { type: "contain", w: cardW - 2 * pad, h: cardH - 2 * pad },
      });
      return;
    }
    const concept = it.icon || _inferIcon(it.name);
    let textY = y + cardH * 0.32;
    if (concept && TABLER.iconPaths(concept)) {
      const d = Math.min(0.62, cardH * 0.40);
      addIcon(pres, slide, { x: x + (cardW - d) / 2, y: y + cardH * 0.16, diameter: d, color: theme.accent, name: concept });
      textY = y + cardH * 0.16 + d + 0.06;
    }
    slide.addText(String(it.name || ""), {
      x: x + 0.1, y: textY, w: cardW - 0.2, h: 0.4,
      fontFace: fonts.header, fontSize: STYLE.type.support, color: CARD.title,
      bold: true, align: "center", valign: "mid", margin: 0, fit: "shrink",
    });
  });
}

// closing — a deliberate sign-off page (谢谢 / Thank you), distinct from the
// summary card. Brand-filled by default; contact row + optional QR.
// Spec: { type:"closing", title?, subtitle?, contact?:[{icon?,label?,value}], qr?(base64), closing_style?:"dark"|"light" }
function renderClosing(pres, slide, spec, theme, fonts, index) {
  const style = String(spec.closing_style || "dark").toLowerCase();
  const dark = style !== "light";
  const bg = dark ? brandBg(theme) : theme.bg;
  const onDark = isDarkHex(bg);
  slide.background = anchorBackground(theme, onDark ? "dark" : "light", bg);
  const titleColor = onDark ? "FFFFFF" : theme.primary;
  const subColor = onDark ? tintJS(bg, 0.62) : theme.secondary;
  const motif = onDark ? "FFFFFF" : theme.accent;

  // Soft ghost graphic for depth (mirrors the cover).
  const ghost = onDark ? tintJS(bg, 0.10) : tintJS(theme.accent, 0.90);
  slide.addShape(pres.shapes.OVAL, {
    x: -1.5, y: 3.1, w: 4.8, h: 4.8, fill: { color: ghost }, line: { color: ghost },
  });

  addAccentDot(pres, slide, 5.0 - 0.11, 1.30, 0.22, motif);
  slide.addText(String(spec.title || "谢谢观看"), {
    x: 0.5, y: 1.70, w: 9.0, h: 1.4,
    fontFace: fonts.header, fontSize: STYLE.type.large_title, color: titleColor,
    bold: true, align: "center", valign: "mid", margin: 0, charSpacing: 1, fit: "shrink",
  });
  if (spec.subtitle) {
    slide.addText(String(spec.subtitle), {
      x: 1.0, y: 3.12, w: 8.0, h: 0.5,
      fontFace: fonts.body, fontSize: STYLE.type.subtitle, color: subColor,
      align: "center", margin: 0, fit: "shrink",
    });
  }

  const contacts = (Array.isArray(spec.contact) ? spec.contact : [])
    .filter(Boolean).map(c => (typeof c === "object" ? c : { value: String(c) })).slice(0, 4);
  if (contacts.length) {
    // Wider chips when there are few contacts so long emails/URLs don't wrap.
    const cw = contacts.length <= 2 ? 3.4 : (contacts.length === 3 ? 2.6 : 2.05);
    const gap = 0.30;
    const total = contacts.length * cw + (contacts.length - 1) * gap;
    let cx = 5.0 - total / 2;
    contacts.forEach(c => {
      const concept = c.icon || _inferIcon(String(c.label || "")) || "mail";
      const d = 0.42;
      addIcon(pres, slide, { x: cx, y: 4.02, diameter: d, color: theme.accent, name: concept });
      slide.addText(String(c.value || c.label || ""), {
        x: cx + d + 0.10, y: 4.02, w: cw - d - 0.10, h: d,
        fontFace: fonts.body, fontSize: STYLE.type.caption,
        color: onDark ? tintJS(bg, 0.72) : theme.secondary,
        valign: "mid", margin: 0, fit: "shrink",
      });
      cx += cw + gap;
    });
  }

  const qr = spec.qr_base64 || spec.qr;
  if (typeof qr === "string" && qr.length > 50) {
    slide.addImage({ data: `data:image/png;base64,${qr}`, x: 4.55, y: 4.55, w: 0.9, h: 0.9 });
  }
}

// ── WP13: composite / rich components (richer multi-panel slides) ──────────

// split_feature — 三区复合："左详情卡 + 右上深色指标块 + 右下要点卡"。
// Spec: feature:{title,desc?,bullets?}, metrics:[{value,label}], points:[{icon,title,desc}]
function contentSplitFeature(pres, slide, spec, theme, fonts, tc) {
  tc = tc || textColorsForBg(theme);
  const f = spec.feature || {};
  const metrics = (spec.metrics || []).filter(Boolean).slice(0, 4)
    .map(m => (typeof m === "object" ? m : { value: String(m) }));
  const points = (spec.points || []).filter(Boolean).slice(0, 4)
    .map(p => (typeof p === "object" ? p : { title: String(p) }));
  const top = GRID.CONTENT_TOP;
  const H = GRID.CONTENT_BOTTOM - top;
  const gap = GRID.GUTTER;
  const leftW = GRID.CONTENT_W * 0.44;
  const rightX = GRID.MARGIN_X + leftW + gap;
  const rightW = GRID.CONTENT_RIGHT - rightX;

  // ── left detail card ──
  slide.addShape(cardShapeType(pres), withShadow({
    x: GRID.MARGIN_X, y: top, w: leftW, h: H,
    fill: { color: CARD.fill }, line: { color: CARD.line, pt: 0.75 },
    rectRadius: STYLE.radius,
  }, cardShadow()));
  slide.addText(String(f.title || f.kicker || "功能介绍"), {
    x: GRID.MARGIN_X + 0.26, y: top + 0.22, w: leftW - 0.52, h: 0.42,
    fontFace: fonts.header, fontSize: STYLE.type.subtitle - 1, color: tc.title, bold: true, margin: 0, fit: "shrink",
  });
  slide.addShape(pres.shapes.RECTANGLE, { x: GRID.MARGIN_X + 0.26, y: top + 0.70, w: 0.6, h: 0.045, fill: { color: theme.accent }, line: { color: theme.accent } });
  let ly = top + 0.88;
  if (f.desc) {
    slide.addText(String(f.desc), {
      x: GRID.MARGIN_X + 0.26, y: ly, w: leftW - 0.52, h: 0.8,
      fontFace: fonts.body, fontSize: STYLE.type.support, color: tc.body, valign: "top", margin: 0, fit: "shrink",
    });
    ly += 0.9;
  }
  if (Array.isArray(f.bullets) && f.bullets.length) {
    addBullets(slide, f.bullets, { x: GRID.MARGIN_X + 0.26, y: ly, w: leftW - 0.52, h: top + H - ly - 0.18 },
      theme, fonts, { color: tc.body, fontSize: STYLE.type.support });
  }

  // ── right-top dark metric block ──
  const rtH = metrics.length ? (points.length ? H * 0.46 : H) : 0;
  if (metrics.length) {
    const navy = brandBg(theme);
    slide.addShape(cardShapeType(pres), withShadow({
      x: rightX, y: top, w: rightW, h: rtH, fill: { color: navy }, line: { color: navy }, rectRadius: STYLE.radius,
    }, cardShadow()));
    const mc = metrics.length <= 2 ? metrics.length : 2;
    const mr = Math.ceil(metrics.length / mc);
    const mw = rightW / mc, mh = rtH / mr;
    metrics.forEach((m, i) => {
      const r = Math.floor(i / mc), c = i % mc;
      const mx = rightX + c * mw, my = top + r * mh;
      slide.addText(String(m.value || m.number || ""), {
        x: mx + 0.26, y: my + mh * 0.14, w: mw - 0.45, h: mh * 0.48,
        fontFace: fonts.header, fontSize: STYLE.type.title, color: theme.accent, bold: true, valign: "mid", margin: 0, wrap: false, fit: "shrink",
      });
      slide.addText(String(m.label || m.title || ""), {
        x: mx + 0.26, y: my + mh * 0.60, w: mw - 0.45, h: mh * 0.32,
        fontFace: fonts.body, fontSize: STYLE.type.caption, color: "FFFFFF", margin: 0, fit: "shrink",
      });
    });
  }

  // ── right-bottom points card ──
  const rbY = top + (metrics.length ? rtH + gap : 0);
  const rbH = top + H - rbY;
  if (points.length && rbH > 0.4) {
    slide.addShape(cardShapeType(pres), withShadow({
      x: rightX, y: rbY, w: rightW, h: rbH, fill: { color: CARD.fill }, line: { color: CARD.line, pt: 0.75 }, rectRadius: STYLE.radius,
    }, cardShadow()));
    const ph = rbH / points.length;
    points.forEach((p, i) => {
      const py = rbY + i * ph;
      const ic = p.icon || _inferIcon(`${p.title || ""} ${p.desc || ""}`);
      const d = Math.min(0.42, ph * 0.5);
      let px = rightX + 0.26;
      if (ic && TABLER.iconPaths(ic)) {
        addIcon(pres, slide, { x: rightX + 0.24, y: py + (ph - d) / 2, diameter: d, color: theme.accent, name: ic });
        px = rightX + 0.24 + d + 0.18;
      }
      slide.addText(String(p.title || ""), {
        x: px, y: py + ph * 0.14, w: rightX + rightW - 0.2 - px, h: ph * 0.46,
        fontFace: fonts.header, fontSize: STYLE.type.support, color: tc.title, bold: true, valign: "mid", margin: 0, fit: "shrink",
      });
      if (p.desc || p.description) {
        slide.addText(String(p.desc || p.description), {
          x: px, y: py + ph * 0.52, w: rightX + rightW - 0.2 - px, h: ph * 0.42,
          fontFace: fonts.body, fontSize: STYLE.type.caption, color: tc.body, valign: "mid", margin: 0, fit: "shrink",
        });
      }
    });
  }
}

// compare_panels — 2-3 个带色头的对照面板（迁移前/后、现状/目标、优势/劣势）。
// Spec: panels:[{label, tone?, bullets:[...]}]  tone: neg/pos/neutral → 红/绿/强调
function contentComparePanels(pres, slide, spec, theme, fonts, tc) {
  tc = tc || textColorsForBg(theme);
  const panels = (spec.panels || []).filter(Boolean).slice(0, 3)
    .map(p => (typeof p === "object" ? p : { label: String(p) }));
  const n = panels.length;
  if (!n) return;
  const gap = GRID.GUTTER;
  const top = GRID.CONTENT_TOP + 0.05;
  const H = GRID.CONTENT_BOTTOM - top;
  const w = (GRID.CONTENT_W - gap * (n - 1)) / n;
  const toneColor = (t) => {
    const s = String(t || "").toLowerCase();
    if (/neg|before|bad|con|前|旧|劣|风险|问题|痛/.test(s)) return "D7493A";
    if (/pos|after|good|pro|后|新|优|提升|收益|成效/.test(s)) return "2E9E5B";
    return theme.accent;
  };
  panels.forEach((p, i) => {
    const x = GRID.MARGIN_X + i * (w + gap);
    const col = p.color ? normalizeHex(p.color, theme.accent) : toneColor(p.tone || p.label);
    slide.addShape(cardShapeType(pres), withShadow({
      x, y: top, w, h: H, fill: { color: tintJS(col, 0.90) }, line: { color: tintJS(col, 0.6), pt: 0.75 }, rectRadius: STYLE.radius,
    }, cardShadow()));
    slide.addText(String(p.label || p.title || ""), {
      x: x + 0.26, y: top + 0.22, w: w - 0.52, h: 0.42,
      fontFace: fonts.header, fontSize: STYLE.type.subtitle - 2, color: col, bold: true, margin: 0, fit: "shrink",
    });
    slide.addShape(pres.shapes.RECTANGLE, { x: x + 0.26, y: top + 0.70, w: 0.6, h: 0.045, fill: { color: col }, line: { color: col } });
    addBullets(slide, p.bullets || p.items, { x: x + 0.26, y: top + 0.92, w: w - 0.52, h: H - 1.1 },
      theme, fonts, { color: shadeJS(col, 0.35), fontSize: STYLE.type.support });
  });
}

// card_list — 纵向卡片列表：左色条 + 图标 + 标题 + 说明（资质/清单/能力卡）。
// Spec: cards:[{icon, title, desc/meta, color?}]  (3-6)
function contentCardList(pres, slide, spec, theme, fonts, tc) {
  tc = tc || textColorsForBg(theme);
  const cards = (spec.cards || []).filter(Boolean).slice(0, 6)
    .map(c => (typeof c === "object" ? c : { title: String(c) }));
  const n = cards.length;
  if (!n) return;
  const gap = 0.16;
  const top = GRID.CONTENT_TOP;
  const H = GRID.CONTENT_BOTTOM - top;
  const cardH = (H - gap * (n - 1)) / n;
  const palette = [theme.accent, ...MULTI_ACCENTS];
  const onDarkBg = isDarkHex(theme.bg);
  const fill = onDarkBg ? tintJS(theme.bg, 0.08) : "FFFFFF";
  cards.forEach((c, i) => {
    const y = top + i * (cardH + gap);
    const col = c.color ? normalizeHex(c.color, palette[i % palette.length]) : palette[i % palette.length];
    slide.addShape(cardShapeType(pres), withShadow({
      x: GRID.MARGIN_X, y, w: GRID.CONTENT_W, h: cardH, fill: { color: fill }, line: { color: shadeJS(theme.bg, 0.08), pt: 0.75 }, rectRadius: STYLE.radius,
    }, cardShadow()));
    slide.addShape(pres.shapes.RECTANGLE, { x: GRID.MARGIN_X, y, w: 0.09, h: cardH, fill: { color: col }, line: { color: col } });
    const ic = c.icon || _inferIcon(`${c.title || ""} ${c.desc || c.meta || ""}`);
    let tx = GRID.MARGIN_X + 0.34;
    if (ic && TABLER.iconPaths(ic)) {
      const d = Math.min(0.5, cardH * 0.52);
      addIcon(pres, slide, { x: GRID.MARGIN_X + 0.28, y: y + (cardH - d) / 2, diameter: d, color: col, name: ic });
      tx = GRID.MARGIN_X + 0.28 + d + 0.24;
    }
    // A card may carry either a one-line meta/desc OR a multi-bullet list.
    const hasBullets = Array.isArray(c.bullets) && c.bullets.length;
    const meta = c.desc || c.meta || c.subtitle || c.description || "";
    const tw = GRID.CONTENT_RIGHT - tx - 0.2;
    if (hasBullets) {
      slide.addText(String(c.title || ""), {
        x: tx, y: y + 0.12, w: tw, h: 0.36,
        fontFace: fonts.header, fontSize: STYLE.type.support, color: tc.title, bold: true, valign: "mid", margin: 0, fit: "shrink",
      });
      addBullets(slide, c.bullets, { x: tx, y: y + 0.5, w: tw, h: cardH - 0.6 },
        theme, fonts, { color: tc.body, fontSize: STYLE.type.caption });
    } else {
      slide.addText(String(c.title || ""), {
        x: tx, y: meta ? y + cardH * 0.14 : y, w: tw, h: meta ? cardH * 0.44 : cardH,
        fontFace: fonts.header, fontSize: STYLE.type.support, color: tc.title, bold: true, valign: "mid", margin: 0, fit: "shrink",
      });
      if (meta) {
        slide.addText(String(meta), {
          x: tx, y: y + cardH * 0.52, w: tw, h: cardH * 0.40,
          fontFace: fonts.body, fontSize: STYLE.type.caption, color: tc.body, valign: "mid", margin: 0, fit: "shrink",
        });
      }
    }
  });
}

// icon_cards — 图标卡片网格（图标圈 + 中/EN 标题 + 说明），可选顶部深色 banner 警示条
// 与底部 footer 备注条。Spec: tiles:[{icon,title,sub?,desc}], banner?, footer?
function contentIconCards(pres, slide, spec, theme, fonts, tc) {
  tc = tc || textColorsForBg(theme);
  const tiles = (spec.tiles || []).filter(Boolean).slice(0, 6)
    .map(t => (typeof t === "object" ? t : { title: String(t) }));
  const n = tiles.length;
  if (!n) return;
  let top = GRID.CONTENT_TOP;
  let bottom = GRID.CONTENT_BOTTOM;
  if (spec.banner) {
    const bt = typeof spec.banner === "object" ? (spec.banner.text || spec.banner.title || "") : String(spec.banner);
    const navy = brandBg(theme);
    const bh = 0.6;
    slide.addShape(cardShapeType(pres), { x: GRID.MARGIN_X, y: top, w: GRID.CONTENT_W, h: bh, fill: { color: navy }, line: { color: navy }, rectRadius: STYLE.radius });
    slide.addShape(pres.shapes.RECTANGLE, { x: GRID.MARGIN_X, y: top, w: 0.09, h: bh, fill: { color: theme.accent }, line: { color: theme.accent } });
    slide.addText(String(bt), {
      x: GRID.MARGIN_X + 0.28, y: top, w: GRID.CONTENT_W - 0.54, h: bh,
      fontFace: fonts.body, fontSize: STYLE.type.support, color: "FFFFFF", bold: true, valign: "mid", margin: 0, fit: "shrink",
    });
    top += bh + 0.18;
  }
  const footerStr = spec.footer ? (typeof spec.footer === "object" ? (spec.footer.text || "") : String(spec.footer)) : "";
  if (footerStr) {
    const fh = 0.44;
    bottom -= fh + 0.14;
    slide.addShape(cardShapeType(pres), { x: GRID.MARGIN_X, y: bottom + 0.14, w: GRID.CONTENT_W, h: fh, fill: { color: tintJS(theme.accent, 0.88) }, line: { color: tintJS(theme.accent, 0.7), pt: 0.5 }, rectRadius: STYLE.radius });
    slide.addText(String(footerStr), {
      x: GRID.MARGIN_X + 0.26, y: bottom + 0.14, w: GRID.CONTENT_W - 0.52, h: fh,
      fontFace: fonts.body, fontSize: STYLE.type.caption, color: shadeJS(theme.accent, 0.4), valign: "mid", margin: 0, fit: "shrink",
    });
  }
  const cols = n <= 2 ? n : (n <= 4 ? 2 : 3);
  const rows = Math.ceil(n / cols);
  const gap = GRID.GUTTER;
  const availH = bottom - top;
  const cw = (GRID.CONTENT_W - gap * (cols - 1)) / cols;
  const ch = (availH - gap * (rows - 1)) / rows;
  const palette = [theme.accent, ...MULTI_ACCENTS];
  tiles.forEach((t, i) => {
    const r = Math.floor(i / cols), c = i % cols;
    const x = GRID.MARGIN_X + c * (cw + gap), y = top + r * (ch + gap);
    const col = palette[i % palette.length];
    slide.addShape(cardShapeType(pres), withShadow({
      x, y, w: cw, h: ch, fill: { color: CARD.fill }, line: { color: CARD.line, pt: 0.75 }, rectRadius: STYLE.radius,
    }, cardShadow()));
    const pad = 0.22;
    const ic = t.icon || _inferIcon(`${t.title || ""} ${t.desc || ""}`);
    const hasIcon = ic && TABLER.iconPaths(ic);
    const d = Math.min(0.62, ch * 0.36);
    if (hasIcon) addIcon(pres, slide, { x: x + pad, y: y + pad, diameter: d, color: col, name: ic });
    const tx = x + pad + (hasIcon ? d + 0.18 : 0);
    const titleRuns = [{ text: String(t.title || ""), options: { bold: true } }];
    if (t.sub) titleRuns.push({ text: "  " + String(t.sub), options: { color: col, bold: true } });
    slide.addText(titleRuns, {
      x: tx, y: y + pad - 0.02, w: x + cw - pad - tx, h: Math.max(0.4, d),
      fontFace: fonts.header, fontSize: STYLE.type.support, color: tc.title, valign: "mid", margin: 0, fit: "shrink",
    });
    if (t.desc || t.description) {
      const dy = y + pad + d + 0.08;
      slide.addText(String(t.desc || t.description), {
        x: x + pad, y: dy, w: cw - pad * 2, h: y + ch - pad - dy,
        fontFace: fonts.body, fontSize: STYLE.type.caption, color: tc.body, valign: "top", margin: 0, fit: "shrink",
      });
    }
  });
}

// journey — 横向流程：图标圆圈 + 箭头连接 + 标题/说明（流程/演进/生命周期）。
// Spec: journey:[{icon,title,desc}]  (3-5)
function contentJourney(pres, slide, spec, theme, fonts, tc) {
  tc = tc || textColorsForBg(theme);
  const steps = (spec.journey || []).filter(Boolean).slice(0, 5)
    .map(s => (typeof s === "object" ? s : { title: String(s) }));
  const n = steps.length;
  if (!n) return;
  const yC = 2.45;
  const slot = GRID.CONTENT_W / n;
  const d = Math.min(1.05, slot - 0.5);
  const accentDark = isDarkHex(theme.accent);
  steps.forEach((s, i) => {
    const cx = GRID.MARGIN_X + slot * i + slot / 2;
    if (i > 0) {
      const pcx = GRID.MARGIN_X + slot * (i - 1) + slot / 2;
      const ax = pcx + d / 2 + 0.12, aw = (cx - d / 2) - (pcx + d / 2) - 0.24;
      if (aw > 0.05) slide.addShape(pres.shapes.RECTANGLE, { x: ax, y: yC - 0.02, w: aw, h: 0.045, fill: { color: tintJS(theme.accent, 0.45) }, line: { color: tintJS(theme.accent, 0.45) } });
    }
    slide.addShape(pres.shapes.OVAL, { x: cx - d / 2, y: yC - d / 2, w: d, h: d, fill: { color: theme.accent }, line: { color: theme.accent } });
    const ic = s.icon || _inferIcon(`${s.title || ""} ${s.desc || ""}`);
    const vec = ic && TABLER.iconPaths(ic);
    if (vec) {
      const id = d * 0.5;
      slide.addImage({ data: _tablerDataUri(vec, accentDark ? "FFFFFF" : "0A0A0A"), x: cx - id / 2, y: yC - id / 2, w: id, h: id, sizing: { type: "contain", w: id, h: id } });
    } else {
      slide.addText(String(i + 1), { x: cx - d / 2, y: yC - d / 2, w: d, h: d, align: "center", valign: "mid", fontFace: fonts.header, fontSize: Math.floor(d * 30), color: accentDark ? "FFFFFF" : "0A0A0A", bold: true, margin: 0 });
    }
    slide.addText(String(s.title || ""), {
      x: cx - slot / 2 + 0.1, y: yC + d / 2 + 0.14, w: slot - 0.2, h: 0.36, align: "center",
      fontFace: fonts.header, fontSize: STYLE.type.support, color: tc.title, bold: true, margin: 0, fit: "shrink",
    });
    if (s.desc || s.description) {
      slide.addText(String(s.desc || s.description), {
        x: cx - slot / 2 + 0.12, y: yC + d / 2 + 0.54, w: slot - 0.24, h: 1.0, align: "center",
        fontFace: fonts.body, fontSize: STYLE.type.caption, color: tc.body, valign: "top", margin: 0, fit: "shrink",
      });
    }
  });
}

// commands — 标签命令行清单：色块标签 + 等宽命令 + 说明（脚本/接口/配置/操作）。
// Spec: commands:[{label, code, desc}]  (3-6)
function contentCommands(pres, slide, spec, theme, fonts, tc) {
  tc = tc || textColorsForBg(theme);
  const cmds = (spec.commands || []).filter(Boolean).slice(0, 6)
    .map(c => (typeof c === "object" ? c : { code: String(c) }));
  const n = cmds.length;
  if (!n) return;
  const gap = 0.14;
  const top = GRID.CONTENT_TOP;
  const H = GRID.CONTENT_BOTTOM - top;
  const rowH = (H - gap * (n - 1)) / n;
  const labelW = 1.15;
  const codeX = GRID.MARGIN_X + 0.16 + labelW + 0.22;
  const descX = codeX + GRID.CONTENT_W * 0.46;
  cmds.forEach((c, i) => {
    const y = top + i * (rowH + gap);
    slide.addShape(cardShapeType(pres), withShadow({
      x: GRID.MARGIN_X, y, w: GRID.CONTENT_W, h: rowH, fill: { color: CARD.fill }, line: { color: CARD.line, pt: 0.5 }, rectRadius: STYLE.radius,
    }, cardShadow()));
    if (c.label) {
      slide.addShape(cardShapeType(pres), { x: GRID.MARGIN_X + 0.16, y: y + rowH / 2 - 0.18, w: labelW, h: 0.36, fill: { color: tintJS(theme.accent, 0.85) }, line: { color: tintJS(theme.accent, 0.85) }, rectRadius: 0.05 });
      slide.addText(String(c.label), { x: GRID.MARGIN_X + 0.16, y: y + rowH / 2 - 0.18, w: labelW, h: 0.36, align: "center", valign: "mid", fontFace: fonts.header, fontSize: STYLE.type.caption, color: shadeJS(theme.accent, 0.3), bold: true, margin: 0, fit: "shrink" });
    }
    const hasDesc = !!(c.desc || c.description);
    slide.addText(String(c.code || c.cmd || c.title || ""), {
      x: codeX, y, w: hasDesc ? descX - codeX - 0.2 : GRID.CONTENT_RIGHT - codeX - 0.2, h: rowH,
      fontFace: "Consolas", fontSize: STYLE.type.support, color: tc.title, bold: true, valign: "mid", margin: 0, fit: "shrink",
    });
    if (hasDesc) {
      slide.addText(String(c.desc || c.description), {
        x: descX, y, w: GRID.CONTENT_RIGHT - descX - 0.2, h: rowH,
        fontFace: fonts.body, fontSize: STYLE.type.caption, color: tc.body, valign: "mid", margin: 0, fit: "shrink",
      });
    }
  });
}

// milestones — 里程碑横条：左色边 + 大号值/标签 + 标题 + 说明（关键成果/三大里程碑）。

// milestones — 里程碑横条：左色边 + 大号值/标签 + 标题 + 说明（关键成果/三大里程碑）。
// Spec: milestones:[{value/label, title, desc}]  (3-5)
function contentMilestones(pres, slide, spec, theme, fonts, tc) {
  tc = tc || textColorsForBg(theme);
  const ms = (spec.milestones || []).filter(Boolean).slice(0, 5)
    .map(m => (typeof m === "object" ? m : { title: String(m) }));
  const n = ms.length;
  if (!n) return;
  const gap = 0.18;
  const top = GRID.CONTENT_TOP;
  const H = GRID.CONTENT_BOTTOM - top;
  const barH = (H - gap * (n - 1)) / n;
  const onDarkBg = isDarkHex(theme.bg);
  const fill = onDarkBg ? tintJS(theme.bg, 0.08) : tintJS(theme.primary, 0.93);
  const valColor = onDarkBg ? theme.accent : theme.primary;
  ms.forEach((m, i) => {
    const y = top + i * (barH + gap);
    slide.addShape(cardShapeType(pres), withShadow({
      x: GRID.MARGIN_X, y, w: GRID.CONTENT_W, h: barH, fill: { color: fill }, line: { color: shadeJS(theme.bg, 0.06), pt: 0.5 }, rectRadius: STYLE.radius,
    }, cardShadow()));
    slide.addShape(pres.shapes.RECTANGLE, { x: GRID.MARGIN_X, y, w: 0.10, h: barH, fill: { color: theme.accent }, line: { color: theme.accent } });
    slide.addText(String(m.value || m.label || m.number || (i + 1)), {
      x: GRID.MARGIN_X + 0.34, y, w: 2.0, h: barH,
      fontFace: fonts.header, fontSize: STYLE.type.title, color: valColor, bold: true, valign: "mid", margin: 0, wrap: false, fit: "shrink",
    });
    slide.addText(String(m.title || ""), {
      x: GRID.MARGIN_X + 2.5, y: y + barH * 0.16, w: GRID.CONTENT_W - 2.8, h: barH * 0.42,
      fontFace: fonts.header, fontSize: STYLE.type.support, color: tc.title, bold: true, valign: "mid", margin: 0, fit: "shrink",
    });
    if (m.desc || m.description) {
      slide.addText(String(m.desc || m.description), {
        x: GRID.MARGIN_X + 2.5, y: y + barH * 0.54, w: GRID.CONTENT_W - 2.8, h: barH * 0.42,
        fontFace: fonts.body, fontSize: STYLE.type.caption, color: tc.body, valign: "mid", margin: 0, fit: "shrink",
      });
    }
  });
}

function renderSummary(pres, slide, spec, theme, fonts, index) {
  // Default to light closer (政府汇报风格); "dark" mirrors a dark cover.
  const style = String(spec.summary_style || "light").toLowerCase();
  const dark = style === "dark";
  // Dark summary fills with the theme's BRAND color (blue for a blue theme),
  // not a black slab — mirrors the dark cover. See brandBg().
  const bg = dark ? brandBg(theme) : theme.bg;
  const onDark = dark && isDarkHex(bg);
  // Light summary on a dark-bg palette: theme.primary is dark and would be
  // invisible against the dark bg. textColorsForBg picks white when bg is
  // dark, primary when bg is light — symmetric with content pages and cover.
  const tcLight = textColorsForBg(theme);
  const titleColor = dark ? (onDark ? "FFFFFF" : "0A0A0A") : tcLight.title;
  const bulletColor = dark ? theme.primary : theme.secondary;  // bullets sit on a white card
  const motif = dark ? (onDark ? "FFFFFF" : "0A0A0A") : theme.accent;

  slide.background = anchorBackground(theme, isDarkHex(bg) ? "dark" : "light", bg);
  addTitle(pres, slide, spec.title || "总结", theme, fonts, {
    y: 0.52, color: titleColor, kicker: spec.kicker, kickerColor: motif,
  });

  // Accent dot motif — top-right, clear of the left-anchored title.
  addAccentDot(pres, slide, GRID.CONTENT_RIGHT - 0.18, 0.55, 0.18, motif);

  // WP3: the summary card is THE one elevated element → raised shadow.
  slide.addShape(pres.shapes.ROUNDED_RECTANGLE, withShadow({
    x: GRID.MARGIN_X, y: GRID.CONTENT_TOP, w: GRID.CONTENT_W, h: 3.34,
    fill: { color: CARD.fill },
    line: { color: shadeJS(theme.bg, 0.06), pt: 0.5 },
    rectRadius: Math.max(STYLE.radius, 0.06),
  }, floatShadow()));

  // Accept the content list under any of the field names the model reaches for
  // — bullets / highlights / items / points. Without this, a summary authored
  // with `items` (a very common slip) renders an EMPTY white card. Entries may
  // be strings or objects ({title/label/text/desc}).
  const bulletSrc =
    (Array.isArray(spec.bullets) && spec.bullets.length) ? spec.bullets :
    (Array.isArray(spec.highlights) && spec.highlights.length) ? spec.highlights :
    (Array.isArray(spec.items) && spec.items.length) ? spec.items :
    (Array.isArray(spec.points) && spec.points.length) ? spec.points : [];
  const bullets = bulletSrc
    .filter(Boolean)
    .map(b => typeof b === "object" ? String(b.title || b.label || b.text || b.desc || "") : String(b))
    .filter(s => s.trim() !== "");
  if (bullets.length) {
    slide.addText(
      bullets.map(text => ([
        { text: "✓  ", options: { color: theme.accent, bold: true } },
        { text: String(text), options: { breakLine: true } },
      ])).flat(),
      {
        x: GRID.MARGIN_X + 0.30, y: GRID.CONTENT_TOP + 0.30, w: GRID.CONTENT_W - 0.6, h: 2.30,
        fontFace: fonts.body,
        fontSize: STYLE.type.subtitle,
        color: dark ? theme.primary : theme.secondary,
        paraSpaceAfterPt: STYLE.density === "relaxed" ? 12 : 8,
        valign: "top",
      },
    );
  }

  if (spec.body) {
    slide.addText(String(spec.body), {
      x: GRID.MARGIN_X + 0.30, y: 4.22, w: GRID.CONTENT_W - 0.6, h: 0.40,
      fontFace: fonts.body, fontSize: STYLE.type.caption,
      color: CARD.body,
      italic: true,
      fit: "shrink",
    });
  }

  addBadge(pres, slide, theme, index, { onDark });
  // Optional image on summary (rarely used)
  if (spec.image && typeof spec.image === "object") {
    placeSlideImage(slide, spec.image, theme, fonts);
  }
}

function renderSlides(pres, slides, theme, fonts) {
  // Real chapter titles, used as the TOC fallback when the model leaves 目录
  // entries as placeholders / filler (see renderToc).
  const sectionTitles = slides
    .filter(s => String(s && s.type || "").toLowerCase() === "section")
    .map(s => String(s.title || "").trim())
    .filter(Boolean);
  // Per-slide layout actually chosen by the engine — returned so the Python
  // caller can run layout-variety diagnostics against ground truth instead of
  // re-deriving the layout with a drift-prone mirror of detectLayout().
  const layouts = [];
  slides.forEach((spec, idx) => {
    const slide = pres.addSlide();
    SHADOW_BUDGET = makeShadowBudget(3);  // fresh depth budget per slide
    const type = String(spec.type || "content").toLowerCase();
    const pageIndex = idx + 1;
    let layout = type;  // anchor pages (cover/toc/section/summary/closing) → type
    if (type === "cover") renderCover(pres, slide, spec, theme, fonts);
    else if (type === "toc") renderToc(pres, slide, spec, theme, fonts, pageIndex, sectionTitles);
    else if (type === "section") renderSection(pres, slide, spec, theme, fonts, pageIndex);
    else if (type === "summary") renderSummary(pres, slide, spec, theme, fonts, pageIndex);
    else if (type === "closing" || type === "thanks") renderClosing(pres, slide, spec, theme, fonts, pageIndex);
    // A content slide with nothing renderable would otherwise fall through to
    // contentSingle and produce a blank page — render a graceful closing card
    // instead (fixes the "最后一页是空的" failure).
    else if (isEmptyContentSpec(spec)) { renderClosing(pres, slide, spec, theme, fonts, pageIndex); layout = "closing"; }
    else layout = renderContent(pres, slide, spec, theme, fonts, pageIndex) || "single";
    layouts.push({ index: pageIndex, type, layout });
  });
  return layouts;
}

async function loadSpec() {
  const args = parseArgs(process.argv.slice(2));
  if (args.input) {
    return { spec: JSON.parse(fs.readFileSync(args.input, "utf8")), outOverride: args.out };
  }
  const raw = (await readStdin()).trim();
  if (!raw) {
    throw new Error("No JSON payload provided on stdin");
  }
  return { spec: JSON.parse(raw), outOverride: args.out };
}

async function main() {
  const PptxGenJS = loadPptxGenJS();
  const { spec, outOverride } = await loadSpec();
  if (!Array.isArray(spec.slides) || spec.slides.length === 0) {
    throw new Error("slides must be a non-empty array");
  }

  const theme = resolveTheme(spec.theme);
  const fonts = resolveFonts(spec.fonts);
  STYLE = resolveStyle(spec.style);
  GRADIENT_BG = spec.bg_gradient !== false;   // WP9: on by default, opt-out per deck
  // Resolve the canonical theme name (string themes only) so a theme can carry
  // a default design pack. spec.pack always wins; object themes have no name.
  const themeName = (typeof spec.theme === "string")
    ? (ALIASES[spec.theme] || spec.theme).toLowerCase() : null;
  PACK = String(spec.pack || (themeName && THEME_DEFAULT_PACK[themeName]) || "default").toLowerCase();
  // Pack aliases — let users/specs request a pack by its human style name.
  const PACK_ALIASES = {
    "技术风格": "dark_gold", "技术风": "dark_gold", "科技风": "dark_gold",
    "深色科技": "dark_gold", "深蓝金": "dark_gold", "tech": "dark_gold", "dark": "dark_gold",
  };
  PACK = PACK_ALIASES[PACK] || PACK;
  CARD = computeCard(theme);                   // WP14: adaptive card colors (pack-aware; needs final PACK)

  const outFile = String(outOverride || spec.out || "presentation.pptx");
  const outDir = path.dirname(outFile);
  if (outDir && outDir !== ".") {
    fs.mkdirSync(outDir, { recursive: true });
  }

  const pres = new PptxGenJS();
  pres.layout = "LAYOUT_16x9";
  pres.author = String(spec.author || "HugAgentOS");
  pres.subject = String(spec.subject || spec.title || "Presentation");
  pres.title = String(spec.title || "Presentation");
  pres.company = "HugAgentOS";
  pres.lang = "zh-CN";

  const layouts = renderSlides(pres, spec.slides, theme, fonts);
  await pres.writeFile({ fileName: outFile });

  const stat = fs.statSync(outFile);
  console.log(JSON.stringify({
    status: "ok",
    out: outFile,
    slides: spec.slides.length,
    size_kb: Math.round(stat.size / 1024),
    theme,
    layouts,
  }));
}

main().catch((err) => {
  console.error(JSON.stringify({
    status: "error",
    error: String(err && err.message ? err.message : err),
  }));
  process.exit(1);
});
