import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import 'antd/dist/reset.css'
import './index.css'
import './styles'
import App from './App.tsx'
import PromptConfigPage from './components/prompts/PromptConfigPage'
import ApiDocApp from './ApiDocApp.tsx'
import SharePreviewApp from './SharePreviewApp.tsx'
import { AppThemeProvider } from './AppThemeProvider'
import { installPreloadErrorReload } from './preloadReload'

// CE Config exposes shared prompt management only.

installPreloadErrorReload()

const isApiDocs = window.location.pathname.startsWith('/api-docs')
const isSharePreview = new URLSearchParams(window.location.search).has('share')

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    {/* 分享预览是对外页面，锁定浅色（与 index.html 防闪烁脚本的 share 判断保持一致） */}
    <AppThemeProvider forceLight={isSharePreview}>
      {isSharePreview ? <SharePreviewApp /> : isApiDocs ? <ApiDocApp /> : window.location.pathname.replace(/\/$/, '') === '/config' ? <PromptConfigPage /> : <App />}
    </AppThemeProvider>
  </StrictMode>,
)
