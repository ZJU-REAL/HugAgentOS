"""统一证据锚点（Evidence Anchor）：引用编号的唯一真源。

设计（见 internal design docs）：
- 编号权收归后端 —— ``AnchorAllocator`` 按会话内单调计数发 ``e1、e2、…`` 全局锚点；
  模型只负责把工具结果里标注的 ``cite_id`` 原样复制进 ``[锚文本](cite:eN)``，不做计算。
- ``CitationAnchorMiddleware``（core/llm/middlewares.py）在工具结果回给模型前调用
  :func:`annotate_tool_result` 完成 提取 → 发号 → 回注，并把 :class:`AnchorCitation`
  登记进 allocator；workflow / batch / autonomous 按 tool_id 从 allocator 精确取引用。
  发号器是唯一编号方——旧的 per-tool 偏移提取（orchestration/citations.py）已随本方案删除。
- 提取四层降级：工具自声明 ``__citations__`` → 工具规格注册表（TOOL_SPECS 配置，
  加工具只加配置不改代码）→ 通用启发式（唯一字典数组字段）→ 整份结果 1 个锚点。
  操作型工具（写文件 / pin / 任务增删改等）在 SKIP_TOOLS 里直接放行不发号。

约定（工具开发规范）：需要精确控制引用粒度的工具，返回 JSON 里带
``"__citations__": [{"title": …, "url": …, "snippet": …, "source_type": …}, …]``，
条目顺序与结果正文对应；中间件会就地为每条注入 ``cite_id`` 并登记。
"""

from __future__ import annotations

import json
import logging
import re
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

ANCHOR_RE = re.compile(r"^e(\d+)$")


# ── 引用条目 ────────────────────────────────────────────────────────────────


@dataclass
class AnchorCitation:
    """带全局锚点的引用条目（CitationItem 的超集，dict 形态向后兼容）。"""

    id: str  # 全局锚点，如 "e7"
    tool_name: str
    tool_id: Optional[str]
    title: str
    url: str
    snippet: str
    source_type: str
    item_index: int = -1  # 条目在该次工具结果列表中的 0-based 下标；整体型为 -1

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ── 工具规格注册表（配置即真源；新工具加一行，不改代码） ─────────────────────

# 标题/链接/摘要字段别名（按序取第一个非空；覆盖存量工具的中英文键名）
DEFAULT_TITLE_KEYS: Tuple[str, ...] = (
    "title",
    "标题",
    "文件名称",
    "企业名称",
    "产品名称",
    "name",
    "名称",
    "document_name",
)
DEFAULT_URL_KEYS: Tuple[str, ...] = ("url", "链接", "link", "href")
DEFAULT_SNIPPET_KEYS: Tuple[str, ...] = (
    "content",
    "文件内容",
    "snippet",
    "摘要",
    "summary",
    "description",
    "abstract",
    "text",
)
# 启发式识别列表字段时的候选名（按序优先）
LIST_FIELD_ALIASES: Tuple[str, ...] = (
    "items",
    "results",
    "events",
    "pages",
    "records",
    "entries",
    "data",
    "list",
    "rows",
    "docs",
)

_TITLE_MAX = 120
_SNIPPET_MAX = 3000
_WHOLE_SNIPPET_MAX = 500


@dataclass(frozen=True)
class CitationSpec:
    """单个工具的引用提取规格。mode: list（逐条）/ whole（整份 1 条）。"""

    mode: str = "whole"
    # 列表字段路径（支持多层如 ("result", "results")；支持并列多列表）
    items_paths: Tuple[Tuple[str, ...], ...] = ()
    title_keys: Tuple[str, ...] = DEFAULT_TITLE_KEYS
    url_keys: Tuple[str, ...] = DEFAULT_URL_KEYS
    snippet_keys: Tuple[str, ...] = DEFAULT_SNIPPET_KEYS
    # 摘要拼接键（如企业搜索的 法定代表人·注册资金·企业状态）；命中则优先于 snippet_keys
    snippet_join: Tuple[str, ...] = ()
    source_type: str = "unknown"
    title: str = ""  # whole 模式的显示标题；空则用工具名


