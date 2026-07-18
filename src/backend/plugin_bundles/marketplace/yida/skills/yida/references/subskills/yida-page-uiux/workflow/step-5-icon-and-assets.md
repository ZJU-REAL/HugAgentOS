# Step 5：图标与素材策略

> 核心：**默认内联 SVG，图标只作功能用途**。图标是最明显的 AI 味来源之一，纪律要硬。

## 图标策略

**默认方案：内联 SVG 语义集（零依赖、无死链）**

- 维护一份「语义 → SVG path」映射表 + `renderIcon(name, size, color)` 助手（骨架复用 `../../yida-custom-page/references/assets-guide.md` 已有的 `renderIcon`）。
- 描边粗细 / 端点风格随 Step 4 的视觉方向统一（细线=克制高端；2px 圆头=温暖亲和）。
- 常用 B 端语义清单：`search 搜索 / filter 筛选 / add 新建 / edit 编辑 / delete 删除 / more 更多 / arrow 箭头 / check 完成 / warning 告警 / user 用户 / calendar 日期 / export 导出 / refresh 刷新 / setting 设置`。同页只用一套风格。

**opt-in 方案：iconfont（仅在用户提供自己的项目 URL 时）**

- 用 `this.utils.loadStyleSheet(url)` 加载，**URL 必须是用户提供的 `at.alicdn.com` / `alicdn.com` 项目地址**（该域在 `alicdn.com` 下，符合 CDN 安全规范）。
- ❌ **禁止 AI 编造任何 iconfont URL**——编造的地址要么 404、要么指向他人图标集，是死链和安全风险源。
- 用户没有现成图标集时，引导用户自助：iconfont.cn 建项目 → 加入图标 → 「查看在线链接」发布 → 把生成的 `//at.alicdn.com/t/c/xxxxxx.css` 粘回来。拿到后才 opt-in。

**图标纪律（去 AI 味硬约束）**

- 图标**只作功能用途**：按钮、状态、导航、可点操作。
- ❌ 禁止**每个章节/卡片标题前配一枚装饰性线性图标**——人做设计很少这样，AI 特别爱这么干，是最明显的 AI 味来源之一。
- ❌ 禁止用 **emoji 当图标**。
- 同一页面只用一套图标风格（描边粗细、端点、填充方式统一）。

## 素材（图片/音效）

需要图片/音效时，参考 `../../yida-custom-page/references/assets-guide.md`：优先无署名素材库（Unsplash/Pexels/Pixabay/Mixkit），生产资源上自有 CDN，遵守 CDN 安全规范（只用 `g.alicdn.com`/`alicdn.com` 或企业自托管，禁 `fonts.googleapis.com`/`cdn.jsdelivr.net`/来源不明 CDN）。

## 产出

在决策块「图标策略」一行记录：内联 SVG 语义集（默认）/ 用户提供的 iconfont URL（opt-in）；描边风格；只作功能用途。

## 下一步

→ [Step 6：去 AI 味自检](step-6-deai-selfcheck.md)
