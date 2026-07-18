---
name: hugagent-backend-dev
description:
  HugAgentOS 后端开发规范。当需要新增或修改后端代码（API路由、服务层、数据库模型、MCP工具等）时使用此 skill，
  确保代码风格、分层架构、错误处理、响应格式等与项目现有规范保持一致。
---

# HugAgentOS 后端开发规范

本 skill 定义了 HugAgentOS 项目后端（FastAPI + SQLAlchemy + AgentScope 2.0）的开发规范与流程。
所有后端代码变更必须遵守以下规范。

## 文件索引

### 模板 (`templates/`)
| 文件 | 用途 |
|------|------|
| `route.py` | API 路由模板（CRUD 全套） |
| `service.py` | Service 层模板（业务逻辑 + 审计） |
| `repository.py` | Repository 层模板（CRUD + 软删除） |
| `model.py` | ORM 模型模板（字段、索引、约束） |
| `test.py` | 测试模板（fixture + repo/service 测试） |

### 参考文档 (`references/`)
| 文件 | 内容 |
|------|------|
| `architecture.md` | 架构图、分层职责、模块索引、请求流转 |
| `error-codes.md` | 完整错误码表（2xxxx-5xxxx） |
| `api-envelope.md` | API 响应信封格式与工具函数用法 |

### 脚本 (`scripts/`)
| 文件 | 用途 |
|------|------|
| `scaffold_feature.sh` | 一键生成新功能骨架（路由+服务+测试） |

> 模板中 `${Feature}` / `${feature}` / `${table_name}` 为占位符，使用时替换为实际名称。

---

## 1. 目录结构与分层架构

```
src/backend/
├── api/                         # API 层（路由 + 中间件）
│   ├── app.py                   # FastAPI 实例、中间件注册（路由按注册表注册）
│   ├── deps.py                  # 依赖注入（认证、DB session）
│   ├── health.py                # 健康检查端点
│   ├── schemas.py               # 请求/响应 Pydantic 模型
│   ├── middleware/              # CORS、错误处理、日志中间件
│   └── routes/v1/               # 路由文件；__init__.py 是 CE_ROUTERS 注册表（单一真源）
├── core/                        # 核心业务逻辑（15 子模块）
│   ├── agent_skills/            # 技能引擎：加载/注册/选择（SKILL.md 解析、{dir} 沙箱路径注入）
│   ├── artifacts/               # 生成物注册与下载
│   ├── auth/                    # 认证/权限接缝（permissions_iface.py）
│   ├── chat/                    # 聊天上下文、工具日志
│   ├── config/                  # Settings dataclass + catalog 五件套 + mcp_config + runtime_env
│   ├── content/                 # 内容块、文件解析
│   ├── db/                      # engine + models/ 包（11 领域文件）+ repository/ 包
│   ├── infra/                   # 异常、响应、日志、指标、限流、Redis
│   ├── kb/                      # 自建知识库：分块、向量化、混合检索
│   ├── licensing/               # 版本/License：features.py（能力位+402）、manager.py（状态机）
│   ├── llm/                     # Agent 工厂、中间件（middlewares.py）、MCP 池、offloader、tools/ 自研工具
│   ├── memory/                  # 分层记忆（L1 画像 / L2 向量 / L3 图谱，mem0 底座）
│   ├── sandbox/                 # 沙箱 provider：protocol.py + script_runner 等实现
│   ├── services/                # 高级业务服务（30+）
│   └── storage/                 # 存储协议 + 实现（local）
├── orchestration/               # 流式编排：workflow.py、chat_run_executor.py、strategy.py、citations.py、
│                                #   memory_integration.py、batch_orchestrator.py、schedulers/、subagents/
├── prompts/                     # 系统提示词装配（DB 版本池优先，prompt_text/ 文件兜底）
├── mcp_servers/                 # 内置 MCP server（streamable-http 常驻 mcp 容器，端口真源 _ports.py）
├── skill_bundles/               # 技能资产：default/（内置）+ marketplace/（可安装）
├── scripts/                     # 运维脚本（export_content / import_content 等）
├── tests/                       # 测试
└── alembic/                     # 数据库迁移
```