# 操作型 / 资产管理型工具：无可引用内容，中间件直接放行
SKIP_TOOLS = frozenset(
    {
        # 自研文件/沙盒操作
        "Write",
        "Edit",
        "Delete",
        "Move",
        "CreateFolder",
        "bash",
        "Bash",
        "sandbox_put_artifact",
        "sandbox_get_artifact",
        "pin_to_workspace",
        "stage_myspace_file",
        "channel_read_attachment",
        "choose_design",
        "ask_user_question",
        "view_text_file",
        "get_data_context",
        "load_skill",
        "Glob",
        "Grep",
        # 产物/发布/批量
        "generate_chart_tool",
        "batch_plan",
        "publish_site",
        # 定时任务写操作
        "create_scheduled_task",
        "update_scheduled_task",
        "pause_scheduled_task",
        "resume_scheduled_task",
        "delete_scheduled_task",
        "list_scheduled_tasks",
        "list_channel_conversations",
        "get_scheduled_task",
        # 技能管理
        "search_marketplace",
        "list_my_skills",
        "install_from_marketplace",
        "register_skill",
        "submit_to_marketplace",
        "delete_skill",
        "edit_skill",
        # 能力/资产发现
        "list_myspace_files",
        "list_favorite_chats",
        "get_chat_messages",
        "list_datasets",
        # 子智能体的引用由其内部工具的嵌套事件承载
        "call_subagent",
    }
)

TOOL_SPECS: Dict[str, CitationSpec] = {
    # 检索/知识类（逐条）
    "internet_search": CitationSpec(
        mode="list",
        items_paths=(("result", "results"), ("results",)),
        source_type="internet",
    ),
    "retrieve_dataset_content": CitationSpec(
        mode="list", items_paths=(("items",),), source_type="knowledge_base"
    ),
    "retrieve_local_kb": CitationSpec(
        mode="list", items_paths=(("items",),), source_type="knowledge_base"
    ),
    "wiki_fetch_source": CitationSpec(
        mode="list", items_paths=(("items",),), source_type="knowledge_base"
    ),
    "wiki_locate": CitationSpec(
        mode="list", items_paths=(("pages",),), source_type="knowledge_base"
    ),
    "wiki_read_page": CitationSpec(mode="whole", title="Wiki 页面", source_type="knowledge_base"),
    "web_fetch": CitationSpec(mode="whole", title="网页内容", source_type="internet"),
    # 数据/报告类（整份）
    "query_database": CitationSpec(mode="whole", title="数据库查询结果", source_type="database"),
}

# ── 发号器（run 级共享，子智能体经 ContextVar 继承同一实例） ────────────────


class AnchorAllocator:
    """会话内单调锚点计数器 + 按 tool_id 的引用注册表。

    并发安全性：分配是同步代码段（无 await 点），asyncio 并行工具任务
    通过 ContextVar 共享同一实例，不会重号。
    """

    def __init__(self, start: int = 1) -> None:
        self._next = max(1, int(start or 1))
        self._by_tool_id: Dict[str, List[AnchorCitation]] = {}
        self.all: List[AnchorCitation] = []

    def next_id(self) -> str:
        aid = f"e{self._next}"
        self._next += 1
        return aid

    def register(self, tool_id: Optional[str], items: Sequence[AnchorCitation]) -> None:
        if not items:
            return
        self.all.extend(items)
        if tool_id:
            self._by_tool_id.setdefault(str(tool_id), []).extend(items)

    def citations_for(self, tool_id: Optional[str]) -> List[AnchorCitation]:
        if not tool_id:
            return []
        return list(self._by_tool_id.get(str(tool_id), []))


# 子智能体链路的兜底通道：call_subagent 的工具函数与父 agent 在同一 task 调用链，
# 能读到父中间件写入的实例。**不作为主通道**——`astream_chat_workflow` 是 async
# generator，其 `set()` 与 agent 实际执行所在的 task 上下文不互通（实测：中间件
# 侧新建了自己的实例、编排层侧读到 None 回退旧提取）。主通道见 `_ALLOC_ATTR`。
CITATION_ALLOCATOR: ContextVar[Optional[AnchorAllocator]] = ContextVar(
    "jx_citation_allocator", default=None
)

