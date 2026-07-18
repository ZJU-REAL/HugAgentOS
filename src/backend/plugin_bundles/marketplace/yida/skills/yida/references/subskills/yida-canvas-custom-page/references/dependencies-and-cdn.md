# Code Canvas 依赖白名单与 CDN 加载

本文件承载 Code Canvas 的依赖白名单、windowAlias 映射、编译改写规则，以及「预发正常、线上 `antd is not defined`」的根因与物料侧修复方向。核实自 `vc-deep-yida/src/components/yida-code-canvas` 源码（`dependencies.ts` / `factory.tsx`）。

## 依赖白名单（核实自 `yida-code-canvas/dependencies.ts`）

编译阶段把 `import` 改写为下列白名单的 `windowAlias` 引用，运行时按 `windowAlias` 加载到 `window` 上。带 `${cdn}` 的资源在预发走 `dev.g.alicdn.com`、线上走 `g.alicdn.com`（由 `isProd` 决定，见下方「预发/线上差异」）。

| 包名 | windowAlias | 资源 |
| --- | --- | --- |
| react | `React` | g.alicdn.com react 18.3.1 |
| react-dom | `ReactDOM` | g.alicdn.com react-dom 18.3.1 |
| antd | `antd` | g.alicdn.com antd **5.23.3** `antd-with-locales.js`（**依赖外部 `window.dayjs`**）|
| @ant-design/icons | `icons` | g.alicdn.com ant-design-icons 5.5.1 |
| dayjs | `dayjs` | 仅 `assetUrlPlaceholder`（dev CDN 的 locale 文件），**无 `assetUrl`** → 画布自身不会加载 dayjs core，依赖宿主提供 |
| ahooks | `ahooks` | `${cdn}/platform/yida-assets/ahooks.js`（默认追加） |
| d3 | `d3` | g.alicdn.com d3 7.9.0 |
| recharts | `Recharts` | g.alicdn.com recharts 2.15.0 |
| @radix-ui/themes | `Radix` | `${cdn}/.../radix.js` + `radix.css` |
| lucide-react | `DynamicIcon` | `${cdn}/.../lucideReact.js` |
| framer-motion | `FramerMotion` | `${cdn}/.../framerMotion.js` |
| yida-plugin-markdown | `YidaMarkdown` | moduleFederation 0.0.4 |

新增依赖必须同时满足：① 编译能把 import 抽进 `importedModules` 并映射到 windowAlias（见 `canvas-compile.js` 的 `MODULE_ALIAS_MAP`）；② 上表或平台运行时能把依赖加载到 window；③ `runtimeCode` 引用的变量名与 windowAlias 一致；④ CSS 资源可加载，否则组件可能渲染但样式/弹层异常。白名单外的包（yida-utils、`@ali/deep`、原生字段组件等）不能 `import`。

编译位置：OpenYida CLI **本地用 Babel** 把源码转译为 `runtimeCode` + `importedModules`（`import`→`window.<别名>`、`export default`→`YidaComp`、依赖名正则抽取），不调用任何在线编译服务，因此不依赖登录态、不经过风控。别名映射逐条镜像自 `dependencies.ts` 的 `getModuleAliasMap()`；运行时消费契约见 `factory.tsx`（`new Function` 执行 `runtimeCode` 取 `YidaComp`）。

## 预发正常、线上报 `antd is not defined` 的根因

现象：预发（`pre-*.alibaba-inc.com`）页面正常；线上（如 `*.aliwork.com`）白屏，控制台报 `antd is not defined` + `antd-with-locales.js` / `dayjs.js` 抛 `Cannot read properties of undefined (reading 'extend')`。

根因（核实自源码）：

- `antd-with-locales.js`（UMD）初始化时会执行 `dayjs.extend(...)`，其中 `dayjs` 是**外部依赖**，解析为 `window.dayjs`。若加载 antd 前 `window.dayjs` 未就绪 → `undefined.extend` 抛错 → antd 模块未定义 → 业务代码引用 `antd` 报 `antd is not defined`。
- 正确顺序见 `vc-procode-comp/view.js`：先 `window.dayjs = window.__YCCommon?.Common?.dayjs` 再注入 antd。**但 `yida-code-canvas/factory.tsx` 原实现没有这步**：它 `loadDependencies` 时对 antd 只 `push(assetUrl)`，而 dayjs 条目只有 `assetUrlPlaceholder`、无 `assetUrl`，画布自身根本不会加载 dayjs；且加载前 `if (window[windowAlias]) return` 会跳过已存在依赖。
- 于是环境差异被放大：
  - **预发**：宿主设计器 / 同页其它 procode 组件已把 `window.antd`（含 dayjs）挂到全局 → 画布命中 `if (window.antd) return`，直接复用宿主 antd，正常。
  - **线上该租户域**：宿主未预置 `window.antd` / `window.dayjs` → 画布现加载 `antd-with-locales.js`，但没有先设 `window.dayjs` → antd 初始化即抛错。（浏览器 debugger 里可见 `finalDependencies` 含 `antd` 却不含 `dayjs`，`window.dayjs` 为 `undefined`，即命中此缺陷。）
- 附带因素：`isProd = location.hostname.indexOf('pre-') === -1` 是脆弱的域名启发式，非 `pre-` 前缀域一律判为线上，走 `g.alicdn.com` 前缀；antd 本身两端都硬编码 `g.alicdn.com`，故该差异不是 antd 主因，但会影响 `${cdn}` 类依赖（ahooks/radix/lucide/framer-motion/tailwind）的可达性。

## 物料侧修复方向

这是**物料层缺陷**（`factory.tsx` 未在 antd 前保证 `window.dayjs`），页面作者难以在 Canvas 源码内自救（`import dayjs` 也因 dayjs 无 `assetUrl` 不落地）。正确修复在 `factory.tsx` 的 `loadDependencies`：当 `finalDependencies` 含 `antd` 且 `window.dayjs` 未就绪时，**先**复用宿主 `window.__YCCommon?.Common?.dayjs`，否则**单独 `await` 加载 dayjs core**（如 `g.alicdn.com/code/lib/dayjs/<ver>/dayjs.min.js`），确保早于 antd 脚本执行，再进入原有 `assetsUrls` 批量加载。要点：dayjs 必须在**独立且被 await 的加载步骤**里先就绪，避免与 antd 放同一批并行加载产生竞态。

临时规避（未改物料时）：让宿主页先行加载 antd/dayjs，或页面改用不依赖 dayjs 的轻组件（避开 DatePicker、时间 locale 等触发 `dayjs.extend` 的能力）。