**核心原则：** 路由层(routes) → 服务层(services) → 仓库层(repository) → 数据库(models)，禁止跨层调用。

---

## 2. API 路由规范

### 2.1 路由文件结构

每个路由文件遵循以下模板：

```python
from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field
from typing import Optional, List

from api.deps import get_current_user, get_db
from core.infra.responses import success_response, created_response, paginated_response
from core.infra.exceptions import ResourceNotFoundError, BadRequestError
from core.services.xxx_service import XxxService

# 1. 创建路由器（必须指定 prefix 和 tags）
router = APIRouter(prefix="/v1/xxx", tags=["Xxx"])

# 2. 定义请求/响应模型
class CreateXxxRequest(BaseModel):
    name: str = Field(..., description="名称", max_length=200)
    metadata: Optional[dict] = Field(default_factory=dict)

# 3. ORM → dict 转换辅助函数
def _item_to_dict(item) -> dict:
    return {
        "id": item.id,
        "name": item.name,
        "created_at": item.created_at.isoformat() if item.created_at else None,
    }

# 4. 路由端点
@router.get("", summary="获取列表")
async def list_items(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    service = XxxService(db)
    items, total, total_pages = service.list_items(user.user_id, page, page_size)
    return paginated_response(items=[_item_to_dict(i) for i in items], page=page, ...)

@router.post("", status_code=status.HTTP_201_CREATED, summary="创建")
async def create_item(
    body: CreateXxxRequest,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    service = XxxService(db)
    item = service.create(user.user_id, body.name, body.metadata)
    return created_response(data=_item_to_dict(item))
```

### 2.2 路由规则

- **前缀**: 所有路由使用 `/v1/` 前缀
- **认证**: 端点用 `Depends(get_current_user)` 注入当前用户
- **数据库**: 通过 `Depends(get_db)` 获取 Session
- **响应**: 必须使用 `success_response()` / `created_response()` / `paginated_response()` 包装
- **分页**: 使用 `Query(default, ge=, le=)` 验证分页参数
- **状态码**: POST 创建用 201，DELETE 用 204 或返回 success_response
- **注册**: 新路由文件必须注册进 `api/routes/v1/__init__.py` 的 **CE_ROUTERS**（二元组 `("模块名", "router")`）——这是单一真源，`api/app.py` 按表自动注册，**不要**再手工 `include_router()`

---

## 3. 响应格式规范

所有 v1 端点返回统一信封：

```json
{
  "code": 10000,
  "message": "Success",
  "data": { ... },
  "trace_id": "req_abc123",
  "timestamp": 1710000000000
}
```

### 状态码约定

| 范围 | 含义 | 示例 |
|------|------|------|
| 10000-19999 | 成功 | 10000=成功, 10001=已创建 |
| 20000-29999 | 客户端错误 | 20001=参数错误 |
| 30000-39999 | 认证错误 | 30001=未认证, 30002=无权限 |
| 40000-49999 | 资源错误 | 40001=未找到 |
| 50000+ | 服务端错误 | 50001=内部错误 |

### 使用方式

```python
from core.infra.responses import success_response, created_response, paginated_response, error_response

# 成功
return success_response(data={"id": "123"})

# 创建
return created_response(data={"id": "new_123"})

# 分页
return paginated_response(items=items_list, page=1, page_size=20, total_items=100)

# 错误（在异常中使用，不直接在路由中返回）
raise ResourceNotFoundError("chat", chat_id)
raise BadRequestError("参数无效")
```

---

## 4. 数据库模型规范

### 4.1 ORM 模型模板

```python
from sqlalchemy import Column, String, Integer, Boolean, Text, TIMESTAMP, ForeignKey, Index, CheckConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from core.db.engine import Base
from datetime import datetime

class MyModel(Base):
    __tablename__ = "my_table"

    # 主键
    id = Column(String(64), primary_key=True)

    # 外键
    user_id = Column(
        String(64),
        ForeignKey("users_shadow.user_id", ondelete="CASCADE"),
        nullable=False,
    )

    # 常规字段
    title = Column(String(500), nullable=False, default="默认标题")
    content = Column(Text)
    count = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)

    # JSONB 灵活字段
    extra_data = Column("metadata", JSONB, default={})

    # 软删除
    deleted_at = Column(TIMESTAMP(timezone=True))

    # 时间戳（必须包含）
    created_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow)
    updated_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    # 关系
    user = relationship("UserShadow", back_populates="my_models")

    # 索引和约束
    __table_args__ = (
        CheckConstraint("count >= 0", name="my_table_count_check"),
        Index("idx_my_table_user_id", "user_id"),
        Index("idx_my_table_updated_at", "updated_at"),
    )
```