# 主通道：把发号器挂在 agent 实例上——中间件的 on_acting 拿得到 agent，
# 编排层持有 create_agent_executor 返回的同一个 agent，天然同源、不受上下文隔离影响。
_ALLOC_ATTR = "_jx_citation_allocator"


def attach_allocator(agent: Any, allocator: AnchorAllocator) -> AnchorAllocator:
    """run 入口调用：把发号器绑定到 agent（并写 ContextVar 供子链路兜底）。"""
    try:
        setattr(agent, _ALLOC_ATTR, allocator)
    except Exception as exc:  # noqa: BLE001 - pydantic 模型等禁止动态属性时降级到 ContextVar
        logger.debug("[citation-anchor] attach to agent failed: %s", exc)
    CITATION_ALLOCATOR.set(allocator)
    return allocator


def resolve_allocator(agent: Any) -> AnchorAllocator:
    """中间件调用：取本 run 的发号器；缺失时就地建一个并绑定，保证始终可用。"""
    allocator = getattr(agent, _ALLOC_ATTR, None)
    if isinstance(allocator, AnchorAllocator):
        return allocator
    allocator = CITATION_ALLOCATOR.get()
    if not isinstance(allocator, AnchorAllocator):
        allocator = AnchorAllocator()
    return attach_allocator(agent, allocator)


def collect_citation_dicts(
    tool_id: Optional[str],
    allocator: Optional[AnchorAllocator] = None,
) -> List[Dict[str, Any]]:
    """编排层统一取引用入口（dict 形态，直接可进 SSE / trace / 落库）。

    发号器是**唯一**编号方：中间件在结果回给模型前已完成注号并按 tool_id 登记，
    这里只做精确取回，不做二次提取。``allocator`` 由调用方显式传入（run 入口绑定
    在 agent 上的那一个）；不传时退到 ContextVar（子智能体链路）。
    """
    if allocator is None:
        allocator = CITATION_ALLOCATOR.get()
    if allocator is None:
        return []
    return [item.to_dict() for item in allocator.citations_for(tool_id)]


def anchor_start_for_chat(chat_id: Optional[str]) -> int:
    """跨轮续号：扫描该会话已落库消息的 citations，取最大锚点 + 1。

    任何异常都回退 1（引用功能降级，不影响对话）。
    """
    if not chat_id:
        return 1
    try:
        from core.db.engine import SessionLocal
        from core.db.models import ChatMessage

        max_seen = 0
        with SessionLocal() as db:
            rows = (
                db.query(ChatMessage.extra_data)
                .filter(ChatMessage.chat_id == chat_id)
                .order_by(ChatMessage.chat_seq.desc())
                .limit(200)
                .all()
            )
        for (extra,) in rows:
            for cit in (extra or {}).get("citations") or []:
                m = ANCHOR_RE.match(str(cit.get("id", "")) if isinstance(cit, dict) else "")
                if m:
                    max_seen = max(max_seen, int(m.group(1)))
        return max_seen + 1
    except Exception as exc:  # noqa: BLE001
        logger.debug("[citation-anchor] start scan failed for chat %s: %s", chat_id, exc)
        return 1


# ── 提取 + 回注 ─────────────────────────────────────────────────────────────


def _first_str(item: Dict[str, Any], keys: Sequence[str]) -> str:
    for key in keys:
        val = item.get(key)
        if isinstance(val, (str, int, float)) and str(val).strip():
            return str(val)
    return ""


