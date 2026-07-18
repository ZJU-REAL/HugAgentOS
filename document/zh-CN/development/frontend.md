# 前端开发指南
> 最后更新：2026-06-11

前端为 React 18 + TypeScript + Ant Design + Zustand（Vite 构建），源码在 `src/frontend/src/`。本文覆盖构建流程、目录与命名规范、API 调用、新增组件 / Store 的步骤，以及 CE/EE edition 门控的用法。整体架构见 [前端架构](../architecture/frontend.md)。

## 构建与热替换流程

所有服务跑在 Docker 内，前端由 nginx 容器托管静态产物。改完代码有两条路：

**方案 A — 完整重建**（慢但永远正确）：

```bash
docker-compose up -d --build frontend
```

**方案 B — 本地构建 + 热替换**（快，需 Node 20+）：

```bash
cd src/frontend
npm run build
docker cp dist/. hugagent-frontend:/usr/share/nginx/html/
docker exec hugagent-frontend nginx -s reload
```

Lint：

```bash
cd src/frontend && npm run lint
```

## 目录与命名规范

```
src/frontend/src/
├── App.tsx              # 主应用壳
├── AdminApp.tsx         # /admin 内容管理台（EE）
├── ConfigApp.tsx        # /config 系统管理台（EE）
├── main.tsx             # 入口路由（CE 派生树用 overlay 版本，不挂 admin/config）
├── api.ts               # 类型化 API 客户端（信封解包）
├── types.ts             # 共享 TypeScript 类型
├── storage.ts           # localStorage 持久化
├── components/          # 22 个功能组件组（每组一个目录 + index.ts barrel export）
│   ├── chat/ admin/ agent/ automation/ batch/ canvas/ catalog/
│   ├── citation/ common/ config/ docs/ file/ kb/
│   └── lab/ memory/ myspace/ projects/ settings/ share/ sidebar/ tool/ apidoc/
├── hooks/               # useChatInit / useChatActions / useStreaming / usePlanMode
│                        # usePageConfig / useDelayedFlag / useStallDetector
├── stores/              # 18 个 Zustand store（authStore、chatStore、catalogStore、
│                        # editionStore、uiStore、settingsStore、fileStore、kbStore、
│                        # agentStore、automationStore、batchStore、canvasStore、
│                        # mySpaceStore、projectStore、skillDistillStore、…）+ index.ts
├── utils/               # citations / markdown / segments / constants / apiError / adminApi …
└── styles/              # 按功能域拆分的 CSS（variables.css 为变量单一真源）
```

命名约定：

- **组件**：PascalCase 文件名，一文件一组件，**named export**（`export function Xxx()`），Props 接口以 `Props` 结尾；新组件必须在所属组的 `index.ts` 中导出。
- **Store**：`xxxStore.ts`，`export const useXxxStore = create<XxxState>(...)`，状态与 actions 定义在同一 interface；在 `stores/index.ts` 统一 re-export。
- **Hook**：`useXxx.ts` 放 `hooks/`，在 `hooks/index.ts` barrel export。
- **样式**：自定义类名一律 `.jx-` 前缀（BEM：子元素 `-`、修饰符 `--`）；颜色 / 圆角 / 间距必须用 `styles/variables.css` 的 CSS 变量，禁止硬编码色值。
- **类型**：跨组件共享的类型进 `types.ts`；组件私有 Props 留在组件文件内；避免 `any`。

## API 调用规范

所有调用走 `api.ts`，路径为 `getApiUrl()`（`VITE_API_BASE_URL`，默认 `/api`）+ `/v1/...`。后端返回统一信封，`api.ts` 内置解包：

```ts
interface ApiEnvelope<T> {
  code: number;
  message: string;
  data: T;
  trace_id?: string;
  timestamp?: number;
}

// api.ts 内部：isApiEnvelope() 判型 + unwrapData() 取 data
```

新增一个 API 调用的步骤：

1. 在 `types.ts`（共享）或 `api.ts`（仅 API 层使用）定义请求 / 响应类型；
2. 在 `api.ts` 加导出函数，内部走统一的请求封装（带认证、401 触发登录、信封解包），返回解包后的 `data`，参考既有函数（如 `getEditionInfo()`）；
3. 组件 / store 只调 `api.ts` 函数，**不要**散落裸 `fetch`；
4. 管理台（/admin、/config）走 `utils/adminApi.ts` 的 `adminFetch` / `configFetch`（token 由壳组件传入）。