### 4.2 模型规则

- 时间字段统一用 `TIMESTAMP(timezone=True)`，默认 `datetime.utcnow`
- 使用软删除 (`deleted_at`) 而非物理删除
- 灵活数据用 `JSONB` 列
- 外键必须指定 `ondelete="CASCADE"`
- 必须添加合适的索引
- 模型定义在 `core/db/models/` **包**内的对应领域文件（admin / agent / artifact / automation / chat / config / identity / knowledge / logs / memory / project），并在包 `__init__.py` re-export
- 新模型必须创建 Alembic 迁移：`alembic revision --autogenerate -m "描述"`

---

## 5. Repository 模式

```python
from typing import Optional, List, Tuple, Dict, Any
from sqlalchemy.orm import Session
from sqlalchemy import desc

class MyModelRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_by_id(self, id: str) -> Optional[MyModel]:
        return self.db.query(MyModel).filter(
            MyModel.id == id,
            MyModel.deleted_at.is_(None),  # 尊重软删除
        ).first()

    def list_by_user(self, user_id: str, page: int = 1, page_size: int = 20) -> Tuple[List[MyModel], int]:
        query = self.db.query(MyModel).filter(
            MyModel.user_id == user_id,
            MyModel.deleted_at.is_(None),
        )
        total = query.count()
        items = query.order_by(desc(MyModel.updated_at)).offset((page - 1) * page_size).limit(page_size).all()
        return items, total

    def create(self, data: Dict[str, Any]) -> MyModel:
        item = MyModel(**data)
        self.db.add(item)
        self.db.commit()
        self.db.refresh(item)
        return item

    def update(self, id: str, data: Dict[str, Any]) -> Optional[MyModel]:
        item = self.get_by_id(id)
        if not item:
            return None
        for key, value in data.items():
            setattr(item, key, value)
        self.db.commit()
        self.db.refresh(item)
        return item

    def soft_delete(self, id: str) -> bool:
        item = self.get_by_id(id)
        if not item:
            return False
        item.deleted_at = datetime.utcnow()
        self.db.commit()
        return True
```

**规则：** 每个领域实体一个 Repository，放在 `core/db/repository/` **包**内的对应领域文件（agent / artifact / audit / catalog / chat / kb / team / user），查询必须过滤 `deleted_at.is_(None)`。

---

## 6. Service 层规范

```python
class MyService:
    def __init__(self, db: Session):
        self.db = db
        self.repo = MyModelRepository(db)

    def create(self, user_id: str, title: str, metadata: dict = None) -> MyModel:
        # 1. 业务验证
        # 2. 调用 Repository
        item = self.repo.create({
            "id": f"item_{uuid.uuid4().hex[:16]}",
            "user_id": user_id,
            "title": title,
            "extra_data": metadata or {},
        })
        # 3. 审计日志（如需要）
        return item

    def ensure_item(self, id: str, user_id: str) -> Optional[MyModel]:
        """幂等操作：存在则返回，不存在则创建。"""
        existing = self.repo.get_by_id(id)
        if existing:
            if existing.user_id != user_id:
                return None  # 权限不匹配
            return existing
        return self.create(user_id=user_id, ...)
```

**规则：**
- 一个业务领域一个 Service
- 构造函数接收 `Session`，内部创建 Repository
- 业务逻辑在 Service 层，不在 Route 层
- 权限校验在 Service 层
- ORM → dict 转换在 Route 层的辅助函数中

---

## 7. 异常处理规范

```python
from core.infra.exceptions import AppException, BadRequestError, ResourceNotFoundError, AuthenticationError

# 在业务逻辑中抛出
raise BadRequestError("参数 name 不能为空")
raise ResourceNotFoundError("chat_session", chat_id)
raise AuthenticationError("Token 已过期")

# 自定义异常
class QuotaExceededError(AppException):
    def __init__(self, message: str = "配额已用完"):
        super().__init__(code=20010, message=message, status_code=429)
```

