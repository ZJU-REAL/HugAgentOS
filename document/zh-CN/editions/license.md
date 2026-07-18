# License 机制（商业版）
> 最后更新：2026-06-11

商业版（EE）采用 **GitLab 式离线 License 模型**：一份经 Ed25519 签名的授权文件（`.lic`）+ 进程内验签，**全程离线、无 license 服务器**，适配政务内网等隔离环境。本文逐项说明状态机、执法机制、签发流程与管理界面，全部内容以 `src/backend/core/licensing/` 代码为准。

## License 文件格式

`.lic` 文件是 JSON 信封（`src/backend/core/licensing/_ee_verify.py`，格式版本 `jx-license/1`）：

```json
{
  "format": "jx-license/1",
  "payload": "<base64(载荷 JSON 字节)>",
  "signature": "<base64(对载荷字节的 Ed25519 签名)>"
}
```

载荷字段：

```json
{
  "license_id": "lic_xxx",
  "customer": "客户名",
  "edition": "ee",
  "features": ["*"],            // "*" = 全功能；或 ["sso", "billing", ...]
  "seats": 0,                   // 0 = 不限席位
  "issued": "2026-06-10",
  "expires": "2027-06-10"
}
```

验签公钥内置于 `_ee_verify.py`（`_BUILTIN_PUBKEY`），可经环境变量 `LICENSE_PUBLIC_KEY` 覆盖（密钥轮换用）。`_ee_verify.py` 是**商业版专属模块，不进社区版派生树**（`ce/manifest.yaml` 显式排除；CE 树的 `manager.py` 被 overlay 替换为恒 False stub）。

## 状态机

`core/licensing/manager.py::LicenseManager.mode()` 返回 7 种状态之一：

| mode | 触发条件 | EE 能力位 |
|---|---|---|
| `ce` | `JX_EDITION=ce`（社区版树） | 全 False |
| `internal` | ee 且未配置 license 文件且 `JX_LICENSE_REQUIRED=false` | **全 True**——内部/全托管部署，兼容存量部署，行为与历史一致 |
| `licensed` | license 验签通过且在有效期内 | 按 entitlement（`features` 清单或 `"*"` 通配）放行 |
| `grace` | 已过期但在宽限期（`LICENSE_GRACE_DAYS`，默认 14 天）内 | 功能保留，探针 / 管理台报警 |
| `expired` | 过宽限期 | 全 False（应用本身可用，组织级能力降级） |
| `invalid` | 配置了 `LICENSE_KEY_PATH` 但文件缺失 / 不可读 / 验签失败 / 格式错误 | 全 False |
| `missing` | `JX_LICENSE_REQUIRED=true` 且无 license 文件 | 全 False |

关键设计点（均可在 `manager.py` 中验证）：

- **`invalid` 与「未配置」严格区分**：配置了路径但文件不可读必须判 `invalid`，绝不能回落 `internal`——否则删掉过期 license 文件即可恢复全功能。
- **有效期分级单一真源**：`classify_verified()` 同时被运行时状态机和上传校验共用，保证「上传能否激活」与「运行时是否放行」永远一致。
- **mtime 缓存**：验签结果按文件 mtime 缓存，文件未变不重复验签；`reload()` 清缓存供热换。

### 相关环境变量

| 变量 | 默认 | 作用 |
|---|---|---|
| `JX_EDITION` | `ee`（主仓） | 版本形态 |
| `LICENSE_KEY_PATH` | 空 | license 文件路径（容器内路径，建议挂持久卷） |
| `JX_LICENSE_REQUIRED` | `false` | **强制模式**：true 时无有效 license 即关闭全部 EE 能力位（私有化交付建议开启）；false 且无 license = internal 全功能 |
| `LICENSE_GRACE_DAYS` | `14` | 到期宽限期（天） |
| `LICENSE_PUBLIC_KEY` | 空（用内置公钥） | 验签公钥覆盖（密钥轮换） |

## 能力位与执法

### Feature 枚举

`core/licensing/features.py::Feature` 只列「组织级」商业能力位（自动化 / 批量 / 个人画布 / L2-L3 记忆属社区版，不在枚举内）：

`sso`、`multi_tenancy`、`audit`、`memory_audit`、`billing`、`quota`、`persistent_sandbox`、`cloud_storage`、`industry_tools`、`content_admin`、`system_config`、`canvas_collab`、`whitelabel`。

