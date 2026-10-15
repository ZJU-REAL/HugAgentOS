# Workbench SDK 与插件页面

SDK 1.0.0 使用 UI v3 与工作台协议 1.0.0。SDK 独立打包，保持 private / UNLICENSED；不自动发布 npm。

## 构建页面

先在 packages/workbench-protocol 构建协议，再在 packages/workbench-sdk 执行 npm ci、npm test、npm pack。把生成的 SDK tar 包安装到独立插件工程。插件工程仅引用 SDK；无需引用宿主源码、React、store 或桌面 API。

```js
import {connect} from '@hugagent/workbench-sdk';
const connection = await connect();
const info = await connection.request('host.info');
document.querySelector('p').textContent = info.locale;
connection.onUpdate(info => {
  document.documentElement.dataset.theme = info.theme;
});
window.addEventListener('unload', () => connection.dispose());
```

把 SDK 与页面代码打包进插件自己的入口脚本。JavaScript 依赖必须合并到入口脚本；关闭代码分块与动态 import。HTML 可以引用包内独立的入口 JS、CSS 和图片，服务器在隔离页面中嵌入这些静态资源。CSS import 与 srcset 需在插件构建时展开。页面展开后最多 8 MiB；超限明确失败，不加载远程 CDN 或远程资源。

```json
{
  "name": "reports",
  "version": "1.0.0",
  "extensions": {"org.hugagent": {"ui": {
    "version": 3,
    "contributes": {
      "modules": [{"id": "main", "surface": "workspace", "entry": "web/index.html", "grants": []}],
      "applications": [{"schemaVersion": "1.0.0", "id": "reports", "title": "Reports", "target": {"kind": "plugin", "moduleId": "main"}}]
    }
  }}}
}
```

ZIP 中保留 plugin.json 和 web/ 目录。UI-only 插件无需声明技能或 MCP。通过现有插件导入入口安装，再从应用中心打开。未打开的页面不创建 iframe；关闭、停用或账号切换移除页面并释放桥接。旧 UI v1/v2 工具结果与画布仍使用原协议。

## 握手与授权

SDK 使用每次文档独立的 nonce 和 MessageChannel。握手在 8 秒内重试；失败仅影响该页面，用户可以重试。nonce 使用 getRandomValues，支持非安全 HTTP 与 opaque iframe。

可用能力取决于已安装的宿主扩展；必需能力须在就绪前协商。request 的未知方法返回 unknown_method；单条最多 256 KiB、8 个并发、30 次/10 秒，请求在 10 秒后取消。AbortSignal 与 dispose 会取消等待。

上下文只包含 applicationKey、installId、packageRevision。主题与语言随宿主握手与更新传递；不传宿主凭据、聊天正文或原生权限。语言切换沿用宿主重新加载行为。

权限选择器使用完整 plugin applicationKey。声明与外部应用同名的 id 不授予插件访问权。目录、launch 和每个资产请求都校验用户、安装、版本与启用状态。allowed_apps 的 null 和 force-all 保持既有继承语义。

独立工程与权限模拟参见[开发预览](workbench-preview.md)。真实宿主另行验收。参见[管理员指南](workbench-administration.md)、[错误码](workbench-errors.md)和[迁移](workbench-migration.md)。

应用键是不透明引用，由完整安装绑定的摘要与含微秒的 UTC 创建时间组成。导航偏好与权限选择器必须使用目录返回的完整键。升级保留该键；卸载后重装会产生新键，旧启动与包资源地址随之失效。
