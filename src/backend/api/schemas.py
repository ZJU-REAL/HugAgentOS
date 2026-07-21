"""API request/response models."""

from typing import Any, Dict, List, Literal, Optional

from core.config.settings import DEFAULT_CHAT_MODEL_ALIAS
from pydantic import BaseModel, ConfigDict, Field, field_validator

ChatMode = Literal["fast", "medium", "high", "max"]


class AttachmentItem(BaseModel):
    """单个文件附件"""

    name: str = Field(..., description="文件名")
    content: str = Field("", description="文件文本内容（供模型读取）")
    mime_type: str = Field("", description="MIME 类型")
    file_id: str = Field("", description="OSS 持久化后的文件 ID（供下载）")
    download_url: str = Field("", description="下载路径，如 /files/{file_id}")


class QuotedFollowUpItem(BaseModel):
    """追问引用信息"""

    text: str = Field(..., description="被引用的原始文本", min_length=1, max_length=8000)
    ts: Optional[int] = Field(None, description="前端消息时间戳（可选）")

    @field_validator("text")
    @classmethod
    def validate_text(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("Quoted text cannot be empty")
        return v.strip()


class ChatRequest(BaseModel):
    """聊天请求模型"""

    model_config = ConfigDict(protected_namespaces=())

    chat_id: str = Field(..., description="会话ID，用于维持对话上下文", max_length=100)
    message: str = Field(..., description="用户消息内容", min_length=1, max_length=10000)
    model_name: Optional[str] = Field(
        DEFAULT_CHAT_MODEL_ALIAS, description="使用的模型名称（qwen/deepseek）", max_length=50
    )
    model_provider_id: Optional[str] = Field(
        default=None,
        description="用户端模型切换选择的模型供应商 ID；仅在后台开关开启且供应商为 active chat 时生效",
        max_length=64,
    )
    chat_mode: Optional[ChatMode] = Field(
        default=None,
        description=(
            "对话模式：fast=快速、medium=思考·中、high=思考·高、max=思考·超高。"
            "为 None 时按 enable_thinking 兜底（true→medium、false→fast）。"
            "high/max 仅对 supports_reasoning_effort=true 的模型有效，否则后端自动回落到 medium。"
        ),
    )
    attachments: List[AttachmentItem] = Field(
        default_factory=list,
        description="上传的文件附件列表",
    )
    enabled_kbs: Optional[List[str]] = Field(
        default=None,
        description="当前会话中启用的知识库 ID 列表（前端运行时注入）",
    )
    enabled_skills: Optional[List[str]] = Field(
        default=None,
        description="本次请求启用的 skill ID 列表（不传则使用用户/系统默认配置）",
    )
    enabled_mcps: Optional[List[str]] = Field(
        default=None,
        description="本次请求启用的 MCP 工具 ID 列表（不传则使用用户/系统默认配置）",
    )
    enabled_agents: Optional[List[str]] = Field(
        default=None,
        description="本次请求启用的子智能体 ID 列表（不传则使用用户/系统默认配置）",
    )
    agent_id: Optional[str] = Field(
        default=None,
        description="子智能体 ID，传入时使用该智能体配置对话（不传则使用主智能体）",
        max_length=64,
    )
    mention_agent_id: Optional[str] = Field(
        default=None,
        description=(
            "本轮通过 @ 显式指定的子智能体 ID。传入时主智能体保留正常思考与流式输出，"
            "并通过真实 call_subagent 工具把任务交给该子智能体；不会永久绑定整个会话。"
        ),
        max_length=64,
    )
    plan_chat: bool = Field(
        default=False,
        description="是否为计划模式对话（从应用中心入口创建的对话）",
    )
    batch_chat: bool = Field(
        default=False,
        description=(
            "是否为批量执行模式对话（从应用中心『批量执行』入口创建的对话）。"
            "为 True 时强制启用 batch_runner MCP，并在系统提示中提示模型优先调用 batch_plan。"
        ),
    )
    disable_batch_plan: bool = Field(
        default=False,
        description=(
            "禁用 batch_plan 工具。用户在批量执行确认弹窗里点取消后，"
            "前端再次发起本次对话时会带上此标志，让 LLM 用其它工具普通回答。"
        ),
    )
    skill_id: Optional[str] = Field(
        default=None,
        description="显式调用的技能 ID（斜杠命令选择）",
        max_length=64,
    )
    skill_name: Optional[str] = Field(
        default=None,
        description="显式调用的技能名（仅用于会话记录回显徽标，不参与路由）",
        max_length=255,
    )
    mention_name: Optional[str] = Field(
        default=None,
        description=(
            "@ 引用的子智能体名，用于会话记录回显徽标；旧客户端未传 "
            "mention_agent_id 时，后端会按唯一可访问名称解析委派目标。"
        ),
        max_length=255,
    )
    skill_ids: Optional[List[str]] = Field(
        default=None,
        description="显式调用的技能 ID 列表（显式引用插件时由其包含技能展开而来）",
    )
    mcp_ids: Optional[List[str]] = Field(
        default=None,
        description="显式引用插件时一并激活的 MCP server ID 列表（本轮 force-enable 进工具集）",
    )
    plugin_name: Optional[str] = Field(
        default=None,
        description="显式引用的插件名（用于注入提示，可选）",
        max_length=255,
    )
    quoted_follow_up: Optional[QuotedFollowUpItem] = Field(
        default=None,
        description="追问场景下引用的原始文本，用于增强上下文理解",
    )
    project_id: Optional[str] = Field(
        default=None,
        description="若挂载于项目（Claude-style 工作空间），传项目 ID。会写入 chat_sessions.project_id；项目 instructions 会注入 system prompt，项目文件会作为可访问范围。",
        max_length=64,
    )

    @field_validator("message")
    @classmethod
    def validate_message(cls, v: str) -> str:
        """Validate message is not just whitespace."""
        if not v or not v.strip():
            raise ValueError("Message cannot be empty or whitespace only")
        return v.strip()

    @field_validator("model_name")
    @classmethod
    def validate_model_name(cls, v: Optional[str]) -> Optional[str]:
        """Validate model name is in allowed list."""
        if v is None:
            return DEFAULT_CHAT_MODEL_ALIAS
        allowed_models = ["qwen", "deepseek", "gpt-4", "claude"]
        if v not in allowed_models:
            raise ValueError(f"Invalid model name. Allowed: {', '.join(allowed_models)}")
        return v

    @field_validator("model_provider_id")
    @classmethod
    def validate_model_provider_id(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        value = v.strip()
        return value or None


class ChatResponse(BaseModel):
    """聊天响应模型"""

    chat_id: str = Field(..., description="会话ID")
    response: str = Field(..., description="AI响应内容")
    timestamp: str = Field(..., description="响应时间戳")
    is_markdown: bool = Field(False, description="响应是否为Markdown格式")
    route: Optional[str] = Field(None, description="路由信息（main/subagent）")
    sources: List[Dict[str, Any]] = Field(default_factory=list, description="数据来源列表")
    artifacts: List[Dict[str, Any]] = Field(default_factory=list, description="生成的附件列表")
    warnings: List[str] = Field(default_factory=list, description="警告信息列表")


class HealthResponse(BaseModel):
    """健康检查响应"""

    status: str
    service: str
    timestamp: str


class KBGrantItem(BaseModel):
    """单条知识库授权项（用户/团队管理页共用）。"""

    resource_id: str = Field(..., max_length=64)
    resource_type: str = Field("local", pattern="^(local|dify)$")
    level: str = Field("view", pattern="^(view|edit|admin)$")


class KBGrantsBody(BaseModel):
    """全量替换语义：提交后该用户/团队的知识库授权 = grants 列表。"""

    grants: List[KBGrantItem] = Field(default_factory=list)


class MarketVisibilityGrantItem(BaseModel):
    """单条市场可见范围授权（三大市场 admin 端共用）。"""

    principal_type: str = Field(..., pattern="^(user|team|role)$")
    principal_id: str = Field(..., min_length=1, max_length=64)


class MarketVisibilityRequest(BaseModel):
    """全量替换语义：public=全员可见（grants 忽略）；scoped=仅 grants 白名单可见。"""

    visibility: str = Field(..., pattern="^(public|scoped)$")
    grants: List[MarketVisibilityGrantItem] = Field(default_factory=list)
