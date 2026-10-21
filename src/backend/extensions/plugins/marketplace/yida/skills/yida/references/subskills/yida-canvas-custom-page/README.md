
# 宜搭 Code Canvas 自定义页面开发

## 核心定位

Code Canvas 是宜搭的**代码画布自定义页面链路**：以 `YidaCodeCanvas` 物料为承载，用户写标准 React18 函数组件源码，编译为 `runtimeCode` + 依赖清单（OpenYida CLI 本地用 Babel 完成），运行时按依赖白名单加载资源并 `new Function` 执行，渲染出真正的 React 组件。相较宜搭原生 `.oyd.jsx` 页面，它提供现代 hooks 写法、组件级崩溃隔离（ErrorBoundary）、Tailwind 运行时样式，适合现代交互 / 可视化 / AI 生成页面。

> **可用性边界**：Code Canvas 组件在宜搭平台侧尚未全量。完整应用 `fast_build` 和未确认 Canvas 能力的组织默认使用 `yida-custom-page` native 链路；本技能只在用户明确要求、已有页面是 `YidaCodeCanvas`，或已确认当前组织/页面支持 Canvas 时使用。

运行链路（源码核实自 `vc-deep-yida` 物料）：

1. 编译阶段把用户 React 源码转译为 `runtimeCode` + 依赖清单 `importedModules`（OpenYida CLI **本地用 Babel 完成**：JSX/TS → ES5、`import` 改写成 `window.<别名>` 引用、`export default` 收敛为 `YidaComp`，依赖名正则抽取）。
2. `YidaCodeCanvas` 运行时按依赖别名表把 `importedModules` 命中的白名单依赖加载 CDN 资源到 `window[windowAlias]`。
3. `runtimeCode` 在宿主页真实 `window` 中 `new Function` 执行，返回 `YidaComp` / `YidaComp.default` / 组件函数并渲染。

本技能处理从零编写 Code Canvas 页面。宜搭原生 `.oyd.jsx` 页面的 `export function renderJsx()`、`_customState`、`this.utils.yida.*`、`openyida publish` 规范由 `yida-custom-page` 负责；把已有原生页面迁移到 Code Canvas 链路由 `yida-canvas-upgrade` 负责。

## 先判断能不能用 Code Canvas

| 证据 | 判断 |
| --- | --- |
| 用户明确要求 Code Canvas / 代码画布 / runtimeCode / importedModules | 使用本技能 |
| 已确认当前组织/页面支持 `YidaCodeCanvas`，且需要现代 React 交互 / hooks 状态 / 可视化 / AI 生成 / 崩溃隔离 | 使用本技能 |
| 页面 Schema 或组件树里有 `componentName: "YidaCodeCanvas"` | 使用本技能 |
| 需求明确是“从 OpenYida 原链路升级/迁移到 Canvas” | 切到 `yida-canvas-upgrade` |
| 未确认 Canvas 能力，或只是完整应用 `fast_build` 的主入口页 | 使用 `yida-custom-page` |
| 只需通过开放 API（HTTP）读写宜搭数据，但未确认 Canvas 能力 | 使用 `yida-custom-page`；不要为了 HTTP 数据读取默认切 Canvas |
| 强依赖原生实例数据桥：表单内字段双向绑定 `this.$(fieldId)`、`this.utils.yida.*`、`dataSourceMap`、提交流程深度耦合 | 使用 `yida-custom-page`（该实例桥仅 native 免费提供） |
| 需要字段结构、公式、联动、权限、报表、流程 | 切到对应配置型技能，不用 Code Canvas 承载 |

OpenYida CLI 已支持发布 `YidaCodeCanvas` 页面：把 Code Canvas 源码写成 `.canvas.jsx`（推荐 `project/pages/src/<页面名>.canvas.jsx`），执行 `openyida publish <源文件> <appType> <formUuid>` 即自动走 Canvas 链路——`.canvas.jsx` 扩展名会被识别，CLI **本地用 Babel 编译**出 `runtimeCode` + `importedModules`（无需在线编译服务、无需登录态即可完成编译），构造 `YidaCodeCanvas` 组件的 Schema 并保存。扩展名不规范但确为 Canvas 源码时，加 `--canvas` 显式指定。**不要**把 Canvas 源码用普通 `.oyd.jsx`/`.jsx` 走 native 链路发布（会被 Babel 兼容编译器当 `renderJsx` 处理而失败）。

## 运行时事实（速记）

