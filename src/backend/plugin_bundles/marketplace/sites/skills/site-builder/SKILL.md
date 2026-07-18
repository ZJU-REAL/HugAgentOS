---
name: site-builder
description: 用对话把用户的想法做成一个真实可访问的网站并一键发布上线。当用户说"做一个网站/页面/门户/展示站/落地页/看板/H5 并要能打开访问""帮我搭个站""把这份内容做成网页发布出去"，或要在已发布站点上继续迭代修改时，务必使用本技能。它教你两条建站路径——简单内容手写静态站、复杂/精美需求用预装的 React 工程模板构建——并用 choose_design 让用户三选一设计方案，最后用 publish_site 发布为平台托管站点，拿到形如 /site/<slug>/ 的访问链接交付给用户。
---

# 对话建站（Site Builder）

本技能教你把用户的需求做成一个**完整可访问的网站**，发布后返回一个可直接打开的链接
（形如 `/site/<slug>/`）。发布能力由「站点」插件的 `site_publish` MCP 提供。

> **发布后平台会自动为站点建一个同名"项目"并把源码文件存进去**（后端自动完成）。
> 用户随后可在「实验室 → 站点」点站点卡片的「编辑」按钮，通过对话继续修改。

## 沟通原则（面向非技术用户）

把用户当成不懂技术的知识工作者：
- 谈**他的站点**——内容、样式、进度、访问方式；**不要**在给用户的消息里出现
  vite / npm / build / node_modules / 构建产物 / 目录路径 这类词，改说
  「正在搭建页面」「正在生成站点」「即将发布」。
- 每个阶段（准备设计方案 / 搭建 / 发布）至多一句简短进度播报。
- 可恢复的技术问题自己默默重试解决，不向用户展示报错细节；只有真正需要用户
  决策或提供信息时才提问。

## 第一步：选路径（先判断，再动手）

| 用哪条 | 判据 |
|---|---|
| **A. 静态站 fast path** | 单页内容展示、通知/介绍/菜单类落地页、需求简单明确、无组件化交互诉求 |
| **B. React 工程 capability path** | 多页面应用、交互组件、图表/数据看板、用户要求"好看/精致/App 感"、预期持续迭代 |

拿不准时：内容型选 A，产品型选 B。编辑已有站点时**不换路径**（见编辑一节）。

## 路径 A：静态站（保持简单）

1. **生成完整静态站**（用 write / bash）：
   - 放在 **`/workspace/site/`** 下（项目会话则直接在项目文件夹里）；
   - **必须**有 `index.html` 入口；可以有多页面、子目录、`css/`、`js/`、图片；
   - 样式/脚本尽量**内联或本地化**——外部 CDN 在内网环境可能加载不到。
2. **发布**：`publish_site(title='站点名称')`（`src_dir` 留空，后端自动定位）。
3. **交付**：把返回 `url` 以 markdown 链接发给用户（管理入口见「交付话术」）。

## 路径 B：React 工程（复杂/精美站点）

### B1. 初始化（一条命令，秒级完成）

```bash
bash "${SITE_TEMPLATE_HOME:-/opt/site-template}/init-react-site.sh" /workspace/site-src/<英文短名>
```

模板预装 React 18 + antd + echarts + tailwind v4 + lucide/motion/dayjs，依赖开箱即用。
脚本幂等，可反复执行（重开会话后也先跑它自愈环境）；它会打印本工程的构建
命令、产物目录与发布参数，后续步骤以其输出为准。

### B2. 设计三选一（choose_design，新建站点必做）

在动手写正式代码**之前**，让用户从 3 个设计方向里挑一个：

1. 若用户需求里关键信息缺失（受众/用途/内容/风格偏好），先用**一条消息**问清
   1-2 个最关键的问题；用户已给出完整方向或明确说"你定"则跳过。
2. 写 **3 个风格迥异的单文件 mockup** 到 `/workspace/design_options/{a,b,c}.html`：
   - 每个自包含（内联 CSS，不引外部资源），用**真实感内容**（真实标题/数据/文案，
     不要 lorem ipsum），三个方案用相同内容、只比设计（配色/布局/字体/密度）；
   - 避免自绘 SVG 插画——用排版、色彩、CSS 形状表达设计感。
