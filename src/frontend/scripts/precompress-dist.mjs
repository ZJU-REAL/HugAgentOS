#!/usr/bin/env node
/**
 * 构建收尾：给 dist 里的可压缩静态资源生成预压缩副本（.gz / .br）。
 *
 * 为什么要预压缩，而不是让 nginx 实时压：
 *  1. nginx 的 `gzip_comp_level` 默认是 1，压缩率很差 —— 实测主入口 JS 实时压出来 1782KB，
 *     而离线 gzip -9 只要 1563KB，白白多传 219KB；主 CSS 更夸张，120KB vs 86KB。
 *     调高 comp_level 会把 CPU 成本压到每个请求上，预压缩则是一次性的，可以放心用最高档。
 *  2. 实时压缩要等整段压完才出首字节。实测测试机上一个 5MB 的 JS，TTFB 里有可观的一段
 *     是花在压缩上的；预压缩后 nginx 直接 sendfile 静态文件。
 *  3. brotli 只能靠预压缩来用（见下）。
 *
 * nginx 侧配套开关在 `nginx.conf`：`gzip_static on` 让 nginx 优先送同名 `.gz`。
 *
 * 关于 brotli：同一份资源 brotli q11 比 gzip -9 还能再小 20%（实测首屏 1650KB → 1279KB），
 * 但 nginx 送 `.br` 需要 ngx_brotli 模块，而官方 nginx:1.27-alpine 没编译它
 * （`nginx -V` 里搜不到 brotli）。所以这里**默认只产 `.gz`** —— 生成一堆当前送不出去的
 * `.br` 只会白占 3.5MB 镜像体积。等镜像带上模块，把下面 FORMATS 加上 'br'、nginx.conf
 * 加 `brotli_static on` 即可，两处一行。
 *
 * 注意：预压缩会让 dist 变大（多出一份压缩副本，约 +4.4MB）。这是拿部署包体积换每个用户
 * 的首屏时间，对带宽受限的部署环境是划算的 —— 部署传一次，用户每天加载很多次。
 */
import { readdirSync, readFileSync, statSync, writeFileSync } from 'node:fs'
import { join, extname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { gzipSync, brotliCompressSync, constants } from 'node:zlib'

const distDir = join(fileURLToPath(new URL('../', import.meta.url)), 'dist')

// 只压文本类资源。png/jpg/webp/woff2 等本身已是压缩格式，再压一遍只会更大。
const COMPRESSIBLE = new Set([
  '.js', '.mjs', '.css', '.html', '.json', '.svg', '.txt', '.xml', '.map', '.wasm',
])
// 小文件压完可能比原文还大，且省下的字节还不够一个 TCP 包，不值得多占两个 inode。
const MIN_BYTES = 1024
// 要产出哪些预压缩格式。加 'br' 前先确认镜像里的 nginx 有 ngx_brotli 模块，
// 否则 `.br` 谁也送不出去（见文件头说明）。
const FORMATS = ['gz']

const collect = (dir, out = []) => {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name)
    if (entry.isDirectory()) collect(full, out)
    else out.push(full)
  }
  return out
}

let raw = 0
let gz = 0
let br = 0
let count = 0

for (const file of collect(distDir)) {
  if (file.endsWith('.gz') || file.endsWith('.br')) continue
  if (!COMPRESSIBLE.has(extname(file))) continue
  const size = statSync(file).size
  if (size < MIN_BYTES) continue

  const buf = readFileSync(file)

  for (const format of FORMATS) {
    const out =
      format === 'gz'
        ? gzipSync(buf, { level: 9 })
        : brotliCompressSync(buf, {
            params: {
              [constants.BROTLI_PARAM_QUALITY]: 11,
              [constants.BROTLI_PARAM_SIZE_HINT]: size,
            },
          })
    // 压完反而更大就别留了（几乎只发生在已高度压缩的内容上）。
    if (out.length >= size) continue
    writeFileSync(`${file}.${format}`, out)
    if (format === 'gz') gz += out.length
    else br += out.length
  }
  raw += size
  count += 1
}

const mb = (n) => `${(n / 1048576).toFixed(2)} MB`
const pct = (n) => `${((1 - n / raw) * 100).toFixed(1)}%`
const parts = FORMATS.map((f) =>
  f === 'gz' ? `gzip ${mb(gz)}（省 ${pct(gz)}）` : `brotli ${mb(br)}（省 ${pct(br)}）`,
)
console.log(`[precompress-dist] ${count} 个文件：原始 ${mb(raw)} → ${parts.join('、')}`)
