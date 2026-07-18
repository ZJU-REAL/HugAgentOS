#!/usr/bin/env node
/**
 * i18n 一致性门禁（构建前执行）：
 *  1. 各域字典间禁止重复 key——扁平合并 + 后者覆盖意味着重复必有一份是死条目，
 *     译文不一致时更会按 import 顺序"抽奖"；
 *  2. 源码里 t('字面量') / tCtx('ctx', '字面量') 的中文 key 必须存在于字典，
 *     否则英文界面静默回退中文。
 * 动态调用 t(变量)（DB 文案、后端 tag 等）不在检查范围。
 */
import { readFileSync, readdirSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..', 'src');
const dictDir = join(root, 'i18n', 'en');

const entryRe = /^\s*'((?:[^'\\]|\\.)*)'\s*:/;
const keyOwner = new Map();
const errors = [];

for (const f of readdirSync(dictDir).filter((f) => f.endsWith('.ts') && f !== 'index.ts')) {
  const lines = readFileSync(join(dictDir, f), 'utf-8').split('\n');
  lines.forEach((ln, i) => {
    const m = entryRe.exec(ln);
    if (!m) return;
    const key = m[1].replace(/\\'/g, "'");
    if (keyOwner.has(key)) {
      errors.push(`duplicate key '${key}' in ${f}:${i + 1} (already in ${keyOwner.get(key)})`);
    } else {
      keyOwner.set(key, `${f}:${i + 1}`);
    }
  });
}

const tCall = /\bt\(\s*'((?:[^'\\]|\\.)*)'/g;
const tCtxCall = /\btCtx\(\s*'((?:[^'\\]|\\.)*)'\s*,\s*'((?:[^'\\]|\\.)*)'/g;
const hasCjk = (s) => /[一-鿿]/.test(s);

function walk(dir) {
  for (const e of readdirSync(dir, { withFileTypes: true })) {
    const p = join(dir, e.name);
    if (e.isDirectory()) {
      if (e.name === 'i18n' || e.name === 'node_modules') continue;
      walk(p);
    } else if (/\.(ts|tsx)$/.test(e.name)) {
      const src = readFileSync(p, 'utf-8');
      for (const m of src.matchAll(tCall)) {
        const key = m[1].replace(/\\'/g, "'");
        if (hasCjk(key) && !keyOwner.has(key)) {
          errors.push(`missing dict entry for t('${key}') in ${p.slice(root.length + 1)}`);
        }
      }
      for (const m of src.matchAll(tCtxCall)) {
        const [, ctx, text] = m;
        const key = `${text}#${ctx}`;
        if (hasCjk(text) && !keyOwner.has(key) && !keyOwner.has(text)) {
          errors.push(`missing dict entry for tCtx('${ctx}', '${text}') in ${p.slice(root.length + 1)}`);
        }
      }
    }
  }
}
walk(root);

if (errors.length) {
  console.error(`i18n check failed (${errors.length}):`);
  for (const e of errors) console.error('  ' + e);
  process.exit(1);
}
console.log(`i18n check OK: ${keyOwner.size} keys, no duplicates, all literal t() keys covered`);
