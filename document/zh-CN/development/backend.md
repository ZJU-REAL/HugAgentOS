# 后端开发指南
> 最后更新：2026-06-11

后端为 FastAPI + SQLAlchemy + AgentScope 2.0，源码在 `src/backend/`。本文覆盖开发模式、常用命令、分层规范、路由注册、MCP 工具与技能的新增步骤。整体架构请先读 [架构总览](../architecture/overview.md) 与 [后端架构](../architecture/backend.md)。

## 开发模式：所有服务跑在 Docker 内

项目没有常驻的本地 dev server——**改了后端代码必须重建镜像并重启容器才生效**：

```bash
# 改后端
docker-compose up -d --build backend

# 前后端都改了
docker-compose up -d --build backend frontend

# 依赖 / Dockerfile 变更：强制干净重建
docker-compose build --no-cache backend
docker-compose up -d backend

# 看日志
docker-compose logs -f backend
```

> 新代码「没生效」的 90% 原因是命中了 cached layer——确认容器内 `/app/src/backend/...` 文件已更新，必要时 `--no-cache` 重建。
>
> Makefile 提供 `make dev`（本地 uvicorn `--reload`，端口 3001）用于快速起裸 API 调试，但完整链路（nginx / mcp / 数据库 / 沙箱）依赖 compose 栈，验证以 Docker 为准。

## Make 目标速查

以下目标以仓库根 `Makefile` 为准：

| 目标 | 命令实质 |
|---|---|
| `make test` | `PYTHONPATH=src/backend pytest src/backend/tests/ -v --cov=src/backend`（含覆盖率报告） |
| `make selftest` | 同上但 `-x -q`（fail fast） |
| `make format` | `black . --line-length=100` + `isort . --profile black` |
| `make lint` | 同上的 `--check` 模式（只检查不改写） |
| `make type-check` | `mypy src/backend --ignore-missing-imports` |
| `make security-scan` | bandit + safety |
| `make migrate` | `alembic upgrade head` |
| `make migrate-new msg="..."` | `alembic revision --autogenerate -m "..."` |
| `make migrate-down` / `migrate-history` | 回滚一步 / 查看历史 |
| `make db-reset` / `db-seed` | 重置（破坏性）/ 种子数据 |
| `make build` / `up` / `down` / `logs` / `ps` 等 | docker-compose 包装 |

> ⚠️ 已提交代码并非 formatter-clean：**不要对未改动的文件整体跑 black/isort**（会把真实 diff 淹没在格式化噪音里），只格式化自己改的范围。

### 跑单个测试文件

```bash
PYTHONPATH=src/backend pytest src/backend/tests/test_foo.py -v
PYTHONPATH=src/backend pytest src/backend/tests/api/test_bar.py::test_case -v
```

测试可在 backend 容器内跑，也可在配好 `PYTHONPATH` 的本地 venv 跑。测试文件命名 `test_*.py`，放在 `src/backend/tests/` 对应子目录。

## Alembic 迁移流程

1. 在 `src/backend/core/db/models.py` 改 ORM 模型；
2. `make migrate-new msg="add xxx table"` 自动生成迁移（**人工审阅生成结果**，autogenerate 不可全信）；
3. `make migrate`（或容器内 `alembic upgrade head`）应用；
4. 若是 **EE 专属表**：把表名加进 `core/db/edition_tables.py::EE_ONLY_TABLES`（CE 不建 EE 空表，集合内表名有存在性断言）。