### 两道防线

1. **第一道：路由注册表**（CE 树物理不含 EE 路由文件）——见 [CE 构建管线](build-ce.md)。
2. **第二道：`requires_feature` 守卫**（`core/licensing/deps.py`）——防「商业版部署了全量代码、但 license 未购买某能力包」。

EE 路由与能力位的对应关系在注册表 `src/backend/api/routes/v1/__init__.py::EE_ROUTERS` 中声明（表项第三列即能力位），`api/app.py` 注册时按表挂守卫：

| EE 路由模块 | 能力位 |
|---|---|
| `audit`、`admin_chat_history`、`admin_logs` | `audit` |
| `admin_skills`、`admin_kb`、`admin_prompts`、`admin_mcp_servers`、`admin_agents`、`admin_skill_drafts`、`admin_sandbox`、`admin_marketplace` | `content_admin` |
| `admin_usage_logs`、`admin_billing` | `billing` |
| `config_users`、`config_teams`、`config_invites`、`team_files` | `multi_tenancy` |
| `config_security`、`service_configs` | `system_config` |
| `config_verify`、`config_license`、`auth` | **None（显式豁免）** |

三个豁免项是刻意设计：`config_verify` 是控制台登录校验、`config_license` 是换 license 的入口、`auth` 是登录 / 会话基础设施——license 失效时也必须可达，否则用户陷入「402 → 登出 → 登录 → 402」死循环且无从换 license。SSO 能力位不在路由级整体豁免，而是自行守卫：authorize-url 端点挂 `requires_feature(Feature.SSO)`（`api/routes/v1/auth.py`），remote ticket 交换在 `core/auth/sso.py::exchange_ticket` 内检查。

> 注：`quota` / `persistent_sandbox` / `cloud_storage` / `industry_tools` / `canvas_collab` / `whitelabel` / `memory_audit` 当前在 license entitlement 与探针中表达，但**未挂路由级守卫**——这些能力的边界主要由 CE 树物理排除与部署配置兑现。

### 未授权返回 402

未授权时抛 `FeatureNotLicensed`（`features.py`），由全局 error_handler 统一渲染为 HTTP **402** 信封——这是 license 402 的**唯一**信封来源：

```json
{ "code": 40201, "message": "该功能未在当前 license 中授权: xxx", "data": { "feature": "xxx", "mode": "expired" } }
```

选 402 而非 403 是因为前端把 403 当作 token 失效强制登出。席位类拒绝用 `SeatLimitExceeded`（code `40202`，同样 402）。前端 `src/frontend/src/utils/apiError.ts` 以 `LicenseError` 类型识别 402 并附加「请联系管理员在 系统配置 → License 中激活」引导文案。

## 席位上限

席位计数单一真源在 `core/licensing/seats.py`：

- `seats_used(db)`：已占用席位 = `users_shadow` 全量行数（含 SSO 影子账号）；
- `seat_available(db)`：新增用户前的校验（本地注册、SSO 自动建号共用）。CE / internal / 不限席位（`seats=0`）恒放行；`licensed` / `grace` 下要求 `active_users < seats`；`expired` / `invalid` / `missing` 恒拒绝；
- `seat_block_reason(db)`：拒绝文案区分两种根因——真席位不足（`licensed`/`grace`）提示扩容，license 状态异常提示去 License 面板激活。

## 状态查询与热换

### `GET /v1/meta/edition`（无鉴权探针）

`api/routes/v1/meta.py`：返回 `edition` / `mode` / 能力位布尔表。**刻意不返回** license 详情（license_id / 客户名 / 席位 / 到期日）——那些只在鉴权端点暴露。`mode` 保留给登录页提示「license 已过期」等场景。

### `GET/POST /v1/config/license`（CONFIG_TOKEN 鉴权）

`api/routes/v1/config_license.py`：

- `GET`：完整状态（`license_manager.status()` + `seats_used`），含各能力位实时判定、宽限天数、license 元信息；
- `POST`：上传 `.lic` 全文（≤64KB）热换。流程：**先验签再落盘**（无效文件不覆盖现有 license）→ 拒绝激活已过宽限期的 license（grace 期内放行，保证文件丢失 / 主机重建时宽限窗口内能重新挂回）→ 原子写入 `LICENSE_KEY_PATH`（tmp + `os.replace`）→ `license_manager.reload()` 即时生效，**无需重启**。未配置 `LICENSE_KEY_PATH` 时返回 400 提示先配置。

