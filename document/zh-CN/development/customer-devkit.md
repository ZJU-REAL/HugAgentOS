# 客户独立开发包
本包可以在没有 HugAgentOS 主项目源码的电脑上使用。需要 Node.js 22.12 以上版本、npm 和浏览器。首次安装需要访问 npm 软件源。本包不是离线依赖全集。

## 1. 检查交付文件
包内包含 vendor/ 下的 SDK 安装包、src/ 下的客户源码、开发服务器配置、四个 HTML 入口、锁文件及 SHA256SUMS.json。SDK 的 preview 入口是已编译的公共契约宿主。它不包含主项目源码，也不复刻正式产品的全部渲染器。

在解压目录执行：
```bash
node --input-type=module -e "import{readFileSync}from'node:fs';import{createHash}from'node:crypto';for(const[p,h]of Object.entries(JSON.parse(readFileSync('SHA256SUMS.json')))){if(createHash('sha256').update(readFileSync(p)).digest('hex')!==h)throw Error(p)}console.log('Integrity OK')"
npm ci
npm run dev
```
打开 http://127.0.0.1:4310/preview.html。Windows PowerShell、macOS 和 Linux 都可运行上述 npm 命令。

## 2. 定制界面
编辑 src/customization.tsx 和 src/theme.css。保存后浏览器自动更新。
预览页加载与正式平台相同的 SDK Provider、五个插槽及客户组件。
它使用本地模拟消息。发送、取消、附件、确认及问题操作不访问后端。
需要完整产品布局及富内容时，由供应方在正式宿主中进行最后一次集成验收。

## 3. 接入后端
把 .env.example 复制为 .env.local。将 HUGAGENT_BACKEND_URL 改成供应方提供的开发后端源站，例如 https://dev-api.example.com。不能追加 /api、路径、账号密码或查询参数。不要填写生产凭据。
重启 npm run dev。环境配置只在服务器端读取，不会作为 VITE_ 变量注入浏览器。
开发服务器默认仅监听本机。不要把此调试服务器发布到公网。

打开 /debug.html，点击 Sign in to backend。该链接打开后端原有的 /login 页面。输入由管理员提供的独立开发账号。登录回到 /session.html，页面立即移除地址栏的一次性票据，再调用 /api/v1/auth/ticket/exchange。回调还会核对本标签页发起的随机登录状态。会话使用 HttpOnly Cookie，不保存到 localStorage。
代理只转发本目标的会话 Cookie，过滤其他本机 Cookie 及 Authorization。它保留 Secure 和 SameSite 属性。使用 HTTP 本地开发时，请供应方提供合适的专用开发登录配置；不要关闭 TLS 校验或削弱生产 Cookie。
本示例直接支持平台本地账号登录。外部 SSO/OA 的回调域名需由供应方登记和提供适配，不应把生产登录页简单复制到本地。
如果后端使用自定义会话 Cookie 名，在 .env.local 配置 HUGAGENT_SESSION_COOKIE。目标变化后需重新登录。

## 4. 调试聊天和 API
/ 是真实后端聊天示例。测试发送、流式输出、上传、取消和刷新后的历史记录。问题和文件确认会显示在对话下方。
/debug.html 可检查当前账号、聊天列表和后端就绪状态。它显示 HTTP 状态和响应文本。没有配置后端时，预览仍可使用，真实 API 不可用。
在浏览器开发者工具 Network 中查看 /api/v1/agents/responses 的 EventStream，以及上传的 multipart 请求。调试输出可能含账号和业务内容，分享前应删除这些内容。
401：先登录。502：检查后端地址及网络。登录后仍然 401：检查 Cookie 的 Secure、SameSite、名称及后端 AUTH_MODE 配置。上传 409：更换重复的文件名。
代理仅供开发使用。它不是生产网关，不持久保存会话，不提供管理员或注册权限。

## 5. 构建及交付
```bash
npm run build
npm pack
npm run test:package
npm run build:web
npm run preview:web
```
dist/ 是客户组件。组件构建会合并导入的子组件、样式和资源，避免拆分文件后漏打包。npm pack 生成的 hugagent-customer-chat-example-1.0.0.tgz 只包含客户组件产物和说明。把此包交给供应方安装到正式宿主，并通过 HUGAGENT_CHAT_CUSTOMIZATION 选择该包。
web-dist/ 是可独立部署的聊天、预览和调试静态页。正式部署时只发布实际需要的页面，使用正式 HTTPS 网关，把 /api/ 代理到后端根路径，并按平台登录流程代理 /login。开发服务器的代理不会打入静态网页。不要把 .env.local、node_modules、调试记录或账号密码交付给最终用户。
npm run standalone 保留旧示例的单入口构建，用于 /customer/ 路径；本开发包建议使用 build:web。

## 6. 升级
先备份客户项目。供应方提供新 SDK tgz 后，将它放入 vendor/，更新 package.json 中的依赖路径，执行 npm install。提交客户自己的锁文件。重新测试预览、登录、聊天及 build。
SDK 主版本不变时保持现有公共契约；主版本变化时按供应方迁移说明修改。客户源码由客户维护，平台升级不会覆盖它。
