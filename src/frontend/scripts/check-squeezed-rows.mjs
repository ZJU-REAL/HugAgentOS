#!/usr/bin/env node
/**
 * 按钮组挤压门禁（构建前执行，与 check-native-controls 同级）。
 *
 * 背景：确认卡片（.jx-confirmBar）的按钮组 `flex:none` 不换行，只靠 `@media (max-width:640px)` 兜底；
 * 桌面端开着状态栏 / 画布时卡片本身很窄、窗口却很宽，旁边的文案被挤成每行一两个字。
 * 写法规范见 .claude/skills/hugagent-frontend-dev/SKILL.md §7.5，参照实现是 styles/tool.css 的 .jx-confirmBar。
 *
 * 规则 rigid-button-group：选择器最后一个类名像按钮组（action(s) / btns / buttons / control(s)），
 * 声明了不收缩（flex:none / flex-shrink:0 / flex:0 0 …），同一条规则里却没有 flex-wrap:wrap。
 * 这是按类名的近似检查：不叫这些名字的按钮组（如 .jx-shareRecordSide）它看不见，评审时仍要按 §7.5 看。
 * 逃生舱：规则里写注释 `layout-ok: 原因`（纯图标按钮组、所在行本身会换行、容器恒宽）。
 *
 * 用法：node scripts/check-squeezed-rows.mjs [--list]
 */
import { existsSync, readFileSync, readdirSync } from 'node:fs';
import { dirname, join, relative, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

const frontendRoot = join(dirname(fileURLToPath(import.meta.url)), '..');
/* CE overlay 会整文件替换同名源文件，一起扫。 */
const roots = [[join(frontendRoot, 'src'), ''], [join(frontendRoot, '..', '..', 'ce', 'overlay', 'src', 'frontend', 'src'), 'ce-overlay/']];
const SKIP_DIRS = new Set(['node_modules', 'dist', 'assets', 'i18n']);

const GROUP = /\.[\w-]*(?:actions?|Actions?|btns|Btns|buttons|Buttons|controls?|Controls?)(?=$|[:[>+~])/;
const RIGID = /(?:^|;)\s*(?:flex\s*:\s*none|flex-shrink\s*:\s*0|flex\s*:\s*0\s+0)\b/;
const WRAPS = /(?:^|;)\s*flex-wrap\s*:\s*wrap/;

function walk(dir, out = []) {
  if (!existsSync(dir)) return out;
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    if (entry.isDirectory()) { if (!SKIP_DIRS.has(entry.name)) walk(join(dir, entry.name), out); }
    else if (entry.name.endsWith('.css')) out.push(join(dir, entry.name));
  }
  return out;
}

const findings = [];
for (const [root, prefix] of roots) {
  for (const file of walk(root)) {
    const raw = readFileSync(file, 'utf8');
    // Innermost blocks `selector { declarations }`, including rules nested in @media / @container.
    for (const match of raw.matchAll(/([^{}]*)\{([^{}]*)\}/g)) {
      const [, head, body] = match;
      const selectors = head.replace(/\/\*[\s\S]*?\*\//g, ' ').trim();
      if (!selectors || selectors.startsWith('@') || /layout-ok:\s*\S/.test(head + body)) continue;
      const decls = body.replace(/\/\*[\s\S]*?\*\//g, ' ');
      if (!RIGID.test(decls) || WRAPS.test(decls)) continue;
      const groups = selectors.split(',').map(s => s.trim()).filter(s => GROUP.test(s.split(/\s+/).pop() || ''));
      if (!groups.length) continue;
      const line = raw.slice(0, match.index + head.length).split('\n').length;
      findings.push(`  ${prefix}${relative(root, file).split(sep).join('/')}:${line}  ${groups.join(', ')}`);
    }
  }
}

if (process.argv.includes('--list')) console.log(findings.join('\n') || '  （无）');
if (findings.length) {
  console.error('\n✗ rigid-button-group：按钮组不收缩也不换行，窄容器里旁边的文案会被挤成每行一两个字：');
  console.error(findings.join('\n'));
  console.error('  修法见 .claude/skills/hugagent-frontend-dev/SKILL.md §7.5（参照 styles/tool.css 的 .jx-confirmBar）；');
  console.error('  纯图标按钮组、所在行本身会换行或容器恒宽：在规则里写注释 layout-ok: 原因。');
  process.exit(1);
}
console.log('squeezed-rows OK：0 处');
