"""get_data_context tool — on-demand recall of "data dictionary + golden SQL" before data retrieval.

The sole injection vehicle for improving "direct database query" accuracy: the
model proactively calls this tool before writing SQL, getting back relevant
table/column business names + descriptions + enum dictionaries + sample values
+ verified query examples, then writes the SELECT based on them.
**Metadata never enters the system prompt** — it only appears as this tool's
return value; the model sees it only by calling the tool.

agent_factory registers this tool only when there exists a data source that is
"direct-connect (not external_nl2sql) + enabled + annotated" (see the mounting
gate in core/llm/agent_factory.py).
"""

import logging
from typing import List, Optional

from agentscope.message import TextBlock
from agentscope.tool import Toolkit
from agentscope.tool._response import ToolChunk as ToolResponse

logger = logging.getLogger(__name__)

# Guardrail on the number of data sources returned per call (concatenating all of them would be too large with many sources).
_MAX_SOURCES_PER_CALL = 4


def register_get_data_context(toolkit: Toolkit, datasource_ids: List[str]) -> None:
    """Register the get_data_context tool, scoped to the given set of available data sources."""

    scoped = list(datasource_ids)

    async def get_data_context(question: str, datasource: Optional[str] = None) -> ToolResponse:
        """获取数据库的「数据字典 + 已验证查询范例」，用于准确取数。

        ⚠️ **使用时机（强烈建议先调用本工具再写 SQL）**：当你要用「数据库直连查询」
        （execute_sql / search_objects）从业务库取数，而该库字段名晦涩、取值是编码
        （如 status=1）、或表很多不确定选哪张时，**先调用本工具**拿到人工标注的表/列
        业务含义、枚举字典（如 1=已审核）、外键关联和已验证的「问题→SQL」范例，再据此
        写 SELECT，可显著降低取数出错。

        Args:
            question (`str`):
                你要回答的取数问题（自然语言）。用于召回最相关的表/字段/范例。
            datasource (`str`, optional):
                指定数据源 id（仅当你已知道要查哪个库时填）。不填则返回全部可用
                数据源的字典摘要。

        Returns:
            数据字典与黄金 SQL 的 Markdown 文本；若该库尚无标注则返回提示。
            注意：返回内容是人工治理的业务口径，应优先据此选表/字段、解码枚举值。
        """
        from core.services import db_metadata_service as svc

        if not scoped:
            return _text("当前没有可用的数据字典（未配置元数据治理）。可直接用 "
                         "search_objects 探查表结构后再写 SQL。")

        targets: List[str]
        ds = (datasource or "").strip()
        if ds:
            if ds in scoped:
                targets = [ds]
            else:
                return _text(
                    f"数据源 `{ds}` 无可用数据字典。可用的数据源：{', '.join(scoped)}。"
                    "（不填 datasource 可返回全部）"
                )
        else:
            targets = scoped[:_MAX_SOURCES_PER_CALL]

        parts: List[str] = []
        for ds_id in targets:
            try:
                digest = svc.build_digest(ds_id, question=question)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[get_data_context] build_digest failed for %s: %s", ds_id, exc)
                digest = ""
            if digest:
                parts.append(digest)

        if not parts:
            return _text("相关数据源暂无已标注的数据字典内容。可用 search_objects 探查表结构。")

        text = "\n\n---\n\n".join(parts)
        if len(scoped) > len(targets):
            text += (f"\n\n（另有 {len(scoped) - len(targets)} 个数据源未展开；"
                     "如需指定，请用 datasource 参数。可用：" + ", ".join(scoped) + "）")
        return _text(text)

    toolkit.register_tool_function(get_data_context, namesake_strategy="override")
    logger.info("[factory] Registered get_data_context tool (datasources=%s)", scoped)


def _text(s: str) -> ToolResponse:
    return ToolResponse(content=[TextBlock(type="text", text=s)])