### /config 管理台 License 面板

`src/frontend/src/components/config/LicensePanel.tsx`（挂在 `ConfigApp.tsx`）：展示版本 / 模式（7 种状态各有色彩标签与处置提示）、license 详情（客户、到期日、席位用量 `seats_used/seats`）、13 个能力位的中文清单与开关态；提供「上传 license」弹窗（粘贴 `.lic` 全文）与刷新。

### 前端 edition 门控

`src/frontend/src/stores/editionStore.ts`：App 启动时拉取探针；**探针返回前乐观放行**（features 视为全 true，EE 部署下 UI 不闪隐），返回后按能力位收口。组件用响应式选择器：

```ts
const multiTenancy = useEditionStore((s) => (s.loaded ? !!s.features.multi_tenancy : true));
```

用法示例见 `components/settings/SettingsModal.tsx`（隐藏团队分区）、`components/myspace/MySpacePanel.tsx`（隐藏团队文件夹页签）。前端隐藏只是体验优化，后端 402 守卫始终兜底。

## 签发流程（厂商侧）

`scripts/license_tool.py` 是厂商离线签发工具，**不随任何发行版交付**（CE manifest 显式排除）。四个子命令：

```bash
# 1. 生成 Ed25519 密钥对（一次性；私钥务必离线保管，泄露即可被任意签发）
python scripts/license_tool.py keygen --out-dir ~/jx-license-keys
#    输出公钥值 → 更新到 core/licensing/_ee_verify.py::_BUILTIN_PUBKEY（或客户侧 LICENSE_PUBLIC_KEY）

# 2. 签发
python scripts/license_tool.py issue \
    --key ~/jx-license-keys/license_signing.key \
    --customer "客户名" \
    --expires 2027-06-10 --seats 200 --features "*" \
    --out customer.lic

# 3. 查看载荷（不验签，坏签名的文件也能看内容）
python scripts/license_tool.py inspect customer.lic

# 4. 验签 + 状态
python scripts/license_tool.py verify customer.lic --pub ~/jx-license-keys/license_signing.pub
```

`issue` 自动生成 `license_id`（`lic_` + 16 位 hex）、校验日期格式；`--seats 0` 表示不限席位；`--features` 逗号分隔能力位，`"*"` 为全功能。信封格式与验签逻辑的唯一真源在后端 `_ee_verify.py`，签发工具直接复用（显式传公钥，不拖入后端配置链）。

## 私有化交付清单

1. `.env` 配置 `LICENSE_KEY_PATH=/app/data/license.lic`（挂持久卷）、`JX_LICENSE_REQUIRED=true`；
2. 部署后访问 `/config` → License 面板，粘贴 `.lic` 全文上传激活；
3. 验证：`GET /v1/meta/edition` 的 `mode` 应为 `licensed`；
4. 续期：到期前签发新文件，面板上传热换（宽限期内功能不中断）。

## 相关源码

| 主题 | 路径 |
|---|---|
| 状态机 / 门面 | `src/backend/core/licensing/manager.py` |
| Ed25519 验签（EE 专属） | `src/backend/core/licensing/_ee_verify.py` |
| 能力位枚举 + 402 异常 | `src/backend/core/licensing/features.py` |
| `requires_feature` 守卫 | `src/backend/core/licensing/deps.py` |
| 席位计数 | `src/backend/core/licensing/seats.py` |
| EE 路由 ↔ 能力位注册表 | `src/backend/api/routes/v1/__init__.py` |
| 守卫挂接 | `src/backend/api/app.py`（edition 注册循环） |
| 状态查询 / 热换 | `src/backend/api/routes/v1/config_license.py` |
| 探针 | `src/backend/api/routes/v1/meta.py` |
| 签发工具 | `scripts/license_tool.py` |
| License 面板 | `src/frontend/src/components/config/LicensePanel.tsx` |
| 前端门控 / 402 识别 | `src/frontend/src/stores/editionStore.ts`、`src/frontend/src/utils/apiError.ts` |

相关阅读：[社区版与商业版总览](overview.md) · [CE 构建管线](build-ce.md) · [环境变量参考](../deployment/environment-variables.md)
