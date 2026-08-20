import { defineConfig, loadEnv } from 'vite'
import type { PluginOption } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'

// @univerjs/engine-render 为文档排版内置了 78 份 Liang 连字符断词表（匈牙利语、挪威语
// nb/nn/no、德语 1901/1996 两套正字法、教会斯拉夫语、南非荷兰语…），未压缩合计约 4.6MB，
// 全部以动态 import 挂在一张语言枚举表上。本产品只有中英两种界面语言：中文不做连字符
// 断词，英文用 en-us（已内联进 engine-render 的 index.js，不单独成块）和 en-gb（Univer
// 初始化时会主动预加载）。其余 76 份永远不会被任何用户请求到，只白占 dist 与镜像体积，
// 并拖慢每次往测试机 docker cp 的传输——测试机公网出口带宽只有几 Mbps，这 4.6MB 很肉疼。
//
// 这里在 load 阶段把它们换成空模块。Univer 的 loadPattern 本就对空返回值做了 no-op 处理：
//   async loadPattern(e){ let t=al[e]; if(!t) return; let n=await t();
//     let r=Array.isArray(n)?n:n?.[sl(e)]; r!=null && (...) }
// 拿到 undefined 时直接跳过、不抛错，所以裁掉是安全的。
const UNIVER_HYPHENATION_DIR = '@univerjs/engine-render/lib/es/'
// 只保留英文两档。语言名自带连字符（de-1901、la-x-classic），rollup 追加的哈希也可能含
// 连字符（en-gb-Cr-964h0.js），拆分不可靠，所以按前缀匹配。
const HYPHENATION_KEEP = ['en-us-', 'en-gb-']
// 该目录下除 index.js 外全是断词表（当前版本 78 份）。Univer 升级后若产物布局变了，
// 命中数会掉到 0——宁可让构建炸掉，也不要静默把 4.6MB 加回产物。
const HYPHENATION_MIN_STUBBED = 60

function univerHyphenationTrim(): PluginOption {
  let stubbed = 0
  return {
    name: 'univer-hyphenation-trim',
    enforce: 'pre',
    apply: 'build',
    load(id) {
      const file = id.split('?')[0].replace(/\\/g, '/')
      const at = file.indexOf(UNIVER_HYPHENATION_DIR)
      if (at < 0) return null
      const base = file.slice(at + UNIVER_HYPHENATION_DIR.length)
      if (base.includes('/') || !base.endsWith('.js') || base === 'index.js') return null
      if (HYPHENATION_KEEP.some((prefix) => base.startsWith(prefix))) return null
      stubbed += 1
      return 'export default undefined\n'
    },
    buildEnd(error) {
      if (error) return
      if (stubbed < HYPHENATION_MIN_STUBBED) {
        this.error(
          `univer-hyphenation-trim 只裁掉了 ${stubbed} 份连字符断词表（预期 >= ${HYPHENATION_MIN_STUBBED}）。` +
            `多半是 @univerjs/engine-render 升级后改了产物布局，请核对 ${UNIVER_HYPHENATION_DIR} 下的文件名规则。`,
        )
      }
    },
  }
}

// https://vite.dev/config/
export default defineConfig(({ mode }) => {
  const repoRoot = path.resolve(__dirname, '../..')
  const env = loadEnv(mode, repoRoot, '')
  const backendPort = env.BACKEND_PORT || env.PORT || '3001'
  const frontendPort = Number(env.FRONTEND_PORT || '3002')
  const proxyTarget = `http://localhost:${backendPort}`

  return {
    plugins: [react(), univerHyphenationTrim()],
    // 从项目根目录读取 .env，使 VITE_API_BASE_URL 和 SSO_LOGIN_URL 在 build 时生效
    envDir: repoRoot,
    envPrefix: ['VITE_', 'SSO_LOGIN_URL'],
    server: {
      host: '0.0.0.0',
      port: frontendPort,
      strictPort: true,
      proxy: {
        '/api': {
          target: proxyTarget,
          changeOrigin: true,
          rewrite: (path) => path.replace(/^\/api/, ''),
        },
        '/mock-sso': {
          target: proxyTarget,
          changeOrigin: true,
        },
      },
    },
  }
})