def _normalize_citation_url(raw: str) -> str:
    """把工具结果里的来源链接规整成「浏览器点了不会跑偏」的绝对地址。

    检索工具返回的 url 千奇百怪，直接透传会有两类坏结果：

    - **缺协议**（``www.example.gov.cn/a.html``）：前端 ``<a href>`` 会把它当相对
      路径，按当前站点 origin 解析，于是点开变成 ``http://<本平台>/www.example…``，
      落到我们自己的 nginx 上返回 404 —— 看起来像「引用地址错误」。
    - **纯相对路径**（``/module/download/x.doc``）：没有站点根就无从还原，同样会
      指到本平台。这种宁可不给链接，也好过给一个必然打不开的。

    另外只放行 http/https：javascript:、data: 之类既没有意义也不该出现在来源里。
    """
    url = (raw or "").strip()
    if not url:
        return ""
    lowered = url.lower()
    if lowered.startswith(("http://", "https://")):
        return url
    # 协议相对：//example.com/a → https://example.com/a
    if url.startswith("//"):
        return f"https:{url}"
    # 明确的其它协议（javascript:/data:/ftp: …）一律丢弃
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:", url):
        return ""
    # 相对路径：没有站点根，还原不出来
    if url.startswith("/"):
        return ""
    # 剩下的形如 example.gov.cn/path —— 看着像域名就补上协议
    host = url.split("/", 1)[0]
    if "." in host and " " not in host:
        return f"https://{url}"
    return ""


def _dig(data: Dict[str, Any], path: Sequence[str]) -> Any:
    cur: Any = data
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _display_title(tool_name: str, spec: Optional[CitationSpec]) -> str:
    if spec and spec.title:
        return spec.title
    return f"{tool_name} 结果"


def _item_citation(
    aid: str,
    tool_name: str,
    tool_id: Optional[str],
    spec: CitationSpec,
    item: Dict[str, Any],
    index: int,
) -> AnchorCitation:
    title = _first_str(item, spec.title_keys)[:_TITLE_MAX] or _display_title(tool_name, spec)
    if spec.snippet_join:
        parts = [_first_str(item, (k,)) for k in spec.snippet_join]
        snippet = " · ".join(p for p in parts if p)
    else:
        snippet = _first_str(item, spec.snippet_keys)
    return AnchorCitation(
        id=aid,
        tool_name=tool_name,
        tool_id=tool_id,
        title=title,
        url=_normalize_citation_url(_first_str(item, spec.url_keys)),
        snippet=snippet[:_SNIPPET_MAX],
        source_type=spec.source_type,
        item_index=index,
    )


def _looks_like_error(data: Dict[str, Any]) -> bool:
    if not data.get("error"):
        return False
    # 列表字段仍有内容时不算整体失败（部分工具 error+items 并存）
    for key in LIST_FIELD_ALIASES:
        val = data.get(key)
        if isinstance(val, list) and val:
            return False
    return True


def _heuristic_list_field(data: Dict[str, Any]) -> Optional[Tuple[Tuple[str, ...], List[Any]]]:
    """启发式找列表字段：别名优先；否则要求「唯一」的字典数组字段。"""
    scopes: List[Tuple[Tuple[str, ...], Dict[str, Any]]] = [((), data)]
    inner = data.get("result")
    if isinstance(inner, dict):
        scopes.append((("result",), inner))
    for prefix, scope in scopes:
        for alias in LIST_FIELD_ALIASES:
            val = scope.get(alias)
            if isinstance(val, list) and val and all(isinstance(x, dict) for x in val):
                return prefix + (alias,), val
    for prefix, scope in scopes:
        candidates = [
            (key, val)
            for key, val in scope.items()
            if isinstance(val, list) and val and all(isinstance(x, dict) for x in val)
        ]
        if len(candidates) == 1:
            key, val = candidates[0]
            return prefix + (key,), val
    return None


