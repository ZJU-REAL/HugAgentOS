#!/usr/bin/env node
/**
 * 深色模式门禁（构建前执行，与 check-i18n 同级）。
 *
 * 背景：深色模式靠 `<html data-theme="dark">` 切换 variables.css 的令牌覆盖块实现。
 * 只要新写的样式**绕过令牌**直接写死颜色，浅色下看着正常、深色下就是一块白斑——
 * 而且没人会在提交前逐个组件切两次主题。历史上踩过的四类盲区：
 *   1. CSS 声明里直接写 #fff / rgba(0,0,0,.06)；
 *   2. 自定义属性里嵌硬编码色（`--jx-chip-bg:#fff`）——批量替换脚本只扫真实声明，扫不到它；
 *   3. TSX 内联 style / SVG 属性写死颜色（`style={{ background:'#ffffff' }}`）；
 *   4. `var(--never-defined, #fff)`——兜底永远生效，等于硬编码。
 * 外加一条纪律：深色专项覆盖一律用 `:root[data-theme='dark']`，禁止 `@media (prefers-color-scheme:dark)`
 * （会和手动三档打架）。
 *
 * 门禁形态是**棘轮**而不是一刀切：`dark-mode-baseline.json` 冻结存量债，
 * 新增违规直接挂构建，修掉存量后跑 `npm run check:dark -- --update` 把基线收紧。
 * 存量只能变少、不能变多。
 *
 * 逃生舱：确实要写死颜色（暗色代码台、品牌插画、第三方主题对象）在同一行或上一行标
 * `dark-ok: 原因`（CSS 用 `/* dark-ok: … *\/`，TS 用 `// dark-ok: …`）即豁免。
 *
 * 用法：
 *   node scripts/check-dark-mode.mjs            # 门禁，超基线即退出码 1
 *   node scripts/check-dark-mode.mjs --list     # 打印全部违规明细（含基线内的）
 *   node scripts/check-dark-mode.mjs --update   # 以当前实测重写基线
 */
