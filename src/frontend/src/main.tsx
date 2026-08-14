import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import 'antd/dist/reset.css'
import './index.css'
import './styles'
import App from './App.tsx'
import ApiDocApp from './ApiDocApp.tsx'
import SharePreviewApp from './SharePreviewApp.tsx'
import { AppThemeProvider } from './AppThemeProvider'
import { installPreloadErrorReload } from './preloadReload'

// 社区版入口：只挂主应用 / API 文档 / 分享预览。
// 内容台（/admin）与系统台（/config）属商业版，本树不含对应代码。

installPreloadErrorReload()

const isApiDocs = window.location.pathname.startsWith('/api-docs')
const isSharePreview = new URLSearchParams(window.location.search).has('share')

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    {/* 分享预览是对外页面，锁定浅色（与 index.html 防闪烁脚本的 share 判断保持一致） */}
    <AppThemeProvider forceLight={isSharePreview}>
      {isSharePreview ? <SharePreviewApp /> : isApiDocs ? <ApiDocApp /> : <App />}
    </AppThemeProvider>
  </StrictMode>,
)
