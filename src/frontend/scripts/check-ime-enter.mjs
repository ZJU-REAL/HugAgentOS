#!/usr/bin/env node
/**
 * IME 回车门禁（构建前执行，与 check-squeezed-rows 同级）。
 *
 * 背景：拼音输入法里按回车是“确认候选词”，不能发送。Chrome 给这个 keydown 标 isComposing，
 * 但 WebKit（Safari、桌面端 WKWebView）先结束输入法组合再派发 keydown，isComposing 已是 false。
 * 各组件自己写 `nativeEvent.isComposing` 判断，在桌面端就会把消息直接发出去。
 *
 * 现在统一由 utils/ime.ts 的 installImeEnterGuard()（main.tsx 启动时安装）在 window 捕获阶段拦掉
 * 属于输入法的回车：React 处理器、antd onPressEnter、表单隐式提交都收不到它。组件只处理普通回车。
 *
 * 规则 ad-hoc-ime：utils/ime.ts 之外出现 `.isComposing` 或 `keyCode === 229`。
 * 要让其他键（Tab、方向键）留给输入法，用 utils/ime.ts 的 isImeKey。
 * 逃生舱：同一行写注释 `ime-ok: 原因`（例如判断的是 InputEvent 而不是按键）。
 *
 * 用法：node scripts/check-ime-enter.mjs
 */
import { existsSync, readFileSync, readdirSync } from 'node:fs';
import { dirname, join, relative, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

const frontendRoot = join(dirname(fileURLToPath(import.meta.url)), '..');
/* CE overlay 会整文件替换同名源文件，一起扫。 */
const roots = [[join(frontendRoot, 'src'), ''], [join(frontendRoot, '..', '..', 'ce', 'overlay', 'src', 'frontend', 'src'), 'ce-overlay/']];
const SKIP_DIRS = new Set(['node_modules', 'dist', 'assets', 'i18n']);
const OWNER = ['utils', 'ime.ts'].join(sep);
const AD_HOC = /\.isComposing\b|keyCode\s*={2,3}\s*229\b/;

function walk(dir, out = []) {
  if (!existsSync(dir)) return out;
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    if (entry.isDirectory()) { if (!SKIP_DIRS.has(entry.name)) walk(join(dir, entry.name), out); }
    else if (/\.(ts|tsx)$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name)) out.push(join(dir, entry.name));
  }
  return out;
}

const findings = [];
for (const [root, prefix] of roots) {
  for (const file of walk(root)) {
    const rel = relative(root, file);
    if (rel === OWNER) continue;
    readFileSync(file, 'utf8').split('\n').forEach((line, index) => {
      if (AD_HOC.test(line) && !line.includes('ime-ok:')) findings.push(`${prefix}${rel}:${index + 1}: ${line.trim()}`);
    });
  }
}

if (findings.length) {
  console.error('IME 回车门禁失败：输入法判断只在 utils/ime.ts 里做（见文件头注释）。');
  for (const finding of findings) console.error('  ' + finding);
  process.exit(1);
}
console.log('ime-enter OK：0 处');
