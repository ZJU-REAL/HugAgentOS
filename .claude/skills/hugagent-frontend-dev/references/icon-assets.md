# 图标资产清单

本技能带两批图标：

- `assets/design-v2/`：**v2 设计规范**的界面图标风格样例、标志与网页图标（新代码以此为风格基准）
- `assets/icons/`、`assets/mcp-icons/`：v1 设计稿导出的图标（存量界面在用，部分带硬编码色值，复用前先改成 `currentColor`）

## v2 资产（`assets/design-v2/`）

| 目录 | 内容 | 规则 |
|---|---|---|
| `icons-ui/` | 12 个界面图标：new-chat / search / knowledge / skill / automation / my-space / attachment / settings / send / chevron-right / check / close | 24px 画板、留白 2px、线宽 1.5、圆头圆角、`stroke="currentColor"` |
| `logo/hugagentos-mark*.svg` | 方形标：标准 / small（≤24px 用）/ reverse（深色底）/ mono-black / mono-white | 只用于方形容器；禁止与标准字并排 |
| `logo/hugagentos-glyph-*.svg` | 环线（无底板）：蓝 / 白 / 黑 | 头像、App 图标内自行合成 |
| `logo/favicon.svg` | 网页图标（小尺寸版） | 16/32px 使用 |
| `tokens/` | v2 令牌参考实现（CSS / JSON / motionTokens / appTheme） | 见 `css-variables.md` |

图标规则（v2，详见 `ui-design-spec.md` §6）：图标是文字的一部分——尺寸跟随字号、线宽跟随字重（13px→1.5，15/20px→2）、
颜色继承文字色；默认线性，选中态换面性。**不要给图标单独上品牌色，火花绿 `#19D3A7` 不进界面。**

```tsx
// v2 写法：currentColor + 跟随字号
export function SearchIcon({ size = '1em' }: { size?: string | number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor"
         strokeWidth={1.5} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="11" cy="11" r="6.5" /><path d="M16 16l4 4" />
    </svg>
  );
}
```

---

## v1 图标

## 使用方式

将 SVG 内容内联到 React 组件中，或复制到项目的 `src/frontend/public/` 或 `src/frontend/src/assets/` 目录后通过 import 引用。

```tsx
// 方式一：内联 SVG（推荐小图标）
function SendIcon() {
  return (
    <svg width="34" height="34" viewBox="0 0 34 34" fill="none">
      {/* ... SVG path ... */}
    </svg>
  );
}

// 方式二：img 标签引用
import sendIcon from '../assets/icons/send.svg';
<img src={sendIcon} alt="发送" />
```

---

## 通用 UI 图标 (`assets/icons/`)

| 文件 | 说明 | 使用场景 |
|------|------|---------|
| `logo.svg` | HugAgentOS Logo（蓝底白字） | 侧边栏顶部、登录页 |
| `发送.svg` | 发送按钮（蓝色圆形） | 聊天输入框发送按钮 |
| `搜索.svg` | 搜索图标（灰色放大镜） | 搜索栏 |
| `附件.svg` | 附件/回形针图标 | 文件上传按钮 |
| `关闭.svg` | 关闭/叉号图标 | 弹窗关闭、面板关闭 |
| `展开.svg` | 展开图标 | 侧边栏展开 |
| `收起.svg` | 收起图标 | 侧边栏收起 |
| `新建对话 .svg` | 新建对话图标 | 侧边栏新建按钮 |
| `设置.svg` | 设置齿轮图标 | 设置入口 |
| `退出.svg` | 退出/登出图标 | 退出登录 |
| `更多.svg` | 更多操作（三点） | 右键菜单、操作菜单 |
| `头像.svg` | 用户头像占位 | 用户信息区域 |
| `赞.svg` | 点赞图标 | 消息反馈 |
| `选中.svg` | 选中/勾选图标 | 列表选中状态 |
| `箭头-下.svg` | 向下箭头 | 下拉菜单、展开折叠 |
| `帮助.svg` | 帮助图标 | 帮助入口 |
| `更新记录.svg` | 更新记录图标 | 版本更新面板入口 |
| `提示词.svg` | 提示词图标 | 提示词库入口 |
| `思考.svg` | 思考模式图标 | 思考模式切换 |
| `快速.svg` | 快速模式图标 | 快速模式切换 |

### 导航类图标

| 文件 | 说明 | 使用场景 |
|------|------|---------|
| `技能库.svg` | 技能库导航图标 | 侧边栏导航 |
| `子智能体.svg` | 子智能体导航图标 | 侧边栏导航 |
| `工具库.svg` | 工具库导航图标 | 侧边栏导航 |
| `知识库.svg` | 知识库导航图标 | 侧边栏导航 |
| `应用中心.svg` | 应用中心图标 | 侧边栏导航 |

### 能力卡片图标

| 文件 | 说明 | 使用场景 |
|------|------|---------|
| `icon1.svg` | 能力卡片图标 1 | 首页能力卡片 |
| `icon2.svg` | 能力卡片图标 2 | 首页能力卡片 |
| `icon3.svg` | 能力卡片图标 3 | 首页能力卡片 |
| `企业画像.svg` | 企业画像场景图标 | 快捷场景 |
| `企业调研.svg` | 企业调研场景图标 | 快捷场景 |

---

## MCP 工具库图标 (`assets/mcp-icons/`)

| 文件 | 说明 | 使用场景 |
|------|------|---------|
| `互联网.svg` | 互联网搜索 | MCP 工具：互联网搜索 |
| `数据.svg` | 数据查询 | MCP 工具：数据类 |
| `数据库.svg` | 数据库 | MCP 工具：数据库查询 |
| `报告.svg` | 报告生成 | MCP 工具：报告 |
| `知识.svg` | 知识库 | MCP 工具：知识检索 |
| `学习.svg` | 学习/文档 | MCP 工具：学习类 |
| `来源.svg` | 数据来源 | MCP 工具：来源引用 |
| `清单.svg` | 清单/列表 | MCP 工具：清单类 |
| `格式刷.svg` | 格式化 | MCP 工具：格式处理 |
| `开.svg` | 开关-开 | MCP 工具开启状态 |
| `关.svg` | 开关-关 | MCP 工具关闭状态 |

---

## 设计约束（v2 替代 v1 的约定）

- v1 约定"图标主色 `#126DFF`、非激活 `#808080`"**已废止**：v2 图标继承文字色，激活态用面性而不是换色
- v1 SVG 里的硬编码 `fill` / `stroke` 色值在深色模式下不会反相；复用前改成 `currentColor`，否则过不了视觉走查
- 尺寸跟随字号（13 / 15 / 20px 常用），点击区按桌面 ≥32、触屏 ≥44 外扩
- 详见 `ui-design-spec.md` §6「图标」
