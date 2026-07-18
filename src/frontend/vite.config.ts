import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'

// https://vite.dev/config/
export default defineConfig(({ mode }) => {
  const repoRoot = path.resolve(__dirname, '../..')
  const env = loadEnv(mode, repoRoot, '')
  const backendPort = env.BACKEND_PORT || env.PORT || '3001'
  const frontendPort = Number(env.FRONTEND_PORT || '3002')
  const proxyTarget = `http://localhost:${backendPort}`

  return {
    plugins: [react()],
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
