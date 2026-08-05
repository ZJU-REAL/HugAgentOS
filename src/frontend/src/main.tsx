import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { ConfigProvider } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import enUS from 'antd/locale/en_US'
import { getLang } from './i18n'
import 'antd/dist/reset.css'
import './index.css'
import './styles'
import App from './App.tsx'
import ApiDocApp from './ApiDocApp.tsx'
import SharePreviewApp from './SharePreviewApp.tsx'
import { appTheme } from './appTheme'
import { installPreloadErrorReload } from './preloadReload'

// 社区版入口：只挂主应用 / API 文档 / 分享预览。
// 内容台（/admin）与系统台（/config）属商业版，本树不含对应代码。

installPreloadErrorReload()

// 文件拖到非拖放区时，阻止 webview 的默认行为（直接导航打开该文件、离开 SPA）。
// 桌面端已禁用 Tauri 内建拖放拦截（disable_drag_drop_handler），全靠这层兜底；
// 各拖放区（useFileDropZone）在自己的 onDrop 里 preventDefault，不受影响。
window.addEventListener('dragover', (e) => e.preventDefault())
window.addEventListener('drop', (e) => e.preventDefault())

const isApiDocs = window.location.pathname.startsWith('/api-docs')
const isSharePreview = new URLSearchParams(window.location.search).has('share')

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ConfigProvider theme={appTheme} locale={getLang() === 'en' ? enUS : zhCN}>
      {isSharePreview ? <SharePreviewApp /> : isApiDocs ? <ApiDocApp /> : <App />}
    </ConfigProvider>
  </StrictMode>,
)
