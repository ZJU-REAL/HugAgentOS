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

const isApiDocs = window.location.pathname.startsWith('/api-docs')
const isSharePreview = new URLSearchParams(window.location.search).has('share')

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ConfigProvider theme={appTheme} locale={getLang() === 'en' ? enUS : zhCN}>
      {isSharePreview ? <SharePreviewApp /> : isApiDocs ? <ApiDocApp /> : <App />}
    </ConfigProvider>
  </StrictMode>,
)
