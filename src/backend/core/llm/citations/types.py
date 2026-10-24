"""Citation item and per-tool extraction spec types."""
from __future__ import annotations
from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional, Tuple

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