3. 逐个截图并登记：
   ```bash
   npx playwright screenshot --viewport-size=1280,900 \
     file:///workspace/design_options/a.html /workspace/design_options/a.png
   ```
   每张图调 `sandbox_get_artifact(src_path='/workspace/design_options/a.png',
   name='design-a.png')` 拿 `file_id`。**这些截图是选择器素材，不要 pin_to_workspace。**
4. 调 `choose_design(question='您喜欢哪种设计风格？', options=[{id, title, brief,
   image_file_id}, ...])`——工具会**挂起等用户在界面上点选**（等多久都正常）。
5. 拿到返回后**严格按选中方案**展开：布局/配色/字体以该 mockup 为准，不混入其它
   方案元素；用户跳过或超时则选你最推荐的方案并向用户说明一句。

编辑已有站点、或用户已给出完整明确的设计要求时，**跳过**本步骤。

### B3. 实现 → 构建 → 发布

1. 按选定方案改 `src/`（页面放 `src/pages/`，路由表 `src/App.jsx`），并把
   `index.html` 的 `<title>` 改成真实站点名（模板占位是"站点建设中"，留着会
   显示在用户浏览器标签页上）。工程硬约束：
   - **HashRouter 不许换、`vite.config.mjs` 的 `base: './'` 不许改**（改了发布后打不开）；
   - 禁外部 CDN；静态资源放 `src/assets/` 交给构建打包；
   - 动态数据用 `src/lib/siteApi.js`（kvGet/kvSet/kvDelete/submitForm）；
   - 界面图标用预装图标库（`lucide-react` 或 `@ant-design/icons`），**禁止拿
     emoji 当图标**（跨系统渲染不一致、色相杂乱，是"AI 生成感"最强的元素）；
   - echarts 图表注意数值标签防裁切：柱状图外置 label 要给 `grid.top` 留够
     空间或适当抬高 `yAxis.max`；轴刻度单位归口到标题一处，不要每个刻度都带；
   - 优先用预装库（antd/echarts/…）；确需新依赖：编辑 `package.json` 后**重跑
     init 脚本**（此时会物化独立依赖副本并增量安装，可能需要几分钟——bash 调用
     记得给足超时），**禁止**在工程目录里直接 `npm install`。
2. 构建：`cd /workspace/site-src/<名> && npm run build`（产物自动落
   `/workspace/.site-dist/<名>/`）。构建报错先修再继续，禁止带错发布。
3. **发布前自检清单**（逐条过，都是高频遗漏）：
   - `index.html` 的 `<title>` 已改成真实站点名（不是"站点建设中"）；
   - 图表上的统计标注（均值线/合计等）由数据**计算得出**，不是拍脑袋写死的数；
   - 涨跌/同比类徽章按数据符号驱动（负增长要红色下箭头，不能写死绿色上箭头）；
   - y 轴不写死 min/max（换数据会裁剪出图），柱宽用 `barMaxWidth` 而非固定值；
   - 删掉未使用的 import 和死变量；重复样式抽成常量或小组件。
3. 发布（**两个参数都必传**）：
   ```
   publish_site(title='站点名',
                src_dir='/workspace/.site-dist/<名>',
                source_dir='/workspace/site-src/<名>')
   ```
   产物进托管，**源码工程**镜像进项目文件夹（保证「编辑」进来的是可改的源码）。
   漏传 `source_dir` 会导致项目里的源码被产物覆盖——绝不允许。
4. **尽早发第一版**：完成核心页面即可先发布（发布后源码即进项目保险箱，
   不受沙箱回收影响），再迭代细节发新版。

## 在同一会话里继续改

改完文件 → （路径 B 先重新 `npm run build`）→ 再次 `publish_site`（路径 B 仍带双参数）
并带 `site_id='<上次返回值>'`——URL 不变、版本 +1（不带 site_id 会新建另一个站点）。

## 从站点卡片「编辑」进来时

项目文件已带回工作区，路径 **`/myspace/<项目文件夹名>/`**（与站点同名）。流程：

1. 先 `glob('/myspace/<项目文件夹名>/', '**/*')` 看现有文件。
2. **看到 `package.json` = React 构建型工程**：
   - 先跑 `bash "${SITE_TEMPLATE_HOME:-/opt/site-template}/init-react-site.sh" /workspace/myspace/<uid>/<项目文件夹名>`
     自愈依赖环境（幂等，重开会话后 node_modules 链接可能已失效，必跑）；
   - **只改源码**（`src/` 等原文件增量改），禁止直接改产物、禁止另起新站；
   - 改完 `npm run build` → `publish_site(title=...,
     src_dir='/workspace/.site-dist/<项目文件夹名>',
     source_dir='/workspace/myspace/<uid>/<项目文件夹名>')`。
