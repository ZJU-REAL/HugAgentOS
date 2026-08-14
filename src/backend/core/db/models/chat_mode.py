"""对话模式（chat mode）表。

一个「模式」= 一段对话的**装配契约**：这段对话能用哪些 MCP 工具 / 技能 / 插件、
配哪段专属提示词、默认想多深。它比"这一轮要不要多思考"高一层，所以在前端摆在
输入框外面常驻（见 ``components/chat/ChatModeSwitch.tsx``）。

两类行，靠 ``owner_user_id`` 区分：

* **官方模式**（``owner_user_id IS NULL``）——全员可见，只有 Config 台能改。内置的
  「标准模式」「极速模式」是其中两条，``is_builtin`` 置位、不可删。
* **私有模式**（``owner_user_id`` = 建者）——只有本人能看见能用。

两类模式的提示词都写在 ``prompt_text`` 上。``prompt_kind`` 只留给内置的极速模式：
它的正文历史上就在提示词版本池的 ``turbo`` 里，继续读那儿以保持兼容。**新模式不要
用 kind**——版本池的 kind 是前端写死的一组 tab，加不了新项，靠它扩展会走不通。

历史包袱：极速模式原来存在 SystemConfig 的 ``turbo.*`` 键上，由
``alembic`` 迁移一次性搬进本表的内置行，之后 ``turbo.*`` 不再是真源。
"""

from datetime import datetime

from core.db.engine import Base
from sqlalchemy import (
    JSON,
    TIMESTAMP,
    Boolean,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB

JSONType = JSON().with_variant(JSONB(), "postgresql")

#: 工具面范围。``all`` = 不额外收窄（标准模式：跟随 catalog 与用户自身授权）；
#: ``restricted`` = 只装配本行显式列出的 MCP / 技能 / 插件（极速模式与自定义模式）。
TOOL_SCOPE_ALL = "all"
TOOL_SCOPE_RESTRICTED = "restricted"

#: 思考强度档位；与前端 ``ThinkingEffort`` 同集合。
EFFORT_VALUES = ("fast", "medium", "high", "max")


class ChatMode(Base):
    __tablename__ = "chat_modes"

    id = Column(String(64), primary_key=True)
    #: 落到线上 ``mode_slug`` 字段的值；官方模式全局唯一，私有模式在人内唯一。
    slug = Column(String(64), nullable=False)
    name = Column(String(100), nullable=False)
    description = Column(String(500), default="")

    #: NULL = 官方模式（全员可见）；非空 = 该用户的私有模式。
    owner_user_id = Column(
        String(64),
        ForeignKey("users_shadow.user_id", ondelete="CASCADE"),
        nullable=True,
    )
    #: 内置行（标准 / 极速）：可改配置，不可删、不可改 slug。
    is_builtin = Column(Boolean, nullable=False, default=False)
    enabled = Column(Boolean, nullable=False, default=True)
    sort_order = Column(Integer, nullable=False, default=100)

    # ── 工具面 ──
    tool_scope = Column(String(16), nullable=False, default=TOOL_SCOPE_RESTRICTED)
    mcp_server_ids = Column(JSONType, nullable=False, default=list)
    skill_ids = Column(JSONType, nullable=False, default=list)
    plugin_ids = Column(JSONType, nullable=False, default=list)
    #: 这个模式下可入场的子智能体（``UserAgent.agent_id``）。收窄模式下只有这批
    #: （加上本轮被显式 @ 的那个）能被委派；标准模式不收窄。
    agent_ids = Column(JSONType, nullable=False, default=list)
    #: 是否允许用户本轮显式呼唤（/技能、@子智能体、插件）临时装配到这个模式里。
    manual_invoke_enabled = Column(Boolean, nullable=False, default=True)
    #: 收窄模式（restricted）下是否保留沙箱代码执行与文件工具（bash / 读写文件 /
    #: 产物进出等原生内置工具）。历史上"收窄=一律无代码"是极速模式的私货，泛化成
    #: 模式表后要让建模式的人自己选。``all`` 作用域不看这个位（本就全量装配，仍受
    #: 全局 ``sandbox.code_capability_enable`` 总开关约束）。
    code_exec_enabled = Column(Boolean, nullable=False, default=False)
    #: ReAct 迭代硬上限；NULL = 不额外收紧，走全局默认。
    max_iters = Column(Integer, nullable=True)

    # ── 思考策略 ──
    #: 选中该模式时右侧思考强度位跟随切到这一档。
    default_effort = Column(String(16), nullable=False, default="fast")
    #: 锁死则用户改不了强度（极速模式即如此：那条链路本就不思考）。
    effort_locked = Column(Boolean, nullable=False, default=False)

    # ── 提示词 ──
    #: 遗留字段：只有内置极速模式用它（值 ``turbo``），指向提示词版本池里那段正文。
    #: 版本池的 kind 是前端写死的枚举，新模式扩不了，所以新建的一律走 prompt_text。
    prompt_kind = Column(String(64), nullable=True)
    #: 这个模式的专属提示词正文。非空时优先于 prompt_kind。
    prompt_text = Column(Text, nullable=True)

    deleted_at = Column(TIMESTAMP(timezone=True))
    created_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow)
    updated_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        # 官方模式（owner NULL）之间 slug 唯一；私有模式在人内唯一。两条约束都带上
        # owner，NULL 在唯一索引里各自独立，所以官方那条另用部分索引保证。
        UniqueConstraint("owner_user_id", "slug", name="uq_chat_modes_owner_slug"),
        CheckConstraint(
            "tool_scope IN ('all', 'restricted')", name="ck_chat_modes_tool_scope"
        ),
        CheckConstraint(
            "default_effort IN ('fast', 'medium', 'high', 'max')",
            name="ck_chat_modes_effort",
        ),
        Index("idx_chat_modes_owner", "owner_user_id"),
        Index("idx_chat_modes_sort", "sort_order"),
    )