注意：主仓 alembic 链是 EE 链；CE 派生树用独立的 `ce_0001` 基线链（见 [CE 构建管线](../editions/build-ce.md#ce-数据库差异)），主仓新增迁移不会进入 CE。

## 代码规范要点

### 分层架构

```
api/routes/v1/*.py   路由层：参数校验、依赖注入、ORM→dict 转换、信封包装
core/services/*.py   服务层：业务逻辑、权限校验
core/db/repository.py 仓库层：CRUD、软删除过滤（deleted_at IS NULL）
core/db/models.py    ORM 模型
```

**禁止跨层调用**（路由直接摸 ORM 查询属于违例）。Service 构造函数接收 `Session`，内部创建 Repository。

### 统一响应信封

所有 v1 端点返回 `{ code, message, data, trace_id, timestamp }`，必须用 `core/infra/responses.py` 的工具函数：

```python
from core.infra.responses import success_response, created_response, paginated_response

return success_response(data={"id": item.id})
return created_response(data=_item_to_dict(item))          # POST 创建配 201
return paginated_response(items=[...], page=page, page_size=page_size, total_items=total)
```

### 错误处理

**不要在路由里手搓错误响应 / HTTPException**——统一抛 `core/infra/exceptions.py` 的异常，由全局 error_handler 渲染信封：

```python
from core.infra.exceptions import BadRequestError, ResourceNotFoundError

raise ResourceNotFoundError("chat_session", chat_id)
raise BadRequestError("参数 name 不能为空")
```

license 相关 402 同理：唯一来源是 `core/licensing/features.py` 的 `FeatureNotLicensed`（40201）/ `SeatLimitExceeded`（40202），路由 / 服务层不要再手搓 402。错误码分段见 [错误码参考](../api/error-codes.md)。

### 依赖注入

```python
from api.deps import get_current_user, get_db, require_admin, require_config

user: UserContext = Depends(get_current_user)   # 普通端点
_: None = Depends(require_admin)                # /admin 内容台端点（ADMIN_TOKEN）
_: None = Depends(require_config)               # /config 系统台端点（CONFIG_TOKEN）
db: Session = Depends(get_db)
```

## 新路由如何注册（CE/EE 注册表）

路由**不再**在 `api/app.py` 逐行 `include_router`。唯一真源是 `src/backend/api/routes/v1/__init__.py` 的两张注册表，`app.py` 按表循环注册（先 CE 后 EE）：

```python
# api/routes/v1/__init__.py
CE_ROUTERS: tuple[tuple[str, str], ...] = (
    ("chats", "router"),
    ...
    ("meta", "router"),
)

EE_ROUTERS: tuple[tuple[str, str, str | None], ...] = (
    ("audit", "router", "audit"),              # 第三列 = license 能力位
    ("admin_skills", "router", "content_admin"),
    ("config_verify", "router", None),         # None = 显式豁免 feature 守卫
    ...
)
```

新增路由步骤：

1. 在 `api/routes/v1/` 新建路由文件，`router = APIRouter(prefix="/v1/xxx", tags=["Xxx"])`；
2. 判断归属：
   - **CE 能力**（个人自洽）→ 在 `CE_ROUTERS` 追加 `("模块名", "router")`；
   - **EE 能力**（组织规模化）→ 在 `EE_ROUTERS` 追加 `("模块名", "router", "<feature>")`，feature 取 `core/licensing/features.py::Feature` 的值；仅当端点在 license 失效时也必须可达（登录、换 license 类基础设施）才用 `None` 豁免，并写明理由；
3. EE 路由还要在 `ce/manifest.yaml` 的 `exclude` 中排除该文件（`admin_*.py` / `config_*.py` 已有通配模式覆盖）；
4. 注意表内顺序即注册顺序，同前缀族的先后关系不可变（例：`config` 公开读必须先于 `config_*` 管理台）。

`iter_edition_routers` 对物理缺失的模块（CE 树）静默跳过，因此该文件原样进 CE 树、无需 overlay 副本。EE 项的能力位会被 `app.py` 转成 `requires_feature(Feature(...))` 路由级依赖，未授权返回 402（详见 [License 机制](../editions/license.md)）。

## 新增 MCP server 步骤

每个 MCP 工具是 `mcp` 容器内一个长驻 streamable-http 进程，backend 经 `HttpStatefulClient` 连接：

1. 新建 `src/backend/mcp_servers/<name>_mcp/`（参考 `internet_search_mcp/`：`server.py` + `_selftest.py`）；
2. 在 `src/backend/mcp_servers/_ports.py::PORTS` 分配端口（端口分配是稳定契约，不要复用 / 重排）；`core/config/mcp_config.py::MCP_SERVERS` 会自动据此生成连接配置；
3. 在 `src/backend/core/config/display_names.py` 补 server / tool 展示名与描述；
4. 在 `src/backend/core/config/catalog.json` 的 `mcp` 数组加种子条目（`id` / `kind: "mcp_server"` / `name` / `desc` / `enabled` / `config.server`）——catalog 是能力开关的单一真源；
5. 本地调试：

```bash
PYTHONPATH=src/backend python -m mcp_servers.<name>_mcp.server
PYTHONPATH=src/backend python -m mcp_servers.<name>_mcp._selftest
```

6. 重建 mcp 容器（`docker-compose up -d --build mcp`，依 compose 服务名为准）；
7. 若工具属 **EE 行业工具**：`ce/manifest.yaml` exclude 整个目录 + `prunes.catalog_json.drop_mcp_ids` 加 id，CE overlay 的 `_ports.py` 中该端口标注 reserved。

## 新增技能（Agent Skill）步骤

技能加载是多源架构（`core/agent_skills/config.py`）：内置（`skill_bundles/default/`，always-on）、admin（DB / `/app/storage/admin_skills/`）、用户、项目四个加载源按优先级合并；`skill_bundles/marketplace/` 是安装制技能市场种子，由 marketplace 服务单独扫描，**不在默认加载源内**。

新增**内置技能**：

1. 新建 `src/backend/skill_bundles/default/<skill-id>/SKILL.md`（id 规则：小写字母 / 数字 / `-_`，≤63 字符）；
2. SKILL.md 写 frontmatter（`name` / `description` / 可选 `version` / `tags` / `allowed_tools`）+ 正文指令；
3. 可选附 `scripts/`（可执行脚本，`.py/.js/.sh/.r` 无 `_scripts.json` 时自动检出白名单）、`references/`、`evals/`；
4. 重建 backend 容器生效。

新增**市场技能**放 `skill_bundles/marketplace/<skill-id>/`，结构相同，另需市场元数据（参考现有条目）；含品牌 / 行业依赖的技能要同步进 `ce/manifest.yaml` 排除清单。运行时技能目录在沙盒内呈现为 `/workspace/skills/<id>`，提示词中的 `{dir}` 占位符以该路径展示。

## 新功能检查清单

- [ ] ORM 模型 + 索引 + 时间戳（`core/db/models.py`），EE 表已加 `EE_ONLY_TABLES`
- [ ] Repository 过滤软删除；Service 承载业务与权限
- [ ] 路由用信封响应、异常走 `core/infra/exceptions`
- [ ] 路由已登记 `CE_ROUTERS` / `EE_ROUTERS`（EE 配能力位 + manifest 排除）
- [ ] alembic 迁移已生成并人工审阅
- [ ] 测试已写，`make selftest` 通过
- [ ] 只对改动范围 format / lint

## 相关源码

| 主题 | 路径 |
|---|---|
| FastAPI 入口 / 注册循环 | `src/backend/api/app.py` |
| 路由注册表 | `src/backend/api/routes/v1/__init__.py` |
| 响应信封 | `src/backend/core/infra/responses.py` |
| 异常体系 | `src/backend/core/infra/exceptions.py` |
| 依赖注入 | `src/backend/api/deps.py` |
| ORM / 建表边界 | `src/backend/core/db/models.py`、`src/backend/core/db/edition_tables.py` |
| MCP 端口表 / 连接配置 | `src/backend/mcp_servers/_ports.py`、`src/backend/core/config/mcp_config.py` |
| 能力目录 | `src/backend/core/config/catalog.json` |
| 技能加载 | `src/backend/core/agent_skills/`（`config.py` / `loader.py` / `registry.py`） |
| 工作流编排 | `src/backend/orchestration/workflow.py` |

相关阅读：[前端开发指南](frontend.md) · [API 总览](../api/overview.md) · [MCP 工具](../modules/mcp-tools.md) · [技能系统](../modules/agent-skills.md)