- `runtimeCode` 在宿主页真实 `window` 中 `new Function` 执行（`fn(window, window)`），执行后必须返回 `YidaComp` / `YidaComp.default` / 组件函数。
- 物料只透传 `code / runtimeCode / importedModules / pageType`，无 `this` 上下文、无 `dataSourceMap` 数据桥；`this.utils.yida.*`、`didMount()` 等普通页契约都不可用。
- 宿主 window 全局不是数据桥：`__yida_plugin_runtime__` 是插件扩展点管理器、`__VcDeepYidaUtils__` 只是 `{ VuMicroUtils }`，也无 `window.Deep` 字段组件。不要指望从 window 直接拿表单数据。
- **读宜搭数据 = 自建 HTTP 桥**：在 `YidaComp` 内 `fetch` 调宜搭开放 API 或已配置连接器代理（同源带 cookie），自行处理 appType / formUuid / 鉴权；这是 Canvas 唯一干净的数据读写路径。
- 依赖走白名单（React、ReactDOM、antd、ahooks、d3、recharts、Radix、lucide-react、framer-motion…）；新增依赖须被 CLI 编译识别（能抽入 `importedModules`）、有 window alias 映射、CSS 可加载。白名单无 yida-utils / `@ali/deep` / 原生字段组件，不能 `import`。
- `EmployeeField` 等宜搭原生字段组件不能因表单支持同名字段就默认可用，必须先做最小验证；缺证据时降级为 antd/自定义控件，只存已知 userId / unionId。

> 📖 运行时事实、原生组件判断、EmployeeField JSX 示例与验收清单 → [references/employeefield-verification.md](references/employeefield-verification.md)
> 📦 依赖白名单、windowAlias / CDN、antd/dayjs `is not defined` 根因与物料侧修复 → [references/dependencies-and-cdn.md](references/dependencies-and-cdn.md)
> 🔌 在 `YidaComp` 内自写 HTTP 桥读写宜搭数据（连接器代理 / 同源 fetch / 可复用 hook）→ [references/data-bridge-guide.md](references/data-bridge-guide.md)
> 🎨 主色跟随 App 品牌：antd `ConfigProvider.colorPrimary` / Tailwind CSS 变量 / 图表配色的落地写法 → [references/canvas-design-system.md](references/canvas-design-system.md)

## 视觉方向：按需定方向

单点页面美化、用户明确要求好看/去 AI 味，或 `yida-app` 进入 `deep_design` 时，再调用 `use_skill("yida-page-uiux", "确定 Code Canvas 页面的视觉方向")`。`yida-app fast_build` 默认不加载 `yida-page-uiux`：直接使用克制的 MVP 工作台/列表/入口布局，禁 emoji、少装饰、主色跟随 App 品牌即可。

Canvas 这套栈（React18 + antd + Tailwind）怎么把「主色跟随 App 品牌」落地由 [references/canvas-design-system.md](references/canvas-design-system.md) 负责。一句话记忆：**CSS 变量 `var(--color-brand1-*)` 对普通 DOM / Tailwind 直接可用（Canvas 跑在真 window、节点在宿主树内会级联）；antd token 和图表颜色是 JS 消费，需用 `getComputedStyle` 把品牌色解析成真实色值再喂进去。** 语义色（成功/警告/错误）保持固定，不随主题变。

## 编码规则

1. **不要写普通页面契约**：不要写 `export function renderJsx()`、`didMount()`、`this.forceUpdate()`、`this.utils.yida.*`，除非已验证 Code Canvas props 中提供等价能力。
2. **入口必须明确**：源码必须导出或返回 `YidaComp`；不要只定义局部组件却不导出。
3. **依赖必须可追踪**：所有 import 都要能出现在编译结果 `dependencies` 中；不在白名单的依赖不要交付。
4. **状态按 React 组件写法处理**：Code Canvas 组件用标准 React hooks 写法（本地 Babel 编译支持）；不要套用普通自定义页 `_customState` 模式。
5. **副作用要清理**：使用 `useEffect` 注册事件、定时器、图表实例时必须返回 cleanup。
6. **不要绕过数据源治理**：调用连接器或远程 API 时，优先通过平台已配置数据源或传入 props；不要在组件中硬编码外部 URL、Cookie、CSRF。
7. **先验收依赖，再扩展业务**：原生组件、上传、组织搜索、弹层类能力必须先做最小验证页，确认可用后再写复杂业务页面。
8. **主色跟随 App 品牌，勿硬编码蓝**：antd 页面在最外层包 `ConfigProvider`，`token.colorPrimary` 用 `getComputedStyle` 读平台 `--color-brand1-6` 解析出的真实色值；Tailwind / 普通 DOM 直接用 `var(--color-brand1-*)`；图表颜色同样读品牌色。语义色（成功/警告/错误）保持固定。禁止散落 `#1677ff` / `bg-blue-500` 等硬编码蓝。详见 [references/canvas-design-system.md](references/canvas-design-system.md)。

