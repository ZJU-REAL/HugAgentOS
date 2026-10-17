# 客户对话界面定制

HugAgentOS 提供三种定制入口：页面配置、客户组件包和独立前端。

| 修改范围 | 使用入口 | 升级方式 |
| --- | --- | --- |
| 品牌、文案、首页问题、菜单 | 现有页面配置 | 保留部署的数据配置 |
| 首页、输入区、消息、侧栏、会话布局 | 客户组件包 | 客户仓库锁定 SDK 版本，独立构建 |
| 接入已有平台或完整重做界面 | 对话 HTTP/SSE SDK | 独立发布前端，验证后端接口兼容性 |

## 组件包

客户代码只依赖 `@hugagent/chat-sdk/react`。不要导入 HugAgentOS 的 store、控制器或内部组件路径。
公开接口包含不可变视图、动作和五个组件插槽。每个插槽收到默认内容 children。
保留 children 可以继续使用完整的文件预览、引用、工具确认等产品交互。

```tsx
import { defineChatCustomization, useChatRuntime, type ChatSlotProps } from '@hugagent/chat-sdk/react';

function Welcome({ children }: ChatSlotProps) {
  const { view, actions } = useChatRuntime();
  return <section>
    <header>{view.productName}</header>
    <button onClick={() => actions.setDraft('请分析这份材料')}>填写模板</button>
    {children}
  </section>;
}
export default defineChatCustomization({
  apiVersion: 1, id: 'customer-portal', slots: { welcome: Welcome },
});
```

可替换位置为 welcome、composer、message、sidebar、conversation。message 额外收到当前消息的复制视图。
客户输入区可以通过公开动作管理草稿、附件上传/重试/移除、发送/取消、重新生成及编辑重发。
确认和问题回答保留服务端过期信息。专门的计划、画布等复杂交互可继续组合默认 children。
客户 CSS 必须限定在 `[data-chat-customization="customer-portal"]` 内，使用宿主颜色和尺寸令牌。
组件包属于受信任的应用代码，具有应用权限；此入口不执行用户通过 URL 指定的脚本。

仓库内示例：

```sh
cd src/frontend
npm ci
HUGAGENT_CHAT_CUSTOMIZATION=../../examples/customer-chat/src/customization.tsx npm run build
```

未设置 HUGAGENT_CHAT_CUSTOMIZATION 时构建官方界面。选择不存在的模块会让构建失败。
API 版本不兼容时，应用显示明确错误。
公共分享对话页保留默认组件，不获得登录应用的定制运行时。

## 独立客户仓库

先在 SDK 目录执行 `npm ci && npm pack`，将安装包交付到客户仓库。
复制 examples/customer-chat 后，把 SDK 的 file 相对依赖改为锁定的安装包或私有仓库版本。
执行 `npm install <SDK安装包>`、`npm run build` 和 `npm pack`。
客户包的 dist 包含 JavaScript、声明文件和样式。
在平台前端安装客户包，再将 HUGAGENT_CHAT_CUSTOMIZATION 设置为客户包名。

SDK 安装包不依赖平台源码。客户源码和锁文件放在客户仓库，平台升级不复制或覆盖客户目录。
分别记录后端、SDK、客户包和前端镜像的版本。跨主版本升级需要适配和验证。

## 独立前端

`@hugagent/chat-sdk` 提供 HTTP 客户端。它不依赖 React。
示例目录的 `npm run standalone` 生成独立静态文件。
构建时通过 VITE_CUSTOMER_API_BASE 和 VITE_CUSTOMER_LOGIN_URL 指定 API 与登录入口。
推荐通过同源反向代理复用平台会话。跨域场景由服务端配置认证、Cookie、CORS 和 CSRF 策略。
浏览器中不要存放管理员令牌或共享密钥。

客户端提供会话、分页历史、发送、续播、快照订阅、取消、文件上传和人工确认接口。
断开网络不等于任务完成。保留 run_id 和 event_offset，以便续播。
AbortSignal 只关闭本地连接；取消后端任务必须调用 cancel。
工具人工确认事件必须显示给用户。不能静默批准。
订阅快照是权威状态，收到后替换旧投影；终态快照后连接正常关闭。
未知事件保留给调用方处理。独立示例展示文本和人工交互，不包含完整产品的所有领域渲染器。
SDK 的完整契约见 packages/chat-sdk/README.md。

## Docker 构建

标准前端 Dockerfile 自动构建 SDK。Compose 将 HUGAGENT_CHAT_CUSTOMIZATION 作为构建参数传入。
例如选择仓库示例时，构建变量可设为 `../../examples/customer-chat/src/customization.tsx`。
客户包代码、依赖和样式必须进入构建上下文。外部客户仓库使用自己的构建阶段安装客户包。
后端镜像和客户前端镜像独立升级。升级前运行目标组合的登录、对话、附件及人工确认验收。
原生桌面端内置前端，因此客户界面变更仍须按对应平台流程重新构建客户端。

## 验证

在 WSL 项目根目录，以现有 Node 20+ 工具链执行：

```sh
cd packages/chat-sdk
npm ci
npm test
cd ../../src/frontend
npm ci
npm run build
node scripts/test-chat-customization-contract.mjs
```

完整应用测试使用 scripts/chat-customization-environment.py，创建独立容器、临时数据库和确定性模型。
它不读取真实 .env，也不连接业务数据库。先构建 examples/customer-chat 的 standalone 产物。
启动后运行 `node scripts/test-chat-customization.mjs default`。
定制构建完成后重新准备测试静态目录，再运行该脚本的 custom 模式。
测试结束执行环境脚本 down。详细命令和本次结果见内部实施记录。

## Independent customer developer kit

The SDK, customer source, host contract preview, login callback and API proxy can be exported without the main project source. Run node scripts/export-customer-devkit.mjs from the project root. Follow document/zh-CN/development/customer-devkit.md for installation and acceptance.