**规则：** 不要在路由中直接返回错误响应，统一通过抛异常 → 全局 handler 转换为信封格式。

---

## 8. 依赖注入

```python
from api.deps import get_current_user, get_db

# 普通端点
@router.get("")
async def list_items(
    user: UserContext = Depends(get_current_user),  # 用户认证
    db: Session = Depends(get_db),                   # DB Session
):
    ...
```

---

## 9. 配置管理

```python
from core.config.settings import settings

# 读取配置（frozen dataclass，启动时从环境变量加载）
auth_mode = settings.auth.mode          # AUTH_MODE
db_url = settings.db.url                # DATABASE_URL
is_prod = settings.server.is_prod       # IS_PROD

# 新增配置字段：在 core/config/settings.py 对应的 dataclass 中添加
@dataclass(frozen=True)
class MySettings:
    my_flag: bool = _bool(os.getenv("MY_FLAG", "false"))
    my_url: str = os.getenv("MY_URL", "")
```

---

## 10. Pydantic 模型规范

```python
from pydantic import BaseModel, Field, field_validator

class MyRequest(BaseModel):
    # 必填字段用 ...
    name: str = Field(..., description="名称", min_length=1, max_length=200)

    # 可选字段用 Optional + default
    desc: Optional[str] = Field(None, description="描述")

    # 可变默认值用 default_factory
    tags: List[str] = Field(default_factory=list, description="标签")

    # 字段验证器
    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        return v.strip()
```

---

## 11. 测试规范

```python
# 文件命名：test_*.py，放在 src/backend/tests/
# 运行：PYTHONPATH=src/backend pytest src/backend/tests/test_xxx.py -v

import pytest
from fastapi.testclient import TestClient

@pytest.fixture
def db_session():
    # 使用 SQLite 内存数据库
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()

def test_create_item(db_session):
    service = MyService(db_session)
    item = service.create(user_id="test_user", title="Test")
    assert item.title == "Test"
    assert item.user_id == "test_user"
```

---

## 12. 代码风格

- **格式化**: Black（100 字符行宽）+ isort（black profile）约定；但**存量代码并非 formatter-clean，不要对已有文件整体跑 black/isort**（会淹没真实 diff），改动处手工对齐风格即可
- **类型检查**: mypy（permissive mode）
- **命令**: `make format` / `make lint` / `make type-check`（format 仅用于全新文件）
- **import 顺序**: 标准库 → 第三方 → 项目内部（isort 自动排序）
- **函数命名**: snake_case
- **类命名**: PascalCase
- **常量**: UPPER_SNAKE_CASE

---

## 13. Docker 开发流程

```bash
# 修改后端代码后重建
docker-compose up -d --build backend

# 前后端同时修改
docker-compose up -d --build backend frontend

# 依赖变更时强制重建
docker-compose build --no-cache backend
docker-compose up -d backend

# 查看日志
docker-compose logs -f backend
```

---

## 14. 数据库迁移

```bash
# 创建迁移
alembic revision --autogenerate -m "add xxx table"

# 应用迁移
alembic upgrade head

# 回滚一步
alembic downgrade -1
```

---

## 15. 新功能开发检查清单

- [ ] ORM 模型定义在 `core/db/models/` 包的对应领域文件，包含时间戳和索引
- [ ] Repository 在 `core/db/repository/` 包的对应领域文件，过滤软删除
- [ ] Service 在 `core/services/`，包含业务逻辑和权限校验
- [ ] Pydantic 请求模型在路由文件或 `api/schemas.py`
- [ ] 路由在 `api/routes/v1/`，使用信封响应
- [ ] 路由已注册进 `api/routes/v1/__init__.py` 的 CE_ROUTERS 注册表
- [ ] 使用 Depends 进行认证和 DB 注入
- [ ] 异常通过 `core/infra/exceptions.py` 抛出
- [ ] Alembic 迁移已创建
- [ ] 测试已编写
- [ ] 代码风格与所在文件一致（不要对存量文件整体跑 black/isort）