错误处理统一经 `utils/apiError.ts`：`readErrorMessage()` 解析信封 / detail；HTTP 402（license 未授权）以 `LicenseError` **类型**识别（不是文案子串），并由 `licenseErrorMessage()` 附加「联系管理员激活 license」引导。

## 新增组件组 / Store 的步骤

**新组件组**：

1. `mkdir src/frontend/src/components/<group>/`，写组件 + `index.ts` barrel；
2. 样式放 `styles/<group>.css`（`.jx-` 前缀 + CSS 变量），并在 `styles/index.ts` 引入；
3. 在 `components/index.ts` re-export（如该文件聚合）。

**新 Store**：

1. 新建 `stores/xxxStore.ts`：先定义 `interface XxxState`（状态 + actions），再 `create<XxxState>((set, get) => ({...}))`；
2. 在 `stores/index.ts` 加 export；
3. 异步 action 内部 try/catch + `set({ loading })`；需要持久化的状态在 setter 中写 localStorage（键名 `hugagent_ui_*` 风格，见 `storage.ts`）；
4. 组件内订阅用 hook 形式（`useXxxStore((s) => s.field)` 选择器优先），事件回调里读最新值用 `useXxxStore.getState()`。

## Edition 门控（CE/EE）

前端按部署版本与 license 能力位隐藏 EE 区块。机制（`stores/editionStore.ts`）：

- App 启动时调一次 `fetchEdition()`（`App.tsx`），拉取无鉴权探针 `GET /v1/meta/edition`，得到 `edition`（ce/ee）、`mode`（license 状态机）、`features`（能力位布尔表）；
- **探针返回前乐观放行**（features 视为全 true）——EE 部署下 UI 不闪隐；探针失败也静默维持全量 UI，后端 402 门控兜底；
- 返回后按能力位收口：CE 全 false，EE 按 license entitlement。

组件中取能力位用**响应式选择器**（带 `loaded` 判断的标准写法）：

```ts
import { useEditionStore } from '../../stores';

// 团队/多租户能力位：未加载时乐观 true，加载后按实际值
const multiTenancy = useEditionStore((s) => (s.loaded ? !!s.features.multi_tenancy : true));

// 用法：CE / 未授权 license 下隐藏 EE 区块
{multiTenancy && <TeamsSection />}
```

仓内现成范例：

- `components/settings/SettingsModal.tsx`——`multi_tenancy=false` 时把「团队」分区从设置导航里整段过滤；
- `components/myspace/MySpacePanel.tsx`——隐藏团队文件夹页签并回退个人空间。

注意事项：

- 前端隐藏**只是体验优化**，权限边界由后端 `requires_feature` 守卫（402）兜底——不要只做前端隐藏就当作安全措施；
- 新增 EE 界面区块时：① 用上面的选择器按对应能力位门控；② 若整个页面属 EE（如 /admin、/config），无需 runtime 门控——CE 派生树直接物理排除组件目录并用 overlay 的 `main.tsx` 摘掉路由（见 [CE 构建管线](../editions/build-ce.md)）；
- 处理接口报错时记得识别 `LicenseError`（402），给出「联系管理员更新 license」而非笼统失败提示。

## 新功能检查清单

- [ ] 组件 PascalCase + named export + Props 接口
- [ ] 全局状态走 Zustand，store 已在 `stores/index.ts` 导出
- [ ] API 调用经 `api.ts` / `adminApi.ts`，类型完整无 `any`
- [ ] 类名 `.jx-` 前缀，颜色 / 间距 / 圆角用 CSS 变量
- [ ] EE 区块已按能力位门控（或确认属物理排除范围）
- [ ] 402 错误走 `LicenseError` 分支
- [ ] `npm run lint` 通过，已重建或热替换验证

## 相关源码

| 主题 | 路径 |
|---|---|
| API 客户端 / 信封解包 | `src/frontend/src/api.ts` |
| 共享类型 | `src/frontend/src/types.ts` |
| 错误 / 402 识别 | `src/frontend/src/utils/apiError.ts` |
| 管理台请求 | `src/frontend/src/utils/adminApi.ts` |
| edition 门控 store | `src/frontend/src/stores/editionStore.ts` |
| 门控用法范例 | `src/frontend/src/components/settings/SettingsModal.tsx`、`src/frontend/src/components/myspace/MySpacePanel.tsx` |
| CSS 变量 | `src/frontend/src/styles/variables.css` |
| 入口（CE 差异点） | `src/frontend/src/main.tsx`、`ce/overlay/src/frontend/src/main.tsx` |

相关阅读：[后端开发指南](backend.md) · [前端架构](../architecture/frontend.md) · [License 机制](../editions/license.md)