> 📖 最小 `YidaComp`、副作用清理、recharts 图表、数据拉取组件等 vetted 脚手架模板 → [references/canvas-authoring-examples.md](references/canvas-authoring-examples.md)

## 开发流程

```bash
# 1. 只读检查环境和登录态；真实创建资源前必须通过
openyida env --json
openyida login --check-only --json

# 2. 如需新页面，先创建空白自定义页拿 formUuid
openyida create-page <appType> "<页面名>"

# 3. 编写 Code Canvas 源码，写成 .canvas.jsx（推荐 project/pages/src/<页面名>.canvas.jsx）
#    只用白名单依赖；入口导出/返回 YidaComp；副作用带 cleanup。

# 4. 本地预检：不要用 openyida check-page/compile 校验 Canvas，它们当前是 native 页面检查器。
#    Canvas 预检以 publish 的 Code Canvas 编译阶段为准；需要离线快检时可调用 lib/app/canvas-compile。
node -e "const fs=require('fs'); const {compileCanvasLocal}=require('./lib/app/canvas-compile'); const src=fs.readFileSync('project/pages/src/<页面名>.canvas.jsx','utf8'); const out=compileCanvasLocal(src); console.log(out.importedModules)"

# 5. 发布：.canvas.jsx 自动走 Canvas 链路，CLI 本地用 Babel 编译出
#    runtimeCode + importedModules，构造 YidaCodeCanvas Schema 保存。
openyida publish project/pages/src/<页面名>.canvas.jsx <appType> <formUuid>
#    扩展名不规范但确为 Canvas 源码时，显式加 --canvas。

# 6. 发布后回读 Schema 验收：确认组件树存在 YidaCodeCanvas，且 runtimeCode 非空
openyida get-schema <appType> <formUuid>
```

如需保存完整 Schema，使用 create_file / Write / file edit tool 创建 `<projectRoot>/.cache/openyida/<页面名或任务名>/<页面名>-schema.json`；从 workspace 根执行后续命令时路径加 `project/` 前缀。不要把 `openyida` stdout 通过 shell 重定向保存成 JSON。

## 与其他技能分工

| 需求 | 使用 |
| --- | --- |
| 普通 `.oyd.jsx` 自定义页 | `yida-custom-page` |
| 发布 Canvas 页（`.canvas.jsx` → `openyida publish` 自动走 Canvas 链路） | 本技能开发流程 step 4 |
| 编译并发布 native `.oyd.jsx`/`.jsx` 页面 | `yida-publish-page` |
| 创建空白自定义页面 | `yida-create-page` |
| 用户明确要求通过设计器数据源调用连接器 | `yida-data-source-connectors`；`fast_build` 不默认加载 |
| 创建或管理 HTTP 连接器 | `yida-connector` |
| 配表单字段结构，如真正的 EmployeeField 字段 | `yida-create-form-page` |

## 常见误区

| 误区 | 正确处理 |
| --- | --- |
| 看到表单支持 `EmployeeField`，就在 Code Canvas 写 `<EmployeeField />` | 先验证依赖映射、CSS、上下文和 `onChange` 结构 |
| 把 Code Canvas 源码用 `.oyd.jsx`/`.jsx` 发布 | native 链路会走 Babel 兼容编译器当 `renderJsx` 处理而失败；Canvas 源码要写成 `.canvas.jsx`（或加 `--canvas`），`openyida publish` 会自动走 Canvas 链路生成 `YidaCodeCanvas` Schema |
| 在 Code Canvas 中照搬 `this.utils.yida.searchFormDatas` | Code Canvas 组件没有普通页面实例契约；先看 props 或数据源注入方式 |
| 依赖加载失败后继续写业务 | 先补依赖 alias/CDN 或降级到已支持组件 |
| 用 `openyida check-page` / `openyida compile` 校验 `.canvas.jsx` | 这两个命令当前面向 native `.oyd.jsx`，会误报 Hook / default export 限制；Canvas 用 `compileCanvasLocal` 离线快检，最终以 `openyida publish .canvas.jsx` 的 Code Canvas 编译阶段为准 |