import { existsSync, readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { join, dirname, relative, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

const scriptDir = dirname(fileURLToPath(import.meta.url));
const frontendRoot = join(scriptDir, '..');
const srcRoot = join(frontendRoot, 'src');
const baselinePath = join(scriptDir, 'dark-mode-baseline.json');

/* CE 派生树的 overlay：`ce/overlay/src/frontend/src/**` 会在 build_ce 时**整文件替换**
   同名的 `src/frontend/src/**`。不一起扫，overlay 里的硬编码色在本仓库就完全没有反馈——
   要等派生出去之后、在另一个仓库跑构建才炸，那正是本门禁要消灭的「无反馈回路」。
   仓库根 = frontendRoot 的上两级（src/frontend → repo root）。 */
const repoRoot = join(frontendRoot, '..', '..');
const ceOverlayRoot = join(repoRoot, 'ce', 'overlay', 'src', 'frontend', 'src');

/* 桌面壳（Tauri）：`desktop/src-tauri/src/**.rs` 里用 Rust 字符串内嵌了整整几张 HTML 页面
   （登录 / 首启选模式 / 本机部署 / 关闭确认 / 服务器地址 / 更新进度），外加一条注进 SPA 文档的
   自定义标题栏。它们和前端**同属一个界面**，用户看不出边界，却因为不是 .css/.tsx 而一直在
   门禁视野外——深色模式上线后，这几张页面全是写死的浅色，桌面端深色档直接是白板。
   这正是本门禁要消灭的「无反馈回路」：改的人没有任何信号，只能靠人肉记得两边都改。 */
const desktopShellRoot = join(repoRoot, 'desktop', 'src-tauri', 'src');

const args = process.argv.slice(2);
const UPDATE = args.includes('--update');
const LIST = args.includes('--list');

/* ── 允许写死颜色的文件：调色板真源与成对主题定义 ──────────────────────
   这些文件**本身**就是「浅色一套 / 深色一套」的定义处，写死颜色是它们的职责。 */
const ALLOWED_FILES = new Set([
  'src/styles/variables.css',      // 令牌调色板真源（:root 与 :root[data-theme=dark] 两块）
  'src/styles/code-highlight.css', // 代码高亮双主题（github / github-dark 两套 token 色）
  'src/theme.ts',                  // antd ConfigProvider 的成对主题对象
]);

/* 目录级跳过 */
const SKIP_DIRS = new Set(['node_modules', 'dist', 'assets', 'i18n']);

/* ── 颜色字面量识别 ───────────────────────────────────────────────
   命名色只收「会造成明暗事故」的那批（白/黑/灰系）；rebeccapurple 之流不管。 */
const NAMED_COLORS =
  /\b(white|black|whitesmoke|snow|ivory|floralwhite|ghostwhite|gainsboro|lightgray|lightgrey|silver|darkgray|darkgrey|dimgray|dimgrey|gray|grey)\b/i;
const HEX = /#[0-9a-fA-F]{3,8}\b/;
const FUNC_COLOR = /\b(rgba?|hsla?|hwb|lab|lch|oklab|oklch)\s*\(/i;

/** `var(--已定义令牌, #fff)` 的兜底永远走不到，是死代码不是深色事故——整段摘掉别报。
 *  令牌未定义的那种由 undefined-var-fallback 规则单独抓。 */
function stripSafeVarFallbacks(value) {
  return value.replace(
    /var\(\s*(--[A-Za-z0-9_-]+)\s*,([^()]*(?:\([^()]*\)[^()]*)*)\)/g,
    (whole, name) => (definedVars.has(name) ? ' ' : whole),
  );
}

function hasColorLiteral(value) {
  // 先摘掉令牌名本身：--color-text-white / --color-bg-gray 里的 white/gray 是名字不是颜色。
  // color-mix(in srgb, var(--x) 8%, transparent) 是推荐写法，摘完就不含任何字面色。
  const v = stripSafeVarFallbacks(value).replace(/--[A-Za-z0-9_-]+/g, '');
  if (HEX.test(v)) return true;
  if (FUNC_COLOR.test(v)) return true;
  if (NAMED_COLORS.test(v)) return true;
  return false;
}

/* 承载颜色的 CSS 属性（含自定义属性，单独判断） */
const COLOR_PROPS =
  /^(color|background|background-color|background-image|border|border-(top|right|bottom|left)?-?color|border(-(top|right|bottom|left))?|outline|outline-color|box-shadow|text-shadow|fill|stroke|caret-color|accent-color|text-decoration-color|column-rule-color|text-emphasis-color|scrollbar-color|filter|backdrop-filter|stop-color|flood-color|lighting-color)$/;

/* TSX 内联 style 的同类属性（camelCase） */
const JS_COLOR_PROPS = new Set([
  'color', 'background', 'backgroundColor', 'backgroundImage', 'border', 'borderColor',
  'borderTop', 'borderRight', 'borderBottom', 'borderLeft', 'borderTopColor',
  'borderRightColor', 'borderBottomColor', 'borderLeftColor', 'outline', 'outlineColor',
  'boxShadow', 'textShadow', 'fill', 'stroke', 'caretColor', 'accentColor',
  'textDecorationColor', 'columnRuleColor', 'scrollbarColor', 'filter', 'backdropFilter',
  'stopColor', 'floodColor', 'lightingColor',
]);

/* 规则 id 与它的修法写在一处——分成两份列表迟早会对不上（改了 id 忘了改帮助文案）。 */
const RULE_SPECS = {
  CSS_LITERAL: {
    id: 'css-literal-color',                    // 盲区 1
    fix: '颜色改用令牌 var(--color-*)；半透明面用\n'
       + '                           color-mix(in srgb, var(--color-bg-container) 60%, transparent)',
  },
  CSS_VAR_DEF: {
    id: 'css-var-literal-color',                // 盲区 2
    fix: '自定义属性的值本身也要引令牌，别把 #fff 藏进 --jx-xxx',
  },
  JS_INLINE: {
    id: 'js-inline-color',                      // 盲区 3
    fix: '内联 style 改 var(--color-*)，或把样式挪进 CSS 类',
  },
  VAR_FALLBACK: {
    id: 'undefined-var-fallback',               // 盲区 4
    fix: 'var(--x, #fff) 的兜底永远生效；补上 --x 定义或直接引已有令牌',
  },
  MEDIA_SCHEME: {
    id: 'prefers-color-scheme',                 // 纪律：禁止绕开手动三档
    fix: "深色覆盖统一写 :root[data-theme='dark'] 选择器，别用媒体查询\n"
       + '                           （媒体查询会和手动 light/dark 三档打架）',
  },
};
const RULES = Object.fromEntries(Object.entries(RULE_SPECS).map(([k, v]) => [k, v.id]));
const FIX_HINTS = Object.values(RULE_SPECS);
const RULE_ID_WIDTH = Math.max(...FIX_HINTS.map((r) => r.id.length));

const findings = [];
function report(rule, file, line, snippet) {
  findings.push({ rule, file, line, snippet: snippet.replace(/\s+/g, ' ').trim().slice(0, 160) });
}

/* ── 文件遍历 ─────────────────────────────────────────────────── */
/** 一次读盘读完：definedVars 与扫描两遍都用同一份内容，别把每个文件读两次。 */
const sources = new Map(); // 展示用相对路径 → 文件内容
function collect(root, relTo) {
  if (!existsSync(root)) return;
  (function walk(dir) {
    for (const e of readdirSync(dir, { withFileTypes: true })) {
      if (e.name.startsWith('.')) continue;
      const p = join(dir, e.name);
      if (e.isDirectory()) {
        if (SKIP_DIRS.has(e.name)) continue;
        walk(p);
      } else if (/\.(css|ts|tsx)$/.test(e.name)) {
        sources.set(relative(relTo, p).split(sep).join('/'), readFileSync(p, 'utf-8'));
      }
    }
  })(root);
}
collect(srcRoot, frontendRoot);
// overlay 以仓库根为基准展示（ce/overlay/src/frontend/src/…），与主树同名文件区分开：
// 两边都要扫——FULL 用主树那份，CE 派生后用 overlay 那份，谁都不能漏。
collect(ceOverlayRoot, repoRoot);

/* ── 桌面壳：从 .rs 里把 <style> 段抠出来当 CSS 扫 ────────────────────
   做法是「留 CSS、其余填空格」而不是切片：字符位置全程不动，报出来的行号
   就是 .rs 文件里的真实行号，点开即所见。Rust 那些 r##"…"## 的引号、宏、
   代码全被抹成空白，不会被误当成声明。 */
const embeddedCssFiles = new Set();
function blankNonCss(raw) {
  const keep = new Array(raw.length).fill(false);
  for (const m of raw.matchAll(/<style[^>]*>([\s\S]*?)<\/style>/g)) {
    const open = m.index + m[0].indexOf('>') + 1;
    const close = m.index + m[0].length - '</style>'.length;
    for (let i = open; i < close; i++) keep[i] = true;
  }
  let out = '';
  for (let i = 0; i < raw.length; i++) out += keep[i] || raw[i] === '\n' ? raw[i] : ' ';
  return out;
}
function collectEmbeddedCss(root, relTo) {
  if (!existsSync(root)) return;
  (function walk(dir) {
    for (const e of readdirSync(dir, { withFileTypes: true })) {
      if (e.name.startsWith('.')) continue;
      const p = join(dir, e.name);
      if (e.isDirectory()) {
        if (!SKIP_DIRS.has(e.name)) walk(p);
      } else if (e.name.endsWith('.rs')) {
        const blanked = blankNonCss(readFileSync(p, 'utf-8'));
        if (!blanked.trim()) continue; // 这个 .rs 里没有内嵌样式
        const rel = relative(relTo, p).split(sep).join('/');
        sources.set(rel, blanked);
        embeddedCssFiles.add(rel);
      }
    }
  })(root);
}
collectEmbeddedCss(desktopShellRoot, repoRoot);

/* ── 全仓自定义属性定义集合（供盲区 4 取差集）───────────────────── */
const definedVars = new Set();
for (const src of sources.values()) {
  for (const m of src.matchAll(/(--[A-Za-z0-9_-]+)\s*:/g)) definedVars.add(m[1]);
  // JS 侧 setProperty('--x', …) 也算定义
  for (const m of src.matchAll(/setProperty\(\s*['"`](--[A-Za-z0-9_-]+)/g)) definedVars.add(m[1]);
}

/** 豁免标记，三种写法：
 *  1. 本行带 `dark-ok`；
 *  2. 紧邻其上的注释块里带 `dark-ok`（注释可跨多行）；
 *  3. 整段包在 `dark-ok-begin` / `dark-ok-end` 之间——用于生成独立文档的大段模板
 *     （导出 PDF/HTML、邮件正文），那种整块都不该跟随界面主题。 */
const COMMENT_LINE = /^\s*(\/\*|\*|\/\/)|\*\/\s*$/;
const regionCache = new WeakMap();
function exemptRegions(lines) {
  let flags = regionCache.get(lines);
  if (flags) return flags;
  flags = new Array(lines.length).fill(false);
  let open = false;
  lines.forEach((ln, i) => {
    if (/dark-ok-begin/.test(ln)) open = true;
    flags[i] = open;
    if (/dark-ok-end/.test(ln)) open = false;
  });
  regionCache.set(lines, flags);
  return flags;
}
function exempt(lines, idx) {
  if (exemptRegions(lines)[idx]) return true;
  if (/dark-ok/.test(lines[idx] || '')) return true;
  for (let i = idx - 1; i >= 0 && idx - i <= 8; i--) {
    const ln = lines[i] || '';
    if (/dark-ok/.test(ln)) return true;
    if (!COMMENT_LINE.test(ln) && ln.trim() !== '') return false;
  }
  return false;
}

/* 未定义令牌 + 含颜色兜底：`var(--x, #fff)` */
function scanVarFallback(text, lines, file) {
  for (const m of text.matchAll(/var\(\s*(--[A-Za-z0-9_-]+)\s*,([^()]*(?:\([^()]*\)[^()]*)*)\)/g)) {
    const [, name, fallback] = m;
    if (definedVars.has(name)) continue;
    if (!hasColorLiteral(fallback)) continue;
    const line = text.slice(0, m.index).split('\n').length;
    if (exempt(lines, line - 1)) continue;
    report(RULES.VAR_FALLBACK, file, line, m[0]);
  }
}

/* ── CSS 扫描 ─────────────────────────────────────────────────── */
function scanCss(file, raw) {
  const lines = raw.split('\n');
  // 注释掏空但保留换行，行号才不漂
  const text = raw.replace(/\/\*[\s\S]*?\*\//g, (c) => c.replace(/[^\n]/g, ' '));

  for (const m of text.matchAll(/@media[^{]*prefers-color-scheme/g)) {
    const line = text.slice(0, m.index).split('\n').length;
    if (exempt(lines, line - 1)) continue;
    report(RULES.MEDIA_SCHEME, file, line, m[0]);
  }

  scanVarFallback(text, lines, file);
  if (ALLOWED_FILES.has(file)) return;

  // 声明扫描：跟踪 selector 栈，`:root[data-theme` 块内允许写死（那就是深色覆盖块本身）
  const selStack = [];
  let buf = '';
  let pos = 0;
  // 声明可能跨多行（多层 gradient）。行号一律报**起始行**，dark-ok 注释才好写在它正上方。
  let declStart = 0;
  const flushDecl = () => {
    const decl = buf.trim();
    const startIdx = declStart;
    buf = '';
    if (!decl || !decl.includes(':')) return;
    const ci = decl.indexOf(':');
    const prop = decl.slice(0, ci).trim().toLowerCase();
    const value = decl.slice(ci + 1);
    const isVarDef = prop.startsWith('--');
    if (!isVarDef && !COLOR_PROPS.test(prop)) return;
    if (!hasColorLiteral(value)) return;
    // 深色覆盖块内部写死颜色是允许的（它只在 data-theme=dark 下生效）
    if (selStack.some((s) => /\[data-theme/.test(s))) return;
    const line = text.slice(0, startIdx).split('\n').length;
    if (exempt(lines, line - 1)) return;
    report(isVarDef ? RULES.CSS_VAR_DEF : RULES.CSS_LITERAL, file, line, decl);
  };

  for (; pos < text.length; pos++) {
    const ch = text[pos];
    if (!buf.trim() && !/\s/.test(ch)) declStart = pos;
    if (ch === '{') {
      selStack.push(buf.trim());
      buf = '';
    } else if (ch === '}') {
      flushDecl();
      selStack.pop();
    } else if (ch === ';') {
      if (selStack.length > 0) flushDecl();
      else buf = ''; // 块外的 @import / @charset 之类，不是声明
    } else {
      buf += ch;
    }
  }
}

/* ── TS/TSX 扫描 ──────────────────────────────────────────────── */
/** 从 `prop:` 后取一段值：到同层 `,` 或 `}` 为止，跨过嵌套括号与字符串。 */
function readValue(src, start) {
  let i = start;
  let depth = 0;
  let quote = '';
  for (; i < src.length; i++) {
    const c = src[i];
    if (quote) {
      if (c === '\\') { i++; continue; }
      if (c === quote) quote = '';
      continue;
    }
    if (c === '"' || c === "'" || c === '`') { quote = c; continue; }
    if ('([{'.includes(c)) { depth++; continue; }
    if (')]}'.includes(c)) {
      if (depth === 0) break;
      depth--;
      continue;
    }
    if (c === ',' && depth === 0) break;
    if (c === '\n' && depth === 0 && /[,;}]\s*$/.test(src.slice(start, i))) break;
  }
  return src.slice(start, i);
}

function scanJs(file, raw) {
  if (ALLOWED_FILES.has(file)) return;
  const lines = raw.split('\n');
  // 行注释/块注释掏空，避免注释里的示例色误报
  const text = raw
    .replace(/\/\*[\s\S]*?\*\//g, (c) => c.replace(/[^\n]/g, ' '))
    .replace(/(^|[^:'"`\\])\/\/[^\n]*/g, (m, p1) => p1 + ' '.repeat(m.length - p1.length));

  scanVarFallback(text, lines, file);

  // 对象字面量属性：{ color: …, background: … }
  const propRe = /(?:^|[{,(\s])([A-Za-z]+)\s*:/g;
  for (const m of text.matchAll(propRe)) {
    const prop = m[1];
    if (!JS_COLOR_PROPS.has(prop)) continue;
    const value = readValue(text, m.index + m[0].length);
    if (!hasColorLiteral(value)) continue;
    const line = text.slice(0, m.index).split('\n').length;
    if (exempt(lines, line - 1)) continue;
    report(RULES.JS_INLINE, file, line, `${prop}:${value}`);
  }

  // JSX 属性：<circle fill="#fff" stroke="rgba(…)">
  const attrRe = /\b(fill|stroke|stopColor|color|bgColor)\s*=\s*(["'])([^"']*)\2/g;
  for (const m of text.matchAll(attrRe)) {
    if (!hasColorLiteral(m[3])) continue;
    const line = text.slice(0, m.index).split('\n').length;
    if (exempt(lines, line - 1)) continue;
    report(RULES.JS_INLINE, file, line, m[0]);
  }
}

for (const [file, raw] of sources) {
  if (file.endsWith('.css') || embeddedCssFiles.has(file)) scanCss(file, raw);
  else scanJs(file, raw);
}

/* ── 棘轮比对 ─────────────────────────────────────────────────── */
/** 基线按 file → rule → 计数聚合：行号会漂，快照文本又太脆，计数最稳。
 *  已知取舍：同一 file+rule 桶内「删一处、又新增一处」计数不变，门禁不会报——
 *  但那种改动必然出现在 diff 里、由 review 兜住，而棘轮要保证的「存量只减不增」不受影响。
 *  真要收严，可改成对 snippet 内容做哈希集合（对行号漂移免疫），代价是基线体积与噪声。 */
function tally(list) {
  const out = {};
  for (const f of list) {
    (out[f.file] ||= {});
    out[f.file][f.rule] = (out[f.file][f.rule] || 0) + 1;
  }
  return out;
}

const current = tally(findings);

if (UPDATE) {
  const sorted = {};
  for (const file of Object.keys(current).sort()) {
    sorted[file] = Object.fromEntries(Object.entries(current[file]).sort());
  }
  writeFileSync(baselinePath, `${JSON.stringify(sorted, null, 2)}\n`, 'utf-8');
  console.log(`[check-dark-mode] 基线已更新：${Object.keys(sorted).length} 个文件 / ${findings.length} 处存量`);
  process.exit(0);
}

const baseline = existsSync(baselinePath) ? JSON.parse(readFileSync(baselinePath, 'utf-8')) : {};

if (LIST) {
  for (const f of findings.sort((a, b) => a.file.localeCompare(b.file) || a.line - b.line)) {
    console.log(`${f.file}:${f.line}  [${f.rule}]  ${f.snippet}`);
  }
  console.log(`\n合计 ${findings.length} 处`);
}

/* 取 current 与 baseline 的键并集再逐条比：只走 current 会漏掉「已清零」的桶（它不再有键），
   只走 baseline 会漏掉新出现的文件。一次遍历同时得出超标与改善。 */
const buckets = new Map(); // file → Set(rule)；用嵌套结构而不是拼接字符串当键
for (const source of [current, baseline]) {
  for (const [file, rules] of Object.entries(source)) {
    const set = buckets.get(file) ?? new Set();
    for (const rule of Object.keys(rules)) set.add(rule);
    buckets.set(file, set);
  }
}

const regressions = [];
let improved = 0;
for (const [file, rules] of buckets) {
  for (const rule of rules) {
    const count = current[file]?.[rule] || 0;
    const allowed = baseline[file]?.[rule] || 0;
    if (count > allowed) regressions.push({ file, rule, count, allowed });
    else if (count < allowed) improved += allowed - count;
  }
}

if (regressions.length) {
  console.error('\n[check-dark-mode] 深色模式门禁未通过——下列位置绕过了颜色令牌：\n');
  for (const r of regressions) {
    console.error(`  ${r.file}  [${r.rule}]  ${r.count} 处（基线允许 ${r.allowed} 处）`);
    for (const f of findings.filter((x) => x.file === r.file && x.rule === r.rule).slice(0, 12)) {
      console.error(`      ${f.file}:${f.line}  ${f.snippet}`);
    }
  }
  const hints = FIX_HINTS.map((r) => `  ${r.id.padEnd(RULE_ID_WIDTH)} \u2192 ${r.fix}`).join('\n');
  console.error(`
\u4fee\u6cd5\uff08\u6309\u76f2\u533a\u5206\u7c7b\uff09\uff1a
${hints}
\u786e\u5b9e\u9700\u8981\u5199\u6b7b\u989c\u8272\uff08\u6697\u8272\u4ee3\u7801\u53f0\u3001\u54c1\u724c\u63d2\u753b\u3001\u6210\u5bf9\u4e3b\u9898\u5bf9\u8c61\uff09\u2192 \u8be5\u884c\u52a0 dark-ok: \u539f\u56e0 \u8c41\u514d\uff1b
\u6574\u6bb5\u6a21\u677f\u7528 dark-ok-begin / dark-ok-end \u5305\u8d77\u6765\u3002
\u660e\u7ec6\uff1anpm run check:dark -- --list
`);
  process.exit(1);
}

if (improved > 0) {
  console.log(`[check-dark-mode] 通过（存量比基线少 ${improved} 处，可跑 npm run check:dark -- --update 收紧基线）`);
} else {
  console.log(`[check-dark-mode] 通过（存量 ${findings.length} 处，全部在基线内）`);
}