3. **没有 `package.json` = 老静态站**：在原文件上增量改，改完直接
   `publish_site(title='站点名称')`（后端按会话绑定项目自动定位，URL 不变、版本 +1）。
   返回里的 `packed_dir` 用于确认打包目录正确。
4. 编辑迭代**不做设计三选一**。

> 会话刚开始 read/glob 报"沙盒连接失败"多半是沙箱冷启动——等几秒**重试同一路径**，
> 不要另起炉灶重建站点。

## 沙箱生命周期规则（React 工程务必遵守）

沙箱空闲一段时间会被回收销毁，只有**项目文件夹**（`/myspace/...`）里的文件永久幸存：
- **源码只能放项目文件夹或尽快通过发布进入项目**；`/workspace/` 其余区域随沙箱销毁丢失。
- `node_modules` / 构建产物 / 缓存**永远不进项目文件夹**（工程里的 `node_modules`
  是指向 `/workspace` 临时区的符号链接，不要删除或替换成真实目录）。
- 重开会话后发现依赖目录消失/链接失效 → **重跑 init 脚本**即自愈（秒级）。
- 发布或 init 过程中 `package.json` / `package-lock.json` 落入项目文件夹时可能弹一次
  「同步到我的空间」确认，属正常，等用户确认即可。

## 约束与要点

- 规模上限：≤300 文件、总量 ≤30MB、单文件 ≤10MB（React 产物通常几 MB，余量充足）。
- 公开站点在浏览器里以**沙箱模式**运行（无 cookie / localStorage）——不要写依赖
  登录态或浏览器本地存储的逻辑，持久化用下面的轻后端 API。
- 可见性 `visibility`：`public`（默认，凭链接访问）/ `private`（仅本人登录可见）/
  `team`（指定团队成员可见，配合 `team_id`）。

## 站点内置轻后端 API（可选，需要动态能力时用）

站内 JS 用**相对路径**（不带前导 `/`）fetch，平台已配好 CORS：

- **KV 存储**（计数器 / 分数 / 简单配置）：
  - 读：`GET __api/kv/<key>` → `{value, exists}`
  - 写：`PUT __api/kv/<key>`，body `{"value":"..."}`（≤4KB，≤200 键）
- **表单收集**（留言 / 报名 / 反馈，站主可在站点管理里导出 CSV）：
  - `POST __api/forms/<form_key>`，body 为扁平 JSON 对象（≤8KB）

> `__api/` 是保留前缀，站点文件不能用这个目录名。React 工程用
> `src/lib/siteApi.js` 封装，静态站直接 `fetch('__api/kv/score')`。

## 禁止事项清单

- 禁止在工程目录（尤其 `/myspace/` 下）直接 `npm install`——加依赖走"改
  package.json + 重跑 init 脚本"。
- 禁止把源码目录当站点发布（路径 B 的 `src_dir` 必须指向构建产物目录）。
- 禁止改 `base: './'`、换 BrowserRouter、引外部 CDN。
- 禁止构建报错未修就发布；禁止编辑会话里绕开原文件另建新站。
- 禁止把设计 mockup 截图 pin 给用户、或对同一问题反复调 choose_design。

## 交付话术

把返回的 `url` 以 **markdown 链接**发给用户，并告知：可在「实验室 → 站点」里管理
（改可见性 / 版本回滚 / 看访问量 / 导出表单数据），点站点卡片上的「编辑」按钮可随时
回来通过对话继续修改。

## 示例

用户："做一个部门数据看板网站，能看各科室的月度指标，要好看。"
你（路径 B）：init 模板 → 问一句"指标数据我先用示例数据占位，之后您可以发我真实数据"
→ 写 3 个 mockup（深色科技风 / 明亮商务风 / 极简卡片风）截图 → `choose_design` 等用户
选 → 按选中方案实现（antd 布局 + echarts 图表 + siteApi 存配置）→ `npm run build` →
`publish_site(title='部门数据看板', src_dir='/workspace/.site-dist/dashboard',
source_dir='/workspace/site-src/dashboard')` → 交付链接 + 管理/编辑指引。
