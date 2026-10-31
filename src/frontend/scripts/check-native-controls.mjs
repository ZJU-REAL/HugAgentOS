#!/usr/bin/env node
/**
 * 原生控件门禁（构建前执行，与 check-dark-mode 同级）。
 *
 * 背景：工作台（src/workbench）整块是「先接逻辑、UI 以后再说」写出来的——裸 <button> / <input> /
 * <select> / <p role="alert">，没有 className、没有 CSS、没用 antd。浏览器默认样式在浅色下勉强能看，
 * 混在 v2 界面里就是一块原生灰框（见「云端: 此来源暂不可用」那条全局挂载的提示）。随后有人在外层
 * 用 `.xxx button { … }` 元素选择器去补，又反过来盖掉 antd 的样式。两个问题的根都是：
 * **写裸控件没有任何反馈**——tsc、eslint、深色门禁都不管它。
 *
 * 两条规则：
 *   1. workbench-host-ui（严格，无基线）：src/workbench/** 只能通过 src/workbench/ui 的 Host* 组件
 *      渲染按钮和表单控件；ui/ 目录本身是唯一允许写原生控件的地方。
 *   2. unstyled-native-control（棘轮）：其余 TSX 里不带 className 的原生 <button|input|select|textarea>。
 *      存量冻结在 native-controls-baseline.json，只能变少；修掉后 `--update` 收紧。
 *
 * 逃生舱：同一行或紧邻上一行写 `raw-ok: 原因`（例如自绘图标按钮、隐藏的文件选择 input）。
 * type="hidden" / type="file" 的 input 天然不可见，不计入。
 *
 * 用法：
 *   node scripts/check-native-controls.mjs            # 门禁
 *   node scripts/check-native-controls.mjs --list     # 列出全部（含基线内）
 *   node scripts/check-native-controls.mjs --update   # 以当前实测重写基线
 */
