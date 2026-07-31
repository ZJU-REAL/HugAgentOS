// Payload 深路径门禁：Windows MAX_PATH=260。
//
// 本机安装的最长落地前缀（实测最坏情形，含较长用户名余量）约为
//   C:\Users\<user>\AppData\Local\<identifier>\local-server\runtime\source.next-XXXXXXXX\
// ≈ 110–120 字符。据此给包内相对路径设 165 字符预算：超限的文件会把
// ZipFile 兜底解压顶向 260（tar.exe 缺席的老系统直接失败），也压缩运行期
// 读取的余量。曾实际炸过：yida 技能 141 字符深路径 + 32 位暂存名 = 恰好 260。
import { execFileSync } from 'node:child_process';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import assert from 'node:assert/strict';

const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const BUDGET = 165;
// payload 打包的后端子树（create-ce-archive 以 git 跟踪文件为准）
const PAYLOAD_PREFIXES = ['src/backend/'];

test(`payload 相对路径不超过 ${BUDGET} 字符（Windows MAX_PATH 余量）`, () => {
  const out = execFileSync('git', ['ls-files', '-z'], { cwd: REPO_ROOT, maxBuffer: 64 * 1024 * 1024 });
  const files = out.toString('utf8').split('\0').filter(Boolean);
  const offenders = files
    .filter((f) => PAYLOAD_PREFIXES.some((p) => f.startsWith(p)))
    // 打包时以 src/backend 为根：相对路径 = 去掉前缀后再加 src/backend/ 自身？
    // 实际 payload 布局保留 src/backend/... 层级，直接按完整仓库相对路径计。
    .filter((f) => f.length > BUDGET)
    .sort((a, b) => b.length - a.length);
  assert.deepEqual(
    offenders.map((f) => `${f.length} ${f}`),
    [],
    `以下文件路径超过 ${BUDGET} 字符预算，Windows 安装可能触顶 MAX_PATH：\n` + offenders.join('\n'),
  );
});