def _extract_and_inject(
    tool_name: str,
    tool_id: Optional[str],
    data: Dict[str, Any],
    allocator: AnchorAllocator,
) -> List[AnchorCitation]:
    """在 data 上就地注入 cite_id，返回登记的引用条目。"""
    spec = TOOL_SPECS.get(tool_name)

    # L1：工具自声明 __citations__（优先级最高，条目顺序即 item_index）
    declared = data.get("__citations__")
    if isinstance(declared, list) and declared and all(isinstance(x, dict) for x in declared):
        base = spec or CitationSpec(source_type="unknown")
        out: List[AnchorCitation] = []
        for idx, entry in enumerate(declared):
            aid = allocator.next_id()
            entry["cite_id"] = aid
            out.append(
                AnchorCitation(
                    id=aid,
                    tool_name=tool_name,
                    tool_id=tool_id,
                    title=str(entry.get("title") or _display_title(tool_name, spec))[:_TITLE_MAX],
                    url=_normalize_citation_url(str(entry.get("url") or "")),
                    snippet=str(entry.get("snippet") or "")[:_SNIPPET_MAX],
                    source_type=str(entry.get("source_type") or base.source_type),
                    item_index=idx,
                )
            )
        return out

    if _looks_like_error(data):
        return []

    # L2：配置映射（列表型）
    if spec and spec.mode == "list":
        out = []
        for path in spec.items_paths:
            items = _dig(data, path)
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict) or item.get("error"):
                    continue
                aid = allocator.next_id()
                item["cite_id"] = aid
                out.append(_item_citation(aid, tool_name, tool_id, spec, item, len(out)))
        return out

    # L3：无 spec → 启发式找唯一字典数组
    if spec is None:
        found = _heuristic_list_field(data)
        if found is not None:
            _path, items = found
            generic = CitationSpec(mode="list", source_type="unknown")
            out = []
            for item in items:
                if item.get("error"):
                    continue
                aid = allocator.next_id()
                item["cite_id"] = aid
                out.append(_item_citation(aid, tool_name, tool_id, generic, item, len(out)))
            if out:
                return out

    # L4：整份 1 条（显式 whole spec 或彻底认不出）
    aid = allocator.next_id()
    data["cite_id"] = aid
    snippet = json.dumps(data, ensure_ascii=False)[:_WHOLE_SNIPPET_MAX]
    return [
        AnchorCitation(
            id=aid,
            tool_name=tool_name,
            tool_id=tool_id,
            title=_display_title(tool_name, spec),
            url=_normalize_citation_url(_first_str(data, (spec or CitationSpec()).url_keys)),
            snippet=snippet,
            source_type=(spec.source_type if spec else "unknown"),
            item_index=-1,
        )
    ]


def annotate_tool_result(
    tool_name: str,
    tool_id: Optional[str],
    text: str,
    allocator: AnchorAllocator,
) -> Tuple[str, List[AnchorCitation]]:
    """给一份工具结果文本注号。

    返回 (注号后的文本, 登记的引用条目)。不可注号（跳过名单 / 空结果 /
    解析失败且判定无引用价值）时原样返回 (text, [])。**绝不抛错**。
    """
    if not text or tool_name in SKIP_TOOLS:
        return text, []
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        parsed = None

    try:
        if isinstance(parsed, dict):
            items = _extract_and_inject(tool_name, tool_id, parsed, allocator)
            if not items:
                return text, []
            return json.dumps(parsed, ensure_ascii=False), items
        if isinstance(parsed, list):
            # 裸数组结果：包壳后按启发式处理，回注仍输出裸数组语义（保持兼容 → 整份 1 条）
            parsed = {"items": parsed}
            items = _extract_and_inject(tool_name, tool_id, parsed, allocator)
            if not items:
                return text, []
            return json.dumps(parsed["items"], ensure_ascii=False), items
        # 纯文本结果：整份 1 条，文末追加锚点行
        spec = TOOL_SPECS.get(tool_name)
        if spec and spec.mode == "list":
            # 声明为列表型但拿到纯文本 → 形态异常，放行不注号
            return text, []
        aid = allocator.next_id()
        item = AnchorCitation(
            id=aid,
            tool_name=tool_name,
            tool_id=tool_id,
            title=_display_title(tool_name, spec),
            url="",
            snippet=str(text)[:_SNIPPET_MAX],
            source_type=(spec.source_type if spec else "unknown"),
            item_index=-1,
        )
        return f"{text}\n\n[cite_id: {aid}]", [item]
    except Exception as exc:  # noqa: BLE001
        logger.warning("[citation-anchor] annotate failed tool=%s: %s", tool_name, exc)
        return text, []
