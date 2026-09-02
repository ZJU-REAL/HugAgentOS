#!/usr/bin/env node
/**
 * 动效降级门禁（构建前执行，与 check-i18n / check-dark-mode 同级）。
 *
 * 背景：系统开启「减弱动画」（Windows 节电模式会自动开）后，装饰性动效该停，但表示
 * 「正在加载 / 正在运行」的循环动画一停就变成静止的残圈或灰条，比动起来更像卡死——
 * 而本机不开这个开关就永远看不到，是个没有反馈回路的盲区。
 *
 * 降级的实现方式决定了本门禁检查什么：我们**不**用全局选择器压制动画（那会连第三方
 * 组件的加载转圈一起冻住，而 antd 里只有 Spin 输出 aria-busy，其余转圈没有任何可判定
 * 的语义），而是**给每条装饰性关键帧就地补一份「定格在终态」的同名覆盖版**放进
 * @media (prefers-reduced-motion: reduce)——同名 @keyframes 后定义者胜，用到它的规则
 * 自动一起降级，且不可能误伤第三方。工作指示器的关键帧不补覆盖版，于是原样继续动。
 *
 * 于是每条关键帧都必须二选一，本门禁强制这个选择：
 *   1. 装饰性 → 在同文件里有一份 @media (prefers-reduced-motion: reduce) 内的同名覆盖版；
 *   2. 工作指示器 → 使用它的规则上标注 `motion-keep: 理由`，说明为什么必须继续动。
 * 另外禁止同一条关键帧既被 motion-keep 的规则使用、又被补了定格版——那说明它同时承担
 * 了「在跑」和「装饰」两种语义，应当拆成两条。
 *
 * 用法：
 *   node scripts/check-loading-motion.mjs          # 门禁，有问题退出码 1
 *   node scripts/check-loading-motion.mjs --list   # 打印每条关键帧的立场
 */
import { readFileSync, readdirSync, existsSync } from 'node:fs';
import { join, dirname, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const scriptDir = dirname(fileURLToPath(import.meta.url));
const frontendRoot = join(scriptDir, '..');
const srcRoot = join(frontendRoot, 'src');
const repoRoot = join(frontendRoot, '..', '..');
const ceOverlayStyles = join(repoRoot, 'ce', 'overlay', 'src', 'frontend', 'src');

function walk(dir, out = []) {
  if (!existsSync(dir)) return out;
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const p = join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name === 'node_modules' || entry.name === 'dist') continue;
      walk(p, out);
    } else if (entry.name.endsWith('.css')) {
      out.push(p);
    }
  }
  return out;
}

const files = [...walk(srcRoot), ...walk(ceOverlayStyles)];

/** name -> { file, defined, stilled(有定格版), keptBy[] } */
const frames = new Map();
const get = (name, file) => {
  if (!frames.has(name)) frames.set(name, { file, defined: false, stilled: false, keptBy: [] });
  return frames.get(name);
};

for (const file of files) {
  const text = readFileSync(file, 'utf8');
  const rel = relative(frontendRoot, file);
  const lines = text.split('\n');

  // 关键帧定义；落在 reduced-motion 媒体块里的算「定格版」
  let mediaDepth = 0;
  let depth = 0;
  let inReduceMedia = false;
  lines.forEach((line) => {
    const opens = (line.match(/\{/g) || []).length;
    const closes = (line.match(/\}/g) || []).length;
    if (/@media[^{]*prefers-reduced-motion[^{]*reduce/.test(line)) {
      inReduceMedia = true;
      mediaDepth = depth;
    }
    const kf = line.match(/@keyframes\s+([A-Za-z0-9_-]+)/);
    if (kf) {
      const entry = get(kf[1], rel);
      if (inReduceMedia) entry.stilled = true;
      else { entry.defined = true; entry.file = rel; }
    }
    depth += opens - closes;
    if (inReduceMedia && depth <= mediaDepth) inReduceMedia = false;
  });

  // 规则的立场注释（motion-keep）——注释可写在规则内或选择器上方 4 行
  let start = 0;
  let body = [];
  depth = 0;
  lines.forEach((line, i) => {
    const opens = (line.match(/\{/g) || []).length;
    const closes = (line.match(/\}/g) || []).length;
    if (depth === 0 && opens) { start = i; body = [line]; } else if (depth > 0) body.push(line);
    depth += opens - closes;
    if (depth === 0 && body.length) {
      const inner = body.join('\n');
      const blob = lines.slice(Math.max(0, start - 4), start).join('\n') + inner;
      if (/motion-keep:/.test(blob)) {
        for (const m of inner.matchAll(/animation(?:-name)?\s*:\s*([A-Za-z0-9_-]+)/g)) {
          const name = m[1];
          if (['none', 'inherit', 'initial'].includes(name)) continue;
          get(name, rel).keptBy.push(`${rel}:${start + 1}`);
        }
      }
      body = [];
    }
  });
}

const undecided = [];
const conflicting = [];
for (const [name, info] of frames) {
  if (!info.defined) continue;                        // 只在 reduce 块里出现的是定格版本身
  const kept = info.keptBy.length > 0;
  if (kept && info.stilled) conflicting.push({ name, info });
  else if (!kept && !info.stilled) undecided.push({ name, info });
}

if (process.argv.includes('--list')) {
  for (const [name, info] of [...frames].sort()) {
    if (!info.defined) continue;
    console.log(`${info.keptBy.length ? 'keep' : info.stilled ? 'stop' : '????'}  ${info.file}  ${name}`);
  }
}

if (undecided.length || conflicting.length) {
  if (undecided.length) {
    console.error('\n[check-loading-motion] 以下关键帧没有表明立场（减弱动效下要继续动还是定格）：');
    for (const u of undecided) console.error(`    ${u.info.file}  @keyframes ${u.name}`);
    console.error('  装饰性 → 在同文件里紧跟定义补一份定格版：');
    console.error('      @media (prefers-reduced-motion: reduce) { @keyframes <名字> { from, to { <终帧声明> } } }');
    console.error('  工作指示器 → 在使用它的规则上方写 motion-keep: 理由（说明停下为什么会被读成卡死）。');
  }
  if (conflicting.length) {
    console.error('\n[check-loading-motion] 以下关键帧同时被标为工作指示器又补了定格版，语义冲突：');
    for (const c of conflicting) console.error(`    ${c.info.file}  @keyframes ${c.name}  （keep 于 ${c.info.keptBy.join(', ')}）`);
    console.error('  同一条关键帧不能既表示「在跑」又是装饰，按用途拆成两条。');
  }
  console.error('');
  process.exit(1);
}

const keepCount = [...frames.values()].filter((i) => i.defined && i.keptBy.length).length;
const stopCount = [...frames.values()].filter((i) => i.defined && i.stilled).length;
console.log(`[check-loading-motion] 通过（关键帧 ${keepCount + stopCount} 条：工作指示器 ${keepCount} 条保持动，装饰性 ${stopCount} 条减弱动效下定格）`);
