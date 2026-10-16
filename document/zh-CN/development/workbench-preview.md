# 工作台开发预览

[English](../../en/development/workbench-preview.md) · [SDK](workbench-sdk.md)

开发者可用私有 SDK 安装产物在宿主仓库外开发。
产物包含 README、API、VERSIONS、CHANGELOG、开发工具和工作区示例。
工程无需宿主源码、React store 或原生桌面 API。
公开注册表发行需要另行授权。

## 独立工程

1. 解压管理员提供的 SDK tar 包。
2. 将 `package/examples/workspace/` 复制到独立工程。
3. 在工程中安装同一 SDK tar 包。
4. 构建并校验插件。

```sh
npm install --ignore-scripts --offline /artifacts/hugagent-workbench-sdk-1.0.0.tgz
npm run build
npm run validate
npm run preview
```

离线构建需要 Node 20 及以上，以及已缓存的 esbuild 0.27.3。
SDK tar 包没有运行时注册表依赖。
缓存缺失时，应按已批准的依赖准备流程提供编译工具。
构建将 SDK 合并到自包含 HTML，不使用 CDN 或宿主导入。

## 权限预览

打开命令输出的 127.0.0.1 地址。点击“Open example”前不创建 iframe。
选择允许、拒绝或缺少必需能力场景。
打开示例，切换主题和语言，选择 Overview 与 Files。
通过浏览器历史和刷新检查子页面恢复。
查询示例数据源、选择模拟文件引用并保存该引用。
拒绝场景应显示 capability_denied；不兼容场景应显示 host_incompatible。
关闭操作移除 iframe 和消息通道。
切换权限场景先关闭旧会话，再打开新会话。

预览属于协议模拟，不含真实账号或生产后端。
它不读取真实文件，也不执行真实数据查询。
预览服务只监听回环地址，只提供固定预览路由。
iframe 使用 opaque origin，不能请求外部资源。
不得把预览当作认证绕过通道或真实鉴权证据。

## 真实宿主演练

将示例数据源地址替换为管理员批准的受控接口。
上游认证配置只保留服务端。不得将凭据写入 HTML、日志或 URL 查询参数。
把 dist/plugin.json 和 dist/web/ 放在 ZIP 根目录。
通过宿主插件管理入口导入。
从认证后的目录读取准确 applicationKey。
打开插件，通过宿主审批其声明能力。
测试两个子页面、刷新和浏览器历史。
查询已批准的数据源，选择临时自有文件，并确认保存。
用拒绝授权、另一账号、停用安装及过期版本重复测试。
检查越权调用失败，并核对资源释放。

真实数据/文件、目标系统登录和原生客户端结果必须分别记录。
预览不能代替这些检查。
参见[错误码](workbench-errors.md)和[迁移](workbench-migration.md)。

## CLI

`workbench validate <JSON> [--assets directory]` 输出字段路径和错误码。
它接受独立应用声明或 UI v3 插件清单。
它复用统一协议校验器，检查模块引用和有大小限制的包内 HTML。
完整插件包仍须经过宿主权威校验。

`workbench preview <plugin-directory> [--port 4179]` 在启动前校验。
端口 0 可自动选择空闲回环端口。
退出码 0 表示声明有效，1 表示输入无效，2 表示命令或预览启动失败。