import { existsSync, readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { dirname, join, relative, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

const scriptDir = dirname(fileURLToPath(import.meta.url));
const frontendRoot = join(scriptDir, '..');
const srcRoot = join(frontendRoot, 'src');
const repoRoot = join(frontendRoot, '..', '..');
/* CE overlay 会整文件替换同名源文件，一起扫，免得派生出去才发现。 */
const ceOverlayRoot = join(repoRoot, 'ce', 'overlay', 'src', 'frontend', 'src');
const baselinePath = join(scriptDir, 'native-controls-baseline.json');

const args = process.argv.slice(2);
const UPDATE = args.includes('--update');
const LIST = args.includes('--list');

const WORKBENCH = 'src/workbench/';
const WORKBENCH_UI = 'src/workbench/ui/';
const TAG = /<(button|input|select|textarea)\b/g;
const SKIP_DIRS = new Set(['node_modules', 'dist', 'assets', 'i18n']);

function walk(dir, out = []) {
  if (!existsSync(dir)) return out;
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    if (entry.isDirectory()) { if (!SKIP_DIRS.has(entry.name)) walk(join(dir, entry.name), out); }
    else if (entry.name.endsWith('.tsx')) out.push(join(dir, entry.name));
  }
  return out;
}

/** Attributes of the opening tag that starts at `start`, skipping `{…}` expressions and strings. */
function openingTag(text, start) {
  let depth = 0, quote = '';
  for (let i = start + 1; i < text.length; i++) {
    const c = text[i];
    if (quote) { if (c === quote && text[i - 1] !== '\\') quote = ''; continue; }
    if (c === '"' || c === "'" || c === '`') { quote = c; continue; }
    if (c === '{') depth++;
    else if (c === '}') depth--;
    else if (c === '>' && depth === 0) return text.slice(start, i + 1);
  }
  return text.slice(start);
}

function waived(lines, lineIndex) {
  const hasReason = (line) => /raw-ok:\s*\S/.test(line ?? '');
  return hasReason(lines[lineIndex]) || (lineIndex > 0 && hasReason(lines[lineIndex - 1]));
}

const findings = [];
/** Blank out comments (keeping newlines) so prose that mentions <button> is not a finding. */
function stripComments(text) {
  const blank = value => value.replace(/[^\n]/g, ' ');
  return text.replace(/\/\*[\s\S]*?\*\//g, blank).replace(/(^|[^:'"`])\/\/[^\n]*/g, (m, lead) => lead + blank(m.slice(lead.length)));
}

function scan(file, label) {
  const raw = readFileSync(file, 'utf8');
  const lines = raw.split('\n');
  const text = stripComments(raw);
  for (const match of text.matchAll(TAG)) {
    const line = text.slice(0, match.index).split('\n').length - 1;
    if (waived(lines, line)) continue;
    const tag = openingTag(text, match.index);
    if (/\btype=["'](hidden|file)["']/.test(tag)) continue;
    const inWorkbench = label.startsWith(WORKBENCH) && !label.startsWith(WORKBENCH_UI);
    if (inWorkbench) findings.push({ rule: 'workbench-host-ui', file: label, line: line + 1, snippet: tag });
    else if (!/\bclassName=/.test(tag)) findings.push({ rule: 'unstyled-native-control', file: label, line: line + 1, snippet: tag });
  }
}

const toPosix = value => value.split(sep).join('/');
for (const file of walk(srcRoot)) scan(file, toPosix(relative(frontendRoot, file)));
for (const file of walk(ceOverlayRoot)) scan(file, 'ce-overlay/' + toPosix(relative(ceOverlayRoot, file)));

const strict = findings.filter(f => f.rule === 'workbench-host-ui');
const ratchet = findings.filter(f => f.rule === 'unstyled-native-control');
const counts = {};
for (const f of ratchet) counts[f.file] = (counts[f.file] || 0) + 1;

const show = f => `  ${f.file}:${f.line}  [${f.rule}]  ${f.snippet.replace(/\s+/g, ' ').slice(0, 120)}`;

if (UPDATE) {
  const sorted = Object.fromEntries(Object.entries(counts).sort(([a], [b]) => a.localeCompare(b)));
  writeFileSync(baselinePath, JSON.stringify(sorted, null, 2) + '\n');
  console.log(`native-controls 基线已更新：${ratchet.length} 处 / ${Object.keys(sorted).length} 个文件`);
  if (strict.length) { console.error(strict.map(show).join('\n')); process.exit(1); }
  process.exit(0);
}

const baseline = existsSync(baselinePath) ? JSON.parse(readFileSync(baselinePath, 'utf8')) : {};
const regressions = Object.entries(counts).filter(([file, n]) => n > (baseline[file] || 0));
const shrunk = Object.entries(baseline).filter(([file, n]) => (counts[file] || 0) < n);

if (LIST) console.log(findings.map(show).join('\n') || '  （无）');

let failed = false;
if (strict.length) {
  failed = true;
  console.error(`\n✗ workbench-host-ui：src/workbench 里有 ${strict.length} 处原生控件。`);
  console.error(strict.map(show).join('\n'));
  console.error('  修法：改用 src/workbench/ui 的 HostButton / HostInput / HostSelect / HostCheckbox / HostNotice …');
}
if (regressions.length) {
  failed = true;
  console.error('\n✗ unstyled-native-control：以下文件新增了不带 className 的原生控件（超出基线）：');
  for (const [file, n] of regressions) {
    console.error(`  ${file}: ${n}（基线 ${baseline[file] || 0}）`);
    for (const f of ratchet.filter(f => f.file === file)) console.error('  ' + show(f));
  }
  console.error('  修法：用 antd 组件，或给原生控件一个有样式的 className；确属例外写 raw-ok: 原因。');
}
if (failed) process.exit(1);
console.log(`native-controls OK：workbench 0 处，其余 ${ratchet.length} 处（基线内）`
  + (shrunk.length ? `；有 ${shrunk.length} 个文件已低于基线，可跑 --update 收紧` : ''));
